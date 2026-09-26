# Radar Lab

A free, self-hosted NEXRAD weather radar viewer — the core value of apps
like RadarScope or RadarOmega, built entirely on public NOAA/NWS data, with
no subscription and no account.

- **Live radar**: reflectivity, velocity, and dual-pol (ZDR/CC/PhiDP),
  with per-panel tilt selection across all of a site's real elevation
  angles — not just the lowest scan.
- **Split-screen**: view 1, 2, or 4 panels at once, each with its own
  product, tilt, pan/zoom, and — as of 2026-09-24 — its own radar site.
  Watch two different storms in two different states at once, not just
  two products of the same storm.
- **Any radar site, nationwide**: switch between all 159 real WSR-88D
  stations from a dropdown, or click a labeled pin at the station's
  actual location on the map — globally (every panel) from the main HUD,
  or per-panel from that panel's own toolbar/pins.
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

- **OS**: built and verified on Linux x86_64. **Windows support is in
  progress (started 2026-09-24), not yet verified end-to-end** -- Py-ART,
  NumPy, and MetPy all install fine via plain `pip` on Windows per their
  own docs, and the one library that didn't (`pygrib`, used only for the
  optional national radar mosaic) is now an optional dependency --
  `radar_lab.py` runs and serves everything else fine without it (see
  `bin/requirements-mosaic.txt`). What hasn't happened yet: actually
  running this on a real Windows machine. Treat "should work" as exactly
  that until it's been tried. macOS/ARM (Apple Silicon, Raspberry Pi):
  not investigated at all yet.
- **Python**: 3.10 or newer.
- **RAM**: under 1GB steady-state **per active radar site** (measured:
  ~225MB base + ~45MB per cached radar scan, ~13 scans in the default
  90-minute rolling window). Since 2026-09-24, grid mode can have each
  panel watching a genuinely different site (see below) -- each one
  concurrently polled/cached/decoded means memory scales roughly
  linearly with how many *different* sites are actually open at once,
  not a fixed cost. Measured live with 3 different sites open across a
  grid: ~1.9GB total. Idle sites (no panel watching them anymore) are
  evicted after 30 minutes, so this doesn't grow unbounded over a long
  session -- but size for "however many different sites you'll realistically
  have open in a grid at once", not the old flat single-site number.
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

**Platform**: verified on Linux x86_64. Windows support is real work in
progress, not yet verified live -- see System requirements above.
macOS/ARM (Apple Silicon, Raspberry Pi, ARM Chromebooks) not
investigated yet. Worth deciding, once someone actually tries a Windows
run, whether conda-forge (has ARM/macOS Py-ART builds too) ends up an
easier path than chasing pip wheels per-platform.

### Running as a standalone desktop app (not a browser tab)

```
.venv/bin/pip install -r bin/requirements-desktop.txt
.venv/bin/python bin/radar_lab_app.py
```

Opens Radar Lab in a real native window (`pywebview`, wrapping the OS's
own webview -- WebView2 on Windows, WKWebView on macOS, WebKitGTK on
Linux) instead of a browser tab -- no address bar, no tabs, a genuine
double-click program. Same backend as `radar_lab.py`, just bound to
127.0.0.1 only and launched on a background thread instead of the
headless `serve_forever()` used for the systemd/server deployment; that
deployment path is unchanged and still the right choice for a
box other people reach over the network (e.g. Tailscale).

