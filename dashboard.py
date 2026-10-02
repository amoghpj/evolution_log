#!/usr/bin/env python3
"""
Live Dash dashboard for evolver experiment data.

Run from the same working directory as plot.py:
    python dashboard.py

Opens at http://localhost:8050.  Figures refresh automatically every 10 s
whenever any source CSV/txt file has been modified.
"""

import os
import threading
import traceback

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import dash
from dash import dcc, html, Input, Output, State, dash_table

from plot import load_config, load_data, TWINDOW

_MONO = "'Courier New', Courier, monospace"

# ─── setup constants ──────────────────────────────────────────────────────────

_EVOLVER_NAMES = ["spongebob", "gary", "patrick", "sandy", "plankton",
                  "pearl", "barnacleboy", "mermaidman", "squidward", "krabs"]
_IPDICT = {
    "spongebob": "192.168.1.3",  "gary":        "192.168.1.6",
    "patrick":   "192.168.1.4",  "sandy":       "192.168.1.5",
    "plankton":  "192.168.1.12", "mermaidman":  "169.254.51.129",
    "barnacleboy": "192.168.1.9","krabs":       "169.254.6.231",
    "pearl":     "169.254.219.11","squidward":  "169.254.195.106",
}
_REV_IPDICT = {v: k for k, v in _IPDICT.items()}

_OP_MODES = ["calibration", "growthcurve", "chemostat",
             "turbidostat", "morbidostat", "pumpcontrol_ramp",
             "alternating_selection"]

_PERVIAL_COLS = {
    "calibration":      ["vial","to_run","volume","calib_initial_od","calib_end_od","description"],
    "growthcurve":      ["vial","to_run","volume","calib_initial_od","calib_end_od","description"],
    "morbidostat":      ["vial","to_run","volume","morbidostat_setpoint","doubling_time",
                         "input_pump2","calib_initial_od","calib_end_od","description"],
    "turbidostat":      ["vial","to_run","volume","turbidostat_low","turbidostat_high",
                         "calib_initial_od","calib_end_od","description"],
    "chemostat":        ["vial","to_run","volume","chemo_rate","chemo_start_od",
                         "chemo_start_time","description"],
    ## These two lists are the per-vial fields custom_script.py's own branch
    ## READS -- config_validation.py's _REQUIRED_PER_VIAL_BY_MODE plus
    ## _ALWAYS_REQUIRED_PER_VIAL (volume, temperature), which are required for
    ## every vial whether or not it runs.
    ##
    ## pumpcontrol_ramp's list used to be morbidostat's leftovers: of the eight
    ## fields the mode requires it offered ONE (input_pump2), while offering
    ## four (morbidostat_setpoint, doubling_time, dilution_fraction,
    ## growthdelta) that its branch never reads. Saving from this tab therefore
    ## stripped the whole PG ramp out of a running config -- and handle_write
    ## does not validate, so nothing said so.
    "pumpcontrol_ramp": ["vial","to_run","volume","temperature","setpoint","interval",
                         "input_pump2","number_consecutive_intervals",
                         "initial_concentration","high_concentration",
                         "low_concentration","target_ramp","description"],
    ## The four trailing fields are optional -- Settings applies a defensible
    ## default for each rather than falling into one by accident -- but they are
    ## offered here because they are the knobs that actually shape the protocol,
    ## and an operator who cannot see them cannot change them.
    "alternating_selection": ["vial","to_run","volume","temperature","setpoint",
                              "input_pump2","initial_concentration",
                              "high_concentration","low_concentration",
                              "n_tolerant","n_dilutions","ramp","media_wait_time",
                              "initial_drug_target","growth_interval_multiplier",
                              "stress_wait_fraction","fold_dilution","description"],
}
## Defaults shown in the editor. For the fields custom_script.py would
## otherwise fall back on silently, these MATCH that fallback
## (config_validation.py's _SILENT_DEFAULTS), so the table shows what the
## controller would actually do rather than a different number the operator
## then has to notice.
_PERVIAL_DEFAULTS = {
    "vial": 0, "to_run": False, "volume": 20.0, "temperature": 37,
    "setpoint": 100.0, "interval": 10000.0, "number_consecutive_intervals": 1000,
    "initial_concentration": 0.0, "high_concentration": 0.0,
    "low_concentration": 0.0, "target_ramp": 0.0,
    "n_tolerant": 5, "n_dilutions": 6, "ramp": 0.5, "media_wait_time": 1.5,
    ## Left blank on purpose: Settings defaults it to initial_concentration,
    ## i.e. "start challenging at whatever is already in the vial", and a
    ## number typed here would silently override that coupling.
    "initial_drug_target": None,
    "growth_interval_multiplier": 3.0, "stress_wait_fraction": 0.9,
    "fold_dilution": 10.0,
    "morbidostat_setpoint": None, "doubling_time": None, "input_pump2": None,
    "turbidostat_low": 0.0, "turbidostat_high": 0.0,
    "calib_initial_od": 0.0, "calib_end_od": 0.0,
    "chemo_rate": 0.0, "chemo_start_od": 0.0, "chemo_start_time": 0.0,
    "dilution_fraction": 0.05, "growthdelta": 0.0001, "description": "",
}


## Per-vial columns that must be numbers. Everything custom_script.py feeds to
## float()/int() -- i.e. everything except the three that are genuinely not
## numeric. Used both to type the grid's columns and to refuse a write whose
## cells cannot be parsed.
_PERVIAL_TEXT = {"description"}
_PERVIAL_BOOL = {"to_run"}


def _pervial_numeric(mode):
    return [c for c in _PERVIAL_COLS.get(mode, _PERVIAL_COLS["calibration"])
            if c not in _PERVIAL_TEXT and c not in _PERVIAL_BOOL]


def _coerce_pervial(rows, mode):
    """(clean_rows, type_errors). Numbers as numbers, or a precise complaint.

    The DataTable hands every cell back as a STRING, and handle_write used to
    pass them through untouched -- so `n_tolerant: five` and `media_wait_time:
    ''` were written to the yaml verbatim, where custom_script.py's own
    float() raises on load. Worse for a live edit: refresh_live_settings never
    raises and keeps the settings already in force, so the file changed, the
    form said "Config written", and the controller silently went on using the
    old value.

    Only TYPES are checked here. A missing or out-of-range field is the
    validator's business, not the grid's, and blocking on it would stop an
    operator saving a config they intend to finish.
    """
    numeric = set(_pervial_numeric(mode))
    clean, errors = [], []
    for row in (rows or []):
        out = {}
        for k, v in row.items():
            if k in _PERVIAL_BOOL:
                out[k] = bool(v)
                continue
            if k in _PERVIAL_TEXT:
                out[k] = v
                continue
            if v is None or (isinstance(v, str) and not v.strip()):
                ## Blank stays blank -- NaN is how this file has always
                ## spelled "not set", and the validator is what decides
                ## whether this vial needed it.
                out[k] = float("nan")
                continue
            if k in numeric:
                try:
                    num = float(v)
                except (TypeError, ValueError):
                    errors.append("vial %s, %s: %r is not a number"
                                  % (row.get("vial", "?"), k, v))
                    continue
                if k in ("vial", "input_pump2", "n_tolerant", "n_dilutions",
                         "number_consecutive_intervals"):
                    if num != int(num):
                        errors.append("vial %s, %s: %r must be a whole number"
                                      % (row.get("vial", "?"), k, v))
                        continue
                    num = int(num)
                out[k] = num
            else:
                out[k] = v
        clean.append(out)
    return clean, errors


def _clean(val):
    """Convert NaN / inf to None so Dash can JSON-serialize it."""
    if isinstance(val, float) and not np.isfinite(val):
        return None
    return val


def _pervial_rows(config, mode):
    cols = _PERVIAL_COLS.get(mode, _PERVIAL_COLS["calibration"])
    pvs = config.get("experiment_settings", {}).get("per_vial_settings", [])
    if pvs:
        return [{c: _clean(pvc.get(c, _PERVIAL_DEFAULTS.get(c))) for c in cols}
                for pvc in pvs]
    return [{"vial": i, **{c: (True if c == "to_run" else _PERVIAL_DEFAULTS.get(c))
                           for c in cols if c != "vial"}}
            for i in range(16)]


def _pervial_columns(mode):
    cols = _PERVIAL_COLS.get(mode, _PERVIAL_COLS["calibration"])
    result = []
    for c in cols:
        col = {"id": c, "name": c, "editable": c != "vial"}
        if c == "to_run":
            col["presentation"] = "dropdown"
        elif c not in _PERVIAL_TEXT:
            ## Typed, so the grid itself rejects text where a number belongs
            ## rather than letting it reach the yaml. The write path checks
            ## again regardless: this is the browser's opinion, not a guarantee.
            col["type"] = "numeric"
        result.append(col)
    return result

# ─── data cache ──────────────────────────────────────────────────────────────

_lock = threading.Lock()
_cache: dict = {"config": None, "df": None, "last_mtime": 0.0}


def _source_paths(config):
    exp = config["experiment_settings"]["exp_name"]
    calib = config["experiment_settings"].get("calib_name", "")
    mode = config["experiment_settings"]["operation"]["mode"]
    paths = ["experiment_parameters.yaml"]
    for vs in config["experiment_settings"]["per_vial_settings"]:
        if not vs["to_run"]:
            continue
        v = vs["vial"]
        for sensor in ["od_90_raw", "od_135_raw"]:
            paths.append(f"./{exp}/{sensor}/vial{v}_{sensor}.txt")
        paths.append(f"./{exp}/pump_log/vial{v}_pump_log.txt")
        paths.append(f"./{exp}/OD/vial{v}_OD.txt")
        paths.append(f"./{exp}/drugconc/vial{v}_drugconc.txt")
        paths.append(f"./{exp}/temp/vial{v}_temp.txt")
        if calib:
            paths.append(f"./{exp}/OD_autocalib/vial{v}_OD_autocalib.txt")
        if mode == "turbidostat" and calib:
            paths.append(f"./{exp}/ODset/vial{v}_ODset.txt")
    return paths


def _max_mtime(paths):
    mtimes = [os.path.getmtime(p) for p in paths if os.path.exists(p)]
    return max(mtimes) if mtimes else 0.0


