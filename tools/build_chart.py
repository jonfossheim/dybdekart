"""Build the printable fishing chart (A3 landscape, 1:20 000) for Skogseidvatnet.

Reads the depth grid and features made by tools/build_data.py (app/data/) plus
OpenStreetMap context (kilder/osm-skogseid.json), writes chart/skogseid-kart.html
and prints it to chart/skogseid-kart-A3.pdf with headless Chromium.

Run from the repo root:  python tools/build_chart.py
"""
import base64
import datetime as dt
import io
import json
import math
import subprocess
from html import escape
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "app" / "data"
SRC = ROOT / "kilder"
OUT = ROOT / "chart"

LAT0 = 60.215
KX = 111320 * math.cos(math.radians(LAT0))
KY = 110540.0
SCALE = 20.0                     # metres per paper millimetre (1:20 000)
FRAME_W, FRAME_H = 262.0, 271.0  # map frame on the A3 sheet, mm
UPSAMPLE = 4                     # depth raster: 5 m cells -> 1.25 m pixels

# Depth shading, nautical style: shallow water darkest, deep water near white.
BANDS = [  # (from m, to m, colour)
    (0, 10, "#86bfdc"), (10, 20, "#a4d0e6"), (20, 40, "#c2e0ef"),
    (40, 60, "#d9ecf5"), (60, 80, "#e9f4f9"), (80, 999, "#f7fbfd"),
]
LAND = "#f1e8cd"
INK = "#1f2a30"
WATER_INK = "#2f6584"
RED = "#c2412d"


def m_of(lon, lat):
    return (np.asarray(lon) - 5.87) * KX, (np.asarray(lat) - LAT0) * KY


class Frame:
    """Metric <-> paper mm for the map frame (north up)."""

    def __init__(self, cx, cy):
        self.x0 = cx - FRAME_W * SCALE / 2
        self.y1 = cy + FRAME_H * SCALE / 2

    def mm(self, lon, lat):
        x, y = m_of(lon, lat)
        return (x - self.x0) / SCALE, (self.y1 - y) / SCALE

    def lonlat(self, px, py):
        x = self.x0 + px * SCALE
        y = self.y1 - py * SCALE
        return 5.87 + x / KX, LAT0 + y / KY


def path_d(pts, close=False):
    if len(pts) < 2:
        return ""
    s = "M" + "L".join(f"{x:.2f} {y:.2f}" for x, y in pts)
    return s + ("Z" if close else "")


def load_grid():
    meta = json.loads((DATA / "depth.json").read_text())
    depth = np.asarray(Image.open(DATA / "depth.png")).astype(float)
    return meta, depth


def depth_raster(meta, depth, rings_m):
    """Banded RGBA image of the lake at 1.25 m/px; land transparent."""
    lake = depth != meta["land"]
    d = np.where(lake, depth, -3.0)
    hi = ndimage.zoom(d, UPSAMPLE, order=1)
    H, W = hi.shape
    cell = meta["cell_m"] / UPSAMPLE
    gx0, gy1 = m_of(meta["west"], meta["north"])
    # crisp shoreline from NVE instead of the blocky grid edge
    mask = Image.new("1", (W, H), 0)
    dr = ImageDraw.Draw(mask)
    for i, ring in enumerate(rings_m):
        px = [((x - gx0) / cell, (gy1 - y) / cell) for x, y in ring]
        dr.polygon(px, fill=0 if i else 1)
    water = np.array(mask, dtype=bool)
    rgba = np.zeros((H, W, 4), np.uint8)
    hi = np.maximum(hi, 0)
    for lo, top, col in BANDS:
        sel = water & (hi >= lo) & (hi < top)
        rgba[sel] = [int(col[1:3], 16), int(col[3:5], 16), int(col[5:7], 16), 255]
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def depth_at(meta, depth, lon, lat):
    c = int((lon - meta["west"]) / (meta["east"] - meta["west"]) * meta["width"])
    r = int((meta["north"] - lat) / (meta["north"] - meta["south"]) * meta["height"])
    if 0 <= r < meta["height"] and 0 <= c < meta["width"] and depth[r, c] != meta["land"]:
        return float(depth[r, c])
    return None


