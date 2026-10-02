"""GET /skill: operator-facing instructions for an LLM client appending to
this log, generated fresh on every request rather than hand-maintained
prose.

Per README.md's "deliberate divergence from decision #1", there is no single
Pydantic definition this whole app is generated from -- so this is not "one
definition generates the validator, the OpenAPI spec and the skill text"
literally. It's the next best thing, section by section, each pulling from
whichever place is actually authoritative for that fact, so nothing here is
a second, hand-copied, driftable description of something already written
down elsewhere:

  - the route list: introspected from the live FastAPI app, not a hand-kept
    list that could fall out of sync with what routes actually exist.
  - request shapes for POST /events and POST /lines: introspected from the
    live Pydantic models (models.py, line_models.py) via model_fields, not
    re-described by hand.
  - GET /media's query parameters: introspected from the live route's own
    Query(...) descriptions (app/routes/media.py) via
    route.dependant.query_params -- there's no Pydantic model for a plain
    GET's query params, so this is a second, narrower introspection path
    for the one route that needs it, not a second copy of the request-shape
    logic above.
  - event_types / parameter_registry: read from the log itself, live, every
    request -- this IS the current registry, not a snapshot of it.
  - the hard rules (missing_fields, timestamps, corrections): quoted
    verbatim from schema/evolution_log.schema.json's own field descriptions,
    which were already written anticipating exactly this reader (Phase 1/2
    of the log repo's own work).

Only the narrative framing (what this is, why it exists, what's out of
scope, GET /media's response shape) is static hand-written prose, because
there's nothing to introspect a plain dict response against. NOT generated
from LOG_PROTOCOL.md or SERVER_DESIGN.md, despite both being cited in the
static prose below -- those citations are inspiration for hand-written text,
not something this module parses; editing either file has no effect on
what this endpoint returns.
"""
import re
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from .config import Settings
from .line_models import BranchRequest, MergeRequest, RestartRequest, SplitRequest
from .log_repo import experiment_identity, load_log, load_schema
from .models import NewEventRequest
from .pump_events import DEFAULT_WINDOW_H, MAX_WINDOW_H

# The experiment's name, title and units are filled in from the log per
# request (_render_header) -- never hardcoded, or every server introduces
# itself as whichever experiment this text was first written for.
_HEADER = """\
# @@NAME@@ evolution log — API skill

This server reads and appends to `evolution_log.json`, the provenance
record for **@@NAME@@**@@ABOUT@@.
It records **actions and beliefs, not telemetry** — decisions, interventions, faults, and the reasoning behind them, not per-dilution
sensor data. Full rationale lives in this repo's
`LOG_PROTOCOL.md` and `SERVER_DESIGN.md`; this document is the operational
summary for an LLM client, generated fresh from the live schema, registry,
and route table every time it's requested — if something here looks wrong,
it reflects what those actually say right now, not a stale copy.

**The one rule that outranks the others: history is append-only.** Nothing
this API can do edits or deletes a previously-committed event or line.
Every route below either reads, or adds something new.

**If your task is generating or changing an eVOLVER unit's own config**
(`experiment_parameters.yaml` -- what pumps a unit runs, not what already
happened), that is a SEPARATE document: fetch `GET /config/skill` instead.
Nothing below this line covers it, deliberately -- see that document's own
header for why the two are kept apart rather than merged.

## Authentication

`POST /events` and `POST /lines` require `Authorization: Bearer <token>`.
The token is for **attribution, not perimeter defence** — it determines the
`operator` field on what you write and the git commit author, nothing more.
Get a token from whoever operates this server. `GET /health`'s
`auth_configured` field tells you whether tokens are set up at all, without
revealing any of them.

## Routes
"""

