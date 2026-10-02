#!/usr/bin/env bash
# Initialise THIS checkout for one experiment: write its evolution_log.json,
# an operator token and the server's settings, validate, and commit.
#
#   ./init_experiment.sh            (edit the block below first, or pass
#                                    any of its variables in the environment)
#   ./run_server.sh                 then start the server, as often as you like
#
# Run it once per checkout. It refuses to run where a log already exists, so
# running it twice by accident cannot overwrite a real record.
set -euo pipefail

# ── SET THESE ────────────────────────────────────────────────────────────────
# The name and one-line title go into the log's `experiment` block, and the
# server reads them from there: /health, /skill and /config/skill all
# introduce the experiment by this name, so it is what an LLM is told it is
# working on.
EXPERIMENT="${EXPERIMENT:-}"            # short name, e.g. OR06
IDENTITY="${IDENTITY:-}"                # one line, e.g. "OR06 evolution, phase 1"
UNITS="${UNITS:-}"                      # eVOLVER unit names, space separated

# Media bottles, as unit:media:role:pg_g_per_L, space separated. At least one
# is REQUIRED: the server validates the whole log on every write and the
# schema requires a non-empty reservoirs.items, so with none the first
# POST /lines is refused. role is low or high; the concentration is the
# selective agent (phloroglucinol) in g/L, and mM is computed from it.
RESERVOIRS="${RESERVOIRS:-}"
RESERVOIR_VOLUME_L="${RESERVOIR_VOLUME_L:-1.0}"
# When the bottles were prepared. Blank records the moment this script runs
# and says so -- set it if they were made earlier, rather than leave a
# plausible-looking time that is really "when I ran setup".
PREPARED_AT="${PREPARED_AT:-}"          # e.g. 2026-10-02T09:00:00-04:00

PORT="${PORT:-8556}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"       # 0.0.0.0 accepts from the network (Tailscale)

# OPTIONAL. Where each unit's dashboard.py answers /api/v1/*, as unit=url --
# the host running dashboard.py, NOT the eVOLVER's 192.168.1.x address.
# Blank: localhost:8050, 8051, ... in UNITS order. Only the live pump
# columns need it; logging works without.
DASHBOARDS="${DASHBOARDS:-}"
DASHBOARD_TIMEOUT_S="${DASHBOARD_TIMEOUT_S:-8}"

# OPTIONAL. Each unit's own git checkout of the eVOLVER code, as JSON
# unit -> absolute path, so the LLM can read/edit experiment_parameters.yaml
# through /config. Blank: /config answers 500 and everything else works.
UNIT_PATHS="${UNIT_PATHS:-}"            # '{"patrick": "/home/me/evolver/patrick"}'

OPERATOR_INITIALS="${OPERATOR_INITIALS:-}"
GIT_NAME="${GIT_NAME:-$(git config user.name 2>/dev/null || true)}"
GIT_EMAIL="${GIT_EMAIL:-$(git config user.email 2>/dev/null || true)}"
TOKEN="${TOKEN:-}"                      # blank = generate one and print it once

