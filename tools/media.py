#!/usr/bin/env python3
"""Media consumption and depletion forecast for the OR05 evolution.

Reads evolution_log.json and reports, per reservoir: what was made, what is
left, how fast it is going, and when it runs out. Nothing here is written back
to the log — only reported measurements are treated as fact, and every figure
below that is inferred is labelled as such.

    python3 tools/media.py                 # report as of now
    python3 tools/media.py --at "2026-08-24T09:00:00-04:00"
    python3 tools/media.py --json          # machine-readable

Consumption is estimated two ways:
  measured  — from two or more level readings on the same reservoir
  inferred  — from a per-line rate measured elsewhere on the same media and
              role, scaled by how many lines this reservoir feeds

An inferred rate is a planning aid, not data. A reservoir with no reading and
no comparable rate is reported as unknown rather than guessed at.
"""
import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(os.path.dirname(HERE), "evolution_log.json")


def parse(ts):
    return datetime.fromisoformat(ts)


def hours(a, b):
    return (parse(a) - parse(b)).total_seconds() / 3600.0


def load(path):
    with open(path) as fh:
        return json.load(fh, object_pairs_hook=OrderedDict)


def readings_for(log, rid):
    """All level observations for a reservoir, oldest first: the prep itself
    counts as a reading of the full volume."""
    # A reservoir POSITION keeps its history when its composition changes and it
    # is given a new id. media_prep records the id it replaces, so follow that
    # chain backwards; without it a reformulation orphans everything before it
    # and the position looks brand new.
    aliases, cur = {rid}, rid
    for _ in range(20):
        prev = None
        for e in log.get("experiment_events", []):
            p = e.get("params", {})
            if e["event_type"] == "media_prep" and p.get("reservoir_id") == cur:
                rep = p.get("replaces")
                if rep and rep != cur and rep not in aliases:
                    prev = rep
        if not prev:
            break
        aliases.add(prev)
        cur = prev

    out = []
    for e in log.get("experiment_events", []):
        p = e.get("params", {})
        if p.get("reservoir_id") in aliases and p.get("reservoir_id") != rid:
            p = dict(p, reservoir_id=rid)   # treat an earlier name as this position
        # A swap names several positions in reservoir_ids and has no singular
        # reservoir_id, so it has to be matched before the filter below rather
        # than after it.
        if e["event_type"] == "reservoir_swap" and \
           any(a in (p.get("reservoir_ids") or []) for a in aliases):
            # the unit half of the id never changes, so the volume key is stable
            # even when the position has since been renamed
            key = "volume_to_%s" % rid.split("/")[0]
            if key in p:
                out.append((e["timestamp"], p[key]["value"], "measured", e["event_id"],
                            p.get("measurement_qualifier", "approximate")))
            continue
        if p.get("reservoir_id") != rid:
            continue
        if e["event_type"] == "media_prep":
            out.append((e["timestamp"], p["volume_prepared"]["value"], "prepared", e["event_id"], "exact"))
        elif e["event_type"] == "level_reading":
            out.append((e["timestamp"], p["volume_remaining"]["value"], p.get("level_source", "measured"),
                        e["event_id"], p.get("measurement_qualifier", "exact")))
    # A bottle that runs dry is read at 0.0 and refilled in the same minute, so
    # the reading and the media_prep carry the SAME timestamp. Sorting on the
    # timestamp alone left their order to however the events happened to sit in
    # experiment_events, which decides whether the refill or the empty bottle is
    # the baseline -- and measuring from the empty one spans the refill and
    # reports a rate that is confidently wrong rather than obviously broken.
    # Order it explicitly: at one instant, the reading comes first and the
    # refill that answers it comes last.
    out.sort(key=lambda r: (r[0], 1 if r[2] == "prepared" else 0))
    return out


MIN_SPAN_H = 2.0   # below this, a coarse level reading says more about the eye than the pumps


