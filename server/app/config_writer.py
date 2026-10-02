"""Read/write experiment_parameters.yaml in a single unit's own evolver_code
checkout, and commit each write into THAT repo's own git history --
deliberately never evolution_log.json (app/writer.py's repo). Per the
operator's explicit instruction (2026-09-01): "POST config should not
directly touch the log.json ... There is a local git repo in each evolver
specific directory. That git repo should be used for a per evolver logging
of changes." Each eVOLVER unit accumulates its own commit history of config
changes, independent of the others and independent of the experiment log --
logging a config change into evolution_log.json is a separate, deliberate
POST /events call (see app/config_skill.py's workflow section), never
implicit here.
"""
import math
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import yaml

from .auth import Operator
from .evolver_config import EvolverConfigSettings

# One lock for all units, not one per unit -- traffic here is a handful of
# human/LLM operators, occasionally, the same proportionality argument
# app/writer.py's write_lock already makes for evolution_log.json.
config_write_lock = threading.Lock()


class UnknownEvolverUnit(Exception):
    """No EVOLVER_UNIT_PATHS entry for this unit. Maps to 404."""


class ConfigCommitFailed(Exception):
    """The yaml write succeeded but git commit did not; the working tree has
    already been reverted to HEAD. Treat exactly like the write never
    happened -- safe to retry."""


def _unit_path(settings: EvolverConfigSettings, unit: str) -> Path:
    path = settings.path_for(unit)
    if path is None:
        raise UnknownEvolverUnit(
            "no evolver_code path configured for unit %r -- EVOLVER_UNIT_PATHS knows: %s"
            % (unit, sorted(settings.unit_paths))
        )
    return path


def read_config(settings: EvolverConfigSettings, unit: str) -> dict[str, Any]:
    path = _unit_path(settings, unit) / "experiment_parameters.yaml"
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def _file_in_head(repo: Path) -> bool:
    """True if experiment_parameters.yaml already exists in HEAD -- decides
    whether reverting a failed write means restoring the OLD content
    (git checkout) or removing the file entirely (there was nothing to
    check out TO, because this was the unit's first-ever write).

    Found necessary by simulating a first-ever-write commit failure:
    `git checkout HEAD -- <path>` silently no-ops with a nonzero exit when
    HEAD has no such path, leaving the rejected write's content sitting on
    disk, staged -- while the exception this function's caller used to
    raise unconditionally claimed "already been reverted to HEAD.\""""
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", "HEAD:experiment_parameters.yaml"],
        capture_output=True,
    )
    return result.returncode == 0


def _revert_to_head(repo: Path, was_in_head: bool) -> None:
    path = repo / "experiment_parameters.yaml"
    if was_in_head:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "HEAD", "--", "experiment_parameters.yaml"],
            capture_output=True,
        )
    else:
        # Nothing to check out TO. `git checkout HEAD -- <path>` is a no-op
        # here (see _file_in_head); unstage and delete instead, so the
        # working tree ends up exactly as before this call -- no file,
        # nothing staged -- matching what "reverted to HEAD" actually means
        # when HEAD never had this file at all.
        subprocess.run(["git", "-C", str(repo), "reset", "-q", "--", "experiment_parameters.yaml"],
                        capture_output=True)
        path.unlink(missing_ok=True)


