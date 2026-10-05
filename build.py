"""Build Civic Assembly panel-composition data + embeddable charts.

Usage:
  python build.py INPUT [--date 2026-10-05] [--panel-size 40] [--no-targets] [--no-png]
  python build.py --placeholder            # "results coming" pages, so embeds can be placed early

INPUT can be CSV or XLSX in any of these shapes (auto-detected):
  1. One row per panelist, one column per category (e.g. Gender, Age, Region...).
  2. Long counts:   category,value,count
  3. Long percents: category,value,percent   (converted with --panel-size)
  4. A printed Panelot "Panel Draw" PDF (see extract_pdf.py)
Category/value text may be the Panelot/registration wording or the display label.
"""
import argparse
import html
import json
import math
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
CONFIG = ROOT / "config" / "categories.csv"
SITE = ROOT / "site"
ASSEMBLY = "Cambridge Civic Assembly"

CATEGORY_ALIASES = {
    "neighborhood": "Region",
    "neighbourhood": "Region",
    "housing": "Household Type",
    "housing status": "Household Type",
    "race": "Race/Ethnicity",
    "hispanic": "Hispanic/Latinx",
    "latinx": "Hispanic/Latinx",
    "hispanic/latino": "Hispanic/Latinx",
    "gun violence": "Experience with Gun Violence",
}


def norm(s):
    s = str(s).strip().lower()
    s = re.sub(r"\(\s*\d+\s*\)", "", s)          # "(11)" neighborhood numbers
    s = re.sub(r"\(age [^)]*\)", "", s)          # "(age 25+)"
    s = s.replace("- alone", "").replace(" alone", "")
    s = re.sub(r"[^a-z0-9+]+", " ", s)
    return " ".join(s.split())


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_config():
    cfg = pd.read_csv(CONFIG, dtype=str, keep_default_na=False)
    cfg["min"] = cfg["min"].astype(int)
    cfg["max"] = cfg["max"].astype(int)
    cfg["show"] = cfg["show"].astype(int).astype(bool)
    cfg["category_order"] = cfg.groupby("category", sort=False).ngroup() + 1
    # display values in the same order Panelot shows them in the room
    cfg = cfg.assign(_o=cfg["panelot_order"].astype(int)).sort_values(["category_order", "_o"], kind="stable").drop(columns=["_o", "panelot_order"]).reset_index(drop=True)
    cfg["value_order"] = cfg.groupby("category").cumcount() + 1
    cfg["value_order"] = cfg.groupby("category").cumcount() + 1
    return cfg


def category_lookup(cfg):
    look = {}
    for _, r in cfg.drop_duplicates("category").iterrows():
        look[norm(r.category)] = r.category
        look[norm(r.category_label)] = r.category
    for k, v in CATEGORY_ALIASES.items():
        look.setdefault(norm(k), v)
    return look


def value_lookup(cfg):
    look = {}
    for _, r in cfg.iterrows():
        for key in (r.value, r.label):
            look[(r.category, norm(key))] = r.value
    return look


PDF_PROBLEMS = []


def read_any(path):
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        from extract_pdf import extract
        df, problems, _ = extract(path)
        for p in problems:
            print("PDF PROBLEM:", p)
        PDF_PROBLEMS.extend(problems)
        return {"pdf": df}, path
    if path.suffix.lower() in (".xlsx", ".xls"):
        sheets = pd.read_excel(path, sheet_name=None, dtype=str)
        # pick the sheet with the most recognizable columns
        return sheets, path
    return {"csv": pd.read_csv(path, dtype=str, keep_default_na=False)}, path


