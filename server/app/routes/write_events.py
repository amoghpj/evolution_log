"""POST /events -- the only write route in this server.

See writer.py for the actual pipeline; this module only maps its exceptions
to HTTP status codes. 404: the named line doesn't exist. 409: the write
cannot be expressed as a pure append (SERVER_DESIGN.md §3.A). 422: the
resulting log would fail schema or cross-field validation, or references an
event that doesn't exist. 500: the file write succeeded but the git commit
did not (writer.py has already reverted the file to HEAD by then).
"""
from fastapi import APIRouter, Depends, HTTPException

from ..auth import Operator, get_operator
from ..config import Settings, get_settings
from ..models import NewEventRequest
from ..writer import CommitFailed, ValidationFailed, WriteConflict, append_event

router = APIRouter()


@router.post("/events", status_code=201,
             summary="Append one event to an existing line, or to experiment_events")
def post_event(
    request: NewEventRequest,
    settings: Settings = Depends(get_settings),
    operator: Operator = Depends(get_operator),
):
    fields = request.model_dump(exclude={"target"}, exclude_none=True)
    target = request.target.model_dump(exclude_none=True)

    try:
        event = append_event(settings, target, fields, operator)
    except WriteConflict as exc:
        status = 404 if "no such line" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except ValidationFailed as exc:
        raise HTTPException(status_code=422, detail=exc.problems) from exc
    except CommitFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return event