def get_data():
    """Return (config, df), reloading from disk only when source files changed."""
    with _lock:
        try:
            config = load_config()
            mtime = _max_mtime(_source_paths(config))
            if mtime > _cache["last_mtime"]:
                _cache["config"] = config
                _cache["df"] = load_data(config)
                _cache["last_mtime"] = mtime
        except Exception:
            print("[dashboard] reload error:")
            traceback.print_exc()
        return _cache["config"], _cache["df"]


# ─── subplot helpers ──────────────────────────────────────────────────────────

COL_WRAP = 4


def _facet_fig(labels, height_per_row=350, shared_yaxes=False):
    n = len(labels)
    ncols = min(COL_WRAP, n)
    nrows = max(1, int(np.ceil(n / ncols)))
    fig = make_subplots(
        rows=nrows,
        cols=ncols,
        subplot_titles=[str(l) for l in labels],
        shared_yaxes=shared_yaxes,
    )
    fig.update_layout(
        height=height_per_row * nrows,
        showlegend=False,
        margin=dict(t=80, b=40),
        font=dict(family=_MONO, size=12),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#fafafa",
    )
    return fig, nrows, ncols


def _rc(i, ncols):
    return i // ncols + 1, i % ncols + 1


MAX_PTS_PER_VIAL = 3_000


def _downsample(df):
    """Stride-downsample a per-vial DataFrame to at most MAX_PTS_PER_VIAL rows."""
    n = len(df)
    if n <= MAX_PTS_PER_VIAL:
        return df
    stride = n // MAX_PTS_PER_VIAL
    return df.iloc[::stride]


def _axis_suffix(row, col, ncols):
    """Plotly axis number for subplot (row, col) in an ncols-wide grid: '' for 1, '2', '3'..."""
    n = (row - 1) * ncols + col
    return "" if n == 1 else str(n)


def _shape_hline(row, col, ncols, y, color, dash="dash"):
    s = _axis_suffix(row, col, ncols)
    return dict(type="line",
                xref=f"x{s} domain", x0=0, x1=1,
                yref=f"y{s}", y0=float(y), y1=float(y),
                line=dict(color=color, dash=dash, width=1.5))


def _shape_vline(row, col, ncols, x, color, dash="dot", width=1):
    s = _axis_suffix(row, col, ncols)
    return dict(type="line",
                xref=f"x{s}", x0=float(x), x1=float(x),
                yref=f"y{s} domain", y0=0, y1=1,
                line=dict(color=color, dash=dash, width=width))


def _shape_vrect(row, col, ncols, x0, x1, fillcolor="blue", opacity=0.10):
    s = _axis_suffix(row, col, ncols)
    return dict(type="rect",
                xref=f"x{s}", x0=float(x0), x1=float(x1),
                yref=f"y{s} domain", y0=0, y1=1,
                fillcolor=fillcolor, opacity=opacity, line_width=0, layer="below")


def _shape_hrect(row, col, ncols, y0, y1, fillcolor="blue", opacity=0.05):
    s = _axis_suffix(row, col, ncols)
    return dict(type="rect",
                xref=f"x{s} domain", x0=0, x1=1,
                yref=f"y{s}", y0=float(y0), y1=float(y1),
                fillcolor=fillcolor, opacity=opacity, line_width=0)


# ─── figure builders ─────────────────────────────────────────────────────────

def fig_timecourse(df, sensor, vials):
    """One line per vial, time on x-axis — mirrors the sensor timecourse loop."""
    if sensor not in df.columns:
        return go.Figure().update_layout(title=f"{sensor} – column not in data")

    fig, _, ncols = _facet_fig([f"Vial {v}" for v in vials])
    for i, v in enumerate(vials):
        r, c = _rc(i, ncols)
        sub = _downsample(df[df.vial == v].sort_values("time"))
        fig.add_trace(
            go.Scatter(x=sub["time"], y=sub[sensor], mode="lines",
                       line=dict(width=1.5)),
            row=r, col=c,
        )
        fig.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig.update_yaxes(title_text=sensor, row=r, col=c)
    return fig.update_layout(title_text=f"Timecourse — {sensor}")


def fig_sensor_scatter(df, config, vials):
    """Sensor 90 vs 135 scatter coloured by time, with optional calibration overlay."""
    fig, _, ncols = _facet_fig([f"Vial {v}" for v in vials])
    calib_name = config["experiment_settings"].get("calib_name", "")
    calibdf = None
    if calib_name and os.path.exists(f"{calib_name}.csv"):
        calibdf = pd.read_csv(f"{calib_name}.csv", index_col=0)
        calibdf["sensor"] = calibdf["sensor"].astype(str)

    for i, v in enumerate(vials):
        r, c = _rc(i, ncols)
        sub = df[df.vial == v]

        if calibdf is not None:
            csub = (
                calibdf[calibdf.vial == v][["estimated_od", "reading", "sensor"]]
                .pivot(index="estimated_od", values="reading", columns="sensor")
            )
            if "90" in csub.columns and "135" in csub.columns:
                fig.add_trace(
                    go.Scatter(
                        x=csub["90"], y=csub["135"], mode="markers",
                        name="Calibration",
                        marker=dict(symbol="circle-open", size=7, color="black"),
                    ),
                    row=r, col=c,
                )

        sub_ds = _downsample(sub)
        fig.add_trace(
            go.Scatter(
                x=sub_ds["od_90_raw"], y=sub_ds["od_135_raw"], mode="markers",
                name=f"Vial {v}",
                marker=dict(
                    color=sub_ds["time"], colorscale="Viridis",
                    size=3, opacity=0.5,
                    colorbar=dict(title="Time (h)") if i == 0 else None,
                    showscale=(i == 0),
                ),
            ),
            row=r, col=c,
        )
        fig.update_xaxes(title_text="Sensor 90", row=r, col=c)
        fig.update_yaxes(title_text="Sensor 135", row=r, col=c)

    return fig.update_layout(title_text="Sensor scatter — 90 vs 135 (colour = time)")


def fig_turbidostat_od(df, config, vials):
    """od_plinear_135 timecourse with high/low threshold lines per vial."""
    col = "od_plinear_135"
    if col not in df.columns:
        return go.Figure().update_layout(title="od_plinear_135 – column not in data")

    per_vial = {vs["vial"]: vs for vs in config["experiment_settings"]["per_vial_settings"]}
    fig, _, ncols = _facet_fig([f"Vial {v}" for v in vials])
    shapes = []

    for i, v in enumerate(vials):
        r, c = _rc(i, ncols)
        sub = df[df.vial == v].dropna(subset=[col]).sort_values("time")
        fig.add_trace(
            go.Scatter(
                x=sub["time"], y=sub[col], mode="lines+markers",
                marker=dict(size=4),
            ),
            row=r, col=c,
        )
        vs = per_vial.get(v, {})
        if (hi := vs.get("turbidostat_high")) is not None:
            shapes.append(_shape_hline(r, c, ncols, hi, "black"))
        if (lo := vs.get("turbidostat_low")) is not None:
            shapes.append(_shape_hline(r, c, ncols, lo, "red"))
        fig.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig.update_yaxes(title_text="OD (plinear 135)", row=r, col=c)

    return fig.update_layout(title_text="Turbidostat — OD with limits", shapes=shapes)


def fig_turbidostat_growth(config, vials, yvar):
    """Growth rate or doubling time derived from ODset transitions."""
    exp = config["experiment_settings"]["exp_name"]
    dflist = []
    for vs in config["experiment_settings"]["per_vial_settings"]:
        if not vs["to_run"]:
            continue
        v = vs["vial"]
        path = f"{exp}/ODset/vial{v}_ODset.txt"
        if not os.path.exists(path):
            continue
        tlow, thigh = vs["turbidostat_low"], vs["turbidostat_high"]
        _df = pd.read_csv(path, names=["time", "ODset"], skiprows=[0]).astype(float)
        _df = _df.assign(vial=v, turbidostat_low=tlow, turbidostat_high=thigh)
        _df = _df[(_df.ODset == tlow) | (_df.ODset == thigh)]
        _df = _df.assign(
            growthwindow=_df.time.shift(-1),
            growthod=_df.ODset.shift(-1),
        )
        _df = _df[_df.ODset == thigh]
        dflist.append(_df[["time", "growthwindow", "growthod", "vial", "ODset"]])

    if not dflist:
        return go.Figure().update_layout(title=f"{yvar} – no ODset files found")

    gdf = pd.concat(dflist).reset_index(drop=True)
    gdf = gdf.assign(
        GrowthRate=np.log(gdf.growthod / gdf.ODset) / (gdf.time - gdf.growthwindow),
        DoublingTime=lambda d: np.log(2) / d.GrowthRate,
    )

    fig, _, ncols = _facet_fig([f"Vial {v}" for v in vials])
    for i, v in enumerate(vials):
        r, c = _rc(i, ncols)
        sub = gdf[gdf.vial == v]
        fig.add_trace(
            go.Scatter(x=sub["time"], y=sub[yvar], mode="lines+markers"),
            row=r, col=c,
        )
        fig.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig.update_yaxes(title_text=yvar, row=r, col=c)

    return fig.update_layout(title_text=yvar)


def _load_pump3(exp, vials_config):
    """Read pump logs in 3-column format (time, timein, pump-type)."""
    pumplist = []
    for vs in vials_config:
        if not vs["to_run"]:
            continue
        v = vs["vial"]
        path = f"{exp}/pump_log/vial{v}_pump_log.txt"
        if os.path.exists(path):
            pump = pd.read_csv(path, names=["time", "timein", "pump"], skiprows=[0])
            pumplist.append(pump.assign(vial=v))
    return pd.concat(pumplist).reset_index(drop=True) if pumplist else pd.DataFrame()