**Built 2026-09-24, not yet run in a real GUI environment** -- this dev
box is a headless server with no display, so `webview.create_window()`
and `webview.start()` have been checked against pywebview's real,
installed API (verified live: `inspect.signature()` against the actual
package, not guessed from memory) but never actually launched a window.
The one non-obvious fix already made: pywebview defaults to
`private_mode=True` (incognito), which would silently wipe `localStorage`
-- including the night-mode toggle's persistence -- on every relaunch;
`radar_lab_app.py` explicitly passes `private_mode=False`. Genuinely the
next thing to verify on a real machine before trusting this.

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
- **Per-panel radar sites, built 2026-09-24** (each panel's own toolbar
  now has a site dropdown, on top of the existing product/tilt ones):
  real architecture change, not a UI-only add. The backend used to track
  exactly one active site globally (`CACHE`/`SITE`, one poll thread) --
  replaced with a `get_cache(site)` registry: one `Cache` + one dedicated
  poll thread per site actually in use, created lazily on first request,
  evicted after 30 minutes idle. Chosen deliberately (asked, not
  assumed) over a lighter on-demand-snapshot alternative -- every open
  panel keeps getting real live updates regardless of how many different
  sites are open at once, not just the first one. Verified live: KVWX,
  KIND, KLVX all polling and decoding independently and concurrently,
  each returning its own real lat/lon/reflectivity, no cross-contamination.
  - **Real, documented scope limit**: the playback scrubber (`scans[]`/
    the slider) is fetched for one site's scan list -- a panel showing a
    *different* site than the global default has no synchronized scan
    list of its own to scrub through, so it always shows that site's
    live latest, ignoring the shared slider position. Same shape as the
    existing "tilt selection only works on the live scan" limit, not an
    oversight -- per-site playback would need per-site scan lists, a
    bigger change than this feature asked for.
  - **Also still shared/global, deliberately**: cameras, NWS alerts,
    storm-track/mesocyclone (NST/NMD), GPS, and the national mosaic all
    stay scoped to the single default site's location, not per-panel --
    only `siteLatLon` from the default-site panel(s) drives them. The
    ask was specifically about radar data per panel, not multiplying
    every secondary overlay by however many sites are open.
  - **The global "Radar site" HUD dropdown still works exactly as
    before** -- a bulk action that resets every panel to the same site.
    The new per-panel controls (that panel's own dropdown, or clicking a
    site pill on that panel's own map) are an override layered on top,
    not a replacement.
  - **Real memory cost, not free**: see System requirements above --
    measured ~1.9GB with 3 different sites open at once, versus the old
    flat ~1GB single-site number.
- **MADIS surface weather observations, built 2026-09-25** (`toggle-obs`
  in the HUD, off by default): real-time temperature, dewpoint,
  humidity, wind, and pressure from real weather stations -- not radar
  data, station-level ground truth. Public "guest" access, no auth
  needed, but took genuine trial and error to get right: the query needs
  ~15 form parameters, and several reasonable-looking guesses were
  simply wrong (`stasel="Y"` looked right from the field name but the
  real default is a hidden field set to `"0"`; `rdr="metar"` looked
  right but the page's own submit handler clears it to empty first) --
  only found the true values by reading the guest page's actual HTML
  form source line by line, not by guessing from field names or labels.
  Verified live against a real Midwest bounding box: 729 distinct
  stations, 4,677 observations, spanning genuinely different real
  networks (ASOS airport stations, RAWS fire-weather stations, MesoWest,
  citizen stations via APRSWXNET, marine/tide stations) -- confirms this
  is a real aggregator, not one single network. Scoped near the active
  radar site (asked for over a state picker) -- same bounding-box-
  around-a-point shape as the near-site camera mode, computed server-
  side and passed straight to MADIS's own native bbox query mode rather
  than over-fetching and filtering client-side. Off by default since a
  real bounding box can return several hundred stations at once, dense
  enough to clutter the map before anyone's actually asked for it.
- **Live snowplow truck tracking (Iowa), built 2026-09-25** (`toggle-
  snowplows` in the HUD): real position tracking, not the periodic
  dashcam photos above -- found while researching that feature, since
  its publisher's org name already contained "AVL" (Automatic Vehicle
  Location), a real hint that paid off. Public ArcGIS FeatureServer
  (`AVL_Direct_View`), no auth, genuinely rich data: heading, speed,
  road/air temperature, even material spread rates (salt/brine) and
  individual plow blade states. Rendered as a heading-rotated triangle
  marker (`snowplowIcon()`, CSS `rotate()` directly on the real compass
  heading -- no offset correction needed since a "▲" glyph already
  points north at 0deg, matching the heading convention).
  - **Iowa only** -- Nebraska and Minnesota (same publisher, same photo-
    feed pattern) checked and confirmed to only publish photos, not live
    position. Indiana's own TrafficWise system (what prompted this
    whole feature) isn't published as open data anywhere found -- same
    kind of JS SPA as VA/TX turned out to be, a separate uncracked
    investigation.
  - **Real, honest limitation, not a bug**: the source itself only
    reports trucks currently moving faster than 3mph -- a parked/idle
    truck isn't "active" and won't appear. Confirmed live 2026-09-25
    (September, no real winter operations) that the feed correctly
    returns a valid empty result rather than erroring -- but this also
    means the actual live truck data (real coordinates, a real heading
    rotating the marker correctly, popup content) has **not been
    visually verified this session** for the same reason mosaic/
    lightning needed a real event to confirm -- worth checking again
    once real snow operations are happening.
