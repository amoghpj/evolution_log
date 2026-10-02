#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_line_lifecycle_projection.py

Regression coverage for the gap reported after ISSUE_001 shipped: a
termination event posted via POST /events (rather than through POST
/lines' own embedded terminations) left line.status/lineage.terminated_at/
terminated_by_event stale, and since GET /vials and every begin_mode's
destination check derive occupancy from status alone, the vial silently
stayed "occupied" forever. media_switch (current_media) and the two
reservoir-status event types (reservoir_retired, reservoir_change) share the
same shape and are covered here too.
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


def termination_body(**overrides):
    body = {
        "target": {"line_id": "testunit-v01"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "termination",
        "provenance": "reported",
        "params": {},
        "notes": "line ended",
    }
    body.update(overrides)
    return body


def main():
    # ── 1. termination via POST /events flips status/terminated_at/
    # terminated_by_event, and frees the vial (GET /vials derives occupancy
    # from status alone) ─────────────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json=termination_body())
    ck(r.status_code == 201, "termination write returns 201 (%s)" % r.status_code)
    body = r.json()
    lifecycle = body["line_lifecycle_projection"]
    ck(lifecycle == {"event_type": "termination", "applied": True},
       "line_lifecycle_projection reports applied=True (%s)" % lifecycle)

    on_disk = json.loads(settings.log_file.read_text())
    line = on_disk["lines"]["testunit-v01"]
    ck(line["status"] == "ended", "line.status flipped to ended")
    ck(line["lineage"]["terminated_at"] == "2026-01-02T09:00:00-05:00", "lineage.terminated_at set to the event's timestamp")
    ck(line["lineage"]["terminated_by_event"] == body["event_id"], "lineage.terminated_by_event names the termination event")

    r_vial = client.get("/vials/testunit/1")
    ck(r_vial.json()["occupied_by"] is None, "GET /vials/testunit/1 now reports nothing occupying it")

    # the practical payoff: a restart into that now-vacated vial succeeds,
    # where it would have 409'd before this fix (destination checks derive
    # "occupied" from status too)
    r_restart = client.post("/lines", json={
        "begin_mode": "restart",
        "new_line": {
            "unit": "testunit", "vial": 1, "strain": "test strain",
            "initial_media": "LB", "current_media": "LB", "mode": "constant",
            "t0": "2026-01-03T09:00:00-05:00",
            "pg_regime": {
                "low": {"value_g_per_L": 0.5, "value_mM": 3.9648, "unit_primary": "g/L"},
                "high": {"value_g_per_L": 5.0, "value_mM": 39.6479, "unit_primary": "g/L"},
                "effective_from": "2026-01-03T09:00:00-05:00",
            },
            "reservoirs": {"low": "testunit/LB-0", "high": "testunit/LB-5"},
            "founding_event": {
                "timestamp": "2026-01-03T09:00:00-05:00", "event_type": "inoculation",
                "provenance": "reported", "params": {}, "notes": "restart after termination",
            },
        },
        "predecessor_line_id": "testunit-v01",
    })
    ck(r_restart.status_code == 201,
       "restarting into the vacated vial succeeds now (%s: %s)" % (r_restart.status_code, r_restart.text[:200]))

    # ── 2. a second, unrelated termination on an already-ended line is
    # REJECTED (409) -- found by simulating real operator use: this used to
    # be a soft accept, and two independent simulated operators both hit
    # variants of "the API silently accepts a write that contradicts
    # recorded state" ────────────────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r1 = client.post("/events", json=termination_body())
    first_id = r1.json()["event_id"]
    before = settings.log_file.read_text()
    r2 = client.post("/events", json=termination_body(
        timestamp="2026-01-03T09:00:00-05:00", notes="a second, unrelated termination"))
    ck(r2.status_code == 409, "a second, unlinked termination on an already-ended line is refused (%s)" % r2.status_code)
    ck("already ended" in r2.json()["detail"], "the 409 names why: the line is already ended")
    ck("supersedes" in r2.json()["detail"], "the 409 points the caller at supersedes instead")

    after = settings.log_file.read_text()
    ck(after == before, "the rejected write touched nothing on disk -- not even the event was appended")
    on_disk = json.loads(before)
    ck(on_disk["lines"]["testunit-v01"]["lineage"]["terminated_by_event"] == first_id,
       "terminated_by_event still names the FIRST (only) termination")

    # ── 3. supersedes-ing the existing termination DOES move terminated_at/
    # terminated_by_event -- a deliberate correction, not an unrelated one ───
    client, settings = make_client_with_settings()
    r_first = client.post("/events", json=termination_body())
    first_id = r_first.json()["event_id"]
    r_correction = client.post("/events", json=termination_body(
        timestamp="2026-01-02T10:00:00-05:00",
        notes="correcting the termination reason", supersedes=first_id,
    ))
    ck(r_correction.status_code == 201, "a superseding termination is appended (201)")
    lifecycle3 = r_correction.json()["line_lifecycle_projection"]
    ck(lifecycle3["applied"] is True, "a superseding termination reports applied=True")
    on_disk = json.loads(settings.log_file.read_text())
    line = on_disk["lines"]["testunit-v01"]
    ck(line["lineage"]["terminated_by_event"] == r_correction.json()["event_id"],
       "terminated_by_event moved to the superseding event")
    ck(line["lineage"]["terminated_at"] == "2026-01-02T10:00:00-05:00",
       "terminated_at moved to the superseding event's timestamp")

    # ── 4. a facility-scoped termination/media_switch names no line -- recorded, not applied ─
    client, settings = make_client_with_settings()
    r = client.post("/events", json=termination_body(target={"scope": "facility"}))
    ck(r.status_code == 201, "a facility-scoped termination is still appended (201)")
    ck(r.json()["line_lifecycle_projection"]["applied"] is False,
       "a facility-scoped termination reports applied=False")
    ck("must be line-scoped" in r.json()["line_lifecycle_projection"]["reason"],
       "the reason names why: termination must be line-scoped")

    # ── 5. media_switch moves current_media ──────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json={
        "target": {"line_id": "testunit-v01"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "media_switch",
        "provenance": "reported",
        "params": {"media_from": "LB", "media_to": "M9"},
        "notes": "switched to M9",
    })
    ck(r.status_code == 201, "media_switch write returns 201 (%s)" % r.status_code)
    lifecycle = r.json()["line_lifecycle_projection"]
    ck(lifecycle["applied"] is True, "media_switch reports applied=True")
    ck("warning" not in lifecycle, "no warning when media_from matches the line's prior current_media")
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["current_media"] == "M9", "current_media moved to media_to")

    # ── 6. media_switch with a media_from that doesn't match is REJECTED
    # (409), not silently applied with a buried warning -- found by
    # simulating real operator use ───────────────────────────────────────────
    client, settings = make_client_with_settings()
    before = settings.log_file.read_text()
    r = client.post("/events", json={
        "target": {"line_id": "testunit-v01"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "media_switch",
        "provenance": "reported",
        "params": {"media_from": "M9", "media_to": "M9+PG"},  # line's actual current_media is LB
        "notes": "switch with a stale media_from",
    })
    ck(r.status_code == 409, "media_switch with mismatched media_from is refused (%s)" % r.status_code)
    ck("current_media" in r.json()["detail"], "the 409 names the actual current_media")
    after = settings.log_file.read_text()
    ck(after == before, "the rejected write touched nothing on disk")

    # ── 7. media_switch missing media_to never invents a value ──────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json={
        "target": {"line_id": "testunit-v01"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "media_switch",
        "provenance": "reported",
        "params": {"media_from": "LB"},
        "notes": "malformed media_switch, no media_to",
    })
    ck(r.status_code == 201, "media_switch missing media_to is still appended (201)")
    lifecycle = r.json()["line_lifecycle_projection"]
    ck(lifecycle["applied"] is False, "reports applied=False when media_to is missing")
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["current_media"] == "LB", "current_media was NOT invented/changed")

    # ── 8. media_switch on a CONSTANT-mode line is REJECTED (409) -- its own
    # registry description is explicit: "for a switch-mode line." Found by
    # simulating a real operator applying it to the wrong line by mistake ──
    client, settings = make_client_with_settings()
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v03"]["mode"] == "constant",
       "sanity check: testunit-v03 really is constant-mode")
    before = settings.log_file.read_text()
    r = client.post("/events", json={
        "target": {"line_id": "testunit-v03"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "media_switch",
        "provenance": "reported",
        "params": {"media_from": "LB", "media_to": "M9"},
        "notes": "media_switch applied to a constant-mode line by mistake",
    })
    ck(r.status_code == 409, "media_switch on a constant-mode line is refused (%s)" % r.status_code)
    ck("mode" in r.json()["detail"], "the 409 names why: the line's mode isn't 'switch'")
    after = settings.log_file.read_text()
    ck(after == before, "the rejected write touched nothing on disk")

    # ── 9. reservoir_retired flips that reservoir's status ──────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "reservoir_retired",
        "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0"},
        "notes": "retiring the low reservoir",
    })
    ck(r.status_code == 201, "reservoir_retired write returns 201 (%s)" % r.status_code)
    proj = r.json()["reservoir_projection"]
    ck(proj == {"event_type": "reservoir_retired", "reservoir_id": "testunit/LB-0",
                "projected": True, "touched": ["status"]},
       "reservoir_retired's reservoir_projection reports exactly the status column touched (%s)" % proj)
    on_disk = json.loads(settings.log_file.read_text())
    res = next(x for x in on_disk["reservoirs"]["items"] if x["id"] == "testunit/LB-0")
    ck(res["status"] == "retired", "the reservoir's status is now retired")

    # retiring an already-retired reservoir is a no-op, reported as such
    r2 = client.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": "2026-01-03T09:00:00-05:00",
        "event_type": "reservoir_retired",
        "provenance": "reported",
        "params": {"reservoir_id": "testunit/LB-0"},
        "notes": "retiring it again by mistake",
    })
    ck(r2.status_code == 201, "a second reservoir_retired is still appended (201)")
    ck(r2.json()["reservoir_projection"]["projected"] is False,
       "retiring an already-retired reservoir reports projected=False")
    ck("already retired" in r2.json()["reservoir_projection"]["reason"],
       "the reason names why: already retired")

    # ── 10. reservoir_change is explicitly NOT auto-projected -- too
    # structurally different (multi-position, arrays) to guess safely ───────
    client, settings = make_client_with_settings()
    r = client.post("/events", json={
        "target": {"scope": "facility"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "reservoir_change",
        "provenance": "reported",
        "params": {"reservoir_id_from": "testunit/LB-0", "reservoir_id_to": "testunit/LB-0.5"},
        "notes": "facility-level reservoir change",
    })
    ck(r.status_code == 201, "reservoir_change write returns 201 (%s)" % r.status_code)
    proj = r.json()["reservoir_projection"]
    ck(proj["projected"] is False, "reservoir_change reports projected=False")
    ck("not auto-projected" in proj["reason"], "the reason explains why -- multi-position, not guessed at")

    # ── 11. hardware_swap is recognized but explicitly NOT auto-projected --
    # its real parameter_registry entry has no new_unit/new_vial field at
    # all (only free-text what_moved/reason), so line.unit/vial are left
    # alone rather than guessed at. Found by simulating a real hardware-
    # fault operator: this used to silently return line_lifecycle_
    # projection: null, with zero acknowledgment that GET /vials is now
    # wrong for both the old and new position ──────────────────────────────
    client, settings = make_client_with_settings()
    before = json.loads(settings.log_file.read_text())
    r = client.post("/events", json={
        "target": {"line_id": "testunit-v01"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "hardware_swap",
        "provenance": "reported",
        "params": {"what_moved": "testunit-v01's culture moved to a different physical body"},
        "notes": "stir-bar fault, vessel moved intact",
    })
    ck(r.status_code == 201, "hardware_swap write returns 201 (%s)" % r.status_code)
    lifecycle = r.json()["line_lifecycle_projection"]
    ck(lifecycle == {"event_type": "hardware_swap", "applied": False,
                      "reason": "hardware_swap has no structured destination fields in the parameter_registry "
                                "(only free-text what_moved/reason) -- line.unit/vial are NOT updated by this "
                                "event; GET /vials will report the old position as still occupied and the new "
                                "one as still empty until a human updates line.unit/vial by hand"},
       "hardware_swap reports an explicit, non-silent non-projection (%s)" % lifecycle)
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["unit"] == before["lines"]["testunit-v01"]["unit"]
       and on_disk["lines"]["testunit-v01"]["vial"] == before["lines"]["testunit-v01"]["vial"],
       "line.unit/vial are genuinely untouched, matching what the report says")

    # ── 12. ISSUE_002: hardware_swap WITH new_unit/new_vial relocates the ══
    # ══ line -- same-unit case ══════════════════════════════════════════════
    def hardware_swap_body(line_id="testunit-v01", **param_overrides):
        params = {"what_moved": "culture moved intact"}
        params.update(param_overrides)
        return {
            "target": {"line_id": line_id},
            "timestamp": "2026-01-02T09:00:00-05:00",
            "event_type": "hardware_swap",
            "provenance": "reported",
            "params": params,
            "notes": "ISSUE_002 relocation test",
        }

    client, settings = make_client_with_settings()
    r = client.post("/events", json=hardware_swap_body(new_unit="testunit", new_vial=2))
    ck(r.status_code == 201, "same-unit relocation write returns 201 (%s / %s)" % (r.status_code, r.text))
    lifecycle = r.json()["line_lifecycle_projection"]
    ck(lifecycle == {"event_type": "hardware_swap", "applied": True, "touched": ["unit", "vial"]},
       "same-unit relocation reports applied=True, touched=[unit, vial] (%s)" % lifecycle)
    on_disk = json.loads(settings.log_file.read_text())
    line = on_disk["lines"]["testunit-v01"]
    ck(line["unit"] == "testunit" and line["vial"] == 2, "line.unit/vial actually moved to (testunit, 2)")
    ck(line["line_id"] == "testunit-v01", "line_id itself is UNTOUCHED -- the whole point of ISSUE_002")

    # old position freed, new position occupied
    ck(client.get("/vials/testunit/1").json()["occupied_by"] is None,
       "the OLD position (testunit, 1) now reports unoccupied")
    ck(client.get("/vials/testunit/2").json()["occupied_by"] == "testunit-v01",
       "the NEW position (testunit, 2) now reports testunit-v01 occupying it")

    # ── 13. ISSUE_002: cross-unit relocation ════════════════════════════════
    client, settings = make_client_with_settings()
    on_disk = json.loads(settings.log_file.read_text())
    on_disk["hardware"]["units"]["otherunit"] = {"mode": "constant", "vials_in_use": [], "n_lines": 0}
    settings.log_file.write_text(json.dumps(on_disk, indent=2))

    r = client.post("/events", json=hardware_swap_body(new_unit="otherunit", new_vial=9))
    ck(r.status_code == 201, "cross-unit relocation write returns 201 (%s / %s)" % (r.status_code, r.text))
    lifecycle = r.json()["line_lifecycle_projection"]
    ck(lifecycle["applied"] is True, "cross-unit relocation reports applied=True (%s)" % lifecycle)
    on_disk = json.loads(settings.log_file.read_text())
    line = on_disk["lines"]["testunit-v01"]
    ck(line["unit"] == "otherunit" and line["vial"] == 9, "line.unit/vial moved across units to (otherunit, 9)")
    ck(line["line_id"] == "testunit-v01",
       "line_id is STILL untouched, even though it now visibly mismatches line.unit -- the accepted, "
       "documented tradeoff (ISSUE_002)")
    ck(client.get("/vials/otherunit/9").json()["occupied_by"] == "testunit-v01",
       "GET /vials on the new unit/vial correctly shows the relocated line")
    ck(client.get("/vials/testunit/1").json()["occupied_by"] is None,
       "GET /vials on the old unit/vial correctly shows nothing there anymore")

    # ── 14. destination already occupied by a DIFFERENT active line -- ═════
    # ══ rejected, nothing moves (testunit-v03 is active at vial 3) ═════════
    client, settings = make_client_with_settings()
    before_disk = settings.log_file.read_text()
    r = client.post("/events", json=hardware_swap_body(new_unit="testunit", new_vial=3))
    ck(r.status_code == 409, "relocating onto an already-occupied vial is refused (%s)" % r.status_code)
    ck("already occupied" in r.json()["detail"], "the 409 names why: already occupied")
    ck(settings.log_file.read_text() == before_disk, "the rejected write touched nothing on disk")

    # confirm relocating a line to its OWN current position is NOT treated
    # as a conflict with itself (exclude_line_id in assert_destination_empty)
    r = client.post("/events", json=hardware_swap_body(new_unit="testunit", new_vial=1))
    ck(r.status_code == 201,
       "relocating a line to the vial it ALREADY occupies is not a false self-conflict (%s / %s)"
       % (r.status_code, r.text))

    # ── 15. new_unit naming an unregistered hardware unit -- rejected ══════
    client, settings = make_client_with_settings()
    before_disk = settings.log_file.read_text()
    r = client.post("/events", json=hardware_swap_body(new_unit="not-a-real-unit", new_vial=5))
    ck(r.status_code == 422, "relocating onto an unknown hardware unit is refused (%s)" % r.status_code)
    ck(any("not a known hardware unit" in p for p in r.json()["detail"]), "the 422 names why")
    ck(settings.log_file.read_text() == before_disk, "the rejected write touched nothing on disk")

    # new_vial out of range / wrong type
    r = client.post("/events", json=hardware_swap_body(new_unit="testunit", new_vial=99))
    ck(r.status_code == 422, "new_vial out of the 0-15 range is refused (%s)" % r.status_code)
    r = client.post("/events", json=hardware_swap_body(new_unit="testunit", new_vial="2"))
    ck(r.status_code == 422, "new_vial as a non-integer is refused (%s)" % r.status_code)

    # new_unit as a non-string -- unlike branch/split/restart's own
    # destinations (Pydantic-typed `unit: str`, so a non-string never
    # reaches assert_known_unit), hardware_swap's new_unit arrives through
    # the untyped params dict; assert_known_unit's `unit not in known`
    # requires a hashable value, so an unhashable one used to crash with an
    # unhandled TypeError instead of a clean 422 -- found by verifying this
    # implementation rather than taking it on faith.
    r = client.post("/events", json=hardware_swap_body(new_unit=["not", "a", "string"], new_vial=2))
    ck(r.status_code == 422, "new_unit as a non-string (unhashable) is refused, not an unhandled crash (%s)"
       % r.status_code)

    # ── 16. previous_unit/previous_vial mismatch -- rejected (409), mirroring ═
    # ══ media_switch's own media_from precedent exactly ════════════════════
    client, settings = make_client_with_settings()
    before_disk = settings.log_file.read_text()
    r = client.post("/events", json=hardware_swap_body(
        new_unit="testunit", new_vial=2, previous_unit="testunit", previous_vial=99))
    ck(r.status_code == 409, "a previous_vial that doesn't match the line's real current vial is refused (%s)"
       % r.status_code)
    ck("vial" in r.json()["detail"], "the 409 names the actual current vial")
    ck(settings.log_file.read_text() == before_disk, "the rejected write touched nothing on disk")

    r = client.post("/events", json=hardware_swap_body(
        new_unit="testunit", new_vial=2, previous_unit="wrong-unit", previous_vial=1))
    ck(r.status_code == 409, "a previous_unit that doesn't match the line's real current unit is refused (%s)"
       % r.status_code)

    # regression, found by round-1 adversarial testing of the vacate
    # follow-up: previous_vial=True or previous_vial=1.0 against a real
    # vial of 1 both pass Python's bare `!=` silently (True == 1, 1.0 == 1)
    # -- _check_previous_position (app/writer.py, shared by both the
    # relocate and vacate branches) now guards the TYPE explicitly, not
    # just the value, so a wrong-typed previous_vial that happens to be
    # numerically equal to the real one is still refused. testunit-v01 is
    # still at its real (testunit, 1) here -- nothing above has moved it
    # yet (both prior checks in this scenario were rejected).
    r = client.post("/events", json=hardware_swap_body(
        new_unit="testunit", new_vial=2, previous_unit="testunit", previous_vial=True))
    ck(r.status_code == 409, "previous_vial=True (bool, not int) against a real vial of 1 is refused, "
       "not silently accepted as equal (%s)" % r.status_code)
    r = client.post("/events", json=hardware_swap_body(
        new_unit="testunit", new_vial=2, previous_unit="testunit", previous_vial=1.0))
    ck(r.status_code == 409, "previous_vial=1.0 (float, not int) against a real vial of 1 is refused (%s)"
       % r.status_code)

    # a CORRECT previous_unit/previous_vial is accepted and applied normally
    r = client.post("/events", json=hardware_swap_body(
        new_unit="testunit", new_vial=2, previous_unit="testunit", previous_vial=1))
    ck(r.status_code == 201, "a correct previous_unit/previous_vial is accepted (%s / %s)"
       % (r.status_code, r.text))

    # ── 17. only ONE of new_unit/new_vial supplied -- treated as not a ══════
    # ══ relocation at all, same as neither being present ═══════════════════
    client, settings = make_client_with_settings()
    before_disk = settings.log_file.read_text()
    r = client.post("/events", json=hardware_swap_body(new_unit="testunit"))
    ck(r.status_code == 201, "only new_unit supplied -- still appended (201)")
    lifecycle = r.json()["line_lifecycle_projection"]
    ck(lifecycle["applied"] is False, "reports applied=False when only one of new_unit/new_vial is given")
    ck("BOTH" in lifecycle["reason"], "the reason explains both are needed together (%s)" % lifecycle["reason"])
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["unit"] == "testunit" and on_disk["lines"]["testunit-v01"]["vial"] == 1,
       "line.unit/vial genuinely untouched")

    # ── 18. ISSUE_002 follow-up: hardware_swap params.vacate: true removes
    # the line from the evolver entirely -- unit/vial both become null,
    # rather than moving to a different real position ══════════════════════
    client, settings = make_client_with_settings()
    r = client.post("/events", json=hardware_swap_body(vacate=True))
    ck(r.status_code == 201, "vacate write returns 201 (%s / %s)" % (r.status_code, r.text))
    lifecycle = r.json()["line_lifecycle_projection"]
    ck(lifecycle == {"event_type": "hardware_swap", "applied": True, "touched": ["unit", "vial"],
                      "reason": "line vacated -- unit/vial set to null, a real 'not currently on any "
                                "evolver' state, until a future hardware_swap relocates it"},
       "vacate reports applied=True, touched=[unit, vial], and why (%s)" % lifecycle)
    on_disk = json.loads(settings.log_file.read_text())
    line = on_disk["lines"]["testunit-v01"]
    ck(line["unit"] is None and line["vial"] is None, "line.unit/vial are both null after vacate")
    ck(line["line_id"] == "testunit-v01", "line_id itself is untouched by vacate, same as relocation")

    # the vacated vial reads unoccupied
    ck(client.get("/vials/testunit/1").json()["occupied_by"] is None,
       "the vacated position (testunit, 1) now reports unoccupied")

    # ── 19. re-vacating an already-off-evolver line is idempotent, not a
    # conflict -- same "confirming unchanged state" precedent media_switch's
    # own media_from match already sets ═════════════════════════════════════
    r2 = client.post("/events", json=hardware_swap_body(vacate=True))
    ck(r2.status_code == 201, "re-vacating an already-vacated line is still accepted (201) (%s)" % r2.text)
    lifecycle2 = r2.json()["line_lifecycle_projection"]
    ck(lifecycle2 == {"event_type": "hardware_swap", "applied": True, "touched": [],
                       "reason": "line was already off-evolver (unit/vial already null) -- this "
                                 "vacate re-confirms that, nothing changed"},
       "re-vacating reports applied=True, touched=[], and why nothing moved (%s)" % lifecycle2)

    # ── 20. vacate combined with new_unit/new_vial in the same event is
    # rejected (422) -- a malformed request, not a conflict with recorded
    # state, since the whole point is these are two separate events ════════
    client, settings = make_client_with_settings()
    before_disk = settings.log_file.read_text()
    r = client.post("/events", json=hardware_swap_body(vacate=True, new_unit="testunit", new_vial=2))
    ck(r.status_code == 422, "vacate combined with new_unit/new_vial is refused (%s)" % r.status_code)
    ck(any("cannot be combined" in p for p in r.json()["detail"]), "the 422 names why")
    ck(settings.log_file.read_text() == before_disk, "the rejected write touched nothing on disk")

    # vacate as a non-boolean value -- rejected, not silently truthy
    r = client.post("/events", json=hardware_swap_body(vacate="true"))
    ck(r.status_code == 422, "vacate as a non-boolean string is refused, not treated as truthy (%s)"
       % r.status_code)

    # ── 21. vacate with a mismatched previous_unit/previous_vial is refused
    # (409), same precedent as a relocation's own previous_unit/previous_vial ═
    client, settings = make_client_with_settings()
    before_disk = settings.log_file.read_text()
    r = client.post("/events", json=hardware_swap_body(vacate=True, previous_unit="testunit", previous_vial=99))
    ck(r.status_code == 409, "vacate with a wrong previous_vial is refused (%s)" % r.status_code)
    ck("vial" in r.json()["detail"], "the 409 names the actual current vial")
    ck(settings.log_file.read_text() == before_disk, "the rejected write touched nothing on disk")

    # a CORRECT previous_unit/previous_vial is accepted and vacate applies normally
    r = client.post("/events", json=hardware_swap_body(vacate=True, previous_unit="testunit", previous_vial=1))
    ck(r.status_code == 201, "vacate with a correct previous_unit/previous_vial is accepted (%s / %s)"
       % (r.status_code, r.text))


    # ── 22. relocating INTO a real position out of "not on evolver" works
    # exactly like any other relocation -- a null unit/vial never conflicts
    # with assert_destination_empty's real-occupant check ════════════════════
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["unit"] is None, "sanity check: testunit-v01 really is off-evolver now")
    r = client.post("/events", json=hardware_swap_body(new_unit="testunit", new_vial=8))
    ck(r.status_code == 201, "relocating an off-evolver line into a real, empty vial succeeds (%s / %s)"
       % (r.status_code, r.text))
    on_disk = json.loads(settings.log_file.read_text())
    line = on_disk["lines"]["testunit-v01"]
    ck(line["unit"] == "testunit" and line["vial"] == 8, "line.unit/vial moved from null to the real destination")

    # ── 23. the actual reported scenario (EVT-00488): two lines trading
    # physical positions reciprocally. Relocating either one directly onto
    # the other's CURRENT position 409s (each destination is still occupied
    # by the line that hasn't moved yet) -- vacating both first, then
    # relocating both, has no such moment where any destination is occupied ─
    client, settings = make_client_with_settings()
    # testunit-v01 is at (testunit, 1), testunit-v03 is at (testunit, 3);
    # trade positions: v01 -> 3, v03 -> 1.
    r_direct = client.post("/events", json=hardware_swap_body(
        line_id="testunit-v01", new_unit="testunit", new_vial=3))
    ck(r_direct.status_code == 409,
       "confirms the reported problem: relocating v01 directly onto v03's still-occupied vial 409s (%s)"
       % r_direct.status_code)

    r_v1 = client.post("/events", json=hardware_swap_body(line_id="testunit-v01", vacate=True))
    r_v3 = client.post("/events", json=hardware_swap_body(line_id="testunit-v03", vacate=True))
    ck(r_v1.status_code == 201 and r_v3.status_code == 201, "both lines vacate successfully (%s, %s)"
       % (r_v1.status_code, r_v3.status_code))

    r_v1_in = client.post("/events", json=hardware_swap_body(line_id="testunit-v01", new_unit="testunit", new_vial=3))
    r_v3_in = client.post("/events", json=hardware_swap_body(line_id="testunit-v03", new_unit="testunit", new_vial=1))
    ck(r_v1_in.status_code == 201 and r_v3_in.status_code == 201,
       "once both are vacated, each relocates into the OTHER's old position with no destination "
       "ever occupied at write time (%s, %s)" % (r_v1_in.status_code, r_v3_in.status_code))

    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["unit"] == "testunit" and on_disk["lines"]["testunit-v01"]["vial"] == 3,
       "testunit-v01 ends at (testunit, 3), v03's old position")
    ck(on_disk["lines"]["testunit-v03"]["unit"] == "testunit" and on_disk["lines"]["testunit-v03"]["vial"] == 1,
       "testunit-v03 ends at (testunit, 1), v01's old position -- the reciprocal swap fully resolved")
    ck(client.get("/vials/testunit/1").json()["occupied_by"] == "testunit-v03",
       "GET /vials/testunit/1 now shows testunit-v03")
    ck(client.get("/vials/testunit/3").json()["occupied_by"] == "testunit-v01",
       "GET /vials/testunit/3 now shows testunit-v01")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
