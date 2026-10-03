"""Build the app's depth data for Skogseidvatnet from the sources in kilder/.

Pipeline:
  1. Rasterise the 1983 depth bands (Skogheim 1983, vector figure in report 3892)
     from kilder/skogseid-konturer-1983.svg.
  2. Warp the bands onto NVE's shoreline (lake 2043): affine ICP, then a
     thin-plate spline fitted on shoreline correspondences.
  3. Interpolate a continuous depth grid between the 20 m contour bands.
  4. Write app/data/: depth.png (8-bit grid, 255 = land), depth.json (grid
     georeference), contours.geojson, shore.geojson, features.geojson.

Requires rsvg-convert on PATH and the packages in tools/requirements.txt.
Run from the repo root:  python tools/build_data.py
"""
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.interpolate import RBFInterpolator
from scipy.spatial import cKDTree
from shapely.geometry import LineString, mapping
from skimage import measure

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "kilder"
OUT = ROOT / "app" / "data"

SVG_VIEWBOX = (185, 205, 350, 395)  # x, y, w, h of the contour SVG
SVG_PX_PER_UNIT = 8
CELL_M = 5.0
BAND_STEP = 20  # metres per band in the 1983 map
MAX_DEPTH_1983 = 115
LAT0 = 60.215
KX = 111320 * math.cos(math.radians(LAT0))
KY = 110540.0

# Features read off the 1983 figure, in SVG user units (x, y down).
SVG_FEATURES = [
    ("inflow", "Inflow (north shore)", 304.5, 393.5),
    ("inflow", "Inflow (northeast arm)", 512.0, 221.0),
    ("outlet", "Outlet to Henangervatnet", 191.0, 526.0),
    ("hump", "Underwater hill", 335.0, 451.0),
]
IN_LAKE_PENS = {12042, 12096, 27956, 28796}
SHORE_SITES = {12041, 10145}


def to_m(lon, lat):
    return np.c_[(np.asarray(lon) - 5.87) * KX, (np.asarray(lat) - LAT0) * KY]


def to_lonlat(xy):
    xy = np.atleast_2d(xy)
    return np.c_[5.87 + xy[:, 0] / KX, LAT0 + xy[:, 1] / KY]


def render_band_raster():
    """Rasterise depth bands; returns (band index image, -1 = no water)."""
    svg = (SRC / "skogseid-konturer-1983.svg").read_text()
    levels = {f"d{k}": 20 + 40 * k for k in range(6)}
    style = "".join(f".{c}{{fill:rgb({v},{v},{v})}}" for c, v in levels.items())
    style += ".ink,.ln{display:none}"
    svg = re.sub(r"<style>.*?</style>", f"<style>{style}</style>", svg, flags=re.S)
    w = SVG_VIEWBOX[2] * SVG_PX_PER_UNIT
    with tempfile.TemporaryDirectory() as td:
        src, png = Path(td) / "b.svg", Path(td) / "b.png"
        src.write_text(svg)
        subprocess.run(["rsvg-convert", "-w", str(w), str(src), "-o", str(png)], check=True)
        im = np.asarray(Image.open(png).convert("LA"), dtype=np.int16)
    grey, alpha = im[..., 0], im[..., 1]
    band = np.clip(np.rint((grey - 20) / 40), 0, 5).astype(np.int8)
    band[alpha < 128] = -1
    return band


def nve_lake():
    feat = json.loads((SRC / "nve-innsjo-2043.geojson").read_text())["features"][0]
    rings = feat["geometry"]["coordinates"]
    return [to_m([p[0] for p in r], [p[1] for p in r]) for r in rings]


def densify(ring, step):
    out = []
    for a, b in zip(ring[:-1], ring[1:]):
        n = max(1, int(np.hypot(*(b - a)) / step))
        out += [a + (b - a) * i / n for i in range(n)]
    return np.array(out)


def svg_boundary(band):
    water = band >= 0
    water = ndimage.binary_fill_holes(ndimage.binary_closing(water, iterations=2))
    lab, n = ndimage.label(water)
    water = lab == (np.argmax(ndimage.sum(water, lab, range(1, n + 1))) + 1)
    edge = water ^ ndimage.binary_erosion(water)
    ys, xs = np.nonzero(edge)
    u = SVG_VIEWBOX[0] + (xs + 0.5) / SVG_PX_PER_UNIT
    v = SVG_VIEWBOX[1] + (ys + 0.5) / SVG_PX_PER_UNIT
    return np.c_[u, v][::4]


