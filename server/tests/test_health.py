#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_health.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client  # noqa: E402
from app.log_repo import experiment_identity  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    client = make_client()
    r = client.get("/health")
    ck(r.status_code == 200, "GET /health returns 200")
    body = r.json()
    ck(body["status"] == "ok", "status is ok")
    ck(body["n_lines"] == 3, "reports the fixture's 3 lines")
    ck(body["n_reservoirs"] == 2, "reports the fixture's 2 reservoirs")
    ck(body["log_meta"]["event_counter"] == 7, "reports log_meta.event_counter")
    ck(body["writes_supported"] is True, "reports that writes are supported (POST /events exists)")
    ck("auth_configured" in body, "reports whether operator tokens are configured, without leaking them")
    ck(bool(body["log_repo_head"]), "found a git HEAD for the fixture repo")
    ck(body["server_time"], "server_time is present (non-zero clock)")

    # The experiment is named by the LOG, not by the server -- one codebase
    # serves many experiments, and a hardcoded "OR05" once introduced every
    # new experiment's server as OR05's.
    ck(body["experiment"] == {"name": "unnamed experiment", "title": "fixture experiment"},
       "names the experiment from the log (fixture has only `identity`, so no name) (%s)" % body.get("experiment"))
    skill = client.get("/skill").text
    config_skill = client.get("/config/skill").text
    ck(skill.startswith("# unnamed experiment evolution log"), "GET /skill's title is the log's experiment")
    ck("fixture experiment" in skill.split("\n## ", 1)[0], "GET /skill's header carries the log's title")
    ck(config_skill.startswith("# unnamed experiment evolver config skill"), "GET /config/skill names the same experiment")
    ck("OR05" not in skill and "OR05" not in config_skill, "neither skill names OR05 for a log that is not OR05's")
    ck("@@" not in skill and "@@" not in config_skill, "no unfilled template placeholder survives")
    ck(client.get("/openapi.json").json()["info"]["title"] == "Evolution log server",
       "OpenAPI metadata names no experiment (it is fixed at import, before any log is read)")

    ck(experiment_identity({"experiment": {"name": "OR06", "title": "t", "id": "x"}}) == ("OR06", "t"),
       "`name` wins over `id` (what new_log_server.sh writes)")
    ck(experiment_identity({"experiment": {"id": "OR05-evolution-phase-2", "title": "T"}}) == ("OR05-evolution-phase-2", "T"),
       "OR05's own log, which has `id` and `title` but no `name`, still names itself")
    ck(experiment_identity({}) == ("unnamed experiment", None),
       "a log with no experiment block says so rather than borrowing a name")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
