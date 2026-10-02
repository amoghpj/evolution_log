#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_write_lines.py

Covers all four ways a line can begin (LOG_PROTOCOL.md §5) against the
fixture's three lines: testunit-v01 (active, vial 1), testunit-v02 (ended,
vial 2), testunit-v03 (active, vial 3).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_settings  # noqa: E402
from app import lines_writer  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def pg_regime(low=0.5, high=5.0):
    def c(g):
        return {"value_g_per_L": g, "value_mM": round(g / 126.11 * 1000, 4), "unit_primary": "g/L"}
    return {"low": c(low), "high": c(high), "effective_from": "2026-01-10T09:00:00-05:00"}


def new_line_spec(unit="testunit", vial=10, event_type="inoculation", **overrides):
    spec = {
        "unit": unit,
        "vial": vial,
        "strain": "test strain",
        "initial_media": "LB",
        "current_media": "LB",
        "mode": "constant",
        "t0": "2026-01-10T09:00:00-05:00",
        "pg_regime": pg_regime(),
        "reservoirs": {"low": "testunit/LB-0", "high": "testunit/LB-5"},
        "founding_event": {
            "timestamp": "2026-01-10T09:00:00-05:00",
            "event_type": event_type,
            "provenance": "reported",
            "params": {},
            "notes": "test founding event",
        },
    }
    spec.update(overrides)
    return spec


def termination_event(timestamp="2026-01-10T09:00:00-05:00", **overrides):
    ev = {"timestamp": timestamp, "provenance": "reported", "params": {}, "notes": "test termination event"}
    ev.update(overrides)
    return ev


def hardware_swap_event(line_id, timestamp="2026-01-10T08:00:00-05:00", **param_overrides):
    params = {"what_moved": "test relocation"}
    params.update(param_overrides)
    return {
        "target": {"line_id": line_id}, "timestamp": timestamp, "event_type": "hardware_swap",
        "provenance": "reported", "params": params, "notes": "test hardware_swap",
    }