def baseline_reset(log, rid):
    """The most recent event that restarts consumption accounting for a reservoir.

    Two kinds qualify. A level reading the log marks as unrepresentative --
    media lost to a flood or a spill -- is not thrown away, it becomes the new
    baseline so the loss is not charged to the cultures. And any media_prep
    after the first is a refill or a bottle swap: the level jumps back up, and
    measuring across it would give a negative rate.

    Returns the event id of the latest such event, or None.
    """
    return (baseline_events(log, rid) or [None])[-1]


def baseline_events(log, rid):
    """Every baseline reset for a reservoir, oldest first.

    The list matters, not just the latest: the segment between the last two
    resets is the previous bottle, which is the best available guide to what a
    freshly refilled reservoir will do next.
    """
    known = {o[3] for o in readings_for(log, rid)}
    out, seen_prep = [], False
    # readings_for has already resolved this position's earlier names; anything
    # it accepted belongs to this position regardless of the id it carries
    for e in sorted(log.get("experiment_events", []), key=lambda x: (x["timestamp"], x["event_id"])):
        p = e.get("params", {})
        if e["event_id"] in known and p.get("reservoir_id") not in (rid, None):
            p = dict(p, reservoir_id=rid)   # an earlier name for this position
        # a swap names several positions at once
        if e["event_type"] == "reservoir_swap" and e["event_id"] in known:
            out.append(e["event_id"])
            continue
        if p.get("reservoir_id") != rid:
            continue
        if e["event_type"] == "media_prep":
            if seen_prep:
                out.append(e["event_id"])   # a refill: everything before it is a previous bottle
            seen_prep = True
        elif e["event_type"] == "level_reading":
            note = (e.get("notes") or "").lower()
            if "flood" in note or "not representative" in note or "must not be used" in note:
                out.append(e["event_id"])
    return out