_HARD_RULES = """\
## Hard rules (not suggestions)

These have each caused real errors in this log before. Read them before
writing anything you're not sure about.

**Timestamps.** {timestamp_desc}

**Timestamps must not be in the future** (server-enforced, not a schema
rule — `app/writer.py`). An event this API writes records something that
already happened; a timestamp more than an hour ahead of wall-clock now is
refused with 422, for every write route. The one-hour grace window
tolerates ordinary clock skew between your clock and the server's, not a
wrong day/year. Found necessary the hard way: a future-dated `level_reading`
that WAS accepted let `GET /media`'s default (no `at`) view linearly
extrapolate backward across it, reporting a confident, entirely fictitious
"current" volume/hours-remaining for a reservoir whose real latest reading
said empty.

**`missing_fields`.** {missing_fields_desc}

**`provenance`.** {provenance_desc}

**Corrections.** Never ask this API to edit an existing event — it can't,
by construction (there is no route that takes an existing `event_id` to
modify, and the server proves every write is a pure append before
committing). To correct something already logged: append a *new* event
whose `supersedes` field names the event being corrected, and explain what
was wrong and why in `notes`. The superseded event is never touched.
{supersedes_desc}

**Three different PG quantities — do not conflate them.** (1) *The
regime* — `pg_regime.low`/`.high`: the reservoir concentrations a line is
dosed between, changing only when media changes. (2) *The vial
concentration* — live controller state, never transcribed by hand; not
something you set via this API. (3) *The delivered dose* — mean
concentration actually pumped, derived by the log repo's own tooling, not
computed here. If you're not certain which one a value describes, say so in
`notes` rather than guessing which field it belongs in.

**Never invent a value.** If a fact is unknown, the field is `null` and its
name goes in `missing_fields` — never a plausible-looking guess. A gap can
be filled later; a guess cannot be detected.

**A vial number alone does not name a line.** Every unit numbers its own
vials from scratch, so "vial 5" can be a real, active, different line on
each unit at once — this is the normal case here, not a rare edge case.
`GET /lines` (with no `unit` filter) is now sorted by `(vial, unit)`
specifically so every line that has ever shared a vial number sits next to
each other in the response — check it before acting on any human
instruction that names a vial without a unit. If a human's instruction is
genuinely ambiguous this way (more than one real candidate, nothing else
disambiguating it), the correct move is to say so and ask, not to guess —
this API has no field for "not fully confident this targets the right
line," so an incorrect guess, once written, is structurally
indistinguishable from a correct one to anyone reading the log later.

**This API has no way to mark a value as proposed-but-unconfirmed.**
`provenance` is a closed set (`reported`/`document`/`instrument`/`derived`)
with no "an assistant suggested this, pending human confirmation" option,
and `missing_fields` means a fact is *known to be absent* — it does not
cover "I picked a plausible number myself and need it checked." If a human
gives an instruction like "bump the PG up a bit" with no actual number, do
not invent one and log it as reported fact: log what's actually known (that
a change was requested) with the undecided specifics in `notes` and
`missing_fields`, and get the real number before writing a `media_prep`/
`reservoir_change`/`pg_change` event with a concrete value in it.

**`GET /media`'s rates carry their own provenance — never drop it.** Every
reservoir row's `rate_basis` (`measured` · `prior_bottle` · `inferred` ·
`upper_bound` · `unknown`) says how that number was obtained, and
`rate_is_upper_bound`/`rate_provisional`/`baseline_orphaned` qualify it
further. A `measured` rate is not the same claim as an `upper_bound` one
even when the number looks identical. Do not present a depletion figure
(hours remaining, "empty at") without also saying its `rate_basis` — stating
`{{"hours_remaining": 16}}` alone reproduces exactly the failure this log
exists to prevent: a confident number whose basis is invisible.

**Never present a pump-derived figure as a bottle reading, or the reverse.**
A `pump` block is what the rig dispensed; the fields beside it are what a
human saw in a bottle and what follows from it. They are different claims
about different intervals, and they are reported separately on purpose:

- Say which one you are quoting. "About 0.6 L left, measured from the pumps
  since the 06:15 reading" and "about 0.6 L left, extrapolated from the
  consumption rate since 06:15" are not the same statement.
- **Never average them, and never substitute one for the other** because it
  looks more precise or more current. If `pump.basis` is `unavailable`, say
  the level-derived figure is all there is, and give `pump.reason` — the
  reasons are specific (a rig that restarted, a line that changed vials, a
  missing pump calibration) and each one tells the operator something.
- **Which one to act on.** When both are present and `drawn_is_upper_bound`
  is false, the `pump` figure is the better estimate of the bottle *right
  now*: it is the last hand-read level minus what the rigs actually
  dispensed since. The row's own `estimated_now_L` extrapolates a rate
  measured over `rate_span_h` across `window_h`, and cannot know about a
  ramp step, a terminated line or a blocked pump since. So act on the pump
  figure and quote the level-derived one beside it — except when
  `drawn_is_upper_bound` is true (the pump figure then bounds the answer
  rather than locating it: the draw is at most that, what is left at least
  that), or when `divergence_comparable` is false (the extrapolation has run
  past the bottle's contents, so only the pump figure means anything).
  Neither is settled until someone reads the bottle again.
- If `pump.divergence_note` is present, surface it. The pumps and the
  bottle disagreeing by more than rounding is the single most informative
  thing this route can tell you, and it is invisible in either number alone.
- `pump.overdrawn: true` means the pumps dispensed more than the bottle was
  recorded as holding. `estimated_now_L` is floored at 0 there; the bottle
  is empty or the reading was wrong, and either way it needs a human.
- With `?pump=auto` or `?pump=only`, an active reservoir ALWAYS has a `pump`
  block — a measurement or a named reason — so "no pump block" never means
  "nothing to say". With `?pump=off` no row has one at all, and the top-level
  `pump.note` says that plainly. Retired reservoirs never get one.
- **`attention` entries carry a `pump` sub-object too**, with `available`,
  `basis`, `hours_remaining`, `drawn_L`, `estimated_now_L`, `overdrawn`,
  `divergence_L`, `divergence_note` and `quiet_vials`. It is the list most
  worth summarising, so the two fields above that need a human are carried
  into it rather than left behind on the reservoir row.
- **A null `divergence_L` does not mean they agree.** It means either the
  comparison was refused (`divergence_comparable: false`, with
  `divergence_skipped_because` saying why) or the reservoir has no
  level-derived rate at all. Agreement looks like a number near zero, not a
  null. Never report "no divergence flagged" without checking which it was.
- **`attention` is ordered by the LEVEL-derived `hours_remaining` only**, and
  deliberately so: ordering by whichever estimate happened to be available
  would make the same request come back in a different order for reasons the
  caller cannot see. A bottle whose pumps say hours can therefore sit well
  down the list. Each entry carries `pump.more_urgent_than_level` and, when
  true, an `urgency_note` — re-rank on those before telling an operator what
  needs doing first. Position in this list is not rank.
- **The `pump` lists are scoped by `?unit=`/`?status=`.** Filters apply
  before any rig is contacted, so `measured`, `unavailable` and `sources`
  describe only what you asked for; `pump.scope` says which filters were in
  force. `unavailable: []` under `?unit=patrick` says nothing whatever about
  plankton. Retired reservoirs appear in neither list. An empty `sources`
  under `mode: auto` does not mean the rigs are healthy — it can mean every
  reservoir was refused before one was reached, and `pump.note` says so.
- **Watch the units in the aggregates.** `per_line.rate_L_per_h` is L/h *per
  line*, keyed by `(media, role)` only — it merges the units and ignores
  `?unit=`, so it is never one unit's figure. `delivered_pg.total_rate_mL_per_h`
  and `high_outlook.total_per_line_mL_per_h` are **mL/h**, while
  `high_outlook.scenarios[].rate_L_per_h` is L/h. Convert before comparing
  any two; the same quantity appears in this response at three scales.
- **`quiet_vials` is the silent-fault field.** A vial that drew none of THIS
  reservoir's media in the window is named there. That is a real measurement,
  not missing data — and a blocked or dead line looks exactly like a quiet
  one, so say which vials were quiet rather than only reporting the total.
"""

