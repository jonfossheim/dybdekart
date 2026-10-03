/* Skogseid Sonar: offline depth map + GPS readout + catch log for Skogseidvatnet. */
(() => {
  'use strict';

  // Depth colour stops (m -> rgb). Shallow water is brightest so it reads in sunlight.
  const STOPS = [
    [0, [255, 207, 90]], [20, [58, 214, 196]], [40, [30, 163, 171]], [60, [20, 122, 144]],
    [80, [16, 86, 118]], [100, [11, 58, 88]], [130, [8, 40, 64]],
  ];
  const SPECIES_COLOR = { Char: '#ff7a7a', Trout: '#f5a54a', Salmon: '#c7a6ff', Other: '#e8f0f0' };
  const PEN_LIMIT_M = 100;
  const PEN_CAUTION_M = 150;
  const KNOTS = 1.943844;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage full or blocked */ } },
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const state = {
    grid: null,          // {meta, values: Uint8Array}
    pos: null,           // {lat, lon, acc, speed, course, t, source}
    follow: true,
    testMode: false,
    recording: store.get('skg.rec', false),
    track: store.get('skg.track', []),       // [lat, lon, t, depth]
    catches: store.get('skg.catches', []),
    pens: [],
    insidePen: false,
    wakeLock: null,
  };

  // ---------- map ----------
  const map = L.map('map', {
    zoomControl: false, minZoom: 12, maxZoom: 19, zoomSnap: 0.25, preferCanvas: true, attributionControl: true,
  });
  L.control.zoom({ position: 'topright' }).addTo(map);
  map.attributionControl.setPrefix(false).addAttribution('Depth: Skogheim 1983 · Shore: NVE · Pens: Fiskeridirektoratet');

  const layers = {
    shade: L.layerGroup(), contours: L.layerGroup(), labels: L.layerGroup(), pens: L.layerGroup(),
    spots: L.layerGroup(), catches: L.layerGroup(), track: L.layerGroup(),
  };
  const trackLine = L.polyline([], { color: '#ffffff', weight: 2, opacity: 0.8, dashArray: '4 4' }).addTo(layers.track);
  const accCircle = L.circle([0, 0], { radius: 1, color: '#a6fff2', weight: 1, opacity: 0.5, fillOpacity: 0.08, interactive: false });
  const boatIcon = L.divIcon({
    className: '', iconSize: [34, 34], iconAnchor: [17, 17],
    html: '<svg class="boat" viewBox="0 0 34 34" width="34" height="34"><circle cx="17" cy="17" r="7" fill="#fff" stroke="#061016" stroke-width="2"/><path d="M17 1 L22.5 11 H11.5 Z" fill="#fff" stroke="#061016" stroke-width="1.5"/></svg>',
  });
  const boat = L.marker([0, 0], { icon: boatIcon, interactive: false, keyboard: false, zIndexOffset: 1000 });

  function rgbAt(d) {
    for (let i = 1; i < STOPS.length; i++) {
      if (d <= STOPS[i][0]) {
        const [d0, c0] = STOPS[i - 1], [d1, c1] = STOPS[i];
        const f = (d - d0) / (d1 - d0);
        return c0.map((v, j) => Math.round(v + (c1[j] - v) * f));
      }
    }
    return STOPS[STOPS.length - 1][1];
  }

  function buildShade(meta, values) {
    const c = document.createElement('canvas');
    c.width = meta.width; c.height = meta.height;
    const ctx = c.getContext('2d');
    const img = ctx.createImageData(meta.width, meta.height);
    const lut = Array.from({ length: 256 }, (_, d) => rgbAt(d));
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (v === meta.land) continue;
      const [r, g, b] = lut[v];
      img.data[i * 4] = r; img.data[i * 4 + 1] = g; img.data[i * 4 + 2] = b; img.data[i * 4 + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    return c.toDataURL('image/png');
  }

  function depthAt(lat, lon) {
    const g = state.grid;
    if (!g) return null;
    const { meta, values } = g;
    const fx = (lon - meta.west) / (meta.east - meta.west) * meta.width - 0.5;
    const fy = (meta.north - lat) / (meta.north - meta.south) * meta.height - 0.5;
    const x0 = Math.floor(fx), y0 = Math.floor(fy);
    if (x0 < 0 || y0 < 0 || x0 + 1 >= meta.width || y0 + 1 >= meta.height) return null;
    const at = (x, y) => values[y * meta.width + x];
    const q = [at(x0, y0), at(x0 + 1, y0), at(x0, y0 + 1), at(x0 + 1, y0 + 1)];
    if (q.some((v) => v === meta.land)) {
      const v = at(Math.round(fx), Math.round(fy));
      return v === meta.land ? null : v;
    }
    const tx = fx - x0, ty = fy - y0;
    return (q[0] * (1 - tx) + q[1] * tx) * (1 - ty) + (q[2] * (1 - tx) + q[3] * tx) * ty;
  }

  const bandText = (d) => (d >= 100 ? '100+ m' : `${Math.floor(d / 20) * 20}–${Math.floor(d / 20) * 20 + 20} m`);

  // ---------- data ----------
  async function loadData() {
    const [meta, contours, shore, features] = await Promise.all(
      ['data/depth.json', 'data/contours.geojson', 'data/shore.geojson', 'data/features.geojson']
        .map((u) => fetch(u).then((r) => { if (!r.ok) throw new Error(u); return r.json(); })),
    );
    const img = new Image();
    img.src = 'data/depth.png';
    await img.decode();
    const c = document.createElement('canvas');
    c.width = meta.width; c.height = meta.height;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const rgba = ctx.getImageData(0, 0, meta.width, meta.height).data;
    const values = new Uint8Array(meta.width * meta.height);
    for (let i = 0; i < values.length; i++) values[i] = rgba[i * 4];
    state.grid = { meta, values };

    const bounds = [[meta.south, meta.west], [meta.north, meta.east]];
    L.imageOverlay(buildShade(meta, values), bounds, { interactive: false }).addTo(layers.shade);

    L.geoJSON(contours, {
      interactive: false,
      style: (f) => (f.properties.major
        ? { color: '#a6fff2', weight: 1.3, opacity: 0.6 }
        : { color: '#a6fff2', weight: 0.7, opacity: 0.28 }),
    }).addTo(layers.contours);
    for (const f of contours.features) {
      if (!f.properties.major) continue;
      const pts = f.geometry.coordinates.map(([lon, lat]) => L.latLng(lat, lon));
      let run = 350;
      for (let i = 1; i < pts.length; i++) {
        run += pts[i - 1].distanceTo(pts[i]);
        if (run >= 900) {
          run = 0;
          L.marker(pts[i], { interactive: false, keyboard: false,
            icon: L.divIcon({ className: '', iconSize: null, html: `<span class="clabel">${f.properties.depth}</span>` }) }).addTo(layers.labels);
        }
      }
    }

    const shoreLayer = L.geoJSON(shore, { interactive: false, style: { color: '#a6fff2', weight: 1.6, opacity: 0.9, fill: false } }).addTo(map);
    map.fitBounds(shoreLayer.getBounds(), { padding: [10, 10] });
    map.setMaxBounds(shoreLayer.getBounds().pad(0.6));

    for (const f of features.features) {
      const [lon, lat] = f.geometry.coordinates;
      const p = f.properties;
      if (p.kind === 'pen') {
        state.pens.push({ name: p.name, lat, lon });
        L.circle([lat, lon], { radius: PEN_LIMIT_M, color: '#ff6a52', weight: 1, dashArray: '4 3', fillColor: '#ff6a52', fillOpacity: 0.12, interactive: false })
          .addTo(layers.pens);
        L.circleMarker([lat, lon], { radius: 5, color: '#061016', weight: 1.5, fillColor: '#ff6a52', fillOpacity: 1 })
          .bindPopup(`<b>Fish farm pen: ${esc(p.name)}</b><br>Registered site position, site ${p.siteNr}. Keep at least 100 m away. Spilled feed draws big fish, so try the drift side.`)
          .addTo(layers.pens);
      } else if (p.kind === 'shore_site') {
        L.circleMarker([lat, lon], { radius: 3, color: '#061016', weight: 1, fillColor: '#ffb03a', fillOpacity: 1 })
          .bindPopup(`<b>${esc(p.name)}</b><br>Land-based fish farm site on the shore.`).addTo(layers.pens);
      } else {
        const text = {
          inflow: 'Trout often feed where streams come in, especially in spring and after rain.',
          outlet: 'The lake drains here towards Henangervatnet.',
          hump: 'Open-water structure. Fish the slopes around it.',
          deep: 'Deepest area on the 1983 map.',
        }[p.kind] || '';
        const d = depthAt(lat, lon);
        L.circleMarker([lat, lon], { radius: 6, color: '#a6fff2', weight: 2, fillColor: '#061016', fillOpacity: 1 })
          .bindPopup(`<b>${esc(p.name)}</b><br>${text}${d != null && p.kind !== 'inflow' && p.kind !== 'outlet' ? `<br>Map depth here: ~${Math.round(d)} m` : ''}`)
          .addTo(layers.spots);
      }
    }
    for (const k of Object.keys(layers)) layers[k].addTo(map);
    updateLabels();
  }

  function updateLabels() {
    map.getContainer().classList.toggle('show-labels', map.getZoom() >= 14.5 && map.hasLayer(layers.contours));
  }
  map.on('zoomend', updateLabels);

  // ---------- position ----------
  let lastFix = null;

  function onPosition(p) {
    const prev = lastFix;
    if (prev) {
      const dt = (p.t - prev.t) / 1000;
      const dist = L.latLng(prev.lat, prev.lon).distanceTo([p.lat, p.lon]);
      if (p.speed == null && dt > 0.5) p.speed = dist / dt;
      if ((p.course == null || Number.isNaN(p.course)) && dist > 3) p.course = bearing(prev, p);
    }
    if (p.course == null && state.pos) p.course = state.pos.course;
    lastFix = p;
    state.pos = p;

    const ll = [p.lat, p.lon];
    boat.setLatLng(ll);
    if (!map.hasLayer(boat)) boat.addTo(map);
    const svg = boat.getElement()?.querySelector('svg');
    if (svg) svg.style.transform = `rotate(${p.course ?? 0}deg)`;
    if (p.acc) { accCircle.setLatLng(ll).setRadius(p.acc); if (!map.hasLayer(accCircle)) accCircle.addTo(map); }
    if (state.follow) {
      if (!state.centred) { map.setView(ll, Math.max(map.getZoom(), 15), { animate: false }); state.centred = true; }
      else map.panTo(ll, { animate: true, duration: 0.4 });
    }

    renderHud();
    checkPens();
    appendTrack(p);
  }

  function bearing(a, b) {
    const r = Math.PI / 180;
    const y = Math.sin((b.lon - a.lon) * r) * Math.cos(b.lat * r);
    const x = Math.cos(a.lat * r) * Math.sin(b.lat * r) - Math.sin(a.lat * r) * Math.cos(b.lat * r) * Math.cos((b.lon - a.lon) * r);
    return (Math.atan2(y, x) / r + 360) % 360;
  }

  function renderHud() {
    const p = state.pos;
    const d = p ? depthAt(p.lat, p.lon) : null;
    const el = $('depth');
    if (d == null) {
      $('depthVal').textContent = '--';
      el.classList.add('off');
      $('depthNote').textContent = p ? 'Not on the lake' : 'Waiting for position';
      $('band').textContent = '--';
    } else {
      $('depthVal').textContent = Math.round(d);
      el.classList.remove('off');
      $('depthNote').textContent = state.testMode ? 'Test position · map depth' : 'Map depth (1983 survey)';
      $('band').textContent = bandText(d);
    }
    $('speed').textContent = p?.speed != null ? `${(p.speed * KNOTS).toFixed(1)} kn` : '--';
    $('course').textContent = p?.course != null && (p.speed ?? 0) > 0.3 ? `${Math.round(p.course).toString().padStart(3, '0')}°` : '--';
    $('gps').textContent = state.testMode ? 'test' : p?.acc ? `±${Math.round(p.acc)} m` : '--';
  }

  function checkPens() {
    const p = state.pos;
    const box = $('penAlert');
    if (!p || !state.pens.length) { box.hidden = true; return; }
    let best = null;
    for (const pen of state.pens) {
      const d = L.latLng(p.lat, p.lon).distanceTo([pen.lat, pen.lon]);
      if (!best || d < best.d) best = { ...pen, d };
    }
    const d = Math.round(best.d);
    if (best.d < PEN_LIMIT_M) {
      box.className = 'alert danger';
      box.textContent = `Inside the 100 m zone of ${best.name} (${d} m). Move away from the pen.`;
      box.hidden = false;
      if (!state.insidePen && navigator.vibrate) navigator.vibrate([200, 100, 200]);
      state.insidePen = true;
    } else if (best.d < PEN_CAUTION_M) {
      box.className = 'alert caution';
      box.textContent = `${best.name} pen is ${d} m away. The limit is 100 m.`;
      box.hidden = false;
      state.insidePen = false;
    } else {
      box.hidden = true;
      state.insidePen = false;
    }
  }

  function startGps() {
    if (!('geolocation' in navigator)) {
      showBanner('This browser has no location access. Turn on test mode in Layers to try the app.');
      return;
    }
    if (!window.isSecureContext) {
      showBanner('GPS only works when the app is opened over https. Turn on test mode in Layers to try it here.');
      return;
    }
    navigator.geolocation.watchPosition((pos) => {
      if (state.testMode) return;
      hideBanner();
      const c = pos.coords;
      onPosition({
        lat: c.latitude, lon: c.longitude, acc: c.accuracy,
        speed: c.speed != null && !Number.isNaN(c.speed) ? c.speed : null,
        course: c.heading != null && !Number.isNaN(c.heading) && (c.speed ?? 0) > 0.5 ? c.heading : null,
        t: pos.timestamp, source: 'gps',
      });
    }, (err) => {
      if (err.code === 1) showBanner('Location is blocked for this site. Allow location in the browser settings, or use test mode in Layers.');
      else if (!state.pos) showBanner('Looking for GPS… Open sky helps. Use test mode in Layers to try the app indoors.');
    }, { enableHighAccuracy: true, maximumAge: 1000, timeout: 20000 });
  }

  function showBanner(t) { const b = $('banner'); b.textContent = t; b.hidden = false; }
  function hideBanner() { $('banner').hidden = true; }

  // ---------- follow ----------
  function setFollow(on) {
    state.follow = on;
    $('follow').setAttribute('aria-pressed', String(on));
    if (on && state.pos) map.panTo([state.pos.lat, state.pos.lon]);
  }
  map.on('dragstart', () => setFollow(false));
  $('follow').addEventListener('click', () => setFollow(!state.follow));

  // ---------- test mode ----------
  map.on('click', (e) => {
    if (closeSheets()) return;
    if (!state.testMode) return;
    onPosition({ lat: e.latlng.lat, lon: e.latlng.lng, acc: null, speed: 0, course: null, t: Date.now(), source: 'test' });
  });
  $('testMode').addEventListener('change', (e) => {
    state.testMode = e.target.checked;
    if (state.testMode) { setFollow(false); toast('Tap the map to move the boat'); hideBanner(); }
    renderHud();
  });

  // ---------- track ----------
  function trackDistance() {
    let m = 0;
    for (let i = 1; i < state.track.length; i++) {
      m += L.latLng(state.track[i - 1][0], state.track[i - 1][1]).distanceTo([state.track[i][0], state.track[i][1]]);
    }
    return m;
  }

  function appendTrack(p) {
    if (!state.recording) return;
    const last = state.track[state.track.length - 1];
    if (last) {
      const moved = L.latLng(last[0], last[1]).distanceTo([p.lat, p.lon]);
      if (moved < 8 && p.t - last[2] < 30000) return;
    }
    const d = depthAt(p.lat, p.lon);
    state.track.push([+p.lat.toFixed(6), +p.lon.toFixed(6), p.t, d == null ? null : Math.round(d)]);
    store.set('skg.track', state.track);
    drawTrack();
  }

  function drawTrack() {
    trackLine.setLatLngs(state.track.map((t) => [t[0], t[1]]));
    $('trackDist').textContent = (trackDistance() / 1000).toFixed(2);
    $('trackPts').textContent = state.track.length;
  }

  function setRecording(on) {
    state.recording = on;
    store.set('skg.rec', on);
    $('btnTrack').setAttribute('aria-pressed', String(on));
    $('trackToggle').textContent = on ? 'Stop tracking' : 'Start tracking';
    $('trackToggle').className = on ? '' : 'go';
    if (on) { keepAwake(); if (state.pos) appendTrack(state.pos); } else releaseWake();
  }

  async function keepAwake() {
    try { if ('wakeLock' in navigator && !state.wakeLock) state.wakeLock = await navigator.wakeLock.request('screen'); state.wakeLock?.addEventListener('release', () => { state.wakeLock = null; }); } catch { /* not allowed */ }
  }
  function releaseWake() { state.wakeLock?.release(); state.wakeLock = null; }
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && state.recording) keepAwake(); });

  $('trackToggle').addEventListener('click', () => setRecording(!state.recording));
  const clearBtn = $('trackClear');
  clearBtn.addEventListener('click', () => {
    if (clearBtn.dataset.armed) {
      state.track = []; store.set('skg.track', []); drawTrack();
      delete clearBtn.dataset.armed; clearBtn.textContent = 'Clear track'; toast('Track cleared');
    } else {
      clearBtn.dataset.armed = '1'; clearBtn.textContent = 'Tap again to clear';
      setTimeout(() => { delete clearBtn.dataset.armed; clearBtn.textContent = 'Clear track'; }, 3000);
    }
  });

  // ---------- catches ----------
  const catchMarkers = new Map();

  function catchLabel(c) {
    const parts = [c.species];
    if (c.weight) parts.push(`${c.weight} kg`);
    if (c.length) parts.push(`${c.length} cm`);
    return parts.join(' · ');
  }
  const timeText = (t) => new Date(t).toLocaleString('nb-NO', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

  function drawCatches() {
    layers.catches.clearLayers();
    catchMarkers.clear();
    for (const c of state.catches) {
      const m = L.circleMarker([c.lat, c.lon], { radius: 7, color: '#061016', weight: 2, fillColor: SPECIES_COLOR[c.species] || '#fff', fillOpacity: 1 })
        .bindPopup(() => {
          const div = document.createElement('div');
          div.innerHTML = `<b>${esc(catchLabel(c))}</b><br>${esc(timeText(c.t))}${c.depth != null ? ` · ${esc(c.depth)} m` : ''}${c.lure ? `<br>${esc(c.lure)}` : ''}${c.note ? `<br>${esc(c.note)}` : ''}<br>`;
          const del = document.createElement('button');
          del.type = 'button'; del.textContent = 'Delete';
          del.addEventListener('click', () => {
            if (!del.dataset.armed) { del.dataset.armed = '1'; del.textContent = 'Tap again to delete'; return; }
            state.catches = state.catches.filter((x) => x.id !== c.id);
            store.set('skg.catches', state.catches);
            map.closePopup(); drawCatches(); toast('Catch deleted');
          });
          div.append(del);
          return div;
        });
      m.addTo(layers.catches);
      catchMarkers.set(c.id, m);
    }
    renderCatchList();
    const lures = [...new Set(state.catches.map((c) => c.lure).filter(Boolean))];
    $('lures').innerHTML = lures.map((l) => `<option value="${esc(l)}">`).join('');
  }

  function renderCatchList() {
    const ol = $('catchList');
    $('catchCount').textContent = state.catches.length;
    if (!state.catches.length) { ol.innerHTML = '<li class="empty">No catches yet. Tap Log catch when you land a fish.</li>'; return; }
    ol.innerHTML = '';
    for (const c of [...state.catches].sort((a, b) => b.t - a.t)) {
      const li = document.createElement('li');
      li.innerHTML = `<i style="background:${SPECIES_COLOR[c.species] || '#fff'}"></i><div>${esc(catchLabel(c))}<small>${esc(timeText(c.t))}${c.depth != null ? ` · ${esc(c.depth)} m` : ''}${c.lure ? ` · ${esc(c.lure)}` : ''}</small></div><b>›</b>`;
      li.addEventListener('click', () => { closeSheets(); setFollow(false); map.setView([c.lat, c.lon], Math.max(map.getZoom(), 16)); catchMarkers.get(c.id)?.openPopup(); });
      ol.append(li);
    }
  }

  let pendingWhere = null;
  function openCatchForm() {
    const p = state.pos;
    const center = map.getCenter();
    pendingWhere = p ? { lat: p.lat, lon: p.lon, src: p.source === 'test' ? 'test position' : `GPS ±${Math.round(p.acc || 0)} m` }
      : { lat: center.lat, lon: center.lng, src: 'map centre (no GPS fix)' };
    const d = depthAt(pendingWhere.lat, pendingWhere.lon);
    $('fDepth').value = d == null ? '' : Math.round(d);
    $('fWhere').textContent = `Position: ${pendingWhere.lat.toFixed(5)}, ${pendingWhere.lon.toFixed(5)} · ${pendingWhere.src}`;
    openSheet('sheetCatch');
  }

  $('catchForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const num = (k) => { const v = parseFloat(String(f.get(k)).replace(',', '.')); return Number.isFinite(v) ? v : null; };
    const c = {
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6), t: Date.now(),
      lat: +pendingWhere.lat.toFixed(6), lon: +pendingWhere.lon.toFixed(6),
      species: f.get('species'), weight: num('weight'), length: num('length'), depth: num('depth'),
      lure: String(f.get('lure') || '').trim(), note: String(f.get('note') || '').trim(),
    };
    state.catches.push(c);
    store.set('skg.catches', state.catches);
    e.target.reset();
    closeSheets();
    drawCatches();
    toast(`${c.species} saved`);
  });

  // ---------- export ----------
  const stamp = () => new Date().toISOString().slice(0, 10);
  const xml = (s) => esc(s);

  function gpx() {
    const wpts = state.catches.map((c) => `  <wpt lat="${c.lat}" lon="${c.lon}"><time>${new Date(c.t).toISOString()}</time><name>${xml(catchLabel(c))}</name><desc>${xml([c.depth != null ? `${c.depth} m` : '', c.lure, c.note].filter(Boolean).join(' · '))}</desc><type>${xml(c.species)}</type></wpt>`).join('\n');
    const trk = state.track.length ? `  <trk><name>Skogseidvatnet ${stamp()}</name><trkseg>\n${state.track.map((t) => `   <trkpt lat="${t[0]}" lon="${t[1]}"><time>${new Date(t[2]).toISOString()}</time></trkpt>`).join('\n')}\n  </trkseg></trk>` : '';
    return `<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="Skogseid Sonar" xmlns="http://www.topografix.com/GPX/1/1">\n${wpts}\n${trk}\n</gpx>\n`;
  }

  function csv() {
    const q = (v) => (v == null ? '' : /[",\n;]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
    const rows = [['time', 'species', 'weight_kg', 'length_cm', 'depth_m', 'lure', 'note', 'lat', 'lon']];
    for (const c of state.catches) rows.push([new Date(c.t).toISOString(), c.species, c.weight, c.length, c.depth, c.lure, c.note, c.lat, c.lon]);
    return rows.map((r) => r.map(q).join(',')).join('\n') + '\n';
  }

  async function save(name, text, type) {
    const file = new File([text], name, { type });
    if (navigator.canShare?.({ files: [file] })) {
      try { await navigator.share({ files: [file], title: name }); return; } catch (err) { if (err.name === 'AbortError') return; }
    }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(file); a.download = name;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  $('exportGpx').addEventListener('click', () => {
    if (!state.catches.length && !state.track.length) { toast('Nothing to export yet'); return; }
    save(`skogseid-${stamp()}.gpx`, gpx(), 'application/gpx+xml');
  });
  $('exportCsv').addEventListener('click', () => {
    if (!state.catches.length) { toast('No catches to export yet'); return; }
    save(`skogseid-catches-${stamp()}.csv`, csv(), 'text/csv');
  });

  // ---------- sheets + layers ----------
  function openSheet(id) {
    closeSheets();
    $(id).hidden = false;
  }
  function closeSheets() {
    let closed = false;
    for (const s of document.querySelectorAll('.sheet')) { if (!s.hidden) { s.hidden = true; closed = true; } }
    return closed;
  }
  for (const b of document.querySelectorAll('[data-close]')) b.addEventListener('click', closeSheets);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSheets(); });
  $('btnLayers').addEventListener('click', () => ($('sheetLayers').hidden ? openSheet('sheetLayers') : closeSheets()));
  $('btnTrack').addEventListener('click', () => ($('sheetTrack').hidden ? openSheet('sheetTrack') : closeSheets()));
  $('btnLog').addEventListener('click', openCatchForm);

  const layerToggles = { lyrShade: ['shade'], lyrContours: ['contours', 'labels'], lyrPens: ['pens'], lyrSpots: ['spots'], lyrCatches: ['catches'], lyrTrack: ['track'] };
  const savedLayers = store.get('skg.layers', {});
  for (const [id, names] of Object.entries(layerToggles)) {
    const box = $(id);
    if (id in savedLayers) box.checked = savedLayers[id];
    const apply = () => {
      for (const n of names) box.checked ? layers[n].addTo(map) : layers[n].remove();
      savedLayers[id] = box.checked; store.set('skg.layers', savedLayers);
      updateLabels();
    };
    box.addEventListener('change', apply);
    box._apply = apply;
  }

  $('ramp').style.background = `linear-gradient(90deg, ${STOPS.slice(0, 6).map(([d, c], i) => `rgb(${c}) ${i * 20}%`).join(', ')})`;

  let toastTimer;
  function toast(t) {
    const el = $('toast');
    el.textContent = t; el.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 2200);
  }

  // ---------- boot ----------
  loadData().then(() => {
    for (const id of Object.keys(layerToggles)) $(id)._apply();
    drawTrack();
    drawCatches();
    setRecording(state.recording);
    renderHud();
    startGps();
  }).catch((err) => {
    showBanner(`Could not load the map data (${err.message}). Reload with a connection once so it can be stored for offline use.`);
  });

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('sw.js').catch(() => { /* offline cache unavailable */ });
  }
})();