def analyse(log, at):
    res = log["reservoirs"]["items"]
    rows = []

    # pass 1: measured rates
    for r in res:
        rid = r["id"]
        obs = readings_for(log, rid)
        reset = baseline_reset(log, rid)
        orphan = None
        if reset:
            i = next((k for k, o in enumerate(obs) if o[3] == reset), None)
            if i is None:
                # baseline_reset and readings_for disagree about what counts as
                # an observation. Defaulting to the full history here would
                # quietly measure across a refill and report a confident wrong
                # rate, so refuse instead.
                orphan = reset
                usable = []
            else:
                usable = obs[i:]      # the reset reading is the new starting point
        else:
            usable = obs
        rate = None          # L/h
        basis = "unknown"
        span = None
        bounded = False
        provisional = False

        # This reservoir's own rate on its PREVIOUS bottle. After a refill there
        # is nothing to measure yet, and the same position feeding the same
        # lines at a similar concentration is a far better guide than a rate
        # borrowed from another unit -- high-reservoir demand scales with vial
        # concentration, so a unit sitting at a different point on the ramp
        # gives a badly wrong answer.
        prior = None
        if reset and orphan is None:
            resets = baseline_events(log, rid)
            cur = next((k for k, o in enumerate(obs) if o[3] == reset), None)
            # the previous bottle runs from the reset before this one (or the
            # very first observation, if this is the first refill) up to the
            # last reading before this reset
            prev_reset = resets[-2] if len(resets) >= 2 else None
            start = 0 if prev_reset is None else next(
                (k for k, o in enumerate(obs) if o[3] == prev_reset), 0)
            seg = obs[start:cur] if cur is not None else []
            if len(seg) >= 2:
                pt0, pv0 = seg[0][0], seg[0][1]
                pt1, pv1 = seg[-1][0], seg[-1][1]
                pdt = hours(pt1, pt0)
                if pdt >= MIN_SPAN_H and pv0 > pv1:
                    prior = (pv0 - pv1) / pdt

        if len(usable) >= 2:
            (t0, v0, _, _, _), (t1, v1, _, _, q1) = usable[0], usable[-1]
            dt = hours(t1, t0)
            if dt > 0 and v0 > v1:
                rate = (v0 - v1) / dt
                basis = "measured"
                span = dt
                # '>750 mL' bounds the volume below, so it bounds consumption above
                bounded = (q1 == "at_least")
                provisional = dt < MIN_SPAN_H
        rows.append(OrderedDict([
            ("id", rid), ("unit", r["unit"]), ("media", r["media"]), ("role", r["role"]),
            ("pg_g_per_L", r["pg"]["value_g_per_L"]), ("status", r["status"]),
            ("prepared_L", r["volume_prepared"]["value"]), ("prepared_at", r["prepared_at"]),
            ("n_lines", len(r.get("lines_fed") or [])),
            ("lines_fed", r.get("lines_fed") or []),
            ("level_L", (r["current_volume"] or {}).get("value")),
            ("level_as_of", r["level_as_of"]), ("level_source", r["level_source"]),
            ("rate_L_per_h", rate), ("rate_basis", basis), ("rate_span_h", span),
            ("rate_is_upper_bound", bounded), ("rate_provisional", provisional),
            ("baseline_reset_at", reset),
            ("baseline_orphaned", orphan),
            ("prior_rate_L_per_h", prior),
        ]))

    # pass 2: per-line rates observed anywhere, keyed by (media, role).
    # Provisional rates (too short a window) are excluded from the pool so they
    # cannot contaminate forecasts for other reservoirs.
    perline = {}
    for row in rows:
        if row["rate_basis"] == "measured" and row["n_lines"] and not row["rate_provisional"]:
            perline.setdefault((row["media"], row["role"]), []).append(row["rate_L_per_h"] / row["n_lines"])
    perline = {k: sum(v) / len(v) for k, v in perline.items()}

    # Retired reservoirs are deliberately NOT used to seed the per-line pool.
    # An earlier version recovered their rates via the replacement's line count,
    # which put a four-day-old figure from a bottle retired on 23 Aug into the
    # pool as the only LB low rate available -- and from there into the bound
    # that drew an unrelated M9 reservoir to zero. A position's own history now
    # reaches it through readings_for's alias chain and the prior-bottle rate,
    # which is both more recent and specific to that position.

    # Fastest per-line draw seen anywhere for each role, used as an upper bound
    # for reservoirs never measured, so "no data" does not read as "no risk".
    #
    # This used to apply to low reservoirs only, on the theory that the high
    # ones are barely touched until the ramp starts. Reading the controller
    # showed that to be wrong: pumpcontrol_ramp dispenses a fixed 10 mL per
    # cycle split between the two pumps, and the minimum hardware step draws
    # 0.5 mL from the high pump even at zero concentration. High reservoirs are
    # consumed from the first dilution onward, so they are bounded too.
    fastest = {}
    for (m, role), v in perline.items():
        fastest[role] = max(fastest.get(role, 0.0), v)

    # pass 3: infer where nothing was measured, then project
    for row in rows:
        if row["status"] != "active":
            row["projection"] = None
            continue
        if row["rate_L_per_h"] is None and row.get("prior_rate_L_per_h"):
            # same position, same lines, most recent comparable conditions
            row["rate_L_per_h"] = row["prior_rate_L_per_h"]
            row["rate_basis"] = "prior_bottle"
        if row["rate_L_per_h"] is None:
            pl = perline.get((row["media"], row["role"]))
            if pl is not None and row["n_lines"]:
                row["rate_L_per_h"] = pl * row["n_lines"]
                row["rate_basis"] = "inferred"
            elif fastest.get(row["role"]) and row["n_lines"]:
                row["rate_L_per_h"] = fastest[row["role"]] * row["n_lines"]
                row["rate_basis"] = "upper_bound"

        lvl, rate = row["level_L"], row["rate_L_per_h"]
        if lvl is None or rate is None or rate <= 0:
            row["projection"] = None
            continue
        # draw down from the last known level to the requested moment
        elapsed = hours(at, row["level_as_of"])
        now_lvl = max(lvl - rate * elapsed, 0.0)
        h_left = now_lvl / rate if rate > 0 else None
        row["estimated_now_L"] = round(now_lvl, 3)
        row["projection"] = OrderedDict([
            ("hours_remaining", round(h_left, 1)),
            ("empty_at", (parse(at) + timedelta(hours=h_left)).isoformat()),
            ("basis", row["rate_basis"]),
        ])
    return rows, perline


