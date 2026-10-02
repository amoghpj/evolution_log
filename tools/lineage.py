#!/usr/bin/env python3
"""Recompute derived lineage fields in evolution_log.json and validate the log.

The JSON is the single source of truth. Everything this script writes is
derivable from it: lineage.children, lineage.roots, lineage.depth,
lineage.is_founder and the lineage_summary block. Run it after any hand edit.

    python3 tools/lineage.py            # validate only
    python3 tools/lineage.py --write    # recompute derived fields and save
"""
import argparse
import json
import os
import re
import sys
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(os.path.dirname(HERE), "evolution_log.json")

# Phloroglucinol, C6H6O3. Every concentration in the log carries a g/L value and
# an mM value for the same quantity, both entered by hand, and until now nothing
# checked that the two agreed. The schema cannot: it is a relationship between
# two fields. Per LOG_PROTOCOL.md section 11 the dangerous failure here is a
# confident number, not a crash.
PG_MW_G_PER_MOL = 126.11
# Tolerance is for rounding in the stored mM value, not for disagreement. The
# errors this is meant to catch -- a transcribed digit, a unit confusion, a value
# copied from the wrong row -- are all far outside it.
CONC_REL_TOL = 0.005
CONC_ABS_TOL_MM = 0.01

# LOG_PROTOCOL.md, "missing_fields, precisely" (SERVER_DESIGN.md Phase 1 #6):
# every params key present with value null must be named in missing_fields, so
# a gap can never quietly present as "checked, and it's genuinely null". These
# 57 (event_id, key) pairs violate that and predate the rule (2026-08-27); the
# one rule that outranks the others forbids editing an existing event to add
# the name now, so they are grandfathered by exact identity rather than
# silently exempted by being null, which would hide any *new* violation too.
MISSING_FIELDS_GRANDFATHERED = frozenset(
    [("EVT-%05d" % n, "pg_target") for n in
        list(range(1, 17)) + list(range(26, 32))] +
    [("EVT-%05d" % n, k) for n in range(147, 160)
        for k in ("stock_id", "storage_location")] +
    [("EVT-%05d" % n, "colony_count") for n in range(174, 181)] +
    [("EVT-00188", "lines_affected"), ("EVT-00221", "input_pump2")]
)


def load(path=LOG):
    with open(path) as fh:
        return json.load(fh, object_pairs_hook=OrderedDict)


def save(log, path=LOG):
    with open(path, "w") as fh:
        json.dump(log, fh, indent=2)
        fh.write("\n")


def unique_events(log):
    """Every event in the log, keyed by event_id: per-line events plus the
    facility-level experiment_events. A split or merge may appear in several
    lines' event arrays; it is one event and must be counted once."""
    out = OrderedDict()
    for L in log.get("lines", {}).values():
        for e in L.get("events", []):
            out.setdefault(e.get("event_id"), e)
    for e in log.get("experiment_events", []):
        out.setdefault(e.get("event_id"), e)
    return out


def iter_concentrations(obj, path="$"):
    """Yield (path, obj) for every concentration object anywhere in the log.

    Walks the whole document rather than a list of known field names, so a
    concentration added in a place nobody anticipated is still checked."""
    if isinstance(obj, dict):
        if {"value_g_per_L", "value_mM", "unit_primary"} <= set(obj):
            yield path, obj
        for k, v in obj.items():
            for hit in iter_concentrations(v, "%s/%s" % (path, k)):
                yield hit
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            for hit in iter_concentrations(v, "%s[%d]" % (path, i)):
                yield hit


# SERVER_DESIGN.md Phase 1 #4: parameter_registry entries now carry a type, so
# a params value can be checked against what its own registry entry declares
# it should be -- catching an od_reliable: "no" or a pg_high stored as a bare
# number, not just an unregistered key. Regexes mirror schema/evolution_log
# .schema.json's $defs exactly; they are duplicated (not imported from the
# schema) because the schema is data, not code, and this keeps the two
# checkable independently of each other. null is never checked here: whether
# a null is allowed is check_missing_fields's job, not this one's.
EVENT_ID_RE = re.compile(r"^EVT-\d{5,}$")
LINE_ID_RE = re.compile(r"^[a-z]+-v\d{2}(#\d+)?(\.[a-z])?(\+([a-z]+-)?v\d{2}(#\d+)?)*$")
RESERVOIR_ID_RE = re.compile(r"^[a-z]+/[A-Za-z0-9]+-\d+(\.\d+)?$")