def to_counts(df, cfg, panel_size):
    """Return dict {(category, value): count} from a dataframe of any supported shape."""
    df = df.fillna("")
    cols = {norm(c): c for c in df.columns}
    cat_look, val_look = category_lookup(cfg), value_lookup(cfg)
    errors = []

    def resolve(cat, val):
        c = cat_look.get(norm(cat))
        if c is None:
            return None, None
        v = val_look.get((c, norm(val)))
        return c, v

    counts = {}
    if "category" in cols and "value" in cols and ("count" in cols or "percent" in cols):
        kind = "count" if "count" in cols else "percent"
        for _, r in df.iterrows():
            raw = str(r[cols[kind]]).strip().rstrip("%")
            if raw == "":
                continue
            c, v = resolve(r[cols["category"]], r[cols["value"]])
            if c is None or v is None:
                errors.append(f"Unrecognized: category={r[cols['category']]!r} value={r[cols['value']]!r}")
                continue
            n = float(raw)
            if kind == "percent":
                n = n * panel_size / 100
            counts[(c, v)] = counts.get((c, v), 0) + int(round(n))
        return counts, errors, f"long {kind}s"

    # wide: one row per panelist
    matched = {c: cat_look[norm(c)] for c in df.columns if norm(c) in cat_look}
    if not matched:
        return None, [f"No recognizable category columns in {list(df.columns)}"], None
    df = df[[c for c in df.columns if c in matched]]
    df = df[(df != "").any(axis=1)]
    for col, cat in matched.items():
        for val in df[col]:
            if str(val).strip() == "":
                continue
            v = val_look.get((cat, norm(val)))
            if v is None:
                errors.append(f"Unrecognized value in column {col!r}: {val!r}")
                continue
            counts[(cat, v)] = counts.get((cat, v), 0) + 1
    return counts, errors, f"one row per panelist ({len(df)} rows; columns: {', '.join(matched)})"


def build_table(cfg, counts, panel_size, draw_date):
    t = cfg.copy()
    t["count"] = [counts.get((r.category, r.value), 0) for r in t.itertuples()] if counts else None
    t["percent"] = (t["count"] / panel_size * 100).round(1) if counts else None
    t.insert(0, "draw_date", draw_date)
    t.insert(0, "assembly", ASSEMBLY)
    t["panel_size"] = panel_size
    return t


def validate(t, panel_size):
    warnings = []
    for cat, g in t[t["show"]].groupby("category", sort=False):
        total = int(g["count"].sum())
        if total != panel_size:
            warnings.append(f"{cat}: counts sum to {total}, expected {panel_size}")
        for r in g.itertuples():
            if not (r.min <= r.count <= r.max):
                warnings.append(f"{cat} / {r.value}: {r.count} outside target {r.min}-{r.max}")
    return warnings


# ---------------------------------------------------------------- HTML

FONTS = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;700&family=Noto+Sans+Condensed:wght@400;700&display=swap" rel="stylesheet">"""

