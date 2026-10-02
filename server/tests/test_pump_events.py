#!/usr/bin/env python3
"""    ~/py/bin/python server/tests/test_pump_events.py

GET /pump_events against fake rigs (tests/fake_rig.py). The fixture log has
testunit-v01 in vial 1 and testunit-v03 in vial 3, both active, both drawing
from testunit/LB-0 (low) and testunit/LB-5 (high); testunit-v02 (vial 2) has
ended. A rig's events are given in CONTROLLER hours; with elapsed_h = 100 and
generated_at = now, controller hour t is wall time now - (100 - t) h.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_settings  # noqa: E402
from tests.fake_rig import client_for, dashboards_for, dead_client, rig  # noqa: E402
from app.main import app  # noqa: E402
from app.routes.media import get_dashboards, get_http_client  # noqa: E402

_fails = []
URL = "http://testunit.test"
E = 100.0


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def wire(vials, mutate=None, dead=False, **rig_kw):
    client, settings = make_client_with_settings()
    log = json.loads(settings.log_file.read_text())
    # the fixture's one termination is four days after t0 -- settle it well
    # before any window here, so it never overlaps one by accident
    for L in log["lines"].values():
        for e in L.get("events") or []:
            if e["event_type"] == "termination":
                e["timestamp"] = "2026-01-02T09:00:00-05:00"
        if (L.get("lineage") or {}).get("terminated_at"):
            L["lineage"]["terminated_at"] = "2026-01-02T09:00:00-05:00"
    if mutate:
        mutate(log)
    settings.log_file.write_text(json.dumps(log, indent=2))
    calls = []
    rigs = {"testunit.test": rig("testunit", vials, elapsed_h=rig_kw.pop("elapsed_h", E), **rig_kw)}
    http = dead_client(calls) if dead else client_for(rigs, calls)
    ds = dashboards_for({"testunit": URL})
    app.dependency_overrides[get_dashboards] = lambda: ds
    app.dependency_overrides[get_http_client] = lambda: http
    return client, calls


def ago(hours):
    return datetime.now().astimezone() - timedelta(hours=hours)


def swap(line_id, when, **params):
    def mutate(log):
        log["lines"][line_id]["events"].append({
            "event_id": "EVT-0%04d" % (900 + len(log["lines"][line_id]["events"])),
            "timestamp": when.isoformat(timespec="seconds"), "event_type": "hardware_swap",
            "operator": "AJ", "provenance": "reported", "params": params, "notes": "test move"})
        if params.get("vacate"):
            log["lines"][line_id]["unit"] = log["lines"][line_id]["vial"] = None
        elif "new_vial" in params:
            log["lines"][line_id]["unit"], log["lines"][line_id]["vial"] = params["new_unit"], params["new_vial"]
    return mutate


def near(iso, hours_ago, tol_s=5):
    return abs((datetime.fromisoformat(iso) - ago(hours_ago)).total_seconds()) < tol_s


def main():
    # ── 1. the window, the clock, the labels ─────────────────────────────
    client, calls = wire({1: {"events": [[90.0, 9.9, "low"],      # 10 h ago: outside 6 h
                                         [95.5, 2.0, "low"],      # 4.5 h ago
                                         [97.0, 0.5, "high"]]},   # 3 h ago
                          3: {"events": [[99.0, 1.0, "low"]]}})
    r = client.get("/pump_events?window_h=6")
    ck(r.status_code == 200, "GET /pump_events?window_h=6 answers (%s %s)" % (r.status_code, r.text[:200]))
    b = r.json()
    ck(b["window"]["hours"] == 6 and near(b["window"]["from"], 6), "the window is the last 6 hours, by the wall clock")
    ck(any(p == "/api/v1/vials/1/dispenses" and abs(float(q["since_h"]) - 94.0) < 0.01
           for _h, p, q in calls),
       "it asked the rig for controller hours after 94 (= elapsed 100 - 6), not 'since 6'")
    u = b["units"]["testunit"]
    ck(u["ok"] and u["clock_exact"] and not u["window_truncated"], "the rig is usable, its clock exact, the window whole")
    v1 = u["vials"]["1"]
    ck(v1["n_events"] == 2, "the event 10 h ago is outside the window and left out (%s)" % v1["n_events"])
    ck(b["event_fields"] == ["at", "mL", "pump", "line_id", "reservoir_id"], "event rows are labelled by event_fields")
    e0 = v1["events"][0]
    ck(near(e0[0], 4.5), "controller hour 95.5 comes back as wall time 4.5 h ago (%s)" % e0[0])
    ck(e0[1:] == [2.0, "low", "testunit-v01", "testunit/LB-0"], "labelled with its line and its LOW bottle (%s)" % e0)
    ck(v1["events"][1][2:] == ["high", "testunit-v01", "testunit/LB-5"], "the high pump is charged to the HIGH bottle")
    ck(v1["total_mL"] == {"low": 2.0, "high": 0.5}, "per-vial totals by pump")
    tr = b["totals_by_reservoir"]
    ck(tr["testunit/LB-0"]["mL"] == 3.0 and tr["testunit/LB-0"]["vials"] == ["testunit/1", "testunit/3"],
       "LB-0's window total is both vials' low dispenses (%s)" % tr.get("testunit/LB-0"))
    ck(tr["testunit/LB-0"]["complete"] is True, "and complete, since the whole window was read")
    ck(b["totals_by_line"]["testunit-v03"] == {"low_mL": 1.0, "high_mL": 0.0, "n_events": 1},
       "per-line totals (%s)" % b["totals_by_line"].get("testunit-v03"))

    b2 = client.get("/pump_events?window_h=6&events=false").json()
    ck("events" not in b2["units"]["testunit"]["vials"]["1"] and b2["totals_by_reservoir"] == tr,
       "events=false drops the event lists and keeps every total")

    # ── 2. a culture that moved inside the window ────────────────────────
    client, _ = wire({3: {"events": [[97.0, 1.0, "low"],          # 3 h ago: v03 still here
                                     [98.05, 1.0, "low"],         # 1.95 h ago: just after it left
                                     [99.0, 1.0, "low"]]},        # 1 h ago: nobody
                      7: {"events": [[99.0, 4.0, "low"]]}},       # 1 h ago: v03, moved here
                     mutate=swap("testunit-v03", ago(2), new_unit="testunit", new_vial=7,
                                 previous_unit="testunit", previous_vial=3))
    vs = client.get("/pump_events?window_h=6").json()["units"]["testunit"]["vials"]
    ck([e[3] for e in vs["3"]["events"]] == ["testunit-v03", None, None],
       "vial 3 belongs to v03 until it moved out, and to no one after")
    ck(vs["7"]["events"][0][3] == "testunit-v03", "the dispense into vial 7 after the move is v03's")
    ck(any("records as empty" in x["reason"] for x in vs["3"]["unattributed"]),
       "a dispense into a vial no line occupies is reported, with that reason")
    ck(vs["3"].get("near_line_change", {}).get("n_events") == 1,
       "the dispense 3 min after the move is flagged as near a line change")

    # ── 3. a founding position the log never recorded ────────────────────
    client, _ = wire({1: {"events": [[97.0, 1.0, "low"], [99.5, 1.0, "low"]]}},
                     mutate=swap("testunit-v01", ago(1), vacate=True))
    v1 = client.get("/pump_events?window_h=6").json()["units"]["testunit"]["vials"]["1"]
    ck(v1["events"][0][3] is None and any("no recorded position" in x["reason"] for x in v1["unattributed"]),
       "before a vacate with no previous_vial, v01's vial is unknown: unattributed, not guessed")
    ck(v1["events"][0][4] is None, "and so charged to no bottle")

    # a superseded move does not count
    def superseded_move(log):
        swap("testunit-v03", ago(2), new_unit="testunit", new_vial=7,
             previous_unit="testunit", previous_vial=3)(log)
        bad = log["lines"]["testunit-v03"]["events"][-1]["event_id"]
        log["lines"]["testunit-v03"]["events"].append({
            "event_id": "EVT-09999", "timestamp": ago(1.5).isoformat(timespec="seconds"),
            "event_type": "note", "operator": "AJ", "provenance": "reported", "params": {},
            "supersedes": bad, "notes": "that move never happened"})
        log["lines"]["testunit-v03"]["unit"], log["lines"]["testunit-v03"]["vial"] = "testunit", 3
    client, _ = wire({3: {"events": [[99.0, 1.0, "low"]]}}, mutate=superseded_move)
    ck(client.get("/pump_events").json()["units"]["testunit"]["vials"]["3"]["events"][0][3] == "testunit-v03",
       "a superseded hardware_swap is ignored: v03 never left vial 3")

    # ── 4. a rig that restarted inside the window, or cannot date itself ─
    client, _ = wire({1: {"events": [[1.0, 1.0, "low"]]}}, elapsed_h=3.0)
    b = client.get("/pump_events?window_h=6").json()
    u = b["units"]["testunit"]
    ck(u["window_truncated"] and near(u["run_started_at"], 3), "a run that began 3 h ago truncates a 6 h window, and says when")
    ck(b["totals_by_reservoir"]["testunit/LB-0"]["complete"] is False, "so its totals are not complete")

    client, _ = wire({1: {"events": [[99.0, 1.0, "low"]]}}, reports_staleness=False)
    u = client.get("/pump_events").json()["units"]["testunit"]
    ck(u["clock_exact"] is False and "LAST WRITE" in u["clock_note"],
       "an un-updated rig's times are flagged as possibly late, not presented as exact")

    client, _ = wire({1: {"events": [[99.0, 1.0, "sideways"]]}})
    v1 = client.get("/pump_events").json()["units"]["testunit"]["vials"]["1"]
    ck(v1["events"][0][4] is None and "neither low nor high" in v1["unattributed"][0]["reason"],
       "an unrecognised pump is charged to no bottle, and said")

    client, _ = wire({1: {"events": []}, 3: {"events": [[99.0, 1.0, "low"]]}})
    ck("quiet" in client.get("/pump_events").json()["units"]["testunit"]["vials"]["1"],
       "a vial with no dispenses is named as quiet, not omitted")

    # ── 5. rigs that cannot be used ──────────────────────────────────────
    for label, kw, needle in [("dead", {"dead": True}, "unreachable"),
                              ("wrong rig", {"answers_as": "plankton"}, "answers for evolver"),
                              ("uncalibrated", {"calibrated": False}, "calibration"),
                              ("skewed clock", {"generated_at": ago(2).isoformat()}, "minutes from this server")]:
        client, _ = wire({1: {"events": [[99.0, 1.0, "low"]]}}, **kw)
        b = client.get("/pump_events").json()
        u = b["units"]["testunit"]
        ck(u["ok"] is False and needle in u["reason"], "%s rig: refused with the reason (%s)" % (label, u.get("reason", "")[:80]))
        ck(b["totals_by_reservoir"] == {} and b["notes"], "%s rig: no totals, and a note saying what was not measured" % label)

    # ── 6. the query itself ──────────────────────────────────────────────
    client, _ = wire({1: {"events": []}})
    for q, why in [("window_h=0", "zero window"), ("window_h=49", "past the 48 h cap"),
                   ("vial=1", "vial without unit"), ("unit=nosuch", "unknown unit"),
                   ("unit=testunit&vial=16", "vial out of range")]:
        ck(client.get("/pump_events?" + q).status_code == 422, "%s -> 422" % why)
    r = client.get("/pump_events?unit=testunit&vial=9")
    ck(r.status_code == 200 and "no vial 9" in r.json()["units"]["testunit"]["reason"],
       "a vial the rig does not have is reported by the rig's own list")
    ck("/pump_events" in client.get("/skill").text, "GET /skill lists the route")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
