/* Extract the viewer's pure derivation logic and exercise it against the real
   log and a synthetic branched fixture. Run with: node test_viewer.js */
const fs = require("fs"), path = require("path");
const ROOT = path.dirname(__dirname);

const HTML = path.join(ROOT, "viewer.html");
// The frozen reference log -- this repo's own may be new and nearly empty.
const REAL = path.join(ROOT, "reference", "or05_log.json");
const FIX  = "/tmp/fixture.json";

let fails = 0;
const check = (cond, msg) => {
  console.log((cond ? "PASS  " : "FAIL  ") + msg);
  if (!cond) fails++;
};

/* ---- pull the <script> block and the pure section out of the viewer ---- */
const html = fs.readFileSync(HTML, "utf8");
const script = html.split(/<script>/)[1].split(/<\/script>/)[0];
check(script.length > 2000, "viewer script block extracted");

const start = script.indexOf("function gL(");
const end = script.indexOf("/* ---------------------------------------------------------------- filters */");
check(start > 0 && end > start, "pure derivation section located");
const pure = script.slice(start, end) +
  "\nmodule.exports = {derive, gL, fmt, T, cmpId, facilityGroups, placeLabels};";

const H = 3600e3;
const mod = {exports:{}};
new Function("module", "H", pure)(mod, H);
const {derive, facilityGroups, placeLabels, T} = mod.exports;

/* ---- syntax check of the entire browser script ---- */
try { new Function("document","window","fetch","setInterval","Date", script.replace(/^\s*"use strict";/, "")); check(true, "full viewer script parses as valid JS"); }
catch (e) { check(false, "full viewer script parses as valid JS -- " + e.message); }

/* ---- alternating_selection ribbons ----
   altselBands lives beside the live-poll helpers rather than in the pure
   section above, so it is lifted out on its own. It is the whole of the
   ribbon's arithmetic: controller hours -> wall clock -> clipped to one
   line's life. */
const bandSrc = script.match(/function altselBands[\s\S]*?\n}/);
check(!!bandSrc, "altselBands extracted from the viewer");
if (bandSrc){
  const bm = {exports:{}};
  new Function("module", "H", bandSrc[0] + "\nmodule.exports = {altselBands};")(bm, H);
  const {altselBands} = bm.exports;
  const t0ms = T("2026-09-24T12:00:00-04:00") - 10*H;      // elapsed_h = 10
  const asel = {t0ms, spans:[[0,5,"HIGH"],[5,9,"LOW"],[9,10,"HIGH"]]};
  const start = t0ms + 2*H, end = t0ms + 8*H;              // line lived hours 2..8

  const b = altselBands(asel, start, end);
  check(b.length === 2, "spans outside the line's life are dropped (" + b.length + ")");
  check(b[0].t0 === start && b[1].t1 === end,
        "and the surviving ones are CLIPPED to it -- a vial's state log describes " +
        "the vial, which may have held another culture before and after this line");
  check(b[0].state === "HIGH" && b[1].state === "LOW", "the state rides along");
  check(altselBands({spans:[[0,1,"HIGH"]]}, 0, 1e12).length === 0,
        "no t0ms means no anchor to the wall clock, so nothing is drawn rather " +
        "than everything drawn at the epoch");
  check(altselBands({t0ms, spans:[[20,21,"HIGH"]]}, start, end).length === 0,
        "a span wholly after the line ends draws nothing");
  check(altselBands(null, start, end).length === 0, "a vial with no altsel state is quiet");
}

/* ================================ real log ================================ */
/* These assert invariants that must hold at any point in the run, not the
   counts of any one day, so they stay meaningful as the log grows. */
const real = JSON.parse(fs.readFileSync(REAL, "utf8"));
const M = derive(real);
const nLines = Object.keys(real.lines).length;

check(M.order.length === nLines, `real log: every line rendered (${M.order.length}/${nLines})`);
check(new Set(M.order).size === M.order.length, "real log: no duplicate rows");
check(M.order.length === real.lineage_summary.n_nodes, "real log: row count matches lineage_summary.n_nodes");
check(M.edges.length === real.lineage_summary.n_edges, "real log: edge count matches lineage_summary.n_edges");

/* distinct event_ids across per-line events AND facility events */
const ids = new Set();
for (const L of Object.values(real.lines)) for (const e of L.events) ids.add(e.event_id);
for (const e of (real.experiment_events||[])) ids.add(e.event_id);
check(M.nEvents === ids.size, `real log: counts every distinct event once (${M.nEvents}/${ids.size})`);
check(M.nEvents === real.log_meta.event_counter,
      `real log: viewer count matches log_meta.event_counter (${M.nEvents}/${real.log_meta.event_counter})`);