_CANNOT_DO = """\
## What this API cannot do

- **Cannot edit or delete anything, ever.** Not events, not lines, not
  reservoirs. If something was logged wrong, `supersedes` a new event onto
  it (see "Corrections" above) — never ask for the old one to change.
- **Cannot remove a reservoir, touch hardware config, or edit the
  `design`/`experiment`/`conventions` sections.** These are free-form,
  human-maintained narrative (LOG_PROTOCOL.md §3) with no route that
  writes them. `POST /events` for an *existing* reservoir's
  `level_reading`/`media_prep`/`reservoir_retired` moves that reservoir's
  own current-state fields, nothing about the reservoir list itself. For
  anything here -- a hardware change, a design-level fact -- tell the
  human operator it needs a direct edit to `evolution_log.json`.
  **Bringing a NEW reservoir online is the one exception**: a `media_prep`
  naming a `reservoir_id` that doesn't exist yet CREATES it, but only when
  the event also supplies `media`, `role`, `pg_concentration`, and
  `volume_prepared` (all four already registered params for `media_prep`)
  -- `reservoir_projection` reports `"created": true` when this happens.
  Missing any of those four falls through to the ordinary "no reservoir
  with this id" report, exactly as before this existed, never a
  half-built reservoir. `unit` is derived from the `reservoir_id`'s own
  `<unit>/<media>-<pg>` shape and checked against `hardware.units` --
  an unknown unit prefix is refused (422), same as any other destination
  check in this API. **A media_prep against an EXISTING, currently-
  retired reservoir does NOT reactivate it by default** -- volume/pg/
  current_volume still move (the physical fact of what was prepared is
  never withheld), but `status` stays `retired` and the response's
  `reservoir_projection` carries a `note` saying so. Pass
  `params.reactivate: true` on that same `media_prep` to flip `status`
  back to `active` in the same write, reported as `"status"` in
  `touched` -- deliberately explicit, never inferred from the media_prep
  alone, since retirement is itself a deliberate administrative fact.
  `reactivate: true` against an already-active reservoir is a harmless
  no-op (a `note`, not an error).
- **Does not keep every denormalised field fresh.** `POST /lines` updates
  `lineage.children`/`roots`/`depth` and, for the specific line(s) an
  operation ends, `status`/`terminated_at`/`terminated_by_event`. `POST
  /events` ALSO does this for a bare `termination` event logged directly
  against a line (not through `POST /lines`), and moves `current_media` for
  a `media_switch` -- see `line_lifecycle_projection` in the response,
  below. **Both are refused (409) rather than silently applied when they'd
  contradict what's already recorded**: a second, unlinked `termination` on
  an already-ended line (correct it with `supersedes` instead), a
  `media_switch` whose `media_from` doesn't match the line's actual
  `current_media`, or a `media_switch` on a line whose `mode` isn't
  `"switch"` (its own registry description is explicit: "for a switch-mode
  line" — there is no way to change a line's `mode` itself after creation,
  via this API or any documented event type).
  `hardware_swap` NOW relocates a line when `params` carries BOTH
  `new_unit` and `new_vial` (registered 2026-09-01, ISSUE_002) — `line.unit`/
  `vial` move to the named destination, reported as `touched: ["unit",
  "vial"]`. Neither field, or only one, leaves `line.unit`/`vial` untouched
  exactly as before (a `hardware_swap` about calibration/pump/IP state with
  no physical move is unaffected). Rejected outright (422/409), nothing
  applied: `new_unit` naming anything other than a real `hardware.units`
  entry; `new_vial` outside 0–15; a destination already held by a
  *different* active line; or optional `previous_unit`/`previous_vial`
  that don't match the line's actual current position (mirrors
  `media_switch`'s own `media_from` check exactly). **`line_id` is NEVER
  changed by a relocation, same-unit or cross-unit** — it is a permanent
  label, not a live description of where a culture currently sits
  (LOG_PROTOCOL.md §4). A relocated line's id can end up visibly
  mismatched with its own `line.unit` (e.g. `patrick-v09` now reading
  `line.unit: "plankton"`) — this is the accepted, documented tradeoff:
  do NOT mint a new line id to "fix" a stale-looking label; that would
  fabricate a branch/restart that never actually happened, in append-only
  history that cannot take it back once committed.
  `hardware_swap` also accepts `params.vacate: true` (ISSUE_002 follow-up,
  "not on evolver") — the line's culture came OFF the evolver entirely
  (pelleted, discarded, or simply between sleeves), not relocated to
  another position: `line.unit`/`vial` both move to `null` together, a
  real named state this schema treats the same way it already treats
  e.g. `pg_regime.current` being null-by-design, never a gap to fill in.
  `vacate` is **mutually exclusive with `new_unit`/`new_vial` in the same
  event** — combining them is rejected (422). This is specifically how a
  **reciprocal swap** between two (or more) lines trading physical
  positions gets logged, since relocating both directly would need each
  destination to still look empty while it's actually held by the OTHER
  line that hasn't moved yet: vacate every line first (each its own
  event, `line.unit`/`vial` become `null`), THEN relocate every line to
  its real new position (each its own ordinary `new_unit`+`new_vial`
  event) — no line ever needs a destination that's still occupied,
  because nothing occupies a `null` position. Re-vacating an
  already-off-evolver line is accepted, not a 409 (`touched: []`,
  same "confirming unchanged state isn't a contradiction" precedent
  `media_switch`'s own `media_from` match already sets) — only a
  *mismatched* `previous_unit`/`previous_vial` is refused.
  And for `level_reading`/`media_prep`, `POST /events` projects that
  reservoir's own `current_volume`/`level_as_of`/`level_source`/
  `level_qualifier` (and, for `media_prep`, closes the outgoing bottle into
  `fill_history`, and moves `pg` too if `params.pg_concentration` was
  supplied -- the ONLY way to correct a reservoir's own displayed
  concentration if it's ever wrong; log a corrective `media_prep` carrying
  `supersedes`, same as any other correction). **`params.reservoir_id` on a
  `media_prep`/`level_reading` REQUIRES the matching volume field
  (`volume_prepared`/`volume_remaining`) -- this is refused (422), not a
  soft no-op**: a real outage was caused by two events naming a reservoir
  with no volume (`GET /media` crashed for every caller on
  `tools/media.py`'s own unconditional read of that field); a
  concentration-only correction belongs on a `supersedes` of the event that
  actually set the volume, not a new one with none at all.
  `reservoir_retired` moves
  ONLY `status` to `"retired"` -- it does not touch
  `current_volume`/`lines_fed`/`fill_history`, which can make a
  just-retired reservoir look, at a glance, like it's still actively
  supplying lines. All of this is reported in the same write, exactly what
  was touched/left untouched, in the response's `reservoir_projection`.
  `reservoir_swap` and `reservoir_change` are explicitly NOT auto-projected
  (both are multi-position shapes -- `reservoir_ids`/`volume_to_<unit>`, or
  `reservoir_id_from`/`_to` as arrays -- ambiguous enough that guessing
  risked exactly the "confident wrong number" this log exists to prevent;
  both need a direct edit).
  Fields like `reservoirs[].lines_fed` or
  `hardware.units.*.vials_in_use`/`n_lines` are not recomputed by this
  server yet and can go stale exactly as they already can by hand. Don't
  infer freshness of a field from a successful write elsewhere -- check the
  write's own response instead.
- **No bulk operations.** One event or one line-creation per call.
- **`POST /lines` cannot express "this is a bookkeeping correction, not a
  real biological event."** Every field a restart/branch/split/merge
  produces (`status`, `lineage.*`, `provenance`, `event_type`) sits in
  exactly the same value-space a genuine event does — there is no
  "synthetic"/"reconciliation" marker anywhere. If a human asks you to "fix"
  something that maps onto a human-only log-maintenance procedure (e.g.
  reconciling generation numbers after a controller restart — LOG_PROTOCOL.md
  §10a, explicitly reserved for humans), do not use `POST /lines` to
  manufacture a line that didn't really happen biologically just to patch a
  bookkeeping mismatch — it would create a permanent, indistinguishable-
  from-real fabrication in lineage history. Tell the human this needs a
  direct edit to `evolution_log.json`, not an API call. **This is exactly
  why `restart`'s `predecessor_line_id`/`lineage.occupies_vial_of` stay
  hardware-only, never ancestry**: a `restart` genuinely founding a new
  culture (with `founding_event.params.source_culture` narrating where the
  material came from, if anywhere) records something real that actually
  happened. Naming `predecessor_line_id` for a line it did NOT physically
  replace, purely to make lineage LOOK more connected or to backfill a
  relationship `branch`/`split`/`merge` refused because the real source had
  already ended, would be precisely this fabrication — a synthetic
  ancestry claim with no marker distinguishing it from a real one, in
  history that cannot take it back.
- **A merge's parents must be distinct.** `parent_line_ids` naming the same
  line twice is rejected (422) — a line cannot merge with itself.
"""

_ERRORS = """\
## What the errors mean

- **401**: missing or unrecognised bearer token.
- **404**: something you named (a line, a target) doesn't exist.
- **409**: the write **cannot succeed no matter what you change in the
  request body** — e.g. a destination vial is already occupied, a branch/
  split/merge parent has already ended, or the line/reservoir named already
  moved. `POST /events` also uses 409 for a `termination`/`media_switch`/
  `hardware_swap` whose content contradicts the line's already-recorded
  state (a second, unlinked termination on an already-ended line; a
  `media_switch` whose `media_from` doesn't match `current_media`, or
  whose target line's `mode` isn't `"switch"`; a relocating or vacating
  `hardware_swap` whose destination is already held by a different active
  line, or whose `previous_unit`/`previous_vial` don't match the line's
  actual current position) — retry with `supersedes` (termination), the
  correct `media_from`/`previous_unit`/`previous_vial`, or the correct
  line, don't just resend the same body. A `hardware_swap` combining
  `vacate: true` with `new_unit`/`new_vial` in the same event is a 422,
  not 409 — that's a malformed request, not a conflict with recorded
  state; vacate and relocate as two separate events instead.
  `POST /parameter_registry` uses 409 for the
  one case that's
  genuinely about identity, not content: the `key` you named already has
  an entry — retry with a different, actually-new key name; this route
  cannot change or retire the existing one (see "Registering a new
  `params` key" above).
- **422**: the request parsed, but either the resulting log would fail
  validation, or the request is missing something THIS operation needs
  given the current state (fixable by adding it, not a dead end): an
  unregistered `params` key, an undeclared `event_type`, a dangling
  `caused_by_event`/`supersedes` reference, a malformed id, a timestamp
  more than an hour in the future, a `restart` whose named predecessor
  is still active and needs `predecessor_termination` supplied, or a
  `merge` naming the same `parent_line_ids` entry twice (a line can't
  merge with itself). The
  dangling-reference check applies everywhere a new event is minted, not
  just `POST /events` — `POST /lines`' founding and termination events are
  checked the same way. It checks only that the referenced event_id
  EXISTS, not that it's a sensible antecedent for this line — a
  `caused_by_event`/`supersedes` pointing at a real event belonging to a
  completely unrelated line is accepted; omit rather than guess a causal
  link you're not sure of, since nothing else will catch a wrong one. The
  response body names each specific problem; fix all of them and retry.
- **500**: two different causes, not one — read the `detail` before deciding
  whether to retry.
  - The file write succeeded but the git commit failed: the server has
    already reverted the file to match git history, nothing was lost, safe
    to retry as-is.
  - The server has no operator tokens configured at all (`secrets/
    operators.json` missing or malformed) — `detail` says so explicitly.
    This is **not** transient: retrying the identical request will 500
    again every time until a human sets up that file. Tell the operator,
    don't loop.
"""


