"""Request models for POST /events.

Deliberately NOT a reimplementation of schema/evolution_log.schema.json as
Pydantic -- see README.md, "a deliberate divergence from decision #1". That
JSON schema plus parameter_registry are already the one definition
(hardened over two whole phases of work in the log repo); re-deriving it a
second time by hand here would be exactly the kind of duplication this
project has spent two phases eliminating. These models describe only the
REQUEST shape -- what a caller may supply. The JSON schema and
tools/lineage.py's cross-field checks are what actually validate the
resulting event and log, run in writer.py against the real thing, not a
Pydantic re-encoding of it.

event_id and operator are absent on purpose: the server assigns event_id
(monotonic -- SERVER_DESIGN.md Phase 2 #15) and operator comes from the
bearer token (decision #3), never from the request body.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base for every request model in this server: an unrecognized field is
    REJECTED (422), never silently dropped. Found necessary by simulating
    real operator use: pydantic's own default (extra="ignore") let a typo'd
    or made-up top-level field -- an old-idiom corrected_from/corrected_at
    guessed to live at the top level by analogy with supersedes, or
    LOG_PROTOCOL.md's own replicate_independence/divergence_time, neither of
    which this registry has a slot for -- vanish with a plain 201 and no
    warning, exactly the "confident number, not a crash" failure mode this
    project's docs warn against elsewhere. `params` is unaffected by this --
    it's a plain dict, checked separately against parameter_registry, not a
    modeled object with named fields."""
    model_config = ConfigDict(extra="forbid")


class EventTarget(StrictModel):
    """Exactly one of these: a line-scoped event, or a facility-scope one."""
    line_id: str | None = None
    scope: Literal["facility"] | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.line_id is None) == (self.scope is None):
            raise ValueError("target must set exactly one of line_id or scope")
        return self


class NewEventRequest(StrictModel):
    target: EventTarget
    timestamp: str
    event_type: str
    provenance: str
    params: dict[str, Any] = Field(default_factory=dict)
    notes: str
    missing_fields: list[str] = Field(default_factory=list)
    timestamp_precision: Literal["minute", "hour", "day"] | None = None
    elapsed_h: float | None = None
    caused_by_event: str | None = None
    supersedes: str | None = None
    source_document: str | None = None
