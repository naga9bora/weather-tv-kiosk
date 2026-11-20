"""Tiny static server for the TV weather dashboard.

Only the Python standard library is used, so it runs anywhere:

    python app.py                # http://127.0.0.1:8080
    python app.py --port 9000

It serves dist/ (the built dashboard). It binds loopback by default because it
proxies the optional API keys; pass --host 0.0.0.0 only on a trusted LAN.
The dashboard itself talks straight to api.data.gov.my / Open-Meteo, so this
process only avoids the file:// restrictions inside Chrome on Android TV.

It also proxies the Department of Environment's Air Pollutant Index feed
(/api/haze -> https://eqms.doe.gov.my/api3/publicportalapims/mapcaqm) because the
upstream only allows its own web-app origin (https://eqmp.doe.gov.my) to read it.
The preview page and these routes share one origin, so no CORS header is sent.
Requests addressed to an unknown host name are refused (DNS-rebinding guard).
"""

import argparse
import email.utils
import functools
import http.server
import os
import socket
import socketserver
import sys
import json as _json
import time as _time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
HAZE_CACHE = os.path.join(HERE, "cache", "apims.json")
HAZE_TTL = 30 * 60
HAZE_URL = "https://eqms.doe.gov.my/api3/publicportalapims/mapcaqm"

# MET Malaysia rain radar: proxy so the kiosk can show it even though the TV's
# Android 10 CA store lacks GlobalSign Root R46 (the root met.gov.my uses).
RADAR_TTL = 5 * 60
RADAR_BASE = "https://www.met.gov.my/data/"
RADAR_FILES = {
    "my": "radar_malaysia.gif",
    "sem": "radar_peninsular.gif",
    "east": "radar_east.gif",
}

# Tomorrow.io (free tier): 60 one-minute precipitation steps, used for the
# "hujan ±N min" line on the radar card. Proxied so the key stays out of the
# page source and the kiosk only ever talks to its own origin.
#
# No key is kept in this file: read TOMORROW_KEY / OWM_KEY from the
# environment, or from keys.local.json next to this file (git-ignored) for
# local preview. The APK gets them the same way at build time.
def _load_keys():
    keys = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "keys.local.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            keys.update(_json.load(f))
    except (IOError, OSError, ValueError):
        pass
    for env, name in (("TOMORROW_KEY", "tomorrow"), ("OWM_KEY", "owm"),
                      ("CARTO_KEY", "carto")):
        if os.environ.get(env):
            keys[name] = os.environ[env]
    return keys


KEYS = _load_keys()

TOMORROW_URL = ("https://api.tomorrow.io/v4/weather/forecast"
                "?location=%s,%s&timesteps=1m&units=metric"
                "&fields=precipitationProbability,rainIntensity,weatherCode")

# NOAA aviation weather: real METAR observations (VATSIM's feed is simulated
# flight-sim weather, not the real airport report). Keyless, but no CORS.
METAR_URL = "https://aviationweather.gov/api/data/metar?ids=%s&format=raw&hours=2"

# OpenWeatherMap: proxied like Tomorrow.io so the key never lands in the page
# (which is plain text on the TV's sdcard).
OWM_URLS = {
    "/api/owm/current": "https://api.openweathermap.org/data/2.5/weather"
                        "?lat=%.4f&lon=%.4f&units=metric",
    "/api/owm/forecast": "https://api.openweathermap.org/data/2.5/forecast"
                         "?lat=%.4f&lon=%.4f&units=metric",
}

# CARTO Dark Matter basemap tiles. Anonymous tiles are stamped "API KEY
# REQUIRED", so this key is proxied the same way and never appears in the page.
CARTO_TILE_URL = "https://a.basemaps.cartocdn.com/%s/%d/%d/%d.png?key=%s"


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


