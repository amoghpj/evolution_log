# OR05 log server — design decisions

Status: **decided, not yet built.** Recorded 2026-08-27 (AJ + Claude).
Companion to `LOG_PROTOCOL.md`, which describes the log itself. This file
describes the machinery that will maintain it once more than one operator and
more than one assistant can write.

---

## 1. The change

Today: one operator dictates, one assistant reads `evolution_log.json`, decides
where content belongs, and writes it back. The schema lives in that assistant's
context window.

Target: **the log has a contract, not a convention.** Any operator (AJ, MS)
using any LLM appends to it over HTTP. The schema is enforced at the server
boundary, so a confused model produces a rejected request rather than a
corrupted log.

The authority over structure moves out of the model and into the server. That
is the whole point, and every decision below follows from it.

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **FastAPI**, serving both the API and `viewer.html` | Pydantic models are the schema: validation, the OpenAPI spec and the LLM-facing skill text all derive from one definition, so they cannot drift |
| 2 | Host reachable over **Tailscale**, not the public internet | No public exposure. Host must be kept awake; prefer a lab workstation over a laptop |
| 3 | **Per-operator bearer token** | For *attribution*, not perimeter defence — the tailnet is the perimeter. The token determines `operator` and the git commit author |
| 4 | **Hybrid write model** (see §3) | Append-only where history lives; recomputed where copies live; untouched where prose lives |
| 5 | Corrections are **new events carrying `supersedes`** | Requirement: never modify prior content |
| 6 | **One git commit per API call**, author = operator, message from the event | git is the enforcement mechanism, not just a backup: `git log -p` proves each append touched no history |

---

## 3. The hybrid write model

Three classes of content, three different rules. This is the core of the design.

### A. History — append-only, never touched

`lines[*].events[]` and `experiment_events[]`.

Events are never modified, never reordered, never deleted. The server appends
and does nothing else. A request that would alter an existing event is rejected
with 409, not accommodated.

### B. Projections — recomputed by the server after every append

Verified reconstructable from the event stream (checked against the live log,
2026-08-27):

| Field | Derivation |
|---|---|
| `reservoirs[].current_volume`, `level_as_of`, `level_set_by_event` | Newest of `level_reading.volume_remaining` / `media_prep.volume_prepared` — matched all 10 reservoirs exactly |
| `reservoirs[].lines_fed` | Active lines referencing that reservoir — reproduced all 10 exactly |
| `line.status`, `lineage.terminated_by_event`, `lineage.terminated_at` | Termination events — matched all 14 ended lines |
| `hardware.units.*.vials_in_use`, `vials_empty`, `n_lines` | Active lines' vials |
| `lineage.children`, `roots`, `depth`, `lineage_summary` | Already `tools/lineage.py` |
| `log_meta.event_counter`, `next_event_id`, `last_updated` | Trivially derived |

**These stop being hand-maintained.** Motivating evidence: at the time of the
audit `hardware.units.patrick.n_lines` read **6** while `vials_in_use` listed
**8** vials and 8 patrick lines were active — and the unit's own note said "All
six positions are now occupied" in the same paragraph that described bringing
vials 4 and 6 back into use. Both `validate_schema.py` and `lineage.py` passed
the file. A hand-maintained counter went stale within a day, silently.

Also to be fixed by recomputation: `level_set_by_event` is `null` on 8 of 10
reservoirs despite the value provably originating in a `media_prep` event.

### C. Prose — the server refuses to write

`hardware.units.*.note`, `hardware.note`, `design.layout`,
`design.mode_unit_confound`, `conventions`, `reservoirs.*_caveat`, and
`design.total_lines`.

Nothing in the event stream determines these. `design.total_lines` is 16 — the
original 2x2x2x2 design — while 30 line records exist; it is a **design
constant, not a counter**, and `LOG_PROTOCOL.md` §10 step 6 is misleading in
listing it as denormalised state to update.

