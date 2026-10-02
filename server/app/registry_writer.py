"""POST /parameter_registry -- registers ONE new key in evolution_log.json's
parameter_registry, committing into the SAME repo POST /events/POST /lines
already write to (never a separate one, unlike POST /config, which
deliberately writes to a completely different repo per unit).

Before this existed, the only way to register a new params key was to
hand-edit evolution_log.json directly -- no validation ran until AFTER the
edit already landed, which is exactly the "directly manipulating the data"
the operator asked this feature to replace (2026-09-01). This reuses
almost everything app/writer.py already built for POST /events: the same
validate_candidate (schema + tools/lineage.py), the same write_and_commit
git pipeline, and the same process-wide write_lock -- this writes to the
SAME file POST /events does, so it has to serialize against it, not just
against itself.

Unlike POST /events, there is no new "event" here -- no event_id, no
timestamp, nothing for tools/lineage.py:recompute to touch, no
log_meta.event_counter to advance. The whole operation is: copy the log,
add exactly one key to parameter_registry, validate the whole candidate,
confirm nothing else moved, commit.

Deliberately does NOT do more than that. Two things a full "registry
management" feature might also want are explicitly out of scope, both by
the operator's own decision (2026-09-01):

  - Retiring or editing an EXISTING entry (status -> "retired", or fixing a
    typo in an existing description) is a genuinely different operation --
    an update, not a pure addition -- and needs its own design later, not
    bundled in here.
  - Auto-detecting first use (flipping a "planned" entry to "active" with a
    real first_seen the moment some event actually uses the key) stays a
    manual, human step for now, matching how every "planned" entry already
    in the real log (dilution_rate, controller_version, od_calibration) got
    there and is expected to be promoted.
"""
import copy
import json
from typing import Any

from .auth import Operator
from .config import Settings
from .log_repo import lineage_module, load_log
from .writer import ValidationFailed, WriteConflict, validate_candidate, write_and_commit, write_lock

# A fresh registration may only ever produce one of these two states.
# "retired" describes something that already existed and stopped being
# used -- not a state a brand-new key can start in; see the module
# docstring on why retiring is a separate, not-yet-built operation.
_ALLOWED_CREATE_STATUSES = ("planned", "active")


def _duplicate_key_problem(log: dict, key: str) -> str | None:
    if key in log.get("parameter_registry", {}):
        return ("parameter_registry already has an entry named %r -- this route only registers a "
                "NEW key; changing or retiring an existing one is a different, not-yet-built "
                "operation" % key)
    return None


def _check_registration(log: dict, lineage, key: str, entry: dict[str, Any]) -> list[str]:
    """Field-level checks shared by the real write and its /candidate
    preview -- everything EXCEPT the duplicate-key check, which each caller
    handles on its own terms (a 409 for the real write, an ordinary problem
    string for /candidate, which never raises).

    Does NOT check description/status/type/enum/items shape -- that's
    schema/evolution_log.schema.json's own registryEntry $def, enforced by
    validate_candidate() against the whole candidate log, the same
    definition tools/lineage.py's check_param_types already uses for every
    EVENT's params. Duplicating that here by hand would be exactly the kind
    of second, driftable copy this project has repeatedly avoided."""
    if not key or not isinstance(key, str):
        return ["key must be a non-empty string"]

    status = entry.get("status")
    if status not in _ALLOWED_CREATE_STATUSES:
        return ["status must be one of %s when registering a NEW key -- 'retired' describes "
                "something that already existed and stopped being used, not a state a brand-new "
                "key can start in (got %r)" % (_ALLOWED_CREATE_STATUSES, status)]

    problems: list[str] = []
    first_seen = entry.get("first_seen")
    if status == "planned":
        if first_seen is not None:
            problems.append(
                "a 'planned' entry (registered ahead of any real use) must have first_seen: null -- "
                "if %r has already been used by a real event, register it as status: active with "
                "that event's id instead" % key
            )
    else:  # active
        if not first_seen:
            problems.append(
                "status: active requires first_seen to name the event that already uses %r -- if "
                "nothing has used it yet, register it as status: planned with first_seen: null "
                "instead" % key
            )
        else:
            referenced = lineage.unique_events(log).get(first_seen)
            if referenced is None:
                problems.append(
                    "first_seen %r does not name an existing event -- log the event that uses "
                    "this key first, or register as status: planned if nothing has used it yet"
                    % first_seen
                )
            elif key not in (referenced.get("params") or {}):
                # Found by construction, not yet an observed mistake: an
                # operator/LLM could plausibly copy a plausible-looking
                # event_id as first_seen without checking it actually
                # contains this key -- exactly the "confident wrong number"
                # this project's docs warn against elsewhere, just for a
                # registry entry instead of an event.
                problems.append(
                    "first_seen %r is a real event, but its params don't actually contain %r -- "
                    "first_seen must name the event that genuinely first used this key, not just "
                    "any existing event id" % (first_seen, key)
                )
    return problems


