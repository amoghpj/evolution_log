"""Builds a throwaway log repo for tests to point LOG_REPO_PATH at.

Deliberately NOT the real evolution_log.json: that file is under active,
concurrent edit by the actual experiment (see this server's own repo's
history for how often that's already happened), so a test asserting an exact
line count or event count against it would be flaky by construction. schema/
evolution_log.schema.json, tools/lineage.py and tools/media.py ARE copied
from the real repo -- they're the shared ground truth this server is meant
to consume, never a second copy to maintain -- but the log content itself
is small and synthetic.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

REAL_REPO = Path(__file__).resolve().parent.parent.parent


def _concentration(g_per_l: float) -> dict:
    return {"value_g_per_L": g_per_l, "value_mM": round(g_per_l / 126.11 * 1000, 4), "unit_primary": "g/L"}


def _quantity(value, unit: str) -> dict:
    return {"value": value, "unit": unit}


def build_log() -> dict:
    low = _concentration(0.5)
    high = _concentration(5.0)
    t0 = "2026-01-01T09:00:00-05:00"
    t_read = "2026-01-01T12:00:00-05:00"  # +3h, clears media.py's MIN_SPAN_H (2h)

    line_active = {
        "line_id": "testunit-v01",
        "unit": "testunit",
        "vial": 1,
        "strain": "test strain",
        "initial_media": "LB",
        "current_media": "LB",
        "media_switch_count": 0,
        # switch-mode (not constant) -- needed so tests can legitimately
        # exercise media_switch against it; nothing else in the fixture
        # depends on this line's mode being constant specifically.
        "mode": "switch",
        "status": "active",
        "t0": t0,
        "lineage": {
            "parents": [], "children": [], "roots": ["testunit-v01"],
            "depth": 0, "is_founder": True,
            "created_by_event": None, "created_at": t0,
            "terminated_by_event": None, "terminated_at": None,
        },
        "pg_regime": {"low": low, "high": high, "effective_from": t0},
        "reservoirs": {"low": "testunit/LB-0", "high": "testunit/LB-5"},
        "events": [{
            "event_id": "EVT-00001", "timestamp": t0, "event_type": "inoculation",
            "operator": "TEST", "provenance": "reported", "params": {},
            "notes": "fixture inoculation event", "missing_fields": [],
        }],
    }

    t_end = "2026-01-05T09:00:00-05:00"
    line_ended = {
        "line_id": "testunit-v02",
        "unit": "testunit",
        "vial": 2,
        "strain": "test strain",
        "initial_media": "LB",
        "current_media": "LB",
        "media_switch_count": 0,
        "mode": "constant",
        "status": "ended",
        "t0": t0,
        "lineage": {
            "parents": [], "children": [], "roots": ["testunit-v02"],
            "depth": 0, "is_founder": True,
            "created_by_event": None, "created_at": t0,
            "terminated_by_event": "EVT-00002", "terminated_at": t_end,
        },
        "pg_regime": {"low": low, "high": high, "effective_from": t0},
        "reservoirs": {"low": "testunit/LB-0", "high": "testunit/LB-5"},
        "events": [{
            "event_id": "EVT-00002", "timestamp": t_end, "event_type": "termination",
            "operator": "TEST", "provenance": "reported", "params": {},
            "notes": "fixture termination event", "missing_fields": [],
        }],
    }

    line_active_2 = {
        "line_id": "testunit-v03",
        "unit": "testunit",
        "vial": 3,
        "strain": "test strain",
        "initial_media": "LB",
        "current_media": "LB",
        "media_switch_count": 0,
        "mode": "constant",
        "status": "active",
        "t0": t0,
        "lineage": {
            "parents": [], "children": [], "roots": ["testunit-v03"],
            "depth": 0, "is_founder": True,
            "created_by_event": None, "created_at": t0,
            "terminated_by_event": None, "terminated_at": None,
        },
        "pg_regime": {"low": low, "high": high, "effective_from": t0},
        "reservoirs": {"low": "testunit/LB-0", "high": "testunit/LB-5"},
        "events": [{
            "event_id": "EVT-00003", "timestamp": t0, "event_type": "inoculation",
            "operator": "TEST", "provenance": "reported", "params": {},
            "notes": "fixture inoculation event (second active line, for merge/split tests)",
            "missing_fields": [],
        }],
    }

    return {
        "schema_version": "1.0.0",
        "log_meta": {
            "maintained_by": "fixture", "last_updated": t_end,
            "event_counter": 7, "next_event_id": "EVT-00008",
        },
        "experiment": {"identity": "fixture experiment"},
        "design": {"total_lines": 3},
        "hardware": {"units": {"testunit": {"mode": "constant", "vials_in_use": [1, 2, 3], "n_lines": 2}}},
        "reservoirs": {
            "items": [
                {
                    # prepared 1.0 L at t0, read at 0.7 L 3h later -> a real,
                    # non-provisional MEASURED rate (0.1 L/h) for media.py's
                    # analyse() to find -- GET /media has nothing to report
                    # against a reservoir with no consumption history at all.
                    "id": "testunit/LB-0", "unit": "testunit", "media": "LB", "role": "low",
                    "pg": low, "status": "active",
                    "volume_prepared": _quantity(1.0, "L"), "prepared_at": t0,
                    "current_volume": _quantity(0.7, "L"), "level_as_of": t_read,
                    "level_source": "measured", "lines_fed": ["testunit-v01", "testunit-v03"],
                },
                {
                    # prepared 1.0 L at t0, read at 0.85 L 3h later -> 0.05 L/h.
                    "id": "testunit/LB-5", "unit": "testunit", "media": "LB", "role": "high",
                    "pg": high, "status": "active",
                    "volume_prepared": _quantity(1.0, "L"), "prepared_at": t0,
                    "current_volume": _quantity(0.85, "L"), "level_as_of": t_read,
                    "level_source": "measured", "lines_fed": ["testunit-v01", "testunit-v03"],
                },
            ]
        },
        "conventions": {"fixture": "this is test data, not the real experiment"},
        "event_types": {
            "inoculation": "starts a line", "termination": "ends a line", "merge": "merges two or more lines",
            "media_prep": "preparation of a media volume, loaded into a reservoir",
            "level_reading": "reported remaining volume in a reservoir",
            "media_switch": "change of media background for a switch-mode line",
            "reservoir_retired": "a reservoir taken off line",
            "reservoir_change": "facility-level change to a shared media reservoir",
            "hardware_swap": "vials moved to different physical hardware with cultures intact",
            "controller_config_change": "change to eVOLVER control parameters, ramp logic, or firmware",
            "note": "free observation with no structured action attached",
        },
        # Non-empty on purpose: tools/lineage.py's registered-params check is
        # `if reg and k not in reg` -- an EMPTY registry disables that check
        # entirely rather than rejecting everything, so a fixture with {}
        # here would silently defeat any test of that check.
        "parameter_registry": {
            "fixture_note": {"description": "unused by any fixture event; exists only to make the registry non-empty", "status": "active", "type": "string"},
            "predecessor_in_vial": {"description": "the line that previously occupied this vial (restart)", "status": "active", "type": "lineId", "applies_to_event_types": ["inoculation"]},
            "reservoir_id": {"description": "the reservoir this event concerns", "status": "active", "type": "reservoirId", "applies_to_event_types": ["media_prep", "level_reading", "reservoir_retired"]},
            "volume_prepared": {"description": "volume made and loaded", "status": "active", "type": "quantity", "applies_to_event_types": ["media_prep"]},
            "media": {"description": "media background present in the vessel at the time of the event", "status": "active", "type": "string"},
            "source_culture": {"description": "description of the material used to inoculate; descriptive provenance, not ancestry", "status": "active", "type": "string", "applies_to_event_types": ["inoculation"]},
            "role": {"description": "whether the reservoir is the low or high PG feed", "status": "active", "type": "string", "enum": ["low", "high"], "applies_to_event_types": ["media_prep", "reservoir_change"]},
            "volume_remaining": {"description": "volume left in the bottle at the time of reading", "status": "active", "type": "quantity", "applies_to_event_types": ["level_reading"]},
            "level_source": {"description": "measured from the bottle, or the prepared volume unread since", "status": "active", "type": "string", "enum": ["measured", "prepared"], "applies_to_event_types": ["level_reading"]},
            "measurement_qualifier": {"description": "exact, approximate, or a lower bound (at_least)", "status": "active", "type": "string", "enum": ["exact", "approximate", "at_least"], "applies_to_event_types": ["level_reading"]},
            "media_from": {"description": "media background before a media_switch", "status": "active", "type": "string", "applies_to_event_types": ["media_switch"]},
            "media_to": {"description": "media background after a media_switch", "status": "active", "type": "string", "applies_to_event_types": ["media_switch"]},
            "replaced_by": {"description": "the reservoir a retired one was replaced by", "status": "active", "type": "reservoirId", "applies_to_event_types": ["reservoir_retired"]},
            "reservoir_id_from": {"description": "reservoir(s) a reservoir_change moves away from", "status": "active", "type": ["string", "array"], "applies_to_event_types": ["reservoir_change"]},
            "reservoir_id_to": {"description": "reservoir(s) a reservoir_change moves to", "status": "active", "type": ["string", "array"], "applies_to_event_types": ["reservoir_change"]},
            "pg_concentration": {"description": "PG concentration in the vessel (inoculation) or the bottle just prepared (media_prep)", "status": "active", "type": "concentration", "applies_to_event_types": ["inoculation", "media_prep"]},
            "what_moved": {"description": "free text describing what physically moved in a hardware_swap", "status": "active", "type": "string", "applies_to_event_types": ["hardware_swap"]},
            "new_unit": {"description": "ISSUE_002: hardware unit a line's culture was relocated to", "status": "planned", "type": "string", "applies_to_event_types": ["hardware_swap"]},
            "new_vial": {"description": "ISSUE_002: vial number a line's culture was relocated to", "status": "planned", "type": "integer", "applies_to_event_types": ["hardware_swap"]},
            "previous_unit": {"description": "ISSUE_002: hardware unit a line's culture occupied before a hardware_swap relocated it", "status": "planned", "type": "string", "applies_to_event_types": ["hardware_swap"]},
            "previous_vial": {"description": "ISSUE_002: vial number a line's culture occupied before a hardware_swap relocated it", "status": "planned", "type": "integer", "applies_to_event_types": ["hardware_swap"]},
            "vacate": {"description": "ISSUE_002 follow-up: hardware_swap removes the culture from the evolver entirely (unit/vial become null) instead of relocating it; mutually exclusive with new_unit/new_vial", "status": "planned", "type": "boolean", "applies_to_event_types": ["hardware_swap"]},
            "reactivate": {"description": "marks a media_prep against an existing, retired reservoir as deliberately bringing it back into active service", "status": "planned", "type": "boolean", "applies_to_event_types": ["media_prep"]},
            "controller_parameter": {"description": "name of the controller setting that changed", "status": "active", "type": "string", "applies_to_event_types": ["controller_config_change"]},
            "ramp_step_size": {"description": "deterministic ramp increment in force at the time of the event", "status": "active", "type": "concentration", "applies_to_event_types": ["controller_config_change"]},
            "previous_ramp_step_size": {"description": "target_ramp before a controller config change", "status": "active", "type": "concentration", "applies_to_event_types": ["controller_config_change"]},
            "lines_affected": {"description": "lines a facility-level event applies to", "status": "active", "type": "array", "items": "lineId", "applies_to_event_types": ["controller_config_change"]},
            "unit": {"description": "eVOLVER unit a facility-level event applies to", "status": "active", "type": "string", "applies_to_event_types": ["controller_config_change"]},
        },
        "lineage_summary": {"founders": ["testunit-v01", "testunit-v02", "testunit-v03"], "n_nodes": 3, "n_edges": 0, "max_depth": 0},
        "lines": {"testunit-v01": line_active, "testunit-v02": line_ended, "testunit-v03": line_active_2},
        "experiment_events": [
            {
                "event_id": "EVT-00004", "timestamp": t0, "event_type": "media_prep",
                "operator": "TEST", "provenance": "reported", "scope": "facility",
                "params": {"reservoir_id": "testunit/LB-0", "volume_prepared": _quantity(1.0, "L")},
                "notes": "fixture media prep", "missing_fields": [],
            },
            {
                "event_id": "EVT-00005", "timestamp": t0, "event_type": "media_prep",
                "operator": "TEST", "provenance": "reported", "scope": "facility",
                "params": {"reservoir_id": "testunit/LB-5", "volume_prepared": _quantity(1.0, "L")},
                "notes": "fixture media prep", "missing_fields": [],
            },
            {
                "event_id": "EVT-00006", "timestamp": t_read, "event_type": "level_reading",
                "operator": "TEST", "provenance": "reported", "scope": "facility",
                "params": {"reservoir_id": "testunit/LB-0", "volume_remaining": _quantity(0.7, "L"),
                           "level_source": "measured"},
                "notes": "fixture level reading", "missing_fields": [],
            },
            {
                "event_id": "EVT-00007", "timestamp": t_read, "event_type": "level_reading",
                "operator": "TEST", "provenance": "reported", "scope": "facility",
                "params": {"reservoir_id": "testunit/LB-5", "volume_remaining": _quantity(0.85, "L"),
                           "level_source": "measured"},
                "notes": "fixture level reading", "missing_fields": [],
            },
        ],
    }


def build_fixture_repo() -> Path:
    """Create a fresh temp directory laid out like a checkout of the real
    repo (schema/, tools/lineage.py, evolution_log.json) and git-init it, so
    /health's `git rev-parse` has something real to report."""
    root = Path(tempfile.mkdtemp(prefix="evolution_log_server_test_"))
    (root / "schema").mkdir()
    (root / "tools").mkdir()

    shutil.copy(REAL_REPO / "schema" / "evolution_log.schema.json", root / "schema" / "evolution_log.schema.json")
    shutil.copy(REAL_REPO / "tools" / "lineage.py", root / "tools" / "lineage.py")
    shutil.copy(REAL_REPO / "tools" / "media.py", root / "tools" / "media.py")

    # importing tools/lineage.py leaves a __pycache__/ behind it -- matches
    # the real repo's own .gitignore so `git status` in the fixture stays
    # clean the same way it does in the real thing.
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")

    with open(root / "evolution_log.json", "w") as fh:
        json.dump(build_log(), fh, indent=2)

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=test@test", "-c", "user.name=test",
                    "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=test@test", "-c", "user.name=test",
                    "commit", "-q", "-m", "fixture"], cwd=root, check=True)
    return root


