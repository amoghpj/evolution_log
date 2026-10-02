"""GET /config, POST /config/candidate -- read the current
experiment_parameters.yaml for one eVOLVER unit, and validate a candidate
one WITHOUT writing it anywhere.

See app/config_validator.py for what "valid" means (pumpcontrol_ramp only,
by explicit instruction -- every other operation.mode is 501 Not
Implemented) and app/config_writer.py / app/routes/write_config.py for the
actual write path (POST /config -- requires auth, since it commits into the
unit's own git repo and needs an attributable author; these two routes
never write anything, so they stay open, same reasoning as every other GET
in this server).

Neither of these routes ever touches evolution_log.json. See
GET /config/skill for the workflow an LLM client is told to follow after a
successful POST /config.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings, get_settings
from ..config_models import ConfigRequest
from ..config_validator import (
    SUPPORTED_MODES, ModeNotImplemented, validate_config)
from ..config_writer import (
    UnknownEvolverUnit, check_no_silent_removal, check_physically_impossible,
    find_non_finite, json_safe, mode_not_implemented_detail, read_config, unknown_unit_message,
)
from ..evolver_config import EvolverConfigSettings, get_evolver_config_settings
from ..log_repo import load_log

router = APIRouter()


def _validate_on_read(config: dict | None) -> dict:
    """Validate what is ON DISK, every read, and never fail the read for it.

    A config is written by three different things -- POST /config, the
    dashboard's Setup tab, and an operator with an editor -- and only the
    first validates. So the file a reader gets can be anything, and the read
    is the last moment before a human or an LLM acts on it.

    This NEVER raises and never turns a bad file into a failed request: a
    read that 500s because the config is wrong tells you less than one that
    hands you the config and says what is wrong with it. `problems` means
    custom_script.py would not run this, or would run something the operator
    did not choose; `warnings` never block anything.
    """
    if not config:
        return {"checked": False, "reason": "no experiment_parameters.yaml at this path"}
    try:
        problems, warnings = validate_config(config)
    except ModeNotImplemented as exc:
        return {"checked": False, "mode": exc.mode,
                "supported_modes": sorted(SUPPORTED_MODES),
                "reason": "this server's validator does not cover mode %r, so the "
                          "config below is UNCHECKED -- it is not thereby correct"
                          % exc.mode}
    except Exception as exc:                        # noqa: BLE001
        # A validator fault must not cost the caller the config itself.
        return {"checked": False,
                "reason": "the validator failed on this config (%s: %s)"
                          % (type(exc).__name__, exc)}
    return {"checked": True, "ok": not problems,
            "problems": problems, "warnings": warnings}


@router.get("/config", summary="Read the current experiment_parameters.yaml for one eVOLVER unit")
def get_config(
    unit: str = Query(..., description="eVOLVER unit name, e.g. patrick -- must be a known hardware.units entry"),
    settings: Settings = Depends(get_settings),
    evolver_settings: EvolverConfigSettings = Depends(get_evolver_config_settings),
):
    log = load_log(settings)
    if unit not in log.get("hardware", {}).get("units", {}):
        raise HTTPException(status_code=404, detail=unknown_unit_message(log, unit))

    try:
        config = read_config(evolver_settings, unit)
    except UnknownEvolverUnit as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"unit": unit, "config": json_safe(config), "exists": bool(config),
            "validation": _validate_on_read(config)}


@router.post("/config/candidate", summary="Validate a candidate config WITHOUT writing it anywhere")
def post_config_candidate(
    request: ConfigRequest,
    settings: Settings = Depends(get_settings),
    evolver_settings: EvolverConfigSettings = Depends(get_evolver_config_settings),
):
    log = load_log(settings)
    if request.unit not in log.get("hardware", {}).get("units", {}):
        raise HTTPException(status_code=404, detail=unknown_unit_message(log, request.unit))

    try:
        problems, warnings = validate_config(request.config)
    except ModeNotImplemented as exc:
        raise HTTPException(status_code=501, detail=mode_not_implemented_detail(exc, request.config)) from exc

    problems = list(problems) + find_non_finite(request.config) + check_physically_impossible(request.config)

    # {} if the unit has no configured evolver_code path yet (or no file
    # written yet) -- /candidate must stay usable for a brand-new unit
    # that isn't ready to write to at all; there is nothing to have
    # silently removed from "nothing".
    try:
        old_config = read_config(evolver_settings, request.unit)
    except UnknownEvolverUnit:
        old_config = {}
    if old_config and not request.confirm_removed_fields:
        problems += check_no_silent_removal(old_config, request.config)

    return {"unit": request.unit, "valid": not problems, "problems": problems, "warnings": warnings}
