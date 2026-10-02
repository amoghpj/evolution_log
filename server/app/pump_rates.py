"""The pump-derived half of GET /media: what the rigs actually dispensed
since each bottle was last looked at.

WHAT PROBLEM THIS SOLVES
tools/media.py estimates a reservoir's present level by extrapolating:
`now = level - rate * elapsed`, where `rate` was measured between two
readings that both predate the last one. The further the request drifts from
`level_as_of`, the more of the answer is a projection of past average
behaviour onto a present nobody has looked at -- and that rate cannot know
about a ramp step, a terminated line or a blocked pump since. The rigs have
recorded every dispense that actually happened in exactly that window.

So the two compose rather than compete:

    level-derived : rate between reading0 and reading1  -- bottle truth, stale
    pump-derived  : volume from reading1 to now         -- live, calibrated

    estimated_now_L = level_at_last_reading - sum(dispensed since it)

WHAT THIS MODULE IS NOT
It does not recompute anything tools/media.py computes. Every rate, basis and
forecast in the response still comes from analyse(); this adds a parallel,
separately-labelled view and never edits the existing one. It also does not
live in tools/media.py, which LOG_PROTOCOL.md §8 documents as using bottle
readings only, never live data -- a promise worth keeping for a module that
also runs as a standalone CLI with no network.

WHY SO MUCH OF THIS FILE IS REFUSALS
The dangerous failure here is a confident number, not a crash. Pump data can
be wrong in ways that look exactly like pump data being right: a rig that
restarted, a line that changed vials mid-window, a bottle whose lines were
re-pointed, a dashboard answering for the other unit. Each of those is
checked and produces a NAMED unavailability. Nothing in this module ever
falls back to zero, and nothing ever presents a level-derived number under a
pump-derived label.
"""
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

# Event types that can invalidate the reservoir -> line -> vial join for a
# window. Each one, occurring after a reservoir's last reading, means the map
# used to attribute dispenses does not describe the whole window.
MAPPING_EVENT_TYPES = {
    "hardware_swap",     # a line changed vial or unit, or left the evolver
    "media_switch",      # a line started drawing from different bottles
    "reservoir_swap",    # bottles exchanged between positions
    "reservoir_change",  # a position's composition changed
    "pg_change",         # pg_regime moved, including source_reservoirs
    "termination",       # a line stopped drawing, and has left lines_fed
    "merge", "split",    # lines combined or divided across vials
}

# How far `at` may sit from the server's own clock before the pump view is
# refused. The rigs report the present and only the present; a historical `at`
# has no pump answer, and pretending otherwise would break the reproducibility
# promise in MEDIA_TRACKING.md §4.
AT_TOLERANCE_MIN = 5.0

# How far a rig's own wall clock may sit from this server's before its
# integration window stops meaning what the anchor asked for. A rig converts
# the anchor using ITS generated_at; the server measures window_h using ITS
# clock, and the two were never compared. A Raspberry Pi has no RTC, so a rig
# that missed NTP can be months out, silently integrating a window thousands
# of hours from the one requested -- and reporting basis: pump_integrated.
RIG_SKEW_TOLERANCE_MIN = 10.0

# ── BOUNDS ───────────────────────────────────────────────────────────────────
# A per-request timeout bounds a socket read. It does not bound a response, a
# request count, or a body size, and GET /media is an unauthenticated route an
# LLM may poll. Measured before these existed: a rig dribbling one byte every
# 2.5 s under a 3 s read timeout held a request open past 60 s; a
# degraded-but-alive rig answering in 2.8 s produced an 84 s successful
# request, with a 126 s worst case; and a 299 KB gzip bomb cost 1.3 GB of RAM.

# Total wall-clock budget for all rig traffic in one request. Once spent,
# every remaining reservoir gets a named unavailability, exactly like any
# other refusal in this module.
PUMP_BUDGET_S = 8.0

# Ceiling on planned rig requests. 2 units x 4 anchors x (1 + vials) is
# reachable from ordinary operator behaviour (reading bottles one at a time),
# and discovering that ceiling as wall time is the wrong way to find it.
MAX_FETCHES = 24

# Decompressed body cap, ~100x the largest legitimate payload. Enforced while
# streaming, so a compression bomb is abandoned rather than decoded.
MAX_BODY_BYTES = 256 * 1024

CONSUMPTION_SCHEMA = "or05.consumption/1"

# A divergence worth saying out loud: both a relative and an absolute floor, so
# a tiny bottle with a tiny draw does not shout and a large one does not hide.
DIVERGENCE_REL = 0.20
DIVERGENCE_ABS_L = 0.02

# Below this, a pump-derived RATE says more about when the request happened
# than about the culture -- one 10 mL dilution cycle landing five minutes
# after a level round reads as 0.48 L/h and "empty in 1.6 h". tools/media.py
# carries MIN_SPAN_H = 2.0 for exactly this reason on its own side; the draw
# itself is still a real measurement and is still reported.
MIN_WINDOW_H = 2.0


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _hours(a: str, b: str) -> float:
    return (_parse(a) - _parse(b)).total_seconds() / 3600.0


def unavailable(reason: str, **extra) -> dict:
    out = {"basis": "unavailable", "reason": reason}
    out.update(extra)
    return out


# ─── the log side ─────────────────────────────────────────────────────────────

def newest_reading_after(log: dict, rid: str, anchor: str) -> dict | None:
    """A `level_reading` for this reservoir later than the anchor the row carries.

    analyse() takes level_L/level_as_of from `reservoirs.items[*]`, which is a
    projection this server maintains on write -- and on the live log eight of
    eight active bottles have a level_reading EVENT newer than their item
    block, by up to 0.30 L on a 1 L bottle, every one in the direction that
    says there is more media than there is. Events appended by hand without
    being projected do this, and no validator catches it. Subtracting a
    measured draw from a level the same log contradicts is not a measurement.
    """
    # CLAUDE.md: "A correction is a new event carrying supersedes." An event
    # something else supersedes has been retracted, and refusing a reservoir
    # by naming a retracted reading -- then telling the operator to project
    # its number -- is worse than not checking at all.
    superseded = set()
    for e in log.get("experiment_events") or []:
        target = (e.get("supersedes")
                  or (e.get("params") or {}).get("supersedes"))
        for victim in (target if isinstance(target, list) else [target]):
            if isinstance(victim, str):
                superseded.add(victim)

    best = None
    for e in log.get("experiment_events") or []:
        if e.get("event_type") != "level_reading":
            continue
        if e.get("event_id") in superseded:
            continue
        p = e.get("params") or {}
        if p.get("reservoir_id") != rid:
            continue
        ts = e.get("timestamp")
        if not ts or _parse(ts) <= _parse(anchor):
            continue
        volume = (p.get("volume_remaining") or {}).get("value")
        if volume is None:
            # Nothing to compare, so nothing to refuse on: a reading with no
            # volume rendered as "records a later reading of None L".
            continue
        # `>=`, not `>`. Correcting a VALUE means a second event at the same
        # instant, and keeping the first reported the retracted number.
        if best is None or _parse(ts) >= _parse(best["timestamp"]):
            best = {"event_id": e.get("event_id"), "timestamp": ts, "volume": volume}
    return best


def line_positions(log: dict) -> dict[str, dict]:
    """line_id -> its current position and status, for the join."""
    out = {}
    for line_id, L in (log.get("lines") or {}).items():
        # The log records a line's feeding bottles TWICE -- line-level
        # `reservoirs` and `pg_regime.source_reservoirs` -- both required by
        # the schema, both hand-maintained, and cross-checked by nothing
        # (`grep source_reservoirs tools/lineage.py` is empty). Merging them
        # let the second silently overwrite the first, so a crossed pair in
        # one copy vanished and which copy was stale decided whether you got a
        # refusal or a confident number. Disagreement is recorded, not
        # resolved: a module built to refuse rather than guess must not pick
        # one of two contradicting records.
        own, conflicts = {}, {}
        legacy = L.get("reservoirs") if isinstance(L.get("reservoirs"), dict) else {}
        regime = (L.get("pg_regime") or {}).get("source_reservoirs")
        regime = regime if isinstance(regime, dict) else {}
        for role in set(legacy) | set(regime):
            a, b = legacy.get(role), regime.get(role)
            values = {v for v in (a, b) if isinstance(v, str)}
            if len(values) > 1:
                conflicts[role] = {"reservoirs": a, "source_reservoirs": b}
            if values:
                own[role] = (a if isinstance(a, str) else b)
        out[line_id] = {
            "line_id": line_id,
            "unit": L.get("unit"),
            "vial": L.get("vial"),
            "status": L.get("status"),
            "reservoirs": own,
            "reservoir_conflicts": conflicts,
        }
    return out


