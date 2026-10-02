"""Shared validation and live-reload spec for experiment_parameters.yaml.

CANONICAL COPY. This file is imported by two independent consumers and must not
depend on either of them:

  1. evolver_code/custom_script.py, running on the machine beside an eVOLVER.
     It has no access to the log server, so this module must stay stdlib-only.
  2. evolution_log_server/app/config_validator.py, which re-exports it so the
     server rejects a bad config with a 422 before writing it to a unit repo.

Keeping one definition is the point. The server validates what it is about to
write; the rig validates what it is about to run; if those two ever disagree,
the server will happily commit a config the rig then refuses, or worse, accepts
differently. tests/test_config_validation_shared.py asserts the two checked-in
copies are byte-identical.

`from __future__ import annotations` is deliberate: the server runs 3.10, but
the rig-side interpreter beside an eVOLVER may be older, and the annotations
below use 3.10-only syntax that would otherwise be evaluated at import time.

The validation rules originated in app/config_validator.py (2026-09-01) and are
moved here unchanged. Its reasoning, preserved verbatim below, still applies:
this validates the INTENDED rules rather than Settings()'s bug-compatible
behaviour. It covers pumpcontrol_ramp and alternating_selection; every other
mode still raises ModeNotImplemented.
"""
from __future__ import annotations

import math
import os
from typing import Any

CONFIG_FILENAME = "experiment_parameters.yaml"

_SUPPORTED_MODES = ("pumpcontrol_ramp", "alternating_selection")

# Every per-vial field custom_script.py's pumpcontrol_ramp branch actually
# reads (Settings.__init__, "if self.operation_mode == 'pumpcontrol_ramp'").
# Each currently defaults to 0/100/10000 rather than being required for an
# ACTIVE vial -- exactly the gap this validator closes.
_PUMPCONTROL_RAMP_REQUIRED_PER_VIAL = (
    "setpoint", "interval", "input_pump2", "number_consecutive_intervals",
    "initial_concentration", "high_concentration", "low_concentration", "target_ramp",
)

# Same contract for alternating_selection's branch. Deliberately excluded from
# the required list, because each has a defensible default that Settings applies
# explicitly rather than falling into by accident:
#   initial_drug_target          -- defaults to initial_concentration (start the
#                                   experiment challenging at whatever is in the vial)
#   growth_interval_multiplier   -- 3.0, provisional per the protocol notes
#   stress_wait_fraction         -- 0.9
#   fold_dilution                -- 10.0
_ALTERNATING_SELECTION_REQUIRED_PER_VIAL = (
    "setpoint", "input_pump2", "initial_concentration", "high_concentration",
    "low_concentration", "n_tolerant", "n_dilutions", "ramp", "media_wait_time",
)

_REQUIRED_PER_VIAL_BY_MODE = {
    "pumpcontrol_ramp": _PUMPCONTROL_RAMP_REQUIRED_PER_VIAL,
    "alternating_selection": _ALTERNATING_SELECTION_REQUIRED_PER_VIAL,
}

# Read for EVERY vial regardless of operation mode (Settings.__init__'s
# self.volume/self.temperature blocks), not just pumpcontrol_ramp's own list
# above. Confirmed against the real experiment_parameters.yaml this feature
# was designed against: all 16 vial entries carry both, active or not.
_ALWAYS_REQUIRED_PER_VIAL = ("volume", "temperature")

# Accepted by the yaml and read into per-vial state, then never referenced
# anywhere else in custom_script.py -- confirmed by grep, not assumed. Not a
# hard rejection (an inert field harms nothing at runtime) -- surfaced as a
# warning so an LLM/operator setting it doesn't believe it does anything.
_DEAD_PER_VIAL_FIELDS = ("dilution_fraction", "growthdelta")

