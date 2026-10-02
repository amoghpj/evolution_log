#!/usr/bin/env python3
"""Check that live config reload applies what it should and refuses what it should not.

Same discipline as tools/test_schema.py: a reload path that always succeeds is worse
than none, because it produces a green result while a running culture quietly keeps
stale settings. Every case below asserts a REFUSAL as carefully as an application.

    python3 tools/test_live_config.py

Loads only the top of evolver_code/custom_script.py -- everything above the module-level
`settings = Settings()` -- so importing it neither reads the rig's real config nor
instantiates hardware state, the same trick tools/ramp_model.py uses.
"""
import contextlib
import copy
import io
import os
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "evolver_code", "custom_script.py")

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def load_script_head():
    src = open(SCRIPT).read()
    marker = "\nsettings = Settings()"
    assert marker in src, "custom_script.py no longer has a module-level settings = Settings()"
    head = src[:src.index(marker)]
    ns = {"__file__": SCRIPT, "__name__": "custom_script_head"}
    exec(compile(head, "custom_script_head", "exec"), ns)
    return ns


def vial_entry(vial, to_run, **overrides):
    base = {"vial": vial, "to_run": to_run, "volume": 22.0, "temperature": 37}
    if to_run:
        base.update({"setpoint": 55000.0, "interval": 1.5, "input_pump2": float(vial),
                     "number_consecutive_intervals": 2, "initial_concentration": 0.5,
                     "high_concentration": 5.0, "low_concentration": 1.0,
                     "target_ramp": 0.1})
    base.update(overrides)
    return base


def build_config(active=(4, 5)):
    return {"experiment_settings": {
        "exp_name": "live-config-test", "calib_name": None,
        "operation": {"mode": "pumpcontrol_ramp"},
        "per_vial_settings": [vial_entry(v, v in active) for v in range(16)]}}


class FakeSettings(object):
    """Just the attributes refresh_live_settings touches."""
    def __init__(self, ns, config):
        self.exp_name = config["experiment_settings"]["exp_name"]
        for attr, values in ns["configval"].extract_live_values(config).items():
            setattr(self, attr, list(values))


def write(path, config):
    with open(path, "w") as fh:
        yaml.safe_dump(config, fh)
    return path


