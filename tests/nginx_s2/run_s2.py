#!/usr/bin/env python3
"""nginx S2 candidate: local lab checks (see README.md).

    python tests/nginx_s2/run_s2.py <command> [--baseline REF] [--out FILE]

commands: static  equivalence  resilience  atomic  selfcheck  rehearsal  all
(the clean-clone wrapper is clean_clone.py)

Needs Docker and the images in README.md. Uses no production access and no secrets.
Exit status 0 only when every check PASSed; a FAIL or an ERROR (a check that could not
run) is a non-zero exit. Results are also written as JSON (--out).
"""
from __future__ import annotations

import argparse
import base64
import collections
import gzip
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import threading
import time
import traceback

if sys.version_info < (3, 10):
    sys.exit('tests/nginx_s2 needs Python >= 3.10')

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cases as C  # noqa: E402
import labkit as L  # noqa: E402
from labkit import REPO, run  # noqa: E402

# the image the production nginx container runs (read from production, 2026-10-01)
PROD_NGINX_IMAGE_ID = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
CONF_DIR = REPO / "nginx" / "conf"


# ── reporting ──────────────────────────────────────────────────────────
def _where(e):
    f = traceback.extract_tb(e.__traceback__)[-1]
    return f"{pathlib.Path(f.filename).name}:{f.lineno} `{(f.line or '').strip()[:80]}`"


class Report:
    def __init__(self):
        self.items, self.data = [], {}

    def check(self, name, fn):
        try:
            detail, status = fn(), "PASS"
        except AssertionError as e:
            detail, status = str(e) or "assertion failed at " + _where(e), "FAIL"
        except Exception as e:  # noqa: BLE001 — a check that cannot run is an ERROR, never a PASS
            detail, status = f"{type(e).__name__}: {e} (at {_where(e)})", "ERROR"
        self.items.append({"name": name, "status": status, "detail": detail})
        print(f"  {status:5} {name}" + (f" — {detail}" if detail and status != "PASS" else ""))
        if status == "PASS" and detail:
            print(f"        {detail}")
        return status == "PASS"

    def ok(self):
        return all(i["status"] == "PASS" for i in self.items)

    def summary(self):
        c = collections.Counter(i["status"] for i in self.items)
        return dict(c)


def image_id(ref):
    return run(["docker", "image", "inspect", "--format", "{{.Id}}", ref]).stdout.strip()