CSS = """
@font-face{font-family:"Cantabrigia";src:url("https://www.cambridgema.gov/layouts/cambridge2016/_resources/fonts/cantabrigia-bt-regular.woff2") format("woff2"),url("https://www.cambridgema.gov/layouts/cambridge2016/_resources/fonts/cantabrigia-bt-regular.woff") format("woff");font-weight:700;font-display:swap}
:root{--blue:#196CC6;--dark:#1D2F8D;--pastel:#CDECFF;--grid:#E6E6E6;--band:#D9D9D9}
*{box-sizing:border-box}
body{margin:0;font-family:"Noto Sans",Aptos,Arial,sans-serif;color:#000;background:#fff;line-height:1.35}
.chart{margin:0;padding:16px 18px 12px;background:#fff}
.chart h2{font-family:"Noto Sans",Aptos,Arial,sans-serif;font-weight:700;font-size:1.25rem;margin:0 0 2px}
.chart .sub{margin:0 0 12px;font-size:.875rem}
.rows{list-style:none;margin:0;padding:0}
.row{margin:0 0 10px}
.lab{font-size:.9rem;margin-bottom:3px}
.line{display:flex;align-items:center;gap:10px}
.track{position:relative;flex:1;height:26px}
.band{position:absolute;top:0;bottom:0;background:var(--band);border-left:2px solid #000;border-right:2px solid #000}
.bar{position:absolute;top:5px;bottom:5px;left:0;background:var(--blue)}
.track::before{content:"";position:absolute;left:0;top:0;bottom:0;border-left:1px solid #000}
.val{flex:0 0 5.6em;font-size:.9rem;font-variant-numeric:tabular-nums;white-space:nowrap}
.val b{font-weight:700}
.chart figcaption,.note{font-family:"Noto Sans Condensed","Aptos Narrow",Arial,sans-serif;font-size:.8rem;margin-top:8px}
.key{display:inline-block;width:22px;height:10px;vertical-align:middle;margin-right:4px}
.key.bar-k{background:var(--blue)}
.key.band-k{background:var(--band);border-left:2px solid #000;border-right:2px solid #000;height:14px}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);border:0}
.banner{background:#FFB800;color:#000;font-weight:700;padding:6px 18px;font-size:.9rem}
.placeholder{padding:28px 18px;background:var(--pastel);font-size:1rem}
a{color:#000}
.chart.card{border:1px solid #D6DBE6;border-radius:10px;padding:18px 20px 12px}
.card h2{color:#15203B;font-size:1.3rem;margin:0 0 6px}
.pie{display:block;width:100%;height:auto;margin:0 auto}
.pie .pct{fill:#fff;font-weight:700;stroke-width:3px;paint-order:stroke;stroke-linejoin:round;font-family:"Noto Sans",Aptos,Arial,sans-serif}
.pie .lbl{fill:#15203B;font-family:"Noto Sans",Aptos,Arial,sans-serif}
.pie .leader{fill:none;stroke:#6B7389;stroke-width:1.2}
.card figcaption p{margin:4px 0 0;color:#000}
.card .src{font-size:.75rem}
.pie.narrow,.legend{display:none}
.legend{list-style:none;margin:6px 0 0;padding:0;font-size:.9rem}
.legend li{margin:3px 0;display:flex;gap:8px;align-items:baseline}
.legend .sw{flex:0 0 14px;height:14px;border-radius:3px;transform:translateY(2px)}
@media (max-width:520px){.pie.wide{display:none}.pie.narrow{display:block;max-width:240px!important}.legend{display:block}}
"""
EMBED_PIE_CSS = "body{background:#F3F5F8;padding:8px}.banner{border-radius:6px;margin-bottom:8px}"

HEIGHT_JS = """<script>
(function(){function post(){try{parent.postMessage({type:"civic-assembly-embed",slug:%s,height:Math.ceil(document.body.getBoundingClientRect().height)},"*")}catch(e){}}
addEventListener("load",post);addEventListener("resize",post);})();
</script>"""


def pct_label(p):
    return f"{int(p + 0.5)}%"  # round half up, matching Panelot


# Panelot pie palette (wraps after 8), by position in Panelot's value list
PIE_PALETTE = ["#2978D6", "#EB6833", "#1CB07A", "#EDA100", "#E87AA3", "#008200", "#4A3BA6", "#E34A47"]
CATEGORY_NOTES = {
    "Education": "Census education data is collected differently on people above or below the age of 25",
}
STYLE = {"chart": "pie"}


def _darken(hexcolor, f=0.62):
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02X%02X%02X" % (int(r * f), int(g * f), int(b * f))


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + [cur] if cur else lines


