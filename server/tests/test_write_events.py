#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_write_events.py"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_settings, make_client_with_real_auth  # noqa: E402
from app import writer  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def git(*args, settings):
    return subprocess.run(
        ["git", "-C", str(settings.log_repo_path), *args],
        capture_output=True, text=True,
    )


def good_body(**overrides):
    body = {
        "target": {"line_id": "testunit-v01"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "inoculation",
        "provenance": "reported",
        "params": {},
        "notes": "a test event",
    }
    body.update(overrides)
    return body


def main():
    # ── happy path: line-scoped ───────────────────────────────────────────
    client, settings = make_client_with_settings()
    head_before = git("rev-parse", "HEAD", settings=settings).stdout.strip()

    r = client.post("/events", json=good_body())
    ck(r.status_code == 201, "successful line-scoped write returns 201 (%s)" % r.status_code)
    event = r.json()
    ck(event["event_id"] == "EVT-00008", "server assigned the next monotonic event_id")
    ck(event["operator"] == "TEST", "operator comes from the authenticated identity, not the request body")
    ck("scope" not in event, "a line-scoped event carries no scope key")

    on_disk = json.loads(settings.log_file.read_text())
    ck(any(e["event_id"] == "EVT-00008" for e in on_disk["lines"]["testunit-v01"]["events"]),
       "the new event is actually in the line's events[] on disk")
    ck(on_disk["log_meta"]["event_counter"] == 8, "log_meta.event_counter recomputed to 8")

    head_after = git("rev-parse", "HEAD", settings=settings).stdout.strip()
    ck(head_after != head_before, "a new git commit was made")
    author = git("log", "-1", "--format=%an <%ae>", settings=settings).stdout.strip()
    ck(author == "Test Operator <test@example.com>", "commit author matches the operator identity (%s)" % author)
    msg = git("log", "-1", "--format=%s", settings=settings).stdout.strip()
    ck(msg.startswith("EVT-00008:"), "commit message starts with the new event id")

    # a second write on a fresh client against the SAME fixture gets the next id
    r2 = client.post("/events", json=good_body(notes="a second test event"))
    ck(r2.json()["event_id"] == "EVT-00009", "ids keep climbing monotonically across calls")

    client, settings = make_client_with_settings()
    r = client.post("/events", json=good_body(elapsed_h=500))
    ck(r.status_code == 201,
       "a large-but-positive elapsed_h is still accepted, even off from this line's own t0 -- "
       "a real, legitimate convention (a shared/facility reference point) this API deliberately "
       "does not reject")

    # ── happy path: facility-scoped ───────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json=good_body(target={"scope": "facility"}))
    ck(r.status_code == 201, "facility-scoped write returns 201")
    ck(r.json()["scope"] == "facility", "facility-scoped event carries scope: facility")
    on_disk = json.loads(settings.log_file.read_text())
    ck(any(e["event_id"] == r.json()["event_id"] for e in on_disk["experiment_events"]),
       "the new event landed in experiment_events, not a line")

    # ── target validation ─────────────────────────────────────────────────
    client, settings = make_client_with_settings()
    r = client.post("/events", json=good_body(target={}))
    ck(r.status_code == 422, "neither line_id nor scope set -> 422")
    r = client.post("/events", json=good_body(target={"line_id": "testunit-v01", "scope": "facility"}))
    ck(r.status_code == 422, "both line_id and scope set -> 422")

    r = client.post("/events", json=good_body(target={"line_id": "does-not-exist"}))
    ck(r.status_code == 404, "a target line that doesn't exist -> 404")

    # ── schema / cross-field validation failures leave the file untouched ─
    client, settings = make_client_with_settings()
    before = settings.log_file.read_text()

    r = client.post("/events", json=good_body(params={"some_unregistered_key": 1}))
    ck(r.status_code == 422, "an unregistered params key -> 422")
    ck(any("some_unregistered_key" in p for p in r.json()["detail"]), "the problem names the offending key")

    r = client.post("/events", json=good_body(event_type="not_a_declared_type"))
    ck(r.status_code == 422, "an undeclared event_type -> 422")

    r = client.post("/events", json=good_body(caused_by_event="EVT-99999"))
    ck(r.status_code == 422, "caused_by_event referencing a nonexistent event -> 422")
    ck(any("EVT-99999" in p for p in r.json()["detail"]), "the problem names the dangling reference")

    r = client.post("/events", json=good_body(timestamp="not-a-timestamp"))
    ck(r.status_code == 422, "a malformed timestamp -> 422 (schema pattern rejects it)")

    # regression: elapsed_h is documented as "hours since this line's own
    # t0" but real data proves it's sometimes reckoned from a shared/
    # facility reference point instead (a batch of real historical events
    # across several lines share one elapsed_h despite different t0s) --
    # so only the one invariant real data actually supports is enforced:
    # elapsed_h can never be negative. Found by simulating an elapsed_h-
    # consistency probe.
    r = client.post("/events", json=good_body(elapsed_h=-5))
    ck(r.status_code == 422, "a negative elapsed_h -> 422")
    ck("negative" in r.json()["detail"][0], "the problem names why: elapsed_h can't be negative")

    # regression: an unrecognized TOP-LEVEL field (e.g. the deprecated
    # corrected_from/corrected_at idiom, guessed to live at the top level by
    # analogy with supersedes) used to be silently dropped by pydantic's
    # default "ignore extra" behavior -- a 201 with the caller's actual
    # intent quietly discarded, not an error. Found by simulating an
    # operator following real historical precedent for the OLD correction
    # idiom instead of supersedes.
    body = good_body()
    body["corrected_from"] = {"value": 1.0, "unit": "L"}
    body["corrected_at"] = "2026-01-01T09:00:00-05:00"
    r = client.post("/events", json=body)
    ck(r.status_code == 422, "an unrecognized top-level field (corrected_from) -> 422, not silently dropped")
    ck(any(e.get("type") == "extra_forbidden" for e in r.json()["detail"]),
       "the problem is specifically 'extra field not permitted', not some other error")

    after = settings.log_file.read_text()
    ck(after == before, "none of the rejected writes touched the file on disk")

    # ── append-only invariant, exercised directly (not reachable via the
    # public API, since operators can't name an existing event_id) ────────
    old_log = {"lines": {"a": {"events": [{"event_id": "EVT-00001", "notes": "original"}]}}, "experiment_events": []}
    tampered = {"lines": {"a": {"events": [{"event_id": "EVT-00001", "notes": "TAMPERED"}]}}, "experiment_events": []}
    lineage = writer_lineage_stub()
    try:
        writer.assert_pure_append(old_log, tampered, lineage)
        ck(False, "assert_pure_append should have raised on a modified existing event")
    except writer.WriteConflict:
        ck(True, "assert_pure_append refuses a write that alters an existing event")

    # ── auth ───────────────────────────────────────────────────────────────
    client, valid_token, operator = make_client_with_real_auth()

    r = client.post("/events", json=good_body())
    ck(r.status_code == 401, "no Authorization header -> 401")

    r = client.post("/events", json=good_body(), headers={"Authorization": "Bearer wrong-token"})
    ck(r.status_code == 401, "wrong bearer token -> 401")

    r = client.post("/events", json=good_body(), headers={"Authorization": "Bearer %s" % valid_token})
    ck(r.status_code == 201, "valid bearer token -> 201")
    ck(r.json()["operator"] == operator.initials, "operator field matches the token's mapped initials")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


def writer_lineage_stub():
    """A minimal stand-in for the real tools/lineage.py module, exposing
    just what _assert_pure_append needs (unique_events), so this one
    direct-call test doesn't need a whole fixture repo to import the real
    module from."""
    class _Stub:
        @staticmethod
        def unique_events(log):
            out = {}
            for line in log.get("lines", {}).values():
                for e in line.get("events", []):
                    out.setdefault(e["event_id"], e)
            for e in log.get("experiment_events", []):
                out.setdefault(e["event_id"], e)
            return out
    return _Stub()


if __name__ == "__main__":
    sys.exit(main())
