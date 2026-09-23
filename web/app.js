// Radar Lab frontend -- client-side rendering per design doc §2.
// The backend only ever ships plain JSON (radial arrays, camera lists,
// GeoJSON alerts); every pixel on screen is drawn here in the browser.
//
// Split-screen (2026-09-23): each panel is its own Leaflet map instance
// with its own product selection and pan/zoom, so two panels can show
// the same area in different products (reflectivity vs velocity) or
// different areas entirely. Playback (which scan timestamp is shown) is
// shared across all panels -- splitting *time* per-panel wasn't asked
// for and would make "watching two storm sections at once" confusing
// (they'd drift out of sync with each other). Camera/alert/GPS/NST/NMD
// overlays are also shared toggles (one checkbox governs all panels) --
// per-panel overlay visibility wasn't part of the ask either, and
// duplicating those controls per panel adds real UI clutter for no
// clear benefit.

const PRODUCT_ENDPOINTS = {
  reflectivity_dbz: "/api/reflectivity",
  velocity_ms: "/api/velocity",
  zdr_db: "/api/zdr",
  cc: "/api/cc",
  phidp_deg: "/api/phidp",
};
const PRODUCT_LABELS = {
  reflectivity_dbz: "Base Reflectivity",
  velocity_ms: "Velocity",
  zdr_db: "Diff. Reflectivity (ZDR)",
  cc: "Correlation Coef. (CC)",
  phidp_deg: "Diff. Phase (PhiDP, raw)",
};
const DEFAULT_PRODUCTS = ["reflectivity_dbz", "velocity_ms", "zdr_db", "cc"];
const DEFAULT_CENTER = [38.4, -87.7];
const DEFAULT_ZOOM = 8;

// Three basemap choices, all Esri (arcgisonline.com), all free/no-key,
// all confirmed 2026-09-23 by actually viewing tile pixels (not just
// checking HTTP status -- CARTO's basemaps.cartocdn.com, the original
// "dark" choice, returns a valid 200 PNG but now stamps a large "API
// KEY REQUIRED" watermark across every tile, which a status check alone
// can't catch). "streets" is the default -- real road-level detail
// (highway shields, road labels), which matters for correlating radar
// position against DOT cameras/GPS. Shared/global selection applied to
// every panel at once, same reasoning as the other shared overlays.
const BASE_LAYERS = {
  streets: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    attribution: "Tiles &copy; Esri",
  },
  dark: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    attribution: "Tiles &copy; Esri",
  },
  satellite: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "Tiles &copy; Esri",
  },
};

// GOES-East cloud imagery, NASA GIBS (WMTS) -- free, no API key,
// confirmed live 2026-09-23, real ~10min update cadence. "default" in
// the time slot is a GIBS shortcut for "latest available tile" -- saves
// having to compute/guess a valid timestamp client-side (there's real
// latency between "now" and what's actually been processed).
// IR (Band 13) works day and night; Visible and GeoColor only show
// anything meaningful in daylight -- that's a real property of the data,
// not a bug, worth knowing when picking which to leave on overnight.
// maxNativeZoom values are real, checked against GIBS' own
// TileMatrixSet definitions 2026-09-23 (2km set: zoom 0-5, 1km set:
// zoom 0-6) -- not guessed. Leaflet upscales past this rather than
// showing blank tiles, which is the right behavior for a coarser
// overlay under a much higher-zoom base map.
const GOES_LAYERS = {
  ir: { id: "GOES-East_ABI_Band13_Clean_Infrared", matrixSet: "2km", maxNativeZoom: 5, label: "IR (Band 13, day/night)" },
  visible: { id: "GOES-East_ABI_Band2_Red_Visible_1km", matrixSet: "1km", maxNativeZoom: 6, label: "Visible (Band 2, day only)" },
  geocolor: { id: "GOES-East_ABI_GeoColor", matrixSet: "1km", maxNativeZoom: 6, label: "GeoColor (day/night composite)" },
};
function goesTileUrl(key) {
  const g = GOES_LAYERS[key];
  return `https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/${g.id}/default/default/${g.matrixSet}/{z}/{y}/{x}.png`;
}

