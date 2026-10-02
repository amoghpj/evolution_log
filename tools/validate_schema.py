#!/usr/bin/env python3
"""Validate evolution_log.json against schema/evolution_log.schema.json.

    python3 tools/validate_schema.py
    python3 tools/validate_schema.py --path some_other_log.json

The schema checks SHAPE. It cannot check the things this log most depends on,
because they are relationships between fields rather than properties of one:

    every params key is in parameter_registry
    every event_type is declared in event_types
    every lineage parent exists, and the graph has no cycle
    a line's PG floor matches the reservoir it actually draws from
    log_meta.event_counter equals the number of distinct event ids

Those live in tools/lineage.py and tools/test_media.py. This script runs the
schema and then names the checks it did not perform, so a green result is not
mistaken for a complete one.

Exit 0 means the real draft 2020-12 schema ran and passed. If jsonschema is
missing or too old, this script FAILS rather than quietly checking less. It used
to degrade to a built-in subset and still print "schema OK", which meant the
full schema had never actually been run against this log. Pass --allow-subset to
run the subset deliberately, on a machine where you cannot pip install.

Exit codes:
    0   the schema ran and passed (or --allow-subset was given and it passed)
    1   the log violates the schema
    3   the schema could not be run: jsonschema missing, too old, or invalid
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = os.path.join(ROOT, "evolution_log.json")
SCHEMA = os.path.join(ROOT, "schema", "evolution_log.schema.json")

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
EID = re.compile(r"^EVT-\d{5,}$")


def fallback_validate(log):
    """A hand-rolled subset for when jsonschema is unavailable. Deliberately
    small: it checks the invariants that have actually been violated in this
    log's history rather than trying to reimplement the schema."""
    errs = []

    def bad(where, msg):
        errs.append("%s: %s" % (where, msg))

    for key in ("schema_version", "log_meta", "lines", "experiment_events",
                "reservoirs", "event_types", "parameter_registry"):
        if key not in log:
            bad("$", "missing required key %r" % key)
    if errs:
        return errs

    seen_ids = {}
    def check_event(e, where):
        for k in ("event_id", "timestamp", "event_type", "operator", "provenance", "params", "notes"):
            if k not in e:
                bad(where, "event missing %r" % k)
                return
        if not EID.match(e["event_id"]):
            bad(where, "event_id %r is malformed" % e["event_id"])
        if not TS.match(e["timestamp"]):
            bad(where, "%s timestamp %r lacks an explicit UTC offset"
                % (e["event_id"], e["timestamp"]))
        if not isinstance(e["params"], dict):
            bad(where, "%s params is not an object" % e["event_id"])
        prev = seen_ids.get(e["event_id"])
        if prev and prev != json.dumps(e, sort_keys=True):
            bad(where, "%s appears twice with different content" % e["event_id"])
        seen_ids[e["event_id"]] = json.dumps(e, sort_keys=True)

    for lid, L in log["lines"].items():
        w = "lines/%s" % lid
        if L.get("line_id") != lid:
            bad(w, "line_id %r does not match its key" % L.get("line_id"))
        if L.get("status") not in ("active", "ended"):
            bad(w, "status %r is not active or ended" % L.get("status"))
        if L.get("mode") not in ("constant", "switch"):
            bad(w, "mode %r is not constant or switch" % L.get("mode"))
        lin = L.get("lineage") or {}
        if lin.get("terminated_at") and L.get("status") == "active":
            bad(w, "has a termination timestamp but is still active")
        if lin.get("is_founder") and lin.get("parents"):
            bad(w, "marked founder but has parents")
        if lin.get("is_founder") is False and not lin.get("parents"):
            bad(w, "not marked founder but has no parents")
        rg = L.get("pg_regime") or {}
        for side in ("low", "high"):
            c = rg.get(side)
            if not isinstance(c, dict) or "value_g_per_L" not in c:
                bad(w, "pg_regime.%s is not a concentration" % side)
        for e in L.get("events", []):
            check_event(e, w)

    for e in log["experiment_events"]:
        check_event(e, "experiment_events")

    for r in log["reservoirs"].get("items", []):
        w = "reservoirs/%s" % r.get("id")
        if r.get("role") not in ("low", "high"):
            bad(w, "role %r is not low or high" % r.get("role"))
        if r.get("status") not in ("active", "retired"):
            bad(w, "status %r is not active or retired" % r.get("status"))
        if r.get("level_source") not in ("measured", "prepared"):
            bad(w, "level_source %r is not measured or prepared" % r.get("level_source"))
        cv, vp = r.get("current_volume"), r.get("volume_prepared")
        if isinstance(cv, dict) and isinstance(vp, dict) and cv.get("value") is not None:
            if cv["value"] > vp["value"] + 1e-9:
                bad(w, "current volume %s exceeds prepared %s" % (cv["value"], vp["value"]))
    return errs


