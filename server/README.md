# OR05 evolution log server

FastAPI server over `evolution_log.json`, the provenance record for the OR05
modified-morbidostat evolution experiment. Full design rationale lives in the
sibling log repo's `SERVER_DESIGN.md` and `LOG_PROTOCOL.md` — read those
first; this README covers only what's specific to running and extending this
codebase.

## Why this is a separate repo

This server has an ordinary software-development commit rhythm — refactors,
dependency bumps, bug fixes — that would pollute the log repo's history if
mixed in. The log repo's discipline is one git commit per logged lab action,
and `git log -p` there is meant to *prove* every append touched no prior
history. Mixing this repo's commits into that one would defeat that
guarantee. See the log repo's `SERVER_DESIGN.md` §7 for the decision record.

This repo is a **consumer** of the log repo's schema and tools, never a
second place either gets edited. Concretely: `app/log_repo.py` imports
`tools/lineage.py` and `tools/media.py` from the log repo by file path at
runtime, rather than duplicating their logic — if that repo's own tools
change, this server picks the change up automatically the next time it's
restarted, instead of quietly drifting from what that repo's own validators
consider correct. `LOG_REPO_PATH` therefore has to point at a full checkout
of that repo (log, schema, and `tools/`) — not just the log file copied out
on its own — and `Settings` checks for all of them at first use, so a wrong
path fails loudly on the very first request rather than piecemeal, on
whichever route happens to touch the missing piece.

## Requirements