def fig_morbidostat(df, config, vials, zoom=False):
    """OD scatter with pump=in1 and pump=in2 events overlaid as coloured markers."""
    col = "od_plinear_135"
    colormap = {"in1": "green", "in2": "red"}

    _df = df.copy()
    _df[col] = _df.groupby("vial")[col].ffill()
    if zoom:
        _df = _df.groupby("vial").tail(720).reset_index(drop=True)

    fig, _, ncols = _facet_fig([f"Vial {v}" for v in vials], height_per_row=350)

    for i, v in enumerate(vials):
        r, c = _rc(i, ncols)
        sub = _df[_df.vial == v].sort_values("time")

        fig.add_trace(
            go.Scatter(
                x=_downsample(sub)["time"], y=_downsample(sub)[col],
                mode="markers", marker=dict(size=3, opacity=0.3, color="steelblue"),
                showlegend=False,
            ),
            row=r, col=c,
        )

        for ptype, color in colormap.items():
            events = sub[sub["pump"] == ptype].dropna(subset=[col])
            fig.add_trace(
                go.Scatter(
                    x=events["time"], y=events[col], mode="markers",
                    marker=dict(size=7, color=color),
                    name=ptype, showlegend=(i == 0),
                ),
                row=r, col=c,
            )

        fig.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig.update_yaxes(title_text="OD (plinear 135)", row=r, col=c)

    title = "Morbidostat — last 2 h (zoom)" if zoom else "Morbidostat — full run"
    return fig.update_layout(title_text=title)


def fig_customplot(config, t_min=None):
    """Phloroglucinol concentration over time — mirrors customplot() in plot.py."""
    import json

    EXP_NAME = config["experiment_settings"]["exp_name"]
    active_vials = [pvs for pvs in config["experiment_settings"]["per_vial_settings"]
                    if pvs["to_run"]]

    if not os.path.exists("pump_cal.json"):
        return go.Figure().update_layout(title="pump_cal.json not found")

    with open("pump_cal.json", "r") as f:
        pump_coef = json.load(f)["coefficients"]

    pumplist = []
    for vialconfig in active_vials:
        vial = vialconfig["vial"]
        path = f"{EXP_NAME}/pump_log/vial{vial}_pump_log.txt"
        if not os.path.exists(path):
            continue
        pump = pd.read_csv(path, names=["time", "timein", "pump"], skiprows=[0])
        if t_min is not None:
            pump = pump[pump.time >= t_min]
        pumplist.append(pump.assign(
            vial=vial,
            expname=EXP_NAME,
            doubling_time=vialconfig.get("doubling_time"),
            dilution_fraction=vialconfig.get("dilution_fraction"),
        ))

    if not pumplist:
        return go.Figure().update_layout(title="No pump data found")

    pump = pd.concat(pumplist).reset_index(drop=True)

    # Determine timeswitch once from experiment name (mirrors customplot logic)
    if "patrick" in EXP_NAME.lower():
        timeswitch = 140
    elif "plankton" in EXP_NAME.lower():
        timeswitch = 139
    else:
        timeswitch = float("inf")  # default: always in rescue mode

    def calculate_conc_for_vial(gdf, vial):
        """Compute Phloroglucinol concentration for a single vial's pump events."""
        coef = pump_coef[vial]
        # Ignore rows with no recognised pump type (e.g. the initial 0,0 header row)
        valid = gdf[gdf["pump"].isin(["in1", "in2"])].copy()
        Clist = [0]
        vvial = 22
        for _, row in valid.iterrows():
            vadd = coef * row.timein
            if row.time < timeswitch:
                selection, rescue = 5, 0
            else:
                selection, rescue = 5, 1
            if row.pump == "in1":
                Clist.append((Clist[-1] * vvial + vadd * rescue) / (vvial + vadd))
            elif row.pump == "in2":
                Clist.append((Clist[-1] * vvial + vadd * selection) / (vvial + vadd))
        # Clist[0] is the initial state used during the loop; Clist[1:] aligns with valid rows
        return valid.assign(Phloroglucinol_gL=Clist[1:])[["time", "Phloroglucinol_gL"]].assign(vial=vial)

    conclist = []
    for vc in active_vials:
        v = vc["vial"]
        gdf = pump[pump["vial"] == v].copy()
        if len(gdf):
            conclist.append(calculate_conc_for_vial(gdf, v))

    if not conclist:
        return go.Figure().update_layout(title="No concentration data computed")

    concdf = pd.concat(conclist).reset_index(drop=True)

    vials = [vc["vial"] for vc in active_vials]
    fig, _, ncols = _facet_fig([f"Vial {v}" for v in vials])
    for i, v in enumerate(vials):
        r, c = _rc(i, ncols)
        sub = concdf[concdf.vial == v]
        fig.add_trace(
            go.Scatter(x=sub["time"], y=sub["Phloroglucinol_gL"],
                       mode="lines+markers",
                       line=dict(color="#777777", width=1.5),
                       marker=dict(size=4, color="#777777")),
            row=r, col=c,
        )
        fig.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig.update_yaxes(title_text="Phloroglucinol (g/L)", row=r, col=c)

    return fig.update_layout(title_text="Phloroglucinol concentration")


# ─── alternating_selection ───────────────────────────────────────────────────
# Three logs custom_script.py's alternating_selection owns, read the same way
# every other source here is: straight off disk, tolerantly. A vial that has
# not reached a given state yet simply has no file, which is not an error.

_ALTSEL_CYCLE_COLS = ["time", "state", "kind", "conc_before", "conc_after",
                      "cycle_duration", "dilution_counter", "time_in_state"]

# What each `kind` means at a glance. HIGH earns the streak only with `counted`;
# `climb` spends budget getting there; `overrun` wipes it; `tooshort` is a cycle
# too brief to be biological evidence either way.
ALTSEL_KIND_COLOR = {
    "counted": "#2e7d32", "climb": "#90a4ae", "overrun": "#c62828",
    "tooshort": "#ef6c00", "purge": "#5e35b1", "recovered": "#00838f",
    "recover_overrun": "#c62828",
}
ALTSEL_STATE_COLOR = {"HIGH": "#e15759", "LOW": "#4e79a7"}


def _altsel_read(path, names, numeric=("time",)):
    """One of the mode's logs as a DataFrame, or empty if it is not there yet.

    The controller appends to these files while this reads them, so a torn final
    row is ordinary rather than exceptional. `on_bad_lines="skip"` alone is not
    enough for that: it drops a row with too MANY fields, while a row cut short
    mid-write is padded with NaN and survives. Every column this plots is
    therefore required to be finite, so a half-written cycle is dropped rather
    than drawn as a gap in the streak.
    """
    if not os.path.exists(path):
        return pd.DataFrame(columns=names)
    try:
        df = pd.read_csv(path, on_bad_lines="skip")
    except Exception:
        return pd.DataFrame(columns=names)
    if list(df.columns[:len(names)]) != names:
        return pd.DataFrame(columns=names)
    keep = np.ones(len(df), dtype=bool)
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        keep &= np.isfinite(df[col])
    return df[keep].sort_values("time")


def altsel_logs(exp, vial):
    """(states, targets, cycles) for one vial."""
    return (_altsel_read(f"{exp}/state_log/vial{vial}_state.txt", ["time", "state"]),
            _altsel_read(f"{exp}/drug_target/vial{vial}_drug_target.txt",
                         ["time", "target"], numeric=("time", "target")),
            _altsel_read(f"{exp}/cycle_log/vial{vial}_cycles.txt", _ALTSEL_CYCLE_COLS,
                         numeric=("time", "cycle_duration", "dilution_counter")))


def altsel_state_spans(states, t_end):
    """[(state, t_from, t_to)] from the transition rows.

    The log records only TRANSITIONS, so a span runs from each row to the next
    and the last one runs to now. Reading it any other way would draw a vial as
    stateless for everything except the instants it changed.
    """
    spans = []
    rows = list(zip(states["time"], states["state"]))
    for i, (t, st) in enumerate(rows):
        t_to = rows[i + 1][0] if i + 1 < len(rows) else t_end
        if t_to > t:
            spans.append((str(st), float(t), float(t_to)))
    return spans


_TAB10 = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
          "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac"]


