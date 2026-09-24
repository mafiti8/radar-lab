# Radar Lab

A free, self-hosted NEXRAD weather radar viewer — the core value of apps
like RadarScope or RadarOmega, built entirely on public NOAA/NWS data, with
no subscription and no account.

- **Live radar**: reflectivity, velocity, and dual-pol (ZDR/CC/PhiDP),
  with per-panel tilt selection across all of a site's real elevation
  angles — not just the lowest scan.
- **Split-screen**: view 1, 2, or 4 panels at once, each with its own
  product and pan/zoom — e.g. reflectivity next to velocity for the same
  storm, or two different storm sections side by side.
- **Any radar site, nationwide**: switch between all 159 real WSR-88D
  stations from a dropdown, or click a labeled pin at the station's
  actual location on the map.
- **Severe weather overlays**: live NWS storm-track and mesocyclone
  detection, plus active NWS alert polygons.
- **DOT traffic cameras**: live public traffic camera feeds (Indiana,
  Illinois, Kentucky, Wisconsin), clustered so multiple cameras at one
  interchange don't hide behind each other.
- **GOES satellite overlay**: infrared, visible, and GeoColor cloud
  imagery from NASA GIBS.
- **Streets, dark, or satellite basemap**, playback of recent scans, and
  (on hardware with a receiver) live GPS position overlay.

Everything renders client-side in the browser — the backend only ever
ships plain data, never pre-rendered images — so panning and zooming
stays responsive without a round trip to the server for every move.

## System requirements

- **OS**: Linux x86_64 only. The scientific dependency stack (Py-ART,
  NumPy, SciPy via MetPy) ships as compiled `manylinux` wheels with no
  ARM or Windows/macOS builds. Windows: use WSL2. macOS/ARM (Apple
  Silicon, Raspberry Pi): not supported.
- **Python**: 3.10 or newer.
- **RAM**: under 1GB steady-state (measured: ~225MB base + ~45MB per
  cached radar scan, ~13 scans in the default 90-minute rolling window).
  No swap or special tuning needed.
- **CPU**: any x86_64 CPU from roughly the last decade. Decoding a new
  volume scan takes ~2-3s, but that only happens once every 6-7 minutes
  (the real NEXRAD update cadence) — not a sustained load.
- **Disk**: ~650MB for the Python virtual environment (dominated by
  SciPy/pandas, pulled in by MetPy). No database, no permanent storage —
  everything is an in-memory rolling cache.
- **Network**: modest. ~9MB per radar volume scan every 6-7 minutes, plus
  small camera/alert/satellite-tile fetches. Comfortable over a
  cellular connection or Starlink, let alone home broadband.
- **Browser**: any modern browser. Built and tested against desktop
  Chrome/Firefox and iOS Safari.

## Install

```
./install.sh
```

Checks for Linux x86_64 + Python 3.10+, creates `.venv`, installs
dependencies, prompts for a NEXRAD site (defaults to `KVWX`), and
optionally installs itself as a `systemd --user` service. Safe to
re-run -- reuses an existing `.venv`/`.env` instead of clobbering them.
Verified 2026-09-23 against a clean directory (no `.venv`/`.env`
present) -- real install, real dependency install, real live decode
against current NEXRAD data afterward, not just written-and-assumed.

Or by hand:

```
python3 -m venv .venv
.venv/bin/pip install -r bin/requirements.txt
cp .env.example .env   # edit RADAR_LAB_SITE if not KVWX
.venv/bin/python bin/radar_lab.py
```

Then open `http://localhost:8297/`.

**Platform**: Linux x86_64 only for now. The dependency stack (Py-ART,
numpy, scipy via MetPy) ships as compiled `manylinux` wheels -- no ARM
build, no Windows/macOS wheels. Windows users would need WSL2; macOS and
ARM (Apple Silicon, Raspberry Pi, ARM Chromebooks) aren't supported at
all right now. If this gets published for wider use, that's the first
real limitation to flag for anyone outside Linux x86_64 -- worth
deciding then whether it's worth chasing (conda-forge has ARM/macOS
Py-ART builds, which might be an easier path than fighting pip wheels).

---

## Developer / build notes