def _matches_type(v, vtype):
    """True if v matches one registryValueType, ignoring null (caller's job)."""
    if vtype == "string":
        return isinstance(v, str)
    if vtype == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if vtype == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if vtype == "boolean":
        return isinstance(v, bool)
    if vtype == "array":
        return isinstance(v, list)
    if vtype == "object":
        return isinstance(v, dict)
    if vtype == "quantity":
        return isinstance(v, dict) and {"value", "unit"} <= set(v)
    if vtype == "concentration":
        return isinstance(v, dict) and {"value_g_per_L", "value_mM", "unit_primary"} <= set(v)
    if vtype == "eventId":
        return isinstance(v, str) and bool(EVENT_ID_RE.match(v))
    if vtype == "lineId":
        return isinstance(v, str) and bool(LINE_ID_RE.match(v))
    if vtype == "reservoirId":
        return isinstance(v, str) and bool(RESERVOIR_ID_RE.match(v))
    if vtype == "any":
        return True
    return False  # an unrecognised type name is a registry bug, not a log bug


def check_param_types(log):
    """Every non-null params value must match its own registry entry's
    declared type (and enum, where the entry has one)."""
    problems = []
    reg = log.get("parameter_registry", {})
    for eid, e in unique_events(log).items():
        for k, v in (e.get("params") or {}).items():
            if v is None:
                continue
            entry = reg.get(k)
            if not entry or "type" not in entry:
                continue  # unregistered key / no type: a different check's job
            vtype = entry["type"]
            types = vtype if isinstance(vtype, list) else [vtype]
            if not any(_matches_type(v, t) for t in types):
                problems.append(
                    "%s: params.%s = %r does not match registered type %s"
                    % (eid, k, v, vtype))
                continue
            if "array" in types and isinstance(v, list) and entry.get("items"):
                itype = entry["items"]
                for i, item in enumerate(v):
                    if not _matches_type(item, itype):
                        problems.append(
                            "%s: params.%s[%d] = %r does not match item type %s"
                            % (eid, k, i, item, itype))
            if entry.get("enum") is not None:
                values = v if isinstance(v, list) and "array" in types else [v]
                for val in values:
                    if val not in entry["enum"]:
                        problems.append(
                            "%s: params.%s = %r is not one of the registered enum %s"
                            % (eid, k, val, entry["enum"]))
    return problems


def check_ceiling_matches_high(log):
    """pg_regime.ceiling is not a separate design constant -- it's the same
    fact as pg_regime.high, kept as a distinct field only for contexts that
    are talking about the safety bound specifically (LOG_PROTOCOL.md §6,
    SERVER_DESIGN.md Phase 2 #13). The two must never diverge."""
    problems = []
    for lid, line in log.get("lines", {}).items():
        pg = line.get("pg_regime") or {}
        ceiling, high = pg.get("ceiling"), pg.get("high")
        if ceiling is None or high is None:
            continue
        if (ceiling.get("value_g_per_L"), ceiling.get("value_mM")) != \
           (high.get("value_g_per_L"), high.get("value_mM")):
            problems.append("%s: pg_regime.ceiling %s != pg_regime.high %s" % (lid, ceiling, high))
    return problems


def check_reservoir_agreement(log):
    """A line's two records of its own feeding reservoirs must agree.

    The log states this twice -- line-level `reservoirs` and
    `pg_regime.source_reservoirs` -- both schema-shaped, both hand-maintained,
    and nothing compared them until now.

    It matters because the evolution log server's GET /media refuses to report
    a pump-derived measurement when they disagree, which is right (a module
    built to refuse rather than guess must not pick one of two contradicting
    records) but turns a silent log inconsistency into a silently MISSING
    measurement -- discoverable only as "the number I wanted isn't there".

    DELIBERATELY NOT CHECKED: whether `reservoirs.items[*].lines_fed` mirrors
    these. That denormalisation is not maintained by the server -- GET /skill
    says so in as many words -- so a line created through POST /lines, or
    terminated through POST /events, legitimately leaves it stale until
    somebody edits it. Making that an integrity failure would reject ordinary,
    correct writes; a first draft of this check did exactly that and broke
    three of the server's own suites. Existence is not checked either: an
    ended line properly names a reservoir id that a later reformulation
    renamed out of reservoirs.items (LOG_PROTOCOL.md section 4,
    media_prep.replaces), which is 12 cases in the log today and none a defect.
    """
    problems = []
    for lid, line in (log.get("lines") or {}).items():
        legacy = line.get("reservoirs") if isinstance(line.get("reservoirs"), dict) else {}
        regime = (line.get("pg_regime") or {}).get("source_reservoirs")
        regime = regime if isinstance(regime, dict) else {}
        for role in sorted(set(legacy) | set(regime)):
            a, b = legacy.get(role), regime.get(role)
            if isinstance(a, str) and isinstance(b, str) and a != b:
                problems.append(
                    "%s: reservoirs.%s is %r but pg_regime.source_reservoirs.%s is %r "
                    "-- two records of the same fact, disagreeing" % (lid, role, a, role, b))
    return problems


