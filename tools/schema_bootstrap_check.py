#!/usr/bin/env python3
"""Schema-check a log that may legitimately have no lines yet.

    ~/py/bin/python tools/schema_bootstrap_check.py [evolution_log.json]

The schema requires `lines` to be non-empty, which a brand-new experiment
cannot satisfy: cultures arrive through POST /lines, after the log exists.
That single error is expected at scaffold time and clears itself on the first
line created -- app/writer.py validates the whole CANDIDATE, which does
contain the new line. Every other schema error is real.

So this exits 0 when the only complaint is the empty `lines`, and 1 otherwise,
printing whatever it found either way. validate_schema.py stays the tool for a
log with an experiment in it; this one is only for the gap before that.
"""
import json
import sys

try:
    import jsonschema
except ImportError:
    sys.exit("jsonschema is not installed in this interpreter -- see CLAUDE.md")

log_path = sys.argv[1] if len(sys.argv) > 1 else "evolution_log.json"
schema_path = sys.argv[2] if len(sys.argv) > 2 else "schema/evolution_log.schema.json"

with open(log_path) as fh:
    log = json.load(fh)
with open(schema_path) as fh:
    schema = json.load(fh)

def expected_at_bootstrap(err):
    # ONLY "lines is empty", and only when it actually is. Matching every
    # error whose path is `lines` would wave through a real complaint about
    # the lines object itself (wrong type, say) as if it were the bootstrap
    # gap -- a filter that passes something real, which is this project's
    # characteristic failure: a confident pass, not a crash.
    return (list(err.absolute_path) == ["lines"]
            and err.validator == "minProperties"
            and log.get("lines") == {})


validator = jsonschema.Draft202012Validator(schema)
expected, other = [], []
for err in validator.iter_errors(log):
    text = "%s: %s" % ("/".join(str(p) for p in err.absolute_path), err.message)
    (expected if expected_at_bootstrap(err) else other).append(text)
errors = expected + other

for e in other:
    print("   SCHEMA: %s" % e)
if expected:
    print("   (expected until the first line is created: %s)" % expected[0])
if not errors:
    print("   schema: clean")
sys.exit(1 if other else 0)
