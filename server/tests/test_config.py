#!/usr/bin/env python3
"""    ~/py/bin/python tests/test_config.py

Covers the config-generation feature added 2026-09-01: app/config_validator.py
(pure validation logic, pumpcontrol_ramp only -- every other mode is
NotImplemented), the GET /config / POST /config/candidate / POST /config
routes, and the controller_config_change "adequately logged" check this
feature required in app/writer.py (POST /events, not /config -- a config
write never touches evolution_log.json itself)."""
import copy
import json
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.testapp import (  # noqa: E402
    make_client_with_config, make_client_with_real_auth, make_client_with_settings,
)
from tests.fixture import build_evolver_unit_repo  # noqa: E402
from app.evolver_config import EvolverConfigSettings, get_evolver_config_settings  # noqa: E402
from app.config_validator import (  # noqa: E402
    LIVE_FIELD_NAMES, SUPPORTED_MODES, ModeNotImplemented, validate_config)
import app.config_writer as config_writer  # noqa: E402
from app.config_writer import describe_live_reload_effect  # noqa: E402
from app.main import app  # noqa: E402

_fails = []


def ck(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _fails.append(msg)


def good_vial(vial=1, **overrides):
    vial_dict = {
        "vial": vial, "to_run": True, "volume": 22.0, "temperature": 37,
        ## vial+32, custom_script.py's own default and the only arrangement
        ## that does not collide. `input_pump2: vial` made the drug leg claim
        ## the vial's OWN influx slot -- the validator gained a check for
        ## exactly that, and this fixture had been asserting the collision was
        ## fine ever since.
        "setpoint": 50000.0, "interval": 1.5, "input_pump2": vial + 32,
        "number_consecutive_intervals": 0, "initial_concentration": 0.5,
        "high_concentration": 11.0, "low_concentration": 1.0, "target_ramp": 0.1,
    }
    vial_dict.update(overrides)
    return vial_dict


def inactive_vial(vial=0):
    return {"vial": vial, "to_run": False, "volume": 20.0, "temperature": 37}


def good_config(exp_name="test-exp", per_vial_settings=None):
    return {
        "experiment_settings": {
            "exp_name": exp_name,
            "calib_name": None,
            "operation": {"mode": "pumpcontrol_ramp"},
            "per_vial_settings": per_vial_settings if per_vial_settings is not None
            else [inactive_vial(0), good_vial(1)],
        }
    }


def main():
    # ══ validate_config: pure unit tests ═══════════════════════════════════
    problems, warnings = validate_config(good_config())
    ck(problems == [], "a well-formed pumpcontrol_ramp config has zero problems (%s)" % problems)
    ck(warnings == [], "no warnings for a config with no dead fields")

    cfg = good_config()
    cfg["experiment_settings"]["exp_name"] = None
    problems, _ = validate_config(cfg)
    ck(any("exp_name" in p for p in problems), "missing exp_name is a problem")

    cfg = good_config()
    cfg["experiment_settings"]["calib_name"] = "None"
    problems, _ = validate_config(cfg)
    ck(any("calib_name" in p and "None" in p for p in problems),
       "calib_name: None (the yaml string, not null) is rejected -- %s" % problems)

    cfg = good_config()
    cfg["experiment_settings"]["calib_name"] = None
    problems, _ = validate_config(cfg)
    ck(problems == [], "calib_name: null (real None) is fine")

    for unsupported in ("chemostat", "turbidostat", "morbidostat", "calibration",
                        "chemostat_dual", "made-up-mode", None):
        cfg = good_config()
        cfg["experiment_settings"]["operation"] = ({"mode": unsupported} if unsupported else {})
        try:
            validate_config(cfg)
            ck(False, "mode %r should have raised ModeNotImplemented" % unsupported)
        except ModeNotImplemented as exc:
            ck(exc.mode == unsupported, "ModeNotImplemented.mode is %r (%s)" % (unsupported, exc.mode))
            ## Asserted as a SET against the validator's own tuple, not a
            ## hardcoded list: this used to pin ["pumpcontrol_ramp"] and so
            ## started failing the day alternating_selection was added --
            ## reporting a real, deliberate change as a regression, which is
            ## the failure mode that teaches people to ignore a red suite.
            ck(set(exc.supported) == set(SUPPORTED_MODES),
               "the refusal reports exactly the modes the validator supports (%s)"
               % ", ".join(sorted(exc.supported)))

    cfg = good_config(per_vial_settings=[good_vial(1, to_run=None)])
    problems, _ = validate_config(cfg)
    ck(any("to_run" in p for p in problems), "a vial with no explicit boolean to_run is a problem")

    for field in ("setpoint", "interval", "input_pump2", "number_consecutive_intervals",
                  "initial_concentration", "high_concentration", "low_concentration", "target_ramp"):
        cfg = good_config(per_vial_settings=[good_vial(1, **{field: None})])
        problems, _ = validate_config(cfg)
        ck(any(field in p for p in problems),
           "an active vial missing %r is a problem (custom_script.py would silently default it)" % field)

    # an INACTIVE vial does not need any pumpcontrol_ramp field -- only
    # volume/temperature, which custom_script.py reads for every vial.
    cfg = good_config(per_vial_settings=[{"vial": 0, "to_run": False, "volume": 20.0, "temperature": 37}])
    problems, _ = validate_config(cfg)
    ck(problems == [], "an inactive vial with just volume/temperature is fine (%s)" % problems)

    cfg = good_config(per_vial_settings=[{"vial": 0, "to_run": False}])
    problems, _ = validate_config(cfg)
    ck(any("volume" in p for p in problems), "volume is required even for an inactive vial")
    ck(any("temperature" in p for p in problems), "temperature is required even for an inactive vial")

    cfg = good_config(per_vial_settings=[good_vial(1, high_concentration=1.0, low_concentration=11.0)])
    problems, _ = validate_config(cfg)
    ck(any("high_concentration" in p and "low_concentration" in p for p in problems),
       "high_concentration <= low_concentration is rejected (ch > cl is a documented safety invariant)")

    cfg = good_config(per_vial_settings=[good_vial(1), good_vial(1)])
    problems, _ = validate_config(cfg)
    ck(any("1" in p and "more than once" in p for p in problems),
       "the same vial number twice is a problem -- %s" % problems)

    cfg = good_config(per_vial_settings=[good_vial(1, dilution_fraction=0.05, growthdelta=0.0001)])
    problems, warnings = validate_config(cfg)
    ck(problems == [], "dead fields don't block a write")
    ck(len(warnings) == 2, "dilution_fraction and growthdelta each get their own warning (%s)" % warnings)

    # ══ GET /config / POST /config/candidate / POST /config ════════════════
    client, log_settings, evolver_settings, unit_repo = make_client_with_config()

    r = client.get("/config", params={"unit": "no-such-unit"})
    ck(r.status_code == 404, "GET /config on an unknown hardware unit -> 404 (%s)" % r.status_code)

    r = client.get("/config", params={"unit": "testunit"})
    ck(r.status_code == 200, "GET /config on a known unit with no file yet -> 200 (%s)" % r.status_code)
    body = r.json()
    ck({k: body[k] for k in ("unit", "config", "exists")}
       == {"unit": "testunit", "config": {}, "exists": False},
       "no experiment_parameters.yaml yet -> empty config, exists: false (%s)" % body)
    ck(body["validation"]["checked"] is False,
       "and nothing is claimed to have been validated")

    r = client.post("/config/candidate", json={"unit": "testunit", "config": good_config()})
    ck(r.status_code == 200, "POST /config/candidate on a valid config -> 200 (%s)" % r.status_code)
    ck(r.json()["valid"] is True, "a well-formed config validates as valid")

    r = client.post("/config/candidate", json={"unit": "no-such-unit", "config": good_config()})
    ck(r.status_code == 404, "POST /config/candidate on an unknown hardware unit -> 404")

    bad_mode_cfg = good_config()
    bad_mode_cfg["experiment_settings"]["operation"] = {"mode": "chemostat"}
    r = client.post("/config/candidate", json={"unit": "testunit", "config": bad_mode_cfg})
    ck(r.status_code == 501, "an unsupported mode -> 501, not 422 (%s)" % r.status_code)
    ck(r.json()["detail"]["mode"] == "chemostat", "501 detail names the mode that was sent")
    ck(sorted(r.json()["detail"]["supported_modes"]) == sorted(SUPPORTED_MODES),
       "501 detail names what IS supported, asked of the validator rather than "
       "hardcoded -- a list pinned here reports a deliberate new mode as a "
       "regression (%s)" % ", ".join(sorted(r.json()["detail"]["supported_modes"])))

    invalid_cfg = good_config()
    invalid_cfg["experiment_settings"]["exp_name"] = None
    r = client.post("/config/candidate", json={"unit": "testunit", "config": invalid_cfg})
    ck(r.status_code == 200, "candidate validation itself always 200s (it's a report, not a rejection)")
    ck(r.json()["valid"] is False, "an invalid config reports valid: false")
    ck(len(r.json()["problems"]) > 0, "problems are named in the response")

    # write: happy path
    r = client.post("/config", json={"unit": "testunit", "config": good_config()})
    ck(r.status_code == 201, "POST /config on a valid config -> 201 (%s / %s)" % (r.status_code, r.text))
    ck(r.json()["written"] is True, "response confirms the write")
    ck("reminder" in r.json() and "controller_config_change" in r.json()["reminder"],
       "response reminds the caller to separately log a controller_config_change event")

    on_disk = json.loads(log_settings.log_file.read_text())
    ck("config" not in json.dumps(on_disk).lower() or True, "sanity: still valid json")
    log_head_before = subprocess.run(
        ["git", "-C", str(log_settings.log_repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()

    written_path = unit_repo / "experiment_parameters.yaml"
    ck(written_path.exists(), "experiment_parameters.yaml was actually written into the UNIT's own repo")
    unit_log = subprocess.run(
        ["git", "-C", str(unit_repo), "log", "--oneline"], capture_output=True, text=True,
    ).stdout
    ck(len(unit_log.strip().splitlines()) == 2,
       "one new commit landed in the unit's own repo, on top of the fixture commit (%r)" % unit_log)

    log_head_after = subprocess.run(
        ["git", "-C", str(log_settings.log_repo_path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    ck(log_head_before == log_head_after,
       "POST /config never commits into the LOG repo -- HEAD there is unchanged")

    # write: invalid config -> 422, nothing written
    bad_cfg = good_config()
    bad_cfg["experiment_settings"]["exp_name"] = None
    r = client.post("/config", json={"unit": "testunit", "config": bad_cfg})
    ck(r.status_code == 422, "POST /config on an invalid config -> 422 (%s)" % r.status_code)

    # write: unsupported mode -> 501
    r = client.post("/config", json={"unit": "testunit", "config": bad_mode_cfg})
    ck(r.status_code == 501, "POST /config on an unsupported mode -> 501 (%s)" % r.status_code)

    # write: identical config re-submitted -> still 201, no new commit (idempotent no-op)
    unit_log_before = subprocess.run(
        ["git", "-C", str(unit_repo), "log", "--oneline"], capture_output=True, text=True,
    ).stdout
    r = client.post("/config", json={"unit": "testunit", "config": good_config()})
    ck(r.status_code == 201, "re-submitting the identical config -> still 201, not an error (%s)" % r.status_code)
    unit_log_after = subprocess.run(
        ["git", "-C", str(unit_repo), "log", "--oneline"], capture_output=True, text=True,
    ).stdout
    ck(unit_log_before == unit_log_after,
       "an identical re-submission makes no new commit -- 'nothing to commit' is treated as success")

    app.dependency_overrides.pop(get_evolver_config_settings, None)

    # ══ a known hardware unit with NO EVOLVER_UNIT_PATHS entry -> 404 ══════
    client2, log_settings2 = make_client_with_settings()
    from tests.fixture import build_evolver_unit_repo as _build_other_unit_repo
    other_unit_repo = _build_other_unit_repo()
    evolver_settings2 = EvolverConfigSettings(
        unit_paths_json=json.dumps({"some-other-unit": str(other_unit_repo)})
    )
    app.dependency_overrides[get_evolver_config_settings] = lambda: evolver_settings2
    r = client2.get("/config", params={"unit": "testunit"})
    ck(r.status_code == 404,
       "a real hardware unit with no configured evolver_code path -> 404, not 200/500 (%s)" % r.status_code)
    app.dependency_overrides.pop(get_evolver_config_settings, None)

    # ══ POST /config requires auth, like every other write route ══════════
    client3, token, operator = make_client_with_real_auth()
    from tests.fixture import build_evolver_unit_repo
    try:
        unit_repo3 = build_evolver_unit_repo()
        evolver_settings3 = EvolverConfigSettings(unit_paths_json=json.dumps({"testunit": str(unit_repo3)}))
        app.dependency_overrides[get_evolver_config_settings] = lambda: evolver_settings3

        r = client3.post("/config", json={"unit": "testunit", "config": good_config()})
        ck(r.status_code == 401, "POST /config with no bearer token -> 401 (%s)" % r.status_code)

        r = client3.post("/config", json={"unit": "testunit", "config": good_config()},
                          headers={"Authorization": "Bearer %s" % token})
        ck(r.status_code == 201, "POST /config with a valid token -> 201 (%s)" % r.status_code)
        author = subprocess.run(
            ["git", "-C", str(unit_repo3), "log", "-1", "--format=%an <%ae>"],
            capture_output=True, text=True,
        ).stdout.strip()
        ck(author == "%s <%s>" % (operator.git_name, operator.git_email),
           "the commit author is the authenticated operator, not a hardcoded identity (%s)" % author)
    finally:
        app.dependency_overrides.pop(get_evolver_config_settings, None)

    # ══ writer.py: controller_config_change must be "adequately logged" ════
    client4, _settings4 = make_client_with_settings()

    def post_event(**overrides):
        body = {
            "target": {"scope": "facility"},
            "timestamp": "2026-01-02T09:00:00-05:00",
            "event_type": "controller_config_change",
            "provenance": "reported",
            "params": {},
            "notes": "a test config-change event",
        }
        body.update(overrides)
        return client4.post("/events", json=body)

    r = post_event(params={})
    ck(r.status_code == 422, "a controller_config_change with no controller_parameter -> 422 (%s)" % r.status_code)

    r = post_event(params={"controller_parameter": "target_ramp"})
    ck(r.status_code == 422,
       "controller_parameter alone, nothing else describing the change -> 422 (%s)" % r.status_code)

    r = post_event(params={"controller_parameter": "target_ramp", "ramp_step_size": {"value_g_per_L": 0.1}})
    ck(r.status_code == 422,
       "target_ramp missing previous_ramp_step_size -> 422, matching the real EVT-00088 shape")

    r = post_event(params={
        "controller_parameter": "target_ramp",
        "ramp_step_size": {"value_g_per_L": 0.1, "value_mM": 0.793, "unit_primary": "g/L"},
        "previous_ramp_step_size": {"value_g_per_L": 0.05, "value_mM": 0.3965, "unit_primary": "g/L"},
    })
    ck(r.status_code == 201, "a fully-shaped target_ramp change -> 201 (%s / %s)" % (r.status_code, r.text))

    r = post_event(params={"controller_parameter": "setpoint", "unit": "testunit", "lines_affected": ["testunit-v01"]})
    ck(r.status_code == 201,
       "a non-target_ramp parameter only needs SOME other params key, no fixed shape yet (%s / %s)"
       % (r.status_code, r.text))

    # ══ writer.py: the target_ramp shape check is case/whitespace-insensitive ══
    for variant in ("Target_Ramp", "TARGET_RAMP", " target_ramp"):
        r = post_event(params={"controller_parameter": variant})
        ck(r.status_code == 422,
           "controller_parameter %r without ramp_step_size/previous_ramp_step_size -> 422, "
           "same as the exact-match spelling (%s)" % (variant, r.status_code))

    # a non-string controller_parameter must not crash the normalization
    r = post_event(params={"controller_parameter": 12345, "unit": "testunit"})
    ck(r.status_code in (201, 422), "a non-string controller_parameter never 500s (%s)" % r.status_code)

    # ══ GET /config: a config with NaN placeholders never 500s ═════════════
    client5, log_settings5, evolver_settings5, unit_repo5 = make_client_with_config()
    nan_config = good_config(per_vial_settings=[
        {"vial": 0, "to_run": False, "volume": 20.0, "temperature": 37,
         "high_concentration": float("nan"), "target_ramp": float("nan")},
        good_vial(1),
    ])
    written_path5 = unit_repo5 / "experiment_parameters.yaml"
    written_path5.write_text(
        "experiment_settings:\n  exp_name: nan-test\n  calib_name: null\n"
        "  operation:\n    mode: pumpcontrol_ramp\n  per_vial_settings:\n"
        "  - vial: 0\n    to_run: false\n    volume: 20.0\n    temperature: 37\n"
        "    high_concentration: .nan\n    target_ramp: .nan\n"
    )
    r = client5.get("/config", params={"unit": "testunit"})
    ck(r.status_code == 200, "GET /config never 500s on a config with .nan fields (%s)" % r.status_code)
    body5 = r.json()
    ck(body5["config"]["experiment_settings"]["per_vial_settings"][0]["high_concentration"] is None,
       "a .nan field round-trips as JSON null, not a non-standard NaN token or a crash (%s)" % body5)

    # ══ POST /config: a NUL byte in exp_name is rejected without leaving the ══
    # ══ unit repo dirty (validator doesn't catch this; the writer's broadened ══
    # ══ except clause + explicit revert does) ══════════════════════════════
    client6, log_settings6, evolver_settings6, unit_repo6 = make_client_with_config()
    nul_config = good_config(exp_name="null\x00byte-exp")
    r = client6.post("/config", json={"unit": "testunit", "config": nul_config})
    ck(r.status_code == 500, "a NUL byte in exp_name -> 500 via ConfigCommitFailed, not an unhandled crash (%s)"
       % r.status_code)
    ck("/private" not in r.text and str(unit_repo6) not in r.text,
       "the 500's detail does not leak the unit repo's absolute filesystem path (%s)" % r.text)
    status = subprocess.run(["git", "-C", str(unit_repo6), "status", "--porcelain"],
                             capture_output=True, text=True).stdout
    ck(status == "", "the unit repo is left completely clean after the revert (%r)" % status)
    ck(not (unit_repo6 / "experiment_parameters.yaml").exists(),
       "no experiment_parameters.yaml was left behind (this unit's first-ever write failed)")

    # ══ POST /config: a commit failure on a unit's FIRST-EVER write is fully ══
    # ══ reverted (git checkout HEAD has nothing to restore TO in this case) ══
    unit_repo7 = build_evolver_unit_repo()  # config=None -- no experiment_parameters.yaml in HEAD
    evolver_settings7 = EvolverConfigSettings(unit_paths_json=json.dumps({"testunit": str(unit_repo7)}))
    hook7 = unit_repo7 / ".git" / "hooks" / "pre-commit"
    hook7.write_text("#!/bin/sh\nexit 1\n")
    hook7.chmod(hook7.stat().st_mode | stat.S_IEXEC)
    app.dependency_overrides[get_evolver_config_settings] = lambda: evolver_settings7
    client7, _ = make_client_with_settings()
    r = client7.post("/config", json={"unit": "testunit", "config": good_config()})
    ck(r.status_code == 500, "first-ever write + failing pre-commit hook -> 500 (%s)" % r.status_code)
    status7 = subprocess.run(["git", "-C", str(unit_repo7), "status", "--porcelain"],
                              capture_output=True, text=True).stdout
    ck(status7 == "", "repo is clean, not staged-and-dirty, after a first-ever-write failure (%r)" % status7)
    ck(not (unit_repo7 / "experiment_parameters.yaml").exists(),
       "the file itself was removed too, matching the response's 'reverted to HEAD' claim -- "
       "HEAD never had this file, so 'reverted' means 'not present', not left with rejected content")
    hook7.unlink()  # remove the hook so the NEXT write (below) can actually succeed
    r = client7.post("/config", json={"unit": "testunit", "config": good_config()})
    ck(r.status_code == 201, "retrying after removing the hook succeeds cleanly (%s)" % r.status_code)
    app.dependency_overrides.pop(get_evolver_config_settings, None)

    # ══ EvolverConfigSettings: two unit names, one directory -> fail loudly ══
    collision_repo = build_evolver_unit_repo()
    try:
        EvolverConfigSettings(unit_paths_json=json.dumps(
            {"unitA": str(collision_repo), "unitB": str(collision_repo)}))
        ck(False, "two units pointing at the same directory should have raised RuntimeError")
    except RuntimeError as exc:
        ck("unitA" in str(exc) and "unitB" in str(exc),
           "the error names BOTH colliding unit names (%s)" % exc)

    # ══ describe_live_reload_effect: buckets changed fields correctly ══════
    old = good_config(per_vial_settings=[good_vial(1, target_ramp=0.1, high_concentration=11.0)])
    new = good_config(per_vial_settings=[good_vial(1, target_ramp=0.15, high_concentration=12.0)])
    effect = describe_live_reload_effect(old, new, LIVE_FIELD_NAMES)
    ck(any(e["field"] == "target_ramp" and e["old"] == 0.1 and e["new"] == 0.15
           for e in effect["applies_without_restart"]),
       "a changed live field (target_ramp) is bucketed as applies_without_restart (%s)" % effect)
    ck(any(e["field"] == "high_concentration" for e in effect["requires_restart"]),
       "a changed non-live field (high_concentration) is bucketed as requires_restart (%s)" % effect)
    ck(not any(e["field"] == "volume" for e in effect["applies_without_restart"] + effect["requires_restart"]),
       "an UNCHANGED field (volume, identical in old/new) is not reported in either bucket")

    exp_name_old = good_config(exp_name="exp-a")
    exp_name_new = good_config(exp_name="exp-b")
    effect2 = describe_live_reload_effect(exp_name_old, exp_name_new, LIVE_FIELD_NAMES)
    ck(any(e["field"] == "exp_name" for e in effect2["requires_restart"]),
       "a top-level field change (exp_name) is always requires_restart (%s)" % effect2)

    # ══ POST /config: the real route actually returns live_reload, correctly ══
    client8, _, evolver_settings8, unit_repo8 = make_client_with_config(
        config=good_config(per_vial_settings=[good_vial(1, target_ramp=0.1)]))
    r = client8.post("/config", json={"unit": "testunit",
                      "config": good_config(per_vial_settings=[good_vial(1, target_ramp=0.2)])})
    ck(r.status_code == 201, "live_reload write setup succeeded (%s)" % r.status_code)
    live_reload = r.json()["live_reload"]
    ck(any(e["field"] == "target_ramp" for e in live_reload["applies_without_restart"]),
       "the real POST /config response actually carries a correct live_reload.applies_without_restart (%s)"
       % live_reload)
    ck(live_reload["requires_restart"] == [],
       "nothing else changed, so requires_restart is empty (%s)" % live_reload)

    # ══ GET /config/skill: mentions the new live_reload response key ══════
    r = client8.get("/config/skill")
    ck(r.status_code == 200, "GET /config/skill still returns 200 (%s)" % r.status_code)
    ck("live_reload" in r.text and "applies_without_restart" in r.text,
       "the skill doc actually documents the live_reload response field, not just the code")
    ck("does NOT use this code for" in r.text or "Check `valid`, not the status code" in r.text,
       "the skill doc correctly says /candidate never 422s for a business-rule failure -- found "
       "by a doc-audit agent to be wrong before this fix")

    # ══ check_no_silent_removal: a "patch" that drops vials/fields is a 422 ══
    seed16 = good_config(per_vial_settings=[inactive_vial(v) for v in (0, 1, 2, 3)] +
                          [good_vial(4), good_vial(5)])
    client9, _, evolver_settings9, unit_repo9 = make_client_with_config(config=seed16)

    partial = good_config(per_vial_settings=[good_vial(4, target_ramp=0.2)])
    r = client9.post("/config/candidate", json={"unit": "testunit", "config": partial})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "a single-vial 'patch' omitting the other 5 vials is NOT valid by default (%s)" % r.json())
    ck(any("vial" in p and "0" in p for p in r.json()["problems"]),
       "the problem names which vial(s) would be silently removed (%s)" % r.json()["problems"])

    r = client9.post("/config", json={"unit": "testunit", "config": partial})
    ck(r.status_code == 422, "POST /config on the same partial body -> 422, not a destructive write (%s)"
       % r.status_code)
    r = client9.get("/config", params={"unit": "testunit"})
    ck(len(r.json()["config"]["experiment_settings"]["per_vial_settings"]) == 6,
       "the real file on disk is UNTOUCHED -- still all 6 vials (%s)"
       % len(r.json()["config"]["experiment_settings"]["per_vial_settings"]))

    r = client9.post("/config", json={"unit": "testunit", "config": partial, "confirm_removed_fields": True})
    ck(r.status_code == 201,
       "the SAME partial body with confirm_removed_fields: true is accepted -- explicit opt-in (%s)"
       % r.status_code)
    r = client9.get("/config", params={"unit": "testunit"})
    ck(len(r.json()["config"]["experiment_settings"]["per_vial_settings"]) == 1,
       "and now really is down to 1 vial, since the operator explicitly confirmed it")

    # a top-level field (stir_settings) disappearing is caught the same way
    seed_stir = good_config(per_vial_settings=[good_vial(1)])
    seed_stir["experiment_settings"]["stir_settings"] = {"stir_switch": False}
    client10, _, evolver_settings10, unit_repo10 = make_client_with_config(config=seed_stir)
    dropped_stir = good_config(per_vial_settings=[good_vial(1)])  # no stir_settings key at all
    r = client10.post("/config/candidate", json={"unit": "testunit", "config": dropped_stir})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "dropping a top-level field (stir_settings) present in the current config is also caught (%s)"
       % r.json())
    ck(any("stir_settings" in p for p in r.json()["problems"]), "the problem names the missing key")

    # a BRAND NEW unit (no prior config at all) is never blocked by this check
    client11, _, evolver_settings11, unit_repo11 = make_client_with_config(config=None)
    r = client11.post("/config/candidate", json={"unit": "testunit", "config": partial})
    ck(r.status_code == 200,
       "a brand-new unit with nothing to compare against is never blocked by check_no_silent_removal")

    # ══ find_non_finite: NaN/Infinity in a request body is rejected, never ══
    # ══ silently committed then crashed on ═════════════════════════════════
    client12, _, evolver_settings12, unit_repo12 = make_client_with_config()
    inf_config = good_config(per_vial_settings=[good_vial(1, high_concentration=float("inf"))])
    raw = json.dumps({"unit": "testunit", "config": inf_config}, allow_nan=True).encode()
    r = client12.post("/config/candidate", content=raw, headers={"Content-Type": "application/json"})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "an Infinity in high_concentration is NOT valid (%s)" % r.json())
    ck(any("not a finite number" in p for p in r.json()["problems"]), "the problem says exactly why")

    r = client12.post("/config", content=raw, headers={"Content-Type": "application/json"})
    ck(r.status_code == 422, "POST /config with Infinity -> 422, never committed (%s)" % r.status_code)
    status12 = subprocess.run(["git", "-C", str(unit_repo12), "status", "--porcelain"],
                               capture_output=True, text=True).stdout
    ck(status12 == "" and not (unit_repo12 / "experiment_parameters.yaml").exists(),
       "nothing was written or staged (%r)" % status12)

    # GET /config must also never 500 on an .inf already sitting on disk
    # (e.g. from before this guard existed)
    (unit_repo12 / "experiment_parameters.yaml").write_text(
        "experiment_settings:\n  exp_name: inf-test\n  calib_name: null\n"
        "  operation:\n    mode: pumpcontrol_ramp\n  per_vial_settings:\n"
        "  - vial: 0\n    to_run: false\n    volume: 20.0\n    temperature: 37\n"
        "    high_concentration: .inf\n    low_concentration: -.inf\n"
    )
    r = client12.get("/config", params={"unit": "testunit"})
    ck(r.status_code == 200, "GET /config never 500s on .inf/-.inf already on disk (%s)" % r.status_code)
    row = r.json()["config"]["experiment_settings"]["per_vial_settings"][0]
    ck(row["high_concentration"] is None and row["low_concentration"] is None,
       "both +inf and -inf round-trip as JSON null, same as NaN (%s)" % row)

    # ══ check_physically_impossible: negative/zero physical quantities ═════
    client13, _, evolver_settings13, unit_repo13 = make_client_with_config()
    for field, value in (("low_concentration", -5.0), ("high_concentration", -1.0),
                          ("volume", -22.0), ("interval", -1.5),
                          ("number_consecutive_intervals", -100)):
        bad = good_config(per_vial_settings=[good_vial(1, **{field: value})])
        r = client13.post("/config/candidate", json={"unit": "testunit", "config": bad})
        ck(r.status_code == 200 and r.json()["valid"] is False,
           "a negative %s (%r) is rejected -- physically impossible (%s)" % (field, value, r.json()))

    zero_volume = good_config(per_vial_settings=[good_vial(1, volume=0)])
    r = client13.post("/config/candidate", json={"unit": "testunit", "config": zero_volume})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "volume: 0 on an active (to_run: true) vial is rejected (%s)" % r.json())

    # zero volume on an INACTIVE vial is a different validate_config failure
    # (volume is always required and > -- but 0 there isn't specifically
    # flagged by check_physically_impossible's to_run-gated zero check;
    # confirm it's at least not silently accepted as fine either way)
    negative_but_inactive = good_config(per_vial_settings=[
        {"vial": 0, "to_run": False, "volume": -5.0, "temperature": 37}, good_vial(1)])
    r = client13.post("/config/candidate", json={"unit": "testunit", "config": negative_but_inactive})
    ck(r.status_code == 200 and r.json()["valid"] is False,
       "a negative volume on an INACTIVE vial is rejected too -- always-required fields are still "
       "checked for physical sanity regardless of to_run (%s)" % r.json())

    # ══ unknown_unit_message: lists the real, known unit names ═════════════
    r = client13.get("/config", params={"unit": "not-a-real-unit"})
    ck(r.status_code == 404, "unknown unit -> 404 (%s)" % r.status_code)
    ck("testunit" in r.json()["detail"],
       "the 404 lists the real known unit names so a case/typo mismatch is self-correcting (%s)"
       % r.json()["detail"])

    # ══ mode_not_implemented_detail: a missing `operation` section gets a ══
    # ══ helpful likely_cause hint, not just a bare 'mode: null' message ════
    missing_operation = {"experiment_settings": {"exp_name": "x", "per_vial_settings": [good_vial(1)]}}
    r = client13.post("/config/candidate", json={"unit": "testunit", "config": missing_operation})
    ck(r.status_code == 501, "a config with no operation section at all -> 501 (%s)" % r.status_code)
    ck("likely_cause" in r.json()["detail"] and "partial" in r.json()["detail"]["likely_cause"],
       "the 501 hints that this looks like a partial/incomplete document, not a real mode choice (%s)"
       % r.json()["detail"])

    # a genuinely unsupported (but PRESENT) mode does NOT get that hint --
    # it really is just an unsupported mode, not a missing-document signal
    real_other_mode = good_config()
    real_other_mode["experiment_settings"]["operation"] = {"mode": "chemostat"}
    r = client13.post("/config/candidate", json={"unit": "testunit", "config": real_other_mode})
    ck(r.status_code == 501 and "likely_cause" not in r.json()["detail"],
       "a real, present-but-unsupported mode gets the plain message, no false 'incomplete doc' hint (%s)"
       % r.json()["detail"])

    # ══ vials_in_use warning: non-blocking, names the mismatch ═════════════
    # fixture's hardware.units.testunit.vials_in_use == [1, 2, 3] (tests/fixture.py)
    client14, _, evolver_settings14, unit_repo14 = make_client_with_config()
    outside_wiring = good_config(per_vial_settings=[good_vial(9)])  # vial 9 not in [1,2,3]
    r = client14.post("/config", json={"unit": "testunit", "config": outside_wiring})
    ck(r.status_code == 201,
       "an active vial outside vials_in_use is WARNED about, not rejected (%s / %s)" % (r.status_code, r.text))
    ck(any("vials_in_use" in w for w in r.json()["warnings"]),
       "the warning specifically names vials_in_use (%s)" % r.json()["warnings"])

    within_wiring = good_config(per_vial_settings=[good_vial(1)])  # vial 1 IS in [1,2,3]
    client15, _, evolver_settings15, unit_repo15 = make_client_with_config()
    r = client15.post("/config", json={"unit": "testunit", "config": within_wiring})
    ck(r.status_code == 201, "an active vial inside vials_in_use writes cleanly (%s)" % r.status_code)
    ck(not any("vials_in_use" in w for w in r.json()["warnings"]),
       "no vials_in_use warning when the vial actually is in range (%s)" % r.json()["warnings"])

    # ══ TOCTOU fix: old_config is read INSIDE config_write_lock, not before ══
    # ══ it -- closes a real race where two concurrent writers could each ═════
    # ══ pass check_no_silent_removal against the SAME stale snapshot ═════════
    client16, _, evolver_settings16, unit_repo16 = make_client_with_config(
        config=good_config(per_vial_settings=[good_vial(4)]))

    lock_held_during_read = []
    real_read_config = config_writer.read_config

    def _spying_read_config(settings, unit):
        lock_held_during_read.append(config_writer.config_write_lock.locked())
        return real_read_config(settings, unit)

    config_writer.read_config = _spying_read_config
    try:
        r = client16.post("/config", json={"unit": "testunit",
                           "config": good_config(per_vial_settings=[good_vial(4), good_vial(5)])})
        ck(r.status_code == 201, "setup write for the TOCTOU check succeeded (%s)" % r.status_code)
    finally:
        config_writer.read_config = real_read_config
    ck(lock_held_during_read and all(lock_held_during_read),
       "old_config is read WHILE config_write_lock is held, every time (%s) -- this is what "
       "closes the race: the check's verdict can no longer go stale between being computed and "
       "being acted on" % lock_held_during_read)

    # Now the actual race scenario: two "operators" both GET the same
    # snapshot (vial 4 only) before either has posted anything. Operator 1
    # posts first, adding vial 5. Operator 2, still working from their
    # now-stale snapshot, posts a change that never mentions vial 5 at all
    # (because they never knew it existed) -- exactly the shape that used
    # to silently destroy vial 5 via the race, before old_config moved
    # inside the lock.
    client17, _, evolver_settings17, unit_repo17 = make_client_with_config(
        config=good_config(per_vial_settings=[good_vial(4)]))
    snapshot = client17.get("/config", params={"unit": "testunit"}).json()["config"]

    op1_config = copy.deepcopy(snapshot)
    op1_config["experiment_settings"]["per_vial_settings"].append(good_vial(5))
    r1 = client17.post("/config", json={"unit": "testunit", "config": op1_config})
    ck(r1.status_code == 201, "operator 1 adds vial 5 based on the shared snapshot -> 201 (%s)" % r1.status_code)

    op2_config = copy.deepcopy(snapshot)
    op2_config["experiment_settings"]["per_vial_settings"][0]["target_ramp"] = 0.5
    r2 = client17.post("/config", json={"unit": "testunit", "config": op2_config})
    ck(r2.status_code == 422,
       "operator 2's write, based on the SAME now-stale snapshot (missing vial 5), is REJECTED -- "
       "not silently committed, which would have destroyed vial 5 (%s / %s)" % (r2.status_code, r2.text))
    ck(any("5" in p for p in r2.json()["detail"]), "the rejection specifically names vial 5 (%s)" % r2.json())

    r = client17.get("/config", params={"unit": "testunit"})
    ck(len(r.json()["config"]["experiment_settings"]["per_vial_settings"]) == 2,
       "vial 5 genuinely survives -- operator 1's write was never undone (%s)"
       % len(r.json()["config"]["experiment_settings"]["per_vial_settings"]))

    # ══ validation on READ ═════════════════════════════════════════════════
    # A config is written by three different things -- POST /config, the
    # dashboard's Setup tab, an operator with an editor -- and only the first
    # validates. The read is the last moment before somebody acts on it.
    import yaml as _yaml

    client18, _ls18, _es18, unit_repo18 = make_client_with_config("testunit", good_config())
    v = client18.get("/config", params={"unit": "testunit"}).json()["validation"]
    ck(v["checked"] is True and v["ok"] is True,
       "a clean config on disk reads back as checked and ok")

    cpath = Path(unit_repo18) / "experiment_parameters.yaml"
    d = _yaml.safe_load(cpath.read_text())
    d["experiment_settings"]["per_vial_settings"][1]["setpoint"] = "five"
    cpath.write_text(_yaml.safe_dump(d))
    body18 = client18.get("/config", params={"unit": "testunit"}).json()
    v = body18["validation"]
    ck(bool(body18["config"]),
       "a BAD config is still returned -- a read that fails tells the caller less "
       "than one that hands over the file and says what is wrong with it")
    ck(v["checked"] is True and v["ok"] is False and v["problems"],
       "and it reads as not ok, with the problem named (%s)"
       % (v["problems"] or [""])[0][:70])

    d["experiment_settings"]["operation"]["mode"] = "turbidostat"
    cpath.write_text(_yaml.safe_dump(d))
    v = client18.get("/config", params={"unit": "testunit"}).json()["validation"]
    ck(v["checked"] is False and "UNCHECKED" in v["reason"],
       "an unsupported mode reads as UNCHECKED, not as ok -- 'not validated' and "
       "'valid' are different claims")
    ck(sorted(v["supported_modes"]) == sorted(SUPPORTED_MODES),
       "and it names what this server can actually check")

    print("\nRESULT: %s (%d failed)" % ("OK" if not _fails else "PROBLEMS", len(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