def dose_estimates(log, rows):
    """Time-averaged PG concentration actually delivered to each group of lines.

    Derived from measured per-reservoir RATES rather than from raw volumes.
    Volumes were the obvious approach and were wrong: each reservoir has its own
    baseline, so after one bottle is refilled its "volume drawn since baseline"
    resets to zero while its neighbour's keeps accumulating. Dividing one by the
    other then reports the group as though it were drinking nothing but high
    media. Rates are per-hour and therefore comparable across reservoirs whose
    measurement windows do not line up.

    A group needs a measured, non-provisional rate for at least one low and one
    high reservoir. Anything less is reported as indeterminate rather than
    estimated, because a missing side biases the answer in a known direction and
    a number would be read as if it did not.

    Bounds propagate: a rate derived from an 'at least' reading is an upper
    bound, so if only the high side is bounded the mean is an upper bound, if
    only the low side is it is a lower bound, and if both are it is
    indeterminate.
    """
    groups = OrderedDict()
    for r in rows:
        if r["status"] != "active":
            continue
        if r["rate_basis"] != "measured" or r["rate_provisional"] or not r["rate_L_per_h"]:
            continue
        g = groups.setdefault((r["unit"], r["media"]), {
            "num": 0.0, "den": 0.0, "roles": set(), "hi_bounded": False,
            "lo_bounded": False, "span": [], "lines": set()})
        g["num"] += r["rate_L_per_h"] * r["pg_g_per_L"]
        g["den"] += r["rate_L_per_h"]
        g["roles"].add(r["role"])
        if r["rate_is_upper_bound"]:
            g["hi_bounded" if r["role"] == "high" else "lo_bounded"] = True
        g["span"].append(r["rate_span_h"] or 0.0)
        g["lines"] |= set(r["lines_fed"])

    out = []
    for (unit, media), g in groups.items():
        if g["den"] <= 0:
            continue
        missing = {"low", "high"} - g["roles"]
        if missing:
            out.append(OrderedDict([
                ("unit", unit), ("media", media), ("n_lines", len(g["lines"])),
                ("direction", "indeterminate"),
                ("reason", "no measured rate for the %s reservoir" % ", ".join(sorted(missing))),
                ("mean_pg_g_per_L", None), ("span_h", round(min(g["span"]), 2) if g["span"] else 0.0),
                ("provisional", False)]))
            continue
        if g["hi_bounded"] and g["lo_bounded"]:
            direction = "indeterminate"
        elif g["hi_bounded"]:
            direction = "at_most"
        elif g["lo_bounded"]:
            direction = "at_least"
        else:
            direction = "about"
        out.append(OrderedDict([
            ("unit", unit), ("media", media), ("n_lines", len(g["lines"])),
            ("mean_pg_g_per_L", round(g["num"] / g["den"], 3)),
            ("direction", direction),
            ("span_h", round(min(g["span"]), 2)),
            ("total_rate_mL_per_h", round(g["den"] * 1000, 1)),
            ("provisional", False)]))
    return out


