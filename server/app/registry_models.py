"""Request model for POST /parameter_registry and its /candidate dry-run.

See models.py's StrictModel docstring for why extra="forbid" -- the same
reasoning applies here: a typo'd top-level key should be a 422, not
silently dropped.

`entry` is intentionally a plain dict, not a modeled object -- the same
divergence models.py already documents for `params` and config_models.py
for `config`: schema/evolution_log.schema.json's own `registryEntry` $def
is the one definition of what a registry entry looks like (required
description/status/type, the closed set of value-type names, optional
unit/enum/items/applies_to_event_types/note); re-deriving that a second
time as a hand-authored Pydantic model would drift from it exactly the way
this whole project has repeatedly avoided elsewhere."""
from typing import Any

from pydantic import Field

from .models import StrictModel


class RegistryEntryRequest(StrictModel):
    key: str
    entry: dict[str, Any] = Field(default_factory=dict)
