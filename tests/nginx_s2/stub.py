#!/usr/bin/env python3
"""Synthetic upstream for the nginx S2 lab. Not one of the real services.

    python stub.py <name> <port>

* every ordinary request is answered with JSON that echoes what nginx forwarded:
  method, raw request target, ALL headers in order (duplicates kept), body length
  and sha256. Nothing in the answer depends on which nginx variant asked.
* `?pad=N` adds N bytes of padding (gzip tests); header `X-Stub-Status: NNN`
  forces the status (pass-through tests).
* a path ending in `/stream` answers text/event-stream, `n` events `delay` seconds
  apart (query `n`, `delay`), flushed one by one. Like search/app/server.py it adds
  `X-Accel-Buffering: no` only for `/v1/ask/stream`; any other stream path does not.
* `Upgrade: websocket` gets a real RFC 6455 handshake and echoes text frames.
"""
import base64
import hashlib
import json
import struct
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = sys.argv[1] if len(sys.argv) > 1 else "stub"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_exact(rfile, n):
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


class Handler(BaseHTTPRequestHandler):
    server_version = "stub"
    sys_version = ""

    def log_message(self, *args):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self):
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self._websocket()
        path = urllib.parse.urlsplit(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        body = self._body()
        if path.endswith("/stream") and self.command in ("GET", "POST"):
            return self._sse(path, query)
        out = {
            "stub": NAME, "port": PORT, "method": self.command, "target": self.path,
            "headers": [[k, v] for k, v in self.headers.items()],
            "body_len": len(body), "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        if "pad" in query:
            out["pad"] = "x" * int(query["pad"][0])
        data = json.dumps(out).encode()
        status = int(self.headers.get("X-Stub-Status") or 200)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _handle

    def _sse(self, path, query):
        n = int(query.get("n", ["5"])[0])
        delay = float(query.get("delay", ["0.5"])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        if path.endswith("/v1/ask/stream"):
            self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.flush()
        for i in range(n):
            time.sleep(delay)
            self.wfile.write(f"data: {i}\n\n".encode())
            self.wfile.flush()

    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        try:
            while True:
                b1, b2 = _read_exact(self.rfile, 2)
                opcode, masked, length = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", _read_exact(self.rfile, 2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", _read_exact(self.rfile, 8))[0]
                mask = _read_exact(self.rfile, 4) if masked else b"\0\0\0\0"
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(_read_exact(self.rfile, length)))
                if opcode == 0x8:
                    self.wfile.write(b"\x88\x00")
                    self.wfile.flush()
                    return
                if opcode == 0x1:
                    reply = ("echo:" + payload.decode()).encode()
                    self.wfile.write(bytes([0x81, len(reply)]) + reply)
                    self.wfile.flush()
        except EOFError:
            return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
