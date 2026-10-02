# OR05 evolution log — protocol

How to maintain `evolution_log.json`. Written so an assistant picking this up
cold can keep logging in the same way. Read this before writing to the log.

---

## 1. What the log is for

A modified-morbidostat laboratory evolution: *E. coli* lineages under a rising
phloroglucinol (PG) ramp across two eVOLVER units.

`evolution_log.json` is the **provenance record**. Its job is to let someone
reconstruct, months later, *what happened to each culture and what was known at
the time each decision was taken*. Not "what is the state now" — the rig knows
that — but how the state came to be, including the parts that went wrong.

Three consequences follow, and most of the rules below are downstream of them:

- **It records actions and beliefs, not telemetry.** Per-dilution OD and pump
  data live on the eVOLVER. The log holds decisions, interventions, faults,
  manual readings, and the reasoning attached to them.
- **It must be able to say "I don't know."** A guessed value is worse than a
  gap, because a gap can be filled and a guess cannot be detected.
- **It must survive being wrong.** Diagnoses get overturned. Corrections are
  applied in place *and* left visible.

---

## 2. Non-negotiables

| Rule | Why |
|---|---|
| Never invent a value. Unknown → `null`, and name the field in `missing_fields`. | A confident wrong number is the failure mode that costs real experiments. |
| Ask before writing a critical field you are unsure of: timestamp, line identity, concentration, which unit. | These are the fields that silently corrupt everything downstream. |
| Timestamps are ISO 8601 **with explicit UTC offset**. | Several entries here were only interpretable because the offset was present. |
| A bound stays a bound. `>750 mL` is `at_least`, never a point value. | Bounds propagate: an upper bound on volume is a lower bound on time remaining. |
| Corrections are amended in place with `corrected_from`, `corrected_at`, provenance `reported (corrected)`, and a note saying what the superseded version claimed. | A log that quietly rewrites itself cannot show what was known when a decision was made. |
| Derived fields are never hand-edited. Run the tool. | `lineage.children/roots/depth`, `lineage_summary`, `event_counter`. |
| One git commit per logged action, message explaining *why*, not just what. | The commit history is a second, narrative copy of the record. |

---

## 3. Structure

```
schema_version        shape of this file (semver)
log_meta              counters, last_updated, which tools validate it
experiment            identity, operators, selective agent          (*)
design                factors, cells, ramp definition, imbalance caveats (*)
hardware              per-unit vial state, faults, body history
reservoirs            items[] (positions), policy, PG stability caveat
controller            the eVOLVER script's logic and parameters     (optional)
conventions           prose rules — every one was learned the hard way
event_types           every event_type in use must be declared here
parameter_registry    every params key in use must be registered here
lineage_summary       derived: founders, nodes, edges, depth
lines                 keyed by line_id; each holds its own events[]
experiment_events     facility-scope events, attributable to no single line
```

