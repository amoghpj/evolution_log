#!/usr/bin/env python3
"""Check that the schema REJECTS malformed logs, not just that it accepts a good one.

A schema that passes everything is worse than no schema, because it produces a
green result. Each mutation below corresponds to a mistake that is easy to make
by hand or by a script writing into this log.

    python3 tools/test_schema.py

Requires jsonschema with draft 2020-12 support. Without it this suite used to
test the built-in subset instead and report the subset's gaps as schema
failures -- on a machine with an old jsonschema it printed 6 FAILs for a schema
that is in fact correct, sending the reader hunting for bugs that do not exist.
It now refuses instead (exit 3). --allow-subset restores the old behaviour
deliberately, for a machine where you cannot pip install.
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import validate_schema as V  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def errors_for(log, validator):
    return [e for e in validator.iter_errors(log)]


def main():
    # The reference log, not this repo's own: these checks mutate or measure a
    # RICH real log, and this repo's evolution_log.json belongs to whatever
    # experiment it was initialised for -- possibly days old, possibly absent.
    log = json.load(open(os.path.join(ROOT, "reference", "or05_log.json")))
    schema = json.load(open(os.path.join(ROOT, "schema", "evolution_log.schema.json")))
    allow_subset = "--allow-subset" in sys.argv
    try:
        import jsonschema
        validator = jsonschema.Draft202012Validator(schema)
    except (ImportError, AttributeError):
        if not allow_subset:
            print("CANNOT TEST THE SCHEMA -- jsonschema with draft 2020-12 support")
            print("is not available.")
            print("  pip install -U jsonschema")
            print("\nThese cases assert what schema/evolution_log.schema.json rejects.")
            print("The built-in subset is a different, weaker checker, so running them")
            print("against it reports its gaps as schema defects. Refusing rather than")
            print("printing failures that say nothing about the schema.")
            print("Re-run with --allow-subset to test the subset on purpose.")
            return 3
        print("=" * 68)
        print("PARTIAL: jsonschema 2020-12 unavailable, testing the built-in")
        print("subset. FAILs below are gaps in the SUBSET, not in the schema.")
        print("=" * 68 + "\n")
        validator = None

    def rejects(mutate, msg):
        bad = copy.deepcopy(log)
        mutate(bad)
        errs = errors_for(bad, validator) if validator else V.fallback_validate(bad)
        ck(len(errs) > 0, msg)

    # the good file must pass, or nothing below means anything
    base = errors_for(log, validator) if validator else V.fallback_validate(log)
    ck(not base, "the real log passes cleanly (%d errors)" % len(base))

    first_line = next(iter(log["lines"]))
    first_res = log["reservoirs"]["items"][0]["id"]

    def line(b):
        return b["lines"][first_line]

    def res(b):
        return next(r for r in b["reservoirs"]["items"] if r["id"] == first_res)

    # ── timestamps without an offset have bitten this log more than once ──────
    rejects(lambda b: line(b)["events"][0].__setitem__("timestamp", "2026-08-22 18:00:00"),
            "rejects a timestamp with no UTC offset")
    rejects(lambda b: line(b)["events"][0].__setitem__("timestamp", "2026-08-22T18:00:00"),
            "rejects an ISO timestamp missing its offset")
    rejects(lambda b: line(b)["events"][0].__setitem__("timestamp", "2026-08-22T18:00:00-0400"),
            "rejects a colonless offset -- -0400 and -04:00 must not both validate")
    rejects(lambda b: line(b)["events"][0].__setitem__("timestamp", "2026-08-22T18:00:00Z"),
            "rejects Z -- it asserts UTC, not which local timezone was meant (SERVER_DESIGN #7)")

    # ── the founder/parents contradiction ────────────────────────────────────
    rejects(lambda b: line(b)["lineage"].update({"is_founder": True, "parents": ["patrick-v09"]}),
            "rejects a founder that has parents")
    rejects(lambda b: line(b)["lineage"].update({"is_founder": False, "parents": []}),
            "rejects a non-founder with no parents")

    # ── a terminated line left marked active ─────────────────────────────────
    rejects(lambda b: (line(b)["lineage"].__setitem__("terminated_at", "2026-08-26T12:00:00-04:00"),
                       line(b).__setitem__("status", "active")),
            "rejects a line terminated but still active")

    # ── shape of the two value types everything else is built from ───────────
    rejects(lambda b: line(b)["pg_regime"].__setitem__("low", 0.5),
            "rejects a bare number where a concentration belongs")
    rejects(lambda b: line(b)["pg_regime"]["low"].pop("value_mM"),
            "rejects a concentration missing its mM value")
    rejects(lambda b: line(b)["pg_regime"]["low"].__setitem__("value_g_per_L", -1),
            "rejects a negative concentration")
    rejects(lambda b: res(b)["volume_prepared"].__setitem__("value", "2-3"),
            "rejects a quantity.value that's a string -- a range is a quantityRange, not a string (Phase 2 #14)")
    rejects(lambda b: b["reservoirs"]["policy"]["replacement_interval"].pop("max"),
            "rejects a quantityRange missing its max")
    rejects(lambda b: b["reservoirs"]["policy"]["replacement_interval"].__setitem__("value", 2),
            "rejects a quantityRange carrying a bare value instead of min/max")

    def replacement_interval_as_plain_quantity(b):
        b["reservoirs"]["policy"]["replacement_interval"] = {"value": 3, "unit": "days"}
    good5 = copy.deepcopy(log)
    replacement_interval_as_plain_quantity(good5)
    errs5 = errors_for(good5, validator) if validator else V.fallback_validate(good5)
    ck(not errs5, "ACCEPTS policy.replacement_interval as a plain quantity too, not just a range")

    # ── enums that carry meaning ─────────────────────────────────────────────
    rejects(lambda b: line(b).__setitem__("status", "dead"),
            "rejects an unknown line status")
    rejects(lambda b: line(b).__setitem__("mode", "alternating"),
            "rejects an unknown mode")
    rejects(lambda b: res(b).__setitem__("role", "medium"),
            "rejects a reservoir role that is neither low nor high")
    rejects(lambda b: res(b).__setitem__("level_source", "guessed"),
            "rejects an unknown level_source")
    rejects(lambda b: res(b).__setitem__("level_qualifier", "roughly"),
            "rejects an unknown measurement qualifier")

    # ── ids ──────────────────────────────────────────────────────────────────
    rejects(lambda b: line(b)["events"][0].__setitem__("event_id", "EVT-7"),
            "rejects a malformed event id")
    rejects(lambda b: line(b)["reservoirs"].__setitem__("low", "LB-0.5"),
            "rejects a reservoir id that is not unit-scoped")
    rejects(lambda b: line(b).__setitem__("vial", 99),
            "rejects a vial number outside the rig")

    # ── required structure ───────────────────────────────────────────────────
    rejects(lambda b: line(b)["events"][0].pop("params"),
            "rejects an event with no params block")
    rejects(lambda b: line(b)["events"][0].pop("notes"),
            "rejects an event with no notes")
    rejects(lambda b: line(b).pop("pg_regime"),
            "rejects a line with no pg_regime")
    rejects(lambda b: b.pop("parameter_registry"),
            "rejects a log with no parameter_registry")
    rejects(lambda b: b["lines"].__setitem__("not a line id", b["lines"][first_line]),
            "rejects a line key that is not a valid line id")

    # ── line-id grammar (SERVER_DESIGN.md Phase 2 #16) ───────────────────────
    rejects(lambda b: line(b).__setitem__("line_id", "patrick-v09+10"),
            "rejects a merge addend missing its v (patrick-v09+10, not +v10)")
    rejects(lambda b: line(b).__setitem__("line_id", "patrick-v05.a#2"),
            "rejects split-then-occupancy order -- occupancy comes first")

    def cross_unit_merge_id(b):
        line(b)["line_id"] = "patrick-v09+plankton-v10"
    good6 = copy.deepcopy(log)
    cross_unit_merge_id(good6)
    errs6 = errors_for(good6, validator) if validator else V.fallback_validate(good6)
    ck(not errs6, "ACCEPTS a cross-unit merge id (patrick-v09+plankton-v10)")

    # ── the open part must STAY open, or the log stops absorbing new dimensions
    def add_novel_param(b):
        line(b)["events"][0]["params"]["some_dimension_nobody_anticipated"] = 42
    good = copy.deepcopy(log)
    add_novel_param(good)
    errs = errors_for(good, validator) if validator else V.fallback_validate(good)
    ck(not errs, "ACCEPTS an unanticipated params key -- params must stay open")

    def add_novel_event_type(b):
        e = copy.deepcopy(line(b)["events"][0])
        e["event_id"] = "EVT-99999"
        e["event_type"] = "some_new_kind_of_event"
        line(b)["events"].append(e)
    good2 = copy.deepcopy(log)
    add_novel_event_type(good2)
    errs2 = errors_for(good2, validator) if validator else V.fallback_validate(good2)
    ck(not errs2, "ACCEPTS a new event_type -- declaring it is lineage.py's job, not the schema's")

    # ── event-level supersedes (SERVER_DESIGN.md Phase 1 #8) ─────────────────
    def add_correction(b):
        e = copy.deepcopy(line(b)["events"][0])
        e["event_id"] = "EVT-99998"
        e["supersedes"] = line(b)["events"][0]["event_id"]
        line(b)["events"].append(e)
    good3 = copy.deepcopy(log)
    add_correction(good3)
    errs3 = errors_for(good3, validator) if validator else V.fallback_validate(good3)
    ck(not errs3, "ACCEPTS a new event carrying schema-level supersedes")

    rejects(lambda b: line(b)["events"][0].__setitem__("supersedes", "EVT-7"),
            "rejects a malformed event id in supersedes")

    # ── a top-level typo must not pass silently ──────────────────────────────
    rejects(lambda b: b.__setitem__("linez", {}),
            "rejects an unknown top-level key, catching a typo")

    # ── nested objects must be closed too, or a typo inside one validates clean
    # (SERVER_DESIGN.md Phase 1 #3: these were all open until now) ───────────
    rejects(lambda b: line(b)["events"][0].__setitem__("notse", "typo"),
            "rejects an unknown key on an event")
    rejects(lambda b: line(b).__setitem__("statuss", "active"),
            "rejects an unknown key on a line")
    rejects(lambda b: line(b)["lineage"].__setitem__("depht", 0),
            "rejects an unknown key on lineage")
    rejects(lambda b: line(b)["pg_regime"].__setitem__("lowe", {}),
            "rejects an unknown key on pg_regime")
    rejects(lambda b: b["log_meta"].__setitem__("maintaned_by", "AJ"),
            "rejects an unknown key on log_meta")
    rejects(lambda b: next(iter(b["parameter_registry"].values())).__setitem__("descriptoin", "x"),
            "rejects an unknown key on a parameter_registry entry")

    # ── registry entries now carry a type (SERVER_DESIGN.md Phase 1 #4) ──────
    rejects(lambda b: b["parameter_registry"]["role"].pop("type"),
            "rejects a registry entry with no type")
    rejects(lambda b: b["parameter_registry"]["role"].__setitem__("type", "stringg"),
            "rejects a registry entry with an unrecognised type name")
    rejects(lambda b: b["parameter_registry"]["role"].__setitem__("enum", []),
            "rejects a registry entry with an empty enum")
    rejects(lambda b: b["parameter_registry"]["reservoir_id_from"]
                       .__setitem__("type", ["string", "array", "boolean"]),
            "rejects a registry entry with more than two polymorphic types")

    def add_novel_registry_type(b):
        b["parameter_registry"]["role"]["type"] = "eventId"
    good4 = copy.deepcopy(log)
    add_novel_registry_type(good4)
    errs4 = errors_for(good4, validator) if validator else V.fallback_validate(good4)
    ck(not errs4, "ACCEPTS any declared registryValueType, not just the ones currently used")
    rejects(lambda b: res(b).__setitem__("statuss", "active"),
            "rejects an unknown key on a reservoir item")
    rejects(lambda b: b["lines"]["patrick-v04"]["ramp"]["history"][0].__setitem__("stepsize", {}),
            "rejects an unknown key on a ramp interval")
    rejects(lambda b: next(r for r in b["reservoirs"]["items"] if r["id"] == "patrick/LB-5")
                       ["fill_history"][0].__setitem__("preparedat", "x"),
            "rejects an unknown key on a fill record")
    rejects(lambda b: b["lineage_summary"].__setitem__("n_nods", 0),
            "rejects an unknown key on lineage_summary")
    rejects(lambda b: b["hardware"]["units"]["patrick"].__setitem__("n_liness", 0),
            "rejects an unknown key on a hardware unit")

    subject = "schema" if validator else "built-in subset (NOT the schema)"
    print("\nRESULT: %s (%d failed) -- subject: %s"
          % ("OK" if not _fails else "PROBLEMS", len(_fails), subject))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