- **Lightning (GOES-East + GOES-West GLM), built 2026-09-24 (West added
  same day)**: real, free, near-
  real-time flash detections -- verified live against `noaa-goes19`
  (AWS S3, same no-auth-needed access pattern as everything else in this
  app), ~17-60s real measured latency, new files every 20s. Flash-level
  data only (lat/lon/energy), not the finer event/group hierarchy the
  raw files also carry -- that's what `glmtools` is for, overkill for a
  map overlay. `netCDF4` decodes it -- already an existing transitive
  dependency (pulled in by MetPy/Py-ART), and unlike `pygrib` it has real
  Windows wheels (verified 2026-09-24), so this is a hard dependency, not
  an optional one. Server-side rolling window (`GLM_WINDOW_MINUTES`,
  default 5min -- started at 15, cut down same day: real usage found an
  active storm accumulates ~1,700 flashes in just 4 real minutes, so 15
  minutes' worth turned into thousands of markers on screen, unreadable;
  expiry/fade were both already correct, the window itself was just too
  generous), filtered to a generous CONUS+margin bounding box before
  shipping as raw JSON (full-disk coverage is mostly ocean/South
  America, irrelevant here) -- verified live, ~72-102 real flashes per
  satellite per 20s window in range. Markers fade (smaller/more
  transparent) with age within that window so recent strikes read as
  more prominent -- the frontend reads the actual window size from the
  server's own response rather than hardcoding it a second time, so the
  fade timing can't drift out of sync with the real server-side cutoff
  the way a duplicated constant could.
  - **GOES-West added same day**: identical instrument, different bucket
    (`noaa-goes18`), same file format/cadence -- one poll thread now
    handles both satellites sequentially each 20s cycle (small, fast
    fetches, not worth a second thread). Confirmed live: GOES-West
    already sees real flashes as far east as Arizona/New Mexico, fixing
    the degraded-sensitivity gap GOES-East alone had toward the western
    edge of its field of view. No deduplication between the two where
    their coverage overlaps -- a real, accepted simplification (see the
    code comment for why matching flashes across satellites isn't a
    simple id/coordinate match). Verified live after doubling the decode
    load: memory spiked to ~730MB right after restart (2x the netCDF4
    decode work per cycle) then settled back to ~355MB and held stable
    through several cycles -- the malloc_trim fix handles this the same
    way it handled the single-satellite case.
- **All GOES satellite products, built 2026-09-24** (was GOES-East IR/
  Visible/GeoColor only): expanded to every product NASA GIBS actually
  publishes for GOES-East *and* GOES-West, confirmed directly against
  GIBS' own `WMTSCapabilities.xml` rather than assumed -- 6 products
  (IR, Visible, GeoColor, Air Mass, Dust, Fire Temperature) times 2
  satellites, all real, all checked live (real PNG tiles, not error
  placeholders). New "Satellite" dropdown alongside the existing
  product one. This same GOES-East/GOES-West pattern is exactly what
  made adding GOES-West to the lightning feature (below) a same-day
  follow-up rather than new architecture.
- **Default startup state changed, 2026-09-25**: everything off at
  launch except the national mosaic (was: cameras/alerts/GPS/NST/NMD/
  site-pills/lightning/snowplows on, mosaic off). A clean map on first
  load, not a cluttered one -- turn on what you actually want to see.
