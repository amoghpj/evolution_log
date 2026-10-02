"""Config validation for the server -- a thin re-export of the shared module.

The rules used to live here. They now live in evolver_code/config_validation.py
so that the eVOLVER control loop and this server enforce ONE definition: the
server validates a config it is about to write, custom_script.py validates the
same config before applying it to a running culture, and a disagreement between
those two would mean the server commits a config the rig then rejects -- or,
worse, accepts on different terms.

Nothing else in the server should reach past this module; import
validate_config and ModeNotImplemented from here as before. The public names,
their signatures, and their behaviour are unchanged.

CONFIG_VALIDATOR_PATH overrides which copy of the shared module this server
loads. Defaults to evolver_code/config_validation.py at the root of this
repo -- the one canonical copy, now that server and log share a repo (it used
to be a symlink across two repos, which dangled whenever they were not nested
on the same host). The env var remains for running this server against a
DIFFERENT generation of the validator on purpose (e.g. rolling out config_validation_v2.py to one unit
before the others) -- point this at that file instead, without touching code.

The shared module is loaded by explicit path rather than by adding evolver_code
to sys.path. evolver_code/ is a checkout of controller code, not a package this
server installs, and a bare `import config_validation` would be a name anything
else on the path could shadow.
"""
import importlib.util
import os
from pathlib import Path

_DEFAULT_SHARED = Path(__file__).resolve().parents[2] / "evolver_code" / "config_validation.py"
_SHARED = Path(os.environ.get("CONFIG_VALIDATOR_PATH", _DEFAULT_SHARED)).resolve()

if not _SHARED.is_file():
    raise RuntimeError(
        "shared config validator not found at %s -- set CONFIG_VALIDATOR_PATH to point at one, "
        "or make sure evolver_code/config_validation.py exists at the repo root" % _SHARED
    )

_spec = importlib.util.spec_from_file_location("or05_config_validation", _SHARED)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

ModeNotImplemented = _module.ModeNotImplemented
validate_config = _module.validate_config

# Also shared, for callers that need to describe or apply live-reloadable
# settings (app/config_skill.py explaining which fields take effect without a
# restart, for one).
LIVE_FIELDS = _module.LIVE_FIELDS
LIVE_FIELD_NAMES = _module.LIVE_FIELD_NAMES
extract_live_values = _module.extract_live_values
validate_live_values = _module.validate_live_values

# The modes the shared validator will accept. Re-exported so a caller -- the
# skill text, a test -- can ASK rather than hardcode a list that silently goes
# stale the day a mode is added, which is exactly what happened when
# alternating_selection arrived.
SUPPORTED_MODES = _module._SUPPORTED_MODES

SHARED_MODULE_PATH = _SHARED

__all__ = ["ModeNotImplemented", "validate_config", "LIVE_FIELDS", "LIVE_FIELD_NAMES",
           "extract_live_values", "validate_live_values", "SUPPORTED_MODES",
           "SHARED_MODULE_PATH"]
