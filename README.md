# Skogseidvatnet fishing chart

Printable A3 fishing chart for Skogseidvatnet: `chart/skogseid-kart-A3.pdf`. Print it at A3 (1:20 000) or
scaled to A4; the scale bar stays correct either way. Laminate it, and use a marker on the catch log.
Rebuild it with `python tools/build_chart.py` (run `tools/build_data.py` first if the sources change).
It needs Chromium and pdftoppm on PATH.

The chart is published at https://jonfossheim.github.io/dybdekart/ (`site/` plus the PDF and preview, deployed by
`.github/workflows/pages.yml`).

Background map data © OpenStreetMap contributors (ODbL), from `kilder/osm-skogseid.json`.

## Skogseid Sonar (phone app)

Offline phone app for fishing Skogseidvatnet (NVE 2043, Bjørnafjorden): a depth map, a live depth
readout under your GPS position, a warning near the fish farm pens, track recording and a catch log.

## Use it

The app is a static site in `app/`. It is no longer deployed; the Pages site serves the chart instead.
GPS only works over **https** (or on `localhost`), so it has to be hosted before it works on a phone. Open it once with a connection,
then use "Add to Home Screen". After that it works with no signal.

Try it on a computer:

    cd app && python3 -m http.server 8765
    # open http://localhost:8765, then Layers → Test mode, and tap the map to move the boat

Catches and tracks stay in the phone's browser storage. Export GPX or CSV from the Track sheet after each trip.

## Data

| File | What | Source |
| --- | --- | --- |
| `app/data/depth.png` + `depth.json` | 5 m depth grid, 255 = land | Skogheim 1983 contours, interpolated |
| `app/data/contours.geojson` | 10 m lines (20 m from source, 10 m interpolated) | derived from the grid |
| `app/data/shore.geojson` | shoreline and islands | NVE Innsjødatabase |
| `app/data/features.geojson` | pens, inflows, outlet, hill, deepest point | Fiskeridirektoratet, 1983 map |

Rebuild with `python tools/build_data.py` (needs `rsvg-convert` and `tools/requirements.txt`).
The build warps the 1983 map onto NVE's shoreline (mean error ~12 m) and interpolates between the
20 m contours. The grid's mean depth comes out at 44.1 m; NVE gives 44 m.

**Limits**
- The 1983 map stops at 115 m, but NIVA measured 129 m by sonar in 1994.
- Depths between contours are interpolated, not measured.
- Pen positions are the registered site points, not the pens themselves.

Machine-readable sources are in `kilder/`. The contours came from Rådgivende Biologer report 3892
(Tilstandsrapport for Skogseidvatnet og Henangervatnet 2022), which isn't in the repo; get it from
radgivende-biologer.no.

## Rules (check before the trip)

The sources disagree on daily quota, minimum size and net fishing. Buy the fishing card and check the
rules with Hålandsdalen Grunneigarlag on inatur.no. The sources that do agree say: keep 100 m from the
pens, max 6 hp, and wear a life vest.
