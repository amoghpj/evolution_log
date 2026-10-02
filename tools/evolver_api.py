#!/usr/bin/env python3
"""Read-only JSON API over the eVOLVER's own on-disk data.

Drop this next to dashboard.py and register it:

    import evolver_api
    evolver_api.register(app.server, get_data)

That is the whole change to dashboard.py — two lines, nothing existing moves.

WHY A SUMMARY AND NOT THE RAW LOG
The pump log grows without bound; a viewer polling every few seconds does not
want it and should not aggregate it in the browser. What a viewer actually
needs is about ten numbers per vial, a payload whose size is fixed by the
number of vials rather than by how long the run has been going.

The raw files stay exactly where they are. Nothing here writes to disk, and
nothing here belongs in evolution_log.json: this is live state, not a record
of decisions. The join key between the two is (evolver, vial), which the log
already carries on every line.

TIME
The controller counts elapsed hours from its own experiment start. Rather than
requiring that start to be configured anywhere, the payload reports both
`generated_at` (wall clock) and `elapsed_h` (controller clock) at the moment
of the request, which is enough to convert any controller timestamp t:

    wall_clock(t) = generated_at - (elapsed_h - t) hours
"""

import json
import os
import re
import time
from datetime import datetime

import numpy as np
import pandas as pd

SCHEMA = "or05.vials/1"
CONSUMPTION_SCHEMA = "or05.consumption/1"
ALTSEL_SCHEMA = "or05.altsel/1"
DEFAULT_WINDOW_H = 6.0


def iso_now():
    """Wall clock with a COLON in the offset (-04:00, not -0400).

    strftime's %z omits the colon, which Date.parse in the viewer accepts but
    datetime.fromisoformat only learned to accept in 3.11 -- and the log repo's
    own timestamp rule requires the colon form outright. A caller converting a
    controller time to wall clock via this field should not have to normalise it
    first, so emit the form everything downstream already agrees on.
    """
    s = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if len(s) >= 5 and s[-5] in "+-":
        return s[:-2] + ":" + s[-2:]
    return s


# ─── disk readers ─────────────────────────────────────────────────────────────

def _read_pump_log(exp, vial):
    """One vial's dispense log, with every unusable row dropped rather than
    carried into the arithmetic.

    `time` and `timein` are forced numeric: a torn final line from an unclean
    shutdown, an empty field, or a stray token would otherwise make the column
    object-dtype or NaN, and a NaN volume reaches the payload as a bare `NaN`
    token -- which Python's json accepts and a browser's JSON.parse rejects.
    This module is polled by a browser, so that is the "never take the
    dashboard down" case arriving as a 200.

    The count of dropped rows is attached to the frame so a caller can be told
    the log was imperfect, rather than being handed a quietly smaller number.
    """
    path = os.path.join(exp, "pump_log", "vial%d_pump_log.txt" % vial)
    if not os.path.exists(path):
        df = pd.DataFrame(columns=["time", "timein", "pump"])
        df.attrs["dropped_rows"] = 0
        df.attrs["exists"] = False
        return df
    df = pd.read_csv(path, names=["time", "timein", "pump"], skiprows=[0],
                     on_bad_lines="skip")
    before = len(df)
    df["pump"] = df["pump"].fillna("")
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df["timein"] = pd.to_numeric(df["timein"], errors="coerce")
    df = df[np.isfinite(df["time"]) & np.isfinite(df["timein"])]
    df.attrs["dropped_rows"] = before - len(df)
    df.attrs["exists"] = True
    return df


def _read_drugconc(exp, vial):
    """Same discipline as _read_pump_log, which round 1 hardened and this was
    left out of: a zero-byte or header-less file took the WHOLE rig down with
    an EmptyDataError, and `1e400` in a time column became `inf`, which reaches
    the body as a bare Infinity token that no browser can parse. custom_script
    already refuses to WRITE a non-finite concentration; the read path had no
    such guard, so a hand-edited or torn file reintroduced it."""
    path = os.path.join(exp, "drugconc", "vial%d_drugconc.txt" % vial)
    empty = pd.DataFrame(columns=["time", "concentration"])
    if not os.path.exists(path):
        return empty
    try:
        df = pd.read_csv(path, on_bad_lines="skip")
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return empty
    if "time" not in df.columns or "concentration" not in df.columns:
        return empty
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df["concentration"] = pd.to_numeric(df["concentration"], errors="coerce")
    return df[np.isfinite(df["time"]) & np.isfinite(df["concentration"])]


class ClockUntrustworthy(Exception):
    """A wall-clock anchor cannot be converted on this rig. Distinct from a
    bad request: the caller's timestamp is fine, the rig's own clock is not."""


class CalibrationProblem(Exception):
    """pump_cal.json exists but cannot be used. Distinct from its absence,
    which is an ordinary state with its own flag."""


