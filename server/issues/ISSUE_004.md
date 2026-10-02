# ISSUE_004 — `GET /media` extrapolates the present instead of measuring it, because nothing on the media path can see pump data

**Status:** implemented 2026-09-21, hardened over three rounds of adversarial
testing (~60 confirmed defects). See "What the testing found"
below — several of the worst were in the parts that looked most finished.
**Found:** 2026-09-21, from an operator question — "can an LLM querying about
the media status currently see the pump history?" The answer is no, and the
interesting part is what the report does *instead*.
**Affects:** `app/routes/media.py`, `app/skill.py`, new `app/pump_rates.py`
and new `app/dashboards.py`; in the log repo, `tools/evolver_api.py`,
`dashboard.py`, `tools/check_api.py`, new `tools/test_evolver_api.py`.
(`app/config.py` was named here in the first draft and turned out not to need
touching — the rig roster went into its own module instead.)

---

## The problem

Every number in `GET /media` traces back through `tools/media.py`'s
`analyse()` to two hand-entered event types: `level_reading` and
`media_prep`. The log has no pump event type at all, by design —
`LOG_PROTOCOL.md` §1: *"It records actions and beliefs, not telemetry.
Per-dilution OD and pump data live on the eVOLVER."*

That boundary is right for the log. It is not automatically right for the
*report*, and the current consequence is a specific, quantifiable loss.

`analyse()` estimates a reservoir's present level by linear extrapolation
from its last reading (`tools/media.py`, pass 3):

```python
elapsed  = hours(at, row["level_as_of"])
now_lvl  = max(lvl - rate * elapsed, 0.0)
```

where `rate` itself came from two readings *on the same bottle, before that
last reading*. So the further `at` drifts from `level_as_of`, the more of
the answer is a projection of past average behaviour onto a present nobody
has looked at. The rate is stale by construction: it cannot know about a
ramp step taken since, a line terminated since, a blocked pump since, or a
vial that went off setpoint since. Meanwhile the eVOLVER has recorded every
individual dispense that actually happened in that window, to the
millilitre, and the media report cannot see any of it.

Two smaller symptoms of the same gap, both already in the tree:

- `_clamp_backward_extrapolation` (`app/routes/media.py`) exists because
  that formula run backwards grows a 1 L bottle to 1015 L. A measured
  integration has no backwards mode to guard.
- `delivered_pg` is documented to an LLM client as *"the mean PG
  concentration actually delivered"* (`app/skill.py`) and in `media.py`'s
  own docstring as *"actually pumped"*, while being computed entirely from
  bottle-level rates. The wording is aspirational; nothing about it is
  wrong today only because no consumer has yet asked it to be.

## What already exists, and what does not

The rig side is further along than the server side:

| Piece | State |
|---|---|
| `<exp>/pump_log/vial<N>_pump_log.txt` | Written by `custom_script.py` on every dispense: `time` (controller hours), `timein` (pump-on seconds), `pump` (`in1`=low, `in2`=high). Volume is never recorded — every mL is `timein × coefficient` from `pump_cal.json`. |
| `tools/evolver_api.py` | Read-only JSON API over those files. `vial_rates()` is the single implementation of the rate maths. Serves `/api/v1/vials` (trailing-window burn rates), `/api/v1/vials/<v>/dispenses` (raw `[t, mL, role]` triples, incremental via `since_h`), `/api/v1/health`. |
| `dashboard.py` | Runs beside each rig and hosts that API — **but the registration is not in this repo's copy**. `evolver_api`'s docstring calls it "two lines"; nothing in `dashboard.py` imports it, and `check_api.py`'s own failure text anticipates exactly this ("Is evolver_api registered in dashboard.py?"). Confirmed by the operator to be applied out-of-band on the live rigs. |
| `viewer.html` | Already consumes all of it — burn-rate column, generation counts from dispense volumes, unit-identity confirmation, controller-restart detection. |
| `GET /media` | Sees none of it. |

So the viewer has had live pump data for weeks and the media report, which
is the thing an LLM is pointed at, has not.

## Why the two estimates compose rather than compete

They answer different questions about different intervals:

```
level-derived : rate measured between reading₀ and reading₁ — bottle truth, coarse, stale
pump-derived  : volume integrated from reading₁ to now      — fine, live, calibration-dependent
```

The level reading stays the anchor. Pump data replaces the *guess about
what happened after it*:

```
estimated_now_L = level_at_last_reading − Σ(dispensed since that reading)
```

That is a measurement where there is currently an extrapolation. And the
difference between the two — a bottle that lost 260 mL while the pumps
claim 180 mL — is a new instrument this experiment does not currently have:
a leak, an unlogged spill, a miscalibrated pump, or a line drawing from a
bottle nobody recorded. Neither number alone can say that.

