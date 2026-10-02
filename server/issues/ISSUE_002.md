# ISSUE_002 — physical relocation of a lineage isn't logged unambiguously, and the id can't safely "catch up"

**Status:** implemented 2026-09-01 (`app/writer.py`: `project_line_state`'s
`hardware_swap` branch now relocates a line when `params` carries both
`new_unit`+`new_vial`, reusing `assert_known_unit`/`assert_destination_empty`
-- moved from `app/lines_writer.py` into `writer.py` itself so both share
one definition, `assert_destination_empty` gaining an `exclude_line_id`
param so a line relocating to its own current vial isn't a false self-
conflict against itself; optional `previous_unit`/`previous_vial`
cross-checked against the line's actual current position, mirroring
`media_switch`'s `media_from` check exactly, same `WriteConflict`/409.
Both open questions below resolved: field names as originally proposed;
`previous_unit`/`previous_vial` optional-but-checked, matching a corrected
reading of `media_switch`'s own `media_from` (optional, not required, per
the actual code) and `target_ramp`'s existing convention. The four new
`parameter_registry` keys are registered for real in the live
`evolution_log.json`, via `POST /parameter_registry`'s own pipeline (four
separate commits in the log repo, one per key) -- not a hand-edit,
dogfooding the sibling feature added the same day. `line_id` is never
touched by any of this, confirmed by test, not just by comment. `GET
/skill` and its own regression test updated (the test used to assert the
OPPOSITE of the new behavior). 6 new scenarios in
`tests/test_line_lifecycle_projection.py`; full 18-file suite passes.
`LOG_PROTOCOL.md`'s draft language below is left in place for the operator
to place -- not this server's file to edit. `vials_in_use`/`n_lines`
reconciliation remains explicitly out of scope, as originally proposed.)

**Follow-up, implemented 2026-09-02:** a real reciprocal 8-line sleeve swap
found that the design above CANNOT express a reciprocal swap between two
lines at all -- relocating either one onto the other's still-occupied vial
409s no matter which order they're attempted in, since
`assert_destination_empty` checks each destination against the CURRENT
state one line at a time. Superseded by a simpler design (the operator's
own proposal, not the batch/atomic relocation this file originally floated
below): `line.unit`/`vial` can now be `null` *together*, a real "not
currently on any evolver" state; `hardware_swap` gained `params.vacate:
true` to enter it. A reciprocal swap of any size is now two ordinary,
single-line `hardware_swap` events per line (vacate, then relocate) --
never a multi-line atomic write, and `assert_destination_empty` needed no
changes at all, since a `null` position never conflicts with a real one.
See `README.md`'s own write-up of this follow-up for the full mechanics,
`tools/lineage.py`'s two new cross-field checks in the log repo, and the
reciprocal-swap integration test in
`tests/test_line_lifecycle_projection.py` that replays this exact scenario.
`media_switch_count` (a second, unrelated gap the same real event exposed --
it never got bumped by `media_switch`) is fixed too, entirely in the log
repo's `tools/lineage.py:recompute()`, with no server-code change.

**Status (original):** open
**Found:** 2026-09-01, while trying to log a real setpoint change surfaced
the adjacent question of how a lineage's physical location is tracked
**Affects:** `app/writer.py` (`project_line_state`), `app/skill.py`,
`LOG_PROTOCOL.md` (narrative only — no schema change needed), the
`hardware_swap` entries in `parameter_registry`

---

## The problem

`LOG_PROTOCOL.md` §4 states the identity rule plainly: *"Line identity
follows the culture, not the hardware. Vials can be moved between eVOLVER
bodies and the lines continue unbroken with no lineage edge."* But nothing
in the schema, the registry, or this server actually carries out "the
lines continue unbroken" when a vial number or unit genuinely changes.

**`hardware_swap`'s registered params are free-text only.** Confirmed by
reading `parameter_registry`: `what_moved`, `reason`, `downtime`,
`downtime_from`, `pumps_moved`, `pump_calibration_valid`,
`od_calibration_reloaded`, `control_ip_changed` — plus the generic facility
fields `unit`/`lines_affected`/`incident_id`. **There is no `new_unit` or
`new_vial` key anywhere in the registry.** A `hardware_swap` event can
*describe* a move in prose; it has no structured field to put the new
position in.

**The server explicitly, permanently no-ops it.** `app/writer.py`,
`project_line_state`, lines 559–573:

```python
if event_type == "hardware_swap":
    # Names what moved only as free text (what_moved/reason) -- no
    # structured new_unit/new_vial to move line.unit/vial to. Explicit,
    # not a silent no-op: GET /vials will keep reporting the OLD
    # position as occupied and the NEW one as empty until a human
    # updates line.unit/vial (and lineage.occupies_vial_of, if this
    # move also crosses into a vial some other line's history cares
    # about) by hand.
    return {
        "event_type": event_type, "applied": False,
        "reason": "hardware_swap has no structured destination fields in the parameter_registry "
                  "(only free-text what_moved/reason) -- line.unit/vial are NOT updated by this "
                  "event; GET /vials will report the old position as still occupied and the new "
                  "one as still empty until a human updates line.unit/vial by hand",
    }