def _pump_coefficients():
    """Per-pump mL/s coefficients, or None if the rig has no calibration.

    Every shape that would otherwise surface as an IndexError, KeyError or
    TypeError three call-frames away -- a dict instead of a list, string
    values, a short list, a truncated file -- is turned into a named
    CalibrationProblem here, at the one place that knows what the file is
    supposed to contain.
    """
    if not os.path.exists("pump_cal.json"):
        return None
    try:
        with open("pump_cal.json") as fh:
            raw = json.load(fh)
    except ValueError as exc:
        raise CalibrationProblem("pump_cal.json is not valid JSON: %s" % exc)
    if not isinstance(raw, dict) or "coefficients" not in raw:
        raise CalibrationProblem("pump_cal.json has no `coefficients` key")
    coefs = raw["coefficients"]
    if not isinstance(coefs, list) or not coefs:
        raise CalibrationProblem("pump_cal.json's `coefficients` is %s, not a non-empty list"
                                 % type(coefs).__name__)
    # Entries are validated WHERE THEY ARE USED, in _coefficient(), not here.
    # Checking the whole list up front refused the entire rig -- every vial,
    # and /api/v1/vials with it, which is what the viewer's live column reads
    # -- because ONE unused entry was empty. Both live rigs have
    # coefficient 16 = "" and sixteen perfectly good calibrations either side
    # of it. A pump nobody dispenses through does not have to be calibrated.
    return coefs


def _coefficient(coefs, index, what):
    """One pump's coefficient, or a named problem. `int()` alone accepted
    anything pd.isna let through: -1.0 silently selected the LAST pump (990 mL
    from a 10 s run, HTTP 200), 8.7 truncated to 8, and True became pump 1."""
    if coefs is None:
        return None
    if isinstance(index, bool) or index is None:
        raise CalibrationProblem("%s is %r, not a pump number" % (what, index))
    try:
        as_float = float(index)
    except (TypeError, ValueError):
        raise CalibrationProblem("%s is %r, not a pump number" % (what, index))
    if not np.isfinite(as_float) or as_float != int(as_float):
        raise CalibrationProblem("%s is %r, not a whole pump number" % (what, index))
    idx = int(as_float)
    if idx < 0 or idx >= len(coefs):
        raise CalibrationProblem(
            "%s is %d, outside the %d calibrated pumps. custom_script defaults "
            "input_pump2 to 32+vial when the yaml omits it, which is this error"
            % (what, idx, len(coefs)))
    coef = coefs[idx]
    if isinstance(coef, bool) or not isinstance(coef, (int, float)) or not np.isfinite(coef):
        raise CalibrationProblem(
            "%s is pump %d, whose pump_cal.json coefficient is %r rather than a finite "
            "number -- that pump is not calibrated, so its volumes cannot be computed"
            % (what, idx, coef))
    return coef


# ─── the one implementation of the rate maths ─────────────────────────────────
# fig_pumpcontrol_ramp should call this too, so the figure and the API can
# never disagree about what a burn rate is.

def vial_rates(pump, coef_low, coef_high, window_h, now_h):
    """Volumes and trailing-window rates for one vial's pump log.

    'low' is pump in1, 'high' is in2. Rates are over the trailing window_h,
    which is what you want for 'what is it doing now' — a whole-run average
    hides exactly the changes worth seeing.
    """
    out = {
        "cumulative_mL": {"low": 0.0, "high": 0.0, "total": 0.0},
        "burn_rate_mL_per_h": {"low": None, "high": None, "total": None},
        "n_dispense_events": int(len(pump)),
        "last_event_h": None,
    }
    if pump.empty or coef_low is None or coef_high is None:
        return out

    vol = np.where(pump["pump"].values == "in1",
                   pump["timein"].values * coef_low,
                   pump["timein"].values * coef_high)
    is_low = pump["pump"].values == "in1"
    out["cumulative_mL"] = {
        "low": round(float(vol[is_low].sum()), 2),
        "high": round(float(vol[~is_low].sum()), 2),
        "total": round(float(vol.sum()), 2),
    }
    out["last_event_h"] = round(float(pump["time"].max()), 4)

    t0 = now_h - window_h
    win = pump["time"].values >= t0
    span = min(window_h, now_h) or window_h
    if win.any() and span > 0:
        lo = float(vol[win & is_low].sum()) / span
        hi = float(vol[win & ~is_low].sum()) / span
        out["burn_rate_mL_per_h"] = {"low": round(lo, 2), "high": round(hi, 2),
                                     "total": round(lo + hi, 2)}
    else:
        out["burn_rate_mL_per_h"] = {"low": 0.0, "high": 0.0, "total": 0.0}
    return out


def evolver_name(es):
    """Which eVOLVER this is.

    experiment_parameters.yaml carries the control IP, not the name, so most
    rigs will not have an evolver_name to read. dashboard.py already keeps the
    IP-to-name table, so borrow it rather than duplicating a copy here that
    would drift. The import is deferred to call time because dashboard imports
    this module, and at request time it is long since fully loaded.
    """
    for key in ("evolver_name", "evolver"):
        if es.get(key):
            return es[key]
    ip = es.get("ip")
    if not ip:
        return None
    try:
        from dashboard import _REV_IPDICT
        return _REV_IPDICT.get(ip)
    except Exception:
        return None


