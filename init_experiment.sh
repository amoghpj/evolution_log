#!/usr/bin/env bash
# Initialise THIS checkout for one experiment: write its evolution_log.json,
# an operator token and the server's settings, validate, and commit.
#
#   ./init_experiment.sh        in a terminal: asks for each setting, checking
#                               every answer as you give it, then shows them
#                               all and asks before writing anything
#   ./init_experiment.sh -i     ask even for settings already given in the
#                               environment (they become the defaults)
#   EXPERIMENT=... UNITS=... ./init_experiment.sh </dev/null
#                               no terminal: takes everything from the
#                               environment, prompts for nothing
#
#   ./run_server.sh             then start the server, as often as you like
#
# Run it once per checkout. It refuses to run where a log already exists, so
# running it twice by accident cannot overwrite a real record. If anything
# fails before the final commit, everything it wrote is removed again, so a
# failed run can simply be re-run.
set -euo pipefail

# ── settings: each can come from the environment, or is asked for ───────────
# The name and one-line title go into the log's `experiment` block, and the
# server reads them from there: /health, /skill and /config/skill all
# introduce the experiment by this name, so it is what an LLM is told it is
# working on.
EXPERIMENT="${EXPERIMENT:-}"            # short name, e.g. OR06
IDENTITY="${IDENTITY:-}"                # one line, e.g. "OR06 evolution, phase 1"
UNITS="${UNITS:-}"                      # eVOLVER unit names, space separated, lowercase letters

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
UVICORN="${UVICORN:-}"                  # blank = the uvicorn beside $PY
# ─────────────────────────────────────────────────────────────────────────────

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mSTOP: %s\033[0m\n' "$*" >&2; exit 1; }

INTERACTIVE=""
for arg in "$@"; do
  case "$arg" in
    -i|--interactive) INTERACTIVE=1 ;;
    -h|--help)        sed -n '2,19p' "$0"; exit 0 ;;
    *)                die "unknown argument $arg (try --help)" ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# ── anything written before the final commit is removed again on failure ────
# Without this, a run that failed validation left evolution_log.json behind,
# and every later run then refused because "a log already exists".
CREATED=()
COMMITTED=0
cleanup() {
  [ "$COMMITTED" = "1" ] && return
  [ "${#CREATED[@]}" -gt 0 ] || return
  for f in "${CREATED[@]}"; do rm -f "$f"; done
  git reset -q -- evolution_log.json viewer.config.json 2>/dev/null || true
  printf '   (removed what this run wrote: %s -- fix the problem and run it again)\n' "${CREATED[*]}" >&2
}
trap cleanup EXIT
created() { CREATED+=("$@"); }

# ── what can be checked before asking anything ──────────────────────────────
# A question you answer and then have refused for reasons unrelated to your
# answer is wasted, so the checkout, the interpreter and the packages come first.
[ -e evolution_log.json ] && die "this checkout already has an evolution_log.json.
     It is an append-only record; this script will not write over it.
     To serve it:  ./run_server.sh
     For a NEW experiment, clone this repo again and initialise the clone."
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "$ROOT is not a git checkout -- the server commits every write, so it must be one"
[ "$(git rev-parse --show-toplevel)" = "$(pwd -P)" ] \
  || die "$ROOT is inside some other git repo, not the root of its own"

# Interactive when asked for, or when there is a terminal and something
# required is still missing. Without a terminal, nothing is ever asked.
if [ -z "$INTERACTIVE" ]; then
  INTERACTIVE=0
  if [ -t 0 ] && [ -t 1 ]; then
    for v in EXPERIMENT IDENTITY UNITS RESERVOIRS OPERATOR_INITIALS; do
      [ -n "${!v}" ] || INTERACTIVE=1
    done
  fi
fi
[ "$INTERACTIVE" = "1" ] && { [ -t 0 ] || die "-i needs a terminal; without one, set the variables in the environment"; }

# ── prompting ────────────────────────────────────────────────────────────────
# ask VAR "question" [validator] -- the variable's current value is the
# default (Enter keeps it); the validator prints why an answer is refused and
# returns 1, and the question is asked again.
ask() {
  local __var="$1" __q="$2" __check="${3:-}" __ans __why
  while :; do
    if [ -n "${!__var}" ]; then printf '  %s [%s]: ' "$__q" "${!__var}"
    else                       printf '  %s: ' "$__q"; fi
    IFS= read -r __ans || die "input ended before setup was complete"
    [ -n "$__ans" ] || __ans="${!__var}"
    if [ -n "$__check" ] && ! __why="$("$__check" "$__ans")"; then
      printf '    \033[33m! %s\033[0m\n' "$__why"; continue
    fi
    printf -v "$__var" '%s' "$__ans"; return
  done
}
section() { printf '\n\033[1m%s\033[0m\n' "$1"; [ -z "${2:-}" ] || printf '  \033[2m%s\033[0m\n' "$2"; }