**Python 3.10+.** This code uses `X | Y` union type hints (PEP 604)
throughout, which is not valid syntax to *evaluate* before 3.10 (it parses,
then raises `TypeError` at class/function-definition time — a confusing
failure if you don't already know why). `pydantic` 2.x itself requires
Python ≥3.9 regardless. `app/main.py` checks this explicitly on startup and
fails with a clear message naming the actual problem, rather than the raw
`TypeError` from whatever module happens to trip it first — if you hit that
raw traceback instead of the clear one, you're running a version of this
code from before that check existed; update it.

If your machine's default `python3` or an existing venv is older than that
(check with `python3 --version`), create a fresh one against a newer
interpreter rather than reusing it:
`python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Running it

Uses the shared `~/py` venv, not a venv local to this repo (operator's
instruction — one Python environment across projects, not one per project).
Everything this server needs (`fastapi`, `uvicorn`, `pydantic`, `jsonschema`,
`python-multipart`, `httpx`) is already installed there; `requirements.txt`
just documents the set. Git operations shell out to the `git` CLI directly
(`subprocess`), not a library, so there's no separate git dependency to
install.

```
LOG_REPO_PATH=/path/to/or05-evolution-phase-2 \
    ~/py/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

`LOG_REPO_PATH` defaults to the parent directory, since in dev this repo
currently lives nested inside a checkout of the log repo
(`evolution_log_server/`) even though it's a separate repo. In a real
(Tailscale-hosted) deployment, point it at wherever that repo is checked out
on the host.

The `/config` routes (below) additionally need `EVOLVER_UNIT_PATHS` -- a
JSON object mapping eVOLVER unit name to that unit's own `evolver_code`
checkout (a real, writable git repo, distinct from `LOG_REPO_PATH` and from
every other unit's checkout):

```
EVOLVER_UNIT_PATHS='{"patrick": "/path/to/patrick/evolver_code", "plankton": "/path/to/plankton/evolver_code"}' \
    LOG_REPO_PATH=/path/to/or05-evolution-phase-2 \
    ~/py/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Checked at first use of any `/config` route, same discipline as
`LOG_REPO_PATH`: every path must exist, be a directory, and already be a git
repo, or the request fails loudly naming exactly which path is wrong -- and
now also fails loudly if two different unit names resolve to the literal
same directory (2026-09-01 finding: nothing used to catch that, silently
breaking "each unit has its own independent history"). If
`EVOLVER_UNIT_PATHS` is unset entirely, every other route is unaffected --
only `/config` fails, and only when it's actually used.

`CONFIG_VALIDATOR_PATH` (optional) overrides which copy of the shared
validator (`evolver_code/config_validation.py`) this server loads --
default is a symlink beside `app/config_validator.py` pointing at the
canonical copy in the sibling log repo's `evolver_code/`. Set this to point
at a different file entirely for a deployment where the two checkouts
aren't nested together, or to run this server against a different
*generation* of the validator on purpose.

`GET /docs` and `GET /openapi.json` are automatic (FastAPI).

## Testing

No pytest — matches the log repo's own testing idiom (plain scripts,
PASS/FAIL lines, an exit code), rather than mixing conventions across the two
projects' test suites.

```
~/py/bin/python tests/test_health.py
~/py/bin/python tests/test_lines.py
~/py/bin/python tests/test_reservoirs.py
~/py/bin/python tests/test_events.py
~/py/bin/python tests/test_vials.py
~/py/bin/python tests/test_write_events.py
~/py/bin/python tests/test_commit_failure.py
~/py/bin/python tests/test_write_lines.py
~/py/bin/python tests/test_skill.py
~/py/bin/python tests/test_auth.py
~/py/bin/python tests/test_media.py
~/py/bin/python tests/test_pump_media.py
~/py/bin/python tests/test_reservoir_projection.py
~/py/bin/python tests/test_line_lifecycle_projection.py
~/py/bin/python tests/test_future_timestamp_guard.py
~/py/bin/python tests/test_config.py
~/py/bin/python tests/test_config_validation_shared.py
~/py/bin/python tests/test_settings_error_visibility.py
~/py/bin/python tests/test_parameter_registry.py
```

Every test runs against a **synthetic fixture repo** built fresh per test
process (`tests/fixture.py`), not the real `evolution_log.json`. The real log
is under active, concurrent edit by the actual experiment — a test asserting
an exact line or event count against it would be flaky by construction, and
would break every time a real lab action gets logged. The fixture copies
`schema/evolution_log.schema.json`, `tools/lineage.py` and `tools/media.py`
from the real repo (shared ground truth, never re-authored here) but writes
its own small, schema-valid `evolution_log.json`: three lines (two active,
one ended, for branch/split/merge/restart to have something real to act on)
and two reservoirs with real consumption history (a `media_prep` + a
`level_reading` each, far enough apart to clear `media.py`'s minimum
measurement window, so `GET /media` has an actual measured rate to report,
not just an empty shape), with a non-empty `parameter_registry` (an empty
one disables `tools/lineage.py`'s registered-params check entirely — `if reg
and k not in reg` — rather than rejecting everything, so a truly empty
registry would silently defeat any test of that check).

`test_commit_failure.py` installs a `pre-commit` hook that always fails, to
prove the file write is reverted to HEAD rather than left ahead of git
history when the commit itself fails.

Before considering either write route done, each was also smoke-tested by
hand against a throwaway `git clone` of the real log repo (never the live
working copy) — a real `POST /events` call and a real `POST /lines` branch,
each a real git commit with a real author, each confirmed absent from the
real repo afterward. `GET /media` was checked the same way but doesn't
write anything, so this doubled as running its acceptance checks
(MEDIA_TRACKING.md §8) against the real log at the exact commit and
timestamp they name — see that file's own status note for the one finding.

## What's built

**Read-only**: `GET /health` (now also reports `auth_configured`, whether
operator tokens exist, without leaking them), `GET /lines` (+ `status`/`unit`
filters, summaries only, **sorted by `(vial, unit)`** — every unit numbers
its own vials from scratch, so a bare "vial 5" from a human can name more
than one real, active line at once; sorting this way puts every line that
has ever shared a vial number next to each other in the response, found
necessary by simulating an operator resolving exactly that kind of
ambiguity), `GET /lines/{line_id}` (full object, events
included), `GET /reservoirs` (+ filters), `GET /reservoirs/{reservoir_id}`
(id contains a slash — path param uses the `:path` converter), `GET /events`
(+ `since`/`line_id`/`event_type`/`limit`, **sorted by timestamp, never
`event_id`** — LOG_PROTOCOL.md is explicit that event_id order is not
chronological), `GET /events/{event_id}` (now also carries a computed
`superseded_by`: every event_id whose OWN `supersedes` names this one --
`supersedes` is a bare, one-way pointer with no other aggregate view
anywhere, found by simulating a correction-chain operator), `GET /vials/{unit}/{vial}` (current
occupant only; history lives in `GET /lines` + `lineage.occupies_vial_of`,
not here), `GET /media` (see below). No auth. `status` filters on
`/lines`/`/reservoirs` and `since` on `/events` now validate against the
real enum/timestamp shape (422 on a bad value) instead of silently
returning zero rows — found by simulating a filter-combination probe: a
typo'd status was indistinguishable from "genuinely no matches," and
inconsistent with `GET /media?at=` which already validated the same way.

**Every request model (`app/models.py`, `app/line_models.py`) now rejects
unrecognized fields (`StrictModel`, `extra="forbid"`) instead of silently
dropping them**, recursively at every nesting level, not just the top.
Found necessary by simulating real operator use across two different
rounds: LOG_PROTOCOL.md's own `replicate_independence`/divergence-time
requirement, and a plausible top-level guess at the deprecated
`corrected_from`/`corrected_at` correction idiom (by analogy with
`supersedes`, which genuinely is top-level), both vanished with a plain
201 and zero warning under pydantic's default `extra="ignore"` — exactly
the "confident number, not a crash" failure mode this project's docs warn
against. A follow-up sweep (a schema-nested-strictness simulation)
confirmed the SAME gap existed two levels deep too — an extra key inside
`pg_regime` or inside a concentration object was silently stripped the
same way — `StrictModel` fixes all of it in one place, since every nested
shape (`Concentration`, `PgRegimeInit`, `LineReservoirs`, ...) inherits
from it. `params` dicts are unaffected — they're plain dicts checked
separately against `parameter_registry`, not modeled fields.

**`POST /events`** — validate → append → recompute → git commit
(`app/writer.py`). Bearer-token auth (`app/auth.py`) required; the token
determines `operator` and the git commit author, never the request body.
The pipeline:

1. Build the candidate event server-side — `event_id` is always the next
   monotonic id (`log_meta.event_counter + 1`), never client-supplied.
   `build_event()` bumps `log_meta.event_counter` itself as each id is
   minted (not just once, at the very end) — found necessary by simulating
   a real split: `POST /lines`' split/merge/restart-with-embedded-
   termination handlers call `build_event()` more than once per request (a
   parent termination plus N child founding events, or vice versa), and
   without this, every event built before the final recompute collided on
   one shared `event_id`.
   `build_event()` also refuses (422) a timestamp more than an hour ahead
   of wall-clock now, for every event it mints — found necessary by
   simulating real operator use: a future-dated `level_reading` was
   accepted with no complaint, and `GET /media`'s default (no `at`, i.e.
   real "now" — which was BEFORE that future event) linearly extrapolated
   BACKWARD across it, reporting a confident, entirely fictitious "current"
   volume/hours-remaining for a reservoir whose real latest reading said
   empty. The one-hour grace window tolerates ordinary clock skew, not a
   wrong day/year.
2. Append it to a **deep-copied candidate log**, into the named line's
   `events[]` or `experiment_events` (`target: {"line_id": ...}` or
   `target: {"scope": "facility"}`, exactly one).
3. Check `caused_by_event`/`supersedes` resolve to a real, existing event —
   a cross-field check the schema itself can't express. This runs for
   `POST /lines`' new events too (founding events, embedded terminations) —
   found missing there by simulating a real "revival from an old glycerol
   stock" operator: a typo'd `caused_by_event` in a restart's
   `founding_event` was accepted with 201 and permanently written, when the
   identical mistake via `POST /events` correctly 422s. Both routes now
   share the same check (`_referenced_events_exist` in `app/writer.py`,
   called from `app/lines_writer.py:_new_events_from` too). It checks only
   that the id EXISTS, not that it's a sensible antecedent for this line —
   a reference to a real event on a completely unrelated line is still
   accepted (see "What's NOT built yet" below). A line-scoped event's
   `elapsed_h`, if supplied, is rejected (422) if negative — the one
   invariant real data actually supports; see "Findings from round 5"
   below for why a tighter "must match `timestamp - t0`" check was
   considered and rejected.
4. **Project reservoir state, project line lifecycle state, and advance
   `log_meta.last_updated`** — all three in the same candidate, so all are
   covered by the validate → `assert_pure_append` → commit pipeline below
   and can't half-apply (ISSUE_001 and its line-lifecycle follow-up;
   `project_reservoir_state`/`project_line_state`/`advance_last_updated` in
   `app/writer.py`).
   - `level_reading`/`media_prep`: move the named reservoir's
     `current_volume`/`level_as_of`/`level_source`/`level_qualifier` (and,
     for `media_prep`, close the outgoing bottle into `fill_history` first,
     and move `pg` too if `params.pg_concentration` was supplied — real
     historical events confirm this is what that registered param means
     for `media_prep`, e.g. "Prepared 1 L of LB at 0 g/L PG for the
     plankton low reservoir"; found missing by simulating a "mistaken
     reservoir concentration" operator — every OTHER field a prep implies
     moved, but this one never did, so a reservoir's own displayed
     concentration had no way to ever be corrected, not even via a
     corrective `media_prep` carrying `supersedes`). Only ever **forward**
     for the level/volume fields — an event older than the LATEST
     already-recorded reading for that reservoir is still appended but the
     projection is left alone. That comparison scans the reservoir's full
     event history, not just its `level_as_of` field: found necessary by
     simulating real operator use against a log with a known-stale
     projection, where `level_as_of` itself lagged behind an already-
     recorded (just never-successfully-projected) reading — comparing only
     against the field let a new event that was actually OLDER than real
     history look like "moving forward" and get applied, silently
     regressing state behind what was already there.
   - `reservoir_retired`: moves ONLY `status` to `"retired"` — an
     unambiguous field flip (`reservoirItem.status`'s enum is exactly
     `{active, retired}`), not time-ordered the way level/volume are, and
     deliberately does not touch `current_volume`/`lines_fed`/
     `fill_history` (those aren't implied by "this position is off duty").
     Re-retiring an already-retired reservoir is a no-op, reported as such.
   - `reservoir_swap`/`reservoir_change` are recognized but explicitly NOT
     auto-projected — both name several positions at once
     (`reservoir_ids`/`volume_to_<unit>`, or `reservoir_id_from`/`_to` as
     arrays) and guessing which of several reservoirs moved which way risks
     exactly the "confident wrong number" this project exists to catch;
     they report `projected: false` with a reason instead.
   - `termination`/`media_switch` on a *line-scoped* event: flip that
     line's `status`/`lineage.terminated_at`/`lineage.terminated_by_event`,
     or `current_media`. This is the same fix `POST /lines`' embedded
     terminations already had for the lines *they* end (`_terminate` in
     `app/lines_writer.py`) — a bare `termination` logged on its own via
     `POST /events`, the far more common case, used to leave `status`
     stuck on `active` forever, which also silently blocked any future
     branch/restart/split/merge into that line's vial (every destination
     check derives "occupied" from `status`, and `GET /vials` does too).
     **Both are refused outright (409), not silently applied, when they'd
     contradict already-recorded state** — a second, unlinked `termination`
     on an already-ended line (unless the new event's `supersedes` names
     the existing one, in which case `terminated_at`/`terminated_by_event`
     move to the correcting event), a `media_switch` whose `media_from`
     doesn't match the line's actual `current_media`, or a `media_switch`
     on a line whose `mode` isn't `"switch"` (its own registry description
     says "for a switch-mode line" -- applying one to a constant-mode line
     produced an internally contradictory record: a "constant" line whose
     media had switched, found by simulating an operator applying it to
     the wrong line). Found by simulating real operator use: all three were
     originally a soft accept-and-report (or, for the `media_from`
     mismatch, accept-with-a-warning); independent simulated operators kept
     hitting variants of "the API silently accepts a write that contradicts
     recorded state," which is exactly the "confident wrong number" failure
     mode this project's docs warn against elsewhere. (Note: there is still
     no way to ever correct a line's `mode` itself after creation -- no
     route, no registered param, no documented event type touches it; see
     "What's NOT built yet.")
   - `hardware_swap` is recognized but deliberately NOT projected at all,
     for the same reason as `reservoir_swap`/`reservoir_change`: its real
     registry entry (found by simulating a hardware-fault operator) has no
     `new_unit`/`new_vial` field, only free-text `what_moved`/`reason` --
     inventing one would mean inventing a registry entry this log doesn't
     actually have, a decision for whoever owns `evolution_log.json`'s
     registry, not this server. `line.unit`/`vial` are left untouched, and
     the response says so explicitly (previously this silently returned
     `line_lifecycle_projection: null` with zero acknowledgment that `GET
     /vials` is now wrong for both the old and new position).
   - Never invent, in every case above — a field the event's `params`
     doesn't supply is left untouched, and the response's
     `reservoir_projection`/`line_lifecycle_projection` names exactly what
     was touched/untouched or why nothing moved, rather than silently
     doing nothing.
   - `log_meta.last_updated` moves to the event's own **timestamp** (never
     wall-clock now), also only forward.
   - Every other event type: all of the above are no-ops.
5. Run `tools/lineage.py:recompute` on the candidate, then reproduce the
   `event_counter`/`next_event_id` update from that tool's own `--write` CLI
   path (a separate step there, not inside `recompute()` itself — easy to
   miss, caught by a failing test before this was fixed).
6. Validate the **whole candidate log**, not just the new event: the real
   JSON schema (`jsonschema.Draft202012Validator`, loaded from the log
   repo — see "A deliberate divergence" below), then `tools/lineage.py`'s
   own cross-field `validate()`. Any failure → 422, nothing written.
7. **Prove** the write is a pure append: every event that existed before
   must still exist afterward, byte-for-byte identical
   (`assert_pure_append`). This is what SERVER_DESIGN.md §3.A's 409 rule
   actually protects, checked directly rather than inferred from "the code
   above only appends" — not reachable through the public API today (a
   caller can't name an existing `event_id`), so it's exercised with a
   direct unit test rather than over HTTP.
8. Write via a temp file + atomic `os.replace`, then `git add` + `git
   commit` with the operator's identity. **If the commit fails**, the
   working tree is reverted to `HEAD` (`git checkout HEAD -- <path>`, not
   bare `checkout -- <path>`, which would restore from the now-staged index
   instead of undoing the add) and the call returns 500 — the file on disk
   must never sit ahead of git history, even transiently. Verified with a
   test that installs a failing `pre-commit` hook.

A single process-wide lock serializes all writes — proportionate for a
handful of human operators, not a public API.

**`POST /lines`** — the operation `POST /events` cannot do: creating a new
line. All four ways a line can begin, LOG_PROTOCOL.md §5 (`begin_mode`:
`branch` | `split` | `merge` | `restart`), plus a fifth case §5 doesn't name
because this experiment never needed it before now: a true day-one founder
(no predecessor, no parent at all — `restart` with `predecessor_line_id`
omitted). Grounded in the real data, not the prose spec alone: the two real
merges and three real restarts already in the log were inspected directly
(see git history) before any of this was written, and two points were
confirmed with the operator where real precedent ran out — see
`app/lines_writer.py`'s module docstring for exactly what and why. Briefly:

- **branch**: parent must be active (continues unaffected — gets no new
  event); destination vial must be empty. `line_id` and `occupies_vial_of`
  (if the vial was previously used) are server-derived. Every
  destination's `unit` is now checked against `hardware.units` (422 if
  unknown) — found by simulating malformed input: a wrong-case
  (`"Patrick"`) or trailing-space (`"patrick "`) unit used to be rejected
  only as an accidental side effect of the resulting `line_id` failing its
  own regex, producing a confusing multi-error cascade that pointed at the
  vial number rather than plainly saying the unit itself was the problem.
  A new line's `pg_regime.low` is now rejected (422) if it's greater than
  `.high` — an inverted regime was previously accepted and permanently
  recorded with no complaint.
- **split**: parent must be active, is ended by this call (one termination
  event); ≥2 children, each in its own empty destination vial, each its own
  ordinary founding event (`caused_by_event` pointing at the parent's
  termination) — **not** a shared `event_id` across parent and children.
  Confirmed with the operator: real merges use one event on the child only,
  so this mirrors that same "one event per line, cross-referenced" shape
  rather than the (unverifiable, since no split exists yet) alternate
  reading of LOG_PROTOCOL §5 where one event_id is shared across several
  lines' `events[]`.
- **merge**: ≥2 **distinct** parents, all active going in (naming the same
  `parent_line_id` twice is rejected, 422 — a line cannot merge with
  itself; found by simulating a real "extreme merge edge cases" operator:
  this used to be silently accepted, producing a merge child whose
  `lineage.parents` held one ancestor twice, permanently misrepresenting a
  single ancestor as two independent contributing cultures); ≥1 must be
  named in `parent_terminations` (the vial-owning one — its standing
  population is what's being overwritten) and ends; others may continue
  untouched. A destination vial matching none of the ending parents' own
  vials is rejected (422) with a message that now names exactly which
  vial(s) the ending parents actually occupy (previously it echoed back
  only what was asked for, leaving the caller to go look the real vials up
  themselves).
  `new_line.line_id` is **caller-supplied, only validated, never
  server-derived** — the one real example (`patrick-v09+v10`) names the
  *continuing* parent as the base, not the vial the child physically
  occupies, which isn't a rule this code could safely reproduce. The server
  checks the id decomposes (as a set, any order) into exactly the stated
  parents, and that it structurally matches the line-id grammar
  (`app/line_ids.py:validate_merge_id`), including the cross-unit case.
  Two bugs here found by simulating a real split-then-merge operator, both
  fixed:
  1. A parent whose own id is occupancy-shaped (`unit-vNN`, optionally
     `#N`) matched fine; anything else — a split child's `.letter` id, a
     prior merge's `+`-joined id — was rejected outright regardless of
     `new_line.line_id`, even though LOG_PROTOCOL.md §5's table has no such
     exception ("merge parent: either ... ≥2"). Fixed: a non-occupancy-
     shaped parent's id must now appear **verbatim** as one of
     `new_line.line_id`'s `+`-joined parts, instead of being rejected
     outright.
  2. A `#generation` suffix (e.g. `patrick-v12#3`) decomposed to a
     generation fragment that disagreed with itself depending on which of
     two regexes parsed it (one kept the `#`, one didn't) — a merge
     involving any restarted/reoccupied vial as a parent failed no matter
     how `new_line.line_id` was spelled. Fixed by making both regexes agree.

  **Still a real, separate limitation, in the schema (log repo), not
  fixable here**: the schema's `lineIdPattern` only allows a single
  `.letter` split-suffix, and only on the composite id's BASE (first) part
  — a `+`-addend has no `.letter` slot at all. So a split child CAN be a
  merge parent (as the id's base, addended with a plain parent), but
  merging TWO split children back together has no schema-valid id to give
  it at all. Flagging for whoever owns the log repo's schema, not
  attempting to relitigate that decision here.
- **restart**: predecessor (if any) must be active-with-embedded-termination
  or already-ended-with-none; new line is a founder, zero parents, hardware
  continuity via `occupies_vial_of` + `predecessor_in_vial` on its own
  founding event — never by touching the predecessor's own (possibly
  already-committed) termination event.

Two concepts that look similar in the schema but aren't, worth restating
because it's easy to conflate them in code: `lineage.parents` is a CULTURE
relationship; `lineage.occupies_vial_of` is a HARDWARE one
(LOG_PROTOCOL.md §4). A branch or split child's `occupies_vial_of`, if any,
is whichever *ended* line most recently held that child's own destination
vial — unrelated to who its lineage parent is. Only for merge and restart do
the two happen to coincide.

The invariant check generalizes `POST /events`'s "prove it's a pure append"
to also allow creating lines and, only for the specific lines an operation
names, transitioning them from active to ended with exactly one new
termination event
(`app/lines_writer.py:_assert_safe_mutation`) — any other existing line
gaining an event, changing status, or changing any field beyond
`lineage.children`/`roots`/`depth` (which legitimately grow when a new child
parents onto it) is refused, not silently accepted.

**`GET /media`** (`app/routes/media.py`) — consumption rates and depletion
projections, shaped from `tools/media.py` in the log repo (see
`MEDIA_TRACKING.md`, this repo's own implementation spec, for the full
design). No auth (read-only, writes nothing). `?at=<ISO timestamp>`
projects to that instant, defaulting to **request time**, never
`log_meta.last_updated` (which would freeze every result at whenever
someone last logged something) — `at` is always echoed back so a result is
reproducible. `?unit=`/`?status=` filter the reservoir list, and everything else in the
response is derived from that same filtered set. `?pump=only` is the one
exception and deliberately so: it filters only the `reservoirs` list, because
membership there would otherwise depend on which rigs happened to answer —
and `high_media_outlook` looks its low reservoir up *inside* that list,
falling back to `c_low = 0.0` when it is missing. Dropping a row for a reason
as incidental as an unreachable dashboard moved the ramp forecast by 80% and
made `delivered_pg` report "no measured rate for the low reservoir" about a
reservoir whose `rate_basis` is `measured`. So under `?pump=only`,
`pump.unavailable` does name reservoirs absent from `reservoirs`.

Response: `{at, skipped_events, reservoirs, per_line, delivered_pg,
high_outlook, attention, pump}`. `reservoirs` passes `analyse()`'s rows through **whole** — no
selecting a "useful subset". `per_line` is `analyse()`'s `perline` dict
flattened from `(media, role)` tuple keys (which can't survive JSON) into a
list of `{media, role, rate_L_per_h}`. `attention` is new: active
reservoirs with a projection, soonest-to-empty first, each still carrying
its `rate_basis` — a structured view over fields `analyse()` already
computed, not a new computation.

`pump` is the one thing here that is **not** from `analyse()`: what each
eVOLVER actually dispensed since that bottle's last level reading, making
`estimated_now_L` a measurement where the level-derived one is an
extrapolation (`app/pump_rates.py`, `issues/ISSUE_004.md`). `?pump=auto` is
the default and attaches a block to every active row — a measurement, or a
named reason there is none; `?pump=off` contacts no rig; `?pump=only` filters
as described above. The two estimates are reported side by side and are never
averaged or substituted for one another: one is a bottle someone looked at,
the other is the pumps' record of the interval since.

Rig addresses come from the log repo's own `viewer.config.json` — the same
file the viewer uses, rather than a second copy that can drift —
overridable per deployment with `EVOLVER_DASHBOARD_URLS`
(`{"patrick": "http://host:8050"}`, a URL string per unit, replacing the
file's roster whole) and `EVOLVER_DASHBOARD_TIMEOUT_S` (default 3 s). Both
are optional: with no rigs reachable, `/media` answers exactly as it always
did and says why the pump half is missing.

The rule the whole spec is organized around: never separate a depletion
figure from its provenance. Every reservoir row keeps `rate_basis`
(`measured`/`prior_bottle`/`inferred`/`upper_bound`/`unknown`),
`rate_is_upper_bound`, `rate_provisional`, `baseline_orphaned`, and
`level_source` — `GET /skill`'s hard rules say so explicitly, so a
consumer reading only the skill text still gets the warning.

**One real inconsistency found in `tools/media.py` while wiring this up,
confirmed by construction rather than assumed from reading**: a reservoir
whose rate falls back to the fastest-per-line-rate bound gets
`rate_basis: "upper_bound"`, but `analyse()` never sets
`rate_is_upper_bound` to match — it stays at its default of `False` for a
reservoir with no measured rate of its own. Zero rows in the real log have
hit this path as of this writing, which is exactly why
`tools/test_media.py` never caught it. Per this feature's own spec,
**not** fixed in `media.py` — this server shapes output, it doesn't touch
the consumption model — so it's corrected in `app/routes/media.py`
(`_fix_upper_bound_inconsistency`) instead, with a fixture in
`tests/test_media.py` that constructs the scenario deliberately rather than
waiting for the live log to produce one.

**A second `tools/media.py` inconsistency, same treatment, found by
simulating a filter-combination probe with a far-past `at`**:
`analyse()`'s linear draw-down (`now_lvl = max(lvl - rate * hours(at,
level_as_of), 0.0)`) is correctly floored at zero going forward in time,
but nothing bounds it going backward — an `at` well before a reservoir's
last reading runs the same formula in reverse and grows the level without
limit (a real case produced `estimated_now_L: 1015.349` for a 1 L bottle).
Also corrected in `app/routes/media.py` (`_clamp_backward_extrapolation`),
clamping `estimated_now_L` to the reservoir's own `prepared_L` — a
reservoir can never hold more than it was ever prepared with — and
recomputing `hours_remaining`/`empty_at` from the clamped level so the
projection stays internally consistent rather than showing a clamped level
next to an unclamped forecast.

**A third `tools/media.py` incident — this one a real production outage,
not found by simulation.** `readings_for()` dereferences
`params.volume_prepared["value"]` / `params.volume_remaining["value"]`
**unconditionally** for any `media_prep`/`level_reading` event naming a
`reservoir_id` — correct for every event a human ever hand-entered, but
this server's own write path never required that field (`project_reservoir
_state` always treated it as optional, "never invent a value" reasoning:
if it's missing, project nothing, don't reject). Two real live events
(`media_prep` on two different reservoirs, each recording only a
`pg_concentration` correction, no volume) hit exactly this gap and took
`GET /media` down with a bare `500` for **every** caller. Two-part fix,
because the log is append-only and those two events can never be
un-appended:
1. **Read side** (`app/routes/media.py:_drop_events_readings_for_cant_
   survive`) — before calling `analyse()`, drop any `media_prep`/
   `level_reading` event that names a `reservoir_id` but is missing its
   volume field, from the copy fed to it (not from the log itself — `GET
   /events` still returns it verbatim). The response's new
   `skipped_events` key names exactly which ones, so this is never a
   silent workaround. This is what actually unblocks the two already-live
   events, since nothing can remove them from history.
2. **Write side** (`app/writer.py:project_reservoir_state`) — a `media_prep`
   /`level_reading` naming a `reservoir_id` with no volume field is now
   **rejected outright (422)**, not a soft no-op, so this can't recur.
   Checked against real data before tightening it (same discipline as the
   `elapsed_h`/`(replicate, group)` near-misses): 29 of the 31 real
   `media_prep` events already supply `volume_prepared`; the 2 that didn't
   were a one-time mistake, not a legitimate convention, so this one WAS
   safe to reject.

**`GET /skill`** (`app/skill.py`) — operator-facing instructions for an LLM
client, generated fresh on every request rather than hand-maintained prose,
so it can't quietly go stale. No auth (it only describes the API; nothing
about reading it needs attribution). Each section pulls from whichever place
is actually authoritative for that fact, rather than re-describing it by
hand a second time:

- the **route table** is introspected from the live FastAPI app (path,
  method, `summary=`) — a route added or removed shows up automatically,
  never a hand-kept list that drifts from what's actually registered.
- **request shapes** for `POST /events` and each `POST /lines` `begin_mode`
  are introspected from the live Pydantic models via `model_fields`, not
  re-typed by hand. **`GET /media`'s query parameters** get the analogous
  treatment despite having no Pydantic model to introspect — a plain GET's
  `Query(...)` args live on the route's `dependant.query_params` instead, so
  their descriptions (already written once, in `app/routes/media.py`) are
  pulled from there rather than retyped a second time.
- **`event_type`s and `parameter_registry`** are read from the log itself,
  live, on every request — optionally filtered to one event_type's
  registered keys via `?event_type=<name>`.
- the **hard rules** (timestamps, `missing_fields`, `provenance`) are quoted
  verbatim from `schema/evolution_log.schema.json`'s own field descriptions
  — text already written in Phase 1/2 of the log repo's own work,
  anticipating exactly this reader, resolved through a `$ref` where a
  property has no inline description of its own (e.g. `timestamp`).

Only the narrative framing (what this is, why it exists, what's out of
scope, `GET /media`'s response shape) is static hand-written prose — there's
nothing to introspect a plain dict response against. **Not generated from
`LOG_PROTOCOL.md` or `SERVER_DESIGN.md`**, despite both being cited inside
that static prose — those citations are what the hand-written text was
inspired by, not something `render_skill()` parses. Editing either file has
zero effect on what this route returns; the two real sources of truth are
the ones listed above.

`tests/test_skill.py` checks the live pieces are actually live (add a key to
the fixture's registry, expect it to appear; filter by `event_type`, expect
real inclusion/exclusion), not just that the route returns 200.

**Audited once already, against every route that existed at the time**
(a general-purpose agent, instructed to run the real rendered output against
`make_client()` and cross-check it route by route, not just read the
template strings). It found real gaps, since fixed: `GET /health`,
`/lines`, `/reservoirs`, `/events`, `/vials` had no explanation beyond their
bare route-table row (now a `## Reading the log` section, `GET /events`'
`limit`/`total_matching` pagination included, since a silently-truncated
50-row default reading as complete is exactly the kind of confident-wrong
answer this whole project exists to avoid); the two write routes' *response*
shapes were undocumented (only their request shapes were); a `retired`
registry key was silently dropped from the list rather than shown and
tagged, with no mention anywhere that `tools/lineage.py`'s registered-params
check doesn't actually look at `status` at all — a foreign model had no way
to know a "retired" key would still be silently accepted; the `500` error
explanation covered only the retryable (commit-failed) case, not the
non-retryable one (no operator tokens configured at all, which 500s exactly
the same way every time until a human fixes it); and the route table leaked
Starlette's `{name:converter}` syntax verbatim. 13 new `test_skill.py`
checks for all of it. Worth re-running this kind of sweep after any future
route or field addition, the same way `GET /media`'s gap was found by hand
first and this fixed the rest systematically.

**`GET /config`, `POST /config/candidate`, `POST /config`** (added
2026-09-01) -- generates/validates `experiment_parameters.yaml`, the config
`evolver_code/custom_script.py`'s `Settings()` class reads to run one
eVOLVER unit's control loop. A deliberately SEPARATE feature from
everything above: forward-looking, replace-not-append config generation for
one unit's own controller, not append-only event logging into
`evolution_log.json` -- with its own skill doc (`GET /config/skill`), never
folded into `GET /skill`, to avoid an LLM blending the two idioms (see
`app/config_skill.py`'s docstring for the reasoning).

- **Does not drive the real `Settings()` class.** It calls `bye()` ->
  `sys.exit()` on any problem -- importing and instantiating it against
  request input in this server's own process would kill the server, not
  return a 422. `app/config_validator.py` instead encodes the INTENDED
  rules for `operation.mode: pumpcontrol_ramp` (by explicit instruction,
  the only mode implemented so far -- every other mode, including ones
  `Settings()` itself already handles, is `501 Not Implemented`), closing
  real gaps found by reading that class directly: every active vial
  (`to_run: true`) must fully specify its own pump parameters rather than
  silently defaulting to `0`/`100`/`10000`; `high_concentration` must
  exceed `low_concentration` (`find_optimal_pump_volumes`'s own docstring:
  `ch > cl`); a vial number may not repeat (`Settings()`'s dict
  comprehension silently keeps only the last entry); `dilution_fraction`/
  `growthdelta` are flagged as dead (read into `Settings`, never used
  anywhere else in the script) as a warning, not a rejection.
- **Catches an LLM-shaped mistake found in the real config this feature was
  designed against**: `experiment_settings.calib_name: None` in YAML is the
  four-character STRING `"None"`, not Python's `None` --
  `yaml.safe_load` has no bareword null spelled that way. `Settings()`'s own
  `.get("calib_name", None)` then reads that string right past its later
  `is not None` check, treating it as a real calibration name. Rejected
  outright for `calib_name` and any other field allowed to be genuinely
  absent -- exactly the kind of mistake an LLM trained mostly on Python is
  liable to make.
- **Per-unit git repos, never `evolution_log.json`.** Per the operator's
  explicit instruction: "POST config should not directly touch the
  log.json... There is a local git repo in each evolver specific directory.
  That git repo should be used for a per evolver logging of changes."
  `EVOLVER_UNIT_PATHS` maps unit -> its own checkout; `app/config_writer.py`
  commits into THAT repo only. Re-submitting an identical config is treated
  as success, not a `git commit` failure (`nothing to commit` is not an
  error here).
- **A config change is still required to be logged as an event -- just not
  by this route.** `POST /config`'s response always carries a `reminder`
  telling the caller to log a `controller_config_change` event via
  `POST /events` separately. Checked against real data before building
  this: `controller_config_change` already exists and is already used this
  way 17 times in the real log (e.g. `EVT-00088`, the facility-wide
  `target_ramp` doubling) -- no new event type was invented.
  `app/writer.py` now enforces that such an event actually says what
  changed: `params.controller_parameter` is required, at least one more
  params key is required, and for `controller_parameter: "target_ramp"`
  specifically, `ramp_step_size`/`previous_ramp_step_size` are required,
  matching every real `target_ramp` change in the log exactly. Other
  `controller_parameter` values (`setpoint`, `input_pump2`, ...) are only
  checked for "names something more than its own name" -- no consistent
  before/after-value convention exists yet for those (they appear in the
  real log only in fault records, never a deliberate change record), so
  none is invented here.

**Findings from an adversarial round on `/config` (6 simulated agents,
2026-09-01), fixed:**

- **`GET /config` 500'd unconditionally on the real config it was designed
  against.** The real `experiment_parameters.yaml` has ~240 optional
  per-vial fields left as YAML's `.nan` -- normal, not an edge case.
  Starlette's default `JSONResponse` calls `json.dumps(..., allow_nan=False)`
  (not the stdlib default), so returning that dict verbatim crashed with a
  bare `"Internal Server Error"`, no detail, every time. Fixed by converting
  NaN to JSON `null` at the `GET /config` boundary (`app/routes/config.py`)
  -- provably lossless here: `validate_config` already treats `None` and
  NaN identically, and `custom_script.py` never calls `float()` on an
  inactive vial's unused fields, the only ones this can apply to.
- **A NUL byte in `exp_name` crashed `POST /config` with an unhandled,
  uncaught `ValueError`, leaving the unit's repo dirty.** `exp_name` flows
  into the git commit message's argv; Python rejects an embedded NUL there
  before even forking a subprocess, so it never became the
  `subprocess.CalledProcessError` the revert logic was the only thing
  listening for. Fixed by broadening the except clause
  (`app/config_writer.py`) to `ValueError`/`OSError` too, always reverting.
- **The revert-on-failure claim was false for a unit's first-ever write.**
  `git checkout HEAD -- experiment_parameters.yaml` silently no-ops when
  HEAD has no such path yet (a brand-new unit) -- the rejected write's
  content was left on disk, staged, while the raised exception claimed
  "already been reverted to HEAD." Fixed by checking whether the file was
  in HEAD *before* the write, and using `git reset` + delete instead of
  `checkout` when it wasn't.
- **`ConfigCommitFailed`'s message leaked an absolute host filesystem path**
  on a commit failure, inconsistent with its sibling in `app/writer.py`
  (same scenario, for `evolution_log.json`), which deliberately doesn't.
  Not a perimeter breach (`POST /config` already requires a token, and any
  valid token grants what any other would per decision #3) but a real
  regression against this project's own established convention. Fixed by
  dropping the path from the message.
- **`controller_config_change`'s `target_ramp`-specific shape check was
  case- and whitespace-sensitive.** `"Target_Ramp"`, `"TARGET_RAMP"`, and
  `" target_ramp"` (leading space) all silently skipped the
  `ramp_step_size`/`previous_ramp_step_size` requirement that the exact
  string `"target_ramp"` enforced -- letting a target_ramp change through
  with no before/after values at all, the exact failure mode the check
  exists to prevent. Fixed with a strip+lowercase comparison
  (`app/writer.py`).
- **No collision detection for two unit names resolving to the same
  directory in `EVOLVER_UNIT_PATHS`.** A plausible copy/paste
  misconfiguration would silently break "each unit has its own independent
  history," with no warning at startup. Fixed with a uniqueness check on
  resolved paths in `EvolverConfigSettings.__init__`, failing loudly.

**Flagged, not fixed** (out of scope for this round, or a bigger call than
this feature should make unilaterally):

- The "at least one more params key" fallback for a `controller_config_change`
  on a parameter OTHER than `target_ramp` can be satisfied by any key
  registered anywhere in the log, even one whose own registry entry names a
  completely unrelated event type (`applies_to_event_types` is rendered in
  `GET /skill`'s prose but never enforced by any validator). Closing this
  properly means enforcing `applies_to_event_types` project-wide, well
  beyond this one check.
- `config_write_lock` is process-wide across ALL units, not per-unit
  (deliberately, mirroring `app/writer.py`'s `write_lock` for the same
  proportionality reasoning) -- but confirmed under test that this means one
  unit's HUNG git operation (a stuck hook, a stale `index.lock`) blocks
  every other unit's config writes indefinitely, with no timeout. Not a
  deadlock, but a real single point of failure specific to this lock
  spanning independent physical repos rather than one shared file.

**Also added this round, per the operator's explicit instruction:**
`evolution_log_server/evolver_code/config_validation.py` is now a symlink
to the canonical copy in the sibling log repo's `evolver_code/`, superseding
an earlier two-checked-in-copies design -- one real file, not two a test has
to keep proving are identical (`tests/test_config_validation_shared.py`
still exists, now asserting the symlink itself rather than byte-equality of
two files). `CONFIG_VALIDATOR_PATH` (see "Running it" above) makes which
copy/generation gets loaded a deployment-time choice, not a hardcoded path.
`POST /config`'s response now carries `live_reload`, splitting every field
the write actually changed into `applies_without_restart` (per-vial fields
named in `evolver_code/config_validation.py`'s `LIVE_FIELDS` -- picked up by
the rig's `refresh_live_settings()` on its very next cycle) and
`requires_restart` (everything else) -- computed per-write from a real diff
against the previous config, not a static list, so it stays accurate
however `LIVE_FIELDS` changes in the future.

**Second adversarial round on `/config` (5 simulated agents, 2026-09-01),
fixed:**

- **`POST /config` had zero safeguard against a partial "patch" silently
  destroying every other vial's real config.** `GET /config/skill`
  explicitly says `config` must be the WHOLE document, not a patch -- but
  nothing enforced it. A body naming only ONE changed vial validated as
  `valid: true` and, once written, permanently discarded the other 15
  vials' real operational settings (388 lines gone) with no warning before
  or after. Fixed with `check_no_silent_removal` (`app/config_writer.py`):
  a write that would drop a vial, or a top-level `experiment_settings` key,
  present in the CURRENT config is now a hard `422` by default -- an
  operator/LLM that means to retire something must resubmit with the new
  `confirm_removed_fields: true` request field, once, explicitly.
- **The `live_reload` diff added last round crashed `POST /config` with an
  unhandled `ValueError` -- including on the fully correct, textbook full-
  document workflow, and AFTER the write had already committed.** My own
  regression: `old_config` was read straight off disk (real, un-sanitized
  `.nan` placeholders -- the real file has ~240) and diffed with a bare
  `==`; IEEE-754 says `nan != nan`, so every untouched-but-`.nan` field on
  every other vial was reported as "changed," embedding a raw `nan` that
  then failed the same `allow_nan=False` JSON encoding the original NaN
  fix was supposed to close everywhere. Fixed by sanitizing both sides of
  the diff inside `describe_live_reload_effect` itself, so every caller
  gets the fix automatically.
- **The NaN fix from last round didn't cover `+inf`/`-inf`, which is the
  identical bug class.** `GET /config` 500'd on a `.inf`/`-.inf` already
  on disk the same way it used to on `.nan`. Worse: a literal JSON
  `Infinity` in a `POST /config` request body is NOT actually impossible
  -- Starlette's request parsing accepts the non-standard
  `Infinity`/`-Infinity`/`NaN` tokens by default, and a client's own
  `json.dumps` (also `allow_nan=True` by default) can produce exactly one
  -- so `high_concentration: Infinity` sailed through validation, got
  WRITTEN AND COMMITTED, and only then crashed the response, meaning the
  caller saw a bare 500 and reasonably believed the write had failed while
  it had already durably succeeded. Fixed two ways: `json_safe`
  (generalizing the old `_nan_to_null`) now handles all three non-finite
  values on read, and a new `find_non_finite` check rejects any of them in
  a candidate config BEFORE either route ever calls `write_config`.
- **No sign check at all on physical quantities.** A negative
  `volume`/`high_concentration`/`low_concentration`/`initial_concentration`/
  `interval`/`number_consecutive_intervals`, or `volume: 0` on an active
  vial, all validated as `valid: true` -- finite, well-typed, and
  physically meaningless. Fixed with `check_physically_impossible`
  (`app/config_writer.py`), deliberately narrow (sign only, not magnitude
  -- see "flagged, not fixed" below for why magnitude bounds are a
  separate, harder call).
- **`404 no such unit` never listed the valid unit names**, giving an LLM
  that guessed the wrong case/spelling nothing to self-correct from without
  a separate lookup. Fixed (`unknown_unit_message`), matching the existing
  `_assert_known_unit` precedent elsewhere in this server.
- **A config missing `operation` entirely surfaced as a misleading `501
  mode: null not implemented`**, reading as "try a different mode" rather
  than "you sent an incomplete document" -- right at the exact scenario
  the "not a partial patch" rule warns about. Fixed by adding a
  `likely_cause` hint to the `501` detail (`mode_not_implemented_detail`)
  when `operation` is absent entirely, without changing the status code
  (mode genuinely is unset, so `501` is still correct).
- **`hardware.units.<unit>.vials_in_use` was loaded to check the unit name
  exists and never consulted again.** An active vial well outside a unit's
  real wiring validated and wrote cleanly. Fixed as a non-blocking
  `warnings` entry, deliberately NOT a rejection -- CLAUDE.md documents
  this exact field as having known live drift (`n_lines` vs `vials_in_use`
  disagreeing for a real unit today), so treating it as authoritative
  enough to hard-block on would risk rejecting a config that's actually
  correct.

**Flagged, not fixed** (touches the shared canonical validator this server
doesn't own the content of, needs the operator's own real-world knowledge
of the hardware, or is a bigger design call than this round should make
unilaterally):

- **`evolver_code/config_validation.py`'s own `LIVE_FIELDS` sanity ranges
  (e.g. `target_ramp` clamped to `[0.0, 2.0]`, `interval` to `[0.0, 1e6]`)
  are never applied at write time, or even at rig startup** -- only at
  live-reload time, deliberately narrower than this feature's own write
  path. `target_ramp: 50.0` (25x the documented ceiling) and `interval:
  5400` (a plausible seconds-for-hours units mixup, silently freezing
  dosing for hundreds of real hours) both validate and commit cleanly, and
  `Settings.__init__` builds the rig's own runtime state from the raw
  value with no range check either. `evolver_code/config_validation.py`'s
  own docstring reasons that `validate_config`/write-time is deliberately
  structural-only and `validate_live_values`/live-reload-time is the real
  safety gate -- worth confirming with the operator whether that's the
  intended final shape (in which case the rig-side startup gap is the real
  bug, in a file this server doesn't own) or a gap in both places.
- **No relationship check between `initial_concentration` and
  `low_concentration`/`high_concentration`.** `initial_concentration: 20.0`
  with `high_concentration: 11.0` -- starting already past the ceiling --
  validates cleanly. Same shared-file concern as above.
- **Nothing inside a config names which unit it's for.** Patrick's exact
  config, re-posted with only the outer request's `unit` changed to
  plankton, is accepted and written verbatim -- nothing catches a copy-
  paste-as-a-starting-point mistake short of physically wrong pump
  behavior later. Closing this means adding a new field to
  `experiment_parameters.yaml`'s own schema, not something to invent
  unilaterally on the server side alone.
- ~~`input_pump2` collision across DIFFERENT units is undetectable here.~~
  **RESOLVED, not a bug (2026-09-01, closed by evidence, not a guess):** a
  research pass into the real `evolution_log.json` found `patrick` and
  `plankton` with OVERLAPPING vial numbers (4, 5, 6, 11, 12)
  *simultaneously* `vials_in_use` on both -- physically possible only if
  each is a separate physical device with its own independent 0-15
  vial/pump address space, not two labels for vial groups sharing one
  board. `LOG_PROTOCOL.md`'s own vocabulary backs this up (culture
  identity, "unit," is explicitly distinct from physical hardware,
  "body," which can even change under a unit over time -- `body_history`/
  `body_changed_at` exist in the schema for exactly that). No cross-unit
  `input_pump2` check is needed; the missing one matches physical reality.
- **No per-unit awareness beyond `vials_in_use`'s new warning.** A
  `high_concentration`/`low_concentration` swap that's internally
  consistent (still `ch > cl`) but backwards relative to which physical
  reservoir is actually which is undetectable -- `experiment_parameters.yaml`
  has no `reservoir_id`-style link back to `evolution_log.json`, unlike
  `media_prep`/`level_reading` events. Inherent to what this file
  represents today, not a gap this validator alone can close.

**Third adversarial round on `/config` (5 simulated agents, 2026-09-01),
fixed:**

- **The removal check added last round could itself be defeated by a race
  between two concurrent writers.** `check_no_silent_removal` used to run
  in the ROUTE, using an `old_config` read BEFORE `write_config` ever
  acquired `config_write_lock`. Confirmed reproducible: two requests each
  reading the same stale snapshot could each pass the check relative to
  what THEY saw (neither ever claimed to remove anything), while the
  second one's commit still silently undid whatever vial/field the first
  had just added -- the safety check reporting "fine" on both requests,
  while real data was destroyed with no error to either caller. This
  wasn't the generic, already-accepted concurrent-write tradeoff
  `config_write_lock`'s own docstring argues for (which is about the
  write+commit itself not tearing) -- it was the safety check's own
  guarantee being defeated by the exact concurrency the code already has a
  lock for. Fixed with `write_config_checked` (`app/config_writer.py`):
  `old_config` is now re-read and `check_no_silent_removal` re-run INSIDE
  the same `config_write_lock` acquisition as the write+commit itself, so
  the check's verdict can no longer go stale between being computed and
  being acted on. A second writer working from a now-stale snapshot gets a
  clean `422` naming exactly what it would have silently destroyed,
  instead of silently destroying it.
- Everything else this round came back clean: `confirm_removed_fields`
  (23/23 checks -- correctly scoped to only the removal check, immune to
  the `bool("false")==True`-style coercion trap, never persisted to disk),
  YAML round-trip fidelity (numeric types, special characters, unicode,
  garbage keys, list ordering), and multi-step sequential correctness
  across an 11-write session all held up with no bugs found.
- One documentation-only note from the `confirm_removed_fields` probe: an
  unrelated garbage key nested inside `experiment_settings` (not the flag
  itself misused) gets silently persisted with no warning -- a "no
  unknown-key check" gap in the shared validator, not specific to this
  round's work.

**Flagged, not fixed** (touches the shared canonical validator this server
doesn't own, or confirms an existing flagged gap end-to-end without closing
it):

- **`config_fingerprint`'s `(mtime, size)` staleness gate can produce a
  false "unchanged," silently skipping a genuine live-reloadable change.**
  Confirmed concretely: two configs differing only in `target_ramp`'s
  value, serialized to byte-identical YAML length, with `os.utime`-forced
  identical mtimes, produce the same fingerprint -- and `refresh_live_
  settings` then leaves the rig running the STALE value indefinitely,
  believing nothing changed. Not purely theoretical: a single-digit float
  edit commonly preserves both file size and (on coarser filesystems) a
  same-second mtime. This lives in `evolver_code/config_validation.py`,
  the shared file this server doesn't own the content of.
- **The previously-flagged `target_ramp: 50.0` gap is now confirmed
  end-to-end, not just by code inspection**: a real `Settings()`
  construction (run via a real subprocess importing `custom_script.py` the
  way the eVOLVER framework does) genuinely ends up with
  `settings.target_ramp[vial] == 50.0`, 25x the documented ceiling, with
  zero refusal at startup. Confirms the earlier flag; still not fixed here
  for the same shared-file reason.
- Confirmed clean, no gap: the rig-side `live_reload` claim matches what
  the real `apply_live_values`/`extract_live_values` code actually
  produces, and a rejected write (via either `check_no_silent_removal` or
  `find_non_finite`) leaves the file byte-for-byte, and rig-validation-
  result-for-byte, unchanged -- genuinely a no-op from the rig's own
  perspective, not just "the response says 422."

**Fourth adversarial round on `/config` (5 simulated agents, 2026-09-01),
fixed:**

- **A full re-audit of `GET /config/skill` against the current code (three
  rounds of changes since it was last checked end-to-end) found one real
  doc bug: the `422` entry in "What the errors mean" was written for
  `POST /config`, but sat right after instructions to call
  `POST /config/candidate` first -- and `/candidate` never returns `422`
  for a business-rule failure, only `200` with `valid: false`. An LLM
  client branching on `status_code == 422` to detect a bad candidate would
  never see it fire. Fixed: the entry now says explicitly which route
  actually uses `422` and how, and what `/candidate`'s only possible `422`
  (a plain request-shape error, different shape again) looks like instead.
  Two minor doc additions from the same audit: idempotent re-submission
  (`201`, no new commit) and `GET /config`'s NaN/Inf-to-`null` laundering
  were both real, undocumented behaviors -- now noted.
- The self-audit of round 3's own `write_config_checked` fix (lock-once,
  check-inside-lock) found no new bug: no lock leak/deadlock on either
  exception path, the revert-on-commit-failure logic is unaffected by the
  new call path, `_unit_path` resolution can't diverge (the mapping is
  immutable after construction), and a 16-way concurrency stress test
  (the real hardware cap -- vial numbers are 0-15) produced exactly one
  201 and the rest correctly-rejected 422s, with zero data loss among the
  201s. One honest, non-bug observation: `POST /config/candidate`'s own
  preview can go stale relative to a `POST /config` that happens
  moments later -- inherent to any snapshot-based dry-run, and harmless,
  since the REAL write still re-checks fresh inside the lock regardless of
  what an earlier `/candidate` call said.
- **Resolved a previously-flagged item with real evidence, not a guess**:
  a research-only pass found `patrick` and `plankton` (the real
  `hardware.units`) with overlapping `vials_in_use` at the same time --
  physically possible only if each is a separate physical device with its
  own pump address space. The `input_pump2` cross-unit-collision gap
  flagged two rounds ago is closed: not a bug, the missing check matches
  physical reality (see the struck-through entry above).

**Flagged, not fixed** (design questions for the operator, not code bugs
against an existing spec):

- **Any valid operator token can write `/config` to ANY unit -- confirmed,
  and unlike `/events`/`/lines`, this route's stakes don't obviously fit
  the reasoning that made "token = attribution only" the right call there.**
  A wrong `POST /events` is a correctable row in an append-only log; a
  wrong `POST /config` -- even internally valid, even past every content
  safeguard this project has built across four rounds -- commits a
  correct-looking config to a REAL running culture's own file, which
  `refresh_live_settings` (confirmed, an earlier round) can apply within
  one cycle, unattended, no restart, possibly to the WRONG physical rig if
  the mistake is just a wrong `unit` string. `SERVER_DESIGN.md` decision
  #3 didn't have this route's stakes in view when it was made (this
  feature didn't exist yet). A cheap, backward-compatible fix exists if
  wanted: an optional `allowed_units` field on the existing `Operator`
  `NamedTuple` (defaulting to `None` = today's "all units," so every
  existing token entry keeps working unchanged), plus a ~4-line check in
  `write_config.py` -- `/events`/`/lines` untouched, no new permission
  system. Not implemented here; this is a policy call, not a bug fix.
- **No route in this server declares a Pydantic `response_model=`**
  (confirmed project-wide, not `/config`-specific) -- `GET /openapi.json`
  says nothing about any response shape (`schema: {}`), and worse, the
  ONE response shape it DOES auto-document for a `422` (FastAPI's own
  validation-error object) is actively wrong for `/config`'s own
  business-rule `422` (a flat list of strings). This only matters to a
  schema/tool-calling-driven LLM client, never the prose-reading one this
  whole project assumes -- a known, deliberate tradeoff per `app/skill.py`'s
  own stated design, not an oversight this round introduced. Estimated
  fix scope if ever pursued: ~5 response models across 3 routes, roughly
  half a day -- a cross-cutting change to this server's whole pattern, not
  something to fold into a bug-fix batch.

**Live incident (2026-09-01): a real deployment's `EVOLVER_UNIT_PATHS`
misconfiguration was invisible to the caller.** An operator hit exactly
the RuntimeError `app/evolver_config.py`'s own docstring describes
(`EVOLVER_UNIT_PATHS is not set...`) -- correct, specific wording, but it
only ever reached server stdout. `get_settings()`/`get_evolver_config_
settings()` are `@lru_cache`-wrapped `Depends()` callables; a plain
`RuntimeError` raised out of one is NOT caught by FastAPI's own exception
handling at all, so it propagates as an opaque, bodyless "Internal Server
Error" -- invisible to a caller that's often an LLM with no shell access to
read logs. `app/auth.py`'s `get_operator()` already had the fix for the
identical class of problem (`load_operators()`'s `RuntimeError`); it just
hadn't been applied to these two. Fixed by catching `RuntimeError` in both
dependency functions and re-raising as `HTTPException(500, detail=...)` --
`Settings`/`EvolverConfigSettings` themselves stay framework-agnostic,
raising plain `RuntimeError`, easy to construct and test with no FastAPI
involved; only the `Depends()`-facing wrapper needs to know about HTTP.
`tests/test_settings_error_visibility.py` covers both, using a
deliberately non-overridden `TestClient` (the one test file that must NOT
use `make_client_with_settings()`/`make_client_with_config()`, since both
of those exist specifically to bypass this real code path).

**`GET /parameter_registry`, `POST /parameter_registry/candidate`,
`POST /parameter_registry`** (added 2026-09-01) -- registers a new key in
`evolution_log.json`'s `parameter_registry`, the log's own extension point
for a genuinely new experimental dimension ("new keys are registered here
rather than the schema being changed" -- the schema's own `registryEntry`
$def docstring). Before this, the only way to add one was a direct
hand-edit of `evolution_log.json` -- no validation ran until AFTER the
edit already landed. The operator's own words prompted this: "I don't
think it is great to be directly manipulating the data like this."

Unlike `POST /config`, this writes to the SAME repo `POST /events`/
`POST /lines` already do -- `parameter_registry` lives in
`evolution_log.json` itself, not a separate per-unit file -- so it reuses
almost the entire existing pipeline rather than building a new one:

- `app/registry_writer.py`'s `register_parameter`/`validate_registration`
  call the exact same `validate_candidate()` (schema +
  `tools/lineage.py`) and `write_and_commit()` `app/writer.py` already has
  for `POST /events`, under the same process-wide `write_lock` (this has
  to serialize against event/line writes too, since it's the same file).
  No new schema-shape validation was written by hand: `registryEntry`'s
  `$def` (required `description`/`status`/`type`, the closed value-type
  vocabulary, `enum`/`items` shape) already fully validates a candidate
  entry generically -- this was true before this feature existed, just
  never wired into anything that ran it BEFORE a write landed.
- A "pure addition" guarantee mirrors `assert_pure_append`'s guarantee for
  event history, applied to registry keys instead: every existing entry
  must survive the write byte-for-byte; only the one new key may appear.
- **A brand-new key may only be registered `status: "planned"` (with
  `first_seen: null`) or `status: "active"` (with `first_seen` naming a
  REAL, already-existing event whose own `params` genuinely contains this
  key)** -- `status: "retired"` is refused outright for a brand-new key,
  since it describes something that already existed and stopped being
  used, not a state a fresh key can start in. The `active` path exists for
  a real, legitimate case: a human hand-edits an event using a brand-new
  key (this project has never claimed to be the only way to touch the
  log), then registers that key properly afterward -- not something
  reachable purely through this API's own two write paths in one sitting,
  since `POST /events` itself refuses an unregistered key, but a real
  scenario nonetheless.
- **Deliberately does NOT support changing or retiring an EXISTING entry**
  (flipping `planned` -> `active` once something starts using it, fixing a
  typo in a description, or retiring one the way the real `pg_target` entry
  was) -- both explicit decisions made together with the operator
  (2026-09-01): that's a genuinely different operation (an update, not a
  pure addition) needing its own design later, not bundled into this first
  cut. Attempting it via `POST /parameter_registry` 409s, naming the
  existing key and saying plainly this route can't do that.
- `GET /skill` gained a new "Registering a new `params` key" section
  (`app/skill.py`) explaining all of the above to an LLM client, right
  after the "Registered `params` keys" section that already told the
  reader to "register it first rather than inventing an ad hoc key" --
  this is the "how."

`tests/test_parameter_registry.py`: ~25 checks, including the realistic
`active`-at-creation scenario (a directly-injected event simulating a
hand-edit, mirroring `tests/test_media.py`'s `EVT-LEGACY-*` precedent) and
confirming `log_meta.event_counter`/`last_updated` are genuinely untouched
by a registry-only write (it isn't an event). Full 18-file suite passes.

**`hardware_swap` now relocates a line (`issues/ISSUE_002.md`, implemented
2026-09-01)** -- closes a real gap found while designing the
`parameter_registry` feature above: a lineage's physical move to a
different sleeve, on the same eVOLVER unit or a different one, had no
structured way to be recorded. `hardware_swap`'s real registry entry had
only free-text `what_moved`/`reason`; `app/writer.py`'s `project_line_state`
recognized the event type but permanently, explicitly refused to move
`line.unit`/`vial`, leaving `GET /vials` reporting the OLD position as
occupied and the NEW one as empty indefinitely.

- Four new `parameter_registry` keys — `new_unit`, `new_vial`,
  `previous_unit`, `previous_vial`, all `applies_to_event_types:
  ["hardware_swap"]`, `status: "planned"` — registered in the REAL
  `evolution_log.json` via `POST /parameter_registry`'s own pipeline (four
  separate commits, one per key, in the log repo's own git history), not
  a hand-edit. Dogfoods the feature this repo gained on the same day.
- A line-scoped `hardware_swap` carrying BOTH `new_unit` and `new_vial`
  relocates the line: `app/writer.py`'s `project_line_state` now applies
  it, reusing (not reinventing) the exact destination checks
  `app/lines_writer.py` already had for branch/split/restart --
  `assert_known_unit`/`assert_destination_empty` moved into `writer.py`
  itself so both modules share one definition, since `lines_writer.py`
  already imports from `writer.py` and the reverse would be circular.
  `assert_destination_empty` gained an `exclude_line_id` parameter so a
  line "relocating" to the vial it already occupies doesn't see itself as
  the conflicting occupant. Optional `previous_unit`/`previous_vial`, when
  supplied, are cross-checked against the line's actual current position
  before applying -- mirrors `media_switch`'s own `media_from` check
  exactly, including the exception type (`WriteConflict`, 409).
  Neither field, or only one, leaves `line.unit`/`vial` untouched exactly
  as before this existed -- a `hardware_swap` about calibration/pump/IP
  state alone is unaffected.
- **`line_id` is never touched by any of this** -- the whole point of the
  operator's explicit call that a label may go stale rather than risk the
  much worse alternative (minting a new id to "fix" a mismatched label
  would fabricate a branch/restart that never happened, permanent once
  committed to this append-only log). A relocated line's id can end up
  visibly mismatched with its own `line.unit` afterward; this is the
  accepted, documented tradeoff, not a bug.
- `GET /skill`'s "What this API cannot do" section, and its own regression
  test in `tests/test_skill.py` (which used to assert the OPPOSITE --
  "hardware_swap is recognized but never projected at all" -- now asserts
  the real, current behavior), both updated.
- 6 new scenarios in `tests/test_line_lifecycle_projection.py`: same-unit
  relocation, cross-unit relocation, destination-occupied rejection (plus
  confirming a line relocating to its OWN current vial is not a false
  self-conflict), unknown-`new_unit` rejection (plus `new_vial` out of
  range/wrong type), `previous_unit`/`previous_vial` mismatch rejection,
  and the existing "hardware_swap with neither field" test kept exactly
  as it was (still the real regression coverage for that case). Full
  18-file suite passes throughout.
- `LOG_PROTOCOL.md`'s own draft language (in `issues/ISSUE_002.md`) is
  left for the operator to place -- this server doesn't own that file.

**Out of scope, deliberately, per the issue:**
`hardware.units.*.vials_in_use`/`n_lines` reconciliation -- already
documented elsewhere as known-stale, wants a proper server-recomputed
projection of its own, not bundled into this change.

**Found while independently verifying the above, fixed separately
(2026-09-01):** `assert_known_unit`'s `unit not in known` requires `unit`
to be hashable. `branch`/`split`/`restart`'s own destinations never hit
this, because their `unit` field is Pydantic-typed (`unit: str` on the
request model) -- a non-string never reaches this far. `hardware_swap`'s
`new_unit` arrives through the untyped `params` dict instead, so a
list/dict value crashed with an unhandled `TypeError` (an opaque 500, no
clean body) rather than a `422`. Fixed with an explicit
`isinstance(new_unit, str)` check in `project_line_state`, in the same
place `new_vial` was already type-checked, before `assert_known_unit` is
ever called. One new regression test in
`tests/test_line_lifecycle_projection.py`.

**ISSUE_002 follow-up: "not on evolver" as a real state, plus
`media_switch_count` (2026-09-02)** -- a real reciprocal 8-line sleeve swap
exposed two gaps ISSUE_002's original relocation-only design didn't cover:

1. **Two lines trading positions could never both relocate via
   `new_unit`/`new_vial`.** `assert_destination_empty` checks each line's
   destination against the CURRENT state one line at a time -- so relocating
   line A onto line B's vial 409s while B is still there, and vice versa;
   there's no ordering that lets a reciprocal swap complete through the
   original design at all (confirmed as a real, live 409 before this was
   fixed). The operator's own alternative, simpler than the batch/atomic
   relocation this repo's own `issues/ISSUE_002.md` originally floated, is
   what shipped instead: a line's `unit`/`vial` can now be **`null`
   together** -- a real, named "not currently on any evolver" state (this
   schema's own existing convention, e.g. `pg_regime.current` being
   null-by-design on an ended line -- never a gap to fill in). A new
   `hardware_swap` param, `params.vacate: true`, moves a line to that state;
   an ordinary `new_unit`+`new_vial` `hardware_swap` moves it back out.
   **A reciprocal swap of any size is now: vacate every line first (each its
   own event), then relocate every line to its real new position (each its
   own event)** -- no line ever needs a destination that's still occupied,
   because nothing occupies a `null` position, so `assert_destination_empty`
   needed zero changes for this to work. `vacate` is rejected (422) if
   combined with `new_unit`/`new_vial` in the same event -- they're two
   separate facts, never one. Re-vacating an already-off-evolver line is
   accepted, not a 409 (`touched: []`) -- confirming unchanged state isn't a
   contradiction, same precedent `media_switch`'s own `media_from` match
   already sets; only a mismatched `previous_unit`/`previous_vial` is
   refused. The schema change (`line.unit`/`vial` typed `["string", "null"]`
   / `["integer", "null"]`, plus `tools/lineage.py` cross-field checks that
   the two are always null *together* and that no two ACTIVE lines claim the
   same real `(unit, vial)`) lives in the log repo, not here -- this repo
   only had to teach `project_line_state` the new param and update its own
   test fixture to the now-required schema. Fifth new `parameter_registry`
   key (`vacate`, `status: "planned"`), registered the same way the other
   four were.
2. **`media_switch_count` never got bumped by `media_switch` events.**
   Already present in the schema as an optional integer, never wired to
   anything real. Deliberately given **zero server-code changes**: it's
   computed entirely inside the log repo's `tools/lineage.py:recompute()` by
   counting each line's own `media_switch` events from scratch, the exact
   same "recomputed on every write, never incrementally maintained by
   whatever triggered it" treatment `lineage.children`/`roots`/`depth`
   already get -- and `append_event()` here already calls
   `lineage.recompute(candidate)` on every write, so the counter is correct
   with no new code in this repo at all. Promoted from optional to required
   in the schema now that it has a real mechanism; this repo's
   `tests/fixture.py` (which builds its synthetic log by hand, not through
   `recompute()`) needed the field added to all three of its lines to keep
   passing schema validation.

12 new scenarios in `tests/test_line_lifecycle_projection.py`: vacate applies
and reports `touched: [unit, vial]`; re-vacating is idempotent
(`touched: []`); vacate rejected combined with `new_unit`/`new_vial`; vacate
rejected with a non-boolean value; vacate's own `previous_unit`/
`previous_vial` mismatch/match; relocating an off-evolver line into a real
vial; and an end-to-end replay of the actual reported scenario -- confirming
the direct relocation 409s, then vacating and relocating both lines
resolves the reciprocal swap cleanly. Full suite passes throughout.

**Not this server's job, deliberately:** fixing the ALREADY-LOGGED real
incident's stale derived state on the live deployment's own copy of
`evolution_log.json` -- that data is managed separately from this repo's
own working copy.

**Round 1 of adversarial testing on the vacate/media_switch_count feature
(2026-09-02):** 5 independent agents (type/shape, state-machine,
multi-line reciprocal/cyclic swaps, media_switch_count accuracy,
response-shape/doc-accuracy), each making real API calls against isolated
fixtures. Two real, confirmed, load-bearing bugs found and fixed:

- **`next_occupancy_id` (`app/line_ids.py`) filtered by a line's CURRENT
  unit/vial**, so a line that relocated OR vacated away from a vial made
  that vial's occupancy history invisible to future id-minting -- a later
  branch/restart into the now-empty vial tried to mint the SAME bare id
  the moved line still holds, and got refused with a confusing "already
  exists" 409 that named nothing about the real cause. Fixed by matching
  on the line's own id SHAPE (`OCCUPANCY_RE`) instead, which survives
  relocation/vacate by construction (ids are permanent -- LOG_PROTOCOL.md
  §4) -- delivering on what this function's own docstring already claimed
  ("every line that has EVER used this exact (unit, vial)") rather than
  silently degrading to "every line CURRENTLY there."
- **`_find_prior_occupant` (`app/lines_writer.py`) had the identical bug**
  for `occupies_vial_of` (hardware-continuity tracking, not id-minting): an
  ended line later vacated (its tube physically removed after being logged
  as terminated) read `unit`/`vial` as null, so a bare `==` silently missed
  it as a vial's true prior occupant. Fixed with a new helper,
  `app/line_ids.py:last_real_position`, which reconstructs a line's last
  REAL position from its own `hardware_swap` history (`new_unit`+`new_vial`
  moving it, optional `previous_unit`/`previous_vial` seeding it) when its
  current fields are null. Honestly returns `None` -- never fabricates a
  position -- when the trail runs out (e.g. the line's very first
  `hardware_swap` was a vacate with neither supplied): a documented
  limitation of the data model itself (no separate position-history field),
  not something worth guessing around.

Also fixed: `GET /skill`'s hand-written `GET /lines` field-list text had
drifted (missing `media_switch_count`, added earlier this same day) --
doc drift the introspected parts of `GET /skill` self-correct but a plain
string literal doesn't. `previous_unit`/`previous_vial`'s mismatch check
(`app/writer.py`) is now one shared, explicitly-typed helper
(`_check_previous_position`) instead of two copy-pasted blocks (relocate
and vacate) -- closes a latent gap where `previous_vial: true` or `1.0`
against a real vial of `1` passed Python's bare `!=` silently (`True == 1`,
`1.0 == 1`); it never actually mismatched a real write because a separate,
downstream validator (`tools/lineage.py:check_param_types`) happened to
catch the wrong type first, but that was defense-in-depth working by
accident, not by design.

One pre-existing, independent gap filed rather than fixed inline --
`issues/ISSUE_003.md`: `restart`'s `predecessor_line_id` is never checked
against the destination vial at all (its own docstring promises otherwise).
Predates vacate; a plain relocation reproduces it with no vacate involved.
Needs a deliberate decision (refuse or allow when the true position can't
be reconstructed) this pass didn't make unilaterally.

One semantic ambiguity documented, not changed: `media_switch_count`
(schema description, log repo) counts every `event_type==media_switch`
entry in a line's `events[]` literally, including ones the server declined
to apply (missing `media_to`) and ones later corrected via `supersedes` --
matching its own stated "a real count, not a flag," but meaning it isn't
always "how many times the media actually, successfully changed" once
either idiom is in play. Changing this would mean duplicating
`app/writer.py`'s applied/superseded logic into `tools/lineage.py`'s
`recompute()`, which doesn't otherwise know anything about a single event's
outcome -- a bigger design change than this pass makes unilaterally.

12 new regression tests added across `tests/test_write_lines.py` (the two
id-minting fixes, including the exact repro shapes) and
`tests/test_line_lifecycle_projection.py` (the `previous_vial` type
hardening). `tests/testapp.py` gained a documentation note on a real test-
harness trap found while adding these: `make_client_with_settings()`
mutates ONE shared, module-level `app.dependency_overrides` -- a second
call mid-scenario silently redirects an earlier client/settings pair still
in scope, with no error. Full suite passes throughout.

**Round 2 of adversarial testing (2026-09-02):** 5 more independent agents,
mixing fix-auditors (re-attacking round 1's own fixes) with fresh angles
round 1 hadn't covered (split/merge x vacate, write-atomicity/concurrency,
`occupies_vial_of` edge cases, a full realistic multi-step workflow
replay). One real, confirmed bug found and fixed; one confirmed doc-drift
bug; everything else -- including every attempt to re-break round 1's
fixes -- held up.

- **`last_real_position` (`app/line_ids.py`) sorted a line's own
  `hardware_swap` history by TIMESTAMP** -- exactly the ordering
  CLAUDE.md's own warning is about ("`event_id` order is not
  chronological... because corrections are appended later carrying
  earlier timestamps"), and sorting by timestamp reproduced precisely
  that failure: a correction (`supersedes: EVT-X`) legitimately carrying
  an EARLIER timestamp than the mistake it corrects sorted BEFORE that
  mistake, so the mistake's superseded, wrong position got walked LAST
  and silently won. Fixed by sorting on `event_id` instead -- a
  monotonically increasing write-order counter, which is what deciding
  "which of two conflicting values wins" actually needs; chronological
  order is a different question entirely (`tools/lineage.py` already
  makes exactly this distinction for the log as a whole). Real, silent
  downstream consequence confirmed: a new branch's `occupies_vial_of`
  came back missing where it should have named the true prior occupant,
  because `_find_prior_occupant` compares against the (miscomputed)
  reconstructed position.
- `GET /skill`'s `line_lifecycle_projection` write-up still said this key
  is "present only for `termination`/`media_switch` events" -- stale
  since `hardware_swap` joined `_LINE_LIFECYCLE_EVENT_TYPES`; it also
  never mentioned the `touched` key `hardware_swap`'s own response
  carries. Same class of doc-drift as round 1's `GET /lines` field-list
  gap, just a different sentence that wasn't caught then. Fixed.

4 new regression tests in `tests/test_write_lines.py`, replaying the
exact mistake→earlier-timestamped-correction→vacate→branch shape that
exposed the bug, plus confirming the SUPERSEDED position (vial 9, in the
test) correctly shows no occupancy history at all. Full suite passes.

**Round 3 of adversarial testing (2026-09-02, the third and final required
round):** 5 more independent agents -- auditing round 2's own `event_id`
sort fix specifically, an end-to-end sweep re-validating 19 real-API-built
scenarios against the log repo's OWN schema + `tools/lineage.py` (never
just the server's opinion of itself), an exhaustive sentence-by-sentence
`GET /skill` accuracy pass, a scale/performance attack (184 lines, 427
events, deep occupancy chains), and a 300-iteration randomized fuzzer with
its own oracle model and a fixed seed for reproducibility. Two more real
bugs found and fixed; everything else -- including the fuzzer and the
full schema/lineage cross-validation sweep -- held up clean, the strongest
signal yet that this feature has converged.

- **`previous_unit`/`previous_vial` went completely UNVALIDATED on a
  `hardware_swap` naming neither `new_unit`/`new_vial` nor `vacate`** (a
  pure calibration/IP-change note, or `previous_unit`/`previous_vial` with
  nothing else at all) -- `project_line_state`'s hardware_swap branch only
  ever called `_check_previous_position` from inside the vacate branch or
  after the relocate branch's own destination checks, so this "applied:
  false, nothing to project" shape skipped it entirely. A wrong,
  unvalidated claim then sat permanently in the line's own recorded
  history, and `last_real_position` (round 1's fix) reads
  `previous_unit`/`previous_vial` off ANY `hardware_swap` event it walks,
  trusting every one was already checked -- so a single mistaken or
  adversarial no-op event was enough to silently corrupt a LATER
  `occupies_vial_of` lookup, with no crash and no warning. Fixed by making
  `_check_previous_position` run UNCONDITIONALLY, before any of
  hardware_swap's branches -- closing this at the one place that can
  actually enforce it, write time, rather than trying to filter bad data
  out later at read time.
- `GET /skill`'s `line_lifecycle_projection` write-up claimed `reason`
  appears "optionally... for a supersedes-driven correction" -- true for
  `termination`, but `hardware_swap`'s vacate branch ALWAYS includes
  `reason` (real vacate or idempotent re-confirmation alike), regardless
  of `supersedes`. Same class of doc-drift as rounds 1 and 2's findings,
  a third and (so far) final stale sentence in the same file. Fixed.

2 new regression tests in `tests/test_write_lines.py` for the validation
fix (a wrong `previous_unit`/`previous_vial` on a no-op hardware_swap now
correctly 409s; a correct one is still accepted). Full suite passes.

**Informational, not a bug (flagged by the scale/performance round, not
acted on):** `next_occupancy_id`/`last_real_position`/
`_find_prior_occupant` are all O(total lines) per branch/restart/split/
merge call -- negligible at the real log's current size (~37 lines,
microseconds), confirmed via microbenchmark to stay well under 10ms even
at 30,000 synthetic lines. Worth revisiting only if the real log ever
grows by orders of magnitude; not a concern today.

**Task status: done.** Three full rounds of adversarial agent testing (15
agents total) against the vacate/"not on evolver" mechanism and
`media_switch_count`, per the operator's explicit requirement before this
could be marked complete. 5 real bugs found and fixed across the three
rounds (2 in round 1, 1 in round 2, 2 in round 3), plus 3 doc-drift fixes
and one viewer.html hardening found by a direct follow-up read (not an
agent round). Round 3's own fix-audits, the independent schema/lineage
cross-validation sweep, and the 300-iteration fuzzer all came back clean --
convergence, not just an absence of a fourth round to find something in.

**A `media_prep` can now bring a brand-new reservoir online (2026-09-02, done
efficiently, minimal-change, on request)** -- closes a real gap: there was
no way to add a reservoir to `reservoirs[].items` via this API at all, not
just "reactivate a retired one." Every one of the real log's 10 reservoirs
was a direct hand-edit, despite `reservoirs` (unlike `hardware`/`design`/
`experiment`) being schema-validated, not narrative (`LOG_PROTOCOL.md` §3),
and despite this being routine, recurring operational reality -- the real
log already has 3 `reservoir_change` events and 2 retirements. `GET
/skill` had documented this as an intentional, permanent limitation
("no route that writes them"); it wasn't one on reflection, just an
unfinished corner, the same shape ISSUE_001/ISSUE_002 both were.

The fix is deliberately the smallest one that works: **zero new routes,
zero new Pydantic models, zero schema changes, zero new
`parameter_registry` entries.** `app/writer.py:project_reservoir_state`'s
existing `reservoir is None` branch (previously an unconditional "nothing
to project against") now tries `_create_reservoir` first, for `media_prep`
only -- if the event supplies `media`, `role`, `pg_concentration`, and
`volume_prepared`, all four **already registered specifically for
`media_prep`** (confirmed against the real log's own registry before
writing a line of code -- every real historical `media_prep` already
carries `volume_prepared`; `media`/`role`/`pg_concentration` were already
legal, just never required together), it builds a real `reservoirItem`
and appends it. `unit` is derived from the `reservoir_id`'s own
`<unit>/<media>-<pg>` shape (the schema's own `reservoirId` pattern
guarantees exactly one `/`) and checked against `hardware.units` via the
same `assert_known_unit` every other destination check already shares --
an unknown unit prefix is refused (422), not silently created. Missing
any of the four required params falls through to the exact, unchanged
message every unknown-`reservoir_id` `media_prep` has always gotten --
zero behavior change for the 29+ real `media_prep` events that only ever
touch an *existing* reservoir. `reservoir_change`/`reservoir_swap` --
the event types that actually narrate this in real usage -- are
untouched, still explicitly not auto-projected (still genuinely ambiguous
for the reasons already documented); every real historical
`reservoir_change` has always been paired with a same-timestamp
`media_prep` per new reservoir anyway (e.g. `EVT-00025` → `EVT-00043`/
`EVT-00044`), so hooking creation into the event that already, in
practice, carries the volume needed no new multi-value shape invented for
`reservoir_change` itself.

`reservoir_projection` gains one new key, `"created": true`, only on this
path -- an ordinary update to an existing reservoir is unaffected.
`tests/fixture.py` gained two registry entries (`media`, `role` for
`media_prep`) that the real log's own registry already had -- a test-
fixture completeness gap, not a real one. 3 new scenarios in
`tests/test_reservoir_projection.py` (creation with everything supplied;
each of the four required params missing, one at a time, confirming no
partial reservoir is ever created; an unknown unit prefix refused). `GET
/skill` updated (the "cannot do" claim and the `reservoir_projection`
response-shape write-up), `tests/test_skill.py` updated to match. Full
suite passes.

Not run against this one: the three-round adversarial-agent methodology
the vacate/`media_switch_count` feature required -- explicitly skipped
per the operator's own "efficiently, least number of changes, ASAP"
framing this time, not an oversight. Worth a lighter follow-up pass if
this sees real, sustained use.

**Follow-up, same day: `media_prep` can now REACTIVATE an existing
retired reservoir too (`reactivate: true`).** The fix above only ever
fires when `reservoir_id` doesn't exist in `reservoirs[].items` AT ALL --
it does nothing for an EXISTING reservoir that's merely retired, which
turned out to be the operator's actual, immediate need (confirmed by a
real, direct test against a real retired reservoir): a `media_prep`
against an existing retired reservoir already succeeded and moved
volume/pg/current_volume exactly like any other prep, but silently left
`status: "retired"` unchanged -- `projected: true` with no hint the
reservoir was, and remained, out of service. `GET /skill` had, until
this same session, described "an already-retired reservoir" as one of
`reservoir_projection`'s `projected: false` reasons -- accurate for
`reservoir_retired` retiring something twice, but silent on what
`media_prep` does against a retired one, which is the gap that actually
mattered here.

New registered param, `reactivate` (boolean, `media_prep` only, `planned`
in the real log -- same as `vacate`/`new_unit`/etc. before their first
real use). Explicit, never inferred from the mere presence of a
media_prep: a retired reservoir's status is a deliberate administrative
fact (contamination, decommissioned), so flipping it back as an
unannounced side effect of ANY media_prep naming that id would risk
exactly the silent, unintended reactivation this flag exists to
prevent -- the same reasoning `hardware_swap`'s own `vacate` flag was
built on. Without `reactivate: true`, a media_prep against a retired
reservoir still records the fresh volume/pg (never withheld) but leaves
`status` alone and returns an explicit `note` saying so and how to fix
it. With `reactivate: true`, `status` flips to `active` in the same
write, reported in `touched`. `reactivate: true` against an
already-active reservoir is a harmless no-op (`note`, not an error) --
mirrors `vacate`'s own idempotent-reconfirmation precedent exactly.

4 new scenarios in `tests/test_reservoir_projection.py` (no-reactivate
leaves status alone with an explicit note; `reactivate: true` flips it;
the harmless no-op case; a non-boolean `reactivate` refused, 422). `GET
/skill` updated in both places (the "cannot do" section and the
`reservoir_projection` response-shape write-up, which now also
distinguishes `reservoir_retired`'s own already-retired case from
`media_prep`'s new `note`-carrying one), `tests/test_skill.py` updated
to match. Full suite passes.

**Documentation fix: `restart`'s `predecessor_line_id` vs real ancestry
(2026-09-09).** A real operator's confusion, reported directly: seeding new
lineages from a subset of already-terminated ones, expecting
`predecessor_line_id` to record the connection as biological descent. It
never does -- `restart` only ever writes `lineage.occupies_vial_of` and
`params.predecessor_in_vial`, both hardware/vial continuity, never a
`lineage.parents` edge (`lineage.parents` stays `[]`, `is_founder` stays
`true`, unconditionally -- LOG_PROTOCOL.md already states this as an
absolute, "a restart is not descent," not a limitation to route around).
`branch`/`split`/`merge` are the only three modes that create a real
`lineage.parents` edge, and all three require the named parent(s) to be
ACTIVE -- there is deliberately no mode that creates one from an
already-ended lineage: descent means a continuously-growing population
handed off, and once a line has ended there's no population left to
biologically continue, only material that can be *described*.
`founding_event.params.source_culture` (free text, already used this way
~20 times in the real log, e.g. `"1 mL of patrick-v05#2"`) is that
description -- but its own registry entry never said so, and neither it
nor `GET /skill` ever clarified that naming a prior line's id inside its
free text creates no structured link either (prose, like `notes`, not a
graph edge the server tracks).

None of this was a code defect -- `restart`'s behavior is exactly what
`LOG_PROTOCOL.md` already specifies, and is exactly right (a stock
revival or fresh re-inoculation genuinely isn't a continuation of the
original population). The gap was entirely in the docs failing to say so
clearly at every point a reader could reasonably expect it, confirmed by
checking five distinct spots a follow-up review named specifically:
`RestartRequest.predecessor_line_id`'s own field description
(`app/line_models.py`, also surfaced into `GET /skill`'s per-field
render) gained the full distinction plus the `source_culture` pointer;
`GET /vials/{unit}/{vial}`'s own mention of `lineage.occupies_vial_of`
gained a one-sentence disambiguation; `branch`/`merge`'s
`parent_line_id`/`parent_line_ids` field descriptions now cross-reference
the restart note and `source_culture` rather than leaving a reader who
only looked at `branch` to wonder why there's no ended-parent exception;
and the existing "`POST /lines` cannot express a bookkeeping correction"
warning now explicitly names `predecessor_line_id`/`occupies_vial_of` as
exactly the mechanism a genuine restart uses correctly vs. the
fabrication that warning exists to prevent. `source_culture`'s own
registry description (log repo) gained the same "descriptive provenance,
not ancestry" clarification, since `GET /skill`'s auto-generated
per-key registry section renders that text directly. 5 new assertions in
`tests/test_skill.py` pin all five. Full suite passes.

## A deliberate divergence from SERVER_DESIGN.md decision #1

Decision #1 says Pydantic models should be *the* schema, generating the
validator, the OpenAPI spec, and the skill text from one definition. This
server does not do that, and `GET /skill` only partly closes the gap: it
introspects the live Pydantic models for request *shape* and the live
route table, but the validator and the skill's hard rules/registry both
come from `schema/evolution_log.schema.json` and the log itself, not from
those Pydantic models — there still isn't one single definition all three
(validator, OpenAPI spec, skill) derive from, there are two authorities
(the Pydantic request models, and the schema+log) each covering the part
they're actually authoritative for. `app/models.py` and `app/line_models.py`
describe only the **request shape** (what a caller may supply); the actual
event and log are validated against `schema/evolution_log.schema.json` and
`tools/lineage.py` directly — the log repo's existing, two-phases-hardened
definition — not a hand-authored Pydantic re-encoding of the same 120
registry entries and event shape. Re-deriving all of that a second time
risked exactly the kind of drift Phase 1 and 2 spent their whole effort
eliminating. If this gets revisited, the right shape is probably a generator
that produces Pydantic models *from* the schema + registry, so decision #1's
"one definition" is still true — just not authored by hand twice.

## What's NOT built yet

- **Projections** (SERVER_DESIGN.md §3.B): most of the rest of it —
  `reservoirs[].lines_fed`, `hardware.units.*.vials_in_use`/`n_lines` etc. —
  recomputed by the server rather than hand-maintained. Both write routes run
  `tools/lineage.py:recompute`, which only touches
  `lineage.children`/`roots`/`depth`/`is_founder` and `lineage_summary`; none
  of §3.B's other projections besides the two below are recomputed by
  anything here yet, so they can still go stale after a server-driven write
  exactly as they already can today by hand (e.g. the
  `hardware.units.patrick.n_lines` drift CLAUDE.md already documents).
  (`line.status`/`lineage.terminated_*`/`current_media` ARE now maintained
  correctly for every write path: `POST /lines` for whichever specific
  lines an operation terminates, and `POST /events` for a bare
  `termination`/`media_switch` logged directly against a line (the gap
  found by simulating real operator use after ISSUE_001 shipped — see
  `app/writer.py:project_line_state`; both now reject outright, 409, rather
  than silently applying a write that contradicts what's already recorded).
  And — as of ISSUE_001 — `reservoirs[].current_volume`/`level_as_of`/
  `level_source`/`level_qualifier`/`fill_history` (via
  `level_reading`/`media_prep`) and `status` (via `reservoir_retired`, which
  moves ONLY `status` — not volume/`lines_fed`/`fill_history`) and
  `log_meta.last_updated` ARE now maintained by `POST /events`; see the
  pipeline above. `reservoir_swap`/`reservoir_change` are explicitly still
  out of scope — see `app/writer.py:project_reservoir_state`.)
- A **web form fallback** for chat-only LLM clients (SERVER_DESIGN.md §5).
- `GET /openapi.json`'s description of both POST routes is generic (`dict`-
  ish `params`) rather than per-`event_type` typed, for the same reason as
  the divergence above.
- Multi-process safety: the write lock is per-process, not cross-process.
  Confirmed with the operator this is fine as-is: the concurrency this
  server needs to handle is multiple LLM *clients*, not multiple server
  *processes* — a single `uvicorn` worker (the deployment SERVER_DESIGN.md
  describes) already serializes all of them correctly through one lock.
  Would need a real cross-process lock only if this were ever run with
  multiple worker processes, which isn't the plan.
- **`GET /events` never says which line an event belongs to.** A line-scoped
  event carries no `line_id` (only a facility-scoped one carries `scope`).
  Reconstructing "every event across every line for incident X" means
  already knowing which line_ids to check and querying each one, or
  fetching every line's full `events[]` and cross-referencing client-side —
  there's no way to ask "give me every event tagged `incident_id: X`"
  directly, and no `incident_id` filter on `GET /events` either. Found by
  simulating a real contamination-cascade operator. Worth fixing (attach
  `line_id`/`scope` to the response's copy of each event, the same
  shallow-copy-augmentation pattern `reservoir_projection` already uses —
  never touching the stored event itself), not attempted in this pass.
- **`caused_by_event`/`supersedes` are existence-checked, never
  relevance-checked.** Both now correctly 422 on a reference to an
  event_id that doesn't exist (see the pipeline above), but a reference to
  a REAL event on a completely unrelated line is accepted without
  complaint — found by an adversarial-input simulation. Extending this
  would mean deciding what "relevant" means (same line? an ancestor
  line?), which is a real design question, not a quick patch — flagging
  rather than guessing at an answer.
- **No range/sign sanity checks on concentration or volume values** —
  confirmed empirically: a negative `volume_remaining`, a `pg_concentration`
  of `-5.0 g/L`, and a `10000 L` reading into a 0.5 L bottle were all
  accepted and written permanently, with `GET /media` projecting an
  ~95-year runway for the last one. This belongs in
  `schema/evolution_log.schema.json` (the log repo owns quantity/
  concentration bounds for every consumer, not just this server) — flagging
  for whoever owns that schema, not adding a second, server-only bound that
  a direct hand-edit to the log wouldn't be subject to.
- **Registry-rejection errors don't suggest what IS valid.** An undeclared
  `event_type` or an unregistered `params` key correctly 422s and names the
  bad value, but never lists the real `event_types`/registry keys, and
  never suggests an obvious near-match (e.g. `od600` when the registered
  key is `od`) the way GET /skill's own text would if the caller had read
  it first. Found by an adversarial-input simulation. Low-risk, worth
  doing, not attempted in this pass.
- **`incident_id` and reservoir/line references inside arbitrary `params`
  are free-form, by design** — confirmed intentional, not a gap: two
  genuinely unrelated incidents on different units CAN legitimately share
  one `incident_id` (a facility-wide cause), so no per-unit scoping or
  uniqueness check was added. Similarly, a `pg_change`/`reservoir_change`
  event's `reservoir_id_from`/`_to` naming a reservoir that doesn't exist
  is accepted (only `level_reading`/`media_prep`/`reservoir_retired`, which
  actually project state, check existence, and even then only to report
  "nothing to project," never to reject the write).

### Findings from round 3 (arcane scenarios + LLM-misunderstanding
simulations) that need a decision in the LOG REPO, not this server

None of these were fixed here — each would mean adding to or changing
`schema/evolution_log.schema.json` or `evolution_log.json`'s own
`parameter_registry`, a decision for whoever owns that repo, not something
this server should invent unilaterally (the same reasoning that's kept
concentration/volume bounds and `hardware_swap`'s missing destination
fields out of this codebase already):

- **No structured way to mark a value as "proposed, not yet confirmed by a
  human."** `provenance` is a closed enum
  (`reported`/`document`/`instrument`/`derived`) with no fifth option for
  "an LLM assistant suggested this pending confirmation," and
  `missing_fields` means a fact is *known absent*, not *invented and
  unverified* — a materially different and more dangerous state. Confirmed
  by direct construction: an honest event (no invented number, uncertainty
  spelled out in `notes`/`missing_fields`) and a deliberately fabricated
  one (a made-up concentration, `provenance: "reported"`,
  `missing_fields: []`, written as if confirmed) are **completely
  indistinguishable once both are sitting in the log** — same shape, same
  attribution, same apparent authority. This is arguably the single most
  important finding from this round: it's a risk specific to LLM operators
  (a human doesn't casually invent a plausible number under social
  pressure to seem decisive the way an assistant might), and nothing in
  the current schema gives an operator a way to be structurally honest
  about it even if they want to be.
- **`replicate_independence` and "divergence time"** (LOG_PROTOCOL.md §5:
  required whenever a child is seeded from a running culture, e.g. every
  `branch`) **have zero expression anywhere in the real API** — not in the
  params registry, not in `new_line`'s shared fields, and even forced in as
  unrecognized extra JSON they're silently dropped, not persisted. This
  documented protocol requirement currently has no enforcement and no
  dedicated field at all; it can only be honored by writing it into
  `notes`.
- **`pg_estimate_confidence` isn't enum-constrained** to its own documented
  vocabulary (low/medium/high) — any string passes, confirmed with
  `"absolutely certain, 110 percent"`.
- **`pg_estimated_actual` and `pg_controller_value` aren't required
  together** — LOG_PROTOCOL.md's "neither substitutes for the other" isn't
  backed by any cross-field check; logging the inferred value alone, with
  no setpoint of record, succeeds silently.
- **LOG_PROTOCOL.md's prose names for vial-concentration divergence
  (`vial_concentration`/`vial_concentration_estimated_actual`) don't match
  the real registry keys** (`pg_controller_value`/`pg_estimated_actual`/
  `pg_estimate_basis`/`pg_estimate_confidence`). An operator following the
  protocol doc literally gets a clean 422 on the first attempt. A
  documentation-consistency fix for the log repo, not a server bug.
- **`GET /skill` doesn't warn an LLM operator away from log-maintenance
  procedures LOG_PROTOCOL.md reserves for humans** (e.g. §10a, reconciling
  generation numbers after a controller restart) — confirmed by grepping
  the full generated doc for any mention; there is none, because `/skill`
  is generated from the live API surface, not from `LOG_PROTOCOL.md`'s
  prose. `/skill` now at least warns generally against using `POST /lines`
  to fabricate a bookkeeping-motivated "restart" (see "what this API
  cannot do," above) but doesn't and can't name every specific human-only
  procedure LOG_PROTOCOL.md documents.
- **A line's `mode` (`constant`/`switch`) has no correction path, ever** —
  set once at creation, no route/param/event type touches it afterward.
  Combined with the `media_switch`-requires-`mode=switch` fix above, this
  means a line whose `mode` was simply wrong from the start (a data-entry
  mistake at creation time) can never legitimately receive a
  `media_switch`, and there's no way to fix the `mode` field itself short
  of a direct hand-edit.

### Findings from round 4 (legacy idioms, correction chains, malformed
input, auth/attribution, filter combinations, commit durability, nested
schema strictness)

Four real fixes shipped this round (`StrictModel`'s `extra="forbid"`,
`pg_regime.low <= high`, unit-must-be-known, `GET /media`'s backward-
extrapolation clamp, plus the read-filter validation and URL-encoding doc
fix folded into the sections above). Auth/attribution and commit-failure/
git-durability simulations came back completely clean — no bugs found,
noted here so it's clear those were genuinely tested, not skipped. The
rest, checked and deliberately NOT fixed:

- **A duplicated `(replicate, group)` pair across two different lines is
  allowed, and this is correct, not a gap** — checked directly against the
  real log before considering a fix: (group, replicate) pairs repeat
  constantly in real data (up to 4 lines sharing one pair), because a
  logical replicate's identity persists across restarts/hardware moves
  while its `line_id` changes each time. Enforcing uniqueness would have
  been a real regression, breaking completely normal experiment operation.
  A round-4 simulation proposed this as a gap; checking real data first is
  what caught that it wasn't one.
- **`vial` accepts a numeric string and silently coerces it to int** (e.g.
  `"5"` -> `5`) — pydantic's ordinary lenient-coercion behavior, not data
  loss (the value survives exactly), unlike the `extra="forbid"` gap this
  round did fix. Not changed.
- **`strain` has no maximum length** — accepted a 5000+ character value
  verbatim. Consistent with how `notes` already has no cap either (a known
  characteristic, not new); a length constraint would be a schema decision
  for the log repo, not invented here.
- **The params-registry check only runs after pydantic's own shape
  validation fully passes** — a request with both a missing required field
  and an unregistered `params` key only ever surfaces the first in one
  round trip. This is the intended boundary (`app/models.py`'s own
  docstring: pydantic models describe request SHAPE, the registry
  validates CONTENT) — merging the two stages would mean moving registry
  logic into the models layer, blurring a boundary this project has
  deliberately kept clean since Phase 3 started. Not changed.
- **The legacy `provenance: "reported (corrected)"` idiom is still
  schema-legal for brand-new events, indefinitely** — CLAUDE.md's rule
  ("do not migrate the old [11 events]; write only `supersedes`") is prose
  guidance, not a schema constraint; the `provenance` regex
  (`^(reported|document|instrument|derived)( \(corrected\))?$`) permits the
  suffix on any future write. An LLM operator who pattern-matches one of
  the 11 real historical examples can still reproduce the deprecated idiom
  today, with `corrected_from`/`corrected_at` (still-registered `params`
  keys) round-tripping correctly. This round's `extra="forbid"` fix closes
  the WORSE half of this (guessing the fields belong top-level no longer
  silently discards them — it now 422s), but closing the schema-legal path
  entirely means retiring the `(corrected)` regex suffix and the two
  `params` keys, a decision for whoever owns `schema/evolution_log
  .schema.json`, not this server.
- **`supersedes` is a bare, unindexed, one-way pointer with no aggregate
  view and no ordering/conflict check** — confirmed by direct construction:
  a 3-deep correction chain works fine, but two DIFFERENT events
  independently superseding the SAME original are both silently accepted
  with no flag that they conflict, and a "correction" timestamped BEFORE
  the event it corrects is accepted too. Deliberately not treated as an
  error to reject: unlike two terminations of the same line (a real state-
  machine contradiction, already rejected in round 2), two independent
  corrections of the same narrative fact from two different operators
  reflects a plausible, legitimate async-multi-operator scenario this
  server is explicitly built for — rejecting it outright would risk
  blocking a real correction just because another one got there first.
  Whether a correction's timestamp may legitimately precede what it
  corrects is also genuinely ambiguous (a correction can be about WHEN
  something really happened, not just what). Flagging both rather than
  guessing at a fix; a real fix would be additive (e.g. a `superseded_by`
  reverse-lookup on `GET /events/{id}`), not a new rejection.
- **Duplicate `Authorization` headers are resolved by taking the first
  one** — standard ASGI/Starlette header-parsing behavior, not app logic;
  RFC 7230 treats duplicate `Authorization` headers as invalid, but no
  realistic client sends two, and the behavior is deterministic (not
  undefined) if one ever does. Not changed.

### Findings from round 5 (intra-request conflicts, hot-reloading tokens,
`elapsed_h` consistency, concurrent split/merge/branch/restart races,
pathological input sizes, compounded validation failures)

By this round, the return on further simulation was visibly diminishing —
most scenarios came back clean, and one proposed fix (`elapsed_h`) was
caught as WRONG by checking real data before shipping it, rather than
finding a new bug. Two real fixes shipped (a `superseded_by` reverse-lookup
on `GET /events/{event_id}`, and `app/auth.py` no longer crashing opaquely
on invalid JSON in the tokens file); a scoped-down `elapsed_h` guard
(negative values only, not a full consistency check); everything else
below either came back completely clean or is flagged rather than fixed.

- **A tighter `elapsed_h` check was drafted, checked against real data, and
  REJECTED before shipping.** The obvious rule — reject unless `elapsed_h`
  matches `timestamp - line.t0` within a small tolerance — looked safe
  (real data appeared to confirm it, e.g. EVT-00277's `elapsed_h: 119.3` is
  exactly right for its own line). But a systematic check against all 166
  real `elapsed_h` values turned up 5 real historical events
  (`EVT-00267`/`269`/`271`/`273`/`275`, a facility-level sampling batch
  logged across 5 different lines at one shared timestamp) that all carry
  the identical `elapsed_h`, matching each line's own `t0` for only one of
  them — off by up to ~96h for the others. `elapsed_h` evidently can
  legitimately be reckoned from a shared/facility reference point for a
  batch action, not only the individual line's own `t0`. Shipping the
  tighter check would have rejected a real, already-used convention — this
  is the SAME shape of near-miss as round 4's `(replicate, group)`
  proposal: check real data before enforcing a rule that looks obviously
  right from a handful of examples. What actually shipped instead:
  `elapsed_h` can never be negative (true across all 166 real values,
  regardless of reference point), and nothing tighter.
- **`app/auth.py:load_operators()` crashed opaquely on invalid JSON in the
  tokens file** — a raw, uncaught `json.JSONDecodeError` propagated past
  every other, more careful error path in that function (all of which
  raise a clear `RuntimeError` the route turns into a diagnosable 500
  detail), producing FastAPI's generic, contentless "Internal Server
  Error" instead — found by simulating a hot-reload-of-tokens operator.
  Fixed to raise the same kind of clear, named error as every other
  failure mode in that function. Confirmed while investigating: token
  hot-reload itself (adding or revoking a token with no server restart)
  already worked correctly and immediately — no caching layer, no fix
  needed there.
- **Every validator in this server gates the next, strictly sequentially**
  — confirmed directly by injecting several simultaneous problems into one
  request: pydantic's own shape/extra-field/model-validator checks run
  first and batch together (multiple `extra_forbidden` errors CAN appear
  together), but nothing from a later layer (future-timestamp, dangling-
  reference, registry, unit-validity, `pg_regime` shape) is even attempted
  until every earlier layer passes cleanly, and each business-logic check
  in that later sequence also appears to run one at a time. A request with
  N distinct problems takes N round trips to reach success, with each
  response naming only the next problem, never all of them — despite `GET
  /skill`'s own text implying otherwise ("fix all of them and retry"). Same
  reasoning as round 4's identical finding at the pydantic/registry
  boundary specifically: unifying every validator into one pass would mean
  substantially restructuring how validation is layered in this server,
  for a real but modest UX improvement (an LLM operator can just resubmit
  quickly, and each response IS accurate about what it reports, just not
  exhaustive). Flagged, not restructured; `GET /skill`'s claim is worth
  softening in a future pass to say "one problem may not be the only one."
- **An intermittent, unreproducible 500 on cold start** — two independent
  simulations, both among the first agents to hit a just-started scratch
  server, each saw one or two bare `Internal Server Error` responses on
  their very first requests (one on plain minimal-valid writes, one on a
  malformed `begin_mode` combined with a second field error), which never
  recurred on retry and could not be reproduced deliberately afterward.
  Both scratch servers in question were hit by 6-8 agents dispatching
  their first requests within moments of each other, immediately after
  process start — a pattern of concurrent "first contact" this specific
  testing methodology creates but a real deployment (one human/LLM
  operator at a time, per `SERVER_DESIGN.md`'s own stated concurrency
  model) is unlikely to reproduce. Flagged rather than guessed at with a
  blind fix, since the root cause (a lazily-initialized pydantic
  discriminated-union schema? a cold `@lru_cache`d module import racing
  under concurrent first access?) was never pinned down.
- **Everything else came back clean**: two children of one split both
  targeting the identical destination vial correctly 409s with the whole
  split left completely untouched (not half-applied); the `begin_mode`
  discriminator cleanly rejects a missing/unknown/wrong-case tag and
  cross-mode field leakage on any *single*-problem request; concurrent
  split/merge/branch-vs-restart races (byte-identical competing requests,
  thread-barrier-synchronized) all resolve to exactly one winner and a
  clean 409 for the loser, with zero partial writes, zero duplicate
  terminations, and correct lineage on every line touched; and pathological
  input sizes (a 2 MB `notes` string, a 10,000-element `missing_fields`
  array, 5,000 unregistered `params` keys, 200 levels of JSON nesting, an
  empty body, a top-level JSON array, and raw invalid/binary bytes) all
  produced clean, fast (&lt;0.2s) 4xx responses or bounded-time 201s, never a
  hang, a crash, or a stack-overflow-style failure.

**Capstone check, run after each of rounds 4 and 5**: the scratch clone each
round's agents had been writing to for hours — full of real terminations,
splits, merges, restarts, hardware swaps, rejected malformed-input attempts,
and concurrent races — was run through the log repo's OWN offline
validators (`tools/validate_schema.py`, `tools/lineage.py`) afterward, not
just this server's test suite. Both passed cleanly both times ("schema OK",
"log is internally consistent"), confirming this server's write pipeline
holds up against the log repo's own authoritative checks even under
adversarial, concurrent, multi-operator load across five rounds of
simulation — not just this server's own idea of what "valid" means.
