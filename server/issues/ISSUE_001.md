# ISSUE_001 — `POST /events` leaves reservoir state and `last_updated` stale

**Status:** implemented 2026-08-28 (`app/writer.py`: `project_reservoir_state`,
`advance_last_updated`; tests in `tests/test_reservoir_projection.py`, all 6
scenarios in §4 verified, including test 5 against a scratch clone of the
real log — confirmed `patrick/M9-1` now projects to a measured 0.45 L and
`tools/media.py`'s `analyse()` reports it that way instead of `0.50 L*`.
`GET /skill`'s "what this API cannot do" text updated to match. One ordering
bug caught by the existing suite before it shipped, not by a user report: the
new projection/last-updated code originally ran before schema validation, so
a malformed timestamp crashed with an unhandled exception instead of the
expected 422 — fixed by making the internal timestamp parser return `None`
on failure rather than raising. The live log's own stale projection (the
`2026-08-28T11:30` readings this issue describes) is a separate follow-up
for whoever operates the server next — see this file's closing note, still
accurate and not carried out by this change.)

**Status (original):** open
**Found:** 2026-08-28, first day of API-only logging
**Affects:** `app/writer.py`, every consumer of `reservoirs[]` and
`log_meta.last_updated`

---

## The problem

`POST /events` appends the event correctly and does nothing else. Two kinds of
derived state in `evolution_log.json` are left behind:

1. **`reservoirs[].current_volume`, `level_as_of`, `level_source`,
   `level_qualifier`** — the current-state projection of a reservoir. A
   `level_reading` or `media_prep` event is the *only* thing that should ever
   move these, and the server writes the event without moving them.
2. **`log_meta.last_updated`** — never touched, so it still names whenever a
   human last edited the file by hand.

Both are known gaps: the generated `/skill` text says so under "What this API
cannot do". This issue is that the consequences are worse than "a field is
stale", because two downstream tools read the projection rather than the
events and report confident wrong numbers from it.

### What it actually broke

On 2026-08-28 at 11:30 an operator logged eight `level_reading` events —
every active reservoir — through the API (`EVT-00296`..`EVT-00303`). The
events are well formed and validate. Then:

- **`viewer.html`'s reservoir panel** reads `reservoirs[].current_volume`. It
  showed the previous day's levels. The operator's report was "the viewer
  doesn't display all events" — the events were there, the projection they
  feed was not.
- **`tools/media.py`** defaults its `at` to `log_meta.last_updated`. It
  therefore reported "Media status as of 2026-08-27T18:25", ~17 h stale, and
  projected every level to that instant. `patrick/M9-1` printed as `0.50 L*`
  — the `*` meaning "prepared volume, nothing measured since" — when a
  measured 0.45 L for that bottle was sitting in the log.

Note the asymmetry, because it is the dangerous part: `media.py` derives
**rates** from the events, so the rate column was correct while the level
column was wrong. Output that is half right is harder to catch than output
that is wrong throughout, and neither tool raised anything. Nothing failed;
the numbers were merely false.

### Why this is urgent now

Until 2026-08-27 every write went through a human editing the file, who
updated the projection in the same pass. From 2026-08-28 all logging is via
this API. **Every future level reading will silently fail to move the state
it exists to record**, and the divergence grows monotonically.

---

## Proposed solution

### 1. Project reservoir state inside the write transaction

In `app/writer.py`, after `build_event` and before validation, apply the
event's effect on `reservoirs[]` to the same candidate document, so it is
covered by the existing validate → `assert_pure_append` → commit pipeline
and cannot half-apply.

| Event | Effect on the named reservoir |
|---|---|
| `level_reading` | `current_volume` ← `params.volume_remaining`; `level_as_of` ← event timestamp; `level_source` ← `params.level_source`; `level_qualifier` ← `params.measurement_qualifier` |
| `media_prep` | as above from `params.volume_prepared`, plus `volume_prepared`, `prepared_at`, `prepared_by_event`, `level_source: "prepared"`, and close the outgoing bottle into `fill_history` with `retired_at` and `remaining_at_swap` |

Rules:

- **Only ever move it forward.** If the event's timestamp is older than the
  existing `level_as_of`, append the event but leave the projection alone. A
  backfilled reading must not overwrite a newer measurement.
- **Never invent.** If `params` lacks the field a column needs, leave that
  column untouched rather than guessing — and do not silently succeed:
  return the untouched columns in the response so the caller sees it.
- `reservoir_swap` names several positions in `reservoir_ids` and carries
  `volume_to_<unit>` keys rather than a single `reservoir_id`. Either handle
  that shape explicitly or reject it as out of scope; do not let it fall
  through the `reservoir_id` path and update nothing.

### 2. Set `log_meta.last_updated`

Set it to the appended event's **timestamp**, not to wall-clock now, and only
when that is later than the current value. The field's job is "how current is
this log's content", and an event logged an hour after the fact should not
make the log claim to be current as of now.

Format: ISO 8601 with a **colon** offset (`-04:00`). `strftime("%z")` emits
`-0400` and the schema's `timestamp` pattern rejects it — this exact bug was
fixed in the log repo's `tools/carry_generations.py`, don't reintroduce it.

### 3. Do not compute anything else here

`reservoirs[].lines_fed`, `hardware.units.*.n_lines` and `vials_in_use` are
also stale, by older and unrelated routes. They are **out of scope** — they
are functions of the line roster, not of an event's payload, and belong in a
recompute pass (log repo `SERVER_DESIGN.md` notes `n_lines` should become a
server-recomputed projection). Fixing them opportunistically here would mix
two different kinds of derivation in one transaction.

### 4. Tests

1. `POST` a `level_reading`; assert the reservoir's `current_volume`,
   `level_as_of` and `level_source` all move, in the *same* response.
2. `POST` one with a timestamp older than the current `level_as_of`; assert
   the event is appended and the projection is unchanged.
3. `POST` a `media_prep`; assert `fill_history` gains a closed entry with
   `remaining_at_swap` and the new bottle becomes current.
4. `POST` a `level_reading` whose `params` omits `measurement_qualifier`;
   assert no invented value and that the response reports the column as
   untouched.
5. Regression on the real failure: replay `EVT-00296`..`EVT-00303` against a
   scratch clone, then assert `tools/media.py` reports `as of` 11:30 and a
   *measured* 0.45 L for `patrick/M9-1` — not `0.50 L*`.
6. Assert `log_meta.last_updated` matches the schema `timestamp` pattern
   (colon offset), and never moves backwards.

Use a scratch clone via `LOG_REPO_PATH`, never the live log — appends are
permanent and would consume real event ids.

---

## Note for whoever picks this up

The eight readings from 2026-08-28T11:30 are correctly recorded as events;
only the projection is behind. Once this is fixed, the projection can be
brought current by replaying the newest `level_reading` per reservoir — do
**not** hand-edit `reservoirs[]` to patch it, and do not re-post the events,
which would duplicate them under new ids. Nothing in the history is wrong,
so nothing in the history needs correcting.
