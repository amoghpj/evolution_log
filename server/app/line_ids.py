"""Line-id derivation and validation (LOG_PROTOCOL.md §4, SERVER_DESIGN.md
Phase 2 #16's composability grammar), grounded in real precedent rather than
the prose spec alone -- see git history for what was checked against the two
real merges and three real restarts before this was written.

Three of the four begin_modes have a fully mechanical id: branch and restart
share one rule (unit-vial + next occupancy count); split appends a letter to
the PARENT's own id, unrelated to the child's physical vial, because line
identity follows the culture, not the hardware (LOG_PROTOCOL.md §4) -- two
split children can land in two different physical vials and still be named
patrick-v04.a / patrick-v04.b. Merge is NOT mechanical: the one real example
(patrick-v09+v10) names the parent that keeps running independently as the
base, not the vial the child physically occupies, which reads as operator
judgment rather than a rule -- so merge ids are supplied by the caller and
only validated here, never generated.
"""
import re

BASE_RE = re.compile(r"^([a-z]+)-v(\d{2})$")
OCCUPANCY_RE = re.compile(r"^([a-z]+)-v(\d{2})(?:#(\d+))?$")
SPLIT_CHILD_RE = re.compile(r"^(.+)\.([a-z])$")
# The generation group must NOT capture the leading '#' -- OCCUPANCY_RE's
# equivalent group doesn't either. These two used to disagree (this one kept
# the '#', OCCUPANCY_RE didn't), so the SAME parent's generation compared
# unequal to itself depending on which regex parsed it -- found by
# simulating a real split-then-merge operator composing an id from a
# #generation parent.
MERGE_ADDEND_RE = re.compile(r"^(?:([a-z]+)-)?v(\d{2})(?:#(\d+))?$")


def _base_id(unit: str, vial: int) -> str:
    return "%s-v%02d" % (unit, vial)


def last_real_position(line: dict) -> tuple[str, int] | None:
    """This line's most recent REAL (non-null) (unit, vial) -- its current
    position if it's currently on the evolver, else reconstructed from its
    own hardware_swap history: a vacate nulls unit/vial, but the real
    position it vacated FROM (or was ever relocated to) is still genuine
    hardware history, and _find_prior_occupant (lines_writer.py) needs it
    for hardware-continuity tracking on a vial a vacated line once held,
    not the null it currently reads.

    Walks this line's OWN events[] only, chronologically, tracking the
    LAST REAL position ever established -- never the current (possibly
    null) one -- from two sources, either of which can appear on any
    hardware_swap (relocate or vacate alike): `new_unit`+`new_vial` (the
    position it moved TO) and optional `previous_unit`/`previous_vial`
    (the position it held right BEFORE that event, supplied by the caller
    and cross-checked against the line's real position at write time when
    present -- app/writer.py). A vacate's own `vacate: true` deliberately
    does nothing here by itself -- it carries no position information of
    its own, and must never erase a real position this same event's own
    `previous_unit`/`previous_vial` (or an earlier event's `new_unit`/
    `new_vial`) just established. Found necessary by testing this exact
    case: a vacate WITH previous_unit/previous_vial supplied used to have
    its own seeded position immediately nulled back out again by the very
    same event's vacate flag, in the same loop iteration.

    There is a real limit here, by construction, not oversight: this
    line's FOUNDING unit/vial (set once, at creation, never itself stored
    as a separate historical field) is only recoverable through this walk
    if some hardware_swap along the way happened to carry previous_unit/
    previous_vial -- if the very first hardware_swap this line ever
    receives is a vacate with neither supplied, the founding position is
    genuinely gone, nowhere left to read it from. Returns None rather than
    guessing in that case (CLAUDE.md: "never invent a value") -- a known,
    documented gap (README.md), not a bug to paper over with a fabricated
    answer.

    Sorted by event_id, NEVER by timestamp -- found necessary by round-2
    adversarial testing after getting this wrong on the first attempt.
    CLAUDE.md says it plainly for the log as a whole: "event_id order is
    not chronological... because corrections are appended later carrying
    earlier timestamps," and sorting BY timestamp here did exactly what
    that warning predicts -- a correction (`supersedes: EVT-X`) legitimately
    carrying an earlier timestamp than the mistake it corrects sorted
    BEFORE EVT-X, so EVT-X's superseded, wrong value got walked LAST and
    silently overwrote the correction. event_id is a monotonically
    increasing append-order counter (tools/lineage.py's log_meta.
    event_counter), so it always reflects true write/precedence order --
    exactly what deciding "which of these two conflicting values wins"
    needs, which chronological order does not."""
    if line.get("unit") is not None and line.get("vial") is not None:
        return (line["unit"], line["vial"])
    position = None
    for event in sorted(line.get("events", []), key=lambda e: e.get("event_id") or ""):
        if event.get("event_type") != "hardware_swap":
            continue
        params = event.get("params") or {}
        pu, pv = params.get("previous_unit"), params.get("previous_vial")
        if isinstance(pu, str) and isinstance(pv, int) and not isinstance(pv, bool):
            position = (pu, pv)
        new_unit, new_vial = params.get("new_unit"), params.get("new_vial")
        if isinstance(new_unit, str) and isinstance(new_vial, int) and not isinstance(new_vial, bool):
            position = (new_unit, new_vial)
    return position


