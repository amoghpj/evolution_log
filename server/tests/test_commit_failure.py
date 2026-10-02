#!/usr/bin/env python3
"""Simulates a git commit failing after the file write already happened, via
a pre-commit hook that always exits nonzero. Confirms writer.py reverts the
working tree to HEAD rather than leaving evolution_log.json ahead of git
history.

    ~/py/bin/python tests/test_commit_failure.py
"""
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_settings  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def install_failing_precommit_hook(settings):
    hook = settings.log_repo_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)


def main():
    client, settings = make_client_with_settings()
    before_content = settings.log_file.read_text()
    before_head = subprocess.run(
        ["git", "-C", str(settings.log_repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()

    install_failing_precommit_hook(settings)

    r = client.post("/events", json={
        "target": {"line_id": "testunit-v01"},
        "timestamp": "2026-01-02T09:00:00-05:00",
        "event_type": "inoculation",
        "provenance": "reported",
        "params": {},
        "notes": "this write's commit will be forced to fail",
    })
    ck(r.status_code == 500, "a forced commit failure surfaces as 500, not 201 (%s)" % r.status_code)
    ck("reverted" in r.json()["detail"], "the error says the file was reverted, so a caller knows it's safe to retry")

    after_content = settings.log_file.read_text()
    ck(after_content == before_content,
       "evolution_log.json on disk is byte-identical to before the attempted write")

    after_head = subprocess.run(
        ["git", "-C", str(settings.log_repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    ck(after_head == before_head, "HEAD did not move")

    status = subprocess.run(
        ["git", "-C", str(settings.log_repo_path), "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    ck(status.strip() == "", "working tree is fully clean -- nothing staged, nothing modified (%r)" % status)

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
