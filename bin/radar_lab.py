#!/usr/bin/env python3
"""Radar Lab -- standalone NEXRAD radar/severe-weather viewer for the field laptop.

Design doc: ~/docs/projects/radar-lab/README.md (read that first -- this is
the V1 scaffold built against the decisions recorded there).

Plain stdlib http.server, no framework -- same pattern as every other tool
in ~/docker/ (parts-wishlist, harness-designer, etc.), except this one has
a real dependency stack (Py-ART, MetPy, numpy) because actual radar-data
decoding requires it. That's an explicitly-accepted cost, not a mistake.

Client-side rendering: this backend decodes and serves plain JSON (radial
arrays, not images). The browser (web/app.js) does all the drawing. See
design doc §2 for why.
"""
import datetime as dt
import json
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pyart

BASE = Path(__file__).resolve().parent.parent
WEB_DIR = BASE / "web"


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
    with tempfile.NamedTemporaryFile(suffix="_V06") as f:
        f.write(raw)
        f.flush()
        radar = pyart.io.read_nexrad_archive(f.name)

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

    with tempfile.NamedTemporaryFile() as f:
        f.write(raw)
        f.flush()
        f3 = Level3File(f.name)

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


# ---------------------------------------------------------------------------
# Background poller -- rolling in-memory cache, no permanent DB (matches
# design doc §2). Refreshes on POLL_INTERVAL_SEC, well under the real
# ~6-7 min NEXRAD update cadence.
# ---------------------------------------------------------------------------

class Cache:
    def __init__(self, site: str):
        self.lock = threading.Lock()
        self.site = site
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

    def set_site(self, new_site: str):
        # Switching sites: old scans/level3 data belong to the old site
        # and would be actively misleading if left in the cache (wrong
        # location, but still structurally valid so nothing would error
        # -- worse than just being empty). Clear rather than keep both
        # sites cached: matches the "single active site" design (doc
        # §2), and avoids unbounded memory growth from every site a user
        # ever glances at.
        with self.lock:
            self.site = new_site
            self.scans = {}
            self.level3 = {}
            self.status = {"last_poll": None, "last_error": None}
            self.latest_raw_l2 = None
            self.tilt_cache = {}

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


def _parse_ts(key: str) -> dt.datetime:
    # KVWX20260923_030241_V06 -> datetime
    stamp = key.split("/")[-1].split("_")[0][-8:] + key.split("_")[1]
    return dt.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)


CACHE = Cache(SITE)
POLL_WAKE = threading.Event()  # lets a site switch skip the wait, see do_GET


