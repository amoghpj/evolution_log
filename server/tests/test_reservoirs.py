#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_reservoirs.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    client = make_client()

    r = client.get("/reservoirs")
    ck(r.status_code == 200, "GET /reservoirs returns 200")
    body = r.json()
    ck(body["count"] == 2, "lists both fixture reservoirs")

    r = client.get("/reservoirs", params={"unit": "testunit"})
    ck(r.json()["count"] == 2, "unit filter matches both")

    r = client.get("/reservoirs", params={"status": "retired"})
    ck(r.json()["count"] == 0, "status=retired matches none (both are active)")

    # reservoir_id contains a slash -- the route must accept it whole, not
    # truncate at the first path segment.
    r = client.get("/reservoirs/testunit/LB-0")
    ck(r.status_code == 200, "GET /reservoirs/{id} handles a slash-bearing id")
    ck(r.json()["id"] == "testunit/LB-0", "returns the reservoir with the exact id asked for")
    ck(r.json()["role"] == "low", "returns the right reservoir, not just any 200")

    r = client.get("/reservoirs/testunit/LB-5")
    ck(r.json()["role"] == "high", "the other reservoir resolves correctly too")

    r = client.get("/reservoirs/does-not-exist")
    ck(r.status_code == 404, "unknown reservoir id 404s")

    # regression: an invalid ?status= used to silently return zero rows,
    # indistinguishable from "no reservoirs genuinely match" -- found by
    # simulating a filter-combination probe.
    r = client.get("/reservoirs", params={"status": "bogus_status"})
    ck(r.status_code == 422, "an invalid status value -> 422, not a silent empty result")
    ck("active" in r.text and "retired" in r.text, "the problem names the real valid values")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
