#!/usr/bin/env python3
"""Radar Lab -- standalone NEXRAD radar/severe-weather viewer for the field laptop.

Design doc: ~/docs/projects/radar-lab/README.md (read that first -- this is
the V1 scaffold built against the decisions recorded there).

Plain stdlib http.server, no framework -- same pattern as every other tool
in ~/docker/ (parts-wishlist, harness-designer, etc.), except this one has
a real dependency stack (Py-ART, MetPy, numpy) because actual radar-data
decoding requires it. That's an explicitly-accepted cost, not a mistake.

Client-side rendering: this backend decodes and serves plain JSON (radial
arrays, not images) for single-site NEXRAD data. The browser (web/app.js)
does all the drawing. See design doc §2 for why.

One deliberate exception (2026-09-23): the national MRMS radar mosaic
(/api/mosaic.png) is rendered server-side to a PNG. That data is a
24.5-million-point national grid -- too large to ship as raw JSON for
client-side rendering the way single-site data works. "Server-side" here
still just means this same process, on whatever machine runs it (today
homehub, eventually the field laptop) -- not a separate remote service.
"""
import contextlib
import csv
import ctypes
import datetime as dt
import gc
import gzip
import html
import io
import json
import math
import os
import re
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pyart
from PIL import Image

try:
    # pygrib has no Windows wheels on PyPI (conda-forge only) -- the
    # national mosaic feature is the one thing this app can't offer on a
    # plain `pip install` Windows build yet. Everything else (single-site
    # radar, all camera sources, GPS, alerts) works fine without it, so
    # this is an optional import, not a hard dependency: the app runs
    # fine with the mosaic feature simply reporting itself unavailable
    # (see MOSAIC_AVAILABLE below) rather than failing to start at all.
    import pygrib
    MOSAIC_AVAILABLE = True
except ImportError:
    pygrib = None
    MOSAIC_AVAILABLE = False

try:
    # Unlike pygrib, netCDF4 has real Windows wheels (verified 2026-09-24,
    # not assumed) -- still a guarded import for the same defensive reason
    # as MOSAIC_AVAILABLE though: no single feature should be able to take
    # the whole app down just because one optional decode library is
    # missing or broken on some platform.
    import netCDF4
    LIGHTNING_AVAILABLE = True
except ImportError:
    netCDF4 = None
    LIGHTNING_AVAILABLE = False

# Packaging prep, 2026-09-25 -- a PyInstaller-frozen build (the real
# "two-click install" target) extracts its bundled read-only files
# (web/) to a temp directory (sys._MEIPASS) that's wiped after the
# process exits. Writable state -- .env (site config) and exports/
# (saved pins/shapes) -- must NOT live there, or every single run would
# silently lose its config and any saved work. Writable state goes next
# to the actual .exe instead, so it persists exactly the way it already
# does for a plain source checkout (where .env/exports/ both live at the
# project root). Not frozen (the normal source/dev case): both are the
# same directory, same as before this existed.
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent  # writable: .env, exports/
    ASSETS_BASE = Path(sys._MEIPASS)  # read-only: bundled web/ files
else:
    BASE = Path(__file__).resolve().parent.parent
    ASSETS_BASE = BASE
WEB_DIR = ASSETS_BASE / "web"
# User-drawn pins/shapes auto-save here (2026-09-24) -- deliberately NOT
# restored into the live map on reload (the ask was "session only"), but
# each session's work still lands on disk as its own timestamped file
# rather than being lost when the tab closes.
EXPORTS_DIR = BASE / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)


def load_env() -> dict:
    env = dict(os.environ)
    envfile = BASE / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    return env


ENV = load_env()
PORT = int(ENV.get("RADAR_LAB_PORT", "8297"))
SITE = ENV.get("RADAR_LAB_SITE", "KVWX").upper()
POLL_INTERVAL_SEC = int(ENV.get("RADAR_LAB_POLL_INTERVAL_SEC", "150"))
CACHE_MINUTES = int(ENV.get("RADAR_LAB_CACHE_MINUTES", "90"))

L2_BUCKET = "https://unidata-nexrad-level2.s3.amazonaws.com"
L3_BUCKET = "https://unidata-nexrad-level3.s3.amazonaws.com"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

# MRMS national composite (2026-09-23) -- verified live, no auth needed,
# real cadence ~2min (faster than a single NEXRAD site's own 6-7min
# volume scan). Poll a bit faster than that cadence for margin, same
# pattern as POLL_INTERVAL_SEC vs. the real NEXRAD update rate.
MRMS_BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com"
MRMS_PRODUCT_PREFIX = "CONUS/MergedReflectivityQCComposite_00.50"
MOSAIC_POLL_INTERVAL_SEC = int(ENV.get("RADAR_LAB_MOSAIC_POLL_INTERVAL_SEC", "60"))
# Output size, not native grid size (3500x7000 native) -- measured
# 2026-09-23: 1600x800 is ~370KB as a PNG, a reasonable size for a
# wide-area context layer over a real (possibly cellular/Funnel)
# connection. This is a national overview, not the primary interactive
# product, so native-resolution sharpness matters much less here than
# it did for the single-site tile pyramid.
MOSAIC_WIDTH, MOSAIC_HEIGHT = 1600, 800
# Same color stops as dbzColor() in web/app.js, kept in sync by hand --
# duplicated (not shared) because one is JS running in the browser and
# this one is Python running server-side for the mosaic PNG specifically.
DBZ_STOPS = [
    (5, (0x40, 0xe0, 0xd0)), (15, (0x00, 0x90, 0x00)), (25, (0x00, 0xe0, 0x00)),
    (30, (0xff, 0xff, 0x00)), (35, (0xff, 0xc0, 0x00)), (40, (0xff, 0x80, 0x00)),
    (45, (0xff, 0x00, 0x00)), (50, (0xc0, 0x00, 0x00)), (55, (0xff, 0x00, 0xff)),
    (65, (0xff, 0xff, 0xff)),
]

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None  # non-glibc platform (e.g. musl) -- release_decode_memory() just skips the trim, harmless


def release_decode_memory():
    """Called after each heavy decode (Py-ART volume parse, MRMS mosaic
    render) -- both build and discard large numpy arrays.
    gc.collect() alone frees the *Python* objects, but real incident
    2026-09-24: RSS kept climbing anyway, including under genuinely idle
    single-site conditions with no user interaction driving it -- glibc's
    malloc doesn't return freed heap pages to the OS by default, it keeps
    them mapped for its own future reuse (documented behavior, not a
    Python-level leak; decode_level2()'s own docstring already noted
    glibc's allocator doesn't release promptly). malloc_trim(0) explicitly
    asks it to release what it can back to the OS. Real fix for "gc says
    it's garbage but RSS doesn't reflect that", not a superstitious extra
    gc.collect()."""
    gc.collect()
    if _libc is not None:
        _libc.malloc_trim(0)


@contextlib.contextmanager
def temp_file_for(raw: bytes, suffix: str = ""):
    """Windows prep, 2026-09-25 -- every decode path in this app (Py-ART,
    MetPy, pygrib, netCDF4) writes raw bytes to a temp file, then hands
    the file's *path* to a separate library to open on its own. The
    obvious `with tempfile.NamedTemporaryFile() as f: f.write(...);
    <library>.open(f.name)` pattern works on Linux/Mac but is a real,
    documented Windows failure -- Python's own tempfile docs say
    directly that a NamedTemporaryFile's name isn't usable to reopen the
    file a second time while the first handle is still open, on Windows
    specifically (something else trying to open it hits a real
    PermissionError there). Fix: delete=False + close the handle before
    anything else touches the path, clean up in a finally block since
    delete=False means nothing does that automatically anymore. Found by
    code review while prepping for a real Windows test, not discovered
    by a failed run -- would have broken radar decode, lightning decode,
    and mosaic decode all at once there."""
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        f.write(raw)
        f.close()
        yield f.name
    finally:
        os.unlink(f.name)


# ---------------------------------------------------------------------------
# S3 helpers (plain HTTPS REST calls, no boto3 -- verified 2026-09-22 that
# these public buckets are listable/fetchable with zero auth this way)
# ---------------------------------------------------------------------------

def s3_list(bucket_url: str, prefix: str, max_keys: int = 50) -> list[str]:
    params = urllib.parse.urlencode(
        {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys)}
    )
    with urllib.request.urlopen(f"{bucket_url}/?{params}", timeout=20) as resp:
        root = ET.fromstring(resp.read())
    return [
        el.find("s3:Key", NS).text
        for el in root.findall("s3:Contents", NS)
    ]


def s3_fetch(bucket_url: str, key: str) -> bytes:
    with urllib.request.urlopen(f"{bucket_url}/{key}", timeout=30) as resp:
        return resp.read()


def latest_level2_key(site: str) -> str | None:
    today = dt.datetime.now(dt.timezone.utc)
    for days_back in (0, 1):  # roll back a day near UTC midnight
        d = today - dt.timedelta(days=days_back)
        prefix = f"{d:%Y/%m/%d}/{site}/"
        keys = [k for k in s3_list(L2_BUCKET, prefix, 1000) if k.endswith("V06")]
        if keys:
            return sorted(keys)[-1]
    return None


def latest_level3_key(site: str, product: str) -> str | None:
    short = site[1:] if site.startswith("K") else site
    today = dt.datetime.now(dt.timezone.utc)
    for days_back in (0, 1):
        d = today - dt.timedelta(days=days_back)
        prefix = f"{short}_{product}_{d:%Y_%m_%d}"
        keys = s3_list(L3_BUCKET, prefix, 1000)
        if keys:
            return sorted(keys)[-1]
    return None


# ---------------------------------------------------------------------------
# Level II decode (Py-ART) -- reflectivity + velocity, lowest sweep only
# for V1 (matches design doc §3: single active site, base products).
# ---------------------------------------------------------------------------

# /api/<name> -> (pyart field name, internal cache key, JSON response
# key). Dual-pol fields (zdr/cc/phidp) added 2026-09-23 (V2) -- they come
# from the exact same volume scan already being downloaded/decoded for
# reflectivity and velocity, so this cost almost nothing to add: no new
# data source, no new decode step, just three more keys pulled off the
# same `lowest` sweep object. Drives both decode_level2() and the
# do_GET() field endpoints from one place instead of repeating each
# product's boilerplate.
FIELD_MAP = {
    "reflectivity": ("reflectivity", "_reflectivity", "reflectivity_dbz"),
    "velocity": ("velocity", "_velocity", "velocity_ms"),
    "zdr": ("differential_reflectivity", "_zdr", "zdr_db"),
    "cc": ("cross_correlation_ratio", "_cc", "cc"),
    "phidp": ("differential_phase", "_phidp", "phidp_deg"),
}


def list_tilts(radar) -> list[float]:
    """Distinct elevation angles in a decoded volume, sorted ascending.
    Real example checked live 2026-09-23 (KVWX, 12 total sweeps): 9
    distinct angles (0.5, 0.9, 1.3, 1.8, 2.4, 3.1, 4.0, 5.1, 6.4) -- the
    first 3 are split-cut into 2 sweeps each (see decode_level2), which
    is why 12 sweeps only gives 9 selectable tilts, not 12."""
    fixed_angles = radar.fixed_angle["data"]
    return sorted({round(float(a), 1) for a in fixed_angles})


def decode_level2(raw: bytes, site: str, tilt_index: int = 0) -> dict:
    """Returns a dict with metadata plus raw numpy masked arrays under
    "_reflectivity"/"_velocity" (leading underscore = not directly
    JSON-serializable, see serialize_field() below).

    Measured 2026-09-22: keeping these as float32 numpy arrays instead of
    eagerly converting to nested Python lists roughly halves process RSS
    growth per retained scan (~90MB/scan as Python lists vs. ~45MB/scan
    as numpy arrays, measured with repeated decodes of the same file).
    The *logical* data is ~10x smaller as numpy (1.3M float32s + mask
    ~10-20MB vs. 1.3M individual Python float objects ~90MB+list
    overhead) but glibc's allocator doesn't return freed heap to the OS
    promptly, so real-world RSS improvement is smaller than the
    theoretical one -- don't assume a "smaller data structure" claim
    translates 1:1 into "smaller RSS" without measuring. Still a
    worthwhile, free win either way.

    tilt_index selects which of the volume's *distinct* elevation angles
    to decode (0 = lowest, matching the original V1 behavior before tilt
    selection existed) -- see list_tilts(). Added 2026-09-23.
    """
    with temp_file_for(raw, suffix="_V06") as path:
        radar = pyart.io.read_nexrad_archive(path)

    # Many VCPs "split cut" some elevations (almost always the lowest
    # few) into two sweeps at the *same* angle: a long-pulse
    # "surveillance" sweep (reflectivity/dual-pol -- better range, no
    # velocity) and a short-pulse "Doppler" sweep (velocity/spectrum
    # width only). Found 2026-09-23 by noticing /api/velocity came back
    # all-null: sweep index 0 alone doesn't carry every field. Fix: for
    # the target elevation angle, check every sweep sharing that angle
    # and use whichever one actually has non-empty data for each field,
    # instead of assuming the first matching sweep has everything.
    fixed_angles = radar.fixed_angle["data"]
    tilts = list_tilts(radar)
    if not (0 <= tilt_index < len(tilts)):
        raise ValueError(f"tilt_index {tilt_index} out of range (0-{len(tilts) - 1})")
    target_angle = tilts[tilt_index]
    candidate_idx = [i for i, a in enumerate(fixed_angles) if abs(round(float(a), 1) - target_angle) < 0.1]

    primary = radar.extract_sweeps([candidate_idx[0]])
    az = primary.azimuth["data"]
    rng = primary.range["data"]
    out = {
        "site": site,
        "lat": float(radar.latitude["data"][0]),
        "lon": float(radar.longitude["data"][0]),
        "azimuths": [round(float(a), 1) for a in az],
        "gate0_m": float(rng[0]),
        "gate_step_m": float(rng[1] - rng[0]),
        "ngates": primary.ngates,
        "tilt_index": tilt_index,
        "tilt_angle": target_angle,
        "tilts": tilts,
    }

    sweeps = {candidate_idx[0]: primary}
    for pyart_field, cache_key, _resp_key in FIELD_MAP.values():
        best_sweep, best_count = None, -1
        for idx in candidate_idx:
            sw = sweeps.setdefault(idx, radar.extract_sweeps([idx]))
            if pyart_field not in sw.fields:
                continue
            data = sw.fields[pyart_field]["data"]
            count = int((~data.mask).sum()) if hasattr(data, "mask") else data.size
            if count > best_count:
                best_sweep, best_count = sw, count
        if best_sweep is None or best_count <= 0:
            continue
        if best_sweep is not primary:
            # Split-cut sweeps have matched geometry in every VCP checked
            # so far (2026-09-23), but don't silently trust that forever.
            brng = best_sweep.range["data"]
            if best_sweep.ngates != primary.ngates or abs(float(brng[0]) - out["gate0_m"]) > 1:
                print(f"[radar-lab] WARNING: {pyart_field} sweep geometry differs from "
                      f"primary sweep -- values may be misaligned on the map")
        out[cache_key] = best_sweep.fields[pyart_field]["data"].astype("float32")
    return out


def serialize_field(scan: dict, internal_key: str) -> list[list[float | None]] | None:
    """Converts a cached numpy masked array to a JSON-serializable nested
    list on demand (request time), not at decode/cache time -- see
    decode_level2() docstring for why that split matters."""
    arr = scan.get(internal_key)
    if arr is None:
        return None
    filled = arr.filled(float("nan")) if hasattr(arr, "filled") else arr
    return [
        [None if v != v else round(float(v), 1) for v in row]  # v != v -> NaN
        for row in filled
    ]