def poll_loop():
    last_l2_key = None
    last_l3_key = {"NST": None, "NMD": None}
    last_site = None
    while True:
        site = CACHE.site
        if site != last_site:
            # Site changed since the last iteration -- Cache.set_site()
            # already cleared the cache, but these locals also need
            # resetting so a same-named key from a *previous* visit to
            # this site (e.g. switching A -> B -> A) doesn't get
            # mistaken for "already have this one, skip it".
            last_l2_key = None
            last_l3_key = {"NST": None, "NMD": None}
            last_site = site

        try:
            key = latest_level2_key(site)
            if key and key != last_l2_key:
                raw = s3_fetch(L2_BUCKET, key)
                decoded = decode_level2(raw, site)
                CACHE.add_scan(key, decoded, raw)
                last_l2_key = key
                print(f"[radar-lab] new level2 scan: {key}")
        except Exception as e:  # noqa: BLE001 -- poller must never die
            CACHE.status["last_error"] = f"level2: {e}"
            print(f"[radar-lab] level2 poll error: {e}")

        for product in ("NST", "NMD"):
            try:
                key = latest_level3_key(site, product)
                if key and key != last_l3_key[product]:
                    raw = s3_fetch(L3_BUCKET, key)
                    decoded = decode_level3(raw, site)
                    CACHE.set_level3(product, decoded)
                    last_l3_key[product] = key
                    print(f"[radar-lab] new level3 {product}: {key}")
            except Exception as e:  # noqa: BLE001
                CACHE.status["last_error"] = f"{product}: {e}"
                print(f"[radar-lab] level3 {product} poll error: {e}")

        CACHE.status["last_poll"] = dt.datetime.now(dt.timezone.utc).isoformat()
        # Event.wait() instead of time.sleep() so a site switch (do_GET
        # calls POLL_WAKE.set()) fetches immediately instead of waiting
        # up to POLL_INTERVAL_SEC for the new site's first scan.
        POLL_WAKE.wait(POLL_INTERVAL_SEC)
        POLL_WAKE.clear()


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
    of one marker per camera record."""
    groups: dict[tuple, dict] = {}
    for cam in cams:
        key = (round(cam["lat"], 4), round(cam["lon"], 4))
        group = groups.setdefault(key, {
            "id": cam["id"], "names": [], "src": cam["src"],
            "lat": cam["lat"], "lon": cam["lon"], "snapshots": [],
        })
        label = cam.get("name") or cam["id"]
        if label not in group["names"]:
            group["names"].append(label)
        group["snapshots"].append(cam["snapshot"])
    return [
        {
            "id": g["id"],
            "name": " / ".join(g["names"]),
            "src": g["src"],
            "lat": g["lat"],
            "lon": g["lon"],
            "snapshots": g["snapshots"],
        }
        for g in groups.values()
    ]


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
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[radar-lab] {self.address_string()} - {fmt % args}")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/api/status":
            ts, _ = CACHE.latest_scan()
            self._json({
                "site": CACHE.site, "latest_scan": ts,
                "scan_count": len(CACHE.scan_list()), **CACHE.status,
            })

        elif path == "/api/scans":
            self._json({"scans": CACHE.scan_list()})

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
                CACHE.set_site(new_site)
                POLL_WAKE.set()  # don't make the switch wait for the next poll cycle
                print(f"[radar-lab] site switched to {new_site}")
            self._json({"site": CACHE.site})

        elif path.startswith("/api/") and path[5:] in FIELD_MAP:
            name = path[5:]
            _pyart_field, cache_key, resp_key = FIELD_MAP[name]
            ts = qs.get("ts", [None])[0]
            tilt_param = qs.get("tilt", [None])[0]

            if tilt_param is None:
                data = CACHE.get_scan(ts) if ts else CACHE.latest_scan()[1]
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
                target_ts = ts or CACHE.latest_scan()[0]
                raw = CACHE.raw_for(target_ts) if target_ts else None
                if raw is None:
                    self._json({
                        "error": "tilt selection is only available for the current live scan"
                        if target_ts else "no scan cached yet"
                    }, 400 if target_ts else 503)
                    return
                data = CACHE.get_tilt(target_ts, tilt_index)
                if data is None:
                    try:
                        data = decode_level2(raw, CACHE.site, tilt_index)
                    except ValueError as e:
                        self._json({"error": str(e)}, 400)
                        return
                    CACHE.set_tilt(target_ts, tilt_index, data)

            if data is None or cache_key not in data:
                self._json({"error": f"no {name} data cached yet"}, 503)
                return
            self._json({
                **{k: v for k, v in data.items() if not k.startswith("_")},
                resp_key: serialize_field(data, cache_key),
            })

        elif path == "/api/level3/nst":
            self._json(CACHE.get_level3("NST") or {"points": []})

        elif path == "/api/level3/nmd":
            self._json(CACHE.get_level3("NMD") or {"points": []})

        elif path == "/api/cameras":
            try:
                lat = float(qs["lat"][0])
                lon = float(qs["lon"][0])
                radius_km = float(qs.get("radius_km", ["50"])[0])
            except (KeyError, ValueError):
                self._json({"error": "lat & lon query params required"}, 400)
                return
            cams = get_cameras()
            nearby = [
                {**c, "distance_km": round(haversine_km(lat, lon, c["lat"], c["lon"]), 1)}
                for c in cams
            ]
            nearby = [c for c in nearby if c["distance_km"] <= radius_km]
            nearby.sort(key=lambda c: c["distance_km"])
            self._json({"cameras": nearby})

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

        elif path == "/" or path == "":
            self._static(WEB_DIR / "index.html")

        else:
            rel = path.lstrip("/")
            self._static(WEB_DIR / rel)


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[radar-lab] serving on :{PORT}, site={SITE}, poll every {POLL_INTERVAL_SEC}s")
    server.serve_forever()


if __name__ == "__main__":
    main()