def water_near(meta, depth, lon, lat, min_d=4.0, max_d=60.0):
    """Nearest lake cell to (lon, lat) whose depth is in [min_d, max_d]."""
    lake = (depth != meta["land"]) & (depth >= min_d) & (depth <= max_d)
    rr, cc = np.nonzero(lake)
    clon = meta["west"] + (cc + 0.5) * (meta["east"] - meta["west"]) / meta["width"]
    clat = meta["north"] - (rr + 0.5) * (meta["north"] - meta["south"]) / meta["height"]
    k = np.argmin(((clon - lon) * KX) ** 2 + ((clat - lat) * KY) ** 2)
    return float(clon[k]), float(clat[k])


def offset(lon, lat, dx_m, dy_m):
    return lon + dx_m / KX, lat + dy_m / KY


def line_labels(pts, text, every, first, cls, avoid, min_gap=5.0):
    """Place rotated labels along a polyline in paper mm."""
    out = []
    seg = np.hypot(*np.diff(np.asarray(pts), axis=0).T)
    cum = np.r_[0, np.cumsum(seg)]
    pos = first
    while pos < cum[-1] - 4:
        i = int(np.searchsorted(cum, pos))
        a = np.asarray(pts[max(0, i - 3)])
        b = np.asarray(pts[min(len(pts) - 1, i + 3)])
        mid = np.asarray(pts[min(i, len(pts) - 1)])
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        if ang > 90:
            ang -= 180
        elif ang < -90:
            ang += 180
        if all(math.hypot(mid[0] - x, mid[1] - y) > min_gap for x, y in avoid):
            out.append(f'<text class="{cls}" transform="translate({mid[0]:.2f} {mid[1]:.2f}) rotate({ang:.1f})">{escape(text)}</text>')
            avoid.append(tuple(mid))
        pos += every
    return out