# ---------------------------------------------------------------------------
# Level III decode (MetPy) -- NST (storm tracks) + NMD (mesocyclone
# detection). These are small vector/text products, not big sweeps.
# ---------------------------------------------------------------------------

def decode_level3(raw: bytes, site: str) -> dict:
    from metpy.io import Level3File

    with temp_file_for(raw) as path:
        f3 = Level3File(path)

    out = {
        "product": getattr(f3, "product_name", "?"),
        "site": getattr(f3, "siteID", site),
        "lat": getattr(f3, "lat", None),
        "lon": getattr(f3, "lon", None),
        "points": [],
    }
    # Storm tracks / mesocyclone detections show up as graphic "pages" of
    # symbol packets. Point-type packets carry an (x, y, text) tuple in
    # km relative to the radar; pull whatever's actually present rather
    # than assuming a fixed shape (empty on a quiet day is normal for NMD).
    for page in getattr(f3, "graph_pages", []):
        for packet in page:
            for item in packet:
                if isinstance(item, tuple) and len(item) >= 2:
                    x, y = item[0], item[1]
                    text = item[2] if len(item) > 2 else ""
                    out["points"].append({"x_km": x, "y_km": y, "text": str(text)})
    return out


# Level III radial products, built 2026-09-25 -- Storm Relative Velocity
# (N0S), Digital VIL (DVL), Enhanced Echo Tops (EET), Hydrometeor
# Classification (HHC), 1-Hour Precipitation (OHA). Found by listing every
# real product NOAA actually publishes for a real site (100 distinct
# codes) rather than guessing which exist -- most of that 100 turned out
# to be per-tilt repeats of moments already available from Level II
# (reflectivity/velocity/etc at each elevation, already covered by tilt
# selection), or currently-dormant alert products (no active severe
# weather right now). These five are the real, currently-decodable,
# genuinely new ones that fit this app's severe-weather scope.
#
# Confirmed live these use the *same* azimuth/range radial shape as the
# existing Level II products (not the x/y raster grid Composite
# Reflectivity turned out to use) -- so they reuse the existing
# client-side RadarTileLayer renderer as just more product choices,
# verified field-by-field against a real decoded file rather than assumed
# from the product family name alone.
LEVEL3_RADIAL_PRODUCTS = {
    # endpoint name -> (real NEXRAD product code, response JSON key)
    "storm_relative_velocity": ("N0S", "srv_ms"),
    "vil": ("DVL", "vil_kgm2"),
    "echo_tops": ("EET", "echo_tops_kft"),
    "hydrometeor_class": ("HHC", "hc_code"),
    "precip_1h": ("OHA", "precip_in"),
}


def decode_level3_radial(raw: bytes, site: str, resp_key: str) -> dict:
    from metpy.io import Level3File

    with temp_file_for(raw) as path:
        f3 = Level3File(path)
    item = f3.sym_block[0][0]
    data = f3.map_data(np.asarray(item["data"]))
    if isinstance(data, tuple):
        # Echo Tops (EET) specifically returns (values, is_below_radar_
        # coverage_flag) instead of a plain array -- found live
        # 2026-09-25, not documented anywhere obvious. The flag is real
        # (distinguishes a directly-measured top from one estimated
        # beyond the radar's vertical coverage) but not worth surfacing
        # as a separate field for a first pass -- just the height values.
        data = data[0]
    return {
        "site": site,
        "lat": f3.lat,
        "lon": f3.lon,
        "azimuths": [round(float(a), 1) for a in item["start_az"]],
        # gate_scale/first are real km values (confirmed live: gate_scale
        # * ngates matches the file's own max_range) -- *1000 for meters,
        # matching the convention every other radial product in this app
        # already uses.
        "gate0_m": float(item["first"]) * 1000,
        "gate_step_m": float(item["gate_scale"]) * 1000,
        "ngates": data.shape[1],
        resp_key: [[None if np.isnan(v) else round(float(v), 2) for v in row] for row in data],
    }


def decode_and_render_composite(raw: bytes, site: str) -> tuple[bytes, list]:
    """Site-level Composite Reflectivity (NCR) -- NOAA's own precomputed
    'maximum reflectivity across every tilt' field, not something
    recomputed here from raw Level II tilts (NOAA already does the real
    geometric work of combining tilts of different resolution correctly;
    redoing that would be a lot of work for a worse result). Real x/y
    raster grid (not radial -- confirmed live, unlike the products
    above), so this renders server-side to a PNG, same approach and same
    DBZ_STOPS color table as the national mosaic, for visual consistency
    between the two."""
    from metpy.io import Level3File

    with temp_file_for(raw) as path:
        f3 = Level3File(path)
    item = f3.sym_block[0][0]
    data = f3.map_data(np.asarray(item["data"]))
    if isinstance(data, tuple):
        # Echo Tops (EET) specifically returns (values, is_below_radar_
        # coverage_flag) instead of a plain array -- found live
        # 2026-09-25, not documented anywhere obvious. The flag is real
        # (distinguishes a directly-measured top from one estimated
        # beyond the radar's vertical coverage) but not worth surfacing
        # as a separate field for a first pass -- just the height values.
        data = data[0]

    h, w = data.shape
    rgba = np.zeros((h, w, 4), dtype="uint8")
    valid = ~np.isnan(data) & (data >= 5)
    for threshold, color in DBZ_STOPS:
        mask = valid & (data >= threshold)
        rgba[mask, 0], rgba[mask, 1], rgba[mask, 2], rgba[mask, 3] = *color, 200

    img = Image.fromarray(rgba, "RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)

    # Bounds: a square grid centered on the site, spanning max_range km in
    # each direction -- confirmed live (464x464 grid, max_range=230km ->
    # ~0.99km/pixel, consistent with a square +/-max_range extent), same
    # flat-local-plane approximation already used elsewhere in this app
    # (hazcam ring offsets, MADIS bounding boxes).
    south, west, north, east = bbox_from_radius(f3.lat, f3.lon, f3.max_range)
    bounds = [[south, west], [north, east]]
    return buf.getvalue(), bounds


# ---------------------------------------------------------------------------
# Background poller -- rolling in-memory cache, no permanent DB (matches
# design doc §2). Refreshes on POLL_INTERVAL_SEC, well under the real
# ~6-7 min NEXRAD update cadence.
# ---------------------------------------------------------------------------

class Cache:
    def __init__(self, site: str):
        self.lock = threading.Lock()
        self.site = site
        self.last_used = time.time()  # see get_cache() -- drives idle eviction
        self.scans: dict[str, dict] = {}  # ts -> decoded level2 (tilt 0 / lowest)
        self.level3: dict[str, dict] = {}  # product -> decoded level3
        self.status = {"last_poll": None, "last_error": None}
        # Tilt selection (added 2026-09-23): decoding all ~9 tilts for
        # every cached scan would multiply the already-measured
        # ~45MB/scan cache cost by 9x, blowing past the "under 1GB"
        # machine-sizing finding for no real benefit -- almost nobody
        # looks at 9 tilts of history, they look at multiple tilts of
        # *right now*. So: keep the latest scan's raw bytes (~9MB, cheap)
        # and decode non-default tilts on demand, only for that latest
        # scan. Older/playback scans stay tilt-0-only -- a real, honest
        # V1 scope limit, not an oversight.
        self.latest_raw_l2: tuple[str, bytes] | None = None  # (ts, raw)
        self.tilt_cache: dict[int, dict] = {}  # tilt_index -> decoded, latest scan only
        # Level III radial products (2026-09-25) -- Storm Relative
        # Velocity, Digital VIL, Echo Tops, Hydrometeor Classification,
        # 1-Hour Precipitation. Same azimuth/range radial shape as the
        # Level II products above (found live -- NOT the same x/y raster
        # shape Composite Reflectivity turned out to use), so these reuse
        # the existing client-side RadarTileLayer renderer as just more
        # product choices, instead of needing image rendering like the
        # composite/mosaic do.
        self.level3_radial: dict[str, dict] = {}  # product code -> decoded radial dict
        # Composite Reflectivity (NCR) -- the one Level III product here
        # that really is an x/y raster grid, not radial -- rendered
        # server-side to a PNG per site, same reasoning as the national
        # mosaic (too different a shape for the radial tile renderer).
        self.composite_png: bytes | None = None
        self.composite_bounds: list | None = None
        self.composite_updated: str | None = None

    def add_scan(self, ts: str, data: dict, raw: bytes):
        with self.lock:
            self.scans[ts] = data
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=CACHE_MINUTES)
            for old_ts in [t for t in self.scans if _parse_ts(t) < cutoff]:
                del self.scans[old_ts]
            self.latest_raw_l2 = (ts, raw)
            self.tilt_cache = {}  # new scan -> any on-demand tilt decodes are stale

    def get_tilt(self, ts: str, tilt_index: int) -> dict | None:
        with self.lock:
            if not self.latest_raw_l2 or self.latest_raw_l2[0] != ts:
                return None  # tilt selection only supported for the current latest scan
            return self.tilt_cache.get(tilt_index)

    def set_tilt(self, ts: str, tilt_index: int, data: dict):
        with self.lock:
            if self.latest_raw_l2 and self.latest_raw_l2[0] == ts:
                self.tilt_cache[tilt_index] = data

    def raw_for(self, ts: str) -> bytes | None:
        with self.lock:
            if self.latest_raw_l2 and self.latest_raw_l2[0] == ts:
                return self.latest_raw_l2[1]
            return None

    def latest_scan(self) -> tuple[str, dict] | tuple[None, None]:
        with self.lock:
            if not self.scans:
                return None, None
            ts = sorted(self.scans)[-1]
            return ts, self.scans[ts]

    def scan_list(self) -> list[str]:
        with self.lock:
            return sorted(self.scans)

    def get_scan(self, ts: str) -> dict | None:
        with self.lock:
            return self.scans.get(ts)

    def set_level3(self, product: str, data: dict):
        with self.lock:
            self.level3[product] = data

    def get_level3(self, product: str) -> dict | None:
        with self.lock:
            return self.level3.get(product)

    def set_level3_radial(self, product_code: str, data: dict):
        with self.lock:
            self.level3_radial[product_code] = data

    def get_level3_radial(self, product_code: str) -> dict | None:
        with self.lock:
            return self.level3_radial.get(product_code)

    def set_composite(self, png: bytes, bounds: list):
        with self.lock:
            self.composite_png = png
            self.composite_bounds = bounds
            self.composite_updated = dt.datetime.now(dt.timezone.utc).isoformat()

    def get_composite(self) -> tuple[bytes | None, list | None, str | None]:
        with self.lock:
            return self.composite_png, self.composite_bounds, self.composite_updated

class MosaicCache:
    """Separate from the per-site Cache class (2026-09-24 multi-site
    refactor) -- the national mosaic was already deliberately independent
    of whichever site(s) are active, so it gets its own single instance
    rather than living awkwardly inside one arbitrary site's cache."""
    def __init__(self):
        self.lock = threading.Lock()
        self.png: bytes | None = None
        self.bounds: list | None = None
        self.updated: str | None = None

    def set(self, png: bytes, bounds: list):
        with self.lock:
            self.png = png
            self.bounds = bounds
            self.updated = dt.datetime.now(dt.timezone.utc).isoformat()

    def get(self) -> tuple[bytes | None, list | None, str | None]:
        with self.lock:
            return self.png, self.bounds, self.updated


MOSAIC_CACHE = MosaicCache()


def _parse_ts(key: str) -> dt.datetime:
    # KVWX20260923_030241_V06 -> datetime
    stamp = key.split("/")[-1].split("_")[0][-8:] + key.split("_")[1]
    return dt.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)


# Multi-site cache registry (2026-09-24, replaces the original
# single-global-CACHE design) -- built so each grid panel can watch a
# genuinely different radar site at once, not just a different
# product/tilt of the same shared site. One Cache + one dedicated poll
# thread per site actually in use, created lazily on first request and
# evicted after sitting idle -- "full concurrent polling", the option
# picked over lighter on-demand-snapshot alternatives specifically so
# every open panel keeps getting real live updates regardless of how
# many different sites are open across a grid, not just the first one.
_caches: dict[str, Cache] = {}
_caches_lock = threading.Lock()
CACHE_IDLE_EVICT_SEC = 1800  # unused site's cache + poll thread torn down after this long
# Real incident, 2026-09-24: with no cap here, per-panel site selection
# (each grid panel can watch a different site) let real usage ramp up to
# ~10 concurrently-active sites -- each one's own Py-ART decode + rolling
# scan cache is heavy enough that this exhausted RAM, forced the box into
# swap, and got this process OOM-killed by systemd (degrading Tailscale/
# the portal/general reachability for the ~2 hours leading up to the
# kill, not just radar-lab itself -- one process thrashing on swap can
# drag down a whole small box). CACHE_IDLE_EVICT_SEC alone doesn't
# prevent this -- 30 minutes is way too slow to stop a burst of many new
# sites piling up in the meantime. This is the actual fix: a hard ceiling
# on how many *different* sites can be concurrently active, oldest
# (least-recently-used) one evicted immediately to make room for a new
# one past that ceiling, same idea as the idle eviction just enforced
# proactively instead of reactively.
MAX_CONCURRENT_SITES = int(ENV.get("RADAR_LAB_MAX_SITES", "4"))  # matches the max grid layout (4 panels) -- no reason to poll more sites than there are screens to show them on
DEFAULT_SITE = SITE  # seeds new panels and any request that omits ?site=


def get_cache(site: str) -> Cache:
    site = site.upper()
    with _caches_lock:
        cache = _caches.get(site)
        if cache is None:
            if len(_caches) >= MAX_CONCURRENT_SITES:
                lru_site = min(_caches, key=lambda s: _caches[s].last_used)
                del _caches[lru_site]
                print(f"[radar-lab] evicted {lru_site} (least-recently-used) to stay "
                      f"under MAX_CONCURRENT_SITES={MAX_CONCURRENT_SITES}")
            cache = Cache(site)
            _caches[site] = cache
            threading.Thread(target=poll_site_loop, args=(site, cache), daemon=True).start()
            print(f"[radar-lab] started polling site {site}")
        cache.last_used = time.time()
        return cache