def build_summary(config, window_h=DEFAULT_WINDOW_H, now_h=None):
    """The whole payload. Pure apart from reading the experiment's own files."""
    es = config["experiment_settings"]
    exp = es["exp_name"]
    evolver = evolver_name(es)
    coefs = _pump_coefficients()

    active = [v for v in es["per_vial_settings"] if v.get("to_run")]

    # The controller clock NOW -- see controller_now(). Using the last write
    # instead made stale_h structurally zero for whichever vial wrote last, so
    # the viewer's "red when stalled" column could not go red, and every
    # wall-clock conversion a caller did against elapsed_h opened its window
    # early. The trailing burn-rate window is measured against the same clock,
    # so an idle rig's rate now decays toward zero instead of freezing at its
    # last active value.
    # Read each file ONCE. This used to re-read every pump log and drugconc to
    # find the clock and then again in the vial loop: 64 CSV parses per request
    # on a 16-vial rig, polled every 15 s by every open viewer tab, against
    # files the controller is appending to.
    logs = {int(vc["vial"]): (_read_pump_log(exp, int(vc["vial"])),
                              _read_drugconc(exp, int(vc["vial"]))) for vc in active}
    staleness_h = None
    if now_h is None:
        last_write_h = 0.0
        for pair in logs.values():
            for df in pair:
                if not df.empty:
                    last_write_h = max(last_write_h, float(df["time"].max()))
        now_h, staleness_h = controller_now(exp, active, last_write_h)

    vials = []
    for vc in active:
        v = int(vc["vial"])
        pump, conc = logs[v]
        p2 = vc.get("input_pump2")
        try:
            coef_low = _coefficient(coefs, v, "vial %d's own pump" % v)
            coef_high = (None if (p2 is None or (isinstance(p2, float) and pd.isna(p2)))
                         else _coefficient(coefs, p2, "vial %d's input_pump2" % v))
        except CalibrationProblem:
            coef_low = coef_high = None

        rec = {"vial": v}
        rec.update(vial_rates(pump, coef_low, coef_high, window_h, now_h))

        if not conc.empty:
            last = conc.sort_values("time").iloc[-1]
            rec["drug_concentration_g_per_L"] = round(float(last["concentration"]), 3)
            rec["drug_concentration_at_h"] = round(float(last["time"]), 4)
            rec["n_concentration_steps"] = int(len(conc))
        else:
            rec["drug_concentration_g_per_L"] = None
            rec["drug_concentration_at_h"] = None
            rec["n_concentration_steps"] = 0

        # high fraction is the diagnostic that ties burn rate to concentration:
        # at hold it tracks (c - c_low)/(c_high - c_low)
        br = rec["burn_rate_mL_per_h"]
        rec["high_fraction"] = (round(br["high"] / br["total"], 3)
                                if br["total"] else None)
        rec["stale_h"] = (round(now_h - rec["last_event_h"], 3)
                          if rec["last_event_h"] is not None else None)
        rec["setpoint"] = vc.get("setpoint")
        rec["target_ramp"] = vc.get("target_ramp")
        rec["volume_mL"] = vc.get("volume")
        vials.append(rec)

    return {
        "schema": SCHEMA,
        "evolver": evolver,
        "experiment": exp,
        "generated_at": iso_now(),
        "elapsed_h": round(now_h, 4),
        "staleness_h": None if staleness_h is None else round(staleness_h, 4),
        "clock_problem": clock_problem(staleness_h),
        "window_h": window_h,
        "pump_calibration": coefs is not None,
        "n_vials": len(vials),
        "vials": vials,
    }


def controller_now(exp, active, last_write_h):
    """The controller hour at THIS MOMENT, not at the last log write.

    The largest timestamp in the logs is when the rig last wrote something,
    which is not the same as now -- and using it as `elapsed_h` made the
    documented identity `wall(t) = generated_at - (elapsed_h - t)` false by
    exactly the write staleness. Every wall-clock anchor converted with it
    landed EARLIER than intended, so windows over-counted, always in the same
    direction, and by the most during a hold or a stall: precisely when the
    number matters.

    The missing link is on disk. A pump log's mtime is the wall clock of its
    last row, whose controller hour we already know, so:

        controller_now = last_write_h + (now - mtime)

    A rig that has stopped writing therefore reports a clock that keeps
    advancing, which is the truth and is what makes a stalled vial visible.
    Returns (controller_now, staleness_h); falls back to last_write_h when no
    log file exists to date, with staleness None to say so.
    """
    newest = None
    for vc in active:
        v = int(vc["vial"])
        for sub in ("pump_log", "vial%d_pump_log.txt"), ("drugconc", "vial%d_drugconc.txt"):
            path = os.path.join(exp, sub[0], sub[1] % v)
            if os.path.exists(path):
                mtime = os.path.getmtime(path)
                newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return last_write_h, None
    # NOT clamped at zero. A negative staleness means the newest file is dated
    # in the FUTURE -- a clock stepped forward, an NTP correction, a file
    # copied from a machine whose clock is ahead, NFS/SMB skew. Clamping it
    # made that case byte-identical to a perfectly current rig, which is the
    # one thing it must not look like: the whole point of this function is
    # that a stale rig should be visible. It is reported as-is and
    # `clock_trustworthy` below turns it into a refusal.
    staleness_h = (time.time() - newest) / 3600.0
    return last_write_h + staleness_h, staleness_h