let panels = [];
let goesLayerKey = "off"; // "off" | "ir" | "visible" | "geocolor"
let baseLayerKey = "streets"; // "streets" | "dark" | "satellite"
let syncEnabled = false;
let syncingView = false; // re-entrancy guard for the pan/zoom sync loop
let siteLatLon = null;
let siteList = []; // [{id, name, lat, lon}], from /api/sites -- see loadSiteList()
let currentSite = null; // active site id, tracked client-side to highlight its pill
let scans = [];
let scanIndex = -1;
let playing = false;
let playTimer = null;
const gpsTrailPoints = [];
const scanDataCache = new Map(); // "product:ts" -> decoded JSON, avoids
                                  // refetching the same scan twice when
                                  // two panels happen to want it.

const statusEl = document.getElementById("status");
const siteEl = document.getElementById("site");
const siteSelect = document.getElementById("site-select");
const tsLabel = document.getElementById("ts-label");
const slider = document.getElementById("scan-slider");
const gridEl = document.getElementById("grid");

function setStatus(msg) {
  statusEl.textContent = msg;
}

async function fetchJSON(url) {
  const resp = await fetch(url);
  // Read the body even on a non-OK response -- endpoints like
  // /api/reflectivity return a real JSON {error: "..."} explaining why
  // (e.g. "no scan cached yet" right after a site switch) alongside the
  // 503, and discarding that in favor of a generic "HTTP 503" made a
  // real, recoverable state (still loading) hard to tell apart from an
  // actual failure.
  const data = await resp.json().catch(() => null);
  if (!resp.ok) throw new Error(data?.error || `${url}: HTTP ${resp.status}`);
  return data;
}

// ---------------------------------------------------------------------
// Color scales
// ---------------------------------------------------------------------

function lerpColor(c1, c2, t) {
  return [0, 1, 2].map((i) => Math.round(c1[i] + (c2[i] - c1[i]) * t));
}

function dbzColor(v) {
  if (v < 5) return null; // treat as clear
  const stops = [
    [5, [0x40, 0xe0, 0xd0]], [15, [0x00, 0x90, 0x00]], [25, [0x00, 0xe0, 0x00]],
    [30, [0xff, 0xff, 0x00]], [35, [0xff, 0xc0, 0x00]], [40, [0xff, 0x80, 0x00]],
    [45, [0xff, 0x00, 0x00]], [50, [0xc0, 0x00, 0x00]], [55, [0xff, 0x00, 0xff]],
    [65, [0xff, 0xff, 0xff]],
  ];
  for (let i = stops.length - 1; i >= 0; i--) {
    if (v >= stops[i][0]) return stops[i][1];
  }
  return stops[0][1];
}

function velColor(v) {
  const mag = Math.min(Math.abs(v) / 35, 1);
  if (v > 0) return [Math.round(255 * mag), 0, 0];
  if (v < 0) return [0, Math.round(255 * mag), 0];
  return null;
}

function zdrColor(v) {
  const t = Math.max(0, Math.min(1, (v + 8) / 16)); // -8..+8 dB
  return t < 0.5
    ? lerpColor([30, 60, 200], [255, 255, 255], t / 0.5)
    : lerpColor([255, 255, 255], [200, 30, 30], (t - 0.5) / 0.5);
}

function ccColor(v) {
  if (v < 0.2) return null; // below the noise floor
  // Low CC (non-uniform returns -- debris, biological, clutter) in
  // purple so it stands out; high CC (uniform weather) fades to white.
  const t = Math.max(0, Math.min(1, (v - 0.2) / 0.85));
  return lerpColor([160, 32, 240], [255, 255, 255], t);
}

function hslToRgb(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  let r, g, b;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}

function phidpColor(v) {
  // Raw PhiDP (0-360, cyclic) -- shown as-is, not KDP. See project
  // README known-gaps: KDP (PhiDP's derivative, the actually-useful
  // product) isn't computed yet.
  return hslToRgb(v, 0.7, 0.5);
}

const COLOR_FNS = {
  reflectivity_dbz: dbzColor,
  velocity_ms: velColor,
  zdr_db: zdrColor,
  cc: ccColor,
  phidp_deg: phidpColor,
};

