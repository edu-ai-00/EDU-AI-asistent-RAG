#!/usr/bin/env python3
"""HTTP client that runs INSIDE the lab network (copied into a client container).

    python client.py <mode>      parameters as JSON on stdin, result as JSON on stdout

modes: cases   a list of requests, full responses back (status, ordered headers,
                body in base64 — nothing is truncated, redirects are not followed)
       burst   N rapid requests, the status of each
       series  timed samples while the harness changes the upstream; the harness
               sends `MARK <label>` / `STOP` lines on stdin
       sse     one streaming request, arrival time of each event
       ws      one real WebSocket handshake and one echoed frame

A transport failure is reported as {"error": ...}. It is never turned into a status.
"""
import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
import threading
import time

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _b64(raw):
    return base64.b64encode(raw).decode()


def request(c):
    t0 = time.time()
    try:
        conn = http.client.HTTPConnection(c["target"], c["port"], timeout=c.get("timeout", 20))
        conn.putrequest(c["method"], c["path"], skip_host=True, skip_accept_encoding=True)
        for k, v in c.get("headers", []):
            conn.putheader(k, v)
        body = c.get("body")
        data = b""
        if body:
            data = (body.get("fill", "a") * body["len"]).encode()
            conn.putheader("Content-Length", str(body.get("declare_len", body["len"])))
        conn.endheaders()
        if data:
            try:
                conn.send(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the server may answer (413) before the body is sent; read it
        resp = conn.getresponse()
        raw = resp.read()
        out = {"id": c.get("id"), "status": resp.status, "reason": resp.reason,
               "headers": [[k, v] for k, v in resp.getheaders()],
               "body_b64": _b64(raw), "body_len": len(raw), "elapsed": round(time.time() - t0, 3)}
        conn.close()
        return out
    except Exception as e:  # noqa: BLE001 — reported, never swallowed into a status
        return {"id": c.get("id"), "error": f"{type(e).__name__}: {e}", "elapsed": round(time.time() - t0, 3)}


def envelope_code(res):
    try:
        return json.loads(base64.b64decode(res["body_b64"]))["error"]["code"]
    except Exception:  # noqa: BLE001
        return None


def mode_cases(p):
    out = []
    for c in p["cases"]:
        out.append(request(c))
        time.sleep(p.get("pace", 0))   # stay under the 10 r/s limit_req zone
    return out


def mode_burst(p):
    statuses, first429 = [], None
    for i in range(p["n"]):
        r = request({"target": p["target"], "port": p["port"], "method": "GET", "path": p["path"],
                     "headers": p.get("headers", []), "timeout": 10})
        statuses.append(r.get("status") if "error" not in r else "ERR:" + r["error"])
        if first429 is None and r.get("status") == 429:
            first429 = {"index": i, "body_b64": r["body_b64"], "headers": r["headers"]}
    return {"statuses": statuses, "first429": first429}


def mode_series(p):
    t0 = time.time()
    samples, marks, lock, threads = [], [], threading.Lock(), []
    stop = threading.Event()

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if line == "STOP":
                stop.set()
                return
            if line.startswith("MARK "):
                with lock:
                    marks.append({"label": line[5:], "t": round(time.time() - t0, 3)})
        stop.set()

    def sample(path, t_start):
        r = request({"target": p["target"], "port": p["port"], "method": "GET", "path": path,
                     "headers": p.get("headers", []), "timeout": p.get("timeout", 8)})
        with lock:
            samples.append({"path": path, "t": round(t_start, 3), "elapsed": r["elapsed"],
                            "status": r.get("status"), "code": envelope_code(r) if "error" not in r else None,
                            "error": r.get("error")})

    threading.Thread(target=reader, daemon=True).start()
    paths = [p["path"]] + p.get("extra_paths", [])
    while not stop.is_set() and time.time() - t0 < p.get("max_seconds", 120):
        t_start = time.time() - t0
        for path in paths:
            th = threading.Thread(target=sample, args=(path, t_start))
            th.start()
            threads.append(th)
        time.sleep(p.get("interval", 0.25))
    for th in threads:
        th.join(p.get("timeout", 8) + 2)
    return {"samples": sorted(samples, key=lambda s: (s["t"], s["path"])), "marks": marks}


def mode_sse(p):
    t0 = time.time()
    try:
        conn = http.client.HTTPConnection(p["target"], p["port"], timeout=30)
        conn.putrequest(p["method"], p["path"], skip_host=True, skip_accept_encoding=True)
        for k, v in p.get("headers", []):
            conn.putheader(k, v)
        conn.putheader("Content-Length", "0")
        conn.endheaders()
        resp = conn.getresponse()
        t_headers = time.time() - t0
        events = []
        while True:
            line = resp.readline()   # HTTPResponse de-chunks; resp.fp would block on the open keep-alive connection
            if not line:
                break
            if line.startswith(b"data:"):
                events.append({"t": round(time.time() - t0, 3), "data": line[5:].strip().decode()})
        total = time.time() - t0
        return {"status": resp.status, "headers": [[k, v] for k, v in resp.getheaders()],
                "t_headers": round(t_headers, 3), "events": events, "total": round(total, 3)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def mode_ws(p):
    try:
        s = socket.create_connection((p["target"], p["port"]), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {p['path']} HTTP/1.1\r\nHost: edu.example\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise EOFError("closed during handshake")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        headers = {k.lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:])}
        want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        msg = b"hello"
        mask = os.urandom(4)
        s.sendall(bytes([0x81, 0x80 | len(msg)]) + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(msg)))
        data = rest
        while len(data) < 2 or len(data) < 2 + (data[1] & 0x7F):
            chunk = s.recv(4096)
            if not chunk:
                raise EOFError("closed before echo")
            data += chunk
        echo = data[2:2 + (data[1] & 0x7F)].decode()
        s.sendall(b"\x88\x80" + os.urandom(4))
        s.close()
        return {"status_line": lines[0], "accept_ok": headers.get("sec-websocket-accept") == want,
                "upgrade": headers.get("upgrade"), "connection": headers.get("connection"), "echo": echo}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    mode = sys.argv[1]
    params = json.loads(sys.stdin.readline())
    result = {"cases": mode_cases, "burst": mode_burst, "series": mode_series,
              "sse": mode_sse, "ws": mode_ws}[mode](params)
    sys.stdout.write(json.dumps(result))