check(M.facility.length === (real.experiment_events||[]).length, "real log: facility events carried into the model");

check(Object.values(M.nodes).every(n => n.end > n.start), "real log: every line has positive duration");
check(M.tMax - M.tMin >= 6*H, "real log: time span padded to a renderable minimum");
check(Object.values(M.nodes).every(n => n.pg.length >= 1 && n.media.length >= 1),
      "real log: every line has at least one PG and one media segment");
check(Object.values(M.nodes).every(n => n.pg.every(s => s.t1 >= s.t0)),
      "real log: no PG segment runs backwards");
check(M.pgMax >= 5, `real log: PG axis covers the high reservoir (${M.pgMax})`);

/* terminated lines must stop at their termination time; active ones reach now */
for (const [id,n] of Object.entries(M.nodes)){
  const term = n.lin.terminated_at;
  if (term && n.end !== new Date(term).getTime()){
    check(false, `real log: ${id} terminated but lane does not end at termination time`);
  }
}
check(true, "real log: terminated lanes end at their termination timestamp");

/* the state the operator actually reported at 03:11 on 23 Aug */
const activeLB = Object.entries(M.nodes)
  .filter(([,n]) => n.L.status === "active" && (n.L.current_media||n.L.initial_media) === "LB");
const activeM9 = Object.entries(M.nodes)
  .filter(([,n]) => n.L.status === "active" && (n.L.current_media||n.L.initial_media) === "M9");
const rawActive = Object.values(real.lines).filter(L => L.status === "active");
const rawLB = rawActive.filter(L => (L.current_media||L.initial_media) === "LB").length;
const rawM9 = rawActive.filter(L => (L.current_media||L.initial_media) === "M9").length;
check(activeLB.length === rawLB, `real log: active LB lines match the log (${activeLB.length}/${rawLB})`);
check(activeM9.length === rawM9, `real log: active M9 lines match the log (${activeM9.length}/${rawM9})`);
check(activeLB.length + activeM9.length === rawActive.length, "real log: every active line is LB or M9");
/* A line's exposure floor must equal the PG of the low reservoir it actually
   draws from. Testing that invariant rather than a hardcoded number, since the
   floors move whenever media is reformulated. */
const resPG = Object.fromEntries(real.reservoirs.items.map(r => [r.id, r.pg.value_g_per_L]));
const floorOK = [...activeLB, ...activeM9].filter(([,n]) =>
  n.pg[n.pg.length-1].low !== resPG[n.L.reservoirs.low]);
check(floorOK.length === 0,
      "real log: every active line's floor matches its own low reservoir" +
      (floorOK.length ? " -- " + floorOK.map(([id,n]) =>
        `${id}: ${n.pg[n.pg.length-1].low} vs ${n.L.reservoirs.low}=${resPG[n.L.reservoirs.low]}`).join(", ") : ""));
check([...activeLB, ...activeM9].every(([,n]) =>
        n.pg[n.pg.length-1].high === resPG[n.L.reservoirs.high]),
      "real log: every active line's ceiling matches its own high reservoir");
check(["patrick-v04","patrick-v05","patrick-v06","patrick-v07"].every(i => M.nodes[i].terminated),
      "real log: all four flooded patrick LB lines are terminated");
check(["patrick-v05#2","patrick-v06#2"].every(i => M.nodes[i] && M.nodes[i].parents.length === 0),
      "real log: clean restarts are founders, not descendants of the flooded lines");
check(M.nodes["patrick-v05#2"].lin.occupies_vial_of === "patrick-v05",
      "real log: restart records hardware predecessor without a lineage edge");
/* A line gains an envelope segment on every reservoir reformulation, so assert
   the structure rather than a count that grows with the run. */
{
  const seg = M.nodes["plankton-v03"].pg;
  check(seg.length >= 2, `real log: plankton-v03 has ${seg.length} PG envelope segments`);
  check(seg.every((s,i,a) => i === 0 || a[i-1].t1 === s.t0),
        "real log: its envelope segments are contiguous");
  check(seg.every((s,i,a) => i === 0 || s.low !== a[i-1].low || s.high !== a[i-1].high),
        "real log: each segment differs from the one before, so none is a spurious split");
  check(seg[seg.length-1].low === resPG[M.nodes["plankton-v03"].L.reservoirs.low],
        "real log: its final segment matches its current low reservoir");
}
check(M.nodes["patrick-v05#2"].start > M.nodes["patrick-v05"].start,
      "real log: restart t0 is later than the founder it replaced");

