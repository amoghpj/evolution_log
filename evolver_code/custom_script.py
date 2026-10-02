#!/usr/bin/env python3
import math
import numpy as np
import logging
import os.path
import pandas as pd
import yaml
import time
import sys
from scipy.optimize import minimize_scalar, minimize

# Concentration tolerances, g/L. HOLD_TOL is the slack allowed when deciding whether a
# candidate dispense counts as "not an increase". It must sit above L-BFGS-B convergence
# noise (measured up to 2.2e-6) and far below anything physically meaningful (the ramp step
# is 0.1): at 1e-9 the filter rejected genuinely exact holds as marginally positive and
# substituted a worse undershoot, breaking 36 of 608 exact holds by as much as 0.08 g/L.
# RAMP_TOL is how far the achievable step may sit from the requested one before the
# controller says so out loud.
HOLD_TOL = 1e-4
RAMP_TOL = 1e-3


def stepsize(c1, cl, ch, vl1, vl2, maxdispense, vialvolume=22):
    """
    Compute the net concentration change in the vial over one two-step dispense cycle.

    Args:
        c1:          Starting vial concentration (same units as cl, ch).
        cl:          Concentration of the low (media) pump fluid.
        ch:          Concentration of the high (drug) pump fluid.
        vl1:         Volume dispensed by the low pump in step 1 (mL).
        vl2:         Volume dispensed by the low pump in step 2 (mL).
        maxdispense: Total volume dispensed per step; vl + vh = maxdispense (mL).
        vialvolume:  Vial volume before each dispense step (mL); resets between steps.

    Returns:
        delta: c2 - c1, the net concentration change over the cycle.
    """
    c1p = (c1 * vialvolume + (maxdispense - vl1) * ch + vl1 * cl) / (vialvolume + maxdispense)
    c2  = (c1p * vialvolume + (maxdispense - vl2) * ch + vl2 * cl) / (vialvolume + maxdispense)
    return c2 - c1


def append_drug_concentration(path, elapsed_time, concentration):
    """Append one row to a vial's drugconc log, refusing a non-finite concentration.

    round(nan, 2) is nan, which serialises as the string 'nan' and parses straight back as
    NaN on the next cycle. One bad value therefore poisons every later cycle, and because
    all NaN comparisons are False it used to drive the solver into a maximum-drug dispense.
    A gap in the log is recoverable; a NaN in it is not.

    Returns True if the row was written.
    """
    try:
        finite = math.isfinite(concentration)
    except TypeError:
        finite = False
    if not finite:
        print("REFUSING to write non-finite concentration %r to %s at t=%.4f"
              % (concentration, path, elapsed_time))
        return False
    with open(path, "a+") as outfile:
        outfile.write(f"{round(elapsed_time,4)},{round(concentration,2)}\n")
    return True


def find_optimal_pump_volumes(c1, cl, ch, target, maxdispense=5.0, vialvolume=22, pump_min=0.5,
                              return_delta=False):
    """
    Find low-pump volumes (vl1, vl2) for a two-step dispense cycle that best match
    a target concentration ramp, subject to hardware and biological safety constraints.

    Hardware constraint: each pump dispenses either 0 mL or [pump_min, maxdispense - pump_min].
    Safety constraint: intermediate concentration c1p after step 1 must not exceed c1 + target
    (cells are transiently diluted, never spiked above their current exposure level).
    That bound holds ONLY when c1 >= cl. Below the low reservoir's own concentration, pure
    low media raises the vial, so c1p > c1 + target by construction and no choice of
    volumes can prevent it.

    Two further limits are physics, not defects, and the caller should expect them:
      - The pump_min floor quantises the achievable step. Near c1 = cl a hold is accurate
        only to about +/- pump_min*(ch-cl)/(2*(vialvolume+maxdispense)).
      - The ramp stalls below the nominal ceiling: with cl=0, ch=5, V=22, D=5 the largest
        achievable step reaches zero at c1 ~ 4.776, and a 0.1 g/L step is already
        unachievable above c1 ~ 4.478.

    Args:
        c1:          Current vial concentration (same units as cl, ch).
        cl:          Concentration of the low (media) pump fluid.
        ch:          Concentration of the high (drug) pump fluid. Must satisfy ch > cl.
        target:      Desired concentration increase per cycle.
        maxdispense: Total volume dispensed per step (mL); default 5.0.
        vialvolume:  Vial volume before each dispense step (mL); default 22.
        pump_min:    Minimum nonzero dispense volume for either pump (mL); default 0.5.

        return_delta: When True, also return the concentration change the returned
                      volumes actually produce, so the caller can compare what it asked
                      for against what the hardware can deliver.

    Returns:
        (vl1, vl2): Low-pump volumes for step 1 and step 2 (mL).
                    High-pump volumes are maxdispense - vl1 and maxdispense - vl2.
        (vl1, vl2, achieved_delta) when return_delta is True.

    Raises:
        ValueError: if any argument is not finite, or if ch <= cl. Both used to produce
                    plausible-looking volumes: a NaN anywhere returned (maxdispense, 0),
                    a full high-concentration dispense, because every NaN comparison is
                    False and the candidate list was then selected by position.
    """
    for _name, _val in (("c1", c1), ("cl", cl), ("ch", ch), ("target", target),
                        ("maxdispense", maxdispense), ("vialvolume", vialvolume),
                        ("pump_min", pump_min)):
        try:
            _finite = math.isfinite(_val)
        except TypeError:
            _finite = False
        if not _finite:
            raise ValueError("find_optimal_pump_volumes: %s is not a finite number (%r). "
                             "Refusing to compute pump volumes." % (_name, _val))
    if ch <= cl:
        raise ValueError("find_optimal_pump_volumes: high reservoir concentration (%r) must "
                         "exceed low (%r)." % (ch, cl))

    def _ret(vl1, vl2):
        vl1, vl2 = float(vl1), float(vl2)
        if return_delta:
            return vl1, vl2, float(stepsize(c1, cl, ch, vl1, vl2, maxdispense, vialvolume))
        return vl1, vl2

    # b: concentration increment from swapping pump_min from low to high drug (scales with
    # the spread ch-cl, not ch alone, since the low pump now contributes cl instead of 0).
    # r²: two-step attenuation factor (each step dilutes by V/(V+D)).
    # c1_min: bootstrap threshold — below this, the min hardware step overshoots target more
    # than doing nothing. Derived by equating (delta_min - target)² = (delta_zero - target)².
    b = pump_min * (ch - cl) / (vialvolume + maxdispense)
    r = (vialvolume / (vialvolume + maxdispense)) ** 2
    c1_min = max(0, (b - 2 * target) / (2 * (1 - r)))

    # Only bootstrap when an increase was actually requested. With target <= 0 this
    # returned the minimum hardware drug step on a HOLD cycle -- precisely the option the
    # comment above identifies as worse than doing nothing -- silently adding up to
    # pump_min*(ch-cl)/(vialvolume+maxdispense) g/L per cycle. At cl=0 that is +0.0926 g/L,
    # 93% of a full ramp step, on a cycle that requested nothing, compounding to +0.2755
    # over twenty holds. (maxdispense, maxdispense) is already a candidate below and
    # achieves exactly zero.
    if target > 0 and c1 < c1_min:
        return _ret(maxdispense, maxdispense - pump_min)

    # vl1_lo: lower bound on vl1 enforcing c1p <= c1 + target (no transient spike).
    # Derived from (c1*V + (D-vl1)*ch + vl1*cl) / (V+D) <= c1 + target, solved for vl1.
    # Denominator is (ch-cl) because the low pump now displaces at concentration cl, not 0.
    vl1_lo = (maxdispense * (ch - c1) - (vialvolume + maxdispense) * target) / (ch - cl)
    vl1_lo = max(vl1_lo, pump_min)
    vl2_lo = pump_min
    hi = maxdispense - pump_min

    def delta_of(vl1, vl2):
        return stepsize(c1, cl, ch, vl1, vl2, maxdispense, vialvolume)

    def loss(vl1, vl2):
        return (delta_of(vl1, vl2) - target) ** 2

    candidates = []
    pinned_vl1 = [maxdispense]        # only low drug in step 1 (maximally diluting)
    pinned_vl2 = [0, maxdispense]     # only high or only low drug in step 2

    # Both steps pinned
    for vl1 in pinned_vl1:
        for vl2 in pinned_vl2:
            candidates.append((vl1, vl2, loss(vl1, vl2)))

    # Step 1 pinned, step 2 free
    for vl1 in pinned_vl1:
        if vl2_lo <= hi:
            res = minimize_scalar(lambda v: loss(vl1, v), bounds=(vl2_lo, hi), method='bounded')
            candidates.append((vl1, res.x, res.fun))

    # Step 2 pinned, step 1 free
    for vl2 in pinned_vl2:
        if vl1_lo <= hi:
            res = minimize_scalar(lambda v: loss(v, vl2), bounds=(vl1_lo, hi), method='bounded')
            candidates.append((res.x, vl2, res.fun))

    # Both steps free
    if vl1_lo <= hi and vl2_lo <= hi:
        x0 = [(vl1_lo + hi) / 2, (vl2_lo + hi) / 2]
        res = minimize(lambda x: loss(x[0], x[1]), x0,
                       method='L-BFGS-B', bounds=[(vl1_lo, hi), (vl2_lo, hi)])
        candidates.append((res.x[0], res.x[1], res.fun))

    # Squared error is symmetric, so on a hold it will take an overshoot that is
    # marginally closer than the available undershoot -- at cl=0 it prefers +0.046 to
    # -0.047 and climbs. A culture that drifts down recovers; one that drifts up does not.
    # Prefer candidates that do not exceed the request, falling back to the full set only
    # when physics forbids it (c1 < cl, where pure low media still carries drug).
    if target <= 0:
        not_increasing = [t for t in candidates if delta_of(t[0], t[1]) <= target + HOLD_TOL]
        if not_increasing:
            candidates = not_increasing

    vl1_opt, vl2_opt, _ = min(candidates, key=lambda t: t[2])
    return _ret(vl1_opt, vl2_opt)

def bye(arg=None):
    if arg is not None:
        print(arg)
    sys.exit()

