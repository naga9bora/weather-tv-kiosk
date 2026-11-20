"""Build and sign the kiosk APK without Gradle.

    python tools/build_apk.py
    python tools/build_apk.py --allow-keyless   # panel without provider keys
    python tools/build_apk.py --debug           # keep WebView DevTools on

Requires the SDK bits in .sdk/ (platform android.jar + build-tools). Everything
else is Python/Java that is already on the machine.
"""

import glob
import json
import os
import secrets
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ANDROID = os.path.join(ROOT, "android")
SDK = os.path.join(ROOT, ".sdk")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")

PKG = "tv.weather.kiosk"


def run(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd)[:220])
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def unzip_all(src, dst):
    os.makedirs(dst, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(dst)


def find(pattern):
    hits = glob.glob(os.path.join(SDK, "**", pattern), recursive=True)
    if not hits:
        raise SystemExit(f"missing {pattern} under {SDK} - run tools/fetch_sdk.py")
    return hits[0]


def load_keys():
    """API keys from keys.local.json (git-ignored) or the environment.

    They are written into android/assets/keys.properties so the APK still ships
    them, but the repository never contains them.

    The same file also holds the page-signing key ("page"), generated on first
    build. scripts/update-page.ps1 signs the pushed page with it, and
    MainActivity refuses an external page whose signature does not match.
    """
    keys = {}
    file_keys = {}
    path = os.path.join(ROOT, "keys.local.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            file_keys = json.load(f) or {}
    except (IOError, OSError, ValueError):
        file_keys = {}
    keys.update(file_keys)
    for env, name in (("TOMORROW_KEY", "tomorrow"), ("OWM_KEY", "owm"),
                      ("CARTO_KEY", "carto")):
        if os.environ.get(env):
            keys[name] = os.environ[env]

    if not keys.get("page"):
        page = secrets.token_hex(32)
        keys["page"] = page
        file_keys["page"] = page
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(file_keys, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError as e:
            raise SystemExit("cannot write page key to keys.local.json: %s" % e)
        print("generated page-signing key in keys.local.json")
    return keys


def java_tool(name):
    """javac/keytool live in JAVA_HOME/bin but are not always on PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    hit = shutil.which(exe)
    if hit:
        return hit
    home = os.environ.get("JAVA_HOME")
    if home:
        cand = os.path.join(home, "bin", exe)
        if os.path.exists(cand):
            return cand
    for base in (r"C:\Program Files\Java", r"C:\Program Files (x86)\Java",
                 "/usr/lib/jvm"):
        for cand in sorted(glob.glob(os.path.join(base, "*", "bin", exe)), reverse=True):
            return cand
    raise SystemExit(f"cannot locate {exe}; set JAVA_HOME")


def main():
    # --allow-keyless: build without API keys (no nowcast, no CARTO basemap).
    allow_keyless = "--allow-keyless" in sys.argv
    # --debug: keep WebView DevTools available all the time (development only).
    # The shipped default is off; a technician can still open a 10-minute window
    # with MENU x3 or `adb shell am start ... --ez debug true`.
    debug = "--debug" in sys.argv

    # --- unpack SDK if needed -------------------------------------------------
    for name, pattern in (("platform-29.zip", "android.jar"), ("build-tools-29.zip", "aapt2.exe")):
        src = os.path.join(SDK, name)
        if not glob.glob(os.path.join(SDK, "**", pattern), recursive=True):
            if not os.path.exists(src):
                raise SystemExit(f"{src} not found - run tools/fetch_sdk.py")
            print("extracting", name)
            unzip_all(src, SDK)

    android_jar = find("android.jar")
    aapt2 = find("aapt2.exe")
    zipalign = find("zipalign.exe")
    d8_jar = find(os.path.join("lib", "d8.jar"))
    apksigner_jar = find(os.path.join("lib", "apksigner.jar"))

    # --- fresh build dir ------------------------------------------------------
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(os.path.join(BUILD, "classes"))
    os.makedirs(os.path.join(BUILD, "dex"))
    os.makedirs(DIST, exist_ok=True)

    assets = os.path.join(ANDROID, "assets")
    os.makedirs(assets, exist_ok=True)
    shutil.copyfile(os.path.join(DIST, "index.html"), os.path.join(assets, "index.html"))

    # --- API keys -------------------------------------------------------------
    keys = load_keys()
    missing = [n for n in ("tomorrow", "owm", "carto") if not keys.get(n)]
    if missing and not allow_keyless:
        raise SystemExit(
            "missing API key(s): %s\n"
            "create keys.local.json in the project root:\n"
            '  {"tomorrow": "...", "owm": "...", "carto": "..."}\n'
            "or set TOMORROW_KEY, OWM_KEY and CARTO_KEY in the environment.\n"
            "or pass --allow-keyless to build a panel without them."
            % ", ".join(missing))
    if missing:
        print("warning: no API key(s) (%s) - keyless build; the 60-minute "
              "outlook and CARTO basemap stay off, everything else works"
              % ", ".join(missing))
    with open(os.path.join(assets, "keys.properties"), "w", encoding="utf-8") as f:
        for name in ("tomorrow", "owm", "carto"):
            f.write("%s=%s\n" % (name, keys.get(name, "")))
        # The page-signing key and the DevTools default live with the keys so
        # the repository never carries either. A build without the key would
        # reject every pushed page and fall back to the packaged asset.
        f.write("page=%s\n" % keys.get("page", ""))
        f.write("debug=%s\n" % ("true" if debug else "false"))

    res_dir = os.path.join(ANDROID, "res")
    compiled_res = os.path.join(BUILD, "res.zip")
    # aapt2 creates the output file itself; don't pre-create it

    # --- 1. resources + manifest ---------------------------------------------
    unsigned = os.path.join(BUILD, "app-unsigned.apk")
    link_cmd = [aapt2, "link", "-o", unsigned,
         "-I", android_jar,
         "--manifest", os.path.join(ANDROID, "AndroidManifest.xml"),
         "-A", assets,
         "--min-sdk-version", "26", "--target-sdk-version", "29",
         "--version-code", "1", "--version-name", "1.0"]
    if os.path.isdir(res_dir):
        run([aapt2, "compile", "--dir", res_dir, "-o", compiled_res])
        link_cmd += ["-R", compiled_res]
    run(link_cmd)

    # --- 2. compile java ------------------------------------------------------
    sources = sorted(glob.glob(os.path.join(ANDROID, "src", "**", "*.java"), recursive=True))
    run([java_tool("javac"), "-source", "8", "-target", "8", "-nowarn",
         "-bootclasspath", android_jar,
         "-d", os.path.join(BUILD, "classes")] + sources)

    # --- 3. dex ---------------------------------------------------------------
    classes = sorted(glob.glob(os.path.join(BUILD, "classes", "**", "*.class"), recursive=True))
    run(["java", "-cp", d8_jar, "com.android.tools.r8.D8",
         "--lib", android_jar, "--min-api", "26",
         "--output", os.path.join(BUILD, "dex")] + classes)

    # --- 4. fold classes.dex into the apk -------------------------------------
    with_dex = os.path.join(BUILD, "app-withdex.apk")
    shutil.copyfile(unsigned, with_dex)
    with zipfile.ZipFile(with_dex, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(BUILD, "dex", "classes.dex"), "classes.dex")

    # --- 5. align + sign ------------------------------------------------------
    aligned = os.path.join(BUILD, "app-aligned.apk")
    run([zipalign, "-f", "4", with_dex, aligned])

    keystore = os.path.join(ROOT, ".sdk", "debug.keystore")
    if not os.path.exists(keystore):
        run([java_tool("keytool"), "-genkeypair", "-keystore", keystore,
             "-storepass", "android", "-keypass", "android",
             "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048",
             "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US"])

    out = os.path.join(DIST, "weather-kiosk.apk")
    run(["java", "-jar", apksigner_jar, "sign",
         "--ks", keystore, "--ks-pass", "pass:android", "--key-pass", "pass:android",
         "--out", out, aligned])
    run(["java", "-jar", apksigner_jar, "verify", "--print-certs", out],
        stdout=subprocess.DEVNULL)

    print(f"\nbuilt {out}  ({os.path.getsize(out)/1024:.0f} KB)")


if __name__ == "__main__":
    sys.exit(main())