def high_media_outlook(log, rows, perline):
    """How long the high reservoirs last as the ramp climbs.

    pumpcontrol_ramp dispenses a fixed 10 mL per dilution cycle, always: only
    the split between the low and high pumps changes. So a line's TOTAL media
    draw depends on how often it dilutes, not on its PG concentration, while
    the high share tracks roughly (c - c_low) / (c_high - c_low).

    That makes high-reservoir demand rise steeply over a run. Forecasting it
    from today's measured rate alone would understate it badly, so this
    projects forward using the invariant instead.
    """
    out = []
    for r in rows:
        if r["status"] != "active" or r["role"] != "high" or not r["n_lines"]:
            continue
        media, ch = r["media"], r["pg_g_per_L"]
        lo = next((x for x in rows if x["unit"] == r["unit"] and x["media"] == media
                   and x["role"] == "low" and x["status"] == "active"), None)
        per_low = perline.get((media, "low"))
        per_high = perline.get((media, "high"))
        if per_low is None or per_high is None:
            continue
        total_per_line = per_low + per_high          # invariant per dilution cycle
        cl = lo["pg_g_per_L"] if lo else 0.0
        scen = []
        for c in (1.0, 2.0, 3.0, 4.0, 4.5):
            if c <= cl or ch <= cl:
                continue
            frac = min((c - cl) / (ch - cl), 1.0)
            rate = total_per_line * frac * r["n_lines"]
            lvl = r.get("estimated_now_L", r["level_L"]) or 0.0
            scen.append(OrderedDict([("vial_pg_g_per_L", c), ("high_fraction", round(frac, 3)),
                                     ("rate_L_per_h", rate),
                                     ("hours_at_current_level", round(lvl / rate, 1) if rate > 0 else None)]))
        out.append(OrderedDict([
            ("id", r["id"]), ("n_lines", r["n_lines"]),
            ("total_per_line_mL_per_h", round(total_per_line * 1000, 1)),
            ("level_L", r.get("estimated_now_L", r["level_L"])),
            ("scenarios", scen)]))
    return out


def fmt_rate(row):
    r = row["rate_L_per_h"]
    if r is None:
        return "—"
    s = "%.0f mL/h" % (r * 1000)
    if row.get("rate_is_upper_bound"):
        s = "≤" + s
    if row.get("rate_provisional"):
        s += "?"
    return s