def _load_shared_config_validation():
    """Load config_validation.py from beside this file.

    By explicit path rather than `import config_validation`: this script is launched by
    the eVOLVER framework, whose working directory is not guaranteed to be this one, and
    a bare import would silently depend on that. This is the SAME file the log server
    validates against -- see its docstring.
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_validation.py")
    spec = importlib.util.spec_from_file_location("or05_config_validation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


configval = _load_shared_config_validation()

# Resolved against the process working directory, exactly as Settings() has always done.
# Named once so startup and live reload can never end up reading different files.
CONFIG_PATH = configval.CONFIG_FILENAME

# Fingerprint of the config as of the last SUCCESSFUL live apply. Updated only on
# success, so a config that fails validation is re-read and re-reported every cycle
# rather than being skipped as "unchanged" and going quiet.
_LIVE_CONFIG_STATE = {"fingerprint": None, "mode_notice": None}


def report_config_problems(config, context):
    """Print validation problems and warnings. Returns (problems, warnings).

    Startup deliberately warns rather than exiting. The shared validator enforces the
    INTENDED rules, which are stricter than what Settings() has historically accepted --
    the config running at the time this was written fails one of them
    (calib_name: 'None'). Making that fatal would mean any future tightening of the
    rules could stop an eVOLVER from booting, quite possibly one nobody is sitting next
    to. The live-reload path DOES refuse a config with problems, because there the
    alternative is to keep the settings already in force, which is always safe.
    """
    try:
        problems, warnings = configval.validate_config(config)
    except configval.ModeNotImplemented as exc:
        print("[config:%s] not validated -- %s" % (context, exc))
        return ([], [])
    for w in warnings:
        print("[config:%s] warning: %s" % (context, w))
    for prob in problems:
        print("[config:%s] PROBLEM: %s" % (context, prob))
    return (problems, warnings)


def plan_live_changes(settings, values):
    """Work out what WOULD change, without touching `settings`.

    Returns (changes, staged): changes is [(field, vial, old, new)], staged is
    {attr: a complete new 16-element list}. Anything that could go wrong -- a missing or
    wrong-length target list -- raises here, before a single value has been written, so a
    failure cannot leave the rig half-configured. The previous version mutated as it
    walked; adversarial testing left 58 of 64 values applied and the remaining 6 not,
    while the caller printed that nothing had changed.
    """
    changes, staged = [], {}
    for attr in configval.LIVE_FIELD_NAMES:
        current = getattr(settings, attr, None)
        if current is None:      # a mode whose Settings branch never built this list
            continue
        new_values = values.get(attr)
        if new_values is None:
            continue
        if len(current) != len(new_values):
            raise ValueError("settings.%s holds %d entries but the config produced %d; "
                             "refusing to apply a partial update"
                             % (attr, len(current), len(new_values)))
        staged[attr] = list(new_values)
        for vial, new in enumerate(new_values):
            old = current[vial]
            if old != new:
                changes.append((attr, vial, old, new))
    return changes, staged


def commit_live_changes(settings, staged):
    """Install the planned lists. Assumes plan_live_changes already validated them.

    Slice assignment rather than rebinding: `settings` is a module-level global whose
    lists are read directly at dozens of call sites (settings.target_ramp[x] and
    friends), so each list object's identity must survive the update.
    """
    for attr, new_values in staged.items():
        getattr(settings, attr)[:] = new_values


def apply_live_values(settings, values):
    """Plan, then commit. Returns [(field, vial, old, new)] for what changed."""
    changes, staged = plan_live_changes(settings, values)
    commit_live_changes(settings, staged)
    return changes


def log_config_changes(path, elapsed_time, changes):
    """Append one row per applied change: time,field,vial,old,new.

    A live settings change is a real experimental event. Without a durable record, a
    ramp that changed at hour 40 is indistinguishable later from one that was always
    set that way -- and this file is what evolution_log.json would cite as its source.
    """
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
    ## By SIZE, not existence: a previous append that failed part-way (a full disk, for
    ## one) can leave a zero-byte file behind, and treating that as "already has a header"
    ## makes the first data row masquerade as the header for every later reader.
    empty = (not os.path.exists(path)) or os.path.getsize(path) == 0
    with open(path, "a+") as fh:
        if empty:
            fh.write("time,field,vial,old,new\n")
        for field, vial, old, new in changes:
            fh.write("%s,%s,%d,%s,%s\n" % (round(elapsed_time, 4), field, vial, old, new))


## Restart-only per-vial fields worth NAMING when they differ, with the Settings
## attribute holding the running value. Each describes something physical (a bottle,
## a vial, a pump) or is read only once, so applying it live would reinterpret
## history already logged -- but the operator still deserves to be told it was seen.
_FROZEN_FIELDS = (
    ("high_concentration", "high_concentration"),
    ("low_concentration", "low_concentration"),
    ("volume", "volume"),
    ("input_pump2", "input_pump2"),
    ("fold_dilution", "fold_dilution"),
    ("initial_concentration", "initial_concentration"),
    ("initial_drug_target", "initial_drug_target"),
    ("temperature", "temperature"),
)


def _describe_frozen_changes(settings, config):
    """['vial 3 high_concentration 5.0 -> 20.0 ...'] for restart-only differences."""
    out = []
    try:
        per_vial = (((config or {}).get("experiment_settings") or {})
                    .get("per_vial_settings") or [])
        for entry in per_vial:
            if not isinstance(entry, dict) or not entry.get("to_run"):
                continue
            vial = entry.get("vial")
            if isinstance(vial, bool) or not isinstance(vial, int) \
                    or not (0 <= vial <= 15):
                continue
            ## to_run first, separately: it is a bool, it is the field LIVE_CONFIG.md
            ## singles out as most misleading, and flipping it changes nothing until
            ## a restart while the vial keeps being dosed either way.
            running_run = getattr(settings, "vials_to_run", None)
            if running_run is not None and vial < len(running_run) \
                    and isinstance(entry.get("to_run"), bool):
                was = bool(running_run[vial] == 1)
                if bool(entry["to_run"]) != was:
                    out.append("vial %d to_run %s -> %s (vial is still %s)"
                               % (vial, was, entry["to_run"],
                                  "running" if was else "excluded"))
            for key, attr in _FROZEN_FIELDS:
                running = getattr(settings, attr, None)
                if running is None or vial >= len(running):
                    continue
                if key not in entry:
                    continue
                new_value = entry[key]
                if isinstance(new_value, bool) \
                        or not isinstance(new_value, (int, float)):
                    continue
                current = running[vial]
                if current is None or not isinstance(current, (int, float)):
                    continue
                if abs(float(new_value) - float(current)) > 1e-9:
                    out.append("vial %d %s %s -> %s (running value still %s)"
                               % (vial, key, current, new_value, current))
    except Exception:
        return []                    # a diagnostic must never break the reload
    return out


def refresh_live_settings(settings, elapsed_time, config_changes_path=None):
    """Re-read the config and apply any changed live-reloadable field. Never raises.

    Called once per event cycle, before the per-vial loop, so every vial in a cycle sees
    one consistent snapshot rather than a file that might be saved halfway through.

    Every failure mode -- unreadable file, malformed yaml (an editor caught mid-save), a
    validation problem, a live value out of range -- leaves the settings already in
    force completely untouched and prints why. Keeping the last good config running is
    always safe; refusing to run is not. Nothing is applied partially: either the whole
    config validates and every live field is applied, or none is.

    Returns the applied changes, empty when nothing changed or nothing could be.
    """
    def refuse(problems):
        print("[config:live] REFUSING to apply %s -- %d problem(s); the settings "
              "already in force are unchanged:" % (CONFIG_PATH, len(problems)))
        for prob in problems:
            print("[config:live]   %s" % prob)
        return []                           # fingerprint NOT cached: re-report next cycle

    try:
        fingerprint = configval.config_fingerprint(CONFIG_PATH)
        if fingerprint is None:
            ## The file is gone or unreadable. Drop the cached fingerprint rather than
            ## keeping one that describes a file that no longer exists: otherwise a
            ## restore from a mtime-preserving copy (cp -p, rsync -a, tar -x) lands with
            ## the old fingerprint still cached and is ignored forever, in silence.
            _LIVE_CONFIG_STATE["fingerprint"] = None
        elif fingerprint == _LIVE_CONFIG_STATE["fingerprint"]:
            return []

        config = configval.read_config(CONFIG_PATH)

        ## Checked BEFORE validate_config. Otherwise a config switched to the other
        ## mode has to satisfy THAT mode's rules first, so the operator gets a wall
        ## of "missing n_tolerant" problems instead of "you changed the mode".
        config_mode = (((config or {}).get("experiment_settings") or {})
                       .get("operation") or {}).get("mode")
        if config_mode != settings.operation_mode:
            return refuse(["operation.mode in the file is %r but this controller is "
                           "running %r. Live reload applies only to the running "
                           "mode's fields; restart to change mode."
                           % (config_mode, settings.operation_mode)])

        try:
            problems, _warnings = configval.validate_config(config)
        except configval.ModeNotImplemented as exc:
            ## Say so. Silence here meant a one-character corruption of `mode:` disabled
            ## live reload permanently with no output at all. Once per distinct message,
            ## so it does not repeat every cycle forever.
            ## Keyed on (message, fingerprint), not the message alone. Deduping on
            ## text meant a one-character corruption of `mode:` printed a single line
            ## and then swallowed every later edit in silence -- an n_tolerant change
            ## written during that window only took effect once the mode was fixed.
            notice = (str(exc), fingerprint)
            if _LIVE_CONFIG_STATE.get("mode_notice") != notice:
                print("[config:live] not applied -- %s" % exc)
                print("[config:live] NOTHING in this file will be applied while "
                      "operation.mode is unusable, including live-field edits.")
                _LIVE_CONFIG_STATE["mode_notice"] = notice
            return []
        _LIVE_CONFIG_STATE["mode_notice"] = None

        ## Raw-value checks before extraction, because the cast can both launder an
        ## out-of-range value into range and raise before any check runs.
        problems = list(problems) + configval.validate_raw_live_values(config)
        if problems:
            return refuse(problems)

        ## Only now: extract_live_values documents that it assumes a validated config, and
        ## calling it earlier replaced the validator's precise message with a bare
        ## IndexError in 16 of the cases adversarial testing found.
        values = configval.extract_live_values(config)
        problems = configval.validate_live_values(values)
        if problems:
            return refuse(problems)

        changes, staged = plan_live_changes(settings, values)

        ## Write the audit record BEFORE mutating anything. Previously the order was
        ## mutate, cache the fingerprint, then log -- so any log failure (a full disk, a
        ## read-only mount, a bad path) left the new dose live, printed "keeping the
        ## settings already in force", returned [], and never retried. A log row for a
        ## change that then fails to commit is a harmless discrepancy; a live dose change
        ## with no audit record is not.
        if changes and config_changes_path:
            log_config_changes(config_changes_path, elapsed_time, changes)

        commit_live_changes(settings, staged)
        _LIVE_CONFIG_STATE["fingerprint"] = fingerprint
        for field, vial, old, new in changes:
            print("[config:live] vial %d %s: %s -> %s" % (vial, field, old, new))
        ## Reported whether or not live fields also changed. Suppressing it when
        ## something else applied hid exactly the likeliest real save -- "I swapped
        ## the drug bottle and bumped the ramp while I was in there" -- leaving the
        ## controller solving doses against the old reservoir with no output at all.
        for line in _describe_frozen_changes(settings, config):
            print("[config:live] NOT LIVE: %s -- restart to apply" % line)
        if not changes:
            ## Acknowledge the read even when nothing moved. Reaching this point means the
            ## file was actually touched (the fingerprint gate returns early otherwise), so
            ## this is not chatter -- it is the answer to "did it see my edit?", which is
            ## otherwise indistinguishable from the file never having been read. Says which
            ## fields are live, because editing one of the others is the usual reason a
            ## change appears to have been ignored.
            ## The RUNNING mode's live fields, not the cross-mode union: the union
            ## advertised six fields that pumpcontrol_ramp never reads, on the one
            ## line whose job is answering "did it see my edit?".
            live_names = [spec[0] for spec in configval.live_fields_for_config(config)]
            ## And name what differs but is NOT live. "No live field changed" reads
            ## as "your edit was a no-op", which is wrong if the operator edited
            ## high_concentration BECAUSE they swapped the bottle -- the controller
            ## then keeps solving doses against the old number.
            frozen = _describe_frozen_changes(settings, config)
            for line in frozen:
                print("[config:live] NOT LIVE: %s -- restart to apply" % line)
            if not frozen:
                print("[config:live] re-read %s: valid, nothing changed. Live fields "
                      "for this mode are %s; anything else takes effect on the next "
                      "controller start." % (CONFIG_PATH, ", ".join(live_names)))
        return changes

    except Exception as exc:                # never take the control loop down
        print("[config:live] could not reload %s (%s: %s); keeping the settings already "
              "in force" % (CONFIG_PATH, type(exc).__name__, exc))
        return []


class Settings():
    def __init__(self):
        config = configval.read_config(CONFIG_PATH)
        ## Shared validation, reporting only -- see report_config_problems for why
        ## startup warns where live reload refuses.
        report_config_problems(config, "startup")
        ### Validate:
        try:
            config.get("experiment_settings")
        except:
            bye("Required section `experiment_settings` missing. Exiting...")

        self.exp_name = config["experiment_settings"].get("exp_name", None)
        if self.exp_name is None:
            bye("Required variable `exp_name` missing")
            
        self.calib_name = config["experiment_settings"].get("calib_name", None)
        per_vial_dict = {vialsettings["vial"]:vialsettings\
                         for vialsettings in\
                         config["experiment_settings"].get("per_vial_settings", {})}
        if len(per_vial_dict) == 0:
            bye("Per vial settings may be needed for custom functions!")
        if len(per_vial_dict) < 16:
            print(f"Found settings for {len(per_vial_dict)} vials")
            print(f"Active vials are {self.fmt([vial if (vial in per_vial_dict.keys() and per_vial_dict[vial]['to_run'])  else '' for vial in range(16)])}")

        ## active_vials is the main entry point for overriding all other per vial settings.
        self.active_vials = []
        
        for vial, vialsettings in per_vial_dict.items():
            if vialsettings.get("to_run") is None:
                print(f"Should I run vial['vial']?")
            if vialsettings.get("to_run"):
                if type(vial) is int:
                    self.active_vials.append(vial)
                else:
                    bye(f"Which vial does this entry specify? Stuck at {vialsettings}.")
        
        # Global stir handling
        if config["experiment_settings"].get("stir_settings") is not None:
            self.stir_switch = config["experiment_settings"]["stir_settings"].get("stir_switch")            

            self.stir_on_rate = [0]*16
            for vidx in self.active_vials:
                self.stir_on_rate[vidx] = config["experiment_settings"]["stir_settings"]["stir_on_rate"]

            if (self.stir_switch is not None) and (self.stir_switch):
                self.stir_off_rate = [0]*16
                self.stir_on_duration = [0] * 16
                self.stir_off_duration = [0] * 16                
                for vidx in self.active_vials:
                    self.stir_on_rate[vidx] = per_vial_dict[vidx].get("stir_on_rate",
                                                                      config["experiment_settings"]["stir_settings"]["stir_on_rate"])
                    self.stir_off_rate[vidx] = per_vial_dict[vidx].get("stir_off_rate",
                                                                       config["experiment_settings"]["stir_settings"]["stir_off_rate"])
                    
                    self.stir_on_duration[vidx] = per_vial_dict[vidx].get("stir_on_duration",
                                                                          config["experiment_settings"]["stir_settings"].get("stir_on_duration", 0))
                                                                          
                    self.stir_off_duration[vidx] = per_vial_dict[vidx].get("stir_off_duration",
                                                                           config["experiment_settings"]["stir_settings"].get("stir_off_duration", 0))
                
            
        # Global temperature handling
        if config["experiment_settings"].get("temp_all") is not None:
            temp = config["experiment_settings"]["temp_all"]
            self.temperature = [temp]*16
            for vidx in self.active_vials:
                self.temperature[vidx] = per_vial_dict[vidx].get("temperature",25)


        if config["experiment_settings"].get("estimate_gr") is not None:
            self.estimate_gr = config["experiment_settings"].get("estimate_gr")

        
        self.operation_mode = config["experiment_settings"]["operation"].get("mode", None)
        if self.operation_mode is None:
            bye("Invalid operation mode specification")



        #for 
                    
        ## The following are variables that can be set per vial, but are not required
        ## for all operation modes.
        self.volume = [per_vial_dict[vidx].get("volume")\
                       if vidx in self.active_vials\
                       else 0
                       for vidx in range(16)]

        if self.operation_mode == "calibration":
            """
            Calibration requires three parameters:
            1. calibration initial od
            2. calibration final od 
            3. number of calibration steps DEFAULT 20
            """
            self.vials_to_run = [0]*16
            for vidx in self.active_vials:
                self.vials_to_run[vidx] = 1
            self.calibration_initial_od = [0.0]*16 ### TODO CHECK FOR BEHAVIOR
            self.calibration_end_od = [0.0]*16 ### TODO CHECK FOR BEHAVIOR
            self.calibration_fold_range = config["experiment_settings"]["operation"].get("fold_calibration", np.nan)
            self.calibration_measured_od = config["experiment_settings"]["operation"].get("measured_od")
            
            for vidx in self.active_vials:
                self.calibration_initial_od[vidx] = per_vial_dict[vidx].get("calib_initial_od")
                self.calibration_end_od[vidx] = per_vial_dict[vidx].get("calib_end_od", None)
                if self.calibration_end_od[vidx] is None and not np.isnan(self.calibration_fold_range):
                    self.calibration_end_od[vidx] = self.calibration_initial_od[vidx]/self.calibration_fold_range
                # if self.calibration_end_od is None:
                #     bye("Please specify global setting `end_od` in operation: calibration.")

            self.calibration_num_pump_events = config["experiment_settings"]["operation"].get("num_pump_events", 20)
            
        if self.operation_mode == "chemostat":
            """
            Chemostat operation:
            Required parameters:
            1. chemostat rate: per vial configuration
            Optional parameters:
            1. chemostat start OD DEFAULT 0
            2. chemostat start time DEFAULT 0
            3. chemostat growth rate responsive start DEFAULT 0
               This requires accurate growth rate estimation.
            """
            self.chemo_rate = [0]*16
            self.chemo_start_od = [0.]*16
            self.chemo_start_time = [0.] * 16
            self.chemo_growth_rate_responsive_start = config["experiment_settings"]["operation"].get("growth_rate_responsive_start", False)            
            for vidx in self.active_vials:
                for key in ["chemo_rate"]:
                    if key not in per_vial_dict[vidx].keys():
                        bye(f"Missing {key} specification for vial {vidx}")
                self.chemo_rate[vidx] = per_vial_dict[vidx].get("chemo_rate")
                self.chemo_start_od[vidx] = per_vial_dict[vidx].get("chemo_start_od", 0)
                self.chemo_start_time[vidx] = per_vial_dict[vidx].get("chemo_start_time", 0)

        if self.operation_mode == "chemostat_dual":
            """
            Chemostat operation:
            Required parameters:
            1. chemostat rate: per vial configuration
            Optional parameters:
            1. chemostat start OD DEFAULT 0
            2. chemostat start time DEFAULT 0
            3. chemostat growth rate responsive start DEFAULT 0
               This requires accurate growth rate estimation.
            """
            self.chemo_rate = [0]*16
            self.chemo_rate_2 = [0]*16
            self.chemo_start_od = [0.]*16
            self.chemo_start_time = [0.] * 16
            self.chemo_growth_rate_responsive_start = config["experiment_settings"]["operation"].get("growth_rate_responsive_start", False)            
            for vidx in self.active_vials:
                for key in ["chemo_rate"]:
                    if key not in per_vial_dict[vidx].keys():
                        bye(f"Missing {key} specification for vial {vidx}")
                self.chemo_rate[vidx] = per_vial_dict[vidx].get("chemo_rate")
                self.chemo_rate_2[vidx] = per_vial_dict[vidx].get("chemo_rate_2")
                self.chemo_start_od[vidx] = per_vial_dict[vidx].get("chemo_start_od", 0)
                self.chemo_start_time[vidx] = per_vial_dict[vidx].get("chemo_start_time", 0)
                
        if self.operation_mode == "turbidostat":
            """
            Turbidostat operation:
            Required parameters:
            1. turbidostat low threshold : per vial configuration
            2. turbidostat high threshold : per vial configuration
            """
            self.turbidostat_low = [9999]*16
            self.turbidostat_high = [9999]*16
            self.vials_to_run = [0]*16
            self.input_pump2 = [32 + i for i in range(16)]
            for vidx in self.active_vials:
                self.vials_to_run[vidx] = 1                
                for key in ["turbidostat_low", "turbidostat_high"]:
                    if key not in per_vial_dict[vidx].keys():
                        bye(f"Missing {key} specification for vial {vidx}")
                self.turbidostat_low[vidx] = per_vial_dict[vidx].get("turbidostat_low")
                self.turbidostat_high[vidx] = per_vial_dict[vidx].get("turbidostat_high")
                
        if self.operation_mode == "morbidostat":
            """
            Morbidostat operation
            """
            self.vials_to_run = [0]*16            
            for vidx in self.active_vials:
                self.vials_to_run[vidx] = 1
            self.setpoint = [100]*16
            self.interval = [1000]*16
            self.doubling_time = [np.nan]*16
            self.input_pump2 = [32 + i for i in range(16)]            
            for vidx in self.active_vials:
                self.setpoint[vidx] = per_vial_dict[vidx].get("morbidostat_setpoint", 100)
                self.doubling_time[vidx] = per_vial_dict[vidx].get("doubling_time", np.nan)
                self.interval[vidx] = round(np.log(1.1)*self.doubling_time[vidx]/np.log(2),3)
                self.input_pump2[vidx] = int(per_vial_dict[vidx].get("input_pump2", vidx+32))

        if self.operation_mode == "pumpcontrol_ramp":
            """
            Pumpcontrol Ramp 
            """
            self.vials_to_run = [0]*16            
            for vidx in self.active_vials:
                self.vials_to_run[vidx] = 1
            self.setpoint = [100]*16
            self.interval = [10000]*16
            self.number_consecutive_intervals = [10000]*16
            self.input_pump2 = [32 + i for i in range(16)]            
            self.initial_concentration = [0]*16
            self.high_concentration = [0]*16
            self.low_concentration = [0]*16
            self.target_ramp = [0]*16
            for vidx in self.active_vials:
                self.setpoint[vidx] = per_vial_dict[vidx].get("setpoint", 100)
                self.interval[vidx] = per_vial_dict[vidx].get("interval", 10000)
                self.input_pump2[vidx] = int(per_vial_dict[vidx].get("input_pump2", vidx+32))
                self.number_consecutive_intervals[vidx] = int(per_vial_dict[vidx].get("number_consecutive_intervals", 1000))
                self.initial_concentration[vidx] = float(per_vial_dict[vidx].get("initial_concentration",0))
                self.high_concentration[vidx] = float(per_vial_dict[vidx].get("high_concentration",0))
                self.low_concentration[vidx] = float(per_vial_dict[vidx].get("low_concentration",0))
                self.target_ramp[vidx] = float(per_vial_dict[vidx].get("target_ramp",0))

        if self.operation_mode == "alternating_selection":
            """
            Alternating selection (switch_logic_outline.txt, 2026-09-21).

            Two states per vial, alternating forever:

              HIGH  challenge at Current_Drug. Count consecutive growth cycles that
                    reach the OD setpoint within growth_interval, counting only once
                    the vial has CLIMBED to Current_Drug. n_tolerant in a row inside
                    Stress_Wait_Time earns +ramp; the budget expiring first holds.
              LOW   purge fold_dilution of the drug in fixed 5 mL steps, then dilute
                    with plain media until n_dilutions consecutive cycles come in
                    inside growth_interval. Then back to HIGH.

            Two timings are DERIVED at the point of use, not cached here, so that a
            live edit to media_wait_time or n_tolerant retimes them in the same cycle:
                growth_interval  = growth_interval_multiplier * media_wait_time
                Stress_Wait_Time = n_tolerant * stress_wait_fraction * media_wait_time
            """
            self.vials_to_run = [0]*16
            for vidx in self.active_vials:
                self.vials_to_run[vidx] = 1
            self.setpoint = [100.]*16
            self.input_pump2 = [32 + i for i in range(16)]
            self.initial_concentration = [0.]*16
            self.high_concentration = [0.]*16
            self.low_concentration = [0.]*16
            self.initial_drug_target = [0.]*16
            self.n_tolerant = [5]*16
            self.n_dilutions = [6]*16
            self.ramp = [0.5]*16
            self.media_wait_time = [1.5]*16
            self.growth_interval_multiplier = [3.0]*16
            self.stress_wait_fraction = [0.9]*16
            self.fold_dilution = [10.0]*16
            for vidx in self.active_vials:
                self.setpoint[vidx] = float(per_vial_dict[vidx].get("setpoint", 100))
                self.input_pump2[vidx] = int(per_vial_dict[vidx].get("input_pump2", vidx+32))
                self.initial_concentration[vidx] = float(per_vial_dict[vidx].get("initial_concentration", 0))
                self.high_concentration[vidx] = float(per_vial_dict[vidx].get("high_concentration", 0))
                self.low_concentration[vidx] = float(per_vial_dict[vidx].get("low_concentration", 0))
                ## Default the starting target to what is already in the vial: with no
                ## climb to do, the tolerance streak can start on the first cycle.
                self.initial_drug_target[vidx] = float(
                    per_vial_dict[vidx].get("initial_drug_target",
                                            self.initial_concentration[vidx]))
                self.n_tolerant[vidx] = int(per_vial_dict[vidx].get("n_tolerant", 5))
                self.n_dilutions[vidx] = int(per_vial_dict[vidx].get("n_dilutions", 6))
                self.ramp[vidx] = float(per_vial_dict[vidx].get("ramp", 0.5))
                self.media_wait_time[vidx] = float(per_vial_dict[vidx].get("media_wait_time", 1.5))
                self.growth_interval_multiplier[vidx] = float(
                    per_vial_dict[vidx].get("growth_interval_multiplier", 3.0))
                self.stress_wait_fraction[vidx] = float(
                    per_vial_dict[vidx].get("stress_wait_fraction", 0.9))
                self.fold_dilution[vidx] = float(per_vial_dict[vidx].get("fold_dilution", 10.0))

    def fmt(self, l, numtabs=0):
        sep = "".join(["\t"]*numtabs)
        s = "\n" + sep
        if type(l) is list:
            for idx, v in enumerate(l):
                s +=str(v).format('%.1f') + "\t"
                if (idx +1) % 4 == 0:
                    s += "\n" + sep
        else:
             s = str(l)       
        return(s)
            
    def __repr__(self):
        s = ""
        s += f'Experiment name: {self.exp_name}\n'
        if self.calib_name is None:
            s += f'Calib name: CURRENTLY UNSET\n'
        else:
            s += f'Calib name : {self.calib_name}\n'
        s += "--- GLOBAL ---\n"
        s += f"Estimate Growth Rate: {self.estimate_gr}\n"
        s += f"Temperature: {self.fmt(self.temperature)}\n"
        if self.stir_switch:
            s += f"Switch stir rate: Yes\n"
            s += f"Stir on rates: {self.fmt(self.stir_on_rate)}\n"
            s += f"Stir off rates: {self.fmt(self.stir_off_rate)}\n"
            s += f"Stir on durations: {self.fmt(self.stir_on_duration)}\n"
            s += f"Stir off durations: {self.fmt(self.stir_off_duration)}\n"             
        else:
            s += f"Switch stir rate: No\n"
            s += f"Stir rate: {self.stir_on_rate}\n"
        #s += f"Temperature: {self.temp_all}\n"                
        s += "--------------\n"        
        s += f'Vial Volumes : {self.fmt(self.volume)}\n'
        if self.operation_mode == "calibration":
            s += "Mode selection: calibration\n"
            s += f"\tInitial ODs: {self.fmt(self.calibration_initial_od, 1)}\n"
            s += f"\tFinal OD: {self.calibration_end_od}\n"
        if self.operation_mode == "chemostat":
            s += "Mode selection: chemostat\n"
            s += f"\tChemostat rate: {self.fmt(self.chemo_rate, 1)}\n"
            s += f"\tChemostat start od: {self.fmt(self.chemo_start_od,1)}\n"
            s += f"\tChemostat start time: {self.fmt(self.chemo_start_time,1)}\n"
            s += f"\tGrowth rate responsive start?: {self.chemo_growth_rate_responsive_start}\n"
        if self.operation_mode == "chemostat_dual":
            s += "Mode selection: chemostat\n"
            s += f"\tChemostat rate: {self.fmt(self.chemo_rate, 1)}\n"
            s += f"\tChemostat rate In2: {self.fmt(self.chemo_rate_2, 1)}\n"
            s += f"\tChemostat start od: {self.fmt(self.chemo_start_od,1)}\n"
            s += f"\tChemostat start time: {self.fmt(self.chemo_start_time,1)}\n"
            s += f"\tGrowth rate responsive start?: {self.chemo_growth_rate_responsive_start}\n"            
        if self.operation_mode == "turbidostat":
            s += "Mode selection: turbidostat\n"
            s += f"\tTurbidostat low: {self.fmt(self.turbidostat_low, 1)}\n"
            s += f"\tTurbidostat high: {self.fmt(self.turbidostat_high, 1)}\n"
        if self.operation_mode == "morbidostat":
            s += "Mode selection: morbidostat\n"
            s += f"\tSetpoints: {self.fmt(self.setpoint, 1)}\n"
            s += f"\tIntervals: {self.fmt(self.interval, 1)}\n"
            s += f"\tDoubling Time: {self.fmt(self.doubling_time, 1)}\n"
        if self.operation_mode == "alternating_selection":
            s += "Mode selection: alternating_selection\n"
            s += f"\tSetpoints: {self.fmt(self.setpoint, 1)}\n"
            s += f"\tCurrent_Drug at start: {self.fmt(self.initial_drug_target, 1)}\n"
            s += f"\tVial start conc: {self.fmt(self.initial_concentration, 1)}\n"
            s += f"\tReservoirs low: {self.fmt(self.low_concentration, 1)}\n"
            s += f"\tReservoirs high: {self.fmt(self.high_concentration, 1)}\n"
            s += f"\tRamp: {self.fmt(self.ramp, 1)}\n"
            s += f"\tN tolerant: {self.fmt(self.n_tolerant, 1)}\n"
            s += f"\tN dilutions: {self.fmt(self.n_dilutions, 1)}\n"
            s += f"\tMedia wait time (h): {self.fmt(self.media_wait_time, 1)}\n"
            ## The two derived timings, spelled out at startup because they are what
            ## the controller actually compares against and neither appears in the
            ## config file.
            s += (f"\tgrowth_interval (h): "
                  f"{self.fmt([round(self.growth_interval_multiplier[v] * self.media_wait_time[v], 4) for v in range(16)], 1)}\n")
            s += (f"\tStress_Wait_Time (h): "
                  f"{self.fmt([round(self.n_tolerant[v] * self.stress_wait_fraction[v] * self.media_wait_time[v], 4) for v in range(16)], 1)}\n")
            s += f"\tFold dilution: {self.fmt(self.fold_dilution, 1)}\n"
        return(s)
        
        
settings = Settings()
print(settings)
#bye()
        
# Port for the eVOLVER connection. You should not need to change this unless you have multiple applications on a single RPi.
EVOLVER_PORT = 8081

# if using a different mode, name your function as the OPERATION_MODE variable

##### END OF USER DEFINED GENERAL SETTINGS #####


# logger setup
logger = logging.getLogger(__name__)


def at_target_GR(vial, averageGR, target_GR, targetcheck,
                 time, GRpass, GRcheck, file_name):
    SAVE_PATH = os.path.dirname(os.path.realpath(__file__))
    EXP_DIR = os.path.join(SAVE_PATH, settings.exp_name)
    gr_status_path = os.path.join(EXP_DIR, 'growthrate_status', file_name)
    TOLERANCE = 0.05
    if GRpass == 1:
        with open(gr_status_path, "a+") as outfile:
            outfile.write(f"{time[-1]},1,1\n")
        return True
    else:
        if GRcheck == 1:
            if abs(averageGR - target_GR) < TOLERANCE:
                file_name =  f"vial{vial}_growthrate_status.txt"
                with open(gr_status_path, "a+") as outfile:
                    outfile.write(f"{time[-1]},1,1\n")
                return True
            else:
                with open(gr_status_path, "a+") as outfile:
                    outfile.write(f"{time[-1]},0,1\n")            
                    return False
        else:
            if abs(averageGR - targetcheck) < TOLERANCE:
                file_name =  f"vial{vial}_growthrate_status.txt"
                with open(gr_status_path, "a+") as outfile:
                    outfile.write(f"{time[-1]},0,1\n")
                return False
            else:
                file_name =  f"vial{vial}_growthrate_status.txt"
                with open(gr_status_path, "a+") as outfile:
                    outfile.write(f"{time[-1]},0,0\n")
                return False           

if "sleevecalib" in settings.exp_name:
    currentcalibs = list(sorted([f for f in os.listdir("./") if settings.exp_name in f]))
    if len(currentcalibs) > 0:
        calibnum = max([int(f.split("_")[1]) for f in currentcalibs])
        newcalib = calibnum + 1
    else:
        newcalib = 0

    EXP_NAME = f"{settings.exp_name}_{newcalib}"
    print(EXP_NAME)
    
def stir_rate_control(eVOLVER, vials, settings, elapsed_time):
    def update_stir_log(stir_path, t1,t2,s):
        text_file = open(stir_path, "a+")
        text_file.write("{0},{1},{2}\n".format(t1,
                                               t2,
                                               s))
        text_file.close()                

        
    newstirrates = [0]*16
    odlogs = [os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            "od_90_raw", f"vial{x}_od_90_raw.txt")
              for x in vials]
    for x in settings.active_vials:
        ################################################
        # Stir rate control
        file_name =  "vial{0}_stirrate.txt".format(x)
        stir_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'stirrate', file_name)
        stirdata = np.genfromtxt(stir_path, delimiter=',')
        currstir = stirdata[len(stirdata)-1][2]
        currstirtime = stirdata[len(stirdata)-1][1]
        newstir = 0
        oddata = pd.read_csv(odlogs[x],
                               sep=",",names=["elapsed_time","od90"],
                               skiprows=[0])
              
        data = oddata[oddata.elapsed_time > currstirtime]
              

        if currstir == settings.stir_on_rate[x]:
            if data.shape[0] >= settings.stir_on_duration[x]:
                newstir = settings.stir_off_rate[x]
                update_stir_log(stir_path, elapsed_time, elapsed_time, newstir)
            else:
                newstir = settings.stir_on_rate[x]
                update_stir_log(stir_path, elapsed_time, currstirtime, settings.stir_on_rate[x])
        if currstir == settings.stir_off_rate[x]:
            if data.shape[0] >= settings.stir_off_duration[x]:
                newstir = settings.stir_on_rate[x]
                update_stir_log(stir_path, elapsed_time, elapsed_time, newstir)
            else:
                newstir = settings.stir_off_rate[x]                
                update_stir_log(stir_path, elapsed_time, currstirtime, settings.stir_off_rate[x])

        newstirrates[x] = newstir
        ################################################        
    eVOLVER.update_stir_rate(newstirrates)    
    
def growth_curve(eVOLVER, input_data, vials, elapsed_time):
    if settings.stir_switch:
        stir_rate_control(eVOLVER, vials, settings, elapsed_time)
    WINSIZE = 100
    SAVE_PATH = os.path.dirname(os.path.realpath(__file__))
    EXP_DIR = os.path.join(SAVE_PATH, settings.exp_name)        
    for x in vials: #main loop through each vial
        # Update chemostat configuration files for each vial
        #initialize OD and find OD path
        if settings.calib_name is not None:
            file_name =  "vial{0}_OD_autocalib.txt".format(x)
            OD_path = os.path.join(EXP_DIR, 'OD_autocalib', file_name)            
        else:
            file_name =  "vial{0}_OD.txt".format(x)
            OD_path = os.path.join(EXP_DIR, 'OD', file_name)

        ## First read in the entire data set...
        ODdata_full = np.genfromtxt(OD_path, delimiter=',')
        ## ...then average the last few values
        if settings.estimate_gr:
            ODdata_forgr = ODdata_full[-WINSIZE:, 1]
            time = ODdata_full[-WINSIZE:,0]            
            eVOLVER.aj_growth_rate(x, time, ODdata_forgr)                
    return

    
def calibration(eVOLVER, input_data, vials, elapsed_time):
    """
    Runs pumps for specified duration of time
    """

    ## First define pump action.
    ## Modify this section to increase the dilution size
    #endOD = settings.calibration_end_od


    #######
    num_pump_events = settings.calibration_num_pump_events + 1 ##### VERY IMPORTANT LOGIC
    ## This is set to +1 because the pump log file is initialized with a zero time point line.
    ## This makes the logic a little messy.
    
    
    volume_per_step = [vtr*vsleeve*( (od/endod)**(1/num_pump_events) - 1)\
                       if (od > 0) else 0 for vsleeve, endod, od, vtr in zip(settings.volume,
                                                                             settings.calibration_end_od,
                                                                             settings.calibration_initial_od,
                                                                             settings.vials_to_run)]
    flow_rate = eVOLVER.get_flow_rate() #read from calibration file

    pump_run_duration = [volume_per_step[x]/flow_rate[x]
                         if flow_rate[x] != '' else 0 for x in vials]

    MESSAGE = ["--"]*48
    pumplogs = [os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            "pump_log", f"vial{x}_pump_log.txt")
                for x in vials]
    odlogs = [os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            "od_90_raw", f"vial{x}_od_90_raw.txt")
                for x in vials]    
    pumpdata = pd.read_csv(pumplogs[0],
                           sep=",",names=["elapsed_time","last_pump"],
                           skiprows=[0])

    for x in vials:
        oddata = pd.read_csv(odlogs[x],
                               sep=",",names=["elapsed_time","od90"],
                               skiprows=[0])
        pumpdata = pd.read_csv(pumplogs[x],
                               sep=",",names=["elapsed_time","last_pump"],
                               skiprows=[0])
        last_pump = pumpdata.iloc[-1,0]
        timein = 0
        oddata = oddata[oddata.elapsed_time > last_pump]

        if (pumpdata.shape[0] == num_pump_events) and (not settings.calibration_measured_od):
            print("Ending calibration. Please insert tubes to measure ODs next.")            
            sys.exit()
        if (oddata.shape[0] == 10) and (settings.calibration_measured_od):
            print("Calibration done.")
            sys.exit()
            
        if oddata.shape[0] == 9:                
            MESSAGE[x] = "--"
            if settings.vials_to_run[x] == 1:
                MESSAGE[x+16] = str(15)
            else:
                MESSAGE[x+16] = "--"

            
        elif oddata.shape[0] == 10:
            timein = round(pump_run_duration[x],2)
            MESSAGE[x] = str(timein)
            MESSAGE[x + 16] = "--"
            
            with open(pumplogs[x], "a+") as outfile:
                outfile.write(f"{elapsed_time},{timein}\n")        
            
    if MESSAGE != ["--"]*48:
        eVOLVER.fluid_command(MESSAGE)

def turbidostat(eVOLVER, input_data, vials, elapsed_time):
    OD_data = input_data['transformed']['od']

    ##### USER DEFINED VARIABLES #####

    turbidostat_vials = vials #vials is all 16, can set to different range (ex. [0,1,2,3]) to only trigger tstat on those vials
    stop_after_n_curves = np.inf #set to np.inf to never stop, or integer value to stop diluting after certain number of growth curves
    OD_values_to_average = 50  # Number of values to calculate the OD average

    lower_thresh = settings.turbidostat_low #[0.2] * len(vials) #to set all vials to the same value, creates 16-value list
    upper_thresh = settings.turbidostat_high # [0.6] * 16

    ##### END OF USER DEFINED VARIABLES #####

    ##### Turbidostat Settings #####
    #Tunable settings for overflow protection, pump scheduling etc. Unlikely to change between expts

    time_out = 15 #(sec) additional amount of time to run efflux pump

    ## This variable is no long used.
    # pump_wait = 3 # (min) minimum amount of time to wait between pump events

    ##### End of Turbidostat Settings #####

    flow_rate = eVOLVER.get_flow_rate() #read from calibration file

    ##### Turbidostat Control Code Below #####

    # fluidic message: initialized so that no change is sent
    MESSAGE = ['--'] * 48
    newstirrates = []

    #### We shouldn't have to wait too long for the dilution, as it will mess up the experiment.
    #### Add at most 3mLs for each dilution event.
    num_pump_events = 10 # settings.calibration_num_pump_events

    pumplogs = [os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            "pump_log", f"vial{x}_pump_log.txt")
                for x in vials]
    ## NOTE I don't understand the conditional check here right now.
    volume_per_step = [vtr*vsleeve*( (highod/lowod)**(1/num_pump_events) - 1)\
                       if (highod > 0) else 0\
                       for vsleeve, lowod, highod, vtr\
                       in zip(settings.volume,
                              settings.turbidostat_low,
                              settings.turbidostat_high,
                              settings.vials_to_run)]

    pump_run_duration = [volume_per_step[x]/flow_rate[x]
                         if flow_rate[x] != '' else 0 for x in vials]     
    
    if settings.calib_name is not None:
        calibration = pd.read_csv(os.path.join(eVOLVER.exp_dir, f"{settings.calib_name}.csv"))

    WINSIZE = 60 
    #vtr = settings.active_vials
    for x in turbidostat_vials: #main loop through each vial
        # Update turbidostat configuration files for each vial
        # initialize OD and find OD path
        if settings.stir_switch:
            stirdf = pd.read_csv(os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                              'stirrate', f"vial{x}_stirrate.txt"),
                                 sep=",", )
        file_name =  "vial{0}_ODset.txt".format(x)
        
        ODset_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'ODset', file_name)
        data = np.genfromtxt(ODset_path, delimiter=',')
        ODset = data[len(data)-1][1]
        ODsettime = data[len(data)-1][0]
        num_curves=len(data)/2;
        
        if settings.calib_name is not None:
            file_name =  "vial{0}_OD_autocalib.txt".format(x)
            OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'OD_autocalib', file_name)
            data =  pd.read_csv(OD_path,sep=",",)
            ################################################################################################
            try:                                                                                         #
                inflectionpoint = calibration[calibration.vial == x].estimated_od_inflection.unique()[0] #
            except:                                                                                      #
                inflectionpoint = 0.01                                                                   #
            ################################################################################################
                
            # if inflectionpoint - np.median(data.od_plinear_135.values[-50:])  < 0.175: ## Arbirtrary cutoff close to inflection
            #     ## If we are close to the 135 inflection point, switch to 90
            #     sensor_to_use = "od_plinear_90"
            #     data["OD"] = data.od_plinear_90
            # else:
            #     sensor_to_use = "od_plinear_135"
            #     data["OD"] = data.od_plinear_135

            # if x == 0:
            #     sensor_to_use = "od_plinear_90"
            #     raw_path = "od_90_raw"
            #     sensor = 90
            sensor_to_use = "od_plinear_135"
            raw_path = "od_135_raw"
            sensor = 135
            data["OD"] = data[sensor_to_use]
        else:
            file_name =  "vial{0}_OD.txt".format(x)
            OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'OD', file_name)            
            data =  pd.read_csv(OD_path,sep=",", skiprows=[0],names=["time","OD"])
        try:
            if settings.estimate_gr and data.shape[0] > 2*WINSIZE:
                if settings.stir_switch:
                    grdata = data.merge(stirdf,
                                        left_on="time",
                                        right_on="Clock time")
                    grdata = grdata[grdata.stir_rate == 0]
                else:
                    grdata = data
                    
                if settings.calib_name:
                    ODdata_forgr = grdata[sensor_to_use].tail(WINSIZE)#.iloc[-WINSIZE:, sensor_to_use]
                    time = grdata["time"].tail(WINSIZE)#grdata.loc[-WINSIZE:,"time"]
                else:
                    ODdata_forgr = grdata.loc[-WINSIZE:, 1]
                    time = grdata.loc[-WINSIZE:,0]
                
                eVOLVER.aj_growth_rate(x, time, ODdata_forgr)
        except:
            pass
            
        ## Custom
        
        raw_file_name =  "vial{0}_{1}.txt".format(x,raw_path)
        OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, raw_path, raw_file_name)        

        sensordata = pd.read_csv(OD_path,
                                 sep=",",names=["elapsed_time","od"],
                                skiprows=[0]) 
        average_OD = 0
        # Determine whether turbidostat dilutions are needed
        collecting_more_curves = (num_curves <= (stop_after_n_curves + 2)) #logical, checks to see if enough growth curves have happened

        if data.shape[0] != 0:
            # Take median to avoid outlier
            od_values_from_file = data.OD.tail(20)    # Use only od90 if using ODac

            average_OD = float(np.median(od_values_from_file))

            #if recently exceeded upper threshold, note end of growth curve in ODset, allow dilutions to occur and growthrate to be measured


            ### If we have exceeded the calibration range, compare the raw values directly.
            beyond_upper_setpoint = ((average_OD > upper_thresh[x]) and (ODset != lower_thresh[x]))\
                or (float(np.median(sensordata.od.tail(10))) < calibration[(calibration.vial == x) & (calibration.sensor == sensor)].reading.min())
                
            if beyond_upper_setpoint:
                text_file = open(ODset_path, "a+")
                text_file.write("{0},{1}\n".format(elapsed_time,
                                                   lower_thresh[x]))
                text_file.close()
                ODset = lower_thresh[x]
            below_lower_setpoint = ((np.median(data.OD.tail(5)) <= lower_thresh[x] ) and (ODset != upper_thresh[x]))\
                or ((float(np.median(sensordata.od.tail(5))) > calibration[(calibration.vial == x) & (calibration.sensor == sensor)].reading.max()))
            
            if below_lower_setpoint:                
                text_file = open(ODset_path, "a+")
                text_file.write("{0},{1}\n".format(elapsed_time, upper_thresh[x]))
                text_file.close()
                ODset = upper_thresh[x]
            # # calculate growth rate

            #if have approx. reached lower threshold, note start of growth curve in ODset
            ## If we are _now_ lower than the lower set point, and in the past we were diluting,
            ## then stop diluting, and set the target to the upper threshold.
            is_diluting = ((average_OD > lower_thresh[x]) and (ODset == lower_thresh[x])) or beyond_upper_setpoint
            if is_diluting and collecting_more_curves:
                pumpdata = pd.read_csv(pumplogs[x],
                                       sep=",",names=["elapsed_time","last_pump"],
                                       skiprows=[0])
                last_pump = pumpdata.iloc[-1,0]
                oddata = data[data.time > last_pump]
                ### This logic is different from the original turbidostat logic
                ### Trace back the dilution curve like a calibation event
                
                # Wait two timepoints, 40 s.
                if oddata.shape[0] == 1:                
                    MESSAGE[x] = "--"   ### Don't run influx
                    if settings.vials_to_run[x] == 1:
                        MESSAGE[x+16] = str(5) ## Run efflux
                    else:
                        MESSAGE[x+16] = "--"

                elif oddata.shape[0] >= 2:
                    timein = round(pump_run_duration[x],2)
                    MESSAGE[x] = str(timein)  ## Influx
                    MESSAGE[x + 16] = "--" ## Efflux

                    with open(pumplogs[x], "a+") as outfile:
                        outfile.write(f"{elapsed_time},{timein}\n")                        
                # if ((elapsed_time - last_pump)*60) >= pump_wait: # if sufficient time since last pump, send command to Arduino
                #     logger.info('turbidostat dilution for vial %d' % x)
                #     # influx pump
                #     MESSAGE[x] = str(time_in)
                #     # efflux pump
                #     MESSAGE[x + 16] = str(time_in + time_out)

                #     file_name =  "vial{0}_pump_log.txt".format(x)
                #     file_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'pump_log', file_name)

                #     text_file = open(file_path, "a+")
                #     text_file.write("{0},{1}\n".format(elapsed_time, time_in))
                #     text_file.close()
        else:
            logger.debug('not enough OD measurements for vial %d' % x)


    if settings.stir_switch:
        stir_rate_control(eVOLVER, turbidostat_vials, settings, elapsed_time)    
    
    # send fluidic command only if we are actually turning on any of the pumps
    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)

        # your_FB_function_here() #good spot to call feedback functions for dynamic temperature, stirring, etc for ind. vials
    # your_function_here() #good spot to call non-feedback functions for dynamic temperature, stirring, etc.

    # end of turbidostat() fxn

def chemostat(eVOLVER, input_data, vials, elapsed_time):
    OD_data = input_data['transformed']['od']
    WINSIZE = 100
    bolus = 0.5 #mL, can be changed with great caution, 0.2 is absolute minimum    
    ##### USER DEFINED VARIABLES #####

    # Note that script uses AND logic, so both start time and start OD must be surpassed
    values_to_average = 6  # Number of values to calculate the OD average
    gr_values_to_average = 6   # Number of values to calculate the OD average
    
    chemostat_vials = vials #vials is all 16, can set to different range (ex. [0,1,2,3]) to only trigger tstat on those vials
    #UNITS of 1/hr, NOT mL/hr, rate = flowrate/volume, so dilution rate ~ growth rate, set to 0 for unused vials
    target_GR = list(settings.chemo_rate)
    targetcheck = 0.2                                ## Cross this growth rate first
    flow_rate = eVOLVER.get_flow_rate() #read from calibration file
    
    period_config = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0] #initialize array
    bolus_in_s = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0] #initialize array


    ##### Chemostat Control Code Below #####
    SAVE_PATH = os.path.dirname(os.path.realpath(__file__))
    EXP_DIR = os.path.join(SAVE_PATH, settings.exp_name)
    
    for x in chemostat_vials: #main loop through each vial
        # Update chemostat configuration files for each vial

        #initialize OD and find OD path
        if settings.calib_name is not None:
            file_name =  "vial{0}_OD_autocalib.txt".format(x)
            OD_path = os.path.join(EXP_DIR, 'OD_autocalib', file_name)            
        else:
            file_name =  "vial{0}_OD.txt".format(x)
            OD_path = os.path.join(EXP_DIR, 'OD', file_name)

        ## First read in the entire data set...
        ODdata_full = np.genfromtxt(OD_path, delimiter=',')
        ## ...then average the last few values
        ODdata = ODdata_full[-values_to_average:, :]

        ## Estimate growth rate
        if settings.estimate_gr:
            ODdata_forgr = ODdata_full[-WINSIZE:, 1]
            time = ODdata_full[-WINSIZE:,0]            
            eVOLVER.aj_growth_rate(x, time, ODdata_forgr)        
        ###
        
        average_OD = 0

        dataSizeSatisfied = (ODdata.size != 0 )        
        if settings.chemo_growth_rate_responsive_start:
            #initialize GR and find GR path
            file_name =  "vial{0}_growthrate_fromOD.txt".format(x)
            GR_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'growthrate_fromOD', file_name)
            GRdata = eVOLVER.tail_to_np(GR_path, gr_values_to_average)

            file_name =  "vial{0}_growthrate_status.txt".format(x)        
            GRstatus_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'growthrate_status', file_name)
            GRstatus = pd.read_csv(GRstatus_path)
            GRpass = GRstatus.iloc[-1]["GRstatus"]
            GRcheck = GRstatus.iloc[-1]["GRcheck"]

            average_GR = 0

            dataSizeSatisfied = (ODdata.size != 0 ) and (GRdata.size != 0)

        #enough_ODdata = (len(data) > 7) #logical, checks to see if enough data points (couple minutes) for sliding window

        if dataSizeSatisfied: #waits for seven OD measurements (couple minutes) for sliding window
            #calculate median OD
            od_values_from_file = ODdata[:,1]
            average_OD = float(np.median(od_values_from_file))

            # set chemostat config path and pull current state from file
            file_name =  "vial{0}_chemo_config.txt".format(x)
            chemoconfig_path = os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            'chemo_config', file_name)
            chemo_config = np.genfromtxt(chemoconfig_path, delimiter=',')
            last_chemoset = chemo_config[len(chemo_config)-1][0] #should t=0 initially, changes each time a new command is written to file
            last_chemophase = chemo_config[len(chemo_config)-1][1] #should be zero initially, changes each time a new command is written to file
            last_chemorate = chemo_config[len(chemo_config)-1][2] #should be 0 initially, then period in seconds after new commands are sent

            chemophaseSatisfied = ((elapsed_time > settings.chemo_start_time[x])\
                                   and (average_OD > settings.chemo_start_od[x]))
            
            if settings.chemo_growth_rate_responsive_start: 
                gr_values_from_file = GRdata[:,1]
                average_GR = float(np.median(gr_values_from_file))
                chemophaseSatisfied = ((elapsed_time > settings.chemo_start_time[x])\
                                       and (average_OD > settings.chemo_start_od[x]))\
                                       and (at_target_GR(x, average_GR, target_GR[x], targetcheck,
                                                         time, GRpass, GRcheck, f"vial{x}_growthrate_status.txt"))


            ## once start time has passed and culture hits start OD, 
            ## if no command has been written, write new chemostat command to file
            ## This condition is also growth rate responsive. 
            ## 1. If growth rate has already been passed, continue chemostat
            ## 2. Else, compute growth rate and store status, 0 is fail, 1 is pass 
            if chemophaseSatisfied:
                #calculate time needed to pump bolus for each pump
                bolus_in_s[x] = bolus/flow_rate[x]
                
                # calculate the period (i.e. frequency of dilution events) based on user specified growth rate and bolus size
                if settings.chemo_rate[x] > 0:
                    period_config[x] = (3600*bolus)/((settings.chemo_rate[x])*settings.volume[x]) #scale dilution rate by bolus size and volume
                else: # if no dilutions needed, then just loops with no dilutions
                    period_config[x] = 0

                if  (last_chemorate != period_config[x]):
                    print('Chemostat updated in vial {0}'.format(x))
                    logger.info('chemostat initiated for vial %d, period %.2f'
                                % (x, period_config[x]))
                    # writes command to chemo_config file, for storage
                    text_file = open(chemoconfig_path, "a+")
                    text_file.write("{0},{1},{2},1\n".format(elapsed_time,
                                                           (last_chemophase+1),
                                                           period_config[x])) #note that this changes chemophase
                    text_file.close()
        else:
            logger.debug('not enough OD measurements for vial %d' % x)

        # your_FB_function_here() #good spot to call feedback functions for dynamic temperature, stirring, etc for ind. vials
    # your_function_here() #good spot to call non-feedback functions for dynamic temperature, stirring, etc.

    eVOLVER.update_chemo(input_data, chemostat_vials, bolus_in_s, period_config) #compares computed chemostat config to the remote one
    # end of chemostat() fxn


def chemostatdual(eVOLVER, input_data, vials, elapsed_time):
    """
    NOTE Does not currently implement growth rate responsive start
    """
    OD_data = input_data['transformed']['od']
    WINSIZE = 100
    bolus = 0.5 #mL, can be changed with great caution, 0.2 is absolute minimum    
    ##### USER DEFINED VARIABLES #####

    # Note that script uses AND logic, so both start time and start OD must be surpassed
    values_to_average = 1  # Number of values to calculate the OD average
    gr_values_to_average = 3   # Number of values to calculate the OD average
    
    chemostat_vials = vials #vials is all 16, can set to different range (ex. [0,1,2,3]) to only trigger tstat on those vials
    #UNITS of 1/hr, NOT mL/hr, rate = flowrate/volume, so dilution rate ~ growth rate, set to 0 for unused vials
    
    #target_GR = list(settings.chemo_rate)
    targetcheck = 0.2                                ## Cross this growth rate first
    flow_rate = [float(v) for v in eVOLVER.get_flow_rate()] #read from calibration file
    
    period_config_1 = [0,0,0,0,
                     0,0,0,0,
                     0,0,0,0,
                     0,0,0,0] #initialize array
    period_config_2 = [0,0,0,0,
                     0,0,0,0,
                     0,0,0,0,
                     0,0,0,0] #initialize array
    bolus_in_s_1 = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0] #initialize array
    bolus_in_s_2 = [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0] #initialize array    
    


    ##### Chemostat Control Code Below #####
    SAVE_PATH = os.path.dirname(os.path.realpath(__file__))
    EXP_DIR = os.path.join(SAVE_PATH, settings.exp_name)
    
    for x in chemostat_vials: #main loop through each vial
        # Update chemostat configuration files for each vial

        #initialize OD and find OD path
        if settings.calib_name is not None:
            file_name =  "vial{0}_OD_autocalib.txt".format(x)
            OD_path = os.path.join(EXP_DIR, 'OD_autocalib', file_name)            
        else:
            file_name =  "vial{0}_OD.txt".format(x)
            OD_path = os.path.join(EXP_DIR, 'OD', file_name)

        ## First read in the entire data set...
        ODdata_full = np.genfromtxt(OD_path, delimiter=',')
        ## ...then average the last few values
        ODdata = ODdata_full[-values_to_average:, :]

        ## Estimate growth rate
        if settings.estimate_gr:
            ODdata_forgr = ODdata_full[-WINSIZE:, 1]
            time = ODdata_full[-WINSIZE:,0]            
            eVOLVER.aj_growth_rate(x, time, ODdata_forgr)        
        ###
        
        average_OD = 0

        dataSizeSatisfied = (ODdata.size != 0 )        
        if settings.chemo_growth_rate_responsive_start:
            #initialize GR and find GR path
            file_name =  "vial{0}_growthrate_fromOD.txt".format(x)
            GR_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'growthrate_fromOD', file_name)
            GRdata = eVOLVER.tail_to_np(GR_path, gr_values_to_average)

            file_name =  "vial{0}_growthrate_status.txt".format(x)        
            GRstatus_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'growthrate_status', file_name)
            GRstatus = pd.read_csv(GRstatus_path)
            GRpass = GRstatus.iloc[-1]["GRstatus"]
            GRcheck = GRstatus.iloc[-1]["GRcheck"]

            average_GR = 0

            dataSizeSatisfied = (ODdata.size != 0 ) and (GRdata.size != 0)

        #enough_ODdata = (len(data) > 7) #logical, checks to see if enough data points (couple minutes) for sliding window

        if dataSizeSatisfied: #waits for seven OD measurements (couple minutes) for sliding window
            #calculate median OD
            od_values_from_file = ODdata[:,1]
            average_OD = float(np.median(od_values_from_file))

            # set chemostat config path and pull current state from file
            file_name =  "vial{0}_chemo_config.txt".format(x)
            chemoconfig_path = os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            'chemo_config', file_name)
            
            chemo_config_df = pd.read_csv(chemoconfig_path, 
                                          names=["elapsed_time", 
                                                 "chemophase",
                                                 "period","pump"],
                                          sep=",")
            # chemo_config = np.genfromtxt(chemoconfig_path, delimiter=',')
            chemo_config = chemo_config_df[chemo_config_df.elapsed_time == chemo_config_df.elapsed_time.max()]
            # last_chemoset = chemo_config[len(chemo_config)-1][0] #should be t=0 initially, changes each time a new command is written to file
            # last_chemophase = chemo_config[len(chemo_config)-1][1] #should be zero initially, changes each time a new command is written to file
            # last_chemorate = chemo_config[len(chemo_config)-1][2] #should be 0 initially, then period 
            #in seconds after new commands are sent
            last_chemoset = chemo_config.elapsed_time.values[0]
            last_chemophase = chemo_config.chemophase.values[0]
            last_chemorate_1,last_chemorate_2  = chemo_config[chemo_config.pump==1].period.values[0],\
                chemo_config[chemo_config.pump==2].period.values[0]

            # General parameters
            chemophaseSatisfied = ((elapsed_time > settings.chemo_start_time[x])\
                                   and (average_OD > settings.chemo_start_od[x]))

            ## NOT YET SUPPORTED
            # if settings.chemo_growth_rate_responsive_start: 
            #     gr_values_from_file = GRdata[:,1]
            #     average_GR = float(np.median(gr_values_from_file))
            #     chemophaseSatisfied = ((elapsed_time > settings.chemo_start_time[x])\
            #                            and (average_OD > settings.chemo_start_od[x]))\
            #                            and (at_target_GR(x, average_GR, target_GR[x], targetcheck,
            #                                              time, GRpass, GRcheck, f"vial{x}_growthrate_status.txt"))


            ## once start time has passed and culture hits start OD, 
            ## if no command has been written, write new chemostat command to file
            ## This condition is also growth rate responsive. 
            ## 1. If growth rate has already been passed, continue chemostat
            ## 2. Else, compute growth rate and store status, 0 is fail, 1 is pass 
            if chemophaseSatisfied:
                #calculate time needed to pump bolus for each pump
                bolus_in_s_1[x] = bolus/flow_rate[x]
                bolus_in_s_2[x] = bolus/flow_rate[x + 32]
                
                # calculate the period (i.e. frequency of dilution events) based on user specified growth rate and bolus size
                if settings.chemo_rate[x] > 0:
                    period_config_1[x] = (3600*bolus)/((settings.chemo_rate[x])*settings.volume[x]) #scale dilution rate by bolus size and volume
                else: # if no dilutions needed, then just loops with no dilutions
                    period_config_1[x] = 0
                    
                if settings.chemo_rate_2[x] > 0:
                    period_config_2[x] = (3600*bolus)/((settings.chemo_rate_2[x])*settings.volume[x]) #scale dilution rate by bolus size and volume
                else: # if no dilutions needed, then just loops with no dilutions
                    period_config_2[x] = 0


                if  (int(last_chemorate_1) != int(period_config_1[x])) or (int(last_chemorate_2) != int(period_config_2[x])):
                    print('Chemostat updated in vial {0}'.format(x))
                    logger.info('chemostat initiated for vial, in1 %d, period %.2f'
                                % (x, period_config_1[x]))
                    logger.info('chemostat initiated for vial, in2 %d, period %.2f'
                                % (x, period_config_2[x]))                    
                    # writes command to chemo_config file, for storage
                    text_file = open(chemoconfig_path, "a+")
                    text_file.write("{0},{1},{2},1\n".format(elapsed_time,
                                                           (last_chemophase+1),
                                                           period_config_1[x])) #note that this changes chemophase
                    text_file.write("{0},{1},{2},2\n".format(elapsed_time,
                                                           (last_chemophase+1),
                                                           period_config_2[x])) #note that this changes chemophase
                    text_file.close()
        else:
            logger.debug('not enough OD measurements for vial %d' % x)

        # your_FB_function_here() #good spot to call feedback functions for dynamic temperature, stirring, etc for ind. vials
    # your_function_here() #good spot to call non-feedback functions for dynamic temperature, stirring, etc.

    eVOLVER.update_chemo_dual(input_data, chemostat_vials, bolus_in_s_1,
                              bolus_in_s_2, period_config_1, period_config_2) #compares computed chemostat config to the remote one
    # end of chemostat() fxn
    
# def your_function_here(): # good spot to define modular functions for dynamics or feedback
def is_higher_than_setpoint(od, setpoint):
    if np.isnan(od):
        return True
    elif od > (setpoint - 0.05):
        return True
    else:
        return False
    
def morbidostat_toprak(eVOLVER, input_data, vials, elapsed_time):
    """
    CREATED: 2024-03-29
    COMMENTARY:
    Direct implementation of logic from Toprak 2011
    """
    OD_data = input_data['transformed']['od']
    flow_rate = eVOLVER.get_flow_rate()       #read from calibration file
    MESSAGE = ['--'] * 48
    pumplogs = [os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            "pump_log", f"vial{x}_pump_log.txt")
                for x in vials]

    ## Dispense 10% of vial volume => 9% of stock stress, and 90.1% OD
    ## Dispense 5% of vial volume => 4.7% of stock stress, and 95.2% OD
    ## We don't want to balance out the growth. The dilution should be smaller than the growth at each step.
    ## Implemented: Allow for 10% increase in OD, and dilute down to 5%.
    ## if growth rate is 0.33, time for 10% increase is log2(1.1)/0.33.
    ## In this time, dilute it down to 95% of the OD: v = 0.05 v1 = 22*0.05 = 1.1mL
    # dil_interval = np.log2(1.1)/0.33
    
    volume = [0.05*vsleeve for vsleeve, vtr\
              in zip(settings.volume,
                     settings.vials_to_run)]

    pump_run_duration = [volume[x]*vtr/flow_rate[x]
                         if flow_rate[x] != ''\
                         else 0\
                         for x, vtr in zip(vials, settings.vials_to_run)]

    ## How much time to allow for mixing before running efflux
    WAITDURATION = 25./3600 
    GROWTHDELTA = 0.001
    for x in vials:
        if settings.vials_to_run[x] == 1:
            file_name =  "vial{0}_OD_autocalib.txt".format(x)
            OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'OD_autocalib', file_name)

            pump_file_name =  "vial{0}_pump_log.txt".format(x)
            pump_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'pump_log', pump_file_name)

            data =  pd.read_csv(OD_path,sep=",",)
            pumpdata =  pd.read_csv(pump_path,
                                    sep=",",
                                    names=["time","timein","pump"],
                                    skiprows=[0])
            pumpdata.loc[pumpdata.pump.isna(), "pump"] = ""
            """
            If it has been DURATION minutes since the last pump event, run the efflux pump.
            At the start of the experiment, 
            """
            
            if (elapsed_time - pumpdata.time.tail(1).values[0]) < WAITDURATION:
                MESSAGE[x+16] = str(round(pump_run_duration[x]+4, 2))
            sensor = 135
            data["OD"] = data[f"od_plinear_{sensor}"]

            # initialize
            #lastpumptime = settings.interval[x]
            lastpumptime = 0
            if pumpdata.shape[0] > 1:
                lastpumptime = pumpdata.tail(1).time.values[0]
            # Current OD
            # taildf = data[data.time > (elapsed_time - lastpumptime)]
            taildf = data[data.time > (lastpumptime)]
            lastODvals = np.nanmedian(taildf.OD.tail(10).values)# .nanmedian()
            # print(x, lastODvals, taildf.OD.tail(10).values)            
            # Before the previous dilution
            taildf = data[data.time <  lastpumptime]            
            firstODvals = np.nanmedian(taildf.OD.tail(10).values)# .nanmedian()
            if np.isnan(firstODvals ):
                firstODvals = 0
            """
            First check if the OD vals are within the calibration range.
            """
            if not np.isnan(lastODvals):
                """
                If the time condition is satisfied....
                """
                if (elapsed_time - lastpumptime) > settings.interval[x]:
                    #print("exceeded dilution interval")                    
                    # Did we grow since more than the start of the previous dilution?
                    deltaOD = lastODvals - firstODvals
                    file_name =  f"vial{x}_od_{sensor}_raw.txt"
                    ODpath = os.path.join(eVOLVER.exp_dir, settings.exp_name, f'od_{sensor}_raw', file_name)        
                    sensordata = pd.read_csv(ODpath,
                                             sep=",",names=["elapsed_time","od"],
                                             skiprows=[0])
                    """
                    ... and there has been net growth AND a threshold crossing, then add stress.
                    """

                    if ((lastODvals > settings.setpoint[x]) and (deltaOD > GROWTHDELTA)):
                        ### Run stress pump - in2
                        MESSAGE[x + 32] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)
                        # print(x, deltaOD)
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in2\n")                        
                            # elif (deltaOD > GROWTHDELTA):
                    else:
                            
                        """
                        ... else dilute with media
                        """                        
                        MESSAGE[x] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)                        
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in1\n")
            else:
                """
                Keep diluting
                """
                file_name =  f"vial{x}_od_{sensor}_raw.txt"
                OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, f'od_{sensor}_raw', file_name)        
                sensordata = pd.read_csv(OD_path,
                                         sep=",",names=["elapsed_time","od"],
                                        skiprows=[0])

                calibration = pd.read_csv(os.path.join(eVOLVER.exp_dir,\
                                                       f"{settings.calib_name}.csv"))
                beyond_calibration_range = (float(np.median(sensordata.od.tail(10))) < calibration[(calibration.vial == x) & (calibration.sensor == sensor)].reading.min())
                below_low_calibration = (float(np.median(sensordata.od.tail(10))) > calibration[(calibration.vial == x) & (calibration.sensor == sensor)].reading.max())                
                #beyond_upper_setpoint = is_higher_than_setpoint(lastODvals, setpoint)
                #if beyond_upper_setpoint and (x in [0,1,2]):
                print(f"Vial {x}: beyond upper calib: {beyond_calibration_range}, below lower calib: {below_low_calibration}")

                """
                keep diluting with media, every pump event, until the OD is back in range.
                """
                if beyond_calibration_range:
                    if (elapsed_time - lastpumptime) > 50./3600.:
                        ### Run media pump - in1
                        MESSAGE[x + 32] = str(round(pump_run_duration[x], 2))
                        MESSAGE[x + 16] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)                        
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in2\n")                                        
                    # MESSAGE[x] = str(round(pump_run_duration[x], 2))

                    # timein = round(pump_run_duration[x],2)                        
                    # with open(pumplogs[x], "a+") as outfile:
                    #     outfile.write(f"{elapsed_time},{timein},in2\n")
                if below_low_calibration:
                    """
                    if very low OD, dilute at regular intervals, giving the vial enough time for growth/recovery
                    """
                    if (elapsed_time - lastpumptime) > settings.interval[x]:
                        ### Run media pump - in1
                        MESSAGE[x] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)                        
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in1\n")                    

    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)

def morbidostat_dualpumprack(eVOLVER, input_data, vials, elapsed_time):
    """
    CREATED: 2024-07-31
    COMMENTARY:
    Modification of Toprak et al to ignore OD perturbations. This is in reponse to salinity dependent OD jumps.
    """
    OD_data = input_data['transformed']['od']
    flow_rate = eVOLVER.get_flow_rate()       #read from calibration file
    MESSAGE = ['--'] * 48
    pumplogs = [os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            "pump_log", f"vial{x}_pump_log.txt")
                for x in vials]

    ## Dispense 10% of vial volume => 9% of stock stress, and 90.1% OD
    ## We don't want to balance out the growth. The dilution should be smaller than the growth at each step.
    ## Implemented: Allow for 10% increase in OD, and dilute down to 5%.
    ## if growth rate is 0.33, time for 10% increase is log2(1.1)/0.33.
    ## In this time, dilute it down to 95% of the OD: v = 0.05 v1 = 22*0.05 = 1.1mL
    # dil_interval = np.log2(1.1)/0.33
    
    volume = [0.05*vsleeve for vsleeve, vtr\
              in zip(settings.volume,
                     settings.vials_to_run)]

    pump_run_duration = [volume[x]*vtr/flow_rate[x]
                         if flow_rate[x] != ''\
                         else 0\
                         for x, vtr in zip(vials, settings.vials_to_run)]

    ## How much time to allow for mixing before running efflux
    WAITDURATION = 25./3600 
    GROWTHDELTA = 1e-4
    for x in vials:
        if settings.vials_to_run[x] == 1:
            file_name =  "vial{0}_OD_autocalib.txt".format(x)
            OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'OD_autocalib', file_name)

            pump_file_name =  "vial{0}_pump_log.txt".format(x)
            pump_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'pump_log', pump_file_name)

            data =  pd.read_csv(OD_path,sep=",",)
            pumpdata =  pd.read_csv(pump_path,
                                    sep=",",
                                    names=["time","timein","pump"],
                                    skiprows=[0])
            pumpdata.loc[pumpdata.pump.isna(), "pump"] = ""
            """
            If it has been DURATION minutes since the last pump event, run the efflux pump.
            At the start of the experiment, 
            """
            
            if (elapsed_time - pumpdata.time.tail(1).values[0]) < WAITDURATION:
                MESSAGE[x+16] = str(round(pump_run_duration[x]+4, 2))
            sensor = 135
            data["OD"] = data[f"od_plinear_{sensor}"]


            ############################################################
            ##### Absolute OD based computatio, from TOPRAK
            # initialize
            lastpumptime = 0
            if pumpdata.shape[0] > 1:
                lastpumptime = pumpdata.tail(1).time.values[0]
            # Current OD
            taildf = data[data.time > (lastpumptime)]
            lastODvals = np.nanmedian(taildf.OD.tail(10).values)
            # print(x, lastODvals, taildf.OD.tail(10).values)            
            # Before the previous dilution
            taildf = data[data.time <  lastpumptime]            
            firstODvals = np.nanmedian(taildf.OD.tail(10).values)
            ############################################################
            ##### COMMENTARY
            ## Two problems with the absolute OD logic
            ## 1. It fails in the salt case because of the salt spike dependent OD increase
            ## 2. It is meant to the dilution has been perfect.
            ## [1] above is definitely the important problem to solve, but I hadn't realized
            ## that [2] is a problem as well until I took a look at the data.
            ## There are instances where there is _larger_ net growth in a time window, but the
            ## lower absolute OD obscures this.

            # initialize
            pumptime = 0
            prevpumptime = 0
            OFFSET_OBS = 30 ## 10 minutes            
            if pumpdata.shape[0] > 1:
                """
                Compute if there has been net growth since the previous window
                """
                pumptime = pumpdata.tail(1).time.values[0]
                prevpumptime = pumpdata.tail(2).time.values[0]
                thiswindow = data[data.time > pumptime]
                prevwindow = data[(data.time < pumptime) & (data.time > prevpumptime)]
                if prevwindow.shape[0] > 0:
                    netgrowth_prevwindow = prevwindow.tail(20).OD.median() - prevwindow.head(OFFSET_OBS).tail(20).OD.median()
                else:
                    netgrowth_prevwindow = 0

                netgrowth_thiswindow = thiswindow.tail(20).OD.median() - thiswindow.head(OFFSET_OBS).tail(20).OD.median() 
                deltaGrowth = netgrowth_thiswindow - netgrowth_prewindow
            else:
                """
                Initially, deltagrowth is initialized to 0.
                """
                deltaGrowth = 0
                
            ############################################################
            
            if np.isnan(firstODvals ):
                firstODvals = 0
            """
            First check if the OD vals are within the calibration range.
            """
            if not np.isnan(lastODvals):
                """
                If the time condition is satisfied....
                """
                if (elapsed_time - lastpumptime) > settings.interval[x]:

                    deltaOD = lastODvals - firstODvals
                    file_name =  f"vial{x}_od_{sensor}_raw.txt"
                    ODpath = os.path.join(eVOLVER.exp_dir, settings.exp_name, f'od_{sensor}_raw', file_name)        
                    sensordata = pd.read_csv(ODpath,
                                             sep=",",names=["elapsed_time","od"],
                                             skiprows=[0])
                    """
                    ... and there has been net growth AND a threshold crossing, then add stress.
                    """

                    if ((lastODvals > settings.setpoint[x]) and (deltaGrowth > GROWTHDELTA)):
                        ### Run stress pump - in2
                        MESSAGE[x + 32] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in2\n")                        
                    else:
                            
                        """
                        ... else dilute with media
                        """                        
                        MESSAGE[x] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)                        
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in1\n")
            else:
                """
                Keep diluting
                """
                file_name =  f"vial{x}_od_{sensor}_raw.txt"
                OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, f'od_{sensor}_raw', file_name)        
                sensordata = pd.read_csv(OD_path,
                                         sep=",",names=["elapsed_time","od"],
                                        skiprows=[0])

                calibration = pd.read_csv(os.path.join(eVOLVER.exp_dir,\
                                                       f"{settings.calib_name}.csv"))
                beyond_calibration_range = (float(np.median(sensordata.od.tail(10))) < calibration[(calibration.vial == x) & (calibration.sensor == sensor)].reading.min())
                below_low_calibration = (float(np.median(sensordata.od.tail(10))) > calibration[(calibration.vial == x) & (calibration.sensor == sensor)].reading.max())                
                #beyond_upper_setpoint = is_higher_than_setpoint(lastODvals, setpoint)
                #if beyond_upper_setpoint and (x in [0,1,2]):
                print(f"Vial {x}: beyond upper calib: {beyond_calibration_range}, below lower calib: {below_low_calibration}")

                """
                keep diluting with media, every pump event, until the OD is back in range.
                """
                if beyond_calibration_range:
                    if (elapsed_time - lastpumptime) > 50./3600.:
                        ### Run media pump - in1
                        MESSAGE[x + 32] = str(round(pump_run_duration[x], 2))
                        MESSAGE[x + 16] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)                        
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in2\n")                                        
                    # MESSAGE[x] = str(round(pump_run_duration[x], 2))

                    # timein = round(pump_run_duration[x],2)                        
                    # with open(pumplogs[x], "a+") as outfile:
                    #     outfile.write(f"{elapsed_time},{timein},in2\n")
                if below_low_calibration:
                    """
                    if very low OD, dilute at regular intervals, giving the vial enough time for growth/recovery
                    """
                    if (elapsed_time - lastpumptime) > settings.interval[x]:
                        ### Run media pump - in1
                        MESSAGE[x] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)                        
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in1\n")                    

    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)


def morbidostat(eVOLVER, input_data, vials, elapsed_time):
    """
    CREATED: 2026-05-19
    COMMENTARY:
    - Generalized code that allows user to specify the pump index to use as input 2 on the same pump array.
    """
    OD_data = input_data['transformed']['od']
    flow_rate = eVOLVER.get_flow_rate()       #read from calibration file
    MESSAGE = ['--'] * 48
    pumplogs = [os.path.join(eVOLVER.exp_dir, settings.exp_name,
                                            "pump_log", f"vial{x}_pump_log.txt")
                for x in vials]

    ## Dispense 10% of vial volume => 9% of stock stress, and 90.1% OD
    ## We don't want to balance out the growth. The dilution should be smaller than the growth at each step.
    ## Implemented: Allow for 10% increase in OD, and dilute down to 5%.
    ## if growth rate is 0.33, time for 10% increase is log2(1.1)/0.33.
    ## In this time, dilute it down to 95% of the OD: v = 0.05 v1 = 22*0.05 = 1.1mL
    # dil_interval = np.log2(1.1)/0.33
    
    volume = [0.05*vsleeve for vsleeve, vtr\
              in zip(settings.volume,
                     settings.vials_to_run)]

    pump_run_duration = [volume[x]*vtr/flow_rate[x]
                         if flow_rate[x] != ''\
                         else 0\
                         for x, vtr in zip(vials, settings.vials_to_run)]

    inputpump2_index = {vial:pumpid for  pumpid, vial in\
                        zip(settings.input_pump2,
                            vials)}

    ## How much time to allow for mixing before running efflux
    WAITDURATION = 25./3600 
    GROWTHDELTA = 1e-4
    for x in vials:
        if settings.vials_to_run[x] == 1:
            file_name =  "vial{0}_OD_autocalib.txt".format(x)
            OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'OD_autocalib', file_name)

            pump_file_name =  "vial{0}_pump_log.txt".format(x)
            pump_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'pump_log', pump_file_name)

            data =  pd.read_csv(OD_path,sep=",",)
            pumpdata =  pd.read_csv(pump_path,
                                    sep=",",
                                    names=["time","timein","pump"],
                                    skiprows=[0])
            pumpdata.loc[pumpdata.pump.isna(), "pump"] = ""
            """
            If it has been DURATION minutes since the last pump event, run the efflux pump.
            At the start of the experiment, 
            """
            
            if (elapsed_time - pumpdata.time.tail(1).values[0]) < WAITDURATION:
                MESSAGE[x+16] = str(round(pump_run_duration[x]+4, 2))
            sensor = 135
            data["OD"] = data[f"od_plinear_{sensor}"]


            ############################################################
            ##### Absolute OD based computatio, from TOPRAK
            # initialize
            lastpumptime = 0
            if pumpdata.shape[0] > 1:
                lastpumptime = pumpdata.tail(1).time.values[0]

            ## Do this calculation twice, first on OD_autocalib, next on raw sensor data.
            ## We use the second deltaod computation if the values are outside the calibration range.
            ## 
            # Current OD
            NUM_TO_AVERAGE = 5
            current_growth  = data[data.time > lastpumptime]
            previous_growth = data[data.time <  lastpumptime]            

            prevODvals = np.nanmedian(previous_growth.OD.tail(NUM_TO_AVERAGE).values)
            currODvals = np.nanmedian(current_growth.OD.tail(NUM_TO_AVERAGE).values)

            if np.isnan(currODvals ):
                currODvals = 0
            ## Also do this computation for sensor data
            file_name =  f"vial{x}_od_{sensor}_raw.txt"
            ODpath = os.path.join(eVOLVER.exp_dir, settings.exp_name, f'od_{sensor}_raw', file_name)        
            sensordata = pd.read_csv(ODpath,
                                     sep=",",
                                     names=["elapsed_time",f"od_{sensor}_raw"],
                                     skiprows=[0])
            current_growth_sensor  = sensordata[sensordata.elapsed_time > lastpumptime]
            previous_growth_sensor = sensordata[sensordata.elapsed_time <  lastpumptime]            

            prevSensorvals = np.nanmedian(previous_growth_sensor[f"od_{sensor}_raw"].tail(NUM_TO_AVERAGE).values)
            currSensorvals = np.nanmedian(current_growth_sensor[f"od_{sensor}_raw"].tail(NUM_TO_AVERAGE).values)

            """
            First check if the OD vals are within the calibration range.
            """
            if not np.isnan(currODvals):
                """
                If the time condition is satisfied....
                """
                if (elapsed_time - lastpumptime) > settings.interval[x]:
                    deltaOD = currODvals - prevODvals
                    """
                    ... and there has been net growth AND a threshold crossing, then add stress.
                    """
                    if ((currODvals > settings.setpoint[x]) and (deltaOD > GROWTHDELTA)):
                        ### Run stress pump - in2
                        MESSAGE[inputpump2_index[x]] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in2\n")                        
                    else:
                        """
                        ... else dilute with media
                        """                        
                        MESSAGE[x] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)                        
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in1\n")
            else:
                """
                Try computing based on raw sensor values
                Keep diluting
                """
                # file_name =  f"vial{x}_od_{sensor}_raw.txt"
                # OD_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, f'od_{sensor}_raw', file_name)        
                # sensordata = pd.read_csv(OD_path,
                #                          sep=",",names=["elapsed_time","od"],
                #                         skiprows=[0])

                calibration = pd.read_csv(os.path.join(eVOLVER.exp_dir,\
                                                       f"{settings.calib_name}.csv"))
                beyond_calibration_range = (float(np.median(sensordata.od.tail(10))) < calibration[(calibration.vial == x) & (calibration.sensor == sensor)].reading.min())
                below_low_calibration = (float(np.median(sensordata.od.tail(10))) > calibration[(calibration.vial == x) & (calibration.sensor == sensor)].reading.max())                
                print(f"Vial {x}: beyond upper calib: {beyond_calibration_range}, below lower calib: {below_low_calibration}")
                """
                We don't know how much growth there has been.
                Continue dispensing only stress every _interval_.
                This makes sure the ramp isn't too extreme, and eventually the culture
                will come back into range.
                """
                if beyond_calibration_range:
                    # hack, run efflux once after a pump event, a minute later.
                    if ((elapsed_time - lastpumptime) > 50./3600.) and ((elapsed_time - lastpumptime) < 70./3600.):
                        ### Run efflux pump some time after the dispense.
                        MESSAGE[x + 16] = str(round(pump_run_duration[x], 2) + 4)
                    if (elapsed_time - lastpumptime) > settings.interval[x]:
                        # NOTE: od135 is generally non-monotonic wrt OD.
                        # This logic is absolutely unreliable generally!!!!
                        # The logic here is based on the OD range used with E. coli where the 
                        # od_135 happened to be monotonic over the relevant range.
                        # The 'delta' logic is also flipped:
                        ## Higher signal == lower density == add media
                        ## Lower signal == higher density == add stress.
                        deltaSensor = currSensorvals - prevSensorvals
                        ### Run stress pump - in2
                        if deltaSensor < 0:
                            MESSAGE[inputpump2_index[x]] = str(round(pump_run_duration[x], 2))
                            timein = round(pump_run_duration[x],2)                        
                            with open(pumplogs[x], "a+") as outfile:
                                outfile.write(f"{elapsed_time},{timein},in2\n")
                        else:
                            """
                            ... else dilute with media
                            """                        
                            MESSAGE[x] = str(round(pump_run_duration[x], 2))
                            timein = round(pump_run_duration[x],2)                        
                            with open(pumplogs[x], "a+") as outfile:
                                outfile.write(f"{elapsed_time},{timein},in1\n")
                if below_low_calibration:
                    """
                    if very low OD, dilute at regular intervals, giving the vial enough time for growth/recovery.
                    Note, this is always true because we have hopefully set the morbidostat setpoint /in/ the calibration range
                    and not below it.
                    """
                    if (elapsed_time - lastpumptime) > settings.interval[x]:
                        ### Run media pump - in1
                        MESSAGE[x] = str(round(pump_run_duration[x], 2))
                        timein = round(pump_run_duration[x],2)                        
                        with open(pumplogs[x], "a+") as outfile:
                            outfile.write(f"{elapsed_time},{timein},in1\n")                    
    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)


def pumpcontrol_ramp_old(eVOLVER, input_data, vials, elapsed_time):
    """
    Turbidostat logic with ramp on pump action.
    Independent of actual OD values
    User defined parameters:
    1. Sensor value at which to dilute
    2. Number of consecutive intervals
    3. Max time for each interval.

    Logic:
    0. Initialize:
       =setting= 
       volume_low_conc = 5
       volume_high_conc = 0
       number_dispenses_at_current_setting = 0
    1. If Sensor values above setpoint, 
       Dispense total of 5mL, wait 6 minutes to run efflux to bring down the concentration by 18%.
       5mL is made of running both input pumps simultaneously.
       if( elapsed_time - lastpumptime ) < max_time_for_interval:
           increment =number_dispenses_at_current_setting=
       Duration of each pump to be run is determined in step 3..
    2. if =number_dispenses_at_current_setting= ==  =number_consecutive_intervals= intervals:
       1. number_dispenses_at_current_setting = 0. 
       if volume_low_conc > 0:
       2. decrease volume_low_conc by 0.5, increase volume_high_conc by 0.5.
    3. Configure pump durations based on pumpcal and previous step, send fluidics MESSAGE.
    """
    inputpump2_index = {vial:pumpid for pumpid, vial in\
                        zip(settings.input_pump2,
                            vials)}
    ## Initialize the volumes that we want the pumps to dispense.

    WAITDURATION = 360./3600
    flow_rate = eVOLVER.get_flow_rate()       #read from calibration file
    NUM_TO_AVERAGE = 10
    MESSAGE = ['--'] * 48
    for x in vials:
        if settings.vials_to_run[x] == 1:
            lastpumptime = 0
            pump_file_name =  "vial{0}_pump_log.txt".format(x)
            pump_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'pump_log', pump_file_name)
            pumpdata =  pd.read_csv(pump_path,
                                    sep=",",
                                    names=["time","timein","pump"],
                                    skiprows=[0])
            pumpdata.loc[pumpdata.pump.isna(), "pump"] = ""            
            if pumpdata.shape[0] > 1:
                lastpumptime = pumpdata.tail(1).time.values[0]
            """
            Initialize pump settings
            """
            if lastpumptime == 0:
                volume_low_conc = 5.0
                volume_selection = 0.0
                latest_low_conc_setting = volume_low_conc/float(flow_rate[x])
                latest_high_conc_setting = volume_selection/float(flow_rate[inputpump2_index[x]])
                with open(pump_path, "a+") as outfile:
                    outfile.write(f"{elapsed_time},{latest_low_conc_setting},in1\n")                                    
                    outfile.write(f"{elapsed_time},{latest_high_conc_setting},in2\n")                                    
            sensor = 135
            file_name =  f"vial{x}_od_{sensor}_raw.txt"
            ODpath = os.path.join(eVOLVER.exp_dir, settings.exp_name, f'od_{sensor}_raw', file_name)        
            sensordata = pd.read_csv(ODpath,
                                     sep=",",
                                     names=["elapsed_time",f"od_{sensor}_raw"],
                                     skiprows=[0])
            current_growth_sensor  = sensordata[sensordata.elapsed_time > lastpumptime]
            
            #previous_growth_sensor = sensordata[sensordata.elapsed_time <  lastpumptime]            
            #prevSensorvals = np.nanmedian(previous_growth_sensor[f"od_{sensor}_raw"].tail(NUM_TO_AVERAGE).values)
            currSensorvals = np.nanmedian(current_growth_sensor[f"od_{sensor}_raw"].tail(NUM_TO_AVERAGE).values)

            """
            If its been WAITDURATION (6 minutes) since last pump dispense, run the efflux.
            """
            if ((elapsed_time - lastpumptime ) > 60./3600.)\
               and ((elapsed_time - lastpumptime ) < (85./3600.)):
                MESSAGE[x + 16] = str(round(10, 2))
            if (currSensorvals <= settings.setpoint[x]):
                ### Figure out how much media we were adding at the most recent dispense
                if lastpumptime != 0:
                    latest_low_conc_setting = pumpdata[(pumpdata.time == pumpdata.time.max())\
                                                       & (pumpdata.pump == "in1")].timein.values[0]
                    latest_high_conc_setting = pumpdata[(pumpdata.time == pumpdata.time.max())\
                                                       & (pumpdata.pump == "in2")].timein.values[0]

                current_volume_low_conc = flow_rate[x]*latest_low_conc_setting
                current_volume_high_conc = flow_rate[inputpump2_index[x]]*latest_high_conc_setting

                ## Set default new message
                new_volume_low_conc = current_volume_low_conc
                new_volume_high_conc = current_volume_high_conc

                ### And see how many times we've done that.
                pumpdata_current = pumpdata[(pumpdata.timein == latest_low_conc_setting)\
                                            & (pumpdata.pump == "in1")]
                pumpdata_current = pumpdata_current.assign(prev_dispense_time = pumpdata_current.time.shift(1))
                pumpdata_current["growth_time"] = pumpdata_current.time - pumpdata_current.prev_dispense_time
                print(x, pumpdata_current.tail(20))
                number_dispensed_so_far\
                    = pumpdata_current[(pumpdata_current.growth_time <= settings.interval[x])]\
                                       .shape[0]
                print(f"Dispensed {number_dispensed_so_far}/{settings.number_consecutive_intervals[x]}")
                if number_dispensed_so_far >= settings.number_consecutive_intervals[x]:
                    
                    """
                    Increment time for high_conc,
                    decrement time for low_conc
                    """
                    print(x, current_volume_low_conc)
                    if (current_volume_low_conc - 0.5) >= -0.25:
                        new_volume_low_conc = max(current_volume_low_conc - 0.5,0.)
                        new_volume_high_conc = max(current_volume_high_conc + 0.5,0)

                new_low_conc_pumptime = new_volume_low_conc/flow_rate[x]
                new_high_conc_pumptime = new_volume_high_conc/flow_rate[inputpump2_index[x]]

                if (elapsed_time - lastpumptime) > (210./3600):
                    MESSAGE[x] = str(round(new_low_conc_pumptime,2))
                    MESSAGE[inputpump2_index[x]] = str(round(new_high_conc_pumptime,2))

                    with open(pump_path, "a+") as outfile:
                        outfile.write(f"{round(elapsed_time,3)},{round(new_low_conc_pumptime,2)},in1\n")                                    
                        outfile.write(f"{round(elapsed_time,3)},{round(new_high_conc_pumptime,2)},in2\n")                                    
                
    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)


def pumpcontrol_ramp(eVOLVER, input_data, vials, elapsed_time):
    """
    Turbidostat logic with ramp on pump action with history of concentration
    Features: 
    1. Independent of actual OD values, relies only on raw sensor values
    2. Uses a combination of two volume dispense sequences to achieve fine control on a per vial basis
    3. New configurations can be flexibly defined from the configuration

    User defined parameters:
    1. Sensor value at which to dilute.
    2. Number of consecutive intervals.
    3. Initial concentration of stress in vial
    4. Concentration of High media
    5. Max time for each interval.
    6. Ramp of stress 
    
    If 
        V_vial = 22mL
        minimum dispense of 0.5mL
        maximum dispense of 8mL
    Logic:
    1. If Sensor values above setpoint, 
       calculate number of dispenses at current drug concentration.
       if number_dispenses < number_consecutive_intervals: 
           ramp = 0 ## Stay at current concentration
       else:
           ramp = settings.target_ramp[x] ## Increase
       Calculate vl1 and vl2. vh1 and vh2 will be (5-vl1) and (5-vl2) respectively.
       If the input pump has not gone off in the last 1 minute,
           Send fluidics message with Step 1 times.
       Else
           Send fluidics message with Step 2 times.
           Also, update the latest drug concentration.
       5mL is made of running both input pumps simultaneously.
       if( elapsed_time - lastpumptime ) < max_time_for_interval:
           increment =number_dispenses_at_current_setting=
       Duration of each pump to be run is determined in step 3..
    3. Configure pump durations based on pumpcal and previous step, send fluidics MESSAGE.
    """
    ## Live config: re-read experiment_parameters.yaml and apply any changed
    ## live-reloadable field BEFORE anything in this cycle reads settings, so every vial
    ## in one cycle acts on the same snapshot. Never raises; on any problem the settings
    ## already in force stay in place.
    refresh_live_settings(settings, elapsed_time,
                          config_changes_path=os.path.join(eVOLVER.exp_dir,
                                                           settings.exp_name,
                                                           "config_changes.txt"))

    inputpump2_index = {vial:pumpid for pumpid, vial in\
                        zip(settings.input_pump2,
                            vials)}

    WAITDURATION = 360./3600
    flow_rate = eVOLVER.get_flow_rate()       #read from calibration file
    MAXDISPENSE = 5
    NUM_TO_AVERAGE = 10
    MESSAGE = ['--'] * 48
    for x in vials:
        if settings.vials_to_run[x] == 1:
            lastpumptime = 0
            pump_file_name =  "vial{0}_pump_log.txt".format(x)
            pump_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'pump_log', pump_file_name)
            pumpdata =  pd.read_csv(pump_path,
                                    sep=",",
                                    names=["time","timein","pump"],
                                    skiprows=[0])
            pumpdata.loc[pumpdata.pump.isna(), "pump"] = ""            
            if pumpdata.shape[0] > 1:
                lastpumptime = pumpdata.tail(1).time.values[0]

            drugconc_file_name =  "vial{0}_drugconc.txt".format(x)
            drugconc_path = os.path.join(eVOLVER.exp_dir, settings.exp_name, 'drugconc', drugconc_file_name)
            if not os.path.exists(os.path.join(eVOLVER.exp_dir, settings.exp_name, 'drugconc')):
                os.makedirs(os.path.join(eVOLVER.exp_dir, settings.exp_name, 'drugconc'))

            """
            Initialize pump settings and concentration history
            """
            if (lastpumptime == 0) and not os.path.exists(drugconc_path):
                drugconcentration = settings.initial_concentration[x]
                with open(drugconc_path, "a+") as outfile:
                    outfile.write(f"time,concentration\n")
                    outfile.write(f"{round(elapsed_time,4)},{round(drugconcentration,2)}\n")
            drugconc_data =  pd.read_csv(drugconc_path,
                                         sep=",")
            lastdrugtime = drugconc_data.tail(1).time.values[0]
            sensor = 135
            file_name =  f"vial{x}_od_{sensor}_raw.txt"
            ODpath = os.path.join(eVOLVER.exp_dir, settings.exp_name, f'od_{sensor}_raw', file_name)        
            sensordata = pd.read_csv(ODpath,
                                     sep=",",
                                     names=["elapsed_time",f"od_{sensor}_raw"],
                                     skiprows=[0])

            ### We care about growth since the last drug change.
            current_growth_sensor  = sensordata[sensordata.elapsed_time > lastdrugtime]
            currSensorvals = np.nanmedian(current_growth_sensor[f"od_{sensor}_raw"].tail(NUM_TO_AVERAGE).values)

            """
            If its been 1 period since last pump dispense, run the efflux.
            """
            if ((elapsed_time - lastpumptime ) > 17./3600.)\
               and ((elapsed_time - lastpumptime ) < (26./3600.)):
                MESSAGE[x + 16] = str(round(10, 2))

            """
            Run the effluc pump again 6 minutes after the last pump event for good measure.
            """
            if ((elapsed_time - lastpumptime ) > 6.*60./3600.)\
               and ((elapsed_time - lastpumptime ) < ((25 + 6.*60.)/3600.)):
                MESSAGE[x + 16] = str(round(10, 2))

            all_drug_data = drugconc_data.copy()
            all_drug_data = all_drug_data.assign(prev_dispense_time = all_drug_data.time.shift(1))
            all_drug_data["growth_period"] = all_drug_data.time - all_drug_data.prev_dispense_time            

            
            current_growth_period = np.nan
            previous_growth_period = np.nan
            if len(all_drug_data.growth_period.values) >=2:
                current_growth_period = elapsed_time - lastdrugtime #all_drug_data.growth_period.values[-1]
                previous_growth_period = all_drug_data.growth_period.values[-1]                                
                # current_growth_period = all_drug_data.growth_period.values[-1]
                # previous_growth_period = all_drug_data.growth_period.values[-2]                
            
            ### Figure out how much media we were adding at the most recent dispense
            last_seen_concentration = drugconc_data[drugconc_data.time == drugconc_data.time.max()]\
                .concentration.values[0]

            ## A NaN here used to pass straight through find_optimal_pump_volumes and come
            ## back as (maxdispense, 0) -- a full high-concentration dispense -- because
            ## every NaN comparison is False. Refuse to dose this vial instead. The efflux
            ## message set above still stands.
            if not np.isfinite(last_seen_concentration):
                print(f"V{x} SKIPPED: last recorded concentration is "
                      f"{last_seen_concentration!r} in {drugconc_path}. "
                      f"Not dosing until this is corrected.")
                continue
            
            drugconc_data = drugconc_data.tail(settings.number_consecutive_intervals[x] + 5)
            ## How many times have we seen the current drug concentration

            drugconc_data = drugconc_data[(drugconc_data.concentration == last_seen_concentration)]
            drugconc_data = drugconc_data.assign(prev_dispense_time = drugconc_data.time.shift(1))
            drugconc_data["growth_time"] = drugconc_data.time - drugconc_data.prev_dispense_time
            number_dispensed_so_far\
                = drugconc_data[(drugconc_data.growth_time <= settings.interval[x])]\
                .shape[0]
            ### Should we ramp up or hold?
            if ( np.isnan(current_growth_period))\
               or (np.isnan(previous_growth_period)):
                target_ramp = 0
            else:
                ramp_predicate = (number_dispensed_so_far >= settings.number_consecutive_intervals[x])\
                    and (current_growth_period < settings.interval[x]) and (current_growth_period < (previous_growth_period*(1./0.93)))
                if ramp_predicate:
                    target_ramp = settings.target_ramp[x]
                    # if last_seen_concentration > 4.:
                    #     target_ramp = 0.15
                    # if last_seen_concentration > 4.0:
                    #    target_ramp = 0.1
                    # if last_seen_concentration > 6:
                    #     target_ramp = 0.05
                else:
                    target_ramp = 0

            ## Compute the next pump volumes. These are not executed yet, only computed.
            ## A config with ch <= cl, or any non-finite argument, now raises rather than
            ## returning plausible volumes. Skip this vial rather than taking the whole rig
            ## down: 15 other cultures are running.
            try:
                volume_low_1, volume_low_2, achieved_delta = find_optimal_pump_volumes(
                    last_seen_concentration,
                    settings.low_concentration[x],
                    settings.high_concentration[x],
                    target_ramp,
                    maxdispense=MAXDISPENSE,
                    vialvolume=settings.volume[x],
                    return_delta=True)
            except ValueError as exc:
                print(f"V{x} SKIPPED: {exc}")
                continue

            ## Say so when the hardware cannot deliver what was asked. Near the ceiling the
            ## best available step goes negative, and nothing previously compared the
            ## requested ramp against the step actually achieved.
            if abs(achieved_delta - target_ramp) > RAMP_TOL:
                print(f"V{x} NOTE requested ramp {target_ramp:+.4f} but the best available "
                      f"step is {achieved_delta:+.4f} g/L "
                      f"(c1={last_seen_concentration:.3f}, "
                      f"low={settings.low_concentration[x]}, high={settings.high_concentration[x]})")
            if (currSensorvals <= settings.setpoint[x]):
                print(f"V{x} -- [{number_dispensed_so_far}/{settings.number_consecutive_intervals[x]}] Target ramp = {target_ramp}  (curr:{round(current_growth_period,3)}; prev: {round(previous_growth_period,3)})")
                ## Check if the most recent drug step was 3.5 minutes ago, allow for 10 measurements and dilution.
                if ((elapsed_time - lastdrugtime) > (60.*3.5/3600.)):
                    ## We should have seen a single pump dispense since the last drug time
                    if (pumpdata[(pumpdata.time > lastdrugtime) & (pumpdata.pump == "in1")].shape[0] == 1):
                        if ((elapsed_time - lastpumptime) > 30./3600.):
                            print("\t",x,"Step2")
                            new_low_conc_pumptime = volume_low_2/flow_rate[x]
                            new_high_conc_pumptime = (MAXDISPENSE-volume_low_2)/flow_rate[inputpump2_index[x]]
                            # compute current drug concentration:
                            current_drug_concentration = last_seen_concentration + stepsize(last_seen_concentration,
                                                                                            settings.low_concentration[x],
                                                                                            settings.high_concentration[x],
                                                                                            volume_low_1,
                                                                                            volume_low_2,
                                                                                            MAXDISPENSE)
                            append_drug_concentration(drugconc_path, elapsed_time,
                                                      current_drug_concentration)
                            MESSAGE[x] = str(round(new_low_conc_pumptime,2))
                            MESSAGE[inputpump2_index[x]] = str(round(new_high_conc_pumptime,2))
                            MESSAGE[x+16] = "--" ### Do not run efflux when running influx
                            with open(pump_path, "a+") as outfile:
                                outfile.write(f"{round(elapsed_time,4)},{round(new_low_conc_pumptime,2)},in1\n")
                                outfile.write(f"{round(elapsed_time,4)},{round(new_high_conc_pumptime,2)},in2\n")
                        # else: Wait one more period
                    if (pumpdata[(pumpdata.time > lastdrugtime) & (pumpdata.pump == "in1")].shape[0] == 0 ):
                        ## else, do Step 1.
                        print("\t",x,"Step1")                        
                        new_low_conc_pumptime = volume_low_1/flow_rate[x]
                        new_high_conc_pumptime = (MAXDISPENSE-volume_low_1)/flow_rate[inputpump2_index[x]]                

                        MESSAGE[x] = str(round(new_low_conc_pumptime,2))
                        MESSAGE[inputpump2_index[x]] = str(round(new_high_conc_pumptime,2))
                        MESSAGE[x+16] = "--" ### Do not run efflux when running influx
                        with open(pump_path, "a+") as outfile:
                            outfile.write(f"{round(elapsed_time,4)},{round(new_low_conc_pumptime,2)},in1\n")
                            outfile.write(f"{round(elapsed_time,4)},{round(new_high_conc_pumptime,2)},in2\n")
            else:
                """
                We could have dropped below the threshold without doing a second dispense. 
                First check to see if the drug addition action is completed (i.e. the last drug time has been updated).
                """
                if ((elapsed_time - lastdrugtime) > (60.*3.5/3600.)) and ((elapsed_time - lastdrugtime) < settings.interval[x]):
                    ## We should have seen a single pump dispense since the last drug time
                    if (pumpdata[(pumpdata.time > lastdrugtime) & (pumpdata.pump == "in1")].shape[0] == 1):
                        if ((elapsed_time - lastpumptime) > 30./3600.):
                            print("\t",x,"Step2")
                            new_low_conc_pumptime = volume_low_2/flow_rate[x]
                            new_high_conc_pumptime = (MAXDISPENSE-volume_low_2)/flow_rate[inputpump2_index[x]]
                            # compute current drug concentration:
                            current_drug_concentration = last_seen_concentration + stepsize(last_seen_concentration,
                                                                                            settings.low_concentration[x],
                                                                                            settings.high_concentration[x],
                                                                                            volume_low_1,
                                                                                            volume_low_2,
                                                                                            MAXDISPENSE)
                            append_drug_concentration(drugconc_path, elapsed_time,
                                                      current_drug_concentration)
                            MESSAGE[x] = str(round(new_low_conc_pumptime,2))
                            MESSAGE[inputpump2_index[x]] = str(round(new_high_conc_pumptime,2))
                            MESSAGE[x+16] = "--" ### Do not run efflux when running influx
                            with open(pump_path, "a+") as outfile:
                                outfile.write(f"{round(elapsed_time,4)},{round(new_low_conc_pumptime,2)},in1\n")
                                outfile.write(f"{round(elapsed_time,4)},{round(new_high_conc_pumptime,2)},in2\n")
                        # else: Wait one more period                    

                elif ((elapsed_time - lastdrugtime) > 3*settings.interval[x]):
                    """
                    The cells are too stressed to recover, so we force-initiate a drug dispense step.
                    """
                    ## We should have seen a single pump dispense since the last drug time
                    if (pumpdata[(pumpdata.time > lastdrugtime) & (pumpdata.pump == "in1")].shape[0] == 1):
                        if ((elapsed_time - lastpumptime) > 30./3600.):
                            print("\t",x,"Step2")
                            new_low_conc_pumptime = volume_low_2/flow_rate[x]
                            new_high_conc_pumptime = (MAXDISPENSE-volume_low_2)/flow_rate[inputpump2_index[x]]
                            # compute current drug concentration:
                            current_drug_concentration = last_seen_concentration + stepsize(last_seen_concentration,
                                                                                            settings.low_concentration[x],
                                                                                            settings.high_concentration[x],
                                                                                            volume_low_1,
                                                                                            volume_low_2,
                                                                                            MAXDISPENSE)
                            append_drug_concentration(drugconc_path, elapsed_time,
                                                      current_drug_concentration)
                            MESSAGE[x] = str(round(new_low_conc_pumptime,2))
                            MESSAGE[inputpump2_index[x]] = str(round(new_high_conc_pumptime,2))
                            MESSAGE[x+16] = "--" ### Do not run efflux when running influx
                            with open(pump_path, "a+") as outfile:
                                outfile.write(f"{round(elapsed_time,4)},{round(new_low_conc_pumptime,2)},in1\n")
                                outfile.write(f"{round(elapsed_time,4)},{round(new_high_conc_pumptime,2)},in2\n")
                        # else: Wait one more period
                    if (pumpdata[(pumpdata.time > lastdrugtime) & (pumpdata.pump == "in1")].shape[0] == 0 ):
                        ## else, do Step 1.
                        print("\t",x,"Step1")                        
                        new_low_conc_pumptime = volume_low_1/flow_rate[x]
                        new_high_conc_pumptime = (MAXDISPENSE-volume_low_1)/flow_rate[inputpump2_index[x]]                

                        MESSAGE[x] = str(round(new_low_conc_pumptime,2))
                        MESSAGE[inputpump2_index[x]] = str(round(new_high_conc_pumptime,2))
                        MESSAGE[x+16] = "--" ### Do not run efflux when running influx
                        with open(pump_path, "a+") as outfile:
                            outfile.write(f"{round(elapsed_time,4)},{round(new_low_conc_pumptime,2)},in1\n")
                            outfile.write(f"{round(elapsed_time,4)},{round(new_high_conc_pumptime,2)},in2\n")
                
    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)
                
            
## ═════════════════════════════════════════════════════════════════════════════
## alternating_selection
## ═════════════════════════════════════════════════════════════════════════════
## Two-state adaptive-evolution controller. The protocol is switch_logic_outline.txt
## [2026-09-21]; switch_logic_flow.dot/.png render the same machine, and the line
## numbers quoted in the comments below refer to the outline.
##
##   HIGH  Challenge the culture at Current_Drug. Each growth cycle that reaches the
##         OD setpoint inside growth_interval counts toward a streak -- but only once
##         the vial has actually CLIMBED to Current_Drug; the climb cycles dilute and
##         spend the Stress_Wait_Time budget without counting. n_tolerant in a row
##         inside the budget is a "happy stress state" and earns +ramp. The budget
##         expiring first holds the concentration. Either way, go to LOW.
##   LOW   Purge fold_dilution of the drug in fixed 5 mL plain-media steps, then
##         dilute with plain media until n_dilutions consecutive cycles come in
##         inside growth_interval -- the evidence that viable cells recovered. Then
##         back to HIGH at the (possibly ramped) Current_Drug.
##
## The two states alternate forever, by design, including once Current_Drug has been
## clamped at the high reservoir (outline line 65: "Do Forever, even if we hit high
## media concentration").
##
## ALL per-vial state lives on disk, as everywhere else in this file: nothing is
## carried in memory between event cycles, so a controller restart resumes mid-state,
## mid-purge, or mid-dispense-pair from the logs alone.
##
## Files this mode owns, under <exp_dir>/<exp_name>/:
##   state_log/vial{x}_state.txt    time,state      one row per state TRANSITION
##   drug_target/vial{x}_drug_target.txt
##                                  time,target     one row per ramp
##   cycle_log/vial{x}_cycles.txt   time,state,kind,conc_before,conc_after,
##                                  cycle_duration,dilution_counter,time_in_state
##                                                  one row per completed cycle
## and it reuses pumpcontrol_ramp's pump_log/ and drugconc/ formats unchanged.

ALTSEL_HIGH = "HIGH"
ALTSEL_LOW = "LOW"

## Fluidics constants, all lifted from pumpcontrol_ramp so that the two modes behave
## identically at the pump. A dilution is a PAIR of 5 mL steps at least
## ALTSEL_STEP_SEPARATION apart, with the efflux windows sitting in between.
ALTSEL_MAXDISPENSE = 5.0
ALTSEL_SENSOR = 135
ALTSEL_NUM_TO_AVERAGE = 10
ALTSEL_EFFLUX_SECONDS = 10

## How long a dose sits before the efflux clears the volume it added. 17 s is
## pumpcontrol_ramp's own lower window edge, kept so the mixing behaviour is
## unchanged; only the upper bound is gone.
ALTSEL_EFFLUX_SETTLE = 17. / 3600.
ALTSEL_PAIR_SETTLE = 60. * 3.5 / 3600.      # 3.5 min after a dose before the next pair
ALTSEL_STEP_SEPARATION = 30. / 3600.        # 30 s between the two legs of a pair

## "The vial has arrived at Current_Drug", in g/L. Loose enough to absorb the
## quantisation the pump_min floor imposes on a hold -- find_optimal_pump_volumes'
## own docstring puts that at +/- pump_min*(ch-cl)/(2*(V+D)), which is 0.046 g/L at
## cl=0, ch=5, V=22 -- and tight enough that a whole 0.5 g/L ramp step can never be
## mistaken for having arrived.
## FLOOR for the at-target tolerance; the operative value is derived per vial in
## _altsel_at_target_tol from the configured reservoirs and volume. This floor only
## has to clear the solver's own convergence noise and the 2 dp drugconc log.
ALTSEL_AT_TARGET_TOL = 2e-2

## find_optimal_pump_volumes' pump_min default. Mirrored here because the at-target
## tolerance is derived from it; if that default changes, this must follow.
ALTSEL_PUMP_MIN = 0.5

ALTSEL_PUMP_HEADER = "time,timein,pump"
## How far outside [low_concentration, high_concentration] a logged concentration
## may sit before it is treated as corruption rather than rounding. The drugconc log
## is written at 2 dp, so a legitimate value can miss a reservoir bound by 0.005.
ALTSEL_CONC_SLACK = 0.02

## How long to wait for a logged efflux between purge steps before saying so. Long
## enough that the 6-minute backstop window has had its chance.
ALTSEL_PURGE_EFFLUX_WAIT = 7. * 60. / 3600.

## How far behind its own logs elapsed_time may sit before the clock is disbelieved.
## Generous enough to absorb the 4 dp quantisation and a little jitter, far below any
## real NTP correction.
ALTSEL_CLOCK_SLACK = 1e-3

## How much wall clock past a vial's last observed activity still counts toward the
## HIGH budget. Anything beyond this is treated as controller downtime rather than
## time the culture spent being challenged. One growth_interval would be too
## generous; a few event cycles is enough to bridge ordinary jitter.
ALTSEL_DOWNTIME_GRACE = 10. * 60. / 3600.

## A growth cycle shorter than this fraction of media_wait_time cannot be biological
## evidence of anything and must not be credited to a tolerance streak.
ALTSEL_MIN_CYCLE_FRACTION = 0.05

ALTSEL_STATE_HEADER = "time,state"
ALTSEL_TARGET_HEADER = "time,target"
ALTSEL_CYCLE_HEADER = ("time,state,kind,conc_before,conc_after,cycle_duration,"
                       "dilution_counter,time_in_state")


def _altsel_append_row(path, header, row):
    """Append one row, writing `header` first if the file is empty.

    Emptiness is tested BY SIZE, not existence, for the same reason
    log_config_changes does it: an append that failed part-way (a full disk) leaves a
    zero-byte file behind, and treating that as "already has a header" makes the
    first data row masquerade as the header for every later reader.
    """
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
    empty = (not os.path.exists(path)) or os.path.getsize(path) == 0
    ## ONE write() call for header+row, not two. Two calls let a kill land between
    ## them, leaving a header-only file -- and a header-only drugconc log used to
    ## raise IndexError out of _altsel_context on every later cycle, skipping the
    ## vial for the rest of the experiment with no efflux. One call cannot produce
    ## that state (a short write can still truncate, which the tail-row guard in
    ## _altsel_read_table handles).
    payload = (header + "\n" + row + "\n") if empty else (row + "\n")
    with open(path, "a+") as fh:
        fh.write(payload)


def _altsel_read_table(path, names=None, skip_header=False):
    """Read one of this mode's logs, dropping an unparseable final row.

    A truncated last line is the single most likely artefact of a power cut
    mid-append, and it describes an action that did not complete. Every reader used
    to propagate it: `nan` reached `str()` as the state, `int()` as the counter and
    `float()` as the concentration, and each of those stranded the vial on every
    subsequent cycle -- loudly, but forever, with no self-repair. Dropping the row
    and saying so once recovers the vial and loses nothing real.

    Returns an empty DataFrame for a missing, empty or header-only file, so callers
    can test `.shape[0] == 0` instead of each guarding `.tail(1)` differently.
    """
    if (not os.path.exists(path)) or os.path.getsize(path) == 0:
        return pd.DataFrame()
    try:
        if names is None:
            data = pd.read_csv(path, sep=",")
        else:
            data = pd.read_csv(path, sep=",", names=names,
                               skiprows=[0] if skip_header else None)
    except Exception as exc:
        ## A malformed row anywhere: re-read line by line, keeping the prefix that
        ## parses. Refusing the whole file would strand the vial permanently.
        print("ALTSEL could not parse %s (%s: %s); retrying without its final row"
              % (path, type(exc).__name__, exc))
        try:
            with open(path) as fh:
                lines = fh.read().splitlines()
            if len(lines) <= 1:
                return pd.DataFrame()
            from io import StringIO
            body = "\n".join(lines[:-1]) + "\n"
            if names is None:
                data = pd.read_csv(StringIO(body), sep=",")
            else:
                data = pd.read_csv(StringIO(body), sep=",", names=names,
                                   skiprows=[0] if skip_header else None)
        except Exception as exc2:
            print("ALTSEL still cannot parse %s (%s: %s); treating it as empty"
                  % (path, type(exc2).__name__, exc2))
            return pd.DataFrame()
    if data.shape[0] == 0:
        return data
    ## A tail row that parsed structurally but carries NaN in a column the logic
    ## reads back as authoritative state is a torn write too.
    tail = data.tail(1)
    if tail.isna().any(axis=1).values[0]:
        print("ALTSEL dropping the final row of %s: it carries a missing value, "
              "which means the append did not complete" % path)
        data = data.iloc[:-1]
    return data


def _altsel_paths(eVOLVER, x):
    """Every file one vial's logic reads or appends."""
    base = os.path.join(eVOLVER.exp_dir, settings.exp_name)
    return {
        "pump":   os.path.join(base, "pump_log", "vial{0}_pump_log.txt".format(x)),
        "conc":   os.path.join(base, "drugconc", "vial{0}_drugconc.txt".format(x)),
        "state":  os.path.join(base, "state_log", "vial{0}_state.txt".format(x)),
        "target": os.path.join(base, "drug_target",
                               "vial{0}_drug_target.txt".format(x)),
        "cycle":  os.path.join(base, "cycle_log", "vial{0}_cycles.txt".format(x)),
        "od":     os.path.join(base, "od_{0}_raw".format(ALTSEL_SENSOR),
                               "vial{0}_od_{1}_raw.txt".format(x, ALTSEL_SENSOR)),
    }


