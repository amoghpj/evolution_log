#!/usr/bin/env python3
"""    ~/py/bin/python server/tests/test_viewer_route.py

GET /viewer/ serves three named files and nothing else. Most of what this
asserts is what it REFUSES: the viewer it replaced (serve.sh, a directory
server on the repo root) handed secrets/operators.json to anyone who asked.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client_with_settings  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    client, settings = make_client_with_settings()
    root = settings.log_repo_path
    (root / "viewer.html").write_text("<!doctype html><title>viewer</title>")
    (root / "secrets").mkdir(exist_ok=True)
    (root / "secrets" / "operators.json").write_text('{"SECRET-TOKEN": {}}')
    (root / "secrets" / "server.env").write_text("OPERATOR_TOKENS_FILE='x'\n")

    r = client.get("/viewer", follow_redirects=False)
    ck(r.status_code == 307 and r.headers["location"] == "viewer/",
       "/viewer redirects to /viewer/, so the page's relative fetches resolve under it")

    r = client.get("/viewer/")
    ck(r.status_code == 200 and "<title>viewer</title>" in r.text, "/viewer/ serves viewer.html")
    ck(r.headers["content-type"].startswith("text/html"), "as text/html")
    ck(client.get("/viewer/viewer.html").status_code == 200, "/viewer/viewer.html serves it too")

    r = client.get("/viewer/evolution_log.json?t=123")
    ck(r.status_code == 200 and r.json() == json.loads(settings.log_file.read_text()),
       "/viewer/evolution_log.json is the log on disk (cache-buster query ignored)")
    ck(r.headers.get("cache-control") == "no-store", "never cached -- the viewer polls for new writes")

    r = client.get("/viewer/viewer.config.json")
    ck(r.status_code == 404, "no viewer.config.json yet -> 404, which the page treats as 'no live columns'")
    (root / "viewer.config.json").write_text('{"units": {}}')
    r = client.get("/viewer/viewer.config.json")
    ck(r.status_code == 200 and r.json() == {"units": {}}, "serves viewer.config.json once it exists")

    leaked = []
    for path in ["/viewer/secrets/operators.json", "/viewer/secrets/server.env",
                 "/viewer/../secrets/operators.json", "/viewer/%2e%2e/secrets/operators.json",
                 "/viewer/..%2fsecrets%2foperators.json", "/secrets/operators.json",
                 "/viewer/schema/evolution_log.schema.json", "/viewer/tools/lineage.py",
                 "/viewer/.git/config", "/viewer.html", "/evolution_log.json"]:
        r = client.get(path)
        if r.status_code == 200 or "SECRET-TOKEN" in r.text:
            leaked.append("%s -> %s" % (path, r.status_code))
    ck(not leaked, "nothing outside the three files is reachable, secrets/ above all (%s)" % leaked)

    ck(not any(p.startswith("/viewer") for p in client.get("/openapi.json").json()["paths"]),
       "kept out of the OpenAPI schema, so out of GET /skill's route table")
    ck("/viewer" not in client.get("/skill").text.split("## Reading the log", 1)[0],
       "and not in the skill's route table")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
