"""Extract panel composition counts from a printed Panelot "Panel Draw" PDF.

The PDF's text is unreadable (Type3 fonts), so this reads the pie charts' vector shapes:
  - slice angle  -> count (each panelist = 360/panel_size degrees)
  - slice color  -> position in Panelot's value list (palette index), which also reveals
                    zero-count values that have no slice
Pies are assigned to categories in page order (Gender, Age, Neighborhood, ...).

Usage: python extract_pdf.py "input/Panel Draw.pdf" [-o input/from_pdf.csv] [--panel-size 40]
"""
import argparse
import math
import sys
from pathlib import Path

import pandas as pd
import pymupdf

CONFIG = Path(__file__).parent / "config" / "categories.csv"

# Panelot pie palette, in order (RGB 0-1, as rendered in the PDF)
PALETTE = [
    (0.16, 0.47, 0.84),  # blue
    (0.92, 0.41, 0.20),  # orange
    (0.11, 0.69, 0.48),  # green
    (0.93, 0.63, 0.00),  # yellow
    (0.91, 0.48, 0.64),  # pink
    (0.00, 0.51, 0.00),  # dark green
    (0.29, 0.23, 0.65),  # purple
    (0.89, 0.29, 0.28),  # red
]


def angle(center, pt):
    """Clockwise degrees from 12 o'clock (page y grows downward)."""
    return math.degrees(math.atan2(pt.x - center.x, -(pt.y - center.y))) % 360


def path_points(items):
    pts = []
    for it in items:
        if it[0] == "l":
            pts += [it[1], it[2]]
        elif it[0] == "c":
            pts += [it[1], it[4]]
    return pts


def slice_geometry(dr):
    items = dr["items"]
    if not items or items[0][0] != "l" or not any(i[0] == "c" for i in items):
        return None
    center = items[0][1]
    arc = [it for it in items if it[0] == "c"]
    start, end = arc[0][1], arc[-1][4]
    # accumulate signed angular steps along the arc so direction/wrap don't matter
    pts = [arc[0][1]] + [it[4] for it in arc]
    sweep = 0.0
    for a, b in zip(pts, pts[1:]):
        d = (angle(center, b) - angle(center, a) + 180) % 360 - 180
        sweep += d
    a0 = angle(center, start)
    if sweep < 0:  # drawn counter-clockwise: normalize to clockwise start
        a0, sweep = angle(center, end), -sweep
    radius = math.dist((center.x, center.y), (start.x, start.y))
    return dict(center=(round(center.x, 1), round(center.y, 1)), start_pt=(start.x, start.y),
                start=a0, sweep=sweep, radius=radius)


def palette_index(rgb):
    if rgb is None:
        return None
    best = min(range(len(PALETTE)), key=lambda i: sum((a - b) ** 2 for a, b in zip(PALETTE[i], rgb)))
    return best if sum((a - b) ** 2 for a, b in zip(PALETTE[best], rgb)) < 0.01 else None


def find_pies(pdf):
    pies = []
    for pn, page in enumerate(pymupdf.open(pdf)):
        drs = page.get_drawings()
        fills = [(slice_geometry(d), d["fill"]) for d in drs if d["type"] == "f" and d.get("fill")]
        fills = [(g, f) for g, f in fills if g and g["radius"] > 20]
        outlines = [slice_geometry(d) for d in drs
                    if d["type"] == "s" and d.get("color") and min(d["color"]) > 0.98]
        outlines = [g for g in outlines if g and g["radius"] > 20]
        by_center = {}
        for g in outlines:
            # attach fill color from the fill path sharing center + start point
            color = None
            for fg, f in fills:
                if math.dist(fg["center"], g["center"]) < 1 and math.dist(fg["start_pt"], g["start_pt"]) < 1:
                    color = f
                    break
            g["palette"] = palette_index(color)
            g["rgb"] = color
            key = next((k for k in by_center if math.dist(k, g["center"]) < 2), g["center"])
            by_center.setdefault(key, []).append(g)
        for c, slices in sorted(by_center.items(), key=lambda kv: kv[0][1]):
            pies.append(dict(page=pn + 1, center=c, slices=sorted(slices, key=lambda s: s["start"] % 360)))
    return pies


def extract(pdf, panel_size=40):
    cfg = pd.read_csv(CONFIG, dtype=str, keep_default_na=False)
    cfg = cfg[cfg["show"] == "1"].copy()
    cfg["panelot_order"] = cfg["panelot_order"].astype(int)
    cats = list(dict.fromkeys(cfg["category"]))
    pies = find_pies(pdf)
    problems = []
    if len(pies) != len(cats):
        problems.append(f"Found {len(pies)} pie charts, expected {len(cats)} ({', '.join(cats)})")
    rows = []
    for pie, cat in zip(pies, cats):
        vals = cfg[cfg["category"] == cat].sort_values("panelot_order")
        values = list(vals["value"])
        mins, maxs = dict(zip(vals.value, vals["min"].astype(int))), dict(zip(vals.value, vals["max"].astype(int)))
        total = sum(s["sweep"] for s in pie["slices"])
        if abs(total - 360) > 1:
            problems.append(f"{cat}: slices cover {total:.1f} degrees, not 360")
        # assign palette indexes; slices with no fill color get the next free index
        idx, assigned = -1, []
        for s in pie["slices"]:
            p = s["palette"]
            if p is None:
                p = idx + 1
                s["inferred"] = True
            elif p <= idx:
                # palette wraps after 8 colors: take the next position with this color
                p = idx + 1 + ((p - (idx + 1)) % len(PALETTE))
            idx = p
            assigned.append(p)
        if assigned and assigned[-1] >= len(values):
            problems.append(f"{cat}: {len(pie['slices'])} slices but palette positions {assigned} exceed {len(values)} values")
            continue
        counts = {v: 0 for v in values}
        for s, p in zip(pie["slices"], assigned):
            exact = s["sweep"] / 360 * panel_size
            n = round(exact)
            if abs(exact - n) > 0.15:
                problems.append(f"{cat}/{values[p]}: slice is {exact:.2f} panelists (not a whole number)")
            counts[values[p]] = n
        inferred = [values[p] for s, p in zip(pie["slices"], assigned) if s.get("inferred")]
        for v, n in counts.items():
            rows.append(dict(category=cat, value=v, count=n))
            if not (mins[v] <= n <= maxs[v]):
                problems.append(f"{cat}/{v}: {n} outside target {mins[v]}-{maxs[v]} (check slice/label mapping)")
        if inferred:
            print(f"  note: {cat}: color missing for {inferred}; assigned by position")
        if sum(counts.values()) != panel_size:
            problems.append(f"{cat}: counts sum to {sum(counts.values())}, expected {panel_size}")
    return pd.DataFrame(rows), problems, pies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default=str(Path(__file__).parent / "input" / "from_pdf.csv"))
    ap.add_argument("--panel-size", type=int, default=40)
    a = ap.parse_args()
    df, problems, pies = extract(a.pdf, a.panel_size)
    for cat, g in df.groupby("category", sort=False):
        print(f"{cat}: " + ", ".join(f"{v} {n}" for v, n in zip(g.value, g["count"])))
    if problems:
        print("\nPROBLEMS - verify against the Panelot screen before publishing:")
        for p in problems:
            print("  -", p)
    else:
        print("\nAll checks passed (8 pies, whole-number slices, totals = %d, all within targets)." % a.panel_size)
    df.to_csv(a.out, index=False)
    print(f"Wrote {a.out}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
