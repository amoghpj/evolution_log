"""A stand-in eVOLVER dashboard, for testing GET /media's pump view without a rig.

Answers the same four routes tools/evolver_api.py serves, driven by a plain
dict per unit so a test can express "this rig restarted", "this one has no
pump calibration", "this one answers for the other unit" as data rather than
as mocking. One transport serves every unit at once and dispatches on the
request's hostname, which is what makes the identity-confusion case
expressible at all: the URL says one rig, the payload says another.

Every request is recorded in `calls`, so a test can assert on what was NOT
fetched. "A historical `at` contacts no rig" is a claim about absence, and
absence is only checkable if something counts.

httpx.MockTransport rather than ASGITransport: the route calls the dashboard
synchronously (FastAPI runs a sync route in a threadpool), and ASGITransport
is async-only.
"""
import json
from typing import Any

import httpx


def rig(unit: str, vials: dict[int, dict], **kw) -> dict:
    """One unit's behaviour. vials maps vial number -> {low_mL, high_mL, n_events}."""
    spec = {
        "unit": unit,
        "vials": vials,
        "has_consumption": True,
        "calibrated": True,
        # None means "work it out from `since`, the way the real endpoint
        # does". A test forces a value only when the point IS that flag.
        "clock_ok": None,
        "covers_window": None,
        "answers_as": None,        # None -> answers as itself
        "reports_no_name": False,
        "experiment": "%s-run" % unit,
        # Large enough that the fixture's January level readings fall inside
        # this run. A short clock is not neutral: it makes every window open
        # before the pump log does, which the real endpoint reports as
        # covers_window=False -- a refusal, correctly, but not the one a test
        # about something else is trying to exercise.
        "elapsed_h": 9000.0,
        # NOW, not a fixed string: the server refuses a rig whose clock
        # disagrees with its own, so a hardcoded timestamp makes every fixture
        # fail the moment the wall clock moves past it. A test that wants a
        # skewed rig sets this explicitly.
        "generated_at": None,
        "http_status": None,       # force a status on /consumption
        "summary_status": None,    # force a status on /api/v1/vials
        "omit_vials": (),          # vials to leave out of the payload
        "null_volumes": False,     # report the vials, with null volumes
        "hostile_text": None,      # rig-authored prose, to test what gets relayed
        "not_json": False,         # answer /consumption with a Dash HTML page, 200
        "summary_not_json": False, # answer /api/v1/vials with one too
        # A rig running the CURRENT evolver_api reports staleness_h from its
        # corrected clock. Its absence is how the server tells an un-updated
        # rig -- whose elapsed_h is the last write -- from one whose fallback
        # conversion is exact.
        "reports_staleness": True,
        "status_override": None,   # force a status on /consumption (e.g. 301)
        "huge_body": False,        # answer with a body past the server's cap
        "wrong_schema": False,     # valid JSON, not an or05.consumption payload
        "slow_s": 0.0,             # sleep before answering
        "clock_problem": None,     # the rig's own verdict on its clock
        "vial_problem": None,      # a per-vial calibration diagnosis
        "vial_log_gap_h": None,    # per-vial truncated-log gap
        "vial_dropped_rows": 0,    # rows the rig could not parse
        "odd_role": False,         # emit a dispense whose role is neither low nor high
    }
    spec.update(kw)
    return spec


def _generated_at(spec: dict) -> str:
    from datetime import datetime

    return spec["generated_at"] or datetime.now().astimezone().isoformat()


def _evolver_name(spec: dict):
    if spec["reports_no_name"]:
        return None
    return spec["answers_as"] or spec["unit"]


def _since_h(spec: dict, since: str | None) -> float:
    """The real endpoint's own conversion, so the fake cannot be more
    forgiving than the thing it stands in for."""
    if since is None:
        return 0.0
    from datetime import datetime

    delta_h = ((datetime.fromisoformat(_generated_at(spec)) - datetime.fromisoformat(since))
               .total_seconds() / 3600.0)
    return spec["elapsed_h"] - delta_h


def _consumption_body(spec: dict, since: str | None) -> dict:
    since_h = _since_h(spec, since)
    clock_ok = spec["clock_ok"] if spec["clock_ok"] is not None else since_h <= spec["elapsed_h"]
    covers = spec["covers_window"] if spec["covers_window"] is not None else since_h >= 0
    vials = []
    for vial, v in spec["vials"].items():
        if vial in spec["omit_vials"]:
            continue
        if spec["null_volumes"] or not spec["calibrated"]:
            vials.append({"vial": vial, "low_mL": None, "high_mL": None,
                          "total_mL": None, "n_events": v.get("n_events", 0)})
        else:
            low, high = v.get("low_mL", 0.0), v.get("high_mL", 0.0)
            vials.append({"vial": vial, "low_mL": low, "high_mL": high,
                          "total_mL": low + high, "n_events": v.get("n_events", 0),
                          "problem": spec["vial_problem"],
                          "log_gap_h": spec["vial_log_gap_h"],
                          "dropped_rows": spec["vial_dropped_rows"]})
    return {
        "schema": "or05.consumption/1",
        "evolver": _evolver_name(spec),
        "experiment": spec["experiment"],
        "generated_at": _generated_at(spec),
        "elapsed_h": spec["elapsed_h"],
        "since_h": round(since_h, 4),
        "since": since,
        "since_resolved_from": "wall_clock",
        "clock_ok": clock_ok,
        "covers_window": covers,
        "pump_calibration": spec["calibrated"],
        "clock_problem": spec["clock_problem"],
        "n_vials": len(vials),
        "vials": vials,
    }


