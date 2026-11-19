"""Inline data/locations.json into the dashboard template -> dist/index.html."""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TPL = os.path.join(ROOT, "static", "index.html")
OUT = os.path.join(ROOT, "dist", "index.html")


def main():
    # utf-8-sig: files edited with Notepad/PowerShell often start with a BOM.
    locs = json.load(open(os.path.join(ROOT, "data", "locations.json"), encoding="utf-8-sig"))

    # Keep the payload small: id, name and coords only; drop unresolved entries.
    compact = [
        {"id": l["id"], "name": l["name"], "lat": l["lat"], "lon": l["lon"]}
        for l in locs if l.get("lat") is not None
    ]

    tpl = open(TPL, encoding="utf-8").read()

    # Inline the stylesheet so the page stays a single self-contained document.
    css = open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    leaflet_css_path = os.path.join(ROOT, "static", "vendor", "leaflet.css")
    if os.path.exists(leaflet_css_path):
        css = open(leaflet_css_path, encoding="utf-8").read() + "\n" + css
    a = tpl.index("<style>") + len("<style>")
    b = tpl.index("</style>")
    tpl = tpl[:a] + "\n" + css + "\n" + tpl[b:]

    # Inline Leaflet (if present) so the dashboard works as a single file on the TV.
    leaflet_js_path = os.path.join(ROOT, "static", "vendor", "leaflet.js")
    leaflet_marker = "/*__LEAFLET__*/"
    if os.path.exists(leaflet_js_path):
        ljs = open(leaflet_js_path, encoding="utf-8").read().replace("</script", "<\\/script")
        if leaflet_marker in tpl:
            tpl = tpl.replace(leaflet_marker, ljs)
        else:
            tpl = tpl.replace("</body>", "<script>\n" + ljs + "\n</script>\n</body>")
    elif leaflet_marker in tpl:
        tpl = tpl.replace(leaflet_marker, "")

    marker = "/*__LOCATIONS__*/[]"
    if marker not in tpl:
        raise SystemExit(f"placeholder {marker!r} not found in {TPL}")

    data = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    html = tpl.replace(marker, data)

    # Private settings: kept out of the repository. kiosk.local.json may contain
    # {"manual": {...}, "metar": "EGLL", "hazeStation": "..."}; without it the
    # page falls back to ISP/GPS, skips the METAR station and picks the nearest
    # air-quality station by coordinates.
    placeholders = {
        "/*__MANUAL__*/null": "manual",
        "/*__METAR__*/null": "metar",
        '/*__HAZE__*/""': "hazeStation",
    }
    for marker, key in placeholders.items():
        if marker not in html:
            raise SystemExit(f"placeholder {marker!r} not found in {TPL}")
    local = os.path.join(ROOT, "kiosk.local.json")
    cfg = {}
    try:
        with open(local, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (IOError, OSError, ValueError):
        pass
    for marker, key in placeholders.items():
        value = cfg.get(key)
        if key == "hazeStation":
            injected = json.dumps(value or "", ensure_ascii=False)
        elif value:
            injected = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            injected = "null"
        html = html.replace(marker, injected)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"wrote {OUT}  ({len(html)/1024:.0f} KB, {len(compact)} locations)")


if __name__ == "__main__":
    main()
