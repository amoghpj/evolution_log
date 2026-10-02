#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_future_timestamp_guard.py

Regression coverage for a bug found by simulating real operator use: a
future-dated level_reading was accepted with no complaint, and GET /media
(queried with no ``at``, i.e. real "now" -- which was BEFORE that future
event) linearly extrapolated BACKWARD across it, manufacturing a confident,
entirely fictitious "current" volume/hours-remaining for a reservoir whose
own latest real reading said 0.0 L. build_event() (app/writer.py) now
refuses any event timestamped more than an hour ahead of wall-clock now,
for every write path that mints an event -- POST /events directly, and
POST /lines' founding/termination events, which go through the same
function.

Timestamps here are computed relative to real wall-clock now (not a fixed
date), so this test never goes stale as real time moves on.
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_settings  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def iso(dt: datetime.datetime) -> str:
    """ISO 8601 with a colon offset -- never the Z or -0400 shapes the
    schema's own timestamp pattern rejects."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"


def main():
    now = datetime.datetime.now(datetime.timezone.utc)

    # ── POST /events: far in the future -> refused ───────────────────────────
    client, settings = make_client_with_settings()
    before = settings.log_file.read_text()
    far_future = iso(now + datetime.timedelta(days=400))
    r = client.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": far_future,
        "event_type": "level_reading",
        "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "volume_remaining": {"value": 0.0, "unit": "L"},
                   "level_source": "measured"},
        "notes": "a reading dated far in the future",
    })
    ck(r.status_code == 422, "a level_reading dated ~400 days ahead is refused (%s)" % r.status_code)
    ck("in the future" in r.json()["detail"][0], "the problem names why: too far in the future")
    after = settings.log_file.read_text()
    ck(after == before, "the rejected write touched nothing on disk")

    # ── POST /events: comfortably within the grace window -> accepted ──────
    client, settings = make_client_with_settings()
    near_future = iso(now + datetime.timedelta(minutes=10))
    r = client.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": near_future,
        "event_type": "level_reading",
        "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "volume_remaining": {"value": 0.5, "unit": "L"},
                   "level_source": "measured"},
        "notes": "a reading 10 minutes ahead -- ordinary clock skew, not a real mistake",
    })
    ck(r.status_code == 201, "a reading only ~10 minutes ahead succeeds -- grace window for clock skew (%s)" % r.status_code)

    # ── the exact real failure shape: a future level_reading must not be
    # silently accepted and then manufacture a confident wrong number ──────
    # (this is what the guard exists to prevent at the write boundary --
    # the reservoir's projection itself is covered by test_reservoir_
    # projection.py; here we only need to confirm the write never lands.)
    client, settings = make_client_with_settings()
    before = json.loads(settings.log_file.read_text())
    r = client.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": iso(now + datetime.timedelta(days=2)),
        "event_type": "level_reading",
        "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "volume_remaining": {"value": 0.0, "unit": "L"},
                   "level_source": "measured"},
        "notes": "future-dated empty reading",
    })
    ck(r.status_code == 422, "a 2-day-ahead reading is refused too (not just the ~400-day extreme case)")
    after = json.loads(settings.log_file.read_text())
    res_before = next(x for x in before["reservoirs"]["items"] if x["id"] == "testunit/LB-0")
    res_after = next(x for x in after["reservoirs"]["items"] if x["id"] == "testunit/LB-0")
    ck(res_before == res_after, "the reservoir's projection is completely untouched by the refused write")

    # ── POST /lines: a founding_event timestamped in the future is refused
    # the same way, since it goes through the same build_event() ───────────
    client, settings = make_client_with_settings()
    before = settings.log_file.read_text()
    r = client.post("/lines", json={
        "begin_mode": "restart",  # day-one founder, no predecessor needed
        "new_line": {
            "unit": "testunit", "vial": 10, "strain": "s", "initial_media": "LB", "current_media": "LB",
            "mode": "constant", "t0": far_future,
            "pg_regime": {"low": {"value_g_per_L": 0.5, "value_mM": 3.9648, "unit_primary": "g/L"},
                          "high": {"value_g_per_L": 5.0, "value_mM": 39.6479, "unit_primary": "g/L"},
                          "effective_from": far_future},
            "reservoirs": {"low": "testunit/LB-0", "high": "testunit/LB-5"},
            "founding_event": {
                "timestamp": far_future, "event_type": "inoculation",
                "provenance": "reported", "params": {}, "notes": "a founding event dated far in the future",
            },
        },
    })
    ck(r.status_code == 422, "POST /lines' founding_event dated far ahead is refused too (%s: %s)" % (r.status_code, r.text[:200]))
    after = settings.log_file.read_text()
    ck(after == before, "the rejected POST /lines write touched nothing on disk")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
