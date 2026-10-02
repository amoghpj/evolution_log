"""GET /vials/{unit}/{vial} -- what currently occupies a hardware position.

Distinct from GET /lines/{line_id}: a vial is a hardware position, a line is
a culture (LOG_PROTOCOL.md §4, "line identity follows the culture, not the
hardware"). This route answers "what's in vial 9 on patrick right now", which
may have had several different lines occupy it over time (patrick-v05,
patrick-v05#2, ...) -- this returns only whichever one is currently active,
if any; the rest are history, reachable via GET /lines and lineage.
occupies_vial_of, not through this route.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..config import Settings, get_settings
from ..log_repo import load_log

router = APIRouter()


@router.get("/vials/{unit}/{vial}", summary="Whichever line currently occupies this hardware position, if any")
def get_vial(unit: str, vial: int, settings: Settings = Depends(get_settings)):
    log = load_log(settings)
    if unit not in log.get("hardware", {}).get("units", {}):
        raise HTTPException(status_code=404, detail="no such unit: %r" % unit)

    occupant = None
    for line_id, line in log.get("lines", {}).items():
        if line.get("unit") == unit and line.get("vial") == vial and line.get("status") == "active":
            occupant = line_id
            break

    return {
        "unit": unit,
        "vial": vial,
        "occupied_by": occupant,
        "line": log["lines"][occupant] if occupant else None,
    }
