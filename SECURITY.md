# Security

## No secrets in this repository

The dashboard has a **keyless core**: MET Malaysia forecasts and warnings,
Open-Meteo, NOAA METAR, JAS/APIMS air quality and RainViewer radar need no
account at all. Three optional free-tier keys (Tomorrow.io, OpenWeatherMap,
CARTO) are **never committed**. They live in `keys.local.json` (git-ignored) or
the environment, and are injected at build time into the APK only.

`keys.local.json`, `kiosk.local.json`, `.env`, `dist/`, `android/assets/`,
`.sdk/`, `build/` and `cache/` are git-ignored. If you build from source, keep
your own keys and home coordinates out of commits.

## Reporting a vulnerability

Open a **private security advisory** on GitHub instead of a public issue. Do not
include keys, coordinates, device addresses or screenshots with identifying
information in any report or issue.

## Scope notes

- `app.py` is a local preview proxy; it listens on `127.0.0.1` by default
  because it holds the API keys. Pass `--host 0.0.0.0` only on a network you
  trust.
- The installed APK keeps the keys in its private assets directory, which is
  normal for an app but means the APK file should not be shared.
- The device address is never stored; scripts read `-Device <ip:5555>` or
  `KIOSK_DEVICE`.