def pie_svg(cat_label, g, labels=True):
    R, LH, FS = 112, 18, 15
    wrap_chars = 22
    rows = [r for r in g.itertuples() if r.count > 0]
    total = sum(r.count for r in rows)
    slices, a0 = [], 0.0
    for r in rows:
        sweep = r.count / total * 360
        color = PIE_PALETTE[(r.value_order - 1) % len(PIE_PALETTE)]
        slices.append(dict(r=r, a0=a0, a1=a0 + sweep, mid=a0 + sweep / 2, color=color))
        a0 += sweep

    # outside labels: right side for mid-angle < 180, left otherwise; de-overlap vertically
    for s in slices:
        s["lines"] = _wrap(s["r"].label, wrap_chars)
        s["side"] = 1 if s["mid"] < 180 else -1
        s["y"] = -(R + 14) * math.cos(math.radians(s["mid"]))  # relative to cy
        s["h"] = len(s["lines"]) * LH
    for side in (1, -1):
        col = sorted([s for s in slices if s["side"] == side], key=lambda s: s["y"])
        for i in range(1, len(col)):  # push down
            prev = col[i - 1]
            min_y = prev["y"] + prev["h"] / 2 + col[i]["h"] / 2 + 6
            col[i]["y"] = max(col[i]["y"], min_y)
        if col:  # recenter if pushed too low
            over = (col[-1]["y"] + col[-1]["h"] / 2) - (R + 40)
            if over > 0:
                for s in col:
                    s["y"] -= over
                for i in range(len(col) - 2, -1, -1):
                    nxt = col[i + 1]
                    col[i]["y"] = min(col[i]["y"], nxt["y"] - nxt["h"] / 2 - col[i]["h"] / 2 - 6)
    label_w = max([len(t) * FS * 0.58 for s in slices for t in s["lines"]] + [40]) if labels else -40
    cx = 6 + label_w + R + 46
    W = 2 * cx
    lab = slices if labels else []
    top = min([-R] + [s["y"] - s["h"] / 2 for s in lab]) - 12
    bottom = max([R] + [s["y"] + s["h"] / 2 for s in lab]) + 12
    cy = -top
    H = bottom - top

    def pt(angle, rad):
        a = math.radians(angle)
        return cx + rad * math.sin(a), cy - rad * math.cos(a)

    parts = []
    for s in slices:
        r = s["r"]
        tip = html.escape(f"{r.label}: {r.count} of {total} ({pct_label(r.percent)})")
        if s["a1"] - s["a0"] >= 359.99:
            shape = f'<circle cx="{cx}" cy="{cy}" r="{R}"/>'
        else:
            x0, y0 = pt(s["a0"], R)
            x1, y1 = pt(s["a1"], R)
            large = 1 if s["a1"] - s["a0"] > 180 else 0
            shape = f'<path d="M{cx:.2f},{cy:.2f} L{x0:.2f},{y0:.2f} A{R},{R} 0 {large} 1 {x1:.2f},{y1:.2f} Z"/>'
        parts.append(f'<g fill="{s["color"]}" stroke="#fff" stroke-width="2.5"><title>{tip}</title>{shape}</g>')
    for s in slices:  # percent labels inside slices
        sweep = s["a1"] - s["a0"]
        size = 16 if sweep >= 25 else 12 if sweep >= 14 else 10
        x, y = pt(s["mid"], R * (0.62 if sweep < 330 else 0))
        parts.append(f'<text x="{x:.1f}" y="{y:.1f}" class="pct" font-size="{size}" stroke="{_darken(s["color"])}" '
                     f'dominant-baseline="central" text-anchor="middle">{pct_label(s["r"].percent)}</text>')
    for s in (slices if labels else []):  # leader lines + outside labels
        ex, ey = pt(s["mid"], R + 2)
        ax = cx + s["side"] * (R + 40)
        ly = cy + s["y"]
        tx = ax + s["side"] * 6
        anchor = "start" if s["side"] == 1 else "end"
        parts.append(f'<polyline points="{ex:.1f},{ey:.1f} {ax:.1f},{ly:.1f}" class="leader"/>')
        y0 = ly - (len(s["lines"]) - 1) * LH / 2
        tspans = "".join(f'<tspan x="{tx:.1f}" y="{y0 + i * LH:.1f}">{html.escape(t)}</tspan>'
                         for i, t in enumerate(s["lines"]))
        parts.append(f'<text class="lbl" font-size="{FS}" text-anchor="{anchor}" dominant-baseline="central">{tspans}</text>')
    cls, aria = ("wide", f'role="img" aria-label="Pie chart: {html.escape(cat_label)}"') if labels else ("narrow", 'aria-hidden="true"')
    return (f'<svg class="pie {cls}" viewBox="0 0 {W:.0f} {H:.0f}" style="max-width:{W:.0f}px" {aria} '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')


def chart_html(cat_label, g, panel_size, draw_text, show_targets, heading="h2"):
    if STYLE["chart"] == "pie" and not g["count"].isna().all():
        return pie_chart_html(cat_label, g, panel_size, draw_text, show_targets, heading)
    return bar_chart_html(cat_label, g, panel_size, draw_text, show_targets, heading)


def _data_table(cat_label, g, show_targets):
    trs = [f"<tr><th scope=row>{html.escape(r.label)}</th><td>{r.count}</td><td>{r.percent:.1f}%</td>"
           + (f"<td>{r.min}–{r.max}</td>" if show_targets else "") + "</tr>" for r in g.itertuples()]
    return ("<div class='sr-only'><table><caption>" + html.escape(cat_label) + "</caption><thead><tr><th>Group</th><th>Panelists</th><th>Percent</th>"
            + ("<th>Target range</th>" if show_targets else "") + "</tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>")


def _legend(g):
    items = "".join(
        f'<li><span class="sw" style="background:{PIE_PALETTE[(r.value_order - 1) % len(PIE_PALETTE)]}"></span>'
        f'{html.escape(r.label)} <b>{pct_label(r.percent)}</b></li>' for r in g.itertuples() if r.count > 0)
    return f'<ul class="legend" aria-hidden="true">{items}</ul>'


def pie_chart_html(cat_label, g, panel_size, draw_text, show_targets, heading="h2"):
    zero = [r.label for r in g.itertuples() if r.count == 0]
    notes = []
    if cat_label in CATEGORY_NOTES:
        notes.append(html.escape(CATEGORY_NOTES[cat_label]))
    if zero:
        notes.append("No panelists selected: " + html.escape("; ".join(zero)))
    cap = "".join(f"<p>{n}</p>" for n in notes)
    return (f'<figure class="chart card"><{heading}>{html.escape(cat_label)}</{heading}>'
            f'{pie_svg(cat_label, g)}{pie_svg(cat_label, g, labels=False)}{_legend(g)}{_data_table(cat_label, g, show_targets)}'
            f'<figcaption>{cap}<p class="src">{panel_size} panelists selected by lottery{draw_text}</p></figcaption></figure>')


def bar_chart_html(cat_label, g, panel_size, draw_text, show_targets, heading="h2"):
    if g["count"].isna().all():
        return (f'<figure class="chart"><{heading}>{html.escape(cat_label)}</{heading}>'
                f'<p class="placeholder">Panel results will be posted here after the lottery.</p></figure>')
    dom = max(g["count"].max(), g["max"].max() if show_targets else 0) or 1
    dom = dom * 1.08
    rows, trs = [], []
    for r in g.itertuples():
        pct_bar = r.count / dom * 100
        band = ""
        if show_targets:
            left, width = r.min / dom * 100, (r.max - r.min) / dom * 100
            band = f'<div class="band" style="left:{left:.2f}%;width:calc({width:.2f}% + 2px)" title="Target range {r.min}–{r.max}"></div>'
        rows.append(
            f'<li class="row"><div class="lab">{html.escape(r.label)}</div>'
            f'<div class="line"><div class="track" aria-hidden="true">{band}'
            f'<div class="bar" style="width:{pct_bar:.2f}%"></div></div>'
            f'<div class="val"><b>{r.count}</b> ({pct_label(r.percent)})</div></div></li>')
        trs.append(f"<tr><th scope=row>{html.escape(r.label)}</th><td>{r.count}</td><td>{r.percent:.1f}%</td>"
                   + (f"<td>{r.min}–{r.max}</td>" if show_targets else "") + "</tr>")
    legend = ('<span class="key bar-k"></span>Panelists selected &nbsp; '
              '<span class="key band-k"></span>Target range set before the lottery') if show_targets else ""
    table = ("<div class='sr-only'><table><caption>" + html.escape(cat_label) + "</caption><thead><tr><th>Group</th><th>Panelists</th><th>Percent</th>"
             + ("<th>Target range</th>" if show_targets else "") + "</tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>")
    return (f'<figure class="chart"><{heading}>{html.escape(cat_label)}</{heading}>'
            f'<p class="sub">{panel_size} panelists selected by lottery{draw_text}</p>'
            f'<ul class="rows" aria-hidden="true">{"".join(rows)}</ul>{table}'
            f'<figcaption>{legend}</figcaption></figure>')


def page(title, body, slug=None):
    js = HEIGHT_JS % json.dumps(slug) if slug else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title>{FONTS}<style>{CSS}{{EXTRA}}</style></head>'
            f'<body>{body}{js}</body></html>')


