#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_lines.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client, make_client_with_settings  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    client = make_client()

    r = client.get("/lines")
    ck(r.status_code == 200, "GET /lines returns 200")
    body = r.json()
    ck(body["count"] == 3, "lists all 3 fixture lines")
    ids = {line["line_id"] for line in body["lines"]}
    ck(ids == {"testunit-v01", "testunit-v02", "testunit-v03"}, "all 3 line ids present")
    ck("events" not in body["lines"][0], "list route omits full event histories (summary, not raw log)")

    # regression: media_switch_count was silently missing from GET /lines'
    # own summary (_summarize's hand-picked field list), even though it's
    # required on the full line object -- the whole point of the counter
    # was to make a specific experimental intervention visible without
    # having to pull every line's full events[] one at a time.
    ck(all("media_switch_count" in line for line in body["lines"]),
       "every summarized line reports media_switch_count")
    v01 = next(l for l in body["lines"] if l["line_id"] == "testunit-v01")
    ck(v01["media_switch_count"] == 0, "the summary's media_switch_count matches the full object's (0)")

    r = client.get("/lines", params={"status": "active"})
    ck(r.json()["count"] == 2, "status=active filters to the two active lines")
    ck({l["line_id"] for l in r.json()["lines"]} == {"testunit-v01", "testunit-v03"},
       "the active ones are testunit-v01 and testunit-v03")

    r = client.get("/lines", params={"unit": "testunit"})
    ck(r.json()["count"] == 3, "unit filter matches all 3 (all are testunit)")

    r = client.get("/lines", params={"unit": "nonexistent-unit"})
    ck(r.json()["count"] == 0, "an unknown unit filters to nothing, not an error")

    # regression: an invalid ?status= used to silently return zero rows,
    # indistinguishable from "no lines genuinely match" -- found by
    # simulating a filter-combination probe. (unit stays open-ended above --
    # there's no closed enum of real units to validate against the same way.)
    r = client.get("/lines", params={"status": "bogus_status"})
    ck(r.status_code == 422, "an invalid status value -> 422, not a silent empty result")
    ck("active" in r.text and "ended" in r.text, "the problem names the real valid values")

    r = client.get("/lines/testunit-v01")
    ck(r.status_code == 200, "GET /lines/{id} returns 200 for a real line")
    ck(r.json()["events"][0]["event_id"] == "EVT-00001", "detail route includes the full event history")

    r = client.get("/lines/does-not-exist")
    ck(r.status_code == 404, "GET /lines/{id} 404s for an unknown line id, not a 500 or empty 200")

    # ── regression: GET /lines is sorted by (vial, unit), not left in
    # whatever order the log's own lines{} dict happens to iterate in --
    # found by simulating an operator resolving an ambiguous "vial N died"
    # instruction with more than one candidate line across units. A new
    # line always lands LAST in dict-insertion order regardless of its
    # vial number, so restarting into a low, already-used vial is exactly
    # the case that would expose unsorted output. ───────────────────────────
    client2, settings2 = make_client_with_settings()
    r = client2.post("/lines", json={
        "begin_mode": "restart", "predecessor_line_id": "testunit-v02",  # ended, vial 2
        "new_line": {
            "unit": "testunit", "vial": 2, "strain": "s", "initial_media": "LB", "current_media": "LB",
            "mode": "constant", "t0": "2026-01-10T09:00:00-05:00",
            "pg_regime": {"low": {"value_g_per_L": 0.5, "value_mM": 3.9648, "unit_primary": "g/L"},
                          "high": {"value_g_per_L": 5.0, "value_mM": 39.6479, "unit_primary": "g/L"},
                          "effective_from": "2026-01-10T09:00:00-05:00"},
            "reservoirs": {"low": "testunit/LB-0", "high": "testunit/LB-5"},
            "founding_event": {"timestamp": "2026-01-10T09:00:00-05:00", "event_type": "inoculation",
                                "provenance": "reported", "params": {}, "notes": "restart into vial 2"},
        },
    })
    ck(r.status_code == 201, "restart for the sort-order regression setup succeeds")
    r = client2.get("/lines")
    vials = [line["vial"] for line in r.json()["lines"]]
    ck(vials == sorted(vials), "GET /lines is sorted by vial (%s)" % vials)
    ids_at_vial_2 = [line["line_id"] for line in r.json()["lines"] if line["vial"] == 2]
    ck(len(ids_at_vial_2) == 2 and abs(
        [line["line_id"] for line in r.json()["lines"]].index(ids_at_vial_2[0])
        - [line["line_id"] for line in r.json()["lines"]].index(ids_at_vial_2[1])) == 1,
       "both lines that have ever occupied vial 2 are ADJACENT in the response, not split apart "
       "by insertion order (%s)" % ids_at_vial_2)

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
