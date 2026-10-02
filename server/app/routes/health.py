"""GET /health -- identity and liveness, not a data route.

Modelled on what tools/check_api.py asks of the (separate, eVOLVER-facing)
rig API: identity, a non-zero clock, and enough of a fingerprint that a
client can tell it reached the log server it thinks it reached, not some
other thing on the same port.
"""
import datetime
import subprocess

from fastapi import APIRouter, Depends

from ..auth import tokens_file
from ..config import Settings, get_settings
from ..log_repo import experiment_identity, load_log

router = APIRouter()


@router.get("/health", summary="Liveness, identity, and whether write auth is configured")
def health(settings: Settings = Depends(get_settings)):
    log = load_log(settings)
    meta = log.get("log_meta", {})
    exp_name, exp_title = experiment_identity(log)

    try:
        head = subprocess.run(
            ["git", "-C", str(settings.log_repo_path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except Exception as exc:  # not fatal to /health -- the log still loaded fine
        head = None
        head_error = str(exc)
    else:
        head_error = None

    return {
        "service": "evolution_log_server",
        "status": "ok",
        "server_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "experiment": {"name": exp_name, "title": exp_title},
        "log_repo_path": str(settings.log_repo_path),
        "log_repo_head": head,
        "log_repo_head_error": head_error,
        "schema_version": log.get("schema_version"),
        "log_meta": {
            "last_updated": meta.get("last_updated"),
            "event_counter": meta.get("event_counter"),
        },
        "n_lines": len(log.get("lines", {})),
        "n_reservoirs": len(log.get("reservoirs", {}).get("items", [])),
        "writes_supported": True,
        # Existence only, never contents -- a caller can tell POST /events
        # will 500 for lack of configured tokens, without this route leaking
        # anything about who holds one or what it is.
        "auth_configured": tokens_file().exists(),
    }
