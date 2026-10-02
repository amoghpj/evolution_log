#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_events.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    client = make_client()

    r = client.get("/events")
    ck(r.status_code == 200, "GET /events returns 200")
    body = r.json()
    ck(body["count"] == 7, "all 7 fixture events present (3 line events + 4 media/level events)")
    ids_in_order = [e["event_id"] for e in body["events"]]
    # EVT-00001, 00003, 00004 and 00005 all share the same timestamp (t0) --
    # event_id is the documented tiebreaker (LOG_PROTOCOL.md: sort by
    # timestamp, then event_id, never event_id alone). EVT-00006/00007 share
    # t_read; EVT-00002 (termination, t_end) sorts last of all.
    ck(ids_in_order == ["EVT-00001", "EVT-00003", "EVT-00004", "EVT-00005", "EVT-00006", "EVT-00007", "EVT-00002"],
       "sorted by (timestamp, event_id), not event_id alone")

    r = client.get("/events", params={"line_id": "testunit-v01"})
    ck(r.json()["count"] == 1, "line_id filter scopes to that line's own events")
    ck(r.json()["events"][0]["event_id"] == "EVT-00001", "the right event for that line")

    r = client.get("/events", params={"line_id": "does-not-exist"})
    ck(r.status_code == 404, "unknown line_id 404s rather than returning an empty list")

    r = client.get("/events", params={"event_type": "termination"})
    ck(r.json()["count"] == 1, "event_type filter matches only the termination event")

    r = client.get("/events", params={"since": "2026-01-03T00:00:00-05:00"})
    ck(r.json()["count"] == 1, "since filters out the earlier inoculation event")
    ck(r.json()["events"][0]["event_id"] == "EVT-00002", "only the later event survives the since filter")

    r = client.get("/events", params={"limit": 1})
    ck(r.json()["count"] == 1 and r.json()["total_matching"] == 7,
       "limit truncates the page but total_matching still reports the true count")

    r = client.get("/events/EVT-00001")
    ck(r.status_code == 200 and r.json()["event_type"] == "inoculation", "GET /events/{id} resolves a single event")

    r = client.get("/events/EVT-99999")
    ck(r.status_code == 404, "unknown event id 404s")

    # regression: GET /events/{id} now names every event that supersedes
    # IT, since supersedes itself is a bare one-way pointer with no
    # aggregate view -- found by simulating a correction-chain operator.
    r = client.get("/events/EVT-00001")
    ck(r.json()["superseded_by"] == [], "an uncorrected event reports an empty superseded_by")

    r_correction = client.post("/events", json={
        "target": {"line_id": "testunit-v01"}, "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "note", "provenance": "reported", "params": {},
        "notes": "correcting EVT-00001", "supersedes": "EVT-00001",
    })
    ck(r_correction.status_code == 201, "the corrective event is appended")
    correction_id = r_correction.json()["event_id"]
    r = client.get("/events/EVT-00001")
    ck(r.json()["superseded_by"] == [correction_id],
       "the original event now reports the correcting event's id in superseded_by")

    # regression: a malformed ?since= used to silently match zero events,
    # indistinguishable from "no events since this genuinely valid moment"
    # -- and inconsistent with GET /media's `at`, which already validates
    # the same kind of input this way. Found by simulating a filter-
    # combination probe.
    r = client.get("/events", params={"since": "not-a-timestamp"})
    ck(r.status_code == 422, "a malformed since -> 422, not a silent empty result")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
