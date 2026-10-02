# evolver-log

Everything for one eVOLVER evolution experiment's provenance log, in one repo:
the log itself, the server an LLM appends to it through, the tools that
validate it, and the viewer.

```
evolution_log.json      the record (created by init_experiment.sh; the server commits every write)
init_experiment.sh      run once per checkout: writes the log, a token, the server's settings
run_server.sh           start the server
server/                 the FastAPI app (server/app) and its tests (server/tests)
schema/  tools/         the log's schema and the tools that validate it
viewer.html             a browser view of the log, served by the server at /viewer/
dashboard.py            the per-rig dashboard that serves live pump/OD data at /api/v1/*
evolver_code/           the eVOLVER controller code and its config validator
reference/or05_log.json a frozen copy of the OR05 log: the vocabulary new logs start from,
                        and the real data the test suites mutate
LOG_PROTOCOL.md         the rules for what goes in the log -- read before touching it
SERVER_DESIGN.md        why the server is built the way it is
```

## Setting up a new experiment

One checkout = one experiment. On the machine that will run the server:

**1. Python.** Any interpreter with the server's packages:

```
python3 -m venv ~/py
~/py/bin/pip install -r server/requirements.txt
```

`init_experiment.sh` checks all of them, and that `jsonschema` is new enough
for draft 2020-12, before it writes anything.

**2. Clone and initialise.**

```
git clone https://github.com/amoghpj/evolution_log.git or06
cd or06
./init_experiment.sh
```

In a terminal it asks for each setting in turn, refuses a bad answer on the
spot (and says why), then shows everything and asks before writing anything.
Without a terminal it takes the same settings from the environment instead,
for scripting:

```
EXPERIMENT=OR06 IDENTITY="OR06 evolution, phase 1" UNITS="patrick plankton" \
RESERVOIRS="patrick:LB:low:0 patrick:LB:high:5 plankton:LB:low:0 plankton:LB:high:5" \
OPERATOR_INITIALS=AJ ./init_experiment.sh </dev/null
```

`./init_experiment.sh -i` asks even for settings already in the environment,
offering them as defaults. What it asks for:

| Setting | What | |
|---|---|---|
| `EXPERIMENT`, `IDENTITY` | short name + one line. The server introduces the experiment to an LLM by these. | required |
| `UNITS` | eVOLVER unit names, lowercase letters only (the schema's rule) | required |
| `RESERVOIRS` | the media bottles, `unit:media:role:pg_g_per_L`; role is `low`/`high` (asked per unit) | required, at least one |
| `PREPARED_AT` | when the bottles were made | blank = now, and it says so |
| `PORT`, `BIND_HOST` | where the server listens | default `8556`, `0.0.0.0` |
| `UNIT_PATHS` | each unit's git checkout of the eVOLVER code, for `/config` (asked per unit; JSON in the environment) | optional |
| `DASHBOARDS` | `unit=url` of each unit's `dashboard.py`, for live pump data | optional |

You do **not** declare vials. Each culture becomes a line when it is
inoculated, through the LLM (`POST /lines`).

It writes `evolution_log.json` and `viewer.config.json` and commits them,
and writes `secrets/operators.json` (the token, printed once) and
`secrets/server.env` (everything the server reads). `secrets/` is gitignored.
It refuses to run where a log already exists. If it fails partway, it removes
everything it wrote, so you can fix the problem and simply run it again.

**3. Run it.**

```
./run_server.sh
curl -s http://localhost:8556/health
```

`/health` should show your experiment's name, `n_reservoirs` > 0 and
`auth_configured: true`. The viewer is at `http://<host>:8556/viewer/`, served
by the same process, so there is nothing else to start.

To keep it running, a systemd user unit (`~/.config/systemd/user/evolver-log.service`):

```
[Service]
WorkingDirectory=/path/to/or06
ExecStart=/path/to/or06/run_server.sh
Restart=on-failure
[Install]
WantedBy=default.target
```

then `systemctl --user enable --now evolver-log` and
`sudo loginctl enable-linger $USER`. Without lingering it stops when you log out,
which looks exactly like a crash.

**4. Point an LLM at it.** Give it

- `http://<host>:8556/skill`: generated live, it explains every route
- the token, as `Authorization: Bearer <token>`, from `secrets/operators.json`

It needs to be able to make HTTP requests (Claude Code with `curl` is enough)
and to reach the host. The intended network is Tailscale, not the public internet.

## Updating the code on a running server

Code and log share this repo, and the server commits log events into it, so
the server host's checkout is always ahead of GitHub by some log commits:

```
git pull            # a merge, never a rebase -- rebasing re-hashes committed log events
git push            # publishes the log events too
```

then restart the server. Each server commit touches `evolution_log.json` and
nothing else, so the log's own history stays readable on its own:

```
git log -p -- evolution_log.json
```

## Checks

After any change to the log, the schema or the tools (see `CLAUDE.md`):

```
~/py/bin/python tools/validate_schema.py
~/py/bin/python tools/lineage.py
~/py/bin/python tools/test_schema.py
~/py/bin/python tools/test_media.py
~/py/bin/python tools/test_evolver_api.py
~/py/bin/python tools/make_fixture.py && node tools/test_viewer.js
node tools/test_generations.js
for t in server/tests/test_*.py; do ~/py/bin/python $t; done
```