def next_occupancy_id(log: dict, unit: str, vial: int) -> str:
    """The next id for a fresh (branch or restart) line at (unit, vial):
    bare if this position has never been used, else the next #N -- computed
    from every line that has EVER used this exact (unit, vial), active or
    ended, not just the currently active one.

    Matched by LINE_ID SHAPE (OCCUPANCY_RE against the id itself), never by
    a line's own CURRENT unit/vial fields -- found necessary by simulating
    an adversarial operator combined with ISSUE_002's relocation feature
    (and its own follow-up, hardware_swap's vacate): a line's current
    position can now change after creation (relocate to a real position,
    or vacate to null), but its id is permanent (LOG_PROTOCOL.md §4 --
    "line identity follows the culture, not the hardware"). Filtering by
    CURRENT position used to mean a relocated-or-vacated line's occupancy
    of its ORIGINAL (unit, vial) became invisible to this function the
    moment it moved -- so a later restart/branch back into that same,
    now-empty vial minted the SAME bare id the moved line still holds,
    which _insert_new_line then correctly refuses (409 "already exists"),
    with a message that gives no hint the real cause was an id collision,
    not real occupancy. Matching on id shape instead is exactly what this
    docstring already claimed to do ("every line that has EVER used this
    exact (unit, vial)") -- id shape survives relocation/vacate by
    construction, so this now actually delivers on that claim rather than
    silently degrading to "every line CURRENTLY at this position." A split
    child's id (".letter" suffix) or a merge child's id ("+"-joined) never
    matches OCCUPANCY_RE at all, so those still correctly play no part in
    this count, exactly as before."""
    base = _base_id(unit, vial)
    highest = None  # None means "bare id not seen"; 1 means "bare id seen, no #N yet"
    for lid in log.get("lines", {}):
        m = OCCUPANCY_RE.match(lid)
        if not m or m.group(1) != unit or int(m.group(2)) != vial:
            continue  # a split/merge id derived from this vial's lineage, not an occupant of it
        n = int(m.group(3)) if m.group(3) else 1
        highest = n if highest is None else max(highest, n)
    if highest is None:
        return base
    return "%s#%d" % (base, highest + 1)


def next_split_letter(log: dict, parent_line_id: str) -> str:
    """The next unused split-child letter for a given parent -- 'a' if this
    parent has no split children yet, else the next letter after the
    highest one already used. Does not look at the child's own vial: split
    identity follows the parent's culture, not where the child physically
    lands (LOG_PROTOCOL.md §4)."""
    highest = None
    prefix = parent_line_id + "."
    for lid in log.get("lines", {}):
        if lid.startswith(prefix) and len(lid) == len(prefix) + 1 and lid[-1].isalpha():
            letter = lid[-1]
            highest = letter if highest is None or letter > highest else highest
    if highest is None:
        return "a"
    if highest == "z":
        raise ValueError("%s already has 26 split children (a-z exhausted)" % parent_line_id)
    return chr(ord(highest) + 1)


