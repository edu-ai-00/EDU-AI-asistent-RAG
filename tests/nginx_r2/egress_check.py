#!/usr/bin/env python3
"""The way out of a container on [internal, internal, egress] networks: what cloudflared will have after R2 step 2. Needs Docker.

    python tests/nginx_r2/egress_check.py [--out FILE]

cloudflared moves from `app` (internal) + `edge` to `app` + `tunnel` (both internal) + `tunnel_egress`. It reaches Cloudflare ONLY through
`tunnel_egress`, so the question is whether a container that has two internal networks and one ordinary bridge still gets its default
route from the ordinary one, and loses it when that network is missing (which is the reason the network exists). Own labelled
networks and one container, removed afterwards; nothing else on the daemon is touched. The last checks leave the machine: a DNS lookup of
a Cloudflare tunnel edge name and a TCP connect to it, WITHOUT a token and without any tunnel protocol. When the machine has no internet
they are NOT_RUN, never PASS. This is an observation of this daemon (Docker Desktop), not of the production host.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tests" / "nginx_s2"))
sys.path.insert(0, str(HERE))
import labkit as L  # noqa: E402
import ownership as OW  # noqa: E402

PROBE = r"""
import json, socket, struct, sys
gw = None
for line in open("/proc/net/route").read().splitlines()[1:]:
    f = line.split()
    if f[1] == "00000000" and int(f[3], 16) & 2:
        gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
out = {"default_gateway": gw}
if len(sys.argv) > 1 and sys.argv[1] == "net":
    try:
        a = socket.getaddrinfo("region1.v2.argotunnel.com", 7844, socket.AF_INET)[0][4][0]
        out["dns"] = a
        s = socket.socket(); s.settimeout(5)
        try:
            s.connect((a, 7844)); out["tcp7844"] = "connected"
        except Exception as e:
            out["tcp7844"] = type(e).__name__
    except Exception as e:
        out["dns"] = type(e).__name__
print(json.dumps(out))
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    rid = time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]
    pre = f"b04r2e_{rid}"
    lab = ["--label", f"{OW.LABEL_PURPOSE}={OW.PURPOSE}", "--label", f"{OW.LABEL_RUN}={rid}"]
    rows = []

    def check(cid, desc, fn):
        try:
            detail, status = fn(), "PASS"
        except AssertionError as e:
            detail, status = str(e), "FAIL"
        except NotImplementedError as e:
            detail, status = str(e), "NOT_RUN"
        except Exception as e:  # noqa: BLE001
            detail, status = f"{type(e).__name__}: {e}", "ERROR"
        rows.append({"id": cid, "desc": desc, "status": status, "detail": detail[:600]})
        print(f"  {status:7} {cid}  {desc}\n          {detail[:600]}")

    def gw(net):
        return L.run(["docker", "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Gateway}}{{end}}", net]).stdout.strip()

    def probe(c, *a):
        return json.loads(L.docker_exec(c, "python", "-c", PROBE, *a).stdout)
    nets = {n: f"{pre}_{n}" for n in ("app", "tunnel", "egress")}
    try:
        L.run(["docker", "network", "create", "--internal", *lab, nets["app"]])
        L.run(["docker", "network", "create", "--internal", *lab, nets["tunnel"]])
        L.run(["docker", "network", "create", *lab, nets["egress"]])
        both, only = f"{pre}_cf", f"{pre}_cfnoegress"
        L.run(["docker", "run", "-d", "--name", both, "--network", nets["app"], *lab, L.PY_IMAGE, "sleep", "infinity"])
        for n in ("tunnel", "egress"):
            L.run(["docker", "network", "connect", nets[n], both])
        L.run(["docker", "run", "-d", "--name", only, "--network", nets["app"], *lab, L.PY_IMAGE, "sleep", "infinity"])
        L.run(["docker", "network", "connect", nets["tunnel"], only])

        def route():
            r = probe(both)
            must = r["default_gateway"] == gw(nets["egress"])
            assert must, f"default gateway {r['default_gateway']}, the gateway of tunnel_egress is {gw(nets['egress'])}"
            return f"default route via {r['default_gateway']} = the gateway of the ordinary network (the two internal ones have none)"
        check("egress.default_route", "a container on app + tunnel (internal) + tunnel_egress gets its default route from tunnel_egress", route)

        def without():
            r = probe(only)
            assert r["default_gateway"] is None, f"a default route exists without the egress network: {r}"
            return "without the ordinary network the container has NO default route: tunnel_egress is what gives cloudflared a way out"
        check("egress.needed", "without tunnel_egress there is no way out (so the network is required)", without)

        def internet():
            r = probe(both, "net")
            if not r.get("dns") or "." not in str(r.get("dns")):
                raise NotImplementedError(f"no DNS answer for a Cloudflare tunnel edge name from this machine ({r.get('dns')}): not tested, never PASS")
            assert r.get("tcp7844") == "connected", f"resolved {r['dns']} but the TCP connect to port 7844 gave {r.get('tcp7844')}"
            return f"from the same container: region1.v2.argotunnel.com -> {r['dns']}, TCP 7844 connected (no token, no tunnel protocol)"
        check("egress.cloudflare_edge", "that container resolves and reaches a Cloudflare tunnel edge address (observation on this daemon)", internet)
    finally:
        res = OW.cleanup_run(lambda cmd, check=False, **kw: L.run(cmd, check=False), rid)
        left = [n for n in L.run(["docker", "network", "ls", "--format", "{{.Name}}"]).stdout.split() if n.startswith(pre)]
        check("egress.cleanup", "everything this check created is gone", lambda: (_ for _ in ()).throw(AssertionError(f"left: {left} {res.kept}")) if left or not res.ok() else "clean")
    s = {k: sum(1 for r in rows if r["status"] == k) for k in ("PASS", "FAIL", "ERROR", "NOT_RUN")}
    print("summary:", s)
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps({"summary": s, "results": rows}, indent=1), encoding="utf-8")
    return 0 if not (s["FAIL"] or s["ERROR"]) else 1


if __name__ == "__main__":
    sys.exit(main())