def main():
    OUT.mkdir(exist_ok=True)
    meta, depth = load_grid()
    shore = json.loads((DATA / "shore.geojson").read_text())["features"][0]["geometry"]["coordinates"]
    rings_m = [list(zip(*m_of([p[0] for p in r], [p[1] for p in r]))) for r in shore]
    feats = json.loads((DATA / "features.geojson").read_text())["features"]
    contours = json.loads((DATA / "contours.geojson").read_text())["features"]
    osm = json.loads((SRC / "osm-skogseid.json").read_text())["elements"]

    allx = np.concatenate([[p[0] for p in r] for r in rings_m])
    ally = np.concatenate([[p[1] for p in r] for r in rings_m])
    F = Frame((allx.min() + allx.max()) / 2 + 150, (ally.min() + ally.max()) / 2)

    def P(lon, lat):
        return F.mm(lon, lat)

    lake_ring_mm = [[P(lon, lat) for lon, lat in r] for r in shore]
    svg = []
    avoid = []  # paper points that labels should keep clear of

    # --- land and context (under the water raster) ---
    svg.append(f'<rect width="{FRAME_W}" height="{FRAME_H}" fill="{LAND}"/>')
    for e in osm:
        t = e.get("tags", {})
        if "building" in t and e["type"] == "way":
            pts = [P(g["lon"], g["lat"]) for g in e["geometry"]]
            svg.append(f'<path class="bld" d="{path_d(pts, True)}"/>')
    rivers = []
    for e in osm:
        t = e.get("tags", {})
        if t.get("waterway") in ("river", "stream") and e["type"] == "way":
            pts = [P(g["lon"], g["lat"]) for g in e["geometry"]]
            svg.append(f'<path class="{t["waterway"]}" d="{path_d(pts)}"/>')
            rivers.append((t.get("name"), e["geometry"]))
    road_cls = {"primary": "r1", "secondary": "r2", "tertiary": "r2", "unclassified": "r3", "residential": "r3", "track": "r4"}
    roads = [(road_cls[e["tags"]["highway"]], [P(g["lon"], g["lat"]) for g in e["geometry"]])
             for e in osm if e["type"] == "way" and e.get("tags", {}).get("highway") in road_cls]
    for cls in ("r4", "r3", "r2", "r1"):
        for c, pts in roads:
            if c == cls and cls in ("r1", "r2"):
                svg.append(f'<path class="{cls}c" d="{path_d(pts)}"/>')
    for cls in ("r4", "r3", "r2", "r1"):
        for c, pts in roads:
            if c == cls:
                svg.append(f'<path class="{cls}" d="{path_d(pts)}"/>')

    # --- water ---
    gx0, gy1 = P(meta["west"], meta["north"])
    gx1, gy0 = P(meta["east"], meta["south"])
    png = depth_raster(meta, depth, rings_m)
    svg.append(f'<image x="{gx0:.2f}" y="{gy1:.2f}" width="{gx1 - gx0:.2f}" height="{gy0 - gy1:.2f}" preserveAspectRatio="none" href="data:image/png;base64,{png}"/>')
    for f in contours:
        pts = [P(lon, lat) for lon, lat in f["geometry"]["coordinates"]]
        svg.append(f'<path class="{"c20" if f["properties"]["major"] else "c10"}" d="{path_d(pts)}"/>')
    for r in lake_ring_mm:
        svg.append(f'<path class="shore" d="{path_d(r, True)}"/>')

    # --- pens, spots ---
    pens = [f for f in feats if f["properties"]["kind"] == "pen"]
    by_kind = {}
    for f in feats:
        by_kind.setdefault(f["properties"]["kind"], []).append(f)
    r100 = 100 / SCALE
    for f in pens:
        x, y = P(*f["geometry"]["coordinates"])
        svg.append(f'<circle class="zone" cx="{x:.2f}" cy="{y:.2f}" r="{r100:.2f}"/>')
    for f in pens:
        x, y = P(*f["geometry"]["coordinates"])
        svg.append(f'<g class="pen" transform="translate({x:.2f} {y:.2f})"><rect x="-1.1" y="-1.1" width="2.2" height="2.2"/><path d="M-1.1 0H1.1M0 -1.1V1.1"/></g>')
        avoid.append((x, y))

    # rivers meeting the lake: name the mouth
    shore_pts = np.vstack([np.c_[m_of([p[0] for p in r], [p[1] for p in r])] for r in shore[:1]])
    named_mouth, mouth_d = {}, {}
    for name, geom in rivers:
        if not name:
            continue
        for g in geom:
            x, y = m_of(g["lon"], g["lat"])
            d = float(np.min(np.hypot(shore_pts[:, 0] - x, shore_pts[:, 1] - y)))
            if d < 600 and d < mouth_d.get(name, 1e9):
                named_mouth[name], mouth_d[name] = (g["lon"], g["lat"]), d

    def nearest_river(lon, lat, max_m=700):
        best = None
        for name, (rl, ra) in named_mouth.items():
            d = math.hypot((rl - lon) * KX, (ra - lat) * KY)
            if d < max_m and (best is None or d < best[1]):
                best = (name, d)
        return best[0] if best else None

    inflow_n = next(f for f in by_kind["inflow"] if "north shore" in f["properties"]["name"])["geometry"]["coordinates"]
    inflow_ne = next(f for f in by_kind["inflow"] if "northeast" in f["properties"]["name"])["geometry"]["coordinates"]
    outlet = by_kind["outlet"][0]["geometry"]["coordinates"]
    hump = by_kind["hump"][0]
    deep = by_kind["deep"][0]["geometry"]["coordinates"]
    pen_by = {f["properties"]["name"]: f["geometry"]["coordinates"] for f in pens}
    river_ne = nearest_river(*inflow_ne) or "Inflow"
    river_n = nearest_river(*inflow_n) or "Inflow"

    # big island: largest NVE hole
    holes = sorted(shore[1:], key=lambda r: -len(r))
    isl = np.array(holes[0])
    isl_c = isl.mean(0)

    hump_top = depth_at(meta, depth, *hump["geometry"]["coordinates"])
    pen_s = pen_by["Skogseidvatnet"]
    spots = [
        (1, water_near(meta, depth, *offset(*inflow_ne, -60, -120), min_d=3), f"{river_ne} inflow",
         "Top of the long northeast arm, 0–40 m deep. Trout feed where the river comes in, especially in spring and after rain." +
         (" The river itself is closed to fishing." if river_ne == "Orraelva" else "")),
        (2, water_near(meta, depth, *offset(*pen_by["Skogseidvatnet II"], 40, -170), min_d=6), "North pens",
         f"Pens II and III next to the {river_n} inflow. Spilled feed draws big char and trout. Fish the drift side, outside the 100 m zone."),
        (3, water_near(meta, depth, *offset(*pen_by["Ospenes"], -170, 0), min_d=6), "Ospenes pen",
         "East shore pen. Same feed effect as spot 2."),
        (4, tuple(hump["geometry"]["coordinates"]), "Underwater hill",
         f"Top about {hump_top:.0f} m, roughly 30 m above the bottom around it. Jig or troll deep along the slopes for char."),
        (5, tuple(deep), "Deepest basin",
         "115 m on the 1983 map; NIVA measured 129 m by sonar. Below 20 m the water stays near 4 °C."),
        (6, water_near(meta, depth, *offset(isl_c[0], isl_c[1], 0, 260), min_d=6, max_d=16), "Island shelves",
         "Shallow shelves (0–20 m) around the islands. Trout cruise here in spring and autumn: spinners, wobblers, flies."),
        (7, water_near(meta, depth, *offset(*named_mouth["Skogseidelva"], -120, 60), min_d=4), "Skogseidelva inflow",
         "The 2021 survey found a high density of trout in Skogseidelva. Try the mouth and the drop-off outside it."),
        (8, water_near(meta, depth, *offset(*pen_s, 0, 170), min_d=4), "South pen",
         "Pen at the south tip, near the Tomraelva inflow. Same feed effect as spot 2."),
    ]
    for n, (lon, lat), *_ in spots:
        x, y = P(lon, lat)
        avoid.append((x, y))

    # contour labels on 20 m lines
    labels = []
    for f in contours:
        if not f["properties"]["major"]:
            continue
        pts = [P(lon, lat) for lon, lat in f["geometry"]["coordinates"]]
        if np.sum(np.hypot(*np.diff(np.asarray(pts), axis=0).T)) < 25:
            continue
        labels += line_labels(pts, str(f["properties"]["depth"]), 75, 20, "cl", avoid, 6)
    svg += labels

    # soundings: deepest point and hill top
    for (lon, lat), val in ((deep, "115"), (hump["geometry"]["coordinates"], f"{hump_top:.0f}")):
        x, y = P(lon, lat)
        svg.append(f'<text class="snd" x="{x:.2f}" y="{y + 6.0:.2f}">{val} m</text>')

    for n, (lon, lat), *_ in spots:
        x, y = P(lon, lat)
        svg.append(f'<g class="spot" transform="translate({x:.2f} {y:.2f})"><circle r="2.6"/><text y="0.05">{n}</text></g>')

    # lake name along the main axis
    a = P(5.8930, 60.2300)
    svg.append(f'<text class="lakename" text-anchor="middle" transform="translate({a[0]:.1f} {a[1]:.1f}) rotate(-56)">SKOGSEIDVATNET</text>')

    # place, islet, river and road names (greedy, skip collisions)
    boxes = [(x - 4, y - 4, x + 4, y + 4) for x, y in avoid]

    def free(b):
        return all(b[2] < o[0] or b[0] > o[2] or b[3] < o[1] or b[1] > o[3] for o in boxes) and \
            b[0] > 2 and b[1] > 2 and b[2] < FRAME_W - 2 and b[3] < FRAME_H - 2

    def put(text, x, y, size, cls, anchor="start"):
        w = 0.48 * size * len(text)
        x0 = x - (w / 2 if anchor == "middle" else 0)
        b = (x0 - 0.5, y - size * 0.8, x0 + w + 0.5, y + size * 0.25)
        if free(b):
            boxes.append(b)
            svg.append(f'<text class="{cls}" x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}">{escape(text)}</text>')
            return True
        return False

    # title block (top-left land), legend items live in the side column

    for (lon, lat), name in [(named_mouth[k], k) for k in named_mouth]:
        x, y = P(lon, lat)
        w = 0.48 * 2.8 * len(name)
        any(put(name, x + dx, y + dy, 2.8, "rname") for dx, dy in ((2, -1.5), (2, 3.5), (-w - 2, -1.5), (-w - 2, 3.5), (2, -5), (-w / 2, -4)))
    for e in osm:
        t = e.get("tags", {})
        if t.get("place") == "islet" and t.get("name"):
            x, y = P(e["lon"], e["lat"])
            put(t["name"], x + 1.5, y + 1, 2.4, "islet")
    rank = {"hamlet": 0, "locality": 1, "farm": 2, "isolated_dwelling": 3}
    places = [e for e in osm if e.get("tags", {}).get("place") in rank and "name" in e["tags"]]
    places.sort(key=lambda e: rank[e["tags"]["place"]])
    for e in places:
        x, y = P(e["lon"], e["lat"])
        if not (0 < x < FRAME_W and 0 < y < FRAME_H):
            continue
        if depth_at(meta, depth, e["lon"], e["lat"]) is not None:
            continue
        size = 3.3 if e["tags"]["place"] == "hamlet" else 2.6
        cls = "hamlet" if size > 3 else "farm"
        put(e["tags"]["name"], x + 1, y - 1, size, cls) or put(e["tags"]["name"], x - 1, y + 3, size, cls, "start")
    for e in osm:
        t = e.get("tags", {})
        if t.get("natural") == "peak" and t.get("name"):
            x, y = P(e["lon"], e["lat"])
            if 0 < x < FRAME_W and 0 < y < FRAME_H and put(f'{t["name"]} {t.get("ele", "")}'.strip(), x + 2.2, y + 1, 2.5, "peak"):
                svg.append(f'<path class="peakm" d="M{x - 1.2:.2f} {y + 1:.2f}L{x:.2f} {y - 1.1:.2f}L{x + 1.2:.2f} {y + 1:.2f}Z"/>')
    for name in ("Sævareidvegen", "Hålandsdalsvegen"):
        for e in osm:
            if e.get("tags", {}).get("name") == name and len(e.get("geometry", [])) > 6:
                g = e["geometry"][len(e["geometry"]) // 2]
                x, y = P(g["lon"], g["lat"])
                if put(name, x + 1.5, y - 1.2, 2.3, "road"):
                    break

    # --- graticule and frame ---
    grat, grat_txt = [], []
    lon_w, lat_n = F.lonlat(0, 0)
    lon_e, lat_s = F.lonlat(FRAME_W, FRAME_H)
    m = math.ceil(lat_s * 120)
    while m / 120 < lat_n:
        y = P(lon_w, m / 120)[1]
        L = 3 if m % 2 == 0 else 1.6
        grat.append(f'<path class="tick" d="M0 {y:.2f}h{L}M{FRAME_W} {y:.2f}h-{L}"/>')
        if m % 2 == 0:
            deg, mi = divmod(m // 2, 60)
            grat.append(f'<path class="gl" d="M0 {y:.2f}H{FRAME_W}"/>')
            grat_txt.append(f'<text class="gt" x="-1.5" y="{y + 1:.2f}" text-anchor="end">{deg}°{mi:02d}′N</text>')
        m += 1
    m = math.ceil(lon_w * 60)
    while m / 60 < lon_e:
        x = P(m / 60, lat_s)[0]
        grat.append(f'<path class="tick" d="M{x:.2f} 0v3M{x:.2f} {FRAME_H}v-3"/><path class="gl" d="M{x:.2f} 0V{FRAME_H}"/>')
        deg, mi = divmod(m, 60)
        grat_txt.append(f'<text class="gt" x="{x:.2f}" y="-1.6" text-anchor="middle">{deg}°{mi:02d}′E</text>')
        m += 1

    # scale bar + north arrow, bottom-right on land
    sx, sy = FRAME_W - 70, FRAME_H - 14
    bar = [f'<g class="scale" transform="translate({sx} {sy})">']
    for i in range(10):
        bar.append(f'<rect x="{i * 5}" y="0" width="5" height="1.6" fill="{INK if i % 2 == 0 else "#fff"}"/>')
    bar.append(f'<rect x="0" y="0" width="50" height="1.6" fill="none" stroke="{INK}" stroke-width=".2"/>')
    bar.append('<text x="0" y="-1.4">0</text><text x="25" y="-1.4" text-anchor="middle">500</text><text x="50" y="-1.4" text-anchor="middle">1000 m</text>')
    bar.append('<text x="0" y="5.6" class="sm">1:20 000 on A3. The bar stays correct at any print size.</text></g>')
    nx, ny = FRAME_W - 10, FRAME_H - 30
    bar.append(f'<g transform="translate({nx} {ny})"><path d="M0 -9L3.2 3L0 0.8L-3.2 3Z" fill="{INK}"/><text class="north" y="-10.5" text-anchor="middle">N</text></g>')

    map_svg = (f'<svg class="map" viewBox="-14 -6 {FRAME_W + 15} {FRAME_H + 8}" width="{FRAME_W + 15}mm" height="{FRAME_H + 8}mm">'
               f'<defs><clipPath id="fr"><rect width="{FRAME_W}" height="{FRAME_H}"/></clipPath></defs>'
               f'<g clip-path="url(#fr)">{"".join(svg)}{"".join(grat)}</g>'
               f'<rect width="{FRAME_W}" height="{FRAME_H}" fill="none" stroke="{INK}" stroke-width=".5"/>'
               f'{"".join(grat_txt)}{"".join(bar)}</svg>')

    key = "".join((f'<div class="sw"><i style="background:{c}"></i>{lo}–{hi} m</div>' if hi < 999 else f'<div class="sw"><i style="background:{c}"></i>{lo}+ m</div>') for lo, hi, c in BANDS)
    spot_html = "".join(f'<li><b>{n}</b><div><strong>{escape(title)}</strong> {escape(text)}</div></li>' for n, _, title, text, in spots)
    log_rows = "".join("<tr><td></td><td></td><td></td><td></td><td></td><td></td></tr>" for _ in range(7))
    today = dt.date.today().isoformat()

    html = TEMPLATE.format(map_svg=map_svg, key=key, spots=spot_html, log_rows=log_rows, today=today,
                           river_ne=escape(river_ne))
    (OUT / "skogseid-kart.html").write_text(html)
    print("rivers at the lake:", {k: round(v) for k, v in mouth_d.items()}, "| NE:", river_ne, "| N:", river_n)

    pdf = OUT / "skogseid-kart-A3.pdf"
    subprocess.run(["chromium", "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=8000",
                    f"--print-to-pdf={pdf}", (OUT / "skogseid-kart.html").as_uri()], check=True, capture_output=True)
    print("wrote", pdf.relative_to(ROOT), f"{pdf.stat().st_size / 1e6:.1f} MB")
    subprocess.run(["pdftoppm", "-r", "110", "-jpeg", "-jpegopt", "quality=85", "-singlefile", str(pdf), str(OUT / "preview")], check=True)
    print("wrote", (OUT / "preview.jpg").relative_to(ROOT))


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Skogseidvatnet fishing chart</title>
<style>
@font-face{{font-family:"Barlow";font-weight:400;src:url(fonts/barlow-400.woff2) format("woff2")}}
@font-face{{font-family:"Barlow";font-weight:600;src:url(fonts/barlow-600.woff2) format("woff2")}}
@font-face{{font-family:"Barlow Condensed";font-weight:500;src:url(fonts/barlow-condensed-500.woff2) format("woff2")}}
@font-face{{font-family:"Barlow Condensed";font-weight:600;src:url(fonts/barlow-condensed-600.woff2) format("woff2")}}
@font-face{{font-family:"Barlow Condensed";font-weight:700;src:url(fonts/barlow-condensed-700.woff2) format("woff2")}}
@font-face{{font-family:"Spectral";font-style:italic;font-weight:400;src:url(fonts/spectral-400-italic.woff2) format("woff2")}}
@font-face{{font-family:"Spectral";font-style:italic;font-weight:600;src:url(fonts/spectral-600-italic.woff2) format("woff2")}}
/* Sheet: A3 landscape. Map frame left at 1:20 000, notes column right. */
@page{{size:420mm 297mm;margin:0}}
*{{box-sizing:border-box}}
html,body{{margin:0;background:#fff;color:{ink}}}
body{{font:400 8.4pt/1.35 "Barlow",sans-serif;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.sheet{{width:420mm;height:297mm;padding:8mm 10mm 8mm 8mm;display:grid;grid-template-columns:{mapw}mm 1fr;gap:7mm;overflow:hidden}}
.map{{display:block}}
.map .bld{{fill:#8b8173}}
.map .river{{fill:none;stroke:#5a9cc4;stroke-width:.45;stroke-linecap:round}}
.map .stream{{fill:none;stroke:#7ab2d3;stroke-width:.25;stroke-linecap:round}}
.map .r1c,.map .r2c{{fill:none;stroke:#6e5f4b;stroke-linecap:round;stroke-linejoin:round}}
.map .r1c{{stroke-width:1.1}} .map .r2c{{stroke-width:.85}}
.map .r1,.map .r2{{fill:none;stroke-linecap:round;stroke-linejoin:round}}
.map .r1{{stroke:#f2b866;stroke-width:.75}} .map .r2{{stroke:#fbe1a6;stroke-width:.5}}
.map .r3{{fill:none;stroke:#7d6f5c;stroke-width:.28}}
.map .r4{{fill:none;stroke:#7d6f5c;stroke-width:.22;stroke-dasharray:1 .6}}
.map .c10{{fill:none;stroke:{water};stroke-width:.12;opacity:.55}}
.map .c20{{fill:none;stroke:{water};stroke-width:.28}}
.map .shore{{fill:none;stroke:{ink};stroke-width:.32;stroke-linejoin:round}}
.map .cl{{font:600 2.3px "Barlow Condensed",sans-serif;fill:{water};text-anchor:middle;dominant-baseline:central;paint-order:stroke;stroke:#eef7fb;stroke-width:.7}}
.map .snd{{font:italic 600 3px "Spectral",serif;fill:{water};text-anchor:middle;paint-order:stroke;stroke:#fff;stroke-width:.6}}
.map .lakename{{font:italic 400 4.6px "Spectral",serif;letter-spacing:1.6px;fill:{water};opacity:.7}}
.map .zone{{fill:{red};fill-opacity:.1;stroke:{red};stroke-width:.28;stroke-dasharray:1 .7}}
.map .pen rect{{fill:#fff;stroke:{red};stroke-width:.35}} .map .pen path{{stroke:{red};stroke-width:.3}}
.map .spot circle{{fill:{red};stroke:#fff;stroke-width:.45}}
.map .spot text{{font:700 3.3px "Barlow Condensed",sans-serif;fill:#fff;text-anchor:middle;dominant-baseline:central}}
.map .title{{font:700 9px "Barlow Condensed",sans-serif;letter-spacing:.6px;fill:{ink}}}
.map .sub{{font:500 3px "Barlow Condensed",sans-serif;letter-spacing:.15px;fill:#4c5a61}}
.map .hamlet{{font:600 3.3px "Barlow Condensed",sans-serif;fill:#3a3127;paint-order:stroke;stroke:{land};stroke-width:.6}}
.map .farm{{font:500 2.6px "Barlow Condensed",sans-serif;fill:#5a4d3e;paint-order:stroke;stroke:{land};stroke-width:.5}}
.map .peak{{font:500 2.5px "Barlow Condensed",sans-serif;fill:#5a4d3e}}
.map .peakm{{fill:#5a4d3e}}
.map .road{{font:500 2.3px "Barlow Condensed",sans-serif;fill:#6e5f4b;letter-spacing:.1px}}
.map .rname{{font:italic 400 2.8px "Spectral",serif;fill:#2e6f99;paint-order:stroke;stroke:{land};stroke-width:.6}}
.map .islet{{font:italic 400 2.4px "Spectral",serif;fill:#3a3127}}
.map .gl{{stroke:#6e7d84;stroke-width:.1;opacity:.45}}
.map .tick{{stroke:{ink};stroke-width:.25}}
.map .gt{{font:500 2.5px "Barlow Condensed",sans-serif;fill:{ink}}}
.map .scale text{{font:500 2.6px "Barlow Condensed",sans-serif;fill:{ink}}}
.map .scale .sm{{font-size:2.2px;fill:#4c5a61}}
.map .north{{font:700 3.6px "Barlow Condensed",sans-serif;fill:{ink}}}
aside{{display:flex;flex-direction:column;gap:3mm;padding-top:4mm;min-width:0}}
h1{{margin:0;font:700 22pt/1 "Barlow Condensed",sans-serif;letter-spacing:.04em;text-transform:uppercase}}
.lede{{margin:1mm 0 0;font:500 9.5pt/1.3 "Barlow Condensed",sans-serif;color:#4c5a61;letter-spacing:.02em}}
h2{{margin:0 0 1.3mm;font:700 8.6pt/1 "Barlow Condensed",sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#4c5a61;border-bottom:.25mm solid {ink};padding-bottom:1mm}}
.keys{{display:grid;grid-template-columns:1.1fr 1fr;gap:2mm 5mm}}
.sw{{display:flex;align-items:center;gap:1.6mm;white-space:nowrap;font-variant-numeric:tabular-nums}}
.sw i{{width:5mm;height:3.2mm;border:.15mm solid #9fb0b6;flex:none}}
.bands{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1mm 3mm}}
.sym{{display:grid;grid-template-columns:7mm 1fr;gap:1.1mm 2mm;align-items:center}}
.sym svg{{width:7mm;height:3.6mm;overflow:visible}}
ol{{margin:0;padding:0;list-style:none;display:grid;gap:1.6mm}}
ol li{{display:grid;grid-template-columns:4.6mm 1fr;gap:2mm;align-items:start}}
ol li b{{width:4.4mm;height:4.4mm;border-radius:50%;background:{red};color:#fff;display:grid;place-items:center;font:700 8pt "Barlow Condensed",sans-serif}}
ol li strong{{font-weight:600}}
ul{{margin:0;padding-left:3.6mm;display:grid;gap:1mm}}
p{{margin:0}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:5mm}}
.rules{{border:.3mm solid {red};padding:2mm 2.6mm;color:#6d2417}}
.rules h2{{color:{red};border-color:{red}}}
table{{width:100%;border-collapse:collapse;font-size:7.6pt}}
th{{text-align:left;font:600 7.4pt "Barlow Condensed",sans-serif;letter-spacing:.06em;text-transform:uppercase;color:#4c5a61;border-bottom:.3mm solid {ink};padding:.6mm 1mm}}
td{{border-bottom:.15mm solid #9aa6ab;height:6.2mm}}
.src{{margin-top:auto;font-size:6.6pt;line-height:1.35;color:#5d6a70}}
</style></head>
<body><div class="sheet">
{map_svg}
<aside>
  <div>
    <h1>Skogseidvatnet</h1>
    <p class="lede">Fishing chart · Bjørnafjorden · NVE lake 2043 · 5.26 km² · deepest 115–129 m · mean 44 m</p>
  </div>
  <div class="keys">
    <div>
      <h2>Depth (m)</h2>
      <div class="bands">{key}</div>
    </div>
    <div>
      <h2>Symbols</h2>
      <div class="sym">
        <svg viewBox="0 0 10 5"><path d="M0 2.5H10" stroke="{water}" stroke-width=".9"/></svg><span>20 m contour</span>
        <svg viewBox="0 0 10 5"><path d="M0 2.5H10" stroke="{water}" stroke-width=".4" opacity=".6"/></svg><span>10 m contour (interpolated)</span>
        <svg viewBox="0 0 10 5"><circle cx="5" cy="2.5" r="2.4" fill="{red}" fill-opacity=".1" stroke="{red}" stroke-width=".35" stroke-dasharray="1 .7"/><rect x="4.1" y="1.6" width="1.8" height="1.8" fill="#fff" stroke="{red}" stroke-width=".35"/></svg><span>Fish farm pen, 100 m no-go zone</span>
        <svg viewBox="0 0 10 5"><path d="M0 2.5H10" stroke="#5a9cc4" stroke-width=".9"/></svg><span>River / stream</span>
      </div>
    </div>
  </div>
  <div>
    <h2>Spots</h2>
    <ol>{spots}</ol>
  </div>
  <div class="cols">
    <div>
      <h2>Fish</h2>
      <ul>
        <li>Brown trout, Arctic char and escaped farmed salmon. A 2020–21 net survey caught 411 trout, 21 char and 17 salmon.</li>
        <li>Norwegian record char: 8.285 kg (2002). A 6 kg char was taken through the ice at 20 m in March 2018, on a black and yellow spoon with maggot.</li>
        <li>Reported for big char: 100 mm Dividal spoons with UV, maggot or PowerBait, sinking fly lines. Fish near the pens.</li>
      </ul>
    </div>
    <div>
      <h2>Water</h2>
      <ul>
        <li>Summer and autumn: warm top layer down to 15–20 m, then a sharp drop to about 4 °C to the bottom. Char often hold just below that drop.</li>
        <li>Oxygen is good all the way down. Visibility about 6 m.</li>
        <li>The lake drains west to Henangervatnet.</li>
      </ul>
    </div>
  </div>
  <div class="rules">
    <h2>Rules</h2>
    <ul>
      <li>Fishing card on inatur.no (Hålandsdalen Grunneigarlag). Report your catch.</li>
      <li>Keep at least 100 m from the pens. Max 6 hp. Wear life vests.</li>
      <li>Release trout over 3 kg. Keep big char.</li>
      <li>Sources disagree on daily quota, minimum size and net fishing. Check before you go.</li>
    </ul>
  </div>
  <div>
    <h2>Catch log</h2>
    <table><thead><tr><th style="width:13%">Date</th><th style="width:9%">Spot</th><th style="width:15%">Species</th><th style="width:10%">kg</th><th style="width:11%">Depth</th><th>Lure / note</th></tr></thead>
    <tbody>{log_rows}</tbody></table>
  </div>
  <p class="src">Depths: Skogheim 1983 (20 m contours, from Rådgivende Biologer report 3892), fitted to NVE's shoreline; 10 m lines and spot depths are interpolated. NIVA measured 129 m by sonar in 1994. Pens: Fiskeridirektoratet registered site positions. Background map © OpenStreetMap contributors (ODbL). Fish and rules: Rådgivende Biologer 3478, flue.no, fiskeavisen.no, inatur.no, skogseidvatnet.no. Made {today}.</p>
</aside>
</div></body></html>
"""

TEMPLATE = TEMPLATE.replace("{ink}", INK).replace("{water}", WATER_INK).replace("{red}", RED).replace("{land}", LAND).replace("{mapw}", str(FRAME_W + 15))

if __name__ == "__main__":
    main()
