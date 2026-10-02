"""Where each eVOLVER unit's dashboard lives, for the live-pump half of
GET /media.

DELIBERATELY NOT A SECOND COPY OF THE MAPPING
viewer.config.json in the log repo already maps unit -> dashboard URL, and
already carries the discipline that goes with it ("the viewer confirms
identity against /api/v1/health rather than trusting this file"). A second
copy in this server's own environment would be one more thing to keep in
step, and the failure mode of getting it wrong is not an error -- it is one
rig's pump data painted onto the other rig's bottles. So this reads that
file, out of the LOG_REPO_PATH this server already points at.

EVOLVER_DASHBOARD_URLS overrides it per deployment, as a JSON object
{"patrick": "http://host:8050"}, for a host that reaches the rigs at
addresses the viewer's own machine does not. When it is set it REPLACES the
file's units rather than merging with them, so a deployment override is
always the whole story and never half of it.

Distinct from EVOLVER_UNIT_PATHS (app/evolver_config.py): that is a
filesystem path per unit, for reading and committing experiment_parameters
.yaml on a host that has those checkouts. The dashboards are reached over
the network instead -- the rigs are separate machines, and this server has
no filesystem access to their experiment directories.

NOTHING HERE RAISES ON A MISSING OR BROKEN CONFIG
An absent viewer.config.json is not a misconfiguration of this server; it
means nobody has told it where the rigs are. GET /media must still answer,
with the bottle-level numbers it has always returned and an explicit note
that no unit could be consulted. Only the ERRORS are surfaced, never a
silent empty mapping that reads like "no rigs exist".
"""
import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULT_TIMEOUT_S = 3.0
DEFAULT_WINDOW_H = 6.0


class DashboardSettings:
    def __init__(self, log_repo_path: Path, urls_json: str | None = None,
                 timeout_s: float | None = None):
        self.source = None
        self.error = None
        self.units: dict[str, str] = {}
        self.disabled: list[str] = []
        self.timeout_s = timeout_s if timeout_s is not None else _env_timeout()

        raw = urls_json if urls_json is not None else os.environ.get("EVOLVER_DASHBOARD_URLS")
        if raw:
            self.source = "EVOLVER_DASHBOARD_URLS"
            try:
                mapping = json.loads(raw)
            except json.JSONDecodeError as exc:
                self.error = "EVOLVER_DASHBOARD_URLS is not valid JSON: %s" % exc
                return
            if not isinstance(mapping, dict):
                self.error = ("EVOLVER_DASHBOARD_URLS must be a JSON object mapping unit "
                              "name to dashboard URL, e.g. {\"patrick\": \"http://10.0.0.5:8050\"}")
                return
            bad = {k: v for k, v in mapping.items()
                   if not isinstance(v, str) or not v.strip()}
            if bad:
                # str(v) accepted anything: the viewer.config.json shape
                # ({"patrick": {"url": ...}}) -- the single likeliest mistake,
                # since that file is the other source -- became the literal URL
                # "{'url': '...'}", and the operator was then told their rig was
                # down, on another network, or bound to 127.0.0.1. None of the
                # three was true.
                self.error = (
                    "EVOLVER_DASHBOARD_URLS must map each unit to a URL STRING; %s "
                    "%s not. Write {\"patrick\": \"http://host:8050\"}, not the "
                    "viewer.config.json shape {\"patrick\": {\"url\": ...}}"
                    # The unit name only, and the TYPE of what it maps to --
                    # never the value. It is a URL, and a URL can carry
                    # user:password; this message reaches GET /media and
                    # GET /pump_events, which need no token.
                    % (", ".join("%r -> a %s" % (k, type(v).__name__) for k, v in sorted(bad.items())),
                       "is" if len(bad) == 1 else "are"))
                return
            self.units = {str(k): v.strip().rstrip("/") for k, v in mapping.items()}
            return

        path = Path(log_repo_path) / "viewer.config.json"
        self.source = str(path)
        if not path.exists():
            self.error = ("no viewer.config.json at %s and EVOLVER_DASHBOARD_URLS is not "
                          "set, so no eVOLVER dashboard could be consulted" % path)
            return
        try:
            cfg = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            self.error = "%s is not valid JSON: %s" % (path, exc)
            return

        units = cfg.get("units")
        if not isinstance(units, dict):
            self.error = "%s has no `units` object" % path
            return
        for name, spec in units.items():
            if not isinstance(spec, dict) or not isinstance(spec.get("url"), str) \
                    or not spec["url"].strip():
                continue
            # `is False` treated enabled: 0 / "false" / null as ENABLED -- all
            # plausible hand-edits to a JSON config, and each one silently
            # meaning the opposite of what it was written to mean.
            enabled = spec.get("enabled", True)
            if enabled is False or enabled in (0, "", "false", "False", "no", None):
                self.disabled.append(name)
                continue
            self.units[name] = spec["url"].strip().rstrip("/")
        if not self.units and not self.disabled:
            self.error = "%s lists no usable units" % path

    def url_for(self, unit: str) -> str | None:
        return self.units.get(unit)

    def why_not(self, unit: str) -> str:
        """The reason this unit has no URL, in the caller's own terms. Never
        'unknown' -- a media consumer that cannot see a rig is owed which of
        the several different reasons applies."""
        if self.error:
            return self.error
        if unit in self.disabled:
            return "unit %r is present in %s but disabled there" % (unit, self.source)
        return ("unit %r has no dashboard URL in %s (known: %s)"
                % (unit, self.source, ", ".join(sorted(self.units)) or "none"))


def _env_timeout() -> float:
    raw = os.environ.get("EVOLVER_DASHBOARD_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    # A zero or negative timeout would mean "never give up", which on a read
    # route reachable by an LLM is a hung request rather than a fast answer.
    return val if val > 0 else DEFAULT_TIMEOUT_S


@lru_cache
def _cached(log_repo_path: str, urls_json: str | None, timeout_env: str | None,
            stamp: float) -> DashboardSettings:
    # timeout_env is in the key for invalidation; DashboardSettings reads the
    # variable itself, so it is not passed through as a value.
    return DashboardSettings(Path(log_repo_path), urls_json, None)


def get_dashboard_settings(settings) -> DashboardSettings:
    """Built from app.config.Settings, cached so a per-request construction
    never re-reads viewer.config.json.

    `stamp` is that file's mtime, and it is in the cache key deliberately.
    With only the path in the key there was exactly one entry for the process's
    whole life: an operator correcting a wrong URL -- the documented fix for
    the failure this module exists to prevent, one rig's data on the other's
    bottles -- changed nothing until the server was restarted. The roster lives
    in a git repo the operator pulls, so changing underneath a running process
    is its normal case, not an edge one."""
    path = Path(settings.log_repo_path) / "viewer.config.json"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    # The timeout env var is in the key too. It was read inside
    # DashboardSettings but absent from the key, so changing it did nothing
    # until viewer.config.json happened to be touched -- while this module's
    # own docstring argues at length for why the URL must be re-readable under
    # a running process. Its sibling knob was silently not.
    return _cached(str(settings.log_repo_path),
                   os.environ.get("EVOLVER_DASHBOARD_URLS"),
                   os.environ.get("EVOLVER_DASHBOARD_TIMEOUT_S"), stamp)
