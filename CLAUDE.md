# evolver-log — working notes for Claude

One repo per experiment: the provenance log of a modified-morbidostat
laboratory evolution (`evolution_log.json`), the FastAPI server operators
append to it through (`server/`), and the tools that validate it. Setup is in
`README.md`.

**Read before doing anything to the log:** `LOG_PROTOCOL.md`. It is
authoritative and it is not optional. Every rule in it was learned by losing
something. This file does not restate it.

**Read before doing anything to the server:** `SERVER_DESIGN.md`. Decisions
there are settled; do not relitigate them without being asked. One has been
reversed since — see "One repo" below.

---

## Python

Use the venv at `~/py/`, never the system interpreter:

```
~/py/bin/python tools/lineage.py
```

The system `python3` may carry an old `jsonschema` that cannot handle draft
2020-12. If a tool exits **3** saying it cannot validate, that is the cause:
`~/py/bin/pip install -U jsonschema`. The tools refuse to run rather than check
less — see "Validation" below.

Do not install packages into this repo, and do not add a venv here.

---

## The one rule that outranks the others

**`evolution_log.json` history is append-only.**

- Never modify, reorder or delete an existing entry in `lines[*].events[]` or
  `experiment_events[]` — not even one that is wrong.
- A correction is a **new event** carrying `supersedes: EVT-NNNNN` and a reason.
- `reference/or05_log.json` also contains 11 events in an older idiom
  (provenance `reported (corrected)` with `corrected_from` / `corrected_at`).
  Read both idioms; write only `supersedes`.

Derived fields are never hand-edited. Run `tools/lineage.py --write`. That
covers `lineage.children` / `roots` / `depth` / `is_founder`,
`lineage_summary`, `log_meta.event_counter` and `next_event_id`. It also sorts
each line's events by `(timestamp, event_id)`.

---

## One repo

Code and log share this repo (operator's decision, 2026-10-02). It reverses
`SERVER_DESIGN.md` §7, which kept the server in a separate repo so that code
commits could not mix with log commits. What keeps that guarantee now:

- **The server commits `evolution_log.json` and nothing else.** Its commit
  is path-scoped (`git commit -- evolution_log.json` in
  `server/app/writer.py:write_and_commit`), so a staged code change is never
  swept into a log commit under an operator's name. Do not remove that
  pathspec.
- **Never commit `evolution_log.json` together with code.** A log change and a
  code change are always separate commits, so
  `git log -p -- evolution_log.json` remains a complete, uncluttered history
  of the record.
- **On the server host, update code with `git pull` (a merge), never a
  rebase.** The checkout there is always ahead by log commits; rebasing
  re-hashes them.
- **`reference/or05_log.json` is test data and vocabulary, not a log.** The
  test suites mutate copies of it; `init_experiment.sh` takes event types,
  the parameter registry and conventions from it. Never point the server at
  it, and never append to it.

---

## Validation

Run **all of these** after any edit to the log, the schema, or the tools.
Fix what fails before committing.

```
~/py/bin/python tools/validate_schema.py     # shape, against schema/ (the live log)
~/py/bin/python tools/lineage.py             # cross-field integrity (the live log)
~/py/bin/python tools/test_schema.py         # asserts the schema REJECTS bad logs (reference log)
~/py/bin/python tools/test_media.py          # media model regressions (reference log)
~/py/bin/python tools/test_evolver_api.py    # the rig-facing JSON API
~/py/bin/python tools/make_fixture.py && node tools/test_viewer.js
node tools/test_generations.js
for t in server/tests/test_*.py; do ~/py/bin/python $t; done
```

Known baseline, not something you broke: `tools/test_media.py` fails checks of
the form "… measures from its baseline, not across it", and
`server/tests/test_reservoir_projection.py` reports 3 FAIL lines. Both predate
this repo and are undiagnosed.

Before the first line exists, `validate_schema.py` reports `lines` as empty;
`tools/schema_bootstrap_check.py` is the check for that window.

Exit codes for `validate_schema.py`: `0` passed · `1` the log violates the
schema · `3` could not validate at all. `--allow-subset` opts into a partial
check and labels every result partial; never put it in automation, and never
treat a subset pass as a schema pass.

**A file that passes the schema is well formed, not correct.** The schema
validates shape only; the relationships that matter (params keys registered,
event types declared, lineage acyclic, PG floor matching the reservoir, g/L
agreeing with mM) live in `tools/lineage.py`.

---

## Commits

One commit per logged action or per discrete change. The message explains
**why**, not just what — the git history is a second, narrative copy of the
record. Do not batch unrelated changes, and never put a log change and a code
change in one commit.

---

## Things that will bite you

- **`event_id` order is not chronological.** Corrections are appended later
  carrying earlier timestamps. Sort by `timestamp`, never by `event_id`.
  `LOG_PROTOCOL.md` §3.
- **Line identity follows the culture, not the hardware.** Vials move between
  eVOLVER bodies with no lineage edge. Calibration and faults belong to the
  machine; lineage belongs to the culture. `LOG_PROTOCOL.md` §4–5.
- **A restart is not descent.** Founder, zero parents, `occupies_vial_of` for
  hardware continuity. Getting the four ways a line can begin wrong (branch /
  split / merge / restart) is the most consequential available mistake.
- **Three different PG quantities** — regime, vial concentration, delivered
  dose. Conflating them has caused real errors. `LOG_PROTOCOL.md` §6.
- **The experiment's name lives in the log** (`experiment.name` / `title`),
  and the server reads it from there for `/health` and `/skill`. Do not
  hardcode an experiment name in server code.
- **Never invent a value.** Unknown → `null` plus the field named in
  `missing_fields`. A gap can be filled; a guess cannot be detected.
- The dangerous failure in this project is **a confident number, not a crash**.
  Every dosing fault so far has been silent. `LOG_PROTOCOL.md` §11.