Everything from here down is notes from the original build (on a home
server called "homehub"), kept for continuity and for anyone extending
this project -- **none of it is required to just run the software**; the
Install section above is everything you need for that. Full design doc
(data sources, architecture decisions, open questions, reasoning) is
referenced throughout as `~/docs/projects/radar-lab/README.md` -- that
path is on the original build machine, not part of this repo.

**Original deployment target was a dedicated field laptop** -- a
permanent copy also ended up running on homehub itself (the build
machine) as a `systemd --user` service, purely for convenience during
development (easy to check from a phone/browser without needing the
field laptop). If you're self-hosting this from a clone of this repo,
none of the following homehub-specific networking details apply to
you -- your own `./install.sh` run is all you need; how *this specific
original copy* was made reachable (Tailscale Funnel, a particular
firewall's allowlist, etc.) is homehub's own configuration, not a
requirement of the software itself.

## What's real vs. stubbed

- **Level II (reflectivity, velocity, and dual-pol: differential
  reflectivity/ZDR, correlation coefficient/CC, differential phase/
  PhiDP) and Level III (NST/NMD) decode**: fully real, polls the live
  public S3 buckets, decodes with Py-ART/MetPy. Handles split-cut VCPs
  correctly (some VCPs put velocity on a separate sweep from
  reflectivity/dual-pol at the same elevation angle -- found and fixed
  2026-09-23 after `/api/velocity` came back all-null on a real scan).
- **DOT traffic cameras**: fully real, live queries against
  travelmidwest.com (IN/IL/WI) + KYTC's ArcGIS FeatureServer (KY).
- **NWS alerts**: fully real, straight proxy to api.weather.gov.
- **GPS**: real gpsd client code (`gps3`), but soft-fails to "unavailable"
  when no gpsd/receiver is present -- true on this dev box by design,
  untested against real hardware. Verify against an actual USB GPS +
  gpsd once on the field laptop.
- **Split-screen** (2026-09-23): each panel is its own independent
  Leaflet map with its own product selector and pan/zoom (1/2/4-panel
  layouts, chosen from the HUD). Playback (which scan timestamp is
  shown) and the camera/alert/GPS/NST/NMD overlay toggles are shared
  across all panels rather than per-panel -- splitting *those* per panel
  wasn't part of the ask and would've meant panels drifting to different
  points in time, which defeats the point of comparing them side by
  side. An optional "sync pan/zoom" toggle links every panel's view
  together, for the "same area, different product" case (e.g.
  reflectivity + velocity side by side); leave it off for "two different
  storm sections" (independent pan/zoom per panel).
- **Dark basemap is Esri's `World_Dark_Gray_Base`, not CARTO** (fixed
  2026-09-23). The original choice was CARTO's `basemaps.cartocdn.com`
  -- "confirmed live" at the time meant "returns HTTP 200 with a valid
  PNG," which was true but not the whole story: CARTO now stamps a
  large "API KEY REQUIRED, carto.com/basemaps/apikey" watermark across
  every tile from that legacy free endpoint. Only caught this from a
  real screenshot once a browser was actually available to test with --
  a status-code check alone can't catch a service that still succeeds
  but degrades its own output. Real lesson for verifying any imagery
  source going forward: view the actual pixels, not just the HTTP
  response code.
- **Base map is a 3-way dropdown, not a satellite on/off toggle**
  (revised 2026-09-23 from real phone feedback): Streets (Esri
  `World_Street_Map` -- real road/highway labels, matters for
  correlating position against DOT cameras/GPS), Dark (Esri
  `World_Dark_Gray_Base`), Satellite (Esri `World_Imagery`). All three
  confirmed free/no-key by actually viewing tile pixels, not just
  checking HTTP status.
- **Map auto-fits to the radar's real coverage area on first load**
  (fixed 2026-09-23): previously started at a fixed
  `DEFAULT_CENTER`/`DEFAULT_ZOOM` that didn't match the radar site's
  actual extent, so the reflectivity echo showed up as a small blob in
  the corner of a huge multi-state view. Now calls `map.fitBounds()`
  once per panel the first time it gets real data (`panel.hasAutoFit`
  flag) -- deliberately only once, so it doesn't fight the user's own
  pan/zoom on every later scan update, and new panels created by
  switching layouts inherit the existing view instead of re-fitting.
