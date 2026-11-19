"""Layout audit: emulate a 1280x720 TV viewport, reload, and report metrics."""

import json
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp

EXPR = r"""(() => {
  const r = el => { const b = el.getBoundingClientRect();
    return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
  const clipped = [...document.querySelectorAll('*')].filter(e => {
    const b = e.getBoundingClientRect();
    return b.bottom > innerHeight + 1 || b.right > innerWidth + 1;
  }).map(e => e.tagName + '.' + e.className).slice(0, 8);
  const g = id => getComputedStyle(document.getElementById(id));
  return JSON.stringify({
    vp: [innerWidth, innerHeight],
    app: r(document.getElementById('app')),
    header: r(document.querySelector('header')),
    now: r(document.getElementById('now')),
    fc: r(document.getElementById('fc')),
    footer: r(document.querySelector('footer')),
    fontSize: { clock: g('clock').fontSize, temp: g('nowTemp').fontSize,
                place: g('placeName').fontSize, cond: g('nowCond').fontSize },
    clipped, days: document.querySelectorAll('.day').length,
    temp: document.getElementById('nowTemp').textContent,
    cond: document.getElementById('nowCond').textContent,
    warn: document.getElementById('warnTxt').textContent,
    status: document.getElementById('status').textContent
  });
})()"""


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/"
    ws = cdp.WS(cdp.page_target()["webSocketDebuggerUrl"])
    if os.environ.get("SKIP_NAV") != "1":
        ws.call("Emulation.setDeviceMetricsOverride",
                {"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": False})
        ws.call("Page.enable")
        ws.call("Page.navigate", {"url": url})
        time.sleep(9)
    res = ws.call("Runtime.evaluate", {"expression": EXPR, "returnByValue": True})
    if "exceptionDetails" in res:
        print("JS error:", res["exceptionDetails"])
        return
    print(json.dumps(json.loads(res["result"]["value"]), indent=1))


if __name__ == "__main__":
    main()
