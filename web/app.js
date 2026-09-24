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

// All 50 states + DC + the populated territories -- the dropdown lists
// all of them (2026-09-24, for picking a hurricane-relevant area
// regardless of whether a camera source is registered there yet), but
// only some have a real backend source right now (see
// STATE_DATATABLES_DOMAINS/TRAVELMIDWEST_STATES in radar_lab.py).
// Picking an unsupported one shows a clear "no source yet" status
// instead of silently doing nothing.
const US_STATES = {
  AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California",
  CO: "Colorado", CT: "Connecticut", DE: "Delaware", DC: "District of Columbia",
  FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois",
  IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana",
  ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota",
  MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada",
  NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York",
  NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon",
  PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota",
  TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia",
  WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
  PR: "Puerto Rico", VI: "U.S. Virgin Islands", GU: "Guam",
  AS: "American Samoa", MP: "Northern Mariana Islands",
};

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
// True tile pyramid (2026-09-23, V2 upgrade -- previously deferred, see
// design doc §4). Replaces the old single fixed-resolution raster for
// the *whole* radar circle (which meant zooming in just stretched an
// already-rendered image) with a real Leaflet GridLayer: each tile
// actually on screen is rendered fresh, at whatever zoom the user is
// at, using the exact same polar (range/azimuth) lookup as before --
// only *how* it's drawn changes, not the underlying data or the lookup
// logic itself. Still fully client-side, still no server round-trip per
// tile/pan/zoom -- the data fetch already happened once per scan
// update, same as before.
//
// Tile bounds come from GridLayer's own _tileCoordsToBounds() (Leaflet's
// real Web Mercator projection, not an approximation) for the tile's
// corners, then lat/lng is linearly interpolated *within* the tile --
// standard simplification for small (256px) tiles, where Mercator
// curvature inside one tile is well under a pixel regardless of zoom.
// From there, converting a lat/lng back to (range, azimuth) from the
// site uses the same flat-local-plane approximation already used
// everywhere else in this file (kmOffsetToLatLon, metersToLatLonBounds)
// -- kept consistent rather than introducing real geodesy just for
// this, and already good enough at NEXRAD's ~460km max range.
const RadarTileLayer = L.GridLayer.extend({
  setData(props) {
    // siteLat, siteLon, azimuths, values, gate0, gateStep, nGate, colorFn
    Object.assign(this, props);
    this._maxRange = this.gate0 + this.nGate * this.gateStep;
    this._cosLat = Math.cos((this.siteLat * Math.PI) / 180);
  },

  createTile(coords, done) {
    const tile = document.createElement("canvas");
    const size = this.getTileSize();
    tile.width = size.x;
    tile.height = size.y;

    if (this.values) {
      const bounds = this._tileCoordsToBounds(coords);
      const nw = bounds.getNorthWest(), se = bounds.getSouthEast();
      const ctx = tile.getContext("2d");
      const img = ctx.createImageData(size.x, size.y);
      const nAz = this.azimuths.length;
      const mPerDegLat = 111320;
      const mPerDegLon = 111320 * this._cosLat;

      for (let py = 0; py < size.y; py++) {
        // Row-hoisted like the old renderer -- yM only depends on py,
        // no reason to recompute it for every px in the row.
        const lat = nw.lat + ((se.lat - nw.lat) * py) / size.y;
        const yM = (lat - this.siteLat) * mPerDegLat;
        for (let px = 0; px < size.x; px++) {
          const lng = nw.lng + ((se.lng - nw.lng) * px) / size.x;
          const xM = (lng - this.siteLon) * mPerDegLon;
          const rangeM = Math.sqrt(xM * xM + yM * yM);
          if (rangeM > this._maxRange || rangeM < this.gate0) continue;
          let azDeg = (Math.atan2(xM, yM) * 180) / Math.PI;
          if (azDeg < 0) azDeg += 360;
          const azIdx = Math.round((azDeg / 360) * nAz) % nAz;
          const gateIdx = Math.floor((rangeM - this.gate0) / this.gateStep);
          if (gateIdx < 0 || gateIdx >= this.nGate) continue;
          const row = this.values[azIdx];
          if (!row) continue;
          const v = row[gateIdx];
          if (v === null || v === undefined) continue;
          const color = this.colorFn(v);
          if (!color) continue;
          const idx = (py * size.x + px) * 4;
          img.data[idx] = color[0];
          img.data[idx + 1] = color[1];
          img.data[idx + 2] = color[2];
          img.data[idx + 3] = 200;
        }
      }
      ctx.putImageData(img, 0, 0);
    }

    // Leaflet's documented custom-GridLayer pattern: draw synchronously,
    // return the element immediately, and also call done() to signal
    // the tile-fade-in lifecycle. setTimeout(...,0) just yields a tick
    // rather than blocking Leaflet's own bookkeeping.
    setTimeout(() => done(null, tile), 0);
    return tile;
  },
});

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