/* the v9 -> v10 spike-ins: two parents each, parent v9 NOT terminated */
for (const unit of ["patrick","plankton"]){
  const c = `${unit}-v09+v10`, p1 = `${unit}-v09`, p2 = `${unit}-v10`;
  check(M.nodes[c] && M.nodes[c].parents.length === 2, `real log: ${c} has two parents`);
  check(M.edges.filter(e => e.to === c).every(e => e.merge), `real log: ${c} edges flagged as merges`);
  /* Being sampled must not itself end a line. It may still be terminated later
     for an unrelated reason, so test the reason rather than the state. */
  const p1term = (M.nodes[p1].evs || []).find(e => e.event_type === "termination");
  check(!p1term || !/merge|sampl/i.test((p1term.params||{}).termination_reason || ""),
        `real log: ${p1} was not ended by being sampled` +
        (p1term ? ` (ended by: ${(p1term.params||{}).termination_reason})` : " (still running)"));
  check(M.nodes[c].start >= M.nodes[p1].start, `real log: ${c} begins at or after ${p1}`);
  check(M.nodes[p2].terminated, `real log: ${p2} ends, folded into the merge`);
  check(idx0(M, p1) < idx0(M, c) && idx0(M, p2) < idx0(M, c),
        `real log: ${c} renders below both parents`);
}
function idx0(model, id){ return model.order.indexOf(id); }

/* ============================ branched fixture ============================ */
const fx = JSON.parse(fs.readFileSync(FIX, "utf8"));
check(new Set(Object.values(fx.lines).flatMap(L => L.events.map(e => e.event_id))).size
      === Object.values(fx.lines).reduce((a,L) => a + L.events.length, 0) - 0,
      "fixture: no event_id collisions introduced by the fixture builder");
const F = derive(fx);
const idx = Object.fromEntries(F.order.map((id,i) => [id,i]));

check(F.order.length === nLines + 3, `fixture: base lines + 2 split children + 1 merge child = ${nLines+3} (${F.order.length})`);
check(F.edges.length === M.edges.length + 4,
      `fixture: baseline edges + 4 = ${M.edges.length+4} (${F.edges.length})`);
check(new Set(F.order).size === F.order.length, "fixture: no duplicate rows");
check(F.order.length === Object.keys(fx.lines).length, "fixture: every line appears exactly once");

/* every child must render below all of its parents */
let ok = true, bad = [];
for (const e of F.edges) if (!(idx[e.from] < idx[e.to])) { ok = false; bad.push(`${e.from}->${e.to}`); }
check(ok, "fixture: children ordered after all parents" + (ok ? "" : " -- " + bad.join(", ")));

check(idx["patrick-v04.a"] === idx["patrick-v04"] + 1 && idx["patrick-v04.b"] === idx["patrick-v04"] + 2,
      "fixture: split children sit directly beneath their parent");

const merged = F.nodes["plankton-v03+v04"];
check(merged.parents.length === 2, "fixture: merge child has two parents");
check(F.edges.filter(e => e.to === "plankton-v03+v04").every(e => e.merge),
      "fixture: merge edges flagged as merges");
check(merged.depth === 1, `fixture: merge child depth 1 (${merged.depth})`);
check(new Set(merged.lin.roots).size === 2, "fixture: merge child descends from two founders");

const a = F.nodes["patrick-v04.a"];
check(a.depth === 1, "fixture: split child depth 1");
check(a.lin.roots[0] === "patrick-v04", "fixture: split child root is its founder");
check(F.nodes["patrick-v04"].terminated, "fixture: split parent marked terminated");
check(F.nodes["plankton-v03"].terminated && F.nodes["plankton-v04"].terminated,
      "fixture: merge parents marked terminated");

/* terminated lines must stop at their termination time, not run to now */
const p4 = F.nodes["patrick-v04"];
check(p4.end === new Date("2026-09-01T10:00:00-04:00").getTime(),
      "fixture: terminated line ends at its termination timestamp");
check(a.start === p4.end, "fixture: split child starts exactly where the parent ends");

/* PG segmentation */
const v5 = F.nodes["patrick-v05"];
check(v5.pg.length === 2, `fixture: pg_change splits the envelope into 2 segments (${v5.pg.length})`);
check(v5.pg[0].target === null && v5.pg[1].target === 0.15,
      "fixture: ramp target picked up from the pg_change event");
