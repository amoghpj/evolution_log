"""Server configuration.

LOG_REPO_PATH points at the repo holding evolution_log.json, its schema and
its tools/ -- which, in this layout, is the same repo this server lives in.
It defaults to that (server/app/config.py -> the repo root), so a server
started from a checkout serves that checkout's log.

Because the write routes `git commit` into it, LOG_REPO_PATH must be a real,
writable git checkout on the same machine. log_repo.py imports tools/lineage.py
and tools/media.py from it by path, so the server can never drift from what
those tools consider correct. A wrong path fails loudly on the first request
of any kind (Settings() is built once and shared), not silently on whichever
route happens to need the missing file.
"""
import os
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException


class Settings:
    def __init__(self, log_repo_path: str | None = None):
        default_repo = Path(__file__).resolve().parent.parent.parent
        self.log_repo_path = Path(log_repo_path or os.environ.get("LOG_REPO_PATH", default_repo)).resolve()
        self.log_file = self.log_repo_path / "evolution_log.json"
        self.schema_file = self.log_repo_path / "schema" / "evolution_log.schema.json"
        self.lineage_tool = self.log_repo_path / "tools" / "lineage.py"
        self.media_tool = self.log_repo_path / "tools" / "media.py"

        required = (self.log_file, self.schema_file, self.lineage_tool, self.media_tool)
        missing = [p for p in required if not p.exists()]
        if missing:
            raise RuntimeError(
                "LOG_REPO_PATH (%s) doesn't look like an evolver-log checkout "
                "-- missing: %s. Run ./init_experiment.sh in the repo first, or "
                "set LOG_REPO_PATH to a checkout that has been initialised"
                % (self.log_repo_path, ", ".join(str(p) for p in missing))
            )


@lru_cache
def get_settings() -> Settings:
    # Settings() raises a plain RuntimeError (framework-agnostic, so it's
    # easy to construct/test directly with no FastAPI involved) -- but a
    # RuntimeError raised out of a Depends() callable is NOT caught by
    # FastAPI at all; it propagates as an opaque, bodyless "Internal Server
    # Error" that only ever reaches server stdout, never the caller. Found
    # by a real operator hitting exactly this for EVOLVER_UNIT_PATHS
    # (app/evolver_config.py's identical case): the error message was
    # correct and specific, but only visible in a log the caller -- often
    # an LLM with no shell access -- can never read. Same fix app/auth.py's
    # get_operator() already applies to load_operators()'s RuntimeError.
    try:
        return Settings()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