def _altsel_timings(x):
    """(growth_interval, stress_wait_time) in hours.

    Derived on every call rather than cached in Settings, so that a live edit to
    media_wait_time, n_tolerant, or either multiplier retimes the controller within
    the same event cycle -- which is the whole point of them being live fields.
    """
    media_wait = float(settings.media_wait_time[x])
    growth_interval = float(settings.growth_interval_multiplier[x]) * media_wait
    stress_wait = (float(settings.n_tolerant[x])
                   * float(settings.stress_wait_fraction[x]) * media_wait)
    return growth_interval, stress_wait


def _altsel_purge_steps(x):
    """How many 5 mL plain-media steps approximate a fold_dilution-fold washout.

    One step adds MAXDISPENSE to a vial of `volume` and the efflux takes it back
    down, so each step multiplies BOTH cell density and drug by V/(V+D). Solving
    (V/(V+D))**n = 1/fold gives n = ln(fold)/ln((V+D)/V), which is 11.2 for fold=10,
    V=22, D=5 -- hence the 11 steps the protocol names. Rounded, so the delivered
    washout is 9.5x rather than exactly 10x; the protocol says "approximately".
    """
    volume = float(settings.volume[x])
    fold = float(settings.fold_dilution[x])
    if not (volume > 0) or not (fold > 1):
        return 0
    return int(round(math.log(fold)
                     / math.log((volume + ALTSEL_MAXDISPENSE) / volume)))