check(v5.pg[0].t1 === v5.pg[1].t0, "fixture: PG segments are contiguous");
check(a.pg[0].target === 0.2 && F.nodes["patrick-v04.b"].pg[0].target === 0.1,
      "fixture: split children carry their own ramp targets");

/* media segmentation */
const p3 = F.nodes["plankton-v03"];
check(p3.media.length === 2 && p3.media[0].media === "LB" && p3.media[1].media === "M9",
      "fixture: media_switch produces LB then M9 segments");
check(p3.media[0].t1 === p3.media[1].t0, "fixture: media segments are contiguous");
check(F.nodes["patrick-v05"].media.length === 1, "fixture: constant-mode line keeps one media segment");

const baseMerges = real.lineage_summary.n_merges, baseSplits = real.lineage_summary.n_splits;
check(F.summary.n_splits === baseSplits + 1 && F.summary.n_merges === baseMerges + 1,
      `fixture: adds exactly 1 split and 1 merge to the baseline `+
      `(${F.summary.n_splits}/${F.summary.n_merges} vs ${baseSplits}/${baseMerges})`);
check(F.summary.n_edges === F.edges.length, "fixture: summary edge count matches derived edges");
check(F.pgMax === 5, "fixture: PG axis still bounded by the high reservoir");

/* ---- an event shared across two lines must be counted exactly once ---- */
const dupLog = JSON.parse(JSON.stringify(fx));
const shared = dupLog.lines["patrick-v04"].events.find(e => e.event_type === "split");
const before = derive(dupLog).nEvents;
dupLog.lines["patrick-v04.a"].events.push(JSON.parse(JSON.stringify(shared)));
dupLog.lines["patrick-v04.b"].events.push(JSON.parse(JSON.stringify(shared)));
check(derive(dupLog).nEvents === before,
      "shared event written into three lines is counted once, not three times");

/* ---- cycle safety: a self-referential parent must not hang the viewer ---- */
const badLog = JSON.parse(JSON.stringify(fx));
badLog.lines["patrick-v06"].lineage.parents = ["patrick-v06"];
try {
  const B = derive(badLog);
  check(B.order.length === Object.keys(badLog.lines).length,
        "cycle: self-parenting line still renders exactly once (no hang, no drop)");
} catch (e) { check(false, "cycle: derive threw -- " + e.message); }


/* ---- ramp history: per-vial, backfilled, and clipped to the line's life ---- */
{
  const withRamp = Object.values(M.nodes).filter(n => n.L.ramp);
  check(withRamp.length === Object.keys(real.lines).length, "ramp: every line carries a ramp history");
  check(withRamp.every(n => n.ramp.length >= 1), "ramp: every line derives at least one ramp segment");
  check(withRamp.every(n => n.ramp.every(s => s.t0 >= n.start - 1 && s.t1 <= n.end + 1)),
        "ramp: segments stay inside the line's own lifetime");
  check(withRamp.every(n => n.ramp.every((s,i,a) => i === 0 || a[i-1].t1 === s.t0)),
        "ramp: segments are contiguous");
  const changeT0 = new Date("2026-08-24T09:00:00-04:00").getTime();
  const active = withRamp.filter(n => n.L.status === "active");
  check(active.every(n => n.ramp[n.ramp.length-1].step === 0.1),
        "ramp: every active line is on 0.1 g/L now");

  /* A line created after the change never ran at 0.05 and must not be
     backfilled to it -- that would invent history it did not have. */
  const preChange = active.filter(n => n.start < changeT0);
  const postChange = active.filter(n => n.start >= changeT0);
  check(preChange.every(n => n.ramp[0].step === 0.05),
        `ramp: lines predating the change were backfilled to 0.05 (${preChange.length})`);
  check(postChange.every(n => n.ramp.every(s => s.step === 0.1)),
        `ramp: lines created after it start on 0.1 and never claim 0.05 (${postChange.length})`);

  const ended = withRamp.filter(n => n.L.status !== "active");
  check(ended.every(n => n.end <= changeT0 ? n.ramp.every(s => s.step === 0.05) : true),
        "ramp: lines that ended before the change never got 0.1");
  check(M.rampMax === 0.1, `ramp: rampMax is the largest step in the log (${M.rampMax})`);
  check(preChange.every(n => n.ramp.some(s => s.t0 === changeT0)),
        "ramp: every line that predates the change transitions exactly at it");
}