// ---------------------------------------------------------------------
// Polar -> cartesian raster resample. Cost is fixed at canvasSize^2
// regardless of ray/gate count, which is what keeps this fast.
// ---------------------------------------------------------------------

function renderRadarCanvas(data, field) {
  const values = data[field];
  if (!values) return null;
  const az = data.azimuths;
  const nAz = az.length;
  const nGate = data.ngates;
  const gate0 = data.gate0_m, gateStep = data.gate_step_m;
  const maxRange = gate0 + nGate * gateStep;
  // Size was a flat 700px before -- for a ~460km-radius circle that's
  // ~1315m/pixel, nearly 5.3x coarser than the data's real 250m gate
  // spacing (2*maxRange / gateStep ≈ 3680px would be true native
  // resolution). That's real thrown-away detail, not radar-inherent
  // graininess -- the underlying data actually supports much sharper
  // rendering than 700px was giving it. Sized off the real data
  // dimensions now instead of a flat guess, capped for compute time
  // (cost is O(size^2) in the resample loop below -- 1800px measured
  // ~148ms on this dev box (Node/V8) vs. 700px's ~24ms, worth it for a
  // per-scan render, not framerate-critical).
  const size = Math.min(Math.round((2 * maxRange) / gateStep), 1800);
  const canvas = document.createElement("canvas");
  canvas.width = size; canvas.height = size;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(size, size);
  const colorFn = COLOR_FNS[field] || dbzColor;

  for (let py = 0; py < size; py++) {
    const yM = ((size / 2 - py) / (size / 2)) * maxRange;
    for (let px = 0; px < size; px++) {
      const xM = ((px - size / 2) / (size / 2)) * maxRange;
      const rangeM = Math.sqrt(xM * xM + yM * yM);
      const idx = (py * size + px) * 4;
      if (rangeM > maxRange || rangeM < gate0) continue;
      let azDeg = (Math.atan2(xM, yM) * 180) / Math.PI;
      if (azDeg < 0) azDeg += 360;
      const azIdx = Math.round((azDeg / 360) * nAz) % nAz;
      const gateIdx = Math.floor((rangeM - gate0) / gateStep);
      if (gateIdx < 0 || gateIdx >= nGate) continue;
      const row = values[azIdx];
      if (!row) continue;
      const v = row[gateIdx];
      if (v === null || v === undefined) continue;
      const color = colorFn(v);
      if (!color) continue;
      img.data[idx] = color[0];
      img.data[idx + 1] = color[1];
      img.data[idx + 2] = color[2];
      img.data[idx + 3] = 200;
    }
  }
  ctx.putImageData(img, 0, 0);
  return { canvas, maxRange };
}

function metersToLatLonBounds(lat, lon, maxRange) {
  const dLat = maxRange / 111320;
  const dLon = maxRange / (111320 * Math.cos((lat * Math.PI) / 180));
  return [[lat - dLat, lon - dLon], [lat + dLat, lon + dLon]];
}

function kmOffsetToLatLon(lat, lon, xKm, yKm) {
  const dLat = (yKm * 1000) / 111320;
  const dLon = (xKm * 1000) / (111320 * Math.cos((lat * Math.PI) / 180));
  return [lat + dLat, lon + dLon];
}

// ---------------------------------------------------------------------
// Panels
// ---------------------------------------------------------------------