class RemovalRejected(Exception):
    """A write was rejected because it would silently drop a vial or a
    top-level experiment_settings field -- see write_config_checked's
    docstring for why this is raised from INSIDE the write lock rather
    than checked earlier, in the caller, the way the very first version
    of this safety check did. Maps to 422."""
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _write_and_commit_locked(repo: Path, config: dict[str, Any], message: str, operator: Operator) -> None:
    """The actual write+commit. Callers MUST already hold config_write_lock
    -- factored out from write_config()/write_config_checked() so both can
    do their own (different) work under ONE lock acquisition each, rather
    than this function nesting a second `with config_write_lock:` inside
    an outer one already held (threading.Lock is not reentrant; that would
    deadlock)."""
    path = repo / "experiment_parameters.yaml"
    tmp_path = path.with_suffix(".yaml.tmp")
    was_in_head = _file_in_head(repo)  # before the write -- decides how to revert on failure
    with open(tmp_path, "w") as fh:
        yaml.safe_dump(config, fh, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, path)  # atomic on POSIX: never a half-written file on disk

    try:
        subprocess.run(
            ["git", "-C", str(repo), "add", "experiment_parameters.yaml"],
            check=True, capture_output=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(repo),
             "-c", "user.name=%s" % operator.git_name,
             "-c", "user.email=%s" % operator.git_email,
             "commit", "-q", "-m", message],
            capture_output=True,
        )
        if commit.returncode != 0:
            combined = (commit.stdout or b"").decode(errors="replace") + \
                (commit.stderr or b"").decode(errors="replace")
            if "nothing to commit" in combined:
                # The identical config was re-submitted (e.g. an LLM
                # re-validating then re-posting unchanged) -- the file on
                # disk already matches what was asked for; not a failure.
                return
            raise subprocess.CalledProcessError(
                commit.returncode, commit.args, commit.stdout, commit.stderr
            )
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        # ValueError: e.g. a NUL byte in exp_name, which flows verbatim
        # into the commit `message` -- Python's subprocess rejects a NUL
        # in argv before it even forks, so this never becomes a
        # CalledProcessError. OSError: e.g. argv too long, or a git
        # binary that can't be exec'd at all. Found necessary by
        # simulating adversarial exp_name input: the ORIGINAL code here
        # only caught CalledProcessError, so this exact case skipped the
        # revert entirely and propagated as an unhandled crash, leaving
        # the rejected write staged and on disk with no cleanup. Every
        # one of these three exception types means the same thing:
        # the file write already happened, and it must not be allowed
        # to stand uncommitted.
        _revert_to_head(repo, was_in_head)
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode(errors="replace") if exc.stderr else str(exc)
        else:
            detail = str(exc)
        raise ConfigCommitFailed(
            "git commit failed and the write was reverted to HEAD -- nothing was "
            "durably saved, safe to retry: %s" % detail
        ) from exc


def write_config(settings: EvolverConfigSettings, unit: str, config: dict[str, Any],
                  message: str, operator: Operator) -> None:
    repo = _unit_path(settings, unit)
    with config_write_lock:
        _write_and_commit_locked(repo, config, message, operator)


def write_config_checked(settings: EvolverConfigSettings, unit: str, new_config: dict[str, Any],
                          message: str, operator: Operator, confirm_removed_fields: bool) -> dict[str, Any]:
    """Like write_config, but re-reads old_config and re-runs
    check_no_silent_removal INSIDE config_write_lock, immediately before
    writing -- not earlier, in the route, before the lock is ever taken
    (which is where this used to happen). Returns the old_config it read,
    since that's now the only race-free source of it for the caller's own
    use (e.g. building describe_live_reload_effect's diff).

    Closes a real, confirmed TOCTOU race, found by simulating two
    concurrent writers: read_config()+check_no_silent_removal() used to
    run OUTSIDE the lock, in app/routes/write_config.py, before
    write_config() ever acquired it. Two concurrent requests could each
    read the SAME stale old_config, each pass check_no_silent_removal
    relative to THAT stale snapshot (neither request's own body removes
    anything IT ever saw), and the second one's commit would still
    silently undo whatever vial/field the first one had just added -- the
    safety check runs, reports "fine", on BOTH requests, while real data
    is destroyed, with no error and no warning to either caller. Re-reading
    inside the exact same lock the write itself uses means the check's
    verdict is about to be acted on immediately, with nothing able to
    invalidate it in between -- the same reasoning config_write_lock
    already applies to the write+commit not tearing, extended to cover the
    decision made just before it."""
    repo = _unit_path(settings, unit)
    with config_write_lock:
        old_config = read_config(settings, unit)
        if old_config and not confirm_removed_fields:
            problems = check_no_silent_removal(old_config, new_config)
            if problems:
                raise RemovalRejected(problems)
        _write_and_commit_locked(repo, new_config, message, operator)
    return old_config