# YAML has no bareword null literal spelled "None" -- that one is Python's,
# and yaml.safe_load parses an unquoted `None` as the four-character STRING
# "None", not Python's None. The real experiment_parameters.yaml this
# feature was designed against has exactly this mistake
# (experiment_settings.calib_name: None), and custom_script.py's own
# `self.calib_name = config[...].get("calib_name", None)` then reads that
# string right past its own later `is not None` check in growth_curve --
# treating the STRING "None" as a real calibration name. An LLM generating
# this file, trained mostly on Python, is a very plausible source of exactly
# this mistake -- checked wherever a field is allowed to be genuinely absent.
_NULL_LOOKALIKES = {"none", "null", "nil", "nan", "n/a"}


class ModeNotImplemented(Exception):
    """operation.mode is not (yet) validated by this module. Maps to 501."""
    def __init__(self, mode: str | None):
        self.mode = mode
        self.supported = _SUPPORTED_MODES
        super().__init__(
            "operation.mode %r is not yet implemented by this validator -- only %s is "
            "currently supported; other modes will be worked through systematically later"
            % (mode, ", ".join(_SUPPORTED_MODES))
        )


def _is_null_lookalike(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in _NULL_LOOKALIKES


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and value != value  # nan is the only float that != itself


def _is_absent(value: Any) -> bool:
    """True for None, a null-lookalike string, or NaN -- the three ways a
    field can look "supplied" in raw yaml while carrying no real value.
    Never true for 0, 0.0, or False: each is a legitimate value some of
    these fields can genuinely hold (low_concentration: 0.0 is real; a
    target_ramp of 0 on a deliberately non-ramping active vial is real)."""
    return value is None or _is_null_lookalike(value) or _is_nan(value)


def _require_number(problems: list[str], idx: int, vial: Any, field: str, value: Any) -> None:
    if _is_absent(value):
        problems.append(
            "per_vial_settings[%d] (vial %r) has to_run: true but is missing %r -- "
            "custom_script.py would silently default it (to %s) rather than erroring, "
            "producing a live pump config nobody actually chose"
            % (idx, vial, field, _default_note(field))
        )
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(
            "per_vial_settings[%d] (vial %r).%s must be a number (got %r)" % (idx, vial, field, value)
        )


## What Settings.__init__ would silently fall back to, per field, so a "you are
## missing X" message can name the value the operator would actually get.
_SILENT_DEFAULTS = {
    "setpoint": "100", "interval": "10000", "number_consecutive_intervals": "1000",
    "target_ramp": "0", "initial_concentration": "0", "high_concentration": "0",
    "low_concentration": "0", "input_pump2": "vial+32", "volume": "None",
    "temperature": "25", "n_tolerant": "5", "n_dilutions": "6", "ramp": "0.5",
    "media_wait_time": "1.5",
}


def _default_note(field: str) -> str:
    return _SILENT_DEFAULTS.get(field, "a mode-specific default")


## Fields that are NOT required (each has a defensible default) but which
## Settings.__init__ still casts unconditionally -- so a null-lookalike or a
## non-finite value there raises at module import, AFTER this validator passed the
## config clean. Range-checked too, because nothing else checks them anywhere.
##   field, cast, inclusive lo, inclusive hi
## growth_interval_multiplier and stress_wait_fraction are deliberately absent:
## they are LIVE fields, so the live-spec range loop in validate_config already
## covers them and listing them here would report every problem twice.
_ALTERNATING_SELECTION_OPTIONAL_PER_VIAL = (
    ("initial_drug_target", float, 0.0, 1e4),
    ## Strictly above 1: at fold <= 1 the derived purge step count is 0, which
    ## silently skips the entire washout the LOW state exists to perform. The upper
    ## bound is generous but finite -- at V=40 a fold of 1000 is 59 steps and 295 mL.
    ("fold_dilution", float, 1.0 + 1e-9, 1e6),
)


def _check_optional_number(problems, idx, vial, field, value, cast, lo, hi):
    """Type- and range-check a field that is optional but cast unconditionally."""
    if value is None:
        return                                    # genuinely absent: default applies
    if _is_absent(value):
        problems.append(
            "per_vial_settings[%d] (vial %r).%s is %r, which looks like it was meant "
            "to be YAML's null -- omit the key instead. Settings.__init__ casts this "
            "field unconditionally, so %s(%r) raises at startup and the controller "
            "will not boot" % (idx, vial, field, value, cast.__name__, value)
        )
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append("per_vial_settings[%d] (vial %r).%s must be a number (got %r)"
                        % (idx, vial, field, value))
        return
    try:
        finite = math.isfinite(value)
    except (TypeError, OverflowError):
        finite = False
    if not finite:
        problems.append("per_vial_settings[%d] (vial %r).%s is not finite (%r)"
                        % (idx, vial, field, value))
    elif not (lo <= value <= hi):
        problems.append("per_vial_settings[%d] (vial %r).%s = %r is outside the "
                        "allowed range [%s, %s]" % (idx, vial, field, value, lo, hi))


def validate_config(config: dict) -> tuple[list[str], list[str]]:
    """Returns (problems, warnings). Raises ModeNotImplemented if
    operation.mode is not one of _SUPPORTED_MODES -- checked FIRST, before anything
    else, per the "fail fast" instruction: no point validating the rest of a
    config this module can't validate at all. `problems` non-empty means
    this config must be rejected outright; `warnings` never block a write,
    they only say something probably doesn't do what the caller thinks."""
    settings = (config or {}).get("experiment_settings")
    if not isinstance(settings, dict):
        return (["experiment_settings section is missing or not an object"], [])

    mode = (settings.get("operation") or {}).get("mode")
    if mode not in _SUPPORTED_MODES:
        raise ModeNotImplemented(mode)

    problems: list[str] = []
    warnings: list[str] = []

    exp_name = settings.get("exp_name")
    if _is_absent(exp_name) or not str(exp_name).strip():
        problems.append("experiment_settings.exp_name is required and must be a real, non-empty name")

    calib_name = settings.get("calib_name")
    if calib_name is not None and _is_null_lookalike(calib_name):
        problems.append(
            "experiment_settings.calib_name is the string %r, which looks like it was meant to be "
            "YAML's null -- use `calib_name: null` (or omit the key) instead; custom_script.py's own "
            "`is not None` check would treat this string as a real calibration name, not the absence "
            "of one" % calib_name
        )

    per_vial = settings.get("per_vial_settings")
    if not isinstance(per_vial, list) or not per_vial:
        problems.append("experiment_settings.per_vial_settings must be a non-empty list")
        return (problems, warnings)

    seen_vials: dict[int, int] = {}
    for idx, vs in enumerate(per_vial):
        if not isinstance(vs, dict):
            problems.append("per_vial_settings[%d] is not an object" % idx)
            continue
        vial = vs.get("vial")
        if not isinstance(vial, int) or isinstance(vial, bool) or not (0 <= vial <= 15):
            problems.append("per_vial_settings[%d].vial must be an integer 0-15 (got %r)" % (idx, vial))
        else:
            seen_vials[vial] = seen_vials.get(vial, 0) + 1

        to_run = vs.get("to_run")
        if not isinstance(to_run, bool):
            problems.append(
                "per_vial_settings[%d] (vial %r) is missing an explicit boolean to_run -- "
                "custom_script.py only prints a warning and silently excludes the vial from "
                "active_vials when this is absent, which is never what an operator actually wants"
                % (idx, vial)
            )
            to_run = False  # keep checking the rest of this entry on its own terms

        for field in _ALWAYS_REQUIRED_PER_VIAL:
            _require_number(problems, idx, vial, field, vs.get(field))

        if to_run:
            for field in _REQUIRED_PER_VIAL_BY_MODE[mode]:
                _require_number(problems, idx, vial, field, vs.get(field))

            ## Apply the live specs' own ranges AT STARTUP too. They were only
            ## enforced when an operator edited a RUNNING experiment, so a config
            ## committed with n_tolerant: 0 booted silently -- and `counter >= 0` is
            ## vacuously true, so the vial ramped on its first cycle of every HIGH
            ## visit with no growth evidence at all, walking straight to the
            ## reservoir. The unattended restart path was the least-checked one.
            for attr, key, cast, _di, default_active, lo, hi in \
                    _LIVE_FIELDS_BY_MODE.get(mode, ()):
                raw = vs.get(key, default_active)
                if _is_absent(raw) or isinstance(raw, bool) \
                        or not isinstance(raw, (int, float)):
                    if key in _REQUIRED_PER_VIAL_BY_MODE[mode]:
                        continue              # _require_number already reported it
                    ## NOT required, so nothing else reports it -- and
                    ## Settings.__init__ casts it unconditionally, so a bareword
                    ## None, a list, or a bool reaches float()/int() and either
                    ## raises at module import or launders False into 0.0.
                    problems.append(
                        "per_vial_settings[%d] (vial %r).%s must be a number (got "
                        "%r). Settings.__init__ casts this field unconditionally, so "
                        "the controller would fail to start or silently run a value "
                        "nobody chose" % (idx, vial, key, raw))
                    continue
                try:
                    finite = math.isfinite(raw)
                except (TypeError, OverflowError):
                    finite = False
                if not finite:
                    problems.append("per_vial_settings[%d] (vial %r).%s is not finite "
                                    "(%r)" % (idx, vial, key, raw))
                elif not (lo <= raw <= hi):
                    problems.append("per_vial_settings[%d] (vial %r).%s = %r is "
                                    "outside the allowed range [%s, %s]"
                                    % (idx, vial, key, raw, lo, hi))
                elif cast is int and float(raw) != int(raw):
                    problems.append("per_vial_settings[%d] (vial %r).%s = %r must be "
                                    "a whole number" % (idx, vial, key, raw))

            if mode == "alternating_selection":
                for field, cast, lo, hi in _ALTERNATING_SELECTION_OPTIONAL_PER_VIAL:
                    _check_optional_number(problems, idx, vial, field,
                                           vs.get(field), cast, lo, hi)

            if mode == "alternating_selection":
                ## An initial target above the reservoir is unreachable by
                ## construction, so the first HIGH visit spends its whole budget
                ## climbing toward a number the fluidics cannot deliver.
                target = vs.get("initial_drug_target")
                high_c = vs.get("high_concentration")
                if isinstance(target, (int, float)) and not isinstance(target, bool) \
                        and isinstance(high_c, (int, float)) \
                        and not isinstance(high_c, bool) and target > high_c:
                    problems.append(
                        "per_vial_settings[%d] (vial %r): initial_drug_target (%r) "
                        "exceeds high_concentration (%r), so it can never be reached"
                        % (idx, vial, target, high_c))
                ## Surface an unreachable streak at config time rather than after a
                ## week of "budget spent / Current_Drug held" in the logs. A cycle
                ## may run up to growth_interval before it is reclassified overrun,
                ## so n_tolerant cycles fit the budget only if
                ## growth_interval_multiplier <= stress_wait_fraction.
                gim = vs.get("growth_interval_multiplier", 3.0)
                swf = vs.get("stress_wait_fraction", 0.9)
                if isinstance(gim, (int, float)) and not isinstance(gim, bool) \
                        and isinstance(swf, (int, float)) \
                        and not isinstance(swf, bool) and gim > swf:
                    warnings.append(
                        "per_vial_settings[%d] (vial %r): growth_interval_multiplier "
                        "(%r) exceeds stress_wait_fraction (%r), so a tolerance "
                        "streak is only achievable if every cycle averages under "
                        "%.0f%% of growth_interval -- a culture that uses the full "
                        "interval can never complete one"
                        % (idx, vial, gim, swf, 100.0 * swf / gim))

            high, low = vs.get("high_concentration"), vs.get("low_concentration")
            if not _is_absent(high) and not _is_absent(low) and isinstance(high, (int, float)) \
               and isinstance(low, (int, float)) and not (high > low):
                problems.append(
                    "per_vial_settings[%d] (vial %r): high_concentration (%r) must be greater than "
                    "low_concentration (%r) -- find_optimal_pump_volumes's own docstring requires "
                    "ch > cl; this is a documented safety invariant, not a preference"
                    % (idx, vial, high, low)
                )

        for field in _DEAD_PER_VIAL_FIELDS:
            if not _is_absent(vs.get(field)):
                warnings.append(
                    "per_vial_settings[%d] (vial %r) sets %r, which custom_script.py reads into "
                    "Settings but never uses anywhere else in the script -- it has no effect"
                    % (idx, vial, field)
                )

    ## Pump-slot safety. A vial owns three of the 48 slots: influx `vial`, efflux
    ## `vial+16` and drug `input_pump2`. Nothing used to check any of it, and every
    ## failure was silent at the config layer and destructive at the bench: an
    ## out-of-range index raised IndexError out of the control loop and stopped
    ## dosing for the WHOLE rig from that cycle on; a collision with another vial's
    ## slot merged two cultures' lines, or delivered one vial's drug dose as another
    ## vial's efflux; a collision with the vial's own efflux slot erased the dose
    ## after it had already been written to the concentration log.
    claims: dict[int, tuple[Any, str]] = {}
    ## Claim every active vial's OWN influx/efflux slots before any vial can be
    ## skipped below, or a skipped vial's slots stay unclaimed and a second vial can
    ## take one without the collision being reported.
    for vs in per_vial:
        if not isinstance(vs, dict) or not vs.get("to_run"):
            continue
        vial = vs.get("vial")
        if isinstance(vial, bool) or not isinstance(vial, int) \
                or not (0 <= vial <= 15):
            continue
        for slot, role in ((vial, "influx"), (vial + 16, "efflux")):
            claims.setdefault(slot, (vial, role))
    for idx, vs in enumerate(per_vial):
        if not isinstance(vs, dict) or not vs.get("to_run"):
            continue
        vial = vs.get("vial")
        if isinstance(vial, bool) or not isinstance(vial, int) or not (0 <= vial <= 15):
            continue                          # already reported above
        pump2 = vs.get("input_pump2")
        if _is_absent(pump2) or isinstance(pump2, bool) \
                or not isinstance(pump2, (int, float)):
            continue                          # _require_number already reported it
        try:
            finite = math.isfinite(pump2)
        except (TypeError, OverflowError):
            finite = False
        if not finite:
            problems.append("per_vial_settings[%d] (vial %r).input_pump2 is not "
                            "finite (%r)" % (idx, vial, pump2))
            continue
        if float(pump2) != int(pump2):
            problems.append("per_vial_settings[%d] (vial %r).input_pump2 = %r must be "
                            "a whole pump index; int() would silently truncate it to "
                            "%d and address a different pump"
                            % (idx, vial, pump2, int(pump2)))
            continue
        pump2 = int(pump2)
        if not (0 <= pump2 < 48):
            problems.append("per_vial_settings[%d] (vial %r).input_pump2 = %d is not "
                            "a pump slot; the fluid command has 48 slots (0-47)"
                            % (idx, vial, pump2))
            continue
        if pump2 == vial or pump2 == vial + 16:
            problems.append("per_vial_settings[%d] (vial %r).input_pump2 = %d is this "
                            "vial's own %s slot, so its drug leg would overwrite or "
                            "be overwritten by that slot in the same command"
                            % (idx, vial, pump2,
                               "influx" if pump2 == vial else "efflux"))
            continue
        for slot, role in ((vial, "influx"), (vial + 16, "efflux"), (pump2, "drug")):
            owner = claims.get(slot)
            if owner is not None and owner[0] != vial:
                problems.append("pump slot %d is claimed as vial %r's %s AND vial %r's "
                                "%s -- one pump cannot serve two cultures"
                                % (slot, owner[0], owner[1], vial, role))
            else:
                claims[slot] = (vial, role)

    duplicates = sorted(v for v, count in seen_vials.items() if count > 1)
    if duplicates:
        problems.append(
            "per_vial_settings names the same vial more than once: %s -- custom_script.py's "
            "per_vial_dict silently keeps only the LAST entry for a duplicated vial number, "
            "discarding the earlier one with no warning" % duplicates
        )

    return (problems, warnings)


# ─── live-reloadable settings ────────────────────────────────────────────────
# Which fields custom_script.py's pumpcontrol_ramp loop may pick up mid-run,
# without an eVOLVER restart. Everything absent from this tuple needs a restart.
#
# The list is deliberately short and explicit. Excluded on purpose:
#   exp_name, input_pump2, vials_to_run, volume  -- structural. Changing any of
#       them mid-run reinterprets or relocates history that has already been
#       written to the pump and drugconc logs.
#   low_concentration, high_concentration        -- these describe physical
#       bottles. A media change is a real bench event that belongs in
#       evolution_log.json; hot-swapping the number would silently reinterpret
#       every subsequent dose without anything having been poured.
#   initial_concentration                        -- only read when a vial's
#       drugconc log does not yet exist.
#
# Each entry mirrors, exactly, how Settings.__init__ builds that attribute in
# its "if self.operation_mode == 'pumpcontrol_ramp'" branch -- including the
# quirk that the whole-list default and the active-vial default differ for
# number_consecutive_intervals (10000 vs 1000). Extraction here must not
# silently disagree with construction there.
#
#   attr, yaml key, cast, default for an INACTIVE vial, default for an ACTIVE
#   vial whose entry omits the key, and an inclusive sanity range.
_PUMPCONTROL_RAMP_LIVE_FIELDS = (
    ("target_ramp",                  "target_ramp",                  float, 0,     0,     0.0, 2.0),
    ("setpoint",                     "setpoint",                     float, 100,   100,   0.0, 1e7),
    ("interval",                     "interval",                     float, 10000, 10000, 0.0, 1e6),
    ("number_consecutive_intervals", "number_consecutive_intervals", int,   10000, 1000,  0,   1000000),
)

# alternating_selection's live set. The same exclusion reasoning applies as above,
# and two of its own fields are deliberately restart-only:
#   fold_dilution  -- purge geometry. The step count is derived from it, so changing
#       it mid-purge would move the finish line while the ladder is running.
#   initial_drug_target, initial_concentration -- only read when a vial's logs do
#       not yet exist.
# media_wait_time IS live, and growth_interval / Stress_Wait_Time are derived from
# it at the point of use rather than cached, so editing it retimes both at once.
# n_tolerant is live and Stress_Wait_Time scales with it, which is the intended
# coupling: the budget is "N_tolerant cycles at stress_wait_fraction of the wait".
_ALTERNATING_SELECTION_LIVE_FIELDS = (
    ("setpoint",                   "setpoint",                   float, 100, 100, 0.0, 1e7),
    ("n_tolerant",                 "n_tolerant",                 int,   5,   5,   1,   10000),
    ("n_dilutions",                "n_dilutions",                int,   6,   6,   1,   10000),
    ("ramp",                       "ramp",                       float, 0.5, 0.5, 0.0, 2.0),
    ("media_wait_time",            "media_wait_time",            float, 1.5, 1.5, 1e-3, 1e3),
    ## Bounded as corruption guards, not as scientific limits. stress_wait_fraction
    ## may legitimately need to exceed 1 -- a guaranteed-achievable streak requires
    ## growth_interval_multiplier <= stress_wait_fraction, which the 3.0/0.9 defaults
    ## do NOT satisfy -- so (0, 10] with a warning above 1, rather than (0, 1].
    ## 1e4 was no guard at all: with media_wait_time also at 1e4 it permitted a live
    ## growth_interval of 1e8 hours, which removes the overrun path entirely.
    ("growth_interval_multiplier", "growth_interval_multiplier", float, 3.0, 3.0, 0.1, 100.0),
    ("stress_wait_fraction",       "stress_wait_fraction",       float, 0.9, 0.9, 1e-3, 10.0),
)

_LIVE_FIELDS_BY_MODE = {
    "pumpcontrol_ramp": _PUMPCONTROL_RAMP_LIVE_FIELDS,
    "alternating_selection": _ALTERNATING_SELECTION_LIVE_FIELDS,
}

# Kept as a module-level name because it is the documented way to see "what is
# live" (LIVE_CONFIG.md points at it). It is now the union across modes, deduped
# by attribute: `setpoint` appears in both with identical bounds, and
# plan_live_changes skips any attribute the running mode's Settings branch never
# built, so a union is exactly what that loop wants.
def _dedupe_specs(*groups):
    seen, out = {}, []
    for group in groups:
        for spec in group:
            existing = seen.get(spec[0])
            if existing is not None:
                ## The union is only safe while a shared field means the same thing
                ## in both modes. If the bounds ever diverge, validate_raw_live_values
                ## would use the per-mode range while validate_live_values used
                ## whichever mode was declared first -- refusing a value with the
                ## wrong mode's range in the message, every cycle, forever, with no
                ## live field of any kind applied again. Fail at import instead.
                if existing != spec:
                    raise ValueError(
                        "live field %r is declared differently by two modes (%r vs "
                        "%r). A shared live field must have identical cast, defaults "
                        "and bounds in every mode that declares it."
                        % (spec[0], existing, spec))
                continue
            seen[spec[0]] = spec
            out.append(spec)
    return tuple(out)


LIVE_FIELDS = _dedupe_specs(_PUMPCONTROL_RAMP_LIVE_FIELDS,
                            _ALTERNATING_SELECTION_LIVE_FIELDS)

LIVE_FIELD_NAMES = tuple(spec[0] for spec in LIVE_FIELDS)


def _mode_of(config):
    settings = (config or {}).get("experiment_settings")
    if not isinstance(settings, dict):
        return None
    return (settings.get("operation") or {}).get("mode")


def live_fields_for_config(config):
    """The live-field specs for this config's operation mode.

    An unsupported mode yields (), so extraction and range-checking touch nothing
    rather than demanding another mode's fields of it -- validate_config has
    already raised ModeNotImplemented by the time any caller reaches here.
    """
    mode = _mode_of(config)
    if not isinstance(mode, str):
        return ()
    return _LIVE_FIELDS_BY_MODE.get(mode, ())


def read_config(path):
    """Parse a config file. Raises on missing file or malformed yaml.

    yaml is imported here rather than at module scope so that a consumer which
    only wants the pure validation helpers does not need PyYAML installed.
    """
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh)


