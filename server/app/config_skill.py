"""GET /config/skill -- operator-facing instructions for an LLM client
generating an experiment_parameters.yaml for one eVOLVER unit, via
POST /config and POST /config/candidate.

Deliberately a SEPARATE document from GET /skill (app/skill.py), not a
section bolted onto it. The two teach an LLM two different idioms for two
different consumers: /skill teaches append-only, past-tense event logging
into evolution_log.json, validated against schema/evolution_log.schema.json
and tools/lineage.py; this teaches forward-looking, replace-not-append
config generation for custom_script.py's Settings() class, validated
against app/config_validator.py's INTENDED rules for that class (see that
module's own docstring for exactly which gaps in the real Settings() this
closes). Mixing the two risks exactly what this split avoids: an LLM
blending append-only and replace-in-place idioms, or trying to "log" a
config change as if it belonged in evolution_log.json directly.

Hand-written, not generated from the live route table the way GET /skill's
"## Routes" section is (app/skill.py's _route_table/_flatten_routes) -- there
are only three routes here, and hand-writing them avoids coupling this new,
small surface to that fastapi-version-sensitive introspection machinery.
"""

_SKILL_TEXT = """\
# @@NAME@@ evolver config skill

Generates and validates `experiment_parameters.yaml` -- the config
`custom_script.py`'s `Settings()` class reads to run one eVOLVER unit's
control loop. This is a SEPARATE document from `GET /skill` (which covers
`evolution_log.json`) -- see that endpoint for logging what already
happened; this one is for configuring what a unit does next.

## Authentication

`POST /config` requires a bearer token (same tokens as `POST /events` /
`POST /lines`) -- it commits into a unit's own git repo, so it needs an
attributable author. `GET /config` and `POST /config/candidate` are open --
neither writes anything.

## Routes

| Method & path | Auth | What it does |
| --- | --- | --- |
| `GET /config?unit=<unit>` | no | Read the current `experiment_parameters.yaml` for one unit, as JSON. |
| `POST /config/candidate` | no | Validate a candidate config WITHOUT writing it anywhere. Always call this before `POST /config`. |
| `POST /config` | yes | Validate, then write, then commit into that unit's own git repo. Never touches `evolution_log.json`. |

Both POST routes take the same body: `{"unit": "<unit>", "config": {...}}`,
where `config` is the WHOLE `experiment_settings` document (not a partial
patch) -- `GET /config`'s response is exactly this shape, so the normal
cycle is: `GET /config` to see what's there, edit the `config` object you
got back, `POST /config/candidate` to check it, `POST /config` to commit it.

## Hard rules (not suggestions)

- **Two modes are validated: `pumpcontrol_ramp` and
  `alternating_selection`.** Every other mode -- including ones
  `custom_script.py` itself already handles (`calibration`, `chemostat`,
  `chemostat_dual`, `turbidostat`, `morbidostat`) -- returns `501 Not
  Implemented`. That is a deliberate scope limit, not a bug: they will be
  worked through systematically. Do not route around it by disguising one
  mode's config as another.
- **The two validated modes require different per-vial fields**, and the
  required set is the fields `custom_script.py`'s own branch READS. Sending
  the wrong mode's fields does not merely fail validation -- it would
  produce a config whose live parameters the controller silently defaults.
  `pumpcontrol_ramp` needs `setpoint`, `interval`, `input_pump2`,
  `number_consecutive_intervals`, `initial_concentration`,
  `high_concentration`, `low_concentration`, `target_ramp`.
  `alternating_selection` needs `setpoint`, `input_pump2`,
  `initial_concentration`, `high_concentration`, `low_concentration`,
  `n_tolerant`, `n_dilutions`, `ramp`, `media_wait_time` -- and `ramp` there
  is the per-visit step added to `Current_Drug`, NOT `pumpcontrol_ramp`'s
  `target_ramp`, which is a different field with a similar name.
  `volume` and `temperature` are required for EVERY vial in both modes,
  whether or not it runs.
- **`alternating_selection` has four optional knobs with real defaults**:
  `initial_drug_target` (defaults to `initial_concentration`, i.e. start
  challenging at whatever is already in the vial),
  `growth_interval_multiplier` (3.0), `stress_wait_fraction` (0.9) and
  `fold_dilution` (10.0). Omitting one is fine and deliberate; setting it to
  a guess is not. In particular `initial_drug_target` is COUPLED to
  `initial_concentration` by default, and a number written here silently
  breaks that coupling.
- **Never write YAML's null as the string `"None"`, `"null"`, `"nan"`,
  etc.** `yaml.safe_load` parses an unquoted `None` as the four-character
  STRING `"None"`, not Python's `None` -- an easy mistake for an LLM
  trained mostly on Python to make. Use YAML's actual null (omit the key,
  or write `null` explicitly). Checked and rejected for `calib_name`.
- **Every active vial (`to_run: true`) must fully specify its own
  operation-mode parameters.** `custom_script.py`'s `Settings()` class
  silently defaults a missing field to `0`/`100`/`10000` rather than
  erroring -- this validator treats that as a hard rejection instead, since
  a silently-defaulted live pump parameter is exactly the "confident wrong
  number" failure mode this whole project exists to avoid. Provide
  `setpoint`, `interval`, `input_pump2`, `number_consecutive_intervals`,
  `initial_concentration`, `high_concentration`, `low_concentration`, and
  `target_ramp` explicitly for every vial you turn on -- and `volume` /
  `temperature` for every vial, active or not.
- **`high_concentration` must be strictly greater than `low_concentration`**
  for every active vial -- documented directly in
  `find_optimal_pump_volumes`'s own docstring (`ch > cl`) and enforced here.
- **`dilution_fraction` and `growthdelta` do nothing.** `custom_script.py`
  reads them into `Settings` and never uses them anywhere else in the
  script. Setting them is harmless but pointless -- `POST /config/candidate`
  returns a warning, not a rejection, if you set either.
- **A vial number must appear at most once.** `custom_script.py`'s own
  per-vial lookup silently keeps only the LAST entry for a duplicated vial
  number and discards the earlier one with no warning -- this validator
  rejects the duplicate outright instead.
- **`config` must be the WHOLE `experiment_settings` document, and this is
  ENFORCED, not just documented.** A body that omits a vial, or a
  top-level key (`stir_settings`, `temp_all`, ...), that the CURRENT config
  has is rejected outright -- omitting something does not leave it
  unchanged, it deletes it. Found necessary by simulating a client that
  treated this API like a PATCH: a body naming only the one vial it meant
  to change validated as `valid: true` and, once written, permanently
  discarded every other vial's real settings with no warning. If you
  genuinely mean to retire a vial or drop a field, resubmit the SAME body
  with `confirm_removed_fields: true` -- the removal still happens, but
  only once you say so explicitly, never as a side effect of forgetting to
  include something.
- **Every number must be finite.** NaN and +-infinity are rejected outright
  at both `POST /config/candidate` and `POST /config` -- even though a
  literal JSON `Infinity`/`NaN` in a request body is not actually
  impossible (some JSON encoders emit it by default), and even though
  `high_concentration: Infinity` would otherwise pass every other rule
  here. Do not construct one on purpose to represent "unlimited" or
  "unknown" -- use `null`/omit the key for a field that's genuinely not
  applicable to an inactive vial.
- **Physically impossible values are rejected, not just absent/wrong-type
  ones.** A negative `volume`, `high_concentration`, `low_concentration`,
  `initial_concentration`, `interval`, or `number_consecutive_intervals`,
  or `volume: 0` on an active vial, is rejected regardless of type-
  correctness. This does NOT bound magnitude (an absurdly large but
  positive value is not currently caught) or units (an `interval` entered
  in the wrong time unit is not currently caught either) -- only sign and
  the active-vial-needs-nonzero-volume case.
- **`target_ramp: 0` on an active vial is legitimate** for a deliberately
  non-ramping vial -- but it is NOT a true fixed-rate chemostat. It's still
  `pumpcontrol_ramp`'s own OD-sensor-triggered dilution logic, just frozen
  at a constant target concentration, not the separate (currently
  unimplemented) `chemostat`/`chemostat_dual` triggering logic.

## What this API cannot do

- **Cannot add a new eVOLVER unit.** `unit` must already be a registered
  `hardware.units` entry in `evolution_log.json`, AND have a path
  configured server-side (`EVOLVER_UNIT_PATHS`) -- both are human-only,
  deployment-level changes, not something this API can do on request.
- **Cannot touch `evolution_log.json`, ever, from these routes.** See
  "Workflow" below for what you must do instead.

## Workflow: after a successful write

`POST /config` only ever writes `experiment_parameters.yaml` and commits it
into that unit's OWN git repo. It never appends anything to
`evolution_log.json` -- a config change is invisible to the experiment log
until you separately log it. Every `POST /config` response includes a
`reminder` field saying exactly this; do not skip it.

After a successful `POST /config`, log what changed with a SEPARATE
`POST /events` call using `event_type: "controller_config_change"` -- a
real, already-used event type (see `GET /skill`), not a new invention. Name
the setting that changed in `params.controller_parameter`, and name the
actual before/after values -- for `target_ramp` specifically, match the
log's own existing convention exactly: `params.ramp_step_size` (the new
value) and `params.previous_ramp_step_size` (the old one), as in the real
`EVT-00088`. `POST /events` rejects a `controller_config_change` event that
names no `controller_parameter`, or that names one but nothing else
describing what changed -- recording that *something* changed without
saying what is not useful to whoever reads this back later.

If the change affects every active line on a unit, log ONE facility-scoped
event (`target.scope: "facility"`) naming `lines_affected`, the way
`EVT-00088` does, rather than one event per line.

## What takes effect immediately, and what needs a restart

`custom_script.py` re-reads `experiment_parameters.yaml` once per event
cycle and can pick up a handful of fields WITHOUT a restart -- but only
those, and only for a vial that is already `to_run: true`. Everything else
(a brand-new active vial, `exp_name`, `input_pump2`, `volume`,
`high_concentration`/`low_concentration`, `initial_concentration`, and any
field not on that short list) only takes effect the next time the eVOLVER
process is restarted.

Do not guess which is which, and do not rely on this document staying
exhaustive -- every `POST /config` response carries a `live_reload` key
naming EXACTLY which of the fields YOUR write actually changed fall into
each bucket. This is a REAL, ILLUSTRATIVE-ONLY example of the response
SHAPE, not a value to reuse -- always read the actual numbers from your
own response, never these:

```json
"live_reload": {
  "applies_without_restart": [{"vial": 4, "field": "target_ramp", "old": 0.1, "new": 0.15}],
  "requires_restart": [{"vial": 4, "field": "high_concentration", "old": 11.0, "new": 12.0}]
}
```

A field that didn't change at all is not listed in either bucket. If
anything you needed to change landed in `requires_restart`, the change is
saved and committed, but is NOT running yet -- say so plainly to the
operator; do not imply otherwise, and do not attempt to restart the process
yourself, since this API has no route for that.

## What the errors mean

- **`404`** -- `unit` is not a known `hardware.units` entry, or this server
  has no `EVOLVER_UNIT_PATHS` entry for it. The detail message names the
  real, known unit names -- if you guessed the case/spelling wrong, it's
  right there, no need for a separate lookup.
- **`422`** -- `POST /config` returns this when the config fails
  validation; the response `detail` is a flat list of every problem found,
  not just the first. `POST /config/candidate` does NOT use this code for
  a business-rule failure -- it always returns `200`, with `valid: false`
  and the SAME problems in a `problems` field. Check `valid`, not the
  status code, when calling `/candidate`; the only `422` `/candidate` can
  return is a plain request-shape error (e.g. `unit` missing entirely),
  which has a different shape again (a list of `{loc, msg, type}` objects,
  not plain strings).
- **`501`** -- `operation.mode` is not yet implemented by this validator.
  The response names the mode you sent and the modes that ARE supported.
  If `mode` is `null` AND the detail carries a `likely_cause` key, the real
  problem is probably that `experiment_settings.operation` is missing
  entirely -- i.e. you sent an incomplete document (see the partial-patch
  rule above), not that you deliberately chose an unsupported mode. Fix the
  document, don't try a different mode value.
- **`500`** -- the file write succeeded but the git commit into the unit's
  repo did not; the file has already been reverted to match git HEAD, and
  the write is safe to retry.

A `POST /config` response can also carry non-blocking `warnings` alongside
`written: true` -- e.g. an active vial whose number isn't listed in
`hardware.units.<unit>.vials_in_use`. This does not block the write (that
field is known to sometimes be stale), but relay it to the operator rather
than silently dropping it.

Re-submitting an UNCHANGED config to `POST /config` is safe and expected:
you still get `201`/`written: true`, but no new commit is made in the
unit's repo (git's own "nothing to commit" is treated as success here,
not an error) -- this is not a bug if you see it, it means your write was
already reflected on disk.

`GET /config` never returns a raw `NaN`/`Infinity`/`-Infinity` even if one
happens to be sitting in the file on disk (from before write-time
rejection existed) -- any such value comes back as JSON `null`,
indistinguishable from a field that was simply never set. Treat both the
same way: as "not applicable," never as a real number to preserve or
reason about.
"""


def render_config_skill(experiment_name: str) -> str:
    # The name comes from the log (app/log_repo.py:experiment_identity), the
    # same as GET /skill's, so the two documents cannot name different
    # experiments.
    return _SKILL_TEXT.replace("@@NAME@@", experiment_name)
