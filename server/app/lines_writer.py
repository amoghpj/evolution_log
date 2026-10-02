"""POST /lines: the four ways a line can begin (LOG_PROTOCOL.md §5) --
branch, split, merge, restart. What POST /events cannot do: it only appends
to an already-existing line or to experiment_events, never creates a new
key in candidate["lines"].

Grounded in real precedent, not the prose spec alone -- checked against the
two real merges and three real restarts already in the log before this was
written (see this repo's git history for the actual data dumps). Confirmed
with the operator where precedent ran out: splits use one event per line,
cross-referenced (not a shared event_id across parent and children, unlike a
merge's child-only event); merge line_ids are supplied by the caller and
only validated (the one real example, patrick-v09+v10, names the parent that
keeps running as the base -- not a rule this code can safely reproduce); and
a restart may carry an embedded predecessor termination in the same call.

Two concepts that look similar but are not, discovered from the real data
and worth restating here because it's easy to conflate them in code:
`lineage.parents` is a CULTURE relationship (who this line descended from);
`lineage.occupies_vial_of` is a HARDWARE relationship (who last ran in this
exact physical position) -- LOG_PROTOCOL.md §4, "line identity follows the
culture, not the hardware". A restart's predecessor is both (same vial, no
lineage edge). A branch or split child's occupies_vial_of, if any, is
whichever ended line most recently held that CHILD's own destination vial --
which has nothing to do with who its lineage parent is; a merge child's
occupies_vial_of is whichever parent's standing population it physically
overwrites, which is a lineage parent AND the vial predecessor at once.

recompute() (tools/lineage.py) silently ignores a parent_line_id that
doesn't exist in candidate["lines"] -- it filters dangling parents out
rather than raising, so a typo'd parent would otherwise just quietly produce
a line with one less parent than intended. Every parent/predecessor
reference is checked to exist here, before recompute() ever runs.
"""
import copy
from typing import Any

from .auth import Operator
from .config import Settings
from .line_ids import last_real_position, next_occupancy_id, next_split_letter, validate_merge_id
from .line_models import BranchRequest, MergeRequest, NewLineSpec, RestartRequest, SplitRequest
from .log_repo import lineage_module, load_log
from .writer import (
    WriteConflict,
    ValidationFailed,
    _referenced_events_exist,
    assert_destination_empty as _assert_destination_empty,
    assert_known_unit as _assert_known_unit,
    assert_pure_append,
    build_event,
    validate_candidate,
    write_and_commit,
    write_lock,  # the same process-wide lock POST /events uses
)


def _find_line(candidate: dict, line_id: str, role: str) -> dict:
    line = candidate.get("lines", {}).get(line_id)
    if line is None:
        raise WriteConflict("no such %s line: %r" % (role, line_id))
    return line


def _find_prior_occupant(candidate: dict, unit: str, vial: int) -> str | None:
    """Whichever ENDED line most recently held this exact (unit, vial), if
    any -- hardware continuity only, unrelated to lineage parentage.

    Matched via last_real_position (app/line_ids.py), not a bare unit/vial
    == comparison against the line's CURRENT fields -- found necessary by
    simulating an adversarial operator: an ended line that was later
    vacated (its own tube physically removed after being logged as
    terminated -- README.md's own "pelleted, discarded" framing) reads
    unit/vial as null right now, so a bare == comparison would silently
    miss it as this vial's true prior occupant, even though it demonstrably
    was one. last_real_position reconstructs that from the line's own
    hardware_swap history when possible, and returns None (never guesses)
    when the trail runs out -- in which case this function correctly finds
    no candidate for that line, exactly as if it had genuinely never
    occupied the vial, rather than crashing or fabricating an answer."""
    candidates = [
        (lid, line) for lid, line in candidate.get("lines", {}).items()
        if line.get("status") == "ended" and last_real_position(line) == (unit, vial)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[1].get("lineage", {}).get("terminated_at") or "")
    return candidates[-1][0]


def _terminate(candidate: dict, line_id: str, term_fields: dict[str, Any], operator: Operator) -> dict:
    """Mutates candidate in place: appends a termination event to the named
    line and flips it to ended. Returns the termination event."""
    line = _find_line(candidate, line_id, "terminated")
    if line.get("status") != "active":
        raise WriteConflict("line %r is not active -- cannot be terminated" % line_id)
    event = build_event(candidate, term_fields, operator)
    line.setdefault("events", []).append(event)
    line["status"] = "ended"
    line["lineage"]["terminated_by_event"] = event["event_id"]
    line["lineage"]["terminated_at"] = event["timestamp"]
    return event


