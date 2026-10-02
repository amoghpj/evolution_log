"""Request models for POST /lines -- creating a new line, the operation
POST /events cannot do (it only appends to an already-existing line or to
experiment_events). Covers all four ways a line can begin (LOG_PROTOCOL.md
§5): branch, split, merge, restart.

Same divergence from SERVER_DESIGN.md decision #1 as models.py: these
describe only the request shape. app/lines_writer.py validates the
resulting log against the real schema and tools/lineage.py, and additionally
enforces the begin-mode-specific rules from LOG_PROTOCOL.md §5 that neither
of those can express (what happened to the parent, whether the destination
was empty) -- those rules live here and in lines_writer.py, grounded in the
real merges and restarts already in the log, not invented from the prose
spec alone. See that module's docstring and this repo's git history for what
was checked against real precedent before these shapes were fixed, including
the specific points confirmed with the operator: split events are one event
per line (not one shared event_id across parent and children, unlike a
merge's child-only event), merge line_ids are supplied by the caller and
only validated (not derived -- the one real example follows no rule this
code can safely reproduce), and a restart may carry an embedded predecessor
termination in the same call.
"""
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .models import StrictModel


class Concentration(StrictModel):
    value_g_per_L: float
    value_mM: float
    unit_primary: Literal["g/L"] = "g/L"


class PgRegimeInit(StrictModel):
    low: Concentration
    high: Concentration
    effective_from: str

    @model_validator(mode="after")
    def _low_not_above_high(self):
        # Found by simulating a confused-LLM/malformed-input operator: an
        # inverted regime (low > high -- dosing "up" toward a floor above
        # its own ceiling) was accepted with no complaint and permanently
        # recorded, exactly the "confident wrong number" failure mode
        # CLAUDE.md/LOG_PROTOCOL.md warn against.
        if self.low.value_g_per_L > self.high.value_g_per_L:
            raise ValueError(
                "pg_regime.low (%s g/L) is greater than pg_regime.high (%s g/L) -- low must "
                "never exceed high" % (self.low.value_g_per_L, self.high.value_g_per_L)
            )
        return self


class LineReservoirs(StrictModel):
    low: str
    high: str


class FoundingEvent(StrictModel):
    """The one event that goes into a new line's events[] at creation, or a
    termination event for a line this operation ends. Same field set as
    NewEventRequest minus target (implied) and minus supersedes (a founding
    or termination event doesn't correct a prior one; if it needs to, that's
    a separate, later POST /events call)."""
    timestamp: str
    event_type: str
    provenance: str
    params: dict[str, Any] = Field(default_factory=dict)
    notes: str
    missing_fields: list[str] = Field(default_factory=list)
    timestamp_precision: Literal["minute", "hour", "day"] | None = None
    elapsed_h: float | None = None
    caused_by_event: str | None = None
    source_document: str | None = None


class TerminationEvent(FoundingEvent):
    """A FoundingEvent used to end an existing line. event_type is fixed --
    LOG_PROTOCOL.md's examples all use "termination" for this; a caller
    supplying something else is almost certainly a mistake, not a legitimate
    new convention (unlike event_type on a founding event, which is
    deliberately open)."""
    event_type: Literal["termination"] = "termination"


class NewLineSpec(StrictModel):
    """Everything about a new line except line_id (server-derived, except
    for merge) and lineage (server-computed from begin_mode)."""
    line_id: str | None = Field(
        None, description="Only used (and required) for begin_mode=merge; "
                           "ignored and server-derived for the other three modes."
    )
    unit: str
    vial: int
    strain: str
    initial_media: str
    current_media: str
    mode: Literal["constant", "switch"]
    t0: str
    pg_regime: PgRegimeInit
    reservoirs: LineReservoirs
    replicate: int | None = None
    group: str | None = None
    founding_event: FoundingEvent


class BranchRequest(StrictModel):
    begin_mode: Literal["branch"] = "branch"
    parent_line_id: str
    new_line: NewLineSpec


