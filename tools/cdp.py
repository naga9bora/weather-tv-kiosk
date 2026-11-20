"""Minimal Chrome DevTools Protocol client (stdlib only).

Lets us inspect/control the page running in the TV's Chrome:

    python tools/cdp.py targets
    python tools/cdp.py eval "document.body.innerText"
    python tools/cdp.py navigate "http://127.0.0.1:8080/"

Requires: adb forward tcp:9222 localabstract:chrome_devtools_remote
"""

import base64
import json
import os
import socket
import struct
import sys
import urllib.request


def _ws_connect(ws_url: str):
    assert ws_url.startswith("ws://")
    hostport, _, path = ws_url[5:].partition("/")
    path = "/" + path
    host, _, port = hostport.partition(":")
    port = int(port or 80)

    s = socket.create_connection((host, port), timeout=30)
    s.settimeout(20)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("handshake failed")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError("handshake rejected: " + head.decode(errors="replace"))
    return s, rest


class WS:
    def __init__(self, url):
        self.sock, self.buf = _ws_connect(url)

    def send(self, text: str):
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        b1, b2 = self._read(2)
        opcode = b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        masked = b2 & 0x80
        mask = self._read(4) if masked else None
        data = self._read(n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:
            raise RuntimeError("closed by peer")
        if opcode == 0x9:  # ping
            self.sock.sendall(b"\x8a\x80" + os.urandom(4))
            return self.recv()
        return data.decode()

    def call(self, method, params=None, _id=[0]):
        _id[0] += 1
        mid = _id[0]
        self.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})


def targets(timeout=15):
    port = os.environ.get("CDP_PORT", "9222")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=timeout) as r:
        return json.load(r)


def page_target(timeout=15):
    ts = [t for t in targets(timeout) if t.get("type") == "page"]
    if not ts:
        raise SystemExit("no page target; is Chrome running?")
    # Prefer the dashboard served over http; skip dead file:// error tabs.
    # Any http origin works: local preview, a LAN host, or the kiosk's fake
    # origin (http://weather.local).
    for t in ts:
        u = t.get("url") or ""
        if u.startswith("http://") and ("8080" in u or "weather" in u):
            return t
    for t in ts:
        if "weather" in (t.get("url") or ""):
            return t
    return ts[0]


def connect():
    return WS(page_target()["webSocketDebuggerUrl"])


def evaluate(expr):
    ws = connect()
    res = ws.call("Runtime.evaluate", {
        "expression": expr, "returnByValue": True, "awaitPromise": True,
    })
    if "exceptionDetails" in res:
        return {"exception": res["exceptionDetails"]}
    return res.get("result", {}).get("value")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "targets"
    if cmd == "targets":
        for t in targets():
            print(t.get("type"), "|", t.get("title"), "|", t.get("url"))
    elif cmd == "eval":
        print(json.dumps(evaluate(" ".join(sys.argv[2:])), indent=2, ensure_ascii=False))
    elif cmd == "navigate":
        ws = connect()
        ws.call("Page.enable")
        ws.call("Page.navigate", {"url": sys.argv[2]})
        print("navigated")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
