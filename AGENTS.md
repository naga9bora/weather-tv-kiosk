# AGENTS.md

Working notes for coding agents. The product is a **single always-on lobby
display**: if it shows a hole, a clipped card, a raw error page or a frozen
frame, it has failed, even if the code "works". Read this before touching the
page.

## What this is

A 1280x720 WebView kiosk on an Android TV box, plus the shell that hosts it.

| Path | What it is |
|---|---|
| `static/index.html` | The whole page: CONFIG, data fetching, rendering, housekeeping. |
| `static/style.css` | All styles. Inlined into `dist/index.html` at build time. |
| `static/vendor/leaflet.*` | Vendored Leaflet 1.9.4, inlined too. |
| `android/` | The shipping app: `MainActivity`, manifest, icons. Built without Gradle. |
| `tools/build_html.py` | `static/` + `data/locations.json` + `kiosk.local.json` -> `dist/index.html`. |
| `tools/build_apk.py` | `dist/index.html` + `android/` + local keys -> signed APK. |
| `scripts/update-page.ps1` | Push a new page, keep `.bak`, restart. No APK rebuild. |
| `scripts/install.ps1` | Install the APK and grant the autostart app-op. |
| `app.py` | Local preview server with the same `/api/*` routes as the shell. |
| `data/locations.json` | 440 MET districts, 277 with coordinates. Public data. |
| `docs/kiosk.png` | README screenshot, captured from the real device. |
| `native/` | A separate, older Gradle app. **Not** what ships. Do not wire it into the build. |

`dist/`, `build/`, `android/assets/`, `.sdk/`, `cache/`, `keys.local.json` and
`kiosk.local.json` are git-ignored. Never commit them.

## Hard constraints

1. **The device WebView is Chrome 74.** No `clamp()`, `min()`, `max()`, `dvh`,
   `svh`, flex `gap`, `inset`, `??`, optional chaining, or top-level `await` in
   the page. Do not add a framework, a bundler, or an npm dependency.
2. **Vertical sizing is `calc(var(--vh) * N)`** where `--vh` is set from
   `window.innerHeight` (720px on the panel). Do not hard-code pixel heights for
   layout sections.
3. **Edit `static/style.css`, never `dist/`.** CSS is concatenated into a
   `<style>` block by `build_html.py`; edits to `dist/` are overwritten.
4. **Keep the panel complete.** No placeholder boxes, no "no data" holes, no
   scrollbars. A dead source must degrade to the last good value or hide one
   element, not empty a region.
5. **Do not call `radarMap.remove()` at runtime.** It has rebooted/hung the box.
   Update layers in place; hide the card only if no frame ever loaded.
6. **Never `stop adbd; start adbd`** on the device. It drops the wireless link
   and needs physical access to recover.
7. **The page is plain text on `/sdcard`.** No secret may appear in it. Keys are
   read by the shell from `keys.properties` and by `app.py` from
   `keys.local.json` / environment. Proxies exist for this reason: `/api/nowcast`,
   `/api/metar`, `/api/owm/*`, `/api/basemap/*`, `/api/rainviewer/*`,
   `/api/radar/*`, `/api/haze`.
8. **User-facing text is Bahasa Malaysia.** Keep existing labels and status
   words (`ok`, `ok · cache`, `ralat`, `basi N min`) consistent.

## Build, deploy, verify

The device address is never stored; use `-Device <ip:5555>` or `KIOSK_DEVICE`.

```powershell
# page-only change (normal case, ~2 s push + app restart)
python tools\build_html.py
powershell -ExecutionPolicy Bypass -File .\scripts\update-page.ps1 -Device $env:KIOSK_DEVICE

# shell/Java change
python tools\build_apk.py
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Device $env:KIOSK_DEVICE

# local preview with the same proxies
python app.py                      # http://127.0.0.1:8080

# one-command health check (8 assertions)
python tools\smoke.py --device $env:KIOSK_DEVICE

# evaluate JS in the running page
adb -s <device> forward tcp:9222 localabstract:webview_devtools_remote_<pid>
python tools\cdp.py eval "document.getElementById('status').textContent"
```

`update-page.ps1` copies the running page to `index.html.bak` first and checks
the pushed byte count. The shell loads `index.html` -> `index.html.bak` ->
packaged asset, and marks a copy that never sends a heartbeat within 90 s as
unbootable (keyed by length+mtime), so a broken push costs ~90 s once.