function createPanel(product) {
  const container = document.createElement("div");
  container.className = "panel";
  const mapDiv = document.createElement("div");
  mapDiv.className = "panel-map";
  container.appendChild(mapDiv);

  const toolbar = document.createElement("div");
  toolbar.className = "panel-toolbar";
  const select = document.createElement("select");
  for (const [val, label] of Object.entries(PRODUCT_LABELS)) {
    const opt = document.createElement("option");
    opt.value = val; opt.textContent = label;
    select.appendChild(opt);
  }
  select.value = product;
  toolbar.appendChild(select);

  // Tilt options aren't known until the first real scan comes back
  // (they come from the backend's response, see renderPanelRadar) --
  // starts with just the default so the control exists from the start.
  const tiltSelect = document.createElement("select");
  const defaultOpt = document.createElement("option");
  defaultOpt.value = "0";
  defaultOpt.textContent = "Tilt 0.5°";
  tiltSelect.appendChild(defaultOpt);
  toolbar.appendChild(tiltSelect);

  container.appendChild(toolbar);
  gridEl.appendChild(container);

  const map = L.map(mapDiv, { zoomControl: true }).setView(DEFAULT_CENTER, DEFAULT_ZOOM);
  const baseLayers = {};
  for (const [key, def] of Object.entries(BASE_LAYERS)) {
    baseLayers[key] = L.tileLayer(def.url, { attribution: def.attribution, maxZoom: 18 });
  }
  baseLayers[baseLayerKey].addTo(map);

  const panel = {
    container,
    map,
    select,
    product,
    tiltSelect,
    tilt: 0, // index into the tilts list the backend reports, 0 = lowest
    baseLayers,
    currentBaseKey: baseLayerKey,
    goesLayer: null, // current GOES tile layer, if any (see applyGoesLayer)
    hasAutoFit: false, // true once the map has zoomed to frame real radar
                        // data at least once (see renderPanelRadar) --
                        // prevents re-fitting on every later scan update,
                        // which would fight the user's own pan/zoom
    radarOverlay: null,
    cameraLayer: L.layerGroup().addTo(map),
    alertsLayer: L.layerGroup().addTo(map),
    nstLayer: L.layerGroup().addTo(map),
    nmdLayer: L.layerGroup().addTo(map),
    gpsMarker: null,
    gpsTrail: L.polyline([], { color: "#38bdf8", weight: 2 }).addTo(map),
    siteMarkersLayer: L.layerGroup().addTo(map),
  };

  select.addEventListener("change", () => {
    panel.product = select.value;
    renderPanelRadar(panel);
  });

  tiltSelect.addEventListener("change", () => {
    panel.tilt = parseInt(tiltSelect.value, 10);
    renderPanelRadar(panel);
  });

  map.on("moveend", () => {
    if (!syncEnabled || syncingView) return;
    syncingView = true;
    const center = map.getCenter(), zoom = map.getZoom();
    for (const p of panels) {
      if (p !== panel) p.map.setView(center, zoom, { animate: false });
    }
    syncingView = false;
  });

  applyGoesLayer(panel);
  return panel;
}

function applyGoesLayer(panel) {
  if (panel.goesLayer) {
    panel.map.removeLayer(panel.goesLayer);
    panel.goesLayer = null;
  }
  if (goesLayerKey === "off") return;
  const g = GOES_LAYERS[goesLayerKey];
  panel.goesLayer = L.tileLayer(goesTileUrl(goesLayerKey), {
    attribution: "GOES-East imagery &copy; NASA GIBS / NOAA",
    opacity: 0.7,
    maxNativeZoom: g.maxNativeZoom,
    maxZoom: 18,
  }).addTo(panel.map);
}

function destroyPanel(panel) {
  panel.map.remove();
  panel.container.remove();
}

function setLayout(n) {
  gridEl.className = `layout-${n}`;
  const prevView = panels.length
    ? { center: panels[0].map.getCenter(), zoom: panels[0].map.getZoom() }
    : null;

  while (panels.length > n) destroyPanel(panels.pop());
  while (panels.length < n) {
    const product = DEFAULT_PRODUCTS[panels.length % DEFAULT_PRODUCTS.length];
    const panel = createPanel(product);
    if (prevView) {
      panel.map.setView(prevView.center, prevView.zoom, { animate: false });
      panel.hasAutoFit = true; // inherited a real view, don't override it
    }
    panels.push(panel);
  }

  // Leaflet caches container size at creation time; force a recalc once
  // the CSS grid has actually laid the new panel divs out. Multiple
  // triggers on purpose -- a single fixed-delay timeout is a guess about
  // when layout settles, and mobile Safari in particular can still be
  // adjusting the viewport (toolbar show/hide) well after page load.
  requestAnimationFrame(() => panels.forEach((p) => p.map.invalidateSize()));
  setTimeout(() => panels.forEach((p) => p.map.invalidateSize()), 100);
  setTimeout(() => panels.forEach((p) => p.map.invalidateSize()), 500);

  renderAllPanels();
  refreshCameras();
  refreshAlerts();
  refreshLevel3();
  refreshSitePills();
}

