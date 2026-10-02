#!/usr/bin/env python3
"""The shared validator must be ONE definition, not two that drift.

evolver_code/config_validation.py at the repo root is canonical, and since
server and log share one repo (2026-10-02) the server loads that file directly.
Before that, the server repo carried a SYMLINK to it, and before that a checked-
in copy; this test asserted each in turn. A validator that disagrees between the
server and the rig is worse than no validator: the server would commit a config
the rig then refuses, or accepts on different terms. So this asserts there is
exactly one file, and that no vendored copy has crept back under server/.

Also covers CONFIG_VALIDATOR_PATH, the env var that overrides which copy/
generation gets loaded (added alongside the symlink, for a deployment where the
server and the rig checkout aren't nested together, or for rolling out a
different validator generation on purpose).

    python3 tests/test_config_validation_shared.py
"""
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_ROOT = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(SERVER_ROOT)
sys.path.insert(0, SERVER_ROOT)

CANONICAL = os.path.join(REPO_ROOT, "evolver_code", "config_validation.py")
VENDORED = os.path.join(SERVER_ROOT, "evolver_code", "config_validation.py")

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    ck(os.path.isfile(CANONICAL), "canonical evolver_code/config_validation.py exists")
    ck(not os.path.lexists(VENDORED),
       "no second copy (file or symlink) under server/evolver_code/ that could drift")
    if _fails:
        print("\nRESULT: PROBLEMS (%d failed)" % len(_fails))
        return 1

    from app import config_validator as V
    ck(str(V.SHARED_MODULE_PATH) == os.path.realpath(CANONICAL),
       "with no override, the server loads the canonical file at the repo root")
    for name in ("validate_config", "ModeNotImplemented", "LIVE_FIELDS",
                 "extract_live_values", "validate_live_values"):
        ck(hasattr(V, name), "app.config_validator still re-exports %s" % name)

    # the re-export must behave, not merely exist
    problems, _ = V.validate_config({"experiment_settings": {
        "exp_name": "x", "operation": {"mode": "pumpcontrol_ramp"},
        "per_vial_settings": [{"vial": 0, "to_run": False, "volume": 22.0, "temperature": 37}]}})
    ck(problems == [], "a minimal valid config still passes through the re-export")
    try:
        V.validate_config({"experiment_settings": {"operation": {"mode": "turbidostat"}}})
        ck(False, "an unsupported mode still raises ModeNotImplemented")
    except V.ModeNotImplemented:
        ck(True, "an unsupported mode still raises ModeNotImplemented")

    # CONFIG_VALIDATOR_PATH override -- a fresh interpreter, since app.config_validator
    # resolves the path once at import time; this session already has the default
    # loaded, so the override has to be proven in a subprocess, not this process.
    env = dict(os.environ, CONFIG_VALIDATOR_PATH=CANONICAL)
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); from app import config_validator as V; "
         "print(V.SHARED_MODULE_PATH)" % SERVER_ROOT],
        env=env, capture_output=True, text=True,
    )
    ck(result.returncode == 0, "CONFIG_VALIDATOR_PATH override doesn't crash the import (%s)" % result.stderr[-300:])
    ck(result.stdout.strip() == os.path.realpath(CANONICAL),
       "CONFIG_VALIDATOR_PATH=%s is actually honored (%s)" % (CANONICAL, result.stdout.strip()))

    bad_env = dict(os.environ, CONFIG_VALIDATOR_PATH="/no/such/generation.py")
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, %r); import app.config_validator" % SERVER_ROOT],
        env=bad_env, capture_output=True, text=True,
    )
    ck(result.returncode != 0 and "no/such/generation.py" in result.stderr,
       "a nonexistent CONFIG_VALIDATOR_PATH fails loudly, naming the bad path (%s)" % result.stderr[-300:])

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
