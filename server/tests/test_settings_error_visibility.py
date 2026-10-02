#!/usr/bin/env python3
"""A real deployment hit this: EVOLVER_UNIT_PATHS wasn't set, and the
resulting RuntimeError -- correct, specific wording -- only ever reached
server stdout. get_settings()/get_evolver_config_settings() are
@lru_cache-wrapped Depends() callables; a RuntimeError raised out of one is
NOT caught by FastAPI at all, so it propagates as an opaque, bodyless
"Internal Server Error" with the real message visible only in a log the
caller (often an LLM with no shell access) can never read. Same class of
bug tests/test_auth.py already covers for get_operator()/load_operators()
-- that one has no caching, so no special handling was needed there; these
two do, because Settings()/EvolverConfigSettings() are expensive enough
(filesystem checks, JSON parsing) to want memoizing across requests.

Uses a bare, non-overridden TestClient deliberately -- every other test
file's make_client_with_settings()/make_client_with_config() override
get_settings/get_evolver_config_settings specifically to bypass this real
code path; this file is the one place that must NOT do that.

    ~/py/bin/python tests/test_settings_error_visibility.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.evolver_config import get_evolver_config_settings  # noqa: E402
from app.main import app  # noqa: E402
from tests.fixture import build_fixture_repo  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    assert not app.dependency_overrides, \
        "this test needs the REAL get_settings/get_evolver_config_settings, not another test's overrides"
    client = TestClient(app, raise_server_exceptions=False)

    # ── LOG_REPO_PATH pointing nowhere real -- get_settings() itself ───────
    os.environ["LOG_REPO_PATH"] = "/no/such/checkout/anywhere"
    os.environ.pop("EVOLVER_UNIT_PATHS", None)
    get_settings.cache_clear()
    r = client.get("/events")
    ck(r.status_code == 500,
       "a bad LOG_REPO_PATH reaches the caller as a real 500, not an unhandled crash (%s)" % r.status_code)
    ck("LOG_REPO_PATH" in r.json().get("detail", ""),
       "the message actually names LOG_REPO_PATH as the problem, in the RESPONSE body (%s)"
       % r.json().get("detail", "")[:200])

    # ── fix LOG_REPO_PATH, leave EVOLVER_UNIT_PATHS unset entirely -- the ──
    # ── exact real-world report this fix was written for ───────────────────
    real_repo = build_fixture_repo()
    os.environ["LOG_REPO_PATH"] = str(real_repo)
    os.environ.pop("EVOLVER_UNIT_PATHS", None)
    get_settings.cache_clear()
    get_evolver_config_settings.cache_clear()

    r = client.get("/config", params={"unit": "testunit"})
    ck(r.status_code == 500,
       "EVOLVER_UNIT_PATHS unset -> a real 500 reaches the caller, not just stdout (%s)" % r.status_code)
    ck("EVOLVER_UNIT_PATHS" in r.json().get("detail", ""),
       "the response body names EVOLVER_UNIT_PATHS specifically, matching what used to be stdout-only (%s)"
       % r.json().get("detail", "")[:200])

    # ── EVOLVER_UNIT_PATHS set but malformed JSON -- same code path, ────────
    # ── different real message, still must reach the response ──────────────
    os.environ["EVOLVER_UNIT_PATHS"] = "{not valid json,"
    get_evolver_config_settings.cache_clear()
    r = client.post("/config/candidate", json={"unit": "testunit", "config": {}})
    ck(r.status_code == 500, "malformed EVOLVER_UNIT_PATHS JSON also surfaces as a real 500 (%s)" % r.status_code)
    ck("not valid JSON" in r.json().get("detail", ""),
       "the response names the JSON problem specifically (%s)" % r.json().get("detail", "")[:200])

    # ── cleanup: leave the process env sane for anything run after this ────
    os.environ.pop("LOG_REPO_PATH", None)
    os.environ.pop("EVOLVER_UNIT_PATHS", None)
    get_settings.cache_clear()
    get_evolver_config_settings.cache_clear()

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