The API has **no route that can write these**. They are edited by hand and
committed by hand. Consequence, accepted: MS cannot update the narrative
through an LLM.

---

## 4. Corrections

New corrections are appended as an event carrying `supersedes: EVT-NNNNN` plus a
reason. Nothing prior is edited.

**Two idioms will coexist.** The 11 existing events with provenance
`reported (corrected)` keep their in-place shape — `corrected_from`,
`corrected_at`, amended values, narration in `notes`. Migrating them would
mean rewriting history to record that history must not be rewritten.

The skill must therefore say, explicitly: **read both idioms, write only
`supersedes`.** The `( \(corrected\))?` suffix in the `provenance` pattern stays
legal for reading and is forbidden on new writes.

Recommendation pending: declare `supersedes` at **event level**, typed as
`eventId`, alongside `caused_by_event` — rather than as a `params` key like the
existing `superseded_by_event`. Event-level means the schema validates it;
`params` means only prose in the registry describes it.

---

## 5. Out of scope

- `media_switch_count`, `pg_step_count` — 0 on all 30 lines, never maintained.
  Left as they are for now. They remain misleading: a reader may conclude no
  line ever switched media.
- A web form fallback for chat-only LLMs. Worth adding later so no operator is
  blocked by their client lacking tool access.

---

## 6. Prerequisite work, in dependency order

### Phase 0 — make validation trustworthy (do first)

Everything downstream trusts these tools, and one of them is currently lying.

1. **`tools/validate_schema.py` must fail loudly.** On the bench machine it
   printed `jsonschema is installed but too old for draft 2020-12 -- ignoring
   it`, silently ran "the built-in subset", and still printed `schema OK`. The
   real schema had never been run against the log until 2026-08-27 (it passes:
   0 errors under draft 2020-12). A validator that degrades while reporting
   success must not be in a pipeline handed to a second operator.
2. **Add a g/L <-> mM consistency check to `tools/lineage.py`.** 465
   concentration objects, all currently correct at MW 126.11, none enforced.
   The pair is hand-entered twice. Per `LOG_PROTOCOL.md` §11 the dangerous
   failure is a confident number.

### Phase 1 — stabilise the schema (blocks the skill)

The skill cannot be generated from a contract this loose.

3. **Close the nested objects.** `additionalProperties: false` currently appears
   on only four things: the root, `concentration`, `quantity`, and
   `line.reservoirs`. `event`, `line`, `lineage`, `pgRegime`, `logMeta`,
   `registryEntry`, `reservoirItem`, `rampInterval`, `fillRecord`,
   `lineageSummary` and `hardware.units.*` are all open — `notse` inside an
   event validates clean. The live log has zero such keys, so this is latent
   today and certain once an unfamiliar model writes.
   **Resolved 2026-08-27:** all eleven closed. Closing surfaced two places the
   schema had already drifted from the live log (both from EVT-00258): `line`
   was missing `replicate_independence_set_by` / `_superseded`, `logMeta` was
   missing `schema_file` / `validation`. Declared both before closing.
   `test_schema.py` gained one rejection test per closed object.