def _altsel_min_cycle(x):
    """The shortest growth cycle that can be believed, in hours."""
    return ALTSEL_MIN_CYCLE_FRACTION * float(settings.media_wait_time[x])


def _altsel_read_state(path):
    """(state, entry_time) from the last transition row, or (None, None)."""
    data = _altsel_read_table(path)
    if data.shape[0] == 0:
        return (None, None)
    last = data.tail(1)
    return (str(last.state.values[0]), float(last.time.values[0]))


def _altsel_read_target(path):
    """The current Current_Drug, or None if the log has no rows yet."""
    data = _altsel_read_table(path)
    if data.shape[0] == 0:
        return None
    return float(data.tail(1).target.values[0])


def _altsel_read_cycles(path, state_time, state=None):
    """(counter, time_in_state, cycle_start, purge_done) for the CURRENT state.

    Read back from the cycle log rather than recomputed from the pump and
    concentration history. How a finished cycle was classified depends both on the
    concentration the culture grew AT and on growth_interval as it was configured
    when the cycle closed; re-deriving that later would silently reinterpret settled
    history the moment a live config change lands. The row is the record.

    `cycle_start` is the boundary the next growth cycle is measured from: the last
    completed cycle in this state, or the state entry if there is none yet.
    """
    data = _altsel_read_table(path)
    if data.shape[0] == 0:
        return (0, 0.0, state_time, 0)
    ## `>=`, not `>`: elapsed_time is quantised to 4 dp (0.36 s), so a cycle row can
    ## share the state-entry timestamp after a socket reconnect delivers two
    ## broadcasts back to back. With `>` that row was silently dropped, which
    ## restarted a purge ladder mid-flight. Rows are only ever appended in time
    ## order, and a transition never closes a cycle in the same event cycle (both
    ## branches return immediately), so no row can belong to the previous visit.
    data = data[data.time >= state_time]
    ## Also filter on the state the row itself records. The `>=` above admits a row
    ## from the PREVIOUS state whose 4 dp timestamp ties the transition -- which is
    ## the same double-broadcast that made `>` unsafe. Without this, a HIGH cycle
    ## row could become LOW's first row, carrying the HIGH tolerance streak into
    ## LOW's exit test and returning the vial to full challenge with NO purge.
    if state is not None and "state" in data.columns:
        data = data[data.state == state]
    if data.shape[0] == 0:
        return (0, 0.0, state_time, 0)
    last = data.tail(1)
    return (int(last.dilution_counter.values[0]),
            float(last.time_in_state.values[0]),
            float(last.time.values[0]),
            int((data.kind == "purge").sum()))