def _build_line(line_id: str, spec: NewLineSpec, founding_event: dict,
                 parents: list[str], occupies_vial_of: str | None) -> dict:
    line: dict[str, Any] = {
        "line_id": line_id,
        "unit": spec.unit,
        "vial": spec.vial,
        "strain": spec.strain,
        "initial_media": spec.initial_media,
        "current_media": spec.current_media,
        "mode": spec.mode,
        "status": "active",
        "t0": spec.t0,
        "lineage": {
            "parents": parents,
            "children": [],
            # placeholders -- recompute() overwrites roots/depth/is_founder
            # for every line, including this new one, before validation.
            "roots": [line_id],
            "depth": 0,
            "is_founder": not parents,
            "created_by_event": founding_event["event_id"],
            "created_at": founding_event["timestamp"],
            "terminated_by_event": None,
            "terminated_at": None,
        },
        "pg_regime": {
            "low": spec.pg_regime.low.model_dump(),
            "high": spec.pg_regime.high.model_dump(),
            "effective_from": spec.pg_regime.effective_from,
        },
        "reservoirs": {"low": spec.reservoirs.low, "high": spec.reservoirs.high},
        "events": [founding_event],
    }
    if occupies_vial_of is not None:
        line["lineage"]["occupies_vial_of"] = occupies_vial_of
    if spec.replicate is not None:
        line["replicate"] = spec.replicate
    if spec.group is not None:
        line["group"] = spec.group
    return line


def _insert_new_line(candidate: dict, line_id: str, line: dict) -> None:
    if line_id in candidate.get("lines", {}):
        raise WriteConflict("line id %r already exists -- refused" % line_id)
    candidate.setdefault("lines", {})[line_id] = line


# ── one handler per begin_mode. Each returns the set of existing line_ids
# it terminated, for the generalized invariant check that follows. ─────────

def _do_branch(candidate: dict, req: BranchRequest, operator: Operator) -> set[str]:
    parent = _find_line(candidate, req.parent_line_id, "parent")
    if parent.get("status") != "active":
        raise WriteConflict("parent %r is not active -- a branch's parent must continue running" % req.parent_line_id)

    spec = req.new_line
    _assert_known_unit(candidate, spec.unit, "branch destination")
    _assert_destination_empty(candidate, spec.unit, spec.vial, "branch destination")
    new_id = next_occupancy_id(candidate, spec.unit, spec.vial)
    occupies = _find_prior_occupant(candidate, spec.unit, spec.vial)

    event = build_event(candidate, spec.founding_event.model_dump(exclude_none=True), operator)
    _insert_new_line(candidate, new_id, _build_line(new_id, spec, event, [req.parent_line_id], occupies))
    return set()


def _do_restart(candidate: dict, req: RestartRequest, operator: Operator) -> set[str]:
    terminated: set[str] = set()
    predecessor_id = req.predecessor_line_id

    if predecessor_id is not None:
        predecessor = _find_line(candidate, predecessor_id, "predecessor")
        if predecessor.get("status") == "active":
            if req.predecessor_termination is None:
                raise ValidationFailed(
                    ["predecessor %r is still active -- predecessor_termination is required"
                     % predecessor_id]
                )
            _terminate(candidate, predecessor_id,
                       req.predecessor_termination.model_dump(exclude_none=True), operator)
            terminated.add(predecessor_id)
        elif req.predecessor_termination is not None:
            raise ValidationFailed(
                ["predecessor %r is already ended -- predecessor_termination must be omitted"
                 % predecessor_id]
            )

    spec = req.new_line
    _assert_known_unit(candidate, spec.unit, "restart destination")
    _assert_destination_empty(candidate, spec.unit, spec.vial, "restart destination")
    new_id = next_occupancy_id(candidate, spec.unit, spec.vial)

    fields = spec.founding_event.model_dump(exclude_none=True)
    if predecessor_id is not None:
        fields.setdefault("params", {})
        # predecessor_in_vial is how a restart's hardware continuity is found
        # from the NEW line's own (always-writable) event, matching real
        # precedent exactly -- never touches the predecessor's own (possibly
        # already-committed, possibly immutable) termination event.
        fields["params"].setdefault("predecessor_in_vial", predecessor_id)

    event = build_event(candidate, fields, operator)
    # a restart is NOT descent: zero parents, founder, hardware continuity
    # only if there IS a predecessor (LOG_PROTOCOL.md §5). A true day-one
    # founder (predecessor_id is None) is a founder for the simpler reason
    # that nothing preceded it at all.
    _insert_new_line(candidate, new_id, _build_line(new_id, spec, event, [], predecessor_id))
    return terminated