- **HUD is now collapsible** (added 2026-09-23, from real phone
  feedback): previously always-visible and covered most of a phone's
  portrait screen with no way to dismiss it. Toggle button (top-right,
  &#9776;) shows/hides it; collapsed by default under 700px width,
  expanded by default above it.
- **GOES cloud imagery overlay** (2026-09-23): IR (Band 13, day/night),
  Visible (Band 2, day only), and GeoColor, via NASA GIBS' WMTS -- free,
  no API key, confirmed live with real tile fetches. Pure client-side
  addition, no backend endpoint needed at all (unlike every other data
  source in this project) -- it's just another Leaflet tile layer, using
  GIBS' `default` time-slot shortcut for "latest available tile" so the
  frontend never has to compute/guess a valid timestamp. Real update
  cadence is ~10 min. Investigated and ruled out two alternatives first:
  nowCOAST's old public GOES service moved behind an obfuscated SPA
  (couldn't find the new endpoint without a real browser's network tab);
  IEM's free GOES WMS exists but dropped GOES-East coverage back in 2018
  -- doesn't cover this project's actual region (KY/IN/IL) anymore.
- **Radar site switching** (2026-09-23): dropdown in the HUD, live from
  `api.weather.gov/radar/stations` (same authoritative NWS domain
  already used for alerts -- no static list to maintain, cached
  backend-side for an hour). Switching is a real architectural change,
  not just a new endpoint: `SITE` went from a fixed startup constant to
  `CACHE.site`, mutable at runtime via `GET /api/site?set=KXYZ`
  (deliberately GET, not POST -- matches this scaffold's existing
  "everything's a query param" style). Switching clears the cache
  (`Cache.set_site()`) and wakes the poller immediately via a
  `threading.Event` instead of waiting up to `POLL_INTERVAL_SEC` for the
  new site's first scan -- verified live, real KLVX data came back
  within ~10s of switching from KVWX. Frontend clears its own cached
  scan data and resets each panel's `hasAutoFit` flag on switch, so the
  map re-frames to the new site instead of staying zoomed on the old
  one's location.
  - **Real bug found and fixed same day**: switching appeared to just
    not work -- pill clicks registered, but no radar data ever appeared.
    Two compounding causes: (1) `fetchJSON()` threw away the response
    body on any non-2xx status, so the backend's real `{"error": "no
    scan cached yet"}` (a normal, temporary, ~8-10s state right after a
    switch -- measured live) got replaced with a generic "HTTP 503" and
    effectively swallowed; (2) `switchSite()` only tried once, then
    passively waited for the next scheduled `tick()` (up to 30s away) to
    retry -- easy to conclude it's broken before that fires. Fixed by
    having `fetchJSON` surface the real error message, and having
    `switchSite()` actively poll `/api/status` every 1.5s until data's
    ready (with a status message explaining the wait) instead of
    depending on the passive interval.
