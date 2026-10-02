#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_skill.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import make_client, make_client_with_settings  # noqa: E402
import app.skill as skill  # noqa: E402
from app.main import app  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    client = make_client()

    r = client.get("/skill")
    ck(r.status_code == 200, "GET /skill returns 200")
    ck(r.headers["content-type"].startswith("text/markdown"), "content-type is text/markdown")
    text = r.text

    # route table is live, not hand-kept -- every real route should appear
    for path in ("/health", "/lines", "/lines/{line_id}", "/reservoirs",
                 "/events", "/events/{event_id}", "/vials/{unit}/{vial}", "/skill"):
        ck("`%s`" % path in text, "route table includes %s" % path)
    ck("| POST | `/events` |" in text, "route table includes POST /events specifically")
    ck("| POST | `/lines` |" in text, "route table includes POST /lines specifically")

    # event_types and parameter_registry are read live from the fixture log
    ck("`inoculation`" in text, "known event_types includes inoculation (from the fixture log)")
    ck("`termination`" in text, "known event_types includes termination")
    ck("`merge`" in text, "known event_types includes merge")
    ck("`predecessor_in_vial`" in text, "parameter_registry includes predecessor_in_vial")
    ck("`fixture_note`" in text, "parameter_registry includes the fixture's own registered key")

    # schema descriptions are quoted, not re-described by hand
    ck("never -0400 or Z" in text, "timestamp hard rule is the real schema description, not a stub")
    ck("MUST be named here" in text, "missing_fields hard rule is the real schema description")

    # request shapes for both write routes are present
    ck("target" in text and "line_id" in text, "POST /events request shape is documented")
    for mode in ("branch", "split", "merge", "restart"):
        ck("begin_mode: %s" % mode in text, "POST /lines documents begin_mode=%s" % mode)
    ck("begin_mode`** (" not in text, "begin_mode itself isn't rendered as a redundant field line")

    # the day-one-founder case (predecessor_line_id optional) is documented
    ck("day-one founder" in text, "the day-one-founder restart case is documented")

    # GET /media's query params are introspected from the live route's own
    # Query(...) descriptions (app/routes/media.py), not retyped by hand --
    # checked against that exact source text, not a paraphrase of it.
    ck("## `GET /media`" in text, "GET /media gets its own documented section")
    ck("pass this explicitly to reproduce a result exactly" in text,
       "at's description is the real Query(...) text from app/routes/media.py, introspected live")
    ck("**`at`** (optional)" in text, "at is documented as optional")
    ck("**`unit`** (optional)" in text and "**`status`** (optional)" in text,
       "unit and status query params are both documented")
    ck("rate_is_upper_bound" in text.split("## `GET /media`")[1].split("## Known")[0],
       "the GET /media section itself mentions rate_is_upper_bound, not just the general hard rule")

    # what it can't do is documented, not silently omitted
    ck("Cannot edit or delete anything" in text, "documents the API cannot edit/delete")
    ck("Cannot remove a reservoir" in text,
       "documents the API cannot remove a reservoir/touch hardware/design")
    ck("is the one exception" in text,
       "documents the level_reading/media_prep exception to the reservoir-touching rule (ISSUE_001)")
    ck('"created": true' in text,
       "documents that a media_prep bringing a NEW reservoir online reports created=true")
    ck("does NOT reactivate it by default" in text and "params.reactivate: true" in text,
       "documents that reactivating an EXISTING retired reservoir needs an explicit reactivate flag")

    # ── restart's predecessor_line_id vs real ancestry -- a real operator's
    # confusion, reported directly: seeding new lineages from already-
    # terminated ones via restart, expecting predecessor_line_id to record
    # descent. It never does -- hardware/vial continuity only. ─────────────
    ck("is hardware continuity, never" in text and "lineage.parents` stays `[]`" in text,
       "documents that restart's predecessor_line_id never creates a lineage.parents edge")
    ck("DESCRIPTIVE PROVENANCE, not biological ancestry" in text,
       "documents that source_culture is descriptive, not a graph edge, either")
    ck("`occupies_vial_of` is hardware continuity, not ancestry" in text,
       "documents occupies_vial_of's distinction from ancestry where GET /vials mentions it")
    ck("no exception for an already-ended" in text or "no-exception-for-ended-parents" in text,
       "documents that branch/merge have no exception for an already-ended source")
    ck("hardware-only, never ancestry" in text,
       "connects the bookkeeping-fabrication warning explicitly to restart's own fields")

    # the ?event_type= filter actually filters, not just decorative -- scoped
    # to the "Registered params keys" section itself, since predecessor_in_vial
    # is now ALSO named in the always-rendered restart/descent-vs-continuity
    # explanation (POST /lines section, unaffected by this filter, and
    # correctly so -- that's general documentation, not a per-event-type
    # registry listing).
    def registry_section(full_text):
        return full_text.split("## Registered `params` keys")[1].split("## Registering a new")[0]

    r_inoc = client.get("/skill", params={"event_type": "inoculation"})
    r_term = client.get("/skill", params={"event_type": "termination"})
    ck("predecessor_in_vial" in registry_section(r_inoc.text),
       "event_type=inoculation includes predecessor_in_vial in the registry section")
    ck("predecessor_in_vial" not in registry_section(r_term.text),
       "event_type=termination EXCLUDES predecessor_in_vial from the registry section "
       "(registered for inoculation only)")
    ck("fixture_note" in registry_section(r_term.text),
       "an entry with no applies_to_event_types still shows for any filter")

    # ── audit findings, fixed: read routes were previously undocumented
    # beyond their bare route-table row ──────────────────────────────────
    ck("## Reading the log" in text, "the read routes get a dedicated section, not just a table row")
    reading_section = text.split("## Reading the log")[1].split("## `POST /events`")[0]
    ck("`status`" in reading_section and "`unit`" in reading_section,
       "GET /lines' status/unit filters are named")
    ck("REDUCED projection" in reading_section, "GET /lines' summary shape (not the full object) is explained")
    ck("`since`" in reading_section and "`line_id`" in reading_section
       and "`event_type`" in reading_section and "`limit`" in reading_section,
       "GET /events' four filters are all named")
    ck("total_matching" in reading_section, "GET /events' pagination (total_matching vs count) is explained")
    ck("never `event_id`" in reading_section, "GET /events' sort order caveat is stated, not just in README")
    ck("hardware position, not a culture" in reading_section, "GET /vials' current-occupant-only behavior is explained")
    ck("writes_supported" in reading_section and "auth_configured" in reading_section,
       "GET /health's writes_supported vs auth_configured distinction is explained")

    # response shapes for the write routes, not just their request shapes
    ck("Response: the created event object itself" in text, "POST /events' response shape is documented")
    ck("no `scope` key at all" in text, "the scope-key asymmetry (facility vs line-scoped) is called out")
    ck("new_line_ids, lines, terminated_line_ids" in text, "POST /lines' response shape is documented")
    ck("reservoir_projection" in text, "POST /events' reservoir_projection response key is documented (ISSUE_001)")
    ck("projected\": false" in text, "the not-projected shape ({projected: false, reason}) is documented")
    ck("line_lifecycle_projection" in text, "POST /events' line_lifecycle_projection response key is documented")
    ck("applied\": false" in text, "the not-applied shape ({applied: false, reason}) is documented")
    ck("NOT reported this way" in text,
       "the doc is explicit that termination/media_switch contradictions are 409s, not a soft report")
    ck("moves `pg` too" in text, "media_prep's pg_concentration -> reservoir pg projection is documented")

    # the 500 explanation covers BOTH real causes, not just the commit-failure one
    errors_section = text.split("## What the errors mean")[1]
    ck("unlinked termination" in errors_section, "the 409 explanation covers the termination-contradiction case")
    ck("superseded_by" in text, "the superseded_by reverse-lookup field is documented")
    ck("elapsed_h can never be negative" in text or "must never be negative" in text,
       "the elapsed_h negative-value guard is documented")
    ck("media_from" in errors_section, "the 409 explanation covers the media_switch-contradiction case")
    ck("more than an hour in the future" in errors_section, "the future-timestamp guard is documented as a 422 cause")
    ck("checks only that the referenced event_id" in errors_section,
       "the doc is honest that caused_by_event/supersedes are existence-checked, not relevance-checked")
    ck("`POST /lines`' founding and termination events are" in errors_section,
       "the doc confirms POST /lines' new events get the same dangling-reference check as POST /events")
    ck("`mode` isn't" in errors_section, "the 409 explanation covers media_switch on a non-switch-mode line")
    ck("merge with itself" in errors_section, "the 422 explanation covers a merge naming the same parent twice")

    # ── round-3 arcane-scenario findings ────────────────────────────────────
    ck("A vial number alone does not name a line" in text, "the cross-unit vial-ambiguity hard rule is documented")
    ck("sorted by `(vial, unit)`" in text, "GET /lines' vial-sort is documented as the way to spot collisions")
    ck("no way to mark a value as proposed-but-unconfirmed" in text,
       "the no-structured-uncertainty-marker hard rule is documented")
    ck("bookkeeping correction, not a\n  real biological event" in text or "bookkeeping correction" in text,
       "the human-only-procedure warning is documented")
    # ISSUE_002 (2026-09-01): hardware_swap gained a real relocation
    # projection (new_unit+new_vial); this text used to assert the
    # OPPOSITE ("never projected at all") -- updated along with the code.
    ck("hardware_swap` NOW relocates a line" in text,
       "hardware_swap's relocation projection is documented in what-this-API-cannot-do")
    ck("line_id` is NEVER" in text and "same-unit or cross-unit" in text,
       "the doc is explicit that a relocation never touches line_id, same-unit or cross-unit")
    ck("two different causes" in errors_section, "the 500 explanation acknowledges there are two distinct causes")
    ck("transient" in errors_section and "no operator tokens configured" in errors_section,
       "the auth-misconfiguration 500 (non-retryable) is distinguished from the commit-failure one (retryable)")

    # route table: Starlette's :converter syntax must not leak into the
    # LLM-facing path column (the real path is {reservoir_id:path})
    ck("{reservoir_id:path}" not in text, "the :path converter syntax is stripped from the route table")
    ck("`/reservoirs/{reservoir_id}`" in text, "the displayed path uses the plain placeholder instead")

    # retired registry keys: shown and tagged, not silently dropped
    client2, settings2 = make_client_with_settings()
    log = json.loads(settings2.log_file.read_text())
    log["parameter_registry"]["old_retired_key"] = {
        "description": "no longer used", "status": "retired", "type": "string",
    }
    settings2.log_file.write_text(json.dumps(log, indent=2))
    text2 = client2.get("/skill").text
    ck("old_retired_key" in text2, "a retired registry key is shown, not silently omitted")
    ck("do not use for new events" in text2, "a retired key is tagged as such, not shown as if still current")

    # ── real production incident: on one deployment, route lookup for
    # "/lines" returned None (environment-specific -- a different FastAPI/
    # Starlette version's route matching; never reproduced locally) and a
    # bare `_route_by_path(...).dependant` with no None-check crashed the
    # ENTIRE /skill endpoint with a 500 on every single request. Fixed by
    # routing every call through _query_param_names/_render_query_params,
    # both of which handle a missing route gracefully. Simulate the exact
    # failure here so this can never silently regress. ────────────────────
    orig_route_by_path = skill._route_by_path
    skill._route_by_path = lambda app, path, method="GET": None
    try:
        r_missing = client.get("/skill")
        ck(r_missing.status_code == 200,
           "GET /skill still returns 200 even if EVERY route lookup fails (%s)" % r_missing.status_code)
        ck("unable to introspect" in r_missing.text,
           "a failed route lookup degrades to a visible placeholder, not a 500")
    finally:
        skill._route_by_path = orig_route_by_path

    # ── real production incident, a different one: on a live deployment,
    # `isinstance(route, APIRoute)` was False for EVERY route in app.routes.
    # First suspected cause (a duplicate/shadowed fastapi install) did NOT
    # survive checking package versions on that host and reproducing
    # locally with the exact same ones -- the real cause, confirmed by that
    # repro, was a genuine fastapi/starlette version skew (this server was
    # built against fastapi 0.136.3/starlette 0.50.0; that deployment ran
    # fastapi 0.141.1/starlette 1.6.0, where include_router() stopped
    # flattening routes into app.routes -- see _flatten_routes's own
    # docstring for the full story and the follow-up regression below).
    # "## Routes" rendered an empty table and every
    # _route_by_path-dependent section degraded to "unable to introspect"
    # -- not a crash (the round-2 fix above already prevented that), but a
    # real, silent loss of the whole route table and every query-param
    # section, on every request, indefinitely. Fixed by checking for the
    # specific attributes every caller actually uses instead of the class
    # identity. Simulate a route that "quacks like" a real APIRoute but
    # ISN'T one, confirming the duck-typed check still recognizes it. ─────
    class _NotReallyAnAPIRoute:
        """Same shape as a real starlette/fastapi APIRoute for the four
        attributes this module's introspection actually reads, deliberately
        NOT a subclass of APIRoute -- isinstance(this, APIRoute) is False,
        exactly like the real incident."""
        def __init__(self, path, methods, endpoint):
            self.path = path
            self.methods = methods
            self.endpoint = endpoint
            self.dependant = type("D", (), {"query_params": []})()
            self.summary = None

    fake_route = _NotReallyAnAPIRoute("/fake", {"GET"}, lambda: None)
    ck(skill._looks_like_api_route(fake_route) is True,
       "a structurally-identical-but-not-a-real-APIRoute object is still recognized")
    ck(skill._looks_like_api_route(object()) is False,
       "a genuinely unrelated object (missing the attributes) is correctly NOT recognized")

    app.router.routes.append(fake_route)  # app.routes itself is a read-only property over this
    try:
        text3 = client.get("/skill").text
        ck("| GET | `/fake` |" in text3,
           "the route table includes a route that only quacks like an APIRoute, not just real ones")
    finally:
        app.router.routes.remove(fake_route)

    # ── the actual real-incident shape: newer fastapi wraps an entire
    # include_router() call in one opaque wrapper (fastapi.routing.
    # _IncludedRouter on the real deployment) rather than flattening its
    # routes into app.routes. This server's dev fastapi version (0.136.3)
    # doesn't have that class at all -- reproduced ONLY by installing the
    # deployment's exact fastapi 0.141.1/starlette 1.6.0 locally, so this
    # test constructs a minimal fake with the same SHAPE
    # (`.original_router.routes`) rather than depending on that exact
    # class existing in whatever fastapi this suite happens to run under. ──
    class _FakeIncludedRouterWrapper:
        """Not a leaf route at all -- no path/methods/endpoint/dependant --
        matching real _IncludedRouter exactly: the real routes are nested
        one level down, under .original_router.routes."""
        def __init__(self, nested_routes):
            self.original_router = type("R", (), {"routes": nested_routes})()

    nested_fake = _NotReallyAnAPIRoute("/nested-fake", {"GET"}, lambda: None)
    wrapper = _FakeIncludedRouterWrapper([nested_fake])
    ck(skill._looks_like_api_route(wrapper) is False,
       "the wrapper itself is correctly NOT a leaf route")
    ck(skill._flatten_routes([wrapper]) == [nested_fake],
       "_flatten_routes finds the real route nested inside the wrapper")

    app.router.routes.append(wrapper)
    try:
        text4 = client.get("/skill").text
        ck("| GET | `/nested-fake` |" in text4,
           "a route nested inside an include_router()-style wrapper still appears in the table "
           "-- the actual shape of the real incident, not just the simpler single-object case above")
    finally:
        app.router.routes.remove(wrapper)

    # ══ GET /skill points to the separate config skill doc ═══════════════
    text5 = client.get("/skill").text
    ck("/config/skill" in text5,
       "GET /skill tells an LLM client where to find the config-generation "
       "skill doc, so one URL is enough to discover the other -- found missing "
       "by a doc-audit agent (config_skill.py already points back here, but "
       "this direction was silent)")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
