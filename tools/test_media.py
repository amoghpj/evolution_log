#!/usr/bin/env python3
"""Regression tests for the media consumption model.

Run: python3 tools/test_media.py

Each test here exists because the corresponding bug shipped once. The failure
mode they guard against is the dangerous one: not a crash, but a confident
number that happens to be wrong.
"""
import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import media  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    # The reference log, not this repo's own: these checks mutate or measure a
    # RICH real log, and this repo's evolution_log.json belongs to whatever
    # experiment it was initialised for -- possibly days old, possibly absent.
    log = media.load(os.path.join(ROOT, "reference", "or05_log.json"))
    at = log["log_meta"]["last_updated"]
    rows, perline = media.analyse(log, at)
    by = {r["id"]: r for r in rows}

    # ── a swap names positions in reservoir_ids, not reservoir_id ────────────
    # Bug: readings_for filtered on reservoir_id first, so swaps were skipped
    # for both positions while baseline_reset still pointed at them.
    # A position can be renamed when its composition changes, so a swap's
    # reservoir_ids may name ids that no longer exist. Resolve each to whichever
    # CURRENT reservoir inherits that history through the alias chain.
    def current_holder(old_id):
        for r in rows:
            if r["id"] == old_id:
                return old_id
            if any(o[3] for o in media.readings_for(log, r["id"])) and \
               old_id.split("/")[0] == r["id"].split("/")[0]:
                obs_ids = {o[3] for o in media.readings_for(log, r["id"])}
                if any(e["event_id"] in obs_ids for e in log["experiment_events"]
                       if e.get("params", {}).get("reservoir_id") == old_id):
                    return r["id"]
        return None

    swaps = [e for e in log["experiment_events"] if e["event_type"] == "reservoir_swap"]
    for e in swaps:
        for rid in e["params"]["reservoir_ids"]:
            holder = current_holder(rid)
            ck(holder is not None, "swap %s: position %s still resolves to a reservoir" % (e["event_id"], rid))
            if holder:
                obs = media.readings_for(log, holder)
                ck(any(o[3] == e["event_id"] for o in obs),
                   "swap %s survives in %s's history%s" % (e["event_id"], holder,
                                                           "" if holder == rid else " (renamed from %s)" % rid))

    # ── a baseline must be locatable, or no rate may claim to be measured ────
    # Bug: the index lookup defaulted to 0 when it could not find the baseline,
    # silently measuring across a refill and reporting it as measured.
    ck(all(not r.get("baseline_orphaned") for r in rows),
       "every baseline event is present in its own observation list")

    # A swap that carries no volume for this position no longer claims to be a
    # baseline at all, which is safer than becoming an orphaned one. What must
    # still hold is the property the orphan guard existed to protect: no rate is
    # ever measured ACROSS a refill.
    bad = copy.deepcopy(log)
    for e in bad["experiment_events"]:
        if e["event_type"] == "reservoir_swap":
            e["params"].pop("volume_to_plankton", None)
    brows = {r["id"]: r for r in media.analyse(bad, at)[0]}
    for rid, r in brows.items():
        if r["rate_basis"] != "measured":
            continue
        obs = media.readings_for(bad, rid)
        # Slice from the baseline BY INDEX, the way analyse() does. Selecting on
        # o[0] >= base[0] instead pulls in anything sharing the baseline's
        # timestamp -- and a bottle read at 0.0 and refilled in the same minute
        # is exactly that case, so the refill landed inside the window and this
        # test reported a break that was not there.
        i = next((k for k, o in enumerate(obs) if o[3] == r["baseline_reset_at"]), None) \
            if r["baseline_reset_at"] else None
        after = obs[i:] if i is not None else obs
        preps = [o for o in after[1:] if o[2] == "prepared"]
        ck(not preps,
           "degraded log: %s does not measure across a refill (%d prep(s) inside its window)"
           % (rid, len(preps)))

    # ── rates must be measured from the current bottle only ─────────────────
    for rid, r in by.items():
        if r["rate_basis"] != "measured" or not r["baseline_reset_at"]:
            continue
        obs = media.readings_for(log, rid)
        base = next((o for o in obs if o[3] == r["baseline_reset_at"]), None)
        if base:
            ck(abs(media.hours(r["level_as_of"], base[0]) - r["rate_span_h"]) < 0.2,
               "%s measures from its baseline, not across it" % rid)

    # ── no rate may be negative, and none may exceed what was in the bottle ──
    for rid, r in by.items():
        if r["rate_L_per_h"] is not None:
            ck(r["rate_L_per_h"] >= 0, "%s rate is not negative" % rid)
        if r["level_L"] is not None:
            ck(r["level_L"] <= r["prepared_L"] + 1e-9,
               "%s level does not exceed its prepared volume" % rid)

    # ── delivered PG: derived from rates, never from mismatched volumes ──────
    # Bug: volumes were summed per reservoir, each with its own baseline, so a
    # freshly refilled low reservoir contributed zero and the group looked as
    # though it drank pure high media -- reported as a confident 5.00 g/L.
    doses = media.dose_estimates(log, rows)
    for d in doses:
        if d["mean_pg_g_per_L"] is None:
            ck(d["direction"] == "indeterminate",
               "%s %s: no estimate means indeterminate, with a reason" % (d["unit"], d["media"]))
            ck(bool(d.get("reason")), "%s %s: indeterminate carries a reason" % (d["unit"], d["media"]))
        else:
            highs = [r["pg_g_per_L"] for r in rows
                     if r["unit"] == d["unit"] and r["media"] == d["media"] and r["role"] == "high"]
            lows = [r["pg_g_per_L"] for r in rows
                    if r["unit"] == d["unit"] and r["media"] == d["media"] and r["role"] == "low"]
            ck(d["mean_pg_g_per_L"] <= max(highs) + 1e-9,
               "%s %s delivered (%.2f) cannot exceed the high reservoir (%.2f)"
               % (d["unit"], d["media"], d["mean_pg_g_per_L"], max(highs)))
            ck(d["mean_pg_g_per_L"] >= min(lows) - 1e-9,
               "%s %s delivered (%.2f) cannot fall below the low reservoir (%.2f)"
               % (d["unit"], d["media"], d["mean_pg_g_per_L"], min(lows)))

    # a group with only one side measured must never produce a number
    for d in doses:
        if d.get("reason", "").startswith("no measured rate"):
            ck(d["mean_pg_g_per_L"] is None,
               "%s %s: one-sided data yields no estimate" % (d["unit"], d["media"]))

    # ── high reservoirs are never bounded off a low-reservoir rate ───────────
    for rid, r in by.items():
        if r["role"] == "high":
            ck(r["rate_basis"] != "upper_bound" or perline.get((r["media"], "high")) is not None,
               "%s: a high reservoir is not bounded using low-reservoir demand" % rid)

    # ── every active line's reservoirs exist and are active ─────────────────
    for lid, L in log["lines"].items():
        if L["status"] != "active":
            continue
        for role in ("low", "high"):
            rid = L["reservoirs"][role]
            ck(rid in by and by[rid]["status"] == "active",
               "%s draws from an active reservoir (%s)" % (lid, rid))

    # ── a refilled reservoir uses its OWN previous bottle, not another unit's ──
    # Bug: after a refill the rate fell back to a cross-reservoir per-line
    # average. plankton/LB-5 borrowed patrick's single high-concentration vial
    # and reported 53 mL/h against its own measured 27, halving the forecast.
    for rid, r in by.items():
        if r["rate_basis"] != "prior_bottle":
            continue
        resets = media.baseline_events(log, rid)
        obs = media.readings_for(log, rid)
        cur = next((k for k, o in enumerate(obs) if o[3] == resets[-1]), None)
        prev = resets[-2] if len(resets) >= 2 else None
        start = 0 if prev is None else next((k for k, o in enumerate(obs) if o[3] == prev), 0)
        seg = obs[start:cur]
        ck(len(seg) >= 2, "%s prior rate comes from a real segment of its previous bottle" % rid)
        if len(seg) >= 2:
            ck(seg[0][1] > seg[-1][1],
               "%s prior segment falls rather than rises" % rid)
            ck(media.hours(seg[-1][0], seg[0][0]) >= media.MIN_SPAN_H,
               "%s prior segment is longer than the minimum window" % rid)
            expect = (seg[0][1] - seg[-1][1]) / media.hours(seg[-1][0], seg[0][0])
            ck(abs(r["rate_L_per_h"] - expect) < 1e-9,
               "%s prior rate equals its own previous bottle's rate (%.0f mL/h)"
               % (rid, expect * 1000))
        # and it must not have silently used another reservoir's number
        pl = perline.get((r["media"], r["role"]))
        if pl and r["n_lines"]:
            cross = pl * r["n_lines"]
            ck(abs(r["rate_L_per_h"] - cross) > 1e-9 or abs(cross - r["prior_rate_L_per_h"]) < 1e-9,
               "%s did not fall through to the cross-reservoir average" % rid)

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