# Host names this preview answers to. Filled in by main(); loopback is always
# allowed. Anything else is treated as a rebinding attempt and refused.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def hostname_of(value):
    """'Host'/'Origin' authority -> lower-case hostname, keeping IPv6 bare."""
    value = (value or "").strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end].lower() if end > 0 else ""
    return value.rsplit(":", 1)[0].lower() if ":" in value else value.lower()


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def request_allowed(self):
        """Refuse requests not addressed to a local name.

        The proxy holds the API keys. With no Access-Control-Allow-Origin the
        browser cannot read a response cross-site, and this check stops a
        DNS-rebinding page from reaching the proxy through its own hostname.
        """
        if hostname_of(self.headers.get("Host", "")) not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                host = urllib.parse.urlsplit(origin).hostname or ""
            except ValueError:
                return False
            if host.lower() not in ALLOWED_HOSTS:
                return False
        return True

    def end_headers(self):
        # Never let the TV cache a stale dashboard.
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_HEAD(self):
        if not self.request_allowed():
            return self.send_error(403, "forbidden host")
        return super().do_HEAD()

    def do_GET(self):
        if not self.request_allowed():
            return self.send_error(403, "forbidden host")
        path = self.path.split("?", 1)[0]
        if path.rstrip("/") == "/api/haze":
            return self.proxy_haze()
        if path.rstrip("/") in ("/api/nowcast", "/api/metar"):
            return self.proxy_weather(path, self.path)
        if path.rstrip("/") in OWM_URLS:
            return self.proxy_weather(path, self.path)
        if path.startswith("/api/basemap/"):
            return self.proxy_basemap(path)
        if path.startswith("/api/rainviewer/"):
            return self.proxy_rainviewer(path, self.path)
        if path.startswith("/api/radar/"):
            rest = path[len("/api/radar/"):].strip("/")
            if rest.endswith("/meta"):
                return self.radar_meta(rest[:-len("/meta")])
            return self.proxy_radar(rest)
        return super().do_GET()

    def proxy_rainviewer(self, path, full):
        """Proxy RainViewer API and tiles for same-origin access from the kiosk."""
        try:
            if path.rstrip("/") == "/api/rainviewer/maps":
                url = "https://api.rainviewer.com/public/weather-maps.json"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = r.read()
                ctype = "application/json; charset=utf-8"
            elif path.rstrip("/") == "/api/rainviewer/tile":
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(full).query)
                frame = (qs.get("f") or [""])[0]
                z = (qs.get("z") or ["0"])[0]
                x = (qs.get("x") or ["0"])[0]
                y = (qs.get("y") or ["0"])[0]
                # Only allow RainViewer radar frame paths like /v2/radar/<id>
                if not frame.startswith("/v2/radar/") or ".." in frame:
                    self.send_error(400, "bad frame")
                    return
                for v in (z, x, y):
                    if not v.lstrip("-").isdigit():
                        self.send_error(400, "bad tile")
                        return
                url = "https://tilecache.rainviewer.com%s/256/%s/%s/%s/2/1_1.png" % (frame, z, x, y)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = r.read()
                    ctype = r.headers.get("Content-Type", "image/png")
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(502, "rainviewer unavailable: %s" % e)

    def proxy_basemap(self, path):
        """CARTO basemap tiles: /api/basemap/[labels/]z/x/y.png.

        The key lives in the environment or keys.local.json; the page never sees
        it. Only the two styles the dashboard uses can be requested.
        """
        rest = path[len("/api/basemap/"):]
        labels = rest.startswith("labels/")
        if labels:
            rest = rest[len("labels/"):]
        if rest.endswith(".png"):
            rest = rest[:-4]
        parts = rest.split("/")
        try:
            if len(parts) != 3:
                raise ValueError
            z, x, y = (int(v) for v in parts)
            span = 1 << z
            if not (0 <= z <= 20 and 0 <= x < span and 0 <= y < span):
                raise ValueError
        except ValueError:
            self.send_error(400, "bad tile")
            return
        key = KEYS.get("carto")
        if not key:
            self.send_error(503, "CARTO key not configured")
            return
        style = "dark_only_labels" if labels else "dark_nolabels"
        url = CARTO_TILE_URL % (style, z, x, y, key)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                       "Accept": "image/*"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(502, "tile unavailable: %s" % e)

    def proxy_weather(self, path, full):
        """Tomorrow.io minute nowcast, NOAA METAR and OpenWeatherMap, same-origin.

        The keyed upstreams must not be reachable from page source, and
        aviationweather.gov sends no CORS headers, so the kiosk only ever calls
        this server. Arguments are strictly validated.
        """
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(full).query)
        path = path.rstrip("/")
        try:
            if path == "/api/nowcast" or path in OWM_URLS:
                try:
                    lat = float((qs.get("lat") or [""])[0])
                    lon = float((qs.get("lon") or [""])[0])
                except ValueError:
                    self.send_error(400, "bad lat/lon")
                    return
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    self.send_error(400, "bad lat/lon")
                    return
                if path == "/api/nowcast":
                    key = KEYS.get("tomorrow")
                    if not key:
                        self.send_error(503, "Tomorrow.io key not configured")
                        return
                    url = (TOMORROW_URL % (round(lat, 4), round(lon, 4))
                           + "&apikey=" + key)
                    maxage = 120
                else:
                    key = KEYS.get("owm")
                    if not key:
                        self.send_error(503, "OpenWeatherMap key not configured")
                        return
                    url = (OWM_URLS[path] % (round(lat, 4), round(lon, 4))
                           + "&appid=" + key)
                    maxage = 300
                ctype = "application/json; charset=utf-8"
            else:
                ids = (qs.get("ids") or [""])[0].upper()
                if not ids or len(ids) > 40 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789," for c in ids):
                    self.send_error(400, "bad ids")
                    return
                url = METAR_URL % ids
                ctype = "text/plain; charset=utf-8"
                maxage = 300
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                       "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=%d" % maxage)
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(502, "weather upstream unavailable: %s" % e)

    def radar_meta(self, region):
        """Last-Modified of the frame currently cached, in epoch millis."""
        if region not in RADAR_FILES:
            self.send_error(404, "unknown radar region")
            return
        stamp = os.path.join(HERE, "cache", "radar_%s.time" % region)
        cache = os.path.join(HERE, "cache", "radar_%s.gif" % region)
        updated = 0
        try:
            if os.path.exists(stamp):
                with open(stamp, "r") as f:
                    updated = int(f.read().strip() or 0)
        except (OSError, ValueError):
            updated = 0
        if updated <= 0 and os.path.exists(cache):
            updated = int(os.path.getmtime(cache) * 1000)
        body = _json.dumps({"region": region, "updated": updated}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def proxy_radar(self, region):
        file = RADAR_FILES.get(region)
        if not file:
            self.send_error(404, "unknown radar region")
            return
        cache = os.path.join(HERE, "cache", "radar_%s.gif" % region)
        stamp = os.path.join(HERE, "cache", "radar_%s.time" % region)
        now = _time.time()
        body = None
        if os.path.exists(cache) and now - os.path.getmtime(cache) < RADAR_TTL:
            with open(cache, "rb") as f:
                body = f.read()
        if not body:
            try:
                req = urllib.request.Request(
                    RADAR_BASE + file + "?_ts=" + str(int(now * 1000)),
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "image/gif,image/*,*/*",
                             "Referer": "https://www.met.gov.my/"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    body = r.read()
                    published = 0
                    lm = r.headers.get("Last-Modified")
                    if lm:
                        try:
                            published = int(
                                email.utils.parsedate_to_datetime(lm).timestamp() * 1000)
                        except (TypeError, ValueError):
                            published = 0
                try:
                    os.makedirs(os.path.dirname(cache), exist_ok=True)
                    tmp = cache + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(body)
                    os.replace(tmp, cache)
                    tmp = stamp + ".tmp"
                    with open(tmp, "w") as f:
                        f.write(str(published or int(_time.time() * 1000)))
                    os.replace(tmp, stamp)
                except Exception:
                    pass
            except Exception:
                # Keep showing the previous frame rather than nothing.
                if os.path.exists(cache):
                    with open(cache, "rb") as f:
                        body = f.read()
        if not body:
            self.send_error(502, "radar upstream unavailable")
            return
        # MET serves these as .gif but the payload is a PNG snapshot.
        ctype = "image/png" if body[:4] == b"\x89PNG" else "image/gif"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def proxy_haze(self):
        now = _time.time()
        body = None
        try:
            if os.path.exists(HAZE_CACHE):
                with open(HAZE_CACHE, "r") as f:
                    cached = _json.load(f)
                if now - cached.get("ts", 0) < HAZE_TTL and cached.get("body"):
                    body = cached["body"].encode("utf-8")
        except Exception:
            body = None
        if body is None:
            url = HAZE_URL + "?_ts=" + str(int(now * 1000))
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://eqms.doe.gov.my/APIMS/main",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
            try:
                os.makedirs(os.path.dirname(HAZE_CACHE), exist_ok=True)
                with open(HAZE_CACHE, "w") as f:
                    _json.dump({"ts": now, "body": body.decode("utf-8")}, f)
            except Exception:
                pass
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="listen address; pass 0.0.0.0 only for LAN/TV preview")
    args = ap.parse_args()

    if not os.path.exists(os.path.join(DIST, "index.html")):
        raise SystemExit("dist/index.html missing - run: python tools/build_html.py")

    ALLOWED_HOSTS.update(lan_ips())
    if args.host not in ("0.0.0.0", "::", ""):
        ALLOWED_HOSTS.add(args.host)

    handler = functools.partial(Handler, directory=DIST)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((args.host, args.port), handler) as httpd:
        print(f"serving {DIST}")
        # Only loopback by default: the proxy holds the API keys, so don't
        # expose it to the network unless asked.
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            for ip in lan_ips():
                print(f"  http://{ip}:{args.port}")
        print(f"  http://localhost:{args.port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("bye\n")


if __name__ == "__main__":
    main()
