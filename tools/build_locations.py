"""Build a MET forecast-location -> coordinate table.

Downloads Malaysia ADM2 (district) boundaries from geoBoundaries, then matches each
MET Malaysia forecast location (from api.data.gov.my) to a district polygon and
stores that polygon's centroid.

Output: data/locations.json  ->  [{"id","name","lat","lon","matched"}]

Run once; the result is embedded into the TV dashboard.
"""

import json
import math
import os
import re
import ssl
import unicodedata
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, "cache")
GEOJSON = os.path.join(CACHE, "geoBoundaries-MYS-ADM2_simplified.geojson")

GB_META = "https://www.geoboundaries.org/api/current/gbOpen/MYS/ADM2/"
FORECAST = "https://api.data.gov.my/weather/forecast?limit=10000"

# MET uses some names that differ from the administrative spelling.
ALIASES = {
    "petaling": "petaling",
    "hulu selangor": "hulu selangor",
    "tanah tinggi cameron": "cameron highlands",
    "seberang perai utara": "seberang perai utara",
    "seberang perai tengah": "seberang perai tengah",
    "seberang perai selatan": "seberang perai selatan",
    "larut, matang dan selama": "larut matang dan selama",
    "port dickson": "port dickson",
}

# Hand-placed coordinates for locations the boundary file does not carry as a
# separate ADM2 feature (federal territory folded into its host district, etc).
OVERRIDES = {
    "putrajaya": (2.9264, 101.6964),
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _ctx(verify: bool = True):
    if verify:
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            return r.read()
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            print(f"  (TLS verify failed, retrying unverified: {url})")
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx(False)) as r:
                return r.read()
        raise


def fetch_json(url: str):
    return json.loads(_get(url, 90).decode("utf-8"))


def fetch_bytes(url: str) -> bytes:
    return _get(url, 180)


def centroid(geom):
    """Area-weighted centroid of a Polygon/MultiPolygon; falls back to bbox centre."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    best, best_area = None, -1
    for poly in polys:
        ring = poly[0]
        a = cx = cy = 0.0
        for i in range(len(ring) - 1):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[i + 1][0], ring[i + 1][1]
            cross = x1 * y2 - x2 * y1
            a += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        a *= 0.5
        if abs(a) < 1e-9:
            continue
        if abs(a) > best_area:
            best_area = abs(a)
            best = (cx / (6 * a), cy / (6 * a))

    if best is None:
        xs = [p[0] for p in geom["coordinates"][0]] if geom["type"] == "Polygon" else [
            p[0] for poly in geom["coordinates"] for p in poly[0]
        ]
        ys = [p[1] for p in geom["coordinates"][0]] if geom["type"] == "Polygon" else [
            p[1] for poly in geom["coordinates"] for p in poly[0]
        ]
        best = (sum(xs) / len(xs), sum(ys) / len(ys))

    # A centroid of a concave/hollow shape can land outside; verify roughly.
    return best


def point_in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def main():
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)

    if not os.path.exists(GEOJSON):
        meta = fetch_json(GB_META)
        url = meta.get("simplifiedGeometryGeoJSON") or meta["gjDownloadURL"]
        print(f"downloading boundaries: {url}")
        with open(GEOJSON, "wb") as f:
            f.write(fetch_bytes(url))
    print(f"boundaries: {os.path.getsize(GEOJSON)/1e6:.1f} MB")

    gj = json.load(open(GEOJSON, encoding="utf-8"))
    districts = []
    for feat in gj["features"]:
        props = feat["properties"]
        name = props.get("shapeName") or ""
        if not name:
            continue
        lon, lat = centroid(feat["geometry"])
        districts.append({"name": name, "key": norm(name), "lat": lat, "lon": lon,
                          "geom": feat["geometry"]})
    print(f"districts: {len(districts)}")

    rows = fetch_json(FORECAST)
    locs = {}
    for r in rows:
        lid = r["location"]["location_id"]
        locs[lid] = r["location"]["location_name"]

    by_key = {}
    for d in districts:
        by_key.setdefault(d["key"], d)

    out = []
    for lid in sorted(locs):
        name = locs[lid]
        key = norm(ALIASES.get(norm(name), name))
        d = by_key.get(key)
        if key in OVERRIDES:
            lat, lon = OVERRIDES[key]
            out.append({"id": lid, "name": name, "lat": lat, "lon": lon, "matched": "(override)"})
            continue
        if d is None:  # try "contains" for compound names
            for cand in districts:
                if key and (key in cand["key"] or cand["key"] in key) and len(cand["key"]) > 3:
                    d = cand
                    break
        out.append({
            "id": lid,
            "name": name,
            "lat": round(d["lat"], 5) if d else None,
            "lon": round(d["lon"], 5) if d else None,
            "matched": d["name"] if d else None,
        })

    matched = sum(1 for o in out if o["lat"] is not None)
    print(f"matched {matched}/{len(out)} MET locations to a district polygon")

    if matched < len(out):
        print("unmatched:", [o["name"] for o in out if o["lat"] is None][:40])

    path = os.path.join(DATA, "locations.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("wrote", path)


if __name__ == "__main__":
    main()