4. **Type the `parameter_registry`.** Entries carry prose `description` and
   `status` but no type or enum, so nothing can reject `od_reliable: "no"` or
   `anomaly_type: "flood"`. Adding a type/enum per key is what lets one
   definition generate both the validator and the skill text.
   **Resolved 2026-08-27:** all 120 entries typed, audited against every
   value actually observed for that key across the log (not guessed from the
   name). Vocabulary: the six JSON primitives plus four of the log's own
   shapes -- `quantity`, `concentration`, `eventId`, `lineId`, `reservoirId`
   -- and `any` for the one deliberately unconstrained field
   (`corrected_from`, which mirrors whatever field it corrects). Two fields
   (`reservoir_id_from`/`_to`) are legitimately either a single id or an array
   of them, so `type` may be a 2-element array.
   `tools/lineage.py:check_param_types` enforces every non-null value against
   its registry entry; verified by hand that it actually catches a mismatch
   (`od_reliable: "no"`, `pg_high` as a bare number, `colony_count` as a
   string) and an enum violation (`role: "medium"`), and that the real log
   currently passes with zero violations.
   **`enum` deliberately did NOT go the way this item's own example implies.**
   Only 4 of 120 keys got one (`role`, `measurement_qualifier`, `level_source`,
   `pg_exposure_error`) -- all reusing a vocabulary this schema already commits
   to elsewhere for the identical concept. `anomaly_type` stayed a bare
   `string`: it plays the same "open, growing vocabulary, extended by using a
   new value" role that `event_type` already deliberately plays (§9), so
   `anomaly_type: "flood"` is accepted here, not rejected -- the same way a
   new `event_type` is accepted rather than requiring a schema change. If that
   tradeoff is wrong, the fix is adding `enum` to that one registry entry, not
   redoing this item.
5. **Resolve `pg_reliable` vs `pg_history_reliable`.** §7 names the first; the
   schema declares the second as a typed boolean on `pg_regime`, with nearly
   the same description. Both are in the log (12 and 9 uses). A model reading
   §7 writes the params key and never updates the typed field a consumer reads.
   **Resolved 2026-08-27 (AJ):** keep `pg_reliable`, since that is what §7 and
   the params key both already used. The schema's `pgRegime.pg_history_reliable`
   is renamed to `pg_reliable`; the 9 lines using it are renamed in place (not
   an event-history edit — this is denormalised line state, not `events[]`).
   The name is now shared between the per-event params key (a finding on one
   anomaly) and the line-level field (the current aggregate state) — same
   meaning, two scopes, deliberately not disambiguated further.