def json_safe(obj):
    """Recursively replaces any non-finite float (NaN, +inf, -inf) with
    None. Shared by GET /config's response (real .nan/.inf placeholders
    already sitting in an on-disk yaml -- YAML has both; PyYAML parses
    `.nan`/`.inf`/`-.inf` to the corresponding Python float) and by
    describe_live_reload_effect's diff (below).

    Originally handled only NaN (found 2026-09-01: the real
    experiment_parameters.yaml has ~240 .nan placeholders, and Starlette's
    default JSONResponse calls json.dumps(..., allow_nan=False) -- not the
    stdlib default -- so returning one verbatim 500s unconditionally).
    Extended to +-inf after simulating the same class of value: `GET
    /config` 500s identically on a `.inf`/-`.inf` already on disk, and
    (more severely) a literal JSON `Infinity` in a POST body isn't actually
    impossible -- Starlette's Request.json() uses stdlib json.loads(),
    which accepts the non-standard Infinity/-Infinity/NaN tokens by
    default, so a client's own `json.dumps(payload)` (ALSO the stdlib
    default, allow_nan=True) can produce a request FastAPI/pydantic
    accepts without complaint. See find_non_finite() below for the
    write-time counterpart that rejects such a value outright rather than
    silently laundering it into None on the way out.

    None is the correct replacement for an existing .nan/.inf, not a lossy
    one, for the same reason as before: nothing downstream reads either
    sentinel as a real number that matters (see find_non_finite's
    docstring for why a NEWLY submitted one is rejected instead of
    laundered -- the two functions serve different moments: this one makes
    already-existing data safe to return; that one stops new nonsense from
    being written in the first place)."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


def find_non_finite(obj, path: str = "config") -> list[str]:
    """Recursively finds every NaN/+inf/-inf in a candidate config, before
    it's ever written or diffed. Not a structural validation rule (that's
    evolver_code/config_validation.py's job, and this deliberately does NOT
    touch that shared file) -- a server-side defense-in-depth guard against
    a value JSON itself can't even faithfully represent.

    Closes a real incident found by simulating an adversarial POST body: a
    literal `Infinity` in a JSON request is not actually impossible (see
    json_safe's docstring for why), so `high_concentration: Infinity`
    reached validate_config, which has no isfinite check, validated clean,
    and got WRITTEN AND COMMITTED to the unit's git history -- only THEN
    crashing the response with an unhandled ValueError (Starlette's
    allow_nan=False), so the caller saw a bare 500 and reasonably believed
    the write had failed while it had already durably succeeded. Checking
    and rejecting BEFORE any write happens closes the state-desync (response
    says failure, disk/git says success) as well as the missing bound."""
    problems = []

    def walk(o, p):
        if isinstance(o, float) and not math.isfinite(o):
            problems.append("%s = %r is not a finite number" % (p, o))
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, "%s.%s" % (p, k))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, "%s[%d]" % (p, i))

    walk(obj, path)
    return problems


# Fields that are physically non-negative (a concentration, a volume, a
# count, a duration) regardless of operation mode -- checked server-side,
# deliberately NOT added to evolver_code/config_validation.py (the shared
# canonical validator this server doesn't own the content of). Found by
# simulating adversarial numeric input: none of these had a sign check at
# all -- low_concentration: -5.0, volume: -22.0, interval: -1.5, and
# number_consecutive_intervals: -100 all validated as "valid": true.
_NON_NEGATIVE_FIELDS = (
    "volume", "high_concentration", "low_concentration", "initial_concentration",
    "interval", "number_consecutive_intervals",
)


def check_physically_impossible(config: dict[str, Any]) -> list[str]:
    """Rejects a per-vial value that is finite, well-typed, and passes
    evolver_code/config_validation.py's own rules, but is still physically
    impossible: a negative concentration, a negative or zero volume, a
    negative interval or dispense count. Deliberately narrow -- this is
    about SIGN, not magnitude (an absurdly large-but-positive value, e.g.
    target_ramp: 1e10, is a real gap too, but bounding magnitude means
    picking a threshold this server has no authority to invent; sign is
    not a threshold, it's what the field's own name means)."""
    problems = []
    per_vial = _per_vial_map(config)
    for vial, vs in sorted(per_vial.items()):
        for field in _NON_NEGATIVE_FIELDS:
            value = vs.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue  # absence/wrong-type is validate_config's job, not this check's
            if value < 0:
                problems.append(
                    "per_vial_settings (vial %r).%s = %r is negative -- physically impossible "
                    "for this field" % (vial, field, value)
                )
        vol = vs.get("volume")
        if isinstance(vol, (int, float)) and not isinstance(vol, bool) and vol == 0 and vs.get("to_run"):
            problems.append("per_vial_settings (vial %r) has to_run: true but volume: 0 -- "
                             "an active vial cannot have zero volume" % vial)
    return problems


def check_no_silent_removal(old_config: dict[str, Any], new_config: dict[str, Any]) -> list[str]:
    """Rejects a write that would silently drop a vial or a top-level
    experiment_settings field the CURRENT config has, unless the caller
    passes confirm_removed_fields: true.

    Closes a real, severe incident found by simulating a "PATCH-believing"
    LLM client: GET /config/skill explicitly says config is "the WHOLE
    experiment_settings document (not a partial patch)", but nothing
    actually enforced that. A body naming only ONE changed vial validated
    as valid: true and, once written, permanently discarded the other 15
    vials' real operational settings from the file that runs on the rig --
    388 lines gone, no warning before or after. This is exactly the
    confident, silent data-loss failure mode this project's own docs exist
    to prevent, so it is a hard rejection by default, not a warning: an
    operator/LLM that actually means to retire a vial or drop a field must
    say so explicitly, once, rather than the server silently assuming
    "absent" always means "intentionally removed."."""
    problems = []
    old_vials, new_vials = _per_vial_map(old_config), _per_vial_map(new_config)
    removed_vials = sorted(set(old_vials) - set(new_vials))
    if removed_vials:
        problems.append(
            "the new config's per_vial_settings is missing vial(s) %s, present in the CURRENT "
            "config -- config is a full-document REPLACE, not a patch (GET /config/skill); "
            "omitting a vial deletes its entire configuration, it does not leave it unchanged. "
            "If this is intentional, resubmit with confirm_removed_fields: true" % removed_vials
        )

    old_top = (old_config or {}).get("experiment_settings") or {}
    new_top = (new_config or {}).get("experiment_settings") or {}
    removed_top = sorted(
        k for k, v in old_top.items() if k != "per_vial_settings" and v is not None and k not in new_top
    )
    if removed_top:
        problems.append(
            "the new config's experiment_settings is missing top-level key(s) %s, present (and "
            "non-null) in the CURRENT config -- same full-document-replace rule as above; "
            "resubmit with confirm_removed_fields: true if this is intentional" % removed_top
        )
    return problems


def unknown_unit_message(log: dict[str, Any], unit: str) -> str:
    """Shared by both /config routes' 404s. Found by simulating a case/
    whitespace-confused LLM: the original message only echoed back what
    was sent ("no such unit: 'Testunit'"), giving the caller nothing to
    self-correct from without a SEPARATE call to GET /skill or the log
    itself. Lists the real names, matching the precedent
    app/lines_writer.py's _assert_known_unit already sets."""
    known = sorted(log.get("hardware", {}).get("units", {}))
    return "no such unit: %r -- known units: %s" % (unit, known)


def mode_not_implemented_detail(exc, config: dict[str, Any]) -> dict:
    """Shared by both /config routes' 501s. `exc` is a
    evolver_code/config_validation.py ModeNotImplemented (duck-typed here
    on .mode/.supported rather than imported, so this module still doesn't
    need to know about CONFIG_VALIDATOR_PATH's indirection).

    Found by simulating a PATCH-believing LLM one step further: a config
    missing `operation` ENTIRELY (e.g. a partial body naming only one
    changed field) resolves mode=None and raises this exact exception, so
    the caller sees "operation.mode None is not yet implemented -- only
    pumpcontrol_ramp is supported" -- which reads as "try a different
    mode", reinforcing the wrong belief, when the real problem is an
    incomplete document, not a deliberate mode choice. Still a 501 (mode
    genuinely is unset, that part of the message is true) but the detail
    now names the more likely cause first when it applies."""
    detail = {"mode": exc.mode, "supported_modes": list(exc.supported), "message": str(exc)}
    settings_section = (config or {}).get("experiment_settings")
    if exc.mode is None and isinstance(settings_section, dict) and "operation" not in settings_section:
        detail["likely_cause"] = (
            "experiment_settings.operation is missing entirely, not merely set to an unsupported "
            "mode -- if you meant to send a partial update, config must be the WHOLE "
            "experiment_settings document (see GET /config/skill), not a patch"
        )
    return detail


def _per_vial_map(config: dict[str, Any]) -> dict[int, dict]:
    settings = (config or {}).get("experiment_settings") or {}
    return {
        vs.get("vial"): vs
        for vs in (settings.get("per_vial_settings") or [])
        if isinstance(vs, dict) and isinstance(vs.get("vial"), int) and not isinstance(vs.get("vial"), bool)
    }


# Top-level experiment_settings fields checked for live-reload purposes.
# Every one of these ALWAYS needs a restart if changed -- custom_script.py's
# refresh_live_settings() only ever re-reads per-vial fields named in
# evolver_code/config_validation.py's LIVE_FIELDS, never anything at this
# level (exp_name is read once at Settings() construction; operation.mode
# picks an entirely different code branch; stir_settings/temp_all are only
# consulted during that same one-time construction).
_TOP_LEVEL_RESTART_FIELDS = ("exp_name", "calib_name", "operation", "stir_settings", "temp_all")


def describe_live_reload_effect(old_config: dict[str, Any], new_config: dict[str, Any],
                                 live_field_names) -> dict[str, list[dict]]:
    """Diffs old_config against new_config and splits every field that
    actually CHANGED into two buckets: applies_without_restart (a per-vial
    field named in live_field_names -- picked up by the rig's
    refresh_live_settings() on its very next cycle, no restart needed) and
    requires_restart (everything else that changed -- every other per-vial
    field, plus any top-level field). Fields that didn't change are not
    reported at all; this describes the effect of THIS write, not a static
    list of what could ever change.

    live_field_names is passed in (rather than imported here) so this stays
    in sync with whatever generation of the validator CONFIG_VALIDATOR_PATH
    currently points at, without this module needing to know about that
    indirection itself.

    old_config/new_config are run through json_safe() first. Found
    necessary by simulating the textbook-CORRECT GET -> edit -> POST
    workflow, not just the partial-patch mistake: old_config comes straight
    off disk via read_config() (real .nan placeholders, ~240 of them in the
    real file), and IEEE-754 says nan != nan -- so every untouched-but-.nan
    field on every OTHER vial was being reported as "changed" (old: nan,
    new: None, since a round-tripped GET already turns .nan into JSON null)
    even when NOTHING was actually touched, and the raw nan in that entry
    then crashed the response the same way GET /config used to. Sanitizing
    both sides here means "unchanged" is judged the same way regardless of
    which side of the diff still has the raw sentinel."""
    old_config, new_config = json_safe(old_config), json_safe(new_config)
    old_vials, new_vials = _per_vial_map(old_config), _per_vial_map(new_config)
    applies_without_restart: list[dict] = []
    requires_restart: list[dict] = []

    for vial in sorted(set(old_vials) | set(new_vials)):
        old_vs, new_vs = old_vials.get(vial, {}), new_vials.get(vial, {})
        for field in sorted(set(old_vs) | set(new_vs)):
            if field == "vial":
                continue
            old_val, new_val = old_vs.get(field), new_vs.get(field)
            if old_val == new_val:
                continue
            entry = {"vial": vial, "field": field, "old": old_val, "new": new_val}
            (applies_without_restart if field in live_field_names else requires_restart).append(entry)

    old_top = (old_config or {}).get("experiment_settings") or {}
    new_top = (new_config or {}).get("experiment_settings") or {}
    for field in _TOP_LEVEL_RESTART_FIELDS:
        old_val, new_val = old_top.get(field), new_top.get(field)
        if old_val != new_val:
            requires_restart.append({"vial": None, "field": field, "old": old_val, "new": new_val})

    return {"applies_without_restart": applies_without_restart, "requires_restart": requires_restart}