```

**The one real historical `hardware_swap` (EVT-00187) never actually tested
this**, because it happened to preserve every vial's numbering:

```json
{
  "event_id": "EVT-00187", "event_type": "hardware_swap", "scope": "facility",
  "params": {
    "unit": "patrick",
    "what_moved": "every vial, to the same numbered position on a new eVOLVER body",
    "reason": "multiple faulty sleeves on the old body",
    "lines_affected": ["patrick-v05#2", "patrick-v07#2", "patrick-v09",
                        "patrick-v09+v10", "patrick-v11", "patrick-v12"]
  }
}
```
Checked every one of those six lines' current `unit`/`vial` fields against
their own ids right now: every one still matches its id exactly, unchanged.
This event is really a **unit-level** concept (`hardware.units.patrick`'s
own `body_history`/`body_changed_at`/`body_change_event` — the eVOLVER
chassis "patrick" runs on changed, not any individual line's position) and
is structurally distinct from a single lineage moving to a different
sleeve.

### Why this is a real risk, not just a stale field

Two separate hazards, different severities:

1. **The current-state snapshot can silently go wrong with no link back to
   why.** If a future swap ever *does* change a vial number, nothing
   updates `line.unit`/`line.vial` — `GET /vials/{unit}/{vial}` derives
   occupancy directly from those two fields plus `status`
   (`app/routes/vials.py`), so it would keep reporting the OLD position as
   occupied and the NEW one as empty, indefinitely, until a hand-edit. A
   hand-edit, in turn, has no required link to the `hardware_swap` event
   that justified it — the current location becomes a mutable field with
   no structured provenance, unlike everything else "current" in this log.
2. **The sharper risk: the design pressures the wrong fix.** `line_id`
   bakes the unit into itself — `lineIdPattern`'s regex is
   `^[a-z]+-v\d{2}(#\d+)?(\.[a-z])?(\+...)*$`, no suffix for "this moved."
   If a culture is physically relocated onto a *different* unit, the id
   either stays stale (`patrick-v09` describing hardware it no longer
   occupies) or a new id gets minted on the destination unit — which, by
   this log's own id grammar, is indistinguishable in form from a
   **branch** or **restart**. An operator or LLM reaching for "give it an
   accurate id" would, without meaning to, insert a lineage discontinuity
   that never happened. Append-only history cannot take that back once
   committed. This is the same class of mistake CLAUDE.md already calls
   "the most consequential available" — just reached through a different
   door (a relocation, not a misjudged branch/split/merge/restart call).

The operator's explicit call: **the label is allowed to go stale.** That
removes the only reason anyone would reach for fix #2 above. Fix #1 is
what this issue is actually about.

---

## Proposed solution

### Phase 1 — register the new params keys

Via `POST /parameter_registry` (the feature this server gained on
2026-09-01, specifically to replace hand-editing the registry) — no code
change:

| key | type | applies_to_event_types |
|---|---|---|
| `new_unit` | string | `hardware_swap` |
| `new_vial` | integer | `hardware_swap` |
| `previous_unit` | string | `hardware_swap` |
| `previous_vial` | integer | `hardware_swap` |

All four `status: "planned"`, `first_seen: null`, until the first real
event uses them.

### Phase 2 — project it, same shape as `termination`/`media_switch`

In `app/writer.py`'s `project_line_state`, extend the `hardware_swap`
branch:

- A **line-scoped** `hardware_swap` carrying both `new_unit` and
  `new_vial` is a relocation; project it. Neither field, or only one, keeps
  today's behavior (`applied: false`, explicit reason) — a `hardware_swap`
  that's purely about `control_ip_changed`/`pump_calibration_valid`/etc.
  with no physical move stays exactly as unprojected as it is now.
- Validate before applying, reusing the exact checks `lines_writer.py`
  already has for the same hazards on branch/split/restart:
  - `new_unit` must be a known `hardware.units` entry
    (`_assert_known_unit`'s check).
  - `new_vial` must be an integer 0–15.
  - the destination `(new_unit, new_vial)` must not already hold a
    *different* active line (`_assert_destination_empty`'s check) — two
    lines both reading as occupying one vial is exactly the ambiguity
    `GET /vials` exists to prevent.
- If `previous_unit`/`previous_vial` are supplied, cross-check them
  against the line's actual current `unit`/`vial` **before** applying —
  reject (409) on a mismatch, the same precedent `media_switch`'s
  `media_from` check already sets (`project_line_state`, lines 636–651):
  a swap premised on a stale picture of where the line currently is must
  not be applied silently.
- On success: `line["unit"] = new_unit`, `line["vial"] = new_vial`,
  reported as `touched: ["unit", "vial"]` in `line_lifecycle_projection`,
  same response shape `termination`/`media_switch` already use.
- **`line_id` is never touched, by any of this.** Explicit, permanent,
  and worth stating in code as plainly as in prose — this is the whole
  point of the operator's "the label can be outdated" call.

### Phase 3 — documentation

- `GET /skill`'s "What this API cannot do" section currently states
  `hardware_swap` is *never* projected (`app/skill.py`) — wrong once
  Phase 2 ships; update it there (owned by this server).
- Draft language for `LOG_PROTOCOL.md` for the operator to place (owned by
  the log repo, not this server):
  > A line's current `unit`/`vial` can be reassigned by a line-scoped
  > `hardware_swap` event naming `new_unit`/`new_vial`. `line_id` is a
  > permanent label, not a live description of where a culture currently
  > sits — it is never updated by a relocation, on the same unit or a
  > different one. Do not mint a new line id to "fix" a mismatched label;
  > that would fabricate a branch/restart that never happened.

### Phase 4 — tests

1. Same-unit relocation: `new_unit` unchanged, `new_vial` different —
   `line.vial` moves, `line.unit` doesn't, id untouched.
2. Cross-unit relocation: `new_unit` different from the line's current
   unit — both fields move, id still untouched (now visibly mismatched
   with `line.unit`, which is the accepted, documented tradeoff).
3. Destination already occupied by a different active line — rejected,
   nothing moves.
4. `new_unit` naming an unregistered hardware unit — rejected.
5. `previous_unit`/`previous_vial` supplied but not matching the line's
   actual current position — rejected (409), mirroring the existing
   `media_switch`/`media_from` test.
6. A `hardware_swap` with neither `new_unit` nor `new_vial` (calibration/IP
   only) — unchanged, `applied: false`, exactly today's behavior.
7. `GET /vials/{unit}/{vial}` reflects the move immediately: the new
   position shows the line occupying it, the old position shows empty.

**Explicitly out of scope:** `hardware.units.*.vials_in_use`/`n_lines`
reconciliation. CLAUDE.md already documents this as known-stale and wants
a proper server-recomputed projection for it someday — not something to
bundle into this change.

---

## Open questions for whoever implements this (resolved 2026-09-01)

1. **Field names**: kept exactly as proposed (`new_unit`/`new_vial`/
   `previous_unit`/`previous_vial`).
2. **Required vs. optional**: `previous_unit`/`previous_vial` are
   **optional but checked when present** -- correcting this issue's own
   framing above. Re-reading `app/writer.py`'s actual `media_switch` code:
   `media_from = params.get("media_from"); if media_from is not None and
   ...: raise WriteConflict(...)` — it was never actually required/
   load-bearing, only checked-when-supplied, the same shape `target_ramp`
   already uses elsewhere. `previous_unit`/`previous_vial` now match both
   precedents consistently.