INDEX_CSS = """
header{background:var(--dark);color:#fff;padding:28px 24px}
header h1{font-family:"Cantabrigia","Archivo Narrow","Oswald","Noto Sans",sans-serif;font-weight:700;font-size:2.4rem;margin:0}
header p{margin:6px 0 0}
main{max-width:1100px;margin:0 auto;padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px}
.grid .chart{border:1px solid var(--grid)}
.downloads{margin:8px 0 16px}
@media (max-width:480px){.grid{grid-template-columns:1fr}}
"""


def write_site(t, panel_size, draw_date, show_targets, placeholder, banner=""):
    # clear files but keep folders (OneDrive locks directories during sync)
    if SITE.exists():
        for f in SITE.rglob("*"):
            if f.is_file():
                f.unlink()
    for d in ("embed", "data", "charts"):
        (SITE / d).mkdir(parents=True, exist_ok=True)
    (SITE / ".nojekyll").write_text("")

    draw_text = ""
    if draw_date:
        d = date.fromisoformat(draw_date)
        draw_text = f", {d.strftime('%B')} {d.day}, {d.year}"
    shown = t[t["show"]]
    slugs = []
    figs = []
    for cat, g in shown.groupby("category_order", sort=True):
        label = g["category_label"].iloc[0]
        slug = slugify(label)
        slugs.append((slug, label))
        fig = chart_html(label, g, panel_size, draw_text, show_targets)
        figs.append(fig)  # index already has a page-level banner
        if banner:
            fig = f'<div class="banner">{html.escape(banner)}</div>' + fig
        (SITE / "embed" / f"{slug}.html").write_text(
            page(f"{ASSEMBLY} panel: {label}", fig, slug).replace(
                "{EXTRA}", EMBED_PIE_CSS if STYLE["chart"] == "pie" else ""), encoding="utf-8")

    if not placeholder:
        # hidden categories (e.g. intersectional quota helpers) are left out of public data
        out = t[t["show"]].drop(columns=["show", "label"]).rename(columns={"category_label": "category_display"})
        out.insert(out.columns.get_loc("value") + 1, "value_display", t.loc[t["show"], "label"])
        out = out[["assembly", "draw_date", "panel_size", "category", "category_display", "category_order",
                   "value", "value_display", "value_order", "count", "percent", "min", "max"]]
        out = out.rename(columns={"min": "target_min", "max": "target_max"})
        out.to_csv(SITE / "data" / "panel_composition.csv", index=False, encoding="utf-8")
        out.to_json(SITE / "data" / "panel_composition.json", orient="records", indent=1)
        downloads = ('<p class="downloads">Download the data: <a href="data/panel_composition.csv">CSV</a> · '
                     '<a href="data/panel_composition.json">JSON</a></p>')
    else:
        downloads = ""

    body = ((f'<div class="banner">{html.escape(banner)}</div>' if banner else '') + f'<header><h1>Civic Assembly panel</h1><p>Who was selected for the {ASSEMBLY}{draw_text}</p></header>'
            f'<main><p>{"The panel will be drawn by public lottery. Results will appear here shortly after the draw." if placeholder else f"{panel_size} Cambridge residents were selected by lottery from everyone who registered. The lottery chose from panels built to match target ranges for each group below. Individual panelists are not identified."}</p>'
            f'{downloads}<div class="grid">{"".join(figs)}</div></main>')
    (SITE / "index.html").write_text(page(f"{ASSEMBLY} panel", body).replace("{EXTRA}", INDEX_CSS + ("body{background:#F3F5F8}main{max-width:820px}.grid{grid-template-columns:1fr}.grid .chart.card{border-color:#D6DBE6}" if STYLE["chart"] == "pie" else "")), encoding="utf-8")
    return slugs