function createPanel(product, site) {
  const container = document.createElement("div");
  container.className = "panel";
  const mapDiv = document.createElement("div");
  mapDiv.className = "panel-map";
  container.appendChild(mapDiv);

  const toolbar = document.createElement("div");
  toolbar.className = "panel-toolbar";

  // Per-panel radar site (2026-09-24) -- lets grid mode show genuinely
  // different sites side by side, not just different products/tilts of
  // the same one. Defaults to whatever the global "Radar site" HUD
  // dropdown currently shows; the HUD dropdown remains a "set all
  // panels" bulk action (switchSite()), this is the individual override
  // on top of it (switchPanelSite()).
  const siteSelectEl = document.createElement("select");
  populateSiteOptions(siteSelectEl);
  toolbar.appendChild(siteSelectEl);

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

  // Dedicated pane so radar tiles are *guaranteed* to render above both
  // the basemap and the GOES cloud overlay regardless of add/redraw
  // order -- all three are tile-ish layers, and without an explicit
  // pane they'd share Leaflet's default tilePane (z-index 200), where
  // stacking would depend on DOM insertion order instead of being
  // deterministic (a real risk once radarTileLayer.redraw() starts
  // manipulating tile DOM nodes on every scan update).
  map.createPane("radarTilePane").style.zIndex = 350;
  // National mosaic sits between the basemap/GOES (200) and the
  // single-site radar tiles (350) -- wide-area context underneath the
  // primary, more detailed single-site product where they overlap.
  map.createPane("mosaicPane").style.zIndex = 250;

  const panel = {
    container,
    map,
    site: site || currentSite || "", // backfilled in tick() if still empty once the real default is known
    siteSelect: siteSelectEl,
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
    radarTileLayer: null, // created lazily on first real data, see renderPanelRadar
    cameraLayer: L.layerGroup().addTo(map),
    alertsLayer: L.layerGroup().addTo(map),
    nstLayer: L.layerGroup().addTo(map),
    nmdLayer: L.layerGroup().addTo(map),
    gpsMarker: null,
    gpsTrail: L.polyline([], { color: "#38bdf8", weight: 2 }).addTo(map),
    siteMarkersLayer: L.layerGroup().addTo(map),
    mosaicOverlay: null, // current national-mosaic imageOverlay, if shown (see refreshMosaic)
  };
  siteSelectEl.value = panel.site;

  siteSelectEl.addEventListener("change", () => switchPanelSite(panel, siteSelectEl.value));

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
  refreshMosaic();
}

// ---------------------------------------------------------------------
// Radar rendering per panel
// ---------------------------------------------------------------------

