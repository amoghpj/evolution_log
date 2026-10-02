#!/usr/bin/env python3
"""Exercises app.auth.load_operators() directly against malformed tokens
files -- a real bug report: a tester copied secrets/operators.example.json
as a template (exactly what it tells you to do) without deleting its
"_comment" key, and Operator(**info) crashed with a TypeError on that
string value instead of a real operator dict.

    ~/py/bin/python tests/test_auth.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_real_auth  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def good_event():
    return {
        "target": {"scope": "facility"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "inoculation",
        "provenance": "reported",
        "params": {},
        "notes": "auth test event",
    }


def main():
    # ── the exact real-world case: a leading-underscore "comment" key
    # copied straight from the example file, sitting next to a real entry ──
    client, token, operator = make_client_with_real_auth(
        extra_entries={"_comment": "Copy this file to secrets/operators.json..."}
    )
    r = client.post("/events", json=good_event(), headers={"Authorization": "Bearer %s" % token})
    ck(r.status_code == 201, "a _comment key alongside a real entry doesn't break auth (%s: %s)"
       % (r.status_code, r.text[:200]))
    ck(r.json()["operator"] == operator.initials, "the real entry still resolves correctly")

    # a bearer token that happens to equal the literal comment string must
    # not authenticate as anyone -- it was never a real entry
    r = client.post("/events", json=good_event(),
                     headers={"Authorization": "Bearer Copy this file to secrets/operators.json..."})
    ck(r.status_code == 401, "the comment's own text is not a valid token")

    # ── a malformed entry that ISN'T a comment (no leading underscore) --
    # this is a real configuration mistake, and should fail clearly (500),
    # not crash uninformatively ──
    client2, token2, operator2 = make_client_with_real_auth(
        extra_entries={"some-other-token": "not an object"}
    )
    r = client2.post("/events", json=good_event(), headers={"Authorization": "Bearer %s" % token2})
    ck(r.status_code == 500, "a malformed (non-underscore) entry fails clearly, not silently (%s)" % r.status_code)
    detail = r.json().get("detail", "")
    ck("some-other-token" in detail, "the error names the offending entry, not just 'something broke'")

    # ── an entry missing a required field ────────────────────────────────
    client3, token3, operator3 = make_client_with_real_auth(
        extra_entries={"incomplete-token": {"initials": "XX"}}  # missing git_name, git_email
    )
    r = client3.post("/events", json=good_event(), headers={"Authorization": "Bearer %s" % token3})
    ck(r.status_code == 500, "an entry missing required fields fails clearly (%s)" % r.status_code)

    # ── regression: the tokens file itself is not valid JSON at all (e.g. a
    # stray trailing comma from a hand edit) -- found by simulating a hot-
    # reload-of-tokens operator: this used to propagate as a raw, uncaught
    # json.JSONDecodeError, which FastAPI's generic exception handler turned
    # into an opaque "Internal Server Error" with nothing in the response
    # naming the tokens file as the cause, unlike every other failure mode
    # in load_operators() ──────────────────────────────────────────────────
    client4, token4, operator4 = make_client_with_real_auth()
    tokens_path = Path(tempfile.mkdtemp(prefix="operators_bad_json_")) / "operators.json"
    tokens_path.write_text('{"some-token": {"initials": "X",},}')  # trailing commas -- invalid JSON
    os.environ["OPERATOR_TOKENS_FILE"] = str(tokens_path)
    r = client4.post("/events", json=good_event(), headers={"Authorization": "Bearer %s" % token4})
    ck(r.status_code == 500, "invalid JSON in the tokens file fails clearly, not a raw crash (%s)" % r.status_code)
    ck("not valid JSON" in r.json().get("detail", ""),
       "the error specifically names the tokens file as invalid JSON, not a generic message")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