def line_reservoirs(line: dict) -> set[str]:
    """Every reservoir id a line record names as feeding it, across both
    idioms in the log: the line-level `reservoirs` object and
    `pg_regime.source_reservoirs`. A TERMINATED line still carries these, which
    is what makes it possible to tell a line that used to drink from this
    bottle apart from one that never did."""
    out = set()
    for holder in (line.get("reservoirs"), (line.get("pg_regime") or {}).get("source_reservoirs")):
        if isinstance(holder, dict):
            out.update(v for v in holder.values() if isinstance(v, str))
    return out


def _as_ids(value) -> set[str]:
    """A param that names reservoirs or lines may be a string or a list of
    them; both idioms are live in the log."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    return set()


def mapping_changes_since(log: dict, rid: str, unit: str, lines_fed: list[str],
                          anchor: str) -> list[dict]:
    """Events after `anchor` that could make the reservoir -> vial map wrong.

    Deliberately broader than "events naming this reservoir". A line
    TERMINATED mid-window has already left lines_fed, so its dispenses would
    silently go uncounted -- which understates the draw, overstates what is
    left in the bottle, and is therefore the dangerous direction. So a line
    event counts when the line is fed by this reservoir NOW *or* when the
    line's own record still names this reservoir, which a terminated or
    switched-away line does.

    It is not, however, "anything on this unit": an M9 line ending has nothing
    to do with an LB bottle sitting next to it, and refusing on that would
    make the whole feature unavailable most of the time for no gain. An
    earlier version did exactly that via a bare `params.unit` test, which
    blanked all four of a unit's bottles for an event that touched none of
    them -- EVT-00187 is that shape.

    THE PARAM NAMES HERE ARE THE LOG'S, NOT PLAUSIBLE ONES. A first version
    looked for `reservoir_id`/`reservoir_ids`, which no real
    `reservoir_change` or `pg_change` in this log carries: those use
    `reservoir_id_from`/`reservoir_id_to`/`lines_affected`. The check passed
    its tests and caught nothing that actually happens. Every key consulted
    below appears in evolution_log.json today.
    """
    fed = set(lines_fed)
    hits = []

    def consider(event, line_id=None, line_unit=None, line_res=frozenset()):
        if event.get("event_type") not in MAPPING_EVENT_TYPES:
            return
        ts = event.get("timestamp")
        if not ts or _parse(ts) <= _parse(anchor):
            return
        p = event.get("params") or {}
        # A pg_change naming no reservoir moved a controller value, not a
        # line's plumbing (EVT-00089..92 are this shape). Pump integration
        # measures actual mL per role and is indifferent to the low/high mix,
        # so refusing on those was pure over-refusal.
        if event.get("event_type") == "pg_change" and not any(
                k in p for k in ("reservoir_id", "reservoir_ids",
                                 "reservoir_id_from", "reservoir_id_to")):
            return
        named_reservoirs = set()
        for key in ("reservoir_id", "reservoir_ids", "reservoir_id_from",
                    "reservoir_id_to", "replaced_by"):
            named_reservoirs |= _as_ids(p.get(key))
        affected = _as_ids(p.get("lines_affected")) | _as_ids(p.get("line_id"))
        affected |= _as_ids(p.get("parent_line_ids")) | _as_ids(p.get("child_line_ids"))
        affected |= _as_ids(p.get("replaced_by_line_ids"))

        relevant = (
            (line_id is not None and line_id in fed)
            or rid in named_reservoirs
            or rid in line_res
            or bool(affected & fed)
            # a line named as affected whose OWN record still points at this
            # bottle -- the case of a line re-pointed away mid-window and
            # since dropped from lines_fed, which is the dangerous direction
            or any(rid in line_reservoirs((log.get("lines") or {}).get(a) or {})
                   for a in affected)
        )
        if relevant:
            hits.append({"event_id": event.get("event_id"),
                         "event_type": event.get("event_type"),
                         "timestamp": ts,
                         "line_id": line_id})

    for event in log.get("experiment_events") or []:
        consider(event)
    for line_id, L in (log.get("lines") or {}).items():
        res = line_reservoirs(L)
        for event in L.get("events") or []:
            consider(event, line_id=line_id, line_unit=L.get("unit"), line_res=res)

    hits.sort(key=lambda h: (h["timestamp"], h["event_id"] or ""))
    return hits


def reservoir_targets(row: dict, positions: dict[str, dict]) -> tuple[list[dict], str | None]:
    """The (unit, vial, role) tuples whose dispenses belong to this reservoir.

    DERIVED FROM THE LINES, NOT FROM lines_fed.

    The log states this relation twice. Each line's own record names the
    bottles feeding its two pumps, and `reservoirs.items[*].lines_fed` mirrors
    it from the other side. Only ONE of those is maintained: the server writes
    a line's record when the line is created and moves it on a media_switch,
    while `lines_fed` is hand-edited -- GET /skill says so outright, and a
    first draft of tools/lineage.py's agreement check proved it by rejecting
    ordinary correct writes.

    This function read lines_fed. On the live log that meant every bottle
    refused: each array still named the lines of weeks ago, all since ended
    and replaced (patrick-v04#2 where the rig now runs patrick-v04#7), and a
    terminated line in the list is a hard refusal. A denormalisation nobody
    maintains is the wrong source of truth, and it made a stale field into an
    outage.

    So the active lines are asked instead. lines_fed is still consulted, as a
    CROSS-CHECK: an active line listed there that does not name this
    reservoir back is a real contradiction and still refuses. Entries naming
    ended lines are the expected staleness and are reported, not fatal.

    Returns (targets, refusal, notes).
    """
    rid, unit, role = row["id"], row["unit"], row["role"]
    targets, problems = [], []

    for line_id, pos in sorted(positions.items()):
        if pos["status"] != "active":
            continue
        conflict = (pos.get("reservoir_conflicts") or {}).get(role)
        if conflict:
            # Two records of the same fact, disagreeing. Picking one is
            # exactly what a module built to refuse rather than guess must
            # not do -- and a crossed pair credits each bottle the other's
            # volume.
            if rid in (conflict["reservoirs"], conflict["source_reservoirs"]):
                problems.append(
                    "%s's two records of its %s reservoir disagree: `reservoirs` says %s, "
                    "`pg_regime.source_reservoirs` says %s"
                    % (line_id, role, conflict["reservoirs"], conflict["source_reservoirs"]))
            continue
        if (pos.get("reservoirs") or {}).get(role) != rid:
            continue
        if pos["unit"] is None or pos["vial"] is None:
            problems.append(
                "%s draws from %s but is not on an evolver right now (unit/vial are "
                "null), so its share of the draw cannot be attributed" % (line_id, rid))
            continue
        if pos["unit"] != unit:
            problems.append("%s is on %s but %s is a %s reservoir"
                            % (line_id, pos["unit"], rid, unit))
            continue
        targets.append({"line_id": line_id, "unit": pos["unit"], "vial": pos["vial"],
                        "role": role})

    # lines_fed as a cross-check, never as the roster.
    listed = row.get("lines_fed") or []
    derived = {t["line_id"] for t in targets}
    stale, contradicting = [], []
    for line_id in listed:
        if line_id in derived:
            continue
        pos = positions.get(line_id)
        if pos is None or pos["status"] != "active":
            stale.append(line_id)          # the expected, harmless kind
        elif not (pos.get("reservoirs") or {}):
            contradicting.append("%s has no reservoir record of its own" % line_id)
        else:
            contradicting.append("%s names %s as its %s reservoir"
                                 % (line_id, (pos["reservoirs"] or {}).get(role) or "nothing",
                                    role))
    if contradicting:
        problems.append(
            "%s's lines_fed lists active line(s) that do not name it back: %s"
            % (rid, "; ".join(contradicting)))

    if problems:
        return [], ("cannot attribute dispenses for %s: %s. Counting only the "
                    "remaining lines would understate the draw and overstate "
                    "what is left in the bottle" % (rid, "; ".join(problems))), []
    if not targets:
        return [], ("no active line names %s as its %s reservoir, so no vial's dispenses "
                    "belong to it" % (rid, role)), []

    seen = {}
    for t in targets:
        key = (t["unit"], t["vial"])
        if key in seen:
            return [], ("%s is fed by both %s and %s at %s vial %s, so that vial's "
                        "dispenses would be counted twice"
                        % (rid, seen[key], t["line_id"], t["unit"], t["vial"])), []
        seen[key] = t["line_id"]

    notes = []
    if stale:
        notes.append(
            "%s's lines_fed still names %s, which %s no longer active -- that field is "
            "hand-maintained and this server does not update it, so it was used only as "
            "a cross-check and the draw was attributed from the lines themselves"
            % (rid, ", ".join(sorted(stale)), "are" if len(stale) > 1 else "is"))
    return targets, None, notes


# ─── the rig side ─────────────────────────────────────────────────────────────

class _Fetched:
    """A body already read into memory, with just the surface UnitClient uses."""

    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        import json as _json

        return _json.loads(self.content)


class _FetchProblem(Exception):
    """A fetch that produced no usable response, with the reason already
    written for a human. Raised rather than returned so that no code path can
    treat it as an answer."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _strip_userinfo(url: str) -> str:
    """http://user:pw@host:8050 -> http://host:8050"""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if not rest:
        return url
    _, _, hostpart = rest.rpartition("@")
    return "%s://%s" % (scheme, hostpart) if scheme else hostpart


def _looks_like_json(response) -> bool:
    """Is this a JSON body, or a dashboard's own HTML page?

    Content-Type first, then a cheap sniff, because a Dash catch-all returns
    text/html with HTTP 200 for a route it does not have -- which is exactly
    what a rig predating /api/v1/consumption does."""
    ctype = (response.headers.get("content-type") or "").lower()
    if "json" in ctype:
        return True
    if "html" in ctype or "text/plain" in ctype:
        return False
    return (response.text or "").lstrip()[:1] in ("{", "[")


_CONTROL = {c: None for c in list(range(0, 9)) + list(range(11, 32)) + [127]
            + list(range(128, 160))
            # bidi marks, embeddings, overrides and isolates: they make rig
            # text DISPLAY differently from what it says
            + [0x200E, 0x200F] + list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))}