PY="${PY:-$HOME/py/bin/python}"
UVICORN="${UVICORN:-$(dirname "$PY")/uvicorn}"
# ─────────────────────────────────────────────────────────────────────────────

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mSTOP: %s\033[0m\n' "$*" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ── preflight: everything is checked before anything is written ─────────────
say "Checking"
for v in EXPERIMENT IDENTITY UNITS RESERVOIRS OPERATOR_INITIALS GIT_NAME GIT_EMAIL; do
  [ -n "${!v}" ] || die "$v is empty -- set it in the block at the top of $0, or in the environment."
done
[ -e evolution_log.json ] && die "this checkout already has an evolution_log.json.
     It is an append-only record; this script will not write over it.
     To serve it:  ./run_server.sh
     For a NEW experiment, clone this repo again and initialise the clone."
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "$ROOT is not a git checkout -- the server commits every write, so it must be one"
[ "$(git rev-parse --show-toplevel)" = "$(pwd -P)" ] \
  || die "$ROOT is inside some other git repo, not the root of its own"
[ -x "$PY" ]      || die "no python at $PY (set PY to the interpreter that will run the server)"
[ -x "$UVICORN" ] || die "no uvicorn at $UVICORN (set UVICORN, or: $PY -m pip install uvicorn)"
"$PY" - <<'PY' || exit 1
import sys
missing = []
for mod, pkg in [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pydantic", "pydantic"),
                 ("jsonschema", "jsonschema"), ("multipart", "python-multipart"),
                 ("httpx", "httpx"), ("yaml", "pyyaml")]:
    try:
        __import__(mod)
    except Exception:
        missing.append(pkg)
if missing:
    sys.exit("STOP: missing python packages: %s\n     %s -m pip install -U %s"
             % (" ".join(missing), sys.executable, " ".join(missing)))
import jsonschema
if not hasattr(jsonschema, "Draft202012Validator"):
    sys.exit("STOP: jsonschema is too old for draft 2020-12 -- the tools refuse to validate with it\n"
             "     %s -m pip install -U jsonschema" % sys.executable)
PY
if [ -n "$UNIT_PATHS" ]; then
  UNIT_PATHS="$UNIT_PATHS" UNITS="$UNITS" "$PY" - <<'PY' || exit 1
import json, os, sys
try:
    m = json.loads(os.environ["UNIT_PATHS"])
except json.JSONDecodeError as e:
    sys.exit("STOP: UNIT_PATHS is not valid JSON (%s)" % e)
units, bad = os.environ["UNITS"].split(), []
for u, p in m.items():
    if u not in units:                     bad.append("names unit %r, which is not in UNITS" % u)
    elif not os.path.isabs(p):             bad.append("%s: %r is not an absolute path" % (u, p))
    elif not os.path.isdir(os.path.join(p, ".git")) and not os.path.isfile(os.path.join(p, ".git")):
        bad.append("%s: %s is not a git checkout (the /config routes commit into it)" % (u, p))
if len(set(m.values())) != len(m):        bad.append("two units share one directory")
if bad:
    sys.exit("STOP: UNIT_PATHS " + "\n      UNIT_PATHS ".join(bad))
PY
fi
case "$PORT" in *[!0-9]*|"") die "PORT must be a number" ;; esac
echo "   ok: python, packages, git, settings"

# ── the log ──────────────────────────────────────────────────────────────────
say "Writing evolution_log.json for $EXPERIMENT"
EXPERIMENT="$EXPERIMENT" IDENTITY="$IDENTITY" UNITS="$UNITS" RESERVOIRS="$RESERVOIRS" \
RESERVOIR_VOLUME_L="$RESERVOIR_VOLUME_L" PREPARED_AT="$PREPARED_AT" "$PY" - <<'PY' || exit 1
import datetime, json, os, re, sys

now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
prepared_at = os.environ["PREPARED_AT"] or now
if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?[+-]\d\d:\d\d", prepared_at):
    sys.exit("STOP: PREPARED_AT %r is not an ISO timestamp with a +HH:MM offset" % prepared_at)

