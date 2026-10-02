"""append -> validate -> recompute -> commit (SERVER_DESIGN.md §3, §6 Phase 3).

The only place in this server that writes to the log repo. Every step is
deliberately conservative:

  1. Build a candidate event and append it to a deep-copied candidate log --
     never the real one.
  2. Run tools/lineage.py:recompute on the candidate, same as a human would
     run `tools/lineage.py --write` by hand.
  3. Validate the WHOLE candidate log against schema/evolution_log.schema
     .json, then against tools/lineage.py's own cross-field checks -- not
     just the new event in isolation. Reject on any failure; nothing is
     written yet.
  4. Prove the write is a pure append: every event that existed before must
     still exist afterward, byte-for-byte identical. This is what
     SERVER_DESIGN.md §3.A's 409 rule actually protects, checked directly
     rather than inferred from "the code above only appends".
  5. Write via a temp file + atomic rename, then git add + commit, author =
     operator. If the commit fails, revert the working tree to HEAD --
     evolution_log.json on disk must never sit ahead of git history, even
     transiently.

A single process-wide lock serializes all writes. Traffic here is a handful
of human operators, not a public API; a simple lock is proportionate, a
distributed one would not be.
"""
import copy
import datetime
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import jsonschema

from .auth import Operator
from .config import Settings
from .log_repo import lineage_module, load_log, load_schema

write_lock = threading.Lock()  # process-wide; also used by lines_writer.py


class WriteConflict(Exception):
    """The requested write cannot be expressed as a pure append, or targets
    something that doesn't exist. Maps to 409/404 in the route."""


class ValidationFailed(Exception):
    """The candidate log would fail schema or cross-field validation. Maps
    to 422 in the route. Nothing has been written when this is raised."""
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class CommitFailed(Exception):
    """The file write succeeded but git commit did not. The working tree has
    already been reverted to HEAD by the time this is raised -- the caller
    should treat this exactly like the write never happened, and retry."""


def _next_event_id(log: dict) -> str:
    n = log.get("log_meta", {}).get("event_counter", 0)
    return "EVT-%05d" % (n + 1)


def _parse_ts(ts: str) -> datetime.datetime | None:
    """None on anything unparseable, never a raised exception. This runs
    BEFORE schema validation (so the projection it feeds is itself covered
    by that validation step, per ISSUE_001), which means the timestamp
    reaching it hasn't been confirmed well-formed yet. A malformed
    timestamp must still surface as the schema's own 422, not a 500 from a
    side computation tripping over it first -- so here it just means
    "can't order this, skip" rather than a crash; validate_candidate() is
    what actually rejects the request."""
    try:
        return datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


# A grace window, not zero -- tolerates ordinary clock skew between an
# operator's client and this server, not a wrong day/year (which is what
# actually causes this in practice).
_FUTURE_GRACE = datetime.timedelta(hours=1)


def _reject_implausible_future_timestamp(timestamp: str) -> None:
    """Refuses (422, via ValidationFailed) an event timestamped more than
    _FUTURE_GRACE ahead of wall-clock now. Found by simulating real
    operator use: a future-dated level_reading was accepted with no
    complaint, and tools/media.py's GET /media (queried with no ``at``,
    i.e. real "now" -- which was BEFORE that future event) linearly
    extrapolated BACKWARD across it, manufacturing a confident, entirely
    fictitious "current" volume and hours-remaining for a reservoir whose
    own latest real reading said 0.0 L. That is exactly the "confident
    number, not a crash" failure mode CLAUDE.md names as the dangerous one
    here -- caught at the write boundary instead, for every event type, not
    just reservoir ones (an event records something that already happened;
    a future timestamp is never legitimate on its own terms, whatever kind
    of event it is)."""
    ts = _parse_ts(timestamp)
    if ts is None:
        return  # malformed -- schema validation downstream rejects this on its own terms
    now = datetime.datetime.now(datetime.timezone.utc)
    if ts > now + _FUTURE_GRACE:
        raise ValidationFailed(
            ["timestamp %s is more than %s in the future -- an event records something that "
             "already happened, not something scheduled; check the date/year" % (timestamp, _FUTURE_GRACE)]
        )


def build_event(log: dict, fields: dict[str, Any], operator: Operator) -> dict:
    _reject_implausible_future_timestamp(fields["timestamp"])
    event: dict[str, Any] = {
        "event_id": _next_event_id(log),
        "timestamp": fields["timestamp"],
        "event_type": fields["event_type"],
        "operator": operator.initials,
        "provenance": fields["provenance"],
        "params": fields.get("params") or {},
        "notes": fields["notes"],
    }
    if fields.get("missing_fields"):
        event["missing_fields"] = fields["missing_fields"]
    for key in ("timestamp_precision", "elapsed_h", "caused_by_event", "supersedes", "source_document"):
        if fields.get(key) is not None:
            event[key] = fields[key]

    # Bump the counter immediately, in `log` (the caller's working candidate),
    # so a SECOND build_event() call within the same request sees the next
    # id, not this same one again. Found by simulating a real split: POST
    # /lines' split/merge handlers call build_event() more than once per
    # request (one parent termination plus N child founding events, or N
    # parent terminations plus one child) -- previously the counter only
    # advanced once, at the very end of the whole pipeline (recompute), so
    # every event minted before that point collided on one shared event_id.
    # append_event()/create_line() still recompute this from the true
    # unique-event count as a final consistency check; this is what makes
    # each subsequent build_event() call within the SAME request correct in
    # the meantime, before that final recompute ever runs.
    log.setdefault("log_meta", {})["event_counter"] = log.get("log_meta", {}).get("event_counter", 0) + 1
    return event


