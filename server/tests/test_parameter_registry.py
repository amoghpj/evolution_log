#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_parameter_registry.py

Covers the parameter_registry-registration feature added 2026-09-01:
GET /parameter_registry, POST /parameter_registry/candidate, and
POST /parameter_registry -- replacing direct hand-editing of
evolution_log.json's parameter_registry (the operator's own words: "I
don't think it is great to be directly manipulating the data like this")
with a validate-before-write pipeline reusing app/writer.py's existing
schema + tools/lineage.py machinery, the same as POST /events does.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_real_auth, make_client_with_settings  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def planned_entry(**overrides):
    entry = {"description": "a brand-new test dimension", "status": "planned",
              "type": "string", "first_seen": None}
    entry.update(overrides)
    return entry


def main():
    # ══ GET /parameter_registry ═════════════════════════════════════════
    client, settings = make_client_with_settings()

    r = client.get("/parameter_registry")
    ck(r.status_code == 200, "GET /parameter_registry -> 200 (%s)" % r.status_code)
    ck("reservoir_id" in r.json()["parameter_registry"],
       "the fixture's real registry entries are present (%s)" % list(r.json()["parameter_registry"])[:5])

    r = client.get("/parameter_registry", params={"key": "reservoir_id"})
    ck(r.status_code == 200, "GET /parameter_registry?key=reservoir_id -> 200 (%s)" % r.status_code)
    ck(list(r.json()["parameter_registry"].keys()) == ["reservoir_id"],
       "filtering by key returns exactly that one entry (%s)" % r.json())

    r = client.get("/parameter_registry", params={"key": "no-such-key"})
    ck(r.status_code == 404, "an unknown key -> 404, not an empty 200 (%s)" % r.status_code)

    # ══ POST /parameter_registry/candidate: structural checks ══════════════
    r = client.post("/parameter_registry/candidate", json={"key": "new_test_dimension", "entry": planned_entry()})
    ck(r.status_code == 200 and r.json()["valid"] is True,
       "a well-formed brand-new planned entry validates (%s)" % r.json())

    r = client.post("/parameter_registry/candidate", json={"key": "reservoir_id", "entry": planned_entry()})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "registering an ALREADY-registered key is rejected (%s)" % r.json())
    ck(any("already has an entry" in p for p in r.json()["problems"]), "the problem explains why")

    r = client.post("/parameter_registry/candidate",
                     json={"key": "new_test_dimension", "entry": planned_entry(status="retired")})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "status: retired is refused for a brand-new key (%s)" % r.json())
    ck(any("retired" in p for p in r.json()["problems"]), "the problem explains retired isn't a creation state")

    r = client.post("/parameter_registry/candidate",
                     json={"key": "new_test_dimension", "entry": planned_entry(first_seen="EVT-00004")})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "status: planned with a non-null first_seen is rejected (%s)" % r.json())

    r = client.post("/parameter_registry/candidate",
                     json={"key": "new_test_dimension", "entry": planned_entry(status="active", first_seen=None)})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "status: active with no first_seen is rejected (%s)" % r.json())

    r = client.post("/parameter_registry/candidate",
                     json={"key": "new_test_dimension",
                           "entry": planned_entry(status="active", first_seen="EVT-NONEXISTENT")})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "status: active referencing a NONEXISTENT event is rejected (%s)" % r.json())
    ck(any("does not name an existing event" in p for p in r.json()["problems"]), "the problem says so")

    r = client.post("/parameter_registry/candidate",
                     json={"key": "new_test_dimension",
                           "entry": planned_entry(status="active", first_seen="EVT-00004")})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "status: active referencing a REAL event that doesn't actually use this key is rejected (%s)"
       % r.json())
    ck(any("don't actually contain" in p for p in r.json()["problems"]), "the problem says so")

    # missing required schema fields (description/type) -- caught by the
    # SAME schema check the real write is judged against, not a hand-rolled
    # duplicate of it
    r = client.post("/parameter_registry/candidate", json={"key": "new_test_dimension", "entry": {"status": "planned"}})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "a registryEntry missing description/type is rejected by schema validation (%s)" % r.json())

    r = client.post("/parameter_registry/candidate",
                     json={"key": "new_test_dimension",
                           "entry": planned_entry(type="not-a-real-type")})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "an unrecognized type value is rejected by schema validation (%s)" % r.json())

    # ══ status: active referencing a REAL event that DOES use the key ══════
    # (simulates a human hand-editing an event with a brand-new key BEFORE
    # registering it -- exactly the scenario GET /skill's new section
    # describes as the realistic path to an active-at-creation entry)
    on_disk = json.loads(settings.log_file.read_text())
    on_disk["experiment_events"].append({
        "event_id": "EVT-00099", "timestamp": "2026-01-05T09:00:00-05:00", "event_type": "note",
        "operator": "TEST", "provenance": "reported", "scope": "facility",
        "params": {"new_active_key": "a value only this event has"},
        "notes": "simulates a hand-edited event using a brand-new, not-yet-registered key",
        "missing_fields": [],
    })
    on_disk["log_meta"]["event_counter"] += 1  # keep the injected event internally consistent
    settings.log_file.write_text(json.dumps(on_disk, indent=2))

    r = client.post("/parameter_registry/candidate",
                     json={"key": "new_active_key",
                           "entry": {"description": "a key a hand-edited event already uses",
                                     "status": "active", "type": "string", "first_seen": "EVT-00099"}})
    ck(r.status_code == 200 and r.json()["valid"] is True,
       "status: active referencing a real event that DOES use the key validates (%s)" % r.json())

    # ══ POST /parameter_registry: the real write ════════════════════════════
    client3, token3, operator3 = make_client_with_real_auth()

    r = client3.post("/parameter_registry", json={"key": "new_test_dimension", "entry": planned_entry()})
    ck(r.status_code == 401, "POST /parameter_registry with no bearer token -> 401 (%s)" % r.status_code)
    health_before = client3.get("/health").json()["log_meta"]
    head_before = subprocess.run(
        ["git", "-C", str(client3.get("/health").json()["log_repo_path"]), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()

    r = client3.post("/parameter_registry", json={"key": "new_test_dimension", "entry": planned_entry()},
                      headers={"Authorization": "Bearer %s" % token3})
    ck(r.status_code == 201, "a well-formed registration -> 201 (%s / %s)" % (r.status_code, r.text))
    ck(r.json()["key"] == "new_test_dimension", "response echoes the key")
    ck("inert" in r.json().get("reminder", "").lower(),
       "response reminds the caller a planned entry stays inert until used (%s)" % r.json().get("reminder"))

    r = client3.get("/parameter_registry", params={"key": "new_test_dimension"})
    ck(r.status_code == 200 and r.json()["parameter_registry"]["new_test_dimension"]["status"] == "planned",
       "the new entry is actually readable afterward (%s)" % r.json())

    r = client3.get("/parameter_registry", params={"key": "reservoir_id"})
    ck(r.json()["parameter_registry"]["reservoir_id"]["description"] == "the reservoir this event concerns",
       "an EXISTING entry survives the write completely unchanged (%s)" % r.json())

    head_after = subprocess.run(
        ["git", "-C", str(client3.get("/health").json()["log_repo_path"]), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    ck(head_after != head_before, "a real git commit landed in the SAME repo POST /events writes to")

    health_after = client3.get("/health").json()["log_meta"]
    ck(health_after["event_counter"] == health_before["event_counter"],
       "log_meta.event_counter is untouched -- a registry addition is not an event (%s vs %s)"
       % (health_before["event_counter"], health_after["event_counter"]))
    ck(health_after["last_updated"] == health_before["last_updated"],
       "log_meta.last_updated is untouched too (%s vs %s)"
       % (health_before["last_updated"], health_after["last_updated"]))

    # ══ duplicate-key conflict on the real write -> 409 ═════════════════════
    r = client3.post("/parameter_registry", json={"key": "reservoir_id", "entry": planned_entry()},
                      headers={"Authorization": "Bearer %s" % token3})
    ck(r.status_code == 409, "registering an already-taken key for real -> 409, not 422 (%s)" % r.status_code)

    # ══ validation failure on the real write -> 422, nothing written ═══════
    r = client3.post("/parameter_registry",
                      json={"key": "another_new_key", "entry": planned_entry(status="retired")},
                      headers={"Authorization": "Bearer %s" % token3})
    ck(r.status_code == 422, "an invalid registration for real -> 422 (%s)" % r.status_code)
    r = client3.get("/parameter_registry", params={"key": "another_new_key"})
    ck(r.status_code == 404, "the rejected key was never actually written (%s)" % r.status_code)

    # ══ GET /skill documents the new routes/section ════════════════════════
    r = client.get("/skill")
    ck("POST /parameter_registry" in r.text and "Registering a new" in r.text,
       "GET /skill documents the new parameter_registry routes, not just the code")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
