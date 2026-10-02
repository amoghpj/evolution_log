"""POST /config -- validate + write experiment_parameters.yaml for one
eVOLVER unit, committing into THAT unit's own git repo (never
evolution_log.json). See app/config_validator.py / app/config_writer.py.

Requires auth for the same reason POST /events and POST /lines do: the
token supplies the git commit author (SERVER_DESIGN.md decision #3), not
because reads or validation need gating -- GET /config and
POST /config/candidate (config.py) stay open.

Deliberately does NOT log anything to evolution_log.json. Per the
operator's explicit instruction (2026-09-01): a config change made this way
must still be logged as a controller_config_change event, but that is a
SEPARATE, deliberate POST /events call by the operator/LLM -- never
implicit here. See GET /config/skill for the exact workflow.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..auth import Operator, get_operator
from ..config import Settings, get_settings
from ..config_models import ConfigRequest
from ..config_validator import LIVE_FIELD_NAMES, ModeNotImplemented, validate_config
from ..config_writer import (
    ConfigCommitFailed, RemovalRejected, UnknownEvolverUnit, check_physically_impossible,
    describe_live_reload_effect, find_non_finite, mode_not_implemented_detail,
    unknown_unit_message, write_config_checked,
)
from ..evolver_config import EvolverConfigSettings, get_evolver_config_settings
from ..log_repo import load_log

router = APIRouter()


def _vials_in_use_warnings(log: dict, unit: str, config: dict) -> list[str]:
    """Non-blocking. Cross-checks active vials against
    hardware.units.<unit>.vials_in_use -- WARNING only, deliberately not a
    rejection, because CLAUDE.md documents this exact field as having known
    live drift (hardware.units.patrick.n_lines reads 6 where vials_in_use
    lists 8 and 8 patrick lines are active) -- hard-blocking on a field this
    project's own docs say can already be stale would risk rejecting a
    config that's actually correct."""
    vials_in_use = (log.get("hardware", {}).get("units", {}).get(unit) or {}).get("vials_in_use")
    if not isinstance(vials_in_use, list):
        return []
    settings_section = (config or {}).get("experiment_settings") or {}
    warnings = []
    for vs in settings_section.get("per_vial_settings") or []:
        if isinstance(vs, dict) and vs.get("to_run") and vs.get("vial") not in vials_in_use:
            warnings.append(
                "vial %r is set to_run: true but is not in hardware.units.%s.vials_in_use (%s) -- "
                "this may be stale log data rather than a real mismatch (see CLAUDE.md's noted "
                "n_lines/vials_in_use drift), not treated as a hard rejection"
                % (vs.get("vial"), unit, vials_in_use)
            )
    return warnings


@router.post("/config", status_code=201,
             summary="Validate, write, and commit experiment_parameters.yaml for one eVOLVER unit")
def post_config(
    request: ConfigRequest,
    settings: Settings = Depends(get_settings),
    evolver_settings: EvolverConfigSettings = Depends(get_evolver_config_settings),
    operator: Operator = Depends(get_operator),
):
    log = load_log(settings)
    if request.unit not in log.get("hardware", {}).get("units", {}):
        raise HTTPException(status_code=404, detail=unknown_unit_message(log, request.unit))

    try:
        problems, warnings = validate_config(request.config)
    except ModeNotImplemented as exc:
        raise HTTPException(status_code=501, detail=mode_not_implemented_detail(exc, request.config)) from exc

    problems = list(problems) + find_non_finite(request.config) + check_physically_impossible(request.config)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    warnings = warnings + _vials_in_use_warnings(log, request.unit, request.config)

    # check_no_silent_removal now runs INSIDE write_config_checked, under
    # the SAME lock acquisition as the write+commit itself -- not here,
    # before the lock is ever taken. Found necessary by simulating two
    # concurrent writers: a pre-lock read+check here let each of two
    # racing requests see the SAME stale old_config, each pass the check
    # relative to that stale snapshot, and the second one's commit would
    # then silently undo whatever the first had just added -- the safety
    # check reporting "fine" on both requests while real data was
    # destroyed. old_config is now returned by write_config_checked
    # itself: the read that happened INSIDE the lock, immediately before
    # the write it's actually paired with, is the only race-free source of
    # "what this write is really changing from."
    exp_name = (request.config.get("experiment_settings") or {}).get("exp_name", "?")
    message = "config: %s (unit=%s)" % (exp_name, request.unit)
    try:
        old_config = write_config_checked(
            evolver_settings, request.unit, request.config, message, operator,
            request.confirm_removed_fields,
        )
    except UnknownEvolverUnit as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RemovalRejected as exc:
        raise HTTPException(status_code=422, detail=exc.problems) from exc
    except ConfigCommitFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "unit": request.unit,
        "written": True,
        "warnings": warnings,
        # Per the operator's explicit instruction (2026-09-01): loudly name
        # exactly which changed fields take effect on the rig's very next
        # cycle (evolver_code/config_validation.py's LIVE_FIELDS) versus
        # which need a restart -- not left to a static skill-doc paragraph
        # the caller has to remember, and not silently assumed either way.
        "live_reload": describe_live_reload_effect(old_config, request.config, LIVE_FIELD_NAMES),
        "reminder": "this write is NOT reflected in evolution_log.json -- log a "
                    "controller_config_change event for it via POST /events (see GET /config/skill)",
    }
