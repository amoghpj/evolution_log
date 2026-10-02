"""Read access to the log repo named by Settings.log_repo_path.

Nothing here re-implements logic tools/lineage.py or tools/media.py already
has. Both are imported by file path from the configured log repo rather than
duplicated, so this server can never quietly drift from what that repo's own
tools consider correct -- if lineage.py's definition of "unique events"
changes there, or media.py's consumption model does, this server picks the
change up automatically the next time it imports it, rather than carrying a
second, possibly-stale copy.

Read-only itself, but not a description of what this server as a whole does
any more -- app/writer.py and app/lines_writer.py write to the log repo via
the very modules imported here (recompute, validate). Historically this
docstring said "Phase 3's POST /events is not built yet"; both POST routes
exist now (see README.md).
"""
import importlib.util
import json
import types
from functools import lru_cache
from typing import Any

from .config import Settings


def load_log(settings: Settings) -> dict[str, Any]:
    with open(settings.log_file) as fh:
        return json.load(fh)


def experiment_identity(log: dict[str, Any]) -> tuple[str, str | None]:
    """(name, title) for whichever experiment this log records.

    Read from the log rather than configured on the server, so a server can
    never introduce itself as one experiment while serving another's log --
    which is what a hardcoded "OR05" did to every log scaffolded since.
    new_log_server.sh writes `name` and `title`; older logs carry `id`
    (OR05's) or `identity` (early scaffolds), so each is a fallback, and a
    log with none of them says so rather than borrowing a name."""
    exp = log.get("experiment") or {}
    name = exp.get("name") or exp.get("id") or "unnamed experiment"
    title = exp.get("title") or exp.get("identity")
    return str(name), (str(title) if title else None)


def load_schema(settings: Settings) -> dict[str, Any]:
    with open(settings.schema_file) as fh:
        return json.load(fh)


@lru_cache
def _import_module(name: str, path: str) -> types.ModuleType:
    """Import a tools/*.py file from the configured log repo as a module.
    Cached on (name, path), not on log content, so this is safe to call once
    per process even though it does file I/O for the import machinery. name
    just needs to be distinct per tool so two tools' modules in sys.modules
    (if either ever gets registered there) can't collide."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lineage_module(settings: Settings) -> types.ModuleType:
    return _import_module("_log_repo_lineage", str(settings.lineage_tool))


def media_module(settings: Settings) -> types.ModuleType:
    return _import_module("_log_repo_media", str(settings.media_tool))


def unique_events(settings: Settings, log: dict[str, Any]) -> dict[str, dict]:
    """Every event keyed by event_id, deduplicated -- a shared event (a split
    or merge) appears in more than one line's events[] and must count once.
    Delegates to tools/lineage.py:unique_events (see module docstring)."""
    return lineage_module(settings).unique_events(log)