class _AltSelVial(object):
    """One vial's complete situation for one event cycle, read off disk.

    A per-cycle snapshot and nothing more: no field here survives to the next cycle,
    which is what makes a controller restart resume cleanly from the logs.
    """
    pass


def _altsel_context(eVOLVER, x, elapsed_time, MESSAGE, flow_rate, pump2):
    """Build the per-cycle snapshot for vial x, or None if it must be skipped.

    Also seeds, on a vial's first ever cycle, the three logs this mode owns plus the
    drugconc log it shares with pumpcontrol_ramp.
    """
    ctx = _AltSelVial()
    ctx.x = x
    ctx.pump2 = pump2
    ctx.elapsed_time = elapsed_time
    ctx.message = MESSAGE
    ctx.flow_rate = flow_rate
    ctx.paths = _altsel_paths(eVOLVER, x)

    ctx.volume = float(settings.volume[x])
    ctx.cl = float(settings.low_concentration[x])
    ctx.ch = float(settings.high_concentration[x])
    ctx.setpoint = float(settings.setpoint[x])
    ctx.growth_interval, ctx.stress_wait = _altsel_timings(x)

    ## Pump history. The `pump` column is the step tag: "in1"/"in2" for the two legs
    ## of a dilution pair, exactly as pumpcontrol_ramp writes them, and "in1_purge"
    ## for a LOW-state purge step -- which must never be mistaken for the first leg
    ## of a pair, hence its own tag.
    ctx.pumpdata = _altsel_read_table(ctx.paths["pump"],
                                      names=["time", "timein", "pump"],
                                      skip_header=True)
    if ctx.pumpdata.shape[0] == 0:
        ctx.pumpdata = pd.DataFrame(columns=["time", "timein", "pump"])
    ## Normalise the tag column to strings. pumpcontrol_ramp's
    ## `.loc[isna(), "pump"] = ""` assigns a str into what pandas has inferred as a
    ## float64 column whenever every tag is missing (an untouched log holds only the
    ## "0,0" seed row), which is a FutureWarning now and an error in a later pandas.
    ctx.pumpdata["pump"] = ctx.pumpdata["pump"].fillna("").astype(str)
    ## eVOLVER._create_file seeds this log with an experiment header line AND a "0,0"
    ## row, so skiprows=[0] drops the header and an untouched log parses to exactly
    ## one row whose time is 0. Either `> 0` here or pumpcontrol_ramp's `> 1`
    ## therefore yields lastpumptime = 0 for a vial that has never been pumped; `> 0`
    ## simply does not depend on that seed row still being there.
    ## INFLUX rows only. Efflux rows are in this log now too, and counting one as
    ## "the last pump event" would re-arm the efflux window off its own record and
    ## restart the 30 s leg separation from the wrong moment.
    ctx.lastpumptime = 0
    influx_rows = ctx.pumpdata[ctx.pumpdata.pump != "out"]
    if influx_rows.shape[0] > 0:
        ctx.lastpumptime = float(influx_rows.tail(1).time.values[0])

    ## Is this genuinely the vial's first cycle, or has a log been lost? Seeding is
    ## only safe when NOTHING has happened yet. A lost log re-seeded against live
    ## history is the worst failure this mode has: losing state_log re-seeds HIGH
    ## and re-doses a mid-washout culture straight back to the stale Current_Drug,
    ## and losing drugconc resets c1 to the config value while the vial still holds
    ## drug. Both print a line indistinguishable from a real experiment start.
    history = {}
    for key in ("conc", "state", "target", "cycle"):
        rows = _altsel_read_table(ctx.paths[key])
        history[key] = rows.shape[0]
    ## The pump log carries eVOLVER's own "0,0" seed row, so >1 means real pumping.
    history["pump"] = max(0, ctx.pumpdata.shape[0] - 1)
    virgin = all(count == 0 for count in history.values())

    def _refuse_reseed(which):
        print("V%d SKIPPED: %s is empty but this vial has history (%s). Refusing to "
              "re-seed it, because that would re-dose against a stale record. "
              "Restore the log or move the experiment directory aside."
              % (x, ctx.paths[which],
                 ", ".join("%s=%d" % kv for kv in sorted(history.items()))))

    ## Seed the concentration log from the config on the very first cycle. After
    ## that the config value is never consulted again -- the log is the history.
    if history["conc"] == 0 and not virgin:
        _refuse_reseed("conc")
        return None
    if (not os.path.exists(ctx.paths["conc"])) or os.path.getsize(ctx.paths["conc"]) == 0:
        _altsel_append_row(
            ctx.paths["conc"], "time,concentration",
            "%s,%s" % (round(elapsed_time, 4),
                       round(float(settings.initial_concentration[x]), 2)))
        print("V%d seeded drugconc at %.2f g/L"
              % (x, float(settings.initial_concentration[x])))

    concdata = _altsel_read_table(ctx.paths["conc"])
    if concdata.shape[0] == 0:
        ## Header-only or wholly unreadable. Seeding here would reset c1 to the
        ## config value while the vial still holds drug, so refuse instead.
        print("V%d SKIPPED: %s has a header but no usable rows. Not dosing until "
              "this is corrected." % (x, ctx.paths["conc"]))
        return None
    ctx.lastdrugtime = float(concdata.tail(1).time.values[0])
    ctx.c1 = float(concdata.tail(1).concentration.values[0])

    ## A NaN here used to pass straight through find_optimal_pump_volumes and come
    ## back as a full high-concentration dispense, because every NaN comparison is
    ## False. Refuse to dose this vial instead; the efflux message still stands.
    if not np.isfinite(ctx.c1):
        print("V%d SKIPPED: last recorded concentration is %r in %s. Not dosing "
              "until this is corrected." % (x, ctx.c1, ctx.paths["conc"]))
        return None
    ## Finiteness is not enough. A corrupted tail that happens to parse as a float
    ## used to be dosed from: a negative concentration made the solver climb from
    ## below zero (streak never completes, ramp lost), and one above the high
    ## reservoir was COUNTED as a tolerance dilution and ramped on. Neither is
    ## physically reachable, so both mean the record is wrong.
    if not (ctx.cl - ALTSEL_CONC_SLACK <= ctx.c1 <= ctx.ch + ALTSEL_CONC_SLACK):
        print("V%d SKIPPED: last recorded concentration %.4f g/L is outside the "
              "reservoir range [%.4f, %.4f] in %s, so the record is wrong. Not "
              "dosing until this is corrected."
              % (x, ctx.c1, ctx.cl, ctx.ch, ctx.paths["conc"]))
        return None

    ## Seed the state and drug-target logs. A vial always starts in HIGH (outline
    ## line 57), challenging at initial_drug_target.
    if history["state"] == 0 and not virgin:
        _refuse_reseed("state")
        return None
    ctx.state, ctx.state_time = _altsel_read_state(ctx.paths["state"])
    if ctx.state is None:
        ctx.state, ctx.state_time = ALTSEL_HIGH, elapsed_time
        _altsel_append_row(ctx.paths["state"], ALTSEL_STATE_HEADER,
                           "%s,%s" % (round(elapsed_time, 4), ALTSEL_HIGH))
        print("V%d STATE seeded -> HIGH" % x)

    if history["target"] == 0 and not virgin:
        _refuse_reseed("target")
        return None
    ctx.current_drug = _altsel_read_target(ctx.paths["target"])
    if ctx.current_drug is None:
        ctx.current_drug = float(settings.initial_drug_target[x])
        ## Clamp on the way in, not only on a ramp. An initial_drug_target above the
        ## high reservoir is unreachable, so the log would claim a challenge that
        ## was never delivered for the whole experiment.
        if ctx.current_drug > ctx.ch:
            print("V%d NOTE initial_drug_target %.3f g/L exceeds the high reservoir "
                  "%.3f g/L; clamping" % (x, ctx.current_drug, ctx.ch))
            ctx.current_drug = ctx.ch
        if ctx.current_drug < ctx.cl:
            print("V%d NOTE initial_drug_target %.3f g/L is below the low reservoir "
                  "%.3f g/L; clamping" % (x, ctx.current_drug, ctx.cl))
            ctx.current_drug = ctx.cl
        _altsel_append_row(ctx.paths["target"], ALTSEL_TARGET_HEADER,
                           "%s,%s" % (round(elapsed_time, 4),
                                      round(ctx.current_drug, 4)))
        print("V%d Current_Drug seeded at %.3f g/L" % (x, ctx.current_drug))
    if not np.isfinite(ctx.current_drug):
        print("V%d SKIPPED: Current_Drug is %r in %s"
              % (x, ctx.current_drug, ctx.paths["target"]))
        return None

    ## The streak is credited to Current_Drug, so a vial sitting well ABOVE it is
    ## being recorded as tolerant of a concentration far below the one it is
    ## actually growing in. HIGH has no downward path by design; only the LOW purge
    ## brings it back.
    if ctx.c1 > ctx.current_drug + _altsel_at_target_tol(ctx):
        print("V%d NOTE vial is at %.3f g/L, ABOVE Current_Drug %.3f g/L. Cycles "
              "will be credited to %.3f until the next LOW purge brings it down."
              % (x, ctx.c1, ctx.current_drug, ctx.current_drug))

    (ctx.counter, ctx.time_in_state,
     ctx.cycle_start, ctx.purge_done) = _altsel_read_cycles(ctx.paths["cycle"],
                                                            ctx.state_time,
                                                            ctx.state)

    ## Every gate in this mode is a forward difference against a logged timestamp,
    ## so a clock that has moved BACKWARDS fails all of them at once: no influx, no
    ## efflux, no cycle ever closes, time_in_state never advances, and nothing is
    ## printed. eVOLVER derives elapsed_time from an absolute epoch in its pickle
    ## (eVOLVER.py:73), so a backward NTP step or an RTC-less boot produces exactly
    ## this. Say so instead of going quiet.
    newest = max([t for t in (ctx.lastdrugtime, ctx.lastpumptime, ctx.state_time,
                              ctx.cycle_start) if t is not None])
    if elapsed_time < newest - ALTSEL_CLOCK_SLACK:
        print("V%d SKIPPED: elapsed_time %.4f h is BEHIND this vial's own logs "
              "(newest entry %.4f h). The controller clock has moved backwards; "
              "not dosing until it is consistent." % (x, elapsed_time, newest))
        return None

    ## Raw sensor values only -- no OD calibration is involved anywhere in this mode,
    ## exactly as in pumpcontrol_ramp. Readings are taken from the CURRENT growth
    ## cycle alone: anything before the last dilution describes a culture that has
    ## since been diluted.
    ctx.sensorval = np.nan
    sensordata = _altsel_read_table(ctx.paths["od"],
                                    names=["elapsed_time", "od_raw"],
                                    skip_header=True)
    if sensordata.shape[0] > 0:
        recent = sensordata[sensordata.elapsed_time > ctx.cycle_start]
        if recent.shape[0] > 0:
            ctx.sensorval = np.nanmedian(
                recent.od_raw.tail(ALTSEL_NUM_TO_AVERAGE).values)
    return ctx