def _summary_body(spec: dict) -> dict:
    return {
        "schema": "or05.vials/1",
        "evolver": _evolver_name(spec),
        "experiment": spec["experiment"],
        "generated_at": _generated_at(spec),
        "elapsed_h": spec["elapsed_h"],
        "pump_calibration": spec["calibrated"],
        **({"staleness_h": 0.1} if spec["reports_staleness"] else {}),
        "n_vials": len(spec["vials"]),
        "vials": [{"vial": v} for v in spec["vials"]],
    }


def _dispenses_body(spec: dict, vial: int, since_h: float) -> dict:
    v = spec["vials"][vial]
    rows: list[Any] = []
    if "events" in v:
        # Explicit [time_h, mL, role] rows, filtered the way the real endpoint
        # filters them (time > since_h), for tests about WHICH events a window
        # returns rather than what they sum to.
        rows = [list(r) for r in v["events"] if r[0] > since_h]
    # One event per role carrying the whole volume: the fallback integrates
    # whatever it is handed, and splitting it finer tests nothing extra.
    elif spec["calibrated"]:
        if v.get("low_mL"):
            rows.append([spec["elapsed_h"] - 0.5, v["low_mL"], "low"])
        if v.get("high_mL"):
            role = "sideways" if spec["odd_role"] else "high"
            rows.append([spec["elapsed_h"] - 0.4, v["high_mL"], role])
    else:
        rows.append([spec["elapsed_h"] - 0.5, None, "low"])
    return {
        "schema": "or05.vials/1", "vial": vial, "since_h": since_h,
        "pump_calibration": spec["calibrated"],
        "elapsed_h": spec["elapsed_h"], "generated_at": _generated_at(spec),
        "n": len(rows), "dispenses": rows,
    }


def make_handler(rigs: dict[str, dict], calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        params = dict(request.url.params)
        spec = rigs.get(host)
        calls.append((host, path, params))
        if spec is None:
            return httpx.Response(404, json={"error": "no such rig at %s" % host})

        if path == "/api/v1/consumption":
            if spec["slow_s"]:
                import time as _t
                _t.sleep(spec["slow_s"])
            if spec["huge_body"]:
                return httpx.Response(200, json={"schema": "or05.consumption/1",
                                                 "pad": "x" * (400 * 1024)})
            if spec["hostile_text"]:
                return httpx.Response(503, json={"ok": False,
                                                 "error": spec["hostile_text"]})
            if spec["wrong_schema"]:
                return httpx.Response(200, json={"error": "go away"})
            if spec["status_override"]:
                return httpx.Response(spec["status_override"],
                                      json=_consumption_body(spec, params.get("since")))
            if spec["http_status"]:
                return httpx.Response(spec["http_status"], json={"error": "forced"})
            if not spec["has_consumption"]:
                return httpx.Response(404, json={"error": "not found"})
            if spec["not_json"]:
                # What a Dash app really does for a route it does not have:
                # 200, text/html, its own index page. Not a 404.
                return httpx.Response(
                    200, text="<!DOCTYPE html><html><title>Evolver Dashboard</title></html>",
                    headers={"content-type": "text/html; charset=utf-8"})
            return httpx.Response(200, json=_consumption_body(spec, params.get("since")))

        if path == "/api/v1/vials":
            if spec["summary_not_json"]:
                return httpx.Response(
                    200, text="<!DOCTYPE html><html><title>Evolver Dashboard</title></html>",
                    headers={"content-type": "text/html; charset=utf-8"})
            if spec["summary_status"]:
                return httpx.Response(spec["summary_status"], json={"error": "forced"})
            return httpx.Response(200, json=_summary_body(spec))

        if path.startswith("/api/v1/vials/") and path.endswith("/dispenses"):
            vial = int(path.split("/")[4])
            if vial not in spec["vials"]:
                return httpx.Response(404, json={"error": "vial not configured"})
            since_h = float(params.get("since_h", 0.0))
            return httpx.Response(200, json=_dispenses_body(spec, vial, since_h))

        if path == "/api/v1/health":
            return httpx.Response(200, json={"schema": "or05.vials/1", "ok": True,
                                             "evolver": _evolver_name(spec),
                                             "experiment": spec["experiment"]})

        return httpx.Response(404, json={"error": "no such route %s" % path})

    return handler


def client_for(rigs: dict[str, dict], calls: list) -> httpx.Client:
    # follow_redirects=False mirrors the route's own client. Without it the
    # suite was validating httpx's DEFAULT rather than the setting the route
    # actually sets, so a regression there would not have failed a test.
    return httpx.Client(follow_redirects=False,
                        transport=httpx.MockTransport(make_handler(rigs, calls)))


def dead_client(calls: list) -> httpx.Client:
    """A rig that is simply not there -- the case viewer.html describes as
    indistinguishable from a dashboard sending no CORS headers."""
    def refuse(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.url.path, "refused"))
        raise httpx.ConnectError("Connection refused", request=request)

    return httpx.Client(transport=httpx.MockTransport(refuse))


def dashboards_for(units: dict[str, str], timeout_s: float = 3.0):
    """A DashboardSettings without a viewer.config.json behind it."""
    from app.dashboards import DashboardSettings
    from pathlib import Path

    return DashboardSettings(Path("/nonexistent"), urls_json=json.dumps(units),
                             timeout_s=timeout_s)
