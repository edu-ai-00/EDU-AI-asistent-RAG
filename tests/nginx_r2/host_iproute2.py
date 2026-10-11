#!/usr/bin/env python3
"""The preflight in HOST mode (`--routes host`) against a REAL iproute2, in an isolated network namespace. Standard library only.

    wsl -d Ubuntu -e unshare -Urn python3 tests/nginx_r2/host_iproute2.py [--out FILE]

`unshare -Urn` gives a fresh user + network namespace: root inside it, nothing outside it. Every route, interface and address made here
lives in that namespace and vanishes with the process; the host's own routes are never read for a verdict and never changed. What is real:
`ip` (iproute2, whatever the distribution ships), the kernel's routing tables, policy tables with numbers above 255 (51820, the default
of wg-quick), a dummy interface named like a Docker bridge (br-<12 characters of the network id>) with the connected, local and
broadcast routes the kernel makes for it. What is not: Docker (there is none here; `docker network ls/inspect` and `docker info` are
answered from canned data, the same shapes the daemon prints) and the production host's version of iproute2.

Exit status 0 only when every check passes.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))
import tunnel_subnet as TS  # noqa: E402

OWN_ID = "62dd04c0db44aabbccddeeff00112233445566778899aabbccddeeff00112233"
OWN = "10.14.118.48/29"
POLICY = "10.55.5.0/29"
MODE = {"route_fail": False, "addr_fail": False, "no_ip": False, "own_network": False}


def ip(*args, check=True):
    p = subprocess.run(["ip", *args], capture_output=True, text=True, timeout=30)
    if check and p.returncode != 0:
        raise RuntimeError(f"ip {' '.join(args)} rc={p.returncode}: {p.stderr.strip()}")
    return p


def fake_docker(c):
    out = lambda s="", rc=0: types.SimpleNamespace(returncode=rc, stdout=s, stderr="")   # noqa: E731
    if c[:3] == ["docker", "network", "ls"]:
        return out("netown\n" if MODE["own_network"] else "")
    if c[:3] == ["docker", "network", "inspect"]:
        return out(json.dumps([{"Name": "inst_tunnel", "Id": OWN_ID, "IPAM": {"Config": [{"Subnet": OWN}]}}]) if MODE["own_network"] else "[]")
    if c[:2] == ["docker", "info"]:
        return out("null")
    raise AssertionError(f"unexpected command {c}")


def runner(cmd, check=False, **kw):
    c = [str(x) for x in cmd]
    if c[0] == "docker":
        return fake_docker(c)
    if MODE["no_ip"]:
        raise FileNotFoundError("ip")
    if MODE["route_fail"] and "route" in c:
        c = ["ip", "-4", "route", "show", "table", "bogus"]            # a REAL read that iproute2 itself rejects
    if MODE["addr_fail"] and "addr" in c:
        c = ["ip", "-4", "-o", "addr", "show", "dev", "no-such-interface"]
    return subprocess.run(c, capture_output=True, text=True, timeout=30)


def cli(argv):
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        rc = TS.main(argv + ["--routes", "host"], runner=runner)
    return rc, sink.getvalue()


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    rows = []

    def check(cid, desc, fn):
        try:
            detail, status = fn(), "PASS"
        except AssertionError as e:
            detail, status = str(e), "FAIL"
        except Exception as e:  # noqa: BLE001
            detail, status = f"{type(e).__name__}: {e}", "ERROR"
        rows.append({"id": cid, "desc": desc, "status": status, "detail": (detail or "")[:700]})
        print(f"  {status:5} {cid}  {desc}\n        {detail}")

    version = ip("-V").stdout.strip()
    print(f"{version}; python {sys.version.split()[0]}; uid {os.getuid()}")
    check("host.namespace", "the checks run in a fresh network namespace (only lo, no host routes)", lambda: (
        must([l.split(":")[1].strip() for l in ip("-o", "link").stdout.splitlines()] == ["lo"], f"links: {ip('-o', 'link').stdout}"),
        f"{version}; links: lo only")[1])
    ip("link", "set", "lo", "up")

    def setup():
        ip("route", "add", POLICY, "dev", "lo", "table", "51820")
        ip("link", "add", f"br-{OWN_ID[:12]}", "type", "dummy")
        ip("addr", "add", "10.14.118.49/29", "dev", f"br-{OWN_ID[:12]}")
        ip("link", "set", f"br-{OWN_ID[:12]}", "up")
        main_only, every = ip("-4", "route", "show").stdout, ip("-4", "route", "show", "table", "all").stdout
        must(POLICY not in main_only and f"{POLICY} dev lo table 51820" in " ".join(every.split()), f"the policy route is not where it should be:\n{main_only}\n---\n{every}")
        must(any("table local" in l and f"br-{OWN_ID[:12]}" in l for l in every.splitlines()) and any("broadcast" in l for l in every.splitlines()), every)
        return (f"policy route {POLICY} in table 51820 (a number above 255): invisible to `ip -4 route show`, shown by `table all`; a dummy bridge-like "
                f"br-{OWN_ID[:12]} with the kernel's connected, local and broadcast routes")
    check("host.setup", "a real policy-table route and a real bridge-like interface in the namespace", setup)

    def free():
        rc, out = cli(["check", "10.80.7.0/29"])
        must(rc == 0 and out.startswith("OK"), f"rc={rc} {out.strip()[:200]}")
        rc2, out2 = cli(["allocate", "--project", "other"])
        must(rc2 == 0 and out2.count("EDU_AI_TUNNEL_SUBNET=10.") == 1, f"rc={rc2} {out2.strip()[:200]}")
        return f"a free subnet: check exit 0, allocate exit 0 ({out2.strip()})"
    check("host.free", "a complete real read without a collision passes (check 0, allocate 0)", free)

    def policy():
        rc, out = cli(["check", POLICY])
        must(rc == 2 and "HOST_ROUTE" in out and "table 51820" in out, f"rc={rc} {out.strip()[:300]}")
        taken = TS.gather(runner, routes="host")
        first = TS.allocate("other", TS.State(taken.networks, taken.pools, tuple(), taken.addrs))      # where allocate would start if it could not see the table
        ip("route", "add", str(first), "dev", "lo", "table", "51820")
        try:
            rc2, out2 = cli(["allocate", "--project", "other"])
            got = out2.strip().split("=", 1)[1] if rc2 == 0 else None
            must(got and not TS.ipaddress.ip_network(got).overlaps(first), f"allocate chose {got} (rc={rc2}) although {first} is routed in table 51820")
        finally:
            ip("route", "del", str(first), "dev", "lo", "table", "51820")
        return f"{POLICY} (table 51820): check exit 2 HOST_ROUTE; allocate skips {first} when that block is routed in table 51820 and chose {got}"
    check("host.policy_table", "a collision that exists only in policy table 51820 is found by the real `ip` (exit 2) and never allocated", policy)

    def own():
        MODE["own_network"] = True
        try:
            for _ in range(3):
                rc, out = cli(["check", OWN, "--project", "inst"])
                must(rc == 0, f"the running installation refused itself: rc={rc} {out.strip()[:300]}")
            rc, out = cli(["allocate", "--project", "inst"])
            must(rc == 0 and out.strip() == f"EDU_AI_TUNNEL_SUBNET={OWN}", f"allocate did not keep the existing subnet: rc={rc} {out.strip()}")
            rc, out = cli(["check", OWN, "--project", "another"])
            must(rc == 2 and all(k in out for k in ("HOST_ROUTE", "HOST_ADDRESS", "DOCKER_NETWORK")), f"another installation: rc={rc} {out.strip()[:300]}")
        finally:
            MODE["own_network"] = False
        return "a running installation with its REAL bridge routes (connected, local, broadcast) passes three preflights and allocate keeps its subnet; another project is refused"
    check("host.own_installation", "an installation's own bridge, as the kernel makes it, is not a collision for itself", own)

    def failing(key, what, word):
        def f():
            MODE[key] = True
            try:
                for argv in (["check", "10.80.7.0/29"], ["allocate", "--project", "other"]):
                    rc, out = cli(argv)
                    must(rc == 3 and "cannot verify" in out and word in out and "EDU_AI_TUNNEL_SUBNET=" not in out, f"{argv[0]}: rc={rc} {out.strip()[:300]}")
            finally:
                MODE[key] = False
            return f"{what}: check and allocate exit 3, nothing printed; {out.strip()[:150]}"
        return f
    check("host.route_read_fails", "a route read that the real iproute2 itself rejects is unverifiable (exit 3) although the address read succeeds",
          failing("route_fail", "a real iproute2 error on the route read", "route"))
    check("host.addr_read_fails", "an address read that the real iproute2 rejects is unverifiable (exit 3) although the route read succeeds",
          failing("addr_fail", "a real iproute2 error on the address read", "addr"))
    check("host.no_ip", "no `ip` binary: exit 3, not a crash", failing("no_ip", "no ip binary", "could not run"))
    s = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    for r in rows:
        s[r["status"]] += 1
    print("summary:", s)
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps({"summary": s, "iproute2": version, "python": sys.version.split()[0], "results": rows}, indent=1), encoding="utf-8")
    return 0 if not s["FAIL"] and not s["ERROR"] else 1


if __name__ == "__main__":
    sys.exit(main())
