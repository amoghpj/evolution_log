#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_vials.py"""
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

    r = client.get("/vials/testunit/1")
    ck(r.status_code == 200, "GET /vials/{unit}/{vial} returns 200 for a real vial")
    body = r.json()
    ck(body["occupied_by"] == "testunit-v01", "vial 1 is occupied by the active line, not the ended one")
    ck(body["line"]["status"] == "active", "embedded line detail matches the occupant")

    r = client.get("/vials/testunit/2")
    ck(r.json()["occupied_by"] is None,
       "vial 2's line has ENDED, so nothing currently occupies it -- history lives in GET /lines, not here")
    ck(r.json()["line"] is None, "no line embedded when nothing occupies the vial")

    r = client.get("/vials/testunit/99")
    ck(r.json()["occupied_by"] is None, "a vial number nothing has ever used just reports unoccupied")

    r = client.get("/vials/no-such-unit/1")
    ck(r.status_code == 404, "an unknown unit 404s rather than silently reporting unoccupied")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