def fig_pumpcontrol_ramp(df, config, t_min=None):
    """Five figures for pumpcontrol_ramp mode."""
    import json

    EXP_NAME = config["experiment_settings"]["exp_name"]
    active_vials = [pvs for pvs in config["experiment_settings"]["per_vial_settings"]
                    if pvs["to_run"]]
    vials = [vc["vial"] for vc in active_vials]

    # ── load pump log (used for vol/rate figures and growth rate) ─────────────
    pumplist = []
    for vc in active_vials:
        v = vc["vial"]
        path = f"{EXP_NAME}/pump_log/vial{v}_pump_log.txt"
        if not os.path.exists(path):
            continue
        pump = pd.read_csv(path, names=["time", "timein", "pump"], skiprows=[0])
        if t_min is not None:
            pump = pump[pump.time >= t_min]
        pumplist.append(pump.assign(vial=v))

    pump_all = pd.concat(pumplist).reset_index(drop=True) if pumplist else pd.DataFrame(
        columns=["time", "timein", "pump", "vial"])

    # pump_cal.json is optional — only needed for volume/rate figures
    pump_coef = None
    if os.path.exists("pump_cal.json"):
        with open("pump_cal.json") as f:
            pump_coef = json.load(f)["coefficients"]

    if pump_coef and not pump_all.empty:
        coef_map = {}
        for vc in active_vials:
            v = vc["vial"]
            pump2_raw = vc.get("input_pump2")
            if pd.notna(pump2_raw):
                pump2 = int(pump2_raw)
                coef_map[(v, "in1")] = pump_coef[v]
                coef_map[(v, "in2")] = pump_coef[pump2]
        pump_all["volume_dispensed"] = pump_all.apply(
            lambda row: coef_map.get((row.vial, row.pump), 0) * (row.timein if pd.notna(row.timein) else 0),
            axis=1,
        )
    else:
        pump_all["volume_dispensed"] = np.nan

    # ── load drug concentration from file ────────────────────────────────────
    conclist = []
    for vc in active_vials:
        v = vc["vial"]
        path = f"{EXP_NAME}/drugconc/vial{v}_drugconc.txt"
        if not os.path.exists(path):
            continue
        cdf = pd.read_csv(path)  # columns: time, concentration
        if t_min is not None:
            cdf = cdf[cdf.time >= t_min]
        conclist.append(cdf.assign(vial=v))

    concdf = pd.concat(conclist).reset_index(drop=True) if conclist else None

    # ── figure 1: od_135_raw + setpoints ──────────────────────────────────────
    _df_od = df if t_min is None else df[df.time >= t_min]
    od_t_range = [_df_od["time"].min(), _df_od["time"].max()] if not _df_od.empty else None
    fig_od, _, ncols = _facet_fig([f"Vial {v}" for v in vials])
    shapes = []
    for i, vc in enumerate(active_vials):
        v = vc["vial"]
        r, c = _rc(i, ncols)
        sub = _downsample(_df_od[_df_od.vial == v].dropna(subset=["od_135_raw"]).sort_values("time"))
        fig_od.add_trace(go.Scatter(x=sub["time"], y=sub["od_135_raw"],
                                    mode="markers", marker=dict(size=3, color="black")), row=r, col=c)
        if (sp := vc.get("setpoint")) is not None:
            shapes.append(_shape_hline(r, c, ncols, sp, "#333333"))
        fig_od.update_xaxes(title_text="Time (h)", row=r, col=c)
        med = sub["od_135_raw"].median()
        #fig_od.update_yaxes(title_text="od_135_raw", range=[med * 0.95, med * 1.05], row=r, col=c)
        fig_od.update_yaxes(title_text="od_135_raw", range=[vc.get("setpoint") * 0.98, vc.get("setpoint") * 1.2], row=r, col=c)        
    fig_od.update_layout(title_text="OD (135 raw) + setpoints", shapes=shapes)

    # ── figure 2: drug concentration from file ────────────────────────────────
    if concdf is None:
        fig_conc = go.Figure().update_layout(title="No drugconc data found")
    else:
        fig_conc, _, ncols2 = _facet_fig([f"Vial {v}" for v in vials])
        for i, v in enumerate(vials):
            r, c = _rc(i, ncols2)
            sub = concdf[concdf.vial == v].sort_values("time")
            fig_conc.add_trace(go.Scatter(
                x=sub["time"], y=sub["concentration"],
                mode="lines+markers",
                line=dict(color="#777777", width=1.5),
                marker=dict(size=4, color="#777777"),
            ), row=r, col=c)
            fig_conc.update_xaxes(title_text="Time (h)", range=od_t_range, row=r, col=c)
            fig_conc.update_yaxes(title_text="Drug conc. (g/L)", row=r, col=c)
        fig_conc.update_layout(title_text="Drug concentration")

    # ── figure 3: growth rate vs drug concentration ───────────────────────────
    # Growth rate estimated from the interval between consecutive drugconc entries.
    if concdf is None:
        fig_gr = go.Figure().update_layout(title="No growth rate data")
    else:
        growth_parts = []
        for v in vials:
            cv = concdf[concdf.vial == v].sort_values("time").copy()
            if len(cv) > 1:
                cv["growth_time"] = cv["time"].diff()
                cv["growth_rate"] = np.log(1.0 / ((1.0 - 5.0 / 27.0)*(1.0 - 5.0 / 27.0))) / cv["growth_time"]
                growth_parts.append(cv[["time", "concentration", "growth_time", "growth_rate"]].assign(vial=v).dropna(subset=["growth_time"]))

        if not growth_parts:
            fig_gr = go.Figure().update_layout(title="Not enough drugconc entries for growth rate (need ≥ 2)")
        else:
            merged = pd.concat(growth_parts).reset_index(drop=True)
            merged = merged[merged.growth_rate.between(0, 5)]
            fig_gr, _, ncols3 = _facet_fig([f"Vial {v}" for v in vials])
            for i, v in enumerate(vials):
                r, c = _rc(i, ncols3)
                sub = merged[merged.vial == v]
                fig_gr.add_trace(go.Scatter(
                    x=sub["concentration"], y=sub["growth_rate"],
                    mode="markers",
                    marker=dict(
                        size=12, color=sub["time"], colorscale="Viridis",
                        showscale=(i == 0),
                        colorbar=dict(title="Time (h)") if i == 0 else None,
                    ),
                ), row=r, col=c)
                med_x = sub["concentration"].median() if not sub.empty else np.nan
                med_y = sub["growth_rate"].median() if not sub.empty else np.nan
                mean_y = sub["growth_rate"].mean() if not sub.empty else np.nan
                std_y = sub["growth_rate"].std() if not sub.empty else np.nan
                x_range = [med_x * 0.9, med_x * 1.1] if np.isfinite(med_x) else None
                y_range = [mean_y -2*std_y,mean_y +2*std_y] if np.isfinite(med_y) else None
                fig_gr.update_xaxes(title_text="Drug conc. (g/L)", row=r, col=c)
                fig_gr.update_yaxes(title_text="Growth rate (1/h)", range=y_range, row=r, col=c)
            fig_gr.update_layout(title_text="Growth rate vs drug concentration")

    # ── figure 4: volume dispensed per pump event ─────────────────────────────
    has_vol = not pump_all.empty and pump_all["volume_dispensed"].notna().any()
    if not has_vol:
        fig_vol = go.Figure().update_layout(title="Volume dispensed — pump_cal.json not found")
    else:
        fig_vol, _, ncols4 = _facet_fig([f"Vial {v}" for v in vials])
        pump_colors = {"in1": "#4e79a7", "in2": "#f28e2b"}
        for i, v in enumerate(vials):
            r, c = _rc(i, ncols4)
            for ptype, color in pump_colors.items():
                sub = pump_all[(pump_all.vial == v) & (pump_all.pump == ptype)].sort_values("time")
                fig_vol.add_trace(go.Scatter(
                    x=sub["time"], y=sub["volume_dispensed"],
                    mode="markers", name=ptype,
                    marker=dict(size=5, color=color),
                    showlegend=(i == 0),
                ), row=r, col=c)
            fig_vol.update_xaxes(title_text="Time (h)", row=r, col=c)
            fig_vol.update_yaxes(title_text="Volume / event (mL)", row=r, col=c)
        fig_vol.update_layout(title_text="Volume dispensed per pump event", showlegend=True)

    # ── figure 5: media consumption rate + cumulative (mirrored axis) ────────
    if not has_vol:
        fig_rate = go.Figure().update_layout(title="Media consumption rate — pump_cal.json not found")
    else:
        _RATE_COLS = ["time", "in1", "in2"]

        def _hourly_rate(gdf):
            if gdf.empty:
                return pd.DataFrame(columns=_RATE_COLS)
            t_max = gdf["time"].max()
            if t_max <= 0:
                return pd.DataFrame(columns=_RATE_COLS)
            # always produce at least one bin, even for < 1 h experiments
            n_bins = max(1, int(np.ceil(t_max)))
            edges = np.arange(0, n_bins + 1, dtype=float)
            rows = []
            for i in range(len(edges) - 1):
                t0, t1 = edges[i], edges[i + 1]
                w = gdf[(gdf["time"] > t0) & (gdf["time"] <= t1)]
                rows.append({
                    "time": (t0 + t1) / 2,
                    "in1": w[w["pump"] == "in1"]["volume_dispensed"].sum(),
                    "in2": w[w["pump"] == "in2"]["volume_dispensed"].sum(),
                })
            return pd.DataFrame(rows, columns=_RATE_COLS)

        rate_parts = []
        for v in vials:
            sub = pump_all[pump_all.vial == v]
            if len(sub):
                rdf = _hourly_rate(sub)
                rdf["vial"] = v
                rate_parts.append(rdf)

        rate_parts = [r for r in rate_parts if not r.empty]
        if not rate_parts:
            fig_rate = go.Figure().update_layout(title="Not enough data for rate (< 1 h elapsed)")
        else:
            ratedf = pd.concat(rate_parts).reset_index(drop=True)
            for col in ["in1", "in2"]:
                if col not in ratedf.columns:
                    ratedf[col] = 0.0
            ratedf_long = ratedf.melt(id_vars=["time", "vial"],
                                      value_vars=["in1", "in2"],
                                      var_name="Pump",
                                      value_name="mL/hr")
            total = ratedf_long.groupby(["time", "Pump"])["mL/hr"].sum().reset_index()

            # cumulative total volume across all vials, sorted by time
            cum_all = pump_all.sort_values("time").copy()
            cum_all["cumvol"] = cum_all["volume_dispensed"].fillna(0).cumsum()

            fig_rate = make_subplots(specs=[[{"secondary_y": True}]])
            for idx, v in enumerate(vials):
                color = _TAB10[idx % len(_TAB10)]
                for ptype, dash in [("in1", "solid"), ("in2", "dash")]:
                    sub = ratedf_long[(ratedf_long.vial == v) & (ratedf_long.Pump == ptype)]
                    fig_rate.add_trace(go.Scatter(
                        x=sub["time"], y=sub["mL/hr"],
                        mode="lines", name=f"Vial {v} {ptype}",
                        line=dict(color=color, dash=dash, width=1.5),
                        legendgroup=f"vial{v}",
                    ), secondary_y=False)
            for ptype, dash in [("in1", "solid"), ("in2", "dash")]:
                tot = total[total.Pump == ptype]
                fig_rate.add_trace(go.Scatter(
                    x=tot["time"], y=tot["mL/hr"],
                    mode="lines", name=f"Total {ptype}",
                    line=dict(color="#111111", dash=dash, width=3),
                ), secondary_y=False)
            fig_rate.add_trace(go.Scatter(
                x=cum_all["time"], y=cum_all["cumvol"],
                mode="lines", name="Cumulative (all vials)",
                line=dict(color="#888888", dash="dot", width=2),
            ), secondary_y=True)
            fig_rate.update_layout(
                title_text="Media consumption rate",
                font=dict(family=_MONO, size=12),
                paper_bgcolor="#ffffff",
                plot_bgcolor="#fafafa",
                height=450,
            )
            fig_rate.update_xaxes(title_text="Time (h)")
            fig_rate.update_yaxes(title_text="mL / hr", secondary_y=False)
            fig_rate.update_yaxes(title_text="Cumulative volume (mL)", secondary_y=True, showgrid=False)

    return fig_od, fig_conc, fig_gr, fig_vol, fig_rate


