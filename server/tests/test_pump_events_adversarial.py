#!/usr/bin/env python3
"""    ~/py/bin/python server/tests/test_pump_events_adversarial.py

Regressions for what four adversarial reviews of GET /pump_events found
(2026-10-02), one check per finding. Each was reproduced against the code
before it was fixed. The theme throughout: partial or contradictory data must
come back SAYING so, never as a smaller or confidently wrong number.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import test_pump_events as T  # noqa: E402
from tests.fake_rig import _summary_body, dashboards_for, rig  # noqa: E402
from app.main import app  # noqa: E402
from app.routes.media import get_dashboards, get_http_client  # noqa: E402
from app.dashboards import DashboardSettings  # noqa: E402
import app.pump_events as PE  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def raw(rows_by_vial, summary_patch=None, mutate=None, vial_body=None, units=None):
    """A client whose rig returns EXACTLY these rows and summary fields."""
    client, _ = T.wire({v: {"events": []} for v in rows_by_vial}, mutate=mutate)

    def handler(req):
        if req.url.path == "/api/v1/vials":
            b = _summary_body(rig("testunit", {v: {} for v in rows_by_vial}, elapsed_h=100.0))
            b.update(summary_patch or {})
            return httpx.Response(200, content=json.dumps(b), headers={"content-type": "application/json"})
        v = int(req.url.path.split("/")[4])
        body = dict({"pump_calibration": True, "dispenses": rows_by_vial[v]}, **(vial_body or {}))
        return httpx.Response(200, content=json.dumps(body), headers={"content-type": "application/json"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_http_client] = lambda: http
    if units:
        ds = dashboards_for(units)
        app.dependency_overrides[get_dashboards] = lambda: ds
    return TestClient(app, raise_server_exceptions=False)


def post(client, line_id, hours_ago, event_type, params=None, **extra):
    r = client.post("/events", json=dict({
        "target": {"line_id": line_id}, "timestamp": T.ago(hours_ago).isoformat(timespec="seconds"),
        "event_type": event_type, "provenance": "reported", "notes": "adversarial regression",
        "params": params or {}}, **extra))
    return r


def vials(client, q=""):
    return client.get("/pump_events" + q).json()["units"]["testunit"]["vials"]


def main():
    # ═══ 1. attribution: never a confident wrong culture ═════════════════
    # A move retracted by a superseding note: POST /events does not revert the
    # line's unit/vial, so the record says vial 7 while the history says the
    # line never moved. Both readings exist; neither may be picked.
    client, _ = T.wire({3: {"events": [[97.0, 1.0, "low"], [99.0, 1.0, "low"]]},
                        7: {"events": [[97.0, 1.0, "low"]]}})
    r = post(client, "testunit-v03", 2, "hardware_swap",
             {"new_unit": "testunit", "new_vial": 7, "previous_unit": "testunit", "previous_vial": 3})
    post(client, "testunit-v03", 1.5, "note", supersedes=r.json()["event_id"])
    vs = vials(client)
    ck(vs["7"]["events"][0][3] is None and vs["3"]["events"][0][3] is None,
       "a retracted move whose record was not reverted: no confident attribution in either vial")
    ck("id says it was founded in testunit vial 3" in vs["7"]["unattributed"][0]["reason"],
       "  and the reason names the contradiction")

    # A swap logged after a termination must not keep the dead line in its vial.
    client, _ = T.wire({1: {"events": [[97.0, 1.0, "low"]]}})
    post(client, "testunit-v01", 4, "termination")
    post(client, "testunit-v01", 1, "hardware_swap", {"vacate": True, "previous_unit": "testunit", "previous_vial": 1})
    ck(vials(client)["1"]["events"][0][3] is None,
       "a dispense after a line's termination is not credited to it, whatever was logged later")

    # Swaps timestamped in a different order from the one they were written in.
    client, _ = T.wire({5: {"events": [[99.0, 1.0, "low"]]}, 7: {"events": [[99.0, 1.0, "low"]]}})
    post(client, "testunit-v03", 2, "hardware_swap",
         {"new_unit": "testunit", "new_vial": 7, "previous_unit": "testunit", "previous_vial": 3})
    post(client, "testunit-v03", 3, "hardware_swap", {"new_unit": "testunit", "new_vial": 5})
    vs = vials(client)
    ck(vs["7"]["events"][0][3] is None and vs["5"]["events"][0][3] is None,
       "history and record disagree about the final vial: neither is believed")

    # A free-text move with a hand-edited vial (what the writer tells humans to do).
    def free_text(log):
        log["lines"]["testunit-v01"]["events"].append({
            "event_id": "EVT-00950", "timestamp": T.ago(2).isoformat(timespec="seconds"),
            "event_type": "hardware_swap", "operator": "AJ", "provenance": "reported",
            "params": {"what_moved": "moved to vial 7 by hand"}, "notes": "x"})
        log["lines"]["testunit-v01"]["vial"] = 7
    client, _ = T.wire({1: {"events": [[97.0, 1.0, "low"]]}, 7: {"events": [[97.0, 1.0, "low"]]}}, mutate=free_text)
    vs = vials(client)
    ck(vs["7"]["events"][0][3] is None and vs["1"]["events"][0][3] is None,
       "a hand-edited move is not read as 'it was always in vial 7'")

    # An ended line with no recorded end, then a new culture restarted there.
    def no_end(log):
        L = log["lines"]["testunit-v01"]
        L["status"] = "ended"
        L.setdefault("lineage", {})["terminated_at"] = None
        L["events"] = [e for e in L["events"] if e["event_type"] != "termination"]
    client, _ = T.wire({1: {"events": [[99.0, 1.0, "low"]]}}, mutate=no_end)
    v1 = vials(client)["1"]
    ck(v1["events"][0][3] is None and "no readable time" in v1["unattributed"][0]["reason"],
       "an ended line with no recorded end is not credited forever")

    def no_end_then_restart(log):
        no_end(log)
        child = json.loads(json.dumps(log["lines"]["testunit-v01"]))
        child.update(line_id="testunit-v01#2", status="active", t0=T.ago(3).isoformat(timespec="seconds"), events=[])
        child["lineage"] = {"parents": [], "is_founder": True, "occupies_vial_of": "testunit-v01"}
        log["lines"]["testunit-v01#2"] = child
    client, _ = T.wire({1: {"events": [[99.0, 1.0, "low"]]}}, mutate=no_end_then_restart)
    ck(vials(client)["1"]["events"][0][3] is None,
       "  and a restart there cannot be credited while the old line's end is unknown")

    # A superseded termination is not the line's end.
    def superseded_end(log):
        L = log["lines"]["testunit-v01"]
        L["status"] = "ended"
        L.setdefault("lineage", {})["terminated_at"] = None
        L["events"] = [e for e in L["events"] if e["event_type"] != "termination"]
        L["events"] += [
            {"event_id": "EVT-00960", "timestamp": T.ago(1).isoformat(timespec="seconds"), "event_type": "termination",
             "operator": "AJ", "provenance": "reported", "params": {}, "notes": "wrong time"},
            {"event_id": "EVT-00961", "timestamp": T.ago(4).isoformat(timespec="seconds"), "event_type": "termination",
             "operator": "AJ", "provenance": "reported", "params": {}, "notes": "real end",
             "supersedes": "EVT-00960"}]
    client, _ = T.wire({1: {"events": [[98.0, 1.0, "low"]]}}, mutate=superseded_end)
    ck(vials(client)["1"]["events"][0][3] is None,
       "a superseded termination is not used: the corrected, earlier end is")

    # Naive timestamps and malformed swaps: refused into a doubt, never a 500.
    for label, mutate in [
        ("naive t0", lambda log: log["lines"]["testunit-v01"].update(t0="2026-01-01T09:00:00")),
        ("params as a string", lambda log: log["lines"]["testunit-v01"]["events"].append(
            {"event_id": "EVT-00970", "timestamp": T.ago(2).isoformat(), "event_type": "hardware_swap",
             "params": "moved", "operator": "AJ", "provenance": "reported", "notes": "x"})),
        ("new_vial as a string", lambda log: log["lines"]["testunit-v01"]["events"].append(
            {"event_id": "EVT-00971", "timestamp": T.ago(2).isoformat(), "event_type": "hardware_swap",
             "params": {"new_unit": "testunit", "new_vial": "2"}, "operator": "AJ", "provenance": "reported",
             "notes": "x"}))]:
        client, _ = T.wire({1: {"events": [[99.0, 1.0, "low"]]}}, mutate=mutate)
        r = client.get("/pump_events")
        ck(r.status_code == 200 and r.json()["units"]["testunit"]["vials"]["1"]["events"][0][3] is None,
           "%s: 200, and v01 is not credited on a history that cannot be read" % label)

    # A swap that re-states the same vial is not a line change.
    client, _ = T.wire({1: {"events": [[98.05, 1.0, "low"]]}},
                       mutate=T.swap("testunit-v01", T.ago(2), new_unit="testunit", new_vial=1,
                                     previous_unit="testunit", previous_vial=1))
    v1 = vials(client)["1"]
    ck(v1["events"][0][3] == "testunit-v01" and "near_line_change" not in v1,
       "a same-vial hardware_swap neither changes the occupant nor raises near_line_change")

    # ═══ 2. totals: never smaller than what happened, never doubled ══════
    c = raw({1: [[99.0, 5.0, "low"], [99.1, -4.0, "low"], [99.2, 1e308, "low"], [99.3, 150.0, "low"]]})
    b = c.get("/pump_events").json()
    u = b["units"]["testunit"]
    ck(u["vials"]["1"]["total_mL"]["low"] == 5.0, "negative, overflowing and implausible volumes are not summed")
    ck("negative volume" in u["vial_problems"]["1"] and "more than 100 mL" in u["vial_problems"]["1"],
       "  each is refused BY NAME in vial_problems")
    ck(b["complete"] is False and b["totals_by_reservoir"]["testunit/LB-0"]["complete"] is False,
       "  and every total says it is incomplete")

    client, _ = T.wire({1: {"events": [[99.0, 3.0, "low"]]}},
                       mutate=lambda log: log["hardware"]["units"].update(other={"vials_in_use": [], "n_lines": 0}))
    ds = dashboards_for({"testunit": "http://testunit.test", "other": "http://TESTUNIT.test/"})
    app.dependency_overrides[get_dashboards] = lambda: ds
    b = client.get("/pump_events").json()
    ck(b["units"]["testunit"]["ok"] is False and "share one dashboard URL" in b["units"]["testunit"]["reason"]
       and b["totals_by_reservoir"] == {},
       "two units on one dashboard URL are refused, not counted twice")

    # ═══ 3. nothing disappears without a word ════════════════════════════
    c = raw({1: [[99.0, 1.0, "low"], [100.1, 2.0, "low"], [101.0, 7.0, "low"]]})
    u = c.get("/pump_events").json()["units"]["testunit"]
    ck(u["vials"]["1"]["total_mL"]["low"] == 3.0,
       "a row just past the rig's elapsed_h (negative staleness) is kept")
    ck("dated after the rig's own clock" in u["vial_problems"]["1"], "  one far past it is refused and named")

    c = raw({1: [[99.0, None, "low"]]})
    v1 = c.get("/pump_events").json()["units"]["testunit"]["vials"]["1"]
    ck("quiet" not in v1, "a vial whose every row was refused is not called quiet")

    client, _ = T.wire({1: {"events": []}})
    ds = dashboards_for({"testunit": "http://testunit.test", "ghost": "http://ghost.test"})
    app.dependency_overrides[get_dashboards] = lambda: ds
    b = client.get("/pump_events").json()
    ck(b["units"]["ghost"]["ok"] is False and any("ghost" in n for n in b["notes"]),
       "a roster unit the log does not know is reported, not skipped")

    def handler(req):
        if req.url.path == "/api/v1/vials":
            return httpx.Response(200, json=_summary_body(rig("testunit", {1: {}, 3: {}}, elapsed_h=100.0)))
        if "/vials/3/" in req.url.path:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"pump_calibration": True, "dispenses": [[99.0, 1.0, "low"]]})
    client, _ = T.wire({1: {"events": []}, 3: {"events": []}})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    app.dependency_overrides[get_http_client] = lambda: http
    b = client.get("/pump_events").json()
    ck(b["units"]["testunit"]["vials"]["3"]["ok"] is False, "a vial that could not be read is listed, ok:false")
    ck(b["totals_by_line"]["testunit-v01"]["complete"] is False and b["complete"] is False
       and any("vial 3" in x for x in b["incomplete_because"]),
       "  and every total -- per line too -- is marked incomplete, saying which vial")

    client, _ = T.wire({1: {"events": [[99.0, 1.0, "low"]]}})
    b = client.get("/pump_events?unit=testunit").json()
    ck(any("covers that unit only" in n for n in b["notes"]), "a unit filter says the totals are scoped to it")

    # ═══ 4. no input from a rig or the log makes the route 500 ═══════════
    for label, kw in [("pump as a list", {"rows_by_vial": {1: [[99.0, 1.0, ["low"]]]}}),
                      ("vial number Infinity", {"rows_by_vial": {1: []}, "summary_patch": {"vials": [{"vial": float("inf")}]}}),
                      ("elapsed_h 1e8", {"rows_by_vial": {1: []}, "summary_patch": {"elapsed_h": 1e8}}),
                      ("elapsed_h negative", {"rows_by_vial": {1: []}, "summary_patch": {"elapsed_h": -5.0}}),
                      ("vial number True", {"rows_by_vial": {1: []}, "summary_patch": {"vials": [{"vial": True}]}})]:
        r = raw(**kw).get("/pump_events")
        ck(r.status_code == 200, "%s: 200, not 500 (%s)" % (label, r.status_code))
    ck(raw({1: []}, {"elapsed_h": -5.0}).get("/pump_events").json()["units"]["testunit"]["ok"] is False,
       "  a negative controller clock is refused, not placed in the future")
    ck(raw({1: []}, {"vials": [{"vial": float("inf")}]}).get("/media").status_code == 200,
       "  Infinity as a vial number no longer 500s GET /media either")

    real = PE._unit_view
    PE._unit_view = lambda *a, **k: (_ for _ in ()).throw(KeyError("simulated"))
    try:
        r = raw({1: [[99.0, 1.0, "low"]]}).get("/pump_events")
        ck(r.status_code == 200 and "failed while reading testunit" in r.json()["units"]["testunit"]["reason"],
           "an unforeseen error in one unit is that unit's reason, not the route's 500")
    finally:
        PE._unit_view = real

    # ═══ 5. the clock ════════════════════════════════════════════════════
    for minutes in (9, -9):
        skew = (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat()
        b = raw({1: [[100.0, 1.0, "low"], [94.2, 1.0, "low"]]}, {"generated_at": skew}).get("/pump_events").json()
        w0, w1 = (datetime.fromisoformat(b["window"][k]) for k in ("from", "to"))
        times = [datetime.fromisoformat(e[0]) for e in b["units"]["testunit"]["vials"]["1"]["events"]]
        ck(all(w0 - timedelta(seconds=1) <= t <= w1 + timedelta(seconds=1) for t in times),
           "a rig %+d min off: every event still lies inside the reported window" % minutes)
    b = raw({1: [[99.0, 1.0, "low"]]}, {"generated_at": datetime.now().astimezone(
        __import__("datetime").timezone(timedelta(hours=5, minutes=30))).isoformat()}).get("/pump_events").json()
    ck(b["units"]["testunit"]["vials"]["1"]["events"][0][0][-6:] == b["window"]["to"][-6:],
       "events carry the same UTC offset as the window, whatever the rig's")
    b = raw({1: [[99.0, 1.0, "low"]]}, {"staleness_h": None}).get("/pump_events").json()
    ck("opens EARLY" in b["units"]["testunit"]["clock_note"], "the clock note says the window opens early, which it does")

    # ═══ 6. what is repeated, and what is not ════════════════════════════
    d = DashboardSettings("/nonexistent", urls_json='{"testunit": {"url": "http://op:hunter2@10.0.0.5:8050"}}')
    ck("hunter2" not in (d.error or "") and "op:" not in (d.error or ""),
       "a roster entry in the wrong shape does not echo its URL, password and all")

    hostile = "a\x1b[31m\x00b‮<script>" + "x" * 100_000
    c = raw({1: [[99.0, 1.0, hostile]] + [[99.0 + i / 1000, 1.0, "r%03d" % i + "y" * 300] for i in range(1, 300)]})
    r = c.get("/pump_events")
    v1 = r.json()["units"]["testunit"]["vials"]["1"]
    ck(len(r.content) < 120_000, "hostile pump labels do not blow up the response (%d bytes)" % len(r.content))
    ck(all(e[2] == "unrecognised" for e in v1["events"]), "  the event rows say 'unrecognised', not the rig's text")
    ck(len(v1["unattributed"]) == 1 and len(v1["unrecognised_pumps"]["labels"]) <= 5,
       "  300 distinct labels are one reason, with at most 5 shown")
    shown = json.dumps(v1["unrecognised_pumps"]["labels"])
    ck("\\u202e" not in shown and "\\u001b" not in shown and "\\u0000" not in shown,
       "  the labels shown are stripped of control and bidi characters")

    r = raw({1: []}, {"generated_at": "Q" * 200_000}).get("/pump_events")
    ck(len(r.content) < 5_000, "an absurd generated_at is not echoed (%d bytes)" % len(r.content))

    c = raw({1: [[99.0, 2.0, "low"]]}, vial_body={"evolver": "plankton"})
    v1 = c.get("/pump_events").json()["units"]["testunit"]["vials"]["1"]
    ck(v1["ok"] is False and "answers for evolver" in v1["reason"],
       "a vial's dispenses answering as another rig are refused, like the summary is")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
