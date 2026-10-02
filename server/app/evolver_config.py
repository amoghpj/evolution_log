"""Per-unit eVOLVER config-repo settings (the config-generation feature,
added 2026-09-01).

Distinct from app/config.py's LOG_REPO_PATH in a way that matters: there is
only one evolution_log.json, so LOG_REPO_PATH names exactly one repo, shared
by every unit. A config write is different -- each eVOLVER unit runs its own
custom_script.py against its own experiment_parameters.yaml, in its own
directory, with its own git history. Per the operator's explicit
instruction: "The user should specify a unique path per evolver that is
running. There is a local git repo in each evolver specific directory. That
git repo should be used for a per evolver logging of changes." So this is a
MAPPING, one path per unit, not a single path -- and every /config request
must name which unit it means; there is no default to fall back on.

EVOLVER_UNIT_PATHS is a JSON object: {"patrick": "/path/to/patrick/evolver_code",
"plankton": "/path/to/plankton/evolver_code"}. Checked at first use, the
same discipline app/config.py's Settings applies to LOG_REPO_PATH: every
path must exist, be a directory, and already be a git repo (has a .git/) --
POST /config commits into it, so it must be real and writable, not a
directory copied out of one.

What is deliberately NOT checked here: whether a key in this mapping is
actually a registered hardware.units name in evolution_log.json. That's the
log's own live data, which can change independently of how this server is
deployed (a new unit could be registered in the log before this server is
redeployed with a path for it, or vice versa) -- checked per-request against
the live log instead, in app/routes/config.py, the same way GET
/vials/{unit}/... already validates its own unit path parameter rather than
baking a unit allowlist into a Settings object constructed once per process.
"""
import json
import os
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException


class EvolverConfigSettings:
    def __init__(self, unit_paths_json: str | None = None):
        raw = unit_paths_json if unit_paths_json is not None else os.environ.get("EVOLVER_UNIT_PATHS")
        if not raw:
            raise RuntimeError(
                "EVOLVER_UNIT_PATHS is not set -- required for the /config routes. Set it to a "
                "JSON object mapping unit name to that unit's evolver_code checkout, e.g. "
                'EVOLVER_UNIT_PATHS=\'{"patrick": "/path/to/patrick/evolver_code"}\''
            )
        try:
            mapping = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("EVOLVER_UNIT_PATHS is not valid JSON (%s)" % exc) from exc
        if not isinstance(mapping, dict) or not mapping:
            raise RuntimeError("EVOLVER_UNIT_PATHS must be a non-empty JSON object of unit -> path")

        self.unit_paths: dict[str, Path] = {}
        not_dir, not_git = [], []
        for unit, path_str in mapping.items():
            path = Path(path_str).resolve()
            if not path.is_dir():
                not_dir.append("%s -> %s" % (unit, path))
                continue
            if not (path / ".git").exists():
                not_git.append("%s -> %s" % (unit, path))
                continue
            self.unit_paths[unit] = path

        problems = []
        if not_dir:
            problems.append("does not exist or is not a directory: %s" % "; ".join(not_dir))
        if not_git:
            problems.append("is not a git repo (no .git/): %s" % "; ".join(not_git))
        if problems:
            raise RuntimeError("EVOLVER_UNIT_PATHS has bad entries -- " + "; ".join(problems))

        # Two unit names resolving to the literal same directory (a plausible
        # copy/paste mistake in the JSON) would silently break "each unit has
        # its own independent history" -- a write via either name becomes
        # visible under both, and their commits interleave in one git log
        # with no indication anything is wrong. Checked once, at
        # construction, the same "fail loudly on misconfiguration" discipline
        # app/config.py's Settings already applies to LOG_REPO_PATH.
        seen_by_path: dict[Path, list[str]] = {}
        for unit, path in self.unit_paths.items():
            seen_by_path.setdefault(path, []).append(unit)
        collisions = {path: units for path, units in seen_by_path.items() if len(units) > 1}
        if collisions:
            raise RuntimeError(
                "EVOLVER_UNIT_PATHS has two or more unit names pointing at the SAME directory -- "
                "each unit must have its own independent repo: %s"
                % "; ".join("%s -> %s" % (sorted(units), path) for path, units in collisions.items())
            )

    def path_for(self, unit: str) -> Path | None:
        return self.unit_paths.get(unit)


@lru_cache
def get_evolver_config_settings() -> EvolverConfigSettings:
    # EvolverConfigSettings() raises a plain RuntimeError (framework-
    # agnostic, easy to construct/test with no FastAPI involved) -- but a
    # RuntimeError out of a Depends() callable is NOT caught by FastAPI at
    # all; it propagates as an opaque, bodyless "Internal Server Error"
    # that only ever reaches server stdout. Found by a real operator: the
    # message ("EVOLVER_UNIT_PATHS is not set...") was correct and specific,
    # but invisible to the caller -- often an LLM with no shell access to
    # read server logs. Same fix app/auth.py's get_operator() already
    # applies to load_operators()'s RuntimeError, and app/config.py's
    # get_settings() now applies to Settings()'s.
    try:
        return EvolverConfigSettings()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