# Cycle kinds that are a GROWTH cycle. A purge step is a single 5 mL washout
# on a fixed timer, not the culture regrowing a dilution, and `tooshort` is
# custom_script.py's own verdict that a cycle cannot be biological evidence of
# anything. Neither belongs on a growth-rate axis.
ALTSEL_GROWTH_KINDS = {"counted", "climb", "overrun", "recovered", "recover_overrun"}


def altsel_growth_rate(cycle_duration, volume):
    """Specific growth rate implied by one completed dilution cycle, per hour.

    A cycle is a PAIR of ALTSEL_MAXDISPENSE steps (custom_script.py keeps the
    pair in LOW as well as HIGH precisely so the two states' cycles are
    comparable), so the culture is diluted by (V/(V+D))**2 and must regrow that
    factor within cycle_duration:

        mu = 2 * ln((V+D)/V) / cycle_duration

    Derived from the vial's own configured volume rather than the 5/27 that
    pumpcontrol_ramp's figure hardcodes -- that constant is only correct for a
    22 mL vial, and this mode's volume is per-vial and used in the purge
    arithmetic too.

    This is a FLOOR on growth, for the same reason the viewer's generation
    count is: a culture growing below setpoint divides without triggering a
    dilution, and those divisions are invisible here.
    """
    D = 5.0
    if not (volume and volume > 0) or not (cycle_duration and cycle_duration > 0):
        return np.nan
    return 2.0 * np.log((volume + D) / volume) / cycle_duration


def fig_alternating_selection(df, config, t_min=None):
    """Four figures for alternating_selection.

    The first is the one that matters: raw OD with the HIGH/LOW state painted
    behind it, so "why did this vial stop growing" and "which half of the
    protocol is it in" are the same glance. The rest break out the parts that
    decide the next transition.
    """
    EXP = config["experiment_settings"]["exp_name"]
    active = [v for v in config["experiment_settings"]["per_vial_settings"]
              if v["to_run"]]
    vials = [v["vial"] for v in active]

    _df = df if t_min is None else df[df.time >= t_min]
    t_end = float(_df["time"].max()) if not _df.empty else 0.0
    logs = {v: altsel_logs(EXP, v) for v in vials}

    # ── 1: raw OD, with the state behind it ──────────────────────────────
    fig_od, _, ncols = _facet_fig([f"Vial {v}" for v in vials])
    shapes, seen_states = [], set()
    for i, vc in enumerate(active):
        v = vc["vial"]
        r, c = _rc(i, ncols)
        states, _targets, _cycles = logs[v]
        for state, t0, t1 in altsel_state_spans(states, t_end):
            if t_min is not None and t1 < t_min:
                continue
            shapes.append(_shape_vrect(r, c, ncols, max(t0, t_min or t0), t1,
                                       ALTSEL_STATE_COLOR.get(state, "#999999"),
                                       0.13))
            seen_states.add(state)
        sub = _downsample(_df[_df.vial == v].dropna(subset=["od_135_raw"])
                          .sort_values("time"))
        fig_od.add_trace(go.Scatter(x=sub["time"], y=sub["od_135_raw"], mode="markers",
                                    marker=dict(size=3, color="black"),
                                    showlegend=False), row=r, col=c)
        if (sp := vc.get("setpoint")) is not None:
            shapes.append(_shape_hline(r, c, ncols, sp, "#333333"))
        fig_od.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig_od.update_yaxes(title_text="od_135_raw", row=r, col=c)
    # A legend the shapes themselves cannot carry: plotly shapes never appear in
    # one, so without these the colours are unexplained.
    for state in sorted(seen_states):
        fig_od.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=state,
                                    marker=dict(size=10, symbol="square",
                                                color=ALTSEL_STATE_COLOR[state]),
                                    showlegend=True))
    fig_od.update_layout(title_text="OD (135 raw), shaded by state", shapes=shapes,
                         showlegend=bool(seen_states))

    # ── 2: what the culture is in vs what it is being asked to tolerate ──
    fig_conc, _, ncols2 = _facet_fig([f"Vial {v}" for v in vials])
    for i, v in enumerate(vials):
        r, c = _rc(i, ncols2)
        _states, targets, _cycles = logs[v]
        path = f"{EXP}/drugconc/vial{v}_drugconc.txt"
        if os.path.exists(path):
            cdf = pd.read_csv(path, on_bad_lines="skip")
            if t_min is not None and "time" in cdf:
                cdf = cdf[cdf.time >= t_min]
            if "concentration" in cdf:
                fig_conc.add_trace(go.Scatter(x=cdf["time"], y=cdf["concentration"],
                                              mode="lines+markers",
                                              line=dict(color="#777777", width=1.5),
                                              marker=dict(size=4, color="#777777"),
                                              name="actual", showlegend=(i == 0)),
                                   row=r, col=c)
        if not targets.empty:
            # hv: Current_Drug is a step, held until the next ramp, and drawing
            # it as a slope would imply a climb the controller never commands.
            tt = list(targets["time"]) + [t_end]
            tv = list(targets["target"]) + [targets["target"].values[-1]]
            fig_conc.add_trace(go.Scatter(x=tt, y=tv, mode="lines+markers",
                                          line=dict(color="#e15759", width=2,
                                                    shape="hv"),
                                          marker=dict(size=5),
                                          name="Current_Drug", showlegend=(i == 0)),
                               row=r, col=c)
        fig_conc.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig_conc.update_yaxes(title_text="PG (g/L)", row=r, col=c)
    fig_conc.update_layout(title_text="Drug concentration vs Current_Drug target")

    # ── 3: every closed cycle, coloured by what it was worth ─────────────
    fig_cyc, _, ncols3 = _facet_fig([f"Vial {v}" for v in vials], height_per_row=300)
    seen_kinds = set()
    for i, v in enumerate(vials):
        r, c = _rc(i, ncols3)
        _states, _targets, cycles = logs[v]
        if cycles.empty:
            continue
        cy = cycles if t_min is None else cycles[cycles.time >= t_min]
        for kind, grp in cy.groupby("kind"):
            fig_cyc.add_trace(go.Scatter(
                x=grp["time"],
                y=pd.to_numeric(grp["cycle_duration"], errors="coerce"),
                mode="markers", name=str(kind), showlegend=str(kind) not in seen_kinds,
                marker=dict(size=7, color=ALTSEL_KIND_COLOR.get(str(kind), "#999999")),
                hovertemplate=("t=%{x:.3f} h<br>%{y:.3f} h<br>" + str(kind)
                               + "<extra></extra>")), row=r, col=c)
            seen_kinds.add(str(kind))
        fig_cyc.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig_cyc.update_yaxes(title_text="Cycle duration (h)", row=r, col=c)
    fig_cyc.update_layout(title_text="Growth cycles by kind", showlegend=True)

    # ── 4: the two races that decide ramp-or-hold ────────────────────────
    fig_streak, _, ncols4 = _facet_fig([f"Vial {v}" for v in vials], height_per_row=300)
    for i, vc in enumerate(active):
        v = vc["vial"]
        r, c = _rc(i, ncols4)
        _states, _targets, cycles = logs[v]
        if cycles.empty:
            continue
        cy = cycles if t_min is None else cycles[cycles.time >= t_min]
        fig_streak.add_trace(go.Scatter(
            x=cy["time"], y=pd.to_numeric(cy["dilution_counter"], errors="coerce"),
            mode="lines+markers", line=dict(color="#2e7d32", width=1.5),
            marker=dict(size=4), name="streak", showlegend=(i == 0)), row=r, col=c)
        # The two targets the streak is racing. Both are DERIVED per vial, so
        # they are computed here the same way custom_script.py does rather than
        # read from anywhere -- see the settings docstring for alternating_selection.
        # The two thresholds the streak is racing: HIGH needs n_tolerant
        # counted cycles, LOW needs n_dilutions recovered ones. One counter
        # serves both states, so both lines belong on the same axes.
        for key, colour in (("n_tolerant", "#2e7d32"), ("n_dilutions", "#00838f")):
            want = vc.get(key)
            if want:
                fig_streak.add_shape(
                    _shape_hline(r, c, ncols4, float(want), colour, dash="dot"))
        fig_streak.update_xaxes(title_text="Time (h)", row=r, col=c)
        fig_streak.update_yaxes(title_text="Consecutive cycles", row=r, col=c)
    fig_streak.update_layout(
        title_text="Streak vs threshold (green dotted = n_tolerant, "
                   "teal = n_dilutions)")

    # ── 5: does the drug actually cost the culture anything? ─────────────
    # x is conc_BEFORE: the concentration the culture sat at WHILE it grew.
    # conc_after is where the dilution that closed the cycle left it, which is
    # the next cycle's condition, not this one's.
    fig_gr, _, ncols5 = _facet_fig([f"Vial {v}" for v in vials], height_per_row=320)
    shown = False
    # ONE colorbar for the whole figure. Gating on the vial index gave every
    # state trace on vial 0 its own, stacking two bars for the same scale.
    scale_shown = False
    for i, vc in enumerate(active):
        v = vc["vial"]
        r, c = _rc(i, ncols5)
        _states, _targets, cycles = logs[v]
        if cycles.empty:
            continue
        cy = cycles if t_min is None else cycles[cycles.time >= t_min]
        cy = cy[cy["kind"].isin(ALTSEL_GROWTH_KINDS)].copy()
        if cy.empty:
            continue
        cy["conc_before"] = pd.to_numeric(cy["conc_before"], errors="coerce")
        cy["mu"] = [altsel_growth_rate(d, vc.get("volume"))
                    for d in cy["cycle_duration"]]
        cy = cy[np.isfinite(cy["conc_before"]) & np.isfinite(cy["mu"])]
        # One trace per state so the SHAPE carries the state and the COLOUR is
        # free to carry time -- two variables that would otherwise compete for
        # the same channel.
        for state, symbol in (("HIGH", "circle"), ("LOW", "diamond")):
            sub = cy[cy["state"] == state]
            if sub.empty:
                continue
            shown = True
            fig_gr.add_trace(go.Scatter(
                x=sub["conc_before"], y=sub["mu"], mode="markers", name=state,
                legendgroup=state, showlegend=(i == 0),
                marker=dict(size=9, symbol=symbol, color=sub["time"],
                            colorscale="Viridis", cmin=float(cy["time"].min()),
                            cmax=float(cy["time"].max()),
                            line=dict(width=0.5, color="#37474f"),
                            showscale=(not scale_shown),
                            colorbar=(dict(title="Time (h)") if not scale_shown
                                      else None)),
                customdata=np.stack([sub["time"], sub["kind"]], axis=-1),
                hovertemplate=("PG %{x:.3f} g/L<br>mu %{y:.3f} /h"
                               "<br>t=%{customdata[0]:.2f} h"
                               "<br>%{customdata[1]}<extra></extra>"),
            ), row=r, col=c)
            scale_shown = True
        fig_gr.update_xaxes(title_text="PG during the cycle (g/L)", row=r, col=c)
        fig_gr.update_yaxes(title_text="Growth rate (1/h)", row=r, col=c)
    fig_gr.update_layout(
        title_text=("Growth rate vs concentration — colour is experiment time, "
                    "circle = HIGH, diamond = LOW"
                    if shown else "Growth rate vs concentration — no growth cycles yet"),
        showlegend=shown)

    return fig_od, fig_conc, fig_cyc, fig_streak, fig_gr