async function fetchProductData(product, ts, tilt, site) {
  const cacheKey = `${site}:${product}:${ts || "live"}:${tilt}`;
  if (scanDataCache.has(cacheKey)) return scanDataCache.get(cacheKey);
  const endpoint = PRODUCT_ENDPOINTS[product];
  const params = new URLSearchParams();
  if (site) params.set("site", site);
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
  // The playback scrubber (scans[]/scanIndex) is fetched for a single
  // site (see refreshScanList) -- a panel showing a *different* site
  // than the default has no scan list of its own to scrub through, so
  // it always shows that site's live latest instead of whatever
  // timestamp the shared slider happens to be on. Real, documented V1
  // scope limit (same shape as the tilt-selection-only-on-latest-scan
  // limit already in place), not an oversight -- per-site playback would
  // need per-site scan lists, a bigger change than this feature asked for.
  const isDefaultSite = panel.site === siteSelect.value;
  const ts = isDefaultSite ? (scans[scanIndex] || null) : null;
  try {
    const data = await fetchProductData(panel.product, ts, panel.tilt, panel.site);
    // Cameras/alerts/NST/NMD overlays are all still scoped to one shared
    // location (see refreshLevel3, refreshCameras) -- only the
    // default-site panel(s) get to drive that shared siteLatLon, so a
    // panel showing a different site doesn't silently redirect those
    // overlays to the wrong place.
    if (isDefaultSite) siteLatLon = [data.lat, data.lon];
    syncTiltSelect(panel, data);
    const values = data[panel.product];
    if (!values) {
      setStatus(`no ${panel.product} in this scan`);
      return;
    }
    const isNewLayer = !panel.radarTileLayer;
    if (isNewLayer) panel.radarTileLayer = new RadarTileLayer({ pane: "radarTilePane", opacity: 0.75 });
    // setData() before addTo()/redraw() so even the very first tile
    // request already has real data -- otherwise Leaflet's automatic
    // initial tile load (triggered by addTo) would fire with .values
    // still unset, rendering a blank first pass that redraw() then has
    // to immediately redo.
    panel.radarTileLayer.setData({
      siteLat: data.lat,
      siteLon: data.lon,
      azimuths: data.azimuths,
      values,
      gate0: data.gate0_m,
      gateStep: data.gate_step_m,
      nGate: data.ngates,
      colorFn: COLOR_FNS[panel.product] || dbzColor,
    });
    // Keep the data current even while hidden (mosaic mode) so it's
    // ready to show instantly the moment mosaic gets turned back off,
    // instead of showing stale tiles for a beat -- only whether it's
    // *attached to the map* depends on mosaic being active.
    if (isNewLayer) {
      if (!isMosaicActive()) panel.radarTileLayer.addTo(panel.map);
    } else if (panel.map.hasLayer(panel.radarTileLayer)) {
      panel.radarTileLayer.redraw(); // drops currently-loaded tiles, re-requests visible ones with the new data
    }
    if (!panel.hasAutoFit) {
      // First real data this panel has ever shown -- zoom/pan to frame
      // the radar's actual coverage circle instead of leaving it at the
      // generic DEFAULT_CENTER/DEFAULT_ZOOM, which is what made the
      // radar echo look like a small blob in the corner of a huge
      // multi-state view. Only happens once per panel so it doesn't
      // fight the user's own pan/zoom on every later scan update.
      const maxRange = data.gate0_m + data.ngates * data.gate_step_m;
      panel.map.fitBounds(metersToLatLonBounds(data.lat, data.lon, maxRange));
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

function buildCameraPopup(cam) {
  // Some locations have multiple real cameras at the same coordinates
  // (found 2026-09-23 near Evansville, IN -- InDOT publishes 3 separate
  // cameras for one I-64 interchange) -- backend groups those into one
  // marker with several snapshots instead of stacking identical markers
  // invisibly on top of each other, which was making all but the
  // topmost unclickable.
  const images = (cam.snapshots || [])
    .map((url) =>
      `<img src="${url}" class="cam-img" data-base="${url}" ` +
      `style="max-width:240px;display:block;margin-top:4px;cursor:zoom-in" ` +
      `onerror="this.style.display='none'">`
    )
    .join("");
  // distance_km only exists in "near radar site" mode, not state mode --
  // state-scoped results aren't measured against any particular point.
  const distancePart = cam.distance_km !== undefined ? ` &middot; ${cam.distance_km} km` : "";
  const countPart = cam.snapshots?.length > 1 ? ` &middot; ${cam.snapshots.length} cameras` : "";
  return `<b>${cam.name || cam.id}</b><br>${cam.src || ""}${distancePart}${countPart}<br>${images}`;
}

async function refreshCameras() {
  const on = document.getElementById("toggle-cameras").checked;
  const stateCode = document.getElementById("camera-state").value;
  if (!on || (!stateCode && !siteLatLon)) {
    panels.forEach((p) => p.cameraLayer.clearLayers());
    return;
  }
  try {
    // Two modes (2026-09-24): "near radar site" (original behavior,
    // distance-filtered around wherever the active radar is) or a
    // specific state/territory the user picked explicitly -- picking a
    // state is the *only* thing that triggers fetching that state's
    // cameras; nothing is ever preloaded for states not selected.
    const url = stateCode
      ? `/api/cameras?state=${encodeURIComponent(stateCode)}`
      : `/api/cameras?lat=${siteLatLon[0]}&lon=${siteLatLon[1]}&radius_km=75`;
    const { cameras } = await fetchJSON(url);
    panels.forEach((p) => {
      p.cameraLayer.clearLayers();
      for (const cam of cameras) {
        L.circleMarker([cam.lat, cam.lon], { radius: 8, color: "#facc15", fillOpacity: 0.6 })
          .bindPopup(buildCameraPopup(cam))
          .addTo(p.cameraLayer);
      }
    });
    setStatus(`showing ${cameras.length} camera${cameras.length === 1 ? "" : "s"}` +
      (stateCode ? ` in ${stateCode}` : ""));
  } catch (e) {
    // fetchJSON surfaces the real backend message (e.g. "no camera
    // source registered for TX yet") on a non-OK response, not just a
    // generic HTTP code -- see fetchJSON's own comment for why that
    // matters.
    console.error("camera fetch failed", e);
    setStatus(`cameras: ${e.message}`);
    panels.forEach((p) => p.cameraLayer.clearLayers());
  }
}

// Camera snapshot URLs are "latest image" endpoints on the source's own
// server (the URL string itself never changes) -- browsers happily cache
// that, so a popup left open would keep showing the frame from whenever
// it was opened. Cache-bust and reload anything actually visible (popup
// content only exists in the DOM while its popup is open, so this never
// touches closed/off-screen cameras) plus the lightbox image if it's up.
let lightboxEl = null;
function refreshVisibleCameraImages() {
  document.querySelectorAll("img.cam-img").forEach((img) => {
    const base = img.dataset.base;
    if (!base) return;
    img.src = base + (base.includes("?") ? "&" : "?") + "_ts=" + Date.now();
  });
}
setInterval(refreshVisibleCameraImages, 15000);

function openCameraLightbox(url) {
  if (!lightboxEl) {
    lightboxEl = document.createElement("div");
    lightboxEl.id = "camera-lightbox";
    lightboxEl.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,0.9);z-index:5000;" +
      "display:none;align-items:center;justify-content:center;cursor:zoom-out";
    lightboxEl.innerHTML =
      '<img id="camera-lightbox-img" class="cam-img" style="max-width:95vw;max-height:95vh;">';
    lightboxEl.addEventListener("click", () => { lightboxEl.style.display = "none"; });
    document.body.appendChild(lightboxEl);
  }
  const img = document.getElementById("camera-lightbox-img");
  img.dataset.base = url;
  img.src = url;
  lightboxEl.style.display = "flex";
}

document.addEventListener("click", (e) => {
  if (e.target.classList && e.target.classList.contains("cam-img") && e.target.id !== "camera-lightbox-img") {
    openCameraLightbox(e.target.dataset.base);
  }
});

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

document.getElementById("camera-state").addEventListener("change", refreshCameras);

document.getElementById("toggle-site-pills").addEventListener("change", refreshSitePills);
document.getElementById("toggle-mosaic").addEventListener("change", (e) => {
  mosaicUpdated = null; // force a real refetch even if the meta timestamp hasn't changed
  setRadarLayersVisible(!e.target.checked);
  refreshMosaic();
});

// ---------------------------------------------------------------------
// Radar site switching
// ---------------------------------------------------------------------

async function loadCameraStateOptions() {
  const select = document.getElementById("camera-state");
  let supported = new Set();
  try {
    const data = await fetchJSON("/api/camera-states");
    supported = new Set(data.supported);
  } catch (e) {
    console.error("camera-states fetch failed", e);
  }
  for (const [code, name] of Object.entries(US_STATES)) {
    const opt = document.createElement("option");
    opt.value = code;
    // Not disabling unsupported ones -- picking one still gives useful
    // feedback (a clear "no source yet" status) rather than being
    // unselectable with no explanation at all.
    opt.textContent = supported.has(code) ? `${name}` : `${name} (no source yet)`;
    select.appendChild(opt);
  }
}

function populateSiteOptions(selectEl) {
  selectEl.innerHTML = "";
  for (const s of siteList) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.name ? `${s.id} — ${s.name}` : s.id;
    selectEl.appendChild(opt);
  }
}

async function loadSiteList() {
  try {
    const { sites } = await fetchJSON("/api/sites");
    siteList = sites;
    populateSiteOptions(siteSelect);
    // Panels created before this resolved (the very first panel, at
    // boot) got an empty site dropdown -- fill those in now that real
    // options exist, preserving whatever site each panel already has.
    for (const p of panels) {
      populateSiteOptions(p.siteSelect);
      p.siteSelect.value = p.site;
    }
    refreshSitePills();
  } catch (e) {
    console.error("site list fetch failed", e);
  }
}

// Each site is a real map marker at its actual coordinates, styled as a
// small clickable "pill" (L.divIcon, not a plain dot) showing the
// station ID -- clicking one switches *that panel's own map* to that
// radar (switchPanelSite(), 2026-09-24 -- previously always the global
// switchSite(), before panels could show different sites at all).
// Highlighted pill is per-panel too now (that panel's own site), not
// always the global default -- otherwise a grid panel deliberately
// showing a non-default site would misleadingly highlight some other
// site's pill on its own map. Redrawn (not just toggled) whenever the
// panel set changes or any panel's active site changes, since divIcons
// can't be restyled in place without rebuilding them.
function refreshSitePills() {
  const on = document.getElementById("toggle-site-pills").checked;
  for (const p of panels) {
    p.siteMarkersLayer.clearLayers();
    if (!on) continue;
    for (const s of siteList) {
      // iconSize: null (the original version of this) is not
      // well-defined L.divIcon usage -- Leaflet's internal size/anchor
      // math has undocumented behavior for it. Found live 2026-09-23:
      // pills rendered wildly oversized (tens to hundreds of km across
      // on screen), and since real neighboring NEXRAD sites are spaced
      // ~150-260km apart nationally, oversized pills for a site's
      // several real neighbors visually merged into a single connected
      // blob -- looked exactly like a giant, geometrically-impossible
      // radar echo, but was pure CSS/icon-sizing, nothing to do with
      // radar data at all. Fixed with the standard robust pattern for a
      // text-label divIcon: a deterministic near-zero icon box with no
      // Leaflet-side anchor offset, and let the CSS transform on
      // .site-pill (translate(-50%,-50%)) do all the actual centering
      // against the marker's real lat/lng point -- one mechanism, not
      // two different ones fighting each other.
      const icon = L.divIcon({
        className: "site-pill-wrap",
        html: `<span class="site-pill${s.id === p.site ? " site-pill-active" : ""}">${s.id}</span>`,
        iconSize: [1, 1],
        iconAnchor: [0, 0],
      });
      L.marker([s.lat, s.lon], { icon, interactive: true })
        .on("click", () => switchPanelSite(p, s.id))
        .addTo(p.siteMarkersLayer);
    }
  }
}

let mosaicUpdated = null; // last-known server render timestamp, avoids reloading an unchanged image

function isMosaicActive() {
  return document.getElementById("toggle-mosaic").checked;
}

// Showing both the single-site radar and the national mosaic at once is
// redundant clutter over the same area -- mosaic mode hides the
// per-panel radar tile layer instead of layering on top of it. Only
// visibility toggles (map.addLayer/removeLayer); the layer keeps
// fetching and redrawing with current data in the background the whole
// time (see renderPanelRadar), so turning mosaic back off shows current
// data immediately instead of stale tiles from before it was hidden.
function setRadarLayersVisible(show) {
  for (const p of panels) {
    if (!p.radarTileLayer) continue;
    const isShown = p.map.hasLayer(p.radarTileLayer);
    if (show && !isShown) p.radarTileLayer.addTo(p.map);
    else if (!show && isShown) p.map.removeLayer(p.radarTileLayer);
  }
}

async function refreshMosaic() {
  const on = document.getElementById("toggle-mosaic").checked;
  if (!on) {
    for (const p of panels) {
      if (p.mosaicOverlay) { p.map.removeLayer(p.mosaicOverlay); p.mosaicOverlay = null; }
    }
    return;
  }
  try {
    const meta = await fetchJSON("/api/mosaic");
    if (meta.updated === mosaicUpdated) return; // server hasn't rendered a newer one yet
    mosaicUpdated = meta.updated;
    // Cache-bust on the server's own render timestamp (not Date.now())
    // -- only actually refetches the image when there's a genuinely new
    // one, instead of every tick() regardless of whether anything changed.
    const url = `/api/mosaic.png?t=${encodeURIComponent(meta.updated)}`;
    for (const p of panels) {
      if (p.mosaicOverlay) p.map.removeLayer(p.mosaicOverlay);
      p.mosaicOverlay = L.imageOverlay(url, meta.bounds, { pane: "mosaicPane", opacity: 0.6 });
      p.mosaicOverlay.addTo(p.map);
    }
  } catch (e) {
    console.error("mosaic fetch failed", e);
  }
}

async function switchSite(newSite) {
  // The global HUD "Radar site" dropdown is a bulk action -- sets every
  // panel to this site, same as it always has. Per-panel overrides
  // (switchPanelSite(), each panel's own toolbar dropdown / clicking a
  // site pill on that panel's own map) are layered on top of this, not
  // a replacement for it -- picking a new default here still resets
  // every panel back to following it.
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
    p.site = newSite;
    p.siteSelect.value = newSite;
    p.hasAutoFit = false; // let the new site's first scan re-frame the view
    p.tilt = 0; // a different site/VCP may not even have the same tilt count
    if (p.radarTileLayer) { p.map.removeLayer(p.radarTileLayer); p.radarTileLayer = null; }
  }
  refreshSitePills();

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

// Per-panel override (2026-09-24) -- lets one grid panel watch a
// different site than the rest without touching any of them. Deliberately
// narrower than switchSite(): only this one panel's state resets, and it
// never touches the shared scanDataCache/scans/siteLatLon globals those
// belong to the default site's playback timeline and the
// cameras/alerts/NST/NMD overlays, which stay scoped to the default site
// -- see renderPanelRadar's isDefaultSite comment for why.
async function switchPanelSite(panel, newSite) {
  panel.site = newSite;
  panel.siteSelect.value = newSite; // keep the dropdown in sync when the change came from clicking a pill instead
  panel.hasAutoFit = false;
  panel.tilt = 0; // a different site/VCP may not even have the same tilt count -- syncTiltSelect() rebuilds the option list once real data comes back
  if (panel.radarTileLayer) { panel.map.removeLayer(panel.radarTileLayer); panel.radarTileLayer = null; }
  refreshSitePills();

  setStatus(`panel: waiting for first scan from ${newSite}...`);
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const status = await fetchJSON(`/api/status?site=${encodeURIComponent(newSite)}`).catch(() => null);
    if (status && status.scan_count > 0) break;
    await new Promise((r) => setTimeout(r, 1500));
  }
  await renderPanelRadar(panel);
}

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
      // Panels created before the real default site was known (the very
      // first panel, at boot, before this first tick()) started with an
      // empty panel.site -- backfill those now, without disturbing any
      // panel that's already been given an explicit site of its own.
      for (const p of panels) {
        if (!p.site) {
          p.site = status.site;
          p.siteSelect.value = status.site;
        }
      }
      refreshSitePills(); // re-highlight which pill is active on each panel
    }
    await refreshScanList();
    if (!playing) renderAllPanels();
    // None of these three depend on each other or on renderAllPanels --
    // run them together instead of chaining. Previously a slow one (the
    // camera endpoint can take 1-3s on a cache miss, see
    // get_cameras()/CAMERA_CACHE_SEC in radar_lab.py) delayed everything
    // listed after it for no reason, every single tick.
    await Promise.all([refreshCameras(), refreshAlerts(), refreshLevel3(), refreshMosaic()]);
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

// Night mode -- red, low-brightness theme for actual use in a moving
// vehicle after dark (a stray white popup or default-gray button is a
// real glare/distraction problem there, not just cosmetic). One
// always-visible button rather than a checkbox buried in the
// collapsible HUD, since this is the one setting someone might need to
// flip *while driving*, not while parked reading a menu. Persisted so
// a power-cycle (the real-world case: a vehicle-mounted device) doesn't
// reset back to a blinding-bright screen at night.
const nightToggle = document.getElementById("night-toggle");
function setNightMode(on) {
  document.body.classList.toggle("night-mode", on);
  nightToggle.classList.toggle("night-active", on);
  localStorage.setItem("radarLabNightMode", on ? "1" : "0");
}
nightToggle.addEventListener("click", () => {
  setNightMode(!document.body.classList.contains("night-mode"));
});
setNightMode(localStorage.getItem("radarLabNightMode") === "1");

setLayout(1);
loadCameraStateOptions();
loadSiteList().then(tick); // populate the dropdown before tick() tries to select the current site in it
setInterval(tick, 30000);
setInterval(refreshGps, 3000);
