"""GET /pump_events -- every dispense in the last N hours, per vial, labelled
with its line and reservoir. See app/pump_events.py for what is computed and
why; this module only validates the query and guards the worker pool.

Shares /media's two seams (get_dashboards, get_http_client) so a test points
both routes at the same fake rigs, and shares nothing else: it has its own
concurrency allowance, so a slow rig here cannot starve /media's pump view or
the reverse.
"""
import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings, get_settings
from ..dashboards import DashboardSettings
from ..log_repo import load_log
from ..pump_events import DEFAULT_WINDOW_H, MAX_WINDOW_H, build_pump_events
from .media import get_dashboards, get_http_client

_log = logging.getLogger("or05.pump_events")

router = APIRouter()

# One request fetches a summary plus one call per vial from every unit, so it
# is heavier than /media's pump view; two in flight is plenty for a handful
# of operators and leaves the shared thread pool alone when a rig hangs.
EVENTS_CONCURRENCY = 2
_slots = threading.BoundedSemaphore(EVENTS_CONCURRENCY)


@router.get("/pump_events",
            summary="Every pump dispense in the last N hours, per vial, with its line and reservoir",
            responses={503: {"description": "Too many /pump_events requests already in flight; "
                                            "retry shortly"}})
def get_pump_events(
    window_h: float = Query(
        DEFAULT_WINDOW_H, gt=0, le=MAX_WINDOW_H,
        description="Hours back from NOW to report (0 < window_h <= %g). Not a rig "
                    "controller hour: the rig's own since_h means 'controller hour X', "
                    "and this is converted to it." % MAX_WINDOW_H),
    unit: str | None = Query(None, description="Only this unit, e.g. patrick"),
    vial: int | None = Query(None, ge=0, le=15, description="Only this vial; requires unit"),
    events: bool = Query(True, description="false: per-vial, per-line and per-reservoir "
                                           "totals only, without the event lists"),
    settings: Settings = Depends(get_settings),
    dashboards: DashboardSettings = Depends(get_dashboards),
    http_client=Depends(get_http_client),
) -> dict[str, Any]:
    if vial is not None and unit is None:
        raise HTTPException(status_code=422,
                            detail="vial needs unit too: every unit numbers its own vials "
                                   "from 0, so a vial number alone names no position")
    log = load_log(settings)
    known = sorted((log.get("hardware") or {}).get("units") or {})
    if unit is not None and unit not in known:
        raise HTTPException(status_code=422,
                            detail="no unit %r in this log (it has: %s)"
                                   % (unit, ", ".join(known) or "none"))
    if not _slots.acquire(blocking=False):
        _log.warning("pump_events declined: all %d slots busy; a rig is probably not "
                     "answering", EVENTS_CONCURRENCY)
        raise HTTPException(status_code=503,
                            detail="this server is already serving %d pump-event requests "
                                   "and will not start another -- a rig that has stopped "
                                   "responding would otherwise tie up the worker pool every "
                                   "other route shares. Retry shortly." % EVENTS_CONCURRENCY)
    try:
        client = http_client() if callable(http_client) else http_client
        return build_pump_events(log, dashboards, client, window_h, unit=unit, vial=vial,
                                 include_events=events)
    finally:
        _slots.release()