# ─── app layout ───────────────────────────────────────────────────────────────

SENSORS = ["od_90_raw", "od_135_raw", "OD", "od_plinear_135", "temp"]

_TAB_STYLE = {
    "fontFamily": _MONO,
    "fontSize": "13px",
    "color": "#666666",
    "padding": "14px 18px",
    "textAlign": "left",
    "borderLeft": "3px solid transparent",
    "borderTop": "none",
    "borderRight": "none",
    "borderBottom": "1px solid #e8e8e8",
    "backgroundColor": "#f5f5f5",
    "letterSpacing": "0.02em",
}
_SELECTED_TAB_STYLE = {
    **_TAB_STYLE,
    "color": "#111111",
    "backgroundColor": "#ffffff",
    "borderLeft": "3px solid #333333",
    "fontWeight": "600",
}
_HIDDEN_TAB_STYLE = {**_TAB_STYLE, "display": "none"}

app = dash.Dash(__name__, title="Evolver Dashboard")
app.layout = html.Div([
    dcc.Interval(id="tick", interval=10_000, n_intervals=0),
    dcc.Download(id="download-zip"),
    # ── top bar ──────────────────────────────────────────────────────────────
    html.Div([
        html.Span(id="exp-title", style={
            "fontSize": "14px", "fontWeight": "700", "letterSpacing": "0.06em",
        }),
        html.Span(id="last-updated", style={
            "fontSize": "11px", "color": "#999999", "marginLeft": "24px",
        }),
        html.Div([
            html.Span("history:", style={"color": "#999", "marginRight": "6px", "fontSize": "11px"}),
            dcc.Input(
                id="history-hours",
                type="number",
                placeholder="all",
                debounce=True,
                min=1,
                step=1,
                style={
                    "width": "64px", "fontFamily": _MONO, "fontSize": "12px",
                    "padding": "2px 6px", "border": "1px solid #ddd",
                    "backgroundColor": "#fff", "color": "#333",
                },
            ),
            html.Span(" h", style={"marginLeft": "4px", "color": "#999", "fontSize": "11px"}),
            html.Button(
                "⏸ pause",
                id="btn-pause",
                n_clicks=0,
                style={
                    "marginLeft": "20px", "fontFamily": _MONO, "fontSize": "11px",
                    "padding": "2px 10px", "border": "1px solid #bbb",
                    "backgroundColor": "#fff", "color": "#333", "cursor": "pointer",
                },
            ),
            html.Button(
                "⬇ download",
                id="btn-download",
                n_clicks=0,
                style={
                    "marginLeft": "8px", "fontFamily": _MONO, "fontSize": "11px",
                    "padding": "2px 10px", "border": "1px solid #bbb",
                    "backgroundColor": "#fff", "color": "#333", "cursor": "pointer",
                },
            ),
        ], style={"display": "inline-flex", "alignItems": "center", "marginLeft": "auto"}),
    ], style={
        "fontFamily": _MONO,
        "padding": "12px 20px",
        "borderBottom": "1px solid #e0e0e0",
        "backgroundColor": "#fafafa",
        "display": "flex",
        "alignItems": "center",
    }),
    # ── vertical tabs ─────────────────────────────────────────────────────────
    dcc.Tabs(
        vertical=True,
        parent_style={"display": "flex", "alignItems": "stretch", "minHeight": "calc(100vh - 50px)"},
        style={
            "width": "210px",
            "minWidth": "210px",
            "borderRight": "1px solid #e0e0e0",
            "backgroundColor": "#f5f5f5",
        },
        content_style={
            "flex": "1",
            "padding": "20px 28px",
            "backgroundColor": "#ffffff",
            "overflowY": "auto",
        },
        children=[
            dcc.Tab(label="Setup", value="setup",
                    style=_TAB_STYLE, selected_style=_SELECTED_TAB_STYLE,
                    children=[
                # ── helper styles ─────────────────────────────────────────────
                html.Div([
                    # ── two-column global settings ────────────────────────────
                    html.H4("Global settings", style={"fontFamily": _MONO, "fontSize": "13px",
                                                      "marginBottom": "10px", "marginTop": "0"}),
                    html.Div([
                        # left column
                        html.Div([
                            html.Label("Experiment name", style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-exp-name", type="text", debounce=False,
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd", "marginBottom": "6px"}),
                            ## Two free-text fields, shown and written VERBATIM.
                            ## This was a dropdown of _EVOLVER_NAMES whose value
                            ## was reverse-looked-up from _IPDICT, so a rig whose
                            ## control IP had changed -- which is a thing that
                            ## happens, EVT-00187 in the log is one -- displayed
                            ## as whatever name sorted first and then WROTE that
                            ## rig's IP back into the yaml. The table is a
                            ## convenience, not a source of truth about a live
                            ## experiment, so neither field consults it now.
                            html.Label("eVOLVER name (experiment_settings.evolver_name)",
                                       style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-evolver-name", type="text", debounce=False,
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd", "marginBottom": "6px"}),
                            html.Label("Control IP (experiment_settings.ip)",
                                       style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-ip", type="text", debounce=False,
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd", "marginBottom": "6px"}),
                            html.Label("Calibration name", style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-calib-name", type="text", debounce=False,
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd", "marginBottom": "6px"}),
                            html.Label("Operation mode", style={"fontSize": "11px", "color": "#666"}),
                            dcc.Dropdown(id="setup-mode",
                                         options=[{"label": m, "value": m} for m in _OP_MODES],
                                         clearable=False,
                                         style={"fontFamily": _MONO, "fontSize": "12px", "marginBottom": "6px"}),
                            html.Label("Temperature (°C)", style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-temp", type="number",
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd"}),
                        ], style={"width": "45%", "marginRight": "5%", "display": "flex",
                                  "flexDirection": "column"}),
                        # right column — stir + checkboxes
                        html.Div([
                            html.Label("Stir on rate", style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-stir-on-rate", type="number",
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd", "marginBottom": "6px"}),
                            html.Label("Stir off rate", style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-stir-off-rate", type="number",
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd", "marginBottom": "6px"}),
                            html.Label("Stir on duration", style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-stir-on-dur", type="number",
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd", "marginBottom": "6px"}),
                            html.Label("Stir off duration", style={"fontSize": "11px", "color": "#666"}),
                            dcc.Input(id="setup-stir-off-dur", type="number",
                                      style={"width": "100%", "fontFamily": _MONO, "fontSize": "12px",
                                             "padding": "3px 6px", "border": "1px solid #ddd", "marginBottom": "10px"}),
                            dcc.Checklist(id="setup-stir-switch",
                                          options=[{"label": " Stir rate switch", "value": "on"}],
                                          style={"fontFamily": _MONO, "fontSize": "12px", "marginBottom": "6px"}),
                            dcc.Checklist(id="setup-estimate-gr",
                                          options=[{"label": " Estimate growth rate", "value": "on"}],
                                          style={"fontFamily": _MONO, "fontSize": "12px"}),
                        ], style={"width": "45%", "display": "flex", "flexDirection": "column"}),
                    ], style={"display": "flex", "marginBottom": "16px"}),

                    # ── mode-specific ─────────────────────────────────────────
                    html.H4("Mode-specific settings", style={"fontFamily": _MONO, "fontSize": "13px",
                                                              "marginBottom": "8px"}),
                    html.Div([
                        html.Label("Num calibration steps", style={"fontSize": "11px", "color": "#666"}),
                        dcc.Input(id="setup-num-pump-events", type="number", value=20,
                                  style={"width": "120px", "fontFamily": _MONO, "fontSize": "12px",
                                         "padding": "3px 6px", "border": "1px solid #ddd"}),
                    ], id="setup-calib-section", style={"marginBottom": "16px", "display": "none"}),

                    # ── per-vial table ────────────────────────────────────────
                    html.H4("Per-vial settings", style={"fontFamily": _MONO, "fontSize": "13px",
                                                         "marginBottom": "8px"}),
                    dash_table.DataTable(
                        id="setup-pervial-table",
                        editable=True,
                        row_selectable=False,
                        dropdown={"to_run": {"options": [{"label": "True",  "value": True},
                                                          {"label": "False", "value": False}]}},
                        style_table={"overflowX": "auto", "marginBottom": "16px"},
                        style_cell={"fontFamily": _MONO, "fontSize": "11px",
                                    "padding": "4px 8px", "textAlign": "left"},
                        style_header={"fontWeight": "600", "backgroundColor": "#f5f5f5",
                                      "borderBottom": "2px solid #ddd"},
                        style_data_conditional=[
                            {"if": {"row_index": "odd"}, "backgroundColor": "#fafafa"}
                        ],
                    ),

                    # ── action buttons ────────────────────────────────────────
                    html.Div([
                        html.Button("Write configuration", id="btn-write-config",
                                    style={"fontFamily": _MONO, "fontSize": "12px",
                                           "padding": "5px 14px", "marginRight": "8px",
                                           "border": "1px solid #555", "backgroundColor": "#fff",
                                           "cursor": "pointer"}),
                        html.Button("Write calibration", id="btn-write-calib",
                                    style={"fontFamily": _MONO, "fontSize": "12px",
                                           "padding": "5px 14px", "marginRight": "16px",
                                           "border": "1px solid #aaa", "backgroundColor": "#fff",
                                           "cursor": "pointer", "display": "none"}),
                        html.Span(id="setup-status",
                                  style={"fontFamily": _MONO, "fontSize": "11px", "color": "#555"}),
                    ], style={"display": "flex", "alignItems": "center"}),
                ], style={"padding": "4px 0"}),
            ]),
            dcc.Tab(id="tab-ramp", label="Pump Ramp", value="ramp",
                    style=_HIDDEN_TAB_STYLE, selected_style=_HIDDEN_TAB_STYLE,
                    children=[
                dcc.Graph(id="fig-ramp-od"),
                dcc.Graph(id="fig-ramp-conc"),
                dcc.Graph(id="fig-ramp-gr"),
                dcc.Graph(id="fig-ramp-vol"),
                dcc.Graph(id="fig-ramp-rate"),
            ]),
            dcc.Tab(id="tab-altsel", label="Alternating Selection", value="altsel",
                    style=_HIDDEN_TAB_STYLE, selected_style=_HIDDEN_TAB_STYLE,
                    children=[
                dcc.Graph(id="fig-altsel-od"),
                dcc.Graph(id="fig-altsel-conc"),
                dcc.Graph(id="fig-altsel-cycles"),
                dcc.Graph(id="fig-altsel-streak"),
                dcc.Graph(id="fig-altsel-gr"),
            ]),
            dcc.Tab(id="tab-turbidostat", label="Turbidostat", value="turbidostat",
                    style=_HIDDEN_TAB_STYLE, selected_style=_HIDDEN_TAB_STYLE,
                    children=[
                dcc.Graph(id="fig-turb-od"),
                dcc.Graph(id="fig-turb-gr"),
                dcc.Graph(id="fig-turb-dt"),
            ]),
            dcc.Tab(id="tab-morbidostat", label="Morbidostat", value="morbidostat",
                    style=_HIDDEN_TAB_STYLE, selected_style=_HIDDEN_TAB_STYLE,
                    children=[
                dcc.Graph(id="fig-morb-full"),
                dcc.Graph(id="fig-morb-zoom"),
            ]),
            dcc.Tab(id="tab-custom", label="Phloroglucinol", value="custom",
                    style=_HIDDEN_TAB_STYLE, selected_style=_HIDDEN_TAB_STYLE,
                    children=[
                dcc.Graph(id="fig-custom"),
            ]),
            dcc.Tab(label="Sensor Scatter", value="scatter",
                    style=_TAB_STYLE, selected_style=_SELECTED_TAB_STYLE,
                    children=[
                dcc.Graph(id="fig-scatter"),
            ]),
            dcc.Tab(label="Sensor Timecourses", value="timecourse",
                    style=_TAB_STYLE, selected_style=_SELECTED_TAB_STYLE,
                    children=[
                dcc.Dropdown(
                    id="sensor-select",
                    options=[{"label": s, "value": s} for s in SENSORS],
                    value="od_plinear_135",
                    clearable=False,
                    style={"width": "300px", "marginBottom": "16px",
                           "fontFamily": _MONO, "fontSize": "13px"},
                ),
                dcc.Graph(id="fig-timecourse"),
            ]),
        ],
    ),
], style={"fontFamily": _MONO, "margin": "0", "padding": "0", "backgroundColor": "#ffffff"})


# ─── callback ────────────────────────────────────────────────────────────────

@app.callback(
    Output("exp-title", "children"),
    Output("last-updated", "children"),
    Output("fig-timecourse", "figure"),
    Output("fig-scatter", "figure"),
    Output("fig-turb-od", "figure"),
    Output("fig-turb-gr", "figure"),
    Output("fig-turb-dt", "figure"),
    Output("fig-morb-full", "figure"),
    Output("fig-morb-zoom", "figure"),
    Output("fig-custom", "figure"),
    Output("fig-ramp-od", "figure"),
    Output("fig-ramp-conc", "figure"),
    Output("fig-ramp-gr", "figure"),
    Output("fig-ramp-vol", "figure"),
    Output("fig-ramp-rate", "figure"),
    Output("fig-altsel-od", "figure"),
    Output("fig-altsel-conc", "figure"),
    Output("fig-altsel-cycles", "figure"),
    Output("fig-altsel-streak", "figure"),
    Output("fig-altsel-gr", "figure"),
    Input("tick", "n_intervals"),
    Input("sensor-select", "value"),
    Input("history-hours", "value"),
)
def refresh(_, sensor, hours):
    import datetime

    config, df = get_data()

    if config is None or df is None:
        empty = go.Figure().update_layout(
            title="Waiting for data — ensure experiment_parameters.yaml is present"
        )
        return ("No data", "") + (empty,) * 18

    exp_name = config["experiment_settings"]["exp_name"]
    mode = config["experiment_settings"]["operation"]["mode"]
    vials = [
        vs["vial"]
        for vs in config["experiment_settings"]["per_vial_settings"]
        if vs["to_run"]
    ]
    has_calib = bool(config["experiment_settings"].get("calib_name"))

    # Apply history window filter
    t_min = None
    if hours and hours > 0:
        t_min = float(df.time.max() - hours)
        df = df[df.time >= t_min]

    header = f"{exp_name}  |  {mode}  |  {len(vials)} active vials"
    updated = f"Last refreshed: {datetime.datetime.now().strftime('%H:%M:%S')}  (checks every 10 s)"

    timecourse = fig_timecourse(df, sensor, vials)
    scatter = fig_sensor_scatter(df, config, vials)

    _na_turb = go.Figure().update_layout(
        title=f"Turbidostat plots not available — mode is '{mode}'"
    )
    if mode == "turbidostat" and has_calib:
        turb_od = fig_turbidostat_od(df, config, vials)
        turb_gr = fig_turbidostat_growth(config, vials, "GrowthRate")
        turb_dt = fig_turbidostat_growth(config, vials, "DoublingTime")
    else:
        turb_od = turb_gr = turb_dt = _na_turb

    _na_morb = go.Figure().update_layout(
        title=f"Morbidostat plots not available — mode is '{mode}'"
    )
    if mode == "morbidostat":
        morb_full = fig_morbidostat(df, config, vials, zoom=False)
        morb_zoom = fig_morbidostat(df, config, vials, zoom=True)
    else:
        morb_full = morb_zoom = _na_morb

    custom = fig_customplot(config, t_min=t_min)

    _na_ramp = go.Figure().update_layout(
        title=f"Pump Ramp plots not available — mode is '{mode}'"
    )
    if mode == "pumpcontrol_ramp":
        ramp_od, ramp_conc, ramp_gr, ramp_vol, ramp_rate = fig_pumpcontrol_ramp(df, config, t_min=t_min)
    else:
        ramp_od = ramp_conc = ramp_gr = ramp_vol = ramp_rate = _na_ramp

    _na_altsel = go.Figure().update_layout(
        title=f"Alternating Selection plots not available — mode is '{mode}'"
    )
    if mode == "alternating_selection":
        (altsel_od, altsel_conc, altsel_cyc, altsel_streak,
         altsel_gr) = fig_alternating_selection(df, config, t_min=t_min)
    else:
        altsel_od = altsel_conc = altsel_cyc = altsel_streak = altsel_gr = _na_altsel

    return (
        header, updated,
        timecourse, scatter,
        turb_od, turb_gr, turb_dt,
        morb_full, morb_zoom,
        custom,
        ramp_od, ramp_conc, ramp_gr, ramp_vol, ramp_rate,
        altsel_od, altsel_conc, altsel_cyc, altsel_streak, altsel_gr,
    )


# ─── tab visibility ──────────────────────────────────────────────────────────

_MODE_TABS = {
    "turbidostat":    {"tab-turbidostat"},
    "morbidostat":    {"tab-morbidostat", "tab-custom"},
    "pumpcontrol_ramp": {"tab-ramp"},
    "alternating_selection": {"tab-altsel"},
}


@app.callback(
    Output("tab-turbidostat",  "style"), Output("tab-turbidostat",  "selected_style"),
    Output("tab-morbidostat",  "style"), Output("tab-morbidostat",  "selected_style"),
    Output("tab-custom",       "style"), Output("tab-custom",       "selected_style"),
    Output("tab-ramp",         "style"), Output("tab-ramp",         "selected_style"),
    Output("tab-altsel",       "style"), Output("tab-altsel",       "selected_style"),
    Input("tick", "n_intervals"),
)
def update_tab_visibility(_):
    config, _ = get_data()
    mode = config["experiment_settings"]["operation"]["mode"] if config else ""
    visible_ids = _MODE_TABS.get(mode, set())

    def styles(tab_id):
        if tab_id in visible_ids:
            return _TAB_STYLE, _SELECTED_TAB_STYLE
        return _HIDDEN_TAB_STYLE, _HIDDEN_TAB_STYLE

    return (
        *styles("tab-turbidostat"),
        *styles("tab-morbidostat"),
        *styles("tab-custom"),
        *styles("tab-ramp"),
        *styles("tab-altsel"),
    )


@app.callback(
    Output("tick", "disabled"),
    Output("btn-pause", "children"),
    Input("btn-pause", "n_clicks"),
)
def toggle_refresh(n):
    paused = n % 2 == 1
    return paused, ("▶ resume" if paused else "⏸ pause")


@app.callback(
    Output("download-zip", "data"),
    Input("btn-download", "n_clicks"),
    prevent_initial_call=True,
)
def download_experiment(n_clicks):
    import io
    import zipfile
    import base64

    config, _ = get_data()
    if config is None:
        return None
    exp_name = config["experiment_settings"]["exp_name"]
    exp_dir = os.path.join(".", exp_name)
    if not os.path.isdir(exp_dir):
        return None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, filenames in os.walk(exp_dir):
            for fname in filenames:
                full_path = os.path.join(dirpath, fname)
                arcname = os.path.relpath(full_path, start=".")
                zf.write(full_path, arcname)
    buf.seek(0)
    return {
        "content": base64.b64encode(buf.read()).decode(),
        "filename": f"{exp_name}.zip",
        "type": "application/zip",
        "base64": True,
    }


# ─── setup callbacks ─────────────────────────────────────────────────────────

_SETUP_FIELDS = 13


@app.callback(
    Output("setup-exp-name",       "value"),
    Output("setup-evolver-name",   "value"),
    Output("setup-ip",             "value"),
    Output("setup-calib-name",     "value"),
    Output("setup-mode",           "value"),
    Output("setup-temp",           "value"),
    Output("setup-stir-on-rate",   "value"),
    Output("setup-stir-off-rate",  "value"),
    Output("setup-stir-on-dur",    "value"),
    Output("setup-stir-off-dur",   "value"),
    Output("setup-stir-switch",    "value"),
    Output("setup-estimate-gr",    "value"),
    Output("setup-num-pump-events", "value"),
    Input("tick", "n_intervals"),
    State("setup-exp-name", "value"),
    State("setup-ip", "value"),
    prevent_initial_call=False,
)
def populate_setup(n, current_exp, current_ip):
    """Fill the form from the live yaml, per BROWSER SESSION, as soon as there
    is a live yaml.

    Every value is what the file says. Nothing is derived, defaulted or
    reverse-looked-up: a form showing a plausible value where the file has a
    different one is worse than a blank, because the next Write makes the
    plausible one true.

    The "when" is fiddlier than it looks, and got it wrong twice.

    dcc.Interval restarts at 0 for every new client, so `n == 0` means "this
    browser just loaded the page" -- and since no dcc.Input in the layout
    declares a value=, NOT populating leaves the field genuinely blank. A
    process-wide latch therefore filled the form for whoever loaded it first
    and showed everyone after them an empty one.

    But `n == 0` alone was the original bug: get_data() may still be loading,
    and returning blanks then left the form empty for the rest of that
    session. So: always populate on a fresh page, and on later ticks only
    while the form is still empty -- which is true exactly when the n == 0
    attempt found no config, and false the moment there is anything to
    clobber.
    """
    config, _ = get_data()
    es = (config or {}).get("experiment_settings") or {}
    if not es:
        # Nothing loaded yet. Say nothing and try again on the next tick
        # rather than writing blanks over the form.
        return [dash.no_update] * _SETUP_FIELDS
    if n and (current_exp or current_ip):
        # Already filled for this session; never overwrite what is on screen.
        return [dash.no_update] * _SETUP_FIELDS

    ss = es.get("stir_settings") or {}
    op = es.get("operation") or {}
    ## evolver_name first, then `evolver`: the same order evolver_api's own
    ## evolver_name() resolves them in, so the form agrees with what the API
    ## reports rather than with a third opinion.
    name = es.get("evolver_name") or es.get("evolver") or ""
    return (
        es.get("exp_name") or "",
        name,
        es.get("ip") or "",
        es.get("calib_name") or "",
        op.get("mode") or "",
        es.get("temp_all"),
        ss.get("stir_on_rate"),
        ss.get("stir_off_rate"),
        ss.get("stir_on_duration"),
        ss.get("stir_off_duration"),
        ["on"] if ss.get("stir_switch") else [],
        ["on"] if es.get("estimate_gr") else [],
        op.get("num_pump_events"),
    )


@app.callback(
    Output("setup-pervial-table", "columns"),
    Output("setup-pervial-table", "data"),
    Output("setup-calib-section", "style"),
    Input("setup-mode", "value"),
)
def update_pervial_table(mode):
    if not mode:
        return [], [], {"display": "none"}
    config, _ = get_data()
    cfg = config or {}
    calib_style = {"marginBottom": "16px"} if mode == "calibration" else {"display": "none"}
    return _pervial_columns(mode), _pervial_rows(cfg, mode), calib_style


@app.callback(
    Output("setup-status",       "children"),
    Output("btn-write-calib",    "style"),
    Input("btn-write-config",    "n_clicks"),
    Input("btn-write-calib",     "n_clicks"),
    State("setup-exp-name",      "value"),
    State("setup-evolver-name",  "value"),
    State("setup-ip",            "value"),
    State("setup-calib-name",    "value"),
    State("setup-mode",          "value"),
    State("setup-temp",          "value"),
    State("setup-stir-on-rate",  "value"),
    State("setup-stir-off-rate", "value"),
    State("setup-stir-on-dur",   "value"),
    State("setup-stir-off-dur",  "value"),
    State("setup-stir-switch",   "value"),
    State("setup-estimate-gr",   "value"),
    State("setup-num-pump-events","value"),
    State("setup-pervial-table", "data"),
    prevent_initial_call=True,
)
def handle_write(n_cfg, n_calib, exp_name, evolver_name, ip, calib_name, mode, temp,
                 stir_on_rate, stir_off_rate, stir_on_dur, stir_off_dur,
                 stir_switch, estimate_gr, num_pump_events, table_data):
    import yaml, shutil, time as _time, subprocess
    from dash import callback_context

    # show/hide calibration button
    calib_btn_style = {
        "fontFamily": _MONO, "fontSize": "12px", "padding": "5px 14px",
        "marginRight": "16px", "border": "1px solid #aaa",
        "backgroundColor": "#fff", "cursor": "pointer",
        "display": ("inline-block" if mode == "calibration" else "none"),
    }

    trigger = callback_context.triggered[0]["prop_id"].split(".")[0]

    if trigger == "btn-write-calib":
        try:
            subprocess.run(["python", "write_calibration_to_file.py"], check=True)
            return "Calibration written.", calib_btn_style
        except Exception as e:
            return f"Error: {e}", calib_btn_style

    if trigger != "btn-write-config":
        return dash.no_update, calib_btn_style

    # build config dict
    ## The IP is whatever the field says. It used to be _IPDICT[evolver],
    ## so writing a config from this tab silently replaced a rig's real
    ## control address with the table's idea of it -- and the table is
    ## hardcoded and already stale for at least one unit.
    if not ip:
        return ("Refusing to write: no control IP. The field is filled from "
                "experiment_settings.ip and writing a blank would point "
                "custom_script.py at nothing."), calib_btn_style
    cfg = {
        "experiment_settings": {
            "exp_name":    exp_name or "",
            "ip":          ip,
            "temp_all":    temp if temp is not None else 37,
            "estimate_gr": bool(estimate_gr),
            "operation":   {"mode": mode or ""},
            "stir_settings": {
                "stir_on_rate":     stir_on_rate  if stir_on_rate  is not None else 8,
                "stir_off_rate":    stir_off_rate if stir_off_rate is not None else 0,
                "stir_on_duration": stir_on_dur   if stir_on_dur   is not None else 6,
                "stir_off_duration":stir_off_dur  if stir_off_dur  is not None else 6,
                "stir_switch":      bool(stir_switch),
            },
        }
    }
    ## evolver_name is what evolver_api resolves identity from FIRST, ahead of
    ## the IP table -- which is how both live rigs stopped reporting
    ## `evolver: null`. Round-trip it so a Write does not drop it.
    if evolver_name:
        cfg["experiment_settings"]["evolver_name"] = evolver_name
    if calib_name:
        cfg["experiment_settings"]["calib_name"] = calib_name
    if mode == "calibration" and num_pump_events is not None:
        cfg["experiment_settings"]["operation"]["num_pump_events"] = int(num_pump_events)

    ## TYPES ONLY. A cell that cannot be a number is refused here, because
    ## writing it produces a yaml custom_script.py cannot load -- or, on a live
    ## edit, one it silently ignores while the form claims success. Everything
    ## else a config can be wrong about is the validator's job, and the server's
    ## POST /config does it; blocking on those here would stop an operator
    ## saving a config they mean to finish.
    per_vial, type_errors = _coerce_pervial(table_data, mode)
    if type_errors:
        shown = "; ".join(type_errors[:4])
        more = "" if len(type_errors) <= 4 else " (+%d more)" % (len(type_errors) - 4)
        return ("Not written — %d cell(s) are not the right type: %s%s"
                % (len(type_errors), shown, more)), calib_btn_style
    cfg["experiment_settings"]["per_vial_settings"] = per_vial

    try:
        if os.path.exists("experiment_parameters.yaml"):
            shutil.copyfile("experiment_parameters.yaml",
                            f"experiment_parameters.yaml.{_time.time():.0f}")
        with open("experiment_parameters.yaml", "w") as f:
            yaml.safe_dump(cfg, f)
        return f"Config written ({_time.strftime('%H:%M:%S')}).", calib_btn_style
    except Exception as e:
        return f"Error: {e}", calib_btn_style


# ─── read-only JSON API ──────────────────────────────────────────────────────
# Two lines, exactly as tools/evolver_api.py's docstring says, and nothing
# existing moves: the API reuses get_data()'s mtime-guarded cache, so it never
# re-reads disk more often than the figures already do.
#
# This registration used to exist only on the rigs themselves, applied by hand
# beside a copy of evolver_api.py -- which meant the viewer's live column and
# tools/check_api.py both depended on a change that was in no repository, and
# check_api.py's own failure text ("Is evolver_api registered in dashboard.py?")
# was asking about a line nobody could find. It lives here now.
#
# The import handles both layouts: on a rig evolver_api.py sits next to this
# file, in a log-repo checkout it sits in tools/.
try:
    import evolver_api
except ImportError:
    import importlib.util as _ilu

    _api_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tools", "evolver_api.py")
    _spec = _ilu.spec_from_file_location("evolver_api", _api_path)
    evolver_api = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(evolver_api)
    import sys as _sys

    # evolver_api resolves the unit name through `from dashboard import
    # _REV_IPDICT` at call time; register it under its own name so that import,
    # and any other by-name lookup, finds this same module object rather than
    # loading a second copy with a second cache.
    _sys.modules.setdefault("evolver_api", evolver_api)

evolver_api.register(app.server, get_data)


if __name__ == "__main__":
    import socket
    port = 8050
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) != 0:
                break
        port += 1
    print(f"Starting on port {port}")
    app.run(debug=True, host="0.0.0.0", port=port)
