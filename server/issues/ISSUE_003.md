# ISSUE_003 — `POST /lines` `begin_mode=restart` never checks `predecessor_line_id` against the destination vial

**Status:** open
**Found:** 2026-09-02, by a round-1 adversarial-testing agent probing the
`hardware_swap` vacate/"not on evolver" follow-up's state-machine
interactions — the gap itself predates vacate (a plain relocation reproduces
it too) and is a `restart`-request-model issue, not a defect in vacate's own
projection logic, which is correct.
**Affects:** `app/lines_writer.py` (`_do_restart`), `app/line_models.py`
(`RestartRequest`)

---

## The problem

`app/line_models.py`'s `RestartRequest.predecessor_line_id` docstring states
a real constraint: *"When given, must name an existing line at the
destination vial."* Nothing enforces it.

`_do_restart` (`app/lines_writer.py`) takes `req.predecessor_line_id` and
passes it straight through as `occupies_vial_of` (via `_build_line`), unlike
`branch`/`split`/`merge`, which independently *derive* the true prior
occupant via `_find_prior_occupant` rather than trusting caller input. There
is no check anywhere that the named predecessor's own position ever matched
the new line's destination `(spec.unit, spec.vial)`.

**Repro:** restart into a brand-new vial 12 naming
`predecessor_line_id: "testunit-v03"`, where `testunit-v03` is active and
actually sitting at `(testunit, 3)` — nowhere near vial 12. The request
succeeds (201); the new line `testunit-v12` is created with
`lineage.occupies_vial_of: "testunit-v03"` and
`params.predecessor_in_vial: "testunit-v03"` — a false hardware-continuity
claim, permanently written into append-only history. No crash, no rejection,
no warning.

Vacate makes the mismatch easier to trigger by accident (a predecessor whose
own `unit`/`vial` are currently `null` is just as uncritically accepted as
any other wrong-vial predecessor — there's no check either way), but it
didn't create the gap: any two unrelated lines already reproduce it, with or
without the vacate follow-up in the picture at all.

## Why this wasn't fixed alongside the vacate follow-up

Fixing it correctly needs `app/line_ids.py:last_real_position` (added for
the vacate follow-up's own `_find_prior_occupant` fix) — the predecessor's
position at the moment of restart might itself be reconstructed history
(vacated after ending), not just its current `unit`/`vial`. That part is
mechanical. The harder, undecided part: what should happen when
`last_real_position(predecessor)` returns `None` — genuinely unknown, not
just mismatched? Refusing outright (this file's default instinct — CLAUDE.md:
"never invent a value") could reject a real, correct restart whose only sin
is that its predecessor's founding position was never captured on record
(the same known, documented gap `last_real_position`'s own docstring
already names). Whoever picks this up should decide that question
deliberately, not as a rushed addendum to an unrelated feature.

## Proposed solution

In `_do_restart`, after resolving `predecessor` (and before or after its
termination — position isn't touched by termination either way): compute
`last_real_position(predecessor)` and compare against
`(spec.unit, spec.vial)`.

- **Mismatch** (a real position that disagrees): reject (`ValidationFailed`
  or `WriteConflict`, TBD which reads better against this file's own
  precedent — `_do_merge`'s equivalent check is `ValidationFailed`), naming
  both the destination and the predecessor's actual last known position, the
  same way `_do_merge`'s "does not match the (unit, vial) of any ending
  parent" message already does.
- **Unknown** (`None`): decide explicitly whether to refuse or warn-and-allow
  — see above. Whichever is chosen, document it in `RestartRequest`'s own
  docstring (currently promises unconditional enforcement, which won't be
  quite true if `None` is allowed through) and in `GET /skill`.
- Update `tests/test_write_lines.py`'s restart scenarios with both a
  mismatch case and (once decided) the unknown-position case.

Not urgent enough to block anything currently shipping — filed so it doesn't
get lost, and so the reasoning above (the harder, undecided part) survives
to whoever does pick it up.