def _do_split(candidate: dict, req: SplitRequest, operator: Operator) -> set[str]:
    parent = _find_line(candidate, req.parent_line_id, "parent")
    if parent.get("status") != "active":
        raise WriteConflict("parent %r is not active -- cannot be split" % req.parent_line_id)

    _terminate(candidate, req.parent_line_id, req.parent_termination.model_dump(exclude_none=True), operator)
    parent_termination_event_id = parent["lineage"]["terminated_by_event"]

    for spec in req.new_lines:
        _assert_known_unit(candidate, spec.unit, "split destination")
        _assert_destination_empty(candidate, spec.unit, spec.vial, "split destination")
        new_id = "%s.%s" % (req.parent_line_id, next_split_letter(candidate, req.parent_line_id))
        occupies = _find_prior_occupant(candidate, spec.unit, spec.vial)

        fields = spec.founding_event.model_dump(exclude_none=True)
        fields.setdefault("caused_by_event", parent_termination_event_id)

        event = build_event(candidate, fields, operator)
        _insert_new_line(candidate, new_id, _build_line(new_id, spec, event, [req.parent_line_id], occupies))

    return {req.parent_line_id}


def _do_merge(candidate: dict, req: MergeRequest, operator: Operator) -> set[str]:
    for pid in req.parent_line_ids:
        parent = _find_line(candidate, pid, "parent")
        if parent.get("status") != "active":
            # physically: you cannot spike material from a line that has
            # already ended. A merge's parents are always active going in;
            # zero or more of them then end AS PART of this same call.
            raise WriteConflict("parent %r is not active -- cannot contribute to a merge" % pid)

    spec = req.new_line
    try:
        validate_merge_id(spec.line_id, req.parent_line_ids)
    except ValueError as exc:
        raise ValidationFailed([str(exc)]) from exc

    if not req.parent_terminations:
        raise ValidationFailed(
            ["a merge needs at least one ending parent -- the destination vial's standing "
             "population must belong to a parent that is ending, per LOG_PROTOCOL.md §5"]
        )

    occupies = None
    ending_positions = []  # [(pid, unit, vial), ...] -- for a precise error message below, if needed
    for pid, term in req.parent_terminations.items():
        parent = _find_line(candidate, pid, "parent")
        ending_positions.append((pid, parent.get("unit"), parent.get("vial")))
        if parent.get("unit") == spec.unit and parent.get("vial") == spec.vial:
            occupies = pid
        _terminate(candidate, pid, term.model_dump(exclude_none=True), operator)

    if occupies is None:
        raise ValidationFailed(
            ["new_line (%s, vial %d) does not match the (unit, vial) of any ending parent in "
             "parent_terminations -- a merge child must physically occupy an ending parent's "
             "vial. Ending parents actually occupy: %s"
             % (spec.unit, spec.vial,
                ", ".join("%s (%s, vial %s)" % (pid, u, v) for pid, u, v in ending_positions))]
        )

    event = build_event(candidate, spec.founding_event.model_dump(exclude_none=True), operator)
    _insert_new_line(candidate, spec.line_id, _build_line(spec.line_id, spec, event, req.parent_line_ids, occupies))
    return set(req.parent_terminations)


_HANDLERS = {
    "branch": _do_branch,
    "restart": _do_restart,
    "split": _do_split,
    "merge": _do_merge,
}