_PATH_CONVERTER_RE = re.compile(r"\{(\w+):\w+\}")


def _display_path(path: str) -> str:
    """Strip Starlette's path-converter syntax ({name:converter}) down to
    the plain placeholder ({name}) a caller should actually substitute --
    the ":path"/":int" part is routing machinery, not something to type."""
    return _PATH_CONVERTER_RE.sub(r"{\1}", path)


def _looks_like_api_route(route: object) -> bool:
    """Duck-typed stand-in for isinstance(route, APIRoute) -- a leaf route
    this module can actually introspect (reads path/methods/endpoint/
    dependant/summary), as opposed to a router-level wrapper that has to be
    expanded first (see _flatten_routes)."""
    return (hasattr(route, "path") and hasattr(route, "methods")
            and hasattr(route, "endpoint") and hasattr(route, "dependant"))


def _flatten_routes(routes) -> list:
    """Expand app.routes into real, introspectable leaf routes, recursively.

    Found on a real deployment, reproduced locally only after installing
    that deployment's EXACT fastapi/starlette versions (0.141.1/1.6.0) --
    not a duplicate-install or class-identity problem as first suspected
    (that theory didn't survive contact with a matching-version repro; see
    git history for the abandoned fix it replaced). The real cause: as of
    some fastapi version between this server's dev version (0.136.3) and
    0.141.1, `app.include_router()` stopped flattening the included
    router's routes into `app.routes` at include time. Instead, `app.routes`
    now holds a handful of framework built-ins (`/docs`, `/openapi.json`,
    ...) as plain `starlette.routing.Route`, plus one opaque
    `fastapi.routing._IncludedRouter` wrapper PER `include_router()` call in
    app/main.py -- and the real `APIRoute`s this module needs are nested
    inside each wrapper's `.original_router.routes`, not present in
    `app.routes` at all. Neither the original `isinstance` check nor the
    duck-typed one that replaced it ever looked inside these wrappers, so
    both saw zero real routes and degraded (gracefully, not a crash) to an
    empty "## Routes" table and "unable to introspect" everywhere else --
    on every single request, not intermittently.

    Handles both shapes without depending on which fastapi version is
    running: a leaf route is kept as-is; anything else is expanded via
    whichever of `.original_router.routes` (the new wrapper) or `.routes`
    (a generic Starlette `Mount`/sub-router, any version) it actually has,
    recursively -- so this survives a FUTURE structural change too, as long
    as whatever wraps a sub-router keeps exposing its children under one of
    those two names."""
    out = []
    for r in routes:
        if _looks_like_api_route(r):
            # A route hidden from the OpenAPI schema (the /viewer pages, for
            # people rather than LLM clients) is hidden from the skill too.
            if getattr(r, "include_in_schema", True):
                out.append(r)
            continue
        nested = getattr(r, "original_router", None) or r
        sub_routes = getattr(nested, "routes", None)
        if sub_routes:
            out.extend(_flatten_routes(sub_routes))
    return out


def _route_table(app: FastAPI) -> str:
    rows = []
    seen = set()
    for route in _flatten_routes(app.routes):
        doc = (route.endpoint.__doc__ or "").strip()
        summary = route.summary or (doc.splitlines()[0] if doc else "")
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            key = (method, route.path)
            if key in seen:
                continue
            seen.add(key)
            rows.append((_display_path(route.path), method, summary))
    rows.sort()
    lines = ["| Method | Path | What it does |", "|---|---|---|"]
    lines += ["| %s | `%s` | %s |" % (method, path, summary) for path, method, summary in rows]
    return "\n".join(lines)


_FIELD_DESCRIPTIONS = {
    # request-only fields with no 1:1 schema counterpart
    "target": "Exactly one of `line_id` (append to that line) or "
              "`scope: \"facility\"` (append to experiment_events).",
    "params": "Open by design. Every key must already be in `parameter_registry` "
              "(see below) -- new keys need registering there first, not invented ad hoc.",
    "notes": "Required prose: what happened, why, and what it implies. This is where "
             "reasoning goes -- params holds structured facts, notes holds judgment.",
    "new_line": "The line being created -- see the shared new-line fields below.",
    "new_lines": "The split's children (at least 2) -- each one a new-line object, see below.",
    "parent_line_id": "Must already exist and be active. No exception for an already-ended "
                      "source -- there is deliberately no mode for that; see restart's own "
                      "note below for why, and for source_culture as the narrative alternative.",
    "parent_line_ids": "Must already exist; every one must be active when this call arrives. "
                       "Same no-exception-for-ended-parents rule as branch's parent_line_id.",
    "predecessor_line_id": "Omit entirely for a true day-one founder (no predecessor at all). "
                           "When given, must already exist.",
    "predecessor_termination": "Required if predecessor_line_id is active when this arrives; "
                                "must be omitted if it's already ended.",
    "parent_termination": "Required -- a split always ends its parent.",
    "parent_terminations": "Keyed by parent_line_id, only for parents that END. At least one "
                            "required: the vial the new line occupies must belong to an ending "
                            "parent. A parent named here but not required to end elsewhere "
                            "continues running independently.",
    "line_id": "Ignored and server-derived for branch/split/restart. REQUIRED for merge -- "
               "supplied by you, only validated (not invented) against parent_line_ids.",
    "founding_event": "The one event that becomes this line's first (or, for a termination, "
                       "its ending) event.",
    "caused_by_event": "The event, if any, whose finding directly triggered this one -- e.g. "
                        "an anomaly's diagnosis prompting an intervention. Optional; omit rather "
                        "than guess a causal link that isn't actually known.",
    "elapsed_h": "Hours since this line's own t0, if you want to record it explicitly -- though "
                 "real precedent shows it's sometimes reckoned from a shared/facility reference "
                 "point instead (a batch of events logged across several lines at once may all "
                 "carry the same elapsed_h despite different t0s), so it is NOT cross-checked "
                 "against timestamp - t0. It must never be negative, whatever it's measured from "
                 "-- that IS rejected (422). Optional -- most events omit it.",
    "event_type": "Any string. New kinds are absorbed by using a new value here, not by asking "
                  "for a schema change -- see the known event_types below, but a genuinely new "
                  "one is legitimate.",
}


def _resolve_description(prop: dict, schema: dict) -> str | None:
    """A property's own description if it has one, else the description on
    whatever $def it $refs (e.g. "timestamp" is just {"$ref": "#/$defs/
    timestamp"} with no sibling description -- the real text lives on the
    $def itself)."""
    if prop.get("description"):
        return prop["description"]
    ref = prop.get("$ref", "")
    if ref.startswith("#/$defs/"):
        return schema.get("$defs", {}).get(ref.removeprefix("#/$defs/"), {}).get("description")
    return None


def _schema_field_descriptions(schema: dict) -> dict[str, str]:
    event_props = schema.get("$defs", {}).get("event", {}).get("properties", {})
    out = {}
    for key in ("timestamp", "event_type", "provenance", "missing_fields",
                "caused_by_event", "supersedes", "source_document", "elapsed_h",
                "timestamp_precision"):
        desc = _resolve_description(event_props.get(key, {}), schema)
        if desc:
            out[key] = desc
    return out


