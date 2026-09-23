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
- **Radar render resolution was artificially low, fixed 2026-09-23**:
  canvas size was a flat 700px regardless of the actual data, which for
  a ~460km-radius circle works out to ~1315m/pixel -- 5.3x coarser than
  the data's real 250m gate spacing. That's genuine thrown-away detail
  (graininess from the renderer, not a real limit of the radar itself),
  which is what made it look grainy while zooming in even though the
  positioning/sizing was correct. Canvas size is now derived from the
  actual data dimensions (`2 * maxRange / gateStep`, ~3680px = true
  native resolution), capped at 1800px for compute time -- measured
  ~148ms per render at that size vs. ~24ms at the old 700px, still fine
  for a per-scan render. A true tile-pyramid renderer (re-rendering at
  the viewport's actual zoom/extent instead of one fixed-resolution
  image for the whole radar circle) would look sharper still at extreme
  zoom, but that's the "bigger, more complex build" the design doc
  already deferred (§2) -- this fix closes the *unnecessary* resolution
  loss without taking on that scope.
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
