"""Tiny static server for the TV weather dashboard.

Only the Python standard library is used, so it runs anywhere:

    python app.py                # http://0.0.0.0:8080
    python app.py --port 9000

It serves dist/ (the built dashboard). The dashboard itself talks straight to
api.data.gov.my / Open-Meteo, so this process does no proxying or caching - it
just avoids the file:// restrictions inside Chrome on Android TV.

It also proxies the Department of Environment's Air Pollutant Index feed
(/api/haze -> https://eqms.doe.gov.my/api3/publicportalapims/mapcaqm) because the
upstream only allows its own web-app origin (https://eqmp.doe.gov.my) to read it,
which the TV kiosk (fake origin http://weather.local/) is not. The proxy replies
with Access-Control-Allow-Origin: * so the kiosk's fetch() is allowed.
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
TOMORROW_KEY = "REDACTED_TOMORROW_KEY"
TOMORROW_URL = ("https://api.tomorrow.io/v4/weather/forecast"
                "?location=%s,%s&timesteps=1m&units=metric"
                "&fields=precipitationProbability,rainIntensity,weatherCode"
                "&apikey=" + TOMORROW_KEY)

# NOAA aviation weather: real METAR observations (VATSIM's feed is simulated
# flight-sim weather, not the real airport report). Keyless, but no CORS.
METAR_URL = "https://aviationweather.gov/api/data/metar?ids=%s&format=raw&hours=2"


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


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
    def end_headers(self):
        # Never let the TV cache a stale dashboard.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.rstrip("/") == "/api/haze":
            return self.proxy_haze()
        if path.rstrip("/") in ("/api/nowcast", "/api/metar"):
            return self.proxy_weather(path, self.path)
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

    def proxy_weather(self, path, full):
        """Tomorrow.io minute nowcast and NOAA METAR, same-origin for the kiosk.

        Both upstreams are either keyed in page source or missing CORS, so the
        kiosk only ever calls this server. Arguments are strictly validated.
        """
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(full).query)
        try:
            if path.rstrip("/") == "/api/nowcast":
                try:
                    lat = float((qs.get("lat") or [""])[0])
                    lon = float((qs.get("lon") or [""])[0])
                except ValueError:
                    self.send_error(400, "bad lat/lon")
                    return
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    self.send_error(400, "bad lat/lon")
                    return
                url = TOMORROW_URL % (round(lat, 4), round(lon, 4))
                ctype = "application/json; charset=utf-8"
                maxage = 120
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
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    if not os.path.exists(os.path.join(DIST, "index.html")):
        raise SystemExit("dist/index.html missing - run: python tools/build_html.py")

    handler = functools.partial(Handler, directory=DIST)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((args.host, args.port), handler) as httpd:
        print(f"serving {DIST}")
        for ip in lan_ips():
            print(f"  http://{ip}:{args.port}")
        print(f"  http://localhost:{args.port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("bye\n")


if __name__ == "__main__":
    main()
