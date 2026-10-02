"""POST /lines -- the four ways a line can begin (LOG_PROTOCOL.md §5):
branch, split, merge, restart. See lines_writer.py for the pipeline; this
module only maps its exceptions to HTTP status codes, same convention as
write_events.py.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..auth import Operator, get_operator
from ..config import Settings, get_settings
from ..line_models import NewLineRequest
from ..lines_writer import create_line
from ..writer import CommitFailed, ValidationFailed, WriteConflict

router = APIRouter()


@router.post("/lines", status_code=201,
             summary="Create a new line: branch, split, merge, or restart (LOG_PROTOCOL.md §5)")
def post_line(
    request: NewLineRequest,
    settings: Settings = Depends(get_settings),
    operator: Operator = Depends(get_operator),
):
    try:
        result = create_line(settings, request, operator)
    except WriteConflict as exc:
        status = 404 if "no such" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except ValidationFailed as exc:
        raise HTTPException(status_code=422, detail=exc.problems) from exc
    except CommitFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return result