# An experiment directory copied or restored with mtimes intact (rsync -a,
# tar -xp, Time Machine, an archived run re-served) looks like a rig that has
# been idle for however long ago that was -- and a wall-clock `since` then
# resolves into a window the log cannot describe, answering 0.0 mL with both
# flags green. Past this many hours of apparent idleness, the mtime is no
# longer evidence of anything and a wall-clock anchor is refused rather than
# answered wrongly.
MAX_TRUSTED_STALENESS_H = 48.0


def clock_problem(staleness_h):
    """Why this rig's clock cannot carry a wall-clock anchor, or None."""
    if staleness_h is None:
        return None
    if staleness_h < -0.0834:                      # more than ~5 minutes ahead
        return ("this rig's newest log file is dated %.2f h in the FUTURE, so its files "
                "cannot date its own writes. A clock step, an NTP correction, or a "
                "directory copied from a machine whose clock is ahead" % -staleness_h)
    if staleness_h > MAX_TRUSTED_STALENESS_H:
        return ("this rig has not written a log line for %.1f h. Past %.0f h the file "
                "timestamps stop being evidence of when the controller was last running "
                "-- an experiment directory restored from a backup or copied with mtimes "
                "intact looks exactly like this" % (staleness_h, MAX_TRUSTED_STALENESS_H))
    return None