def validate_registration(settings: Settings, key: str, entry: dict[str, Any]) -> list[str]:
    """Dry-run: the same checks register_parameter's real write is judged
    against, minus the write and minus write_lock -- nothing here mutates
    anything, so no lock is needed, matching POST /config/candidate's same
    reasoning."""
    log = load_log(settings)
    lineage = lineage_module(settings)

    problems = []
    dup = _duplicate_key_problem(log, key)
    if dup:
        problems.append(dup)
    problems += _check_registration(log, lineage, key, entry)
    if problems:
        return problems

    # Even with no per-field problem found above, the whole candidate log
    # must still pass schema + tools/lineage.py's cross-field checks -- the
    # exact registryEntry shape (required description/status/type, the
    # closed value-type vocabulary, well-formed enum/items) register_
    # parameter's real write is judged against, so /candidate can never
    # bless something the real write would then reject.
    candidate = copy.deepcopy(log)
    candidate.setdefault("parameter_registry", {})[key] = entry
    return validate_candidate(settings, candidate, lineage)


def register_parameter(settings: Settings, key: str, entry: dict[str, Any], operator: Operator) -> dict:
    """The whole pipeline. Returns {"key": ..., "entry": ...} on success. On
    any failure, evolution_log.json on disk is exactly as it was before."""
    with write_lock:
        log = load_log(settings)
        lineage = lineage_module(settings)

        dup = _duplicate_key_problem(log, key)
        if dup:
            raise WriteConflict(dup)

        problems = _check_registration(log, lineage, key, entry)
        if problems:
            raise ValidationFailed(problems)

        candidate = copy.deepcopy(log)
        candidate.setdefault("parameter_registry", {})[key] = entry

        problems = validate_candidate(settings, candidate, lineage)
        if problems:
            raise ValidationFailed(problems)

        # Pure addition: every existing entry must survive byte-for-byte --
        # the same guarantee assert_pure_append gives event history in
        # app/writer.py, applied here to registry keys instead. A
        # `.setdefault(...)[key] =` typo'd into replacing the whole dict
        # (rather than adding to it) would be exactly the kind of silent,
        # confident data loss this project's docs warn against elsewhere --
        # checked directly rather than inferred from "the code above only
        # adds one key".
        old_registry = log.get("parameter_registry", {})
        new_registry = candidate.get("parameter_registry", {})
        for existing_key, existing_val in old_registry.items():
            if json.dumps(new_registry.get(existing_key), sort_keys=True) != json.dumps(existing_val, sort_keys=True):
                raise WriteConflict(
                    "this write would modify existing parameter_registry entry %r -- refused" % existing_key
                )

        message = "parameter_registry: register %r (%s)" % (key, entry.get("status"))
        write_and_commit(settings, candidate, message, operator)

        return {"key": key, "entry": entry}
