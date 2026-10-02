#!/usr/bin/env python3
"""Build a synthetic log containing a split, a merge, PG steps and a media switch,
so the viewer's derivation logic can be tested against a branched pedigree."""
import copy, json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from collections import OrderedDict

# The reference log, not this repo's own: these checks mutate or measure a
# RICH real log, and this repo's evolution_log.json belongs to whatever
# experiment it was initialised for -- possibly days old, possibly absent.
LOG = os.path.join(ROOT, "reference", "or05_log.json")
OUT = "/tmp/fixture.json"
PG_MM = 126.11


def conc(g):
    return OrderedDict([("value_g_per_L", g), ("value_mM", round(g/PG_MM*1000, 4)), ("unit_primary", "g/L")])


log = json.load(open(LOG), object_pairs_hook=OrderedDict)
lines = log["lines"]
n = [log["log_meta"]["event_counter"]]  # continue the real log's numbering


def ev(etype, ts, params, notes="", op="AJ"):
    n[0] += 1
    return OrderedDict([("event_id", "EVT-%05d" % n[0]), ("timestamp", ts), ("elapsed_h", None),
                        ("event_type", etype), ("operator", op), ("params", params), ("notes", notes)])


def new_line(lid, proto, parents, created_at, created_by):
    L = copy.deepcopy(lines[proto])
    L["line_id"] = lid
    L["events"] = []
    L["lineage"] = OrderedDict([("parents", parents), ("children", []), ("roots", []), ("depth", 0),
                                ("is_founder", False), ("created_by_event", created_by),
                                ("created_at", created_at), ("terminated_by_event", None),
                                ("terminated_at", None)])
    L["t0"] = created_at
    return L


T1 = "2026-09-01T10:00:00-04:00"   # phenotyping + split
T2 = "2026-09-10T14:00:00-04:00"   # pg step on one child
T3 = "2026-09-15T09:00:00-04:00"   # media switch on a plankton line
T4 = "2026-09-20T11:00:00-04:00"   # merge

# --- PG step + media switch on founders (exercise segment building) ---
lines["patrick-v05"]["events"].append(ev("pg_change", T2,
    OrderedDict([("pg_low", conc(0.0)), ("pg_high", conc(5.0)), ("pg_target", conc(0.15))]),
    "Ramp advanced three 0.05 g/L steps."))
lines["plankton-v03"]["events"].append(ev("media_switch", T3,
    OrderedDict([("media_from", "LB"), ("media_to", "M9")]), "Scheduled background switch."))
lines["plankton-v03"]["current_media"] = "M9"

# --- split patrick-v04 into two children after phenotyping ---
lines["patrick-v04"]["events"].append(ev("phenotyping", T1,
    OrderedDict([("phenotype_assay", "MIC"), ("mic", conc(1.2))]), "Divergent colonies observed."))
sp = ev("split", T1, OrderedDict([("parent_line_ids", ["patrick-v04"]),
                                  ("child_line_ids", ["patrick-v04.a", "patrick-v04.b"]),
                                  ("split_reason", "divergent MIC among colonies")]),
        "Split into two sublines.")
lines["patrick-v04"]["events"].append(sp)
lines["patrick-v04"]["status"] = "ended"
lines["patrick-v04"]["lineage"]["terminated_at"] = T1
lines["patrick-v04"]["lineage"]["terminated_by_event"] = sp["event_id"]

for suf, vial in (("a", 1), ("b", 2)):
    lid = "patrick-v04." + suf
    L = new_line(lid, "patrick-v04", ["patrick-v04"], T1, sp["event_id"])
    L["vial"] = vial
    L["status"] = "active"
    L["events"].append(ev("inoculation", T1,
        OrderedDict([("media", "LB"), ("pg_low", conc(0.0)), ("pg_high", conc(5.0)),
                     ("pg_target", conc(0.2 if suf == "a" else 0.1))]),
        "Seeded from parent split."))
    lines[lid] = L

# --- merge two plankton lines ---
# Convention: the merge event is recorded once, on the child it creates. Each
# parent gets its own termination event pointing at it, so no event object is
# duplicated across lines.
mg = ev("merge", T4, OrderedDict([("parent_line_ids", ["plankton-v03", "plankton-v04"]),
                                  ("child_line_ids", ["plankton-v03+v04"]),
                                  ("merge_reason", "equivalent phenotype, pooled to save channels")]),
        "Pooled equal volumes.")
for p in ("plankton-v03", "plankton-v04"):
    term = ev("termination", T4, OrderedDict([("termination_reason", "merge"),
                                              ("child_line_ids", ["plankton-v03+v04"])]),
              "Line ends here; continues as plankton-v03+v04 (%s)." % mg["event_id"])
    lines[p]["events"].append(term)
    lines[p]["status"] = "ended"
    lines[p]["lineage"]["terminated_at"] = T4
    lines[p]["lineage"]["terminated_by_event"] = term["event_id"]
M = new_line("plankton-v03+v04", "plankton-v03", ["plankton-v03", "plankton-v04"], T4, mg["event_id"])
M["vial"] = 3
M["status"] = "active"
M["events"].append(mg)
M["events"].append(ev("inoculation", T4, OrderedDict([("media", "M9"), ("pg_low", conc(0.0)),
                                                      ("pg_high", conc(5.0))]), "Pooled culture."))
lines["plankton-v03+v04"] = M

# --- recompute derived lineage fields (same logic as the maintainer script) ---
for L in lines.values():
    L["lineage"]["children"] = []
for lid, L in lines.items():
    for p in L["lineage"]["parents"]:
        if p in lines and lid not in lines[p]["lineage"]["children"]:
            lines[p]["lineage"]["children"].append(lid)

memo_r, memo_d = {}, {}
def roots(lid, seen=frozenset()):
    if lid in memo_r: return memo_r[lid]
    if lid in seen: raise ValueError("cycle at " + lid)
    ps = [p for p in lines[lid]["lineage"]["parents"] if p in lines]
    r = [lid] if not ps else []
    for p in ps:
        for x in roots(p, seen | {lid}):
            if x not in r: r.append(x)
    memo_r[lid] = r
    return r
def depth(lid, seen=frozenset()):
    if lid in memo_d: return memo_d[lid]
    if lid in seen: raise ValueError("cycle at " + lid)
    ps = [p for p in lines[lid]["lineage"]["parents"] if p in lines]
    d = 0 if not ps else 1 + max(depth(p, seen | {lid}) for p in ps)
    memo_d[lid] = d
    return d

n_edges = 0
for lid, L in lines.items():
    L["lineage"]["roots"] = roots(lid)
    L["lineage"]["depth"] = depth(lid)
    L["lineage"]["is_founder"] = not L["lineage"]["parents"]
    n_edges += len(L["lineage"]["parents"])

allev = list({e["event_id"]: e for L in lines.values() for e in L["events"]}.values())
log["lineage_summary"].update({
    "founders": sorted(l for l, L in lines.items() if L["lineage"]["is_founder"]),
    "n_nodes": len(lines), "n_edges": n_edges,
    "max_depth": max(L["lineage"]["depth"] for L in lines.values()),
    "n_splits": sum(1 for e in allev if e["event_type"] == "split"),
    "n_merges": sum(1 for e in allev if e["event_type"] == "merge"),
})

json.dump(log, open(OUT, "w"), indent=2)
print("fixture -> %s  nodes=%d edges=%d splits=%d merges=%d depth=%d" % (
    OUT, len(lines), n_edges, log["lineage_summary"]["n_splits"],
    log["lineage_summary"]["n_merges"], log["lineage_summary"]["max_depth"]))