def render_pngs_and_snippets(slugs, base_url):
    """Screenshot each embed (PNG fallback) and measure heights for iframe snippets."""
    heights = {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed; skipping PNGs")
        return heights
    with sync_playwright() as p:
        b = p.chromium.launch()
        for width in (360, 700):
            pg = b.new_page(viewport={"width": width, "height": 100}, device_scale_factor=2)
            for slug, _ in slugs:
                pg.goto((SITE / "embed" / f"{slug}.html").resolve().as_uri())
                pg.wait_for_load_state("networkidle")
                h = pg.evaluate("Math.ceil(document.body.getBoundingClientRect().height)")
                heights.setdefault(slug, {})[width] = h
                if width == 700:
                    pg.set_viewport_size({"width": width, "height": h})
                    pg.screenshot(path=str(SITE / "charts" / f"{slug}.png"))
                    pg.set_viewport_size({"width": width, "height": 100})
        b.close()
    rows = []
    for slug, label in slugs:
        h = max(heights[slug].values()) + 20
        url = f"{base_url.rstrip('/')}/embed/{slug}.html"
        iframe = (f'<iframe src="{url}" title="Civic Assembly panel: {html.escape(label)}" '
                  f'style="width:100%;max-width:760px;height:{h}px;border:0" loading="lazy"></iframe>')
        img = f'<img src="{base_url.rstrip("/")}/charts/{slug}.png" alt="{STYLE["chart"].title()} chart: Civic Assembly panel by {html.escape(label.lower())}" style="max-width:100%">'
        rows.append(f"<h3>{html.escape(label)}</h3><p>iframe:</p><pre>{html.escape(iframe)}</pre>"
                    f"<p>image fallback:</p><pre>{html.escape(img)}</pre>")
    (SITE / "embed-codes.html").write_text(
        page("Embed codes", "<main style='padding:16px;max-width:900px'><h1>Embed codes</h1>"
             "<p>Heights measured at 360px and 700px widths (+20px). Each embed also posts its height via "
             "<code>postMessage</code> ({type:'civic-assembly-embed', slug, height}) for hosts that auto-size.</p>"
             + "".join(rows) + "</main>").replace("{EXTRA}", "pre{white-space:pre-wrap;background:#f4f4f4;padding:8px}"),
        encoding="utf-8")
    return heights


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?")
    ap.add_argument("--panel-size", type=int, default=40)
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--no-targets", action="store_true")
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--placeholder", action="store_true")
    ap.add_argument("--base-url", default="https://YOUR-ORG.github.io/civic-assembly")
    ap.add_argument("--banner", default="", help='e.g. "TEST DATA - not actual results"')
    ap.add_argument("--strict", action="store_true", help="exit non-zero on validation warnings")
    ap.add_argument("--style", choices=["pie", "bar"], default="pie",
                    help="pie = Panelot-style pies (default); bar = bars with target ranges")
    a = ap.parse_args()
    STYLE["chart"] = a.style

    cfg = load_config()
    if a.placeholder:
        t = build_table(cfg, None, a.panel_size, a.date)
    else:
        if not a.input:
            ap.error("INPUT required unless --placeholder")
        sheets, _ = read_any(a.input)
        best = None
        for name, df in sheets.items():
            counts, errors, kind = to_counts(df, cfg, a.panel_size)
            if counts and (best is None or len(counts) > len(best[0])):
                best = (counts, errors, kind, name)
        if best is None:
            sys.exit("Could not interpret input:\n  " + "\n  ".join(errors))
        counts, errors, kind, sheet = best
        print(f"Read {a.input} [{sheet}] as {kind}")
        if errors:
            print("UNRECOGNIZED VALUES (fix input or add to config/categories.csv):")
            for e in errors:
                print("  -", e)
        t = build_table(cfg, counts, a.panel_size, a.date)
        warns = validate(t, a.panel_size)
        missing = [c for c in t.loc[t["show"], "category"].unique() if not any(k[0] == c for k in counts)]
        if missing:
            warns.insert(0, f"No data for categories: {missing}")
        for w in warns:
            print("WARNING:", w)
        if a.strict and (warns or errors or PDF_PROBLEMS):
            sys.exit(1)

    slugs = write_site(t, a.panel_size, a.date, not a.no_targets, a.placeholder, a.banner)
    if not a.no_png:
        render_pngs_and_snippets(slugs, a.base_url)
    print(f"Wrote {SITE} ({len(slugs)} charts)")


if __name__ == "__main__":
    main()









