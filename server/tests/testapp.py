"""Shared helper: a TestClient wired to a fresh fixture repo, via FastAPI's
dependency_overrides rather than the LOG_REPO_PATH env var, so tests never
depend on process-wide state and can't accidentally point at the real log.

TRAP, found by writing a new regression test that fell into it directly:
`app` (imported below) is ONE shared, module-level FastAPI instance --
`dependency_overrides` lives ON THAT OBJECT, not on the TestClient. Every
call to make_client()/make_client_with_settings() REPLACES the override
globally, for every TestClient anyone is holding, not just the newly
returned one. Calling it a second time while an EARLIER client/settings
pair from this same test function is still going to be used again later
silently redirects that earlier client onto the new (usually throwaway)
fixture instead of its own -- no exception, no warning, just requests that
quietly land in the wrong repo and read/write the wrong file. Existing
tests avoid this because each numbered scenario always reassigns BOTH
`client, settings = make_client_with_settings()` together and never reads
an older client after a newer call. If you need a second, independent
client mid-scenario, either finish with the first one's client entirely
before creating the second, or don't create a second at all -- reuse the
one you have."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import Operator, get_operator  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.evolver_config import EvolverConfigSettings, get_evolver_config_settings  # noqa: E402
from app.main import app  # noqa: E402

from .fixture import build_evolver_unit_repo, build_fixture_repo  # noqa: E402

TEST_OPERATOR = Operator(initials="TEST", git_name="Test Operator", git_email="test@example.com")


def make_client() -> TestClient:
    """A client with a fresh fixture log and a stubbed operator identity --
    for tests exercising business logic, not the auth mechanism itself."""
    client, _settings = make_client_with_settings()
    return client


def make_client_with_settings(repo_path: str | None = None) -> tuple[TestClient, Settings]:
    """Same as make_client(), but also returns the Settings so a test can
    inspect the repo's files/git history directly after a write. Pass
    repo_path (e.g. from fixture.build_real_clone()) to point at something
    other than the default synthetic fixture -- still fully isolated, never
    the live log, whatever it is."""
    root = repo_path or build_fixture_repo()
    settings = Settings(log_repo_path=str(root))
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_operator] = lambda: TEST_OPERATOR
    return TestClient(app), settings


def make_client_with_config(unit: str = "testunit", config: dict | None = None) -> tuple:
    """A client with a fresh fixture log (hardware.units has `unit`) AND a
    fresh, isolated evolver_code repo for that same unit, wired via
    dependency_overrides -- for the /config routes. Returns (client,
    log_settings, evolver_settings, unit_repo_path). The unit repo is
    entirely separate from the log repo, matching production: a config
    write must never touch the same git history evolution_log.json lives
    in."""
    client, log_settings = make_client_with_settings()
    unit_repo = build_evolver_unit_repo(config)
    evolver_settings = EvolverConfigSettings(unit_paths_json=json.dumps({unit: str(unit_repo)}))
    app.dependency_overrides[get_evolver_config_settings] = lambda: evolver_settings
    return client, log_settings, evolver_settings, unit_repo


def make_client_with_real_auth(extra_entries: dict | None = None) -> tuple[TestClient, str, Operator]:
    """A client with real bearer-token auth wired up (get_operator's
    dependency override removed), for testing the auth mechanism itself.
    Returns (client, a valid token, the Operator that token resolves to).

    extra_entries, if given, is merged into the written tokens file
    alongside the real one -- for testing how load_operators() handles
    malformed or comment-like entries sitting next to a valid one."""
    fixture_root = build_fixture_repo()
    settings = Settings(log_repo_path=str(fixture_root))
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides.pop(get_operator, None)

    token = "test-token-12345"
    operator = Operator(initials="AJ", git_name="Test AJ", git_email="aj@test.example")
    tokens_path = Path(tempfile.mkdtemp(prefix="operators_")) / "operators.json"
    contents = {token: operator._asdict()}
    contents.update(extra_entries or {})
    with open(tokens_path, "w") as fh:
        json.dump(contents, fh)
    os.environ["OPERATOR_TOKENS_FILE"] = str(tokens_path)

    return TestClient(app), token, operator