def main():
    ns = load_script_head()
    cv = ns["configval"]
    tmp = tempfile.mkdtemp(prefix="or05-live-config-")
    cfg_path = os.path.join(tmp, cv.CONFIG_FILENAME)
    changes_path = os.path.join(tmp, "config_changes.txt")
    ns["CONFIG_PATH"] = cfg_path            # point the module at our temp config

    good = build_config()
    write(cfg_path, good)

    # ── extraction mirrors Settings.__init__'s pumpcontrol_ramp branch ───────
    vals = cv.extract_live_values(good)
    ck(vals["target_ramp"][4] == 0.1 and vals["target_ramp"][5] == 0.1,
       "extracts target_ramp for active vials")
    ck(vals["target_ramp"][0] == 0 and vals["setpoint"][0] == 100,
       "inactive vials keep Settings()'s whole-list defaults (0 ramp, 100 setpoint)")
    ck(vals["number_consecutive_intervals"][0] == 10000,
       "inactive number_consecutive_intervals default is 10000, matching Settings()")
    omitted = copy.deepcopy(good)
    del omitted["experiment_settings"]["per_vial_settings"][4]["number_consecutive_intervals"]
    ck(cv.extract_live_values(omitted)["number_consecutive_intervals"][4] == 1000,
       "an ACTIVE vial omitting number_consecutive_intervals defaults to 1000, not 10000")
    ck(isinstance(vals["number_consecutive_intervals"][4], int),
       "number_consecutive_intervals is cast to int as Settings() does")

    st = FakeSettings(ns, good)
    ns["_LIVE_CONFIG_STATE"]["fingerprint"] = None

    # ── first call adopts the file ───────────────────────────────────────────
    ns["refresh_live_settings"](st, 1.0, changes_path)
    ck(st.target_ramp[4] == 0.1, "first refresh leaves a matching config alone")

    # ── an unchanged file is not re-parsed ───────────────────────────────────
    ck(ns["refresh_live_settings"](st, 1.1, changes_path) == [],
       "an unchanged config produces no changes")

    # ── a real ramp change is applied and recorded ───────────────────────────
    bumped = copy.deepcopy(good)
    bumped["experiment_settings"]["per_vial_settings"][4]["target_ramp"] = 0.25
    write(cfg_path, bumped)
    os.utime(cfg_path, (0, 0))              # force a distinct fingerprint
    changes = ns["refresh_live_settings"](st, 2.0, changes_path)
    ck(st.target_ramp[4] == 0.25, "a changed target_ramp is applied live")
    ck(st.target_ramp[5] == 0.1, "an untouched vial is left alone")
    ck(changes == [("target_ramp", 4, 0.1, 0.25)], "the change is reported exactly once")
    rows = open(changes_path).read().strip().split("\n")
    ck(rows[0] == "time,field,vial,old,new", "config_changes.txt gets a header")
    ck(rows[1] == "2.0,target_ramp,4,0.1,0.25", "the change is appended: %r" % rows[1])

    # ── refusals: settings in force must survive every one ───────────────────
    def must_refuse(mutate, msg, raw=None):
        before = list(st.target_ramp)
        if raw is not None:
            open(cfg_path, "w").write(raw)
        else:
            bad = copy.deepcopy(bumped)
            mutate(bad)
            write(cfg_path, bad)
        os.utime(cfg_path, (0, 0))
        ns["_LIVE_CONFIG_STATE"]["fingerprint"] = None
        out = ns["refresh_live_settings"](st, 3.0, changes_path)
        ck(out == [] and st.target_ramp == before, msg)

    must_refuse(None, "malformed yaml is refused, settings unchanged",
                raw="experiment_settings:\n  per_vial_settings: [oh no\n")
    must_refuse(lambda b: b["experiment_settings"]["per_vial_settings"][5]
                .__setitem__("target_ramp", 50.0),
                "an out-of-range target_ramp (50 g/L) is refused")
    must_refuse(lambda b: b["experiment_settings"]["per_vial_settings"][5].pop("to_run"),
                "a vial missing to_run is refused")
    must_refuse(lambda b: b["experiment_settings"]["per_vial_settings"][5]
                .__setitem__("low_concentration", 9.0),
                "low >= high is refused")
    must_refuse(lambda b: b["experiment_settings"].__setitem__("calib_name", "None"),
                "the calib_name null-lookalike is refused on the live path")
    must_refuse(lambda b: b["experiment_settings"]["per_vial_settings"][5]
                .__setitem__("target_ramp", float("nan")),
                "a NaN target_ramp on an active vial is refused")
    os.unlink(cfg_path)
    before = list(st.target_ramp)
    ns["_LIVE_CONFIG_STATE"]["fingerprint"] = None
    ck(ns["refresh_live_settings"](st, 3.5, changes_path) == [] and st.target_ramp == before,
       "a missing config file is refused, settings unchanged")

    # ── a bad config keeps re-reporting rather than going quiet ──────────────
    write(cfg_path, {"experiment_settings": {"exp_name": "x",
                                             "operation": {"mode": "pumpcontrol_ramp"},
                                             "per_vial_settings": []}})
    ns["refresh_live_settings"](st, 4.0, changes_path)
    ck(ns["_LIVE_CONFIG_STATE"]["fingerprint"] is None,
       "a refused config is NOT fingerprinted, so it is re-checked every cycle")

    # ── non-live fields must never be applied ────────────────────────────────
    st2_cfg = copy.deepcopy(good)
    write(cfg_path, st2_cfg)
    os.utime(cfg_path, (0, 0))
    ns["_LIVE_CONFIG_STATE"]["fingerprint"] = None
    st2 = FakeSettings(ns, st2_cfg)
    st2.low_concentration = [1.0] * 16
    st2_cfg["experiment_settings"]["per_vial_settings"][4]["low_concentration"] = 2.0
    st2_cfg["experiment_settings"]["per_vial_settings"][4]["high_concentration"] = 6.0
    write(cfg_path, st2_cfg)
    os.utime(cfg_path, (1, 1))
    ns["refresh_live_settings"](st2, 5.0, changes_path)
    ck(st2.low_concentration[4] == 1.0,
       "low_concentration is NOT live-reloadable and is left untouched")
    ck("low_concentration" not in cv.LIVE_FIELD_NAMES,
       "reservoir concentrations are absent from LIVE_FIELDS by design")

    # ── regressions for defects found by adversarial testing (2026-09-01) ────
    def run(settings_obj, t=9.0, path=changes_path):
        """Call refresh, returning (changes, captured stdout)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = ns["refresh_live_settings"](settings_obj, t, path)
        return out, buf.getvalue()

    def fresh(cfg):
        write(cfg_path, cfg)
        os.utime(cfg_path, (0, 0))
        ns["_LIVE_CONFIG_STATE"]["fingerprint"] = None
        ns["_LIVE_CONFIG_STATE"]["mode_notice"] = None

    # 1. a failing audit-log write must NOT leave the new dose in force
    st3 = FakeSettings(ns, good)
    raised = copy.deepcopy(good)
    raised["experiment_settings"]["per_vial_settings"][4]["target_ramp"] = 0.4
    fresh(raised)
    blocked = os.path.join(tmp, "blocked_dir")
    os.makedirs(blocked, exist_ok=True)          # a directory where a file must go
    out, printed = run(st3, path=blocked)
    ck(out == [] and st3.target_ramp[4] == 0.1,
       "a failing audit-log write leaves the settings untouched (was: applied silently)")
    ck("keeping the settings already in force" in printed,
       "...and the message is now true")

    # 2/3/4. the int cast can no longer launder a value into range, or explode
    for bad_nci, why in [(-0.5, "below the floor, int() would rescue it to 0"),
                         (3.7, "non-integral, int() would truncate to 3"),
                         (float("inf"), "inf, int() would raise OverflowError"),
                         (1000000.9, "above the ceiling, int() would rescue it")]:
        cfg = copy.deepcopy(good)
        cfg["experiment_settings"]["per_vial_settings"][4]["number_consecutive_intervals"] = bad_nci
        fresh(cfg)
        before = list(st3.number_consecutive_intervals)
        out, printed = run(st3)
        ck(out == [] and st3.number_consecutive_intervals == before
           and "number_consecutive_intervals for vial 4" in printed,
           "number_consecutive_intervals %r is refused by name (%s)" % (bad_nci, why))

    # 5. a wrong-length settings list must abort before anything is written
    st4 = FakeSettings(ns, good)
    st4.number_consecutive_intervals = [0] * 10          # malformed settings object
    fresh(bumped)
    before = list(st4.target_ramp)
    out, printed = run(st4)
    ck(out == [] and st4.target_ramp == before,
       "a wrong-length settings list aborts with NOTHING applied (was: 58 of 64)")

    # 6. the validator's message survives instead of a bare IndexError
    cfg = copy.deepcopy(good)
    stray = vial_entry(9, True)
    stray["vial"] = 999
    cfg["experiment_settings"]["per_vial_settings"].append(stray)
    fresh(cfg)
    out, printed = run(st3)
    ck("must be an integer 0-15" in printed and "IndexError" not in printed,
       "an out-of-range vial reports the validator's message, not IndexError")

    # 7. an unsupported mode is announced once, not silently ignored forever
    cfg = copy.deepcopy(good)
    cfg["experiment_settings"]["operation"]["mode"] = "turbidostat"
    fresh(cfg)
    _, first = run(st3)
    _, second = run(st3)
    ck("not applied" in first and "turbidostat" in first,
       "an unsupported mode is announced (was: silent forever)")
    ck(second == "", "...and not repeated every cycle")

    # 8. a vanished file must clear the cached fingerprint
    a = copy.deepcopy(good)
    a["experiment_settings"]["per_vial_settings"][4]["target_ramp"] = 0.1
    b = copy.deepcopy(good)
    b["experiment_settings"]["per_vial_settings"][4]["target_ramp"] = 0.9
    st5 = FakeSettings(ns, a)
    fresh(a)
    run(st5)
    stamp = os.stat(cfg_path)
    size_a = stamp.st_size
    os.unlink(cfg_path)
    run(st5)                                   # missing file: must clear the fingerprint
    write(cfg_path, b)
    if os.stat(cfg_path).st_size == size_a:    # only a fair test if the sizes match
        os.utime(cfg_path, (stamp.st_atime, stamp.st_mtime))
        out, _ = run(st5)
        ck(st5.target_ramp[4] == 0.9,
           "a restore with an identical mtime+size is applied after the file vanished")
    else:
        ck(ns["_LIVE_CONFIG_STATE"]["fingerprint"] is None,
           "a vanished config clears the cached fingerprint")

    # 8b. a touched-but-live-identical config acknowledges the read
    st6 = FakeSettings(ns, good)
    fresh(good)
    _, ack1 = run(st6)                          # first read: nothing differs
    ck("no live field changed" in ack1 and "Live fields are" in ack1,
       "a reload that changes nothing still acknowledges the read")
    _, ack2 = run(st6)                          # untouched: gate returns early, silent
    ck(ack2 == "", "...but an untouched file stays silent (no per-cycle chatter)")
    nonlive = copy.deepcopy(good)
    nonlive["experiment_settings"]["per_vial_settings"][4]["low_concentration"] = 2.0
    nonlive["experiment_settings"]["per_vial_settings"][4]["high_concentration"] = 6.0
    fresh(nonlive)
    _, ack3 = run(st6)
    ck("no live field changed" in ack3,
       "editing a NON-live field is acknowledged rather than silently ignored")
    _, ack4 = run(st6, path=changes_path)
    ck(ack4 == "", "...and is not repeated on the next cycle")

    # 9. a zero-byte log file still gets its header
    empty_log = os.path.join(tmp, "empty_log.csv")
    open(empty_log, "w").close()
    ns["log_config_changes"](empty_log, 7.0, [("target_ramp", 3, 0.1, 0.2)])
    ck(open(empty_log).read().startswith("time,field,vial,old,new"),
       "a zero-byte log file is given a header (was: first row became the header)")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