def _altsel_minimal_ctx(eVOLVER, x, elapsed_time, MESSAGE):
    """Just enough context to schedule an efflux: the last pump time and the paths.

    Deliberately separate from _altsel_context, which can refuse a vial for a dozen
    good reasons. None of those reasons is a reason to leave liquid in the vial.
    """
    ctx = _AltSelVial()
    ctx.x = x
    ctx.elapsed_time = elapsed_time
    ctx.message = MESSAGE
    ctx.paths = _altsel_paths(eVOLVER, x)
    pumpdata = _altsel_read_table(ctx.paths["pump"],
                                  names=["time", "timein", "pump"],
                                  skip_header=True)
    ctx.pumpdata = pumpdata
    ctx.lastpumptime = 0
    if pumpdata.shape[0] > 0:
        ## Efflux rows must not reset the window they are scheduled from, or one
        ## efflux would immediately re-arm the next.
        influx = pumpdata[pumpdata.pump != "out"]
        if influx.shape[0] > 0:
            ctx.lastpumptime = float(influx.tail(1).time.values[0])
    return ctx


def _altsel_efflux_seconds():
    """How long to run the efflux, as the fluid_command string.

    Always the fixed ALTSEL_EFFLUX_SECONDS, regardless of calibrated flow rate.
    The meniscus and the vortex the stir bar drives make the vial's liquid surface
    too unstable to trust a computed, shorter pump time to reliably clear a dose;
    running long is safe because the overflow straw sets the level, so an efflux
    that overruns costs nothing but air.
    """
    return str(round(ALTSEL_EFFLUX_SECONDS, 2))