# Fields whose name alone (the section header already states begin_mode, and
# the shared new_line/founding_event fields have their own dedicated section)
# says everything there is to say -- rendering "(optional)" under each of the
# four begin_mode sections adds noise, not information.
_SKIP_FIELDS = {"begin_mode"}


def _render_fields(model_cls, schema_desc: dict[str, str]) -> str:
    lines = []
    for name, field in model_cls.model_fields.items():
        if name in _SKIP_FIELDS:
            continue
        required = "required" if field.is_required() else "optional"
        # schema descriptions win when both exist -- that's the actual "one
        # definition" (schema/evolution_log.schema.json); _FIELD_DESCRIPTIONS
        # is only a fallback for request-only fields with no schema counterpart.
        desc = schema_desc.get(name) or _FIELD_DESCRIPTIONS.get(name) or ""
        lines.append("- **`%s`** (%s)%s" % (name, required, (" — " + desc) if desc else ""))
    return "\n".join(lines)


def _route_by_path(app: FastAPI, path: str, method: str = "GET") -> APIRoute | None:
    for route in _flatten_routes(app.routes):
        if route.path == path and method in route.methods:
            return route
    return None


def _query_param_names(app: FastAPI, path: str) -> str:
    """Comma-joined, backtick-wrapped query param names for a route -- e.g.
    for the "filters: ..." one-liners below. Route lookup can fail (see
    _route_by_path/_render_query_params); never let that crash the whole
    /skill response over one missing detail in one line -- a caller losing
    this one sentence is a far smaller failure than a 500 on the entire
    route, which happened in production once already (a bare
    `_route_by_path(...).dependant` with no None-check, before this
    helper existed)."""
    route = _route_by_path(app, path)
    if route is None:
        return "_(unable to introspect -- route not found)_"
    return ", ".join("`%s`" % p.name for p in route.dependant.query_params)


def _render_query_params(app: FastAPI, path: str) -> str:
    """Introspects a route's actual Query(...) parameters -- the same
    descriptions already written in app/routes/*.py, not retyped here.
    Unlike _render_fields (Pydantic model_fields), plain query params on a
    GET route have no BaseModel to introspect; this walks FastAPI's own
    dependant.query_params instead."""
    route = _route_by_path(app, path)
    if route is None:
        return "_route not found_"
    lines = []
    for p in route.dependant.query_params:
        required = "required" if p.field_info.default is ... else "optional"
        desc = getattr(p.field_info, "description", None) or ""
        lines.append("- **`%s`** (%s)%s" % (p.name, required, (" — " + desc) if desc else ""))
    return "\n".join(lines) if lines else "_no query parameters_"


def _render_event_types(log: dict) -> str:
    types = log.get("event_types", {})
    if not types:
        return "_None declared yet._"
    return "\n".join("- **`%s`** — %s" % (k, v) for k, v in sorted(types.items()))


def _render_registry(log: dict, event_type: str | None) -> str:
    reg = log.get("parameter_registry", {})
    lines = []
    for key, entry in sorted(reg.items()):
        applies = entry.get("applies_to_event_types")
        if event_type and applies and event_type not in applies:
            continue
        vtype = entry.get("type", "?")
        type_str = " or ".join(vtype) if isinstance(vtype, list) else vtype
        if entry.get("items") and "array" in (vtype if isinstance(vtype, list) else [vtype]):
            type_str = type_str.replace("array", "array of %s" % entry["items"])
        if entry.get("enum"):
            type_str += "; one of: %s" % ", ".join(repr(v) for v in entry["enum"])
        status = entry.get("status")
        # "retired" used to be silently dropped from this list entirely --
        # with zero indication anywhere that a key had existed and been
        # withdrawn. Shown instead, tagged: retired status is a convention
        # only, not enforced (tools/lineage.py's registered-params check is
        # membership-only, `if reg and k not in reg` -- it does not look at
        # status at all), so a foreign model needs to be told not to use it,
        # not left to discover the schema would silently accept it anyway.
        if status == "planned":
            tag = " _[planned, not yet used]_"
        elif status == "retired":
            tag = " _[retired -- do not use for new events; not blocked by validation, convention only]_"
        else:
            tag = ""
        lines.append("- **`%s`** (%s)%s — %s" % (key, type_str, tag, entry.get("description", "")))
    if not lines:
        return "_None registered%s._" % (" for event_type=%r" % event_type if event_type else "")
    return "\n".join(lines)


def _render_header(log: dict) -> str:
    name, title = experiment_identity(log)
    units = sorted((log.get("hardware") or {}).get("units") or {})
    about = ": %s" % title if title else ""
    if units:
        about += " (%d eVOLVER unit%s: %s)" % (
            len(units), "" if len(units) == 1 else "s", ", ".join("`%s`" % u for u in units))
    return _HEADER.replace("@@NAME@@", name).replace("@@ABOUT@@", about)


