# Security

## No secrets in this repository

The dashboard has a **keyless core**: MET Malaysia forecasts and warnings,
Open-Meteo, NOAA METAR, JAS/APIMS air quality and RainViewer radar need no
account at all. Three optional free-tier keys (Tomorrow.io, OpenWeatherMap,
CARTO) are **never committed**. They live in `keys.local.json` (git-ignored) or
the environment, and are injected at build time into the APK only.

`keys.local.json`, `kiosk.local.json`, `.env`, `dist/`, `android/assets/`,
`.sdk/`, `build/` and `cache/` are git-ignored. `kiosk.local.json` also holds
the home location, the METAR station id and the air-quality station fallback;
the shipped page has none of them. If you build from source, keep your own keys
and home coordinates out of commits.

`keys.local.json` additionally holds a `page` value, generated on the first
build. It is not a provider key: it signs the page that
`scripts/update-page.ps1` pushes to the device, so `MainActivity` can reject a
page written to shared storage by another app.

## Reporting a vulnerability

Open a **private security advisory** on GitHub instead of a public issue. Do not
include keys, coordinates, device addresses or screenshots with identifying
information in any report or issue.

## Scope notes

- `app.py` is a local preview proxy; it listens on `127.0.0.1` by default
  because it holds the API keys. Pass `--host 0.0.0.0` only on a network you
  trust.
- The installed APK keeps the keys in its private assets directory, which is
  normal for an app but means the APK file should not be shared. Anyone who can
  read the APK can extract the provider keys and the page-signing key.
- The device address is never stored; scripts read `-Device <ip:5555>` or
  `KIOSK_DEVICE`.
- **DevTools.** `WebView.setWebContentsDebuggingEnabled` is off in a shipped
  build and only a 10-minute maintenance window (MENU x3) or an adb intent turns
  it on. On a `userdebug` firmware the platform forces WebView DevTools on
  regardless of that call; this TV box is `userdebug`, so treat wireless adb as
  the real boundary there and keep it off outside maintenance.
- **Page integrity.** A page pushed to shared external storage is loaded only
  when its `index.html.hmac` sidecar matches the HMAC-SHA256 key in the APK.
  This blocks a page planted by another app that does not know the key; it is
  not a boundary against someone who can read the APK or the device's data.
- **Transport.** `android/res/xml/network_security_config.xml` permits cleartext
  only for the local fake origin `weather.local`; every outbound request is
  HTTPS. ISP geolocation no longer uses the HTTP-only `ip-api` endpoint.
