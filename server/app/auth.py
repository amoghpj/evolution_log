"""Bearer token -> operator identity.

SERVER_DESIGN.md decision #3: the token is for ATTRIBUTION, not perimeter
defence -- the Tailscale network is assumed to be the perimeter. A valid
token names who is writing and supplies the git author identity for the
commit; it grants no permissions a different valid token wouldn't.

Tokens live in a JSON file kept out of both repos' git history entirely --
secrets don't belong in a repo whose whole discipline is "history is
forever". OPERATOR_TOKENS_FILE (default secrets/operators.json, gitignored)
maps token -> {initials, git_name, git_email}. See
secrets/operators.example.json for the shape; that example file IS committed
(no real token in it) so the shape is documented without a real secret ever
touching git history.
"""
import json
import os
from pathlib import Path
from typing import NamedTuple

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


class Operator(NamedTuple):
    initials: str
    git_name: str
    git_email: str


def tokens_file() -> Path:
    repo_root = Path(__file__).resolve().parents[2]   # server/app/auth.py -> repo
    return Path(os.environ.get("OPERATOR_TOKENS_FILE", repo_root / "secrets" / "operators.json"))


def load_operators() -> dict[str, Operator]:
    path = tokens_file()
    if not path.exists():
        raise RuntimeError(
            "no operator tokens configured -- create %s (see "
            "secrets/operators.example.json for the shape), or set "
            "OPERATOR_TOKENS_FILE to point at one" % path
        )
    with open(path) as fh:
        try:
            raw = json.load(fh)
        except json.JSONDecodeError as exc:
            # Found by simulating a hot-reload-of-tokens operator: an
            # invalid JSON tokens file (a stray trailing comma from a hand
            # edit, say) previously propagated as a raw, uncaught
            # JSONDecodeError -- FastAPI's generic exception handler turned
            # that into an opaque "Internal Server Error" 500 with nothing
            # in the response naming the tokens file as the cause, unlike
            # every OTHER failure mode in this function, which all raise a
            # clear RuntimeError that get_operator() turns into a
            # diagnosable 500 detail message.
            raise RuntimeError("%s: not valid JSON (%s)" % (path, exc)) from exc

    operators = {}
    for token, info in raw.items():
        if token.startswith("_"):
            # A leading underscore marks a comment/metadata key, not a real
            # token -- JSON has no native comment syntax, and
            # operators.example.json's own "_comment" entry documents the
            # shape this way. Copying that file as a template (which is
            # exactly what it tells you to do) and not deleting that line
            # is expected, not an error.
            continue
        if not isinstance(info, dict):
            raise RuntimeError(
                "%s: entry %r is %s, not an object -- expected "
                '{"initials": ..., "git_name": ..., "git_email": ...}. '
                "See secrets/operators.example.json for the shape."
                % (path, token, type(info).__name__)
            )
        try:
            operators[token] = Operator(**info)
        except TypeError as exc:
            raise RuntimeError("%s: entry %r has the wrong fields (%s)" % (path, token, exc)) from exc
    return operators


def get_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Operator:
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        operators = load_operators()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    operator = operators.get(credentials.credentials)
    if operator is None:
        raise HTTPException(status_code=401, detail="unrecognised bearer token")
    return operator