def render_skill(app: FastAPI, settings: Settings, event_type: str | None = None) -> str:
    log = load_log(settings)
    schema = load_schema(settings)
    schema_desc = _schema_field_descriptions(schema)

    parts = [_render_header(log), _route_table(app), ""]

    parts.append("## Reading the log\n")
    parts.append(
        "- **`GET /health`** — liveness and identity, not log data. Response: "
        "`service`, `status`, `server_time`, `experiment.{name, title}` "
        "(which experiment this log records -- check it before writing), "
        "`log_repo_path`, `log_repo_head` "
        "(+ `log_repo_head_error`), `schema_version`, "
        "`log_meta.{last_updated, event_counter}`, `n_lines`, `n_reservoirs`, "
        "`writes_supported`, `auth_configured`. `writes_supported: true` "
        "means this server build *has* the write routes -- it is not a "
        "promise a write will succeed right now. `auth_configured` is the "
        "field that says whether operator tokens exist; `writes_supported: "
        "true` with `auth_configured: false` is not a contradiction, it "
        "means `POST /events`/`POST /lines` will 500 until tokens are set up."
    )
    parts.append(
        "- **`GET /lines`** — filters: %s\n  Response: `{count, lines: "
        "[summary, ...]}`. Each summary is a REDUCED projection -- "
        "`line_id, unit, vial, strain, status, mode, current_media, "
        "media_switch_count, pg_low, pg_high, is_founder, parents, children, "
        "depth` -- not the "
        "full object (no `events`, `t0`, `reservoirs`, `initial_media`, "
        "`group`, `replicate`). Use `GET /lines/{line_id}` for the full "
        "object, its entire `events[]` included. **Percent-encode `#` and "
        "`+` in a line_id used in a URL path** (e.g. `patrick-v05#2` -> "
        "`patrick-v05%%232`) -- an unencoded `#` is a URL FRAGMENT and never "
        "reaches the server at all, so `GET /lines/patrick-v05#2` silently "
        "asks for `patrick-v05` instead (a different, real line -- a "
        "confident 200 for the WRONG resource, not a 404). `+` does not "
        "need encoding in a path segment, only in a query string, but "
        "encoding it too is always safe."
        % _query_param_names(app, "/lines")
    )
    parts.append(
        "- **`GET /reservoirs`** — filters: %s\n  Response: `{count, "
        "reservoirs: [...]}`, full reservoir objects (`id, unit, media, "
        "role, pg, status, volume_prepared, prepared_at, current_volume, "
        "level_as_of, level_source, lines_fed`), not a reduced projection."
        % _query_param_names(app, "/reservoirs")
    )
    parts.append(
        "- **`GET /events`** — filters: %s. `limit` defaults to 50 and caps "
        "at 1000 -- always check `total_matching` against `count`, not just "
        "`count` alone, or a result silently truncated at the default page "
        "size reads as complete when it isn't. Sorted by **timestamp, never "
        "`event_id`** (`event_id` order is not chronological -- corrections "
        "are appended later carrying the timestamp of what they correct)."
        " Response: `{count, total_matching, events}`. `GET "
        "/events/{event_id}` additionally carries a computed `superseded_by` "
        "key (a list of event_ids, possibly empty) -- every event whose OWN "
        "`supersedes` names this one. `supersedes` itself is a bare, "
        "one-way pointer stored only on the correcting event, with no "
        "aggregate view anywhere else; `superseded_by` is the only way to "
        "ask \"has this event been corrected, and by what\" without fetching "
        "and scanning every event on the line by hand. More than one entry "
        "means two different events independently claim to correct this "
        "one -- not itself an error (a plausible outcome of two operators "
        "correcting the same thing without seeing each other's write "
        "first), but worth a human's attention if you see it."
        % _query_param_names(app, "/events")
    )
    parts.append(
        "- **`GET /vials/{unit}/{vial}`** — a hardware position, not a "
        "culture (LOG_PROTOCOL.md §4: line identity follows the culture, "
        "not the hardware). Returns only whichever line CURRENTLY occupies "
        "that vial, if any (`occupied_by`, `line`; both `null` if nothing "
        "active is there) -- never history. A vial can have held several "
        "different lines over time; reach those through `GET /lines` "
        "filtered by `unit`/`vial`, or `lineage.occupies_vial_of` on the "
        "current occupant, not through this route. **`occupies_vial_of` is "
        "hardware continuity, not ancestry** -- it names whichever line "
        "PHYSICALLY held this vial immediately before, never a biological "
        "parent; a line's real ancestors, if any, live in `lineage.parents` "
        "instead (see `POST /lines`' `restart` note below for the full "
        "distinction and why the two are never the same thing).\n"
    )

    parts.append("## `POST /events` — append a single event to an existing line, or to "
                 "experiment_events\n")
    parts.append(_render_fields(NewEventRequest, schema_desc))
    parts.append(
        "\nResponse: the created event object itself, with the server-"
        "assigned `event_id` and `operator` filled in. A facility-scoped "
        "event additionally carries `\"scope\": \"facility\"` in the "
        "response (and on disk); a line-scoped event carries no `scope` "
        "key at all -- its presence or absence IS the signal, not a value "
        "to check. The response also carries a `reservoir_projection` key, "
        "present only for `level_reading`/`media_prep`/`reservoir_retired`/"
        "`reservoir_swap`/`reservoir_change` events (`null` for everything "
        "else): `{\"projected\": true, \"touched\": [...], \"untouched\": "
        "[...]}` on success -- a `media_prep` that brought a brand-new "
        "reservoir online also carries `\"created\": true`, and one "
        "against an EXISTING retired reservoir carries an explanatory "
        "`note` (whether or not `reactivate` moved `status` -- see \"what "
        "this API cannot do\") -- or `{\"projected\": false, \"reason\": "
        "\"...\"}` when nothing was moved at all (an older-than-current "
        "reading, an unknown reservoir_id whose media_prep is missing one "
        "of the four fields needed to create it, a `reservoir_retired` on "
        "an already-retired reservoir, a reservoir_swap/reservoir_change -- "
        "both explicitly out of scope here, see \"what this API cannot "
        "do\" -- or a params field the event didn't supply) -- check this "
        "rather than assuming the write "
        "moved reservoir state. Similarly, a `line_lifecycle_projection` "
        "key is present only for `termination`/`media_switch`/"
        "`hardware_swap` events (`null` otherwise): `{\"applied\": true}`, "
        "optionally with a `reason` string -- for `termination`, only on a "
        "supersedes-driven correction; for `hardware_swap`, ALWAYS (a plain "
        "relocation carries none, but every vacate does, whether real or "
        "an idempotent re-confirmation of an already-off-evolver line -- "
        "check `touched` to tell those two apart, not the presence of "
        "`reason` itself). `hardware_swap` also always carries a `touched` "
        "list -- `[\"unit\", \"vial\"]` for a real relocation or a real "
        "vacate, `[]` for a vacate that only re-confirmed an already-off-"
        "evolver line), "
        "or `{\"applied\": false, \"reason\": \"...\"}` (the event was "
        "facility-scoped and names no line; media_switch's `media_to` was "
        "missing; or a `hardware_swap` supplied no structured destination "
        "at all, or only one of `new_unit`/`new_vial` -- never invented). "
        "NOTE: a termination on an already-ended line with no matching "
        "`supersedes`, a media_switch whose `media_from` contradicts the "
        "line's actual `current_media`, or a `hardware_swap` relocating "
        "onto an already-occupied vial (or whose `previous_unit`/"
        "`previous_vial` don't match) are NOT reported this way -- all "
        "are refused outright with 409 before anything is written; see "
        "\"what the errors mean\".\n"
    )

    parts.append("## `POST /lines` — create a new line (branch / split / merge / restart)\n")
    parts.append("Every request has `begin_mode` plus mode-specific fields. Fields shared "
                 "across all four (`new_line`/`new_lines`, `founding_event`, and the concentration/"
                 "quantity/reservoir sub-shapes) are not repeated per mode below.\n")
    for label, cls in (("branch", BranchRequest), ("split", SplitRequest),
                       ("merge", MergeRequest), ("restart", RestartRequest)):
        parts.append("**`begin_mode: %s`**" % label)
        parts.append(_render_fields(cls, schema_desc))
        parts.append("")

    parts.append(
        "**`restart`'s `predecessor_line_id` is hardware continuity, never "
        "descent.** Found genuinely ambiguous by an operator seeding new "
        "lineages from a subset of already-terminated ones: naming "
        "`predecessor_line_id` sets `lineage.occupies_vial_of` and "
        "`params.predecessor_in_vial` -- both narrate the SAME PHYSICAL "
        "VIAL's history, nothing about the new culture's biological origin. "
        "`lineage.parents` stays `[]` and `is_founder` stays `true` "
        "REGARDLESS -- LOG_PROTOCOL.md states this as an absolute (\"a "
        "restart is not descent\"), not a limitation to work around. "
        "`branch`/`split`/`merge` are the ONLY three modes that create a "
        "real `lineage.parents` edge, and all three require the named "
        "parent(s) to be ACTIVE at call time (409 otherwise) -- there is "
        "NO mode that creates a `parents` edge from an already-ended "
        "lineage, on purpose: descent means a continuously-growing "
        "population handed off; once a line has ended, there is no "
        "population left to biologically continue, only material that can "
        "be described. To record THAT (a new culture inoculated from 1 mL "
        "of a specific prior lineage, active or already ended, in the same "
        "vial or a different one) use `founding_event.params.source_culture` "
        "instead -- free text, registered for `inoculation`, already used "
        "this way ~20 times in the real log (e.g. `\"1 mL of "
        "patrick-v05#2\"`). **`source_culture` is DESCRIPTIVE PROVENANCE, "
        "not biological ancestry either** -- naming a prior line's id "
        "inside its free text never creates any `lineage.parents` edge or "
        "any other structured link back to that line; it is prose a human "
        "or a future reader can follow, exactly like `notes`, not a graph "
        "relationship the server tracks. It is independent of "
        "`predecessor_line_id`: use one, the other, both, or neither, "
        "depending what actually happened. **Note also** (a separate, "
        "still-open gap, "
        "`issues/ISSUE_003.md`): `predecessor_line_id` naming a line NOT "
        "actually at the new destination vial is currently accepted "
        "without complaint, despite its own description saying it must be "
        "-- don't rely on that going unchecked forever.\n"
    )

    parts.append("### `new_line` / `new_lines[]` shared fields\n")
    parts.append(
        "- **`unit`**, **`vial`**, **`strain`**, **`initial_media`**, **`current_media`**, "
        "**`mode`** (`constant`|`switch`), **`t0`**\n"
        "- **`pg_regime`**: `{low, high, effective_from}` — `low`/`high` are concentration "
        "objects `{value_g_per_L, value_mM, unit_primary: \"g/L\"}`, both required, "
        "computed consistently (MW 126.11)\n"
        "- **`reservoirs`**: `{low, high}` — existing reservoir ids (see `GET /reservoirs`)\n"
        "- **`founding_event`**: same shape as a `POST /events` body minus `target` and "
        "`supersedes` (a founding/termination event doesn't correct a prior one)\n"
        "- optional: **`replicate`**, **`group`**\n"
    )
    parts.append(
        "Response: `{new_line_ids, lines, terminated_line_ids}`. "
        "`new_line_ids` is how you learn the server-derived `line_id` for "
        "branch/split/restart (you did not choose it, except for merge) -- "
        "`lines` maps each of those ids to its full created object. "
        "`terminated_line_ids` names whichever existing line(s) this call "
        "ended, if any (always populated for split; conditional for merge/"
        "restart; always empty for branch).\n"
    )

    parts.append("## `GET /media` — consumption rates and depletion projections\n")
    parts.append(
        "Read-only, no auth. Every rate and forecast comes from the log repo's "
        "own `tools/media.py`; this route only shapes it for JSON. Query "
        "parameters:\n"
    )
    parts.append(_render_query_params(app, "/media"))
    parts.append(
        "\nResponse: `{at, skipped_events, reservoirs, per_line, "
        "delivered_pg, high_outlook, attention, pump}`. `skipped_events` names any "
        "media_prep/level_reading event that named a reservoir but had no "
        "volume field to read (a real, historical production incident, not "
        "hypothetical) -- normally empty; a non-empty list means exactly "
        "those event_ids contributed nothing to the reservoir rows below, "
        "not that anything crashed or was silently dropped from the log "
        "itself (`GET /events` still returns them verbatim). `reservoirs` "
        "is every reservoir's row, passed through "
        "whole -- no trimmed FIELDS, though the LIST itself is narrowed by "
        "`unit`, `status` and `pump=only`, and can be empty while `attention` "
        "is not. `per_line` is per-line rates as "
        "`[{media, role, rate_L_per_h}]`. `delivered_pg` is the mean PG "
        "concentration actually delivered per unit/media group, with a "
        "`direction` (`about`/`at_least`/`at_most`/`indeterminate`) when a "
        "bound rather than a point estimate is all the data supports. "
        "`high_outlook` projects high-reservoir demand forward as the ramp "
        "climbs. `attention` is active reservoirs with a known depletion "
        "time, soonest first.\n\n"
        "Every reservoir row carries **`rate_basis`** "
        "(`measured`/`prior_bottle`/`inferred`/`upper_bound`/`unknown`) plus "
        "`rate_is_upper_bound`, `rate_provisional`, and `baseline_orphaned` — "
        "see the hard rule on this below before presenting any figure from "
        "this route.\n\n"
        "**Two different measurements live in this response.** Everything "
        "listed above comes from hand-entered bottle level readings, and "
        "answers for any `at`. Each active row may ALSO carry a `pump` block, "
        "which is what the eVOLVER itself dispensed since that bottle's last "
        "reading — a measurement where the level-derived `estimated_now_L` is "
        "an extrapolation. `pump.basis` is either `pump_integrated` (a real "
        "measurement: `drawn_L`, `estimated_now_L`, `rate_L_per_h`, its own "
        "`projection`) or `unavailable` with a `reason` naming exactly why. "
        "The top-level `pump` object reports `mode`, per-unit `sources` (each "
        "with a `fetches` list, one per distinct reading instant consulted), "
        "and the `measured`/`unavailable` reservoir ids.\n\n"
        "A measured block also carries: `drawn_L` and the `since` it was "
        "measured from, `window_h`, `n_events` (with "
        "`n_events_is_role_specific` saying whether that count is this "
        "reservoir's own role or the vial's total -- when false, the low and "
        "high bottles of one group report the SAME number and it is not this "
        "bottle's dilution count), `lines_counted` and "
        "`vials_counted`, `source` (`consumption`, or `dispenses` when the rig "
        "is too old for the newer endpoint), `overdrawn`, `quiet_vials` and "
        "`quiet_note`, and the comparison fields below.\n\n"
        "**Modes.** `?pump=off` contacts no rig at all and attaches NO `pump` "
        "block to any row — the top-level object says so in its `note`. "
        "`?pump=only` filters the `reservoirs` list to those with a "
        "measurement; `per_line`, `delivered_pg`, `high_outlook` and "
        "`attention` are always computed from the full set, so a rig outage "
        "can never change a level-derived figure.\n\n"
        "**`at` and `pump` interact.** The rigs report the present and only "
        "the present, so an `at` more than five minutes from now returns no "
        "pump measurement — the reproducibility `at` gives you and the live "
        "measurement are mutually exclusive, by construction. Each row says so "
        "in its own `reason`.\n\n"
        "**The comparison fields.** `predicted_drawn_L` is what the "
        "level-derived rate expects over the same window. "
        "`divergence_comparable` says whether comparing them means anything: "
        "when the last reading is old enough that the extrapolation predicts "
        "more than the bottle held, the difference measures the extrapolation "
        "rather than the plumbing, `divergence_L` is `null`, and "
        "`divergence_skipped_because` explains it. Only when it is `true` do "
        "`divergence_L` and possibly `divergence_note` appear.\n\n"
        "**`drawn_is_upper_bound`.** True when the rig had no "
        "`/api/v1/consumption` and the window had to be resolved against its "
        "last logged event instead of its clock. The draw is then AT MOST the "
        "figure shown and the remaining volume AT LEAST it; `bound_note` says "
        "so. Never quote such a figure as exact."
    )

    parts.append("## `GET /pump_events` — every dispense in the last N hours\n")
    parts.append(
        "Read-only, no auth. Asks each rig for its raw pump log over a window "
        "ending NOW, and labels every dispense with the culture it went into and "
        "the bottle it came from. Use it to answer \"what did the pumps actually "
        "do\" -- per vial, per line, per bottle. For \"how much is left in a "
        "bottle\", use `GET /media`, which already folds the pumps into that. "
        "Query parameters (`window_h` defaults to %g; `vial` is 0-15 and needs "
        "`unit`):\n" % DEFAULT_WINDOW_H
    )
    parts.append(_render_query_params(app, "/pump_events"))
    parts.append(
        "Response: `{window, scope, event_fields, complete, incomplete_because?, "
        "units, totals_by_reservoir, totals_by_line, notes}`. "
        "`units.<unit>.vials.<n>.events` is a list of rows whose columns "
        "`event_fields` names: `[at, mL, pump, line_id, reservoir_id]`. `at` is "
        "wall-clock time on this server's clock and offset, converted from the "
        "rig's controller hours; every `at` lies inside `window`. `pump` is `low` "
        "or `high`, or `unrecognised` when the rig sent anything else -- then "
        "`reservoir_id` is null, the vial's `unrecognised_pumps` shows up to five "
        "of the labels the rig used, and that volume is in no bottle's total. Each "
        "vial also carries `total_mL` by pump, `lines` (every line seen there in "
        "the window), and `n_events`. `totals_by_reservoir` and `totals_by_line` "
        "sum the window across the vials IN SCOPE -- a `unit`/`vial` filter "
        "narrows them, and `notes` says so. Totals are summed before rounding "
        "(events to 4 dp, totals to 3 dp), so very small events can read 0.0 "
        "while their total does not.\n\n"
        "**`window_h` is hours back from now, not a rig hour.** The rig's own "
        "`since_h` means controller hour X since its run began; this route converts "
        "between the two. Never pass a value read from a rig's `since_h` or "
        "`elapsed_h` here.\n\n"
        "**`complete` is the first thing to check.** `false` means some part of "
        "the scope was not measured, and `incomplete_because` lists every reason: "
        "a unit not read (`units.<unit>.ok: false`, with `reason` -- never read a "
        "missing unit as zero dispensing), a vial not read (`vials.<n>.ok: false`), "
        "rows the rig sent that were refused (negative, over 100 mL, unusable, "
        "dated after the rig's own clock -- each named in `vial_problems`), or a "
        "rig whose run began inside the window (`window_truncated`, "
        "`run_started_at`: earlier dispenses are in a previous run's log). Every "
        "entry in `totals_by_reservoir` and `totals_by_line` carries the same "
        "`complete` flag; when it is false, every total is a LOWER bound. Two units "
        "pointing at one dashboard URL are both refused rather than counted twice, "
        "and a unit on the rig roster that this log does not know is listed with "
        "`ok: false` and named in `notes`.\n\n"
        "**A vial number is not a culture.** `line_id` is whichever line the LOG "
        "places in that vial at that instant, from t0, its end, and its "
        "`hardware_swap` history -- so a vial that changed hands inside the window "
        "shows both lines, each against its own dispenses. A line's history is used "
        "only if it reconciles with the line's own record of where it is and with "
        "the founding vial its id encodes; superseded swaps and terminations are "
        "ignored. Where the log cannot decide -- no line recorded there, two at "
        "once, a history that contradicts itself, a move that does not say where "
        "it came from, an end the log does not record -- `line_id` and "
        "`reservoir_id` are `null` and the vial's `unattributed` list says why, "
        "with how many events and mL. Report that reason; never assign those "
        "dispenses to the likeliest line. `near_line_change` counts dispenses "
        "within 15 min of a recorded line change in that vial: the log's times are "
        "operator-reported to the minute, so those may belong to the neighbouring "
        "line.\n\n"
        "**The rig's clock.** `clock_exact: false` means the rig does not report a "
        "trustworthy staleness, so its clock is its last write: every `at` may be "
        "LATE by the rig's idle time, and the window opens EARLY by the same "
        "amount -- it can include dispenses older than `window_h` "
        "(`clock_note`). `quiet` on a vial means the rig reported nothing for it in "
        "the window and refused no rows -- a real measurement, and a blocked or "
        "dead line looks exactly like an idle one.\n\n"
        "**Size.** One request fetches a summary plus one call per vial from every "
        "unit; `window_h` is capped at %g. Pass `events=false` for totals only, and "
        "narrow with `unit`/`vial`. A 503 means too many of these are already in "
        "flight -- retry, don't loop.\n" % MAX_WINDOW_H
    )

    parts.append("## Known `event_type`s (live, from this log)\n")
    parts.append(_render_event_types(log))
    parts.append("")

    parts.append("## Registered `params` keys%s\n" %
                 (" for event_type=%r" % event_type if event_type else " (live, from this log)"))
    parts.append(_render_registry(log, event_type))
    parts.append("Pass `?event_type=<name>` to `GET /skill` to filter this list to the keys "
                 "documented as applying to that event type (undocumented applicability is not "
                 "the same as forbidden -- `params` stays open for a genuinely new dimension, "
                 "but register it first rather than inventing an ad hoc key).\n")

    parts.append(
        "## Registering a new `params` key\n\n"
        "If no existing key above fits what you need to record, register a "
        "new one -- do not invent an ad hoc key and use it anyway; an "
        "unregistered `params` key is refused (422) the same as any other "
        "invalid event, by `tools/lineage.py`'s own registered-key check. "
        "Before 2026-09-01 the only way to add one was a direct hand-edit "
        "of `evolution_log.json`, with no validation until AFTER it already "
        "landed -- these three routes replace that:\n\n"
        "- **`GET /parameter_registry?key=<name>`** -- check whether a name "
        "is already taken before proposing it. Omit `key` to see the whole "
        "registry as JSON (the same live data `GET /skill`'s \"Registered "
        "`params` keys\" section above renders as prose).\n"
        "- **`POST /parameter_registry/candidate`** -- validate a candidate "
        "`{\"key\": \"...\", \"entry\": {...}}` WITHOUT registering it. "
        "Always call this first. No auth needed -- it writes nothing.\n"
        "- **`POST /parameter_registry`** -- register it for real. Requires "
        "a bearer token (same as `POST /events`/`POST /lines`) -- it "
        "commits into `evolution_log.json` directly, the SAME repo those "
        "routes write to, not a separate one the way `POST /config` is.\n\n"
        "`entry` must match the schema's own `registryEntry` shape: "
        "`description` (required, non-empty), `status` (required), `type` "
        "(required -- one of the schema's recognized value shapes: "
        "`string`, `number`, `integer`, `boolean`, `array`, `object`, "
        "`quantity` (`{value, unit}`), `concentration` "
        "(`{value_g_per_L, value_mM, unit_primary}`), `eventId`, `lineId`, "
        "`reservoirId`, or `any`; a 2-item array of two of these means the "
        "value may legitimately be either one), plus optional `unit`, "
        "`enum`, `items` (element type, only when `type` includes `array`), "
        "`applies_to_event_types`, and `note`. Getting any of this wrong "
        "surfaces as an ordinary `422` from `POST /parameter_registry/"
        "candidate` -- the exact same schema check the real write is judged "
        "against, so a candidate that validates WILL be accepted for real.\n\n"
        "**A brand-new key may only be registered as `status: \"planned\"` "
        "(with `first_seen: null`) or `status: \"active\"` (with "
        "`first_seen` naming a REAL, already-existing event whose own "
        "`params` already contains this key).** `status: \"retired\"` "
        "describes something that already existed and stopped being used "
        "-- not a state a brand-new key can start in, and this route "
        "refuses it outright. Register ahead of use as `planned` if you "
        "don't have a real event yet -- the normal case, matching most "
        "already-`planned` entries in this log (`dilution_rate`, "
        "`controller_version`, `od_calibration`) -- and use `active` only "
        "when an event genuinely already uses it (e.g. one a human logged "
        "by directly editing the file, before registering the key it used "
        "properly).\n\n"
        "**This route only ADDS a brand-new key. It cannot change or "
        "retire an EXISTING entry** (a real key already present in the "
        "registry above, even to fix a typo in its own description, or to "
        "flip a `planned` entry to `active` once something finally uses "
        "it) -- that is a different operation, not built yet. Tell the "
        "human operator it needs a direct edit to `evolution_log.json`, "
        "the same way reservoirs/hardware/design already do (see \"what "
        "this API cannot do\" below).\n"
    )

    parts.append(_HARD_RULES.format(
        timestamp_desc=schema_desc.get("timestamp", "ISO 8601 with an explicit, colon-separated "
                                                      "UTC offset (-04:00, never -0400 or Z)."),
        missing_fields_desc=schema_desc.get("missing_fields", "Name any fact known to be absent, "
                                                                "and name every params value that is null."),
        provenance_desc=schema_desc.get("provenance", "reported/document/instrument/derived."),
        supersedes_desc=("(`supersedes`: %s)" % schema_desc["supersedes"]) if schema_desc.get("supersedes") else "",
    ))

    parts.append(_CANNOT_DO)
    parts.append(_ERRORS)

    return "\n\n".join(p.rstrip() for p in parts if p and p.strip())
