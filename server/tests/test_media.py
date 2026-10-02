#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_media.py

Fixture reservoirs (tests/fixture.py): testunit/LB-0 (low, measured rate
0.1 L/h from 1.0L->0.7L over 3h) and testunit/LB-5 (high, 0.05 L/h from
1.0L->0.85L over 3h), both fed by testunit-v01 and testunit-v03.
"""
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


T_READ = "2026-01-01T12:00:00-05:00"  # matches the fixture's reading timestamp


def main():
    client, settings = make_client_with_settings()

    # ── basic shape, at echoed back ──────────────────────────────────────
    r = client.get("/media", params={"at": T_READ})
    ck(r.status_code == 200, "GET /media returns 200 (%s)" % r.status_code)
    body = r.json()
    for key in ("at", "reservoirs", "per_line", "delivered_pg", "high_outlook", "attention"):
        ck(key in body, "response has top-level key %r" % key)
    ck(body["at"] == T_READ, "at is echoed back exactly")
    ck(len(body["reservoirs"]) == 2, "both fixture reservoirs present")

    # ── every provenance field survives, per MEDIA_TRACKING.md §6 --
    # nothing about this is optional ────────────────────────────────────
    lb0 = next(x for x in body["reservoirs"] if x["id"] == "testunit/LB-0")
    for field in ("rate_basis", "rate_is_upper_bound", "rate_provisional",
                  "baseline_orphaned", "level_source"):
        ck(field in lb0, "reservoir row keeps provenance field %r" % field)
    ck(abs(lb0["rate_L_per_h"] - 0.1) < 1e-9, "LB-0's measured rate is 0.1 L/h (%s)" % lb0["rate_L_per_h"])
    ck(lb0["rate_basis"] == "measured", "LB-0's rate basis is measured")
    ck(lb0["level_L"] == 0.7, "LB-0's level is the raw reading, not a projection")

    # ── per_line is a flat list, not the tuple-keyed dict analyse() returns ─
    ck(isinstance(body["per_line"], list), "per_line is a JSON-safe list")
    per_line_lb_low = next(p for p in body["per_line"] if p["media"] == "LB" and p["role"] == "low")
    ck(abs(per_line_lb_low["rate_L_per_h"] - 0.05) < 1e-9,
       "per-line low rate is 0.05 L/h (0.1 L/h over 2 lines)")

    # ── delivered_pg: one group, weighted mean between low and high pg ──────
    ck(len(body["delivered_pg"]) == 1, "one delivered_pg group (single unit/media combo)")
    dose = body["delivered_pg"][0]
    ck(dose["unit"] == "testunit" and dose["media"] == "LB", "the group is testunit/LB")
    ck(abs(dose["mean_pg_g_per_L"] - 2.0) < 1e-9,
       "mean_pg_g_per_L is the rate-weighted mean (0.1*0.5 + 0.05*5.0)/(0.1+0.05) = 2.0 (%s)"
       % dose["mean_pg_g_per_L"])

    # ── attention: sorted soonest-to-empty first, both fixture reservoirs
    # present with distinct hours_remaining ────────────────────────────────
    ck(len(body["attention"]) == 2, "both active reservoirs with a projection appear in attention")
    ck(body["attention"][0]["reservoir_id"] == "testunit/LB-0", "LB-0 (7h left) sorts before LB-5 (17h left)")
    ck(body["attention"][0]["hours_remaining"] < body["attention"][1]["hours_remaining"],
       "attention is actually sorted ascending by hours_remaining")
    ck(all("rate_basis" in a for a in body["attention"]), "every attention entry carries rate_basis")

    # ── at: default is request time, not log_meta.last_updated, and is
    # never earlier than an explicit earlier `at` would project -- omitting
    # it must never show MORE media than a later, fixed timestamp implies ──
    log_meta_last_updated = json.loads(settings.log_file.read_text())["log_meta"]["last_updated"]
    r_default = client.get("/media")
    ck(r_default.json()["at"] not in (T_READ, log_meta_last_updated),
       "default at is neither the fixed reading timestamp nor log_meta.last_updated -- it's request time")
    default_level = next(x for x in r_default.json()["reservoirs"] if x["id"] == "testunit/LB-0")["estimated_now_L"]
    ck(default_level <= lb0["estimated_now_L"],
       "omitting at (a much later 'now') never shows MORE remaining than the earlier, fixed at")

    # ── malformed at -> 422, not a 500 or a silently wrong answer ──────────
    r_bad = client.get("/media", params={"at": "not-a-timestamp"})
    ck(r_bad.status_code == 422, "a malformed at -> 422")

    # ── filters ──────────────────────────────────────────────────────────
    r_unit = client.get("/media", params={"at": T_READ, "unit": "nonexistent-unit"})
    ck(r_unit.json()["reservoirs"] == [], "an unknown unit filters to no reservoirs, not an error")
    ck(r_unit.json()["delivered_pg"] == [], "delivered_pg is empty too, consistent with the filtered set")

    r_status = client.get("/media", params={"at": T_READ, "status": "retired"})
    ck(r_status.json()["reservoirs"] == [], "status=retired matches none in this fixture (both are active)")

    # ── no auth required (read-only) ────────────────────────────────────
    r_noauth = client.get("/media", params={"at": T_READ})
    ck(r_noauth.status_code == 200, "no Authorization header needed")

    # ── the real tools/media.py inconsistency, confirmed by construction:
    # a row that falls back to the fastest-per-line upper bound gets
    # rate_basis="upper_bound" but media.py never sets rate_is_upper_bound
    # to match. This server corrects it before returning. ──────────────────
    log = json.loads(settings.log_file.read_text())
    log["reservoirs"]["items"].append({
        "id": "testunit/NOVEL-1", "unit": "testunit", "media": "NOVELMEDIA", "role": "low",
        "pg": {"value_g_per_L": 1.0, "value_mM": 7.9296, "unit_primary": "g/L"},
        "status": "active",
        "volume_prepared": {"value": 1.0, "unit": "L"}, "prepared_at": T_READ,
        "current_volume": {"value": 0.5, "unit": "L"}, "level_as_of": T_READ,
        "level_source": "prepared", "lines_fed": ["testunit-v01"],
    })
    settings.log_file.write_text(json.dumps(log, indent=2))

    r_novel = client.get("/media", params={"at": T_READ})
    novel_row = next(x for x in r_novel.json()["reservoirs"] if x["id"] == "testunit/NOVEL-1")
    ck(novel_row["rate_basis"] == "upper_bound",
       "the novel-media reservoir (no measured rate of its own) falls back to upper_bound (%s)"
       % novel_row["rate_basis"])
    ck(novel_row["rate_is_upper_bound"] is True,
       "rate_is_upper_bound is corrected to True to match rate_basis, even though "
       "tools/media.py's analyse() itself leaves it False for this fallback path")

    # ── regression: an `at` far in the past used to let analyse()'s linear
    # draw-down run in reverse without limit -- a 1 L bottle reporting over
    # 1000 L "currently remaining." Found by simulating a filter-
    # combination probe. Clamped to the reservoir's own prepared_L, with
    # hours_remaining/empty_at recomputed to stay consistent with the
    # clamped level -- app/routes/media.py:_clamp_backward_extrapolation ──
    far_past = "2000-01-01T00:00:00-05:00"
    r_past = client.get("/media", params={"at": far_past})
    ck(r_past.status_code == 200, "a far-past `at` still returns 200, not an error")
    row = next(x for x in r_past.json()["reservoirs"] if x["id"] == "testunit/LB-0")
    ck(row["estimated_now_L"] == row["prepared_L"] == 1.0,
       "estimated_now_L is clamped to prepared_L, not left at a physically impossible value (%s)"
       % row["estimated_now_L"])
    ck(row["projection"]["hours_remaining"] == round(1.0 / row["rate_L_per_h"], 1),
       "hours_remaining is recomputed FROM the clamped level, staying internally consistent (%s)"
       % row["projection"]["hours_remaining"])

    # ── regression: a real production incident. tools/media.py's
    # readings_for() dereferences params.volume_prepared["value"]/
    # volume_remaining["value"] UNCONDITIONALLY for any media_prep/
    # level_reading event naming a reservoir_id, assuming a human would
    # never omit it. Two real live events (a media_prep on each of two
    # reservoirs, recording only a pg_concentration correction, no volume)
    # hit exactly this and took GET /media down with a bare 500 for every
    # caller. Two-part fix: (1) such a write is now rejected outright
    # (app/writer.py's project_reservoir_state), so this can't recur; but
    # (2) the log is append-only, so the two events that already got
    # through before this existed can never be un-appended -- the read
    # side (app/routes/media.py:_drop_events_readings_for_cant_survive)
    # has to tolerate them regardless. ──────────────────────────────────────
    client2, settings2 = make_client_with_settings()
    r = client2.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "media_prep",
        "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "pg_concentration":
                   {"value_g_per_L": 0.5, "value_mM": 3.9648, "unit_primary": "g/L"}},
        "notes": "a correction to the concentration only -- no volume_prepared, deliberately",
    })
    ck(r.status_code == 422, "a media_prep missing volume_prepared is now rejected outright (%s)" % r.status_code)

    r = client2.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "level_reading",
        "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-5", "level_source": "measured"},
        "notes": "a level_source note with no actual reading -- deliberately incomplete",
    })
    ck(r.status_code == 422, "a level_reading missing volume_remaining is now rejected outright (%s)" % r.status_code)

    # simulate the two events that ALREADY got through before this guard
    # existed -- written directly to the log file, bypassing the write
    # path entirely, exactly matching how the real EVT-00322/EVT-00323
    # actually got there (before this fix was deployed).
    on_disk = json.loads(settings2.log_file.read_text())
    on_disk["experiment_events"].append({
        "event_id": "EVT-LEGACY-1", "timestamp": "2026-01-02T09:00:00-05:00", "event_type": "media_prep",
        "operator": "TEST", "provenance": "reported", "scope": "facility",
        "params": {"reservoir_id": "testunit/LB-0", "pg_concentration":
                   {"value_g_per_L": 0.5, "value_mM": 3.9648, "unit_primary": "g/L"}},
        "notes": "legacy bad data predating the write-time guard", "missing_fields": [],
    })
    on_disk["experiment_events"].append({
        "event_id": "EVT-LEGACY-2", "timestamp": "2026-01-02T09:00:00-05:00", "event_type": "level_reading",
        "operator": "TEST", "provenance": "reported", "scope": "facility",
        "params": {"reservoir_id": "testunit/LB-5", "level_source": "measured"},
        "notes": "legacy bad data predating the write-time guard", "missing_fields": [],
    })
    settings2.log_file.write_text(json.dumps(on_disk, indent=2))

    r_media = client2.get("/media")
    ck(r_media.status_code == 200,
       "GET /media does NOT 500 on pre-existing legacy events missing their volume field (%s)"
       % r_media.status_code)
    ck(sorted(r_media.json()["skipped_events"]) == ["EVT-LEGACY-1", "EVT-LEGACY-2"],
       "the response names exactly the events that had to be skipped, not silently (%s)"
       % r_media.json().get("skipped_events"))
    ck(any(row["id"] == "testunit/LB-0" for row in r_media.json()["reservoirs"]),
       "the affected reservoir still appears in the response, just without that one event's input")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