# ── validators: one rule per setting, used by the prompts AND the env path ───
# The patterns are the schema's own (schema/evolution_log.schema.json:
# reservoirId, lineIdPattern), checked here so a bad name is refused before
# anything is written, not by the schema after.
v_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "letters, digits, . _ - only, no spaces (e.g. OR06)"; return 1; }; }
v_text() { [ -n "$1" ] || { echo "required"; return 1; }; }
v_units() {
  [ -n "$1" ] || { echo "at least one unit"; return 1; }
  local u seen=" "
  for u in $1; do
    [[ "$u" =~ ^[a-z]+$ ]] || { echo "'$u': unit names are lowercase letters only (the schema's line-id rule)"; return 1; }
    case "$seen" in *" $u "*) echo "'$u' is listed twice"; return 1 ;; esac
    seen="$seen$u "
  done
}
# one unit's bottles, "media:role:g_per_L ..."; blank allowed per unit
v_bottles() {
  local b media role pg seen=" "
  for b in $1; do
    IFS=: read -r media role pg <<<"$b"
    [ -n "$pg" ] && [ "${b//[^:]/}" = "::" ] || { echo "'$b' is not media:role:g_per_L (e.g. LB:high:5)"; return 1; }
    [[ "$media" =~ ^[A-Za-z0-9]+$ ]] || { echo "'$media': media names are letters and digits only"; return 1; }
    [ "$role" = low ] || [ "$role" = high ] || { echo "'$b': role must be low or high"; return 1; }
    [[ "$pg" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "'$pg' is not a concentration in g/L (e.g. 0, 2.5)"; return 1; }
    local id="$media-$(printf '%g' "$pg")"
    case "$seen" in *" $id "*) echo "two bottles would both be called $id"; return 1 ;; esac
    seen="$seen$id "
  done
}
v_reservoirs() {   # the full unit:media:role:pg list, for the env path
  [ -n "$1" ] || { echo "at least one bottle is required"; return 1; }
  local spec unit per
  for unit in $UNITS; do
    per=""
    for spec in $1; do [ "${spec%%:*}" = "$unit" ] && per="$per ${spec#*:}"; done
    v_bottles "$per" || return 1
  done
  for spec in $1; do
    case " $UNITS " in *" ${spec%%:*} "*) ;; *) echo "'$spec' names unit '${spec%%:*}', which is not in UNITS"; return 1 ;; esac
  done
}
v_volume() { [[ "$1" =~ ^[0-9]+(\.[0-9]+)?$ ]] && [[ ! "$1" =~ ^0+(\.0+)?$ ]] || { echo "a volume in litres, e.g. 1.0"; return 1; }; }
v_when() {
  [ -z "$1" ] && return 0
  [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?[+-][0-9]{2}:[0-9]{2}$ ]] \
    || { echo "ISO time with a colon offset, e.g. 2026-10-02T09:00:00-04:00 (never Z), or blank for now"; return 1; }
}
v_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ] || { echo "a port number, 1-65535"; return 1; }
  if command -v lsof >/dev/null && lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $1 is already in use on this machine"; return 1
  fi
}
v_host() { [ -n "$1" ] || { echo "e.g. 0.0.0.0 (all interfaces) or 127.0.0.1 (this machine only)"; return 1; }; }
v_checkout() {
  [ -z "$1" ] && return 0
  case "$1" in /*) ;; *) echo "an absolute path"; return 1 ;; esac
  [ -d "$1" ] || { echo "$1 is not a directory"; return 1; }
  [ -e "$1/.git" ] || { echo "$1 is not a git checkout -- the /config routes commit into it"; return 1; }
}
v_url() { [ -z "$1" ] || [[ "$1" =~ ^https?://[^/[:space:]]+ ]] || { echo "http://host:port, or blank"; return 1; }; }
v_initials() { [[ "$1" =~ ^[A-Za-z]{1,5}$ ]] || { echo "1-5 letters, e.g. AJ"; return 1; }; }
v_email() { [[ "$1" =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]] || { echo "an email address"; return 1; }; }
v_python() { [ -x "$1" ] || { echo "no executable python at $1"; return 1; }; }

# The interpreter's packages, checked as soon as the interpreter is known.
pkg_check() {
  "$PY" - <<'PY'
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
}

# ── the questions ────────────────────────────────────────────────────────────
if [ "$INTERACTIVE" = "1" ]; then
  printf '\033[1mNew experiment log in %s\033[0m\n' "$ROOT"
  printf '  Enter keeps the [default]. Nothing is written until you confirm at the end.\n'

  section "Python" "the interpreter that will run the server"
  [ -x "$PY" ] || PY="$(command -v python3 || true)"
  ask PY "python" v_python
  pkg_check || exit 1

  section "The experiment" "the server introduces it to an LLM by this name and title"
  ask EXPERIMENT "short name" v_name
  ask IDENTITY   "one-line title" v_text

  section "The rigs" "eVOLVER unit names as the rigs call themselves; lowercase letters"
  ask UNITS "units (space separated)" v_units

  section "Media bottles" "per unit, as media:role:g_per_L -- role low or high, PG in g/L; e.g. LB:low:0 LB:high:5"
  RESERVOIRS_IN="$RESERVOIRS"; RESERVOIRS=""
  for u in $UNITS; do
    BOTTLES=""
    for spec in $RESERVOIRS_IN; do [ "${spec%%:*}" = "$u" ] && BOTTLES="${BOTTLES:+$BOTTLES }${spec#*:}"; done
    ask BOTTLES "$u" v_bottles
    for b in $BOTTLES; do RESERVOIRS="${RESERVOIRS:+$RESERVOIRS }$u:$b"; done
  done
  while [ -z "$RESERVOIRS" ]; do
    printf '    \033[33m! at least one bottle is required: the schema refuses a log without one, so no line could ever be added\033[0m\n'
    for u in $UNITS; do
      BOTTLES=""; ask BOTTLES "$u" v_bottles
      for b in $BOTTLES; do RESERVOIRS="${RESERVOIRS:+$RESERVOIRS }$u:$b"; done
    done
  done
  ask RESERVOIR_VOLUME_L "volume per bottle, L" v_volume
  ask PREPARED_AT "when were they prepared? (blank = now)" v_when

  section "The server" "where it listens"
  ask PORT "port" v_port
  ask BIND_HOST "bind address (0.0.0.0 = reachable over the network)" v_host

  section "Optional: /config" "each unit's own git checkout of the eVOLVER code, so the LLM can read and edit its experiment_parameters.yaml. Blank skips it."
  UNIT_PATHS_IN="$UNIT_PATHS"; PAIRS=()
  for u in $UNITS; do
    UP="$(UNIT_PATHS="$UNIT_PATHS_IN" U="$u" "$PY" -c 'import json,os;print(json.loads(os.environ["UNIT_PATHS"] or "{}").get(os.environ["U"],""))' 2>/dev/null || true)"
    while :; do
      ask UP "$u checkout" v_checkout
      UP="${UP/#\~/$HOME}"
      dup=""; for pr in ${PAIRS[@]+"${PAIRS[@]}"}; do [ "${pr#*=}" = "$UP" ] && dup="${pr%%=*}"; done
      [ -n "$UP" ] && [ -n "$dup" ] && { printf '    \033[33m! %s already uses that directory; two units cannot share one\033[0m\n' "$dup"; UP=""; continue; }
      break
    done
    [ -n "$UP" ] && PAIRS+=("$u=$UP")
  done
  UNIT_PATHS="$( "$PY" -c 'import json,sys;print(json.dumps(dict(a.split("=",1) for a in sys.argv[1:])) if len(sys.argv)>1 else "")' ${PAIRS[@]+"${PAIRS[@]}"})"

  section "Optional: live pump data" "where each unit's dashboard.py answers -- the machine running it, not the eVOLVER's 192.168.1.x address. Blank = localhost default."
  DASH_IN="$DASHBOARDS"; DASHBOARDS=""; i=0
  for u in $UNITS; do
    URL=""
    for spec in $DASH_IN; do [ "${spec%%=*}" = "$u" ] && URL="${spec#*=}"; done
    [ -n "$URL" ] || URL="http://localhost:$((8050 + i))"
    ask URL "$u dashboard" v_url
    [ -n "$URL" ] && DASHBOARDS="${DASHBOARDS:+$DASHBOARDS }$u=$URL"
    i=$((i + 1))
  done

  section "You" "recorded on every log commit the server makes with your token"
  ask OPERATOR_INITIALS "initials" v_initials
  ask GIT_NAME  "full name" v_text
  ask GIT_EMAIL "email" v_email
fi

# ── every setting checked, whichever way it arrived ─────────────────────────
say "Checking"
BAD=()
chk() { local why; why="$("$2" "${!1}")" || BAD+=("$1: $why"); }
for v in EXPERIMENT IDENTITY UNITS RESERVOIRS OPERATOR_INITIALS GIT_NAME GIT_EMAIL; do
  [ -n "${!v}" ] || BAD+=("$v is empty -- set it in the environment, or run in a terminal to be asked")
done
[ -n "$EXPERIMENT" ] && chk EXPERIMENT v_name
[ -n "$UNITS" ] && chk UNITS v_units
[ -n "$RESERVOIRS" ] && [ -n "$UNITS" ] && chk RESERVOIRS v_reservoirs
chk RESERVOIR_VOLUME_L v_volume
chk PREPARED_AT v_when
chk PORT v_port
[ -n "$OPERATOR_INITIALS" ] && chk OPERATOR_INITIALS v_initials
[ -n "$GIT_EMAIL" ] && chk GIT_EMAIL v_email
for spec in $DASHBOARDS; do
  case "$spec" in *=*) ;; *) BAD+=("DASHBOARDS: '$spec' is not unit=url"); continue ;; esac
  case " $UNITS " in *" ${spec%%=*} "*) ;; *) BAD+=("DASHBOARDS names unit '${spec%%=*}', which is not in UNITS") ;; esac
  v_url "${spec#*=}" >/dev/null || BAD+=("DASHBOARDS: '${spec#*=}' is not http(s)://host:port")
done
if [ "${#BAD[@]}" -gt 0 ]; then
  printf '   \033[31mx %s\033[0m\n' "${BAD[@]}" >&2
  die "fix the settings above; nothing was written"
fi

[ -n "$UVICORN" ] || UVICORN="$(dirname "$PY")/uvicorn"
[ -x "$PY" ]      || die "no python at $PY (set PY to the interpreter that will run the server)"
[ -x "$UVICORN" ] || die "no uvicorn at $UVICORN (set UVICORN, or: $PY -m pip install uvicorn)"

pkg_check || exit 1
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
echo "   ok: python, packages, git, settings"

# ── the whole configuration, once, before anything is written ───────────────
summary() {
  local u specs path url
  printf '  %-14s %s -- %s\n' "experiment" "$EXPERIMENT" "$IDENTITY"
  for u in $UNITS; do
    specs=""; for s in $RESERVOIRS; do [ "${s%%:*}" = "$u" ] && specs="${specs:+$specs  }${s#*:}"; done
    path="$(UNIT_PATHS="$UNIT_PATHS" U="$u" "$PY" -c 'import json,os;print(json.loads(os.environ["UNIT_PATHS"] or "{}").get(os.environ["U"],"-"))')"
    url="-"; for s in $DASHBOARDS; do [ "${s%%=*}" = "$u" ] && url="${s#*=}"; done
    printf '  %-14s bottles: %s\n' "unit $u" "${specs:-none}"
    printf '  %-14s /config: %s\n' "" "$path"
    printf '  %-14s dashboard: %s\n' "" "$url"
  done
  printf '  %-14s %s L each, prepared %s\n' "bottles" "$RESERVOIR_VOLUME_L" "${PREPARED_AT:-now}"
  printf '  %-14s %s:%s\n' "server" "$BIND_HOST" "$PORT"
  printf '  %-14s %s <%s> (%s)\n' "operator" "$GIT_NAME" "$GIT_EMAIL" "$OPERATOR_INITIALS"
  printf '  %-14s %s\n' "python" "$PY"
}
if [ "$INTERACTIVE" = "1" ]; then
  say "Ready to write"
  summary
  printf '\n  Write the log, token and settings, and commit? [y/N] '
  IFS= read -r OK || OK=""
  case "$OK" in y|Y|yes|YES) ;; *) die "nothing was written" ;; esac
fi

# ── the log ──────────────────────────────────────────────────────────────────
say "Writing evolution_log.json for $EXPERIMENT"
created evolution_log.json
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
[ -e viewer.config.json ] || created viewer.config.json
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
created secrets/operators.json secrets/server.env
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
COMMITTED=1
git log --oneline -1 | sed 's/^/   /'

cat <<EOF

Done. Next:
  ./run_server.sh                         start it (Ctrl-C stops it)
  curl -s http://localhost:$PORT/health   check: experiment, n_reservoirs, auth_configured

Give the LLM:
  http://<this host>:$PORT/skill          how to use every route
  Authorization: Bearer <token>           from secrets/operators.json
EOF