6. **Define `missing_fields` precisely.** §2 says "unknown -> null, and name
   the field in `missing_fields`". In practice 221 entries name fields that were
   never params keys, and 57 params keys are null without being named. Two
   defensible readings, neither enforced — the likeliest place for two LLMs to
   invent two conventions.
   **Resolved 2026-08-27 (AJ):** both directions, not one. `missing_fields` may
   name any fact known absent whether or not it was ever a `params` key (the
   log already does this 100+ times and it's correct as written); separately,
   every `params`/`pg_regime` value sitting at `null` must be named. Written up
   in LOG_PROTOCOL.md ("missing_fields, precisely"). The 57 existing exceptions
   predate the rule and are not editable in place, so `tools/lineage.py`
   grandfathers them by exact `(event_id, key)` — new violations still fail.
7. **Canonicalise timestamps.** The pattern rejects `Z`, which satisfies §2 as
   written and is what every LLM emits by default. `[+-]\d{2}:?\d{2}` also
   admits both `-0400` and `-04:00`, so one instant has two spellings that
   compare unequal as strings. Decide, then enforce one.
   **Resolved 2026-08-27 (AJ):** keep rejecting `Z`; require the colon.
   `Z` asserts UTC, not which local timezone the operator meant, and this is
   exactly the ambiguity LOG_PROTOCOL.md §2 says has already caused
   misreadings — permitting it would undo the rule, not just relax a format.
   The pattern now requires `[+-]\d{2}:\d{2}`; the live log had zero
   colonless offsets, so no data needed migrating. The server-facing skill
   text (Phase 3) must tell every LLM client explicitly to emit local offset,
   never `Z`, since that is not what such clients do by default.
8. **Declare the correction and restart fields.** `supersedes`,
   `corrected_from`, `corrected_at`, `superseded_by_event`,
   `replaced_by_line_ids` are all undeclared params keys. Note §5 names
   `replaced_by_line_ids` in the same sentence as `lineage.occupies_vial_of`,
   which *is* declared on `lineage` — one sentence, two fields, two levels.
   **Resolved 2026-08-27 (AJ), partially:** `supersedes` promoted to a
   schema-level, typed (`eventId`) event field, per this section's own earlier
   recommendation — the schema can now validate it, not just describe it in
   prose. EVT-00258 (already committed, before this field existed) keeps
   `params.supersedes`/`params.superseded_reason` untouched; only new events
   use the event-level field. `corrected_from`, `corrected_at`,
   `superseded_by_event`, `replaced_by_line_ids` turned out to already be
   registered in `parameter_registry` (this item's "undeclared" premise was
   stale) — they remain params-level, not promoted to schema fields, and stay
   untyped until item 4 types the registry. The `replaced_by_line_ids` /
   `occupies_vial_of` asymmetry this item flagged is unresolved; that would be
   a separate decision, not implied by this one.

### Phase 2 — documentation gaps that mislead a foreign model

9. `provenance` permits `derived`; the schema's own description explains only
   `reported`/`document`/`instrument`, and the log never uses it.
   **Resolved 2026-08-27:** schema description now defines `derived` --
   computed by a tool with no new outside information entering at all,
   distinct from an operator reporting the output of a calculation (which
   stays `reported`, since the operator's judgment is itself new information).
   Neither `derived` nor `instrument` has been used by any event yet; both are
   now defined for when they are.
10. `pg_error_direction` has 6 enum values; §7 lists 4 and trails off with "...".
    A skill cannot be generated from an ellipsis.
    **Resolved 2026-08-27:** LOG_PROTOCOL.md §7 now lists all six, no
    ellipsis; the schema field gained a description defining each one.
11. `controller` is the only optional top-level block and §3's table does not
    say so. `experiment` and `design` are bare `{"type": "object"}` — wholly
    unvalidated.
    **Resolved 2026-08-27:** §3's table now marks `controller` `(optional)`
    and `experiment`/`design` `(*)` with a note explaining the asterisk;
    the schema's own descriptions for all three say the same thing, so a
    reader of either document gets it.
12. `timestamp_precision`: §3 says hour/day, the enum adds `minute`, the log
    uses only `hour` (52x).
    **Resolved 2026-08-27:** §3 now names all three legal values and explains
    why `minute` is legal but has never been written (it's the default).
13. `pgRegime.ceiling` is declared and never mentioned in §6, which otherwise
    carefully separates the three PG quantities.
    **Resolved 2026-08-27 (AJ):** `ceiling` is not a fourth quantity -- it is
    the same fact as `high`, whatever the high reservoir currently holds.
    Verified before asking: on all 37 lines, `high`, `ceiling`, and
    `line.pg_high_max_reached` were already numerically identical (5.0 g/L
    everywhere), so nothing in the data contradicted this. Documented in
    LOG_PROTOCOL.md §6; `tools/lineage.py:check_ceiling_matches_high` now
    enforces `ceiling == high` wherever both are present.
    `pg_high_max_reached` stays a separate, optional, operator-noted field --
    not derived from the other two -- for the (so far unexercised) case of a
    line that has actually drawn from high reservoirs of different strengths
    over its life.
14. `quantity.value` accepts `type: string`, which is the hole in §2's "a bound
    stays a bound" — one instance exists: `{"value": "2-3", ...}`.
    `level_qualifier` exists only on `reservoirItem`, so no other quantity has
    a legitimate place to put a bound.
    **Resolved 2026-08-27 (AJ):** added `quantityRange` (`{min, max, unit}`,
    both ends required), removed `string` from `quantity.value`'s allowed
    types, and migrated the one real instance --
    `reservoirs.policy.replacement_interval` -- to `{"min": 2, "max": 3,
    "unit": "days"}`. Not an event-history edit: `policy` is denormalised
    state, not `events[]`. `reservoirs.policy.replacement_interval` is now
    typed in the schema as `oneOf: [quantity, quantityRange]` (a future
    fixed-interval policy doesn't need to fake a range), everything else in
    `policy` stays free-form. Confirmed by full-document scan this was the
    only string-valued quantity anywhere in the log before removing the type.
15. **`event_id` order is not chronological** — 10 of 256 adjacent pairs invert,
    because corrections were appended later with earlier timestamps
    (EVT-00135 predates EVT-00134 by four days). Correct behaviour for an
    append log; a server assigning ids monotonically will keep producing them.
    Any consumer sorting by id gets a wrong timeline. The skill must say so.
    **Resolved 2026-08-27:** stated explicitly in LOG_PROTOCOL.md §3 now, not
    just in CLAUDE.md's quick-reference list -- LOG_PROTOCOL is what a skill
    would actually be generated from. Count updated to 10 of 281 (the log grew
    26 events since this item was written; the 10 inversions themselves are
    unchanged, meaning nothing written since introduced a new one).
16. Line-id regex is `(#\d+)?(\.[a-z])?`, so a split of a second occupancy must
    be `patrick-v05#2.a`, never `.a#2`; `(\+v?\d{2}...)` makes the `v` optional,
    so `patrick-v09+10` also validates; no cross-unit merge id is expressible.
    §5's table implies none of this.
    **Resolved 2026-08-27 (AJ):** fixed both bugs in the regex. `v` is now
    mandatory in every merge addend (`patrick-v09+10` no longer validates);
    an addend may optionally carry its own `<unit>-` prefix, so a cross-unit
    merge is now expressible as `patrick-v09+plankton-v10`. Verified against
    all 37 real line ids before applying: the new pattern accepts every one
    of them unchanged. Documented the full composability grammar (occupancy
    before split, mandatory `v`, optional cross-unit prefix, and that
    cross-unit *branches* need no special id at all) in LOG_PROTOCOL.md §4.
    Four test_schema.py regressions added.

### Phase 3 — the server

`GET` read routes, `POST /events` (validate -> append -> recompute projections
-> git commit), `GET /openapi.json`, `GET /skill` generating the operator
instructions from the Pydantic models. Viewer served from the same origin,
which also removes the `file://` fetch problem `serve.sh` exists to solve.

---

## 7. Standing constraints

- The host must not sleep, or MS gets connection refused mid-transfer.
- MS needs Tailscale installed and the tailnet hostname in the skill.
- A chat-only LLM cannot issue a POST. Until the form fallback exists, each
  operator's client needs tool or code-execution access.
- `LOG_PROTOCOL.md` §2's "ask before writing a critical field you are unsure
  of" assumes a human in the loop. With autonomous multi-operator writes, that
  rule has to become a server-side rejection: unknown -> `null` plus
  `missing_fields`, never a guess.
- **Server code lives in its own repo, `evolution_log_server/`, sibling to
  this one** (settled 2026-08-27, AJ). It has an ordinary software-dev commit
  rhythm -- refactors, dependency bumps, bug fixes -- that would pollute this
  repo's history if mixed in, defeating decision #6's guarantee that
  `git log -p` here proves each append touched no history. This repo is added
  to the server repo's ignore list from the server side, and this repo's own
  `.gitignore` excludes `evolution_log_server/` so neither repo sees the
  other's files. The server operates on a clone/checkout of this repo purely
  via `git` subprocess calls for the "validate -> append -> recompute ->
  commit" step (§3, §6 Phase 3) -- it is a *consumer* of this repo's schema
  and tools, never the place either gets edited.
  **Reversed 2026-10-02 (AJ):** server and log now share one repo,
  `evolver-log`, so a new experiment is one clone rather than two repos
  kept in step. The guarantee above is kept by other means: the server's
  commit is path-scoped to `evolution_log.json`, and log and code changes
  never share a commit, so `git log -p -- evolution_log.json` still proves
  each append touched no history. See CLAUDE.md, "One repo".
