# `GET /media` — media consumption and depletion projections

**Status: implemented 2026-08-28** (`app/routes/media.py`, `app/skill.py`,
`app/config.py`, `app/log_repo.py`; tests in `tests/test_media.py`). All 6
acceptance checks in §8 verified, including against the real log at commit
`b03bfb0`. One finding along the way, not in this spec: `tools/media.py`'s
`analyse()` can produce `rate_basis: "upper_bound"` without setting
`rate_is_upper_bound: true` to match (confirmed by constructing a fixture --
0 real rows have hit this path yet, which is exactly why
`tools/test_media.py` never caught it). Not fixed in `media.py` per this
spec's own instruction; corrected in the server layer instead
(`_fix_upper_bound_inconsistency` in `app/routes/media.py`), since shipping
those two fields inconsistent is exactly the failure §6 below warns about.
The rest of this file is kept as-written, as the implementation record —
which means **§5's response shape is now two keys short**. The route returns
`{at, skipped_events, reservoirs, per_line, delivered_pg, high_outlook,
attention, pump}`: `skipped_events` came from a real production 500 (see
README), and `pump` is the live pump-derived measurement added by
`issues/ISSUE_004.md`. §6's rule — never return a depletion figure without its
provenance — applies to the `pump` block too, and that block carries its own
`basis`, its own bound flag, and its own reason when absent.

Implementation spec. Exposes the log repo's `tools/media.py` over the API,
the same way `app/log_repo.py` already exposes `tools/lineage.py`.

**Do not reimplement any of the consumption logic here.** Every rate, basis
and forecast comes from `media.py` in the log repo. This server shapes the
output and nothing else. The reasoning behind each rule in `media.py` is in
the log repo's `LOG_PROTOCOL.md`; several of them exist because a previous
version reported a confident wrong number.

---

## 1. What already works in your favour

`analyse()`, `dose_estimates()` and `high_media_outlook()` are **pure** —
they return data and print nothing. All printing lives in `report()` and
`main()`, behind a `__main__` guard, so importing `media.py` has no side
effects. No changes to `media.py` are needed, and none should be made: it is
covered by `tools/test_media.py` in the log repo, whose tests each exist
because the corresponding bug shipped once.

---

## 2. Wire up the import

In `app/config.py`, add `media_tool` beside `lineage_tool`:

```python
self.media_tool = self.log_repo_path / "tools" / "media.py"
```

and include it in the existing `missing` check, so a wrong `LOG_REPO_PATH`
fails at startup rather than on the first request.

In `app/log_repo.py`, add a `media_module(settings)` mirroring
`lineage_module(settings)` — same `importlib` + `lru_cache`-on-path pattern,
for the same reason: the server must never carry a second, driftable copy of
logic the log repo owns.

---

## 3. The one snag

`analyse()` returns `(rows, perline)`. **`perline` is keyed by `(media,
role)` tuples and will not JSON-serialise.** `rows`, `doses` and `outlook`
serialise as-is.

Flatten it **in this server**, not in `media.py` — changing that return
shape would break `tools/test_media.py` and the CLI:

```python
per_line = [{"media": m, "role": r, "rate_L_per_h": v}
            for (m, r), v in perline.items()]
```

---

## 4. What `at` means

`analyse(log, at)` projects levels forward from the last reading to `at`.
The CLI passes `log_meta.last_updated`.

- Default `at` to **request time**, not `last_updated` — otherwise every
  "empty in" figure freezes at the last time someone logged something.
- Accept `?at=<ISO timestamp with offset>` to override, so a result can be
  reproduced exactly.
- **Always echo `at` in the response.** A projection must never travel
  without the instant it was projected to.

Note this means the same request returns different numbers over time, and
will not match a `media.py` CLI run unless `?at=` is passed. That is
intended; say so in the route summary.

---

## 5. Response shape

```
GET /media[?at=<ts>][&unit=<patrick|plankton>][&status=active]

{ "at": "...",
  "reservoirs":  [ ...rows from analyse(), unmodified... ],
  "per_line":    [ {media, role, rate_L_per_h} ],
  "delivered_pg":[ ...dose_estimates()... ],
  "high_outlook":[ ...high_media_outlook()... ],
  "attention":   [ {reservoir_id, hours_remaining, rate_basis, message} ] }
```

Pass `rows` through **whole**. Do not select a "useful subset" of fields —
see §6.

`attention` mirrors the CLI's closing block: reservoirs projected to empty
soonest. Every entry carries `rate_basis`.

---

## 6. The rule that matters most

**Never return a depletion figure without its provenance.** Each row from
`analyse()` carries:

| Field | Meaning |
|---|---|
| `rate_basis` | `measured` · `prior_bottle` · `inferred` · `upper_bound` |
| `rate_is_upper_bound` | the true rate is this or slower; time remaining is this or longer |
| `rate_provisional` | window under `MIN_SPAN_H` (2 h) — too short to trust |
| `baseline_orphaned` | the baseline could not be located; no rate may claim to be measured |
| `level_source` | `measured` vs `prepared` — whether anyone has actually looked at the bottle |

The CLI renders these as the `*`, `'`, `~`, `≤`, `?` footnote glyphs. An
endpoint that returns `{"empty_in_h": 16}` with these stripped rebuilds
precisely the failure this log exists to prevent: a confident number whose
basis is invisible. Keep all five fields on every row, and state in the
`/skill` text that a consumer must not present a depletion figure without
its `rate_basis`.

The live log demonstrates why, on a 50-minute timescale. At the 17:35 level
round all eight active reservoirs read `rate_basis: "measured"` — the first
time that had ever been true. The 18:25 M9 refill immediately knocked two of
them back:

```
measured      6   patrick/LB-1, LB-5, M9-5, plankton/LB-1, LB-5, M9-5
prior_bottle  2   patrick/M9-1, plankton/M9-1   (level_source: prepared)
```

Those two now report a rate carried over from their previous bottle and a
level that is the prepared volume nobody has looked at yet. A consumer
showing "16 h" for `plankton/M9-1` without that qualification is stating a
guess as a measurement.

---

## 7. Read-only

`GET /media` computes from the log; it writes nothing and needs no auth,
consistent with every other `GET` route. It must not update
`reservoirs[].current_volume` or any other stored field — those change only
by a logged `level_reading` or `media_prep` event.

---

## 8. Acceptance checks

Against the live log at log-repo commit `b03bfb0`:

1. `GET /media?at=2026-08-27T18:25:00-04:00` matches
   `python3 tools/media.py` run in the log repo, reservoir for reservoir, on
   `level_L`, `rate_L_per_h` and `rate_basis`.
2. Ten reservoir rows: 8 `active`, 2 `retired`.
3. Of the active rows, 6 read `rate_basis: "measured"` and 2 —
   `patrick/M9-1`, `plankton/M9-1` — read `prior_bottle` with
   `level_source: "prepared"`, following the 18:25 refill. Both counts, not
   just the total, or the check passes on an endpoint that has flattened the
   basis away.
4. `delivered_pg` returns four groups (patrick/plankton × LB/M9), every mean
   between its group's low and high reservoir concentrations.
5. Omitting `at` returns a *later* `at` and equal-or-smaller `level_L` than
   check 1. Never larger — a projection cannot refill a bottle.
6. A reservoir with `rate_basis: "upper_bound"` must also carry
   `rate_is_upper_bound: true`. Construct one with a fixture rather than
   waiting for the live log to produce it.

Check 6 needs a fixture: copy the log, strip the level readings after a
`media_prep` for one reservoir, and confirm the endpoint degrades to a bound
instead of returning a confident rate.