def _check_elapsed_h(line: dict, event: dict) -> list[str]:
    """elapsed_h is documented as "hours since this line's own t0, if you
    want to record it explicitly." Found by simulating an elapsed_h-
    consistency probe: nothing checked this at all, so an elapsed_h off by
    5 orders of magnitude, or negative, was accepted and stored verbatim.

    The obvious-looking fix -- reject unless elapsed_h matches
    `timestamp - line["t0"]` closely -- was checked against real data
    BEFORE shipping and turned out to be wrong: 5 real historical events
    (EVT-00267/269/271/273/275, a batch of sampling events logged across 5
    DIFFERENT lines at one shared timestamp) all carry the identical
    elapsed_h (119.3), which matches each line's OWN t0 only for one of
    them -- off by up to ~96h for the others. elapsed_h evidently can
    legitimately be reckoned from a shared/facility reference point, not
    only from the individual line's own t0, for an event describing a
    batch action. Enforcing "matches own t0" would have rejected a real,
    already-used convention.

    What DOES hold, checked against all 166 real elapsed_h values in the
    log (zero negative): elapsed_h can never be negative, regardless of
    which reference point it's measured from -- no legitimate reference
    point is ever in the future relative to the event it's attached to.
    That's the one universal invariant enforced here; the tighter "must
    match this line's own t0" check is deliberately NOT implemented."""
    elapsed_h = event.get("elapsed_h")
    if elapsed_h is None or elapsed_h >= 0:
        return []
    return ["elapsed_h %s is negative -- elapsed_h can never be negative, whatever reference "
            "point it's measured from" % elapsed_h]


def _check_controller_config_change_adequate(event: dict) -> list[str]:
    """A controller_config_change event with no controller_parameter, or
    with nothing describing what actually changed, records that SOMETHING
    changed without saying what -- useless to anyone reading it back later
    to understand a run's history. This is the "adequately logged" half of
    the config-generation feature (app/config_writer.py never touches
    evolution_log.json itself; POST /events is where a config change is
    actually recorded, per the operator's explicit instruction).

    Checked against all 17 real controller_config_change events in the log:
    every one names controller_parameter, and every one carries at least
    one more params key describing the change itself. Only target_ramp
    changes have a consistent value/previous-value naming convention so far
    (ramp_step_size/previous_ramp_step_size, from EVT-00088) -- enforced
    exactly for that one. Every other controller_parameter seen in the log
    (exp_name, setpoint, input_pump2) is only checked for "names something
    more than just its own name", since no consistent convention exists yet
    for those to check against -- setpoint/input_pump2 only appear in fault
    records to date, never a deliberate config-change record. Tightening
    those without real precedent would be exactly the kind of invented rule
    CLAUDE.md warns against; left for whoever adds the next mode."""
    if event.get("event_type") != "controller_config_change":
        return []
    params = event.get("params") or {}
    controller_parameter = params.get("controller_parameter")
    if not controller_parameter:
        return ["a controller_config_change event must name params.controller_parameter -- "
                "which controller setting changed"]
    if len(params) < 2:
        return ["controller_config_change for %r names no other params -- record what changed, "
                "not just that something did" % controller_parameter]
    # Normalized comparison, not ==: found by simulating a case/whitespace-
    # variant operator -- "Target_Ramp", "TARGET_RAMP", and " target_ramp"
    # (leading space) all silently skipped the ramp_step_size/
    # previous_ramp_step_size requirement below under a bare ==, since
    # controller_parameter's own registry entry has no enum to reject the
    # casing variant earlier. Skipping this check is exactly the failure
    # mode it exists to prevent: a "target_ramp changed" event with no
    # before/after values at all.
    normalized = controller_parameter.strip().lower() if isinstance(controller_parameter, str) else None
    if normalized == "target_ramp":
        for required in ("ramp_step_size", "previous_ramp_step_size"):
            if required not in params:
                return ["controller_config_change for target_ramp must carry %r, matching every "
                        "existing target_ramp change in the log (e.g. EVT-00088)" % required]
    return []


def assert_known_unit(candidate: dict, unit: str, context: str) -> None:
    """Adding a real new hardware unit is a human-only, hardware-config
    change (README.md/GET /skill: this API cannot touch hardware config) --
    so any unit named as a destination must already be listed in
    hardware.units. Originally lines_writer.py-only (branch/split/restart
    destinations); moved here so project_line_state's hardware_swap
    relocation (ISSUE_002) can share the exact same check rather than a
    second, driftable copy -- writer.py is the module lines_writer.py
    already imports from, not the other way around, so this direction
    avoids a circular import. Found by simulating a confused/malformed-
    input operator: a wrong-case ("Patrick"), trailing-space ("patrick "),
    or empty unit string was previously only ever rejected as an accidental
    side effect of the resulting line_id failing its regex -- a real but
    capitalized unit name produced a confusing multi-error cascade pointing
    at the vial/id rather than plainly saying the unit itself was the
    problem."""
    known = candidate.get("hardware", {}).get("units", {})
    if unit not in known:
        raise ValidationFailed(
            ["%s: %r is not a known hardware unit -- must be one of %s (adding a new "
             "unit is a hardware-config change, outside this API)" % (context, unit, sorted(known))]
        )


def assert_destination_empty(candidate: dict, unit: str, vial: int, context: str,
                              exclude_line_id: str | None = None) -> None:
    """Moved here alongside assert_known_unit -- see that function's
    docstring for why. exclude_line_id is new (ISSUE_002, hardware_swap
    relocation): when a line's OWN destination is being checked (has it
    moved to a vial some OTHER active line already holds?), the line
    itself is still sitting at its old position at the time this runs --
    if that old position happens to equal the new one (a "confirm it
    stayed put" event), the line would otherwise see itself as the
    conflicting occupant. branch/split/restart never pass this (a
    genuinely new line can never already occupy anything), so the default
    of None leaves their behavior exactly as it was."""
    for lid, line in candidate.get("lines", {}).items():
        if lid == exclude_line_id:
            continue
        if line.get("unit") == unit and line.get("vial") == vial and line.get("status") == "active":
            raise WriteConflict(
                "%s: %s vial %d is already occupied by active line %s" % (context, unit, vial, lid)
            )


def _referenced_events_exist(candidate: dict, event: dict, lineage) -> list[str]:
    existing = lineage.unique_events(candidate)
    problems = []
    for key in ("caused_by_event", "supersedes"):
        ref = event.get(key)
        if ref is not None and ref not in existing:
            problems.append("%s references %s, which does not exist" % (key, ref))
    return problems