def _altsel_efflux_if_due(ctx):
    """pumpcontrol_ramp's two efflux windows, unchanged.

    One 10 s efflux about 20 s after a dispense clears the volume just added; the
    second, at 6 minutes, runs again for good measure. Both are keyed off the last
    pump event, and either is overridden back to "--" by any influx this cycle.
    """
    ## pumpcontrol_ramp fires the efflux inside two narrow windows after the last
    ## pump event -- 17-26 s and 6:00-6:25. Those only work at a broadcast cadence
    ## near 20 s, and the cadence is set by the eVOLVER hardware server, not here.
    ## Measured against the simulator with real volume tracking: at a 15 s cadence
    ## the 9 s window is stepped over entirely, only the 6-minute backstop fires,
    ## and each 10 mL dilution pair is met by a single 5 mL efflux -- so the vial
    ## gains 5 mL per cycle without bound and reaches 1267 mL over 22 h. The purge
    ## ladder was only the most visible symptom.
    ##
    ## So the condition is EVIDENCE, not timing: efflux whenever the pump log shows
    ## an influx that no efflux has followed, once the dose has had time to mix.
    ## There is no upper window, so a missed cycle simply retries -- and because the
    ## `out` row is written only when the command survives to fluid_command, an
    ## influx later in this cycle cancelling the efflux leaves the debt recorded and
    ## the next cycle pays it.
    pumpdata = ctx.pumpdata
    if pumpdata.shape[0] == 0:
        return
    influx = pumpdata[pumpdata.pump != "out"]
    outs = pumpdata[pumpdata.pump == "out"]
    if influx.shape[0] == 0:
        return
    last_influx = float(influx.tail(1).time.values[0])
    last_out = float(outs.tail(1).time.values[0]) if outs.shape[0] > 0 else -1.0
    if last_influx <= last_out:
        return                      # the vial is already balanced
    if (ctx.elapsed_time - last_influx) <= ALTSEL_EFFLUX_SETTLE:
        return                      # let the dose mix before pulling it out
    ctx.message[ctx.x + 16] = _altsel_efflux_seconds()
    ## Deliberately NOT logged here. The row is written after the per-vial loop, for
    ## the vials whose efflux slot SURVIVED to the fluid_command -- because an influx
    ## later in the same cycle overrides the slot back to "--", and a row asserting
    ## an efflux that was then cancelled is worse than no row. It also defeated the
    ## purge gate: the gate read the row written moments earlier in the same cycle,
    ## authorised the step, and the step then cancelled that very efflux.


def _altsel_pair_leg(ctx):
    """0 = no pair in flight (the next dispense is leg 1); >=1 = leg 2 is next.

    Counted the way pumpcontrol_ramp counts it: "in1" rows since the last
    concentration row. Leg 2 writes that concentration row, which moves lastdrugtime
    forward and takes the count back to 0.

    Returned as a count rather than compared to exactly 1, because pumpcontrol_ramp's
    `== 1` / `== 0` pair of tests leaves a vial with three or more stray "in1" rows
    matching neither branch: it then receives no influx at all and only a timeout
    branch can restart it.
    """
    pumpdata = ctx.pumpdata
    since = pumpdata[pumpdata.time > ctx.lastdrugtime]
    legs1 = since[since.pump == "in1_leg1"].shape[0]
    legs2 = since[since.pump == "in1_leg2"].shape[0]
    ## Legacy rows from before the legs were tagged: treat a bare "in1" as leg 1 so
    ## an experiment that started on the old format still resumes.
    legs1 += since[since.pump == "in1"].shape[0]
    if legs2 > 0 or legs1 > 1:
        ## Impossible in normal operation: leg 2 writes the concentration row that
        ## moves lastdrugtime forward and takes this count back to zero. Seeing
        ## either means a torn write -- the pump fired and its concentration row did
        ## not land. Dispensing on that reading is how a third 5 mL leg used to get
        ## delivered and then booked as a two-leg pair, leaving a permanent offset
        ## between the log and the vial.
        return -1
    return legs1


def _altsel_leg1_time(ctx):
    """When leg 1 of the pair in flight fired, or None if it cannot be found."""
    pumpdata = ctx.pumpdata
    since = pumpdata[pumpdata.time > ctx.lastdrugtime]
    legs = since[since.pump.isin(["in1_leg1", "in1"])]
    if legs.shape[0] == 0:
        return None
    return float(legs.tail(1).time.values[0])


def _altsel_influx(ctx, volume_low, tag="in1"):
    """One 5 mL step: `volume_low` from the low pump, the remainder from the high."""
    x = ctx.x
    time_low = volume_low / ctx.flow_rate[x]
    time_high = (ALTSEL_MAXDISPENSE - volume_low) / ctx.flow_rate[ctx.pump2]
    ctx.message[x] = str(round(time_low, 2))
    ctx.message[ctx.pump2] = str(round(time_high, 2))
    ## Never efflux in the same cycle as an influx: the dose has to be in the vial
    ## for the dose to be the dose.
    ctx.message[x + 16] = "--"
    ## _altsel_append_row, not a bare append: eVOLVER._create_file always seeds this
    ## log with a line the reader drops via skiprows=[0], so on the vanishingly rare
    ## path where the file is absent this keeps a real dispense row from being eaten
    ## as though it were that header. A no-op for every log eVOLVER created.
    _altsel_append_row(ctx.paths["pump"], ALTSEL_PUMP_HEADER,
                       "%s,%s,%s" % (round(ctx.elapsed_time, 4), round(time_low, 2), tag))
    _altsel_append_row(ctx.paths["pump"], ALTSEL_PUMP_HEADER,
                       "%s,%s,%s" % (round(ctx.elapsed_time, 4), round(time_high, 2), "in2"))


def _altsel_efflux_recorded_since(ctx, since_time):
    """Has an efflux command been logged for this vial after `since_time`?

    The purge is the only path in this mode that dispenses repeatedly without the
    3.5 min settle in between, so it is the only one whose volume balance depends on
    an efflux landing inside a 9 s window, 11 times in a row. The window is only
    reachable at a broadcast cadence near 20 s: measured against the simulator, a
    20 s cadence issues 14 efflux commands per ladder and a 30 s cadence issues 1,
    which puts 55 mL into a 22 mL vial. Rather than assume the cadence, require the
    evidence -- which is why efflux commands are now logged at all.
    """
    pumpdata = ctx.pumpdata
    ## Strictly BEFORE this cycle as well as after the last step. An efflux staged
    ## in the current cycle has not run yet, and the purge step about to fire would
    ## cancel it -- so counting it lets the ladder authorise its own overfill.
    later = pumpdata[(pumpdata.time > since_time)
                     & (pumpdata.time < ctx.elapsed_time)]
    return later[later.pump == "out"].shape[0] > 0


def _altsel_purge_influx(ctx):
    """One purge step: a full MAXDISPENSE of plain low-reservoir media, low pump only.

    Tagged "in1_purge" so _altsel_pair_leg cannot read it as the first leg of a
    dilution pair, which would strand the vial waiting to dispense a leg 2 that no
    caller is going to ask for.
    """
    x = ctx.x
    time_low = ALTSEL_MAXDISPENSE / ctx.flow_rate[x]
    ctx.message[x] = str(round(time_low, 2))
    ctx.message[x + 16] = "--"
    _altsel_append_row(ctx.paths["pump"], ALTSEL_PUMP_HEADER,
                       "%s,%s,%s"
                       % (round(ctx.elapsed_time, 4), round(time_low, 2), "in1_purge"))


def _altsel_run_pair(ctx, volume_low_1, volume_low_2):
    """Dispense whichever leg of the pair is next. Returns (fired, conc_after).

    conc_after stays None until the pair COMPLETES. The pair completing is what
    closes a growth cycle, because it is the only moment at which the vial's new
    concentration is both known and written.

    The caller recomputes both volumes on each of the two cycles, matching
    pumpcontrol_ramp. That is safe here: the inputs cannot move between the legs,
    because the concentration row is written only on completion and neither state's
    guard is evaluated while a pair is in flight.
    """
    leg = _altsel_pair_leg(ctx)
    if leg < 0:
        print("V%d SKIPPED: the pump log shows a dispense past the last "
              "concentration row in %s, so a previous pair did not finish "
              "recording. Not dosing until this is corrected."
              % (ctx.x, ctx.paths["conc"]))
        return (False, None)
    if (ctx.elapsed_time - ctx.lastdrugtime) <= ALTSEL_PAIR_SETTLE:
        return (False, None)            # let the last dose mix and be measured
    if leg == 0:
        _altsel_influx(ctx, volume_low_1, tag="in1_leg1")
        return (True, None)
    if (ctx.elapsed_time - ctx.lastpumptime) <= ALTSEL_STEP_SEPARATION:
        return (False, None)            # wait a cycle so the efflux can run
    ## Compute BEFORE dispensing: a non-finite result must not leave the vial having
    ## been dosed with no concentration row to show for it, because the next cycle
    ## would then read the extra "in1" row as another leg in flight.
    delta = stepsize(ctx.c1, ctx.cl, ctx.ch, volume_low_1, volume_low_2,
                     ALTSEL_MAXDISPENSE, ctx.volume)
    conc_after = ctx.c1 + delta
    if not np.isfinite(conc_after):
        print("V%d SKIPPED leg 2: the resulting concentration would be %r "
              "(c1=%r, vl1=%r, vl2=%r)"
              % (ctx.x, conc_after, ctx.c1, volume_low_1, volume_low_2))
        return (False, None)
    _altsel_influx(ctx, volume_low_2, tag="in1_leg2")
    return (True, conc_after)


def _altsel_close_cycle(ctx, conc_after, kind, counter, time_in_state):
    """Record a completed cycle: the new concentration, then the classified cycle.

    Concentration first, because append_drug_concentration is the one gate that
    refuses to write a non-finite value; if it refuses there is no cycle to record.
    """
    if not append_drug_concentration(ctx.paths["conc"], ctx.elapsed_time, conc_after):
        return False
    _altsel_append_row(
        ctx.paths["cycle"], ALTSEL_CYCLE_HEADER,
        "%s,%s,%s,%s,%s,%s,%d,%s"
        % (round(ctx.elapsed_time, 4), ctx.state, kind,
           round(ctx.c1, 4), round(conc_after, 4),
           round(ctx.elapsed_time - ctx.cycle_start, 4),
           counter, round(time_in_state, 4)))
    return True


def _altsel_dilution_due(ctx, leg):
    """(due, overrun) for the growth cycle in progress.

    A dilution is due when the culture reached the OD setpoint, or when this cycle
    has overrun growth_interval and gets diluted anyway (outline lines 14-15 vs
    20-23). A pair already in flight is always due: leg 2 has to follow leg 1.

    The raw sensor value FALLS as density rises, so the outline's "OD > OD setpoint"
    is `sensorval <= setpoint` here -- the same comparison pumpcontrol_ramp makes.
    """
    cycle_age = ctx.elapsed_time - ctx.cycle_start
    at_setpoint = bool(np.isfinite(ctx.sensorval)) and (ctx.sensorval <= ctx.setpoint)

    ## Latch `overrun` at the moment the dilution was TRIGGERED, which for a pair
    ## already in flight is when leg 1 fired -- not now. cycle_age only grows, and
    ## leg 2 lands at least 30 s plus one event period later, so re-deciding here
    ## could only ever turn a cycle that comfortably beat growth_interval into an
    ## `overrun` and wipe its streak. It never rescues a genuine overrun. The
    ## post-purge first recovery cycle sits right on this boundary, so it is not a
    ## rare corner.
    if leg >= 1:
        leg1_time = _altsel_leg1_time(ctx)
        if leg1_time is not None:
            trigger_age = leg1_time - ctx.cycle_start
            return (True, trigger_age >= ctx.growth_interval)
        return (True, cycle_age >= ctx.growth_interval)
    return (at_setpoint or (cycle_age >= ctx.growth_interval),
            cycle_age >= ctx.growth_interval)


