"""GET /reservoirs, GET /reservoirs/{reservoir_id}.

reservoir_id contains a slash (<unit>/<media>-<pg>), so the detail route
takes it as a query-style path suffix rather than a single path segment --
see the :path converter below -- otherwise FastAPI's router would only ever
see the part before the slash.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings, get_settings
from ..log_repo import load_log

router = APIRouter()

# reservoirItem.status's real enum (schema/evolution_log.schema.json). Kept
# here rather than read from the schema dynamically, matching how mode's
# enum is already hardcoded in line_models.py.
_STATUS_VALUES = ("active", "retired")


@router.get("/reservoirs", summary="List reservoirs, optionally filtered by status/unit")
def list_reservoirs(
    status: str | None = Query(None, description="Filter by status: active or retired"),
    unit: str | None = Query(None, description="Filter by unit, e.g. patrick or plankton"),
    settings: Settings = Depends(get_settings),
):
    if status is not None and status not in _STATUS_VALUES:
        # Found by simulating a filter-combination probe: an invalid status
        # value used to silently return zero rows, identical in shape to a
        # valid filter that genuinely matched nothing -- a typo was
        # indistinguishable from "no results."
        raise HTTPException(
            status_code=422,
            detail="status must be one of %s, not %r" % (list(_STATUS_VALUES), status),
        )
    log = load_log(settings)
    items = log.get("reservoirs", {}).get("items", [])
    out = [
        r for r in items
        if (status is None or r.get("status") == status)
        and (unit is None or r.get("unit") == unit)
    ]
    return {"count": len(out), "reservoirs": out}


@router.get("/reservoirs/{reservoir_id:path}", summary="Single reservoir by id (id contains a slash)")
def get_reservoir(reservoir_id: str, settings: Settings = Depends(get_settings)):
    log = load_log(settings)
    for r in log.get("reservoirs", {}).get("items", []):
        if r.get("id") == reservoir_id:
            return r
    raise HTTPException(status_code=404, detail="no such reservoir: %r" % reservoir_id)
