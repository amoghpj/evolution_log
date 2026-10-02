"""GET /pump_events -- every dispense the rigs made in the last N hours,
labelled with the culture it went into and the bottle it came from.

GET /media answers "how much is left in each bottle"; its `pump` block is a
per-reservoir TOTAL since that bottle's last reading. This answers a
different question -- "what did the pumps actually do, vial by vial, over a
window I choose" -- so it is its own route rather than more fields on that one.

Three things make it more than a proxy for the rig's /dispenses endpoint:

1. **The clock.** The rig counts in controller hours since its run began, not
   wall time, and its `since_h` means "controller hour X", not "X hours ago".
   The window is converted with the same identity pump_rates.py uses
   (wall = generated_at - (elapsed_h - t)), and every event comes back as a
   wall-clock timestamp with an offset. A rig that restarted inside the window
   has no events from before its restart in this run's log, so the window is
   reported as truncated rather than silently shorter.

2. **The culture.** A vial number is hardware; line identity follows the
   culture (LOG_PROTOCOL.md §4). Each event is attributed to whichever line
   the LOG places in that vial at that instant, from each line's t0, its
   termination, and its hardware_swap history. Where the log cannot say -- a
   line whose founding vial was never recorded, two lines placed in one vial
   at once, a vial no line occupies -- the event is left unattributed with
   the reason, never assigned to the likeliest line.

3. **The bottle.** low/high is a pump, not a reservoir. It becomes a reservoir
   id through the attributed line's own `reservoirs` (pump_rates.line_positions,
   which refuses when the line's two records of its bottles disagree).

Everything that crosses the network goes through pump_rates.UnitClient, so
this route inherits its byte cap, its deadline, and its identity, skew and
calibration checks rather than re-deriving them.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from time import monotonic

from .pump_rates import (UnitClient, _FetchProblem, _number, _parse, _quoted,
                         _vials_by_number, line_positions)

# The rig's /dispenses endpoint grows with run length and is fetched once per
# vial; 48 h keeps one call from pulling a run's whole history (and is
# comfortably inside pump_rates.MAX_BODY_BYTES per vial at real dosing rates).
MAX_WINDOW_H = 48.0
DEFAULT_WINDOW_H = 6.0
# Per unit: one summary fetch plus one per vial, sequentially.
EVENTS_BUDGET_S = 12.0
# The log's timestamps are operator-reported and minute-precision; a dispense
# this close to a recorded line change in its vial may belong to either line.
NEAR_CHANGE_MIN = 15.0

EVENT_FIELDS = ["at", "mL", "pump", "line_id", "reservoir_id"]


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


def _end_of(line: dict) -> tuple[datetime | None, bool]:
    """(when this line ended, whether that is known). A line still running
    ends at None, which is known; an ended line with no recorded end is not."""
    if line.get("status") != "ended":
        return None, True
    ts = (line.get("lineage") or {}).get("terminated_at") or line.get("terminated_at")
    if not ts:
        ends = [e.get("timestamp") for e in line.get("events") or []
                if e.get("event_type") == "termination" and e.get("timestamp")]
        ts = max(ends, key=_parse) if ends else None
    try:
        return (_parse(ts), True) if ts else (None, False)
    except (TypeError, ValueError):
        return None, False


def occupancy(log: dict) -> list[dict]:
    """Every line's stays, as segments {line_id, unit, vial, start, end, known}.

    `known: False` means the line was alive then but the log does not record
    where. That happens when its FIRST relocating hardware_swap carries no
    previous_unit/previous_vial: the founding position is stored nowhere else
    (line_ids.last_real_position documents the same gap). A vacate is a known
    position of None -- off the evolver -- not an unknown one.

    Superseded hardware_swap events are skipped: the correction stands in
    their place, and is ordered by its own timestamp.
    """
    superseded = _superseded_ids(log)
    out = []
    for line_id, L in (log.get("lines") or {}).items():
        try:
            start = _parse(L.get("t0"))
        except (TypeError, ValueError):
            continue
        end, end_known = _end_of(L)

        moves = []
        for e in L.get("events") or []:
            if e.get("event_type") != "hardware_swap" or e.get("event_id") in superseded:
                continue
            p = e.get("params") or {}
            nu, nv = p.get("new_unit"), p.get("new_vial")
            relocates = isinstance(nu, str) and isinstance(nv, int) and not isinstance(nv, bool)
            if not (relocates or p.get("vacate") is True):
                continue                        # calibration/pump/IP: no move
            try:
                when = _parse(e.get("timestamp"))
            except (TypeError, ValueError):
                continue
            pu, pv = p.get("previous_unit"), p.get("previous_vial")
            prev = ((pu, pv) if isinstance(pu, str) and isinstance(pv, int)
                    and not isinstance(pv, bool) else None)
            moves.append((when, e.get("event_id") or "", (nu, nv) if relocates else None, prev))
        moves.sort(key=lambda m: (m[0], m[1]))

        if not moves:
            pos, known = ((L.get("unit"), L.get("vial")) if L.get("unit") is not None
                          and L.get("vial") is not None else None), True
        else:
            pos, known = moves[0][3], moves[0][3] is not None

        cursor = start
        for when, _eid, new_pos, _prev in moves:
            if when > cursor:
                out.append({"line_id": line_id, "pos": pos, "start": cursor, "end": when,
                            "known": known})
            cursor, pos, known = max(cursor, when), new_pos, True
        out.append({"line_id": line_id, "pos": pos, "start": cursor, "end": end,
                    "known": known, "end_known": end_known})
    return out


def attribute(segments: list[dict], unit: str, vial: int, t: datetime) -> tuple[str | None, str | None]:
    """(line_id, None) when the log places exactly one line here at `t`;
    (None, reason) otherwise. Never the likeliest line."""
    here, unknown = [], []
    for s in segments:
        if s["start"] > t or (s["end"] is not None and t >= s["end"]):
            continue
        if not s["known"]:
            unknown.append(s["line_id"])
        elif s["pos"] == (unit, vial):
            here.append(s["line_id"])
    if len(here) == 1:
        return here[0], None
    if len(here) > 1:
        return None, ("the log places %d lines in %s vial %d at once (%s), so which "
                      "culture received this cannot be decided"
                      % (len(here), unit, vial, ", ".join(sorted(here))))
    if unknown:
        return None, ("no line is recorded in %s vial %d at this time, but %s %s alive "
                      "with no recorded position then (a founding vial the log never "
                      "stored), so it may have been here"
                      % (unit, vial, ", ".join(sorted(unknown)),
                         "was" if len(unknown) == 1 else "were"))
    return None, ("no line in the log occupied %s vial %d at this time -- the rig "
                  "dispensed into a vial the log records as empty" % (unit, vial))


def _changes_at(segments: list[dict], unit: str, vial: int) -> list[datetime]:
    """Instants at which the occupant of this vial changes, per the log."""
    out = set()
    for s in segments:
        if s["known"] and s["pos"] == (unit, vial):
            out.add(s["start"])
            if s["end"] is not None:
                out.add(s["end"])
    return sorted(out)


# ─── one rig ─────────────────────────────────────────────────────────────────

def fetch_unit(unit: str, url: str, client, timeout_s: float, window_h: float,
               only_vial: int | None, now: datetime | None = None) -> dict:
    """One rig's dispenses over the window, with wall-clock times.

    {"ok": False, "reason"} when the rig cannot be used at all. Otherwise
    {"ok": True, ...} with per-vial raw rows -- and a vial that failed on its
    own is listed under `vial_problems`, not allowed to sink its neighbours,
    because unlike a bottle total these are events and the ones that arrived
    are still real.
    """
    rc = UnitClient(unit, url, client, timeout_s, now=now)
    rc._deadline = monotonic() + EVENTS_BUDGET_S
    summary = rc._summary_once()
    if not isinstance(summary, dict) or "__error__" in summary:
        return {"ok": False, "url": rc.safe_base,
                "reason": "%s's /api/v1/vials could not be read (%s), so there is no "
                          "controller clock to place its pump log on"
                          % (rc.safe_base, (summary or {}).get("__error__", "not an object"))}
    for problem in (rc._identity_problem(summary),
                    None if summary.get("schema") in (None, "or05.vials/1") else
                    "%s's /api/v1/vials reports schema %r, so whatever is answering is not "
                    "an eVOLVER dashboard" % (rc.safe_base, summary.get("schema")),
                    None if summary.get("pump_calibration") is True else
                    "%s reports no usable pump calibration, so pump seconds cannot be "
                    "converted to millilitres" % unit,
                    ("%s cannot date its own writes -- %s"
                     % (unit, _quoted(summary["clock_problem"])))
                    if summary.get("clock_problem") else None,
                    rc._clock_skew(summary.get("generated_at"))):
        if problem:
            return {"ok": False, "url": rc.safe_base, "reason": problem}

    elapsed_h = _number(summary.get("elapsed_h"))
    try:
        generated = _parse(summary.get("generated_at"))
    except (TypeError, ValueError):
        generated = None
    if elapsed_h is None or generated is None:
        return {"ok": False, "url": rc.safe_base,
                "reason": "%s reports no usable generated_at/elapsed_h, so its pump log "
                          "cannot be placed on the wall clock" % unit}

    staleness = _number(summary.get("staleness_h"))
    exact = staleness is not None and 0 <= staleness < 48.0
    since_h = elapsed_h - window_h
    truncated = since_h < 0
    run_started = generated - timedelta(hours=elapsed_h)

    vials, problem = _vials_by_number(summary)
    if problem:
        return {"ok": False, "url": rc.safe_base, "reason": "%s: %s" % (unit, problem)}
    wanted = sorted(vials)
    if only_vial is not None:
        if only_vial not in vials:
            return {"ok": False, "url": rc.safe_base,
                    "reason": "%s reports no vial %d (it has %s)"
                              % (unit, only_vial, ", ".join(map(str, wanted)) or "none")}
        wanted = [only_vial]

    rows_by_vial, vial_problems = {}, {}
    for vial in wanted:
        if monotonic() >= rc._deadline:
            for v in wanted:
                if v not in rows_by_vial and v not in vial_problems:
                    vial_problems[v] = ("not fetched: this request's %.0f s budget for %s "
                                        "ran out" % (EVENTS_BUDGET_S, unit))
            break
        try:
            r = rc._get("/api/v1/vials/%d/dispenses" % vial, {"since_h": max(since_h, 0.0)})
        except _FetchProblem as exc:
            vial_problems[vial] = exc.reason
            continue
        except Exception as exc:                        # noqa: BLE001 -- refuse, never raise
            vial_problems[vial] = rc._unreachable(exc)
            continue
        body, why = rc._accept(r, "vial %d's dispenses" % vial)
        if why:
            vial_problems[vial] = why
            continue
        rows = body.get("dispenses")
        if not isinstance(rows, list):
            vial_problems[vial] = "dispenses came back as %s, not a list" % type(rows).__name__
            continue
        clean, bad = [], 0
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                bad += 1
                continue
            t, mL = _number(row[0]), _number(row[1])
            if t is None or mL is None or t <= since_h or t > elapsed_h + 1e-6:
                bad += t is None or mL is None
                continue
            clean.append((t, mL, row[2]))
        rows_by_vial[vial] = clean
        if bad:
            vial_problems[vial] = ("%d dispense row(s) had no usable time or volume and are "
                                   "left out, so this vial's totals are lower bounds" % bad)

    def wall(t_h: float) -> datetime:
        return generated - timedelta(hours=elapsed_h - t_h)

    return {"ok": True, "url": rc.safe_base, "experiment": _quoted(summary.get("experiment")),
            "clock_exact": exact, "window_truncated": truncated,
            "run_started_at": run_started.isoformat() if truncated else None,
            "rows": {v: [(wall(t), mL, role) for t, mL, role in rows]
                     for v, rows in rows_by_vial.items()},
            "vial_problems": vial_problems}


# ─── the whole view ──────────────────────────────────────────────────────────

def build_pump_events(log: dict, dash, client, window_h: float, unit: str | None = None,
                      vial: int | None = None, include_events: bool = True,
                      now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    start = now - timedelta(hours=window_h)
    segments = occupancy(log)
    positions = line_positions(log)
    units = [unit] if unit else sorted((log.get("hardware") or {}).get("units") or {})

    by_res, by_line, out_units, notes = {}, {}, {}, []
    for u in units:
        url = dash.url_for(u)
        if not url:
            out_units[u] = {"ok": False, "reason": dash.why_not(u)}
            continue
        got = fetch_unit(u, url, client, dash.timeout_s, window_h, vial, now=now)
        if not got["ok"]:
            out_units[u] = {"ok": False, "url": got.get("url"), "reason": got["reason"]}
            continue

        unit_out = {"ok": True, "url": got["url"], "experiment": got["experiment"],
                    "clock_exact": got["clock_exact"],
                    "window_truncated": got["window_truncated"], "vials": {}}
        if not got["clock_exact"]:
            unit_out["clock_note"] = (
                "this rig does not report a trustworthy staleness_h, so its controller "
                "clock is its LAST WRITE: every time below may be late by however long it "
                "has been idle, and the window may open late by the same amount")
        if got["window_truncated"]:
            unit_out["run_started_at"] = got["run_started_at"]
            unit_out["truncation_note"] = (
                "this rig's current run began at %s, inside the window; dispenses from "
                "before that are in a previous run's log and are not included, so every "
                "total for this unit is a lower bound" % got["run_started_at"])
        if got["vial_problems"]:
            unit_out["vial_problems"] = {str(v): why for v, why in sorted(got["vial_problems"].items())}
        incomplete = got["window_truncated"] or bool(got["vial_problems"])

        for v, rows in sorted(got["rows"].items()):
            changes = _changes_at(segments, u, v)
            events, unattributed, totals = [], {}, {"low": 0.0, "high": 0.0}
            lines_seen, near = [], 0
            for when, mL, role in sorted(rows, key=lambda r: r[0]):
                line_id, why = attribute(segments, u, v, when)
                rid, rid_why = None, None
                if role not in ("low", "high"):
                    rid_why = ("the rig reported pump %r, which is neither low nor high, so "
                               "it is charged to no bottle" % (role,))
                elif line_id:
                    pos = positions.get(line_id) or {}
                    if role in (pos.get("reservoir_conflicts") or {}):
                        rid_why = ("%s's two records of its %s bottle disagree (%s), so this "
                                   "dispense is charged to neither"
                                   % (line_id, role, pos["reservoir_conflicts"][role]))
                    else:
                        rid = (pos.get("reservoirs") or {}).get(role)
                        if rid is None:
                            rid_why = "%s names no %s reservoir" % (line_id, role)
                if role in totals:
                    totals[role] += mL
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
                    agg = by_res.setdefault(rid, {"mL": 0.0, "n_events": 0, "vials": set(),
                                                  "lines": set(), "complete": True,
                                                  "why_incomplete": set()})
                    agg["mL"] += mL
                    agg["n_events"] += 1
                    agg["vials"].add("%s/%d" % (u, v))
                    agg["lines"].add(line_id)
                    if incomplete:
                        agg["complete"] = False
                        agg["why_incomplete"].add(u)
                if line_id:
                    lt = by_line.setdefault(line_id, {"low_mL": 0.0, "high_mL": 0.0, "n_events": 0})
                    if role in ("low", "high"):
                        lt["%s_mL" % role] += mL
                    lt["n_events"] += 1
                events.append([when.isoformat(), round(mL, 3), role, line_id, rid])

            vo = {"n_events": len(events),
                  "total_mL": {k: round(x, 3) for k, x in totals.items()},
                  "lines": lines_seen}
            if include_events:
                vo["events"] = events
            if unattributed:
                vo["unattributed"] = [dict(s, mL=round(s["mL"], 3)) for s in unattributed.values()]
            if near:
                vo["near_line_change"] = {
                    "n_events": near,
                    "note": "these dispenses fall within %.0f min of a recorded line change in "
                            "this vial; the log's times are operator-reported to the minute, "
                            "so they may belong to the neighbouring line" % NEAR_CHANGE_MIN}
            if not rows:
                vo["quiet"] = ("no dispenses in the window -- a real measurement, and a "
                               "blocked or dead line looks exactly like an idle one")
            unit_out["vials"][str(v)] = vo
        out_units[u] = unit_out

    unreached = [u for u in units if not out_units.get(u, {}).get("ok")]
    if unreached:
        notes.append("not measured: %s -- see each unit's `reason`. Totals below cover only "
                     "the units that answered" % ", ".join(unreached))
    if vial is not None:
        notes.append("filtered to vial %d, so reservoir totals cover that vial only" % vial)

    return {
        "window": {"hours": window_h, "from": start.isoformat(), "to": now.isoformat()},
        "scope": {"unit": unit, "vial": vial},
        "event_fields": EVENT_FIELDS,
        "units": out_units,
        "totals_by_reservoir": {
            rid: {"mL": round(a["mL"], 3), "n_events": a["n_events"],
                  "vials": sorted(a["vials"]), "lines": sorted(a["lines"]),
                  "complete": a["complete"],
                  **({"incomplete_because": "unit(s) %s did not report the whole window"
                      % ", ".join(sorted(a["why_incomplete"]))} if a["why_incomplete"] else {})}
            for rid, a in sorted(by_res.items())},
        "totals_by_line": {lid: {k: (round(x, 3) if isinstance(x, float) else x)
                                 for k, x in t.items()} for lid, t in sorted(by_line.items())},
        "notes": notes,
    }
