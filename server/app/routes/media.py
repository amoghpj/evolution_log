"""GET /media -- consumption rates and depletion projections, shaped from
tools/media.py in the log repo (MEDIA_TRACKING.md, this repo's own
implementation spec). Every rate, basis, and forecast comes from that
module's analyse()/dose_estimates()/high_media_outlook() -- nothing here
recomputes any of it. This module's only jobs: make the `at` timestamp
explicit and default it sensibly, flatten the one shape that doesn't
JSON-serialise, apply one narrow consistency fix (see
_fix_upper_bound_inconsistency below), and add an `attention` view over
data analyse() already produced.

ONE THING HERE IS NOT FROM analyse(): the `pump` block on each reservoir
row, and the top-level `pump` object describing which rigs could be
consulted. That is a genuinely separate measurement -- what the eVOLVERs
actually dispensed since each bottle was last looked at -- computed in
app/pump_rates.py, kept in its own labelled block, and never merged into or
substituted for the level-derived fields beside it. ISSUE_004 has the
reasoning; LOG_PROTOCOL.md §8's promise that tools/media.py never uses live
data stays true, because none of this happens in tools/media.py.
"""
import datetime
import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import Settings, get_settings
from ..dashboards import DashboardSettings, get_dashboard_settings
from ..log_repo import load_log, media_module
from ..pump_rates import build_pump_view

# _log, not `log`: the route's own local `log` is the loaded evolution log.
_log = logging.getLogger("or05.media")

router = APIRouter()


class _AlreadyBusy(Exception):
    """Raised only to reach the one `finally` that releases the slot."""

PUMP_MODES = ("auto", "off", "only")


def get_dashboards(settings: Settings = Depends(get_settings)) -> DashboardSettings:
    """Split out as its own dependency so a test can hand the route a fixture
    rig roster without touching LOG_REPO_PATH or the environment."""
    return get_dashboard_settings(settings)


# At most this many pump views may be in flight at once. GET /media is a sync
# route, so Starlette runs it on anyio's default thread limiter -- 40 tokens,
# shared with every other route. Measured: one sequential client polling once a
# second against a dribbling rig wedged the whole server at the 40th abandoned
# request, and /health, /lines and even /media?pump=off stopped answering. A
# client that has hung up is never noticed; the thread runs to completion. So
# the pump view gets its own allowance and leaves the rest of the pool alone.
PUMP_CONCURRENCY = 4
_pump_slots = threading.BoundedSemaphore(PUMP_CONCURRENCY)


def get_http_client():
    """A LAZY httpx client, created only if something asks for one.

    Constructing it eagerly cost ~4 ms of SSL-context setup on every request,
    including `?pump=off`, which never makes a call. Closed on the way out
    whether or not it was used. Also the seam tests use to point the route at a
    fake dashboard instead of the network."""
    import httpx

    made = []

    def factory():
        if not made:
            made.append(httpx.Client(follow_redirects=False))
        return made[0]

    try:
        yield factory
    finally:
        for client in made:
            client.close()


def _now_iso() -> str:
    """Now, in this machine's own zone rather than UTC.

    Correct as an instant either way, but `at` is echoed back and the route
    invites a caller to pass it again verbatim; rendering it as +00:00 put a
    zone into the record that appears nowhere else in it -- every timestamp in
    the log, and every rig's generated_at, is -04:00/-05:00."""
    return datetime.datetime.now().astimezone().isoformat()


def _flatten_perline(perline: dict) -> list[dict]:
    """perline is keyed by (media, role) tuples, which json.dumps cannot
    serialise as an object key. Flattening is presentation, not a second
    computation of anything -- the values are exactly what analyse() returned."""
    return [{"media": m, "role": r, "rate_L_per_h": v} for (m, r), v in perline.items()]