def build_consumption(config, since_h=None, since_iso=None, now_h=None):
    """Volume dispensed per vial SINCE one specific instant, integrated from
    the pump log rather than averaged over a trailing window.

    /api/v1/vials answers "what is this vial doing now": a window anchored on
    the present. Reconciling a bottle against its last level reading needs the
    other thing -- everything dispensed since one past instant -- which no
    trailing window can express. /dispenses can express it but ships every
    event (O(run length), one call per vial) and leaves the caller to convert
    wall clock into controller hours. Here the payload is fixed by vial count,
    and the clock conversion happens on the side that owns the clock.

    Pass since_iso (wall clock, with offset) or since_h (the controller's own
    hours), not both. The conversion is this module's documented identity
    solved for t:  t = elapsed_h - (generated_at - wall) / 1h

    FOUR FIELDS THE CALLER MUST READ BEFORE THE NUMBERS
      clock_ok       False when the anchor lands AFTER the controller clock.
                     Either the rig restarted (clock back to ~0, pump log
                     replaced) or the anchor is in the future; either way this
                     log cannot describe that window, and the volumes below
                     are not an answer to the question that was asked.
      covers_window  False when the anchor precedes this experiment's own
                     zero. The window opens before the run does, so every
                     volume is a LOWER bound on what was actually drawn.
      log_gap_h      Hours between the window opening and the log's first
                     SURVIVING row: zero on a healthy rig, large on a log
                     truncated or rotated at a restart, where the missing
                     hours would otherwise read as 0 mL rather than as a gap.
                     Reported per rig AND per vial -- the rig-wide figure is
                     a min across vials, so one untouched vial would
                     otherwise certify coverage for all sixteen.
      clock_problem  Non-null when the file timestamps this module dates its
                     own writes by cannot be trusted: a future mtime, or an
                     idleness past MAX_TRUSTED_STALENESS_H, which is what an
                     experiment directory restored with mtimes intact looks
                     like. A wall-clock anchor is refused outright there.

    Per vial there are three more of the same kind: `problem` (this vial's
    calibration could not be resolved, with what to do about it),
    `dropped_rows` (rows of its own log this module could not parse -- each
    one a dispense missing from the volume) and `unrecognised_rows` (rows
    whose pump column is neither in1 nor in2, counted and priced at nothing).

    All of them are reported rather than raised, because "0.0 mL dispensed"
    and "I cannot see that window" are the same number and must not be the
    same answer. A caller that reads only the volumes will eventually publish
    the second as the first -- which is exactly what happened.
    """
    es = config["experiment_settings"]
    exp = es["exp_name"]
    coefs = _pump_coefficients()
    active = [v for v in es["per_vial_settings"] if v.get("to_run")]

    logs = {int(vc["vial"]): _read_pump_log(exp, int(vc["vial"])) for vc in active}

    last_write_h = 0.0
    log_starts_h = None
    for vc in active:
        v = int(vc["vial"])
        for df in (logs[v], _read_drugconc(exp, v)):
            if not df.empty:
                last_write_h = max(last_write_h, float(df["time"].max()))
        if not logs[v].empty:
            first = float(logs[v]["time"].min())
            log_starts_h = first if log_starts_h is None else min(log_starts_h, first)

    staleness_h = None
    if now_h is None:
        now_h, staleness_h = controller_now(exp, active, last_write_h)

    generated_at = iso_now()
    resolved_from = "controller_hours"
    clock_fault = clock_problem(staleness_h)
    if since_iso is not None and clock_fault:
        raise ClockUntrustworthy(clock_fault)
    if since_iso is not None:
        wall = datetime.fromisoformat(since_iso)
        if wall.tzinfo is None:
            raise ValueError("since must carry an explicit UTC offset, e.g. "
                             "2026-09-21T06:15:00-04:00 -- a bare local time is "
                             "ambiguous across the rig and the caller")
        delta_h = (datetime.fromisoformat(generated_at) - wall).total_seconds() / 3600.0
        since_h = now_h - delta_h
        resolved_from = "wall_clock"
    elif since_h is None:
        # "Everything" must include an event at exactly t=0 -- the strict `>`
        # below silently dropped it, and every event at t<=0, while
        # covers_window still said the window was covered. The default is now
        # an open lower bound rather than the number zero.
        since_h = float("-inf")
        resolved_from = "all"

    vials = []
    for vc in active:
        v = int(vc["vial"])
        pump = logs[v]
        p2 = vc.get("input_pump2")
        rec = {"vial": v, "low_mL": None, "high_mL": None, "total_mL": None,
               "n_events": 0, "first_event_h": None, "last_event_h": None,
               "dropped_rows": int(pump.attrs.get("dropped_rows", 0)),
               "log_present": bool(pump.attrs.get("exists", False)),
               "problem": None}
        # Each pump is resolved independently. Resolving them together and
        # bailing on the first failure meant a bad input_pump2 -- the high
        # pump -- also blanked the LOW volume, which needs only coef_low and
        # was perfectly computable.
        coef_low = coef_high = None
        problems = []
        try:
            coef_low = _coefficient(coefs, v, "vial %d's own pump" % v)
        except CalibrationProblem as exc:
            problems.append(str(exc))
        try:
            coef_high = (None if (p2 is None or (isinstance(p2, float) and pd.isna(p2)))
                         else _coefficient(coefs, p2, "vial %d's input_pump2" % v))
        except CalibrationProblem as exc:
            problems.append(str(exc))
        if problems:
            # One vial's bad calibration must not cost the other fifteen their
            # numbers, and it must not read as "this vial drew nothing".
            rec["problem"] = "; ".join(problems)
        if problems and coef_low is None and coef_high is None:
            # Only a calibration PROBLEM skips the vial. Having no
            # pump_cal.json at all is an ordinary state: the events are still
            # counted and the volumes reported as null, which is how a rig
            # that cannot convert is told apart from a bottle that drew
            # nothing.
            vials.append(rec)
            continue

        if not pump.empty:
            win = pump[pump["time"] > since_h]
            rec["n_events"] = int(len(win))
            if len(win):
                rec["first_event_h"] = round(float(win["time"].min()), 4)
                rec["last_event_h"] = round(float(win["time"].max()), 4)
            # log_starts_h is a min across the whole rig, so one untouched vial
            # certifies coverage for all sixteen. This vial's own first row is
            # the only thing that can speak for this vial's own volume.
            own_start = float(pump["time"].min())
            rec["log_starts_h"] = round(own_start, 4)
            rec["log_gap_h"] = (None if since_h == float("-inf")
                                else round(max(0.0, own_start - max(since_h, 0.0)), 4))
            # Each side needs only its OWN coefficient. Requiring both meant a
            # media-only vial (input_pump2: .nan, legal and normal) reported
            # null for the low volume it could perfectly well compute.
            is_low = win["pump"].values == "in1"
            is_high = win["pump"].values == "in2"
            if coef_low is not None:
                rec["low_mL"] = round(float((win["timein"].values[is_low] * coef_low).sum()), 3)
            if coef_high is not None:
                rec["high_mL"] = round(float((win["timein"].values[is_high] * coef_high).sum()), 3)
            if rec["low_mL"] is not None or rec["high_mL"] is not None:
                rec["total_mL"] = round((rec["low_mL"] or 0.0) + (rec["high_mL"] or 0.0), 3)
            # Per-role counts as well as the vial total: a reservoir's block is
            # role-specific, so reporting the vial's combined event count beside
            # a low-only volume invites exactly the wrong sanity check.
            rec["low_events"] = int(is_low.sum())
            rec["high_events"] = int(is_high.sum())
            # Rows whose pump is neither in1 nor in2 (the 0,0 placeholder, a
            # torn line) are counted as events but contribute no volume: there
            # is no coefficient for a pump nobody named.
            unrecognised = int((~(is_low | is_high)).sum())
            if unrecognised:
                rec["unrecognised_rows"] = unrecognised
        vials.append(rec)

    return {
        "schema": CONSUMPTION_SCHEMA,
        "evolver": evolver_name(es),
        "experiment": exp,
        "generated_at": generated_at,
        "elapsed_h": round(now_h, 4),
        "last_write_h": round(last_write_h, 4),
        "staleness_h": None if staleness_h is None else round(staleness_h, 4),
        "log_starts_h": None if log_starts_h is None else round(log_starts_h, 4),
        "since_h": None if since_h == float("-inf") else round(since_h, 4),
        "since": since_iso,
        "since_resolved_from": resolved_from,
        "clock_ok": since_h <= now_h + 1e-6,
        # covers_window answers ONE question: does the window open before this
        # experiment's own zero? Truncation is a different fact and lives in
        # log_gap_h below.
        #
        # They were briefly conflated -- covers_window tested the log's first
        # surviving row -- and that made it False for since_h=0 on every
        # healthy rig, since none dispenses at exactly t=0, which is the query
        # check_api.py sends. A flag an operator learns to ignore is worse
        # than no flag, so the two facts were separated rather than merged.
        "covers_window": (True if since_h == float("-inf") else since_h >= -1e-6),
        # A SEPARATE fact from covers_window, because conflating them made the
        # flag fire for since_h=0 on every healthy rig (no rig dispenses at
        # exactly t=0), and a flag an operator learns to ignore is worse than
        # no flag. This is the hours between the window opening and the log's
        # first surviving row: zero on a healthy rig, large on a log truncated
        # or rotated at a restart, where the missing hours would otherwise
        # read as 0 mL rather than as a gap.
        "log_gap_h": (None if (log_starts_h is None or since_h == float("-inf"))
                      else round(max(0.0, log_starts_h - max(since_h, 0.0)), 4)),
        "clock_problem": clock_fault,
        "pump_calibration": coefs is not None,
        "n_vials": len(vials),
        "vials": vials,
    }