class SplitRequest(StrictModel):
    begin_mode: Literal["split"] = "split"
    parent_line_id: str
    parent_termination: TerminationEvent
    new_lines: list[NewLineSpec]

    @model_validator(mode="after")
    def _at_least_two_children(self):
        if len(self.new_lines) < 2:
            raise ValueError("a split produces at least 2 children (LOG_PROTOCOL.md §5); "
                              "use begin_mode=branch for exactly one")
        return self


class MergeRequest(StrictModel):
    begin_mode: Literal["merge"] = "merge"
    parent_line_ids: list[str]
    parent_terminations: dict[str, TerminationEvent] = Field(
        default_factory=dict,
        description="Keyed by parent_line_id, only for parents that END as part of this "
                    "merge. At least one is required -- the vial the child physically "
                    "occupies must belong to an ending parent (its standing population is "
                    "what's being overwritten). A parent absent here is asserted to "
                    "CONTINUE running independently, per LOG_PROTOCOL.md §5's 'either'.",
    )
    new_line: NewLineSpec

    @model_validator(mode="after")
    def _shape(self):
        if len(self.parent_line_ids) < 2:
            raise ValueError("a merge has at least 2 parents (LOG_PROTOCOL.md §5)")
        if len(set(self.parent_line_ids)) != len(self.parent_line_ids):
            # Found by simulating an "extreme merge edge cases" operator:
            # naming the same line_id twice used to be accepted outright,
            # producing a merge child whose lineage.parents held the SAME
            # ancestor twice -- permanently misrepresenting a single
            # ancestor as two independent contributing cultures, with no
            # error and no crash. A merge's whole premise is >=2 DISTINCT
            # standing populations combining; a line cannot merge with
            # itself.
            raise ValueError("parent_line_ids has a repeated entry -- a merge needs >= 2 "
                              "DISTINCT parents, a line cannot merge with itself: %r"
                              % self.parent_line_ids)
        if not self.new_line.line_id:
            raise ValueError("begin_mode=merge requires new_line.line_id -- the server "
                              "validates it against parent_line_ids but does not invent one")
        unknown = set(self.parent_terminations) - set(self.parent_line_ids)
        if unknown:
            raise ValueError("parent_terminations names %s, not in parent_line_ids" % sorted(unknown))
        return self


class RestartRequest(StrictModel):
    begin_mode: Literal["restart"] = "restart"
    predecessor_line_id: str | None = Field(
        None, description="Omit for a true day-one founder -- a brand new vial with no "
                           "predecessor at all, not covered by any of the other three modes "
                           "(they all require an existing parent). When given, must name an "
                           "existing line at the destination vial. Records HARDWARE CONTINUITY "
                           "ONLY (lineage.occupies_vial_of, params.predecessor_in_vial) -- a "
                           "restart is never descent (LOG_PROTOCOL.md): the new line is still a "
                           "founder with zero lineage.parents, whatever predecessor_line_id "
                           "names. To narrate that this culture's MATERIAL actually came from a "
                           "specific prior lineage -- including an already-ENDED one, which "
                           "branch/split/merge can never express, since all three require an "
                           "ACTIVE parent -- use founding_event.params.source_culture (free "
                           "text, e.g. '1 mL of patrick-v05#2', already used this way ~20 times "
                           "in the real log), not predecessor_line_id; the two are independent."
    )
    predecessor_termination: TerminationEvent | None = Field(
        None, description="Required if predecessor_line_id is given and still active when "
                           "this arrives; omitted if it was already terminated by an earlier "
                           "event, or if predecessor_line_id itself is omitted."
    )
    new_line: NewLineSpec

    @model_validator(mode="after")
    def _termination_needs_predecessor(self):
        if self.predecessor_termination is not None and self.predecessor_line_id is None:
            raise ValueError("predecessor_termination was given without predecessor_line_id")
        return self


NewLineRequest = Annotated[
    BranchRequest | SplitRequest | MergeRequest | RestartRequest,
    Field(discriminator="begin_mode"),
]