def _fix_upper_bound_inconsistency(rows: list[dict]) -> None:
    """tools/media.py's analyse() has a real inconsistency, confirmed by
    constructing a fixture that exercises it (tests/test_media.py) rather
    than assumed from reading: when a row's rate falls back to the fastest-
    per-line-rate bound (its pass 3, `elif fastest.get(row["role"])...`
    branch), it sets rate_basis="upper_bound" but never sets
    rate_is_upper_bound -- which stays at its pass-1 default of False for a
    reservoir with no measured rate of its own. The real log has never hit
    this path yet (0 such rows as of this writing), which is exactly why
    tools/test_media.py never caught it.

    Not fixed in media.py itself -- out of scope by MEDIA_TRACKING.md's own
    instruction, and this server's whole job is to shape output, not alter
    the consumption model. Fixed here because shipping rate_basis and
    rate_is_upper_bound inconsistent with each other is precisely the
    failure MEDIA_TRACKING.md §6 warns against: a confident number whose
    true bound-ness is invisible. Mutates in place; every other field on
    every row is untouched."""
    for row in rows:
        if row.get("rate_basis") == "upper_bound":
            row["rate_is_upper_bound"] = True


def _clamp_backward_extrapolation(rows: list[dict], at: str) -> None:
    """tools/media.py's analyse() draws a reservoir's level down linearly
    from its last known reading to `at`: `now_lvl = max(lvl - rate *
    hours(at, level_as_of), 0.0)`. That's correctly floored at zero going
    FORWARD, but nothing floors/ceilings it going BACKWARD -- an `at` well
    before the reservoir's last reading makes `elapsed` negative, so the
    same formula runs the draw-down in reverse and grows the level without
    bound. Found by simulating a filter-combination probe using a far-past
    `at`: a 1 L bottle reported `estimated_now_L: 1015.349` and
    `hours_remaining: 58382.6` -- a confident, physically impossible number
    (more media than was ever prepared), not a crash.

    Not fixed in media.py itself -- out of scope by the same reasoning as
    _fix_upper_bound_inconsistency above: this server shapes output, it
    doesn't alter the consumption model. Clamped here to the one physical
    fact analyse() already computed and this function can check without
    recomputing anything: a reservoir can never hold MORE than its own
    `prepared_L`. `hours_remaining`/`empty_at` are recomputed FROM the
    clamped level (using the same already-computed rate) so the projection
    stays internally consistent rather than showing a clamped level next
    to an unclamped forecast."""
    for row in rows:
        prepared = row.get("prepared_L")
        level = row.get("estimated_now_L")
        rate = row.get("rate_L_per_h")
        if prepared is None or level is None or level <= prepared:
            continue
        row["estimated_now_L"] = prepared
        proj = row.get("projection")
        if proj is not None and rate:
            h_left = prepared / rate
            proj["hours_remaining"] = round(h_left, 1)
            proj["empty_at"] = (
                datetime.datetime.fromisoformat(at) + datetime.timedelta(hours=h_left)
            ).isoformat()


_VOLUME_PARAM = {"media_prep": "volume_prepared", "level_reading": "volume_remaining"}


def _drop_events_readings_for_cant_survive(log: dict) -> dict:
    """tools/media.py's readings_for() (and, transitively through it,
    baseline_events/baseline_reset/analyse) dereferences
    params.volume_prepared["value"] / params.volume_remaining["value"]
    UNCONDITIONALLY for any media_prep/level_reading event naming a
    reservoir_id -- correct for every event a human ever hand-entered, but
    this server's own write path does NOT require that volume field
    (app/writer.py:project_reservoir_state treats it as optional and never
    invents it, exactly per this project's "never invent a value" rule --
    a media_prep/level_reading recording only e.g. a pg_concentration
    correction, with no volume, is a legitimate, already-accepted write).
    A real production incident: two live events (media_prep on
    patrick/M9-1 and plankton/M9-1, recording only pg_concentration) hit
    exactly this gap and took GET /media down for every caller with a bare
    500 -- and because the log is append-only, those two events can never
    be un-appended; the fix has to be on this read path, not the write path
    (a write-time guard is a separate, good idea for the FUTURE, but does
    nothing for events already in the log).

    Not fixed in media.py itself -- same "shape output, don't alter the
    model" boundary as _fix_upper_bound_inconsistency/_clamp_backward_
    extrapolation above. Instead: a media_prep/level_reading missing its
    volume field is dropped from the COPY of experiment_events fed to
    analyse() -- it has no volume to contribute to consumption accounting
    anyway, so excluding it loses no real information. The event itself is
    untouched in the log and still returned verbatim by GET /events; only
    this one route's crash-prone inputs are filtered. Returns (filtered_log,
    dropped_event_ids) so the response can say plainly what was excluded,
    rather than silently working around it."""
    dropped = []

    def usable(e):
        vol_key = _VOLUME_PARAM.get(e.get("event_type"))
        if vol_key is None:
            return True
        p = e.get("params") or {}
        if "reservoir_id" not in p:
            return True
        if isinstance(p.get(vol_key), dict) and "value" in p[vol_key]:
            return True
        dropped.append(e.get("event_id"))
        return False

    filtered = dict(log)
    filtered["experiment_events"] = [e for e in log.get("experiment_events", []) if usable(e)]
    return filtered, dropped