// ---------------------------------------------------------------------
// Radar rendering per panel
// ---------------------------------------------------------------------

async function fetchProductData(product, ts, tilt) {
  const cacheKey = `${product}:${ts || "live"}:${tilt}`;
  if (scanDataCache.has(cacheKey)) return scanDataCache.get(cacheKey);
  const endpoint = PRODUCT_ENDPOINTS[product];
  const params = new URLSearchParams();
  if (ts) params.set("ts", ts);
  if (tilt) params.set("tilt", tilt); // omit for tilt 0 -- keeps the fast pre-decoded path on the backend
  const qs = params.toString();
  const url = qs ? `${endpoint}?${qs}` : endpoint;
  // fetchJSON throws on a non-OK response (e.g. 503 "no scan cached
  // yet"), so reaching this line always means real data -- safe to
  // cache unconditionally, no .error field to check here.
  const data = await fetchJSON(url);
  scanDataCache.set(cacheKey, data);
  return data;
}

// Tilt options come from the backend's own response (data.tilts), not
// hardcoded -- different VCPs use different elevation angles, and this
// stays correct without the frontend needing to know that.
function syncTiltSelect(panel, data) {
  const tilts = data.tilts || [];
  const optionValues = Array.from(panel.tiltSelect.options).map((o) => o.value);
  if (optionValues.length !== tilts.length || optionValues[0] !== "0") {
    panel.tiltSelect.innerHTML = "";
    tilts.forEach((angle, i) => {
      const opt = document.createElement("option");
      opt.value = i;
      opt.textContent = `Tilt ${angle}°`;
      panel.tiltSelect.appendChild(opt);
    });
  }
  panel.tiltSelect.value = data.tilt_index ?? 0;
}

async function renderPanelRadar(panel) {
  const ts = scans[scanIndex] || null;
  try {
    const data = await fetchProductData(panel.product, ts, panel.tilt);
    siteLatLon = [data.lat, data.lon];
    syncTiltSelect(panel, data);
    const result = renderRadarCanvas(data, panel.product);
    if (!result) {
      setStatus(`no ${panel.product} in this scan`);
      return;
    }
    const bounds = metersToLatLonBounds(data.lat, data.lon, result.maxRange);
    if (panel.radarOverlay) panel.map.removeLayer(panel.radarOverlay);
    panel.radarOverlay = L.imageOverlay(result.canvas.toDataURL(), bounds, { opacity: 0.75 });
    panel.radarOverlay.addTo(panel.map);
    if (!panel.hasAutoFit) {
      // First real data this panel has ever shown -- zoom/pan to frame
      // the radar's actual coverage circle instead of leaving it at the
      // generic DEFAULT_CENTER/DEFAULT_ZOOM, which is what made the
      // radar echo look like a small blob in the corner of a huge
      // multi-state view. Only happens once per panel so it doesn't
      // fight the user's own pan/zoom on every later scan update.
      panel.map.fitBounds(bounds);
      panel.hasAutoFit = true;
    }
  } catch (e) {
    setStatus(`fetch error: ${e.message}`);
  }
}

function renderAllPanels() {
  tsLabel.textContent = scans[scanIndex] || "live";
  setStatus(`showing ${scans[scanIndex] || "latest"} scan across ${panels.length} panel(s)`);
  panels.forEach(renderPanelRadar);
}

async function refreshScanList() {
  const { scans: list } = await fetchJSON("/api/scans");
  scans = list;
  slider.max = Math.max(0, scans.length - 1);
  if (scanIndex === -1 || scanIndex >= scans.length) {
    scanIndex = scans.length - 1;
    slider.value = scanIndex;
  }
}

// ---------------------------------------------------------------------
// Shared overlays (cameras / alerts / NST / NMD / GPS) -- fetched once,
// drawn into every panel's own layer group.
// ---------------------------------------------------------------------

