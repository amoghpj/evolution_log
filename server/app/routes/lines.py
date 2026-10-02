"""GET /lines, GET /lines/{line_id}.

The list route returns summaries, not full event histories -- LOG_PROTOCOL.md
describes tools/evolver_api.py the same way for the rig's own API ("serves a
per-vial summary, not raw logs"), and the same reasoning applies here: a
client asking "what's running" shouldn't have to pull 30-plus lines' worth of
events to get an answer. The detail route returns the line's stored object
verbatim, events included -- anything less would mean re-deciding, in this
server, which fields matter, which is exactly the kind of duplicated
judgment this whole project has been trying to avoid.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings, get_settings
from ..log_repo import load_log

router = APIRouter()


def _summarize(line_id: str, line: dict) -> dict:
    pg = line.get("pg_regime") or {}
    lineage = line.get("lineage") or {}
    return {
        "line_id": line_id,
        "unit": line.get("unit"),
        "vial": line.get("vial"),
        "strain": line.get("strain"),
        "status": line.get("status"),
        "mode": line.get("mode"),
        "current_media": line.get("current_media"),
        "media_switch_count": line.get("media_switch_count"),
        "pg_low": pg.get("low"),
        "pg_high": pg.get("high"),
        "is_founder": lineage.get("is_founder"),
        "parents": lineage.get("parents"),
        "children": lineage.get("children"),
        "depth": lineage.get("depth"),
    }


_STATUS_VALUES = ("active", "ended")  # line.status's real enum -- see reservoirs.py's identical comment


@router.get("/lines", summary="List lines as summaries, optionally filtered by status/unit")
def list_lines(
    status: str | None = Query(None, description="Filter by line.status: active or ended"),
    unit: str | None = Query(None, description="Filter by unit, e.g. patrick or plankton"),
    settings: Settings = Depends(get_settings),
):
    if status is not None and status not in _STATUS_VALUES:
        # Same reasoning as reservoirs.py's identical check: an invalid
        # status used to silently return zero rows, indistinguishable from
        # "no matches" -- found by simulating a filter-combination probe.
        raise HTTPException(
            status_code=422,
            detail="status must be one of %s, not %r" % (list(_STATUS_VALUES), status),
        )
    log = load_log(settings)
    out = []
    for line_id, line in log.get("lines", {}).items():
        if status is not None and line.get("status") != status:
            continue
        if unit is not None and line.get("unit") != unit:
            continue
        out.append(_summarize(line_id, line))
    # Sorted by (vial, unit), not left in whatever order the log's own
    # lines{} dict happens to iterate in -- vial numbers are NOT unique
    # across units (patrick and plankton each number their own vials from
    # 1), so an ambiguous human instruction naming only a vial ("vial 5
    # died") maps to more than one real, active line. Sorting this way puts
    # every same-numbered vial from every unit next to each other, so that
    # kind of collision is visible at a glance instead of requiring a
    # manual scan across the whole (unsorted) list -- found by simulating
    # an operator resolving exactly this kind of ambiguous request.
    out.sort(key=lambda s: (s["vial"] if s["vial"] is not None else -1, s["unit"] or "", s["line_id"]))
    return {"count": len(out), "lines": out}


@router.get("/lines/{line_id}", summary="Full line object, including its entire events[] history")
def get_line(line_id: str, settings: Settings = Depends(get_settings)):
    log = load_log(settings)
    line = log.get("lines", {}).get(line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="no such line: %r" % line_id)
    return line