def _attention_pump(entry: dict, row: dict) -> None:
    """Carry the pump view onto an attention entry without changing the order.

    The sort stays on the level-derived projection deliberately: swapping the
    ordering key depending on which rigs happened to answer would make the same
    request return a differently-ordered list for reasons the caller cannot
    see. The pump figure rides alongside, labelled."""
    pump = row.get("pump") or {}
    if pump.get("basis") != "pump_integrated":
        if pump.get("reason"):
            entry["pump"] = {"available": False, "basis": pump.get("basis"),
                             "reason": pump["reason"]}
        return
    proj = pump.get("projection") or {}
    bounded = bool(pump.get("drawn_is_upper_bound"))
    entry["pump"] = {
        "available": True,
        "basis": pump["basis"],
        "hours_remaining": proj.get("hours_remaining"),
        "drawn_L": pump.get("drawn_L"),
        "estimated_now_L": pump.get("estimated_now_L"),
        # A bound must travel with the number it bounds. This block used to
        # drop drawn_is_upper_bound and bound_note, so a figure that is "AT
        # MOST this" appeared here identical to an exact one -- in the list
        # GET /skill calls the one most worth summarising.
        "drawn_is_upper_bound": bounded,
        "bound_note": pump.get("bound_note"),
        # Likewise divergence: a null divergence_L means either "they agree"
        # or "the comparison was refused", and only these two fields tell them
        # apart. Carrying the number without them taught a reader that silence
        # meant agreement.
        "divergence_comparable": pump.get("divergence_comparable"),
        "divergence_skipped_because": pump.get("divergence_skipped_because"),
        # attention is the list an LLM is most likely to summarise, and these
        # are the two fields GET /skill's hard rules call out by name -- one as
        # "the single most informative thing this route can tell you", the
        # other as a thing that "needs a human". Dropping them here put them
        # exactly where they would not be read.
        "overdrawn": pump.get("overdrawn"),
        "divergence_L": pump.get("divergence_L"),
        "divergence_note": pump.get("divergence_note"),
        "quiet_vials": pump.get("quiet_vials"),
        "message": ("%s: %s L drawn by the pumps since %s%s"
                    % (row["id"], pump.get("drawn_L"), pump.get("since"),
                       " AT MOST (this rig reports a bound, not a point)" if bounded else "")),
    }
    # The list is ordered by the level-derived clock on purpose (see
    # _attention's docstring), which means a bottle the live pumps say is
    # hours from empty can sit below one the extrapolation merely dislikes.
    # Say so on the entry rather than leaving position to be read as rank.
    level_h = entry.get("hours_remaining")
    pump_h = proj.get("hours_remaining")
    if level_h is not None and pump_h is not None and pump_h < level_h * 0.5:
        entry["pump"]["more_urgent_than_level"] = True
        entry["pump"]["urgency_note"] = (
            "the pumps put this bottle %.1f h from empty where the level-derived "
            "extrapolation says %.1f h. This list is ordered by the latter, so its "
            "position understates the urgency" % (pump_h, level_h))
    else:
        entry["pump"]["more_urgent_than_level"] = False


def _attention(rows: list[dict]) -> list[dict]:
    """Mirrors the CLI's closing "Attention" block: active reservoirs with a
    projection, ordered soonest-to-empty first. Built entirely from fields
    analyse() already computed on these same rows -- no new consumption
    logic, just a different (structured, sorted) shape for numbers that
    already exist."""
    candidates = [r for r in rows if r.get("status") == "active" and r.get("projection")]
    candidates.sort(key=lambda r: r["projection"]["hours_remaining"])

    out = []
    for r in candidates:
        proj = r["projection"]
        if r.get("rate_is_upper_bound"):
            note = (" (rate is an upper bound: the true rate is this or slower, "
                     "time remaining is this or longer)")
        elif r.get("rate_provisional"):
            note = " (rate measured over a window too short to trust yet)"
        elif proj["basis"] == "prior_bottle":
            note = " (rate carried over from this reservoir's own previous bottle)"
        elif proj["basis"] == "inferred":
            note = " (depletion inferred from a per-line rate measured elsewhere)"
        else:
            note = ""
        entry = {
            "reservoir_id": r["id"],
            "hours_remaining": proj["hours_remaining"],
            "rate_basis": r["rate_basis"],
            "message": "%s: empty in %.1f h%s" % (r["id"], proj["hours_remaining"], note),
        }
        _attention_pump(entry, r)
        out.append(entry)
    return out