def _altsel_at_target_tol(ctx):
    """How close to Current_Drug counts as having arrived, in g/L.

    NOT a constant. The pump_min floor quantises what a dispense can deliver, and
    when no exact hold is feasible find_optimal_pump_volumes prefers the nearest
    NON-INCREASING option -- which at low c1 is the full double dilution. The vial
    then has to climb back, and if the tolerance is tighter than that residual the
    climb is scored `climb` rather than `counted`, so `counted` and `climb` alternate
    at 50% duty and the streak can never complete. Measured: with ch=10 and
    Current_Drug=0.5 the vial parks there permanently.

    The residual is pump_min*(ch-cl)/(V+D) -- 0.093 g/L at ch=5, 0.185 at ch=10 --
    so the tolerance has to be at least that. The old hard-coded 6e-2 was sized
    against the docstring's HALF width and was too small at every reservoir strength.
    """
    hardware = (ALTSEL_PUMP_MIN * (ctx.ch - ctx.cl)
                / (ctx.volume + ALTSEL_MAXDISPENSE))
    return max(ALTSEL_AT_TARGET_TOL, hardware)


def _altsel_high_verdict(ctx, n_tolerant):
    """Outline lines 27-33: ramp on a completed streak, hold otherwise, then LOW."""
    x = ctx.x
    ## Has this streak already been cashed? The verdict writes the drug_target row
    ## and then the state row; a crash between them left the vial in HIGH with the
    ## streak still satisfied, so the verdict re-fired and ramped a SECOND time off
    ## the already-ramped target. A target row at or after this state's entry means
    ## the ramp happened -- transition, but do not ramp again.
    already_ramped = False
    targets = _altsel_read_table(ctx.paths["target"])
    if targets.shape[0] > 0:
        already_ramped = bool((targets.time >= ctx.state_time).any())

    if ctx.counter >= n_tolerant and already_ramped:
        print("V%d verdict already recorded for this HIGH visit (Current_Drug "
              "%.3f g/L); transitioning without ramping again"
              % (x, ctx.current_drug))
    elif ctx.counter >= n_tolerant:
        wanted = ctx.current_drug + float(settings.ramp[x])
        ## Clamp at the high reservoir. A target above it can never be reached, so
        ## every later HIGH visit would fail its streak anyway -- with the vial
        ## chasing a number the hardware cannot deliver and the ramp stuck for good.
        new_target = min(wanted, ctx.ch)
        if new_target > ctx.current_drug:
            _altsel_append_row(ctx.paths["target"], ALTSEL_TARGET_HEADER,
                               "%s,%s" % (round(ctx.elapsed_time, 4),
                                          round(new_target, 4)))
            print("V%d HAPPY STRESS STATE: %d/%d consecutive dilutions in %.3f h of "
                  "%.3f h; Current_Drug %.3f -> %.3f g/L"
                  % (x, ctx.counter, n_tolerant, ctx.time_in_state, ctx.stress_wait,
                     ctx.current_drug, new_target))
            if new_target < wanted:
                print("V%d NOTE ramp clamped to the high reservoir (%.3f g/L); "
                      "%.3f was requested" % (x, ctx.ch, wanted))
        elif float(settings.ramp[x]) <= 0:
            print("V%d HAPPY STRESS STATE: %d/%d consecutive dilutions, but ramp is "
                  "%.3f so Current_Drug stays at %.3f g/L"
                  % (x, ctx.counter, n_tolerant, float(settings.ramp[x]),
                     ctx.current_drug))
        else:
            print("V%d HAPPY STRESS STATE but already at the high reservoir "
                  "(%.3f g/L): Current_Drug held" % (x, ctx.ch))
    else:
        print("V%d budget spent: %d/%d consecutive dilutions in %.3f h of %.3f h; "
              "Current_Drug held at %.3f g/L"
              % (x, ctx.counter, n_tolerant, ctx.time_in_state, ctx.stress_wait,
                 ctx.current_drug))
    _altsel_append_row(ctx.paths["state"], ALTSEL_STATE_HEADER,
                       "%s,%s" % (round(ctx.elapsed_time, 4), ALTSEL_LOW))
    print("V%d STATE HIGH -> LOW" % x)


def _altsel_high_logic(ctx):
    """HIGH_Logic (outline lines 7-33): challenge the culture at Current_Drug."""
    x = ctx.x
    n_tolerant = int(settings.n_tolerant[x])
    leg = _altsel_pair_leg(ctx)

    ## The loop guard (line 13), tested only BETWEEN pairs. Transitioning with leg 1
    ## already dispensed would strand half a dilution: the vial would sit 5 mL over
    ## volume with no second leg coming and no concentration row ever written.
    ## The budget is guarded on WALL CLOCK since the state was entered, not on the
    ## sum of closed cycles. The two agree whenever cycles close normally, but the
    ## logged sum can never advance if no cycle can close -- which is what happens
    ## when the solver refuses every cycle (ch <= cl, volume 0) or a pair cannot
    ## complete. The vial then sat in HIGH forever, undiluted and overgrowing, with
    ## no way to reach LOW. The cycle log still records the protocol's own
    ## definition (the sum of cycle durations); only this guard uses wall clock.
    ## Wall clock, but only the part this controller was actually RUNNING for.
    ## elapsed_time comes from an absolute epoch in eVOLVER's pickle, so a power
    ## event or a maintenance restart mid-visit would otherwise hand every HIGH vial
    ## a budget overrun on its first cycle back and drop all sixteen to LOW at once.
    ## Credit is capped at the last observed activity plus a grace period, so a dead
    ## controller stops charging the budget while a live vial that cannot close a
    ## cycle still accrues it (its efflux and refusal cycles keep lastpumptime and
    ## the state entry current).
    last_activity = max(ctx.cycle_start, ctx.lastpumptime, ctx.state_time)
    elapsed_in_state = min(ctx.elapsed_time - ctx.state_time,
                           last_activity - ctx.state_time + ALTSEL_DOWNTIME_GRACE)
    ## `leg <= 0` covers both "no pair in flight" (0) and "the pair is torn" (-1).
    ## Gating on `== 0` alone made the wall-clock escape unreachable in the one
    ## situation that most needs it: a torn pair refuses to dose every cycle, so the
    ## vial sat in HIGH forever, undiluted, with the escape hatch and-ed behind a
    ## condition that could never become true again.
    if leg <= 0 and ((ctx.counter >= n_tolerant)
                     or (max(ctx.time_in_state, elapsed_in_state)
                         >= ctx.stress_wait)):
        _altsel_high_verdict(ctx, n_tolerant)
        return

    due, overrun = _altsel_dilution_due(ctx, leg)
    if not due:
        return

    ## Climb toward Current_Drug, never past it and never downward: the requested
    ## step is whatever gap remains. A gap of zero is a hold, which is the path
    ## find_optimal_pump_volumes' HOLD_TOL logic exists to serve.
    target = max(0.0, ctx.current_drug - ctx.c1)
    try:
        volume_low_1, volume_low_2, achieved = find_optimal_pump_volumes(
            ctx.c1, ctx.cl, ctx.ch, target,
            maxdispense=ALTSEL_MAXDISPENSE, vialvolume=ctx.volume,
            return_delta=True)
    except ValueError as exc:
        ## A config with ch <= cl, or a non-finite argument. Skip this vial rather
        ## than taking the whole rig down: 15 other cultures are running.
        print("V%d SKIPPED: %s" % (x, exc))
        return

    at_target_tol = _altsel_at_target_tol(ctx)

    ## "At target" has to include the case where the hardware cannot climb any
    ## further, or the streak could never start and the vial would never ramp again.
    ##
    ## The test is whether one whole pair can still move the vial by the tolerance we
    ## call "arrived", NOT whether it can move it at all. A dispense replaces only
    ## maxdispense/(V+maxdispense) of the vial, so the climb toward the inflow
    ## concentration is geometric: measured steps from 0.04 g/L toward a 5.0 g/L
    ## target run 1.50, 1.06, 0.70, 0.46, 0.31, 0.21, 0.13, 0.09, 0.06, 0.04 ...
    ## Against RAMP_TOL (1e-3) every one of those still counts as progress, so the
    ## vial creeps at the asymptote (~4.78 g/L for cl=0, ch=5, V=22) and is logged as
    ## "climb" forever while the budget drains -- which is exactly what the `ceiling`
    ## simulation scenario showed before this used ALTSEL_AT_TARGET_TOL.
    capped = (target > at_target_tol) and (achieved < at_target_tol)
    at_target = (target <= at_target_tol) or capped
    if capped:
        print("V%d NOTE at the hardware ceiling: asked for %+.4f g/L but the best "
              "available step is %+.4f (c1=%.3f, low=%.3f, high=%.3f); counting this "
              "cycle as being at target"
              % (x, target, achieved, ctx.c1, ctx.cl, ctx.ch))

    _fired, conc_after = _altsel_run_pair(ctx, volume_low_1, volume_low_2)
    if conc_after is None:
        return                          # leg 1 fired, or the pair is still waiting

    ## Classify the cycle that just closed. An overrun wipes the streak (line 22); a
    ## cycle spent climbing dilutes and spends budget but does not count; only a fast
    ## cycle at the full challenge concentration counts.
    cycle_age = ctx.elapsed_time - ctx.cycle_start
    too_short = cycle_age < _altsel_min_cycle(x)
    if overrun:
        kind, counter = "overrun", 0
    elif too_short:
        ## A cycle of implausible duration cannot be evidence of tolerance. Torn
        ## writes produced 0.011-0.061 h "dilutions" that were scored `counted`,
        ## completed the streak and ramped the drug -- a 40-second growth cycle
        ## standing in for a doubling. Count it as neither success nor failure.
        kind, counter = "tooshort", ctx.counter
        print("V%d HIGH cycle of %.4f h is below the %.4f h plausibility floor; "
              "not crediting it to the streak"
              % (x, cycle_age, _altsel_min_cycle(x)))
    elif at_target:
        kind, counter = "counted", ctx.counter + 1
    else:
        kind, counter = "climb", ctx.counter
    time_in_state = ctx.time_in_state + cycle_age
    if _altsel_close_cycle(ctx, conc_after, kind, counter, time_in_state):
        print("V%d HIGH %-8s [%d/%d] %.3f -> %.3f g/L (target %.3f); cycle %.3f h, "
              "budget %.3f/%.3f h"
              % (x, kind, counter, n_tolerant, ctx.c1, conc_after, ctx.current_drug,
                 cycle_age, time_in_state, ctx.stress_wait))


def _altsel_low_logic(ctx):
    """Low_Logic (outline lines 35-57): purge the drug, then confirm recovery."""
    x = ctx.x
    n_dilutions = int(settings.n_dilutions[x])
    purge_target = _altsel_purge_steps(x)

    ## Phase 1, the purge (lines 41-45). One 5 mL step per event cycle, and a
    ## restart mid-ladder resumes at whichever step the cycle log says was last
    ## completed.
    if ctx.purge_done < purge_target:
        if (ctx.elapsed_time - ctx.lastpumptime) <= ALTSEL_STEP_SEPARATION:
            return
        ## The step separation alone is a timer, not proof. Each step's 5 mL must be
        ## OUT of the vial before the next goes in, so wait for a logged efflux
        ## rather than assuming one fired. Without this the ladder silently
        ## overfills at any broadcast cadence outside roughly 17-26 s.
        ## Step 1 is gated as well, against the last pump event rather than the last
        ## purge step: it otherwise stacks on top of HIGH's leg 2, which has not
        ## necessarily been flushed, and the purge's own concentration arithmetic
        ## assumes the vial is at its nominal volume.
        gate_from = ctx.lastdrugtime if ctx.purge_done > 0 else ctx.lastpumptime
        if not _altsel_efflux_recorded_since(ctx, gate_from):
            if (ctx.elapsed_time - ctx.lastpumptime) > ALTSEL_PURGE_EFFLUX_WAIT:
                print("V%d purge step %d/%d HELD: no efflux has been logged since "
                      "the last step (%.4f h ago). The vial may be over volume; "
                      "check the efflux pump and the broadcast cadence."
                      % (x, ctx.purge_done + 1, purge_target,
                         ctx.elapsed_time - ctx.lastpumptime))
            return
        conc_after = ((ctx.c1 * ctx.volume + ALTSEL_MAXDISPENSE * ctx.cl)
                      / (ctx.volume + ALTSEL_MAXDISPENSE))
        if not np.isfinite(conc_after):
            print("V%d SKIPPED purge step: the resulting concentration would be %r"
                  % (x, conc_after))
            return
        _altsel_purge_influx(ctx)
        ## counter and time_in_state stay at zero through the purge: the recovery
        ## streak has not started, and a purge step is not a growth cycle.
        if _altsel_close_cycle(ctx, conc_after, "purge", 0, 0.0):
            print("V%d LOW purge step %d/%d: %.3f -> %.3f g/L"
                  % (x, ctx.purge_done + 1, purge_target, ctx.c1, conc_after))
        return

    ## Phase 2, recovery (lines 46-55). The guard is the counter alone, with no time
    ## bound, and that is deliberate: this loop exists to revive whatever viable
    ## cells remain, and a culture with none cannot be revived by any other branch.
    leg = _altsel_pair_leg(ctx)
    if leg <= 0 and ctx.counter >= n_dilutions:
        _altsel_append_row(ctx.paths["state"], ALTSEL_STATE_HEADER,
                           "%s,%s" % (round(ctx.elapsed_time, 4), ALTSEL_HIGH))
        print("V%d recovered: %d/%d consecutive dilutions on media; STATE LOW -> HIGH "
              "(Current_Drug %.3f g/L)"
              % (x, ctx.counter, n_dilutions, ctx.current_drug))
        return

    due, overrun = _altsel_dilution_due(ctx, leg)
    if not due:
        return

    ## Plain media on both legs. No solver is involved because there is no
    ## concentration to hit -- the low reservoir IS the target. Keeping the two-leg
    ## pair, rather than a single 5 mL step, matters: it makes a LOW growth cycle the
    ## same 10 mL dilution as a HIGH one, so cycle durations are comparable across
    ## the two states and growth_interval means one thing in both.
    _fired, conc_after = _altsel_run_pair(ctx, ALTSEL_MAXDISPENSE, ALTSEL_MAXDISPENSE)
    if conc_after is None:
        return

    cycle_age = ctx.elapsed_time - ctx.cycle_start
    if overrun:
        kind, counter = "recover_overrun", 0
    elif cycle_age < _altsel_min_cycle(x):
        kind, counter = "tooshort", ctx.counter
        print("V%d LOW cycle of %.4f h is below the %.4f h plausibility floor; "
              "not crediting it to the recovery streak"
              % (x, cycle_age, _altsel_min_cycle(x)))
    else:
        kind, counter = "recovered", ctx.counter + 1
    time_in_state = ctx.time_in_state + cycle_age
    if _altsel_close_cycle(ctx, conc_after, kind, counter, time_in_state):
        print("V%d LOW  %-15s [%d/%d] %.3f -> %.3f g/L; cycle %.3f h"
              % (x, kind, counter, n_dilutions, ctx.c1, conc_after, cycle_age))


def _altsel_unsafe_slots(vials, inputpump2_index):
    """{vial: reason} for every active vial whose 48-slot claims are unsafe.

    A vial owns three slots: influx `x`, efflux `x+16` and drug `input_pump2[x]`.
    They have to be in range, and no two active vials may claim the same one --
    otherwise one pump serves two cultures, or one vial's dose is commanded on
    another's efflux line, both silently.
    """
    active = [x for x in vials if settings.vials_to_run[x] == 1]
    reasons = {}
    claims = {}
    ## Register every active vial's OWN influx and efflux slots first, before any
    ## vial can be refused. Claiming them inside the loop meant a refused vial's
    ## slots stayed unclaimed, so a second vial could take one undetected -- e.g.
    ## vial 0 refused for input_pump2=99, then vial 5 with input_pump2=16 claims
    ## vial 0's efflux slot and delivers its drug leg there, unreported.
    for x in active:
        for slot, role in ((x, "influx"), (x + 16, "efflux")):
            if 0 <= slot < 48:
                claims.setdefault(slot, (x, role))
    for x in active:
        pump2 = inputpump2_index[x]
        if not (0 <= pump2 < 48):
            reasons[x] = ("input_pump2 is %r, which is not a pump slot in 0-47"
                          % pump2)
            continue
        if pump2 in (x, x + 16):
            reasons[x] = ("input_pump2 %d collides with this vial's own %s slot; "
                          "the drug leg would overwrite or be overwritten by it"
                          % (pump2, "influx" if pump2 == x else "efflux"))
            continue
        for slot, role in ((x, "influx"), (x + 16, "efflux"), (pump2, "drug")):
            owner = claims.get(slot)
            if owner is not None and owner[0] != x:
                reasons[x] = ("its %s slot %d is already claimed as vial %d's %s "
                              "slot; two cultures would share one pump"
                              % (role, slot, owner[0], owner[1]))
                reasons.setdefault(owner[0],
                                   "its %s slot %d is also claimed by vial %d's %s"
                                   % (owner[1], slot, x, role))
            else:
                claims[slot] = (x, role)
    return reasons


def alternating_selection(eVOLVER, input_data, vials, elapsed_time):
    """Alternating high-stress / recovery selection, per switch_logic_outline.txt.

    One event cycle: re-read the live config, then walk the active vials, each of
    which is in HIGH or LOW and acts only on what its own logs say. Exactly one
    fluid_command goes out at the end, as in every other mode here.

    `input_data` is unused: like pumpcontrol_ramp, this mode reads the raw sensor
    values back from the od_135_raw log so that the value it acts on is the same one
    a later analysis will see.
    """
    ## Live config: re-read experiment_parameters.yaml and apply any changed
    ## live-reloadable field BEFORE anything in this cycle reads settings, so every
    ## vial in one cycle acts on the same snapshot. Never raises; on any problem the
    ## settings already in force stay in place.
    refresh_live_settings(settings, elapsed_time,
                          config_changes_path=os.path.join(eVOLVER.exp_dir,
                                                           settings.exp_name,
                                                           "config_changes.txt"))

    ## Keyed by vial number. settings.input_pump2 is a 16-list indexed BY VIAL, so
    ## pumpcontrol_ramp's `zip(settings.input_pump2, vials)` is only correct while
    ## `vials` happens to be range(16) in order -- with vials=[5,9,14] it hands
    ## those three cultures pumps 32/33/34 instead of their configured 37/41/46,
    ## i.e. three idle vials' reservoirs pumped into three running cultures while
    ## every drugconc row claims a normal delivery. eVOLVER.py:29 already
    ## contemplates passing a subset, so this cannot stay positional.
    inputpump2_index = {x: int(settings.input_pump2[x]) for x in vials}

    ## Refuse to run a vial whose pump slots are not sane or not its own. An
    ## out-of-range index used to raise IndexError out of MESSAGE[pump2] and kill
    ## every later cycle for the whole rig; a colliding one silently merged two
    ## cultures' lines, delivered one vial's drug as another's efflux, or erased a
    ## dose that had already been written to the log.
    unsafe = _altsel_unsafe_slots(vials, inputpump2_index)
    flow_rate = eVOLVER.get_flow_rate()
    MESSAGE = ['--'] * 48

    for x in vials:
        if settings.vials_to_run[x] != 1:
            continue
        ## EFFLUX FIRST, before anything that can skip this vial. Every reason to
        ## refuse to dose -- a NaN concentration, a torn log, a backwards clock --
        ## is a PERMANENT condition, and the 5 mL of a leg that already dispensed
        ## still has to come out. pumpcontrol_ramp sets its efflux above its own
        ## NaN bail-out for exactly this reason; having the call below the skips
        ## left a refused vial over volume for the rest of the experiment.
        try:
            _altsel_efflux_if_due(_altsel_minimal_ctx(eVOLVER, x, elapsed_time,
                                                      MESSAGE))
        except Exception as exc:
            print("V%d could not schedule efflux (%s: %s)"
                  % (x, type(exc).__name__, exc))

        ## Only now refuse an unsafe vial. Its efflux still stands: the reason it is
        ## refused says nothing about whether it holds liquid.
        if x in unsafe:
            print("V%d SKIPPED: %s" % (x, unsafe[x]))
            continue

        ## Each vial's whole turn is guarded, not just the read. A log write can
        ## fail too (a full disk mid-purge is the likeliest), and letting that
        ## escape skipped fluid_command for ALL sixteen vials -- dropping every
        ## efflux queued above -- while leaving records for dispenses that never
        ## happened.
        try:
            ctx = _altsel_context(eVOLVER, x, elapsed_time, MESSAGE, flow_rate,
                                  inputpump2_index[x])
            if ctx is None:
                continue
            if ctx.state == ALTSEL_HIGH:
                _altsel_high_logic(ctx)
            elif ctx.state == ALTSEL_LOW:
                _altsel_low_logic(ctx)
            else:
                print("V%d SKIPPED: unrecognised state %r in %s"
                      % (x, ctx.state, ctx.paths["state"]))
        except Exception as exc:
            print("V%d SKIPPED: %s: %s" % (x, type(exc).__name__, exc))
            logger.exception("alternating_selection failed for vial %d", x)
            continue

    ## Log the effluxes that SURVIVED. A vial whose state logic dispensed influx
    ## this cycle had its efflux slot overridden back to "--", so writing the row
    ## when the efflux was staged would assert a command that never went out -- and
    ## the purge gate reads these rows as its evidence.
    for x in vials:
        if settings.vials_to_run[x] != 1:
            continue
        staged = MESSAGE[x + 16]
        if staged == "--":
            continue
        try:
            _altsel_append_row(_altsel_paths(eVOLVER, x)["pump"], ALTSEL_PUMP_HEADER,
                               "%s,%s,%s" % (round(elapsed_time, 4), staged, "out"))
        except Exception as exc:
            print("V%d could not log its efflux (%s: %s)"
                  % (x, type(exc).__name__, exc))

    ## In a finally-equivalent position: whatever happened above, the efflux and
    ## influx already staged in MESSAGE must reach the hardware.
    if MESSAGE != ['--'] * 48:
        eVOLVER.fluid_command(MESSAGE)


if __name__ == '__main__':
    print('Please run eVOLVER.py instead')
    logger.info('Please run eVOLVER.py instead')