def report(rows, perline, doses, outlook, at):
    print("Media status as of %s\n" % at)
    hdr = "%-18s %-8s %6s %8s %10s %10s %9s" % ("reservoir", "status", "lines", "made", "level", "rate", "empty in")
    print(hdr)
    print("-" * len(hdr))
    warn = []
    for r in rows:
        lvl = r.get("estimated_now_L", r["level_L"])
        lvl_s = "—" if lvl is None else "%.2f L" % lvl
        if r["level_source"] == "prepared" and r["status"] == "active":
            lvl_s += "*"
        proj = r["projection"]
        left = "—"
        if proj:
            h = proj["hours_remaining"]
            left = ("%.0f h" % h) if h < 48 else ("%.1f d" % (h / 24))
            left += {"inferred": "~", "upper_bound": " (≤)", "prior_bottle": "'"}.get(proj["basis"], "")
        print("%-18s %-8s %6s %8.2f %10s %10s %9s" % (
            r["id"], r["status"], r["n_lines"] or "—", r["prepared_L"], lvl_s, fmt_rate(r), left))

        if r["status"] != "active":
            continue
        if proj and proj["basis"] == "upper_bound":
            h = proj["hours_remaining"]
            if h <= 0:
                warn.append("%s: at the fastest per-line rate observed it would ALREADY be empty. Never "
                            "measured — check the bottle." % r["id"])
            elif h < 24:
                warn.append("%s: could be empty within %.0f h at the fastest observed rate. Never measured — "
                            "check the bottle." % (r["id"], h))
        elif proj and r.get("rate_provisional"):
            warn.append("%s: forecast of %.0f h rests on a %.0f-min window, which is too short to trust. "
                        "Re-read it in a couple of hours." % (r["id"], proj["hours_remaining"],
                                                              r["rate_span_h"] * 60))
        elif proj and proj["hours_remaining"] < 24:
            warn.append("%s runs out in %.0f h (%s)" % (r["id"], proj["hours_remaining"], proj["basis"]))

        if r.get("baseline_orphaned"):
            warn.append("%s: internal error -- baseline event %s is not among its observations, so no rate "
                        "was derived. Report this rather than trusting the display."
                        % (r["id"], r["baseline_orphaned"]))
        if r["rate_L_per_h"] is None:
            if r["baseline_reset_at"] and not r.get("baseline_orphaned"):
                warn.append("%s: consumption is measured from its post-incident baseline only, and there is "
                            "not yet a later reading to measure against. One more reading gives it a rate."
                            % r["id"])
            elif r["role"] == "high":
                warn.append("%s: never measured. Draw should stay near zero until the PG ramp starts, so this "
                            "is low risk for now — but it becomes the binding constraint once dosing begins."
                            % r["id"])
            else:
                warn.append("%s: never measured and no comparable rate exists — consumption unknown." % r["id"])

    print("\n* level is the prepared volume; nothing has been measured since.")
    print("' rate carried over from this reservoir's own previous bottle, which had not been")
    print("  refilled long enough ago to measure yet.")
    print("~ depletion inferred from a per-line rate measured on a different reservoir.")
    print("≤ rate derived from an 'at least' reading, so it is an upper bound: the real rate is this or")
    print("  slower, and the time remaining is this or longer.")
    print("? rate measured over less than %.0f h — too short a window to trust." % MIN_SPAN_H)
    print("(≤) no reading at all: the fastest per-line rate seen anywhere is applied as a bound.")

    if perline:
        print("\nPer-line consumption actually measured:")
        for (media, role), v in sorted(perline.items()):
            print("  %s %-4s  %.0f mL/h per line" % (media, role, v * 1000))

    if doses:
        print("\nPG actually delivered, from the ratio of high to low media drawn:")
        sym = {"about": "~", "at_most": "≤", "at_least": "≥", "indeterminate": "?"}
        for d in doses:
            if d["provisional"]:
                print("  %-8s %-3s  window only %.0f min — too short to estimate"
                      % (d["unit"], d["media"], d["span_h"] * 60))
            elif d["direction"] == "indeterminate":
                print("  %-8s %-3s  indeterminate: %s"
                      % (d["unit"], d["media"], d.get("reason", "both sides are upper bounds")))
            else:
                print("  %-8s %-3s  %s%.2f g/L mean over %.1f h  (%d lines, %.0f mL/h total)"
                      % (d["unit"], d["media"], sym[d["direction"]], d["mean_pg_g_per_L"],
                         d["span_h"], d["n_lines"], d["total_rate_mL_per_h"]))
        print("  This is the mean concentration of media pumped in, not the concentration in any")
        print("  vessel at any moment. At hold it runs about 91-97% of the vial concentration, so")
        print("  divide by ~0.93 for a rough vial figure -- a floor, not a substitute for the")
        print("  controller's drugconc record.")

    if outlook:
        print("\nHigh-reservoir headroom as the ramp climbs.")
        print("Total draw per cycle is a fixed 10 mL; only the low/high split moves, so high")
        print("demand rises with vial concentration while total demand does not.")
        for o in outlook:
            print("  %-18s %d lines, %.0f mL/h total per line, %.2f L in the bottle"
                  % (o["id"], o["n_lines"], o["total_per_line_mL_per_h"], o["level_L"] or 0))
            for s2 in o["scenarios"]:
                print("      at %.1f g/L in the vial: %2.0f%% high, %5.0f mL/h -> %s"
                      % (s2["vial_pg_g_per_L"], 100*s2["high_fraction"], s2["rate_L_per_h"]*1000,
                         ("%.0f h of supply" % s2["hours_at_current_level"])
                         if s2["hours_at_current_level"] is not None else "-"))

    if warn:
        print("\nAttention:")
        for w in warn:
            print("  - " + w)
    return warn


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", help="ISO timestamp to report as of (default: log's last_updated)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--path", default=LOG)
    args = ap.parse_args()

    log = load(args.path)
    at = args.at or log["log_meta"]["last_updated"]
    rows, perline = analyse(log, at)

    if args.json:
        json.dump(OrderedDict([("as_of", at), ("reservoirs", rows),
                               ("per_line_rates_L_per_h", {"%s/%s" % k: v for k, v in perline.items()}),
                               ("delivered_pg", dose_estimates(log, rows)),
                               ("high_media_outlook", high_media_outlook(log, rows, perline))]),
                  sys.stdout, indent=2)
        print()
        return 0
    doses = dose_estimates(log, rows)
    outlook = high_media_outlook(log, rows, perline)
    report(rows, perline, doses, outlook, at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
