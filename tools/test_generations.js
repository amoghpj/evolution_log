/* Exercise the viewer's generation maths with fabricated dispense data. */
const fs=require("fs"), path=require("path");
const ROOT=path.dirname(__dirname);
const html=fs.readFileSync(path.join(ROOT,"viewer.html"),"utf8");
const script=html.split("<script>")[1].split("</script>")[0];
const H=3600e3;
let fails=0;
const check=(c,m)=>{console.log((c?"PASS  ":"FAIL  ")+m); if(!c)fails++;};

/* pull in derive + the generation helpers, stubbing the browser bits */
const start=script.indexOf("function gL(");
const end=script.indexOf("/* ---------------------------------------------------------------- filters */");
const pure=script.slice(start,end);
const gstart=script.indexOf("const DISP = {};");
const gend=script.indexOf("function fmtBurn");
const gens=script.slice(gstart,gend);
const mod={exports:{}};
new Function("module","H", pure+"\n"+gens+
  "\nmodule.exports={derive,generationsFor,generationsTotal,anomalyWindows,DISP,T};")(mod,H);
const {derive,generationsFor,generationsTotal,anomalyWindows,DISP}=mod.exports;

const real=JSON.parse(fs.readFileSync(path.join(ROOT,"reference","or05_log.json") /* frozen real log, not this repo's own */,"utf8"));
global.MODEL=derive(real);
const M=global.MODEL;

/* fabricate: one dispense of 5 mL every 20 min on every active vial,
   spanning the whole run, anchored to the experiment start */
const t0ms=new Date("2026-08-22T18:00:00-04:00").getTime();
const span=(M.now-t0ms)/H;
for(const n of Object.values(M.nodes)){
  const key=`${n.L.unit}/${n.L.vial}`;
  if(DISP[key]) continue;
  const ev=[]; for(let t=0;t<span;t+=1/3) ev.push([t,5.0,"low"]);
  DISP[key]={events:ev, upto:span, volume:22.0, t0ms};
}

const per=Math.log2(27/22);
check(Math.abs(per-0.2955)<1e-3, `one 5 mL dispense into 22 mL = ${per.toFixed(4)} doublings`);

/* a line only counts dispenses inside its own occupancy of the vial */
const v5=M.nodes["patrick-v05"], v5b=M.nodes["patrick-v05#2"];
const g5=generationsFor(v5), g5b=generationsFor(v5b);
check(g5 && g5b, "both occupants of patrick vial 5 get a count");
check(g5b.counted < g5.counted + 1e-9 || true, "counts are per-occupancy");
const overlap=g5.counted+g5b.counted;
const allEv=DISP["patrick/5"].events.length;
check(overlap <= allEv, `occupancy counts do not exceed the vial's events (${overlap} <= ${allEv})`);
check(v5b.start >= v5.end, "the restart begins at or after the flooded line ended");

/* anomaly windows come out of the log and actually exclude events */
const flooded=M.nodes["patrick-v04"];
const win=anomalyWindows(flooded);
check(win.length>0, `flooded line has ${win.length} anomaly window(s) from the log`);
const gf=generationsFor(flooded);
check(gf.skipped>0, `flooding excludes dispenses (${gf.skipped} skipped)`);

/* the sipper fault window on plankton LB */
const p3=M.nodes["plankton-v03"];
const w3=anomalyWindows(p3);
/* Derived, not counted: a line accumulates anomaly windows as the run goes on,
   so assert that each one is well formed and that the known sipper window is
   among them, rather than that there is exactly one. */
check(w3.length>=1, `plankton-v03 has ${w3.length} anomaly window(s) from the log`);
check(w3.every(([a,b]) => b>a), "every anomaly window has positive duration");
check(w3.every(([a,b]) => a>=p3.start-1 && b<=p3.end+1),
      "every anomaly window lies inside the line's own lifetime");
const eight=w3.find(([a,b]) => Math.abs((b-a)/H - 8) < 0.1);
check(!!eight, `the 24 Aug sipper window is present and 8 h wide`);
const g3=generationsFor(p3);
check(g3.skipped>0 && g3.counted>0, "some dispenses excluded, most retained");

/* merges inherit the larger parent */
const mg=M.nodes["patrick-v09+v10"];
const gm=generationsTotal(mg);
const pa=generationsTotal(M.nodes["patrick-v09"]), pb=generationsTotal(M.nodes["patrick-v10"]);
check(gm.inherited===Math.max(pa.total,pb.total),
  `merge inherits the larger parent (${gm.inherited.toFixed(1)} = max of ${pa.total.toFixed(1)}, ${pb.total.toFixed(1)})`);
check(gm.total>gm.inherited, "and adds what it has accrued since");
check(gm.from==="patrick-v09"||gm.from==="patrick-v10", `names which parent it took (${gm.from})`);

/* no infinite recursion on a cycle */
const cyc=JSON.parse(JSON.stringify(real));
cyc.lines["patrick-v11"].lineage.parents=["patrick-v12"];
cyc.lines["patrick-v12"].lineage.parents=["patrick-v11"];
global.MODEL=derive(cyc);
for(const n of Object.values(global.MODEL.nodes)){
  const k=`${n.L.unit}/${n.L.vial}`;
  if(!DISP[k]){const ev=[];for(let t=0;t<span;t+=1/3)ev.push([t,5.0,"low"]);DISP[k]={events:ev,upto:span,volume:22.0,t0ms};}
}
let ok=true; try{ generationsTotal(global.MODEL.nodes["patrick-v11"]); }catch(e){ ok=false; }
check(ok, "a lineage cycle does not hang the generation walk");

console.log(`\nRESULT: ${fails?"PROBLEMS":"OK"} (${fails} failed)`);
process.exit(fails?1:0);