# The vocabulary (event types, parameter registry, conventions) comes from the
# reference log: tools/lineage.py validates every event against it, and a new
# experiment wants the same words for the same things. Nothing experiment-
# SPECIFIC is copied -- inventing a line or a reservoir here would be
# inventing data.
ref = json.load(open("reference/or05_log.json"))
units = os.environ["UNITS"].split()
MW = 126.11   # phloroglucinol; tools/lineage.py cross-checks g/L against mM to 0.5%
reservoirs, seen = [], set()
for spec in os.environ["RESERVOIRS"].split():
    try:
        unit, media, role, pg = spec.split(":")
        pg = float(pg)
    except ValueError:
        sys.exit("STOP: RESERVOIRS entry %r is not unit:media:role:pg_g_per_L" % spec)
    if role not in ("low", "high"):
        sys.exit("STOP: RESERVOIRS entry %r: role must be low or high" % spec)
    if unit not in units:
        sys.exit("STOP: RESERVOIRS entry %r names unit %r, which is not in UNITS" % (spec, unit))
    rid = "%s/%s-%g" % (unit, media, pg)
    if rid in seen:
        sys.exit("STOP: two reservoirs would both be called %r" % rid)
    seen.add(rid)
    vol = {"value": float(os.environ["RESERVOIR_VOLUME_L"]), "unit": "L"}
    # The level fields are what the server writes for a freshly prepared
    # bottle, and tools/media.py reads them by key -- without them GET /media
    # 500s. level_source "prepared" is the schema's own word for "nothing
    # read since it was made, so current_volume IS the prepared volume".
    reservoirs.append({
        "id": rid, "unit": unit, "media": media, "role": role,
        "pg": {"value_g_per_L": pg, "value_mM": round(pg * 1000.0 / MW, 4), "unit_primary": "g/L"},
        "status": "active", "volume_prepared": vol, "prepared_at": prepared_at,
        "current_volume": dict(vol), "level_as_of": prepared_at,
        "level_source": "prepared", "level_qualifier": "exact", "lines_fed": [],
    })

log = {
    "schema_version": ref.get("schema_version", "1.2.0"),
    "log_meta": {"maintained_by": "operators via the evolution log server",
                 "last_updated": now, "event_counter": 0, "next_event_id": "EVT-00001"},
    "experiment": {"name": os.environ["EXPERIMENT"], "title": os.environ["IDENTITY"],
                   "log_started_timestamp": now},
    "design": {},
    "hardware": {"units": {u: {"vials_in_use": [], "n_lines": 0} for u in units}},
    "reservoirs": {"items": reservoirs},
    "conventions": ref.get("conventions", {}),
    "event_types": ref.get("event_types", {}),
    "parameter_registry": ref.get("parameter_registry", {}),
    "lineage_summary": {"founders": [], "n_nodes": 0, "n_edges": 0, "max_depth": 0},
    "lines": {},
    "experiment_events": [],
}
with open("evolution_log.json", "w") as fh:
    json.dump(log, fh, indent=2)
    fh.write("\n")
print("   %s -- %s" % (log["experiment"]["name"], log["experiment"]["title"]))
print("   units: %s" % ", ".join(units))
print("   reservoirs: %s" % ", ".join(r["id"] for r in reservoirs))
if not os.environ["PREPARED_AT"]:
    print("   bottles marked prepared at %s (now) -- set PREPARED_AT if they were made earlier" % now)
print("   vocabulary: %d event types, %d registered params"
      % (len(log["event_types"]), len(log["parameter_registry"])))
PY

# viewer.html, tools/check_api.py and GET /media?pump=auto read the rig
# roster from here. Generated, never copied: a stale-but-plausible url is the
# failure this project keeps having.
UNITS="$UNITS" DASHBOARDS="$DASHBOARDS" "$PY" - <<'PY' || exit 1
import json, os, sys
units, given = os.environ["UNITS"].split(), {}
for spec in os.environ["DASHBOARDS"].split():
    if "=" not in spec:
        sys.exit("STOP: DASHBOARDS entry %r is not unit=url" % spec)
    u, url = spec.split("=", 1)
    if u not in units:
        sys.exit("STOP: DASHBOARDS names unit %r, which is not in UNITS" % u)
    given[u] = url
out = {
    "_about": "Where viewer.html, tools/check_api.py and GET /media?pump=auto find each "
              "eVOLVER's live data: the host running dashboard.py, not the eVOLVER's own IP. "
              "Identity is confirmed against /api/v1/health, not trusted from this file.",
    "units": {u: {"url": given.get(u, "http://localhost:%d" % (8050 + i)), "enabled": True}
              for i, u in enumerate(units)},
    "poll_seconds": 15,
    "burn_rate_window_h": 6,
}
json.dump(out, open("viewer.config.json", "w"), indent=2)
print("   dashboards: " + ", ".join("%s -> %s" % (u, out["units"][u]["url"]) for u in units))
PY

