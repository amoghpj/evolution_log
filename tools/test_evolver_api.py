#!/usr/bin/env python3
"""    ~/py/bin/python tools/test_evolver_api.py

Regression suite for tools/evolver_api.py, with the same discipline as
tools/test_media.py: every check here exists because the corresponding wrong
answer is one a caller would have believed.

The fixture builds a real experiment directory in a temp dir (pump logs,
drugconc, pump_cal.json) and chdir's into it, because evolver_api reads
pump_cal.json and the experiment directory relative to the process's working
directory -- exactly as it does beside a rig.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location

HERE = os.path.dirname(os.path.abspath(__file__))

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def load_api():
    spec = spec_from_file_location("evolver_api", os.path.join(HERE, "evolver_api.py"))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules.setdefault("evolver_api", mod)
    return mod


EXP = "testrun"
# coefficient 0.1 mL/s for every pump makes every expected volume checkable by
# hand: timein 10 -> 1.0 mL.
COEFS = [0.1] * 16


def build_fixture(root, with_calibration=True):
    os.makedirs(os.path.join(root, EXP, "pump_log"))
    os.makedirs(os.path.join(root, EXP, "drugconc"))
    # vial 0: low pump (in1) at t=1,2,3 and high (in2) at t=2 -- 10 s each
    rows = {
        0: [(1.0, 10, "in1"), (2.0, 10, "in1"), (2.0, 10, "in2"), (3.0, 10, "in1")],
        1: [(0.5, 10, "in1"), (2.5, 10, "in2")],
    }
    for vial, events in rows.items():
        with open(os.path.join(root, EXP, "pump_log", "vial%d_pump_log.txt" % vial), "w") as fh:
            fh.write("time,timein,pump\n")          # header row, skipped on read
            for t, tin, which in events:
                fh.write("%s,%s,%s\n" % (t, tin, which))
        with open(os.path.join(root, EXP, "drugconc", "vial%d_drugconc.txt" % vial), "w") as fh:
            fh.write("time,concentration\n1.0,0.5\n3.0,1.5\n")
    if with_calibration:
        with open(os.path.join(root, "pump_cal.json"), "w") as fh:
            json.dump({"coefficients": COEFS}, fh)
    return {
        "experiment_settings": {
            "exp_name": EXP,
            "evolver_name": "testrig",
            "per_vial_settings": [
                {"vial": 0, "to_run": True, "input_pump2": 8, "volume": 20.0},
                {"vial": 1, "to_run": True, "input_pump2": 9, "volume": 20.0},
                {"vial": 2, "to_run": False, "input_pump2": 10, "volume": 20.0},
            ],
        }
    }


def main():
    api = load_api()
    root = tempfile.mkdtemp(prefix="or05-api-")
    cwd = os.getcwd()
    try:
        config = build_fixture(root)
        os.chdir(root)

        # ── the clock is the largest timestamp anything wrote ───────────────
        c = api.build_consumption(config, since_h=0.0)
        ck(c["schema"] == "or05.consumption/1", "schema is or05.consumption/1")
        ck(c["elapsed_h"] == 3.0, "elapsed_h is the controller clock, 3.0 (%s)" % c["elapsed_h"])
        ck(c["pump_calibration"] is True, "pump_calibration true when pump_cal.json exists")
        ck(c["n_vials"] == 2, "only to_run vials are reported (%s)" % c["n_vials"])

        v0 = next(v for v in c["vials"] if v["vial"] == 0)
        ck(abs(v0["low_mL"] - 3.0) < 1e-9, "vial 0 low volume is 3x1.0 mL (%s)" % v0["low_mL"])
        ck(abs(v0["high_mL"] - 1.0) < 1e-9, "vial 0 high volume is 1x1.0 mL (%s)" % v0["high_mL"])
        ck(abs(v0["total_mL"] - 4.0) < 1e-9, "vial 0 total is low+high")
        ck(v0["n_events"] == 4, "vial 0 counts 4 events (%s)" % v0["n_events"])

        # ── since_h EXCLUDES the anchor instant, matching /dispenses ────────
        c2 = api.build_consumption(config, since_h=2.0)
        v0b = next(v for v in c2["vials"] if v["vial"] == 0)
        ck(abs(v0b["low_mL"] - 1.0) < 1e-9,
           "since_h=2.0 counts only the t=3 low event (%s)" % v0b["low_mL"])
        ck(v0b["n_events"] == 1, "since_h is exclusive of events AT the anchor (%s)" % v0b["n_events"])
        ck(v0b["first_event_h"] == 3.0, "first_event_h is inside the window")

        # ── a window nothing falls into is 0.0, and says so with n_events ───
        c3 = api.build_consumption(config, since_h=10.0)
        v0c = next(v for v in c3["vials"] if v["vial"] == 0)
        ck(v0c["total_mL"] == 0.0 and v0c["n_events"] == 0,
           "an empty window reports 0.0 mL over 0 events, not null")
        ck(c3["clock_ok"] is False,
           "clock_ok is False when the anchor is after the controller clock")

        # ── an anchor before the experiment's zero is a LOWER bound ─────────
        c4 = api.build_consumption(config, since_h=-5.0)
        ck(c4["covers_window"] is False,
           "covers_window is False when the anchor precedes the log")
        ck(c4["clock_ok"] is True, "that case is not a clock failure")

        # ── wall clock resolves through generated_at/elapsed_h ──────────────
        gen = datetime.fromisoformat(api.iso_now())
        c5 = api.build_consumption(config, since_iso=(gen - timedelta(hours=1.0)).isoformat())
        ck(c5["since_resolved_from"] == "wall_clock", "since_resolved_from names the wall clock")
        ck(abs(c5["since_h"] - 2.0) < 0.01,
           "1 h before now on a 3.0 h clock is since_h=2.0 (%s)" % c5["since_h"])
        v0d = next(v for v in c5["vials"] if v["vial"] == 0)
        ck(v0d["n_events"] == 1, "the wall-clock window matches the equivalent since_h window")

        # ── a naive timestamp is refused, not guessed at ────────────────────
        try:
            api.build_consumption(config, since_iso="2026-09-21T06:15:00")
            ck(False, "a `since` with no offset is refused")
        except ValueError as exc:
            ck("offset" in str(exc), "a `since` with no offset is refused, by name")

        # ── the colon offset the log repo's own timestamp rule requires ─────
        ck(api.iso_now()[-3] == ":", "iso_now emits a colon offset (%s)" % api.iso_now())
        ck(datetime.fromisoformat(api.iso_now()) is not None,
           "iso_now round-trips through fromisoformat")

        # ── no pump_cal.json: volumes are null, never 0.0 ───────────────────
        os.remove(os.path.join(root, "pump_cal.json"))
        c6 = api.build_consumption(config, since_h=0.0)
        v0e = next(v for v in c6["vials"] if v["vial"] == 0)
        ck(c6["pump_calibration"] is False, "pump_calibration false with no pump_cal.json")
        ck(v0e["low_mL"] is None and v0e["total_mL"] is None,
           "with no calibration the volumes are null, NOT 0.0 -- a bottle that "
           "drew nothing and a rig that cannot convert must not read alike")
        ck(v0e["n_events"] == 4,
           "the events are still counted without calibration (%s)" % v0e["n_events"])

        # ── over the wire, through the registered flask routes ──────────────
        from flask import Flask
        srv = Flask(__name__)
        api.register(srv, lambda: (config, None))
        client = srv.test_client()

        r = client.get("/api/v1/consumption?since_h=2.0")
        ck(r.status_code == 200, "GET /api/v1/consumption -> 200 (%s)" % r.status_code)
        body = r.get_json()
        ck(body["since_h"] == 2.0, "since_h is echoed back")
        ck(r.headers.get("Access-Control-Allow-Origin") == "*",
           "CORS header present, so a browser can read it too")

        r = client.get("/api/v1/consumption?since_h=2.0&since=2026-09-21T00:00:00-04:00")
        ck(r.status_code == 400, "passing both since and since_h is a 400 (%s)" % r.status_code)
        r = client.get("/api/v1/consumption?since_h=banana")
        ck(r.status_code == 400, "a non-numeric since_h is a 400 (%s)" % r.status_code)
        r = client.get("/api/v1/consumption?since=2026-09-21T00:00:00")
        ck(r.status_code == 400, "a naive `since` is a 400 over the wire too (%s)" % r.status_code)

        # the pre-existing routes still answer, unchanged by any of this
        ck(client.get("/api/v1/health").status_code == 200, "GET /api/v1/health still 200")
        ck(client.get("/api/v1/vials").status_code == 200, "GET /api/v1/vials still 200")
        summary = client.get("/api/v1/vials").get_json()
        ck(summary["generated_at"][-3] == ":", "/api/v1/vials generated_at carries the colon offset")
    finally:
        os.chdir(cwd)
        shutil.rmtree(root, ignore_errors=True)

    # ── round-1 adversarial regressions ─────────────────────────────────
    root2 = tempfile.mkdtemp(prefix="or05-api2-")
    try:
        config = build_fixture(root2)
        os.chdir(root2)
        pump_path = os.path.join(root2, EXP, "pump_log", "vial0_pump_log.txt")

        # a NaN/empty timein must never reach the body: json.dumps emits a bare
        # NaN token, which Python accepts and a browser's JSON.parse rejects --
        # and this module is polled by a browser.
        with open(pump_path, "w") as fh:
            fh.write("time,timein,pump\n0.0,10,in1\n1.0,nan,in1\n2.0,,in2\n3.0,10\n")
        c = api.build_consumption(config, since_h=None)
        v0 = next(v for v in c["vials"] if v["vial"] == 0)
        ck("NaN" not in json.dumps(c), "a NaN timein never reaches the JSON body")
        ck(v0["dropped_rows"] == 2, "the unusable rows are counted, not silently lost (%s)"
           % v0["dropped_rows"])
        ck(v0.get("unrecognised_rows") == 1,
           "a torn final line is counted as unrecognised, not charged to a pump")
        ck(abs(v0["low_mL"] - 1.0) < 1e-9, "the surviving rows still integrate (%s)" % v0["low_mL"])

        # the default window means EVERYTHING, including an event at t=0
        ck(c["since_resolved_from"] == "all", "the default anchor is 'all', not the number 0")
        ck(c["since_h"] is None, "and reports no since_h at all")
        ck(c["covers_window"] is True, "which by definition covers the window")
        only_after_zero = api.build_consumption(config, since_h=0.0)
        ck(next(v for v in only_after_zero["vials"] if v["vial"] == 0)["low_mL"] == 0.0,
           "an explicit since_h=0 is exclusive, matching /dispenses")

        # the controller clock is NOW, not the last write
        old_mtime = time.time() - 4 * 3600
        os.utime(pump_path, (old_mtime, old_mtime))
        for extra in ("drugconc/vial0_drugconc.txt", "drugconc/vial1_drugconc.txt",
                      "pump_log/vial1_pump_log.txt"):
            os.utime(os.path.join(root2, EXP, extra), (old_mtime, old_mtime))
        c = api.build_consumption(config, since_h=None)
        ck(c["elapsed_h"] > c["last_write_h"] + 3.5,
           "a rig that has not written for 4 h reports a clock 4 h past its last write "
           "(elapsed_h=%s last_write_h=%s) -- the old code equated the two, so every "
           "wall-clock window opened too early and over-counted"
           % (c["elapsed_h"], c["last_write_h"]))
        ck(c["staleness_h"] is not None and c["staleness_h"] > 3.5,
           "and says how stale it is (%s)" % c["staleness_h"])

        # a truncated log is a gap, not zero consumption. Both vials' logs are
        # rewritten: log_starts_h is the earliest surviving row across the rig,
        # so one untouched vial would (correctly) still cover the window.
        for v in (0, 1):
            with open(os.path.join(root2, EXP, "pump_log", "vial%d_pump_log.txt" % v), "w") as fh:
                fh.write("time,timein,pump\n100.0,10,in1\n101.0,10,in1\n")
        c = api.build_consumption(config, since_h=50.0)
        ck(c["log_starts_h"] == 100.0, "the log's own first row is reported")
        ck(c["log_gap_h"] == 50.0,
           "a window opening 50 h before the log's first surviving row reports that "
           "gap, so the missing hours read as a gap rather than as 0 mL (%s)"
           % c["log_gap_h"])
        ck(c["covers_window"] is True,
           "while covers_window stays about the experiment's own zero -- conflating the "
           "two made it fire for since_h=0 on every healthy rig, since none dispenses "
           "at exactly t=0")
        healthy = api.build_consumption(config, since_h=0.0)
        ck(healthy["covers_window"] is True and healthy["log_gap_h"] > 0,
           "a healthy rig at since_h=0 is covered, with its startup gap reported "
           "separately (%s)" % healthy["log_gap_h"])

        # one vial's bad calibration costs only that vial its number
        for v in (0, 1):
            with open(os.path.join(root2, EXP, "pump_log", "vial%d_pump_log.txt" % v), "w") as fh:
                fh.write("time,timein,pump\n1.0,10,in1\n")
        bad = json.loads(json.dumps(config))
        bad["experiment_settings"]["per_vial_settings"][0]["input_pump2"] = 32
        c = api.build_consumption(bad, since_h=None)
        v0 = next(v for v in c["vials"] if v["vial"] == 0)
        v1 = next(v for v in c["vials"] if v["vial"] == 1)
        ck(v0["problem"] is not None and "outside the 16 calibrated pumps" in v0["problem"],
           "an out-of-range input_pump2 is named, not indexed into another pump")
        ck(v0["high_mL"] is None, "its HIGH volume is null, not a wrong number")
        ck(v0["low_mL"] is not None,
           "but its LOW volume still reports: it needs only coef_low, which is fine. "
           "Resolving both pumps together and bailing on the first failure blanked a "
           "perfectly computable number (%s)" % v0["low_mL"])
        ck(v1["low_mL"] is not None, "while the other vial still reports its own volume")

        # a negative index used to select the LAST coefficient: 990 mL from a 10 s run
        bad["experiment_settings"]["per_vial_settings"][0]["input_pump2"] = -1.0
        v0 = next(v for v in api.build_consumption(bad, since_h=None)["vials"] if v["vial"] == 0)
        ck(v0["problem"] is not None, "a negative input_pump2 is refused, not wrapped around")
        bad["experiment_settings"]["per_vial_settings"][0]["input_pump2"] = 8.7
        v0 = next(v for v in api.build_consumption(bad, since_h=None)["vials"] if v["vial"] == 0)
        ck(v0["problem"] is not None, "a fractional input_pump2 is refused, not truncated")

        # a media-only vial (input_pump2: .nan) can still report its LOW volume
        mono = json.loads(json.dumps(config))
        mono["experiment_settings"]["per_vial_settings"][0]["input_pump2"] = None
        v0 = next(v for v in api.build_consumption(mono, since_h=None)["vials"] if v["vial"] == 0)
        ck(v0["low_mL"] == 1.0 and v0["high_mL"] is None,
           "a vial with no high pump reports the low volume it CAN compute (%s/%s)"
           % (v0["low_mL"], v0["high_mL"]))

        # ONE unused coefficient being empty must not refuse the whole rig.
        # Both live rigs ship coefficient 16 = "" with sixteen good ones
        # either side; validating the whole list up front took out every vial
        # AND /api/v1/vials, which is what the viewer's live column reads.
        holey = [0.1] * 16 + [""] + [0.1] * 15
        json.dump({"coefficients": holey}, open(os.path.join(root2, "pump_cal.json"), "w"))
        with open(pump_path, "w") as fh:
            fh.write("time,timein,pump\n1.0,10,in1\n")
        c = api.build_consumption(config, since_h=None)
        v0 = next(v for v in c["vials"] if v["vial"] == 0)
        ck(c["pump_calibration"] is True,
           "a file with one empty entry is still a calibration")
        ck(v0["low_mL"] == 1.0 and v0["problem"] is None,
           "and a vial that uses none of the empty entries reports normally (%s)"
           % v0["low_mL"])
        srv_h = Flask("holey")
        api.register(srv_h, lambda: (config, None))
        ck(srv_h.test_client().get("/api/v1/vials").status_code == 200,
           "/api/v1/vials still answers -- the viewer's live column depends on it")

        # ...while a vial that DOES use the empty entry is named, alone
        holey_cfg = json.loads(json.dumps(config))
        holey_cfg["experiment_settings"]["per_vial_settings"][0]["input_pump2"] = 16
        v0 = next(v for v in api.build_consumption(holey_cfg, since_h=None)["vials"]
                  if v["vial"] == 0)
        ck(v0["problem"] is not None and "not calibrated" in v0["problem"],
           "a vial whose pump IS the empty entry is named, and only that vial")
        ck(v0["low_mL"] == 1.0,
           "and even it still reports the low volume it can compute (%s)" % v0["low_mL"])

        # a corrupt pump_cal.json is the rig's fault, not the caller's
        with open(os.path.join(root2, "pump_cal.json"), "w") as fh:
            fh.write('{"coefficients": [0.1,')
        from flask import Flask
        srv = Flask("regress")
        api.register(srv, lambda: (config, None))
        cl = srv.test_client()
        r = cl.get("/api/v1/consumption?since_h=0")
        ck(r.status_code == 503,
           "a corrupt pump_cal.json is a 503 about the rig, not a 400 blaming the caller "
           "for a since_h it did send correctly (%s)" % r.status_code)
        ck(r.get_json().get("schema") == "or05.consumption/1",
           "and answers in the schema the caller asked about")
        ck(r.headers.get("Access-Control-Allow-Origin") == "*", "with CORS, so a browser sees it")
        r = cl.get("/api/v1/vials/0/dispenses")
        ck(r.status_code == 503, "/dispenses degrades to 503 too, never a bare 500 (%s)"
           % r.status_code)
        ck(r.headers.get("Access-Control-Allow-Origin") == "*",
           "and carries CORS on the error, or a browser sees only an opaque failure")

        # query-string parsing
        json.dump({"coefficients": COEFS}, open(os.path.join(root2, "pump_cal.json"), "w"))
        for bad_value in ("nan", "inf", "1_0"):
            r = cl.get("/api/v1/consumption?since_h=%s" % bad_value)
            ck(r.status_code == 400, "since_h=%s is refused (%s)" % (bad_value, r.status_code))
        r = cl.get("/api/v1/consumption?since=2026-09-21T00:00:00+00:00")
        ck(r.status_code == 200,
           "a POSITIVE UTC offset survives the query string, where + decodes to a space "
           "-- every caller in UTC or east of Greenwich used to get a 400 (%s)"
           % r.status_code)

        # /dispenses and /consumption must price the same log the same way
        with open(pump_path, "w") as fh:
            fh.write("time,timein,pump\n1.0,10,in1\n2.0,10,in2\n3.0,10,0\n")
        srv2 = Flask("agree")
        api.register(srv2, lambda: (config, None))
        cl2 = srv2.test_client()
        cons = api.build_consumption(config, since_h=0.0)
        v0c = next(v for v in cons["vials"] if v["vial"] == 0)
        disp = cl2.get("/api/v1/vials/0/dispenses?since_h=0").get_json()
        by_role = {"low": 0.0, "high": 0.0}
        for _t, mL, role in disp["dispenses"]:
            by_role[role] += mL
        ck(abs(by_role["low"] - v0c["low_mL"]) < 1e-9
           and abs(by_role["high"] - v0c["high_mL"]) < 1e-9,
           "/dispenses and /consumption price the same window identically "
           "(%s/%s vs %s/%s) -- the stray row used to be charged to the drug bottle by "
           "one and dropped by the other, a real millilitre disagreement over one log"
           % (by_role["low"], by_role["high"], v0c["low_mL"], v0c["high_mL"]))
        ck(disp["unrecognised_rows"] == v0c.get("unrecognised_rows"),
           "and both report the same count of rows they could not price")
        ck(disp["elapsed_h"] == cons["elapsed_h"],
           "/dispenses reports the controller clock, not the filtered frame's max -- it "
           "used to depend on the caller's own since_h (%s vs %s)"
           % (disp["elapsed_h"], cons["elapsed_h"]))
        far = cl2.get("/api/v1/vials/0/dispenses?since_h=1e9").get_json()
        ck(far["elapsed_h"] == disp["elapsed_h"],
           "including for a window that matches nothing, where it used to echo the "
           "caller's own since_h back -- which is what viewer.html's restart guard "
           "compares against, so the guard could never fire")

        # a media-only vial is answerable by BOTH paths
        mono2 = json.loads(json.dumps(config))
        mono2["experiment_settings"]["per_vial_settings"][0]["input_pump2"] = None
        srv3 = Flask("mono")
        api.register(srv3, lambda: (mono2, None))
        rows = srv3.test_client().get("/api/v1/vials/0/dispenses?since_h=0").get_json()
        ck(all(r[1] is not None for r in rows["dispenses"]),
           "a vial with no high pump emits only priceable rows -- a null-volume row made "
           "the caller refuse the whole unit")
        ck(any(r[2] == "low" for r in rows["dispenses"]),
           "and still reports the low dispenses it can price")
    finally:
        os.chdir(cwd)
        shutil.rmtree(root2, ignore_errors=True)

    # ── alternating_selection ────────────────────────────────────────────
    root3 = tempfile.mkdtemp(prefix="or05-altsel-")
    try:
        config = build_fixture(root3)
        os.chdir(root3)
        # a rig NOT running this mode answers with its mode and no vials --
        # "not this mode" and "this mode, nothing logged" must not both be
        # silence.
        a = api.build_altsel(config)
        ck(a["schema"] == "or05.altsel/1", "altsel schema is or05.altsel/1")
        ck(a["mode"] is None and a["vials"] == [],
           "a rig in another mode reports its mode and no vials, not an error")

        config["experiment_settings"]["operation"] = {"mode": "alternating_selection"}
        config["experiment_settings"]["per_vial_settings"][0].update(
            {"n_tolerant": 5, "n_dilutions": 6})
        for sub in ("state_log", "drug_target", "cycle_log"):
            os.makedirs(os.path.join(root3, EXP, sub), exist_ok=True)
        base = os.path.join(root3, EXP)
        open(os.path.join(base, "state_log", "vial0_state.txt"), "w").write(
            "time,state\n0.0,HIGH\n5.0,LOW\n9.0,HIGH\n")
        open(os.path.join(base, "drug_target", "vial0_drug_target.txt"), "w").write(
            "time,target\n5.0,1.5\n")
        open(os.path.join(base, "cycle_log", "vial0_cycles.txt"), "w").write(
            "time,state,kind,conc_before,conc_after,cycle_duration,dilution_counter,"
            "time_in_state\n"
            "1.0,HIGH,climb,0.0,0.5,1.0,0,1.0\n"
            "2.0,HIGH,counted,0.5,1.0,0.9,3,1.9\n"
            "8.0,LOW,tooshort,0.6,0.5\n")     # torn tail, on purpose

        a = api.build_altsel(config, now_h=12.0)
        v0 = next(v for v in a["vials"] if v["vial"] == 0)
        ck(a["mode"] == "alternating_selection", "the mode is reported")
        ck(v0["state"] == "HIGH" and v0["state_since_h"] == 9.0,
           "the current state comes from the LAST transition row (%s)" % v0["state"])
        ck(v0["spans"] == [[0.0, 5.0, "HIGH"], [5.0, 9.0, "LOW"], [9.0, 12.0, "HIGH"]],
           "spans run from each transition to the next, and the last one to now -- "
           "the log holds transitions, so read any other way a vial is stateless "
           "except at the instants it changed (%s)" % v0["spans"])
        ck(v0["current_drug"] == 1.5, "Current_Drug is the newest target row")
        ck(v0["counter"] == 3 and v0["last_kind"] == "counted",
           "the streak and kind come from the last INTACT cycle: the torn row is "
           "padded with NaN by pandas and survives on_bad_lines alone (%s/%s)"
           % (v0["counter"], v0["last_kind"]))
        ck(v0["n_tolerant"] == 5 and v0["n_dilutions"] == 6,
           "the thresholds ride along, so a caller need not re-read the yaml")

        # a vial with no logs yet is present and null, not absent
        v1 = next((v for v in a["vials"] if v["vial"] == 1), None)
        ck(v1 is not None and v1["state"] is None and v1["spans"] == [],
           "a vial that has not transitioned yet is reported as stateless, not omitted")

        srv_a = Flask("altsel")
        api.register(srv_a, lambda: (config, None))
        r = srv_a.test_client().get("/api/v1/altsel")
        ck(r.status_code == 200, "GET /api/v1/altsel -> 200 (%s)" % r.status_code)
        ck(r.headers.get("Access-Control-Allow-Origin") == "*",
           "with CORS, since a browser is the intended caller")
    finally:
        os.chdir(cwd)
        shutil.rmtree(root3, ignore_errors=True)

    print()
    if _fails:
        print("%d check(s) failed" % len(_fails))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
