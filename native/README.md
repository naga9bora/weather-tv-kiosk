# Weather Kiosk — native Android TV app (weather + JAS)

A native Android TV weather kiosk app. **Not** a WebView app: it is a real Android application built
with Gradle (Android Gradle Plugin 8.5.2, JDK 17, compileSdk 34, minSdk 26, targetSdk 34, R8 minify +
shrinkResources on release, debug signing by default so it installs over a debug build).

It takes the **weather reading** and the **JAS (air pollutant index) reading** from the proven
[encikzul/layarkaca-tv](https://github.com/encikzul/layarkaca-tv) reference implementation and nothing
else from it — no prayer times, no launcher grid, no wallpapers.

## What it contains

| Class | What it does |
|---|---|
| `tv.weather.kiosk.Weather` | MET Malaysia (MyCuaca) via `api.data.gov.my`: today's forecast + active warnings, cached in the files dir (forecast 3 h, warnings 20 min). Ported from the reference. |
| `tv.weather.kiosk.Haze` | Air Pollutant Index (JAS/APIMS via `eqms.doe.gov.my`), cached 30 min, nearest-station fallback. Ported from the reference. |
| `tv.weather.kiosk.MainActivity` | The TV screen: live clock with seconds, temperature, condition, min/max, when/morning/afternoon/night, active warning, JAS reading. |

## Build

Prerequisites: JDK 17 and the Android SDK (platform 34 + build-tools 34.0.0).

```bash
# from the repo root, once: copy the Gradle wrapper from the reference launcher module
cp -r /path/to/layarkaca-tv/launcher/gradlew /path/to/layarkaca-tv/launcher/gradlew.bat ./native/
cp -r /path/to/layarkaca-tv/launcher/gradle ./native/gradle

cd native
./gradlew assembleRelease
```

Output: `native/app/build/outputs/apk/release/app-release.apk`

## Install on the TV

```bash
adb connect 192.0.2.10:5555
adb install -r native/app/build/outputs/apk/release/app-release.apk
adb shell am start -n tv.weather.kiosk/.MainActivity
```

## Release

Push a tag like `v1.0` and the [Release workflow](.github/workflows/release.yml) builds, signs (with the
`RELEASE_*` secrets) and publishes the APK to the GitHub Releases page with its SHA-256 and signing
certificate.

## Notes

- The activity declares `LEANBACK_LAUNCHER` + `LAUNCHER`, landscape, not excluded from recents — it is a
  normal Android TV app that shows in the TV app grid, not the TV Home screen.
- Permissions: `INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`, `ACCESS_FINE_LOCATION`,
  `WAKE_LOCK`, `RECEIVE_BOOT_COMPLETED`.
- Auto-refresh every 10 minutes, clock ticks every second.