def _assert_safe_mutation(old_log: dict, candidate: dict, lineage, expected_terminated: set[str]) -> None:
    """Generalizes writer.py's _assert_pure_append for an operation that also
    creates new lines and/or terminates existing ones. Every PRE-EXISTING
    line must be unchanged except: lineage.children/roots/depth (recompute()
    -derived, may grow when a new child parents onto it), and -- ONLY for
    lines in expected_terminated -- status active->ended, terminated_at/
    terminated_by_event set, and exactly one new termination event."""
    assert_pure_append(old_log, candidate, lineage)  # no existing EVENT's content is ever altered

    old_lines = old_log.get("lines", {})
    new_lines = candidate.get("lines", {})
    for lid, old_line in old_lines.items():
        if lid not in new_lines:
            raise WriteConflict("line %s disappeared -- refused" % lid)
        new_line = new_lines[lid]

        old_ids = {e["event_id"] for e in old_line.get("events", [])}
        new_ids = {e["event_id"] for e in new_line.get("events", [])}
        if not old_ids <= new_ids:
            raise WriteConflict("line %s lost an event -- refused" % lid)
        added = new_ids - old_ids

        if lid in expected_terminated:
            if len(added) != 1:
                raise WriteConflict("line %s should receive exactly one termination event (got %d)"
                                     % (lid, len(added)))
            [added_event] = [e for e in new_line["events"] if e["event_id"] in added]
            if added_event.get("event_type") != "termination":
                raise WriteConflict("line %s's new event must be a termination" % lid)
            if new_line.get("status") != "ended":
                raise WriteConflict("line %s was terminated but its status was not set to ended" % lid)
        else:
            if added:
                raise WriteConflict("line %s received an unexpected new event -- refused" % lid)
            if new_line.get("status") != old_line.get("status"):
                raise WriteConflict("line %s changed status without being part of this operation" % lid)

        def _stripped(line_obj):
            s = copy.deepcopy(line_obj)
            s.pop("events", None)
            s["status"] = None
            lg = s.get("lineage", {})
            for k in ("children", "roots", "depth", "terminated_at", "terminated_by_event"):
                lg.pop(k, None)
            return s

        if _stripped(old_line) != _stripped(new_line):
            raise WriteConflict("line %s changed in a way this operation does not allow" % lid)


def _new_events_from(candidate: dict, new_line_ids: list[str], expected_terminated: set[str]) -> list[dict]:
    """Every event THIS call just built: each new line's founding event, plus
    each newly-terminated line's termination event (its LAST event -- the
    one _terminate() just appended). Used to check caused_by_event/
    supersedes references exist, the same check POST /events already runs
    on its own new event -- found missing here by simulating a real
    revival-from-stock operator: a typo'd caused_by_event in a restart's
    founding_event was accepted with 201 and written permanently, when the
    identical mistake on POST /events correctly 422s."""
    events = [candidate["lines"][lid]["events"][0] for lid in new_line_ids]
    events += [candidate["lines"][lid]["events"][-1] for lid in expected_terminated]
    return events


def create_line(settings: Settings, request, operator: Operator) -> dict:
    """The whole pipeline for all four begin_modes. Returns
    {"new_line_ids": [...], "lines": {id: line_dict, ...}} on success. On any
    failure, evolution_log.json on disk is exactly as it was before the call."""
    with write_lock:
        log = load_log(settings)
        lineage = lineage_module(settings)
        candidate = copy.deepcopy(log)

        before_ids = set(candidate.get("lines", {}))
        handler = _HANDLERS[request.begin_mode]
        expected_terminated = handler(candidate, request, operator)
        new_line_ids = sorted(set(candidate.get("lines", {})) - before_ids)

        problems = []
        for event in _new_events_from(candidate, new_line_ids, expected_terminated):
            problems += _referenced_events_exist(candidate, event, lineage)
        if problems:
            raise ValidationFailed(problems)

        try:
            # recompute() raises ValueError on a lineage cycle -- e.g. a
            # merge whose parent_line_ids somehow chain back to the new
            # child. Extremely unlikely given ids are always freshly
            # derived/validated, but a real, reachable code path (recompute()
            # itself documents "raises on a cycle"), not hypothetical.
            lineage.recompute(candidate)
        except ValueError as exc:
            raise ValidationFailed([str(exc)]) from exc
        candidate["log_meta"]["event_counter"] = len(lineage.unique_events(candidate))
        candidate["log_meta"]["next_event_id"] = "EVT-%05d" % (candidate["log_meta"]["event_counter"] + 1)

        problems = validate_candidate(settings, candidate, lineage)
        if problems:
            raise ValidationFailed(problems)

        _assert_safe_mutation(log, candidate, lineage, expected_terminated)

        message = "%s: %s (%s)" % (
            "+".join(new_line_ids), request.begin_mode,
            ", ".join(sorted(expected_terminated)) or "no lines terminated",
        )
        write_and_commit(settings, candidate, message, operator)

        return {
            "new_line_ids": new_line_ids,
            "lines": {lid: candidate["lines"][lid] for lid in new_line_ids},
            "terminated_line_ids": sorted(expected_terminated),
        }
