#!/usr/bin/env python3
"""Compute generations accumulated under a finished eVOLVER run, so they survive a restart.

Starting a fresh experiment resets the controller clock and the pump log, so
the live API can only ever count the CURRENT run. Everything before it lives in
the old experiment directory, which is still on disk. This reads that directory
and writes the totals into evolution_log.json as generations_carried_forward,
after which the viewer adds them to whatever it counts live.

Run it on the machine holding the experiment directories:

    python3 tools/carry_generations.py --exp-dir /path/to/or05_phase2 \\
                                       --unit patrick --pump-cal /path/to/pump_cal.json

    # check first, write second
    python3 tools/carry_generations.py ... --write

Generations use the same arithmetic as the viewer: a dispense of v mL into a
vial of V mL forces the culture to regrow log2((V+v)/V) before the next one.
Windows the log marks as anomalous for a line are excluded, exactly as they are
live, so a restart does not quietly readmit dosing the log has already
disowned.
"""
import argparse
import json
import math
import os
import sys
from collections import OrderedDict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = os.path.join(ROOT, "evolution_log.json")


def parse(ts):
    return datetime.fromisoformat(ts)


def read_pump(exp_dir, vial):
    path = os.path.join(exp_dir, "pump_log", "vial%d_pump_log.txt" % vial)
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        next(fh, None)
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) < 3:
                continue
            try:
                out.append((float(parts[0]), float(parts[1]), parts[2]))
            except ValueError:
                continue
    return out


def anomaly_windows(line):
    """Same rule the viewer uses: an anomaly's window is not trustworthy."""
    out = []
    for e in line.get("events", []):
        if e.get("event_type") != "anomaly":
            continue
        p = e.get("params", {})
        t0 = parse(e["timestamp"])
        if p.get("resolved_at"):
            t1 = parse(p["resolved_at"])
        elif p.get("duration", {}).get("unit") == "h":
            t1 = t0 + __import__("datetime").timedelta(hours=p["duration"]["value"])
        elif line["lineage"].get("terminated_at"):
            t1 = parse(line["lineage"]["terminated_at"])
        else:
            continue
        if t1 > t0:
            out.append((t0, t1))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exp-dir", required=True, help="the FINISHED experiment directory")
    ap.add_argument("--unit", required=True, help="patrick or plankton")
    ap.add_argument("--pump-cal", default="pump_cal.json")
    ap.add_argument("--run-start", help="wall clock of that run's t=0, ISO 8601. Without it, "
                                        "anomaly windows cannot be applied and nothing is excluded.")
    ap.add_argument("--write", action="store_true", help="write into evolution_log.json")
    ap.add_argument("--path", default=LOG)
    args = ap.parse_args()

    if not os.path.isdir(args.exp_dir):
        print("no such directory: %s" % args.exp_dir)
        return 2
    if not os.path.exists(args.pump_cal):
        print("no pump_cal.json at %s -- volumes cannot be computed" % args.pump_cal)
        return 2
    coefs = json.load(open(args.pump_cal))["coefficients"]

    log = json.load(open(args.path), object_pairs_hook=OrderedDict)
    t0 = parse(args.run_start) if args.run_start else None

    print("%-18s %6s %10s %12s %10s" % ("line", "vial", "dispenses", "generations", "excluded"))
    print("-" * 62)
    results = {}
    for lid, L in log["lines"].items():
        if L["unit"] != args.unit:
            continue
        vial = L["vial"]
        pump = read_pump(args.exp_dir, vial)
        if not pump:
            continue
        V = 22.0
        p2 = L.get("input_pump2")
        bad = anomaly_windows(L) if t0 else []
        # a line only owns the part of its vial's log that falls in its own life
        lo = parse(L["lineage"].get("created_at") or L["t0"])
        hi = parse(L["lineage"]["terminated_at"]) if L["lineage"].get("terminated_at") else None

        gens, counted, skipped = 0.0, 0, 0
        for th, timein, which in pump:
            coef = coefs[vial] if which == "in1" else (coefs[int(p2)] if p2 is not None else coefs[vial])
            mL = timein * coef
            if mL <= 0:
                continue
            if t0:
                when = t0 + __import__("datetime").timedelta(hours=th)
                if when < lo or (hi and when > hi):
                    continue
                if any(a <= when <= b for a, b in bad):
                    skipped += 1
                    continue
            gens += math.log2((V + mL) / V)
            counted += 1
        if counted:
            results[lid] = (gens, counted, skipped)
            print("%-18s %6d %10d %12.1f %10d" % (lid, vial, counted, gens, skipped))

    if not results:
        print("nothing found -- check --exp-dir points at the run's own directory")
        return 1
    if not t0:
        print("\nNOTE: --run-start not given, so no anomaly window or occupancy clipping was applied.")
        print("Totals above are for the whole vial log, not for each line's own life.")

    if args.write:
        for lid, (g, c, s) in results.items():
            L = log["lines"][lid]
            prev = (L.get("generations_carried_forward") or {}).get("value", 0.0)
            L["generations_carried_forward"] = OrderedDict([
                ("value", round(prev + g, 3)),
                ("unit", "generations"),
                ("from_run", os.path.basename(args.exp_dir.rstrip("/"))),
                ("dispenses", c),
                ("excluded_dispenses", s),
                ("clipped_to_line_lifetime", bool(t0)),
                ("note", "Accumulated under a previous controller run whose pump log the live API can no "
                         "longer see. The viewer adds this to what it counts in the current run."),
            ])
        # isoformat, not strftime("%z"): %z emits -0400, and the schema requires
        # the colon (-04:00) since SERVER_DESIGN.md Phase 1 #7. This tool predates
        # that decision, so --write used to leave the log failing validate_schema.py.
        log["log_meta"]["last_updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        with open(args.path, "w") as fh:
            json.dump(log, fh, indent=2)
            fh.write("\n")
        print("\nwritten into %s for %d lines" % (args.path, len(results)))
    else:
        print("\ndry run. Re-run with --write to record these in the log.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