- **Site list filtered to real WSR-88D stations** (fixed 2026-09-23):
  the raw NWS list is 208 entries but includes TDWR airport radars and
  wind profilers (different networks, no data in this app's S3 bucket)
  -- e.g. `TBWI`/`TCMH` (TDWR), `AWPA2`/`HWPA2` (Alaska profilers).
  Found this by cross-checking the NWS list against which site folders
  actually exist in the live bucket, then discovering the NWS API
  already self-reports the field that explains it
  (`properties.stationType`) -- filtering on `stationType == "WSR-88D"`
  is a one-line, principled fix, no bucket cross-check needed at request
  time. Down to 159 real sites. Two legitimate WSR-88D sites still slip
  through with no data (`KCRP` -- real maintenance outage when checked;
  `RODN`, Okinawa -- valid station, just not published to this
  particular public bucket) -- not worth chasing further for a personal
  tool, same as any other real network hiccup.
- **Radar sites shown as clickable map pills** (2026-09-23): every
  filtered site renders as a small labeled marker (`L.divIcon`, not a
  plain dot -- the whole pill is the click target, not a tiny circle,
  which matters for tapping accurately on a phone) at its real
  coordinates. Clicking one calls the same `switchSite()` the dropdown
  uses. Active site's pill gets a highlighted style, tracked via a
  client-side `currentSite` variable kept in sync with `/api/status` on
  each `tick()`. Togglable (`toggle-site-pills`) in case 159 markers
  nationwide gets visually cluttered in practice -- untested in a real
  browser, worth checking first.
- **Perceived slowness, diagnosed and partly fixed** (2026-09-23):
  investigated from a "why does this feel slow" question rather than
  assumed. Ruled out with real measurements: server CPU (load average
  0.4 on 4 threads, essentially idle), and network path (direct
  Tailscale connection to a phone on home WiFi, no DERP relay hop --
  this is not "going through the internet" in the way that was
  suspected). Found two real, fixable causes instead:
  - `/api/cameras` took up to **3.06s on a cache miss** (measured) --
    two upstream fetches (travelmidwest.com's ~1MB multi-state payload +
    a KYTC query) ran sequentially in Python. Parallelized with
    `concurrent.futures.ThreadPoolExecutor` -- cold-cache time dropped to
    **0.52s** (measured after the fix), bounded by the slower of the two
    instead of their sum.
  - `tick()` (the frontend's ~30s refresh loop) chained six independent
    API calls with `await` in strict sequence -- status, scans, cameras,
    alerts, storm tracks, mesocyclone -- even though none of them depend
    on each other. Any one slow call (like the camera cold-fetch above)
    delayed everything listed after it. Fixed with `Promise.all()` for
    the three independent overlay refreshes (cameras/alerts/level3), and
    `refreshLevel3`'s own internal NST-then-NMD sequential loop got the
    same fix.
  - **Not yet addressed, real and inherent to the design**: each
    reflectivity/velocity/dual-pol fetch is still a genuine ~7.7MB JSON
    payload (§2's client-side-rendering tradeoff, documented from the
    start) -- multi-panel layouts multiply this by however many
    *different* products are visible at once (up to ~31MB for a 4-panel
    view showing 4 different products). This is a real cost of the
    architecture, not a bug, but worth knowing if performance still
    feels off after the fixes above.
- **Radar render resolution was artificially low, first patched then
  properly fixed 2026-09-23**: canvas size was a flat 700px regardless
  of the actual data, ~1315m/pixel for a ~460km-radius circle -- 5.3x
  coarser than the data's real 250m gate spacing, i.e. genuine
  thrown-away detail from the renderer, not a real radar limit. First
  pass sized the canvas off the real data dimensions instead (capped at
  1800px for compute time), which helped but still didn't hold up under
  real use on a laptop screen -- superseded same day by the real tile
  pyramid below, which doesn't have a resolution ceiling from a fixed
  canvas size at all.
- **True tile pyramid renderer, built 2026-09-23** (previously deferred
  same day as "harder than it sounds," then revisited after the
  interim fix above still didn't feel right in practice): `RadarTileLayer`
  in `web/app.js`, a real Leaflet `GridLayer` subclass -- each tile
  actually visible on screen renders fresh at whatever zoom the user is
  at, instead of one fixed-resolution image for the whole radar circle
  getting stretched by the browser. Stayed fully client-side, the
  original hard requirement: no server round-trip per tile/pan/zoom, the
  data fetch still happens once per scan update exactly as before, only
  *how* it's drawn from that same data changed.
  - Tile bounds come from `_tileCoordsToBounds()` (Leaflet's real Web
    Mercator projection for the tile's corners), then lat/lng is
    linearly interpolated *within* each small tile -- standard
    simplification, Mercator curvature inside one 256px tile is well
    under a pixel at any real zoom level.
  - Converting a tile pixel's lat/lng back to (range, azimuth) from the
    site reuses the exact same flat-local-plane approximation already
    used everywhere else in this file (`kmOffsetToLatLon`,
    `metersToLatLonBounds`) -- deliberately not introducing real geodesy
    just for this one piece, and it was already good enough at NEXRAD's
    ~460km range for everything else.
  - The "client-side + true tile pyramid is a much harder combination"
    concern from the original deferral turned out manageable: Leaflet
    itself handles the zoom-gesture animation (CSS-transforms the
    currently-loaded tiles smoothly while fetching new ones in the
    background), so this was never going to require continuous
    per-frame reprojection during the gesture -- just per-*tile*, same
    cost profile as any ordinary tile layer.
  - Dedicated Leaflet pane (`radarTilePane`, z-index 350) added so radar
    tiles are guaranteed to stack above the basemap and GOES overlay
    regardless of add/redraw order, rather than depending on DOM
    insertion order like they would sharing Leaflet's default tile pane.
  - **Genuinely the highest-risk unverified piece in this whole
    project.** Leaflet `GridLayer`'s tile lifecycle, the
    `_tileCoordsToBounds()` dependency (a protected/internal Leaflet
    method, not officially public API -- stable and commonly used in
    real plugins across Leaflet 1.x, but not a guaranteed contract),
    and canvas-per-tile rendering under actual pan/zoom interaction have
    never been seen in a real browser -- the dev environment this was
    built in still has none. Reasoned through carefully and reviewed
    line-by-line, but this is exactly the kind of integration where a
    subtle bug (tile misalignment, a coordinate sign error, tiles not
    clearing on redraw) would only show up visually, not as a thrown
    error. Check this before trusting it.
- **Camera markers with identical coordinates now grouped** (fixed
  2026-09-23, reported from real use near Evansville, IN): InDOT
  publishes 3 separate real cameras for the same I-64 interchange, all
  at bit-identical lat/lon -- confirmed via the raw upstream feed, not a
  guess. As separate map markers they stacked invisibly on top of each
  other, so only the topmost was ever clickable -- exactly the "click
  it, nothing happens (or no image)" symptom reported; taps were landing
  on whichever camera happened to be buried underneath, and which one
  that was wasn't predictable. `group_cameras_by_location()` in
  `radar_lab.py` merges cameras within ~11m into one marker with a list
  of snapshot images (`snapshots`, not a single `snapshot` string
  anymore -- frontend popup renders all of them). Verified live: went
  from 5 flat markers to 3 real distinct locations for the Evansville
  area, with the 3 previously-buried cameras now showing together in one
  popup.
- **Camera images auto-refresh + fullscreen lightbox, built 2026-09-24**:
  snapshot URLs are "latest image" endpoints on the source's own server
  -- the URL string itself never changes when a new frame is captured,
  so a browser left with a popup open kept showing the frame from
  whenever it was opened. `refreshVisibleCameraImages()` cache-busts and
  reloads every camera `<img>` actually in the DOM (popup content is
  removed from the DOM when a Leaflet popup closes, so this only ever
  touches images someone can actually see) every 15s. Clicking any
  camera image opens it fullscreen in a lightbox overlay (click again to
  close); the lightbox image participates in the same refresh loop.
- **Multi-state camera coverage + state/territory picker, built
  2026-09-24** (`camera-state` dropdown in the HUD, "Near radar site"
  default): originally only IN/IL/WI/KY. Added FL, GA, LA, PA, NC the
  same day, found by accident -- researching Florida's 511 platform
  turned up a shared, undocumented DataTables-style API
  (`/List/GetData/Cameras`) that turned out to be running identically on
  four more states, just a different domain each time (confirmed live,
  not assumed). South Carolina, Virginia, Texas, Alabama, Mississippi
  were tried and don't match this platform -- different systems, not
  yet researched. `/api/camera-states` reports which states actually
  have a source; the dropdown lists all 50 states + DC + PR/VI/GU/AS/MP
  regardless, marking unsupported ones "(no source yet)" rather than
  hiding them, since the ask was explicitly for hurricane-relevant
  coverage across the whole US.
  - **Real pagination problem solved**: the shared platform caps every
    response at 100 cameras/page server-side no matter what's
    requested (confirmed live -- asking for 10,000 on Florida's ~4,959
    still returned 100). A big state needs ~50 sequential requests at
    ~0.5-0.6s each -- parallelized across a 10-worker thread pool
    (same pattern as the original two-source camera fetch), cutting
    Florida's real full fetch from an estimated ~30s to **~5.8s
    measured live**.
  - **Deliberately on-demand, not preloaded** -- this was the explicit
    ask ("not auto-populate all the cameras in the US at once"). Only
    the state actually selected triggers a fetch; nothing loads for any
    other state until picked. Separate cache from the near-site one
    (`_state_camera_cache`, 5min TTL vs. 2min) since a full-state pull
    is much more expensive to redo.
  - **Real counts, verified live**: FL 4,787 grouped locations, GA
    3,095, PA 1,529, NC 1,042, LA 329, SC 787.
  - **South Carolina added, built 2026-09-24**: different platform than
    the DataTables one above -- Iteris ATIS
    (`sc.cdn.iteris-atis.com/geojson/icons/metadata/icons.cameras.geojson`),
    a plain GeoJSON feed, no pagination needed. `fetch_iteris_cameras()`
    in `radar_lab.py`. Tried the same `{state}.cdn.iteris-atis.com`
    pattern against 14 other state codes (VA/AL/MS/TX/TN/OK/AZ/NM/CO/
    UT/NV/OR/WA/CA) -- all failed, so this is SC-specific, not a second
    reusable multi-state shortcut. Verified live: 787 grouped cameras,
    sample image URL followed its redirect to a real `image/png` 200.
  - **Hazcams added, built 2026-09-24** (Hawaii's `HI` entry in the
    state dropdown): USGS Hawaiian Volcano Observatory hazard-monitoring
    webcams (Kilauea + Mauna Loa), not a DOT system at all -- scraped
    from `volcanoes.usgs.gov/cams/index.php` (`fetch_hazcams()`), no
    JSON API exists there. No real per-camera coordinates are published,
    only a shared summit per volcano -- spreading them naively onto that
    one point collapsed 18-19 real distinct cameras into a single
    giant-popup marker via the same location-grouping meant for
    genuinely co-located DOT cameras. Fixed by spreading each camera
    onto a small ring (~2km) around its volcano's summit. Verified live:
    31 distinct clickable markers, zero collisions.
  - **Still not resolved**: Virginia, Texas, Alabama, Mississippi. All
    four are JS-rendered single-page apps, not plain server-rendered
    pages -- a curl of the page HTML returns little or no real content.
    Virginia's Angular bundle (`main-FVQ2IQQK.js`) confirms a real
    `cameraFeed`/`cameraId` component exists client-side, but the actual
    backend API URL that populates it wasn't found by searching the
    bundle for literal `https://` strings -- it's likely built
    dynamically rather than hardcoded. Real attempts made, not just
    skipped; genuinely unresolved.
- **National radar mosaic, built 2026-09-23** (`toggle-mosaic` in the
  HUD, off by default): NOAA's own pre-merged national composite --
  MRMS `MergedReflectivityQCComposite`, all ~160 WSR-88D sites already
  combined by NOAA, no need to merge them ourselves. Real numbers,
  measured live: ~1.4MB per file, updates ~every 2 minutes (faster than
  a single site's own 6-7min cadence), polled every
  `RADAR_LAB_MOSAIC_POLL_INTERVAL_SEC` (default 60s) in its own thread
  (`mosaic_poll_loop`), independent of whichever single site is
  currently selected -- switching sites does not affect it.
  - **Mosaic mode hides the single-site radar layer** (added 2026-09-23,
    reported from real use as redundant clutter): showing the national
    composite and the per-panel single-site tiles over the same area at
    once was confusing, not useful. `setRadarLayersVisible()` toggles
    each panel's `radarTileLayer` on/off the map based on mosaic state --
    only visibility changes, the layer keeps fetching and redrawing with
    current data the whole time it's hidden (`renderPanelRadar` checks
    `isMosaicActive()` before attaching a newly-created layer, and skips
    `redraw()`'s tile-reload work via `map.hasLayer()` when hidden), so
    turning mosaic back off shows current data immediately instead of
    stale tiles from before it was hidden.
  - **The one deliberate exception to client-side rendering in this
    whole app.** The grid is 3500×7000 = 24.5M points -- far too large
    to ship raw for the browser to draw itself the way single-site data
    works. `decode_and_render_mosaic()` renders server-side to a
    1600×800 PNG (~370KB, picked from real measured file-size tradeoffs
    at several resolutions) using the same `DBZ_STOPS` color scale as
    the client-side `dbzColor()` (duplicated by hand, not shared code --
    one's Python, one's JS). "Server-side" here is still the same
    process on the same machine as everything else -- not a different
    remote service, see the design doc for why that's not a compromise
    of the eventual-standalone-laptop goal.
  - **Real dependency problem found and solved, not just noted**:
    decoding GRIB2 needs a new library. `cfgrib` (the more common
    xarray-based reader) decoded real files correctly but **crashed with
    reproducible memory corruption** (`double free`, then
    `free(): invalid pointer` on a separate run) during process cleanup,
    every time it was tested -- a bug in eccodes' own C bindings, not
    fixable from the Python side. Switched to `pygrib`, a lower-level
    binding to the same underlying library: zero crashes across 4+
    repeated decodes of the same file, and ~2x faster (~6.5s vs.
    cfgrib's ~12-17s). Chosen deliberately for reliability, not
    discovered by accident.
  - Verified as a real working pipeline before being wired into the app
    at all: fetched a live file, decoded it, colorized it, and looked at
    the actual output image -- real storm cell structure, not noise --
    before writing a single line of integration code.

## Known scaffold-level gaps (not bugs, just not done yet)

- ~~No tilt selector~~ -- **built 2026-09-23**, per-panel dropdown next
  to the product selector. Real design tradeoff, not just a UI add:
  decoding all ~9 tilts for every cached scan would multiply the
  measured ~45MB/scan cache cost 9x, blowing well past the "under 1GB"
  machine-sizing finding already reported. So tilt selection only works
  against the **current live scan** -- the backend keeps that one scan's
  raw bytes (~9MB) and decodes non-default tilts on demand (~1.7s
  measured live, since it reuses the cached raw bytes instead of
  re-fetching from S3), with its own small per-tilt cache cleared on the
  next new scan. Requesting a tilt against an older/playback scan
  returns a clear "only available for the current live scan" error
  rather than silently doing the wrong thing. Available tilt angles come
  from the backend's own response (`data.tilts`), not hardcoded --
  different VCPs use different elevation angles.
- ~~Never loaded in a real browser~~ -- **partially resolved 2026-09-23**
  via a real iPhone Safari screenshot: single-view layout, HUD controls,
  and the reflectivity overlay all confirmed rendering correctly at the
  right geographic location (this is also what caught the CARTO
  watermark issue above). Map container sizing was fixed at the same
  time (`position: fixed` instead of `absolute`, multiple
  `invalidateSize()` triggers instead of one timing guess -- mobile
  Safari's dynamic toolbar makes viewport height unreliable right at
  load). **Still genuinely unverified**: 2/4-panel split-screen layouts,
  the sync pan/zoom toggle, GOES overlay rendering, and panel
  creation/destruction on layout changes -- only single-view has been
  seen for real so far.
- ~~Dual-pol fields not drawn on the map~~ -- resolved as a side effect
  of the split-screen rewrite (2026-09-23): every panel's product
  dropdown now includes ZDR/CC/PhiDP with real color scales
  (`COLOR_FNS` in `app.js`). PhiDP is still shown raw, not as its
  derivative KDP (the actually-useful product) -- KDP isn't computed
  here, that's a real remaining follow-up, not just a wiring gap.
- Camera/alert/NST/NMD polling is on a fixed 30s timer regardless of playback
  state or map movement -- fine for a scaffold, wasteful long-term.
- No disk-backed cache yet -- `Cache` in `radar_lab.py` is in-memory only,
  so a restart loses the rolling scan history (matches "no permanent DB"
  from the design doc, but even the *rolling* 1-2hr cache doesn't
  survive a restart, which the doc didn't explicitly rule out).
- No real browser has ever loaded this page (dev environment has no
  browser installed) -- math/decode/payload numbers are real measured
  values, but actual on-screen rendering, Leaflet behavior, and Canvas
  API overhead are unverified. Check this first on real hardware.
- No git repo yet, no LICENSE -- both are real blockers before this can
  actually be published/shared, not yet set up (see project chat for
  why: commits happen when asked for, not unprompted).