/* ---- live-lineage scope: hides finished lineages without losing edges ---- */
{
  const live = M.order.filter(id => M.nodes[id].inLiveLineage);
  const dead = M.order.filter(id => !M.nodes[id].inLiveLineage);
  check(Object.values(M.nodes).every(n => n.L.status !== "active" || n.inLiveLineage),
        "scope: every active line is in a live lineage");
  check(dead.every(id => M.nodes[id].L.status !== "active"),
        "scope: nothing active is ever hidden");

  /* the point of the definition: no edge may straddle the visible boundary */
  const straddling = M.edges.filter(e =>
    M.nodes[e.to].inLiveLineage && !M.nodes[e.from].inLiveLineage);
  check(straddling.length === 0,
        "scope: no edge dangles -- terminated ancestors of live lines stay visible" +
        (straddling.length ? " -- " + straddling.map(e => e.from+"->"+e.to).join(", ") : ""));

  /* Derived rather than named: any terminated line with a living descendant
     must survive the filter, and any whose whole subtree is dead must not.
     Naming specific lines here broke as soon as a lineage finished. */
  const hasLiveDesc = id => {
    const seen = new Set(), stack = [id];
    while (stack.length){
      const cur = stack.pop();
      if (seen.has(cur)) continue;
      seen.add(cur);
      if (cur !== id && M.nodes[cur] && M.nodes[cur].L.status === "active") return true;
      for (const c of (M.nodes[cur] ? M.nodes[cur].children : [])) stack.push(c);
    }
    return false;
  };
  const ended = M.order.filter(id => M.nodes[id].L.status !== "active");
  const keptWrong = ended.filter(id => hasLiveDesc(id) && !M.nodes[id].inLiveLineage);
  const shownWrong = ended.filter(id => !hasLiveDesc(id) && M.nodes[id].inLiveLineage);
  check(keptWrong.length === 0,
        `scope: every ended line with a living descendant is kept (${ended.filter(hasLiveDesc).length} such)` +
        (keptWrong.length ? " -- dropped: " + keptWrong.join(", ") : ""));
  check(shownWrong.length === 0,
        `scope: every fully finished lineage is hidden (${ended.filter(id => !hasLiveDesc(id)).length} such)` +
        (shownWrong.length ? " -- shown: " + shownWrong.join(", ") : ""));
  check(live.length < M.order.length, `scope: live view is smaller than all (${live.length}/${M.order.length})`);
}

