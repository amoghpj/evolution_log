#!/usr/bin/env python3
"""Check that each eVOLVER dashboard is serving data the viewer can actually use.

Run this on a machine that can reach the dashboards:

    python3 tools/check_api.py

It reads viewer.config.json, hits every enabled unit, and reports whether the
response is not just reachable but *correct*: right evolver, right shape, vials
that match the roster in evolution_log.json, and numbers that are internally
consistent. Reachability alone is a weak test — a dashboard pointed at the
wrong experiment directory answers 200 and looks fine.

Exit code is 0 only if every enabled unit passes.
"""

import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(ROOT, "viewer.config.json")
LOG = os.path.join(ROOT, "evolution_log.json")
TIMEOUT = 6

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "
_fails, _warns = [0], [0]


def say(status, msg):
    print("[%s] %s" % (status, msg))
    if status == BAD:
        _fails[0] += 1
    elif status == WARN:
        _warns[0] += 1


def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read().decode()), dict(r.headers)


def expected_vials(log, unit):
    """Vials the log believes are running on this unit right now."""
    return sorted({L["vial"] for L in log["lines"].values()
                   if L["unit"] == unit and L["status"] == "active"})


def check_unit(unit, url, log):
    print("\n=== %s -> %s ===" % (unit, url))
    base = url.rstrip("/")

    # 1. reachable at all
    try:
        status, health, _ = get(base + "/api/v1/health")
    except urllib.error.HTTPError as e:
        say(BAD, "health returned HTTP %s. Is evolver_api registered in dashboard.py?" % e.code)
        return
    except Exception as e:
        say(BAD, "cannot reach %s (%s: %s)" % (base, type(e).__name__, e))
        print("       - is dashboard.py running on that host?")
        print("       - does it bind 0.0.0.0 rather than 127.0.0.1?")
        print("       - firewall open on that port?")
        return
    say(OK, "reachable, HTTP %s" % status)

    # 2. it is the unit we think it is
    got = health.get("evolver")
    if got is None:
        say(WARN, "health does not report an evolver name; cannot confirm identity. "
                  "Check experiment_settings.evolver_name in experiment_parameters.yaml")
    elif got != unit:
        say(BAD, "this url serves '%s', not '%s'. Two dashboards on one host take "
                 "ports by start order -- swap the urls in viewer.config.json" % (got, unit))
        return
    else:
        say(OK, "identity confirmed: evolver=%s, experiment=%s" % (got, health.get("experiment")))

    # 3. the summary the viewer will actually poll
    try:
        status, data, headers = get(base + "/api/v1/vials")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode() or "{}")
        say(BAD, "vials returned HTTP %s: %s" % (e.code, body.get("error", "no reason given")))
        return
    except Exception as e:
        say(BAD, "vials failed (%s: %s)" % (type(e).__name__, e))
        return

    if data.get("schema") != "or05.vials/1":
        say(BAD, "unexpected schema %r -- viewer and API are different versions" % data.get("schema"))
    if headers.get("Access-Control-Allow-Origin") != "*":
        say(BAD, "no CORS header. The browser will refuse this response even though curl accepts it.")
    else:
        say(OK, "CORS header present, so the browser can read it")

    if not data.get("pump_calibration"):
        say(WARN, "pump_cal.json not found: volumes and burn rates will all be null. "
                  "Concentrations still work.")

    # A dashboard pointed at a directory that exists but holds no data answers
    # 200 and looks healthy. An experiment clock stuck at zero while vials are
    # configured is the tell.
    # Tested on the LAST WRITE, not the clock: elapsed_h now advances with wall
    # time, so a data-less directory created three hours ago reports a
    # plausible 3.0 h run and this check -- the one diagnostic for the most
    # common deployment mistake -- stopped firing after its first seconds.
    wrote_nothing = not any(v.get("last_event_h") for v in data.get("vials", []))
    if data.get("vials") and wrote_nothing:
        say(BAD, "no vial has ever written a pump event, with %d vials configured: the "
                 "dashboard is reading an experiment directory with no data in it. Check "
                 "exp_name in experiment_parameters.yaml and that dashboard.py was started "
                 "from the right working directory." % len(data["vials"]))
    if data.get("clock_problem"):
        say(BAD, "clock: %s" % data["clock_problem"])

    # 4. does the roster agree with the log
    api_vials = sorted(v["vial"] for v in data.get("vials", []))
    want = expected_vials(log, unit)
    if not api_vials:
        say(BAD, "no vials in the response -- check to_run in experiment_parameters.yaml")
    elif api_vials == want:
        say(OK, "vials match the log exactly: %s" % api_vials)
    else:
        missing, extra = sorted(set(want) - set(api_vials)), sorted(set(api_vials) - set(want))
        say(WARN, "roster differs from the log. api=%s log=%s%s%s" % (
            api_vials, want,
            ("; not served: %s" % missing) if missing else "",
            ("; not in log: %s" % extra) if extra else ""))
        print("       This is expected right after a vial is added or terminated;")
        print("       if it persists, one of the two is out of date.")

    # 5. are the numbers self-consistent
    stale, nodata, negative = [], [], []
    for v in data.get("vials", []):
        br = v.get("burn_rate_mL_per_h") or {}
        if br.get("total") is None:
            nodata.append(v["vial"])
        elif br["total"] < 0:
            negative.append(v["vial"])
        if (v.get("stale_h") or 0) > 2:
            stale.append((v["vial"], v["stale_h"]))
    if negative:
        say(BAD, "negative burn rate on vials %s -- pump calibration is wrong" % negative)
    if nodata and data.get("pump_calibration"):
        say(WARN, "vials with no rate despite calibration present: %s" % nodata)
    if stale:
        say(WARN, "vials with no pump event in over 2 h: %s" %
            ", ".join("v%d (%.1f h)" % s for s in stale))
        print("       A vial that stops dispensing while its neighbours continue is")
        print("       either dead, blocked, or below its OD setpoint.")

    # 6. the diagnostic that ties dosing to concentration
    inspected = 0
    for v in data.get("vials", []):
        hf, c = v.get("high_fraction"), v.get("drug_concentration_g_per_L")
        if hf is None or c is None:
            continue
        inspected += 1
        predicted = max(0.0, min(1.0, c / 5.0))       # assumes a 5 g/L high reservoir
        if abs(hf - predicted) > 0.25:
            say(WARN, "vial %d: drawing %.0f%% high media but reports %.2f g/L "
                      "(expected ~%.0f%%). Controller belief and actual dosing may have "
                      "come apart." % (v["vial"], 100*hf, c, 100*predicted))

    # 7. the endpoint the media report depends on
    try:
        status, cons, _ = get(base + "/api/v1/consumption?since_h=0")
    except urllib.error.HTTPError as e:
        say(BAD, "consumption returned HTTP %s. This dashboard predates "
                 "/api/v1/consumption -- GET /media falls back to aggregating "
                 "/dispenses itself, which works but is far heavier. Deploy the "
                 "current tools/evolver_api.py here." % e.code)
        cons = None
    except ValueError as e:
        # A dashboard without the route answers 200 with its own HTML page, not
        # 404, so THIS is the branch a real un-updated rig takes -- and it used
        # to print a bare JSONDecodeError instead of the diagnosis the 404
        # branch already had.
        say(BAD, "consumption did not return JSON (%s). This dashboard almost certainly "
                 "predates /api/v1/consumption -- a Dash app answers 200 with its own "
                 "index page for a route it does not have. GET /media falls back to "
                 "aggregating /dispenses, which works but is far heavier; deploy the "
                 "current tools/evolver_api.py here." % e)
        cons = None
    except Exception as e:
        say(BAD, "consumption failed (%s: %s)" % (type(e).__name__, e))
        cons = None

    if cons is not None:
        if cons.get("schema") != "or05.consumption/1":
            say(BAD, "unexpected consumption schema %r" % cons.get("schema"))
        else:
            say(OK, "consumption answers, schema %s" % cons["schema"])
        if not cons.get("clock_ok", True):
            say(BAD, "clock_ok is False for since_h=0, which cannot happen on a "
                     "healthy rig: the controller clock is behind its own zero.")
        if cons.get("generated_at", "")[-3:-2] != ":":
            say(WARN, "generated_at has no colon in its offset (%s). Older "
                      "evolver_api; a caller on Python 3.10 cannot parse it."
                      % cons.get("generated_at"))

        # Consumption since zero must agree with the cumulative figure the
        # summary already reports -- two independent paths over the same log.
        # They diverge only where a pump log holds a row that is neither in1
        # nor in2: vial_rates charges it to `high`, consumption ignores it.
        # In practice that row is the 0,0 placeholder, worth 0 mL either way,
        # so a real difference here means a malformed pump log.
        cum = {v["vial"]: (v.get("cumulative_mL") or {}).get("total")
               for v in data.get("vials", [])}
        drift = []
        for v in cons.get("vials", []):
            a, b = v.get("total_mL"), cum.get(v["vial"])
            if a is None or b is None:
                continue
            if abs(a - b) > 0.05:
                drift.append((v["vial"], a, b))
        if drift:
            say(WARN, "consumption(since_h=0) disagrees with cumulative_mL on "
                      "%s. Check that vial's pump log for rows whose pump column "
                      "is neither in1 nor in2."
                      % ", ".join("v%d (%.2f vs %.2f mL)" % d for d in drift))
        elif cons.get("vials"):
            say(OK, "consumption since zero agrees with cumulative_mL on every vial")

    if data.get("vials") and not inspected:
        # Printing nothing is not the same as passing. An idle rig's burn rates
        # decay to zero, high_fraction goes null, and this check -- whose
        # message is "Controller belief and actual dosing may have come apart"
        # -- silently inspects no vials at all. That is exactly what a hold
        # looks like, and this experiment holds deliberately.
        say(WARN, "the dosing-vs-concentration check inspected 0 of %d vials: no vial has "
                  "both a burn rate and a concentration right now (every vial idle for "
                  "longer than the window, or no drugconc data). Not a pass."
                  % len(data["vials"]))

    print("       elapsed_h=%s  window_h=%s  n_vials=%s"
          % (data.get("elapsed_h"), data.get("window_h"), data.get("n_vials")))


def main():
    if not os.path.exists(CONFIG):
        print("no viewer.config.json at %s" % CONFIG)
        return 2
    cfg = json.load(open(CONFIG))
    log = json.load(open(LOG)) if os.path.exists(LOG) else {"lines": {}}

    units = {k: v for k, v in cfg.get("units", {}).items() if v.get("enabled", True)}
    disabled = [k for k, v in cfg.get("units", {}).items() if not v.get("enabled", True)]
    if disabled:
        print("skipping disabled: %s" % ", ".join(disabled))
    if not units:
        print("no enabled units in viewer.config.json")
        return 2

    for unit, spec in units.items():
        check_unit(unit, spec["url"], log)

    warn = (" with %d warning(s)" % _warns[0]) if _warns[0] else ""
    print("\n%s" % ("all checks passed" + warn if not _fails[0]
                    else "%d check(s) failed%s" % (_fails[0], warn)))
    return 1 if _fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
