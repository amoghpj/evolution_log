"""POST /parameter_registry -- registers ONE new key in evolution_log.json's
parameter_registry, committing into the SAME repo POST /events/POST /lines
already write to (never a separate one, unlike POST /config).

Requires auth for the same reason every other write route does: the token
supplies the git commit author (SERVER_DESIGN.md decision #3), not because
reads or validation need gating -- GET /parameter_registry and
POST /parameter_registry/candidate (registry.py) stay open.

See app/registry_writer.py for the pipeline and for what's deliberately
out of scope (retiring/editing an existing entry).
"""
from fastapi import APIRouter, Depends, HTTPException

from ..auth import Operator, get_operator
from ..config import Settings, get_settings
from ..registry_models import RegistryEntryRequest
from ..registry_writer import register_parameter
from ..writer import CommitFailed, ValidationFailed, WriteConflict

router = APIRouter()


@router.post("/parameter_registry", status_code=201,
             summary="Register a new parameter_registry key in evolution_log.json")
def post_parameter_registry(
    request: RegistryEntryRequest,
    settings: Settings = Depends(get_settings),
    operator: Operator = Depends(get_operator),
):
    try:
        result = register_parameter(settings, request.key, request.entry, operator)
    except WriteConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationFailed as exc:
        raise HTTPException(status_code=422, detail=exc.problems) from exc
    except CommitFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "key": result["key"],
        "entry": result["entry"],
        "registered": True,
        "reminder": "this key exists in parameter_registry now, but nothing in the log actually "
                    "uses it yet unless you registered it as status: active with a real "
                    "first_seen -- a status: planned entry stays inert until some event's params "
                    "genuinely uses this key",
    }