@router.get("/media", summary="Media consumption rates and depletion projections, with provenance")
def get_media(
    at: str | None = Query(
        None, description="ISO 8601 timestamp with offset to project to. Default: request "
                          "time, NOT log_meta.last_updated -- pass this explicitly to reproduce "
                          "a result exactly."),
    unit: str | None = Query(None, description="Filter reservoirs to this unit, e.g. patrick or plankton"),
    status: str | None = Query(None, description="Filter reservoirs by status, e.g. active or retired"),
    pump: str = Query("auto", json_schema_extra={"enum": list(PUMP_MODES)},
                      description="auto (default): also measure what the eVOLVERs "
                                          "actually dispensed since each bottle's last "
                                          "reading, where a rig can be reached. off: do not "
                                          "contact any rig. only: return just the reservoirs "
                                          "that got a pump measurement (all their fields, "
                                          "not a subset)."),
    settings: Settings = Depends(get_settings),
    dashboards: DashboardSettings = Depends(get_dashboards),
    http_client=Depends(get_http_client),
) -> dict[str, Any]:
    if pump not in PUMP_MODES:
        raise HTTPException(
            status_code=422,
            detail="pump must be one of %s (got %r)" % (", ".join(PUMP_MODES), pump))

    if at is not None:
        try:
            parsed_at = datetime.datetime.fromisoformat(at)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="at is not a valid ISO 8601 timestamp with an explicit offset: %s" % exc,
            ) from exc
        # fromisoformat happily accepts an offset-less timestamp, so the check
        # above never enforced what its own message promises. A naive `at` then
        # reached tools/media.py's `hours()` and raised TypeError on subtracting
        # it from an offset-aware level_as_of -- a 500 for the whole request,
        # with or without the pump view. Found by an adversarial testing agent;
        # the crash predates this feature.
        if parsed_at.tzinfo is None:
            raise HTTPException(
                status_code=422,
                detail="at must carry an explicit UTC offset, e.g. "
                       "2026-09-21T06:15:00-04:00. Every timestamp in this log does, and "
                       "a bare local time is ambiguous between the rigs and the caller.",
            )
    at = at or _now_iso()

    log = load_log(settings)
    media = media_module(settings)

    log, skipped_events = _drop_events_readings_for_cant_survive(log)
    rows, perline = media.analyse(log, at)
    _fix_upper_bound_inconsistency(rows)
    _clamp_backward_extrapolation(rows, at)

    if unit is not None:
        rows = [r for r in rows if r["unit"] == unit]
    if status is not None:
        rows = [r for r in rows if r["status"] == status]

    # The pump view runs AFTER the filters, so a request narrowed to one unit
    # never contacts the other one. It attaches a `pump` block to each active
    # row -- a measurement, or a named reason there isn't one -- and never
    # touches any existing field.
    if pump == "off":
        pump_view = {"mode": "off", "sources": {}, "measured": [], "unavailable": [],
                     "note": "pump=off, so no eVOLVER was contacted, no reservoir carries a "
                             "`pump` block, and every figure below is derived from bottle "
                             "level readings alone"}
    else:
        # The pump view must never take the level-derived answer down with it.
        # Everything it touches crosses a network to a machine this server does
        # not control, and app/routes/media.py already carries the scar from a
        # read path that 500'd for every caller (see
        # _drop_events_readings_for_cant_survive above). pump_rates refuses
        # rather than raises by design; this is the backstop for the bug that
        # design does not anticipate.
        acquired = _pump_slots.acquire(blocking=False)
        if not acquired:
            # Every diagnosis this feature produces lives inside a /media
            # response body -- fine for the LLM client, invisible to the human
            # on journalctl, who sees a perfectly healthy service while the
            # pump view is 100% unavailable. These two lines are the whole of
            # the operator-facing signal.
            _log.warning("pump view declined: all %d slots busy; a rig is probably not "
                         "answering", PUMP_CONCURRENCY)
            blocks, sources = {}, {}
            for row in rows:
                if row.get("status") == "active":
                    blocks[row["id"]] = {
                        "basis": "unavailable",
                        "reason": "this server is already serving %d pump views and will "
                                  "not start another -- a rig that has stopped responding "
                                  "would otherwise tie up the worker pool every other "
                                  "route shares. Retry, or use ?pump=off for the "
                                  "level-derived figures, which need no rig"
                                  % PUMP_CONCURRENCY,
                    }
        try:
            if not acquired:
                raise _AlreadyBusy
            if dashboards.error:
                _log.warning("no eVOLVER dashboard roster: %s", dashboards.error)
            client = http_client() if callable(http_client) else http_client
            blocks, sources = build_pump_view(log, rows, at, dashboards, client)
        except _AlreadyBusy:
            pass
        except Exception as exc:                        # noqa: BLE001 -- deliberate
            # `sources` is NOT discarded. Clearing it made the response say
            # "no eVOLVER was contacted ... the rigs may be perfectly healthy"
            # after the rigs had been contacted and had answered, which is the
            # opposite of the truth and the opposite of useful.
            blocks = {}
            sources = locals().get("sources") or {}
            for row in rows:
                if row.get("status") == "active":
                    blocks[row["id"]] = {
                        "basis": "unavailable",
                        "reason": "the pump view failed with %s: %s. Every figure beside "
                                  "this one is unaffected -- they come from bottle level "
                                  "readings and never touch a rig"
                                  % (type(exc).__name__, exc),
                    }
        finally:
            if acquired:
                _pump_slots.release()
        for row in rows:
            if row["id"] in blocks:
                row["pump"] = blocks[row["id"]]
        pump_view = {"mode": pump, "sources": sources,
                     "measured": sorted(k for k, v in blocks.items()
                                        if v.get("basis") == "pump_integrated"),
                     "unavailable": sorted(k for k, v in blocks.items()
                                           if v.get("basis") != "pump_integrated")}
        # These three lists describe the FILTERED set: the pump view runs after
        # `unit`/`status` so a narrowed request never contacts the other rig.
        # `unavailable: []` under ?unit=patrick therefore says nothing at all
        # about plankton, and read as a facility-wide all-clear it is false.
        pump_view["scope"] = {
            "unit": unit, "status": status,
            "note": ("these lists cover only the reservoirs this request asked for"
                     if (unit or status) else
                     "no unit/status filter, so these lists cover every active reservoir"),
        }
        if not blocks:
            pump_view["note"] = ("no active reservoir matched this request's filters, so no "
                                 "eVOLVER was contacted")
        elif not sources:
            # Every reservoir was refused before a rig was reached -- a mapping
            # change, a missing level, no roster entry. An empty `sources` here
            # used to be indistinguishable from "no rigs are configured", so a
            # client reporting rig health from it said the opposite of the truth.
            pump_view["note"] = ("no eVOLVER was contacted: every reservoir was refused "
                                 "before a rig was reached. The rigs may be perfectly "
                                 "healthy -- read each reservoir's own `reason`")
    # Every aggregate below is computed from the UNFILTERED rows. pump=only
    # filters the `reservoirs` list and nothing else, because tools/media.py
    # reads those rows as a population, not as a display list:
    # high_media_outlook looks its low reservoir up INSIDE rows and falls back
    # to c_low = 0.0 when it is absent, so dropping a row for a reason as
    # incidental as "its rig was unreachable" silently changed the ramp
    # forecast by 80% -- and dose_estimates then reported "no measured rate
    # for the low reservoir" about a reservoir whose rate_basis is `measured`.
    # A rig outage must not be able to make the level-derived record lie.
    listed = rows
    if pump == "only":
        listed = [r for r in rows
                  if (r.get("pump") or {}).get("basis") == "pump_integrated"]

    return {
        "at": at,
        "skipped_events": skipped_events,
        "reservoirs": listed,
        "per_line": _flatten_perline(perline),
        "delivered_pg": media.dose_estimates(log, rows),
        "high_outlook": media.high_media_outlook(log, rows, perline),
        "attention": _attention(rows),
        "pump": pump_view,
    }