/* ---- live feed wiring: joins on (unit, vial), never touches the log ---- */
{
  const src = script;
  check(/fetch\(\s*["']viewer\.config\.json/.test(src), "live: viewer reads viewer.config.json");
  check(/api\/v1\/vials/.test(src), "live: viewer calls the vials endpoint");
  check(/LIVE\[`\$\{unit\}\/\$\{v\.vial\}`\]/.test(src), "live: keyed on unit/vial");
  check(/d\.evolver !== unit/.test(src),
        "live: rejects a url that answers for a different evolver");
  check(/n\.L\.status !== "active"/.test(src),
        "live: terminated lines get no live state, since their vial may hold another culture");
  check(!/LIVE|UNITS/.test(src.slice(src.indexOf("function derive"), src.indexOf("function cmpId"))),
        "live: derive() stays pure -- no live data leaks into the log model");
  check(!/localStorage\.setItem\(["']or05-live/.test(src), "live: nothing persisted");

  /* the config the operator actually filled in */
  const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, "viewer.config.json"), "utf8"));
  const logUnits = Object.keys(real.hardware.units);
  const cfgUnits = Object.keys(cfg.units);
  check(cfgUnits.every(u => logUnits.includes(u)),
        `live: every configured unit exists in the log (${cfgUnits.join(", ")})`);
  check(cfgUnits.every(u => /^https?:\/\/[^/]+/.test(cfg.units[u].url)),
        "live: every configured url is well formed");

  /* every active line must be addressable by the feed */
  const unreachable = Object.values(real.lines)
    .filter(L => L.status === "active" && !cfg.units[L.unit])
    .map(L => L.line_id);
  check(unreachable.length === 0,
        "live: every active line belongs to a configured unit" +
        (unreachable.length ? " -- orphaned: " + unreachable.join(", ") : ""));
}

/* ---- edges leave the parent at the transfer, not at the parent's end ---- */
{
  check(M.edges.every(e => e.at !== undefined), "edge: every edge carries a branch time");

  for (const e of M.edges){
    const par = M.nodes[e.from], kid = M.nodes[e.to];
    check(e.at >= par.start - 1 && e.at <= par.end + 1,
          `edge: ${e.from}->${e.to} departs inside the parent's own span`);
    check(Math.abs(e.at - kid.start) < 1000 || e.at === par.end,
          `edge: ${e.from}->${e.to} departs at the child's creation (${new Date(e.at).toISOString()})`);
  }

  /* the case that was wrong: a parent still running long after the branch.
     The edge must leave at the branch, not at 'now'. */
  const live = M.edges.filter(e => M.nodes[e.from].L.status === "active");
  check(live.length > 0, `edge: ${live.length} edges have a parent still running`);
  const trailing = live.filter(e => Math.abs(e.at - M.nodes[e.from].end) < 60*1000);
  check(trailing.length === 0,
        "edge: no edge from a live parent departs at the parent's end" +
        (trailing.length ? " -- " + trailing.map(e => `${e.from}->${e.to}`).join(", ") : ""));

  /* and a branch must be strictly earlier than the parent's current end */
  for (const e of live){
    check(e.at < M.nodes[e.from].end,
          `edge: ${e.from}->${e.to} branch precedes the parent's present`);
  }
}
/* ======================= facility axis labels ==============================
   A level round writes one facility event per reservoir at a single instant.
   The axis drew a marker and a label per EVENT, so those instants stacked up to
   ten labels on one pixel and the axis became unreadable. These guard the fix,
   and in particular guard the thing a fix like this can quietly break: making
   the display legible by losing events. */
{
  const facility = real.experiment_events || [];
  const groups = facilityGroups(facility, T);

  check(groups.length < facility.length,
        `axis: ${facility.length} facility events collapse to ${groups.length} instants`);
  check(groups.reduce((a,g) => a + g.events.length, 0) === facility.length,
        "axis: grouping keeps every facility event -- none dropped");
  check(new Set(groups.map(g => g.timestamp)).size === groups.length,
        "axis: one group per distinct timestamp");
  check(groups.every((g,i,a) => i === 0 || a[i-1].t <= g.t),
        "axis: groups are in time order");

  const worst = groups.reduce((m,g) => Math.max(m, g.events.length), 0);
  check(worst > 1, `axis: the real log really does stack events at one instant (worst ${worst})`);

  /* label wording */
  const multi = groups.find(g => g.events.length > 1 &&
                                 new Set(g.events.map(e => e.event_type)).size === 1);
  if (multi) check(/^\S+ ×\d+$/.test(multi.label),
                   `axis: a uniform cluster is labelled "${multi.label}"`);
  const mixed = groups.find(g => new Set(g.events.map(e => e.event_type)).size > 1);
  if (mixed) check(/^\d+ events$/.test(mixed.label),
                   `axis: a mixed cluster is labelled "${mixed.label}"`);
  const single = groups.find(g => g.events.length === 1);
  if (single) check(single.label === single.events[0].event_type,
                    "axis: a lone event keeps its plain type name");

  /* collision suppression: labels that survive must not overlap each other */
  for (const pxPerHour of [2, 8, 34, 120]){
    const tMin = Math.min(...groups.map(g => g.t));
    const xOf = t => ((t - tMin) / 3600e3) * pxPerHour + 14;
    const placed = placeLabels(facilityGroups(facility, T), xOf);
    const shown = placed.filter(g => g.show);
    let ok = true;
    for (let i = 1; i < shown.length; i++){
      if (shown[i].x < shown[i-1].x + shown[i-1].label.length * 6.0) ok = false;
    }
    check(ok, `axis: at ${pxPerHour} px/h no two drawn labels overlap (${shown.length}/${placed.length} drawn)`);
    check(placed.every(g => g.events.length),
          `axis: at ${pxPerHour} px/h every marker still carries its events`);
  }

  /* zoomed far in, everything should fit; far out, almost nothing should */
  const tMin = Math.min(...groups.map(g => g.t));
  const wide = placeLabels(facilityGroups(facility, T), t => ((t-tMin)/3600e3)*400 + 14);
  const tight = placeLabels(facilityGroups(facility, T), t => ((t-tMin)/3600e3)*0.5 + 14);
  check(wide.filter(g => g.show).length > tight.filter(g => g.show).length,
        "axis: zooming in reveals more labels than zooming out");
  check(tight.filter(g => g.show).length >= 1,
        "axis: even fully zoomed out at least one label survives");
}

console.log(`\nRESULT: ${fails ? "PROBLEMS" : "OK"} (${fails} checks failed)`);
process.exit(fails ? 1 : 0);