- **Composite Reflectivity + 5 new Level III radial products + 2 new
  severe-weather alert layers, built 2026-09-25**: real NOAA-precomputed
  products, not recomputed here -- found by listing every product NOAA
  actually publishes for a real site (100 distinct codes total) rather
  than guessing, then verifying each candidate's real data shape against
  an actual decoded file instead of assuming from the product family name.
  - **Composite Reflectivity (NCR)** -- "combine all the tilts" per
    radar site, NOAA's own precomputed maximum-reflectivity-across-every-
    elevation field (not recomputed from raw Level II tilts here --
    NOAA already does the real geometric work of combining tilts of
    different resolution correctly; redoing that would be a lot of work
    for a worse result). Turned out to be a real x/y raster grid (not
    radial data like every other product in this app), so it renders
    server-side to a PNG per site, same approach and same DBZ_STOPS
    color table as the national mosaic -- available as a "Composite
    Reflectivity (all tilts)" product choice per panel.
  - **5 new radial products** -- Storm Relative Velocity (N0S), Digital
    VIL (DVL), Enhanced Echo Tops (EET), Hydrometeor Classification
    (HHC), 1-Hour Precipitation (OHA). Confirmed live these share the
    *same* azimuth/range radial shape as the existing Level II products,
    so they reuse the existing client-side RadarTileLayer renderer as
    just more product choices -- no new rendering pipeline needed, only
    Composite Reflectivity did. Real bug hit and fixed during
    verification: `map_data()` needs the raw integer color-level codes
    as array indices, not floats -- an incorrect `dtype="float64"`
    coercion broke all 6 new server-rendered products with an "arrays
    used as indices" error until removed. A second real, undocumented
    quirk found live: Echo Tops' `map_data()` returns a `(values,
    is_below_radar_coverage)` tuple instead of a plain array, unlike
    every other product -- not documented anywhere obvious, found by
    the actual runtime error.
  - **Hydrometeor Classification's category colors are pulled from the
    decode library's own source** (MetPy's `DigitalHMCMapper`), not
    guessed -- the most trustworthy source available since it's
    literally what produces the numbers being colored.
  - **2 new severe-weather alert layers**: Hail Index (NHI) and
    Tornadic Vortex Signature (NTV) -- same point/graphic-page product
    family as the existing Storm Tracks/Mesocyclone layers, so these
    reused the existing generic point-decoder with no new decode logic,
    just two more product codes in the same poll loop. Both legitimately
    report empty whenever there's no active severe weather to detect --
    confirmed via real testing, same as NST/NMD's already-established
    behavior on a quiet day, not a bug.
- **Site indicator consistency fix, built 2026-09-24**: in single-pane
  view, the top-right HUD "Radar site" dropdown could drift out of sync
  with the panel's own toolbar dropdown / active site pill -- only the
  HUD one updated the shared default site, so switching sites via the
  panel's own control (or clicking a pill) left the HUD showing something
  stale. Fixed: with exactly one panel, `switchPanelSite()` now just
  delegates to the same bulk `switchSite()` path, since "set this panel"
  and "set the shared default" are the same operation when there's only
  one panel. In grid mode the two are genuinely different (panels can
  legitimately show different sites) -- relabeled the HUD dropdown
  "Radar site (all panels)" to make clear it's a bulk-apply action, not a
  live readout of any one panel.
- **Pins, shapes, and CSV/KML export, built 2026-09-24** (draw toolbar,
  top-right of every panel's own map -- `leaflet-draw`): drop pins and
  draw polygons/rectangles/polylines/circles directly on the map. One
  shared set of pins/shapes across every panel (same "shared data drawn
  into every panel's layer" pattern as cameras/alerts/NST/NMD) -- draw in
  any panel, see it in all of them. Deliberately session-only, not
  restored on reload (explicitly asked for) -- but every change
  auto-saves (debounced 2s) to a timestamped file on the server
  (`exports/marks_<session>.csv`/`.kml`) as a rolling backup, plus manual
  "Pins (CSV)" / "All (KML)" download buttons in the HUD for grabbing the
  latest save directly. KML (not CSV) is the real target for "export to
  Google Maps" -- it's Google My Maps' native import format and is the
  only one of the two that can carry shapes at all, not just points;
  verified live: a real drawn polygon + circle + pin round-tripped
  through `/api/marks/save` into valid KML with a correctly-closed
  polygon ring and a circle approximated as a 36-point ring (KML has no
  native circle primitive). CSV is pins only, matching what was actually
  asked for there.
- **Illinois camera images fixed, 2026-09-25** -- real referer-blocking
  bug, not a dead data source. `cctv.travelmidwest.com` (859 of
  Illinois's 1,334 cameras -- IDOT D1/D4, DuPage County, Kane County)
  actively 403s any request whose Referer header isn't its own site,
  which is exactly what a browser sends by default loading an `<img>`.
  Confirmed live: the same URL returns 200 with no Referer at all but
  403 with this page's own origin as Referer -- the backend's raw data
  was always fine (99.7% of all 1,334 IL image URLs checked out live),
  the images were only ever broken in a real browser. Fixed with
  `referrerpolicy="no-referrer"` on the camera `<img>` tags (suppresses
  the Referer header entirely for that request) -- covers every camera
  image in the app, not just Illinois, so any other source with the
  same hotlink-protection habit is fixed for free too. Indiana and
  Wisconsin cameras mostly route through different hosts
  (`content.trafficwise.org`, `511wi.gov`) that don't have this
  restriction, which is why this specifically read as an Illinois-only
  problem even though the fix isn't Illinois-specific.
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
  - **Virginia added 2026-09-25** (1,679 grouped cameras): its own
    fourth distinct platform ("iLog"), found by downloading and grepping
    all 46 of the Angular app's lazy-loaded JS chunks (the main bundle
    alone didn't have it) for the real `getCamerasArray()` call --
    `BASE_URL+"/array/cameras"`, same-origin on `511.vdot.virginia.gov`
    itself via a relative config path (`NODE_ENDPOINT.foo`), not a
    third-party host. Verified live: real image URLs, real 200
    `image/png` responses.
  - **Mississippi added 2026-09-25** (456 cameras): a fifth platform --
    an older ASP.NET WebForms site (not a SPA), found via its classic
    `ScriptManager` "PageMethods" pattern (`Default.aspx/LoadCameraData`,
    POST an empty JSON body). That one request gives coordinates for all
    456 cameras, but not image URLs -- those only exist inside each
    camera's own iframe "bubble" page
    (`mapbubbles/camerasite.aspx?site=N`), fetched individually,
    parallelized (`ThreadPoolExecutor(max_workers=10)`, same pattern as
    the DataTables states' pagination). Real measured cost: ~14-18s for
    the full state on a cache miss.
  - **New York and New England (NH/ME/VT) added 2026-09-25** -- same
    DataTables platform as FL/GA/LA/PA/NC, found with a quick domain
    check (`511ny.org`, `newengland511.org`) rather than a deep
    reverse-engineering dig, once the Northeast became the priority for
    an actual approaching storm. New York: 1,877 real cameras. New
    England: one shared domain covering three states at once via each
    row's own `areaId` field (confirmed live: exactly NH/ME/VT, 406
    total records, no other state present) -- a new mode added to
    `fetch_datatables_cameras` (`state_code=None`) rather than a
    separate function, reusing the exact same pagination/parsing
    already proven for the single-state sources. Massachusetts/Rhode
    Island/Connecticut checked against several likely domains and don't
    match this platform -- not found yet, not on this platform.
    Real reliability issue found and fixed during verification:
    511ny.org's server intermittently 500s on some fraction of the ~19
    concurrent paginated requests a state its size needs -- confirmed
    transient (different pages failed on repeated runs, not the same
    ones) by literally re-running the fetch 3 times and watching the
    failure count change. Fixed with one automatic retry per page,
    benefiting every state on this platform, not just New York.
  - **Real vendor identified, more states found, 2026-09-26**: chasing
    Massachusetts specifically led to Massachusetts's own JS bundle
    referencing `511ny.org`, `cttravelsmart.org`, `az511.gov`,
    `cotrip.org`, `511ia.org`, `kandrive.org`, and `nmroads.com` as
    sibling deployments -- "CARS Program" / Castle Rock ITS runs this
    same DataTables platform (what FL/GA/LA/PA/NC/NY were already
    running on) across a real nationwide list of states, not just a
    coincidence limited to the Southeast. Confirmed two more this way:
    **Arizona** (644 cameras, `az511.gov`) and **Connecticut** (347
    cameras -- its public-facing `cttravelsmart.org` redirects to the
    real API host, `ctroads.org`). Also found a real upgrade for
    **Wisconsin**: `511wi.gov` (this same platform) has 490 real cameras
    vs. the 263 the existing TravelMidwest source was returning for the
    same state -- switched WI to this source rather than keeping both.
    - **Massachusetts, Colorado, Iowa, Kansas -- cracked and added,
      2026-09-26**: this vendor turns out to run at least two more
      generations beyond the classic DataTables platform. Massachusetts's
      own JS bundle builds a real API-map object
      (`{accounts,amber,cameras,cms,...}`) that resolves to a
      microservices backend, not its `mass511.com` frontend (same pattern
      as Connecticut) -- found via that object, confirmed at
      `matg.carsprogram.org/cameras_v1/api/cameras` (a plain JSON array,
      307 cameras). The domain-naming pattern it revealed
      (`{2-letter state}tg.carsprogram.org`) worked directly for
      **Iowa** (`iatg.carsprogram.org`, 859 cameras) and **Kansas**
      (`kstg.carsprogram.org`, 608 cameras) by guess alone. A parallel
      "stage.carstest.org" form of these same domains also answers but
      serves visibly stale data (older `lastUpdated` timestamps,
      confirmed by direct comparison) -- the clean `.carsprogram.org`
      form is what's actually used. **Colorado** (`cotrip.org`) is a
      third, newer generation again: its real runtime config
      (`511.cotrip.org/configs/main.json`) names the camera API host
      (`api-511x-co.carsprogram.org`), whose bare root only answers a
      generic `{"healthy":true}` -- the real data endpoint,
      `/cameras/map-features` (a GeoJSON FeatureCollection, 1,024
      cameras), was found in Colorado's own JS bundle where the frontend
      actually builds that request. All four verified live end-to-end
      through radar-lab's own `/api/cameras` endpoint, not just against
      the upstream APIs directly (grouped counts: CO 794, IA 663, MA 280,
      KS 201 after de-duplicating co-located cameras). **New Mexico**
      checked and confirmed *not* on this platform at all --
      `nmroads.com` is a real but unrelated, older WebGL-based site;
      several domain-pattern guesses against `carsprogram.org` all
      failed to connect.
    - **Minnesota and Nebraska added, 2026-09-26**: same
      `{state}tg.carsprogram.org` pattern, found by brute-forcing every
      remaining state's 2-letter code against it rather than chasing
      another bundle. **Minnesota** (`mntg.carsprogram.org`): 1,528 real
      cameras. **Nebraska** (`netg.carsprogram.org`): 350 real cameras,
      but almost none of them carry the `videoPreviewUrl` field the
      other states on this platform use -- found live that Nebraska's
      own `views` entries are already plain still images
      (`type: "STILL_IMAGE"`, a direct `url` field), a real per-state
      schema difference, not missing data. Fixed by falling back to
      that field when no `videoPreviewUrl` is present, which fixed
      Nebraska without touching any other state's behavior.
  - **Rhode Island added, 2026-09-26**: a distinct platform again, nothing
    to do with CARS Program -- the real data is a public Esri ArcGIS
    FeatureServer layer (`risegis.ri.gov/hosting/rest/services/RIDOT/
    Rhodeways/MapServer/6`), found by tracing RIDOT's interactive camera
    map into its own JS, which constructs a standard ArcGIS
    `FeatureLayer` pointed straight at it -- a documented, queryable
    endpoint, not something needing further reverse-engineering once
    found. 143 real cameras, each with WGS84 lat/lon already on the
    attributes and a direct snapshot field. This closes the original
    Northeast gap (RI, alongside MA and CT, was one of the three states
    flagged as "not found yet" back on 2026-09-25) -- MA and CT are now
    solved via CARS Program above, RI via this ArcGIS route.
  - **Washington added, 2026-09-26**: another ArcGIS FeatureServer,
    found a different way -- WSDOT's own camera-map SPA loads its
    endpoint from a runtime config object that never appears as a
    literal string anywhere in its JS bundle (grepping it the way that
    worked for Rhode Island came up empty), so the actual endpoint was
    found by going straight to WSDOT's own public ArcGIS Server
    (`data.wsdot.wa.gov/arcgis/rest/services`) and browsing its real
    folder listing until a `TravelInformation/TravelInfoCamerasWeather`
    service turned up. 1,705 real cameras, queried with `outSR=4326` so
    ArcGIS reprojects to plain lat/lon server-side instead of a manual
    Web-Mercator conversion. Includes some real cross-border cameras
    (Oregon's own tripcheck.com feed for shared I-5 crossings) -- a
    real feature of WSDOT's own data, not a bug here.
  - **Montana and South Dakota added, 2026-09-26**: found on the same
    Iteris ATIS vendor South Carolina already uses
    (`{state}.cdn.iteris-atis.com/geojson/...`), found the same way as
    the Minnesota/Nebraska win above -- brute-forcing every remaining
    state's 2-letter code against the known URL pattern rather than
    digging through another bundle. Both real but small, rural-interstate
    camera networks (38 sites/38 cameras for MT, 40 sites/173 cameras
    for SD) -- not a partial feed, genuinely how sparse these states'
    camera networks are. Also a distinct, older schema from South
    Carolina's: each site holds a real `cameras` array (multiple
    views -- north/south/road-surface -- at the same physical pole)
    rather than one flat `image_url` per site, needing a second fetcher
    function rather than reusing South Carolina's.
  - **Utah, Nevada, Idaho, and Alaska added, 2026-09-26**: the exact
    same DataTables `/List/GetData/Cameras` platform yet again, but this
    time it's really IBI Group's "ibi511" product line (the vendor
    behind `prod-ut.ibi511.com`'s and `prod-nv.ibi511.com`'s own
    separate, *keyed* developer APIs) sharing the identical unauthenticated
    public-site backend that "CARS Program" states also use -- found by
    testing the known endpoint directly against each state's likely
    domain rather than digging through another bundle. Zero new parsing
    code needed since `fetch_datatables_cameras` already handles this
    shape as-is. Real counts confirmed live: Utah 2,081, Nevada 652,
    Idaho 457, Alaska 130.
  - **Maryland added, 2026-09-26**: CHART (the state's traffic
    management system) does have a public ArcGIS FeatureServer for
    camera locations, but its own `url` field there is just an HTML
    player page, not a usable link -- one more layer than Rhode
    Island's version of this same idea needed. The real source is
    CHART's own JSON feed (`chart.maryland.gov/DataFeeds/
    GetCamerasJson`, found by searching for it directly rather than
    digging further into the ArcGIS layer), whose `publicVideoURL` is
    itself *another* HTML player page -- but that page's own inline
    script builds the real HLS stream URL from two fields already
    present in the original JSON (`cctvIp` + `id`), so the wrapper page
    never actually needs fetching. 552 real cameras with working live
    video (confirmed a real HLS manifest), unlike Texas's expired-token
    problem -- reuses the same hls.js wiring built for TX.
  - **California added, 2026-09-26**: found through Caltrans's own
    *documented, public* API (CWWP2, no key needed) rather than
    reverse-engineering QuickMap's SPA -- the easiest route of any
    state found this session, once its documentation page was found.
    Real per-district JSON feeds (`cwwp2.dot.ca.gov/data/{d1..d12}/
    cctv/cctvStatus{D01..D12}.json` -- zero-padded past D9 but not
    before it, a real inconsistency in Caltrans's own filenames), each
    camera carrying both a real static image URL and a real HLS stream
    URL. No single statewide endpoint exists, so this needs 12 separate
    requests (parallelized) -- by far the largest state found this
    session at 3,591 real cameras.
  - **Missouri added, 2026-09-26**: found via ArcGIS Online's own public
    content search (searching directly for "MoDOT camera" rather than
    digging through traveler.modot.org's SPA) -- a real, currently
    maintained Feature Service owned by Missouri's state emergency
    management account. Its layer id is 1, not the usual 0 (checked the
    FeatureServer's own layer list rather than assuming). 871 real
    cameras with working HLS video via a `URL2` field (`URL1` is always
    null on every record -- a real per-field quirk, not a bug here).
  - **Oregon added, 2026-09-26**: same ArcGIS-content-search approach as
    Missouri and Maryland -- a real, recently-updated Feature Service
    ("Oregon Traffic Cameras," owned by Oregon's own emergency
    management account) pointing at ODOT's TripCheck system
    (`TripCheck_Cameras/FeatureServer`) -- the same tripcheck.com already
    seen as the source for some of Washington's shared border cameras.
    1,188 real cameras -- more than the service's own 1000-per-page
    default cap returns in one query, the first ArcGIS source this
    session that actually needed real pagination (`resultOffset`)
    rather than one request being enough.
  - **Arkansas found but not added**: ArcGIS Online search turned up a
    real, current ArDOT Feature Service (`iDriveCCTV_20260707_v2`), but
    it's a small subset (49 cameras, not the 500+ IDriveArkansas is
    known to run) and its HLS stream requires a `Referer` header
    matching ArDOT's own frontend domain exactly (confirmed live: no
    Referer gets a 403, the literal `idrivearkansas.com` value gets a
    real redirect to a working tokenized stream) -- browsers send the
    *requesting* page's own origin as Referer for cross-origin video
    fetches, not an arbitrary spoofed one, so radar-lab's own frontend
    can't satisfy this check. Not pursued further given the low camera
    count already found didn't justify chasing a workaround.
  - **Alabama added, 2026-09-26**: same ArcGIS-content-search approach
    again -- a real Feature Service (`ALDOT_TC_HFL_public`) tied to
    ALGO Traffic (University of Alabama's Center for Advanced Public
    Safety, which actually runs ALDOT's camera system day to day). Real
    static `ImageUrl` per camera (confirmed live), but its `StreamUrl`
    field 404s on every camera spot-checked -- a systemic problem with
    that field specifically, not per-camera flakiness -- so only the
    working static image is used. 556 real cameras.
  - **North Dakota added, 2026-09-26**: traced travel.dot.nd.gov's own
    Angular bundle for its real backend domain
    (`travelfiles.dot.nd.gov`) and the exact function that builds each
    map layer's URL from it, confirming the "cameras" layer resolves to
    a real, public, no-auth GeoJSON. Same multi-camera-per-site shape as
    Montana/South Dakota's Iteris data (a `Cameras` array per site) but
    a different vendor/schema (`LinkPath` per camera, not `image`) --
    needed its own fetcher. 189 sites, 809 real cameras.
  - **Michigan added, 2026-09-26**: the oldest-feeling platform found
    this session -- a real plain JSON array
    (`mdotjboss.state.mi.us/MiDrive/camera/list`, confirmed via a
    GitHub PR that had already reverse-engineered it) where every field
    that should be structured data is instead a pre-rendered HTML
    fragment: coordinates have to be regexed out of an embedded "Go to"
    link's query string, and the image URL out of an embedded `<img>`
    tag, rather than either being its own real field. 804 real cameras,
    image URL 301-redirects to a real working `micamerasimages.net`
    image once followed.
  - **Tennessee added, 2026-09-26**: SmartWay is a modern Angular SPA
    that -- like Washington's -- loads its real API config at runtime
    rather than baking the URL into its JS bundle, but found the config
    file itself this time by grepping the shared vendor chunk for the
    literal `config.prod.json` reference its loader function uses,
    rather than going around it via a public GIS server the way
    Washington's needed. That reveals both the real API base URL
    (`tdot.tn.gov/opendata/api/public/`) and a real, plainly-embedded
    client-side API key -- meant to be public since it ships in every
    page load, same as a Google Maps browser key. 668 real cameras,
    each with a real static thumbnail and a real HLS stream (both
    confirmed live); ~129 are marked inactive in the feed and skipped.
  - **Oklahoma found but not added**: real camera positions (761,
    confirmed) live behind a LoopBack API found by grepping the site's
    own Angular bundle for its service-layer HTTP calls
    (`CameraPoles?filter=...`, not documented or guessable from the
    site's own public error messages alone). Every camera's HLS
    `streamSrc` 404s, though -- not a stale-token problem like Texas's,
    but Cloudflare's bot-challenge page intercepting the request before
    it reaches the real video server (`stream.oktraffic.org` itself
    returns a JS challenge page, confirmed by fetching it directly).
    That challenge blocks non-interactive requests generally, including
    the kind a `<video>` tag's own fetch would make from a real
    browser, not just this backend -- a real infrastructure blocker,
    not something fixable by adjusting headers.
  - **West Virginia found but not added**: real camera positions (132)
    live behind a genuinely old ASP.NET map widget
    (`wv511.org/wsvc/gmap.asmx/buildCamerasJSONjs`, found by tracing
    three layers of JS -- the map page's own script, into a Google Maps
    wrapper, into a lazily-loaded "cameras" sub-script that finally
    named the real endpoint). That feed carries positions and labels
    but no snapshot/stream URL at all -- the actual streaming logic
    (`LoadStreamingCam`) is called from two different pages but its
    definition wasn't found in any JS file either page actually loads,
    and guessing a Wowza-style stream domain from the pattern several
    other states use didn't connect. Positions only, no viewable image
    -- not pursued further.
  - **New Jersey found but not added**: 511nj.org's own Angular bundle
    references a real `cameraTileService`/`getCameraList()` call, but
    every `/api/*` path guess against the site returned a WAF block
    (403 "Access Denied") rather than a real 404 -- suggesting the real
    API is either on a different, unguessed subdomain or is actively
    defended against exactly this kind of probing. Not cracked this
    session.
  - **Ohio, Wyoming, Delaware's DC-equivalent** (i.e. genuinely not
    pursued to a conclusion): OHGO's public API is real but requires a
    registered developer key (not just an unauthenticated public
    endpoint like every other state found this session); Wyoming's
    511 site is old-style server-rendered ASP.NET with no obvious JS
    bundle to trace, not dug into further given time spent on West
    Virginia's similar platform coming up empty.
  - **Delaware added, 2026-09-26**: real camera list lives behind a
    genuinely old-school jQuery plugin (`camerafy`, built to feed a
    JW Player instance) rather than a modern SPA -- found by fetching
    that plugin's own minified JS and reading its `$.getCameraFeed`
    function directly, which hardcodes both the real endpoint and a
    fixed query-string id (`tmc.deldot.gov/json/videocamera.json?
    id=4yte`). 360 real cameras, almost all enabled/active, each with a
    working HLS URL confirmed live.
  - **Texas added 2026-09-25, but currently non-functional** (3,433
    grouped cameras): a sixth platform, MapLarge (a commercial GIS data
    vendor) -- found by locating the real `table/query` request object
    TxDOT's own map-click handler builds
    (`Api/ProcessDirect?request={"action":"table/query","query":{...
    "table":"appgeo/cameraPoint"...}}`). Real coordinates for all 3,491
    cameras, but **no static snapshot image exists at all** here --
    `imageurl` in the raw data is a dead `https://localhost/...`
    placeholder, confirmed broken, not just untested. The only real
    media is `httpsurl`, a tokenized HLS live stream -- added real video
    playback for this (`hls.js`, native on Safari/iOS, per-panel
    popupopen/popupclose wiring to start/stop streams), the one state
    that needed it.
    - **Real, current problem found by testing, not assumed**: decoded
      a live token and found it already expired; sampled 200 different
      cameras and found *all 200* already expired regardless of how
      fast they were re-fetched. Traced into TxDOT's own app code and
      confirmed their production frontend uses this exact same
      `httpsurl` field directly as the video source, with no separate
      fresh-token endpoint anywhere in the bundle -- meaning
      drivetexas.org's own cameras are very likely showing the same
      broken video right now, not something specific to this
      integration. This looks like a stale token-refresh job on
      TxDOT/MapLarge's side, outside radar-lab's control to fix. Cache
      TTL shortened to 60s for TX specifically (the underlying query is
      fast, ~0.4s for all 3,491) so a real upstream refresh gets picked
      up quickly if/when it happens -- but as of this writing, clicking
      a Texas camera will most likely show a broken player, not a live
      stream. The architecture is correct and will start working the
      moment TxDOT's own data is fresh again.
  - **Still not resolved**: Alabama. Confirmed the real per-camera
    image pattern works (`api.algotraffic.com/v3/Cameras/{id}/
    snapshot.jpg` returns real JPEGs), but after substantial digging
    could not find the actual "list all cameras" endpoint -- the
    obvious collection route 404s even with browser-like headers, and a
    related `/Devices` route requires a logged-in session (real
    OAuth/PKCE flow found in the bundle). Genuinely stuck, not a quick
    fix; real attempts made.
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
