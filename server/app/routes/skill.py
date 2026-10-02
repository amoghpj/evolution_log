"""GET /skill -- see app/skill.py for what this generates and why.
GET /config/skill is a separate document -- see app/config_skill.py."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from ..config import Settings, get_settings
from ..config_skill import render_config_skill
from ..log_repo import experiment_identity, load_log
from ..skill import render_skill

router = APIRouter()


@router.get("/skill", response_class=PlainTextResponse,
            summary="Operator-facing instructions for an LLM client, generated live")
def get_skill(
    request: Request,
    event_type: str | None = Query(None, description="Filter the params-key list to this event_type"),
    settings: Settings = Depends(get_settings),
):
    text = render_skill(request.app, settings, event_type=event_type)
    return PlainTextResponse(content=text, media_type="text/markdown")


@router.get("/config/skill", response_class=PlainTextResponse,
            summary="Operator-facing instructions for an LLM client generating an eVOLVER config")
def get_config_skill(settings: Settings = Depends(get_settings)):
    name, _title = experiment_identity(load_log(settings))
    return PlainTextResponse(content=render_config_skill(name), media_type="text/markdown")