# ─── alternating_selection ────────────────────────────────────────────────────
# custom_script.py's alternating_selection keeps ALL per-vial state on disk:
# state_log/ (one row per transition), drug_target/ (one row per ramp) and
# cycle_log/ (one row per completed cycle). The viewer needs the first two to
# draw which half of the protocol a culture is in; nothing else can tell it,
# because that state exists only in those files.

def _altsel_read(exp, sub, vial, suffix, names, numeric):
    """One of the mode's logs, with torn rows dropped.

    on_bad_lines="skip" drops a row with too MANY fields; a row cut short
    mid-write is PADDED with NaN and survives, so every column that matters is
    required to be finite. The controller appends to these while this reads,
    so a torn tail is ordinary rather than exceptional.
    """
    path = os.path.join(exp, sub, "vial%d_%s.txt" % (vial, suffix))
    if not os.path.exists(path):
        return pd.DataFrame(columns=names)
    try:
        df = pd.read_csv(path, on_bad_lines="skip")
    except Exception:
        return pd.DataFrame(columns=names)
    if list(df.columns[:len(names)]) != names:
        return pd.DataFrame(columns=names)
    keep = np.ones(len(df), dtype=bool)
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        keep &= np.isfinite(df[col])
    return df[keep].sort_values("time")


def build_altsel(config, now_h=None):
    """Per-vial state spans, current target and last cycle, or an empty roster.

    A rig not running this mode is not an error: it answers with its actual
    mode and no vials, so a caller can tell "not this mode" from "this mode,
    nothing logged yet" -- which are different things and would otherwise both
    read as silence.
    """
    es = config["experiment_settings"]
    exp = es["exp_name"]
    mode = (es.get("operation") or {}).get("mode")
    active = [v for v in es["per_vial_settings"] if v.get("to_run")]

    if now_h is None:
        last_write_h = 0.0
        for vc in active:
            v = int(vc["vial"])
            for df in (_read_pump_log(exp, v), _read_drugconc(exp, v)):
                if not df.empty:
                    last_write_h = max(last_write_h, float(df["time"].max()))
        now_h, _staleness = controller_now(exp, active, last_write_h)

    vials = []
    if mode == "alternating_selection":
        for vc in active:
            v = int(vc["vial"])
            states = _altsel_read(exp, "state_log", v, "state",
                                  ["time", "state"], ("time",))
            targets = _altsel_read(exp, "drug_target", v, "drug_target",
                                   ["time", "target"], ("time", "target"))
            cycles = _altsel_read(exp, "cycle_log", v, "cycles",
                                  ["time", "state", "kind", "conc_before",
                                   "conc_after", "cycle_duration",
                                   "dilution_counter", "time_in_state"],
                                  ("time", "cycle_duration", "dilution_counter"))
            # The log holds TRANSITIONS, so a span runs from each row to the
            # next and the last one runs to now. Read any other way, a vial is
            # stateless except at the instants it changed.
            rows = [(float(t), str(st)) for t, st in zip(states["time"], states["state"])]
            spans = [[rows[i][0], (rows[i + 1][0] if i + 1 < len(rows) else round(now_h, 4)),
                      rows[i][1]]
                     for i in range(len(rows))]
            spans = [sp for sp in spans if sp[1] > sp[0]]
            last = cycles.tail(1)
            vials.append({
                "vial": v,
                "state": rows[-1][1] if rows else None,
                "state_since_h": rows[-1][0] if rows else None,
                "spans": spans,
                "current_drug": (round(float(targets["target"].values[-1]), 4)
                                 if not targets.empty else None),
                "targets": [[round(float(t), 4), round(float(g), 4)]
                            for t, g in zip(targets["time"], targets["target"])],
                "counter": (int(last["dilution_counter"].values[0])
                            if not last.empty else None),
                "last_kind": (str(last["kind"].values[0]) if not last.empty else None),
                "n_tolerant": vc.get("n_tolerant"),
                "n_dilutions": vc.get("n_dilutions"),
            })

    return {
        "schema": ALTSEL_SCHEMA,
        "evolver": evolver_name(es),
        "experiment": exp,
        "mode": mode,
        "generated_at": iso_now(),
        "elapsed_h": round(now_h, 4),
        "n_vials": len(vials),
        "vials": vials,
    }


# ─── flask wiring ─────────────────────────────────────────────────────────────