async function refreshCameras() {
  const on = document.getElementById("toggle-cameras").checked;
  if (!on || !siteLatLon) {
    panels.forEach((p) => p.cameraLayer.clearLayers());
    return;
  }
  try {
    const { cameras } = await fetchJSON(
      `/api/cameras?lat=${siteLatLon[0]}&lon=${siteLatLon[1]}&radius_km=75`
    );
    panels.forEach((p) => {
      p.cameraLayer.clearLayers();
      for (const cam of cameras) {
        // Some locations have multiple real cameras at the same
        // coordinates (found 2026-09-23 near Evansville, IN -- InDOT
        // publishes 3 separate cameras for one I-64 interchange) --
        // backend groups those into one marker with several snapshots
        // instead of stacking identical markers invisibly on top of
        // each other, which was making all but the topmost unclickable.
        const images = (cam.snapshots || [])
          .map((url) =>
            `<img src="${url}" style="max-width:240px;display:block;margin-top:4px" ` +
            `onerror="this.style.display='none'">`
          )
          .join("");
        L.circleMarker([cam.lat, cam.lon], { radius: 8, color: "#facc15", fillOpacity: 0.6 })
          .bindPopup(
            `<b>${cam.name || cam.id}</b><br>${cam.src || ""} &middot; ${cam.distance_km} km` +
            `${cam.snapshots?.length > 1 ? ` &middot; ${cam.snapshots.length} cameras` : ""}<br>` +
            images
          )
          .addTo(p.cameraLayer);
      }
    });
  } catch (e) {
    console.error("camera fetch failed", e);
  }
}

async function refreshAlerts() {
  const on = document.getElementById("toggle-alerts").checked;
  if (!on || !siteLatLon) {
    panels.forEach((p) => p.alertsLayer.clearLayers());
    return;
  }
  try {
    const geo = await fetchJSON(`/api/alerts?lat=${siteLatLon[0]}&lon=${siteLatLon[1]}`);
    panels.forEach((p) => {
      p.alertsLayer.clearLayers();
      if (geo.features) {
        L.geoJSON(geo, {
          style: { color: "#f97316", weight: 2, fillOpacity: 0.08 },
          onEachFeature: (f, layer) => {
            const props = f.properties || {};
            layer.bindPopup(`<b>${props.event || "Alert"}</b><br>${props.headline || ""}`);
          },
        }).addTo(p.alertsLayer);
      }
    });
  } catch (e) {
    console.error("alerts fetch failed", e);
  }
}

async function refreshLevel3() {
  if (!siteLatLon) return;
  const jobs = [
    ["toggle-nst", "/api/level3/nst", "nstLayer", "#f472b6"],
    ["toggle-nmd", "/api/level3/nmd", "nmdLayer", "#a855f7"],
  ];
  // NST and NMD don't depend on each other -- fire both requests at
  // once instead of awaiting one fully before starting the next.
  await Promise.all(jobs.map(async ([toggleId, url, layerKey, color]) => {
    const on = document.getElementById(toggleId).checked;
    if (!on) {
      panels.forEach((p) => p[layerKey].clearLayers());
      return;
    }
    try {
      const data = await fetchJSON(url);
      panels.forEach((p) => {
        p[layerKey].clearLayers();
        for (const pt of data.points || []) {
          const [plat, plon] = kmOffsetToLatLon(siteLatLon[0], siteLatLon[1], pt.x_km, pt.y_km);
          L.circleMarker([plat, plon], { radius: 5, color, fillOpacity: 0.9 })
            .bindPopup(`<b>${data.product}</b><br>${pt.text}`)
            .addTo(p[layerKey]);
        }
      });
    } catch (e) {
      console.error(`level3 fetch failed (${url})`, e);
    }
  }));
}

