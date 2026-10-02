"""GET /viewer/ -- the browser view of the log, served by this server.

It used to need its own process (serve.sh: `python3 -m http.server` on the
repo root), which was one more thing to keep running and which served the
WHOLE checkout to the network -- secrets/operators.json included, so anyone
who could reach the viewer could fetch a write token.

So this serves an explicit list of three files and nothing else: never a
directory, never a path taken from the request. The page loads its two data
files by relative URL, which is why everything lives under /viewer/ (with the
trailing slash) -- `evolution_log.json` on a page at /viewer/ resolves to
/viewer/evolution_log.json.

Unauthenticated, like every other read route here: the same log is already
readable through GET /lines, /events and /reservoirs. Kept out of the OpenAPI
schema, and so out of GET /skill's route table, because it is for people, not
for an LLM client.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from ..config import Settings, get_settings

router = APIRouter(include_in_schema=False)

# The viewer polls; a cached copy would show a log that has since moved on.
_NO_STORE = {"Cache-Control": "no-store"}


def _file(path, media_type: str, missing: str) -> FileResponse:
    if not path.is_file():
        raise HTTPException(status_code=404, detail=missing)
    return FileResponse(path, media_type=media_type, headers=_NO_STORE)


@router.get("/viewer")
def viewer_redirect():
    # Relative, so it stays right behind a proxy that mounts this elsewhere.
    return RedirectResponse("viewer/", status_code=307)


@router.get("/viewer/")
@router.get("/viewer/viewer.html")
def viewer_page(settings: Settings = Depends(get_settings)):
    return _file(settings.log_repo_path / "viewer.html", "text/html",
                 "viewer.html is missing from %s" % settings.log_repo_path)


@router.get("/viewer/evolution_log.json")
def viewer_log(settings: Settings = Depends(get_settings)):
    return _file(settings.log_file, "application/json", "no evolution_log.json")


@router.get("/viewer/viewer.config.json")
def viewer_config(settings: Settings = Depends(get_settings)):
    # Absent before init_experiment.sh; the page then shows the log without
    # the live pump columns, which is the right degradation.
    return _file(settings.log_repo_path / "viewer.config.json", "application/json",
                 "no viewer.config.json -- written by init_experiment.sh")