def fit_warp(svg_pts, shore_m):
    """Return f(metric xy) -> svg uv, fitted so the 1983 shoreline meets NVE's."""
    A = np.c_[svg_pts[:, 0], -svg_pts[:, 1]]
    tree_g = cKDTree(shore_m)
    # affine ICP, svg -> metric, initialised from the scale bar (15.09 m per unit)
    sc = 1500 / 99.4
    L, t = np.eye(2) * sc, shore_m.mean(0) - sc * A.mean(0)
    for _ in range(80):
        X = A @ L.T + t
        _, i1 = tree_g.query(X)
        _, i2 = cKDTree(X).query(shore_m)
        aa, bb = np.r_[A, A[i2]], np.r_[shore_m[i1], shore_m]
        M = np.linalg.lstsq(np.c_[aa, np.ones(len(aa))], bb, rcond=None)[0]
        L, t = M[:2].T, M[2]
    X = A @ L.T + t
    # thin-plate refinement: progressively less smoothing
    shore_s = shore_m[:: max(1, len(shore_m) // 600)]
    warp_x = X.copy()
    for smooth in (3000.0, 600.0, 150.0):
        tree_x = cKDTree(warp_x)
        _, j = tree_x.query(shore_s)
        rbf = RBFInterpolator(warp_x[j], shore_s, kernel="thin_plate_spline", smoothing=smooth)
        warp_x = rbf(warp_x)
    d, _ = tree_g.query(warp_x)
    d2, _ = cKDTree(warp_x).query(shore_m)
    print(f"shoreline fit: mean {d.mean():.0f} m / {d2.mean():.0f} m, p90 {np.percentile(d, 90):.0f} / {np.percentile(d2, 90):.0f} m")
    inv = RBFInterpolator(warp_x[::2], svg_pts[::2], kernel="thin_plate_spline", smoothing=5.0)
    fwd = RBFInterpolator(svg_pts[::2], warp_x[::2], kernel="thin_plate_spline", smoothing=5.0)
    return inv, fwd


def interpolate_depth(band, lake):
    """Continuous depth from band indices; band -1 inside the lake counts as band 0."""
    band = np.where(lake & (band < 0), 0, band)
    band = np.where(lake, band, -1)
    depth = np.full(band.shape, np.nan)
    land = band < 0
    for k in range(6):
        cells = band == k
        if not cells.any():
            continue
        shallower = land | ((band >= 0) & (band < k))
        deeper = band > k
        d_sh = ndimage.distance_transform_edt(~shallower) if shallower.any() else None
        d_dp = ndimage.distance_transform_edt(~deeper) if deeper.any() else None
        lab, n = ndimage.label(cells)
        grown_sh = ndimage.binary_dilation(shallower)
        grown_dp = ndimage.binary_dilation(deeper)
        for c in range(1, n + 1):
            comp = lab == c
            lo, hi = BAND_STEP * k, BAND_STEP * (k + 1)
            touch_sh = (comp & grown_sh).any()
            touch_dp = (comp & grown_dp).any()
            if touch_sh and touch_dp:
                a, b = d_sh[comp], d_dp[comp]
                depth[comp] = lo + BAND_STEP * a / (a + b)
            elif touch_sh:  # local deep: deepen towards the middle of the patch
                a = d_sh[comp]
                top = MAX_DEPTH_1983 - lo if k == 5 else BAND_STEP / 2
                depth[comp] = lo + top * a / a.max()
            elif touch_dp:  # local hump: shoal towards the middle of the patch
                b = d_dp[comp]
                depth[comp] = hi - (BAND_STEP / 2) * b / b.max()
            else:
                depth[comp] = (lo + hi) / 2
    # light smoothing inside the lake only
    w = ndimage.gaussian_filter(lake.astype(float), 1.5)
    s = ndimage.gaussian_filter(np.where(lake, depth, 0.0), 1.5)
    depth = np.where(lake, s / np.maximum(w, 1e-6), np.nan)
    return depth


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    band_img = render_band_raster()
    rings = nve_lake()
    shore = np.vstack([densify(r, 10) for r in rings[:1]])
    inv, fwd = fit_warp(svg_boundary(band_img), shore)

    # grid aligned to lon/lat so it drops straight onto the map
    allm = np.vstack(rings)
    pad = 60
    (x0, y0), (x1, y1) = allm.min(0) - pad, allm.max(0) + pad
    dlon, dlat = CELL_M / KX, CELL_M / KY
    west, south = 5.87 + x0 / KX, LAT0 + y0 / KY
    W = int(math.ceil((x1 - x0) / CELL_M))
    H = int(math.ceil((y1 - y0) / CELL_M))
    east, north = west + W * dlon, south + H * dlat
    gx = x0 + (np.arange(W) + 0.5) * CELL_M
    gy = y1 - (np.arange(H) + 0.5) * CELL_M  # row 0 = north
    GX, GY = np.meshgrid(gx, gy)

    # lake mask from NVE (outer ring minus islands)
    def raster(ring):
        img = Image.new("1", (W, H), 0)
        px = [((p[0] - x0) / CELL_M, (y1 - p[1]) / CELL_M) for p in ring]
        ImageDraw.Draw(img).polygon(px, fill=1)
        return np.array(img, dtype=bool)

    lake = raster(rings[0])
    for r in rings[1:]:
        lake &= ~raster(r)

    # sample the 1983 bands through the inverse warp
    uv = inv(np.c_[GX[lake], GY[lake]])
    col = np.floor((uv[:, 0] - SVG_VIEWBOX[0]) * SVG_PX_PER_UNIT).astype(int)
    row = np.floor((uv[:, 1] - SVG_VIEWBOX[1]) * SVG_PX_PER_UNIT).astype(int)
    ok = (col >= 0) & (col < band_img.shape[1]) & (row >= 0) & (row < band_img.shape[0])
    band = np.full((H, W), -1, np.int8)
    vals = np.full(len(uv), -1, np.int8)
    vals[ok] = band_img[row[ok], col[ok]]
    band[lake] = vals
    print(f"lake cells with a 1983 band: {(vals >= 0).mean():.1%}")

    depth = interpolate_depth(band, lake)
    img = np.where(lake, np.clip(np.rint(depth), 0, 250), 255).astype(np.uint8)
    Image.fromarray(img, "L").save(OUT / "depth.png", optimize=True)
    deepest = np.unravel_index(np.nanargmax(depth), depth.shape)
    print(f"grid {W}x{H} @ {CELL_M} m, max {np.nanmax(depth):.0f} m, mean {np.nanmean(depth):.1f} m")

    (OUT / "depth.json").write_text(json.dumps({
        "west": west, "south": south, "east": east, "north": north,
        "width": W, "height": H, "cell_m": CELL_M, "land": 255,
        "source": "Skogheim 1983 (20 m contours) via Rådgivende Biologer rapport 3892; warped to NVE shoreline 2043; depths between contours are interpolated.",
    }, indent=1))

    # contours: 20 m lines come from the source, 10 m lines are interpolated
    padded = np.where(lake, depth, -5.0)
    feats = []
    for level in range(10, int(np.nanmax(depth)) + 1, 10):
        for c in measure.find_contours(padded, level):
            if len(c) < 8:
                continue
            xs = x0 + (c[:, 1] + 0.5) * CELL_M
            ys = y1 - (c[:, 0] + 0.5) * CELL_M
            line = LineString(to_lonlat(np.c_[xs, ys])).simplify(1.5 / KY)
            if line.length * KY < 60:
                continue
            feats.append({"type": "Feature", "properties": {"depth": level, "major": level % 20 == 0},
                          "geometry": mapping(line)})
    (OUT / "contours.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))

    shore_feat = json.loads((SRC / "nve-innsjo-2043.geojson").read_text())["features"][0]
    (OUT / "shore.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": "Skogseidvatnet", "source": "NVE Innsjødatabase 2043"},
        "geometry": shore_feat["geometry"]}]}, separators=(",", ":")))

    features = []
    sites = json.loads((SRC / "fiskeridir-lokaliteter-4624.json").read_text())
    for s in sites:
        if s["siteNr"] in IN_LAKE_PENS | SHORE_SITES:
            features.append({"type": "Feature", "properties": {
                "kind": "pen" if s["siteNr"] in IN_LAKE_PENS else "shore_site",
                "name": s["name"].title().replace(" Iii", " III").replace(" Ii", " II"), "siteNr": s["siteNr"], "source": "Fiskeridirektoratet"},
                "geometry": {"type": "Point", "coordinates": [s["longitude"], s["latitude"]]}})
    lake_rc = np.argwhere(lake)
    shore_dist = ndimage.distance_transform_edt(lake)
    lake_xy = np.c_[gx[lake_rc[:, 1]], gy[lake_rc[:, 0]]]
    lake_tree = cKDTree(lake_xy)
    for kind, name, u, v in SVG_FEATURES:
        p = fwd(np.array([[u, v]]))[0]
        if kind == "hump":  # open-water local minimum nearest the figure's hump
            land_d = np.where(lake, depth, 999.0)
            low = ndimage.minimum_filter(land_d, size=41)
            high = ndimage.maximum_filter(np.where(lake, depth, -1.0), size=81)
            cand = np.argwhere(lake & (land_d == low) & (shore_dist > 150 / CELL_M) & (high - land_d > 20))
            cxy = np.c_[gx[cand[:, 1]], gy[cand[:, 0]]]
            r, c = cand[np.argmin(np.hypot(*(cxy - p).T))]
            p = np.array([gx[c], gy[r]])
            name = f"Underwater hill: top ~{depth[r, c]:.0f} m, bottom around it ~{high[r, c]:.0f} m"
        else:  # inflows/outlet sit on the shoreline: snap to nearest lake cell
            p = lake_xy[lake_tree.query(p)[1]]
        lon, lat = to_lonlat(p)[0]
        features.append({"type": "Feature", "properties": {"kind": kind, "name": name, "source": "Skogheim 1983"},
                         "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}})
    dlon_, dlat_ = to_lonlat(np.array([[GX[deepest], GY[deepest]]]))[0]
    features.append({"type": "Feature", "properties": {
        "kind": "deep", "name": f"Deepest on 1983 map (~{MAX_DEPTH_1983} m; NIVA sonar found 129 m)", "source": "Skogheim 1983"},
        "geometry": {"type": "Point", "coordinates": [round(dlon_, 6), round(dlat_, 6)]}})
    (OUT / "features.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=1, ensure_ascii=False))
    print(f"wrote {len(feats)} contour lines, {len(features)} features to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