def register(server, get_data, allow_origin="*"):
    """Attach the routes to Dash's underlying Flask server.

    get_data() is dashboard.py's existing cached loader, so the API reuses the
    same mtime-guarded cache and never re-reads disk more often than the
    figures already do.
    """
    from flask import jsonify, request

    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = allow_origin
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _unavailable(reason, schema=SCHEMA):
        """dashboard.py's get_data() swallows load errors and returns None, so a
        first-read failure would otherwise surface here as a 500 with a stack
        trace. Report it as data the viewer can display instead."""
        return _cors(jsonify({"schema": schema, "ok": False, "error": reason,
                              "vials": [], "n_vials": 0})), 503

    def _guarded(fn):
        """This route family reads files a controller is actively appending to.
        Without this, every fault /consumption survives as a 503 with a reason
        was a bare 500 here -- and a 500 carries no CORS header, so a browser
        saw an opaque network error instead of the message."""
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as exc:
                return _unavailable("%s: %s" % (type(exc).__name__, exc))
        wrapper.__name__ = fn.__name__
        return wrapper

    @server.route("/api/v1/vials")
    def _vials():
        config, _ = get_data()
        if not config:
            return _unavailable("config unavailable — dashboard could not load "
                                "experiment_parameters.yaml")
        raw_window = request.args.get("window_h", DEFAULT_WINDOW_H)
        try:
            # Parsed INSIDE a guard, and checked for finiteness: this line sat
            # outside the try, so ?window_h=banana was an uncaught 500 with no
            # CORS header on the most-polled endpoint in the module -- exactly
            # the failure round 1 fixed for /dispenses. nan/inf reached the
            # body as bare tokens no browser can parse, and "1_0" silently
            # became 10.0.
            if isinstance(raw_window, str) and "_" in raw_window:
                raise ValueError("underscores are not a number here")
            window = float(raw_window)
            if not np.isfinite(window) or window <= 0:
                raise ValueError("must be a positive, finite number of hours")
        except (TypeError, ValueError) as exc:
            return _cors(jsonify({"schema": SCHEMA, "ok": False,
                                  "error": "window_h is not usable (%r): %s"
                                           % (raw_window, exc)})), 400
        try:
            return _cors(jsonify(build_summary(config, window_h=window)))
        except Exception as exc:                      # never take the dashboard down
            return _unavailable("%s: %s" % (type(exc).__name__, exc))

    @server.route("/api/v1/vials/<int:vial>/series")
    @_guarded
    def _series(vial):
        """Downsampled trace for one vial, on demand. Never polled, never stored."""
        config, _ = get_data()
        if not config:
            return _unavailable("config unavailable")
        exp = config["experiment_settings"]["exp_name"]
        since = float(request.args.get("since_h", 0.0))
        cap = int(request.args.get("max_points", 500))
        conc = _read_drugconc(exp, vial)
        conc = conc[conc["time"] >= since].sort_values("time")
        if len(conc) > cap:
            conc = conc.iloc[:: max(1, len(conc) // cap)]
        return _cors(jsonify({
            "schema": SCHEMA, "vial": vial, "since_h": since,
            "n_points": int(len(conc)),
            "drug_concentration": [[round(float(t), 4), round(float(c), 3)]
                                   for t, c in zip(conc["time"], conc["concentration"])],
        }))

    @server.route("/api/v1/vials/<int:vial>/dispenses")
    @_guarded
    def _dispenses(vial):
        """Per-event dispense times and volumes, for a caller that wants to
        integrate them itself.

        This is the one endpoint whose response grows with run length, so it
        supports incremental fetching: pass since_h with the last timestamp you
        already hold and you get only what is new. A caller that polls from 0
        every time will eventually be shipping megabytes; one that tracks its
        high-water mark ships a handful of rows.

        Events are [time_h, volume_mL, pump] triples rather than objects, which
        is about a third of the bytes for the same information.
        """
        config, _ = get_data()
        if not config:
            return _unavailable("config unavailable")
        es = config["experiment_settings"]
        exp = es["exp_name"]
        since = float(request.args.get("since_h", 0.0))

        vc = next((v for v in es["per_vial_settings"] if int(v["vial"]) == vial), None)
        if vc is None:
            return _cors(jsonify({"schema": SCHEMA, "vial": vial, "error": "vial not configured",
                                  "dispenses": [], "n": 0})), 404

        try:
            coefs = _pump_coefficients()
            p2 = vc.get("input_pump2")
            coef_low = _coefficient(coefs, vial, "vial %d's own pump" % vial)
            coef_high = (None if (p2 is None or (isinstance(p2, float) and pd.isna(p2)))
                         else _coefficient(coefs, p2, "vial %d's input_pump2" % vial))
        except CalibrationProblem as exc:
            return _unavailable(str(exc))

        unfiltered = _read_pump_log(exp, vial)
        pump = unfiltered[unfiltered["time"] > since].sort_values("time")
        rows = []
        skipped = 0
        for t, tin, which in zip(pump["time"], pump["timein"], pump["pump"]):
            # Only in1/in2 are dispenses. `else high` charged every stray row
            # -- the 0,0 placeholder, a torn line -- to the DRUG bottle and
            # priced it, so this endpoint and /consumption disagreed by real
            # millilitres over the same log (measured: 20 mL on one vial) with
            # nothing saying so. They drop the same rows now, and the count is
            # reported the same way.
            if which not in ("in1", "in2"):
                skipped += 1
                continue
            coef = coef_low if which == "in1" else coef_high
            if coef is None:
                # A media-only vial (input_pump2: .nan, legal and normal) has
                # no high coefficient and a perfectly good low one. Emitting a
                # null-volume row made the caller refuse the whole unit.
                skipped += 1
                continue
            rows.append([round(float(t), 4), round(float(tin) * coef, 3),
                         "low" if which == "in1" else "high"])
        return _cors(jsonify({
            "schema": SCHEMA, "vial": vial, "since_h": since,
            "vial_volume_mL": vc.get("volume"),
            "pump_calibration": coefs is not None,
            # The controller clock, NOT max(returned rows) and NOT the
            # caller's own `since` echoed back. Echoing `since` made
            # viewer.html's restart guard compare cur.upto against cur.upto,
            # so it could never fire; and using the last row's time placed a
            # stalled vial's final dispense at "now" on the viewer's timeline
            # while the summary endpoint said it was 48 h stale.
            # From the UNFILTERED log. Passing the filtered frame's max made
            # elapsed_h depend on the caller's own since_h -- 100.0 for
            # since_h=105 while /api/v1/vials said 110.0 at the same instant --
            # which is the opposite of what the comment claimed and exactly
            # what viewer.html's restart guard compares against.
            "elapsed_h": round(controller_now(exp, es["per_vial_settings"],
                                              float(unfiltered["time"].max())
                                              if len(unfiltered) else 0.0)[0], 4),
            "last_event_h": (round(float(unfiltered["time"].max()), 4)
                             if len(unfiltered) else None),
            "dropped_rows": int(unfiltered.attrs.get("dropped_rows", 0)),
            "unrecognised_rows": skipped,
            "clock_problem": clock_problem(
                controller_now(exp, es["per_vial_settings"],
                               float(unfiltered["time"].max())
                               if len(unfiltered) else 0.0)[1]),
            "generated_at": iso_now(),
            "n": len(rows),
            "dispenses": rows,
        }))

    @server.route("/api/v1/consumption")
    def _consumption():
        config, _ = get_data()
        if not config:
            return _unavailable("config unavailable", CONSUMPTION_SCHEMA)
        since = request.args.get("since")
        since_h = request.args.get("since_h")
        if since is not None and since_h is not None:
            return _cors(jsonify({"schema": CONSUMPTION_SCHEMA, "ok": False,
                                  "error": "pass since (wall clock) or since_h "
                                           "(controller hours), not both"})), 400
        if since_h is not None:
            try:
                # Bare float() also accepts "nan", "inf" and Python's "1_0"
                # (which becomes 10.0, silently), and NaN reaches the body as a
                # bare NaN token that no browser can parse.
                if "_" in since_h:
                    raise ValueError("underscores are not a number here")
                since_h = float(since_h)
                if not np.isfinite(since_h):
                    raise ValueError("must be finite")
            except ValueError as exc:
                return _cors(jsonify({"schema": CONSUMPTION_SCHEMA, "ok": False,
                                      "error": "since_h is not a usable number (%r): %s"
                                               % (since_h, exc)})), 400
        if since is not None:
            # A "+" in a query string decodes to a space, so every POSITIVE
            # UTC offset arrived here as "...T00:00:00 00:00" and was refused
            # as malformed. The rig's own -04:00 worked by luck; any caller in
            # UTC or east of Greenwich did not.
            since = re.sub(r" (\d{2}:?\d{2})$", r"+\1", since)
            try:
                _parsed = datetime.fromisoformat(since)
            except ValueError as exc:
                return _cors(jsonify({"schema": CONSUMPTION_SCHEMA, "ok": False,
                                      "error": "since is not an ISO 8601 timestamp: %s"
                                               % exc})), 400
            if since.strip().endswith(("Z", "z")):
                return _cors(jsonify({"schema": CONSUMPTION_SCHEMA, "ok": False,
                                      "error": "since must use a numeric offset such as "
                                               "-04:00, not Z -- this project's timestamp "
                                               "rule rejects Z everywhere else"})), 400
            if _parsed.tzinfo is None:
                return _cors(jsonify({"schema": CONSUMPTION_SCHEMA, "ok": False,
                                      "error": "since must carry an explicit UTC offset, "
                                               "e.g. 2026-09-21T06:15:00-04:00"})), 400
        try:
            return _cors(jsonify(build_consumption(config, since_h=since_h, since_iso=since)))
        except CalibrationProblem as exc:
            return _unavailable(str(exc), CONSUMPTION_SCHEMA)
        except Exception as exc:
            # A fault in the rig's own files is the RIG's problem, not the
            # caller's: a 400 here told an operator their `since_h=0` was
            # malformed when one vial's pump log held one unparseable row.
            return _unavailable("%s: %s" % (type(exc).__name__, exc), CONSUMPTION_SCHEMA)

    @server.route("/api/v1/altsel")
    @_guarded
    def _altsel():
        config, _ = get_data()
        if not config:
            return _unavailable("config unavailable", ALTSEL_SCHEMA)
        return _cors(jsonify(build_altsel(config)))

    @server.route("/api/v1/health")
    def _health():
        config, _ = get_data()
        return _cors(jsonify({
            "schema": SCHEMA, "ok": config is not None,
            "evolver": evolver_name((config or {}).get("experiment_settings", {})),
            "experiment": (config or {}).get("experiment_settings", {}).get("exp_name"),
        }))

    return server