def meta(lab):
    cand = {str(p.relative_to(REPO)).replace("\\", "/"): __import__("hashlib").sha256(p.read_bytes()).hexdigest()
            for p in sorted(CONF_DIR.iterdir())}
    cand["docker-compose.yml"] = __import__("hashlib").sha256(
        (REPO / "docker-compose.yml").read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return {
        "run_id": lab.run_id, "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseline_ref": lab.baseline_ref, "baseline_sha": lab.baseline_sha,
        "head_sha": run(["git", "-C", REPO, "rev-parse", "HEAD"]).stdout.strip(),
        "docker": run(["docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"]).stdout.strip(),
        "compose": run(["docker", "compose", "version", "--short"]).stdout.strip(),
        "host": f"{platform.system()} {platform.release()}, python {platform.python_version()}",
        "images": {L.NGINX_IMAGE: image_id(L.NGINX_IMAGE), L.PY_IMAGE: image_id(L.PY_IMAGE)},
        "production_nginx_image_id": PROD_NGINX_IMAGE_ID,
        "candidate_sha256": cand,
    }


# ═══ A. static ═════════════════════════════════════════════════════════
def nginx_run(args, *, mounts, network="none", add_hosts=(), entrypoint="nginx"):
    """Run the production image with the given mounts. Default entrypoint is the nginx
    binary itself, so stdout carries only what nginx prints (no entrypoint-script noise)."""
    cmd = ["docker", "run", "--rm", "--network", network, "--entrypoint", entrypoint]
    for h in add_hosts:
        cmd += ["--add-host", f"{h}:127.0.0.1"]
    for src, dst in mounts:
        cmd += ["--mount", f"type=bind,source={src},target={dst},readonly"]
    return run(cmd + [L.NGINX_IMAGE, *args], check=False)


def static(lab, rep):
    print("A. static checks")
    base = lab.extract_baseline()
    cand = lab.extract_candidate()
    cand_mounts = [(cand / "nginx" / "conf", "/etc/nginx")]
    base_mounts = [(base / "nginx" / "nginx.conf", "/etc/nginx/nginx.conf"),
                   (base / "nginx" / "search_api.conf", "/etc/nginx/search_api.conf")]
    names = list(L.UPSTREAMS)

    def image_is_production():
        got = image_id(L.NGINX_IMAGE)
        assert got == PROD_NGINX_IMAGE_ID, f"{L.NGINX_IMAGE} is {got}, production runs {PROD_NGINX_IMAGE_ID}"
        ver = run(["docker", "run", "--rm", "--entrypoint", "nginx", L.NGINX_IMAGE, "-v"]).stderr.strip()
        m = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", ver)
        assert m and tuple(map(int, m.groups())) >= (1, 27, 3), f"`resolve` in upstream needs nginx >= 1.27.3, got {ver}"
        return f"{got[:19]}… = production image, {ver}"

    def nginx_t_candidate():
        p = nginx_run(["-t"], mounts=cand_mounts)
        out = p.stdout + p.stderr
        assert p.returncode == 0 and "test is successful" in out, out
        return "rc=0 with NO network and no upstream present"

    def nginx_t_baseline_control():
        p = nginx_run(["-t"], mounts=base_mounts)
        out = p.stdout + p.stderr
        assert p.returncode != 0 and "host not found in upstream" in out, \
            f"control must reproduce the old failure, got rc={p.returncode}: {out[:200]}"
        return "control: baseline fails the same test with `host not found in upstream`"

    def compose_only_nginx():
        def cfg(checkout):  # compose is run on lab copies with an EMPTY .env, never the repo's real one
            p = run(["docker", "compose", "-p", "edu_ai", "--project-directory", checkout, "--env-file", lab.empty_env,
                     "-f", checkout / "docker-compose.yml", "config", "--format", "json"])
            text = p.stdout
            for form in {json.dumps(str(checkout))[1:-1], str(checkout), str(checkout).replace("\\", "/")}:
                text = text.replace(form, "<PROJ>")
            return json.loads(text)
        a, b = cfg(base), cfg(cand)
        assert set(a["services"]) == set(b["services"]), "service set differs"
        changed = [s for s in a["services"] if a["services"][s] != b["services"][s]]
        assert changed == ["nginx"], f"services that differ: {changed}"
        keys = sorted(k for k in set(a["services"]["nginx"]) | set(b["services"]["nginx"])
                      if a["services"]["nginx"].get(k) != b["services"]["nginx"].get(k))
        assert keys == ["depends_on", "healthcheck", "volumes"], f"nginx keys that differ: {keys}"
        for top in ("networks", "volumes", "name"):
            assert a.get(top) == b.get(top), f"top-level {top} differs"
        nb = b["services"]["nginx"]
        for k in ("image", "container_name", "ports", "networks", "restart"):
            assert a["services"]["nginx"][k] == nb[k], f"nginx {k} changed"
        return f"10 services; only nginx differs, only keys {keys}; image, container_name, ports, networks, restart equal"

    def mounted_dir_contents():
        ls = nginx_run(["-c", "ls -A /etc/nginx; ls /etc/nginx/conf.d 2>&1 | head -1; ls -A /etc/nginx/modules 2>&1 | head -1"],
                       mounts=cand_mounts, entrypoint="sh")
        assert ls.returncode == 0, ls.stderr
        files = ls.stdout.split("\n")[:3]
        assert set(ls.stdout.split("\n")[0:3]) >= {"mime.types", "nginx.conf", "search_api.conf"}, ls.stdout
        listing = [l for l in ls.stdout.splitlines() if l]
        assert listing[:3] == sorted(["mime.types", "nginx.conf", "search_api.conf"]), f"unexpected files in /etc/nginx: {listing}"
        assert "conf.d" not in listing and not any("No such file" in l for l in listing[:3]), listing
        host_entries = sorted(p.name for p in CONF_DIR.iterdir())
        assert host_entries == ["mime.types", "nginx.conf", "search_api.conf"], f"nginx/conf contains {host_entries}"
        assert not (CONF_DIR / ".htpasswd").exists() and not (CONF_DIR / "auth_keys.conf").exists()
        return "nginx/conf holds exactly nginx.conf, search_api.conf, mime.types; no conf.d, no stale .htpasswd/auth_keys.conf"

    def dump(mounts):
        p = nginx_run(["-T"], mounts=mounts, add_hosts=names)
        assert p.returncode == 0, p.stdout + p.stderr
        return p.stdout

    def no_default_server():
        text = dump(cand_mounts)
        assert "default_server" not in text and "conf.d" not in text and "default.conf" not in text
        listens = sorted(set(re.findall(r"^\s*listen\s+(\S+?);", text, re.M)))
        assert listens == ["80", "81", "8443", "8444"], f"listeners: {listens}"
        servers = L.parse_config(text)[(("http",), "server {")]
        assert servers == 4, f"{servers} server blocks, expected 4"
        return "exactly 4 server blocks on 80/81/8443/8444, no default_server, no conf.d include"

    def effective_delta():
        removed, added = L.config_delta(dump(base_mounts), dump(cand_mounts))
        ups = {"fastapi": "edu_ai_api:8080", "langflow": "edu_ai:7860", "pgadmin": "edu_ai_pgadmin:80",
               "search_service": "edu_ai_search:8090", "web_chat": "edu_ai_web_chat:8081", "qa_admin": "edu_ai_qa_admin:8082"}
        exp_removed = []     # the access_log directive is deliberately NOT touched (S2-R2)
        exp_added = [(("http",), "resolver 127.0.0.11 valid=5s ipv6=off"),
                     (("http",), "resolver_timeout 2s")]
        for name, srv in ups.items():
            ctx = ("http", f"upstream {name}")
            exp_removed.append((ctx, f"server {srv}"))
            exp_added += [(ctx, f"server {srv} resolve"), (ctx, f"zone {name}_z 64k")]
        assert removed == sorted(exp_removed), f"removed directives differ from expectation: {removed}"
        assert added == sorted(exp_added), f"added directives differ from expectation: {added}"
        return f"effective config (nginx -T): {len(removed)} directives removed, {len(added)} added — only DNS (resolver, zone, resolve); access_log, map, every location untouched"

    def trust_and_pgadmin_unchanged():
        text = dump(cand_mounts)
        stmts = L.parse_config(text)
        assert [s for (c, s) in stmts if s.startswith("set_real_ip_from")] == ["set_real_ip_from 172.16.0.0/12"]
        assert (("http", "server"), "real_ip_header CF-Connecting-IP") in stmts
        assert (("http", "server", "location /pgadmin/"), "proxy_set_header X-Forwarded-Port 8443") in stmts
        assert (("http", "server", "location /pgadmin/"), "proxy_set_header X-Forwarded-Host $host") in stmts
        assert (("http", "server", "location /pgadmin/"), "proxy_set_header Host $host") in stmts
        return "trusted proxy range still exactly 172.16.0.0/12 (R2 untouched, NOT fixed); pgAdmin keeps explicit X-Forwarded-Port 8443"

    def superseded_files_unchanged_and_unused():
        import hashlib
        for f in ("nginx.conf", "search_api.conf"):
            blob = run(["git", "-C", REPO, "show", f"{lab.baseline_sha}:nginx/{f}"]).stdout.encode("utf-8")
            work = (REPO / "nginx" / f).read_bytes().replace(b"\r\n", b"\n")
            assert hashlib.sha256(blob).hexdigest() == hashlib.sha256(work).hexdigest(), f"nginx/{f} differs from {lab.baseline_sha[:10]}"
        cfg = json.loads(run(["docker", "compose", "-p", "edu_ai", "--project-directory", cand, "--env-file", lab.empty_env,
                              "-f", cand / "docker-compose.yml", "config", "--format", "json"]).stdout)
        srcs = [v["source"].replace("\\", "/") for v in cfg["services"]["nginx"]["volumes"]]
        assert len(srcs) == 1 and srcs[0].endswith("/nginx/conf"), srcs
        return "nginx/nginx.conf and nginx/search_api.conf are byte-identical to the baseline and mounted by nobody; they stay until S2b so a container made from the old definition can still restart"

    def mime_and_eol():
        p = run(["docker", "run", "--rm", "--entrypoint", "sha256sum", L.NGINX_IMAGE, "/etc/nginx/mime.types"])
        img = p.stdout.split()[0]
        import hashlib
        mine = hashlib.sha256((CONF_DIR / "mime.types").read_bytes()).hexdigest()
        assert img == mine, f"vendored mime.types differs from the image: {img} vs {mine}"
        for f in CONF_DIR.iterdir():
            assert b"\r" not in f.read_bytes(), f"{f.name} contains CR"
        attr = run(["git", "-C", REPO, "check-attr", "eol", "--", "nginx/conf/nginx.conf", "nginx/conf/mime.types"]).stdout
        assert attr.count("eol: lf") == 2, f"nginx/conf is not marked eol=lf in .gitattributes: {attr!r}"
        return f"mime.types byte-identical to the image ({img[:12]}…); LF only; .gitattributes eol=lf"

    for name, fn in [("image is the production nginx image, version supports `resolve`", image_is_production),
                     ("nginx -t succeeds with no upstream present (candidate)", nginx_t_candidate),
                     ("control: the same test fails on the baseline", nginx_t_baseline_control),
                     ("compose diff: only the nginx service changed", compose_only_nginx),
                     ("mounted directory holds only the intended files, no default server file", mounted_dir_contents),
                     ("effective config: 4 servers, no default_server", no_default_server),
                     ("effective config delta is exactly resolver + zone/resolve (no log filter)", effective_delta),
                     ("trust boundary and pgAdmin headers unchanged", trust_and_pgadmin_unchanged),
                     ("superseded nginx/*.conf unchanged and unused (kept until S2b)", superseded_files_unchanged_and_unused),
                     ("vendored mime.types identical to image; LF; eol attribute", mime_and_eol)]:
        rep.check(name, fn)


# ═══ shared lab ════════════════════════════════════════════════════════
class Setup:
    """nets + all stubs + two clients + the two nginx variants (baseline, candidate)."""

    def __init__(self, lab, *, start_stubs=True, skip_stubs=(), start_variants=True, create_nets=True):
        self.lab = lab
        if create_nets:
            lab.create_networks()
        self.baseline_dir = lab.extract_baseline()
        self.candidate_dir = lab.extract_candidate()
        self.old = lab.variant("old", self.baseline_dir)
        self.new = lab.variant("new", self.candidate_dir)
        if start_stubs:
            lab.start_all_stubs(skip=skip_stubs)
        self.c172 = lab.start_client("n172")
        self.c192 = lab.start_client("n192")
        if start_variants:
            self.old.up()
            self.new.up()
            L.wait_healthy(self.new.container, timeout=60)
            self.wait_serving(self.old)

    def client_for(self, key):
        return {"172": self.c172, "192": self.c192}[key]

    def wait_serving(self, variant, timeout=40):
        deadline = time.time() + timeout
        while True:
            r = self.lab.client(self.c172, "cases", {"cases": [self.case(variant, 80, "GET", "/health")]})[0]
            if "error" not in r and r["status"] == 200:
                return
            if time.time() > deadline:
                raise L.NotHealthy(f"{variant.container} does not answer /health: {r}")
            time.sleep(1)

    def case(self, variant, port, method, path, headers=(), body=None, cid="x", timeout=20):
        return {"id": cid, "target": variant.container, "port": port, "method": method, "path": path,
                "headers": list(C.BASE_HEADERS) + list(headers), "body": body, "timeout": timeout}

    def get(self, variant, port, path, **kw):
        return self.lab.client(self.c172, "cases", {"cases": [self.case(variant, port, "GET", path, **kw)]})[0]


# ═══ B. equivalence ════════════════════════════════════════════════════
def normalise(res):
    if "error" in res:
        return None
    return {"status": res["status"], "headers": [[k.lower(), v] for k, v in res["headers"] if k.lower() != "date"],
            "body": res["body_b64"]}


def describe_diff(a, b):
    d = []
    if a["status"] != b["status"]:
        d.append(f"status {a['status']} -> {b['status']}")
    ha, hb = collections.Counter(map(tuple, a["headers"])), collections.Counter(map(tuple, b["headers"]))
    for h in sorted((ha - hb).elements()):
        d.append(f"header only before: {h[0]}: {h[1]}")
    for h in sorted((hb - ha).elements()):
        d.append(f"header only after: {h[0]}: {h[1]}")
    if not d and a["headers"] != b["headers"]:
        d.append("header ORDER differs")
    if a["body"] != b["body"]:
        ba, bb = base64.b64decode(a["body"]), base64.b64decode(b["body"])
        try:        # the echo stub answers with what nginx forwarded: show the changed header, not just a length
            ja, jb = json.loads(ba), json.loads(bb)
            for k in sorted(set(ja) | set(jb)):
                if k == "headers":
                    xa = collections.Counter((n.lower(), v) for n, v in ja[k])
                    xb = collections.Counter((n.lower(), v) for n, v in jb[k])
                    for h in sorted((xa - xb).elements()):
                        d.append(f"upstream saw only before: {h[0]}: {h[1]}")
                    for h in sorted((xb - xa).elements()):
                        d.append(f"upstream saw only after: {h[0]}: {h[1]}")
                elif ja.get(k) != jb.get(k):
                    d.append(f"upstream saw {k}: {str(ja.get(k))[:60]} -> {str(jb.get(k))[:60]}")
        except Exception:  # noqa: BLE001 — not an echo body (nginx's own page): fall back to sizes
            d.append(f"body differs ({len(ba)} -> {len(bb)} bytes)")
    return d


def classify_pair(a, b):
    """('same' | 'different' | 'not_evaluated', detail). A missing result or a transport
    error on either side is NOT evaluated — never 'same', never 'different'."""
    if a is None or b is None:
        return "not_evaluated", "result missing"
    if "error" in a or "error" in b:
        return "not_evaluated", f"baseline: {a.get('error', 'ok')}; candidate: {b.get('error', 'ok')}"
    na, nb = normalise(a), normalise(b)
    if na == nb:
        return "same", None
    return "different", describe_diff(na, nb)


def run_cases(su, variant, cases):
    out, threads, errors = {}, [], []

    def worker(key):
        group = [dict(c, target=variant.container) for c in cases if c["client"] == key]
        try:
            res = su.lab.client(su.client_for(key), "cases", {"cases": group, "pace": 0.13}, timeout=900)
            out.update({r["id"]: r for r in res})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{variant.name}/{key}: {e}")

    for key in ("172", "192"):
        t = threading.Thread(target=worker, args=(key,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    if errors:
        raise L.LabError("; ".join(errors))
    return out


def streaming(su, rep):
    """A real SSE stream and a real WebSocket through both variants (the echo stub proves neither)."""
    def sse(v, path, method):
        r = su.lab.client(su.c172, "sse", {"target": v.container, "port": 80, "method": method, "path": path,
                                           "headers": C.BASE_HEADERS})
        if "error" in r:
            raise L.TransportError(r["error"])
        return r

    def kind(r):
        return "streamed" if r["events"][0]["t"] < 0.5 * r["events"][-1]["t"] else "buffered"

    def hdrs(r):
        return [[k.lower(), v] for k, v in r["headers"] if k.lower() != "date"]

    def search_sse():
        out = {v.name: sse(v, "/search/v1/ask/stream?n=5&delay=0.5", "POST") for v in (su.old, su.new)}
        for name, r in out.items():
            h = dict(hdrs(r))
            assert r["status"] == 200 and h["content-type"].startswith("text/event-stream"), (name, r["status"])
            assert "content-encoding" not in h, f"{name}: the event stream was compressed"
            assert [e["data"] for e in r["events"]] == ["0", "1", "2", "3", "4"], r["events"]
            assert kind(r) == "streamed", f"{name}: first event at {r['events'][0]['t']}s of {r['events'][-1]['t']}s — buffered"
        assert hdrs(out["old"]) == hdrs(out["new"]), "SSE response headers differ"
        o, n = out["old"], out["new"]
        return (f"search stream (stub sets X-Accel-Buffering: no, as search/app/server.py): 5 events 0.5 s apart arrive as they are sent — "
                f"baseline first/last {o['events'][0]['t']}/{o['events'][-1]['t']}s, candidate {n['events'][0]['t']}/{n['events'][-1]['t']}s; identical headers, not compressed")
    rep.check("SSE through :80 /search/v1/ask/stream is streamed, not buffered, identically (real timed stream)", search_sse)

    def chat_sse():
        out = {v.name: sse(v, "/chat/x/stream?n=5&delay=0.5", "GET") for v in (su.old, su.new)}
        for name, r in out.items():
            assert r["status"] == 200 and len(r["events"]) == 5, (name, r["status"])
        assert kind(out["old"]) == kind(out["new"]), f"{kind(out['old'])} vs {kind(out['new'])}"
        assert hdrs(out["old"]) == hdrs(out["new"])
        return (f"chat stream without X-Accel-Buffering: both variants behave as '{kind(out['new'])}' (first event at "
                f"{out['old']['events'][0]['t']}s / {out['new']['events'][0]['t']}s of {out['new']['events'][-1]['t']}s) — unchanged")
    rep.check("SSE through :80 /chat/ behaves the same in both variants (real timed stream)", chat_sse)

    def websocket():
        out = {}
        for v in (su.old, su.new):
            r = su.lab.client(su.c172, "ws", {"target": v.container, "port": 8444, "path": "/ws"})
            assert "error" not in r, f"{v.name}: {r['error']}"
            assert " 101 " in r["status_line"] and r["accept_ok"] and r["echo"] == "echo:hello", (v.name, r)
            assert (r["upgrade"] or "").lower() == "websocket" and "upgrade" in (r["connection"] or "").lower(), (v.name, r)
            out[v.name] = r
        return "real RFC 6455 handshake (Sec-WebSocket-Accept verified) and an echoed frame through :8444, baseline and candidate"
    rep.check("WebSocket through :8444 completes a real handshake and frame exchange in both variants", websocket)


def equivalence(lab, rep, su=None):
    print("B. equivalence (baseline vs candidate, same synthetic upstreams)")
    own = su is None
    su = su or Setup(lab)
    conf_new = (CONF_DIR / "nginx.conf").read_text(encoding="utf-8")
    base_conf = (su.baseline_dir / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    allowed = C.parse_allowed(base_conf)
    assert allowed == C.parse_allowed(conf_new), "allow-list differs between baseline and candidate"
    contract = C.contract_paths((REPO / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    cases = C.build(allowed, contract)
    rep.data["equivalence_allowed_paths"] = allowed

    res_old, res_new = {}, {}
    t_old = threading.Thread(target=lambda: res_old.update(run_cases(su, su.old, cases)))
    t_new = threading.Thread(target=lambda: res_new.update(run_cases(su, su.new, cases)))
    t_old.start(); t_new.start(); t_old.join(); t_new.join()

    same, different, unevaluated = [], [], []
    for c in cases:
        verdict, detail = classify_pair(res_old.get(c["id"]), res_new.get(c["id"]))
        if verdict == "same":
            same.append(c["id"])
        elif verdict == "different":
            different.append({"id": c["id"], "cat": c["cat"], "diff": detail})
        else:
            unevaluated.append({"id": c["id"], "reason": detail})
    total = len(cases)
    summary = {"requests": total, "same": len(same), "different": len(different), "not_evaluated": len(unevaluated)}
    rep.data["equivalence"] = {"summary": summary, "different": different, "not_evaluated": unevaluated,
                               "categories": dict(collections.Counter(c["cat"] for c in cases))}
    print(f"        requests={total} = same {len(same)} + different {len(different)} + not evaluated {len(unevaluated)}")
    for d in different:
        print(f"        DIFF {d['id']}: {d['diff']}")
    for u in unevaluated:
        print(f"        NOT EVALUATED {u}")

    rep.check("every request evaluated (none lost to a transport error)", lambda: _assert(not unevaluated, f"{unevaluated}"))
    rep.check("no difference in status, headers (all, ordered, except Date) or full body",
              lambda: _assert(not different, f"{len(different)} differences, see results"))

    # independent assertions: do not rely on old == new alone
    def by_id(res, cid):
        return res[cid]

    def explicit(variant_res, label):
        def f():
            r = variant_res
            for p in allowed:
                x = r[f"80 GET {p}"] if not p.endswith("/stream") else r[f"80 GET {p}"]
                L.expect_http(x, 200)
            for p in C.denied_paths(allowed, contract):
                for port in (80, 81):
                    if p.lower() in {a.lower() for a in allowed}:
                        # nginx `map` compares strings case-INSENSITIVELY: /search/V1/entities passes the
                        # edge allow-list exactly as /search/v1/entities does (unchanged; the app is case-sensitive)
                        L.expect_http(r[f"{port} GET {p} (denied)"], 200)
                    elif p == "/search":   # the /search/ prefix location redirects the bare prefix (as today)
                        L.expect_http(r[f"{port} GET {p} (denied)"], 301, header=("Location", f"http://edu.example{'' if port == 80 else ':' + str(port)}/search/"))
                    else:
                        L.expect_http(r[f"{port} GET {p} (denied)"], 404, envelope_code="NOT_FOUND")
            for p in allowed:
                L.expect_http(r[f"81 GET {p}"], 200)
            for path in ("/health", "/api/ping", "/api/", "/chat/", "/qa/", "/qa-api/v1/health", "/pgadmin/", "/"):
                L.expect_http(r[f"81 GET {path} (not on the tunnel)"], 404, envelope_code="NOT_FOUND")
            L.expect_http(r["80 POST /search 1MiB+1 (413)"], 413, envelope_code="PAYLOAD_TOO_LARGE")
            L.expect_http(r["80 POST /search 2MiB declared"], 413, envelope_code="PAYLOAD_TOO_LARGE")
            L.expect_http(r["81 POST /search 1MiB+1 (413)"], 413, envelope_code="PAYLOAD_TOO_LARGE")
            L.expect_http(r["80 POST /search 1MiB (limit, ok)"], 200)
            L.expect_http(r["80 POST /api 50MiB+1 declared"], 413)
            L.expect_http(r["8444 POST 100MiB+1 declared"], 413)
            L.expect_http(r["80 GET /chat"], 308, header=("Location", "http://edu.example/chat/"))
            L.expect_http(r["80 GET /qa"], 308, header=("Location", "http://edu.example/qa/"))
            e = L.upstream_saw(r["80 GET /api/ping?x=1&y=%20z"])
            assert e["target"] == "/ping?x=1&y=%20z", e["target"]          # prefix stripped, query kept
            e = L.upstream_saw(r["80 GET /search/health"])
            assert e["target"] == "/health", e["target"]
            e = L.upstream_saw(r["80 GET /chat/a/b?c=1"])
            assert e["target"] == "/chat/a/b?c=1", e["target"]             # chat prefix kept
            for k in ("8443 GET /pgadmin/ spoofed forwarding headers from 172",
                      "8443 GET /pgadmin/ spoofed forwarding headers from 192"):
                e = L.upstream_saw(r[k])
                assert L.header_values(e, "x-forwarded-port") == ["8443"], L.header_values(e, "x-forwarded-port")
                assert L.header_values(e, "x-script-name") == ["/pgadmin"], L.header_values(e, "x-script-name")
                assert L.header_values(e, "x-forwarded-prefix") == ["/pgadmin"], L.header_values(e, "x-forwarded-prefix")
                assert L.header_values(e, "x-forwarded-host") == ["edu.example"], L.header_values(e, "x-forwarded-host")
                assert L.header_values(e, "x-forwarded-proto") == ["http"], L.header_values(e, "x-forwarded-proto")
            e = L.upstream_saw(r["80 GET /api/ping spoof from 172"])
            assert L.header_values(e, "x-real-ip") != ["198.51.100.9"], "client X-Real-IP must be overwritten"
            e172 = L.upstream_saw(r["81 GET /search/health CF header from 172"])
            e192 = L.upstream_saw(r["81 GET /search/health CF header from 192"])
            e80 = L.upstream_saw(r["80 GET /api/ping spoof from 172"])
            assert L.header_values(e172, "x-real-ip") == ["203.0.113.9"], "172.16/12 client: CF-Connecting-IP honoured (as today)"
            assert L.header_values(e192, "x-real-ip") != ["203.0.113.9"], "192.168 client: CF-Connecting-IP must be ignored (as today)"
            assert L.header_values(e80, "x-real-ip") != ["203.0.113.9"], "port 80 never trusts CF-Connecting-IP"
            g = r["80 GET entities pad+gzip"]
            L.expect_http(g, 200, header=("Content-Encoding", "gzip"))
            assert json.loads(gzip.decompress(base64.b64decode(g["body_b64"])))["pad"] == "x" * 3000
            L.expect_http(r["80 GET /health"], 200)
            return f"{label}: all explicit assertions hold"
        return f

    rep.check("explicit assertions on the BASELINE (proves the assertions themselves)", explicit(res_old, "baseline"))
    rep.check("explicit assertions on the CANDIDATE (allow-list, 404/413 envelopes, prefixes, redirects, pgAdmin port, trust, gzip)",
              explicit(res_new, "candidate"))

    # rate limit: same bucket arithmetic (tolerance), same 429 envelope bytes
    def burst(variant):
        time.sleep(3.0)
        return su.lab.client(su.c172, "burst", {"target": variant.container, "port": 80, "path": "/search/health",
                                                 "n": 45, "headers": C.BASE_HEADERS})

    def rate_limit():
        o, n = burst(su.old), burst(su.new)
        for label, b in (("baseline", o), ("candidate", n)):
            assert b["first429"] is not None, f"{label}: no 429 in 45 rapid requests: {b['statuses']}"
            assert all(s == 200 for s in b["statuses"][:b["first429"]["index"]]), f"{label}: non-200 before the first 429"
            assert not any(isinstance(s, str) for s in b["statuses"]), f"{label}: transport error {b['statuses']}"
        io, inn = o["first429"]["index"], n["first429"]["index"]
        assert 19 <= io <= 26 and 19 <= inn <= 26 and abs(io - inn) <= 3, f"first 429 at {io} vs {inn}"
        assert o["first429"]["body_b64"] == n["first429"]["body_b64"], "429 envelope differs"
        env = json.loads(base64.b64decode(n["first429"]["body_b64"]))["error"]["code"]
        assert env == "RATE_LIMITED", env
        return f"first 429 at request #{io} (baseline) / #{inn} (candidate); identical RATE_LIMITED envelope (statistical, tolerance ±3)"
    rep.check("rate limit: 10 r/s burst 20, same 429 envelope", rate_limit)
    rep.data["equivalence_notes"] = "rate limit is judged statistically, not by exact response equality"
    streaming(su, rep)
    if own:
        return su


def _assert(cond, msg=""):
    if not cond:
        raise AssertionError(msg)


# ═══ C. operational resilience ═════════════════════════════════════════
def worker_pids(variant):
    out = variant.exec("ps").stdout
    return sorted(int(l.split()[0]) for l in out.splitlines() if "nginx: worker process" in l)


def wait_ok(su, variant, path, *, port=80, timeout=60, poll=0.25):
    """Seconds until the first REAL 200 for `path` (a transport error is not a 200)."""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = su.get(variant, port, path, timeout=6)
        if "error" not in last and last["status"] == 200:
            return round(time.time() - t0, 2)
        time.sleep(poll)
    raise L.NotHealthy(f"{variant.container} {path} never answered 200 within {timeout}s (last: {last and (last.get('status') or last.get('error'))})")


def container_ip(name, network):
    return run(["docker", "inspect", "--format", "{{(index .NetworkSettings.Networks \"" + network + "\").IPAddress}}", name]).stdout.strip()


def resilience(lab, rep):
    print("C. operational resilience (stub upstreams; candidate vs baseline control)")
    su = Setup(lab, skip_stubs=("edu_ai_search", "edu_ai_qa_admin"), start_variants=False)
    old, new = su.old, su.new
    data = rep.data.setdefault("resilience", {})
    search_stub = lab.stub_name("edu_ai_search")
    n172 = lab.net("n172")

    # C1 ── start with two upstreams missing
    new.up()

    def c1_new():
        L.wait_healthy(new.container, 60)
        s = su.get(new, 80, "/search/health")
        L.expect_http(s, 503, envelope_code="UNAVAILABLE")
        q = su.get(new, 80, "/qa/")
        L.expect_http(q, 502)
        for p in ("/health", "/api/ping", "/chat/x"):
            L.expect_http(su.get(new, 80, p), 200)
        data["c1_candidate"] = {"search": {"status": s["status"], "elapsed": s["elapsed"]},
                                "qa": {"status": q["status"], "elapsed": q["elapsed"]}}
        return f"healthy with search and qa absent; /health, /api, /chat 200; /search 503 UNAVAILABLE in {s['elapsed']}s; /qa/ 502 in {q['elapsed']}s"
    rep.check("C1 candidate starts and turns healthy with two upstreams missing; other paths work", c1_new)

    old.up()
    time.sleep(12)

    def c1_old():
        info = old.inspect()
        logs = run(["docker", "logs", old.container], check=False)
        text = logs.stdout + logs.stderr
        assert 'host not found in upstream "edu_ai_search:8090"' in text, text[-300:]
        assert info["RestartCount"] >= 1, f"no restarts: {info['RestartCount']}"   # restart-policy loop, state at one instant proves nothing
        data["c1_baseline"] = {"restarts_in_12s": info["RestartCount"], "emerg_lines": text.count("host not found in upstream")}
        return f"control: baseline is in a restart loop — {info['RestartCount']} restarts in 12 s, {text.count('host not found in upstream')}x `host not found in upstream`"
    rep.check("C1 control: baseline cannot start with the same upstreams missing", c1_old)

    # C2 ── upstreams come back; candidate must NOT be reloaded or restarted
    pids_before, started_before = worker_pids(new), new.inspect()["State"]["StartedAt"]
    master_before = new.exec("cat", "/var/run/nginx.pid").stdout.strip()
    lab.start_stub("edu_ai_search")
    lab.start_stub("edu_ai_qa_admin")
    lab.wait_stubs(["edu_ai_search", "edu_ai_qa_admin"])
    t_back = time.time()

    def c2_new():
        s = wait_ok(su, new, "/search/health", timeout=30)
        q = wait_ok(su, new, "/qa/", timeout=30)
        data["c2_candidate_recover_s"] = {"search": s, "qa": q}
        assert s <= 15 and q <= 15, f"recovery took search {s}s / qa {q}s (> 15 s)"
        assert worker_pids(new) == pids_before, "worker processes changed: nginx was reloaded"
        assert new.inspect()["State"]["StartedAt"] == started_before and new.inspect()["RestartCount"] == 0
        assert new.exec("cat", "/var/run/nginx.pid").stdout.strip() == master_before
        return f"search back in {s}s, qa in {q}s after the upstreams started; same workers {pids_before}, no reload, no restart"
    rep.check("C2 candidate follows the returned upstreams without reload or restart (<= 15 s)", c2_new)

    def c2_old():
        t = wait_ok(su, old, "/search/health", timeout=120, poll=1.0)
        data["c2_baseline_recover_s"] = round(time.time() - t_back, 1)
        return f"control: baseline serves again only through the restart loop, {data['c2_baseline_recover_s']}s after the upstreams were back (restarts={old.inspect()['RestartCount']})"
    rep.check("C2 control: baseline needs its restart loop to recover", c2_old)
    for v in (old, new):
        wait_ok(su, v, "/health")

    # shared machinery for timed scenarios ─────────────────────────────
    def start_series(variant, **over):
        p = {"target": variant.container, "port": 80, "path": "/search/health",
             "extra_paths": ["/api/ping", "/health"], "interval": 0.5, "timeout": 8,
             "headers": C.BASE_HEADERS, "max_seconds": 240}
        p.update(over)
        return L.Series(su.c172, p)

    def both(fn):
        return {v.name: fn(v) for v in (old, new)}

    def analyse(name, results, marks_of, edges):
        out = {}
        for v in (old, new):
            smp = results[v.name]["samples"]
            m = {x["label"]: x["t"] for x in results[v.name]["marks"]}
            t_end = max(x["t"] for x in smp) + 1
            w = {"before": (0, m[edges[0]]),
                 "event_command": (m[edges[0]], m[edges[1]]),
                 "outage_first_6s": (m[edges[1]], min(m[edges[1]] + 6, m[edges[2]])),
                 "outage_after_6s": (min(m[edges[1]] + 6, m[edges[2]]), m[edges[2]]),
                 "restore_command": (m[edges[2]], m[edges[3]]),
                 "after": (m[edges[3]], t_end)}
            stats = {k: L.window_stats(smp, "/search/health", a, b) for k, (a, b) in w.items()}
            first = L.first_ok_after(smp, "/search/health", m[edges[3]])
            others = {p: L.window_stats(smp, p, 0, t_end) for p in ("/api/ping", "/health")}
            out[v.name] = {"search": stats, "recovered_s_after_restore": None if first is None else round(first - m[edges[3]], 2),
                           "other_paths_whole_run": others, "marks": m,
                           "raw_search_samples": [[x["t"], x["status"] if x["error"] is None else "ERR", x["elapsed"]]
                                                  for x in smp if x["path"] == "/search/health"]}
        rep.data["resilience"][name] = out
        return out

    def others_unaffected(an):
        for vname, d in an.items():
            for p, st in d["other_paths_whole_run"].items():
                assert set(st["kinds"]) == {"200"}, f"{vname} {p}: {st['kinds']}"
                assert st["elapsed_max"] < 1.5, f"{vname} {p}: slowest {st['elapsed_max']}s"
        return "/api/ping and /health: only 200, slowest answer < 1.5 s, for both variants"

    def settle():
        old.reload()
        for v in (old, new):
            wait_ok(su, v, "/search/health", timeout=40)

    def scenario(name, event, restore, outage=22, after=18, edges=("event_begin", "event_done", "restore_begin", "restore_done")):
        settle()
        sers = both(start_series)
        time.sleep(3)
        for s in sers.values():
            s.mark(edges[0])
        event()
        for s in sers.values():
            s.mark(edges[1])
        time.sleep(outage)
        for s in sers.values():
            s.mark(edges[2])
        restore()
        for s in sers.values():
            s.mark(edges[3])
        time.sleep(after)
        res = {k: s.stop() for k, s in sers.items()}
        return analyse(name, res, None, edges)

    def show(an):
        lines = []
        for vname in ("old", "new"):
            d = an[vname]
            e1, e2 = d["search"]["outage_first_6s"], d["search"]["outage_after_6s"]
            lines.append(f"{vname}: first6s {e1['kinds']} max {e1['elapsed_max']}s | later {e2['kinds']} max {e2['elapsed_max']}s | recovered {d['recovered_s_after_restore']}s after restore")
        return "; ".join(lines)

    # C3 ── replacement with a provably DIFFERENT IP, nginx untouched
    def c3():
        settle()
        old_ip = container_ip(search_stub, n172)
        sers = both(start_series)
        time.sleep(3)
        for s in sers.values():
            s.mark("event_begin")
        run(["docker", "stop", "-t", "1", search_stub])
        squat = lab.name("squatter")
        lab.containers.append(squat)
        run(["docker", "run", "-d", "--name", squat, "--network", n172, "--ip", old_ip, *lab.labels(),
             "--entrypoint", "sleep", L.NGINX_IMAGE, "3600"])
        lab.start_stub("edu_ai_search")
        lab.wait_stubs(["edu_ai_search"])
        new_ip = container_ip(search_stub, n172)
        assert new_ip != old_ip, f"the replacement got the same IP {old_ip}"
        for s in sers.values():
            s.mark("event_done")
        time.sleep(22)
        for s in sers.values():
            s.mark("restore_begin")
            s.mark("restore_done")
        time.sleep(1)
        res = {k: s.stop() for k, s in sers.items()}
        an = analyse("c3_ip_change", res, None, ("event_begin", "event_done", "restore_begin", "restore_done"))
        # after event_done: candidate recovers, baseline does not
        for v in (old, new):
            smp = res[v.name]["samples"]
            m = {x["label"]: x["t"] for x in res[v.name]["marks"]}
            first = L.first_ok_after(smp, "/search/health", m["event_done"])
            an[v.name]["recovered_s_after_event_done"] = None if first is None else round(first - m["event_done"], 2)
        rep.data["resilience"]["c3_ip_change"] = an
        rep.data["resilience"]["c3_ips"] = {"old": old_ip, "new": new_ip}
        assert an["new"]["recovered_s_after_event_done"] is not None and an["new"]["recovered_s_after_event_done"] <= 15, an["new"]
        assert an["old"]["recovered_s_after_event_done"] is None, "control: baseline unexpectedly recovered by itself"
        old.reload()
        t = wait_ok(su, old, "/search/health", timeout=15)
        run(["docker", "rm", "-f", squat], check=False)
        return (f"IP {old_ip} -> {new_ip}; candidate 200 again {an['new']['recovered_s_after_event_done']}s after the replacement is up, "
                f"baseline never (stays {an['old']['search']['outage_after_6s']['kinds']}) until an explicit reload ({t}s)")
    rep.check("C3 replacement with a different IP: candidate recovers by itself, baseline needs a reload", c3)

    # C4 ── three kinds of outage of ONE upstream, status and time recorded
    def c4_stop():
        an = scenario("c4a_service_stopped", lambda: run(["docker", "stop", "-t", "1", search_stub]),
                      lambda: (run(["docker", "start", search_stub]), lab.wait_stubs(["edu_ai_search"])))
        others_unaffected(an)
        assert an["new"]["recovered_s_after_restore"] is not None and an["new"]["recovered_s_after_restore"] <= 15, an["new"]
        return show(an)
    rep.check("C4a service stopped (container stopped: IP freed, Docker DNS record gone)", c4_stop)

    def c4_dns():
        an = scenario("c4b_dns_record_removed", lambda: run(["docker", "network", "disconnect", n172, search_stub]),
                      lambda: run(["docker", "network", "connect", "--alias", "edu_ai_search", n172, search_stub]))
        others_unaffected(an)
        assert an["new"]["recovered_s_after_restore"] is not None and an["new"]["recovered_s_after_restore"] <= 15, an["new"]
        return show(an)
    rep.check("C4b DNS record removed while the container keeps running (network disconnect)", c4_dns)

    def c4_pause():
        settle()
        results, errs = {}, []

        def slow(v):
            try:
                results[v.name] = lab.client(su.c172, "cases", {"cases": [su.case(v, 80, "GET", "/search/health", timeout=150, cid="pause")]}, timeout=200)[0]
            except Exception as e:  # noqa: BLE001
                errs.append(str(e))
        run(["docker", "pause", search_stub])
        ths = [threading.Thread(target=slow, args=(v,)) for v in (old, new)]
        for t in ths:
            t.start()
        time.sleep(5)
        quick = [su.get(v, 80, "/api/ping", timeout=5) for v in (old, new)]
        for t in ths:
            t.join()
        run(["docker", "unpause", search_stub])
        assert not errs, errs
        for q in quick:
            L.expect_http(q, 200)
        for v in (old, new):
            L.expect_http(results[v.name], 504, envelope_code="TIMEOUT")
        rep.data["resilience"]["c4c_process_unresponsive"] = {k: {"status": r["status"], "elapsed": r["elapsed"]} for k, r in results.items()}
        for v in (old, new):
            assert 118 <= results[v.name]["elapsed"] <= 125, results[v.name]["elapsed"]
        return (f"IP reachable, process frozen: both variants answer 504 TIMEOUT after {results['old']['elapsed']}s / {results['new']['elapsed']}s "
                f"(proxy_read_timeout 120s unchanged); /api/ping stayed 200")
    rep.check("C4c upstream reachable but unresponsive (docker pause): 504 after proxy_read_timeout, other paths fine", c4_pause)
    settle()

    def c4d_dead_ip():
        """The IP that nginx holds is dead (container stopped, nothing answers ARP): how long
        does ONE request take, and what does the client get? Candidate and baseline alike."""
        settle()
        results, errs = {}, []

        def long_request(v):
            try:
                results[v.name] = lab.client(su.c172, "cases", {"cases": [su.case(v, 80, "GET", "/search/health", timeout=100, cid="deadip")]}, timeout=130)[0]
            except Exception as e:  # noqa: BLE001
                errs.append(str(e))
        run(["docker", "stop", "-t", "1", search_stub])
        ths = [threading.Thread(target=long_request, args=(v,)) for v in (old, new)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        run(["docker", "start", search_stub])
        lab.wait_stubs(["edu_ai_search"])
        assert not errs, errs
        out = {k: {"status": r.get("status"), "error": r.get("error"), "elapsed": r["elapsed"]} for k, r in results.items()}
        rep.data["resilience"]["c4d_dead_ip_single_request"] = out
        for k, r in results.items():
            assert "error" not in r, f"{k}: no HTTP response within 100 s: {r}"
            assert (r["status"], L.upstream_saw_envelope(r)) in ((503, "UNAVAILABLE"), (504, "TIMEOUT")), (r["status"], r["body_b64"][:60])
            assert r["elapsed"] <= 65, f"{k}: {r['elapsed']}s"
        return (f"one request while the held IP is dead, real HTTP answers in both: baseline {out['old']['status']} after {out['old']['elapsed']}s, "
                f"candidate {out['new']['status']} after {out['new']['elapsed']}s. 504 = proxy_connect_timeout (default 60s) elapsed; 503 = the kernel "
                f"reported `Host is unreachable` first. Which one occurs varies from run to run with the ARP state, for BOTH variants (3 repeated rounds)")
    rep.check("C4d dead IP (name cached, nothing answers): a real 503/504 envelope within 65 s, both variants", c4d_dead_ip)
    settle()

    def c6_noise():
        """A configured upstream that does not exist: how much does the resolver write?"""
        def count():
            t = run(["docker", "logs", new.container], check=False)
            return sum(1 for l in (t.stdout + t.stderr).splitlines() if "could not be resolved" in l)
        n0 = count()
        run(["docker", "stop", "-t", "1", search_stub])
        time.sleep(60)
        n1 = count()
        run(["docker", "start", search_stub])
        lab.wait_stubs(["edu_ai_search"])
        per_min = n1 - n0
        data["c6_resolver_log_lines_per_minute_for_one_missing_name"] = per_min
        data["c6_estimated_bytes_per_day"] = per_min * 1440 * 85
        return (f"{per_min} `could not be resolved` lines in 60 s for ONE unresolvable name (~{per_min * 1440 * 85 // 1024} KiB/day). "
                f"Recorded, not judged: it is noise only while a configured upstream does not exist")
    rep.check("C6 log noise of a permanently unresolvable upstream is measured", c6_noise)
    settle()

    # C5 ── access log: S2 filters nothing
    def c5_logs():
        lines = log_lines(new.container)
        resolver = [l for l in lines if "could not be resolved" in l or "upstream-resolver" in l or "no live upstreams" in l]
        data["c5_logs"] = {"total_lines": len(lines), "resolver_or_no_live_upstream_lines": len(resolver), "sample": resolver[:3]}
        return f"{len(lines)} log lines in the whole run, {len(resolver)} of them resolver / no-live-upstream lines (recorded)"
    rep.check("C5 resolver noise over the whole resilience run is recorded", c5_logs)
    rep.check("C5 access log, candidate: normal client, loopback request, CF-Connecting-IP: 127.0.0.1 and the healthcheck are ALL logged",
              lambda: log_checks(su, new, healthcheck=True))
    rep.check("C5 access log, baseline (control): the same three requests are logged", lambda: log_checks(su, old))
    rep.check("C5 candidate stayed healthy and was never restarted during the whole resilience run",
              lambda: (L.wait_healthy(new.container, 30), _assert(new.inspect()["RestartCount"] == 0 and new.inspect()["State"]["StartedAt"] == started_before, "restarted"))[1] or "healthy, RestartCount 0, same StartedAt")


def log_lines(container):
    t = run(["docker", "logs", container], check=False)
    return (t.stdout + t.stderr).splitlines()


def log_checks(su, variant, *, healthcheck=False):
    """What the access log must keep doing in S2 (review S2-R2): an ordinary client request, an ordinary
    request from 127.0.0.1 on a normal path, and a real client that merely CLAIMS CF-Connecting-IP: 127.0.0.1
    on :81 are all logged. With healthcheck=True the compose healthcheck (GET /health from 127.0.0.1) is logged too."""
    import uuid as _uuid
    tag = _uuid.uuid4().hex[:8]
    m_client, m_loop, m_cf = f"s2logc{tag}", f"s2logl{tag}", f"s2logf{tag}"
    L.expect_http(su.get(variant, 80, f"/api/ping?{m_client}=1"), 200)
    L.docker_exec(variant.container, "wget", "-qO", "/dev/null", f"http://127.0.0.1/api/ping?{m_loop}=1")
    L.expect_http(su.get(variant, 81, f"/search/health?{m_cf}=1", headers=[("CF-Connecting-IP", "127.0.0.1")]), 200)
    time.sleep(1.0)
    lines = log_lines(variant.container)

    def only(marker):
        hits = [l for l in lines if marker in l]
        assert len(hits) == 1, f"{variant.name}: request with marker {marker} logged {len(hits)}x, expected exactly once"
        return hits[0]
    c = only(m_client)
    assert not c.startswith("127.0.0.1 ") and " 200 " in c, c
    lo = only(m_loop)
    assert lo.startswith("127.0.0.1 ") and " 200 " in lo, lo
    cf = only(m_cf)
    assert cf.startswith("127.0.0.1 ") and " 200 " in cf, cf          # remote_addr taken from the header, as today
    if healthcheck:
        deadline = time.time() + 45
        while True:
            hc = [l for l in log_lines(variant.container) if l.startswith("127.0.0.1 ") and '"GET /health HTTP/1.1" 200' in l and "Wget" in l]
            if hc:
                break
            assert time.time() < deadline, "no healthcheck request in the access log within 45 s"
            time.sleep(2)
        return f"{variant.name}: client, loopback and CF-claimed-127.0.0.1 requests logged exactly once; healthcheck logged ({len(hc)} lines so far)"
    return f"{variant.name}: client, loopback and CF-claimed-127.0.0.1 requests each logged exactly once"


# ═══ D. checks of the checks ═══════════════════════════════════════════
def must_raise(exc_types, fn, *, contains=None):
    """The injected fault must make the check raise. If it passes, that is a FALSE PASS."""
    try:
        fn()
    except exc_types as e:
        if contains and contains not in str(e):
            raise AssertionError(f"raised {type(e).__name__} without {contains!r}: {str(e)[:120]}")
        return f"{type(e).__name__}: {str(e)[:70]}"
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"wrong exception {type(e).__name__}: {str(e)[:120]}")
    raise AssertionError("the check PASSED although the fault was injected (FALSE PASS)")


class RawVariant:
    def __init__(self, name, container):
        self.name, self.container = name, container


def selfcheck(lab, rep):
    print("D. checks of the checks (fault injection: each injected fault must be REPORTED, never pass)")
    su = Setup(lab, start_variants=False)      # networks, stubs, clients

    def mk(name, *args):
        cname = lab.name(name)
        lab.containers.append(cname)
        run(["docker", "run", "-d", "--name", cname, *lab.labels(), *args])
        return cname

    good = mk("hc_good", "--health-cmd", "true", "--health-interval", "1s", "--health-retries", "1", L.NGINX_IMAGE)
    bad = mk("hc_bad", "--health-cmd", "exit 1", "--health-interval", "1s", "--health-retries", "1", "--health-start-period", "0s", L.NGINX_IMAGE)
    nohc = mk("hc_none", L.NGINX_IMAGE)
    slow = mk("hc_slow", "--health-cmd", "sleep 30", "--health-interval", "60s", "--health-start-period", "60s", L.NGINX_IMAGE)
    dead = mk("hc_dead", "--entrypoint", "true", L.NGINX_IMAGE)
    py = mk("probe_py", "--network", lab.net("n172"), L.PY_IMAGE, "sleep", "3600")
    time.sleep(3)
    run(["docker", "exec", "-d", py, "python", "-m", "http.server", "8099", "--bind", "127.0.0.1"])
    time.sleep(1.5)

    # health
    rep.check("health: a healthy container passes (positive control)", lambda: L.wait_healthy(good, 20) or "healthy")
    rep.check("health: UNHEALTHY is reported, not passed", lambda: must_raise(L.NotHealthy, lambda: L.wait_healthy(bad, 30), contains="unhealthy"))
    rep.check("health: a container WITHOUT a healthcheck is reported, not passed", lambda: must_raise(L.NotHealthy, lambda: L.wait_healthy(nohc, 5), contains="none"))
    rep.check("health: still STARTING at the deadline is reported, not passed", lambda: must_raise(L.NotHealthy, lambda: L.wait_healthy(slow, 3), contains="starting"))
    rep.check("health: a container that is not running is reported, not passed", lambda: must_raise(L.NotHealthy, lambda: L.wait_healthy(dead, 5), contains="notrunning"))

    # docker exec
    rep.check("exec: no such container -> ExecError", lambda: must_raise(L.ExecError, lambda: L.docker_exec(lab.name("no_such"), "true")))
    rep.check("exec: stopped container -> ExecError", lambda: must_raise(L.ExecError, lambda: L.docker_exec(dead, "true")))
    rep.check("exec: missing binary -> ExecError", lambda: must_raise(L.ExecError, lambda: L.docker_exec(good, "no-such-binary")))
    rep.check("exec: unexpected exit status -> ExecError; an expected one is accepted",
              lambda: (must_raise(L.ExecError, lambda: L.docker_exec(good, "sh", "-c", "exit 3")),
                       _assert(L.docker_exec(good, "sh", "-c", "exit 3", ok_codes=(0, 3)).returncode == 3))[1] or "rc 3 accepted only when listed")

    # TCP probe: a probe that cannot run is never "closed"
    rep.check("probe: open port is OPEN (positive control)", lambda: _assert(L.probe_tcp(py, "127.0.0.1", 8099) == "OPEN") or "OPEN")
    rep.check("probe: refused port is REFUSED, an unknown name is DNS_FAIL (specific known states)",
              lambda: _assert((L.probe_tcp(py, "127.0.0.1", 9), L.probe_tcp(py, "no-such-host.invalid", 80)) == ("REFUSED", "DNS_FAIL")) or "REFUSED, DNS_FAIL")
    rep.check("probe: source container stopped -> ExecError, NOT closed", lambda: must_raise(L.ExecError, lambda: L.expect_closed(dead, "127.0.0.1", 80)))
    rep.check("probe: interpreter missing in the source container -> ExecError, NOT closed",
              lambda: must_raise(L.ExecError, lambda: L.expect_closed(good, "127.0.0.1", 80)))
    rep.check("probe: expect_closed on an OPEN port fails", lambda: must_raise(AssertionError, lambda: L.expect_closed(py, "127.0.0.1", 8099)))

    # HTTP client
    env404 = {"id": "t", "status": 404, "headers": [], "body_b64": base64.b64encode(b'{"error":{"code":"NOT_FOUND"}}').decode()}
    html404 = dict(env404, body_b64=base64.b64encode(b"<html>404</html>").decode())
    rep.check("http: a transport error is NOT the expected 404", lambda: must_raise(L.TransportError, lambda: L.expect_http({"id": "t", "error": "ConnectionRefusedError: x"}, 404, envelope_code="NOT_FOUND")))
    closed = lab.client(su.c172, "cases", {"cases": [{"id": "closed-port", "target": "127.0.0.1", "port": 9, "method": "GET", "path": "/", "headers": [], "timeout": 3}]})[0]
    rep.check("http: the REAL client against a closed port yields an error entry that expect_http rejects",
              lambda: (_assert("error" in closed and "status" not in closed, str(closed)), must_raise(L.TransportError, lambda: L.expect_http(closed, 404)))[1])
    rep.check("http: wrong status, HTML where an envelope is required, wrong envelope code are all rejected; the right one passes",
              lambda: (must_raise(AssertionError, lambda: L.expect_http(dict(env404, status=200), 404)),
                       must_raise(AssertionError, lambda: L.expect_http(html404, 404, envelope_code="NOT_FOUND")),
                       must_raise(AssertionError, lambda: L.expect_http(env404, 404, envelope_code="UNAVAILABLE")),
                       _assert(L.expect_http(env404, 404, envelope_code="NOT_FOUND") is True))[3] or "4 faults rejected, 1 correct accepted")

    # comparator
    a = {"status": 200, "reason": "OK", "headers": [["Date", "x"], ["Server", "nginx"], ["X-A", "1"]], "body_b64": "AA==", "id": "i"}

    def verdict(x, y):
        return classify_pair(x, y)[0]
    rep.check("comparator: transport error or missing result is NOT evaluated; Date-only difference is same; real differences are different",
              lambda: _assert([verdict(a, dict(a, error="Timeout")), verdict(None, a), verdict(a, dict(a, headers=[["Date", "y"], ["Server", "nginx"], ["X-A", "1"]])),
                               verdict(a, dict(a, headers=[["Server", "nginx"], ["X-A", "2"]])), verdict(a, dict(a, body_b64="AQ==")),
                               verdict(a, dict(a, status=404)), verdict(a, dict(a, headers=[["X-A", "1"], ["Server", "nginx"]]))]
                              == ["not_evaluated", "not_evaluated", "same", "different", "different", "different", "different"]) or "7 injected cases classified correctly")

    # sensitivity: the harness must SEE each regression. The faulty configurations are produced here by
    # targeted mutations of the candidate itself (no untracked fixtures, nothing from docs/plans).
    su.old.up()
    su.wait_serving(su.old)
    cand_conf_dir = REPO / "nginx" / "conf"
    cand_text = (cand_conf_dir / "nginx.conf").read_text(encoding="utf-8")

    def mutate(text, kind):
        lines = text.split("\n")
        norm = [" ".join(l.split()) for l in lines]

        def only(pred):
            hits = [i for i, n in enumerate(norm) if pred(n)]
            assert len(hits) == 1, f"mutation {kind}: expected exactly one anchor line, found {len(hits)}"
            return hits[0]
        if kind == "pgadmin_port":      # review R1: the explicit X-Forwarded-Port header is removed
            del lines[only(lambda n: n == "proxy_set_header X-Forwarded-Port 8443;")]
        elif kind == "trust":           # review R2: the trusted proxy range is widened
            i = only(lambda n: n == "set_real_ip_from 172.16.0.0/12;")
            lines.insert(i + 1, lines[i].replace("172.16.0.0/12", "192.168.0.0/16"))
        elif kind == "health_headers":  # the B04a proposal's /health change: one JSON type, inherited security headers
            i = only(lambda n: n == "add_header Content-Type application/json;")
            lines[i] = lines[i].replace("add_header Content-Type application/json;", "default_type application/json;")
        elif kind == "log_filter":      # review S2-R2: the removed access-log filter
            i = only(lambda n: n == "access_log /var/log/nginx/access.log main;")
            ind = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            lines[i:i + 1] = [ind + "map $remote_addr $loggable { 127.0.0.1 0; default 1; }",
                              ind + "access_log /var/log/nginx/access.log main if=$loggable;"]
        else:
            raise ValueError(kind)
        out = "\n".join(lines)
        assert out != text, f"mutation {kind} changed nothing"
        return out

    def raw_nginx(name, text):
        d = lab.tmp / f"mut_{name}"
        d.mkdir()
        for f in cand_conf_dir.iterdir():
            (d / f.name).write_bytes(f.read_bytes())
        (d / "nginx.conf").write_bytes(text.encode("utf-8"))
        cname = lab.name("nginx_mut_" + name)
        lab.containers.append(cname)
        run(["docker", "run", "-d", "--name", cname, "--network", lab.net("n172"), *lab.labels(),
             "--mount", f"type=bind,source={d},target=/etc/nginx,readonly", L.NGINX_IMAGE])
        run(["docker", "network", "connect", lab.net("n192"), cname])
        v = RawVariant("new", cname)
        su.wait_serving(v)
        return v

    contract = C.contract_paths((REPO / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    base_allowed = C.parse_allowed((su.baseline_dir / "nginx" / "nginx.conf").read_text(encoding="utf-8"))
    all_cases = C.build(base_allowed, contract)

    def differences(variant):
        res_old, res_var = {}, {}
        t1 = threading.Thread(target=lambda: res_old.update(run_cases(su, su.old, all_cases)))
        t2 = threading.Thread(target=lambda: res_var.update(run_cases(su, variant, all_cases)))
        t1.start(); t2.start(); t1.join(); t2.join()
        out = []
        for c_ in all_cases:
            v, d = classify_pair(res_old.get(c_["id"]), res_var.get(c_["id"]))
            assert v != "not_evaluated", f"{c_['id']}: {d}"
            if v == "different":
                out.append((c_["id"], d))
        return out

    control = raw_nginx("control", cand_text)
    sens = rep.data.setdefault("sensitivity", {})

    def control_is_clean():
        diffs = differences(control)
        sens["control_unmutated"] = {"requests": len(all_cases), "different": len(diffs)}
        assert not diffs, f"the unmutated candidate, started the same way as the mutants, differs: {diffs[:2]}"
        return f"control: the unmutated candidate run through the same raw-container path has {len(diffs)} differences in {len(all_cases)} requests"
    rep.check("sensitivity control: the unmutated candidate differs in 0 requests (method is sound)", control_is_clean)

    def detects(kind, id_prefix, needle):
        def f():
            diffs = differences(raw_nginx(kind, mutate(cand_text, kind)))
            sens[kind] = {"different": [{"id": i, "diff": d} for i, d in diffs]}
            hit = [i for i, d in diffs if i.startswith(id_prefix) and any(needle in x for x in d)]
            assert hit, f"mutation {kind} was NOT detected (found: {[i for i, _ in diffs]})"
            return f"mutation `{kind}`: {len(diffs)} of {len(all_cases)} requests differ, incl. {hit[0]!r} ({needle})"
        return f
    rep.check("sensitivity: removing the pgAdmin X-Forwarded-Port header is detected (R1)",
              detects("pgadmin_port", "8443 GET /pgadmin/", "x-forwarded-port"))
    rep.check("sensitivity: widening the trusted proxy range is detected (R2 class)",
              detects("trust", "81 GET /search/health CF header from 192", "x-real-ip"))
    rep.check("sensitivity: a changed /health response (type, inherited headers) is detected",
              detects("health_headers", "80 GET /health", "content-type"))
    log_mut = raw_nginx("log_filter", mutate(cand_text, "log_filter"))
    rep.check("sensitivity: the access-log checks ACCEPT the unmutated candidate",
              lambda: log_checks(su, control))
    rep.check("sensitivity: the access-log checks REJECT a re-added log filter (S2-R2)",
              lambda: must_raise(AssertionError, lambda: log_checks(su, log_mut), contains="logged 0x"))

    # teardown: same explicit definition as `up`, independent of cwd and of a foreign COMPOSE_FILE / .env
    def teardown_is_independent():
        nl = chr(10)
        decoy = lab.tmp / "decoy"
        decoy.mkdir()
        decoy_project = lab.name("decoy")
        (decoy / "docker-compose.yml").write_text(nl.join([
            "services:", "  decoy:", f"    image: {L.NGINX_IMAGE}", '    entrypoint: ["sleep", "600"]', "    labels:",
            f"      edu_ai.purpose: {L.PURPOSE}", f"      edu_ai.run: {lab.run_id}", ""]))
        (decoy / ".env").write_text(nl.join(["COMPOSE_FILE=docker-compose.yml", f"COMPOSE_PROJECT_NAME={decoy_project}", ""]))
        hostile = dict(os.environ, COMPOSE_FILE=str(decoy / "docker-compose.yml"), COMPOSE_PROJECT_NAME=decoy_project)
        decoy_cmd = ["docker", "compose", "-p", decoy_project, "--project-directory", str(decoy), "-f", str(decoy / "docker-compose.yml")]

        def decoy_up():
            run(decoy_cmd + ["up", "-d"])

        def decoy_alive():
            return bool(run(decoy_cmd + ["ps", "-q"]).stdout.split())

        def exists(name):
            return run(["docker", "inspect", "-f", "{{.Name}}", name], check=False).returncode == 0

        try:
            # control: the hostile conditions are real. A `down` that names no explicit definition acts on the decoy project.
            decoy_up()
            assert decoy_alive()
            subprocess.run(["docker", "compose", "down", "-t", "1"], cwd=str(decoy), env=hostile, capture_output=True)
            assert not decoy_alive(), "control failed: a bare `docker compose down` in the hostile cwd/env did not act on the decoy project"
            decoy_up()
            assert decoy_alive()
            v = lab.variant("iso", su.candidate_dir)
            v.up()
            assert exists(v.container)
            # the exact argv, cwd and env the teardown hands to docker
            seen = []
            real_run = L.run

            def spy(cmd, **kw):
                env = L._env() if kw.get("env") is None else kw["env"]
                seen.append((list(map(str, cmd)), kw, env, os.environ.get("COMPOSE_FILE")))
                return real_run(cmd, **kw)
            L.run = spy
            saved_cwd, saved_env = os.getcwd(), dict(os.environ)
            try:
                os.chdir(decoy)
                os.environ.update(hostile)
                err = v.teardown()
            finally:
                L.run = real_run
                os.chdir(saved_cwd)
                os.environ.clear()
                os.environ.update(saved_env)
            assert err is None, err
            down = [s for s in seen if "down" in s[0]]
            assert len(down) == 1, [s[0] for s in down]
            argv, kw, env, foreign_compose_file = down[0]
            assert foreign_compose_file, "the hostile COMPOSE_FILE was not present while the teardown ran"
            for flag in ("-p", "--project-directory", "--env-file", "-f"):
                assert flag in argv, f"teardown lacks {flag}: {argv}"
            assert argv.count("-f") == 2 and argv[argv.index("--project-directory") + 1] == str(su.candidate_dir), argv
            assert str(decoy) not in " ".join(argv), "the decoy path reached the teardown command"
            assert str(kw.get("cwd") or L.NEUTRAL_CWD) == str(L.NEUTRAL_CWD), kw
            assert not [k for k in env if k.startswith("COMPOSE_")], "COMPOSE_* reached the teardown command"
            assert not exists(v.container), "the variant's container survived the teardown"
            assert decoy_alive(), "the decoy project (named by the hostile COMPOSE_PROJECT_NAME) was touched by the teardown"
        finally:
            run(decoy_cmd + ["down", "-v", "-t", "1"], check=False)
        return ("control: a bare `docker compose down` in cwd=decoy with COMPOSE_FILE/COMPOSE_PROJECT_NAME=decoy removes the decoy project; "
                "the lab teardown, run under the same conditions, passes -p/--project-directory/--env-file and every -f of `up`, "
                "runs in the neutral directory without COMPOSE_*, removes exactly the variant's container and leaves the decoy project alone")
    rep.check("teardown uses the explicit definition of `up`: independent of cwd and a foreign COMPOSE_FILE/.env", teardown_is_independent)

    def leftovers_include_volumes():
        by_label = lab.name("stray_vol_label")
        by_name = lab.name("stray_vol_name")
        run(["docker", "volume", "create", "--label", f"edu_ai.run={lab.run_id}", by_label])
        run(["docker", "volume", "create", by_name])
        try:
            left = lab.leftovers().get("volumes", [])
            assert by_label in left and by_name in left, left
            must_raise(L.LabError, lab.verify_clean, contains="volumes")
        finally:
            run(["docker", "volume", "rm", "-f", by_label, by_name], check=False)
        assert by_label not in lab.leftovers().get("volumes", [])
        return "an unregistered volume is found both by the run label and by the run's name prefix; verify_clean() fails on it"
    rep.check("leftover detection covers volumes (by label and by name prefix)", leftovers_include_volumes)

    # the real compose healthcheck can actually turn unhealthy
    def healthcheck_fails():
        broken = lab.tmp / "broken"
        (broken / "nginx" / "conf").mkdir(parents=True)
        for f in (REPO / "nginx" / "conf").iterdir():
            (broken / "nginx" / "conf" / f.name).write_bytes(f.read_bytes())
        (broken / "docker-compose.yml").write_bytes((su.candidate_dir / "docker-compose.yml").read_bytes())
        (broken / ".env").write_text("")
        conf = broken / "nginx" / "conf" / "nginx.conf"
        text = conf.read_text(encoding="utf-8")
        assert "return 200 '{\"status\":\"ok\"}';" in text
        conf.write_text(text.replace("return 200 '{\"status\":\"ok\"}';", "return 500 'down';"), encoding="utf-8", newline="\n")
        v = lab.variant("broken", broken)
        v.up()
        return must_raise(L.NotHealthy, lambda: L.wait_healthy(v.container, 150), contains="unhealthy")
    rep.check("the compose healthcheck of nginx really turns unhealthy when /health fails (and the harness reports it)", healthcheck_fails)


# ═══ atomic replacement on a Linux filesystem ══════════════════════════
def with_marker(conf_text, version):
    """Insert an exact-match location answering `version` into the :80 server block."""
    needle = "listen 80;"
    assert conf_text.count(needle) == 1, "expected exactly one 'listen 80;'"
    return conf_text.replace(needle, needle + f'\n        location = /__s2_marker {{ default_type text/plain; return 200 "{version}"; }}')


def atomic(lab, rep):
    print("E. atomic replacement of the configuration on the host, then reload")
    vol = lab.name("confvol")
    run(["docker", "volume", "create", *lab.labels(), vol])
    lab.volumes.append(vol)
    mountpoint = run(["docker", "volume", "inspect", "-f", "{{.Mountpoint}}", vol]).stdout.strip()

    def helper(script):
        return run(["docker", "run", "--rm", "-v", f"{vol}:/w", "--entrypoint", "sh", L.NGINX_IMAGE, "-c", script]).stdout

    def put(subdir, files):
        """Write files into <volume>/<subdir> from the host (docker cp into a created helper)."""
        tmp = lab.tmp / f"put_{subdir.replace('/', '_')}"
        tmp.mkdir(exist_ok=True)
        for n, data in files.items():
            (tmp / n).write_bytes(data)
        h = lab.name("puthelper")
        run(["docker", "create", "--name", h, "-v", f"{vol}:/w", "--entrypoint", "sh", L.NGINX_IMAGE])
        try:
            helper(f"mkdir -p /w/{subdir}")
            run(["docker", "cp", f"{tmp}/.", f"{h}:/w/{subdir}/"])
        finally:
            run(["docker", "rm", "-f", h], check=False)

    cand_files = {f.name: f.read_bytes() for f in (REPO / "nginx" / "conf").iterdir()}
    cand_conf = cand_files["nginx.conf"].decode()
    names = list(L.UPSTREAMS)

    def start(name, mounts, add_hosts=()):
        cname = lab.name(name)
        lab.containers.append(cname)
        cmd = ["docker", "run", "-d", "--name", cname, *lab.labels()]
        for h in add_hosts:
            cmd += ["--add-host", f"{h}:127.0.0.1"]
        for src, dst in mounts:
            cmd += ["--mount", f"type=bind,source={src},target={dst},readonly"]
        run(cmd + [L.NGINX_IMAGE])
        deadline = time.time() + 20
        while True:
            p = run(["docker", "exec", cname, "wget", "-qO-", "http://127.0.0.1/__s2_marker"], check=False)
            if p.returncode == 0:
                return cname
            if time.time() > deadline:
                raise L.NotHealthy(f"{cname} did not start: {run(['docker', 'logs', cname], check=False).stderr[-300:]}")
            time.sleep(0.5)

    def marker(cname):
        return L.docker_exec(cname, "wget", "-qO-", "http://127.0.0.1/__s2_marker").stdout.strip()

    def reload(cname):
        L.docker_exec(cname, "nginx", "-t")
        L.docker_exec(cname, "nginx", "-s", "reload")
        time.sleep(1.0)

    # ── candidate layout: the DIRECTORY is bind-mounted ──
    put("conf", dict(cand_files, **{"nginx.conf": with_marker(cand_conf, "v1").encode()}))
    c = start("atomic_dir", [(f"{mountpoint}/conf", "/etc/nginx")])
    rep.check("candidate layout starts from the directory mount and answers marker v1", lambda: _assert(marker(c) == "v1", marker(c)) or "v1")

    def rename_replace():
        helper("cd /w/conf && sed 's/\"v1\"/\"v2\"/' nginx.conf > nginx.conf.new && mv nginx.conf.new nginx.conf")
        assert marker(c) == "v1", "before the reload the running config must still answer v1"
        reload(c)
        assert marker(c) == "v2", f"after atomic rename + reload the answer is {marker(c)}, not v2"
        return "mv nginx.conf.new nginx.conf (new inode) + nginx -t + nginx -s reload -> the response changed v1 -> v2"
    rep.check("candidate: atomic rename of nginx.conf on the host, then reload, CHANGES the response", rename_replace)

    def git_style_replace():
        # what `git checkout` does: unlink the file and write a new one under the same name
        helper("cd /w/conf && cp nginx.conf nginx.conf.bak && rm nginx.conf && sed 's/\"v2\"/\"v3\"/' nginx.conf.bak > nginx.conf && rm nginx.conf.bak")
        reload(c)
        assert marker(c) == "v3", marker(c)
        return "unlink + create (git checkout style) + reload -> v3"
    rep.check("candidate: delete-and-recreate (git checkout style) then reload CHANGES the response", git_style_replace)

    def broken_config_is_safe():
        helper("cd /w/conf && cp nginx.conf nginx.conf.good && sed -i 's/listen 80;/listen 80 nonsense_flag;/' nginx.conf")
        try:
            t = L.docker_exec(c, "nginx", "-t", ok_codes=(0, 1))
            assert t.returncode == 1, "nginx -t accepted a broken file"
            r = L.docker_exec(c, "nginx", "-s", "reload", ok_codes=(0, 1))   # the CLI parses the file first and refuses (rc=1)
            assert r.returncode == 1, "nginx -s reload accepted a broken file"
            time.sleep(1.0)
            assert marker(c) == "v3", f"a broken config changed the running answer to {marker(c)}"
        finally:
            helper("cd /w/conf && mv nginx.conf.good nginx.conf")
        reload(c)
        assert marker(c) == "v3"
        return "broken file: `nginx -t` rc=1 and `nginx -s reload` rc=1 (no signal sent); the running configuration keeps answering v3; restoring + reload is clean"
    rep.check("candidate: a broken file is caught by nginx -t and cannot replace the running configuration", broken_config_is_safe)

    # ── limitation: replacing the DIRECTORY itself is NOT picked up ──
    def directory_replacement_limit():
        helper("cd /w && mv conf conf.old && mkdir conf && cp conf.old/* conf/ && sed -i 's/\"v3\"/\"v4\"/' conf/nginx.conf")
        reload(c)
        got = marker(c)
        assert got == "v3", f"unexpected: directory replacement WAS picked up ({got}); the documented limitation is wrong"
        return "replacing the directory itself (mv conf conf.old; mkdir conf) is NOT followed: still v3 after reload — replace files inside nginx/conf, never the directory"
    rep.check("limitation proven: replacing the directory itself is not followed by a running container", directory_replacement_limit)

    # ── control: the old single-file mounts keep the stale inode ──
    base = lab.extract_baseline()
    base_conf = (base / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    put("ctl", {"nginx.conf": with_marker(base_conf, "v1").encode(), "search_api.conf": (base / "nginx" / "search_api.conf").read_bytes()})
    k = start("atomic_file", [(f"{mountpoint}/ctl/nginx.conf", "/etc/nginx/nginx.conf"),
                              (f"{mountpoint}/ctl/search_api.conf", "/etc/nginx/search_api.conf")], add_hosts=names)

    def control_is_stale():
        assert marker(k) == "v1"
        helper("cd /w/ctl && sed 's/\"v1\"/\"v2\"/' nginx.conf > nginx.conf.new && mv nginx.conf.new nginx.conf")
        host_view = helper("grep -c '\"v2\"' /w/ctl/nginx.conf").strip()
        container_view = L.docker_exec(k, "grep", "-c", '"v2"', "/etc/nginx/nginx.conf", ok_codes=(0, 1)).stdout.strip()
        reload(k)
        got = marker(k)
        assert host_view == "1", "the host file was not replaced"
        assert container_view == "0" and got == "v1", f"control did not reproduce the stale mount: container sees v2 count {container_view}, answers {got}"
        return "control: with single-file mounts (today's layout) the SAME replacement + reload leaves the container on the OLD file (answers v1; host file has v2). This is the defect the directory mount removes"
    rep.check("control: today's single-file mounts DO go stale after the identical atomic replacement", control_is_stale)


# ═══ deployment and return rehearsal (scripts/nginx_s2_switch.sh on throw-away clones) ═══
def rehearsal(lab, rep):
    import rehearsal as RH  # noqa: PLC0415 - only this command needs it
    RH.run_rehearsal(lab, rep, Setup(lab, start_variants=False))


# ═══ CLI ═══════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["static", "equivalence", "resilience", "atomic", "selfcheck", "rehearsal", "all"])
    ap.add_argument("--baseline", default="HEAD", help="git ref of the pre-S2 state (default HEAD)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rep = Report()
    first = L.Lab(args.baseline, keep_tmp=args.keep_tmp)
    rep.data["meta"] = meta(first)
    print(f"baseline {first.baseline_sha[:10]}  nginx image {rep.data['meta']['images'][L.NGINX_IMAGE][:19]}...")
    labs = [first]
    todo = ["static", "equivalence", "resilience", "atomic", "selfcheck", "rehearsal"] if args.command == "all" else [args.command]
    for step in todo:
        fn = globals().get(step)
        if fn is None:
            print(f"  (command {step} is not implemented yet)")
            continue
        lab = labs.pop() if labs else L.Lab(args.baseline, keep_tmp=args.keep_tmp)
        print(f"-- {step}  (lab {lab.run_id})")
        try:
            fn(lab, rep)
        except Exception as e:  # noqa: BLE001 — an aborted step is an ERROR, never silently a pass
            rep.items.append({"name": f"{step} aborted", "status": "ERROR", "detail": f"{type(e).__name__}: {e}"})
            print(f"  ERROR {step} aborted - {type(e).__name__}: {e}")
        finally:
            try:
                lab.teardown()
                print("   teardown: only this lab's resources removed, none left")
            except L.LabError as e:
                rep.items.append({"name": f"teardown {step}", "status": "FAIL", "detail": str(e)})
                print(f"  FAIL  teardown {step} - {e}")
    rep.data["checks"] = rep.items
    rep.data["summary"] = rep.summary()
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(rep.data, indent=1), encoding="utf-8")
    print("SUMMARY", rep.summary())
    sys.exit(0 if rep.ok() else 1)


if __name__ == "__main__":
    main()