def check_missing_fields(log):
    """Every params key (and every pg_regime field) sitting at null must be
    named in the enclosing object's missing_fields, or the gap is invisible --
    indistinguishable from a value that was checked and is genuinely null.
    The reverse is NOT required: missing_fields may name a fact that never had
    a params key at all (LOG_PROTOCOL.md, "missing_fields, precisely")."""
    problems = []
    for eid, e in unique_events(log).items():
        mf = e.get("missing_fields") or []
        for k, v in (e.get("params") or {}).items():
            if v is None and k not in mf and (eid, k) not in MISSING_FIELDS_GRANDFATHERED:
                problems.append(
                    "%s: params.%s is null but not named in missing_fields" % (eid, k))
    for lid, line in log.get("lines", {}).items():
        pg = line.get("pg_regime") or {}
        mf = pg.get("missing_fields") or []
        for k, v in pg.items():
            if k != "missing_fields" and v is None and k not in mf:
                problems.append(
                    "%s: pg_regime.%s is null but not named in pg_regime.missing_fields" % (lid, k))
    return problems


def check_concentrations(log):
    """Problems where a concentration's g/L and mM halves disagree."""
    problems = []
    for path, c in iter_concentrations(log):
        g, mm = c.get("value_g_per_L"), c.get("value_mM")
        if isinstance(g, bool) or not isinstance(g, (int, float)):
            problems.append("%s: value_g_per_L is not a number (%r)" % (path, g))
            continue
        if isinstance(mm, bool) or not isinstance(mm, (int, float)):
            problems.append("%s: value_mM is not a number (%r)" % (path, mm))
            continue
        expected = g / PG_MW_G_PER_MOL * 1000.0
        if abs(mm - expected) > max(CONC_ABS_TOL_MM, CONC_REL_TOL * expected):
            problems.append(
                "%s: %g g/L is %.4f mM at MW %s, but value_mM reads %g"
                % (path, g, expected, PG_MW_G_PER_MOL, mm))
    return problems


def recompute(log):
    """Rewrite all derived lineage fields. Idempotent. Raises on a cycle."""
    lines = log["lines"]

    for L in lines.values():
        L.setdefault("lineage", OrderedDict([("parents", [])]))
        L["lineage"]["children"] = []
    for lid, L in lines.items():
        for p in L["lineage"].get("parents", []):
            if p in lines and lid not in lines[p]["lineage"]["children"]:
                lines[p]["lineage"]["children"].append(lid)
    for L in lines.values():
        L["lineage"]["children"].sort()

    memo_r, memo_d = {}, {}

    def parents(lid):
        return [p for p in lines[lid]["lineage"].get("parents", []) if p in lines and p != lid]

    def roots(lid, seen=frozenset()):
        if lid in memo_r:
            return memo_r[lid]
        if lid in seen:
            raise ValueError("lineage cycle involving %s" % lid)
        ps = parents(lid)
        r = [lid] if not ps else []
        for p in ps:
            for x in roots(p, seen | {lid}):
                if x not in r:
                    r.append(x)
        memo_r[lid] = r
        return r

    def depth(lid, seen=frozenset()):
        if lid in memo_d:
            return memo_d[lid]
        if lid in seen:
            raise ValueError("lineage cycle involving %s" % lid)
        ps = parents(lid)
        d = 0 if not ps else 1 + max(depth(p, seen | {lid}) for p in ps)
        memo_d[lid] = d
        return d

    n_edges = 0
    for lid, L in lines.items():
        L["lineage"]["roots"] = roots(lid)
        L["lineage"]["depth"] = depth(lid)
        L["lineage"]["is_founder"] = not L["lineage"].get("parents")
        n_edges += len(L["lineage"].get("parents", []))
        L["events"] = sorted(L.get("events", []), key=lambda e: (e.get("timestamp", ""), e.get("event_id", "")))
        # A real count of this line's OWN media_switch events, not a flag --
        # recomputed from scratch every time, the same way children/roots/
        # depth are just above, not incrementally bumped by whatever code
        # appended the triggering event. Keeps "how do we backfill an
        # existing line" and "how do we keep a future one current" the same
        # single definition, never two that could drift.
        L["media_switch_count"] = sum(1 for e in L["events"] if e.get("event_type") == "media_switch")

    ev = unique_events(log).values()
    log.setdefault("lineage_summary", OrderedDict())
    log["lineage_summary"].update(OrderedDict([
        ("founders", sorted(l for l, L in lines.items() if L["lineage"]["is_founder"])),
        ("n_nodes", len(lines)),
        ("n_edges", n_edges),
        ("max_depth", max((L["lineage"]["depth"] for L in lines.values()), default=0)),
        ("n_splits", sum(1 for e in ev if e.get("event_type") == "split")),
        ("n_merges", sum(1 for e in ev if e.get("event_type") == "merge")),
    ]))
    log["lineage_summary"].setdefault(
        "note",
        "Derived summary, rewritten by tools/lineage.py. The 'lines' object remains the source of truth.")
    return log