def _latest_reservoir_event_ts(candidate: dict, reservoir_id: str, exclude_event_id: str) -> datetime.datetime | None:
    """The latest timestamp among all EXISTING level_reading/media_prep
    events already in the log for this reservoir_id, excluding the event
    currently being appended (which is already sitting in `candidate` by
    the time this runs).

    Found necessary by simulating real operator use: the forward-only
    guard originally compared a new event only against
    reservoirs[].level_as_of. That field is itself only as fresh as the
    LAST event that successfully projected -- if some earlier event for
    this reservoir_id already exists in history but was never projected
    (the exact shape ISSUE_001 describes, and exactly what a not-yet-
    caught-up log still has right now for at least one real reservoir), a
    NEW event that is chronologically older than that already-recorded
    reading can still look like "moving forward" relative to the stale
    level_as_of, and get applied -- silently regressing the projection
    behind history that was already there. Scanning full history instead
    of trusting the field alone catches that regardless of why the field
    fell behind, and costs nothing once the field IS caught up (the two
    agree in that case, since the field was always set FROM some event
    already counted here)."""
    latest = None

    def consider(e):
        nonlocal latest
        if e.get("event_id") == exclude_event_id:
            return
        if e.get("event_type") not in ("level_reading", "media_prep"):
            return
        if (e.get("params") or {}).get("reservoir_id") != reservoir_id:
            return
        ts = _parse_ts(e.get("timestamp"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts

    for line in candidate.get("lines", {}).values():
        for e in line.get("events", []):
            consider(e)
    for e in candidate.get("experiment_events", []):
        consider(e)
    return latest


def advance_last_updated(candidate: dict, timestamp: str) -> None:
    """log_meta.last_updated moves to an event's own timestamp, never wall-
    clock now, and only forward -- an event logged an hour late must not
    make the log claim to be current as of right now (ISSUE_001 §2)."""
    meta = candidate.setdefault("log_meta", {})
    current = meta.get("last_updated")
    new_ts, cur_ts = _parse_ts(timestamp), _parse_ts(current) if current else None
    if new_ts is not None and (cur_ts is None or new_ts > cur_ts):
        meta["last_updated"] = timestamp


# Recognized here so a caller always gets an explicit reservoir_projection
# report for all five, even when nothing was actually touched --
# ISSUE_001's "do not let [reservoir_swap] fall through the reservoir_id
# path and update nothing" applies to any event type that TALKS about
# reservoir state without silently succeeding at nothing.
_RESERVOIR_EVENT_TYPES = frozenset({
    "level_reading", "media_prep", "reservoir_swap", "reservoir_retired", "reservoir_change",
})


def _create_reservoir(candidate: dict, reservoir_id: str, params: dict, event: dict) -> dict | None:
    """A media_prep naming a reservoir_id that doesn't exist yet CREATES it,
    if (and only if) the event supplies everything reservoirItem requires
    (schema: id, unit, media, role, pg, status, volume_prepared,
    prepared_at) -- media (already registered, no applies_to_event_types
    restriction), role, pg_concentration, and volume_prepared (all three
    already registered specifically FOR media_prep, since a real media_prep
    always carried them anyway) are ALL that's missing to go from "narrates
    a new reservoir" to "the reservoir actually exists" -- reservoir_change/
    reservoir_swap, which narrate the SAME fact today, stay exactly as
    unprojected as before this (their multi-position, multi-unit shape is
    still genuinely ambiguous -- see this function's own reservoir_change/
    reservoir_swap branches).

    Real motivating gap: there was no way to bring a NEW reservoir online
    via this API at all -- not just "reactivate a retired one," ANY new
    reservoir_id, ever, required a direct hand-edit to evolution_log.json,
    despite reservoirs (unlike hardware/design/experiment) being schema-
    validated, not narrative (LOG_PROTOCOL.md §3), and despite this being a
    routine, recurring operational need (the real log already has 10
    reservoir items and 3 reservoir_change events, none of which could have
    come from this API). Every real historical reservoir_change has always
    been paired with a same-timestamp media_prep for each new id (e.g.
    EVT-00025 -> EVT-00043/00044) -- this hooks the creation into the event
    that already, in practice, carries the volume, keeping reservoir_change
    itself unchanged rather than inventing a new multi-value shape for it
    to carry per-unit volumes for potentially several new ids at once.

    `unit` is derived from reservoir_id's own `<unit>/<media>-<pg>` shape
    (schema: reservoirId's pattern guarantees exactly one `/`) -- a
    structural fact of the id, not a guess, the same precedent
    app/line_ids.py's OCCUPANCY_RE already uses for line ids. Checked
    against hardware.units via assert_known_unit, same check branch/split/
    restart/hardware_swap already share, so a typo'd unit prefix is refused
    (422) rather than silently creating a reservoir for a unit that doesn't
    exist.

    Returns None (never partially creates anything) if the event is
    missing any of media/role/pg_concentration/volume_prepared -- falls
    through to the existing, unchanged "no reservoir with this id -- event
    recorded, nothing projected" message, exactly like today for anyone not
    using this new capability."""
    media = params.get("media")
    role = params.get("role")
    pg = params.get("pg_concentration")
    vol = params.get("volume_prepared")
    if media is None or role is None or pg is None or vol is None:
        return None

    unit = reservoir_id.split("/", 1)[0]
    assert_known_unit(candidate, unit, "new reservoir (media_prep)")

    reservoir = {
        "id": reservoir_id, "unit": unit, "media": media, "role": role, "pg": pg,
        "status": "active",
        "volume_prepared": vol, "prepared_at": event["timestamp"], "prepared_by_event": event["event_id"],
        "current_volume": vol, "level_as_of": event["timestamp"], "level_source": "prepared",
        "level_qualifier": "exact", "level_set_by_event": event["event_id"],
    }
    candidate.setdefault("reservoirs", {}).setdefault("items", []).append(reservoir)
    return {
        "event_type": "media_prep", "reservoir_id": reservoir_id, "projected": True, "created": True,
        "touched": ["id", "unit", "media", "role", "pg", "status", "volume_prepared", "prepared_at",
                    "prepared_by_event", "current_volume", "level_as_of", "level_source",
                    "level_qualifier", "level_set_by_event"],
    }


def project_reservoir_state(candidate: dict, event: dict) -> dict | None:
    """Applies a level_reading/media_prep/reservoir_retired event's effect
    onto the named reservoir in candidate["reservoirs"]["items"], in place
    (ISSUE_001 §1). reservoir_swap and reservoir_change are recognized but
    deliberately NOT auto-projected -- both explicitly report why rather
    than silently doing nothing. Returns a report describing what was
    touched/left untouched and why, or None if this event_type has no
    reservoir-projection meaning at all (the overwhelming majority of event
    types -- inoculation, note, sampling, ...). Never invents a value: a
    column this event's params doesn't supply is left exactly as it was,
    and named in the report rather than silently guessed at or silently
    left unmentioned.
    """
    event_type = event["event_type"]
    if event_type not in _RESERVOIR_EVENT_TYPES:
        return None
    params = event.get("params") or {}

    if event_type == "reservoir_swap":
        # Names several positions via reservoir_ids + volume_to_<unit> keys,
        # not a single reservoir_id -- a genuinely different shape (moving
        # a bottle between units), out of scope for this projection rather
        # than guessed at. Explicit, not a silent no-op: the caller must be
        # told this needs a manual reservoirs[] update, not left to assume
        # it happened the way level_reading/media_prep do.
        return {
            "event_type": event_type, "projected": False,
            "reason": "reservoir_swap's multi-position shape (reservoir_ids, volume_to_<unit>) "
                      "is not auto-projected here -- update reservoirs[] by hand for this event",
        }

    if event_type == "reservoir_change":
        # Facility-level, and structurally unlike the rest of this function:
        # reservoir_id_from/reservoir_id_to are each allowed to be an ARRAY
        # (one reservoir_change can retire/introduce a position on every unit
        # at once -- see the real EVT-00025/EVT-00195), and what it implies
        # for the FROM reservoirs' status is genuinely ambiguous from the
        # params alone (retired outright, vs. still active for lines this
        # event doesn't name in lines_affected). Guessing either way risks
        # exactly the "confident wrong number" CLAUDE.md warns about -- out
        # of scope here, same as reservoir_swap, not silently no-opped.
        return {
            "event_type": event_type, "projected": False,
            "reason": "reservoir_change's multi-position, multi-unit shape (reservoir_id_from/"
                      "_to as arrays) is not auto-projected here -- update reservoirs[] by hand "
                      "for this event (and consider whether the FROM reservoir(s) also need a "
                      "reservoir_retired event logged)",
        }

    reservoir_id = params.get("reservoir_id")
    if not reservoir_id:
        return {"event_type": event_type, "projected": False,
                "reason": "params.reservoir_id missing -- nothing to project against"}

    items = candidate.get("reservoirs", {}).get("items", [])
    reservoir = next((r for r in items if r.get("id") == reservoir_id), None)
    if reservoir is None:
        if event_type == "media_prep":
            created = _create_reservoir(candidate, reservoir_id, params, event)
            if created is not None:
                return created
        return {"event_type": event_type, "reservoir_id": reservoir_id, "projected": False,
                "reason": "no reservoir with this id -- event recorded, nothing projected"}

    if event_type == "reservoir_retired":
        # A single, unambiguous field flip -- reservoirItem.status's enum is
        # exactly {active, retired} -- unlike reservoir_change, safe to
        # apply directly. Not time-ordered the way level_as_of is: retiring
        # doesn't compete with a later or earlier reading, so no forward-
        # only check applies here.
        if reservoir.get("status") == "retired":
            return {"event_type": event_type, "reservoir_id": reservoir_id, "projected": False,
                    "reason": "reservoir is already retired -- nothing moved"}
        reservoir["status"] = "retired"
        return {"event_type": event_type, "reservoir_id": reservoir_id, "projected": True,
                "touched": ["status"]}

    # Compared against the latest EXISTING event in full history, not just
    # reservoirs[].level_as_of -- see _latest_reservoir_event_ts's docstring
    # for why the field alone isn't safe to trust here.
    old_ts = _latest_reservoir_event_ts(candidate, reservoir_id, event["event_id"])
    new_ts = _parse_ts(event["timestamp"])
    # new_ts is None (event's own timestamp doesn't parse) is treated the
    # same as "provably older" -- safe either way, since a malformed
    # timestamp gets rejected by schema validation right after this and
    # nothing here is written unless that passes; this only has to not crash.
    if old_ts is not None and (new_ts is None or new_ts < old_ts):
        return {
            "event_type": event_type, "reservoir_id": reservoir_id, "projected": False,
            "reason": "event predates a reading already recorded for this reservoir -- projection "
                      "only ever moves forward, a backfilled reading must not overwrite a newer one",
        }

    if event_type == "level_reading":
        vol = params.get("volume_remaining")
        if vol is None:
            # A real production incident, not a hypothetical: level_reading's
            # entire purpose is reporting a volume, and (before this was a
            # hard rejection) an event naming a reservoir_id with no
            # volume_remaining was a soft no-op -- accepted, projected
            # nothing, and left a real gap in this reservoir's read-side
            # consumers (see project_reservoir_state's media_prep sibling
            # check and app/routes/media.py's _drop_events_readings_for_
            # cant_survive for the media_prep half of the same incident).
            # 29 of 31 real media_prep events (the same pattern) already
            # supply their volume; the 2 that didn't were a one-time
            # mistake, not a legitimate convention -- unlike elapsed_h
            # (see writer's own _check_elapsed_h docstring), so this one IS
            # safe to reject outright. A concentration-only correction
            # belongs on a `supersedes` of the ORIGINAL prep/reading event
            # that set the volume, not a new one with no volume at all.
            raise ValidationFailed(
                ["params.volume_remaining is required whenever params.reservoir_id is set on a "
                 "level_reading -- its whole purpose is reporting a volume; a concentration-only "
                 "correction belongs on a supersedes of the event that set the volume, not a new "
                 "one with no volume at all"]
            )
        reservoir["current_volume"] = vol
        reservoir["level_as_of"] = event["timestamp"]
        reservoir["level_set_by_event"] = event["event_id"]
        touched = ["current_volume", "level_as_of", "level_set_by_event"]
        untouched = []
        # level_source/level_qualifier can each be legitimately absent even
        # when volume_remaining is present -- handled independently, never
        # forcing a value onto a column this event didn't actually supply.
        for src_key, dst_key in (("level_source", "level_source"), ("measurement_qualifier", "level_qualifier")):
            if params.get(src_key) is not None:
                reservoir[dst_key] = params[src_key]
                touched.append(dst_key)
            else:
                untouched.append(dst_key)
        return {"event_type": event_type, "reservoir_id": reservoir_id, "projected": True,
                "touched": touched, "untouched": untouched}

    # media_prep
    vol = params.get("volume_prepared")
    if vol is None:
        # The real production incident this whole function's media_prep/
        # level_reading rejection now guards against: two live events
        # (media_prep on patrick/M9-1 and plankton/M9-1, recording only a
        # pg_concentration correction, no volume_prepared) were accepted
        # here as a soft no-op, then took GET /media down for every caller
        # with a bare 500 (tools/media.py's readings_for() dereferences
        # params.volume_prepared["value"] unconditionally for any
        # reservoir_id-bearing media_prep -- see app/routes/media.py's
        # _drop_events_readings_for_cant_survive, the read-side fix for the
        # two events that already got through before this existed). 29 of
        # 31 real media_prep events already supply volume_prepared; those
        # two were a one-time mistake, not a legitimate convention.
        raise ValidationFailed(
            ["params.volume_prepared is required whenever params.reservoir_id is set on a "
             "media_prep -- its whole purpose is recording a prepared volume; a concentration-"
             "only correction belongs on a supersedes of the event that set the volume, not a "
             "new one with no volume at all"]
        )
    reactivate = params.get("reactivate")
    if reactivate is not None and not isinstance(reactivate, bool):
        # Found by simulating adversarial params, same shape as vacate's own
        # type check: an untyped params dict lets a string "true" or an
        # integer 1 arrive where only an actual bool should.
        raise ValidationFailed(
            ["media_prep params.reactivate must be a boolean (got %r)" % (reactivate,)]
        )
    was_retired = reservoir.get("status") == "retired"
    # Close the outgoing bottle into fill_history BEFORE overwriting the
    # reservoir's own prepared_at/volume_prepared/current_volume with the
    # new bottle's values -- this is the only place the old bottle's final
    # state is ever recorded.
    reservoir.setdefault("fill_history", []).append({
        "prepared_at": reservoir.get("prepared_at"),
        "volume_prepared": reservoir.get("volume_prepared"),
        "retired_at": event["timestamp"],
        "remaining_at_swap": reservoir.get("current_volume"),
    })
    reservoir["prepared_at"] = event["timestamp"]
    reservoir["volume_prepared"] = vol
    reservoir["prepared_by_event"] = event["event_id"]
    reservoir["current_volume"] = vol
    reservoir["level_as_of"] = event["timestamp"]
    reservoir["level_source"] = "prepared"
    reservoir["level_qualifier"] = "exact"  # a freshly-prepared volume is exact by construction, not a guess
    reservoir["level_set_by_event"] = event["event_id"]
    touched = ["prepared_at", "volume_prepared", "prepared_by_event", "current_volume",
               "level_as_of", "level_source", "level_qualifier", "level_set_by_event", "fill_history"]
    untouched = []
    # pg_concentration is a REGISTERED media_prep param (real usage confirms
    # it means the concentration this bottle was prepared at, matching
    # reservoirItem.pg exactly -- e.g. the real EVT-00032/EVT-00034 pair,
    # "Prepared 1 L of LB at 0 g/L PG for the plankton low reservoir"/"...at
    # 5 g/L..."). Found by simulating a "mistaken reservoir concentration"
    # operator: media_prep moved every OTHER field a prep implies, but never
    # this one, so a reservoir's own displayed `pg` stayed wrong forever --
    # not even a corrective media_prep event carrying supersedes could ever
    # fix it. Never invented if the event doesn't supply it.
    if params.get("pg_concentration") is not None:
        reservoir["pg"] = params["pg_concentration"]
        touched.append("pg")
    else:
        untouched.append("pg")

    result = {
        "event_type": event_type, "reservoir_id": reservoir_id, "projected": True,
        "touched": touched, "untouched": untouched,
    }
    # Real, confirmed gap: a media_prep against an EXISTING, currently-
    # retired reservoir used to move volume/pg/current_volume exactly as
    # above but leave status untouched -- "projected: true" with no hint
    # the reservoir was, and silently remained, retired. Explicit, not
    # inferred from the mere presence of a media_prep: a retired status is
    # a deliberate administrative fact (contamination, decommissioned),
    # and flipping it back as an unannounced side effect of ANY media_prep
    # that happens to name that id would risk exactly the silent,
    # unintended reactivation this flag exists to prevent -- the same
    # reasoning hardware_swap's own vacate flag was built on.
    if was_retired:
        if reactivate:
            reservoir["status"] = "active"
            touched.append("status")
        else:
            result["note"] = (
                "this reservoir was retired -- volume/pg were still recorded (a media_prep's "
                "physical fact is never withheld), but status was NOT changed; resubmit with "
                "params.reactivate: true if this media_prep is meant to bring it back into "
                "active service"
            )
    elif reactivate:
        # Reactivating an already-active reservoir is a harmless no-op, not
        # an error -- mirrors hardware_swap's own vacate precedent for
        # confirming state that already holds.
        result["note"] = "reactivate: true had nothing to do -- this reservoir was already active"
    return result


# The gap this closes (found by simulating real operator use, not from a
# spec): POST /events happily appended a termination event to a line's
# events[] with ZERO side effects. line.status stayed "active",
# lineage.terminated_at/terminated_by_event stayed null -- and since
# GET /vials derives occupancy from status alone (app/routes/vials.py), and
# every begin_mode's destination check in lines_writer.py does the same, the
# vial silently kept reading as occupied, blocking any future
# branch/restart/split/merge into it. media_switch had the identical shape:
# registered as a real event_type, changes a real denormalized field
# (line.current_media), and POST /events never touched it either.
#
# hardware_swap is recognized here too, but NEVER projected (see below) --
# unlike termination/media_switch, its real parameter_registry entry (found
# by simulating a real hardware-fault operator) has no new_unit/new_vial
# field at all, only free-text what_moved/reason. Adding one would mean
# inventing a registry entry this log doesn't actually have -- a decision
# for whoever owns evolution_log.json's registry, not this server. Listed
# here so it gets an explicit, honest report instead of silently doing
# nothing (the same treatment reservoir_swap/reservoir_change already get
# in project_reservoir_state, for the identical reason: ambiguous enough
# that guessing risked the "confident wrong number" this log exists to
# prevent).
_LINE_LIFECYCLE_EVENT_TYPES = frozenset({"termination", "media_switch", "hardware_swap"})


def _check_previous_position(line: dict, params: dict, line_id: str) -> None:
    """Shared by hardware_swap's relocate AND vacate branches (previously
    two copy-pasted blocks, one per branch): optional previous_unit/
    previous_vial, when supplied, must match the line's actual current
    unit/vial, mirroring media_switch's own media_from check exactly,
    including the exception type (WriteConflict, 409).

    Explicit isinstance guards on the line's own real value before
    comparing -- found by simulating adversarial params during round-1
    testing of the vacate follow-up: `previous_vial: true` or
    `previous_vial: 1.0` against a real vial of 1 both pass Python's bare
    `!=` silently (`True == 1` and `1.0 == 1`), so a wrong-typed
    previous_vial that happens to be numerically equal to the real value
    would slip through this check entirely. The only reason that isn't a
    real hole today is that tools/lineage.py's check_param_types (a
    SEPARATE, downstream validator) independently rejects a bool/float
    previous_vial by its registered type before the write ever completes
    -- correct in practice, but relying on that one shared check to be
    what actually saves this one is fragile, not a deliberate defense in
    depth. Guarding directly here means this check is correct on its own,
    with no dependency on a second validator agreeing."""
    previous_unit = params.get("previous_unit")
    if previous_unit is not None:
        current_unit = line.get("unit")
        if not isinstance(previous_unit, str) or previous_unit != current_unit:
            raise WriteConflict(
                "line %r has unit %r, not the %r this hardware_swap's previous_unit claims -- "
                "refused; re-check the line's actual current unit before retrying" %
                (line_id, current_unit, previous_unit)
            )
    previous_vial = params.get("previous_vial")
    if previous_vial is not None:
        current_vial = line.get("vial")
        if (not isinstance(previous_vial, int) or isinstance(previous_vial, bool)
                or previous_vial != current_vial):
            raise WriteConflict(
                "line %r has vial %r, not the %r this hardware_swap's previous_vial claims -- "
                "refused; re-check the line's actual current vial before retrying" %
                (line_id, current_vial, previous_vial)
            )


def project_line_state(candidate: dict, target: dict, event: dict) -> dict | None:
    """Applies a termination/media_switch event's effect onto the line it's
    scoped to, in place. Returns a report (same shape/spirit as
    project_reservoir_state's: what was applied, or why not), or None if
    this event_type has no line-lifecycle meaning at all (the overwhelming
    majority of event types). Mirrors project_reservoir_state's other
    invariants too: never invents a value, and is explicit rather than
    silent when it declines to act.

    Unlike project_reservoir_state, a mismatch here can RAISE WriteConflict
    (409) rather than only report it: an already-ended line getting a
    second, unlinked termination, or a media_switch whose media_from
    contradicts the line's actual current_media, are both contradictions of
    already-recorded state, not merely gaps in what params happened to
    supply -- found by simulating real operator use, where the previous
    soft-warn-but-apply-anyway behavior was exactly the "confident wrong
    number" failure mode this project's own docs warn against elsewhere.
    Since this runs before validate_candidate/assert_pure_append/
    write_and_commit in append_event()'s pipeline, raising here aborts the
    whole write -- nothing is persisted, matching every other WriteConflict
    in this server.

    POST /lines' own embedded terminations (_terminate in lines_writer.py,
    used by split/merge/restart) already flip these same fields for the
    lines THEY end -- this covers a termination logged on its own via
    POST /events, which is the far more common case (21 of 21 real
    terminations in the log to date were logged this way, historically by
    hand)."""
    event_type = event["event_type"]
    if event_type not in _LINE_LIFECYCLE_EVENT_TYPES:
        return None

    line_id = target.get("line_id")
    if line_id is None:
        # A facility-scoped termination/media_switch names no line to
        # update at all -- recorded, but there is nothing to apply this to.
        return {"event_type": event_type, "applied": False,
                "reason": "%s must be line-scoped (target.line_id) -- a facility-scoped event "
                          "names no line to update" % event_type}
    line = candidate["lines"][line_id]  # existence already checked earlier in append_event
    params = event.get("params") or {}

    if event_type == "hardware_swap":
        # ISSUE_002: hardware_swap gained structured new_unit/new_vial (and
        # optional previous_unit/previous_vial) params -- registered
        # 2026-09-01, real precedent none yet. A relocation is projected
        # only when BOTH new_unit and new_vial are given; neither, or only
        # one, is treated exactly like a hardware_swap that isn't a
        # relocation at all (a pure calibration/IP-change record) --
        # line.unit/vial stay untouched, same as before this existed.
        # line_id itself is NEVER touched by any of this, on purpose: it is
        # a permanent label, not a live description of where a culture
        # currently sits (LOG_PROTOCOL.md §4, "line identity follows the
        # culture, not the hardware") -- see ISSUE_002 for why minting a
        # new id to "fix" a mismatched label would be far more dangerous
        # than leaving it stale (it would fabricate a branch/restart that
        # never happened, in append-only history that can't take it back).
        new_unit = params.get("new_unit")
        new_vial = params.get("new_vial")
        vacate = params.get("vacate")
        if vacate is not None and not isinstance(vacate, bool):
            # Found by simulating adversarial params, same shape as the
            # new_unit non-string check just below: an untyped params dict
            # lets a string "true" or an integer 1 arrive where only an
            # actual bool should, and truthiness would silently accept
            # either -- reject outright rather than guess what was meant.
            raise ValidationFailed(
                ["hardware_swap params.vacate must be a boolean (got %r)" % (vacate,)]
            )
        if vacate and (new_unit is not None or new_vial is not None):
            # "not on evolver" redesign (ISSUE_002 follow-up): vacate and a
            # relocation are two different facts, never one event -- a
            # reciprocal swap between two lines works ONLY because each
            # line's vacate is its own event, fully applied (line.unit/vial
            # both null) before either line's relocation is attempted, so
            # assert_destination_empty never sees two lines contesting the
            # same real vial at once. Combining both in one event would
            # re-introduce exactly that race, not avoid it.
            raise ValidationFailed(
                ["hardware_swap params.vacate cannot be combined with new_unit/new_vial in the same "
                 "event -- vacate the line first (its own event, line.unit/vial become null), then "
                 "relocate it to a real position in a separate hardware_swap"]
            )
        # previous_unit/previous_vial, when supplied, are validated against
        # the line's real current position UNCONDITIONALLY here -- for
        # every hardware_swap shape, not just a relocation or a vacate.
        # Found necessary by round-3 adversarial testing: a hardware_swap
        # naming NEITHER new_unit/new_vial NOR vacate (a pure calibration/
        # IP-change note, or previous_unit/previous_vial with nothing else
        # at all) used to return "applied: false" below without this check
        # ever running -- an unvalidated, possibly wrong previous_unit/
        # previous_vial then sat permanently in this line's own recorded
        # history, and app/line_ids.py:last_real_position (which reads
        # previous_unit/previous_vial off ANY of a line's hardware_swap
        # events, trusting that project_line_state already validated
        # every one it walks) had no way to know this particular one never
        # was. Checking it here, before any of the branches below, closes
        # that gap at the one place that can actually enforce it: write
        # time -- append-only history can't take a bad claim back once
        # committed.
        _check_previous_position(line, params, line_id)

        if vacate:
            if line.get("unit") is None and line.get("vial") is None:
                # Idempotent, not a conflict -- the same precedent
                # media_switch already sets for media_from matching
                # current_media: confirming state that already holds is not
                # a contradiction of recorded history, unlike a mismatch.
                return {"event_type": event_type, "applied": True, "touched": [],
                        "reason": "line was already off-evolver (unit/vial already null) -- this "
                                  "vacate re-confirms that, nothing changed"}
            line["unit"] = None
            line["vial"] = None
            return {"event_type": event_type, "applied": True, "touched": ["unit", "vial"],
                    "reason": "line vacated -- unit/vial set to null, a real 'not currently on any "
                              "evolver' state, until a future hardware_swap relocates it"}
        if new_unit is None and new_vial is None:
            return {
                "event_type": event_type, "applied": False,
                "reason": "hardware_swap has no structured destination fields in the parameter_registry "
                          "(only free-text what_moved/reason) -- line.unit/vial are NOT updated by this "
                          "event; GET /vials will report the old position as still occupied and the new "
                          "one as still empty until a human updates line.unit/vial by hand",
            }
        if new_unit is None or new_vial is None:
            return {
                "event_type": event_type, "applied": False,
                "reason": "hardware_swap names only one of new_unit/new_vial -- a relocation needs BOTH "
                          "to project (line.unit/vial are moved together or not at all); this event was "
                          "recorded but line.unit/vial are untouched",
            }
        if not isinstance(new_vial, int) or isinstance(new_vial, bool) or not (0 <= new_vial <= 15):
            raise ValidationFailed(
                ["hardware_swap params.new_vial must be an integer 0-15 (got %r)" % (new_vial,)]
            )
        if not isinstance(new_unit, str):
            # Found by simulating adversarial params: unlike branch/split/
            # restart's own destinations (Pydantic-typed `unit: str` on the
            # request model, so a non-string never reaches this far),
            # hardware_swap's new_unit arrives through the untyped `params`
            # dict -- assert_known_unit's `unit not in known` requires unit
            # to be hashable, so a list/dict value crashed with an unhandled
            # TypeError instead of a clean 422.
            raise ValidationFailed(
                ["hardware_swap params.new_unit must be a string (got %r)" % (new_unit,)]
            )

        assert_known_unit(candidate, new_unit, "hardware_swap destination")
        assert_destination_empty(candidate, new_unit, new_vial, "hardware_swap destination",
                                  exclude_line_id=line_id)
        # previous_unit/previous_vial already validated, unconditionally,
        # above -- before new_unit/new_vial's own shape checks even ran.

        line["unit"] = new_unit
        line["vial"] = new_vial
        return {"event_type": event_type, "applied": True, "touched": ["unit", "vial"]}

    if event_type == "termination":
        if line.get("status") != "active":
            existing = line.get("lineage", {}).get("terminated_by_event")
            if existing is not None and event.get("supersedes") == existing:
                # A correction to the existing termination (wrong reason,
                # wrong timestamp, ...) -- supersedes says so explicitly,
                # so move terminated_at/terminated_by_event to the
                # correcting event rather than leaving them pointing at
                # what was just superseded.
                line["lineage"]["terminated_by_event"] = event["event_id"]
                line["lineage"]["terminated_at"] = event["timestamp"]
                return {"event_type": event_type, "applied": True,
                        "reason": "supersedes the line's existing termination -- terminated_at/"
                                  "terminated_by_event moved to this event"}
            # Found by simulating real operator use: this used to be a soft
            # no-op (event appended, report says nothing moved). Two
            # independent simulated operators both hit variants of "the API
            # silently accepts a write that contradicts recorded state" and
            # flagged it as a real gap in an otherwise careful
            # append-only/supersedes model -- a second, UNLINKED termination
            # naming a different reason for an already-ended line is almost
            # certainly an operator mistake (wrong line_id, or a forgotten
            # supersedes), not a fact worth recording twice with no
            # structural link between the two. Reject outright, matching
            # the precedent POST /lines' own _terminate already set for
            # "line not in the right state for this operation" (409, not a
            # silent accept). Correcting the existing termination is still
            # fully supported -- supersedes it explicitly.
            raise WriteConflict(
                "line %r is already ended (by %s) -- a second termination event doesn't move "
                "anything; supersedes the existing termination event to correct it instead of "
                "logging an unrelated second one" % (line_id, existing)
            )
        line["status"] = "ended"
        line["lineage"]["terminated_by_event"] = event["event_id"]
        line["lineage"]["terminated_at"] = event["timestamp"]
        return {"event_type": event_type, "applied": True}

    # media_switch -- params already set above, shared with hardware_swap
    media_to = params.get("media_to")
    if media_to is None:
        return {"event_type": event_type, "applied": False,
                "reason": "params.media_to missing -- nothing to project"}
    if line.get("mode") != "switch":
        # media_switch's own registry description is explicit: "for a
        # switch-mode line." Applying one to a constant-mode line produces
        # a line whose current_media has moved despite its own mode field
        # asserting that can't happen -- an internally contradictory
        # record, the same class of "confident wrong number" as the
        # media_from mismatch below. Found by simulating a real operator
        # applying media_switch to the wrong (constant-mode) line by
        # mistake. Reject rather than silently accept: if a constant-mode
        # line's media genuinely needs to change, that's a fact about the
        # line itself (current_media set at creation, or a new event type
        # this registry doesn't have yet), not what media_switch means.
        raise WriteConflict(
            "line %r is mode=%r, not 'switch' -- media_switch only applies to switch-mode lines "
            "per its own registry description; refused" % (line_id, line.get("mode"))
        )
    media_from = params.get("media_from")
    if media_from is not None and line.get("current_media") != media_from:
        # Found by simulating real operator use: this used to be a soft
        # warning-but-apply-anyway -- exactly the "confident wrong number"
        # failure mode this project's docs warn against elsewhere. A
        # media_from that doesn't match what's actually recorded means the
        # caller's picture of the line is stale or simply wrong; applying
        # media_to anyway would silently commit current_media to a value
        # premised on a false starting point. Reject rather than warn-and-
        # proceed -- the caller should re-check GET /lines/{line_id} and
        # retry with the correct media_from (or omit it, which is not an
        # error -- see below).
        raise WriteConflict(
            "line %r has current_media %r, not the %r this media_switch's media_from claims -- "
            "refused; re-check the line's actual current_media before retrying" %
            (line_id, line.get("current_media"), media_from)
        )
    line["current_media"] = media_to
    return {"event_type": event_type, "applied": True}


def assert_pure_append(old_log: dict, candidate: dict, lineage) -> None:
    old_events = lineage.unique_events(old_log)
    new_events = lineage.unique_events(candidate)
    for eid, old_event in old_events.items():
        if json.dumps(new_events.get(eid), sort_keys=True) != json.dumps(old_event, sort_keys=True):
            raise WriteConflict("this write would modify existing event %s -- refused" % eid)


def validate_candidate(settings: Settings, candidate: dict, lineage) -> list[str]:
    """Schema + cross-field validation of the WHOLE candidate log. Shared by
    both write pipelines (single-event append, and the four ways a line can
    begin) -- one place this happens, so neither can drift from the other
    about what "valid" means."""
    schema = load_schema(settings)
    validator = jsonschema.Draft202012Validator(schema)
    schema_errors = sorted(
        "%s: %s" % ("/".join(str(p) for p in e.absolute_path), e.message)
        for e in validator.iter_errors(candidate)
    )
    if schema_errors:
        return schema_errors
    return lineage.validate(candidate)


def append_event(settings: Settings, target: dict, fields: dict[str, Any], operator: Operator) -> dict:
    """The whole pipeline. Returns the created event dict on success. On any
    failure, evolution_log.json on disk is exactly as it was before the call."""
    with write_lock:
        log = load_log(settings)
        lineage = lineage_module(settings)

        candidate = copy.deepcopy(log)
        new_event = build_event(candidate, fields, operator)

        line_id = target.get("line_id")
        if line_id is not None:
            line = candidate.get("lines", {}).get(line_id)
            if line is None:
                raise WriteConflict("no such line: %r" % line_id)
            line.setdefault("events", []).append(new_event)
        else:
            line = None
            new_event["scope"] = "facility"
            candidate.setdefault("experiment_events", []).append(new_event)

        problems = _referenced_events_exist(candidate, new_event, lineage)
        if line is not None:
            problems += _check_elapsed_h(line, new_event)
        problems += _check_controller_config_change_adequate(new_event)
        if problems:
            raise ValidationFailed(problems)

        # ISSUE_001: a level_reading/media_prep/reservoir_retired event used
        # to be recorded with no effect on reservoirs[] at all -- the
        # projection every other consumer (viewer.html, tools/media.py)
        # actually reads. A termination/media_switch event had the same gap
        # for lines[] -- status/lineage.terminated_at/terminated_by_event/
        # current_media stayed stale, and since GET /vials and every
        # begin_mode's destination check derive occupancy from status
        # alone, a bare termination via POST /events silently kept blocking
        # any future branch/restart/split/merge into that vial. All three
        # calls mutate `candidate` in place, in the SAME transaction this
        # event is appended in, so they are covered by the validate ->
        # assert_pure_append -> commit pipeline below and cannot half-apply.
        reservoir_report = project_reservoir_state(candidate, new_event)
        line_report = project_line_state(candidate, target, new_event)
        advance_last_updated(candidate, new_event["timestamp"])

        # Recompute derived fields before validating -- the same order a
        # human follows: edit, then `tools/lineage.py --write`, then
        # validate. recompute() itself only touches lineage.children/roots/
        # depth/is_founder and lineage_summary; event_counter/next_event_id
        # are a separate step in lineage.py's own --write CLI path (main()),
        # not inside recompute() -- mirrored here rather than assumed.
        lineage.recompute(candidate)
        candidate["log_meta"]["event_counter"] = len(lineage.unique_events(candidate))
        candidate["log_meta"]["next_event_id"] = "EVT-%05d" % (candidate["log_meta"]["event_counter"] + 1)

        problems = validate_candidate(settings, candidate, lineage)
        if problems:
            raise ValidationFailed(problems)

        assert_pure_append(log, candidate, lineage)

        notes_first_line = next(iter((new_event.get("notes") or "").splitlines()), "")
        message = "%s: %s" % (new_event["event_id"], notes_first_line[:72])
        write_and_commit(settings, candidate, message, operator)

        # A shallow copy, never the stored event itself -- reservoir_projection/
        # line_lifecycle_projection are response metadata about the write,
        # not part of the permanent event record. `new_event` (the object
        # actually inside `candidate`, now durably written) is untouched.
        response = dict(new_event)
        response["reservoir_projection"] = reservoir_report
        response["line_lifecycle_projection"] = line_report
        return response


def write_and_commit(settings: Settings, candidate: dict, message: str, operator: Operator) -> None:
    """Temp file + atomic rename, then git add + commit as the operator. If
    the commit fails, revert the working tree to HEAD -- evolution_log.json
    on disk must never sit ahead of git history, even transiently. Shared by
    both write pipelines; this part has no opinion about what changed, only
    that whatever candidate log it's given becomes durable atomically."""
    path: Path = settings.log_file
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as fh:
        json.dump(candidate, fh, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)  # atomic on POSIX: never a half-written file on disk

    try:
        subprocess.run(
            ["git", "-C", str(settings.log_repo_path), "add", "evolution_log.json"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(settings.log_repo_path),
             "-c", "user.name=%s" % operator.git_name,
             "-c", "user.email=%s" % operator.git_email,
             # `-- evolution_log.json` commits that path ONLY. Code and log
             # share one repo, so a bare commit would sweep in whatever code
             # change happened to be staged and file it under an operator's
             # name, with a log message, as if it were part of the record.
             "commit", "-q", "-m", message, "--", "evolution_log.json"],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        # The file write already happened; git must match it or the change
        # must not exist at all -- there is no valid third state. Revert
        # BOTH the index and the working tree to HEAD (not `checkout --
        # <path>` with no tree-ish, which would restore from the now-staged
        # index instead of undoing the add).
        subprocess.run(
            ["git", "-C", str(settings.log_repo_path), "checkout", "HEAD", "--", "evolution_log.json"],
            capture_output=True,
        )
        stderr = exc.stderr.decode() if exc.stderr else str(exc)
        raise CommitFailed(
            "git commit failed and the write was reverted to HEAD -- nothing "
            "was durably saved, safe to retry: %s" % stderr
        ) from exc
