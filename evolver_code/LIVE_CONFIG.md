# Live config reload — operator reference

`custom_script.py`'s `pumpcontrol_ramp` re-reads `experiment_parameters.yaml`
once per event cycle and can apply a few settings to a **running** experiment,
with no eVOLVER restart.

Everything here is about `pumpcontrol_ramp`. Other operation modes are
unaffected: they never call the reload, and a config whose `operation.mode` is
anything else is read, recognised as out of scope, and left alone.

---

## What is live, and what is not

| Live, applies within one cycle | Needs a controller restart |
|---|---|
| `target_ramp` | `low_concentration`, `high_concentration` |
| `setpoint` | `volume`, `input_pump2`, `to_run` |
| `interval` | `initial_concentration`, `exp_name`, `temperature` |
| `number_consecutive_intervals` | everything else |

The live list is `LIVE_FIELDS` in `config_validation.py`. Adding to it is one
line, but read the note there first — each exclusion has a reason.

**Reservoir concentrations are deliberately excluded.** They describe physical
bottles. Changing the number without pouring anything would silently
reinterpret every subsequent dose, and a media change is a real bench event that
belongs in `evolution_log.json`, not in a hot-swapped yaml value.

**`to_run` is excluded and this one can mislead you.** Flipping it live is
accepted and its four dependent live fields *are* applied and logged, but
`settings.vials_to_run` is not live — so the vial keeps being dosed (or keeps
being skipped) while `config_changes.txt` suggests otherwise. Restart to start
or stop a vial.

---

## What you will see on stdout

| Situation | Output |
|---|---|
| File untouched | nothing |
| Touched, valid, no live field differs | `re-read …: valid, no live field changed. Live fields are …` |
| A live field changed | `[config:live] vial 4 target_ramp: 0.1 -> 0.25`, one line per change |
| Config rejected | `REFUSING to apply … N problem(s):` then each problem |
| Unreadable or malformed | `could not reload … keeping the settings already in force` |
| `operation.mode` not pumpcontrol_ramp | `not applied -- …`, once per distinct message |

These go to **stdout, not the eVOLVER log file**, so capture it (`screen`,
`tmux`, `| tee`) if you want an overnight record. Applied changes are separately
durable in `<exp_dir>/<exp_name>/config_changes.txt` as
`time,field,vial,old,new`. Refusals are **not** persisted anywhere.

The "no live field changed" line only appears when the file was actually
touched, so it is a real answer to "did it see my edit?" and not per-cycle
noise. If you edit a non-live field, that is the line you get.

---

## Behaviour under failure

- **Nothing is ever applied partially.** The new values are computed and checked
  in full before a single one is written.
- **A rejected config changes nothing** and is re-reported every cycle until
  fixed, rather than being cached as "seen" and going quiet.
- **The audit row is written before the settings change.** If the log write
  fails (full disk, read-only mount, bad path) the reload is abandoned with
  nothing applied. A log row for a change that did not happen is a harmless
  discrepancy; a live dose change with no record is not.
- **The reload cannot raise into the control loop.** Every failure is caught and
  reported.

---

## Editing safely while an experiment runs

**Write to a temp file and `mv` it into place.** Rename is atomic, so the
controller never sees a half-written file:

```sh
cp experiment_parameters.yaml /tmp/ep.yaml
$EDITOR /tmp/ep.yaml
mv /tmp/ep.yaml experiment_parameters.yaml
```

This matters because of a known gap: a config truncated mid-save can still parse
as valid yaml describing a **smaller experiment**, and every vial it no longer
mentions is silently reset to defaults (`target_ramp` 0, `setpoint` 100,
`interval` 10000). Empirically 3.1% of byte-truncation points of a real 16-vial
config land there. Validation does not yet require all 16 vials to be present.
Atomic rename avoids the whole class.

**Keep the config on local disk.** On a network mount two things degrade: the
change detector is `(mtime, size)`, which misses a same-length edit when
timestamps are second-granular, and a hung mount can block the read
indefinitely — a case no exception handler can catch.

**Do not restore the config with `cp -p`, `rsync -a`, or `tar -x`** while the
run is live. Those preserve mtime, so a same-length change can go unnoticed.
Touch the file afterwards if you must.

---

## Ranges enforced on live values

Checked on the raw yaml value, before any type conversion, so a cast cannot
launder an out-of-range number into range:

| field | allowed | note |
|---|---|---|
| `target_ramp` | 0 – 2.0 | 2.0 delivers ~+1.6 g/L in ONE cycle at ch=5. Nominal is 0.1 |
| `setpoint` | 0 – 1e7 | raw sensor units |
| `interval` | 0 – 1e6 | hours |
| `number_consecutive_intervals` | 0 – 1e6 | must be whole; 0 means the gate is disabled and the vial may ramp every cycle |

These bounds are wide enough to accept values that are legal but operationally
extreme. They are a guard against a corrupted file, not a substitute for
knowing what you are setting.

---

## Known gaps, not yet fixed

Recorded so they are not rediscovered by accident. All were found by adversarial
testing on 2026-09-01; none affects the config currently in production.

1. Validation does not require all 16 vials, so a truncated-but-parseable config
   resets the vials it omits. Mitigate with atomic rename (above).
2. `high_concentration`, `low_concentration`, `volume` and
   `initial_concentration` have no upper bound. `high_concentration: 500` is
   accepted and would deliver ~+9.6 g/L per cycle.
3. `number_consecutive_intervals: .inf` passes `validate_config`, so the log
   server would commit such a file. The live path refuses it, but a controller
   restart on it fails at import.
4. Duplicate yaml keys, and a duplicated `experiment_settings` or
   `per_vial_settings` block, are resolved silently last-wins by the parser. A
   file with a stale block reads correctly to a human and runs from the last one.
5. A config path that is a named pipe, or on a hung mount, blocks the read
   forever. Nothing catches it.
6. `stepsize()` at the two logging call sites omits `vialvolume`, defaulting to
   22 while the solver receives `settings.volume[x]`. Any vial not at 22 mL
   makes `drugconc` drift from the true concentration (0.47 g/L per cycle at
   15 mL). Pre-dates this feature.