def validate(log):
    """Return a list of problems. Empty list means the log is internally consistent."""
    problems = []
    lines = log.get("lines", {})
    reg = log.get("parameter_registry", {})
    types = set(log.get("event_types", {}))

    for lid, L in lines.items():
        if L.get("line_id") != lid:
            problems.append("%s: line_id field does not match its key" % lid)
        lin = L.get("lineage", {})
        for p in lin.get("parents", []):
            if p not in lines:
                problems.append("%s: parent %s does not exist" % (lid, p))
            if p == lid:
                problems.append("%s: line is its own parent" % lid)
        if lin.get("terminated_at") and L.get("status") == "active":
            problems.append("%s: has a termination timestamp but status is still active" % lid)
        if (L.get("unit") is None) != (L.get("vial") is None):
            problems.append("%s: unit and vial must be null TOGETHER (\"not on evolver\") or not at "
                             "all -- one is null and the other isn't" % lid)

        ts = [e.get("timestamp") for e in L.get("events", [])]
        if ts != sorted(ts):
            problems.append("%s: events are not in timestamp order" % lid)
        start = lin.get("created_at") or L.get("t0")
        for e in L.get("events", []):
            if start and e.get("timestamp", "") < start:
                problems.append("%s/%s: event predates the line's creation" % (lid, e.get("event_id")))

    ev = unique_events(log)
    for eid, e in ev.items():
        if not eid:
            problems.append("an event is missing its event_id")
        if types and e.get("event_type") not in types:
            problems.append("%s: unknown event_type '%s'" % (eid, e.get("event_type")))
        for k in e.get("params", {}):
            if reg and k not in reg:
                problems.append("%s: param '%s' is not in parameter_registry" % (eid, k))

    # every id claimed by a split/merge must exist
    for eid, e in ev.items():
        for key in ("parent_line_ids", "child_line_ids"):
            for ref in (e.get("params", {}).get(key) or []):
                if ref not in lines:
                    problems.append("%s: %s references unknown line '%s'" % (eid, key, ref))

    # no two ACTIVE lines may occupy the same real (unit, vial) at once --
    # a hand edit bypasses the server's own assert_destination_empty check,
    # so this is the one place that catches it regardless of how it happened.
    occupied = {}
    for lid, L in lines.items():
        if L.get("status") != "active" or L.get("unit") is None or L.get("vial") is None:
            continue
        key = (L["unit"], L["vial"])
        if key in occupied:
            problems.append("%s and %s both claim to actively occupy %s vial %d"
                            % (occupied[key], lid, key[0], key[1]))
        else:
            occupied[key] = lid

    meta = log.get("log_meta", {})
    if meta.get("event_counter") is not None and meta["event_counter"] != len(ev):
        problems.append("log_meta.event_counter (%s) != distinct events (%d)"
                        % (meta["event_counter"], len(ev)))

    problems.extend(check_concentrations(log))
    problems.extend(check_missing_fields(log))
    problems.extend(check_param_types(log))
    problems.extend(check_ceiling_matches_high(log))
    problems.extend(check_reservoir_agreement(log))

    try:
        import copy
        recompute(copy.deepcopy(log))
    except ValueError as exc:
        problems.append(str(exc))
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="recompute derived fields and save")
    ap.add_argument("--path", default=LOG)
    args = ap.parse_args()

    log = load(args.path)
    if args.write:
        recompute(log)
        log.setdefault("log_meta", OrderedDict())["event_counter"] = len(unique_events(log))
        log["log_meta"]["next_event_id"] = "EVT-%05d" % (log["log_meta"]["event_counter"] + 1)
        save(log, args.path)
        print("recomputed derived fields")

    problems = validate(log)
    s = log.get("lineage_summary", {})
    print("nodes=%s edges=%s splits=%s merges=%s max_depth=%s events=%s"
          % (s.get("n_nodes"), s.get("n_edges"), s.get("n_splits"), s.get("n_merges"),
             s.get("max_depth"), len(unique_events(log))))
    if problems:
        print("\n%d problem(s):" % len(problems))
        for p in problems:
            print("  - " + p)
        return 1
    print("log is internally consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
