#!/usr/bin/env python3
"""Checks that tools/lineage.py's own cross-field validate() REJECTS
malformed logs, not just that recompute() runs cleanly on a good one --
mirroring tools/test_schema.py's own "a schema that passes everything is
worse than no schema" reasoning, extended to lineage.py's checks, which
currently have no dedicated regression coverage of their own.

    python3 tools/test_lineage.py
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import lineage as L  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    # The reference log, not this repo's own: these checks mutate or measure a
    # RICH real log, and this repo's evolution_log.json belongs to whatever
    # experiment it was initialised for -- possibly days old, possibly absent.
    log = json.load(open(os.path.join(ROOT, "reference", "or05_log.json")))

    base = L.validate(log)
    ck(not base, "the real log passes cleanly (%d problems)" % len(base))

    first_line = next(iter(log["lines"]))
    second_line = next(lid for lid in log["lines"] if lid != first_line)

    def line(b, lid=first_line):
        return b["lines"][lid]

    def rejects(mutate, msg):
        bad = copy.deepcopy(log)
        mutate(bad)
        problems = L.validate(bad)
        ck(len(problems) > 0, msg)

    # ── unit/vial must be null together (ISSUE_002 follow-up: "not on
    # evolver"), never just one -- the schema allows each independently, so
    # this specific pairing is lineage.py's job, the same division CLAUDE.md
    # already draws for every other cross-field relationship ──────────────
    rejects(lambda b: line(b).__setitem__("vial", None),
            "rejects vial: null while unit is still set")
    rejects(lambda b: line(b).__setitem__("unit", None),
            "rejects unit: null while vial is still set")

    # both null together is fine -- confirm the check isn't just "vial is
    # never allowed to be null", which would defeat the whole feature
    good_off_rig = copy.deepcopy(log)
    line(good_off_rig)["unit"] = None
    line(good_off_rig)["vial"] = None
    ck(not L.validate(good_off_rig), "ACCEPTS unit and vial null TOGETHER -- a real off-evolver state")

    # ── no two active lines may claim the same real (unit, vial) ───────────
    def make_duplicate(b):
        line(b, first_line)["status"] = "active"
        line(b, first_line)["unit"], line(b, first_line)["vial"] = "duptest-unit", 7
        line(b, second_line)["status"] = "active"
        line(b, second_line)["unit"], line(b, second_line)["vial"] = "duptest-unit", 7

    rejects(make_duplicate, "rejects two ACTIVE lines occupying the same (unit, vial)")

    # an ENDED line duplicating an active line's position is fine -- e.g. a
    # restart's predecessor, which legitimately still names the same vial
    # after handing it off (occupies_vial_of, not this check's concern)
    ended_dup = copy.deepcopy(log)
    make_duplicate(ended_dup)
    line(ended_dup, second_line)["status"] = "ended"
    ck(not any("both claim to actively occupy" in p for p in L.validate(ended_dup)),
       "an ENDED line sharing a position with an active one is not flagged")

    # ── recompute() actually sets media_switch_count from real history,
    # not a static/inherited value ──────────────────────────────────────────
    fresh = copy.deepcopy(log)
    target = line(fresh)
    target["events"].append({
        "event_id": "EVT-TESTONLY", "timestamp": "2099-01-01T00:00:00-05:00",
        "event_type": "media_switch", "operator": "TEST", "provenance": "reported",
        "params": {"media_to": "M9"}, "notes": "test-only injected event", "missing_fields": [],
    })
    target["media_switch_count"] = 999  # deliberately wrong, to prove recompute overwrites it
    L.recompute(fresh)
    ck(line(fresh)["media_switch_count"] == 1,
       "recompute() sets media_switch_count from a real count, overwriting a stale/wrong value (%s)"
       % line(fresh)["media_switch_count"])

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