def config_fingerprint(path):
    """(mtime, size), or None if the file is unreadable.

    A cheap gate so the common case -- an unchanged file, every event cycle --
    costs one stat() rather than a full yaml parse. Deliberately not a content
    hash: this only decides whether to look closer, and a false positive merely
    causes a parse that changes nothing.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def extract_live_values(config):
    """Build {attr: [16 values]} for the live-reloadable fields only.

    Mirrors Settings.__init__'s pumpcontrol_ramp branch. Assumes the config has
    already passed validate_config: this does no error reporting of its own, so
    calling it on an unvalidated config is a bug in the caller.
    """
    settings = (config or {}).get("experiment_settings") or {}
    per_vial = {vs.get("vial"): vs
                for vs in (settings.get("per_vial_settings") or [])
                if isinstance(vs, dict)}
    active = [v for v, vs in per_vial.items()
              if isinstance(v, int) and not isinstance(v, bool) and vs.get("to_run")]

    out = {}
    for attr, key, cast, default_inactive, default_active, _lo, _hi in live_fields_for_config(config):
        values = [default_inactive] * 16
        for vidx in active:
            raw = per_vial[vidx].get(key, default_active)
            if _is_absent(raw):
                raw = default_active
            values[vidx] = cast(raw)
        out[attr] = values
    return out


def _raw_live_value(entry, key, default_active):
    """The value extract_live_values would use for this field, before any cast."""
    raw = entry.get(key, default_active)
    return default_active if _is_absent(raw) else raw


def validate_raw_live_values(config):
    """Range-check live values BEFORE any cast. Returns a list of problems.

    validate_live_values sees the value only AFTER extract_live_values has cast it, which
    is too late for an int-cast field, in two distinct ways found by adversarial testing:

      - int(-0.5) is 0. A value BELOW the floor is laundered into range, and for
        number_consecutive_intervals 0 is the most dangerous value in the domain: the
        ramp predicate's `number_dispensed_so_far >= 0` is vacuously true, so the
        consecutive-interval gate disappears and the vial ramps every cycle. int(1e6+0.9)
        launders a value above the ceiling the same way.
      - int(float('inf')) raises OverflowError before any check can run at all, so an
        inf reaches the caller as a bare exception rather than a named problem.

    Checking the raw yaml value closes both, and also rejects a non-integral float for an
    int field instead of silently truncating it (3.7 -> 3 with the file, the change log
    and the running value all disagreeing).
    """
    settings = (config or {}).get("experiment_settings") or {}
    if not isinstance(settings, dict):
        return []                    # validate_config reports the real problem
    per_vial = [vs for vs in (settings.get("per_vial_settings") or []) if isinstance(vs, dict)]

    problems = []
    for attr, key, cast, _default_inactive, default_active, lo, hi in live_fields_for_config(config):
        for entry in per_vial:
            vial = entry.get("vial")
            if isinstance(vial, bool) or not isinstance(vial, int) or not (0 <= vial <= 15):
                continue             # validate_config already rejects this entry
            if not entry.get("to_run"):
                continue
            raw = _raw_live_value(entry, key, default_active)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                problems.append("%s for vial %d is not a number (%r)" % (key, vial, raw))
                continue
            try:
                finite = math.isfinite(raw)
            except (TypeError, OverflowError):
                finite = False
            if not finite:
                problems.append("%s for vial %d is not finite (%r)" % (key, vial, raw))
            elif not (lo <= raw <= hi):
                problems.append("%s for vial %d = %r is outside the allowed range "
                                "[%s, %s] (checked before any cast)" % (key, vial, raw, lo, hi))
            elif cast is int and float(raw) != int(raw):
                problems.append("%s for vial %d = %r must be a whole number; casting would "
                                "silently truncate it to %d" % (key, vial, raw, int(raw)))
    return problems


def validate_live_values(values):
    """Range-check extracted live values. Returns a list of problems.

    Separate from validate_config because these bounds guard a value that is
    about to be applied to a RUNNING culture, where validate_config's job is to
    guard a file about to be written. A target_ramp of 50 g/L is well-formed
    yaml and a catastrophic dose.
    """
    problems = []
    for attr, _key, _cast, _di, _da, lo, hi in LIVE_FIELDS:
        for vial, value in enumerate(values.get(attr, [])):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
               or not math.isfinite(value):
                problems.append("%s[%d] is not a finite number (%r)" % (attr, vial, value))
            elif not (lo <= value <= hi):
                problems.append("%s[%d] = %r is outside the allowed range [%s, %s]"
                                % (attr, vial, value, lo, hi))
    return problems