MAX_RIG_TEXT = 400


def _text(value, limit: int = MAX_RIG_TEXT) -> str | None:
    """A string from a rig, made safe to repeat.

    This module checks every NUMBER crossing the rig boundary and had nothing
    for strings -- while deliberately relaying the rig's own diagnosis, which
    is the right call (the rig's diagnosis is better than a status code) and
    which also made the rig an author of this server's prose. GET /skill tells
    the client to surface `pump.reason` and calls `attention` the list most
    worth summarising, so a rig string that reads as an instruction operates
    on the reader rather than the arithmetic, and defeats every refusal here
    at once. A 255 KB `error` produced a 261,172-character reason, copied
    about 24 times across one response on the live geometry.

    So: strings only, control characters removed, length bounded, and the
    caller attributes it to the rig in words the rig cannot forge.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.translate(_CONTROL).strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        cleaned = "%s... (truncated, %d characters)" % (cleaned[:limit], len(value))
    return cleaned


def _quoted(value, limit: int = MAX_RIG_TEXT) -> str | None:
    """Rig text wrapped in an attribution it cannot write itself."""
    cleaned = _text(value, limit)
    return None if cleaned is None else 'the rig reported: "%s"' % cleaned


def _number(value):
    """A finite, non-boolean number, or None.

    A rig is a separate machine running code this server does not control, so
    every number crossing that boundary is checked rather than trusted. `True`
    is excluded deliberately: bool is a subclass of int, and a JSON `true`
    arriving where millilitres belong would otherwise be summed as 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):   # NaN/inf
        return None
    return float(value)


def _vials_by_number(body: dict) -> tuple[dict, str | None]:
    """Index a rig's `vials` array, refusing every shape that would otherwise
    crash or silently lose a vial."""
    vials = body.get("vials")
    if not isinstance(vials, list):
        return {}, "the rig's `vials` field is %s, not a list" % type(vials).__name__
    out = {}
    for entry in vials:
        if not isinstance(entry, dict):
            return {}, "the rig's `vials` list holds a %s, not an object" % type(entry).__name__
        if "vial" not in entry:
            return {}, "the rig reported a vial entry with no `vial` number"
        raw = entry["vial"]
        # int() alone accepted True (-> 1) and 1.9 (-> 1), and raised
        # OverflowError -- a 500, uncaught -- on the JSON token Infinity.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                or raw != raw or raw in (float("inf"), float("-inf")) or int(raw) != raw:
            return {}, ("the rig reported a vial number that is not a whole number: %s"
                        % (_text(repr(raw), 60) or "unprintable"))
        number = int(raw)
        if number in out:
            # Last-entry-wins would silently discard the real volume, in the
            # direction that says there is more media left than there is.
            return {}, ("the rig reported vial %d twice, so which row is the real "
                        "dispense record is undecidable" % number)
        out[number] = entry
    return out, None