NOT_CHECKED = [
    "every params key is registered in parameter_registry",
    "every event_type is declared in event_types",
    "every lineage parent exists and the graph is acyclic",
    "lineage children, roots and depth match the parents they derive from",
    "each line's PG floor matches the low reservoir it draws from",
    "log_meta.event_counter equals the number of distinct event ids",
    "reservoir consumption rates are measured within one bottle",
    "the g/L and mM halves of every concentration agree (tools/lineage.py)",
    "every null params/pg_regime value is named in missing_fields (tools/lineage.py)",
    "every non-null params value matches its parameter_registry type/enum (tools/lineage.py)",
    "pg_regime.ceiling equals pg_regime.high wherever both are present (tools/lineage.py)",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=LOG)
    ap.add_argument("--schema", default=SCHEMA)
    ap.add_argument("--quiet", action="store_true", help="only print failures")
    ap.add_argument("--allow-subset", action="store_true",
                    help="if jsonschema is unavailable, run the built-in subset "
                         "instead of failing. Never silent: the result is labelled "
                         "as partial. Do not use this in automation.")
    args = ap.parse_args()

    with open(args.path) as fh:
        log = json.load(fh)

    try:
        import jsonschema
    except ImportError:
        jsonschema = None

    # An old jsonschema is worse than none: it silently validates against a
    # draft that does not understand $defs or prefixItems, so it passes files it
    # has not really checked. Require 2020-12 support explicitly.
    unavailable = None
    if jsonschema is None:
        unavailable = "jsonschema is not installed"
    elif not hasattr(jsonschema, "Draft202012Validator"):
        unavailable = "jsonschema is installed but too old for draft 2020-12"

    if unavailable and not args.allow_subset:
        print("CANNOT VALIDATE -- %s." % unavailable)
        print("  pip install -U jsonschema")
        print("\nRefusing to report a result. The built-in subset checks strictly")
        print("less than schema/evolution_log.schema.json, so passing it is not")
        print("evidence the log is well formed. Re-run with --allow-subset if you")
        print("understand that and want the partial check anyway.")
        return 3

    if unavailable:
        print("=" * 68)
        print("PARTIAL CHECK ONLY -- %s." % unavailable)
        print("Running the built-in subset. This is NOT the full schema.")
        print("=" * 68 + "\n")
        errs = fallback_validate(log)
        engine = "built-in subset"
    else:
        with open(args.schema) as fh:
            schema = json.load(fh)
        # A malformed schema is another way to validate nothing while looking
        # successful, so check the schema itself before trusting its verdict.
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except Exception as exc:
            print("CANNOT VALIDATE -- %s is not a valid draft 2020-12 schema:" % args.schema)
            print("  %s" % exc)
            return 3
        validator = jsonschema.Draft202012Validator(schema)
        errs = []
        for err in sorted(validator.iter_errors(log), key=lambda e: list(e.absolute_path)):
            where = "/".join(str(x) for x in err.absolute_path) or "$"
            errs.append("%s: %s" % (where, err.message))
        engine = "jsonschema draft 2020-12"

    if errs:
        print("SCHEMA FAILED (%s) -- %d problem(s):\n" % (engine, len(errs)))
        for e in errs[:40]:
            print("  " + e)
        if len(errs) > 40:
            print("  ... and %d more" % (len(errs) - 40))
        return 1

    partial = engine == "built-in subset"
    if not args.quiet:
        if partial:
            print("SUBSET PASSED -- the full schema was NOT run (%s)" % engine)
        else:
            print("schema OK (%s)" % engine)
        print("  %d lines, %d facility events, %d registry entries"
              % (len(log["lines"]), len(log["experiment_events"]), len(log["parameter_registry"])))
        print("\nNOT checked here -- these are cross-field and live in tools/lineage.py:")
        for c in NOT_CHECKED:
            print("  - " + c)
        print("\nRun tools/lineage.py and tools/test_media.py for those.")
        if partial:
            print("\n" + "=" * 68)
            print("Reminder: the above was the built-in subset, not the schema.")
            print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
