"""Download the Android SDK pieces needed to build the kiosk APK.

Only two archives are required (no Gradle, no full SDK install):

    platform-29_r05.zip        -> android.jar
    build-tools_r29.0.3-windows.zip -> aapt2, d8, zipalign, apksigner

They land in .sdk/ and tools/build_apk.py unpacks them on first build.
"""

import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SDK = os.path.join(os.path.dirname(HERE), ".sdk")

BASE = "https://dl.google.com/android/repository/"
FILES = [
    ("platform-29.zip", "platform-29_r05.zip", 75),
    ("build-tools-29.zip", "build-tools_r29.0.3-windows.zip", 53),
]


def main():
    os.makedirs(SDK, exist_ok=True)
    for local, remote, mb in FILES:
        dest = os.path.join(SDK, local)
        if os.path.exists(dest) and os.path.getsize(dest) > mb * 1024 * 1024 * 0.8:
            print(f"{local}: already present")
            continue
        print(f"downloading {remote} (~{mb} MB)")
        urllib.request.urlretrieve(BASE + remote, dest)
        print(f"  -> {dest} ({os.path.getsize(dest)/1e6:.1f} MB)")
    print("done - run: python tools/build_apk.py")


if __name__ == "__main__":
    sys.exit(main())