## Why this does not go in `tools/media.py`

`LOG_PROTOCOL.md` §8 documents that module as *"Consumption rates and
depletion forecasts from bottle readings only. **Never uses live data.**"*
`tools/test_media.py` pins its behaviour, it runs as a standalone CLI with
no third-party imports, and `MEDIA_TRACKING.md` draws the same boundary
three times over ("this server shapes output, it doesn't alter the
consumption model").

The new estimator therefore lives in this server, in `app/pump_rates.py`.
That has an honest cost — the server gains arithmetic the log repo doesn't
own — so it is confined to one function, and every rate, basis and forecast
`media.py` already computes continues to come from `media.py` untouched.

## The join

Verified against the live log, both directions, zero mismatches (8 active
reservoirs, 16 active lines, 4 lines each):

- `lines[*].pg_regime.source_reservoirs.{low,high}` → reservoir id
- `reservoirs.items[*].lines_fed` → the same relation, mirrored
- `lines[*].{unit,vial}` → the dashboard's vial key
- pump `in1` → `low`, `in2` → `high`, already normalised by `evolver_api`

A reservoir's pump-derived draw is the sum, over its `lines_fed`, of each
line's vial's matching-role volume, ÷1000 for litres. The anchor is that
reservoir's own `level_as_of`, which `analyse()` already returns per row.

## Proposed solution

**Rig side** (log repo, must be deployed to each rig):

1. Commit the `evolver_api` registration into `dashboard.py` so it stops
   living only on the rigs, and point `fig_pumpcontrol_ramp` at
   `vial_rates()` so the figure and the API cannot disagree about what a
   burn rate is.
2. Add `GET /api/v1/consumption?since=<ISO>|&since_h=<float>` returning, per
   vial, volume integrated since that instant: `{vial, low_mL, high_mL,
   n_events, first_event_h, last_event_h}` plus `elapsed_h`,
   `pump_calibration`, `clock_ok`. Neither existing endpoint fits: `/vials`
   windows relative to *now* and cannot anchor at a level reading;
   `/dispenses` can, but is O(run length) and one call per vial, and pushes
   the wall-clock↔controller-clock conversion onto the caller. Payload size
   here is fixed by vial count, one call per unit, and the clock conversion
   happens on the side that owns the clock.
3. `generated_at` currently emits `%z` (`-0400`). Emit a colon offset
   (`-04:00`) to match the log's own timestamp rule. `Date.parse` accepts
   both, so the viewer is unaffected.
4. Extend `check_api.py` to verify the new endpoint.

**Server side:**

5. `app/config.py`: per-unit dashboard URLs read from
   `LOG_REPO_PATH/viewer.config.json` — already the single source of truth
   for this mapping and already carrying the identity-confirmation
   discipline — with `EVOLVER_DASHBOARD_URLS` overriding it per deployment.
6. `app/pump_rates.py`: fetch (httpx, already a dependency), join, integrate,
   apply the refusal rules below. Falls back to aggregating `/dispenses`
   itself when `/consumption` 404s, so the feature works against rigs as
   they are today, before step 2 is deployed anywhere. **(As shipped the
   trigger is wider: a 404 OR any 2xx that is not JSON. A live Dash
   dashboard answers 200 with its own index page for a route it does not
   have, so a 404-only trigger fired against no real rig at all — see "What
   the testing found".)**
7. `app/routes/media.py`: `?pump=auto|off|only` (default `auto`), attaching a
   per-reservoir `pump` block and a top-level `pump_sources`. Purely
   additive — every existing field keeps its current value, per
   `MEDIA_TRACKING.md` §5's "pass rows through whole".
8. `app/skill.py`: a hard rule that a pump-derived figure is never presented
   without saying so, the two estimates are never averaged or silently
   swapped, and a material divergence is always surfaced.
9. `LOG_PROTOCOL.md` §8's tools table stays true of `media.py` and needs a
   row saying where live data *does* now enter the media report.

## The refusal rules

This project's stated failure mode is a confident number, not a crash
(`LOG_PROTOCOL.md` §11). Each of these must produce a **named
unavailability** — never a silent zero, and never the level-derived number
wearing a pump-derived label:

1. **Unit unreachable / timed out.** Short timeout, never fatal to the
   request; the level-derived row is untouched.
2. **`pump_calibration: false`** (no `pump_cal.json`). Volumes are `null`,
   not `0`.
3. **Controller restart inside the window** — `elapsed_h` went backwards or
   the experiment name changed. `viewer.html` already handles exactly this
   and its comment explains why it is silent corruption otherwise.
4. **A fed line changed vial or unit since the anchor reading**
   (`hardware_swap`, vacate). The current line→vial map does not describe
   the window, so another culture's dispenses would be charged to this
   bottle. CLAUDE.md's "line identity follows the culture, not the
   hardware", showing up as an arithmetic bug.
5. **A fed line's source reservoir changed in the window**
   (`media_switch`, `reservoir_swap`, a `pg_change` moving
   `source_reservoirs`). Same refusal.
6. **A historical `at`.** The API reports only the present, so an `at`
   meaningfully in the past gets no pump block, with that as the stated
   reason — preserving `MEDIA_TRACKING.md` §4's reproducibility promise.
7. **A stale vial** contributes zero volume, which is a real measurement
   rather than missing data — but is flagged, since a blocked or dead line
   looks identical to a quiet one.
8. **Recalibration drift** cannot be detected through the API at all.
   Documented as a known limitation rather than implied away.

## Acceptance

- Every refusal rule has a test asserting the *absence* of a number plus a
  named reason, against a fake dashboard — the discipline
  `tools/test_media.py` and `tools/test_schema.py` already follow.
- A default `GET /media` with no reachable unit is byte-identical to today's
  response apart from `pump_sources` reporting why.
- With a reachable fake rig, `estimated_now_L` is the measured integration
  and says so in `pump.basis`; the level-derived fields alongside it are
  unchanged.
- `tools/media.py` and `tools/test_media.py` are untouched.


---

## What the testing found

Ten agents across two rounds, each required to reproduce a finding by running
code before reporting it. Recorded here because the reasoning is worth more
than the diff, and because three of these were defects introduced by a fix for
an earlier one.

**The rig's clock was not what either side thought it was.** `elapsed_h` was
`max(time)` across the logs — when the rig last *wrote*, not now — while
`generated_at` is true now. The module's own identity, `wall(t) = generated_at
− (elapsed_h − t)`, holds only if those coincide. They differ by the write
staleness, so every wall-clock anchor resolved earlier than asked and every
window over-counted, always in one direction, and by the most during a hold or
a stall. `controller_now()` closes it with the pump log's mtime, which dates
the last row whose controller hour is known.

Then the fix had to be fixed. `max(staleness, 0.0)` clamped away the evidence:
a future-dated mtime became byte-identical to a current rig, and `stale_h` went
back to structurally zero for the last-writing vial — undoing what the fix
claimed to achieve. And an experiment directory restored with mtimes intact
(`rsync -a`, `tar -xp`, Time Machine) looks exactly like a long-idle rig, which
answered **0.0 mL with both flags green**. Staleness is unclamped now, and past
`MAX_TRUSTED_STALENESS_H` a wall-clock anchor is refused rather than answered.

**The fallback never fired against a real rig.** A Dash app without the route
answers **HTTP 200 with its own `index.html`**, not 404. The trigger was
`status_code == 404`, so the entire "works on rigs as they are today" path was
dead, and the refusal blamed "a body that is not JSON". Invisible to the tests,
because the fake modelled a missing endpoint as a 404 — which is not what Dash
does. Found only by probing the live dashboards.

**The comparison was worse than no comparison.** `predicted_drawn_L` was the
level-derived rate times an unbounded window, so with readings ~600 h old it
predicted up to 11 L drawn from a 1 L bottle, and `GET /skill` instructs a
client to surface `divergence_note` whenever it appears: an LLM would have
reported a leak on all eight active bottles the first time a rig answered. The
guard added for that then compared against the bottle's current *level*, which
switched the check off whenever a bottle was nearly empty — a blocked pump
delivering 20 mL where the record implied 139 was reported at 0.140 L and
silent at 0.139 L. It compares against the bottle's capacity now.

**A filter changed a number it had no business touching.** `?pump=only`
filtered the rows `high_media_outlook` and `dose_estimates` consume;
`high_media_outlook` looks its low reservoir up inside that list and falls back
to `c_low = 0.0`. An unreachable dashboard moved the ramp forecast by 80%, and
`delivered_pg` then reported "no measured rate for the low reservoir" about a
reservoir whose `rate_basis` is `measured`. A rig outage must not be able to
make the level-derived record lie.

**The anchor was a projection the log contradicts.** `analyse()` takes
`level_L`/`level_as_of` from `reservoirs.items[*]`, and on the live log eight
of eight active bottles have a `level_reading` event newer than their stored
block — by up to 0.30 L on a 1 L bottle, every one in the direction that says
there is more media than there is. Both validators pass it. Refused now, with
the contradicting event named.

**Shape of the rest.** Eight rig payloads that 500'd the whole route, taking
the level-derived answer down with the pump view. A duplicated vial entry
silently discarding the real volume. A negative volume making the bottle grow
past `prepared_L`. `quiet_vials` blind to a blocked low pump on a busy vial.
The join checked in only one direction — and then checked against a merge of
two schema-required records that can disagree, with the second silently
winning. `clock_ok`/`covers_window` failing *open*. No rig-clock-skew check at
all. No floor on the window, so five minutes after a level round one dilution
cycle read as 0.48 L/h. A 0.001 mL draw — the smallest a rig can report —
overflowing `datetime` and blanking both units.

And the failure mode that keeps recurring in different costumes: **a refusal
that is correct but too broad is also a defect.** Blanking all four of a
unit's bottles for a `hardware_swap` that touched none of them, refusing a
`pg_change` that moved a controller value rather than any line's plumbing,
refusing a bottle for a vial it does not feed with a reason asserting that it
does — each of those trades a wrong number for a useless feature, which is the
other way to lose.

## Round 3: the bounds

A per-request timeout bounds a socket read and nothing else, and `GET /media`
is an unauthenticated route an LLM may poll. Measured against a real
socket-level rig — the in-process mock the suite uses never touches a socket,
so no timeout in this module had ever actually fired in a test:

- **One ordinary polling client wedged the whole server.** The route is a sync
  `def`, so Starlette runs it on anyio's default thread limiter (40 tokens,
  shared with every route), and a client that hangs up is never noticed — the
  thread runs to completion. One sequential client polling once a second
  against a rig dribbling its response under the read timeout killed
  `/health`, `/lines` and even `/media?pump=off` at the 40th abandoned poll,
  bisected exactly. With a dripping rig it never recovered.
- **Per-request timeouts summed without limit**: a rig answering in 2.8 s
  under a 3 s timeout produced an 84 s *successful* request; worst-case
  fan-out is 126 s, reachable from ordinary operator behaviour (reading
  bottles one at a time makes four anchors).
- **A 299 KB gzip bomb cost 1.3 GB**, because `r.json()` reads and decodes the
  whole body before anything can inspect it.

The four constants that answer this live in code, and are listed here because
nothing else documents them:

| Constant | Value | Bounds |
|---|---|---|
| `PUMP_CONCURRENCY` (`app/routes/media.py`) | 4 | Concurrent pump views; past it, declined by name pointing at `?pump=off` |
| `PUMP_BUDGET_S` (`app/pump_rates.py`) | 8.0 s | All rig traffic in one request |
| `MAX_FETCHES` | 24 | Planned rig requests, refused up front |
| `MAX_BODY_BYTES` | 256 KB | Decompressed body, enforced while streaming |

**None of these is environment-tunable**, unlike `EVOLVER_DASHBOARD_TIMEOUT_S`
(which is). Nor are `MIN_WINDOW_H`, `RIG_SKEW_TOLERANCE_MIN`,
`AT_TOLERANCE_MIN`, `MAX_TRUSTED_STALENESS_H`, `DIVERGENCE_REL` or
`DIVERGENCE_ABS_L`. If one of them needs to vary per deployment, that is a
change, not a setting that already exists.

Two correctness fixes came out of the same pass. **Only a 2xx is a
measurement**: 200–399 all fell through to the body, so a `301` whose body
happened to parse produced a confident `pump_integrated` number from a
response saying the resource is elsewhere. And the payload's **`schema` is
checked before the clock flags**, because any JSON object lacking
`clock_ok`/`covers_window` was reported as "the rig restarted, so its pump log
describes a different run" — an invented diagnosis that would send an operator
to power-cycle a healthy rig.

**A naming collision worth knowing about.** `rate_provisional` exists twice
with different meanings: on the reservoir row it is `analyse()`'s (the
level-derived rate was measured over less than `MIN_SPAN_H`), and inside the
`pump` block it is this module's (less than `MIN_WINDOW_H` has passed since
the bottle was read). `MIN_SPAN_H` and `MIN_WINDOW_H` are independently
defined and both happen to be 2.0 h; nothing keeps them in step, and nothing
requires them to be equal.

## Known preconditions, not defects

- **The newest level reading must be newer than the rigs' last controller
  restart.** Otherwise the pump log begins after the anchor and every volume
  is a lower bound, which makes the remaining level an upper bound — the
  optimistic direction — and the reservoir is refused. As of 2026-09-21 the
  log's newest readings are from 08-27/28 and the rigs restarted ~3 days ago,
  so **every reservoir refuses on this ground until a fresh level round is
  logged**.
- **Both live dashboards report `evolver: null`** and sit on the same host,
  differing only by port. The identity check that stops one rig's pump data
  landing on the other's bottles is therefore inert; setting `evolver_name` in
  each rig's `experiment_parameters.yaml` closes it.
- **A recalibration mid-window is undetectable through the API**, as stated in
  the original refusal list. It remains so.