def main():
    # ── BRANCH ─────────────────────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "branch",
        "parent_line_id": "testunit-v01",
        "new_line": new_line_spec(vial=10),
    })
    ck(r.status_code == 201, "branch into a fresh vial returns 201 (%s: %s)" % (r.status_code, r.text[:300]))
    body = r.json()
    ck(body["new_line_ids"] == ["testunit-v10"], "fresh vial 10 -> bare id testunit-v10")
    new_line = body["lines"]["testunit-v10"]
    ck(new_line["lineage"]["parents"] == ["testunit-v01"], "child's parent is the branch source")
    ck(new_line["lineage"]["is_founder"] is False, "a branch child is not a founder")
    ck("occupies_vial_of" not in new_line["lineage"], "a never-used vial has no occupies_vial_of")
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["status"] == "active", "branch parent stays active")
    ck(len(on_disk["lines"]["testunit-v01"]["events"]) == 1, "branch parent gets no new event")

    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v01",
        "new_line": new_line_spec(vial=2),  # testunit-v02's old (now-empty) vial
    })
    ck(r.status_code == 201, "branch into a vial vacated by an ended line returns 201")
    body = r.json()
    ck(body["new_line_ids"] == ["testunit-v02#2"], "reused vial 2 -> occupancy-suffixed id (%s)" % body["new_line_ids"])
    ck(body["lines"]["testunit-v02#2"]["lineage"]["occupies_vial_of"] == "testunit-v02",
       "occupies_vial_of names the vial's actual prior occupant")

    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v02",  # ended
        "new_line": new_line_spec(vial=10),
    })
    ck(r.status_code == 409, "branching from an ENDED parent -> 409 (parent must continue)")

    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v01",
        "new_line": new_line_spec(vial=1),  # testunit-v01's OWN, currently-active vial
    })
    ck(r.status_code == 409, "branching into a currently-occupied vial -> 409")

    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "does-not-exist",
        "new_line": new_line_spec(vial=10),
    })
    ck(r.status_code == 404, "branching from a nonexistent parent -> 404")

    # ── regressions found by simulating malformed/confused-input operators ──
    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v01",
        "new_line": new_line_spec(vial=10, unit="Testunit"),  # wrong case
    })
    ck(r.status_code == 422, "an unknown unit (wrong case) -> 422, not an incidental line_id-regex failure")
    ck("not a known hardware unit" in r.text, "the problem plainly names the unit as the issue")

    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v01",
        "new_line": new_line_spec(vial=10, pg_regime={
            "low": {"value_g_per_L": 10.0, "value_mM": 79.296, "unit_primary": "g/L"},
            "high": {"value_g_per_L": 5.0, "value_mM": 39.6479, "unit_primary": "g/L"},
            "effective_from": "2026-01-10T09:00:00-05:00",
        }),
    })
    ck(r.status_code == 422, "pg_regime.low > high (inverted) -> 422")
    ck("low" in r.text and "high" in r.text, "the problem names low/high specifically")

    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v01",
        "new_line": new_line_spec(vial=10, pg_regime={
            "low": {"value_g_per_L": 0.5, "value_mM": 3.9648, "unit_primary": "g/L"},
            "high": {"value_g_per_L": 5.0, "value_mM": 39.6479, "unit_primary": "g/L"},
            "effective_from": "2026-01-10T09:00:00-05:00",
            "totally_made_up_field": 123,  # extra, nested two levels deep
        }),
    })
    ck(r.status_code == 422, "an extra field nested inside pg_regime -> 422, not silently stripped")
    ck(any(e.get("type") == "extra_forbidden" for e in r.json()["detail"]),
       "the problem is specifically 'extra field not permitted'")

    # ── RESTART ────────────────────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "restart", "predecessor_line_id": "testunit-v02",  # already ended
        "new_line": new_line_spec(vial=2),
    })
    ck(r.status_code == 201, "restart with an already-ended predecessor returns 201 (%s)" % r.text[:300])
    body = r.json()
    new_line = body["lines"][body["new_line_ids"][0]]
    ck(new_line["lineage"]["is_founder"] is True, "a restart is a founder")
    ck(new_line["lineage"]["parents"] == [], "a restart has zero parents -- not descent")
    ck(new_line["lineage"]["occupies_vial_of"] == "testunit-v02", "occupies_vial_of names the predecessor")
    ck(new_line["events"][0]["params"].get("predecessor_in_vial") == "testunit-v02",
       "founding event records predecessor_in_vial (matches real precedent)")
    ck(body["terminated_line_ids"] == [], "predecessor already ended -- nothing NEW terminated by this call")

    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "restart", "predecessor_line_id": "testunit-v01",  # still active
        "new_line": new_line_spec(vial=1),
    })
    ck(r.status_code == 422, "restart onto a still-active predecessor with no termination -> 422")

    r = client.post("/lines", json={
        "begin_mode": "restart", "predecessor_line_id": "testunit-v01",
        "predecessor_termination": termination_event(),
        "new_line": new_line_spec(vial=1),
    })
    ck(r.status_code == 201, "restart with an embedded predecessor termination returns 201 (%s)" % r.text[:300])
    body = r.json()
    ck(body["terminated_line_ids"] == ["testunit-v01"], "the embedded termination is reported")
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["status"] == "ended", "predecessor is now ended on disk")
    ck(len(on_disk["lines"]["testunit-v01"]["events"]) == 2, "predecessor gained exactly one new event")
    # regression: this mode also calls build_event() twice per request
    # (predecessor termination + new founding event) -- must not collide.
    term_id = on_disk["lines"]["testunit-v01"]["lineage"]["terminated_by_event"]
    founding_id = body["lines"][body["new_line_ids"][0]]["events"][0]["event_id"]
    ck(term_id != founding_id,
       "predecessor termination and new founding event got DISTINCT event_ids (%s, %s)" % (term_id, founding_id))

    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "restart", "predecessor_line_id": "testunit-v02",  # already ended
        "predecessor_termination": termination_event(),  # supplied anyway -- should be rejected
        "new_line": new_line_spec(vial=2),
    })
    ck(r.status_code == 422, "predecessor_termination supplied for an already-ended predecessor -> 422")

    # a true day-one founder: no predecessor at all -- not a branch, split,
    # merge, or a restart-of-something; a brand new vial with no history.
    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "restart",  # predecessor_line_id omitted
        "new_line": new_line_spec(vial=13),
    })
    ck(r.status_code == 201, "restart with no predecessor at all (day-one founder) returns 201 (%s)" % r.text[:300])
    body = r.json()
    new_line = body["lines"][body["new_line_ids"][0]]
    ck(new_line["lineage"]["is_founder"] is True, "a day-one founder is a founder")
    ck("occupies_vial_of" not in new_line["lineage"], "no predecessor means no occupies_vial_of at all")
    ck("predecessor_in_vial" not in new_line["events"][0]["params"],
       "no predecessor_in_vial injected when there is no predecessor")

    r = client.post("/lines", json={
        "begin_mode": "restart",
        "predecessor_termination": termination_event(),  # given without predecessor_line_id
        "new_line": new_line_spec(vial=14),
    })
    ck(r.status_code == 422, "predecessor_termination without predecessor_line_id -> 422 (pydantic)")

    # ── regression: found by simulating a real "revival from an old
    # glycerol stock" operator -- a typo'd caused_by_event in a restart's
    # founding_event was accepted with 201 and permanently written, even
    # though the IDENTICAL mistake via POST /events correctly 422s (the
    # existence check that runs there never ran for POST /lines) ──────────
    client, settings = make_client_with_settings()
    before = settings.log_file.read_text()
    r = client.post("/lines", json={
        "begin_mode": "restart",
        "new_line": new_line_spec(vial=15, founding_event={
            "timestamp": "2026-01-10T09:00:00-05:00", "event_type": "inoculation",
            "provenance": "reported", "params": {}, "notes": "revival from an old stock",
            "caused_by_event": "EVT-99999",  # does not exist
        }),
    })
    ck(r.status_code == 422, "a founding_event.caused_by_event that doesn't exist -> 422 (%s)" % r.status_code)
    ck(any("EVT-99999" in p for p in r.json()["detail"]), "the problem names the dangling reference")
    after = settings.log_file.read_text()
    ck(after == before, "the rejected write touched nothing on disk")

    # same check for split's parent_termination (a differently-shaped new
    # event, same code path). Note: FoundingEvent has no supersedes field
    # at all by design (line_models.py: "a founding or termination event
    # doesn't correct a prior one"), so there's no equivalent check to make
    # for that -- caused_by_event is the only cross-reference founding/
    # termination events carry.
    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "split", "parent_line_id": "testunit-v03",
        "parent_termination": termination_event(caused_by_event="EVT-77777"),
        "new_lines": [new_line_spec(vial=11), new_line_spec(vial=12)],
    })
    ck(r.status_code == 422, "split's parent_termination.caused_by_event that doesn't exist -> 422")

    # ── SPLIT ──────────────────────────────────────────────────────────────
    # child a/b founding events get DISTINCT notes here (rather than the
    # helper's shared default) so a later regression check can tell whether
    # GET /events/{id} resolved to the RIGHT one, not just an
    # identical-looking one.
    def split_child_spec(vial, notes):
        return new_line_spec(vial=vial, founding_event={
            "timestamp": "2026-01-10T09:00:00-05:00", "event_type": "inoculation",
            "provenance": "reported", "params": {}, "notes": notes,
        })

    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "split",
        "parent_line_id": "testunit-v03",
        "parent_termination": termination_event(),
        "new_lines": [split_child_spec(11, "child a founding event"), split_child_spec(12, "child b founding event")],
    })
    ck(r.status_code == 201, "split into 2 children returns 201 (%s)" % r.text[:300])
    body = r.json()
    ck(sorted(body["new_line_ids"]) == ["testunit-v03.a", "testunit-v03.b"], "split children lettered a, b")
    for cid in body["new_line_ids"]:
        ck(body["lines"][cid]["lineage"]["parents"] == ["testunit-v03"], "%s's parent is the split source" % cid)
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v03"]["status"] == "ended", "split parent is ended")
    term_event_id = on_disk["lines"]["testunit-v03"]["lineage"]["terminated_by_event"]
    for cid in body["new_line_ids"]:
        child_on_disk = on_disk["lines"][cid]
        ck(child_on_disk["events"][0].get("caused_by_event") == term_event_id,
           "%s's founding event is caused_by_event the parent's termination" % cid)

    # ── regression: a split mints 3 new events in one request (1 parent
    # termination + 2 child founding events) -- these must NOT collide on
    # one shared event_id (found by simulating a real split: build_event()
    # only advanced log_meta.event_counter once, at the very end of the
    # whole pipeline, so every event minted before that point got the same
    # "next" id) ──────────────────────────────────────────────────────────
    child_a_id = on_disk["lines"][body["new_line_ids"][0]]["events"][0]["event_id"]
    child_b_id = on_disk["lines"][body["new_line_ids"][1]]["events"][0]["event_id"]
    ck(len({term_event_id, child_a_id, child_b_id}) == 3,
       "the parent's termination and both children's founding events all got DISTINCT event_ids "
       "(%s, %s, %s)" % (term_event_id, child_a_id, child_b_id))
    ck(on_disk["log_meta"]["event_counter"] == 10,
       "log_meta.event_counter advanced by 3 for this split (7 -> 10), not by 1 (%s)"
       % on_disk["log_meta"]["event_counter"])
    # each event_id must resolve (via GET /events/{id}, the same index every
    # consumer uses) to ITS OWN event body, not to whichever of the 3 events
    # happens to be found first -- this is exactly what broke before: all 3
    # ids pointed at one one shared/last-written body.
    r_term = client.get("/events/%s" % term_event_id)
    ck(r_term.json()["event_type"] == "termination", "the parent's own event_id resolves to a termination event")
    notes_by_child = {cid: on_disk["lines"][cid]["events"][0]["notes"] for cid in body["new_line_ids"]}
    ck(sorted(notes_by_child.values()) == ["child a founding event", "child b founding event"],
       "each child's OWN stored event carries its own distinct notes, not a shared/collided one")
    for cid, event_id in ((body["new_line_ids"][0], child_a_id), (body["new_line_ids"][1], child_b_id)):
        r_child = client.get("/events/%s" % event_id)
        ck(r_child.json()["notes"] == notes_by_child[cid],
           "GET /events/%s resolves to %s's OWN founding event, not a different one that shares its id"
           % (event_id, cid))

    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "split", "parent_line_id": "testunit-v03",
        "parent_termination": termination_event(),
        "new_lines": [new_line_spec(vial=11)],  # only 1
    })
    ck(r.status_code == 422, "a split with only 1 child -> 422 (pydantic: needs >= 2)")

    r = client.post("/lines", json={
        "begin_mode": "split", "parent_line_id": "testunit-v02",  # already ended
        "parent_termination": termination_event(),
        "new_lines": [new_line_spec(vial=11), new_line_spec(vial=12)],
    })
    ck(r.status_code == 409, "splitting an already-ended parent -> 409")

    client, settings = make_client_with_settings()
    before = settings.log_file.read_text()
    r = client.post("/lines", json={
        "begin_mode": "split", "parent_line_id": "testunit-v03",
        "parent_termination": termination_event(),
        "new_lines": [new_line_spec(vial=11), new_line_spec(vial=1)],  # vial 1 is occupied!
    })
    ck(r.status_code == 409, "a split where one child's destination is occupied -> 409")
    after = settings.log_file.read_text()
    ck(after == before, "a rejected split leaves the file completely untouched -- not half-applied")

    # ── MERGE ──────────────────────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "merge",
        "parent_line_ids": ["testunit-v01", "testunit-v03"],
        "parent_terminations": {"testunit-v03": termination_event()},
        "new_line": new_line_spec(vial=3, line_id="testunit-v01+v03"),  # occupies v03's vial
    })
    ck(r.status_code == 201, "merge returns 201 (%s)" % r.text[:300])
    body = r.json()
    ck(body["new_line_ids"] == ["testunit-v01+v03"], "merge child uses the caller-supplied id")
    ck(sorted(body["lines"]["testunit-v01+v03"]["lineage"]["parents"]) == ["testunit-v01", "testunit-v03"],
       "merge child's parents are both stated parents")
    ck(body["lines"]["testunit-v01+v03"]["lineage"]["occupies_vial_of"] == "testunit-v03",
       "occupies_vial_of is the ending parent whose vial was taken over")
    ck(body["terminated_line_ids"] == ["testunit-v03"], "only the vial-owning parent is terminated")
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["status"] == "active", "the OTHER (continuing) parent stays active")
    ck(len(on_disk["lines"]["testunit-v01"]["events"]) == 1, "the continuing parent gets no new event")
    # regression: this mode also calls build_event() twice per request
    # (the ending parent's termination + the merged child's founding event).
    merge_term_id = on_disk["lines"]["testunit-v03"]["lineage"]["terminated_by_event"]
    merge_founding_id = on_disk["lines"]["testunit-v01+v03"]["events"][0]["event_id"]
    ck(merge_term_id != merge_founding_id,
       "merge's parent termination and child founding event got DISTINCT event_ids (%s, %s)"
       % (merge_term_id, merge_founding_id))

    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "merge", "parent_line_ids": ["testunit-v01", "testunit-v03"],
        "parent_terminations": {"testunit-v03": termination_event()},
        "new_line": new_line_spec(vial=3, line_id="testunit-v01+v99"),  # v99 isn't a parent's vial
    })
    ck(r.status_code == 422, "a merge id that doesn't decompose to its stated parents -> 422")

    r = client.post("/lines", json={
        "begin_mode": "merge", "parent_line_ids": ["testunit-v01", "testunit-v03"],
        "parent_terminations": {"testunit-v03": termination_event()},
        "new_line": new_line_spec(vial=1, line_id="testunit-v01+v03"),  # doesn't match the ending parent's vial
    })
    ck(r.status_code == 422, "a merge destination not matching any ending parent's vial -> 422")
    ck(any("testunit-v03 (testunit, vial 3)" in p for p in r.json()["detail"]),
       "the problem NAMES which vial(s) the ending parent(s) actually occupy, not just what was asked for "
       "-- found confusing by simulating real merge-edge-case use")

    r = client.post("/lines", json={
        "begin_mode": "merge", "parent_line_ids": ["testunit-v01", "testunit-v02"],  # v02 already ended
        "parent_terminations": {"testunit-v02": termination_event()},
        "new_line": new_line_spec(vial=2, line_id="testunit-v01+v02"),
    })
    ck(r.status_code == 409, "a merge naming an already-ended parent -> 409")

    r = client.post("/lines", json={
        "begin_mode": "merge", "parent_line_ids": ["testunit-v01", "testunit-v03"],
        "new_line": new_line_spec(vial=3, line_id="testunit-v01+v03"),  # no parent_terminations at all
    })
    ck(r.status_code == 422, "a merge with no ending parent at all -> 422")

    # ── regression: a "merge" naming the SAME line_id twice used to be
    # accepted outright, producing a merge child whose lineage.parents held
    # one ancestor twice -- permanently misrepresenting a single ancestor
    # as two independent contributing cultures, with no error. Found by
    # simulating an "extreme merge edge cases" operator. ────────────────────
    client, settings = make_client_with_settings()
    before = settings.log_file.read_text()
    r = client.post("/lines", json={
        "begin_mode": "merge", "parent_line_ids": ["testunit-v01", "testunit-v01"],
        "parent_terminations": {"testunit-v01": termination_event()},
        "new_line": new_line_spec(vial=1, line_id="testunit-v01+v01"),
    })
    ck(r.status_code == 422, "a merge naming the same line_id twice -> 422, not a corrupted lineage")
    ck("cannot merge with itself" in r.text, "the problem explains why: a line can't merge with itself")
    after = settings.log_file.read_text()
    ck(after == before, "the rejected self-merge touched nothing on disk")

    # ── regression: merge-id validation used to reject ANY parent whose own
    # id wasn't the one real precedent's simple occupancy shape
    # (unit-vNN(#gen)), regardless of what candidate_id was -- found by
    # simulating a real split-then-merge operator. LOG_PROTOCOL.md §5's
    # table has no such exception ("merge parent: either ... >= 2").
    # NOTE: the schema's own lineIdPattern only allows a SINGLE `.letter`
    # split-suffix, on the base (first) part of a composite id -- it has no
    # `.letter` slot on a later "+addend" at all, so merging TWO split
    # children together is a real, separate, schema-level limitation (log
    # repo, not this server) rather than something validate_merge_id alone
    # can unblock. What IS now fixed and schema-expressible: a split
    # child's id used as the merge id's BASE (first) part, addended with a
    # plain parent. ─────────────────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "split", "parent_line_id": "testunit-v03",
        "parent_termination": termination_event(),
        "new_lines": [new_line_spec(vial=11), new_line_spec(vial=12)],
    })
    ck(r.status_code == 201, "split for the merge-regression setup succeeds")
    child_a, child_b = sorted(r.json()["new_line_ids"])  # testunit-v03.a, testunit-v03.b

    r = client.post("/lines", json={
        "begin_mode": "merge", "parent_line_ids": [child_a, "testunit-v01"],
        "parent_terminations": {child_a: termination_event()},
        "new_line": new_line_spec(vial=11, line_id="%s+testunit-v01" % child_a),
    })
    ck(r.status_code == 201,
       "merging a SPLIT-CHILD line (as the id's base) with a plain line succeeds -- its id has "
       "no compact unit-vial form, so it must appear verbatim (%s: %s)" % (r.status_code, r.text[:300]))
    merged_id = "%s+testunit-v01" % child_a
    ck(sorted(r.json()["lines"][merged_id]["lineage"]["parents"]) == sorted([child_a, "testunit-v01"]),
       "the merge child's parents are exactly the split child and the plain line")

    r_wrong = client.post("/lines", json={
        "begin_mode": "merge", "parent_line_ids": [child_b, "testunit-v01"],
        "parent_terminations": {child_b: termination_event()},
        "new_line": new_line_spec(vial=12, line_id="%s+wrongthing" % child_b),
    })
    ck(r_wrong.status_code == 422,
       "a merge id that DOESN'T include a split-child parent's id verbatim is still rejected")

    # regression: the #generation hash-inclusion bug -- a parent id like
    # "testunit-v02#2" used to decompose to a generation fragment that
    # never equalled itself across the two parsing paths.
    client, settings = make_client_with_settings()
    r = client.post("/lines", json={
        "begin_mode": "restart", "predecessor_line_id": "testunit-v02",  # already ended
        "new_line": new_line_spec(vial=2),
    })
    ck(r.status_code == 201, "restart for the #generation merge-regression setup succeeds")
    restarted_id = r.json()["new_line_ids"][0]  # "testunit-v02#2"

    r = client.post("/lines", json={
        "begin_mode": "merge", "parent_line_ids": ["testunit-v01", restarted_id],
        "parent_terminations": {restarted_id: termination_event()},
        "new_line": new_line_spec(vial=2, line_id="testunit-v01+v02#2"),
    })
    ck(r.status_code == 201,
       "merging with a #generation parent id succeeds (%s: %s)" % (r.status_code, r.text[:300]))

    # ── regression, found by round-1 adversarial testing of ISSUE_002's
    # follow-up (vacate/"not on evolver"): next_occupancy_id used to filter
    # by a line's CURRENT unit/vial, so a line that RELOCATED away from a
    # vial (no vacate needed at all -- plain relocation is enough) made
    # that vial's occupancy history invisible to future id-minting, and a
    # later branch/restart into the now-empty vial tried to mint the SAME
    # bare id the relocated line still holds -- rejected by _insert_new_line
    # with a confusing "already exists" 409 that gave no hint the real
    # cause was an id collision, not real occupancy. ────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json=hardware_swap_event("testunit-v01", new_unit="testunit", new_vial=9))
    ck(r.status_code == 201, "relocate testunit-v01 away from vial 1, to set up the id-collision regression")
    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v03",
        "new_line": new_line_spec(vial=1),  # testunit-v01's now-EMPTY original vial
    })
    ck(r.status_code == 201,
       "branching into a vial a line has RELOCATED AWAY from succeeds, no id collision with that "
       "line's own (unmoved) id (%s: %s)" % (r.status_code, r.text[:300]))
    ck(r.json()["new_line_ids"] == ["testunit-v01#2"],
       "the new occupant is correctly suffixed #2 -- testunit-v01's own id, still held by the "
       "relocated line, is never reissued (%s)" % r.json()["new_line_ids"])

    # same bug, via vacate instead of relocation, restarting rather than
    # branching -- the exact shape the round-1 agent actually reproduced it in.
    client, settings = make_client_with_settings()
    r = client.post("/events", json=hardware_swap_event("testunit-v01", vacate=True))
    ck(r.status_code == 201, "vacate testunit-v01, to set up the id-collision regression via vacate")
    r = client.post("/lines", json={
        "begin_mode": "restart",  # day-one founder, no predecessor
        "new_line": new_line_spec(vial=1),
    })
    ck(r.status_code == 201, "restarting into a vial its line has VACATED succeeds, no id collision (%s: %s)"
       % (r.status_code, r.text[:300]))
    ck(r.json()["new_line_ids"] == ["testunit-v01#2"], "correctly suffixed #2 (%s)" % r.json()["new_line_ids"])

    # and the ALREADY-ENDED-then-vacated case: occupies_vial_of is correctly
    # recovered when the vacate event carried previous_unit/previous_vial --
    # last_real_position (app/line_ids.py) reconstructs the true prior
    # position from that, even though testunit-v02's CURRENT unit/vial are
    # now null.
    client, settings = make_client_with_settings()
    r = client.post("/events", json=hardware_swap_event(
        "testunit-v02", vacate=True, previous_unit="testunit", previous_vial=2))
    ck(r.status_code == 201, "vacate the already-ended testunit-v02, with previous_unit/previous_vial supplied")
    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v03",
        "new_line": new_line_spec(vial=2),
    })
    ck(r.status_code == 201, "branching into testunit-v02's vacated vial succeeds (%s: %s)"
       % (r.status_code, r.text[:300]))
    ck(r.json()["new_line_ids"] == ["testunit-v02#2"], "correctly suffixed (%s)" % r.json()["new_line_ids"])
    ck(r.json()["lines"]["testunit-v02#2"]["lineage"]["occupies_vial_of"] == "testunit-v02",
       "occupies_vial_of correctly names the true prior occupant, reconstructed from the vacate event's "
       "own previous_unit/previous_vial, even though testunit-v02's CURRENT unit/vial are null")

    # WITHOUT previous_unit/previous_vial on the vacate, the true prior
    # position genuinely cannot be reconstructed from anything this line's
    # own history retains -- occupies_vial_of comes back honestly absent,
    # not fabricated (CLAUDE.md: "never invent a value"). A documented
    # limitation (README.md), not a bug: the id-minting fix above doesn't
    # depend on this and still works correctly regardless.
    client, settings = make_client_with_settings()
    r = client.post("/events", json=hardware_swap_event("testunit-v02", vacate=True))
    ck(r.status_code == 201, "vacate testunit-v02 with no previous_unit/previous_vial supplied")
    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v03",
        "new_line": new_line_spec(vial=2),
    })
    ck(r.status_code == 201, "branching into the vial still succeeds -- the id-minting fix doesn't need this")
    ck(r.json()["new_line_ids"] == ["testunit-v02#2"], "still correctly suffixed (%s)" % r.json()["new_line_ids"])
    ck("occupies_vial_of" not in r.json()["lines"]["testunit-v02#2"]["lineage"],
       "occupies_vial_of is honestly absent, not invented, when the true prior position can't be "
       "reconstructed from this line's own retained history")

    # ── regression, found by round-2 adversarial testing: last_real_position
    # used to sort a line's own hardware_swap history by TIMESTAMP, exactly
    # the ordering CLAUDE.md warns is wrong for the log as a whole
    # ("event_id order is not chronological... because corrections are
    # appended later carrying earlier timestamps") -- a correction
    # (supersedes) legitimately carrying an EARLIER timestamp than the
    # mistake it corrects sorted BEFORE that mistake, so the mistake's
    # (superseded, wrong) value got walked LAST and silently won. Fixed by
    # sorting on event_id instead -- a monotonically increasing write-order
    # counter, which timestamp is not. ─────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json=hardware_swap_event(
        "testunit-v01", timestamp="2026-01-15T10:00:00-05:00", new_unit="testunit", new_vial=9))
    ck(r.status_code == 201, "the mistaken relocation (to vial 9) is appended")
    mistake_id = r.json()["event_id"]

    correction = hardware_swap_event(
        "testunit-v01", timestamp="2026-01-10T09:30:00-05:00",  # EARLIER than the mistake it corrects
        new_unit="testunit", new_vial=8)
    correction["supersedes"] = mistake_id
    r = client.post("/events", json=correction)
    ck(r.status_code == 201, "the correction (supersedes the mistake, real vial is 8) is appended (%s: %s)"
       % (r.status_code, r.text[:300]))
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["unit"] == "testunit" and on_disk["lines"]["testunit-v01"]["vial"] == 8,
       "the line's REAL current position is the correction's vial 8, not the mistake's vial 9 "
       "(live-applied immediately -- supersedes doesn't change that)")

    # occupies_vial_of only considers ENDED lines (_find_prior_occupant) --
    # terminate testunit-v01 before vacating so it's eligible at all; order
    # between termination and vacate doesn't matter (round-1 confirmed
    # both orders work).
    r = client.post("/events", json={
        "target": {"line_id": "testunit-v01"}, "timestamp": "2026-01-16T09:00:00-05:00",
        "event_type": "termination", "provenance": "reported", "params": {},
        "notes": "terminate for the last_real_position regression setup",
    })
    ck(r.status_code == 201, "terminate testunit-v01 (its own event, not embedded in a restart)")
    r = client.post("/events", json=hardware_swap_event("testunit-v01", vacate=True))
    ck(r.status_code == 201, "vacate testunit-v01 (now ended) from its (corrected) real position")

    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v03",
        "new_line": new_line_spec(vial=8),  # the CORRECTED position, not the superseded mistake's vial 9
    })
    ck(r.status_code == 201, "branching into vial 8 (the corrected position) succeeds (%s: %s)"
       % (r.status_code, r.text[:300]))
    ck(r.json()["lines"]["testunit-v08"]["lineage"]["occupies_vial_of"] == "testunit-v01",
       "occupies_vial_of correctly names testunit-v01 -- last_real_position resolved to the CORRECTED "
       "vial 8, not the superseded mistake's vial 9 (%s)" % r.json()["lines"]["testunit-v08"]["lineage"])

    # and vial 9 (the superseded mistake) correctly shows NO prior occupant
    # at all -- testunit-v01 never really, truly occupied it once corrected.
    r = client.post("/lines", json={
        "begin_mode": "branch", "parent_line_id": "testunit-v03",
        "new_line": new_line_spec(vial=9),
    })
    ck(r.status_code == 201, "branching into vial 9 (the superseded mistake) succeeds (%s: %s)"
       % (r.status_code, r.text[:300]))
    ck(r.json()["new_line_ids"] == ["testunit-v09"], "bare id -- vial 9 has no real occupancy history (%s)"
       % r.json()["new_line_ids"])
    ck("occupies_vial_of" not in r.json()["lines"]["testunit-v09"]["lineage"],
       "no occupies_vial_of for vial 9 -- testunit-v01's presence there was corrected away, not real")

    # ── regression, found by round-3 adversarial testing: a hardware_swap
    # naming previous_unit/previous_vial but NEITHER new_unit/new_vial NOR
    # vacate (a pure no-op from project_line_state's point of view --
    # "applied: false", line untouched) used to skip _check_previous_
    # position ENTIRELY, since that call only ran inside the vacate branch
    # or after the relocate branch's own destination checks. An unvalidated,
    # WRONG previous_unit/previous_vial then sat permanently in this line's
    # own recorded history -- and last_real_position (app/line_ids.py)
    # reads previous_unit/previous_vial off ANY hardware_swap event it
    # walks, trusting every one was already validated. Fixed by making
    # _check_previous_position run unconditionally, before any of
    # hardware_swap's branches. ─────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json=hardware_swap_event("testunit-v01", new_unit="testunit", new_vial=9))
    ck(r.status_code == 201, "relocate testunit-v01 to its real position, vial 9")

    r = client.post("/events", json=hardware_swap_event(
        "testunit-v01", previous_unit="testunit", previous_vial=13))  # WRONG -- real vial is 9, not 13
    ck(r.status_code == 409,
       "a no-op hardware_swap (no new_unit/new_vial, no vacate) with a WRONG previous_unit/previous_vial "
       "is now refused (%s), not silently accepted with the claim left unvalidated" % r.status_code)
    on_disk = json.loads(settings.log_file.read_text())
    ck(on_disk["lines"]["testunit-v01"]["unit"] == "testunit" and on_disk["lines"]["testunit-v01"]["vial"] == 9,
       "the rejected write changed nothing -- testunit-v01 is still genuinely at vial 9")

    # a CORRECT previous_unit/previous_vial on a no-op hardware_swap is
    # accepted (nothing to project, but nothing wrong was claimed either).
    r = client.post("/events", json=hardware_swap_event(
        "testunit-v01", previous_unit="testunit", previous_vial=9))
    ck(r.status_code == 201, "a no-op hardware_swap with a CORRECT previous_unit/previous_vial is still "
       "accepted (%s: %s)" % (r.status_code, r.text[:300]))
    ck(r.json()["line_lifecycle_projection"]["applied"] is False,
       "still correctly reports applied=False -- no structured destination, nothing to project")

    # ── invariant, exercised directly: an unrelated existing line must
    # never receive a new event or change status as a side effect ──────────
    old_log = {"lines": {
        "keep": {"status": "active", "events": [{"event_id": "EVT-A"}], "lineage": {}},
    }}
    tampered = {"lines": {
        "keep": {"status": "active", "events": [{"event_id": "EVT-A"}, {"event_id": "EVT-B"}], "lineage": {}},
    }}

    class _Stub:
        @staticmethod
        def unique_events(log):
            out = {}
            for line in log.get("lines", {}).values():
                for e in line.get("events", []):
                    out.setdefault(e["event_id"], e)
            return out
    try:
        lines_writer._assert_safe_mutation(old_log, tampered, _Stub(), expected_terminated=set())
        ck(False, "_assert_safe_mutation should reject an unrelated line gaining an unexpected event")
    except lines_writer.WriteConflict:
        ck(True, "_assert_safe_mutation refuses an unrelated line gaining an unexpected event")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