def validate_merge_id(candidate_id: str, parent_line_ids: list[str]) -> None:
    """Raise ValueError unless candidate_id is a well-formed merge id whose
    addends reference exactly the stated parents, as a set (order doesn't
    matter -- the real example names the continuing parent first, which
    isn't a rule this can reproduce, so it isn't required here either).

    Two kinds of parent, matched two different ways. Found necessary by
    simulating a real split-then-merge operator: LOG_PROTOCOL.md §5's table
    has no exception for merge parents ("parent: either ... >= 2") -- any
    line currently holding a standing population is a valid merge parent,
    whatever shape its own id happens to have, but the original version of
    this function rejected every parent whose OWN id wasn't the one real
    precedent's simple occupancy shape ("patrick-v09+v10"), regardless of
    what candidate_id was -- a split child's own id ("patrick-v10#4.c")
    could never be a merge parent at all.

    - A parent whose own id IS occupancy-shaped (unit-vNN, optionally
      #generation) is matched via the compact fragment grammar: the
      corresponding candidate part may omit the unit prefix if every
      fragment-shaped parent shares one unit (ambiguous otherwise --
      spelled out, not guessed).
    - Any other parent (a split child's ".letter" id, a prior merge's
      "+"-joined id, or any future shape) has no shorter canonical form to
      derive -- genuinely no real precedent exists for merging one of
      these, so none is guessed at here. The corresponding candidate part
      must equal that parent's id VERBATIM instead.
    """
    parts = candidate_id.split("+")
    if len(parts) != len(parent_line_ids):
        raise ValueError(
            "%r has %d part(s) joined by '+' but %d parent_line_ids were given"
            % (candidate_id, len(parts), len(parent_line_ids))
        )

    def parent_fragment(pid: str):
        m = OCCUPANCY_RE.match(pid)
        return (m.group(1), m.group(2), m.group(3)) if m else None

    fragment_pids = [pid for pid in parent_line_ids if parent_fragment(pid) is not None]
    literal_pids = [pid for pid in parent_line_ids if parent_fragment(pid) is None]

    remaining_parts = list(parts)
    for pid in literal_pids:
        if pid not in remaining_parts:
            raise ValueError(
                "%r must include %r verbatim as one of its '+'-joined parts -- its own id isn't "
                "the simple unit-vial shape a shorter form could be derived from"
                % (candidate_id, pid)
            )
        remaining_parts.remove(pid)  # only one occurrence removed even if it appears twice

    if not fragment_pids:
        if remaining_parts:
            raise ValueError("%r has extra part(s) %s not matching any parent" % (candidate_id, remaining_parts))
        return

    parent_fragments = set(parent_fragment(pid) for pid in fragment_pids)
    units = {frag[0] for frag in parent_fragments}
    base_unit = next(iter(units)) if len(units) == 1 else None  # only omittable if unambiguous

    candidate_fragments = set()
    for part in remaining_parts:
        m = MERGE_ADDEND_RE.match(part)
        if not m:
            raise ValueError("%r: %r is not a valid merge addend" % (candidate_id, part))
        unit = m.group(1) or base_unit
        if unit is None:
            raise ValueError(
                "%r: %r omits its unit, but the parents span more than one unit -- spell it out"
                % (candidate_id, part)
            )
        candidate_fragments.add((unit, m.group(2), m.group(3)))

    if candidate_fragments != parent_fragments:
        raise ValueError(
            "%r decomposes to %s, which does not match parent_line_ids' fragments %s"
            % (candidate_id, sorted(candidate_fragments), sorted(parent_fragments))
        )
