"""GET /events, GET /events/{event_id}.

Sorted by timestamp, never by event_id -- LOG_PROTOCOL.md is explicit that
event_id order is not chronological (corrections are appended later carrying
the timestamp of what they correct), and CLAUDE.md lists sorting by id as one
of the specific ways this log has already bitten someone. Getting this wrong
here would hand every client of this API the same mistake.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings, get_settings
from ..log_repo import load_log, unique_events

router = APIRouter()


@router.get("/events", summary="List events sorted by timestamp (never event_id), with filters")
def list_events(
    since: str | None = Query(None, description="ISO 8601 timestamp; only events at or after this"),
    line_id: str | None = Query(None, description="Only events that appear in this line's events[]"),
    event_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
    settings: Settings = Depends(get_settings),
):
    if since is not None:
        try:
            datetime.datetime.fromisoformat(since)
        except ValueError as exc:
            # Found by simulating a filter-combination probe: a malformed
            # `since` used to silently match zero events -- identical in
            # shape to "no events since this genuinely valid moment,"
            # inconsistent with GET /media's `at`, which already validates
            # the same kind of input this way.
            raise HTTPException(
                status_code=422,
                detail="since is not a valid ISO 8601 timestamp with an explicit offset: %s" % exc,
            ) from exc
    log = load_log(settings)

    if line_id is not None:
        line = log.get("lines", {}).get(line_id)
        if line is None:
            raise HTTPException(status_code=404, detail="no such line: %r" % line_id)
        events = {e["event_id"]: e for e in line.get("events", [])}
    else:
        events = unique_events(settings, log)

    matches = list(events.values())
    if since is not None:
        matches = [e for e in matches if e.get("timestamp", "") >= since]
    if event_type is not None:
        matches = [e for e in matches if e.get("event_type") == event_type]

    matches.sort(key=lambda e: (e.get("timestamp", ""), e.get("event_id", "")))
    total = len(matches)
    matches = matches[:limit]
    return {"count": len(matches), "total_matching": total, "events": matches}


@router.get("/events/{event_id}", summary="Single event by id")
def get_event(event_id: str, settings: Settings = Depends(get_settings)):
    log = load_log(settings)
    events = unique_events(settings, log)
    event = events.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="no such event: %r" % event_id)

    # superseded_by: every event whose OWN supersedes points back at this
    # one -- supersedes itself is a bare, one-way pointer stored only on
    # the correcting event (found by simulating a correction-chain
    # operator: nothing else surfaces "has this event been corrected, and
    # by what" -- a reader had to fetch and scan every event on the line by
    # hand). Usually 0 or 1 entries; more than 1 means two DIFFERENT events
    # independently claim to correct this one -- not rejected at write
    # time (a plausible async-multi-operator scenario, not necessarily a
    # mistake), but now at least visible here rather than silently buried.
    # A shallow copy, never the stored event itself -- this is response
    # metadata computed fresh on every read, not part of the permanent record.
    superseded_by = sorted(e["event_id"] for e in events.values() if e.get("supersedes") == event_id)
    response = dict(event)
    response["superseded_by"] = superseded_by
    return response
