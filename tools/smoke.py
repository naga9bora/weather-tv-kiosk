"""One-command health check for the running kiosk.

    python tools/smoke.py                 # check the app on the TV
    python tools/smoke.py --json          # raw values
    python tools/smoke.py --adb C:\\platform-tools\\adb.exe --device 192.0.2.10:5555

What it does: finds the adb binary, looks up the running kiosk pid, forwards the
WebView devtools socket, evaluates the page and asserts the things that actually
went wrong in the past - nothing rendered, an empty forecast strip, a lost
Android bridge, or a container spilling off the 1280x720 panel.

Exit code 0 = healthy, 1 = something is wrong (the failing check is printed).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402

PACKAGE = "tv.weather.kiosk"
PORT = 9222

ADB_CANDIDATES = [
    os.environ.get("ADB"),
    os.environ.get("ANDROID_HOME") and os.path.join(os.environ["ANDROID_HOME"], "platform-tools", "adb.exe"),
    r"C:\platform-tools\adb.exe",
    "/usr/bin/adb",
]

PROBE = r"""(() => {
  const t = el => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : null);
  // Layout boxes must not overflow internally: a clipped #haze is a hole in the panel.
  const over = [];
  ['app', 'now', 'haze', 'fc', 'radar'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const dh = el.scrollHeight - el.clientHeight;
    const dw = el.scrollWidth - el.clientWidth;
    if (dh > 2 || dw > 2) over.push(id + ' (+' + Math.max(dw, 0) + 'w +' + Math.max(dh, 0) + 'h)');
  });
  // Anything sticking out of the viewport, ignoring map internals (Leaflet
  // deliberately parks tiles outside its clipping container).
  const clipped = [...document.querySelectorAll('*')].filter(e => {
    if (e.closest('.leaflet-container')) return false;
    const b = e.getBoundingClientRect();
    return b.width > 0 && b.height > 0 && (b.bottom > innerHeight + 1 || b.right > innerWidth + 1);
  }).map(e => e.tagName + '.' + e.className).slice(0, 5);
  const bridge = window.AndroidInterface;
  let beat = false, exit = false;
  try {
    beat = typeof bridge.beat === 'function';
    exit = typeof bridge.requestExit === 'function';
  } catch (e) { /* no bridge */ }
  const dim = document.getElementById('dim');
  return {
    status: t(document.getElementById('status')),
    statusClass: document.getElementById('status') ? document.getElementById('status').className : null,
    days: document.querySelectorAll('#days .day').length,
    temp: t(document.getElementById('nowTemp')),
    nowcast: t(document.getElementById('radarNow')),
    fcSrc: t(document.getElementById('fcSrc')),
    bridge: { beat: beat, exit: exit },
    dim: dim ? getComputedStyle(dim).opacity : null,
    size: [innerWidth, innerHeight],
    over: over,
    clipped: clipped
  };
})()"""


def find_adb(explicit):
    for cand in [explicit] + ADB_CANDIDATES + [shutil.which("adb")]:
        if cand and os.path.isfile(cand):
            return cand
    raise SystemExit("adb not found - pass --adb <path> or set %ADB%")


def adb(adb_path, device, *args, check=True):
    p = subprocess.run([adb_path, "-s", device] + list(args),
                       capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise SystemExit("adb %s failed: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
    return p


def devtools_up(timeout=3):
    """True when the WebView DevTools target is reachable."""
    try:
        cdp.targets(timeout=timeout)
        return True
    except Exception:
        return False


def open_debug_window(adb_path, device):
    """Shipped builds keep DevTools off. Open the 10-minute window over adb.

    The app persists the window in SharedPreferences and rebuilds the WebView,
    because Chromium reads the debugging flag when a WebView is created.
    """
    adb(adb_path, device, "shell", "am", "start", "-n",
        PACKAGE + "/.MainActivity", "--ez", "debug", "true", check=False)
    deadline = time.time() + 60
    while time.time() < deadline:
        if devtools_up(3):
            return True
        time.sleep(2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb")
    ap.add_argument("--device", default=os.environ.get("KIOSK_DEVICE"))
    ap.add_argument("--json", action="store_true", help="print raw probe values")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait for the page before probing")
    args = ap.parse_args()

    if not args.device:
        raise SystemExit("no device: pass --device <ip:5555> or set KIOSK_DEVICE")

    adb_path = find_adb(args.adb)
    adb(adb_path, args.device, "connect", args.device, check=False)

    pid = adb(adb_path, args.device, "shell", "pidof", PACKAGE, check=False).stdout.strip()
    if not pid:
        raise SystemExit("FAIL: %s is not running - start it with: adb shell am start -n %s/.MainActivity"
                         % (PACKAGE, PACKAGE))

    # Forward only the port we use, and take it away again on exit.
    adb(adb_path, args.device, "forward", "--remove", "tcp:%d" % PORT, check=False)
    adb(adb_path, args.device, "forward", "tcp:%d" % PORT,
        "localabstract:webview_devtools_remote_%s" % pid)
    os.environ["CDP_PORT"] = str(PORT)

    try:
        if not devtools_up():
            print("devtools closed (shipped build); opening a 10-minute window ...")
            if not open_debug_window(adb_path, args.device):
                raise SystemExit(
                    "FAIL: devtools still closed - build with tools\\build_apk.py --debug, "
                    "or press MENU three times on the remote")
        if args.wait:
            time.sleep(args.wait)
        # Enabling debugging rebuilds the WebView, so give the reloaded page a
        # moment to render its data before judging it.
        res = None
        deadline = time.time() + 30
        while time.time() < deadline:
            res = cdp.evaluate(PROBE)
            if isinstance(res, dict) and res.get("status") and (res.get("days") or 0) >= 5:
                break
            time.sleep(2)
    except Exception as e:
        raise SystemExit("FAIL: cannot reach the page over devtools: %s" % e)
    finally:
        adb(adb_path, args.device, "forward", "--remove", "tcp:%d" % PORT, check=False)

    if not isinstance(res, dict):
        raise SystemExit("FAIL: page returned %r (still booting?)" % (res,))

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))

    checks = [
        ("page renders", bool(res.get("status")), res.get("status")),
        ("no error status", not str(res.get("status") or "").startswith("ralat"), res.get("status")),
        ("forecast strip", (res.get("days") or 0) >= 5, "%s cards" % res.get("days")),
        ("temperature", bool(res.get("temp")), res.get("temp")),
        ("android bridge", bool((res.get("bridge") or {}).get("beat")), "beat()"),
        ("fits 1280x720", res.get("size") == [1280, 720], res.get("size")),
        ("nothing clipped", not res.get("clipped"), res.get("clipped")),
        ("no container overflow", not res.get("over"), res.get("over")),
    ]

    failed = 0
    for name, ok, detail in checks:
        print(("PASS  " if ok else "FAIL  ") + name + ": " + str(detail))
        if not ok:
            failed += 1

    print("-" * 52)
    if failed:
        print("%d check(s) failed" % failed)
        return 1
    print("kiosk healthy (pid %s, %s)" % (pid, res.get("fcSrc")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