async function refreshGps() {
  const on = document.getElementById("toggle-gps").checked;
  if (!on) {
    panels.forEach((p) => {
      if (p.gpsMarker) { p.map.removeLayer(p.gpsMarker); p.gpsMarker = null; }
      p.gpsTrail.setLatLngs([]);
    });
    return;
  }
  try {
    const pos = await fetchJSON("/api/gps");
    if (!pos || pos.available === false || pos.lat == null) return;
    const latlng = [pos.lat, pos.lon];
    gpsTrailPoints.push(latlng);
    if (gpsTrailPoints.length > 500) gpsTrailPoints.shift();
    panels.forEach((p) => {
      if (!p.gpsMarker) {
        p.gpsMarker = L.circleMarker(latlng, { radius: 6, color: "#38bdf8", fillOpacity: 1 }).addTo(p.map);
      } else {
        p.gpsMarker.setLatLng(latlng);
      }
      p.gpsTrail.setLatLngs(gpsTrailPoints);
    });
  } catch (e) {
    // no gpsd / no fix -- expected on the dev box, stay silent
  }
}

// ---------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------

document.getElementById("layout").addEventListener("change", (e) => {
  setLayout(parseInt(e.target.value, 10));
});

document.getElementById("base-layer").addEventListener("change", (e) => {
  baseLayerKey = e.target.value;
  panels.forEach((p) => {
    if (p.currentBaseKey === baseLayerKey) return;
    p.map.removeLayer(p.baseLayers[p.currentBaseKey]);
    p.baseLayers[baseLayerKey].addTo(p.map);
    p.currentBaseKey = baseLayerKey;
  });
});

document.getElementById("goes-layer").addEventListener("change", (e) => {
  goesLayerKey = e.target.value;
  panels.forEach(applyGoesLayer);
});

document.getElementById("toggle-sync").addEventListener("change", (e) => {
  syncEnabled = e.target.checked;
  if (syncEnabled && panels.length > 1) {
    const { center, zoom } = { center: panels[0].map.getCenter(), zoom: panels[0].map.getZoom() };
    panels.slice(1).forEach((p) => p.map.setView(center, zoom, { animate: false }));
  }
});

slider.addEventListener("input", () => {
  scanIndex = parseInt(slider.value, 10);
  renderAllPanels();
});

document.getElementById("live").addEventListener("click", () => {
  scanIndex = scans.length - 1;
  slider.value = scanIndex;
  renderAllPanels();
});

document.getElementById("play").addEventListener("click", (e) => {
  playing = !playing;
  e.target.textContent = playing ? "Pause" : "Play";
  if (playing) {
    playTimer = setInterval(() => {
      scanIndex = (scanIndex + 1) % Math.max(scans.length, 1);
      slider.value = scanIndex;
      renderAllPanels();
    }, 800);
  } else {
    clearInterval(playTimer);
  }
});

for (const id of ["toggle-cameras", "toggle-alerts", "toggle-nst", "toggle-nmd", "toggle-gps"]) {
  document.getElementById(id).addEventListener("change", () => {
    refreshCameras();
    refreshAlerts();
    refreshLevel3();
    refreshGps();
  });
}

document.getElementById("toggle-site-pills").addEventListener("change", refreshSitePills);

// ---------------------------------------------------------------------
// Radar site switching
// ---------------------------------------------------------------------

async function loadSiteList() {
  try {
    const { sites } = await fetchJSON("/api/sites");
    siteList = sites;
    siteSelect.innerHTML = "";
    for (const s of sites) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.name ? `${s.id} — ${s.name}` : s.id;
      siteSelect.appendChild(opt);
    }
    refreshSitePills();
  } catch (e) {
    console.error("site list fetch failed", e);
  }
}

// Each site is a real map marker at its actual coordinates, styled as a
// small clickable "pill" (L.divIcon, not a plain dot) showing the
// station ID -- clicking one switches to that radar, same switchSite()
// the dropdown uses. Redrawn (not just toggled) whenever the panel set
// changes or the active site changes, since divIcons can't be restyled
// in place without rebuilding them.
function refreshSitePills() {
  const on = document.getElementById("toggle-site-pills").checked;
  for (const p of panels) {
    p.siteMarkersLayer.clearLayers();
    if (!on) continue;
    for (const s of siteList) {
      const icon = L.divIcon({
        className: "site-pill-wrap",
        html: `<span class="site-pill${s.id === currentSite ? " site-pill-active" : ""}">${s.id}</span>`,
        iconSize: null,
      });
      L.marker([s.lat, s.lon], { icon, interactive: true })
        .on("click", () => switchSite(s.id))
        .addTo(p.siteMarkersLayer);
    }
  }
}

