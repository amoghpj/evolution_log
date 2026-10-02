#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_reservoir_projection.py

Regression coverage for ISSUE_001 (POST /events left reservoirs[] and
log_meta.last_updated stale). See issues/ISSUE_001.md "4. Tests" for the six
scenarios this file implements, in order.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_settings  # noqa: E402
from tests.fixture import build_real_clone  # noqa: E402
from app.log_repo import media_module  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def reservoir_body(**overrides):
    body = {
        "target": {"scope": "facility"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "level_reading",
        "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "volume_remaining": {"value": 0.55, "unit": "L"},
                   "level_source": "measured", "measurement_qualifier": "approximate"},
        "notes": "a test level reading",
    }
    body.update(overrides)
    return body


def find_reservoir(log, reservoir_id):
    return next(r for r in log["reservoirs"]["items"] if r["id"] == reservoir_id)


def main():
    # ── 1. a level_reading moves current_volume/level_as_of/level_source,
    # all in the SAME response, not just on the next GET ────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json=reservoir_body())
    ck(r.status_code == 201, "level_reading write returns 201 (%s)" % r.status_code)
    body = r.json()
    proj = body["reservoir_projection"]
    ck(proj["projected"] is True, "reservoir_projection reports projected=True")
    ck(set(proj["touched"]) == {"current_volume", "level_as_of", "level_set_by_event",
                                 "level_source", "level_qualifier"},
       "reservoir_projection names every column it touched (%s)" % proj["touched"])

    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "testunit/LB-0")
    ck(res["current_volume"] == {"value": 0.55, "unit": "L"}, "current_volume moved to the new reading")
    ck(res["level_as_of"] == "2026-01-02T09:00:00-05:00", "level_as_of moved to the event's timestamp")
    ck(res["level_source"] == "measured", "level_source moved to the event's value")
    ck(res["level_qualifier"] == "approximate", "level_qualifier moved to the event's value")
    ck(res["level_set_by_event"] == body["event_id"], "level_set_by_event names the event that set it")

    # ── 2. a backfilled reading, older than the current level_as_of, is
    # appended but must not move the projection backward ────────────────────
    client, settings = make_client_with_settings()
    before = json.loads(settings.log_file.read_text())
    before_res = find_reservoir(before, "testunit/LB-0")

    r = client.post("/events", json=reservoir_body(timestamp="2026-01-01T10:00:00-05:00"))
    ck(r.status_code == 201, "a backfilled (older) reading is still appended (201)")
    proj = r.json()["reservoir_projection"]
    ck(proj["projected"] is False, "an older reading reports projected=False")
    ck("predates" in proj["reason"], "the reason names why: the event predates level_as_of")

    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "testunit/LB-0")
    ck(res == before_res, "the reservoir's projected state is byte-identical to before the backfilled write")
    ck(any(e["event_id"] == r.json()["event_id"]
           for e in on_disk["experiment_events"]), "the backfilled event itself was still recorded")

    # ── 3. media_prep closes the outgoing bottle into fill_history and the
    # new bottle becomes current ─────────────────────────────────────────────
    client, settings = make_client_with_settings()
    before = json.loads(settings.log_file.read_text())
    old_res = find_reservoir(before, "testunit/LB-0")

    r = client.post("/events", json=reservoir_body(
        event_type="media_prep",
        timestamp="2026-01-02T09:00:00-05:00",
        params={"reservoir_id": "testunit/LB-0", "volume_prepared": {"value": 1.2, "unit": "L"}},
    ))
    ck(r.status_code == 201, "media_prep write returns 201 (%s)" % r.status_code)
    proj = r.json()["reservoir_projection"]
    ck(proj["projected"] is True, "media_prep reservoir_projection reports projected=True")
    ck("fill_history" in proj["touched"], "media_prep reports fill_history as touched")
    ck("pg" in proj["untouched"], "media_prep without pg_concentration reports pg as untouched, not invented")

    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "testunit/LB-0")
    ck(len(res.get("fill_history") or []) == 1, "fill_history gained exactly one closed entry")
    closed = res["fill_history"][0]
    ck(closed["prepared_at"] == old_res["prepared_at"], "the closed entry keeps the OLD bottle's prepared_at")
    ck(closed["volume_prepared"] == old_res["volume_prepared"], "the closed entry keeps the OLD bottle's volume_prepared")
    ck(closed["retired_at"] == "2026-01-02T09:00:00-05:00", "the closed entry's retired_at is the media_prep event's timestamp")
    ck(closed["remaining_at_swap"] == old_res["current_volume"],
       "remaining_at_swap is whatever was left in the OLD bottle at swap time")
    ck(res["current_volume"] == {"value": 1.2, "unit": "L"}, "current_volume is now the new bottle's volume")
    ck(res["level_source"] == "prepared", "level_source is 'prepared' for a freshly-prepared bottle")
    ck(res["level_qualifier"] == "exact", "level_qualifier is 'exact' for a freshly-prepared bottle")
    ck(res["prepared_by_event"] == r.json()["event_id"], "prepared_by_event names the media_prep event")
    ck(res["pg"] == old_res["pg"], "pg was NOT changed -- this media_prep didn't supply pg_concentration")

    # ── 4. a level_reading whose params omit measurement_qualifier must not
    # invent a value, and the response must say so ──────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json=reservoir_body(
        timestamp="2026-01-02T09:00:00-05:00",
        params={"reservoir_id": "testunit/LB-0", "volume_remaining": {"value": 0.55, "unit": "L"},
                "level_source": "measured"},
    ))
    ck(r.status_code == 201, "level_reading without measurement_qualifier still succeeds (201)")
    proj = r.json()["reservoir_projection"]
    ck(proj["projected"] is True, "the columns params DID supply are still projected")
    ck("level_qualifier" in proj["untouched"], "level_qualifier is reported as untouched, not silently guessed")
    ck("level_qualifier" not in proj["touched"], "level_qualifier is NOT in touched")

    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "testunit/LB-0")
    ck(res.get("level_qualifier") is None, "no value was invented for level_qualifier on disk")
    ck(res["current_volume"] == {"value": 0.55, "unit": "L"}, "current_volume was still projected normally")

    # ── 5. regression on the real failure shape: patrick/M9-1 reported
    # 0.50 L* (prepared-only, ~17h stale) when a measured 0.45 L reading
    # (the real EVT-00298) was sitting in the log the old code never
    # projected. Use a scratch clone of the real repo; never the live log,
    # and never re-POST the real event id -- exercise the same write path
    # the fix now takes, under a fresh event id, same as any other operator
    # write would ─────────────────────────────────────────────────────────
    real_root = build_real_clone()
    client, settings = make_client_with_settings(repo_path=str(real_root))

    before = json.loads(settings.log_file.read_text())
    before_res = find_reservoir(before, "patrick/M9-1")
    ck(before_res["level_source"] == "prepared",
       "sanity check: patrick/M9-1 in the real repo is still stuck on 'prepared' before the fix runs")

    real_timestamp = "2026-08-28T11:30:00-04:00"
    r = client.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": real_timestamp,
        "event_type": "level_reading",
        "provenance": "reported",
        "params": {"reservoir_id": "patrick/M9-1", "volume_remaining": {"value": 0.45, "unit": "L"},
                   "level_source": "measured", "measurement_qualifier": "approximate"},
        "notes": "Routine reservoir level reading reported by the operator.",
    })
    ck(r.status_code == 201, "replaying the real reading against the scratch clone succeeds (%s)" % r.status_code)
    ck(r.json()["reservoir_projection"]["projected"] is True, "the real reservoir_id projects cleanly")

    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "patrick/M9-1")
    ck(res["current_volume"] == {"value": 0.45, "unit": "L"}, "patrick/M9-1's current_volume is now the measured 0.45 L")
    ck(res["level_source"] == "measured", "patrick/M9-1's level_source is now 'measured', not 'prepared'")

    media = media_module(settings)
    rows, _perline = media.analyse(on_disk, real_timestamp)
    row = next(row for row in rows if row["id"] == "patrick/M9-1")
    ck(row["level_L"] == 0.45, "tools/media.py's analyse() now reports the measured 0.45 L for patrick/M9-1")
    ck(row["level_source"] == "measured",
       "analyse() reports level_source='measured' -- the report layer's '*' (prepared-only) marker will not fire")

    # ── 6. log_meta.last_updated moves to the event's own timestamp, in the
    # right format, and never backward ───────────────────────────────────────
    client, settings = make_client_with_settings()
    before = json.loads(settings.log_file.read_text())
    original_last_updated = before["log_meta"]["last_updated"]

    later_ts = "2026-01-06T09:00:00-05:00"
    r = client.post("/events", json=reservoir_body(timestamp=later_ts))
    ck(r.status_code == 201, "level_reading with a later timestamp succeeds (201)")
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["log_meta"]["last_updated"] == later_ts, "log_meta.last_updated moved to the event's timestamp")
    ck(TIMESTAMP_RE.match(on_disk["log_meta"]["last_updated"]) is not None,
       "log_meta.last_updated uses a colon offset, matching the schema's timestamp pattern")

    earlier_ts = "2026-01-01T08:00:00-05:00"
    ck(earlier_ts < original_last_updated, "sanity check: earlier_ts really is earlier than the fixture's original last_updated")
    r = client.post("/events", json=reservoir_body(timestamp=earlier_ts))
    ck(r.status_code == 201, "level_reading with an earlier timestamp is still appended (201)")
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["log_meta"]["last_updated"] == later_ts,
       "log_meta.last_updated did NOT move backward for an earlier-timestamped write")

    # ── 7. the forward-only guard must compare against the LATEST EXISTING
    # event in full history, not just reservoirs[].level_as_of -- found by
    # simulating real operator use against a log with a known-stale
    # projection: a new reading whose timestamp looks "forward" relative to
    # a lagging level_as_of, but is actually OLDER than an already-recorded
    # (just never-successfully-projected) reading, must still be refused ──
    client, settings = make_client_with_settings()
    t1, t2, t3 = "2026-01-02T09:00:00-05:00", "2026-01-03T09:00:00-05:00", "2026-01-04T09:00:00-05:00"

    # t1: a normal reading that DOES apply -- level_as_of is now t1.
    r1 = client.post("/events", json=reservoir_body(timestamp=t1))
    ck(r1.status_code == 201, "the first reading (t1) succeeds")
    ck(r1.json()["reservoir_projection"]["projected"] is True, "the first reading (t1) is projected")

    # t3: a real, fully-formed level_reading event for this reservoir_id,
    # already sitting in history at t3 -- but with the PROJECTED FIELD
    # deliberately rolled back to before it, simulating exactly the real
    # ISSUE_001 shape (a field that lagged behind history that already
    # existed, e.g. from before this projection code existed at all, or a
    # hand-edit). Constructed directly on disk rather than by posting an
    # incomplete event through the API -- level_reading/media_prep now
    # REQUIRE their volume field whenever reservoir_id is set (a separate,
    # later production incident: two real events omitting it took GET
    # /media down for every caller), so "missing volume_remaining" is no
    # longer a way to make a real event fail to project.
    r3 = client.post("/events", json=reservoir_body(timestamp=t3))
    ck(r3.status_code == 201, "the t3 reading is appended and projects normally")
    on_disk = json.loads(settings.log_file.read_text())
    find_reservoir(on_disk, "testunit/LB-0")["level_as_of"] = t1  # simulate the field lagging behind
    settings.log_file.write_text(json.dumps(on_disk, indent=2))
    ck(find_reservoir(json.loads(settings.log_file.read_text()), "testunit/LB-0")["level_as_of"] == t1,
       "level_as_of is now ARTIFICIALLY behind the t3 event that's really in history")

    # t2 (t1 < t2 < t3): a full, well-formed reading. Under the OLD guard
    # (comparing only against the field, still at t1), t2 > t1 would look
    # like forward progress and get applied -- incorrectly regressing state
    # behind the t3 event already sitting in history. The fixed guard scans
    # full history and must refuse this.
    r2 = client.post("/events", json=reservoir_body(timestamp=t2))
    ck(r2.status_code == 201, "the t2 reading is still appended (append-only)")
    proj2 = r2.json()["reservoir_projection"]
    ck(proj2["projected"] is False,
       "the t2 reading is REFUSED -- it is older than the t3 event already in history, even "
       "though it looks newer than the (stale) level_as_of field")
    ck("predates" in proj2["reason"], "the reason names why: it predates a reading already recorded")
    on_disk = json.loads(settings.log_file.read_text())
    ck(find_reservoir(on_disk, "testunit/LB-0")["level_as_of"] == t1,
       "level_as_of is unchanged by the refused t2 write (still t1, not regressed and not advanced)")

    # ── 8. media_prep's pg_concentration projects onto reservoirs[].pg --
    # found by simulating a "mistaken reservoir concentration" operator:
    # media_prep moved every OTHER field a prep implies, but never this
    # registered one, so a reservoir's own displayed pg stayed wrong
    # forever, even after a corrective media_prep carrying supersedes ──────
    client, settings = make_client_with_settings()
    before = json.loads(settings.log_file.read_text())
    original_pg = find_reservoir(before, "testunit/LB-0")["pg"]

    # the mistake: prepared at the WRONG concentration
    r_wrong = client.post("/events", json=reservoir_body(
        event_type="media_prep", timestamp="2026-01-02T09:00:00-05:00",
        params={"reservoir_id": "testunit/LB-0", "volume_prepared": {"value": 1.0, "unit": "L"},
                "pg_concentration": {"value_g_per_L": 2.0, "value_mM": round(2.0 / 126.11 * 1000, 4), "unit_primary": "g/L"}},
    ))
    ck(r_wrong.status_code == 201, "the mistaken media_prep succeeds (201)")
    ck("pg" in r_wrong.json()["reservoir_projection"]["touched"], "pg_concentration IS reported as touched")
    on_disk = json.loads(settings.log_file.read_text())
    ck(find_reservoir(on_disk, "testunit/LB-0")["pg"]["value_g_per_L"] == 2.0,
       "reservoir pg moved to the (wrong) concentration this media_prep claimed")

    # the correction: a NEW media_prep, supersedes-ing the mistaken one
    wrong_event_id = r_wrong.json()["event_id"]
    r_fixed = client.post("/events", json=reservoir_body(
        event_type="media_prep", timestamp="2026-01-02T09:05:00-05:00",
        params={"reservoir_id": "testunit/LB-0", "volume_prepared": {"value": 1.0, "unit": "L"},
                "pg_concentration": original_pg},
        supersedes=wrong_event_id, notes="correcting the mistaken PG concentration above",
    ))
    ck(r_fixed.status_code == 201, "the corrective media_prep succeeds (201)")
    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "testunit/LB-0")
    ck(res["pg"] == original_pg, "reservoir pg is corrected back to the right concentration")

    # ── 9. a media_prep naming a reservoir_id that does NOT exist yet
    # CREATES it, if the event supplies everything reservoirItem requires
    # (media, role, pg_concentration, volume_prepared -- all already
    # registered for media_prep) -- the fix for "there was no way to bring
    # a new reservoir online via this API at all" ──────────────────────────
    client, settings = make_client_with_settings()
    before = json.loads(settings.log_file.read_text())
    ck(all(r["id"] != "testunit/LB-1" for r in before["reservoirs"]["items"]),
       "sanity check: testunit/LB-1 does not exist yet")

    r = client.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": "2026-01-05T09:00:00-05:00",
        "event_type": "media_prep",
        "provenance": "reported",
        "params": {
            "reservoir_id": "testunit/LB-1",
            "volume_prepared": {"value": 1.0, "unit": "L"},
            "media": "LB",
            "role": "low",
            "pg_concentration": {"value_g_per_L": 1.0, "value_mM": 7.9296, "unit_primary": "g/L"},
        },
        "notes": "brand new reservoir, replacing a retired one",
    })
    ck(r.status_code == 201, "media_prep naming a brand-new reservoir_id succeeds (%s: %s)"
       % (r.status_code, r.text[:300]))
    proj = r.json()["reservoir_projection"]
    ck(proj["projected"] is True and proj.get("created") is True,
       "reservoir_projection reports projected=True and created=True (%s)" % proj)

    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "testunit/LB-1")
    ck(res["unit"] == "testunit", "unit is derived from the reservoir_id's own <unit>/... shape")
    ck(res["media"] == "LB" and res["role"] == "low", "media/role are exactly what the event supplied")
    ck(res["pg"]["value_g_per_L"] == 1.0, "pg is exactly what pg_concentration supplied")
    ck(res["status"] == "active", "a brand-new reservoir starts active")
    ck(res["volume_prepared"] == {"value": 1.0, "unit": "L"} and res["current_volume"] == {"value": 1.0, "unit": "L"},
       "volume_prepared and current_volume both start at the prepared volume")
    ck(res["level_source"] == "prepared" and res["level_qualifier"] == "exact",
       "level_source/level_qualifier match an ordinary fresh prep")
    ck(res["prepared_by_event"] == r.json()["event_id"] and res["level_set_by_event"] == r.json()["event_id"],
       "prepared_by_event/level_set_by_event both name the creating event")
    ck("fill_history" not in res, "a brand-new reservoir has no fill_history -- nothing to close out")

    r_get = client.get("/reservoirs/testunit/LB-1")
    ck(r_get.status_code == 200 and r_get.json()["id"] == "testunit/LB-1",
       "GET /reservoirs/{id} finds the newly-created reservoir (%s)" % r_get.status_code)

    # ── 10. missing ANY of media/role/pg_concentration/volume_prepared ──
    # falls through to the existing, unchanged "nothing projected" message
    # -- never a partial/half-built reservoir ───────────────────────────────
    for missing in ("media", "role", "pg_concentration"):
        client, settings = make_client_with_settings()
        params = {
            "reservoir_id": "testunit/LB-9", "volume_prepared": {"value": 1.0, "unit": "L"},
            "media": "LB", "role": "low",
            "pg_concentration": {"value_g_per_L": 1.0, "value_mM": 7.9296, "unit_primary": "g/L"},
        }
        del params[missing]
        r = client.post("/events", json={
            "target": {"scope": "facility"}, "timestamp": "2026-01-05T09:00:00-05:00",
            "event_type": "media_prep", "provenance": "reported", "params": params,
            "notes": "incomplete new-reservoir attempt",
        })
        ck(r.status_code == 201, "still appended even though %s is missing (201)" % missing)
        proj = r.json()["reservoir_projection"]
        ck(proj["projected"] is False and "created" not in proj,
           "missing %s -- NOT created, same message as any other unknown reservoir_id (%s)" % (missing, proj))
        on_disk = json.loads(settings.log_file.read_text())
        ck(all(r["id"] != "testunit/LB-9" for r in on_disk["reservoirs"]["items"]),
           "missing %s -- no partial reservoir was ever added" % missing)

    # ── 11. an unknown unit prefix in the reservoir_id is refused (422),
    # not silently creating a reservoir for a unit that doesn't exist ──────
    client, settings = make_client_with_settings()
    before_disk = settings.log_file.read_text()
    r = client.post("/events", json={
        "target": {"scope": "facility"}, "timestamp": "2026-01-05T09:00:00-05:00",
        "event_type": "media_prep", "provenance": "reported",
        "params": {
            "reservoir_id": "bogusunit/LB-1", "volume_prepared": {"value": 1.0, "unit": "L"},
            "media": "LB", "role": "low",
            "pg_concentration": {"value_g_per_L": 1.0, "value_mM": 7.9296, "unit_primary": "g/L"},
        },
        "notes": "unknown unit prefix",
    })
    ck(r.status_code == 422, "a reservoir_id prefixed with an unknown unit is refused (%s)" % r.status_code)
    ck(any("not a known hardware unit" in p for p in r.json()["detail"]), "the 422 names why")
    ck(settings.log_file.read_text() == before_disk, "the rejected write touched nothing on disk")

    # ── 12. reactivate: real, confirmed gap found by direct operator
    # testing: a media_prep against an EXISTING, retired reservoir used to
    # move volume/pg/current_volume exactly as any other media_prep, but
    # silently leave status: "retired" unchanged -- the earlier "create a
    # brand-new reservoir" fix only ever fires when reservoir_id doesn't
    # exist AT ALL, which this is not. reactivate: true fixes the actual
    # reported scenario. ─────────────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r_retire = client.post("/events", json={
        "target": {"scope": "facility"}, "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "reservoir_retired", "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0"}, "notes": "retire it",
    })
    ck(r_retire.status_code == 201, "retiring testunit/LB-0 for the reactivate regression setup succeeds")

    # 12a. media_prep against the retired reservoir, WITHOUT reactivate --
    # volume/pg still move (the physical fact is never withheld), status
    # stays retired, and the response says so explicitly.
    r = client.post("/events", json={
        "target": {"scope": "facility"}, "timestamp": "2026-01-03T09:00:00-05:00",
        "event_type": "media_prep", "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "volume_prepared": {"value": 1.0, "unit": "L"}},
        "notes": "prep fresh media into the retired reservoir, no reactivate flag",
    })
    ck(r.status_code == 201, "media_prep against a retired reservoir still succeeds (201)")
    proj = r.json()["reservoir_projection"]
    ck("status" not in proj["touched"], "status is NOT touched without reactivate: true")
    ck("resubmit with params.reactivate" in proj.get("note", ""),
       "the response explicitly says status was left retired and how to fix it (%s)" % proj.get("note"))
    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "testunit/LB-0")
    ck(res["status"] == "retired", "status genuinely stays retired")
    ck(res["current_volume"] == {"value": 1.0, "unit": "L"}, "but the fresh volume WAS recorded -- never withheld")

    # 12b. the SAME media_prep, this time WITH reactivate: true -- brings
    # it back into active service in the same write.
    r2 = client.post("/events", json={
        "target": {"scope": "facility"}, "timestamp": "2026-01-04T09:00:00-05:00",
        "event_type": "media_prep", "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "volume_prepared": {"value": 1.0, "unit": "L"},
                   "reactivate": True},
        "notes": "prep fresh media AND bring it back online",
    })
    ck(r2.status_code == 201, "media_prep with reactivate: true succeeds (201)")
    proj2 = r2.json()["reservoir_projection"]
    ck("status" in proj2["touched"], "status IS reported as touched with reactivate: true")
    ck("note" not in proj2, "no note needed -- reactivate actually did something")
    on_disk = json.loads(settings.log_file.read_text())
    res = find_reservoir(on_disk, "testunit/LB-0")
    ck(res["status"] == "active", "status genuinely flips back to active")

    # 12c. reactivate: true against an ALREADY-active reservoir is a
    # harmless no-op, not an error -- mirrors hardware_swap's own vacate
    # precedent for confirming state that already holds.
    r3 = client.post("/events", json={
        "target": {"scope": "facility"}, "timestamp": "2026-01-05T09:00:00-05:00",
        "event_type": "media_prep", "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "volume_prepared": {"value": 1.0, "unit": "L"},
                   "reactivate": True},
        "notes": "reactivate an already-active reservoir",
    })
    ck(r3.status_code == 201, "reactivate: true on an already-active reservoir still succeeds (201)")
    proj3 = r3.json()["reservoir_projection"]
    ck("already active" in proj3.get("note", ""), "a harmless, explicit no-op note, not an error (%s)"
       % proj3.get("note"))

    # 12d. reactivate as a non-boolean is refused (422), same pattern as
    # hardware_swap's own vacate type check.
    client, settings = make_client_with_settings()
    before_disk = settings.log_file.read_text()
    r4 = client.post("/events", json={
        "target": {"scope": "facility"}, "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "media_prep", "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0", "volume_prepared": {"value": 1.0, "unit": "L"},
                   "reactivate": "yes"},
        "notes": "reactivate as a non-boolean string",
    })
    ck(r4.status_code == 422, "reactivate as a non-boolean string is refused, not treated as truthy (%s)"
       % r4.status_code)
    ck(settings.log_file.read_text() == before_disk, "the rejected write touched nothing on disk")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