After a restart the app needs ~45 s before the DevTools page target exists; a
first probe may print "no page target". That is normal.

## Layout invariants

Measured on the panel (1280x720, warning banner hidden): radar 317, now 352,
fc 253, overflow 0. When an active warning is shown, `main` loses ~40 px and the
air-quality card must still fit; `renderHaze()` hides its guidance line rather
than clipping it. `tools/smoke.py` asserts no container overflow and nothing
outside the viewport; run it after any layout change.

## Data and resilience rules

- Refresh every 10 min. Forecast comes from MET Malaysia
  (`api.data.gov.my`, keyless); current conditions prefer a fresh NOAA METAR
  (< 110 min), then Open-Meteo, then OpenWeatherMap.
- 60-minute rain outlook is Tomorrow.io only, via `/api/nowcast`; free tier is
  25 calls/h (the kiosk uses ~6). A failure keeps the previous line.
- Last-good cache (`CACHE_TTL_MS`, 12 h) covers current, METAR and forecast rows
  in memory + `localStorage`. Cached data keeps its original timestamps.
- Footer status is three-state: `ok`, `ok · cache`, and `ralat` only when
  nothing rendered at all. Do not reintroduce per-source error strings.
- Staleness: amber `basi N min` after 20 min, red after 45.
- Night dimming, burn-in drift, hourly header blanking and the 04:00 reload live
  in the "Kiosk housekeeping" block of `static/index.html`.
- Keep housekeeping best-effort (`try/catch`), never allowed to break rendering.

## Secrets and personal data

- `keys.local.json` = `{"tomorrow": "...", "owm": "...", "carto": "..."}`,
  git-ignored. Environment variables `TOMORROW_KEY`, `OWM_KEY`, `CARTO_KEY`
  override it. A missing key fails `build_apk.py` on purpose; a running APK
  answers `503 Not Configured` for that proxy.
- `kiosk.local.json` = `{"manual": {"locationId": "...", "lat": ..., "lon": ...,
  "region": "...", "note": "..."}}`, git-ignored. It is injected into
  `CONFIG.manual` by `build_html.py`; the repo ships `manual: null` (ISP/GPS
  fallback). Never commit home coordinates, a building name or a device address.
- Read local JSON with `utf-8-sig`: Notepad and PowerShell add a BOM, and plain
  `utf-8` silently returns nothing.
- Before committing, scan the staged tree (review anything it prints):

  ```powershell
  # keys, tokens, emails and IPv4 literals
  git grep --cached -n -i -E "api[_-]?key|token|secret|passw|@[a-z0-9.-]+\.[a-z]{2,}|[0-9]{1,3}(\.[0-9]{1,3}){3}"
  ```

  Then review `git diff --cached` by eye and open any staged screenshot: high
  precision coordinates and a home label survive every automated scan.

- `docs/kiosk.png` must be captured with a generic on-screen location label
  (temporarily set the note to `manual - lokasi tetap`), then restore
  `kiosk.local.json`. A screenshot can leak what the code does not.
- The repository is private. If it ever goes public, rotate all three keys
  first; they exist in no commit after the history rewrite.

## Device / tooling gotchas

- `adb push`/`install` fail on paths containing spaces; stage files in
  `%TEMP%` first. Example device id: `192.0.2.10:5555` (documentation range).
- The box runs Android 10. `SYSTEM_ALERT_WINDOW` is required for autostart from
  `BOOT_COMPLETED`; `install.ps1` grants it.
- PowerShell is CRLF/BOM-friendly only if you avoid binary redirection
  (`adb exec-out ... > file` corrupts PNG; use `adb pull`).
- Tomorrow.io returns 429 when the hourly quota is exhausted by repeated app
  restarts; it recovers within the hour and is not a code bug.
- `tools/audit.py` prints a layout snapshot; `tools/smoke.py` is the pass/fail
  gate.

## When you change behaviour

Update `README.md` (tables for sources, kiosk behaviour, keys) and this file if
you add a constraint, a proxy, or a new local config file. Then:

1. `python tools\build_html.py`
2. `python tools\smoke.py --device ...` — all 8 checks must pass
3. screenshot the panel once, before and after any visual change
4. commit only source: no `dist/`, no keys, no home location
