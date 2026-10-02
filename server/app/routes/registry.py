"""GET /parameter_registry, POST /parameter_registry/candidate -- read the
log's current parameter_registry, and validate a candidate NEW entry
WITHOUT writing it anywhere.

See app/registry_writer.py for what "valid" means and for the pipeline
these two routes share with the real write, and POST /parameter_registry
(write_registry.py) for the actual write path -- requires auth, since it
commits into evolution_log.json and needs an attributable author. Neither
of these two routes ever writes anything, so they stay open, same
reasoning as every other GET in this server.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..config import Settings, get_settings
from ..log_repo import load_log
from ..registry_models import RegistryEntryRequest
from ..registry_writer import validate_registration

router = APIRouter()


@router.get("/parameter_registry", summary="Read the log's current parameter_registry")
def get_parameter_registry(
    key: str | None = None,
    settings: Settings = Depends(get_settings),
):
    log = load_log(settings)
    registry = log.get("parameter_registry", {})
    if key is not None:
        if key not in registry:
            raise HTTPException(status_code=404, detail="no parameter_registry entry named %r" % key)
        return {"parameter_registry": {key: registry[key]}}
    return {"parameter_registry": registry}


@router.post("/parameter_registry/candidate",
             summary="Validate a candidate NEW parameter_registry entry WITHOUT writing it anywhere")
def post_parameter_registry_candidate(
    request: RegistryEntryRequest,
    settings: Settings = Depends(get_settings),
):
    problems = validate_registration(settings, request.key, request.entry)
    return {"key": request.key, "valid": not problems, "problems": problems}