async function switchSite(newSite) {
  try {
    await fetchJSON(`/api/site?set=${encodeURIComponent(newSite)}`);
  } catch (e) {
    // e.g. one of the filtered-out non-WSR-88D ids, or a network error.
    // fetchJSON throws here (400/502 aren't resp.ok) rather than
    // returning a {error} object, so this is the only place that runs.
    setStatus(`site switch failed: ${e.message}`);
    siteSelect.value = currentSite || siteSelect.value; // revert dropdown to what's actually active
    return;
  }
  // Everything cached client-side belongs to the old site's location --
  // clear it all rather than let stale data linger under a new label.
  scanDataCache.clear();
  scans = [];
  scanIndex = -1;
  siteLatLon = null;
  for (const p of panels) {
    p.hasAutoFit = false; // let the new site's first scan re-frame the view
    p.tilt = 0; // a different site/VCP may not even have the same tilt count
    if (p.radarOverlay) { p.map.removeLayer(p.radarOverlay); p.radarOverlay = null; }
  }

  // Real measured latency: the backend needs ~8-10s to fetch + decode
  // the new site's first scan after a switch (S3 fetch + Py-ART decode,
  // see radar_lab.py). Previously this just waited for the next
  // scheduled tick() (up to 30s away, silently) -- actively poll every
  // 1.5s instead so it shows up as soon as it's actually ready, and the
  // status line explains what's happening in the meantime instead of
  // looking broken.
  setStatus(`waiting for first scan from ${newSite}...`);
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const status = await fetchJSON("/api/status").catch(() => null);
    if (status && status.scan_count > 0) break;
    await new Promise((r) => setTimeout(r, 1500));
  }
  await tick();
}

siteSelect.addEventListener("change", (e) => switchSite(e.target.value));

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------

async function tick() {
  try {
    const status = await fetchJSON("/api/status");
    siteEl.textContent = `(${status.site})`;
    if (siteSelect.value !== status.site) siteSelect.value = status.site;
    if (currentSite !== status.site) {
      currentSite = status.site;
      refreshSitePills(); // re-highlight which pill is active
    }
    await refreshScanList();
    if (!playing) renderAllPanels();
    // None of these three depend on each other or on renderAllPanels --
    // run them together instead of chaining. Previously a slow one (the
    // camera endpoint can take 1-3s on a cache miss, see
    // get_cameras()/CAMERA_CACHE_SEC in radar_lab.py) delayed everything
    // listed after it for no reason, every single tick.
    await Promise.all([refreshCameras(), refreshAlerts(), refreshLevel3()]);
  } catch (e) {
    setStatus(`status error: ${e.message}`);
  }
}

window.addEventListener("resize", () => panels.forEach((p) => p.map.invalidateSize()));
window.addEventListener("orientationchange", () => {
  setTimeout(() => panels.forEach((p) => p.map.invalidateSize()), 300);
});

// HUD collapse -- on a phone the options panel covers most of a
// portrait screen with no way to dismiss it, which was the actual
// complaint (not a bug, just no escape hatch). Collapsed by default on
// narrow screens, expanded by default on wider ones (desktop/dev box
// has plenty of room). Re-check width on resize so rotating a phone to
// landscape doesn't leave it stuck collapsed for no reason.
const hudEl = document.getElementById("hud");
const hudToggle = document.getElementById("hud-toggle");
function setHudCollapsed(collapsed) {
  hudEl.classList.toggle("hud-collapsed", collapsed);
}
hudToggle.addEventListener("click", () => {
  setHudCollapsed(!hudEl.classList.contains("hud-collapsed"));
});
setHudCollapsed(window.innerWidth < 700);
window.addEventListener("resize", () => {
  if (window.innerWidth >= 700) setHudCollapsed(false);
});

setLayout(1);
loadSiteList().then(tick); // populate the dropdown before tick() tries to select the current site in it
setInterval(tick, 30000);
setInterval(refreshGps, 3000);
