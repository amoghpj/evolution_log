"""Request model for the /config routes. See models.py's StrictModel
docstring for why extra="forbid" -- the same reasoning applies here: a
typo'd top-level key should be a 422, not silently dropped.

`config` is intentionally a plain dict, not a modeled object -- the whole
`experiment_settings` document, in the same shape GET /config returns it,
validated by app/config_validator.py against the real structure
experiment_parameters.yaml has, not a second hand-authored Pydantic copy of
it (the same divergence models.py already documents for `params`)."""
from typing import Any

from pydantic import Field

from .models import StrictModel


class ConfigRequest(StrictModel):
    unit: str
    config: dict[str, Any] = Field(default_factory=dict)
    # False by default: app/config_writer.py's check_no_silent_removal
    # rejects a write that would drop a vial or a top-level field the
    # CURRENT config has, unless this is explicitly set. Found necessary by
    # simulating a "PATCH-believing" LLM client -- see that function's
    # docstring for the real incident (15 of 16 vials silently destroyed)
    # this closes.
    confirm_removed_fields: bool = False