def build_evolver_unit_repo(config: dict | None = None) -> Path:
    """A throwaway directory laid out like one eVOLVER unit's own
    evolver_code checkout (a git repo containing experiment_parameters.yaml),
    for the /config routes -- deliberately separate from build_fixture_repo(),
    which stands in for LOG_REPO_PATH. Config writes must never touch the
    same repo evolution_log.json lives in (the operator's own instruction:
    "There is a local git repo in each evolver specific directory"), so
    tests need two independent throwaway repos, not one shared between both
    concerns."""
    import yaml as _yaml

    root = Path(tempfile.mkdtemp(prefix="evolver_unit_test_"))
    if config is not None:
        with open(root / "experiment_parameters.yaml", "w") as fh:
            _yaml.safe_dump(config, fh, default_flow_style=False, sort_keys=False)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=test@test", "-c", "user.name=test",
                    "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=test@test", "-c", "user.name=test",
                    "commit", "-q", "--allow-empty", "-m", "evolver unit fixture"], cwd=root, check=True)
    return root


def build_real_clone() -> Path:
    """A throwaway repo holding REAL data: reference/or05_log.json (a frozen
    copy of the OR05 experiment's log) as its evolution_log.json, with this
    repo's schema and tools beside it -- for a test that genuinely needs an
    actual reservoir's actual history. Never the live log: this repo's own
    evolution_log.json belongs to whatever experiment it was initialised
    for, and may not exist at all yet. Writes through it are fully isolated."""
    root = Path(tempfile.mkdtemp(prefix="evolution_log_server_realclone_"))
    shutil.copytree(REAL_REPO / "schema", root / "schema")
    shutil.copytree(REAL_REPO / "tools", root / "tools",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(REAL_REPO / "reference" / "or05_log.json", root / "evolution_log.json")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=test@test", "-c", "user.name=test",
                    "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=test@test", "-c", "user.name=test",
                    "commit", "-q", "-m", "reference clone"], cwd=root, check=True)
    return root
