"""FastAPI entrypoint.

    ./run_server.sh            (from the repo root -- reads secrets/server.env)

/openapi.json and /docs come from FastAPI automatically. POST /events and
POST /lines are validated against schema/evolution_log.schema.json and
tools/lineage.py directly, not against a second, hand-authored Pydantic copy
of them -- see models.py/line_models.py and README.md for why that's a
deliberate divergence from SERVER_DESIGN.md decision #1's literal wording.
GET /skill generates operator-facing instructions for an LLM client from the
live route table, request models, and the log's own event_types/
parameter_registry -- see app/skill.py.

Bearer-token auth (auth.py) gates the two POST routes only -- GET routes
stay open, since nothing about a read needs operator attribution
(SERVER_DESIGN.md decision #3: the token exists to determine `operator` and
the git commit author). The Tailscale perimeter (decision #2) is what's
assumed to gate access at all; this app does not implement or simulate that
itself.
"""
import sys

# Fail loudly and specifically, before any of the imports below can produce a
# cryptic TypeError deep in some other module's class body. This code uses
# `X | Y` union type hints throughout (PEP 604, invalid to evaluate before
# 3.10 -- e.g. config.py's `log_repo_path: str | None`), and pydantic 2.x
# itself requires Python >=3.9 regardless. A machine with an old Python (a
# stale venv, a system interpreter) must be told exactly why this won't run,
# not left to guess from a traceback that bottoms out in someone else's file.
if sys.version_info < (3, 10):
    raise RuntimeError(
        "evolution_log_server requires Python 3.10+ (found %s). This code "
        "uses `X | Y` union type hints (PEP 604) throughout, which is not "
        "valid syntax to evaluate before 3.10, and pydantic 2.x itself "
        "requires Python >=3.9. Point uvicorn at a Python 3.10+ interpreter -- "
        "e.g. `python3.11 -m venv .venv && .venv/bin/pip install -r "
        "requirements.txt` -- rather than an existing older venv."
        % sys.version.split()[0]
    )

from fastapi import FastAPI  # noqa: E402

from .routes import (  # noqa: E402
    config, events, health, lines, media, registry, reservoirs, skill, vials,
    viewer, write_config, write_events, write_lines, write_registry,
)

app = FastAPI(
    # Deliberately names no experiment: this is fixed at import, before any
    # log is read, and one server codebase serves many experiments' logs.
    # GET /health and GET /skill name the one this process is serving.
    title="Evolution log server",
    description=(
        "Read and append access to evolution_log.json -- the provenance "
        "record of one eVOLVER evolution experiment, named in GET /health. "
        "See LOG_PROTOCOL.md and SERVER_DESIGN.md in the log repo for what "
        "this data means and why this server exists."
    ),
    version="0.1.0",
)

app.include_router(health.router)
app.include_router(viewer.router)
app.include_router(lines.router)
app.include_router(reservoirs.router)
app.include_router(events.router)
app.include_router(vials.router)
app.include_router(media.router)
app.include_router(config.router)
app.include_router(registry.router)
app.include_router(skill.router)
app.include_router(write_events.router)
app.include_router(write_lines.router)
app.include_router(write_config.router)
app.include_router(write_registry.router)
