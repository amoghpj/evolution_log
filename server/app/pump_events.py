"""GET /pump_events -- every dispense the rigs made in the last N hours,
labelled with the culture it went into and the bottle it came from.

GET /media answers "how much is left in each bottle"; its `pump` block is a
per-reservoir TOTAL since that bottle's last reading. This answers a
different question -- "what did the pumps actually do, vial by vial, over a
window I choose" -- so it is its own route rather than more fields on that one.

Three things make it more than a proxy for the rig's /dispenses endpoint:

1. **The clock.** The rig counts in controller hours since its run began, not
   wall time, and its `since_h` means "controller hour X", not "X hours ago".
   The rig's current controller hour is anchored on THIS server's clock at
   the moment of the request -- not on the rig's generated_at, which may be
   up to pump_rates.RIG_SKEW_TOLERANCE_MIN off and would shift every event
   outside the window it was selected for. A rig that restarted inside the
   window has no earlier events in this run's log, so the window is reported
   as truncated rather than silently shorter.

2. **The culture.** A vial number is hardware; line identity follows the
   culture (LOG_PROTOCOL.md §4). Each event is attributed to whichever line
   the LOG places in that vial at that instant. A line's stays are rebuilt
   from its t0, its end, and its hardware_swap history -- and then CHECKED:
   against the line's own record of where it is now, and, for an id that
   encodes its founding vial, against that. A history that does not
   reconcile is not used to place the line anywhere; the dispense is left
   unattributed with the reason. So is one into a vial no line occupies,
   two lines placed in one vial at once, or a line alive with no recorded
   position. Never the likeliest line.

3. **The bottle.** low/high is a pump, not a reservoir. It becomes a reservoir
   id through the attributed line's own `reservoirs` (pump_rates.line_positions,
   which refuses when the line's two records of its bottles disagree).

Every total says whether it is complete. One that is not -- a unit not read,
a vial not read, rows that could not be used, a window a restart cut short,
a filter narrowing the scope -- says why, rather than appearing smaller.

Everything that crosses the network goes through pump_rates.UnitClient, so
this route inherits its byte cap, its deadline, and its identity, skew and
calibration checks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from time import monotonic

from .line_ids import OCCUPANCY_RE
from .pump_rates import (UnitClient, _FetchProblem, _number, _parse, _quoted, _text,
                         _vials_by_number, line_positions)

_log = logging.getLogger("or05.pump_events")

# The rig's /dispenses endpoint grows with run length and is fetched once per
# vial; 48 h keeps one call from pulling a run's whole history.
MAX_WINDOW_H = 48.0
DEFAULT_WINDOW_H = 6.0
# Per unit: one summary fetch plus one per vial, sequentially.
EVENTS_BUDGET_S = 12.0
# The log's timestamps are operator-reported and minute-precision; a dispense
# this close to a recorded line change in its vial may belong to either line.
NEAR_CHANGE_MIN = 15.0
# One dispense larger than this is not a dispense: an eVOLVER vial holds
# ~30 mL. Refused and reported rather than summed into a bottle's draw.
MAX_DISPENSE_ML = 100.0
# A rig's elapsed_h is its last write plus a staleness that can be slightly
# negative (tools/evolver_api.py: down to about -0.08 h), which puts its newest
# rows just AFTER elapsed_h. Within this they are real and kept; beyond it they
# are refused and counted.
FUTURE_TOLERANCE_H = 0.25
# A controller clock outside this is not a run this server can place.
MAX_ELAPSED_H = 100_000.0
VIALS_PER_UNIT = 16
# Distinct rig-authored pump labels shown in one unattributed reason.
MAX_LABELS_SHOWN = 5

EVENT_FIELDS = ["at", "mL", "pump", "line_id", "reservoir_id"]
# A stay whose position the log does not record: "it may have been anywhere".
UNKNOWN = "unrecorded"


def _when(value) -> datetime | None:
    """A timestamp as an AWARE datetime, or None. A naive one is refused:
    comparing it with the aware ones raised TypeError -- a 500."""
    if not isinstance(value, str):
        return None
    try:
        moment = _parse(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def _vial_number(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# ─── where the log says each line was, and when ──────────────────────────────

def _superseded_ids(log: dict) -> set[str]:
    out = set()
    for L in (log.get("lines") or {}).values():
        for e in L.get("events") or []:
            if isinstance(e.get("supersedes"), str):
                out.add(e["supersedes"])
    for e in log.get("experiment_events") or []:
        if isinstance(e.get("supersedes"), str):
            out.add(e["supersedes"])
    return out


def _end_of(line: dict, superseded: set) -> tuple[datetime | None, str | None]:
    """(when this line ended, why that is unknown). A running line ends at
    None with no doubt; an ended line whose end cannot be read is a doubt,
    not a line that never ended."""
    if line.get("status") != "ended":
        return None, None
    raw = (line.get("lineage") or {}).get("terminated_at") or line.get("terminated_at")
    if raw is None:
        ends = [_when(e.get("timestamp")) for e in line.get("events") or []
                if e.get("event_type") == "termination" and e.get("event_id") not in superseded]
        ends = [e for e in ends if e is not None]
        end = max(ends) if ends else None
    else:
        end = _when(raw)
    if end is None:
        return None, "it has ended, but the log records no readable time at which it did"
    return end, None


def _founding_vial(line_id: str) -> tuple[str, int] | None:
    """The founding (unit, vial) a branch/restart id encodes, e.g.
    patrick-v05#2 -> (patrick, 5). Split (.a) and merge (+) ids do not
    encode one (app/line_ids.py), so they get no such check."""
    m = OCCUPANCY_RE.match(line_id or "")
    return (m.group(1), int(m.group(2))) if m else None


def occupancy(log: dict) -> list[dict]:
    """Every line's stays, as segments {line_id, pos, start, end, doubt}.

    `pos` is (unit, vial), or None for off the evolver. `doubt` is None when
    the log places the line there unambiguously, and otherwise the reason it
    does not -- in which case `pos` is the best the log offers, used only to
    say "it may have been here", never to attribute.

    A line's history is used only if it reconciles. It does not when:
    - a hardware_swap that tries to move it cannot be read (bad destination,
      bad timestamp, params not an object);
    - the history ends somewhere other than the line's own unit/vial record
      (a superseded move whose record was not reverted, a free-text move
      whose record was edited by hand, moves timestamped in a different
      order from the one they were written in);
    - its id encodes a founding vial its history does not start from;
    - its first move does not say where it moved from;
    - it ended at a time the log does not record.
    Superseded hardware_swaps and terminations are ignored. Every stay is
    clipped to the line's lifetime, so a swap logged after a termination
    cannot keep a dead line in its vial.
    """
    superseded = _superseded_ids(log)
    out = []
    for line_id, L in (log.get("lines") or {}).items():
        record = ((L.get("unit"), L.get("vial"))
                  if isinstance(L.get("unit"), str) and _vial_number(L.get("vial")) is not None
                  else None)
        start = _when(L.get("t0"))
        if start is None:
            out.append({"line_id": line_id, "pos": record if record else UNKNOWN,
                        "start": None, "end": None,
                        "doubt": "its t0 cannot be read, so when it occupied anything is unknown"})
            continue
        end, end_doubt = _end_of(L, superseded)

        moves, broken = [], None
        for e in L.get("events") or []:
            if e.get("event_type") != "hardware_swap" or e.get("event_id") in superseded:
                continue
            p = e.get("params")
            if not isinstance(p, dict):
                broken = broken or "%s is a hardware_swap whose params cannot be read" % e.get("event_id")
                continue
            tries = "new_unit" in p or "new_vial" in p or "vacate" in p
            if not tries:
                continue                        # calibration/pump/IP: no move
            nu, nv = p.get("new_unit"), _vial_number(p.get("new_vial"))
            relocates = isinstance(nu, str) and nv is not None
            vacates = p.get("vacate") is True
            when = _when(e.get("timestamp"))
            if when is None or relocates == vacates:
                broken = broken or ("%s is a hardware_swap whose %s cannot be read"
                                    % (e.get("event_id"),
                                       "timestamp" if when is None else "destination"))
                continue
            pu, pv = p.get("previous_unit"), _vial_number(p.get("previous_vial"))
            prev = (pu, pv) if isinstance(pu, str) and pv is not None else None
            moves.append((when, str(e.get("event_id") or ""), (nu, nv) if relocates else None, prev))
        moves.sort(key=lambda m: (m[0], m[1]))

        # Doubts have a reach. One that breaks the history (unreadable move,
        # history and record disagree, id and history disagree) covers every
        # stay; an unrecorded founding vial covers only the stay before the
        # first move; an unrecorded end covers only the last stay.
        line_doubt = broken
        first_doubt = end_doubt_last = None
        founding = moves[0][3] if moves else record
        if moves and founding is None:
            first_doubt = ("its first move (%s) does not say where it moved from, and its "
                           "founding vial is recorded nowhere else" % moves[0][1])
            founding = UNKNOWN
        final = moves[-1][2] if moves else record
        if not line_doubt and final != record:
            line_doubt = ("its swap history ends %s but its own record says %s -- the log "
                          "contradicts itself about where it is" % (_where(final), _where(record)))
        encoded = _founding_vial(line_id)
        if not line_doubt and encoded and founding not in (None, UNKNOWN) and founding != encoded:
            line_doubt = ("its id says it was founded %s but its history starts %s -- a "
                          "move the history does not explain (a retracted swap whose record "
                          "was not reverted, or a hand-edited one)"
                          % (_where(encoded), _where(founding)))
        if end_doubt:
            end_doubt_last = end_doubt

        stays, cursor, pos = [], start, founding
        for when, _eid, new_pos, _prev in moves:
            when = max(when, start)
            if end is not None and when >= end:
                break
            if when > cursor:
                stays.append([cursor, when, pos, first_doubt if not stays else None])
            elif not stays and first_doubt:
                pass                            # a move at t0: nothing before it to doubt
            cursor, pos = when, new_pos
        if end is None or cursor < end:
            stays.append([cursor, end, pos, first_doubt if not stays else None])
        if stays and end_doubt_last:
            stays[-1][3] = "; ".join(x for x in (stays[-1][3], end_doubt_last) if x)

        merged = []
        for st in stays:                        # a swap that re-states the same vial is no change
            if merged and merged[-1][2] == st[2] and merged[-1][3] == st[3]:
                merged[-1][1] = st[1]
            else:
                merged.append(st)
        for s_start, s_end, s_pos, s_doubt in merged:
            out.append({"line_id": line_id, "pos": s_pos, "start": s_start, "end": s_end,
                        "doubt": line_doubt or s_doubt})
    return out


def _where(pos) -> str:
    if pos is UNKNOWN:
        return "at an unrecorded position"
    return "off the evolver" if pos is None else "in %s vial %d" % pos


def attribute(segments: list[dict], unit: str, vial: int, t: datetime) -> tuple[str | None, str | None]:
    """(line_id, None) when the log places exactly one line here at `t`
    without doubt; (None, reason) otherwise. Never the likeliest line."""
    here, maybe = [], []
    for s in segments:
        if s["start"] is not None and (s["start"] > t or (s["end"] is not None and t >= s["end"])):
            continue
        if s["doubt"]:
            if s["pos"] == (unit, vial) or s["pos"] is UNKNOWN:
                maybe.append("%s (%s)" % (s["line_id"], s["doubt"]))
        elif s["pos"] == (unit, vial):
            here.append(s["line_id"])
    if len(here) == 1 and not maybe:
        return here[0], None
    if len(here) > 1:
        return None, ("the log places %d lines in %s vial %d at once (%s), so which "
                      "culture received this cannot be decided"
                      % (len(here), unit, vial, ", ".join(sorted(here))))
    if maybe:
        lead = ("the log places %s here, but " % here[0]) if here else ""
        return None, ("%sit cannot rule out %s, so which culture received this cannot be "
                      "decided" % (lead, "; ".join(sorted(maybe))))
    return None, ("no line in the log occupied %s vial %d at this time -- the rig "
                  "dispensed into a vial the log records as empty" % (unit, vial))


def _changes_at(segments: list[dict], unit: str, vial: int) -> list[datetime]:
    """Instants at which the occupant of this vial changes, per the log."""
    out = set()
    for s in segments:
        if s["pos"] == (unit, vial) and s["start"] is not None and not s["doubt"]:
            out.add(s["start"])
            if s["end"] is not None:
                out.add(s["end"])
    return sorted(out)


# ─── one rig ─────────────────────────────────────────────────────────────────

def fetch_unit(unit: str, url: str, client, timeout_s: float, window_h: float,
               only_vial: int | None, now: datetime) -> dict:
    """One rig's dispenses over the window, with wall-clock times.

    {"ok": False, "reason"} when the rig cannot be used at all. Otherwise
    {"ok": True, ...} with per-vial rows, and per-vial `failed` (could not be
    read) and `dropped` (rows refused, with why). A vial that failed on its own
    does not sink its neighbours: these are events, and the ones that arrived
    are still real -- but every total they reach is marked incomplete.
    """
    rc = UnitClient(unit, url, client, timeout_s, now=now)
    rc.budget_s = EVENTS_BUDGET_S
    rc._deadline = monotonic() + EVENTS_BUDGET_S
    summary = rc._summary_once()
    if not isinstance(summary, dict) or "__error__" in summary:
        return {"ok": False, "url": rc.safe_base,
                "reason": "%s's /api/v1/vials could not be read (%s), so there is no "
                          "controller clock to place its pump log on"
                          % (rc.safe_base, (summary or {}).get("__error__", "not an object")
                             if isinstance(summary, dict) else "not an object")}
    clock_problem = summary.get("clock_problem")
    for problem in (rc._identity_problem(summary),
                    None if summary.get("schema") in (None, "or05.vials/1") else
                    "%s's /api/v1/vials reports a schema of %s, so whatever is answering is "
                    "not an eVOLVER dashboard"
                    % (rc.safe_base, _quoted(str(summary.get("schema")), 60)),
                    None if summary.get("pump_calibration") is True else
                    "%s reports no usable pump calibration, so pump seconds cannot be "
                    "converted to millilitres" % unit,
                    None if not clock_problem else
                    "%s cannot date its own writes -- %s"
                    % (unit, _quoted(clock_problem) or "a clock problem it did not describe"),
                    rc._clock_skew(summary.get("generated_at"))):
        if problem:
            return {"ok": False, "url": rc.safe_base, "reason": problem}

    elapsed_h = _number(summary.get("elapsed_h"))
    if elapsed_h is None or not 0.0 <= elapsed_h <= MAX_ELAPSED_H:
        return {"ok": False, "url": rc.safe_base,
                "reason": "%s reports a controller clock (elapsed_h) of %s, which is not a "
                          "run this server can place on the wall clock"
                          % (unit, _text(repr(summary.get("elapsed_h")), 40))}

    staleness = _number(summary.get("staleness_h"))
    exact = staleness is not None and 0 <= staleness < 48.0
    since_h = elapsed_h - window_h
    truncated = since_h < 0

    vials, problem = _vials_by_number(summary)
    if problem:
        return {"ok": False, "url": rc.safe_base, "reason": "%s: %s" % (unit, problem)}
    odd = sorted(v for v in vials if not 0 <= v < VIALS_PER_UNIT)
    wanted = sorted(v for v in vials if 0 <= v < VIALS_PER_UNIT)
    if only_vial is not None:
        if only_vial not in wanted:
            return {"ok": False, "url": rc.safe_base,
                    "reason": "%s reports no vial %d (it has %s)"
                              % (unit, only_vial, ", ".join(map(str, wanted)) or "none")}
        wanted = [only_vial]

    rows_by_vial, failed, dropped = {}, {}, {}
    for vial in wanted:
        if monotonic() >= rc._deadline:
            for v in wanted:
                if v not in rows_by_vial and v not in failed:
                    failed[v] = ("not fetched: this request's %.0f s budget for %s ran out"
                                 % (EVENTS_BUDGET_S, unit))
            break
        try:
            r = rc._get("/api/v1/vials/%d/dispenses" % vial, {"since_h": max(since_h, 0.0)})
        except _FetchProblem as exc:
            failed[vial] = exc.reason
            continue
        except Exception as exc:                        # noqa: BLE001 -- refuse, never raise
            failed[vial] = rc._unreachable(exc)
            continue
        body, why = rc._accept(r, "vial %d's dispenses" % vial)
        if why:
            failed[vial] = why
            continue
        # The per-vial body is checked as the summary was: a stray proxy or a
        # rebooted rig answering one path must not slip one vial's rows in.
        why = rc._identity_problem(body)
        if why or body.get("pump_calibration") is False:
            failed[vial] = why or ("%s reports no pump calibration for vial %d" % (unit, vial))
            continue
        rows = body.get("dispenses")
        if not isinstance(rows, list):
            failed[vial] = "dispenses came back as a %s, not a list" % type(rows).__name__
            continue
        clean, why_dropped = [], {}
        for row in rows:
            reason = None
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                reason = "not a [time, mL, pump] row"
            else:
                t, mL = _number(row[0]), _number(row[1])
                if t is None or mL is None:
                    reason = "no usable time or volume"
                elif t <= since_h:
                    continue                            # before the window: not a loss
                elif t > elapsed_h + FUTURE_TOLERANCE_H:
                    reason = "dated after the rig's own clock"
                elif mL < 0:
                    reason = "a negative volume"
                elif mL > MAX_DISPENSE_ML:
                    reason = "more than %g mL in one dispense" % MAX_DISPENSE_ML
                else:
                    clean.append((t, mL, row[2]))
                    continue
            why_dropped[reason] = why_dropped.get(reason, 0) + 1
        rows_by_vial[vial] = clean
        if why_dropped:
            dropped[vial] = why_dropped

    # Anchored on THIS server's now, not the rig's generated_at: the rig's
    # current controller hour IS elapsed_h, and placing it at the rig's own
    # wall time shifted every event by the rig's skew -- out of the window it
    # was selected for, and across line changes in the log.
    def wall(t_h: float) -> datetime:
        return now - timedelta(hours=elapsed_h - t_h)

    return {"ok": True, "url": rc.safe_base, "experiment": _quoted(summary.get("experiment"), 120),
            "clock_exact": exact, "window_truncated": truncated,
            "run_started_at": wall(0.0).isoformat() if truncated else None,
            "rows": {v: [(wall(t), mL, role) for t, mL, role in rows]
                     for v, rows in rows_by_vial.items()},
            "failed": failed, "dropped": dropped,
            "ignored_vials": odd}


# ─── the whole view ──────────────────────────────────────────────────────────

def _pump_label(role) -> str | None:
    """'low'/'high', or None for anything else -- shown only through _text."""
    return role if role in ("low", "high") and isinstance(role, str) else None


def _unit_view(u: str, got: dict, segments: list[dict], positions: dict,
               include_events: bool, by_res: dict, by_line: dict, incomplete: list) -> dict:
    unit_out = {"ok": True, "url": got["url"], "experiment": got["experiment"],
                "clock_exact": got["clock_exact"],
                "window_truncated": got["window_truncated"], "vials": {}}
    if not got["clock_exact"]:
        unit_out["clock_note"] = (
            "this rig does not report a trustworthy staleness_h, so its controller clock "
            "is its LAST WRITE, behind the real time by however long it has been idle. "
            "Every time below may be LATE by that much, and the window opens EARLY by that "
            "much: it can include dispenses older than window_h")
    if got["window_truncated"]:
        unit_out["run_started_at"] = got["run_started_at"]
        unit_out["truncation_note"] = (
            "this rig's current run began at %s, inside the window; dispenses from before "
            "that are in a previous run's log and are not included" % got["run_started_at"])
        incomplete.append("%s's run began inside the window, at %s" % (u, got["run_started_at"]))
    if got["ignored_vials"]:
        unit_out["ignored_vials"] = got["ignored_vials"]
        incomplete.append("%s reported vial numbers outside 0-%d (%s), which were not read"
                          % (u, VIALS_PER_UNIT - 1, ", ".join(map(str, got["ignored_vials"]))))
    problems = {}
    for v, why in got["failed"].items():
        problems[str(v)] = why
        unit_out["vials"][str(v)] = {"ok": False, "reason": why}
        incomplete.append("%s vial %d could not be read" % (u, v))
    for v, reasons in got["dropped"].items():
        text = "; ".join("%d row(s): %s" % (n, r) for r, n in sorted(reasons.items()))
        problems[str(v)] = "rows refused and left out of every total -- %s" % text
        incomplete.append("%s vial %d had rows that could not be used" % (u, v))
    if problems:
        unit_out["vial_problems"] = dict(sorted(problems.items(), key=lambda kv: int(kv[0])))

    for v, rows in sorted(got["rows"].items()):
        changes = _changes_at(segments, u, v)
        events, unattributed, labels = [], {}, {}
        totals = {"low": 0.0, "high": 0.0}
        lines_seen, near = [], 0
        for when, mL, role in sorted(rows, key=lambda r: r[0]):
            pump = _pump_label(role)
            line_id, why = attribute(segments, u, v, when)
            rid, rid_why = None, None
            if pump is None:
                shown = _text(role if isinstance(role, str) else repr(role), 40) or "unprintable"
                labels[shown] = labels.get(shown, 0) + 1
                rid_why = "the rig reported a pump that is neither low nor high, so it is charged to no bottle"
            elif line_id:
                pos = positions.get(line_id) or {}
                if pump in (pos.get("reservoir_conflicts") or {}):
                    rid_why = ("%s's two records of its %s bottle disagree, so this dispense is "
                               "charged to neither" % (line_id, pump))
                else:
                    rid = (pos.get("reservoirs") or {}).get(pump)
                    if rid is None:
                        rid_why = "%s names no %s reservoir" % (line_id, pump)
            if pump:
                totals[pump] += mL
            if line_id and line_id not in lines_seen:
                lines_seen.append(line_id)
            if any(abs((when - c).total_seconds()) < NEAR_CHANGE_MIN * 60 for c in changes):
                near += 1
            reason = why or rid_why
            if reason:
                slot = unattributed.setdefault(reason, {"reason": reason, "n_events": 0,
                                                        "mL": 0.0, "from": None, "to": None})
                slot["n_events"] += 1
                slot["mL"] += mL
                slot["from"] = slot["from"] or when.isoformat()
                slot["to"] = when.isoformat()
            if rid:
                agg = by_res.setdefault(rid, {"mL": 0.0, "n_events": 0, "vials": set(), "lines": set()})
                agg["mL"] += mL
                agg["n_events"] += 1
                agg["vials"].add("%s/%d" % (u, v))
                agg["lines"].add(line_id)
            if line_id:
                lt = by_line.setdefault(line_id, {"low_mL": 0.0, "high_mL": 0.0, "n_events": 0})
                if pump:
                    lt["%s_mL" % pump] += mL
                lt["n_events"] += 1
            events.append([when.isoformat(), round(mL, 4), pump or "unrecognised", line_id, rid])

        vo = {"ok": True, "n_events": len(events),
              "total_mL": {k: round(x, 3) for k, x in totals.items()}, "lines": lines_seen}
        if include_events:
            vo["events"] = events
        if unattributed:
            vo["unattributed"] = [dict(s, mL=round(s["mL"], 4)) for s in unattributed.values()]
        if labels:
            shown = sorted(labels)[:MAX_LABELS_SHOWN]
            vo["unrecognised_pumps"] = {
                "n_events": sum(labels.values()),
                "labels": ['%s' % s for s in shown],
                "note": "pump labels as the rig sent them (cleaned and shortened)%s"
                        % ("" if len(labels) <= MAX_LABELS_SHOWN
                           else ", %d more not shown" % (len(labels) - MAX_LABELS_SHOWN))}
        if near:
            vo["near_line_change"] = {
                "n_events": near,
                "note": "these dispenses fall within %.0f min of a recorded line change in this "
                        "vial; the log's times are operator-reported to the minute, so they may "
                        "belong to the neighbouring line" % NEAR_CHANGE_MIN}
        if not rows and v not in got["dropped"]:
            vo["quiet"] = ("no dispenses in the window -- a real measurement, and a blocked or "
                           "dead line looks exactly like an idle one")
        unit_out["vials"][str(v)] = vo
    unit_out["vials"] = dict(sorted(unit_out["vials"].items(), key=lambda kv: int(kv[0])))
    return unit_out


def build_pump_events(log: dict, dash, client, window_h: float, unit: str | None = None,
                      vial: int | None = None, include_events: bool = True,
                      now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    start = now - timedelta(hours=window_h)
    segments = occupancy(log)
    positions = line_positions(log)
    in_log = sorted((log.get("hardware") or {}).get("units") or {})
    units = [unit] if unit else in_log

    by_res, by_line, out_units, incomplete, notes = {}, {}, {}, [], []

    # Two names for one dashboard would read it twice and count every dispense
    # twice -- the identity check cannot catch it when the rig gives no name.
    by_url = {}
    for u in units:
        url = dash.url_for(u)
        if url:
            by_url.setdefault(url.rstrip("/").lower(), []).append(u)
    shared = {u: names for names in by_url.values() if len(names) > 1 for u in names}

    for u in units:
        url = dash.url_for(u)
        if not url:
            out_units[u] = {"ok": False, "reason": dash.why_not(u)}
        elif u in shared:
            out_units[u] = {"ok": False, "reason":
                            "%s share one dashboard URL, so reading it for each would count "
                            "every dispense more than once; none of them is read. Check the "
                            "roster (EVOLVER_DASHBOARD_URLS or viewer.config.json)"
                            % " and ".join(shared[u])}
        else:
            try:
                got = fetch_unit(u, url, client, dash.timeout_s, window_h, vial, now=now)
                out_units[u] = (_unit_view(u, got, segments, positions, include_events,
                                           by_res, by_line, incomplete)
                                if got["ok"] else
                                {"ok": False, "url": got.get("url"), "reason": got["reason"]})
            except Exception as exc:                    # noqa: BLE001 -- one unit, not the route
                _log.exception("pump_events: unit %s raised", u)
                out_units[u] = {"ok": False, "reason":
                                "this server failed while reading %s (%s). Nothing from it is "
                                "counted; the other units are unaffected"
                                % (u, type(exc).__name__)}
        if not out_units[u]["ok"]:
            incomplete.append("%s was not read" % u)

    # A roster entry the log does not know is a rig nobody is accounting for:
    # said, not silently skipped.
    if unit is None:
        for extra in sorted(set(getattr(dash, "units", {}) or {}) - set(in_log)):
            out_units[extra] = {"ok": False, "reason":
                                "%s is on the dashboard roster but not in this log's "
                                "hardware.units, so it was not read and nothing it dispensed "
                                "is counted" % extra}
            notes.append("the dashboard roster lists %s, which this log does not know" % extra)

    if unit is not None:
        notes.append("filtered to unit %s: every total below covers that unit only" % unit)
    if vial is not None:
        notes.append("filtered to %s vial %d: every total below covers that vial only" % (unit, vial))

    complete = not incomplete
    flag = ({"complete": True} if complete else
            {"complete": False, "incomplete_because": sorted(set(incomplete))})
    return {
        "window": {"hours": window_h, "from": start.isoformat(), "to": now.isoformat()},
        "scope": {"unit": unit, "vial": vial},
        "event_fields": EVENT_FIELDS,
        **flag,
        "units": out_units,
        "totals_by_reservoir": {
            rid: {"mL": round(a["mL"], 3), "n_events": a["n_events"],
                  "vials": sorted(a["vials"]), "lines": sorted(a["lines"]), **flag}
            for rid, a in sorted(by_res.items())},
        "totals_by_line": {lid: {"low_mL": round(t["low_mL"], 3), "high_mL": round(t["high_mL"], 3),
                                 "n_events": t["n_events"], **flag}
                           for lid, t in sorted(by_line.items())},
        "notes": notes,
    }