class UnitClient:
    """One eVOLVER dashboard, fetched over HTTP.

    Prefers /api/v1/consumption, which integrates from one instant on the side
    that owns the controller clock. Falls back to aggregating /dispenses here
    when a rig has not been updated yet -- correct but far heavier (one call
    per vial, every event since the anchor over the wire), so the response says
    which path produced the number.
    """

    def __init__(self, unit: str, base: str, client, timeout_s: float, now=None):
        self.unit = unit
        self.base = base.rstrip("/")
        # Never echo userinfo. `sources[unit].url` is this string, and
        # GET /media needs no token -- so a reverse-proxy password written
        # into viewer.config.json (git-tracked, so already half-lost) was
        # published in full, seven times, on an unauthenticated port.
        self.safe_base = _strip_userinfo(self.base)
        self.client = client
        self.timeout_s = timeout_s
        self._now = now
        self._deadline = None
        self.budget_s = PUMP_BUDGET_S  # named in the messages; the caller sets the deadline
        self._summary = None          # cached /api/v1/vials, for the fallback

    def _get(self, path: str, params: dict | None = None):
        """Fetch with a byte cap AND a clock, both enforced as the body arrives.

        Three things this has to survive, each of which defeated an earlier
        version:

        A read timeout bounds a single socket read, not a response. A rig
        dribbling one byte every 2.5 s under a 3 s timeout never trips it, so
        the deadline is re-checked on every chunk rather than only between
        fetches -- measured before this: a single GET /media held a worker for
        60 s against an 8 s budget, and MAX_BODY_BYTES at that rate is 7.6
        days away.

        `client.get` reads and decodes the whole body first, so a compression
        bomb is already expanded before anything can look at it. Streaming
        helps, but `iter_bytes()` still inflates a whole network read at once,
        which let a 200 KB read become 64 MB in one chunk. Asking for
        `identity` removes the amplification entirely: these payloads are
        kilobytes, and nothing here needs compression.

        Raises _FetchProblem rather than returning a sentinel, so no caller can
        mistake an abandoned body for a legitimate answer -- which is how an
        over-cap response used to be classified as "this dashboard has no
        /consumption" and silently rerouted to the fallback.
        """
        budget = self.timeout_s
        if self._deadline is not None:
            budget = min(budget, max(self._deadline - monotonic(), 0.05))
        try:
            with self.client.stream("GET", self.base + path, params=params or {},
                                    timeout=budget,
                                    headers={"Accept-Encoding": "identity"}) as response:
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    if self._deadline is not None and monotonic() >= self._deadline:
                        raise _FetchProblem(
                            "%s was still sending after this request's %.0f s budget ran "
                            "out (%d bytes so far). A slow trickle defeats a per-read "
                            "timeout by construction, so the request is abandoned here"
                            % (self.safe_base, self.budget_s, total))
                    total += len(chunk)
                    if total > MAX_BODY_BYTES:
                        raise _FetchProblem(
                            "%s's answer passed %d KB and was abandoned. A media summary "
                            "is a few kilobytes; this is either the wrong endpoint or a "
                            "rig fault" % (self.safe_base, MAX_BODY_BYTES // 1024))
                    chunks.append(chunk)
                return _Fetched(response.status_code, response.headers, b"".join(chunks))
        except _FetchProblem:
            raise
        except Exception as exc:
            # A timeout the SERVER imposed by truncating the budget is not
            # evidence that the rig is down, and saying "it may be down, on
            # another network, or bound to 127.0.0.1" about a rig that
            # answered twice in the same response is a confident wrong
            # statement about facility health.
            if self._deadline is not None and monotonic() >= self._deadline - 0.06:
                raise _FetchProblem(
                    "%s did not answer within the time left in this request's %.0f s "
                    "budget. That is this server's limit, not evidence about the rig"
                    % (self.safe_base, self.budget_s)) from exc
            raise _FetchProblem(self._unreachable(exc)) from exc

    def _accept(self, response, what: str) -> tuple[dict | None, str | None]:
        """Status, shape and JSON-ness, in ONE place for every fetch site.

        Applied at only two of the three sites, the 2xx rule left
        `_summary_once` reading `status_code < 400` and calling `.json()`
        directly -- so a 301 on /api/v1/vials still fed the controller clock
        the whole fallback conversion rests on.
        """
        if not 200 <= response.status_code < 300:
            # The rig's own _unavailable() always answers {"ok": false,
            # "error": "<reason>"}. Discarding that and printing the status
            # turned "this rig has not written a log line for 100 h" into
            # "answered HTTP 503 ... a redirect says the resource is somewhere
            # else" -- wrong, and less useful than what the rig had already
            # worked out and sent.
            said = None
            try:
                said = (response.json() or {}).get("error")
            except Exception:
                said = None
            said = _quoted(said)
            if said:
                return None, "%s refused %s -- %s" % (self.safe_base, what, said)
            return None, ("%s answered HTTP %s for %s; only a 2xx is a measurement"
                          % (self.safe_base, response.status_code, what))
        return self._body(response, what)

    def _body(self, response, what: str) -> tuple[dict | None, str | None]:

        """A rig can answer 200 with anything at all -- a captive-portal login
        page, a JSON array, a bare string. Each of those used to reach an
        attribute access and 500 the whole route, taking the level-derived
        answer down with the pump view."""
        try:
            body = response.json()
        except Exception:
            return None, ("%s answered %s with a body that is not JSON"
                          % (self.safe_base, what))
        if not isinstance(body, dict):
            return None, ("%s answered %s with a JSON %s, not an object"
                          % (self.safe_base, what, type(body).__name__))
        return body, None

    def consumption(self, since_iso: str, vials: list[int], deadline=None) -> dict:
        """{ok: True, ...} or a named unavailability. Never raises."""
        self._deadline = deadline
        try:
            r = self._get("/api/v1/consumption", {"since": since_iso})
        except _FetchProblem as problem:
            return {"ok": False, "reason": problem.reason}
        except Exception as exc:
            return {"ok": False, "reason": self._unreachable(exc)}

        # A dashboard without this route does NOT answer 404: Dash's catch-all
        # serves index.html with HTTP 200, so a 404-only trigger meant the
        # fallback -- the whole reason this feature works on rigs as they are
        # today -- never fired against a single real rig. Anything that is not
        # JSON carries the same message: this dashboard has no /consumption.
        if r.status_code == 404 or (r.status_code < 300 and not _looks_like_json(r)):
            return self._via_dispenses(since_iso, vials)
        # 200-399 all used to fall through to the body, so a 301 whose body
        # happened to parse produced a confident `pump_integrated` number from
        # a response saying "this resource is not here".
        body, problem = self._accept(r, "/api/v1/consumption")
        if problem:
            return {"ok": False, "reason": problem}

        if body.get("schema") != CONSUMPTION_SCHEMA:
            # Without this, ANY JSON object lacking clock_ok/covers_window --
            # a captive portal's {"error": ...}, a redirect body -- was
            # reported as "the rig restarted, so its pump log describes a
            # different run", and an operator would go power-cycle a healthy
            # rig. The refusal was right; the diagnosis was invented.
            return {"ok": False, "reason":
                    "%s answered /api/v1/consumption with a JSON object that is not an %s "
                    "payload (schema=%r). Something other than an eVOLVER dashboard is "
                    "answering at this address"
                    % (self.safe_base, CONSUMPTION_SCHEMA, body.get("schema"))}
        bad = self._identity_problem(body)
        if bad:
            return {"ok": False, "reason": bad}
        # Both flags default to FALSE. An older or foreign /consumption that
        # omits them is not thereby trustworthy -- pump_calibration already
        # defaulted this way, and the other two were the odd ones out.
        if not body.get("clock_ok", False):
            return {"ok": False, "reason":
                    "%s's controller clock is behind the requested instant (elapsed_h=%s, "
                    "since_h=%s). The rig restarted, so its pump log describes a different "
                    "run than the one this reading belongs to"
                    % (self.unit, body.get("elapsed_h"), body.get("since_h"))}
        if not body.get("covers_window", False):
            return {"ok": False, "reason":
                    "%s's pump log begins after this reservoir's last reading, so every "
                    "volume would be a lower bound -- which makes the remaining level an "
                    "UPPER bound, the optimistic direction" % self.unit}
        if body.get("clock_problem"):
            # The rig works this out and sends it. Ignoring it produced the
            # worst answer this feature has given: on a restored experiment
            # directory the server saw zero dispenses and reported drawn_L 0.0
            # with a divergence_note alleging "a leak, an unlogged loss, a
            # pump calibration that has drifted" and a quiet_note insisting
            # "That is a real measurement, not missing data". Every word false,
            # about a case the rig had already diagnosed correctly.
            said = _quoted(body["clock_problem"])
            return {"ok": False, "reason":
                    "%s cannot date its own writes -- %s" % (self.unit, said)}
        skew = self._clock_skew(body.get("generated_at"))
        if skew:
            return {"ok": False, "reason": skew}
        if body.get("pump_calibration") is not True:
            return {"ok": False, "reason":
                    "%s reports no usable pump calibration (pump_calibration=%r), so pump "
                    "seconds cannot be converted to millilitres at all"
                    % (self.unit, body.get("pump_calibration"))}

        per_vial, problem = _vials_by_number(body)
        if problem:
            return {"ok": False, "reason": "%s: %s" % (self.safe_base, problem)}

        # Deliberately NOT checked here: whether every vial asked for is
        # present. One fetch is shared by every reservoir read at the same
        # instant, so refusing the whole fetch for a missing vial punished
        # reservoirs that never fed it -- with a reason asserting they did.
        # _measure raises it per reservoir, where it is true.
        return {"ok": True, "source": "consumption", "vials": per_vial,
                # Typed and bounded like everything else: this rides a
                # pump_integrated block, the one a client is told to act on,
                # and arbitrary attacker-shaped JSON was landing inside it.
                "experiment": _text(body.get("experiment"), 120), "elapsed_h": body.get("elapsed_h"),
                "generated_at": body.get("generated_at")}

    def _summary_once(self) -> dict:
        """The unit's summary, fetched once per SUCCESS.

        A failure is not cached. It used to be, so one transient blip -- or
        one read truncated by this request's own budget -- refused every later
        anchor on that unit without ever re-asking, and told the operator to
        deploy new rig code because of a hiccup.
        """
        if self._summary is None:
            try:
                r = self._get("/api/v1/vials")
            except _FetchProblem as problem:
                return {"__error__": problem.reason}
            except Exception as exc:
                return {"__error__": "%s: %s" % (type(exc).__name__, exc)}
            body, problem = self._accept(r, "/api/v1/vials")
            if problem:
                return {"__error__": problem}
            self._summary = body
        return self._summary

    def _via_dispenses(self, since_iso: str, vials: list[int]) -> dict:
        """Older dashboard: no /consumption. Do its job here instead.

        /dispenses takes controller hours, so the wall-clock anchor has to be
        converted using this module's documented identity -- which needs
        generated_at and elapsed_h from the summary endpoint first.
        """
        summary = self._summary_once()
        if not isinstance(summary, dict):
            summary = {"__error__": "the rig's /api/v1/vials is not a JSON object"}
        if "__error__" in summary:
            return {"ok": False, "reason":
                    "%s has no /api/v1/consumption, and its /api/v1/vials could not be read "
                    "either (%s), so the fallback has no controller clock to convert "
                    "against. If this dashboard predates the endpoint, deploying the "
                    "current tools/evolver_api.py there is the fix"
                    % (self.safe_base, summary["__error__"])}
        bad = self._identity_problem(summary)
        if bad:
            return {"ok": False, "reason": bad}
        if summary.get("pump_calibration") is not True:
            return {"ok": False, "reason":
                    "%s reports no usable pump calibration (pump_calibration=%r), so pump "
                    "seconds cannot be converted to millilitres at all"
                    % (self.unit, summary.get("pump_calibration"))}

        if summary.get("schema") not in (None, "or05.vials/1"):
            return {"ok": False, "reason":
                    "%s's /api/v1/vials reports schema %r, so whatever is answering here "
                    "is not an eVOLVER dashboard" % (self.safe_base, summary.get("schema"))}
        if summary.get("clock_problem"):
            said = _quoted(summary["clock_problem"])
            return {"ok": False, "reason":
                    "%s cannot date its own writes -- %s" % (self.unit, said)}
        skew = self._clock_skew(summary.get("generated_at"))
        if skew:
            return {"ok": False, "reason": skew}

        generated_at, elapsed_h = summary.get("generated_at"), summary.get("elapsed_h")
        if _number(elapsed_h) is None:
            return {"ok": False, "reason":
                    "%s reports elapsed_h as %r, which is not a usable controller clock"
                    % (self.unit, elapsed_h)}
        if not generated_at or elapsed_h is None:
            return {"ok": False, "reason":
                    "%s's /api/v1/vials reports no generated_at/elapsed_h, so a wall-clock "
                    "anchor cannot be converted to controller time" % self.unit}
        try:
            delta_h = (_parse(generated_at) - _parse(since_iso)).total_seconds() / 3600.0
        except (ValueError, TypeError) as exc:
            # TypeError, not just ValueError: a naive or non-string
            # generated_at raises that instead, and this method is documented
            # as never raising -- an uncaught one here is a 500 for the whole
            # /media request, on the path added for older rigs.
            return {"ok": False, "reason":
                    "%s's generated_at (%r) is not a timestamp this server can use: %s"
                    % (self.unit, generated_at, exc)}
        since_h = float(elapsed_h) - delta_h
        if since_h > float(elapsed_h) + 1e-6:
            return {"ok": False, "reason":
                    "%s's controller clock is behind the requested instant; the rig "
                    "restarted since this reading" % self.unit}
        if since_h < -1e-6:
            return {"ok": False, "reason":
                    "%s's pump log begins after this reservoir's last reading, so every "
                    "volume would be a lower bound" % self.unit}

        out = {}
        for vial in vials:
            if self._deadline is not None and monotonic() >= self._deadline:
                # The fallback is one request PER VIAL, which is where the
                # unbounded wall time came from. Stopping mid-way and saying so
                # beats a partial sum presented as a measurement.
                return {"ok": False, "reason":
                        "%s ran out of this request's time budget after %d of %d vials. "
                        "A partial sum would understate the draw, so none is reported. "
                        "Deploying the current tools/evolver_api.py there replaces %d "
                        "requests with one"
                        % (self.unit, len(out), len(vials), len(vials))}
            try:
                r = self._get("/api/v1/vials/%d/dispenses" % vial, {"since_h": since_h})
            except _FetchProblem as problem:
                return {"ok": False, "reason": problem.reason}
            except Exception as exc:
                return {"ok": False, "reason": self._unreachable(exc)}
            body, problem = self._accept(r, "vial %d's dispenses" % vial)
            if problem:
                return {"ok": False, "reason": problem}
            if body.get("pump_calibration") is not True:
                return {"ok": False, "reason":
                        "%s reports no pump calibration for vial %d" % (self.unit, vial)}
            rows = body.get("dispenses")
            if not isinstance(rows, list):
                return {"ok": False, "reason":
                        "%s returned vial %d's dispenses as %s, not a list"
                        % (self.safe_base, vial, type(rows).__name__)}
            low = high = 0.0
            n = 0
            first = last = None
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 3:
                    return {"ok": False, "reason":
                            "%s returned a malformed dispense row for vial %d (%r); this "
                            "window cannot be integrated" % (self.safe_base, vial, row)}
                t, mL, role = row[0], row[1], row[2]
                mL = _number(mL)
                if mL is None:
                    return {"ok": False, "reason":
                            "%s returned a dispense on vial %d with no usable volume, so "
                            "this window cannot be integrated" % (self.unit, vial)}
                n += 1
                t = _number(t)
                if t is not None:
                    first = t if first is None else min(first, t)
                    last = t if last is None else max(last, t)
                if role == "low":
                    low += mL
                elif role == "high":
                    high += mL
                else:
                    # `else: high += mL` charged every unrecognised role string
                    # to the drug bottle -- and the rig's two endpoints disagree
                    # about those rows, /consumption dropping them while
                    # /dispenses labels them "high". Same rig, same window, a
                    # different answer depending on which code it is running.
                    return {"ok": False, "reason":
                            "%s reported a dispense on vial %d with role %r, which is "
                            "neither low nor high. Charging it to either bottle would be "
                            "a guess" % (self.unit, vial, role)}
            out[vial] = {"vial": vial, "low_mL": round(low, 3), "high_mL": round(high, 3),
                         "total_mL": round(low + high, 3), "n_events": n,
                         "first_event_h": first, "last_event_h": last}

        # The bound is not automatic. It exists because an un-updated rig's
        # /api/v1/vials reports elapsed_h as its LAST WRITE, so the conversion
        # opens the window early by the rig's idle time. A rig running the
        # current evolver_api reports `staleness_h` from a corrected clock and
        # its conversion is exact -- calling that a bound would put a permanent
        # caveat on every real number this feature produces, and tell the
        # operator to deploy the very code already running.
        return {"ok": True, "source": "dispenses", "vials": out,
                # Through _number(), like every other value crossing this
                # boundary. A presence test called 500.0 (the rsync'd-mtime
                # rig), -12.0 (a future-dated one) and the string "yes" all
                # "exact". A staleness this server would not trust is a bound.
                "drawn_is_upper_bound": not (
                    (_number(summary.get("staleness_h")) or 0.0) >= 0
                    and (_number(summary.get("staleness_h")) is not None)
                    and abs(_number(summary.get("staleness_h"))) < 48.0),
                "elapsed_h": elapsed_h,
                "generated_at": generated_at}

    def _clock_skew(self, generated_at) -> str | None:
        """Refuse a rig whose wall clock disagrees with this server's.

        The rig resolved the anchor against its own clock. If that clock is
        wrong, the volumes are real but belong to a different interval than the
        one asked about -- and the error runs in the optimistic direction as
        often as not, reporting more media left than there is."""
        if not isinstance(generated_at, str):
            return ("%s reports generated_at as a %s, not a timestamp, so its clock cannot "
                    "be checked against this server's"
                    % (self.safe_base, type(generated_at).__name__))
        shown = _text(generated_at, 60) or "blank"
        try:
            moment = _parse(generated_at)
        except ValueError:
            return "%s reports an unparseable generated_at (%s)" % (self.safe_base, shown)
        if moment.tzinfo is None:
            return ("%s reports generated_at with no UTC offset (%s), so the interval it "
                    "integrated cannot be placed on this server's clock"
                    % (self.safe_base, shown))
        reference = self._now or datetime.now().astimezone()
        drift_min = abs((reference - moment).total_seconds()) / 60.0
        if drift_min > RIG_SKEW_TOLERANCE_MIN:
            return ("%s's clock is %.0f minutes from this server's (it reports %s). It "
                    "resolved the anchor against that clock, so its volumes describe a "
                    "different interval than the one asked about"
                    % (self.unit, drift_min, shown))
        return None

    def _identity_problem(self, body: dict) -> str | None:
        """The check that stops one rig's pump data being painted onto the
        other rig's bottles. Two dashboards on one host take ports by start
        order, so a swapped URL is a configuration mistake that otherwise
        produces confident, entirely wrong numbers. viewer.html applies exactly
        this rule; so does tools/check_api.py.

        A dashboard that reports NO name is tolerated, as it is there: an
        unrecognised control IP degrades identity rather than lying about it.
        """
        got = _text(body.get("evolver"), 80)
        if got and got != self.unit:
            return ("%s answers for evolver %r, not %r -- check the unit URLs (two "
                    "dashboards on one host take ports by start order)"
                    % (self.safe_base, got, self.unit))
        return None

    def _unreachable(self, exc: Exception) -> str:
        return ("%s is unreachable (%s: %s). It may be down, on another network, or "
                "bound to 127.0.0.1 rather than 0.0.0.0"
                % (self.safe_base, type(exc).__name__, exc))


# ─── the two halves, joined ───────────────────────────────────────────────────

def at_is_present(at: str, now: datetime | None = None,
                  tolerance_min: float = AT_TOLERANCE_MIN) -> str | None:
    """None when `at` is close enough to now for live data to answer it."""
    now = now or datetime.now().astimezone()
    try:
        moment = _parse(at)
    except ValueError as exc:
        return "at is not a timestamp this server can parse: %s" % exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo)
    drift = abs((now - moment).total_seconds()) / 60.0
    if drift >= tolerance_min:
        direction = "in the past" if moment < now else "in the future"
        return ("at is %.0f minutes %s and the rigs report only the present, so no pump "
                "measurement covers it. Omit `at`, or read the level-derived figures, "
                "which do answer for any instant" % (drift, direction))
    return None


def build_pump_view(log: dict, rows: list[dict], at: str, dash, client,
                    now: datetime | None = None) -> tuple[dict, dict]:
    """(per-reservoir blocks keyed by reservoir id, per-unit source notes).

    Every active row gets a block: either a measurement or a named reason it
    is absent. A row with no block at all would read as "nothing to say here",
    which is the one thing this must never mean.
    """
    blocks: dict[str, dict] = {}
    sources: dict[str, dict] = {}

    active = [r for r in rows if r.get("status") == "active"]
    if not active:
        return blocks, sources

    stale_at = at_is_present(at, now=now)
    if stale_at:
        for r in active:
            blocks[r["id"]] = unavailable(stale_at)
        for unit in sorted({r["unit"] for r in active}):
            sources[unit] = {"ok": False, "reason": stale_at, "consulted": False}
        return blocks, sources

    positions = line_positions(log)
    clients: dict[str, UnitClient] = {}
    plans: list[dict] = []

    for row in active:
        rid, unit = row["id"], row["unit"]
        anchor = row.get("level_as_of")
        if not anchor:
            blocks[rid] = unavailable(
                "%s has no level_as_of, so there is no instant to measure from" % rid)
            continue
        if row.get("level_L") is None:
            blocks[rid] = unavailable(
                "%s has no recorded level, so a measured draw has nothing to subtract from"
                % rid)
            continue

        url = dash.url_for(unit)
        if not url:
            blocks[rid] = unavailable(dash.why_not(unit))
            sources.setdefault(unit, {"ok": False, "reason": dash.why_not(unit),
                                      "consulted": False, "fetches": []})
            continue

        # ANCHOR RESOLUTION. reservoirs.items is a projection; the events are
        # the source of truth. When a level_reading is appended to the file by
        # hand, POST /events never ran project_reservoir_state and the block
        # lags it -- on the live log all eight active bottles lag ~18 h, every
        # stored level HIGHER than the reading that superseded it, which is
        # the direction that says there is more media than there is.
        #
        # This used to refuse outright. Refusing every bottle for weeks
        # because a projection lagged is too brittle a response to something
        # that will recur -- and the refusal had to FIND the newer reading in
        # order to fire, so it can simply use it. Nothing is invented: this is
        # the log's own reported event, preferred over a stale derived copy of
        # it. tools/media.py already does exactly this for the RATE
        # (readings_for scans the events); only level_L came from the block,
        # and that inconsistency is the defect rather than something to route
        # around.
        anchor_source, anchor_event, anchor_note = "reservoir_block", None, None
        level = row["level_L"]
        newer = newest_reading_after(log, rid, anchor)
        if newer:
            anchor_source, anchor_event = "event", newer["event_id"]
            anchor_note = (
                "measured from %s's reading of %s L at %s, not the %s L of %s stored in "
                "reservoirs.items -- that block was never updated for this event, so the "
                "level-derived fields beside this one still use the stored value and "
                "differ from this block by %.3f L. POST /events projects a reading onto "
                "the block; an event appended to the file by hand never had it done, and "
                "tools/lineage.py --write will NOT do it (it recomputes lineage only)"
                % (newer["event_id"], newer["volume"], newer["timestamp"], level, anchor,
                   abs((level or 0.0) - newer["volume"])))
            anchor, level = newer["timestamp"], newer["volume"]
        targets, refusal, join_notes = reservoir_targets(row, positions)
        if refusal:
            blocks[rid] = unavailable(refusal)
            continue

        changes = mapping_changes_since(log, rid, unit, row.get("lines_fed") or [], anchor)
        if changes:
            blocks[rid] = unavailable(
                "the reservoir-to-vial map changed after %s's last reading (%s), so "
                "dispenses since then cannot be attributed to it with confidence"
                % (rid, ", ".join("%s %s at %s" % (c["event_id"], c["event_type"],
                                                   c["timestamp"]) for c in changes[:4])),
                events=[c["event_id"] for c in changes])
            continue

        clients.setdefault(unit, UnitClient(unit, url, client, dash.timeout_s))
        plans.append({"row": row, "anchor": anchor, "targets": targets, "level": level,
                      "anchor_source": anchor_source, "anchor_event": anchor_event,
                      "anchor_note": anchor_note, "join_notes": join_notes})

    # One call per (unit, anchor): reservoirs read at the same moment -- the
    # normal case, since levels are read in rounds -- share a single request.
    #
    # Two bounds around that loop. A DEADLINE, because per-request timeouts sum
    # without limit: a rig answering just under the timeout produced an 84 s
    # successful request, and the fan-out reaches 126 s worst case from
    # ordinary operator behaviour. And a CEILING on planned fetches, so that
    # number is visible as a refusal rather than discovered as wall time.
    keys = sorted({(p["row"]["unit"], p["anchor"]) for p in plans})
    if len(keys) > MAX_FETCHES:
        reason = ("this request would need %d separate rig fetches (%d reservoir groups "
                  "read at different instants). That is past the %d-fetch ceiling, and "
                  "the wall time would exceed any caller's patience. Narrow it with "
                  "?unit=, or log a level round so the bottles share an anchor"
                  % (len(keys), len(keys), MAX_FETCHES))
        for plan in plans:
            blocks[plan["row"]["id"]] = unavailable(reason)
        return blocks, sources

    deadline = monotonic() + PUMP_BUDGET_S
    fetched: dict[tuple[str, str], dict] = {}
    for plan in plans:
        row, anchor, targets = plan["row"], plan["anchor"], plan["targets"]
        unit = row["unit"]
        key = (unit, anchor)
        if key not in fetched:
            if monotonic() >= deadline:
                fetched[key] = {"ok": False, "reason":
                                "the rigs did not answer within this request's %.0f s "
                                "budget, so %s was not asked. Every figure beside this one "
                                "is from bottle level readings and is unaffected"
                                % (PUMP_BUDGET_S, unit)}
            else:
                vials = sorted({t["vial"] for p in plans if p["row"]["unit"] == unit
                                and p["anchor"] == anchor for t in p["targets"]})
                fetched[key] = clients[unit].consumption(anchor, vials, deadline=deadline)
        result = fetched[key]

        # A unit is fetched once per DISTINCT anchor, and reservoirs are often
        # read in staggered rounds -- the live log has two anchors per unit
        # right now. An earlier version let the first fetch define the unit's
        # entry, so `sources` could report a healthy rig that had just refused
        # a bottle, or a dead one next to a real measurement from the same
        # unit in the same response. Every fetch is recorded; the unit is `ok`
        # only if all of them were.
        entry = sources.setdefault(unit, {"url": clients[unit].safe_base, "consulted": True,
                                          "fetches": []})
        entry["consulted"] = True
        entry.setdefault("fetches", [])
        if not any(f["since"] == anchor for f in entry["fetches"]):
            fetch = {"since": anchor, "ok": bool(result.get("ok"))}
            if result.get("ok"):
                fetch.update({"source": result["source"],
                              "experiment": result.get("experiment"),
                              "elapsed_h": result.get("elapsed_h"),
                              "generated_at": result.get("generated_at")})
            else:
                fetch["reason"] = result.get("reason")
            entry["fetches"].append(fetch)
        entry["ok"] = all(f["ok"] for f in entry["fetches"])
        entry.pop("reason", None)
        failed = [f["reason"] for f in entry["fetches"] if not f["ok"] and f.get("reason")]
        if failed:
            entry["reason"] = failed[0] if len(failed) == 1 else "; ".join(failed)
        ok_fetch = next((f for f in entry["fetches"] if f["ok"]), None)
        if ok_fetch:
            entry["source"] = ok_fetch["source"]
            entry["experiment"] = ok_fetch.get("experiment")
            entry["elapsed_h"] = ok_fetch.get("elapsed_h")
            entry["generated_at"] = ok_fetch.get("generated_at")

        if not result.get("ok"):
            blocks[row["id"]] = unavailable(result.get("reason", "the unit could not be read"))
            continue

        try:
            blocks[row["id"]] = _measure(
                row, anchor, targets, result, at, level=plan["level"],
                anchor_source=plan["anchor_source"], anchor_event=plan["anchor_event"],
                anchor_note=plan["anchor_note"], join_notes=plan["join_notes"])
        except Exception as exc:                    # noqa: BLE001 -- deliberate
            # Isolated per reservoir. Without this, an arithmetic fault on one
            # bottle of one unit reached the route's catch-all and blanked all
            # eight on both units, emptying `sources` so the response also
            # claimed no rig had been contacted.
            blocks[row["id"]] = unavailable(
                "this reservoir's measurement failed with %s: %s. Every other reservoir "
                "in this response is unaffected" % (type(exc).__name__, exc))

    return blocks, sources


def _measure(row: dict, anchor: str, targets: list[dict], result: dict, at: str,
             level=None, anchor_source: str = "reservoir_block",
             anchor_event: str | None = None, anchor_note: str | None = None,
             join_notes: list | None = None) -> dict:
    per_vial = result["vials"]
    role_key = "low_mL" if row["role"] == "low" else "high_mL"

    drawn_mL = 0.0
    n_events = 0
    quiet = []
    unpriced: list[dict] = []
    unreadable: list[dict] = []
    missing_counts: list[int] = []
    block_events_are_role_specific: list[bool] = []
    for t in targets:
        rec = per_vial.get(t["vial"])
        if rec is None:
            return unavailable(
                "%s does not report vial %d, which the log says feeds %s. Check to_run "
                "in that unit's experiment_parameters.yaml"
                % (row["unit"], t["vial"], row["id"]))
        if rec.get("problem"):
            # The rig hands over an actionable diagnosis ("input_pump2 is 99,
            # outside the 16 calibrated pumps. custom_script defaults it to
            # 32+vial when the yaml omits it"). Replacing it with "reports no
            # usable volume" threw away the only part that tells an operator
            # what to do.
            return unavailable("%s, vial %d -- %s"
                               % (row["unit"], t["vial"], _quoted(rec["problem"])))
        gap = _number(rec.get("log_gap_h"))
        if gap and gap > 0.01:
            # The rig splits this out of covers_window precisely so a
            # truncated or rotated log does not read as 0 mL. Reading only the
            # rig-wide flag -- a min across vials -- let one untouched vial
            # certify coverage for a vial missing 3.8 h of its own history,
            # understating the draw while quiet_note called it "a real
            # measurement, not missing data".
            return unavailable(
                "%s's pump log for vial %d begins %.2f h after this window opened, so its "
                "draw is missing that stretch entirely. The gap is a gap, not zero "
                "consumption" % (row["unit"], t["vial"], gap))
        dropped = _number(rec.get("dropped_rows"))
        if dropped:
            unreadable.append({"vial": t["vial"], "rows": int(dropped)})
        value = _number(rec.get(role_key))
        if value is None:
            return unavailable(
                "%s reports no usable %s-pump volume for vial %d (%r), so this "
                "reservoir's draw cannot be integrated"
                % (row["unit"], row["role"], t["vial"], rec.get(role_key)))
        if value < 0:
            # A negative draw would make the bottle GROW -- the same
            # physically impossible confident number that
            # _clamp_backward_extrapolation exists to stop on the
            # level-derived side.
            return unavailable(
                "%s reports a negative %s-pump volume for vial %d (%s mL). A bottle "
                "cannot refill itself, so this is a rig-side fault, not a measurement"
                % (row["unit"], row["role"], t["vial"], value))
        drawn_mL += value
        stray = _number(rec.get("unrecognised_rows"))
        if stray:
            # /consumption counts rows whose pump column is neither in1 nor
            # in2 and prices none of them; /dispenses labels the same rows
            # "high" and charges them to the drug bottle. The server cannot
            # see that from the fallback, but where the rig does report it,
            # saying so beats a silently smaller number.
            unpriced.append({"vial": t["vial"], "rows": int(stray)})
        # Prefer the rig's per-role count where it offers one: a low
        # reservoir's block reporting the vial's combined event count invites
        # the wrong sanity check against a low-only volume.
        role_events = _number(rec.get("%s_events" % row["role"]))
        count = role_events if role_events is not None else _number(rec.get("n_events"))
        if count is None:
            missing_counts.append(t["vial"])
        n_events += int(count) if count is not None else 0
        if role_events is None:
            block_events_are_role_specific.append(False)
        # Quiet is judged on THIS reservoir's own role volume, not the vial's
        # total event count: a vial whose low pump is blocked while its high
        # pump runs is busy by event count and silent to the low bottle, which
        # is exactly the case worth seeing.
        if value == 0:
            quiet.append(t["vial"])

    drawn_L = drawn_mL / 1000.0
    window_h = _hours(at, anchor)
    if window_h <= 0:
        # An anchor at or after `at` describes an interval that does not exist.
        # The block used to ship drawn_L, estimated_now_L and overdrawn anyway,
        # with rate and projection null and the divergence cross-check -- the
        # one thing that would have caught it -- skipped for the same reason.
        return unavailable(
            "%s's last reading is dated %s, at or after the instant asked about (%s), so "
            "there is no interval to integrate. A rig clock ahead of this server's, or a "
            "future-dated reading, produces this" % (row["id"], anchor, at))
    if level is None:
        level = row["level_L"]
    remaining = level - drawn_L
    prepared = row.get("prepared_L")
    clamped_to_prepared = False
    if prepared is not None and remaining > prepared:
        # Belt and braces against the level reading itself being above the
        # prepared volume: a bottle can never hold more than was put in it.
        remaining = prepared
        clamped_to_prepared = True

    block = {
        "basis": "pump_integrated",
        "source": result["source"],
        # The /dispenses fallback converts the anchor against the summary
        # endpoint's elapsed_h, which on an un-updated rig is the last logged
        # event's controller hour rather than the controller's now. The window
        # therefore opens early by however long the rig has been idle, so the
        # draw is an over-estimate and what is left in the bottle is an
        # under-estimate -- the pessimistic direction, but still not a point
        # measurement, and it must not read as one.
        "drawn_is_upper_bound": bool(result.get("drawn_is_upper_bound")),
        "since": anchor,
        "level_at_anchor_L": level,
        "anchor_source": anchor_source,
        "anchor_event": anchor_event,
        "anchor_note": anchor_note,
        "lines_fed_note": "; ".join(join_notes) if join_notes else None,
        "window_h": round(window_h, 3),
        "drawn_L": round(drawn_L, 4),
        "estimated_now_L": round(max(remaining, 0.0), 4),
        "overdrawn": remaining < 0,
        # Says when estimated_now_L is NOT level_L - drawn_L, so a reader
        # checking that identity is not left to wonder which number moved.
        "clamped_to_prepared": clamped_to_prepared,
        "n_events": n_events,
        "n_events_is_role_specific": not block_events_are_role_specific,
        "n_events_note": (("this rig reported no event count for vial(s) %s, so the total "
                           "is a floor, not a count"
                           % ", ".join(str(v) for v in missing_counts)) if missing_counts
                          else None if not block_events_are_role_specific else
                          "this rig reports only a per-VIAL event count, so this number "
                          "covers both pumps and the low and high bottles of the same "
                          "group report the same figure. It can be large while quiet_vials "
                          "is non-empty; it is not this bottle's dilution count"),
        "lines_counted": [t["line_id"] for t in targets],
        "vials_counted": [t["vial"] for t in targets],
        "quiet_vials": quiet,
        "unpriced_rows": unpriced,
        "unreadable_rows": unreadable,
        "experiment": result.get("experiment"),
    }
    if 0 < window_h < MIN_WINDOW_H:
        block["rate_L_per_h"] = None
        block["projection"] = None
        block["rate_provisional"] = True
        block["rate_provisional_note"] = (
            "only %.2f h since this bottle was read, which is too short to turn %s L into a "
            "rate: one dilution cycle landing just after a reading reads as an enormous "
            "hourly draw. The volume is real; the rate and the forecast are withheld"
            % (window_h, drawn_L))
    elif window_h > 0:
        block["rate_provisional"] = False
        exact_rate = drawn_L / window_h
        block["rate_L_per_h"] = round(exact_rate, 5)
        # Project from the EXACT rate, not the published one. Rounding first
        # and dividing after made the block fail to reproduce its own forecast
        # (a 5 dp quantum is 18% of a 2.5e-5 L/h rate), and any rate below
        # 5e-6 L/h rounded to 0.0, whose falsy check then dropped the forecast
        # for a real measured draw entirely.
        h_left = block["estimated_now_L"] / exact_rate if exact_rate > 0 else None
        # A 0.001 mL dispense -- the smallest the rig can report, since it
        # rounds millilitres to 3 places -- over a 600 h window gives a rate so
        # small that `at + h_left` overflowed datetime and took the whole pump
        # view down for both units. A forecast past a century is not a
        # forecast; the rate is still published.
        if h_left is not None and h_left > 24 * 365 * 100:
            block["projection"] = None
            block["projection_note"] = (
                "the measured draw over this window implies more than a century of media "
                "left, which means the window caught almost no dispensing rather than that "
                "the bottle is inexhaustible. No forecast is offered")
            h_left = None
        block["projection"] = None if h_left is None else {
            "hours_remaining": round(h_left, 1),
            "empty_at": (_parse(at) + timedelta(hours=h_left)).isoformat(),
            "basis": "pump_integrated",
        }
    else:
        block["rate_L_per_h"] = None
        block["projection"] = None

    # How the two halves compare over the SAME interval. This is a model check,
    # not a leak detector: the level-derived side is a prediction here, not a
    # measurement, and only the next reading can settle which was right.
    level_rate = row.get("rate_L_per_h")
    if block.get("rate_provisional"):
        # The window was too short to make a rate out of -- and it is just as
        # too short to allege a leak from. Suppressing only the rate left the
        # same block saying "too short to turn 0.048 L into a rate" AND "a
        # leak, an unlogged loss, a pump calibration that has drifted".
        # Measured on the real geometry: 5 minutes after a level round, one
        # dilution cycle made 4 of 8 bottles allege a leak; at 115 minutes,
        # 7 of 8 -- every one with rate_L_per_h: null.
        block["predicted_drawn_L"] = None
        block["divergence_L"] = None
        block["divergence_comparable"] = False
        block["divergence_skipped_because"] = (
            "only %.2f h since this bottle was read -- too short to turn the draw into a "
            "rate, and too short to compare against one" % block.get("window_h", 0.0))
        return _finish(block, quiet, row)
    if level_rate is None:
        block["predicted_drawn_L"] = None
        block["divergence_L"] = None
        return _finish(block, quiet, row)

    predicted = level_rate * window_h
    block["predicted_drawn_L"] = round(predicted, 4)

    # A prediction bigger than the bottle held is not a prediction, and
    # comparing against it is not a finding. `analyse()` floors its own use of
    # this product at zero (`max(lvl - rate*elapsed, 0)`) and the server
    # already clamps the backward case; the same unclamped product here made
    # every one of the live log's eight active reservoirs report a leak, with
    # a "predicted" draw of up to 11 L from a 1 L bottle, simply because the
    # last reading was 600 h old and the rate was measured over 17 of them.
    # GET /skill tells a client to surface divergence_note whenever it appears,
    # so an unbounded comparison is an instruction to cry wolf on every bottle.
    span_h = row.get("rate_span_h")
    prepared = row.get("prepared_L")

    # Measured against the bottle's CAPACITY, not its current level. Against
    # the level, the guard fired whenever the bottle had less than about five
    # rate-windows of life left -- ordinary end-of-bottle, and the moment an
    # operator most wants a second opinion. A blocked high pump delivering
    # 20 mL where the record implies 139 mL was reported at level 0.140 L and
    # silent at 0.139 L: a 1 mL difference in a level recorded as
    # `approximate` flipped the alarm off. Against capacity, the absurd case
    # (a 600 h extrapolation "predicting" 11 L from a 1 L bottle) still goes,
    # and the real fault stays visible.
    # CLAMPED, not skipped. Skipping gave a hard cliff that ran the wrong way:
    # the longer a bottle went unread, the larger the true discrepancy and the
    # sooner the alarm went quiet -- a blocked line reporting 50 mL was flagged
    # at a 49 h window and silent at 50 h. And moving the denominator from the
    # level to the capacity opened a band where the prediction already exceeds
    # what the bottle held at the anchor while the comparison still ran, so a
    # bottle that simply ran dry was accused of leaking. The prediction can
    # never exceed what was in the bottle to draw; compare against that.
    if level is not None:
        predicted = min(predicted, level)
        block["predicted_drawn_L"] = round(predicted, 4)
    exhausted = False
    stretched = bool(span_h) and window_h > 5 * span_h
    # A rate that was never measured cannot support an allegation of a leak.
    # rate_span_h is set only on the `measured` branch of tools/media.py, so
    # `stretched` was inert for prior_bottle, inferred and upper_bound -- the
    # three bases where the rate is LEAST trustworthy, and where an
    # upper_bound rate makes a spurious note near-certain.
    unmeasured = row.get("rate_basis") != "measured"
    if exhausted or stretched or unmeasured:
        block["divergence_L"] = None
        block["divergence_comparable"] = False
        if unmeasured:
            block["divergence_skipped_because"] = (
                "the level-derived rate is %r, not `measured`, so the comparison would set a "
                "measurement against an estimate. A difference could only mean the estimate "
                "was wrong, which is already known" % row.get("rate_basis"))
        else:
            block["divergence_skipped_because"] = (
                "the level-derived rate (%s, measured over %s h) extrapolated across %.1f h "
                "predicts %.3f L drawn from a bottle that holds %s L -- the model has run "
                "past what the bottle can contain, so the difference measures the "
                "extrapolation, not the plumbing. The pump figure stands on its own; the "
                "comparison does not"
                % (row.get("rate_basis"), span_h, window_h, predicted, prepared))
        return _finish(block, quiet, row)

    block["divergence_comparable"] = True
    block["divergence_L"] = round(drawn_L - predicted, 4)
    if abs(drawn_L - predicted) > max(DIVERGENCE_ABS_L, DIVERGENCE_REL * predicted):
        block["divergence_note"] = (
            "the pumps dispensed %.3f L where the level-derived rate (%s) predicts "
            "%.3f L. Neither is settled until the next level reading; a persistent "
            "gap means a leak, an unlogged loss, a pump calibration that has drifted, "
            "or a line drawing from a bottle nobody recorded"
            % (drawn_L, row.get("rate_basis"), predicted))
    return _finish(block, quiet, row)


def _finish(block: dict, quiet: list, row: dict) -> dict:

    """The tail both divergence branches share."""
    if block.get("unreadable_rows"):
        block["unreadable_note"] = (
            "this rig could not parse some rows of its own pump log (%s) -- a torn line "
            "from an unclean shutdown looks like this. Each one is a dispense that is "
            "missing from the draw below"
            % ", ".join("vial %d: %d row(s)" % (u["vial"], u["rows"])
                        for u in block["unreadable_rows"]))
    if block.get("unpriced_rows"):
        block["unpriced_note"] = (
            "this rig's pump log holds rows whose pump column is neither in1 nor in2 (%s). "
            "They count as events and price as nothing, so the draw may be understated -- "
            "check that vial's pump log for torn lines"
            % ", ".join("vial %d: %d row(s)" % (u["vial"], u["rows"])
                        for u in block["unpriced_rows"]))
    if block.get("drawn_is_upper_bound"):
        block["bound_note"] = (
            "this rig has no /api/v1/consumption, so the window was resolved against its "
            "last logged event rather than its clock. The draw is therefore AT MOST this, "
            "and what is left in the bottle AT LEAST the figure shown. Deploying the "
            "current tools/evolver_api.py there makes it exact")
    if quiet:
        block["quiet_note"] = (
            "vial(s) %s drew no %s media in this window. That is a real measurement, not "
            "missing data -- but a blocked or dead line looks exactly like a quiet one"
            % (", ".join(str(v) for v in quiet), row["role"]))
    return block