def poll_site_loop(site: str, cache: Cache):
    """One of these runs per active site (see get_cache()) -- permanently
    bound to that one site for its whole life, unlike the original single
    shared poll_loop that had to detect and react to a site *changing*
    underneath it. Exits (and get_cache() will start a fresh one if the
    site's ever requested again) once evicted -- either idle for
    CACHE_IDLE_EVICT_SEC, or bumped by get_cache()'s LRU cap -- checked
    once per poll cycle (at most POLL_INTERVAL_SEC lag on noticing an LRU
    eviction, since that happens directly in get_cache(), not signaled
    here)."""
    last_l2_key = None
    # NHI (Hail Index) and NTV (Tornadic Vortex Signature) added
    # 2026-09-25 -- same point/graphic-page product family as NST/NMD
    # (decode_level3() already handles this shape generically), so this
    # is just two more product codes in the same loop, not new decode
    # logic. Both are real products that are legitimately empty/dormant
    # whenever there's no active severe weather to detect -- same as
    # NST/NMD already were on a quiet day, not a bug.
    last_l3_key = {"NST": None, "NMD": None, "NHI": None, "NTV": None}
    last_l3_radial_key = {code: None for code, _ in LEVEL3_RADIAL_PRODUCTS.values()}
    last_composite_key = None
    while True:
        with _caches_lock:
            if _caches.get(site) is not cache:
                return  # evicted (idle timeout or LRU cap) while this thread was asleep
            if time.time() - cache.last_used > CACHE_IDLE_EVICT_SEC:
                del _caches[site]
                print(f"[radar-lab] evicted idle site {site}")
                return

        try:
            key = latest_level2_key(site)
            if key and key != last_l2_key:
                raw = s3_fetch(L2_BUCKET, key)
                decoded = decode_level2(raw, site)
                cache.add_scan(key, decoded, raw)
                last_l2_key = key
                print(f"[radar-lab] new level2 scan ({site}): {key}")
                # Py-ART's own intermediate radar object (built from the
                # full volume scan, most of which is discarded once the
                # handful of fields FIELD_MAP actually wants are pulled
                # out) is large and short-lived -- see release_decode_memory().
                release_decode_memory()
        except Exception as e:  # noqa: BLE001 -- poller must never die
            cache.status["last_error"] = f"level2: {e}"
            print(f"[radar-lab] level2 poll error ({site}): {e}")

        for product in ("NST", "NMD", "NHI", "NTV"):
            try:
                key = latest_level3_key(site, product)
                if key and key != last_l3_key[product]:
                    raw = s3_fetch(L3_BUCKET, key)
                    decoded = decode_level3(raw, site)
                    cache.set_level3(product, decoded)
                    last_l3_key[product] = key
                    print(f"[radar-lab] new level3 {product} ({site}): {key}")
            except Exception as e:  # noqa: BLE001
                cache.status["last_error"] = f"{product}: {e}"
                print(f"[radar-lab] level3 {product} poll error ({site}): {e}")

        for endpoint_name, (product_code, resp_key) in LEVEL3_RADIAL_PRODUCTS.items():
            try:
                key = latest_level3_key(site, product_code)
                if key and key != last_l3_radial_key[product_code]:
                    raw = s3_fetch(L3_BUCKET, key)
                    decoded = decode_level3_radial(raw, site, resp_key)
                    cache.set_level3_radial(product_code, decoded)
                    last_l3_radial_key[product_code] = key
                    print(f"[radar-lab] new level3 {product_code} ({site}): {key}")
                    release_decode_memory()
            except Exception as e:  # noqa: BLE001
                cache.status["last_error"] = f"{product_code}: {e}"
                print(f"[radar-lab] level3 {product_code} poll error ({site}): {e}")

        try:
            key = latest_level3_key(site, "NCR")
            if key and key != last_composite_key:
                raw = s3_fetch(L3_BUCKET, key)
                png, bounds = decode_and_render_composite(raw, site)
                cache.set_composite(png, bounds)
                last_composite_key = key
                print(f"[radar-lab] new composite reflectivity ({site}): {key}")
                release_decode_memory()
        except Exception as e:  # noqa: BLE001
            cache.status["last_error"] = f"NCR: {e}"
            print(f"[radar-lab] composite poll error ({site}): {e}")

        cache.status["last_poll"] = dt.datetime.now(dt.timezone.utc).isoformat()
        time.sleep(POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# MRMS national radar mosaic (2026-09-23) -- see design doc §4 for the full
# story: this is the one place in the whole app that renders an image
# server-side instead of shipping raw data for the browser to draw. Not a
# different "remote service" -- this is the exact same process, wherever
# it happens to be running (homehub today, the field laptop eventually).
#
# Library note: cfgrib (the more common xarray-based GRIB2 reader)
# decoded real MRMS files correctly but crashed with a reproducible
# memory-corruption error ("double free" / "invalid pointer") on process
# cleanup every time it was tested, in a way traced to eccodes' own C
# bindings, not fixable from the Python side. pygrib -- a more direct,
# lower-level binding to the same underlying library -- decoded the same
# files cleanly across repeated runs with no crash, and was faster besides
# (~6.5s vs. cfgrib's ~12-17s). Used deliberately for that reason, not by
# default/convenience.
# ---------------------------------------------------------------------------

def latest_mrms_key() -> str | None:
    today = dt.datetime.now(dt.timezone.utc)
    for days_back in (0, 1):
        d = today - dt.timedelta(days=days_back)
        prefix = f"{MRMS_PRODUCT_PREFIX}/{d:%Y%m%d}/"
        keys = [k for k in s3_list(MRMS_BUCKET, prefix, 1000) if k.endswith(".grib2.gz")]
        if keys:
            return sorted(keys)[-1]
    return None


def decode_and_render_mosaic(raw_gz: bytes) -> tuple[bytes, list]:
    """Returns (png_bytes, bounds) where bounds is
    [[south, west], [north, east]] in real -180..180 longitude, ready for
    a Leaflet imageOverlay. Real measured cost 2026-09-23: ~6.5s decode +
    ~2.3s vectorized colorize + ~0.4s resize/encode, ~9-10s total."""
    raw = gzip.decompress(raw_gz)
    with temp_file_for(raw, suffix=".grib2") as path:
        grbs = pygrib.open(path)
        grb = grbs[1]
        data, lats, lons = grb.data()
        missing = grb.missingValue
        grbs.close()

    arr = np.asarray(data, dtype="float32")
    h, w = arr.shape
    rgba = np.zeros((h, w, 4), dtype="uint8")
    # missingValue is 9999 for this product (verified live, not assumed --
    # dBZ never legitimately gets close to that) -- treat separately from
    # "below 5 dBZ", which dbzColor() on the frontend also renders as
    # transparent (no significant echo) rather than a real "no data" gap.
    valid = (arr < missing - 1) & (arr >= 5)
    for threshold, color in DBZ_STOPS:
        mask = valid & (arr >= threshold)
        rgba[mask, 0], rgba[mask, 1], rgba[mask, 2], rgba[mask, 3] = *color, 200

    img = Image.fromarray(rgba, "RGBA").resize((MOSAIC_WIDTH, MOSAIC_HEIGHT), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)

    # Grid comes back north-at-top (row 0 = highest latitude) -- verified
    # live, matches image row order directly, no vertical flip needed.
    # Longitude comes back in 0-360 convention (e.g. 230..300) -- convert
    # to the -180..180 range Leaflet/everything else in this app expects.
    def to_signed_lon(lon):
        return lon - 360 if lon > 180 else lon

    bounds = [
        [float(lats.min()), to_signed_lon(float(lons.min()))],
        [float(lats.max()), to_signed_lon(float(lons.max()))],
    ]
    return buf.getvalue(), bounds


def mosaic_poll_loop():
    if not MOSAIC_AVAILABLE:
        print("[radar-lab] mosaic disabled -- pygrib not installed on this platform")
        return
    last_key = None
    while True:
        try:
            key = latest_mrms_key()
            if key and key != last_key:
                raw_gz = s3_fetch(MRMS_BUCKET, key)
                png, bounds = decode_and_render_mosaic(raw_gz)
                MOSAIC_CACHE.set(png, bounds)
                last_key = key
                print(f"[radar-lab] new mosaic: {key} ({len(png)} bytes)")
                release_decode_memory()  # 24.5M-point grid's intermediate arrays
        except Exception as e:  # noqa: BLE001 -- poller must never die
            print(f"[radar-lab] mosaic poll error: {e}")
        time.sleep(MOSAIC_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Lightning (GOES GLM), built 2026-09-24, both satellites added same day --
# another national product independent of whichever NEXRAD site is
# selected, same shape as the MRMS mosaic above. Real, free, near-real-time
# (~17-60s latency, verified live) on AWS S3, no auth needed -- same access
# pattern as everything else in this app. Flash-level data only (lat/lon/
# energy per flash) -- GLM files also carry event- and group-level data
# (individual sensor-pixel detections that get grouped into flashes) but
# that's finer detail than a map overlay needs; flash_lat/flash_lon/
# flash_energy are already top-level arrays in the file, no event/group
# reconstruction (what glmtools is for) required for this.
#
# Both GOES-East (GOES-19, bucket noaa-goes19) and GOES-West (GOES-18,
# bucket noaa-goes18) are identical instruments on different satellites --
# confirmed live 2026-09-24 that GOES-West's own bucket, file format, and
# ~20s cadence exactly mirror the East side, and that it already sees real
# flashes as far east as Arizona/New Mexico. East alone has degraded
# sensitivity toward the western edge of its field of view (not zero
# coverage, just worse) -- West fills that in from a much better angle.
# No deduplication between the two near where their coverage overlaps --
# each satellite reports its own independent flash_id and slightly
# different lat/lon (parallax from two different viewing angles on the
# same real storm), so a handful of boundary-region flashes may render as
# two nearby markers instead of one. Accepted as a minor cosmetic
# simplification, not fixed here -- matching flashes across satellites
# would need real geometric reasoning, not a simple id/coordinate match.
# ---------------------------------------------------------------------------

GLM_PRODUCT_PREFIX = "GLM-L2-LCFA"
GLM_SATELLITES = {
    "east": {"bucket": "https://noaa-goes19.s3.amazonaws.com", "label": "GOES-East"},
    "west": {"bucket": "https://noaa-goes18.s3.amazonaws.com", "label": "GOES-West"},
}
GLM_POLL_INTERVAL_SEC = int(ENV.get("RADAR_LAB_LIGHTNING_POLL_INTERVAL_SEC", "20"))
GLM_WINDOW_MINUTES = int(ENV.get("RADAR_LAB_LIGHTNING_WINDOW_MINUTES", "5"))
# Was 15 -- real usage 2026-09-24 found that far too generous: an active
# storm produces enough flashes (~1,700 accumulated in just 4 real
# minutes, measured live) that 15 minutes' worth turns into thousands of
# markers on screen, unreadable. Expiry/fade were both already working
# correctly (verified live -- nothing older than the window was ever
# retained); the window itself was just longer than useful.
# Generous CONUS + margin -- GLM's real coverage is the full disk
# (includes South America, the Atlantic, etc.), almost all irrelevant to
# a US radar tool. Filtering server-side keeps this shippable as raw
# JSON for the browser to draw, the same client-side-rendering approach
# used everywhere else in this app (the mosaic above is the one
# deliberate exception, and this isn't large enough to need to join it).
GLM_LAT_RANGE = (18.0, 55.0)
GLM_LON_RANGE = (-130.0, -60.0)


def latest_glm_key(bucket: str) -> str | None:
    now = dt.datetime.now(dt.timezone.utc)
    for hours_back in (0, 1):  # roll back an hour near the top of the hour, same pattern as latest_level2_key's day rollback
        t = now - dt.timedelta(hours=hours_back)
        prefix = f"{GLM_PRODUCT_PREFIX}/{t:%Y}/{t:%j}/{t:%H}/"
        keys = s3_list(bucket, prefix, 1000)
        if keys:
            return sorted(keys)[-1]
    return None


def decode_glm_flashes(raw: bytes, satellite_label: str) -> list[dict]:
    with temp_file_for(raw, suffix=".nc") as path:
        ds = netCDF4.Dataset(path)
        try:
            # netCDF4 auto-applies each variable's scale_factor/add_offset
            # (confirmed live 2026-09-24 against a real file) -- these come
            # back as real physical values already, not raw stored ints.
            lats = ds.variables["flash_lat"][:]
            lons = ds.variables["flash_lon"][:]
            energies = ds.variables["flash_energy"][:]
        finally:
            ds.close()

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    out = []
    for lat, lon, energy in zip(lats, lons, energies):
        lat, lon = float(lat), float(lon)
        if not (GLM_LAT_RANGE[0] <= lat <= GLM_LAT_RANGE[1] and GLM_LON_RANGE[0] <= lon <= GLM_LON_RANGE[1]):
            continue
        out.append({"lat": lat, "lon": lon, "energy_j": float(energy), "time": now_iso, "satellite": satellite_label})
    return out


class LightningCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.flashes: list[dict] = []

    def add(self, new_flashes: list[dict]):
        with self.lock:
            self.flashes.extend(new_flashes)
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=GLM_WINDOW_MINUTES)
            self.flashes = [f for f in self.flashes if dt.datetime.fromisoformat(f["time"]) > cutoff]

    def get(self) -> list[dict]:
        with self.lock:
            return list(self.flashes)


LIGHTNING_CACHE = LightningCache()


def lightning_poll_loop():
    if not LIGHTNING_AVAILABLE:
        print("[radar-lab] lightning disabled -- netCDF4 not installed on this platform")
        return
    last_keys = {sat_key: None for sat_key in GLM_SATELLITES}
    while True:
        # One thread, both satellites polled in sequence each cycle --
        # each fetch is small (~500KB) and sub-second, not worth a second
        # thread for. Independent last_key per satellite so one being
        # slow/erroring doesn't affect the other.
        for sat_key, sat in GLM_SATELLITES.items():
            try:
                key = latest_glm_key(sat["bucket"])
                if key and key != last_keys[sat_key]:
                    raw = s3_fetch(sat["bucket"], key)
                    flashes = decode_glm_flashes(raw, sat["label"])
                    LIGHTNING_CACHE.add(flashes)
                    last_keys[sat_key] = key
                    print(f"[radar-lab] new lightning data ({sat['label']}): {key} ({len(flashes)} flashes in range)")
                    release_decode_memory()
            except Exception as e:  # noqa: BLE001 -- poller must never die
                print(f"[radar-lab] lightning poll error ({sat['label']}): {e}")
        time.sleep(GLM_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Live snowplow truck tracking, built 2026-09-25 -- real-time position, not
# the periodic dashcam photos (separate feature entirely, see
# fetch_ia_snowplow_images-style code was never built since this live-
# position feed turned out to exist too). Found while researching the
# photo feed: its publisher's org name literally uses "AVL" (Automatic
# Vehicle Location) already, which turned out to be a real hint -- the
# same org (IowaDOT_SODA on ArcGIS) also publishes a genuinely rich live
# truck feed: position, heading, speed, road/air temperature, and even
# material spread rates (salt/brine) and individual plow blade states
# (front/wing/underbelly). Public ArcGIS FeatureServer, no auth, same
# access pattern as everything else here.
#
# Iowa only -- Nebraska and Minnesota (same org, same photo-feed pattern)
# were checked and only publish the photo feed, not live position.
# Indiana's own TrafficWise system (what prompted this) is not published
# as open data anywhere found -- it's the same kind of JS SPA VA/TX
# turned out to be, a separate not-yet-done investigation.
#
# Real limitation, not a bug: only trucks currently moving >3mph show up
# at all (an intentional filter on the source's side -- a parked/idle
# truck isn't "active"), and the feed is genuinely empty outside real
# winter operations -- confirmed live 2026-09-25 (September, no active
# plowing) that the query executes correctly and returns a valid empty
# result, not an error. Could not visually verify real truck data with
# an actual live truck this session for exactly that reason -- worth
# checking again once real snow operations are happening.
IA_SNOWPLOW_URL = "https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/AVL_Direct_View/FeatureServer/0/query"
SNOWPLOW_POLL_INTERVAL_SEC = int(ENV.get("RADAR_LAB_SNOWPLOW_POLL_INTERVAL_SEC", "60"))  # source itself updates every ~2min


def fetch_ia_snowplows() -> list[dict]:
    params = urllib.parse.urlencode({
        "where": "1=1",
        "outFields": "LABEL,VELOCITY,HEADING,ROADTEMP,AIRTEMP,ROUTE_NAME,LOGDT,ACTIVE_MATERIAL",
        "outSR": "4326",
        "f": "json",
    })
    req = urllib.request.Request(
        f"{IA_SNOWPLOW_URL}?{params}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry")
        if not geom:
            continue
        out.append({
            "id": f"IA-{attrs.get('LABEL')}",
            "label": attrs.get("LABEL"),
            "src": "IA",
            "lat": geom["y"],
            "lon": geom["x"],
            "heading_deg": attrs.get("HEADING"),
            "speed_mph": attrs.get("VELOCITY"),
            "road_temp_f": attrs.get("ROADTEMP"),
            "air_temp_f": attrs.get("AIRTEMP"),
            "route": attrs.get("ROUTE_NAME"),
            "material": attrs.get("ACTIVE_MATERIAL"),
            "updated": attrs.get("LOGDT"),
        })
    return out


class SnowplowCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.trucks: list[dict] = []
        self.updated: str | None = None

    def set(self, trucks: list[dict]):
        with self.lock:
            self.trucks = trucks
            self.updated = dt.datetime.now(dt.timezone.utc).isoformat()

    def get(self) -> tuple[list[dict], str | None]:
        with self.lock:
            return list(self.trucks), self.updated


SNOWPLOW_CACHE = SnowplowCache()


def snowplow_poll_loop():
    while True:
        try:
            trucks = fetch_ia_snowplows()
            SNOWPLOW_CACHE.set(trucks)
            if trucks:
                print(f"[radar-lab] snowplows: {len(trucks)} active trucks (IA)")
        except Exception as e:  # noqa: BLE001 -- poller must never die
            print(f"[radar-lab] snowplow poll error: {e}")
        time.sleep(SNOWPLOW_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# MADIS surface weather observations, built 2026-09-25 -- real-time
# station data (temp/dewpoint/humidity/wind/pressure), not radar. Public,
# no-auth "guest" access confirmed live -- took real trial and error to
# find: the actual query needs ~15 form parameters, and several
# reasonable-looking guesses (stasel="Y", rdr="metar") were flat wrong --
# only found the true defaults by reading the guest page's own HTML form
# source line by line (stasel is really a hidden field defaulting to "0",
# rdr is cleared to "" by the page's own submit handler, varsel=2 selects
# a real preset of 7 standard variables instead of picking them
# individually). Verified live against a real Midwest bounding box: 729
# distinct stations, 4,677 observations, spanning real, different
# networks (ASOS airport stations, RAWS fire-weather stations, MesoWest,
# citizen stations via APRSWXNET, marine/tide stations, and more) -- this
# is genuinely what MADIS is for, a real aggregator, not a single network.
#
# Scoped near the active radar site (asked for over a state picker) --
# same bounding-box-around-a-point shape as the near-site camera mode,
# just computed server-side and passed straight to MADIS's own bbox
# query mode (dfltrsel=1) instead of over-fetching then filtering.
MADIS_BASE_URL = "https://madis-data.ncep.noaa.gov/madisPublic1/cgi-bin/madisXmlPublicDir"
MADIS_CACHE_SEC = 300  # most MADIS station networks report every 5-60min -- no point refetching faster than that


def _k_to_f(k: float) -> float:
    return (k - 273.15) * 9 / 5 + 32


def _mps_to_mph(mps: float) -> float:
    return mps * 2.23694


def _pa_to_inhg(pa: float) -> float:
    return pa / 3386.39


# MADIS's "var" attribute -> (our field name, unit-conversion function).
# All 7 are what varsel=2 ("standard surface variables") actually returns
# -- confirmed against a real response, not guessed from the form's
# label text alone.
MADIS_VAR_MAP = {
    "V-T": ("temp_f", _k_to_f),
    "V-TD": ("dewpoint_f", _k_to_f),
    "V-RH": ("humidity_pct", lambda v: v),
    "V-DD": ("wind_dir_deg", lambda v: v),
    "V-FF": ("wind_speed_mph", _mps_to_mph),
    "V-FFGUST": ("wind_gust_mph", _mps_to_mph),
    "V-ALTSE": ("pressure_inhg", _pa_to_inhg),
}


def bbox_from_radius(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Returns (south, west, north, east) -- same flat-local-plane
    approximation already used elsewhere in this app (e.g. the hazcam
    ring offsets), fine at these distances."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * math.cos(math.radians(lat)))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def fetch_madis_obs(lat: float, lon: float, radius_km: float) -> list[dict]:
    south, west, north, east = bbox_from_radius(lat, lon, radius_km)
    params = {
        "time": "0", "minbck": "-59", "minfwd": "0", "recwin": "3", "timefilter": "0",
        "dfltrsel": "1", "latll": f"{south:.4f}", "lonll": f"{west:.4f}",
        "latur": f"{north:.4f}", "lonur": f"{east:.4f}",
        "stanam": "", "stasel": "0", "pvdrsel": "0", "varsel": "2",
        "qctype": "0", "qcsel": "1", "xml": "1", "csvmiss": "0", "rdr": "",
    }
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{MADIS_BASE_URL}?{qs}", headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        xml_text = resp.read().decode("utf-8", errors="replace")

    root = ET.fromstring(xml_text)
    stations: dict[str, dict] = {}
    for rec in root.findall("record"):
        mapping = MADIS_VAR_MAP.get(rec.get("var"))
        if not mapping:
            continue
        key, convert = mapping
        try:
            raw_value = float(rec.get("data_value"))
        except (TypeError, ValueError):
            continue
        if raw_value <= -99998:  # MADIS's own missing-value sentinel (-99999), confirmed against a real response
            continue
        staid = rec.get("shef_id")
        station = stations.setdefault(staid, {
            "id": staid,
            "lat": float(rec.get("lat")),
            "lon": float(rec.get("lon")),
            "provider": rec.get("provider"),
            "updated": rec.get("ObTime"),
        })
        station[key] = round(convert(raw_value), 1)
    return list(stations.values())


_madis_cache: dict[tuple, dict] = {}  # (rounded lat, rounded lon, radius_km) -> {"data": [...], "ts": float}


def get_madis_obs(lat: float, lon: float, radius_km: float) -> list[dict]:
    key = (round(lat, 2), round(lon, 2), radius_km)
    now = time.time()
    cached = _madis_cache.get(key)
    if cached and now - cached["ts"] < MADIS_CACHE_SEC:
        return cached["data"]
    data = fetch_madis_obs(lat, lon, radius_km)
    _madis_cache[key] = {"data": data, "ts": now}
    return data


# ---------------------------------------------------------------------------
# Camera feeds -- verified live 2026-09-22 (see design doc §3). Fetched
# on demand and cached briefly rather than polled continuously; these
# aren't the core scan cadence and don't need it.
# ---------------------------------------------------------------------------

TRAVELMIDWEST_URL = "https://travelmidwest.com/lmiga/cameraMap.json"
KYTC_URL = (
    "https://kygisserver.ky.gov/arcgis/rest/services/WGS84WM_Services/"
    "Ky_WebCams_WGS84WM/MapServer/0/query"
)
_camera_cache = {"ts": 0, "data": []}
CAMERA_CACHE_SEC = 120


def fetch_travelmidwest_cameras() -> list[dict]:
    req = urllib.request.Request(
        TRAVELMIDWEST_URL, data=b"{}", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read())
    out = []
    for feat in payload.get("features", []):
        props = feat.get("properties", {})
        lon, lat = feat.get("geometry", {}).get("coordinates", [None, None])
        urls = props.get("remUrls") or []
        if lat is None or lon is None or not urls:
            continue
        out.append({
            "id": props.get("id"),
            "name": props.get("locDesc"),
            "src": props.get("src"),
            "age": props.get("age"),
            "lat": lat,
            "lon": lon,
            "snapshot": urls[0],
        })
    return out


def fetch_kytc_cameras() -> list[dict]:
    params = urllib.parse.urlencode(
        {"where": "1=1", "outFields": "*", "f": "json", "returnGeometry": "false"}
    )
    with urllib.request.urlopen(f"{KYTC_URL}?{params}", timeout=20) as resp:
        payload = json.loads(resp.read())
    out = []
    for feat in payload.get("features", []):
        a = feat.get("attributes", {})
        if a.get("latitude") is None or not a.get("snapshot"):
            continue
        out.append({
            "id": a.get("name") or a.get("id"),
            "name": a.get("description") or a.get("name"),
            "src": "KYTC",
            "age": None,
            "lat": a["latitude"],
            "lon": a["longitude"],
            "snapshot": a["snapshot"],
        })
    return out


def group_cameras_by_location(cams: list[dict]) -> list[dict]:
    """Some sources report multiple distinct real cameras at the exact
    same coordinates -- found 2026-09-23 near Evansville, IN: InDOT
    publishes 3 separate cameras (cam-1/2/3) for the same I-64
    interchange, all at bit-identical lat/lon. As separate map markers
    these stack invisibly on top of each other -- only the topmost is
    ever clickable, which is exactly the "click it, nothing happens (or
    no image)" symptom: taps were landing on whichever camera happened
    to be buried underneath. Group by rounded location (~11m, well under
    real-world spacing between genuinely distinct camera sites) into one
    marker per physical location with a list of snapshot images instead
    of one marker per camera record.

    "snapshot" (static image, most states) and "stream" (HLS video, TX --
    see fetch_tx_cameras) are mutually exclusive per input camera, not
    per group -- a group could in principle mix both if two different
    real cameras happened to share a location across two different
    sources, though that's not expected to actually happen given each
    state's cameras all come from one source. Only non-empty lists make
    it into the output dict, so a plain-snapshot state's grouped cameras
    still have no "streams" key at all rather than an always-empty one."""
    groups: dict[tuple, dict] = {}
    for cam in cams:
        key = (round(cam["lat"], 4), round(cam["lon"], 4))
        group = groups.setdefault(key, {
            "id": cam["id"], "names": [], "src": cam["src"],
            "lat": cam["lat"], "lon": cam["lon"], "snapshots": [], "streams": [],
        })
        label = cam.get("name") or cam["id"]
        if label not in group["names"]:
            group["names"].append(label)
        if cam.get("snapshot"):
            group["snapshots"].append(cam["snapshot"])
        if cam.get("stream"):
            group["streams"].append(cam["stream"])
    out = []
    for g in groups.values():
        entry = {
            "id": g["id"], "name": " / ".join(g["names"]), "src": g["src"],
            "lat": g["lat"], "lon": g["lon"],
        }
        if g["snapshots"]:
            entry["snapshots"] = g["snapshots"]
        if g["streams"]:
            entry["streams"] = g["streams"]
        out.append(entry)
    return out


def parse_wkt_point(wkt: str) -> tuple[float, float] | None:
    # "POINT (-80.892882 26.17325)" -> (lon, lat)
    try:
        inner = wkt.split("(")[1].split(")")[0]
        lon_str, lat_str = inner.split()
        return float(lon_str), float(lat_str)
    except (IndexError, ValueError):
        return None


# States confirmed 2026-09-24 running the same shared 511-platform
# camera API (found by accident researching Florida, then confirmed
# identical -- just a different domain -- on four more states in
# minutes). Not every state's 511 system uses this platform -- South
# Carolina uses a different one (see STATE_ITERIS_GEOJSON_URLS below);
# Virginia, Texas, Alabama, Mississippi are all JS-rendered SPAs whose
# real API endpoint hasn't been found yet. This list is expected to grow.
STATE_DATATABLES_DOMAINS = {
    "FL": "fl511.com",
    "GA": "511ga.org",
    "LA": "511la.org",
    "PA": "511pa.com",
    "NC": "www.drivenc.gov",
    "NY": "511ny.org",
    "AZ": "www.az511.gov",
    # Real vendor name found 2026-09-25 while chasing Massachusetts:
    # "CARS Program" / Castle Rock ITS runs this same DataTables platform
    # across a real nationwide list of states (found via mass511.com's
    # own bundle referencing 511ny.org, cttravelsmart.org, az511.gov,
    # cotrip.org, 511ia.org, kandrive.org, nmroads.com, and more as
    # sibling deployments) -- FL/GA/LA/PA/NC/NY were each found
    # independently before this; AZ and CT are the first two confirmed
    # *because* of that shared-vendor list, not independently guessed.
    "CT": "ctroads.org",  # cttravelsmart.org (CT's own public-facing domain) redirects here -- this is the real API host, confirmed live
    # Real upgrade, not just an addition: 511wi.gov (this platform) has
    # 490 real cameras vs. the previous TravelMidwest-sourced 263 for the
    # same state -- switched WI here instead of adding it as a second,
    # smaller source alongside a better one.
    "WI": "511wi.gov",
    # Same platform again, found 2026-09-26 -- this generation is really
    # IBI Group's "ibi511" product (the same platform behind
    # prod-ut.ibi511.com / prod-nv.ibi511.com's separate *keyed*
    # developer API), not just "CARS Program" -- turns out the two
    # vendors' public-facing sites share this exact DataTables backend
    # shape. Found by testing the known `/List/GetData/Cameras` endpoint
    # directly against every remaining un-sourced state's likely domain
    # rather than digging through another bundle -- three hits with zero
    # new parsing code needed, `fetch_datatables_cameras` already handles
    # this shape as-is.
    "UT": "udottraffic.utah.gov",  # 2,081 real cameras
    "NV": "www.nvroads.com",  # 652 real cameras
    "ID": "511.idaho.gov",  # 457 real cameras
    "AK": "511.alaska.gov",  # 130 real cameras
}
# IN/IL all come from one shared multi-state feed (see
# fetch_travelmidwest_cameras) -- filtered by id prefix per state here.
# WI moved to STATE_DATATABLES_DOMAINS above (511wi.gov) 2026-09-25 -- a
# real upgrade (490 cameras vs. 263 from this source), not redundant.
TRAVELMIDWEST_STATES = {"IN", "IL"}
# Same DataTables platform as STATE_DATATABLES_DOMAINS above, but one
# domain covering multiple states at once (see fetch_datatables_cameras's
# state_code=None mode) -- confirmed live 2026-09-25, all 406 real
# records checked, no areaId besides these three present. Massachusetts/
# Rhode Island/Connecticut were checked against several likely domains
# and none matched this platform -- not part of this feed, not yet found
# on another one either.
NEWENGLAND_DOMAIN = "newengland511.org"
NEWENGLAND_STATES = {"NH", "ME", "VT"}


def fetch_datatables_cameras(domain: str, state_code: str | None, page_size: int = 100, max_pages: int = 60) -> list[dict]:
    """Generic fetcher for the shared 511-platform camera API. Server
    enforces a 100-per-page cap regardless of what's requested
    (confirmed live 2026-09-24 -- asking for length=10000 on Florida's
    ~4959 cameras still only returned 100), so a state the size of
    Florida needs ~50 paginated requests. Sequential that's ~30s+
    (~0.5-0.6s/page measured); parallelized across a thread pool like
    the two-source camera fetch already does, real time drops to a few
    seconds. max_pages is a hard safety cap (6000 cameras' worth), not
    expected to actually bind for any current source.

    state_code=None (2026-09-25, for newengland511.org): some domains on
    this platform cover *multiple* states from one shared feed instead
    of one state each -- confirmed live for New England (NH/ME/VT, all
    406 records checked, no other values present) via each row's own
    "areaId" field, not a fixed per-domain state the way every other
    DataTables source here works. None means "use each row's own areaId
    instead of a fixed state_code"."""
    base = f"https://{domain}"

    def fetch_page(start: int) -> dict:
        req = urllib.request.Request(
            f"{base}/List/GetData/Cameras",
            data=f"draw=1&start={start}&length={page_size}".encode(),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; radar-lab)",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        # One retry, not zero -- found live 2026-09-25 that 511ny.org
        # specifically 500s on a real but inconsistent fraction of the
        # ~19 concurrent page requests a state its size needs (different
        # pages failed on repeated runs, not the same ones -- genuinely
        # transient/rate-limiting, not a real permanent error). A single
        # retry recovered every failure seen in testing.
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read())
            except Exception:
                if attempt == 1:
                    raise
                time.sleep(0.5)

    first = fetch_page(0)
    total = first.get("recordsTotal", 0)
    pages_needed = min((total + page_size - 1) // page_size, max_pages)
    all_rows = list(first.get("data", []))

    if pages_needed > 1:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(fetch_page, p * page_size) for p in range(1, pages_needed)]
            for future in futures:
                try:
                    all_rows.extend(future.result().get("data", []))
                except Exception as e:  # noqa: BLE001
                    print(f"[radar-lab] {domain} page fetch error: {e}")

    out = []
    for row in all_rows:
        images = row.get("images") or []
        if not images or not images[0].get("imageUrl"):
            continue
        image_url = images[0]["imageUrl"]
        point = parse_wkt_point((row.get("latLng") or {}).get("geography", {}).get("wellKnownText", ""))
        if not point:
            continue
        lon, lat = point
        row_state = state_code or row.get("areaId") or "?"
        roadway, direction = row.get("roadway"), row.get("direction")
        name = f"{roadway} {direction}".strip() if roadway else (row.get("location") or f"Camera {row.get('id')}")
        out.append({
            "id": f"{row_state}-{row.get('id')}",
            "name": name,
            "src": row.get("source") or row_state,
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": image_url if image_url.startswith("http") else f"{base}{image_url}",
        })
    return out


# USGS Hawaiian Volcano Observatory "HazCams" (2026-09-24) -- real
# hazard-monitoring webcams for Kilauea and Mauna Loa. Genuinely
# different in kind from the DOT traffic cameras (a different agency,
# different purpose), but publicly free the same way, and the user
# explicitly asked about hazcams as a category. No JSON API found for
# this -- the real listing lives at volcanoes.usgs.gov/cams/index.php as
# plain HTML links (each "<a ... cam=K2cam>[K2cam] description</a>"),
# and each camera's live image is a simple, predictable
# /cams/{code}/images/M.jpg -- both confirmed live 2026-09-24, not
# assumed. Alaska (AVO) and Cascades (CVO) volcano observatories were
# checked and don't appear to have an equivalent accessible system --
# Hawaii only for now, a real scope limit, not an oversight.
HAZCAM_BASE = "https://volcanoes.usgs.gov"
HAZCAM_INDEX_URL = f"{HAZCAM_BASE}/cams/index.php"
# No real per-camera coordinates are published on the index page --
# grouped by volcano summit instead (same grouping mechanism already
# used for DOT cameras stacked at one physical interchange). Which code
# belongs to which volcano is read straight off each camera's own real
# name/description (checked 2026-09-24), not guessed.
MAUNA_LOA_HAZCAMS = {
    "MOcam", "SPcam", "MSTcam", "HLcam", "MLcam", "MTcam",
    "MKcam", "MK2cam", "M2cam", "M3cam", "MSPcam", "MDLcam",
}
KILAUEA_SUMMIT = (19.4069, -155.2834)
MAUNA_LOA_SUMMIT = (19.4721, -155.6059)


def fetch_hazcams() -> list[dict]:
    req = urllib.request.Request(HAZCAM_INDEX_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    out = []
    # All cameras at one volcano share that volcano's single summit
    # coordinate (no real per-camera coordinates are published) -- found
    # live 2026-09-24 that group_cameras_by_location() then collapses
    # ALL of them into one marker (19 different real cameras stacked in
    # one popup), which is real data but bad UX here: unlike the DOT
    # camera case this groups for (several genuinely co-located cameras
    # at one interchange), these are ~18-19 *different* real cameras
    # that just don't have individual coordinates. Spread each one onto
    # a small ring (~2km radius) around its volcano's summit so they
    # render as distinct, individually-clickable markers instead.
    counts = {KILAUEA_SUMMIT: 0, MAUNA_LOA_SUMMIT: 0}
    for m in re.finditer(r'<a[^>]*cam=([A-Za-z0-9_]+)[^>]*>(.*?)</a>', page, re.DOTALL):
        code = m.group(1)
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        name = re.sub(r"^\[[A-Za-z0-9_]+\]\s*", "", name)  # drop the leading "[K2cam] " the page prefixes each label with
        summit = MAUNA_LOA_SUMMIT if code in MAUNA_LOA_HAZCAMS else KILAUEA_SUMMIT
        i, counts[summit] = counts[summit], counts[summit] + 1
        angle = (i / 20) * 2 * math.pi  # 20 > real per-volcano count (~18-19), avoids angle collisions
        ring_deg = 0.018  # ~2km at this latitude
        lat = summit[0] + ring_deg * math.cos(angle)
        lon = summit[1] + ring_deg * math.sin(angle) / math.cos(math.radians(summit[0]))
        out.append({
            "id": f"HI-{code}",
            "name": name or code,
            "src": "USGS HVO",
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": f"{HAZCAM_BASE}/cams/{code}/images/M.jpg",
        })
    return out


# South Carolina runs a different 511 platform than the DataTables one
# above (Iteris ATIS) -- confirmed 2026-09-24 via its public GeoJSON feed,
# 790 real cameras with real image URLs. Tried the same {state}.cdn.iteris-
# atis.com pattern against 14 other state codes (VA/AL/MS/TX/TN/OK/AZ/NM/
# CO/UT/NV/OR/WA/CA) -- all failed, so this is SC-specific, not a second
# reusable multi-state shortcut like STATE_DATATABLES_DOMAINS was.
STATE_ITERIS_GEOJSON_URLS = {
    "SC": "https://sc.cdn.iteris-atis.com/geojson/icons/metadata/icons.cameras.geojson",
}
STATE_ITERIS_MULTICAM_GEOJSON_URLS = {
    "MT": "https://mt.cdn.iteris-atis.com/geojson/icons/metadata/icons.cameras.geojson",
    "SD": "https://sd.cdn.iteris-atis.com/geojson/icons/metadata/icons.cameras.geojson",
}


def fetch_iteris_cameras(url: str, state_code: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates")
        image_url = props.get("image_url")
        if not coords or not image_url:
            continue
        lon, lat = coords[0], coords[1]
        route, direction = props.get("route"), props.get("direction")
        name = props.get("description") or (f"{route} {direction}".strip() if route else f"Camera {props.get('id')}")
        out.append({
            "id": f"{state_code}-{props.get('id')}",
            "name": name,
            "src": route or state_code,
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": image_url,
        })
    return out


# Montana and South Dakota are on the same Iteris ATIS vendor as South
# Carolina (`{state}.cdn.iteris-atis.com/geojson/...`, found 2026-09-26
# by brute-forcing the same URL pattern with every state's 2-letter code
# -- only these two plus SC answered with real data) but a distinct,
# older-looking schema: each site feature holds a real `cameras` array
# (one entry per physical view -- e.g. north/south/road-surface at the
# same pole), not one flat `image_url` per feature like SC. Both real:
# 38 sites/38 cameras for MT, 40 sites/173 cameras for SD -- small,
# genuinely rural-interstate camera counts, not a partial/broken feed.
def fetch_iteris_multicam_cameras(url: str, state_code: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        coords = (feat.get("geometry") or {}).get("coordinates")
        if not coords:
            continue
        lon, lat = coords[0], coords[1]
        route = props.get("route")
        site_id = props.get("id") or feat.get("id")
        for cam in props.get("cameras", []):
            image_url = cam.get("image")
            if not image_url:
                continue
            out.append({
                "id": f"{state_code}-{site_id}-{cam.get('id')}",
                "name": cam.get("description") or cam.get("name") or f"Camera {cam.get('id')}",
                "src": route or state_code,
                "age": None,
                "lat": lat,
                "lon": lon,
                "snapshot": image_url,
            })
    return out


# Virginia runs a third distinct platform (its own "iLog" camera system,
# not DataTables or Iteris) -- found 2026-09-24 by chasing the real
# client-side API call through VDOT's Angular app rather than guessing:
# the SPA's own index.html only serves itself for every path (no
# same-origin REST API discoverable by probing common paths), and the
# main JS bundle references NODE_ENDPOINT.foo as a *relative* path
# (/services/511) with the actual getCamerasArray() call living in one
# of 46 separately-loaded lazy chunk files, not the main bundle -- had to
# download and grep all 46 to find `BASE_URL+"/array/cameras"`. Turned
# out to be same-origin after all (511.vdot.virginia.gov itself proxies
# it), just not at any guessable path. Verified live: 1,683 real
# cameras, real snapshot.vdotcameras.com thumbnail confirmed as an
# actual 200 image/png after its own redirect.
VA_CAMERAS_URL = "https://511.vdot.virginia.gov/services/511/map/array/cameras"


def fetch_va_cameras() -> list[dict]:
    req = urllib.request.Request(VA_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("data", []):
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates")
        image_url = props.get("image_url")
        if not coords or not image_url:
            continue
        lon, lat = coords[0], coords[1]
        name = props.get("description") or f"Camera {props.get('id')}"
        out.append({
            "id": f"VA-{props.get('id')}",
            "name": name,
            "src": props.get("jurisdiction") or "VA",
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": image_url,
        })
    return out


# Rhode Island runs a fifth distinct platform: the actual data lives in
# a real, public Esri ArcGIS FeatureServer layer, found 2026-09-26 by
# tracing the interactive camera map (`dot.ri.gov/travel/
# traffic_camera_map/`, redirected from the plain camera-gallery pages
# which are themselves just static per-region HTML with hardcoded
# <img> tags and no coordinates at all) into its own JS module, which
# constructs a `FeatureLayer` pointed at
# `risegis.ri.gov/hosting/rest/services/RIDOT/Rhodeways/MapServer/6` --
# a standard, documented ArcGIS REST query endpoint, not a custom API
# needing further reverse-engineering. Real, clean data: 143 features,
# each with WGS84 `Latitude`/`Longitude` fields already present on the
# attributes (no geometry reprojection needed even though the
# `geometry` block itself is in RI State Plane) and a direct
# `CCVEWebURL` snapshot field -- confirmed live as a real 200
# image/jpeg.
RI_CAMERAS_URL = (
    "https://risegis.ri.gov/hosting/rest/services/RIDOT/Rhodeways/MapServer/6/query"
    "?where=1%3D1&outFields=*&f=json&returnGeometry=false"
)


def fetch_ri_cameras() -> list[dict]:
    req = urllib.request.Request(RI_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        lat, lon = attrs.get("Latitude"), attrs.get("Longitude")
        image_url = attrs.get("CCVEWebURL")
        if not lat or not lon or not image_url:
            continue
        out.append({
            "id": f"RI-{attrs.get('EquipmentID')}",
            "name": attrs.get("Description") or f"Camera {attrs.get('EquipmentID')}",
            "src": "RIDOT",
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": image_url,
        })
    return out


# Maryland's CHART system exposes camera *locations* through a public
# ArcGIS FeatureServer too (`mdgeodata.md.gov/imap/rest/services/
# Transportation/MD_TrafficCameras`), but its own `url` field there is
# just an HTML player page, not a usable image/stream link -- a step
# short of actually usable, same shape as Mississippi's per-camera
# bubble pages. Found the real source instead by searching for CHART's
# own JSON feed directly: `chart.maryland.gov/DataFeeds/GetCamerasJson`,
# a plain JSON array (552 cameras) whose `publicVideoURL` is itself
# another HTML player page (`/Video/GetVideo/{id}`) -- one more layer
# in, that page's own inline script builds the real HLS URL from two
# fields already present in the JSON (`https://{cctvIp}/rtplive/{id}/
# playlist.m3u8`), so the wrapper page never actually needs fetching.
# Confirmed live and playable (`#EXTM3U`, real HLS manifest) -- Maryland
# gets real working video, unlike Texas's expired-token problem, using
# the same hls.js wiring already built for TX.
MD_CAMERAS_URL = "https://chart.maryland.gov/DataFeeds/GetCamerasJson"


def fetch_md_cameras() -> list[dict]:
    req = urllib.request.Request(MD_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read())
    out = []
    for row in rows:
        lat, lon = row.get("lat"), row.get("lon")
        cctv_ip, cam_id = row.get("cctvIp"), row.get("id")
        if not lat or not lon or not cctv_ip or not cam_id:
            continue
        out.append({
            "id": f"MD-{cam_id}",
            "name": row.get("description") or row.get("name") or f"Camera {cam_id}",
            "src": "MDOT CHART",
            "age": None,
            "lat": lat,
            "lon": lon,
            "stream": f"https://{cctv_ip}/rtplive/{cam_id}/playlist.m3u8",
        })
    return out


# Washington's real camera map (a Vue/Vite SPA, `wsdot.com/Travel/
# Real-time/Map/`) loads its ArcGIS FeatureLayer URL from a runtime
# config object never present as a literal string anywhere in its own
# JS bundle -- unlike Rhode Island, grepping the bundle for the actual
# endpoint came up empty. Found instead by going straight to WSDOT's
# own public ArcGIS Server (`data.wsdot.wa.gov/arcgis/rest/services`,
# same domain also used for exactly this per-state pattern) and
# browsing its real folder listing: a `TravelInformation` folder holds
# `TravelInfoCamerasWeather`, whose name alone confirmed it before even
# querying it. Real, clean data: 1,705 features via a single query with
# `outSR=4326` (skips a manual Web-Mercator-to-WGS84 reprojection
# entirely -- ArcGIS reprojects server-side when asked), confirmed live
# as a real 200 image/jpeg. Includes some real cross-border cameras
# (Oregon's own tripcheck.com feed appears for shared I-5 crossings) --
# not a data-quality issue, WSDOT's own feed does the same.
WA_CAMERAS_URL = (
    "https://data.wsdot.wa.gov/arcgis/rest/services/TravelInformation/TravelInfoCamerasWeather/FeatureServer/0/query"
    "?where=1%3D1&outFields=*&f=json&returnGeometry=true&outSR=4326"
)


def fetch_wa_cameras() -> list[dict]:
    req = urllib.request.Request(WA_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry") or {}
        lon, lat = geom.get("x"), geom.get("y")
        image_url = attrs.get("ImageURL")
        if lon is None or lat is None or not image_url:
            continue
        out.append({
            "id": f"WA-{attrs.get('OBJECTID')}",
            "name": attrs.get("CameraTitle") or f"Camera {attrs.get('OBJECTID')}",
            "src": "WSDOT",
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": image_url,
        })
    return out


# California is the one state this session where the *documented,
# official* public API turned out to be the easiest route rather than
# reverse-engineering an SPA -- Caltrans's CWWP2 ("Commercial Wholesale
# Web Portal") publishes real per-district CCTV status JSON
# (`cwwp2.dot.ca.gov/data/{d1..d12}/cctv/cctvStatus{D01..D12}.json`,
# zero-padded past D9 but not before it -- a real inconsistency in
# their own filenames, not a typo here), no API key needed, found from
# Caltrans's own public documentation page for it
# (`cwwp2.dot.ca.gov/documentation/cctv/cctv.htm`). Each camera record
# carries both a real static `currentImageURL` (still image) and a real
# `streamingVideoURL` (HLS m3u8) -- both included here rather than
# picking one, since group_cameras_by_location already treats
# snapshots/streams as independent per-camera lists. 12 separate
# district requests are needed (no single statewide endpoint exists);
# real combined count confirmed live: 3,591 cameras, by far the largest
# single state found this session.
CA_CWWP2_DISTRICTS = [(n, f"D{n:02d}") for n in range(1, 13)]


def fetch_ca_cameras() -> list[dict]:
    def fetch_district(dnum: int, dsuffix: str) -> list[dict]:
        url = f"https://cwwp2.dot.ca.gov/data/d{dnum}/cctv/cctvStatus{dsuffix}.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("data", [])

    out = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(fetch_district, n, suf) for n, suf in CA_CWWP2_DISTRICTS]
        for future in futures:
            try:
                records = future.result()
            except Exception as e:  # noqa: BLE001
                print(f"[radar-lab] CA CWWP2 district fetch error: {e}")
                continue
            for rec in records:
                cctv = rec.get("cctv", {})
                loc = cctv.get("location", {})
                lat, lon = loc.get("latitude"), loc.get("longitude")
                if not lat or not lon:
                    continue
                image_data = cctv.get("imageData", {})
                snapshot = (image_data.get("static") or {}).get("currentImageURL")
                stream = image_data.get("streamingVideoURL")
                if not snapshot and not stream:
                    continue
                cam = {
                    "id": f"CA-{cctv.get('index')}-{loc.get('district')}",
                    "name": loc.get("locationName") or f"Camera {cctv.get('index')}",
                    "src": "Caltrans",
                    "age": None,
                    "lat": float(lat),
                    "lon": float(lon),
                }
                if snapshot:
                    cam["snapshot"] = snapshot
                if stream:
                    cam["stream"] = stream
                out.append(cam)
    return out


# Missouri, found the same way as Maryland's ArcGIS layer and
# California's CWWP2 -- searching ArcGIS Online's own public content
# search directly for "MoDOT camera" turned up a real, current (last
# edited within the past few months) Feature Service
# (`MODOT_Traffic_Cameras`, owned by a MOSEMA -- Missouri State
# Emergency Management -- account) rather than needing to reverse
# engineer traveler.modot.org's own SPA. Its layer id is 1, not the
# usual 0 (the FeatureServer root needs checking per-service, not
# assumed) -- 871 real cameras, each with a real, working HLS stream
# (`URL2`; `URL1` is always null on every record, a real per-field
# quirk of this dataset, not a bug here) confirmed live across several
# different cameras (one, CAM01, was individually offline -- normal
# single-camera flakiness, not a systemic problem).
MO_CAMERAS_URL = (
    "https://services2.arcgis.com/jWXb6JPWtBjOCalT/arcgis/rest/services/MODOT_Traffic_Cameras/FeatureServer/1/query"
    "?where=1%3D1&outFields=*&f=json&returnGeometry=true&outSR=4326"
)


def fetch_mo_cameras() -> list[dict]:
    req = urllib.request.Request(MO_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry") or {}
        lon, lat = geom.get("x"), geom.get("y")
        stream_url = attrs.get("URL2")
        if lon is None or lat is None or not stream_url or stream_url == "<Null>":
            continue
        out.append({
            "id": f"MO-{attrs.get('CAM_ID__')}",
            "name": attrs.get("DESCRIPTION") or f"Camera {attrs.get('CAM_ID__')}",
            "src": "MoDOT",
            "age": None,
            "lat": lat,
            "lon": lon,
            "stream": stream_url,
        })
    return out


# Oregon, found the same ArcGIS-Online-content-search way as Maryland
# and Missouri -- a real, recently-updated Feature Service
# ("Oregon Traffic Cameras", owned by Oregon's own state emergency
# management account) pointing at `TripCheck_Cameras/FeatureServer`,
# ODOT's own TripCheck system (the same tripcheck.com already seen as
# the source for some of Washington's own shared border cameras this
# session -- confirmed reliable there too). 1,188 real cameras -- more
# than the ArcGIS service's own 1000-per-page default cap returns in one
# query, so this needs real pagination (`resultOffset`), unlike every
# other ArcGIS source found so far this session where one query was
# always enough.
OR_CAMERAS_URL = (
    "https://services.arcgis.com/uUvqNMGPm7axC2dD/arcgis/rest/services/TripCheck_Cameras/FeatureServer/0/query"
    "?where=1%3D1&outFields=*&f=json&returnGeometry=true&outSR=4326"
)


def fetch_or_cameras() -> list[dict]:
    out = []
    offset = 0
    while True:
        req = urllib.request.Request(
            f"{OR_CAMERAS_URL}&resultOffset={offset}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        feats = data.get("features", [])
        if not feats:
            break
        for feat in feats:
            attrs = feat.get("attributes", {})
            geom = feat.get("geometry") or {}
            lon, lat = geom.get("x"), geom.get("y")
            image_url = attrs.get("attributes_filename")
            if lon is None or lat is None or not image_url:
                continue
            out.append({
                "id": f"OR-{attrs.get('attributes_cameraId')}",
                "name": attrs.get("attributes_title") or f"Camera {attrs.get('attributes_cameraId')}",
                "src": "ODOT TripCheck",
                "age": None,
                "lat": lat,
                "lon": lon,
                "snapshot": image_url,
            })
        if not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
    return out


# Alabama, found via the same ArcGIS-Online-content-search approach as
# Maryland/Missouri/Oregon -- a real Feature Service
# (`ALDOT_TC_HFL_public`) tied to ALGO Traffic (the University of
# Alabama's Center for Advanced Public Safety, which actually runs
# ALDOT's camera system). Both a real static `ImageUrl`
# (`api.algotraffic.com`, confirmed live) and a `StreamUrl` per camera,
# but StreamUrl 404s on every camera spot-checked -- a real, systemic
# problem with that field specifically (not per-camera flakiness the
# way one dead MoDOT camera was), so only the working static image is
# used here. 556 real cameras.
AL_CAMERAS_URL = (
    "https://services5.arcgis.com/P2OFkRrXCz6u4SBf/arcgis/rest/services/ALDOT_TC_HFL_public/FeatureServer/0/query"
    "?where=1%3D1&outFields=*&f=json&returnGeometry=true&outSR=4326"
)


def fetch_al_cameras() -> list[dict]:
    req = urllib.request.Request(AL_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry") or {}
        lon, lat = geom.get("x"), geom.get("y")
        image_url = attrs.get("ImageUrl")
        if lon is None or lat is None or not image_url:
            continue
        name = attrs.get("Name") or f"Camera {attrs.get('Id')}"
        road = attrs.get("PrimaryRoad")
        out.append({
            "id": f"AL-{attrs.get('Id')}",
            "name": f"{road} & {attrs.get('CrossStreet')}" if road and attrs.get("CrossStreet") else name,
            "src": attrs.get("OrganizationId") or "ALDOT",
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": image_url,
        })
    return out


# North Dakota, found by tracing travel.dot.nd.gov's own Angular bundle
# for the real backend domain (`travelfiles.dot.nd.gov`) and then the
# exact URL-construction function it calls for each map layer
# (`Kt(...)`, which builds `https://{domain}/geojson/{id}/{id}.json` for
# undated layers) -- confirmed the "cameras" layer resolves to a real,
# public, no-auth GeoJSON. Same multi-camera-per-site shape as Montana/
# South Dakota's Iteris data (a `Cameras` array per site, not one
# image per site) but a different vendor/schema (`LinkPath` per
# camera, not `image`) -- needs its own fetcher rather than reusing
# theirs. 189 sites, 809 real cameras total.
ND_CAMERAS_URL = "https://travelfiles.dot.nd.gov/geojson/cameras/cameras.json"


def fetch_nd_cameras() -> list[dict]:
    req = urllib.request.Request(ND_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        coords = (feat.get("geometry") or {}).get("coordinates")
        if not coords:
            continue
        lon, lat = coords[0], coords[1]
        site_id = props.get("ObjectID") or feat.get("id")
        for i, cam in enumerate(props.get("Cameras", [])):
            image_url = cam.get("LinkPath") or cam.get("FullPath")
            if not image_url:
                continue
            out.append({
                "id": f"ND-{site_id}-{i}",
                "name": cam.get("Description") or f"Camera {site_id}-{i}",
                "src": "NDDOT",
                "age": None,
                "lat": lat,
                "lon": lon,
                "snapshot": image_url,
            })
    return out


# Michigan's MiDrive is the oldest-feeling platform found this
# session: a plain JSON array at `mdotjboss.state.mi.us/MiDrive/
# camera/list`, but every field that should be structured data
# (coordinates, image URL) is instead a pre-rendered HTML fragment
# meant to be dropped directly into the page -- lat/lon has to be
# regexed out of an embedded "Go to" link's own query string
# (`county`), and the image src out of an embedded `<img>` tag
# (`image`), rather than either being its own real field. Confirmed
# real and current live: 804 cameras, image URL 301-redirects to
# `micamerasimages.net` (a real, working image once followed).
MI_CAMERAS_URL = "https://mdotjboss.state.mi.us/MiDrive/camera/list"
_MI_LATLON_RE = re.compile(r"lat=([\-\d.]+)&lon=([\-\d.]+)&zoom=\d+&id=(\d+)")
_MI_IMG_SRC_RE = re.compile(r'src="([^"]+)"')


def fetch_mi_cameras() -> list[dict]:
    req = urllib.request.Request(MI_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read())
    out = []
    for row in rows:
        latlon_match = _MI_LATLON_RE.search(row.get("county") or "")
        img_match = _MI_IMG_SRC_RE.search(row.get("image") or "")
        if not latlon_match or not img_match:
            continue
        lat, lon, cam_id = latlon_match.groups()
        name = f"{row.get('route', '')}{row.get('location', '')}".strip() or f"Camera {cam_id}"
        out.append({
            "id": f"MI-{cam_id}",
            "name": name,
            "src": "MDOT",
            "age": None,
            "lat": float(lat),
            "lon": float(lon),
            "snapshot": img_match.group(1),
        })
    return out


# Tennessee's SmartWay is a modern Angular SPA that -- like
# Washington's -- loads its real API config at runtime rather than
# baking the URL into its JS bundle, but found the config file itself
# this time (`grep`ping the shared vendor chunk for the literal
# "config.prod.json" reference the loader function uses) rather than
# going around it via a public GIS server. That reference is
# `` `${baseUrl}config\${suffix}` `` in the original TypeScript -- a
# literal backslash, not a template-literal typo here, which still
# resolves fine served over HTTP (`config/config.prod.json` works
# identically). That config contains both the real API base URL
# (`tdot.tn.gov/opendata/api/public/`) and a real, plainly-embedded
# client-side API key -- meant to be public since it ships in every
# page load, same as a Google Maps browser key. 668 real cameras, each
# with both a real static thumbnail and a real HLS stream (both
# confirmed live) -- ~129 of the 668 are marked `active:"false"` and
# skipped, real inactive/removed cameras still present in the feed
# rather than a data quality problem.
TN_CAMERAS_URL = "https://www.tdot.tn.gov/opendata/api/public/RoadwayCameras?apiKey=8d3b7a82635d476795c09b2c41facc60"


def fetch_tn_cameras() -> list[dict]:
    req = urllib.request.Request(TN_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read())
    out = []
    for row in rows:
        if row.get("active") != "true":
            continue
        lat, lon = row.get("lat"), row.get("lng")
        if lat is None or lon is None:
            continue
        cam = {
            "id": f"TN-{row.get('id')}",
            "name": row.get("title") or row.get("description") or f"Camera {row.get('id')}",
            "src": row.get("jurisdiction") or "TDOT",
            "age": None,
            "lat": lat,
            "lon": lon,
        }
        if row.get("thumbnailUrl"):
            cam["snapshot"] = row["thumbnailUrl"]
        if row.get("httpsVideoUrl"):
            cam["stream"] = row["httpsVideoUrl"]
        if cam.get("snapshot") or cam.get("stream"):
            out.append(cam)
    return out


# Delaware's real camera list lives behind a genuinely old-school
# jQuery plugin (`camerafy`, used to feed a JW Player instance) rather
# than a modern SPA -- found by fetching that plugin's own minified JS
# and reading its `$.getCameraFeed` function directly, which hardcodes
# both the real endpoint and a fixed query-string id:
# `tmc.deldot.gov/json/videocamera.json?id=4yte`. Real, clean data: 360
# cameras, almost all enabled/active, each with a working HLS URL
# (`m3u8s`) confirmed live.
DE_CAMERAS_URL = "https://tmc.deldot.gov/json/videocamera.json?id=4yte"


def fetch_de_cameras() -> list[dict]:
    req = urllib.request.Request(DE_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for row in data.get("videoCameras", []):
        if not row.get("enabled"):
            continue
        lat, lon = row.get("lat"), row.get("lon")
        stream_url = (row.get("urls") or {}).get("m3u8s")
        if lat is None or lon is None or not stream_url:
            continue
        out.append({
            "id": f"DE-{row.get('id')}",
            "name": row.get("title") or f"Camera {row.get('id')}",
            "src": row.get("county") or "DelDOT",
            "age": None,
            "lat": lat,
            "lon": lon,
            "stream": stream_url,
        })
    return out


# Mississippi is a fourth distinct platform again -- an older-style
# ASP.NET WebForms site (not a modern SPA), found via its classic
# ScriptManager "PageMethods" AJAX pattern: the page's own inline script
# lists LoadCameraData as a callable server method, invoked by POSTing an
# empty JSON body to <page>/LoadCameraData and reading the ASP.NET AJAX
# convention's {"d": [...]} wrapper. That one request gives real
# coordinates for all 456 cameras (verified live), but NOT an image URL
# -- each entry only carries an iframe src pointing at a per-camera
# "bubble" page (mapbubbles/camerasite.aspx?site=N), and the real
# snapshot URL only appears inside *that* page's own <img> tag. No bulk
# endpoint for it was found, so getting real images means fetching all
# 456 bubble pages individually -- parallelized (same
# ThreadPoolExecutor(max_workers=10) pattern as the DataTables states'
# pagination) rather than one request each sequentially, since this is
# real, unavoidable API shape here, not a design choice.
MS_CAMERA_LIST_URL = "https://www.mdottraffic.com/Default.aspx/LoadCameraData"
MS_CAMERA_BUBBLE_URL = "https://www.mdottraffic.com/mapbubbles/camerasite.aspx?site={site}"
_MS_SITE_ID_RE = re.compile(r"site=(\d+)")
_MS_IMG_SRC_RE = re.compile(r"id=\"camimg\"[^>]*src='([^']+)'")


def _fetch_ms_camera_image(site_id: str) -> str | None:
    req = urllib.request.Request(
        MS_CAMERA_BUBBLE_URL.format(site=site_id),
        headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html_body = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    m = _MS_IMG_SRC_RE.search(html_body)
    return m.group(1) if m else None


def fetch_ms_cameras() -> list[dict]:
    req = urllib.request.Request(
        MS_CAMERA_LIST_URL,
        data=b"{}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        markers = json.loads(resp.read())["d"]

    site_ids = {}  # markerid -> site id, extracted up front so the parallel fetch below only does image lookups
    for m in markers:
        match = _MS_SITE_ID_RE.search(m.get("framehtml") or "")
        if match:
            site_ids[m["markerid"]] = match.group(1)

    images = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_ms_camera_image, sid): markerid for markerid, sid in site_ids.items()}
        for future in futures:
            markerid = futures[future]
            try:
                images[markerid] = future.result()
            except Exception as e:
                print(f"[radar-lab] MS camera bubble fetch error ({markerid}): {e}")

    out = []
    for m in markers:
        image_url = images.get(m["markerid"])
        if not image_url:
            continue
        out.append({
            "id": f"MS-{m['markerid']}",
            "name": m.get("tooltip") or m["markerid"],
            "src": "MS",
            "age": None,
            "lat": m["lat"],
            "lon": m["lon"],
            "snapshot": image_url,
        })
    return out


# Texas (drivetexas.org) runs on MapLarge, a commercial GIS/mapping data
# platform -- a fifth distinct platform. Found 2026-09-25 the same way as
# VA: the SPA's main bundle had zero literal https:// API strings, but
# did reference VITE_ML_HOST/VITE_CAMERA_TABLE build-time constants
# (`dtx-e-cdn.maplarge.com`, table `cameraPoint`) used to build calls to
# MapLarge's own `Api/ProcessDirect?request=<json>` query endpoint --
# `{"action":"table/query","query":{"sqlselect":[...],"table":
# "appgeo/cameraPoint","take":N,"where":[]}}`, found by locating the
# real `table/query` request object the app's own map-click handler
# builds, not guessed. Verified live: 3,490 real cameras, real
# coordinates (WKT, same format as parse_wkt_point already handles).
#
# Real, unavoidable difference from every other state here: TX has no
# static snapshot image at all -- "imageurl" in the raw data is a
# literally-broken `https://localhost/...` placeholder (confirmed dead,
# not just untested). The only real, working media is "httpsurl", a
# live HLS stream (skyvdn.com, the same CDN family SC/VA's snapshot
# images happen to also sit on) -- and it's tokenized with a ~5 minute
# expiry (decoded a real token's iat/exp: exactly 300s), which is why
# this is the one state whose camera dicts carry "stream" instead of
# "snapshot" (see group_cameras_by_location) and why the frontend needs
# real HLS.js video playback instead of an <img> tag for these. A token
# minted when the state's camera list is fetched will usually still be
# fresh when shown (STATE_CAMERA_CACHE_SEC is also 5 minutes), but a
# stream opened right at the end of that cache window can find its own
# token already expired -- a real, accepted rough edge given TX's actual
# API shape, not something worth re-architecting the shared cache TTL
# over for one state.
TX_MAPLARGE_HOST = "https://dtx-e-cdn.maplarge.com"
TX_CAMERA_TABLE = "appgeo/cameraPoint"


def fetch_tx_cameras() -> list[dict]:
    request_obj = {
        "action": "table/query",
        "query": {
            "sqlselect": ["description", "name", "httpsurl", "XY"],
            "start": 0,
            "table": TX_CAMERA_TABLE,
            "take": 5000,
            "where": [],
        },
    }
    qs = urllib.parse.urlencode({"request": json.dumps(request_obj)})
    req = urllib.request.Request(
        f"{TX_MAPLARGE_HOST}/Api/ProcessDirect?{qs}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    # Column-oriented response (one parallel array per field), not one
    # object per row -- confirmed live, not assumed.
    columns = payload.get("data", {}).get("data", {})
    names = columns.get("name", [])
    descriptions = columns.get("description", [])
    streams = columns.get("httpsurl", [])
    xys = columns.get("XY", [])

    out = []
    for i, name in enumerate(names):
        stream = streams[i] if i < len(streams) else None
        point = parse_wkt_point(xys[i]) if i < len(xys) else None
        if not stream or not point:
            continue
        lon, lat = point
        description = descriptions[i] if i < len(descriptions) else None
        out.append({
            "id": f"TX-{name}",
            "name": description or name,
            "src": "TX",
            "age": None,
            "lat": lat,
            "lon": lon,
            "stream": stream,
        })
    return out


# "CARS Program" / Castle Rock ITS, newer generation -- found 2026-09-26
# chasing Massachusetts's real backend (its own frontend domain isn't
# the API host, same pattern as Connecticut). This generation is a real
# microservices API (`{domain}/cameras_v1/api/cameras`, a plain JSON
# array), not the classic DataTables platform FL/GA/LA/PA/NC/NY/AZ/CT/WI
# run -- found by locating the real API-map object
# (`{accounts,amber,cameras,cms,...}`) Massachusetts's own JS bundle
# builds, then testing the literal base URL it resolved to. That base
# URL looked like a staging/test host (`iatg-carsprogram-org.stage.
# carstest.org`) -- real data came back from it, but a cleaner
# production-looking domain (`iatg.carsprogram.org`, matching the
# pattern Massachusetts's own domain used) turned out to have more
# recent data and is what's actually used here. Same domain-naming
# pattern (`{2-letter state}tg.carsprogram.org`) confirmed working for
# Kansas by direct guess. New Mexico checked (`nmroads.com`) and isn't
# on this platform at all -- an unrelated, much older WebGL-based site.
CARS_TG_DOMAINS = {
    "IA": "iatg.carsprogram.org",
    "MA": "matg.carsprogram.org",
    "KS": "kstg.carsprogram.org",
    "MN": "mntg.carsprogram.org",
    "NE": "netg.carsprogram.org",
}


def fetch_cars_tg_cameras(domain: str, state_code: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://{domain}/cameras_v1/api/cameras",
        headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read())
    out = []
    for row in rows:
        loc = row.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        views = row.get("views") or []
        image_url = next((v.get("videoPreviewUrl") for v in views if v.get("videoPreviewUrl")), None)
        if not image_url:
            # Nebraska's rows carry no videoPreviewUrl at all -- their
            # views are plain still images already (type STILL_IMAGE),
            # confirmed a real, directly-usable snapshot -- found
            # 2026-09-26 checking why NE returned almost nothing under
            # the videoPreviewUrl-only logic that worked for IA/MA/KS/MN.
            image_url = next((v.get("url") for v in views if v.get("type") == "STILL_IMAGE" and v.get("url")), None)
        if lat is None or lon is None or not image_url:
            continue
        out.append({
            "id": f"{state_code}-{row.get('id')}",
            "name": row.get("name") or f"Camera {row.get('id')}",
            "src": (row.get("cameraOwner") or {}).get("name") or state_code,
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": image_url,
        })
    return out


# Colorado is on the same "CARS Program" vendor but a different, newer
# sub-generation again -- a real GeoJSON FeatureCollection at
# `{api}/map-features`, not the plain JSON array the IA/MA/KS domains
# above return. Found via Colorado's own real runtime config
# (`511.cotrip.org/configs/main.json`), which lists the actual camera
# API host directly (`api-511x-co.carsprogram.org`) -- the bare API root
# only returns a generic `{"healthy":true}` health-check response
# regardless of path/method tried; `/map-features` (found in Colorado's
# own JS bundle, where the frontend actually builds this exact request)
# is the real data endpoint.
CO_CAMERAS_URL = "https://api-511x-co.carsprogram.org/cameras/map-features"


def fetch_co_cameras() -> list[dict]:
    req = urllib.request.Request(CO_CAMERAS_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; radar-lab)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        coords = (feat.get("geometry") or {}).get("coordinates")
        image_url = next((v.get("videoPreviewUrl") for v in props.get("views") or [] if v.get("videoPreviewUrl")), None)
        if not coords or not image_url:
            continue
        lon, lat = coords[0], coords[1]
        out.append({
            "id": f"CO-{props.get('id')}",
            "name": props.get("name") or f"Camera {props.get('id')}",
            "src": props.get("cameraOwner") or "CO",
            "age": None,
            "lat": lat,
            "lon": lon,
            "snapshot": image_url,
        })
    return out


# Separate from _camera_cache (the near-radar-site 4-state one) --
# whole-state pulls are much more expensive (many paginated requests for
# a big state), so cached longer (5min not 2min) and only ever fetched
# for a state someone actually picked, never preloaded speculatively.
_state_camera_cache: dict[str, dict] = {}
STATE_CAMERA_CACHE_SEC = 300
SUPPORTED_CAMERA_STATES = sorted(
    set(STATE_DATATABLES_DOMAINS) | set(STATE_ITERIS_GEOJSON_URLS) | set(STATE_ITERIS_MULTICAM_GEOJSON_URLS)
    | TRAVELMIDWEST_STATES | NEWENGLAND_STATES | set(CARS_TG_DOMAINS)
    | {"KY", "HI", "VA", "MS", "TX", "CO", "RI", "WA", "MD", "CA", "MO", "OR", "AL", "ND", "MI", "TN", "DE"}
)


# TX's camera "URLs" are really JWT tokens with a hard ~5min server-side
# expiry baked in by MapLarge (decoded a real one: exp-iat = exactly
# 300s) -- confirmed live 2026-09-25 that the default 5-minute cache TTL
# means a stream clicked late in its cache window is already dead (real
# 401, not theoretical). The underlying query itself is fast (~0.4s for
# all 3,491 cameras, measured live) so refetching far more often than
# the other states is cheap here -- a real per-state override, not a
# blanket change to STATE_CAMERA_CACHE_SEC for everyone.
STATE_CAMERA_CACHE_OVERRIDES = {"TX": 60}


def get_cameras_for_state(state_code: str) -> list[dict] | None:
    """None means no source registered for this state (yet) -- a real,
    honest "not supported" distinct from "supported, zero cameras found
    right now", which the frontend shows differently."""
    state_code = state_code.upper()
    now = time.time()
    ttl = STATE_CAMERA_CACHE_OVERRIDES.get(state_code, STATE_CAMERA_CACHE_SEC)
    cached = _state_camera_cache.get(state_code)
    if cached and now - cached["ts"] < ttl:
        return cached["data"]

    if state_code in STATE_DATATABLES_DOMAINS:
        cams = fetch_datatables_cameras(STATE_DATATABLES_DOMAINS[state_code], state_code)
    elif state_code in STATE_ITERIS_GEOJSON_URLS:
        cams = fetch_iteris_cameras(STATE_ITERIS_GEOJSON_URLS[state_code], state_code)
    elif state_code in STATE_ITERIS_MULTICAM_GEOJSON_URLS:
        cams = fetch_iteris_multicam_cameras(STATE_ITERIS_MULTICAM_GEOJSON_URLS[state_code], state_code)
    elif state_code in TRAVELMIDWEST_STATES:
        cams = [c for c in fetch_travelmidwest_cameras() if str(c.get("id", "")).startswith(f"{state_code}-")]
    elif state_code in NEWENGLAND_STATES:
        cams = [c for c in fetch_datatables_cameras(NEWENGLAND_DOMAIN, None) if str(c.get("id", "")).startswith(f"{state_code}-")]
    elif state_code == "KY":
        cams = fetch_kytc_cameras()
    elif state_code == "HI":
        cams = fetch_hazcams()  # not DOT cameras -- USGS volcano hazard webcams
    elif state_code == "VA":
        cams = fetch_va_cameras()
    elif state_code == "MS":
        cams = fetch_ms_cameras()
    elif state_code == "TX":
        cams = fetch_tx_cameras()
    elif state_code in CARS_TG_DOMAINS:
        cams = fetch_cars_tg_cameras(CARS_TG_DOMAINS[state_code], state_code)
    elif state_code == "CO":
        cams = fetch_co_cameras()
    elif state_code == "RI":
        cams = fetch_ri_cameras()
    elif state_code == "WA":
        cams = fetch_wa_cameras()
    elif state_code == "MD":
        cams = fetch_md_cameras()
    elif state_code == "CA":
        cams = fetch_ca_cameras()
    elif state_code == "MO":
        cams = fetch_mo_cameras()
    elif state_code == "OR":
        cams = fetch_or_cameras()
    elif state_code == "AL":
        cams = fetch_al_cameras()
    elif state_code == "ND":
        cams = fetch_nd_cameras()
    elif state_code == "MI":
        cams = fetch_mi_cameras()
    elif state_code == "TN":
        cams = fetch_tn_cameras()
    elif state_code == "DE":
        cams = fetch_de_cameras()
    else:
        return None

    grouped = group_cameras_by_location(cams)
    _state_camera_cache[state_code] = {"data": grouped, "ts": now}
    return grouped


def get_cameras() -> list[dict]:
    now = time.time()
    if now - _camera_cache["ts"] > CAMERA_CACHE_SEC:
        # Measured 2026-09-23: these two fetches took ~1.1s combined run
        # sequentially (0.69s travelmidwest + 0.45s KYTC) -- neither
        # depends on the other, so running them in parallel threads
        # roughly halves that to whichever one is slower. Small thing,
        # but this cache-miss path was the single slowest thing in the
        # whole app (measured ~3s end-to-end including JSON parsing of
        # travelmidwest's ~1MB response), and it happens on a cache miss
        # roughly every 2 minutes.
        cams = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(f): f for f in (fetch_travelmidwest_cameras, fetch_kytc_cameras)}
            for future in futures:
                try:
                    cams.extend(future.result())
                except Exception as e:  # noqa: BLE001
                    print(f"[radar-lab] camera fetch error ({futures[future].__name__}): {e}")
        _camera_cache["data"] = group_cameras_by_location(cams)
        _camera_cache["ts"] = now
    return _camera_cache["data"]


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# NWS alerts -- straight pass-through proxy, same source already proven in
# the Weather Hub. No new research needed, just reuse.
# ---------------------------------------------------------------------------

def fetch_alerts(lat: float, lon: float) -> dict:
    url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"
    req = urllib.request.Request(url, headers={"User-Agent": "radar-lab (homehub)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Radar site list -- live authoritative source (api.weather.gov/radar/
# stations), same NWS domain already used for alerts. 208 stations total,
# but that list also includes TDWR airport radars and wind profilers
# (different networks, not WSR-88D, no data in the NEXRAD Level II
# bucket this app reads from) -- e.g. AWPA2/HWPA2 (Alaska profilers),
# TBWI/TCMH (TDWR). Filtered to stationType == "WSR-88D" 2026-09-23 --
# found by cross-checking the NWS list against which site folders
# actually exist in the live S3 bucket, then discovering the API already
# self-reports the field that explains the mismatch, so a bucket
# cross-check isn't needed at request time. Two known WSR-88D sites
# (KCRP, RODN) still won't have data despite passing this filter --
# KCRP was down for real maintenance when checked, RODN (Okinawa) is a
# legitimate WSR-88D that simply isn't published to this particular
# public bucket. Not worth chasing further for a personal tool; picking
# one just shows "no scan yet" same as any other real network hiccup.
# Cached an hour since site locations/types are effectively static.
# ---------------------------------------------------------------------------

_sites_cache = {"ts": 0, "data": []}
SITES_CACHE_SEC = 3600


def get_sites() -> list[dict]:
    now = time.time()
    if now - _sites_cache["ts"] > SITES_CACHE_SEC:
        req = urllib.request.Request(
            "https://api.weather.gov/radar/stations",
            headers={"User-Agent": "radar-lab (homehub)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
        sites = []
        for feat in payload.get("features", []):
            props = feat.get("properties", {})
            lon, lat = feat.get("geometry", {}).get("coordinates", [None, None])
            if props.get("id") and lat is not None and props.get("stationType") == "WSR-88D":
                sites.append({"id": props["id"], "name": props.get("name", ""), "lat": lat, "lon": lon})
        sites.sort(key=lambda s: s["id"])
        _sites_cache["data"] = sites
        _sites_cache["ts"] = now
    return _sites_cache["data"]


# ---------------------------------------------------------------------------
# GPS -- gpsd client. Soft-fails to null position when no gpsd/receiver is
# present (true on this dev box; real on the field laptop once wired up).
# ---------------------------------------------------------------------------

def get_gps_position() -> dict | None:
    try:
        from gps3 import gps3
    except ImportError:
        return None
    try:
        socket = gps3.GPSDSocket()
        stream = gps3.DataStream()
        socket.connect(timeout=1)
        socket.watch()
        for raw in socket:
            if not raw:
                continue
            stream.unpack(raw)
            lat, lon = stream.TPV.get("lat"), stream.TPV.get("lon")
            if lat not in ("n/a", None) and lon not in ("n/a", None):
                return {
                    "lat": lat,
                    "lon": lon,
                    "speed_ms": stream.TPV.get("speed"),
                    "heading_deg": stream.TPV.get("track"),
                    "time": stream.TPV.get("time"),
                }
            break
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# User-drawn pins/shapes -- auto-saved export (2026-09-24). CSV is pins
# only (a flat lat/lon/name table is the whole point of CSV; shapes don't
# fit that shape at all). KML carries both pins and shapes and is the
# real target -- it's Google My Maps' native import format, so "export
# to Google Maps" means this file, not the CSV.
# ---------------------------------------------------------------------------

def _safe_session_id(session_id: str) -> str:
    # Used directly in a filename -- only allow what a timestamp-derived
    # id should ever contain, so a crafted session_id can't be used for
    # path traversal or to write outside EXPORTS_DIR.
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", session_id)[:64]
    return cleaned or "session"


def write_pins_csv(path: Path, pins: list[dict]):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "notes", "lat", "lon"])
        for pin in pins:
            writer.writerow([pin.get("name", ""), pin.get("notes", ""), pin.get("lat"), pin.get("lon")])


def _circle_ring(lat: float, lon: float, radius_m: float, points: int = 36) -> list[tuple[float, float]]:
    # KML has no native circle primitive -- approximate with a polygon
    # ring, the standard way to represent one in KML. Same flat-local-
    # plane approximation used elsewhere in this app for short distances.
    ring = []
    for i in range(points + 1):  # +1 to close the ring back on itself
        angle = (i / points) * 2 * math.pi
        dlat = (radius_m * math.cos(angle)) / 111_320
        dlon = (radius_m * math.sin(angle)) / (111_320 * math.cos(math.radians(lat)))
        ring.append((lat + dlat, lon + dlon))
    return ring


def write_marks_kml(path: Path, pins: list[dict], shapes: list[dict]):
    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = ET.SubElement(kml, "Document")

    for pin in pins:
        pm = ET.SubElement(doc, "Placemark")
        ET.SubElement(pm, "name").text = pin.get("name") or "Pin"
        if pin.get("notes"):
            ET.SubElement(pm, "description").text = pin["notes"]
        point = ET.SubElement(pm, "Point")
        ET.SubElement(point, "coordinates").text = f"{pin['lon']},{pin['lat']},0"

    for shape in shapes:
        pm = ET.SubElement(doc, "Placemark")
        ET.SubElement(pm, "name").text = shape.get("name") or shape.get("type", "Shape").title()
        if shape.get("notes"):
            ET.SubElement(pm, "description").text = shape["notes"]
        shape_type = shape.get("type")
        if shape_type == "circle":
            center = shape.get("center") or [0, 0]
            ring = _circle_ring(center[0], center[1], float(shape.get("radius_m", 0)))
            poly = ET.SubElement(pm, "Polygon")
            outer = ET.SubElement(poly, "outerBoundaryIs")
            ring_el = ET.SubElement(outer, "LinearRing")
            ET.SubElement(ring_el, "coordinates").text = " ".join(f"{lon},{lat},0" for lat, lon in ring)
        elif shape_type == "polyline":
            line = ET.SubElement(pm, "LineString")
            coords = shape.get("coordinates") or []
            ET.SubElement(line, "coordinates").text = " ".join(f"{lon},{lat},0" for lat, lon in coords)
        else:  # polygon / rectangle -- both are just closed rings in KML
            poly = ET.SubElement(pm, "Polygon")
            outer = ET.SubElement(poly, "outerBoundaryIs")
            ring_el = ET.SubElement(outer, "LinearRing")
            coords = shape.get("coordinates") or []
            if coords and coords[0] != coords[-1]:
                coords = coords + [coords[0]]  # KML rings must close back on their own first point
            ET.SubElement(ring_el, "coordinates").text = " ".join(f"{lon},{lat},0" for lat, lon in coords)

    ET.ElementTree(kml).write(path, xml_declaration=True, encoding="UTF-8")


_latest_export = {"csv": None, "kml": None}  # Path | None, for the manual "download latest" buttons


def write_marks_export(session_id: str, pins: list[dict], shapes: list[dict]):
    safe_id = _safe_session_id(session_id)
    csv_path = EXPORTS_DIR / f"marks_{safe_id}.csv"
    kml_path = EXPORTS_DIR / f"marks_{safe_id}.kml"
    write_pins_csv(csv_path, pins)
    write_marks_kml(kml_path, pins, shapes)
    _latest_export["csv"] = csv_path
    _latest_export["kml"] = kml_path


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[radar-lab] {self.address_string()} - {fmt % args}")

    def _json(self, obj, code=200):
        # Radar payloads (esp. /api/reflectivity) run 5-8 MB of raw JSON --
        # fine over LAN/tailnet (instant), but nearly unusable over a real
        # internet connection (looked like a permanently "stuck loading"
        # page to a Funnel user on cellular, root-caused 2026-09-23). JSON
        # arrays of numbers compress very well, so gzip when the client
        # supports it (every real browser does).
        body = json.dumps(obj).encode()
        headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: Path):
        if not path.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html", ".js": "application/javascript",
            ".css": "text/css",
        }.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, body: bytes, content_type: str, code=200):
        # PNG is already compressed (internal zlib deflate) -- unlike
        # _json's payloads, gzipping on top wouldn't meaningfully shrink
        # it further, so this doesn't reuse _json's gzip logic.
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global DEFAULT_SITE
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/api/status":
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            ts, _ = cache.latest_scan()
            self._json({
                "site": cache.site, "latest_scan": ts,
                "scan_count": len(cache.scan_list()), **cache.status,
            })

        elif path == "/api/scans":
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            self._json({"scans": cache.scan_list()})

        elif path == "/api/sites":
            try:
                self._json({"sites": get_sites()})
            except urllib.error.URLError as e:
                self._json({"error": str(e)}, 502)

        elif path == "/api/site":
            # GET-for-a-mutation (?set=) instead of POST is a deliberate
            # shortcut, not an oversight -- matches this scaffold's
            # existing "everything's a query param" style, and adding
            # POST-body parsing machinery for one endpoint isn't worth
            # it for a local single-user tool.
            #
            # This no longer means "the one active site" (2026-09-24 --
            # every panel in a grid can watch a different site now, each
            # with its own always-on poll thread via get_cache()). It
            # just sets DEFAULT_SITE, the fallback used by any request
            # that doesn't pass its own ?site= (the very first page load,
            # before app.js has assigned any panel a specific site) --
            # and, as a side effect, starts that site's cache warming
            # immediately rather than waiting for the first real request.
            new_site = qs.get("set", [None])[0]
            if new_site:
                new_site = new_site.upper()
                try:
                    valid_ids = {s["id"] for s in get_sites()}
                except urllib.error.URLError as e:
                    self._json({"error": f"couldn't validate site list: {e}"}, 502)
                    return
                if new_site not in valid_ids:
                    self._json({"error": f"unknown site {new_site}"}, 400)
                    return
                DEFAULT_SITE = new_site
                get_cache(new_site)
                print(f"[radar-lab] default site switched to {new_site}")
            self._json({"site": DEFAULT_SITE})

        elif path == "/api/level3/nst":
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            self._json(cache.get_level3("NST") or {"points": []})

        elif path == "/api/level3/nmd":
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            self._json(cache.get_level3("NMD") or {"points": []})

        elif path == "/api/level3/nhi":
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            self._json(cache.get_level3("NHI") or {"points": []})

        elif path == "/api/level3/ntv":
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            self._json(cache.get_level3("NTV") or {"points": []})

        elif path.startswith("/api/") and path[5:] in LEVEL3_RADIAL_PRODUCTS:
            endpoint_name = path[5:]
            product_code, resp_key = LEVEL3_RADIAL_PRODUCTS[endpoint_name]
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            data = cache.get_level3_radial(product_code)
            if data is None:
                self._json({"error": f"no {endpoint_name} data cached yet"}, 503)
                return
            self._json(data)

        elif path == "/api/composite":
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            _png, bounds, updated = cache.get_composite()
            if bounds is None:
                self._json({"error": "no composite reflectivity rendered yet"}, 503)
                return
            self._json({"bounds": bounds, "updated": updated})

        elif path == "/api/composite.png":
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            png, _bounds, _updated = cache.get_composite()
            if png is None:
                self.send_error(503)
                return
            self._binary(png, "image/png")

        elif path.startswith("/api/") and path[5:] in FIELD_MAP:
            name = path[5:]
            _pyart_field, cache_key, resp_key = FIELD_MAP[name]
            cache = get_cache(qs.get("site", [DEFAULT_SITE])[0])
            ts = qs.get("ts", [None])[0]
            tilt_param = qs.get("tilt", [None])[0]

            if tilt_param is None:
                data = cache.get_scan(ts) if ts else cache.latest_scan()[1]
            else:
                # Tilt selection (2026-09-23): only supported for the
                # current live scan, decoded on demand -- see Cache
                # class docstring for why (avoids a ~9x memory blowup
                # from pre-decoding every tilt of every cached scan).
                try:
                    tilt_index = int(tilt_param)
                except ValueError:
                    self._json({"error": "tilt must be an integer"}, 400)
                    return
                target_ts = ts or cache.latest_scan()[0]
                raw = cache.raw_for(target_ts) if target_ts else None
                if raw is None:
                    self._json({
                        "error": "tilt selection is only available for the current live scan"
                        if target_ts else "no scan cached yet"
                    }, 400 if target_ts else 503)
                    return
                data = cache.get_tilt(target_ts, tilt_index)
                if data is None:
                    try:
                        data = decode_level2(raw, cache.site, tilt_index)
                    except ValueError as e:
                        self._json({"error": str(e)}, 400)
                        return
                    cache.set_tilt(target_ts, tilt_index, data)
                    release_decode_memory()  # this is the on-demand re-decode path (see decode_level2 docstring) -- the one most exposed to real multi-panel tilt-switching load

            if data is None or cache_key not in data:
                self._json({"error": f"no {name} data cached yet"}, 503)
                return
            self._json({
                **{k: v for k, v in data.items() if not k.startswith("_")},
                resp_key: serialize_field(data, cache_key),
            })

        elif path == "/api/mosaic":
            if not MOSAIC_AVAILABLE:
                self._json({"error": "national mosaic not available on this platform (pygrib not installed)"}, 501)
                return
            _png, bounds, updated = MOSAIC_CACHE.get()
            if bounds is None:
                self._json({"error": "no mosaic rendered yet"}, 503)
                return
            self._json({"bounds": bounds, "updated": updated})

        elif path == "/api/mosaic.png":
            png, _bounds, _updated = MOSAIC_CACHE.get()
            if png is None:
                self.send_error(503)
                return
            self._binary(png, "image/png")

        elif path == "/api/cameras":
            state_param = qs.get("state", [None])[0]
            if state_param:
                # State-scoped mode (2026-09-24): the user explicitly
                # picks a state/territory instead of always searching
                # near whichever radar site is active -- deliberately
                # only ever fetches the one state asked for, never
                # preloads others, so picking a state is the only thing
                # that costs a real fetch.
                cams = get_cameras_for_state(state_param)
                if cams is None:
                    self._json({"error": f"no camera source registered for {state_param.upper()} yet",
                                "supported": SUPPORTED_CAMERA_STATES}, 404)
                    return
                self._json({"cameras": cams, "state": state_param.upper()})
                return
            try:
                lat = float(qs["lat"][0])
                lon = float(qs["lon"][0])
                radius_km = float(qs.get("radius_km", ["50"])[0])
            except (KeyError, ValueError):
                self._json({"error": "lat & lon, or state, query param required"}, 400)
                return
            cams = get_cameras()
            nearby = [
                {**c, "distance_km": round(haversine_km(lat, lon, c["lat"], c["lon"]), 1)}
                for c in cams
            ]
            nearby = [c for c in nearby if c["distance_km"] <= radius_km]
            nearby.sort(key=lambda c: c["distance_km"])
            self._json({"cameras": nearby})

        elif path == "/api/camera-states":
            self._json({"supported": SUPPORTED_CAMERA_STATES})

        elif path == "/api/alerts":
            try:
                lat = float(qs["lat"][0])
                lon = float(qs["lon"][0])
            except (KeyError, ValueError):
                self._json({"error": "lat & lon query params required"}, 400)
                return
            try:
                self._json(fetch_alerts(lat, lon))
            except urllib.error.URLError as e:
                self._json({"error": str(e)}, 502)

        elif path == "/api/gps":
            pos = get_gps_position()
            self._json(pos or {"available": False})

        elif path == "/api/lightning":
            if not LIGHTNING_AVAILABLE:
                self._json({"error": "lightning not available on this platform (netCDF4 not installed)"}, 501)
                return
            self._json({"flashes": LIGHTNING_CACHE.get(), "window_minutes": GLM_WINDOW_MINUTES})

        elif path == "/api/snowplows":
            trucks, updated = SNOWPLOW_CACHE.get()
            self._json({"trucks": trucks, "updated": updated})

        elif path == "/api/obs":
            try:
                lat = float(qs["lat"][0])
                lon = float(qs["lon"][0])
                radius_km = float(qs.get("radius_km", ["100"])[0])
            except (KeyError, ValueError):
                self._json({"error": "lat & lon query params required"}, 400)
                return
            try:
                stations = get_madis_obs(lat, lon, radius_km)
            except Exception as e:
                self._json({"error": str(e)}, 502)
                return
            self._json({"stations": stations})

        elif path == "/api/marks/latest.csv":
            if _latest_export["csv"] is None:
                self.send_error(404)
                return
            self._binary(_latest_export["csv"].read_bytes(), "text/csv")

        elif path == "/api/marks/latest.kml":
            if _latest_export["kml"] is None:
                self.send_error(404)
                return
            self._binary(_latest_export["kml"].read_bytes(), "application/vnd.google-earth.kml+xml")

        elif path == "/" or path == "":
            self._static(WEB_DIR / "index.html")

        else:
            rel = path.lstrip("/")
            self._static(WEB_DIR / rel)

    def do_POST(self):
        # Only one POST route exists -- the pin/shape auto-save. Every
        # other endpoint in this app is a GET (this scaffold's established
        # "everything's a query param" style), but pin/shape data is
        # structured and can grow arbitrarily large as someone keeps
        # drawing, which doesn't fit in a query string the way e.g.
        # /api/site?set= does.
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/marks/save":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            session_id = str(body.get("session_id") or "session")
            pins = body.get("pins") or []
            shapes = body.get("shapes") or []
            write_marks_export(session_id, pins, shapes)
            self._json({"ok": True, "pins": len(pins), "shapes": len(shapes)})
        except Exception as e:  # noqa: BLE001 -- a malformed request must not crash the server
            self._json({"error": str(e)}, 400)


def main():
    get_cache(SITE)  # start warming the default site immediately, not on first request
    threading.Thread(target=mosaic_poll_loop, daemon=True).start()
    threading.Thread(target=lightning_poll_loop, daemon=True).start()
    threading.Thread(target=snowplow_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[radar-lab] serving on :{PORT}, site={SITE}, poll every {POLL_INTERVAL_SEC}s, "
          f"mosaic every {MOSAIC_POLL_INTERVAL_SEC}s, lightning every {GLM_POLL_INTERVAL_SEC}s, "
          f"snowplows every {SNOWPLOW_POLL_INTERVAL_SEC}s")
    server.serve_forever()


if __name__ == "__main__":
    main()