`controller` is the only top-level block the schema does not require;
everything else in this table must be present. `experiment` and `design`,
marked `(*)`, are `{"type": "object"}` in the schema -- present is required,
their insides are wholly unvalidated. That's deliberate: they're closer to
`conventions` than to `hardware`, mostly narrative, read by people rather
than checked by tools -- but a foreign model should know the schema will not
catch a typo inside either one the way it would inside `hardware` or
`reservoirs` (SERVER_DESIGN.md Phase 2 #11).

### Event shape

```json
{ "event_id": "EVT-00123", "timestamp": "2026-08-26T16:30:00-04:00",
  "event_type": "inoculation", "operator": "AJ", "provenance": "reported",
  "params": { "...open, every key registered..." },
  "notes": "prose: what happened, why, and what it implies",
  "missing_fields": ["culture_volume"] }
```

Optional: `timestamp_precision` -- three legal values, `minute`/`hour`/`day`,
not just the two you'll actually write. Absent means `minute`, since a
logged timestamp is precise to the minute by default; write `hour` or `day`
when it's coarser than it looks (the only two seen in the log so far, 52x).
Writing `minute` explicitly is legal but has never been needed -- it would
only matter to assert precision against an inherited assumption otherwise
(SERVER_DESIGN.md Phase 2 #12: this used to say only "hour/day", so a foreign
model reading just this line would not know `minute` was a legal value at
all). Also optional:
`elapsed_h`, `scope: "facility"`, `caused_by_event`, `supersedes`, `source_document`.

**`event_id` order is not chronological (SERVER_DESIGN.md Phase 2 #15).**
Corrections are appended later carrying the timestamp of what they correct,
so ids climb monotonically while timestamps don't: as of 2026-08-27, 10 of
the 281 adjacent id pairs in the log invert (EVT-00135 predates EVT-00134 by
four days, because EVT-00135 corrects an earlier reading). That count is
descriptive of today's log, not an invariant -- correct behaviour for an
append log, but any consumer sorting by `event_id` instead of `timestamp`
gets a wrong timeline. Sort by timestamp, then `event_id` as a tiebreaker,
never by `event_id` alone. A future server assigning ids monotonically at
write time will keep producing this same pattern, not fix it.

**`params` is deliberately open.** New experimental dimensions are absorbed by
adding a registry entry, never by changing the schema. That is the mechanism
that has let this log take on ramp history, reservoir policy, generations and
correction fields without a migration.

**`missing_fields`, precisely (settled 2026-08-27, resolving SERVER_DESIGN.md
Phase 1 #6).** Two rules, not one:

1. It may name **any fact known to be absent**, whether or not that fact has
   ever been given a `params` key. `outgrowth_od` above is legitimate even
   though no event's `params` has ever had an `outgrowth_od` key — nothing
   was measured, so there is no value to hold, only the fact that it wasn't
   taken. Do not invent a registry entry just to have somewhere to name it.
2. Every `params` key present with value `null` **must** be named here. A
   `null` with no corresponding name in `missing_fields` is indistinguishable
   from "checked, and it's genuinely null" — the gap disappears instead of
   being recorded as one.

Both readings looked defensible in isolation; in practice the log already
uses (1) over 100 times and never leans on the opposite (stricter) reading,
so (1) is confirmed as written rather than changed. (2) had 57 exceptions —
`pg_target` (EVT-00001–00031), `stock_id`/`storage_location`
(EVT-00147–00159), `colony_count` (EVT-00174–00180), one `lines_affected`
(EVT-00188), one `input_pump2` (EVT-00221) — all predating this rule. They are
**not corrected**: fixing them would mean editing existing events, which the
one rule that outranks the others forbids. `tools/lineage.py` grandfathers
those 57 exactly by `(event_id, key)` and enforces the rule on everything
else, including everything written after 2026-08-27.

---

## 4. Identity and naming

**Line identity follows the culture, not the hardware.** Vials can be moved
between eVOLVER bodies and the lines continue unbroken with no lineage edge.
What does *not* follow the culture: calibration, known faults, control
addresses — those belong to the machine.

| Form | Meaning |
|---|---|
| `patrick-v09` | base: `<unit>-v<NN>` |
| `patrick-v05#2` | second culture to occupy that vial, no descent from the first |
| `patrick-v04.a` | split child |
| `plankton-v09+v10` | merge child, joins its parents |
| `patrick/M9-1` | reservoir: `<unit>/<media>-<pg g/L>` — a **position**, not a bottle |

**Composing suffixes (settled 2026-08-27, resolving SERVER_DESIGN.md Phase 2
#16).** Occupancy comes before split: a split of a second occupancy is
`patrick-v05#2.a`, never `.a#2`. A merge addend always carries `v`
(`patrick-v09+v10`, never `patrick-v09+10` — the latter is not a valid id).
An addend may optionally carry its own `<unit>-` prefix for a **cross-unit
merge** (spiking a culture from one unit's vial into a vial on the other),
e.g. `patrick-v09+plankton-v10`; same-unit addends omit it, as every merge in
this log has so far. Cross-unit *branches* (a new line founded on a
different unit from its parent, e.g. `patrick-v09` → `plankton-v13`) already
happen routinely and need no special id — the child just gets an ordinary id
on its own unit, and the cross-unit fact lives entirely in `lineage.parents`.

Reservoir ids name hardware positions. A bottle can be refilled in place or
carried to another unit; the id stays, and `fill_history` / `bottle_origin`
record what actually moved. A change of *composition* produces a new id, and
`media_prep.replaces` chains the position's history across the rename.

---

## 5. The four ways a line can begin

Getting this wrong is the most consequential mistake available. The test is
**what happened to the parent**, and **whether the destination was empty**.

| | Destination | Parent | Parents recorded |
|---|---|---|---|
| **Branch** | empty | continues | 1 |
| **Split** | empty | ends | 1, on ≥2 children |
| **Merge** | held a standing population | either | ≥2 |
| **Restart** | empty | n/a — seeded from ancestor stock | 0 (founder) |

A restart is *not* descent: record hardware continuity in
`lineage.occupies_vial_of` and the predecessor's `replaced_by_line_ids`, which
deliberately do **not** create a lineage edge.

Any child seeded from a running culture gets `replicate_independence` spelled
out on the line, and the field it must carry is the **divergence time**.
Vessels separated from a common donor — a child branched off a continuing
parent, or two vessels filled from the same 1 mL at the same instant — are
**independent replicates from that moment**. They are identical at the instant
of separation and at no time after it; what is being replicated is the
evolutionary process, not the starting genotype. Do not call them clones.

The divergence time bounds what the replication covers. After it, independent;
before it, shared — so a variant found in both members of a pair may be one
pre-branch event rather than parallel evolution. That constrains reading
convergence, not whether they count as replicates.

Merge children are the exception: they physically contain material from the
line they would be compared against, so ancestry is mixed rather than shared
from a clean point and no divergence time describes them.

**Shared events:** a split is recorded once on the parent it terminates; a
merge once on the child it creates. Every other line involved gets its own
`termination` event referencing it. Nothing is duplicated, and anything
counting events must key on `event_id` regardless.

---

## 6. Three different PG quantities

Keep these apart. Conflating them has caused real errors here.

1. **The regime** — `pg_regime.low` / `.high`: the reservoir concentrations a
   line is dosed between. Changes only when media changes. Recorded as events.
2. **The vial concentration** — controller state in `vialN_drugconc.txt`,
   moving on every dilution. Never transcribed by hand. When it is known to
   diverge from reality, record both: `vial_concentration` (the setpoint that
   drives dosing) and `vial_concentration_estimated_actual` (inferred, with its
   derivation and a confidence). Neither substitutes for the other.
3. **The delivered dose** — mean concentration of media actually pumped,
   derived by `tools/media.py` from the ratio of high to low volume drawn. A
   floor on selective pressure, and an **upper bound** given PG degrades in the
   bottle.

**`pg_regime.ceiling`, precisely (settled 2026-08-27, resolving SERVER_DESIGN.md
Phase 2 #13, operator's ruling).** `ceiling` is not a fourth quantity and not
a separate design constant — it is the same fact as `high`, whatever the high
reservoir is currently noted as having. It exists as its own field only for
contexts that are talking about the safety bound specifically; `tools/lineage
.py` enforces that the two never diverge. Separately, `line.pg_high_max_reached`
is optional and operator-noted, not derived from `ceiling`/`high` — it only
needs setting once a line has actually drawn from high reservoirs of
different strengths over its life, which has not happened to any line yet
(today it just equals `high` on every line, because nothing has forced it to
differ).

A raised low reservoir is not subject to the growth gate: pure low media
carries PG, so any vial below the new floor is carried up within a few
dilutions whatever its growth is doing. Say so when logging such a change.

---

## 7. Faults

Every dosing fault seen so far has been silent — nothing crashed, numbers just
became wrong. When logging one, record: `anomaly_type`, `root_cause` (or null),
the window (`resolved_at`, or `duration`, else the line's termination), whether
`od_reliable` and `pg_reliable` hold, and the **direction** of any exposure
error -- all six legal values, no more: `received_none` (no dose at all),
`received_double` (dosed twice), `received_partial` (an incomplete dose),
`received_excess` / `received_deficit` (recorded history reads more/less
concentrated than actual), `exchanged_with_partner` (swapped with a specific
other vial's dose, named in `params.partner_vial`). See the schema's
`pgRegime.pg_error_direction` description for what each means (settled
2026-08-27, SERVER_DESIGN.md Phase 2 #10 -- this list used to trail off after
four with "…", which a skill can't be generated from).

Anomaly windows are not cosmetic: `viewer.html` and `tools/carry_generations.py`
read them to exclude untrustworthy dispenses from generation counts. A
wrongly-scoped anomaly silently deletes real data.

If a later finding overturns a diagnosis, **retract in place**: correct
`root_cause`, set `superseded_by_event`, keep the original text. If an anomaly
turns out not to apply to a line at all, downgrade that event to a `note`
carrying the withdrawal — do not delete it, and do not leave a false anomaly.

---

## 8. Tools

Run from the repo root. All read `evolution_log.json`; only `lineage.py --write`
and `carry_generations.py --write` modify it.

| Tool | Function |
|---|---|
| `tools/lineage.py [--write]` | Recomputes derived lineage fields and validates cross-field integrity: registered params, declared event types, acyclic lineage, event counter, ordering. **Run after every edit.** |
| `tools/validate_schema.py` | Validates shape against `schema/`. Prints what it did *not* check. |
| `tools/media.py [--at ISO] [--json]` | Consumption rates and depletion forecasts from bottle readings only. Never uses live data. |
| `tools/ramp_model.py [low high ramp vial]` | Characterises the controller ramp by importing the eVOLVER's own functions, so it cannot drift from deployed logic. |
| `tools/evolver_api.py` | Registered by `dashboard.py` (no longer a hand-applied edit on each rig); a read-only JSON API over that rig's files. Serves a per-vial summary, per-event dispenses, and `/api/v1/consumption` — volume dispensed since one given instant. Not raw logs. |
| `tools/test_evolver_api.py` | Regression suite for that API, including the two flags that matter more than the volumes: `clock_ok` (the rig restarted) and `covers_window` (the log opens after the instant asked about). |
| `tools/check_api.py` | Verifies an endpoint is *correct*, not merely reachable: identity, CORS, roster, last-write clock, `clock_problem`, and `/api/v1/consumption` — including a cross-check that consumption since zero agrees with the summary's own `cumulative_mL`, two independent paths over the same pump log. |
| `tools/carry_generations.py` | Reads a finished experiment directory and records `generations_carried_forward`, which a controller restart would otherwise lose. |
| `tools/test_media.py`, `tools/test_schema.py` | Regression suites for the media model and the schema. |
| `node tools/test_viewer.js`, `node tools/test_generations.js` | Viewer suites: they extract the pure derivation logic out of `viewer.html` and run it, so a broken viewer fails here rather than in the browser. Run `tools/make_fixture.py` first — it builds a synthetic branched log in `/tmp` that the viewer tests exercise splits and merges against. |
| `viewer.html` + `serve.sh` | Timeline and pedigree viewer. Needs the http server; `fetch()` is blocked on `file://`. |

### Media model, in one paragraph

Rates come only from bottle readings. Every refill, bottle swap, or reading
marked unrepresentative **resets the measurement baseline**, so a rate is never
computed across one. A freshly refilled reservoir falls back to *its own
previous bottle* before any cross-reservoir average — a rate borrowed from a
unit at a different point on the ramp is badly wrong, because high-media demand
scales with vial concentration. Rates measured over <2 h are `provisional` and
excluded from the shared pool. Retired reservoirs never seed it.

`media.py` itself still never touches live data, and the CLI above is
unchanged. The evolution log server's `GET /media` adds a second, separately
labelled measurement beside it: what a rig actually dispensed since each
bottle's last reading, fetched from `/api/v1/consumption` and integrated in
`evolution_log_server/app/pump_rates.py`. The two are never averaged or
substituted for one another — one is a bottle someone looked at, the other is
the pumps' own record of the interval since — and where they disagree by more
than rounding, that disagreement is the finding. See that repo's
`issues/ISSUE_004.md`.

---

## 9. The schema

`schema/evolution_log.schema.json`, draft 2020-12, servable as a static file.

It validates **shape only** — required fields, value types, id patterns,
timestamp offsets, enums, and the founder/parents and terminated/active
contradictions. It closes the top level (catching typos) and leaves `params`
open (preserving extensibility). `tools/test_schema.py` asserts it actually
*rejects* things, including that it still accepts an unanticipated params key.

It **cannot** check what this log most depends on, because those are
relationships between fields: params keys registered, event types declared,
lineage acyclic, a line's floor matching its reservoir, the event counter
matching distinct ids. Those live in `lineage.py`. **A file that passes the
schema is well formed, not correct.**

`schema_version` describes the file's shape, not the experiment. New event
types and params keys do not bump it.

---

## 10. Procedure for a log update

1. **Parse the entry.** Extract timestamp, unit, vial(s), volumes,
   concentrations, action type.
2. **Sanity-check against the log before writing.** A level that rose without a
   refill is impossible; a vial that changed identity needs its occupancy
   number; a reported reservoir may have been renamed. Two dictation errors in
   this log were caught this way — by arithmetic, not by eye.
3. **Ask only about genuinely ambiguous critical fields.** Offer the reading
   the data supports, and say why.
4. **Write the events.** Line-scoped events into `lines[id].events`;
   facility-scope into `experiment_events` with `scope: "facility"` and a
   per-line event carrying `caused_by_event` where it reaches individual lines.
5. **Register any new params key** in `parameter_registry` with a real
   description; add any new `event_type` to `event_types`.
6. **Update denormalised state**: `pg_regime`, `reservoirs[]`, `lines_fed`,
   `hardware`, `design.total_lines`.
7. **Run** `tools/lineage.py --write`, then `validate_schema.py`, then the test
   suites. Fix what fails. If a test fails because the experiment moved rather
   than because something broke, **fix the test to assert the invariant**, not
   the old value.
8. **Commit**, one action per commit, with the reasoning in the message.
9. **Reply briefly.** "Log confirmed" unless something is urgent or wrong —
   then that one thing only.

---

## 10a. For human users only

**Everything in this section is off-limits to an LLM assistant.** It requires
reading directories on the eVOLVER machine that no assistant can see, and
judging whether a run directory is the right one — a judgement that cannot be
made from inside this repo. An assistant asked to do this should say so and
stop, not improvise from what the log contains.

### Reconciling generations after a controller run restart

Starting a fresh experiment on a unit resets the controller clock and starts
empty `pump_log`/`drugconc` files, so the live API can only ever count the
**current** run. Everything earlier stays in the old experiment directory on
disk. Without reconciliation, every cumulative generation count silently
restarts at zero — after the 26 Aug restart (`EVT-00232`) the affected lines
were showing about 5 generations against a true figure nearer 78, i.e. over
90% missing, and nothing anywhere reported an error.

**Run `tools/carry_generations.py` every time you restart a run, once per
unit, on the machine holding the experiment directories.** Check first:

```
python3 tools/carry_generations.py \
    --exp-dir /path/to/or05_phase2_run1 \
    --unit patrick \
    --pump-cal /path/to/pump_cal.json \
    --run-start 2026-08-22T18:00:00-04:00
```

That prints per-line totals and writes nothing. Read them before continuing:
a plausible figure is tens of generations per line per few days. If a line
comes back at 0, or in the hundreds, the `--exp-dir` or the pump calibration
is wrong — not the arithmetic. Then repeat with `--write`, and again for the
other unit:

```
python3 tools/carry_generations.py --exp-dir ... --unit patrick  ... --write
python3 tools/carry_generations.py --exp-dir ... --unit plankton ... --write
```

Then, as after any edit:

```
python3 tools/lineage.py --write
python3 tools/validate_schema.py
git commit -m "Carry generations from <run dir> into <unit>"
```

**Name the run directory in the commit message.** It is the only durable
record of which runs have been folded in — see the warning below.

Three things to get right:

- **`--run-start` is not optional in practice.** It is the wall clock of that
  run's t=0. Without it the tool says so and skips anomaly-window and
  occupancy clipping, which means dosing the log has already disowned gets
  counted back in, and a vial's whole history is credited to whichever line
  happens to occupy it now.
- **Never run it twice against the same `--exp-dir`.** The value is additive
  (`previous + new`) so that successive restarts accumulate, which is correct
  — but a repeat of the same directory silently doubles that line's count and
  looks identical to a correct result. `from_run` records only the most recent
  directory, so the log cannot tell you what has already been folded in.
- **Preserve the old run directories before migrating anything.** The
  computation is deterministic and replayable from those files indefinitely,
  so this is never urgent — but if a directory is deleted or reused its
  generations are gone for good, and no later care recovers them.

---

## 11. Failure modes already encountered

Recorded because they will recur, and because each one produced plausible
output rather than an error.

- A mis-set `input_pump2` exchanged two vials' high-media doses for an entire
  run. Nothing failed visibly; one vial was poisoned and the other under-dosed.
- A sipper above the falling media line delivered air for 8 h while the
  controller kept recording concentration increases.
- A rate measured across a refill; a rate borrowed from a bottle retired four
  days earlier; a delivered-dose figure above the physically attainable ceiling.
- Renaming a reservoir on reformulation orphaned its entire history.
- A controller run restart reset the clock, so the viewer's high-water mark sat
  in the future and it silently stopped fetching.
- Lineage connectors drawn from the parent's *end* rather than the branch point,
  implying every fork had just happened.

The pattern: **the dangerous failure is a confident number, not a crash.** When
a value could be right or wrong and you cannot tell, say so in the log rather
than picking.