# ── secrets: token + the server's whole environment, one file ────────────────
say "Writing secrets/ (gitignored)"
mkdir -p secrets
if [ -z "$TOKEN" ]; then TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"; GEN=1; fi
TOKEN="$TOKEN" I="$OPERATOR_INITIALS" N="$GIT_NAME" E="$GIT_EMAIL" "$PY" -c '
import json, os
json.dump({os.environ["TOKEN"]: {"initials": os.environ["I"], "git_name": os.environ["N"],
                                 "git_email": os.environ["E"]}},
          open("secrets/operators.json", "w"), indent=2)'
chmod 600 secrets/operators.json

# Single-quoted values: taken literally by both `set -a; . file` (run_server.sh)
# and systemd's EnvironmentFile. Unquoted, systemd strips the JSON's double
# quotes and the server gets something that no longer parses.
emit() {
  case "$2" in *"'"*) die "$1 contains a single quote, which bash and systemd would read differently" ;; esac
  printf "%s='%s'\n" "$1" "$2"
}
DASH_JSON="$(UNITS="$UNITS" "$PY" -c '
import json
c = json.load(open("viewer.config.json"))
print(json.dumps({u: v["url"] for u, v in c["units"].items()}))')"
{
  echo "# Read by run_server.sh (and usable as a systemd EnvironmentFile)."
  echo "# LOG_REPO_PATH is deliberately absent: run_server.sh sets it to the"
  echo "# checkout it lives in, so moving the repo cannot leave this pointing"
  echo "# at an old copy."
  emit OPERATOR_TOKENS_FILE        "$ROOT/secrets/operators.json"
  emit EVOLVER_DASHBOARD_URLS      "$DASH_JSON"
  emit EVOLVER_DASHBOARD_TIMEOUT_S "$DASHBOARD_TIMEOUT_S"
  [ -n "$UNIT_PATHS" ] && emit EVOLVER_UNIT_PATHS "$UNIT_PATHS"
  emit BIND_HOST "$BIND_HOST"
  emit PORT      "$PORT"
  emit UVICORN   "$UVICORN"
} > secrets/server.env
chmod 600 secrets/server.env
echo "   secrets/operators.json, secrets/server.env"
[ "${GEN:-0}" = "1" ] && printf '   generated token (shown once): %s\n' "$TOKEN"

# ── validate ─────────────────────────────────────────────────────────────────
say "Validating"
"$PY" tools/lineage.py --write >/dev/null
"$PY" tools/lineage.py | tail -1         || die "the new log fails cross-field integrity"
# The schema requires `lines` to be non-empty and a new experiment has none
# yet; that one error is expected and clears on the first POST /lines.
"$PY" tools/schema_bootstrap_check.py   || die "schema problems beyond the expected empty 'lines'"
# Asserts the schema REJECTS bad logs, by mutating the reference log.
"$PY" tools/test_schema.py >/dev/null   || die "tools/test_schema.py fails -- the schema is not one to trust"
echo "   schema + integrity: ok"

# ── commit: the log and its rig roster, nothing else ─────────────────────────
say "Committing"
git add evolution_log.json viewer.config.json
git -c user.name="$GIT_NAME" -c user.email="$GIT_EMAIL" commit -q \
    -m "Initialise $EXPERIMENT: $IDENTITY" -- evolution_log.json viewer.config.json
git log --oneline -1 | sed 's/^/   /'

cat <<EOF

Done. Next:
  ./run_server.sh                         start it (Ctrl-C stops it)
  curl -s http://localhost:$PORT/health   check: experiment, n_reservoirs, auth_configured

Give the LLM:
  http://<this host>:$PORT/skill          how to use every route
  Authorization: Bearer <token>           from secrets/operators.json
EOF
