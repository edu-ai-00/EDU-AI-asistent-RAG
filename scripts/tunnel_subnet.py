#!/usr/bin/env python3
"""Allocation and preflight of the R2 `tunnel` subnet, and checks of the Compose model. Standard library only. READ-ONLY.

The trusted network of one installation is a small private subnet that only nginx and cloudflared join. Its CIDR is the
installation's own setting `EDU_AI_TUNNEL_SUBNET` (never a constant in the repository, never shared between installations):
Compose takes it for the network's subnet AND for the nginx trust range from that one variable.

    python scripts/tunnel_subnet.py allocate --project NAME [--routes host|docker] [--reserved CIDR ...]
    python scripts/tunnel_subnet.py check CIDR [--project NAME] [--routes host|docker] [--reserved CIDR ...]

Exit status: 0 the subnet is free / was allocated | 2 REFUSED: a conflict was found (nothing was created, nothing was changed)
             | 3 REFUSED: the host state could not be inspected (an unverifiable subnet is refused, never assumed free).
A caller continues ONLY on 0. Every other status, an unreadable Docker pool and a crash included, is a refusal.
`--routes docker` also needs --helper-image; `--helper-network container:NAME` (tests) reads that container's namespace instead.

Checks of the effective Compose model (`docker compose config --format json` on stdin; only the fields named here are read and
only derived values are printed, so the rest of the model, which can hold secrets, is never shown):

    ... | python scripts/tunnel_subnet.py model-subnet
    ... | python scripts/tunnel_subnet.py model-check --conf-dir nginx/conf [--allow-legacy]
    ... | python scripts/tunnel_subnet.py render-trust
    ... | python scripts/tunnel_subnet.py wiring SERVICE
    ... | python scripts/tunnel_subnet.py image SERVICE
    {"current": MODEL, "candidate": MODEL} | python scripts/tunnel_subnet.py model-delta --step 1|2|3
Exit status of these: 0 ok | 2 the model does not have the expected shape (the reasons are printed).
`model-delta` accepts exactly the change of the step (step 2: the designed networks, alias and trust config of nginx and cloudflared;
steps 1 and 3: none) and names, by path only, every other difference: a port, a mount, a command, a capability, another service.

Only these sources are read: `docker network ls/inspect`, `docker info` (address pools), the host's IPv4 routes of EVERY routing table
(main, local and policy tables: `ip -4 route show table all`) and interface addresses (`ip -4 addr`; `--routes docker` reads them from
the daemon's own network namespace through ONE throw-away container, which is the right view on Docker Desktop and is not needed on a
Linux host), plus the built-in list of well-known ranges below and any range the administrator passes with --reserved (the site's
LAN, VPN, WireGuard, cloud VPC...). Every mandatory read keeps its own exit status: a read that fails, or cannot run, makes the host
state unverifiable (exit 3) and is never completed with what another read printed. The tool cannot know a
network it is not told about: an unusual number is NOT evidence that a range is free, and a free range today can be claimed tomorrow,
so `check` is a mandatory preflight on EVERY create/update, not only on installation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import pathlib
import re
import subprocess
import sys
from typing import Callable, NamedTuple

POOL = "10.0.0.0/8"
PREFIXES = (28, 29)                       # 6-14 usable addresses: nginx, cloudflared and a replacement running at the same time
RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
# Docker's built-in pools for user-defined bridge networks (unless daemon.json sets default-address-pools).
BUILTIN_DOCKER_POOLS = ("172.16.0.0/12", "192.168.0.0/16")
# Well-known ranges that are never allocated (block, why). Not a complete list and not a proof for the rest of the pool.
DENY = (
    ("10.0.0.0/16", "Docker Swarm ingress/overlay start at 10.0.0.0/24, default Azure vNet / sample AWS VPC, many home and office LANs"),
    ("10.8.0.0/16", "OpenVPN default server network 10.8.0.0/24"),
    ("10.42.0.0/15", "k3s default pod and service CIDRs 10.42.0.0/16 and 10.43.0.0/16"),
    ("10.66.0.0/16", "Reserved WireGuard example range and the wireguard-install default 10.66.66.0/24"),
    ("10.96.0.0/12", "kubeadm / minikube default service CIDR 10.96.0.0/12"),
    ("10.128.0.0/9", "GCP auto-mode VPC subnets"),
    ("10.244.0.0/16", "flannel and AKS default pod CIDR"),
    ("100.64.0.0/10", "carrier-grade NAT, Tailscale"),
    ("169.254.0.0/16", "link-local, cloud metadata services"),
    ("172.16.0.0/12", "Docker default address pools (172.17-172.31), AWS default VPC 172.31.0.0/16, Hyper-V NAT"),
    ("192.168.0.0/16", "Docker second default pool, home and office LANs, Docker Desktop VM 192.168.65.0/24, Calico default"),
)


class Conflict(NamedTuple):
    kind: str          # INVALID NOT_PRIVATE DENYLIST DOCKER_NETWORK NETWORK_MISMATCH DOCKER_POOL HOST_ROUTE HOST_ADDRESS RESERVED
    detail: str


class Unverifiable(Exception):
    """The host state could not be inspected. The subnet is refused: unverified is not free."""


class NoFreeSubnet(Exception):
    pass


class NetInfo(NamedTuple):
    name: str
    ident: str
    subnets: tuple


class State(NamedTuple):
    networks: tuple        # NetInfo
    pools: tuple           # ip_network
    routes: tuple          # (ip_network, raw line)
    addrs: tuple           # (ip_network, raw line)


Runner = Callable[..., "subprocess.CompletedProcess"]

# Both reads cover EVERY IPv4 table: a VPN/policy route outside `main` is as real a collision as one inside it. There is no fallback
# to a main-only read; a binary that rejects `table all` fails the read (exit 3) instead of silently reporting less.
ROUTE_CMD = ("ip", "-4", "route", "show", "table", "all")
ADDR_CMD = ("ip", "-4", "-o", "addr", "show")
SEPARATOR = "@@@"
# `a; echo; b` exits with b's status, so a failed route read behind a good address read would look like an empty table. Each read
# keeps its own status here (11 = routes, 12 = addresses) and the helper exits with it.
HELPER_FAILURES = {11: "the route read", 12: "the address read"}
HELPER_SCRIPT = f"{' '.join(ROUTE_CMD)} || exit 11; echo {SEPARATOR}; {' '.join(ADDR_CMD)} || exit 12"


def run_cmd(cmd, check=False, timeout=60):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


# ── parsing and judgement (pure; unit-tested without Docker) ─────────────
def parse_candidate(text: str) -> ipaddress.IPv4Network:
    """The administrator's value. Only the canonical network form is accepted: a typo with host bits set (10.77.3.5/29) is
    refused, not silently normalised into a different network than the one that was meant."""
    try:
        net = ipaddress.ip_network(text.strip(), strict=True)
    except ValueError as e:
        raise ValueError(f"INVALID: {text!r} is not a canonical IPv4 network ({e})")
    if net.version != 4:
        raise ValueError(f"INVALID: {text!r} is not IPv4")
    if net.prefixlen not in PREFIXES:
        raise ValueError(f"INVALID: /{net.prefixlen} is not one of {['/%d' % p for p in PREFIXES]}")
    if not any(net.subnet_of(r) for r in RFC1918):
        raise ValueError(f"NOT_PRIVATE: {net} is not inside an RFC 1918 range")
    return net


def parse_routes(text: str):
    """Destinations of `ip -4 route show table all` lines (default route skipped). Unparseable lines are skipped, not trusted."""
    out = []
    for line in text.splitlines():
        toks = line.split()
        if not toks or toks[0] == "default":
            continue
        for tok in toks[:2]:                       # an optional type word (blackhole, unreachable, ...) may come first
            try:
                out.append((ipaddress.ip_network(tok, strict=False), line.strip()))
                break
            except ValueError:
                continue
    return out


def parse_addrs(text: str):
    """Interface networks of `ip -4 -o addr show` lines (`... inet 192.0.2.1/24 ...`)."""
    out = []
    for line in text.splitlines():
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+(?:/\d+)?)", line)
        if m:
            out.append((ipaddress.ip_interface(m.group(1)).network, line.strip()))
    return out


def _route_iface(raw: str):
    m = re.search(r"\bdev\s+(\S+)", raw)
    return m.group(1) if m else None


def _addr_iface(raw: str):
    m = re.match(r"\s*\d+:\s+(\S+)", raw)
    return m.group(1).rstrip(":") if m else None


def conflicts(candidate: ipaddress.IPv4Network, state: State, *, reserved=(), deny=DENY, project: str | None = None):
    """Every reason to refuse `candidate`. An empty list means: free in everything this tool can see.

    When `project` is given, the installation's OWN `<project>_tunnel` network is not a collision: neither the network itself nor
    the route and interface address its bridge (`br-<first 12 characters of the network id>`) put into the host, otherwise a
    running installation would refuse itself at every re-check."""
    found = []
    for block, why in deny:
        if candidate.overlaps(ipaddress.ip_network(block)):
            found.append(Conflict("DENYLIST", f"{candidate} overlaps {block} ({why})"))
    own = f"{project}_tunnel" if project else None
    own_ifaces = {f"br-{n.ident[:12]}" for n in state.networks if own and n.name == own}
    for n in state.networks:
        if own and n.name == own:
            if tuple(n.subnets) != (candidate,):
                found.append(Conflict("NETWORK_MISMATCH",
                                      f"the installation's own network {n.name} already exists with subnet(s) {[str(s) for s in n.subnets]}, not {candidate}: it is not touched"))
            continue                                # the same installation, same subnet: re-checking an existing setup is fine
        for s in n.subnets:
            if candidate.overlaps(s):
                found.append(Conflict("DOCKER_NETWORK", f"{candidate} overlaps Docker network {n.name} ({s})"))
    for p in state.pools:
        if candidate.overlaps(p):
            found.append(Conflict("DOCKER_POOL", f"{candidate} overlaps the Docker address pool {p}"))
    for dest, raw in state.routes:
        if _route_iface(raw) in own_ifaces:
            continue
        if candidate.overlaps(dest):
            found.append(Conflict("HOST_ROUTE", f"{candidate} overlaps the route {raw!r}"))
    for dest, raw in state.addrs:
        if _addr_iface(raw) in own_ifaces:
            continue
        if candidate.overlaps(dest):
            found.append(Conflict("HOST_ADDRESS", f"{candidate} overlaps the interface address {raw!r}"))
    for r in reserved:
        if candidate.overlaps(ipaddress.ip_network(r, strict=False)):
            found.append(Conflict("RESERVED", f"{candidate} overlaps the reserved range {r} (given by the administrator)"))
    return found


def _subtract(nets, deny_blocks):
    """The CIDR pieces of `nets` that remain after removing every denied block (CIDRs overlap only by containment)."""
    for d in (ipaddress.ip_network(b) for b in deny_blocks):
        rest = []
        for n in nets:
            if not n.overlaps(d):
                rest.append(n)
            elif n.supernet_of(d) and n != d:
                rest.extend(n.address_exclude(d))
            # else: d contains n entirely -> n is dropped
        nets = rest
    return sorted(nets, key=lambda n: int(n.network_address))


def allocate(project: str, state: State, *, pool=POOL, prefix=29, reserved=(), deny=DENY, max_tries=4096):
    """The first /prefix block, counted from a position derived from the project name, that survives every check.

    The pool first LOSES the well-known blocks (so the scan never wastes its tries inside one), then the start position,
    derived from the project name, only spreads installations over what remains; it proves nothing. Whether a block is usable
    is decided by `conflicts` against the real host state, and the answer is meant to be stored in the installation's
    settings, not recomputed at every start."""
    if prefix not in PREFIXES:
        raise ValueError(f"prefix /{prefix} is not allowed")
    own = f"{project}_tunnel"
    for n in state.networks:                      # an installation that already has its network keeps the subnet it has
        if n.name == own and len(n.subnets) == 1 and n.subnets[0].version == 4 and n.subnets[0].prefixlen in PREFIXES:
            if not conflicts(n.subnets[0], state, reserved=reserved, deny=deny, project=project):
                return n.subnets[0]
    pieces = [n for n in _subtract([ipaddress.ip_network(pool)], [b for b, _ in deny]) if n.prefixlen <= prefix]
    counts = [2 ** (prefix - n.prefixlen) for n in pieces]
    total = sum(counts)
    if total == 0:
        raise NoFreeSubnet(f"nothing of {pool} is left after the well-known ranges")
    start = int(hashlib.sha256(project.encode()).hexdigest(), 16) % total
    step = 1 << (32 - prefix)
    for k in range(min(total, max_tries)):
        idx = (start + k) % total
        for n, c in zip(pieces, counts):
            if idx < c:
                block = ipaddress.ip_network((int(n.network_address) + idx * step, prefix))
                break
            idx -= c
        if not conflicts(block, state, reserved=reserved, deny=deny, project=project):
            return block
    raise NoFreeSubnet(f"no free /{prefix} in {pool} after {min(total, max_tries)} candidates")


# ── reading the host (read-only) ─────────────────────────────────────────
def gather(runner: Runner = run_cmd, *, routes="host", helper_image=None, helper_labels=(), helper_network="host", helper_args=()):
    """The state the checks run against. Any source that cannot be read raises Unverifiable.

    `helper_network` / `helper_args` only shape the throw-away container of `--routes docker` (the lab reads a throw-away namespace
    instead of the daemon's, and injects a failing `ip`); the default is the daemon's own namespace."""
    def ok(cmd, what, notes=None):
        try:
            p = runner(cmd)
        except (OSError, subprocess.TimeoutExpired) as e:       # no `ip`/`docker` binary, a hung read: still not a free subnet
            raise Unverifiable(f"{what} could not run: {type(e).__name__}: {e}")
        if p.returncode != 0:
            which = (notes or {}).get(p.returncode)
            raise Unverifiable(f"{what} failed (rc={p.returncode}{', ' + which if which else ''}): {(p.stderr or p.stdout).strip()[:200]}")
        return p.stdout

    ids = ok(["docker", "network", "ls", "-q"], "docker network ls").split()
    nets = []
    if ids:
        try:
            data = json.loads(ok(["docker", "network", "inspect", *ids], "docker network inspect"))
        except json.JSONDecodeError as e:
            raise Unverifiable(f"docker network inspect returned invalid JSON: {e}")
        for d in data:
            subs = []
            for c in ((d.get("IPAM") or {}).get("Config") or []):
                sub = c.get("Subnet")
                if sub:
                    try:
                        net = ipaddress.ip_network(sub, strict=False)
                    except ValueError:
                        raise Unverifiable(f"network {d.get('Name')} has an unreadable subnet {sub!r}")
                    if net.version == 4:
                        subs.append(net)
            nets.append(NetInfo(d.get("Name", "?"), d.get("Id", "?"), tuple(subs)))

    pools = [ipaddress.ip_network(p) for p in BUILTIN_DOCKER_POOLS]
    try:
        info = json.loads(ok(["docker", "info", "--format", "{{json .DefaultAddressPools}}"], "docker info") or "null")
    except json.JSONDecodeError as e:
        raise Unverifiable(f"docker info returned invalid JSON: {e}")
    for p in info or []:
        base = p.get("Base") if isinstance(p, dict) else None
        if base:
            try:
                pools.append(ipaddress.ip_network(base, strict=False))
            except ValueError:
                raise Unverifiable(f"docker info has an unreadable address pool {base!r}")

    if routes == "host":
        r_txt, a_txt = ok(list(ROUTE_CMD), "ip route (all tables)"), ok(list(ADDR_CMD), "ip addr")
    elif routes == "docker":
        if not helper_image:
            raise Unverifiable("--routes docker needs a local helper image")
        txt = ok(["docker", "run", "--rm", "--network", helper_network, *helper_labels, *helper_args, "--entrypoint", "sh", helper_image,
                  "-c", HELPER_SCRIPT], "route/address read through the daemon's network namespace", HELPER_FAILURES)
        if txt.count(SEPARATOR) != 1:
            raise Unverifiable("the route/address read did not print its separator exactly once")
        r_txt, a_txt = txt.split(SEPARATOR, 1)
    else:
        raise ValueError(routes)
    routes_p, addrs_p = parse_routes(r_txt), parse_addrs(a_txt)
    # Empty output of a read that succeeded is a result (a host may have no route); output that yields nothing is not a result.
    if (r_txt.strip() and not routes_p and not all(l.split()[:1] == ["default"] for l in r_txt.splitlines() if l.strip())) \
            or (a_txt.strip() and not addrs_p):
        raise Unverifiable("a route or address read succeeded but printed nothing this tool can parse")
    if not routes_p and not addrs_p:                              # a working namespace always has at least the loopback address
        raise Unverifiable("neither a route nor an interface address could be read: the host network state is unknown")
    return State(tuple(nets), tuple(pools), tuple(routes_p), tuple(addrs_p))


def strip_comments(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def single_source_problems(model: dict, confs: dict, subnet: str, *, allow_legacy: bool = False,
                           include_line: str = "include /etc/edu_ai/tunnel/*.conf;",
                           target: str = "/etc/edu_ai/tunnel/trust.conf") -> list:
    """Why the CIDR does NOT have exactly one source in this rendered configuration (empty list = it has one).

    `model` is the effective Compose model (`docker compose config --format json`, rendered with the installation's value),
    `confs` maps the nginx file names to their text, `subnet` is the value the administrator set. One variable must produce BOTH
    the subnet of the `tunnel` network and the nginx trust range, nginx.conf must carry no copy of its own, and the file must reach
    nginx. `allow_legacy` tolerates the one old literal range of rollout steps 1 and 2."""
    problems = []
    tunnel = (model.get("networks") or {}).get("tunnel")
    if not tunnel:
        return ["the model has no `tunnel` network"]
    if tunnel.get("internal") is not True:
        problems.append("`tunnel` is not internal")
    subs = [c.get("subnet") for c in ((tunnel.get("ipam") or {}).get("config") or [])]
    if subs != [subnet]:
        problems.append(f"the subnet of `tunnel` is {subs}, not the configured {subnet}")
    cfg = (model.get("configs") or {}).get("tunnel_trust")
    if not cfg:
        problems.append("the model has no `tunnel_trust` config")
    else:
        directives = re.findall(r"^\s*([A-Za-z_]+)\s+([^;]+);", strip_comments(cfg.get("content") or ""), re.M)
        trusted = [v.strip() for k, v in directives if k == "set_real_ip_from"]
        if trusted != [subnet]:
            problems.append(f"the trust config carries {trusted}, not the configured {subnet} (a second, independent constant)")
        if [k for k, _ in directives if k != "set_real_ip_from"]:
            problems.append("the trust config carries something besides set_real_ip_from")
    mounts = ((model.get("services") or {}).get("nginx") or {}).get("configs") or []
    if not any(isinstance(m, dict) and m.get("source") == "tunnel_trust" and m.get("target") == target for m in mounts):
        problems.append(f"nginx does not mount `tunnel_trust` at {target}")
    members = sorted(s for s, d in (model.get("services") or {}).items() if "tunnel" in (d.get("networks") or {}))
    if members != ["cloudflared", "nginx"]:
        problems.append(f"members of `tunnel`: {members}")
    for name, text in confs.items():
        d = strip_comments(text)
        literal = re.findall(r"^\s*set_real_ip_from\s+(\S+);", d, re.M)
        extra = [x for x in literal if not (allow_legacy and x == "172.16.0.0/12")]
        if extra:
            problems.append(f"{name} carries its own copy of the range: {extra}")
    if (confs.get("nginx.conf") is not None) and strip_comments(confs["nginx.conf"]).count(include_line) != 1:
        problems.append("nginx.conf does not include the Compose-provided trust file exactly once")
    return problems


def wiring_problems(model: dict) -> list:
    """The R2 wiring of the effective model: nginx on `edge` + `tunnel`, cloudflared on `app` + `tunnel` + `tunnel_egress` and NOT on `edge`,
    `tunnel_egress` has no other member and is not internal (it is cloudflared's only way out)."""
    svc = model.get("services") or {}
    nets = lambda name: sorted(((svc.get(name) or {}).get("networks") or {}).keys())      # noqa: E731
    problems = []
    if nets("nginx") != ["edge", "tunnel"]:
        problems.append(f"nginx is on {nets('nginx')}, expected ['edge', 'tunnel']")
    if nets("cloudflared") != ["app", "tunnel", "tunnel_egress"]:
        problems.append(f"cloudflared is on {nets('cloudflared')}, expected ['app', 'tunnel', 'tunnel_egress']")
    egress = sorted(s for s, d in svc.items() if "tunnel_egress" in (d.get("networks") or {}))
    if egress != ["cloudflared"]:
        problems.append(f"members of `tunnel_egress`: {egress}")
    if ((model.get("networks") or {}).get("tunnel_egress") or {}).get("internal"):
        problems.append("`tunnel_egress` is internal: cloudflared would have no way out")
    return problems


def model_subnet(model: dict) -> str:
    tunnel = (model.get("networks") or {}).get("tunnel") or {}
    subs = [c.get("subnet") for c in ((tunnel.get("ipam") or {}).get("config") or [])]
    if len(subs) != 1 or not subs[0]:
        raise ValueError(f"the model has no single subnet for `tunnel` (found {subs})")
    return str(parse_candidate(subs[0]))


# ── the exact change of a rollout step (R2-R4): current effective model -> candidate effective model ──
TRUST_TARGET = "/etc/edu_ai/tunnel/trust.conf"
NGINX_ALIAS = "edu_ai_nginx"


def _blank(v) -> bool:
    return v is None or v == {} or v == [] or v is False or v == ""


def model_paths(a, b, path: str = "") -> list:
    """Paths (never values: the model can hold secrets) at which two JSON values differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}.{k}" if path else str(k)
            out += [p] if (k not in a or k not in b) else model_paths(a[k], b[k], p)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [path or "."]
        return [x for i, (u, v) in enumerate(zip(a, b)) for x in model_paths(u, v, f"{path}[{i}]")]
    return [] if (a == b and type(a) is type(b)) else [path or "."]


def _attachments(model: dict, service: str) -> dict:
    """The networks of a service as {name: options}; Compose renders an attachment without options as null or {}."""
    nets = ((model.get("services") or {}).get(service) or {}).get("networks")
    if isinstance(nets, list):
        nets = {n: None for n in nets}
    return {k: ({} if v is None else v) for k, v in (nets or {}).items()}


def _set_attachments(model: dict, service: str, nets: dict) -> None:
    svc = (model.get("services") or {}).get(service)
    if isinstance(svc, dict):
        svc["networks"] = nets


def _added(model: dict, section: str, name: str, want: dict, project) -> list:
    """Remove `section.name` from `model` after checking it is exactly `want` (empty values and the generated name aside)."""
    got = (model.get(section) or {}).pop(name, None)
    if got is None:
        return [f"{section}.{name}"]
    got = dict(got)
    if got.pop("name", f"{project}_{name}") != f"{project}_{name}":
        return [f"{section}.{name}.name"]
    return [f"{section}.{name}.{p}" for p in model_paths(want, {k: v for k, v in got.items() if not _blank(v)})]


def step_delta_problems(cur: dict, cand: dict, step: int) -> list:
    """Every difference between the models that the step is NOT allowed to make (empty list = the exact change of the step).

    Steps 1 and 3 change no Compose key. Step 2 changes only: nginx joins `tunnel` under the alias edu_ai_nginx and mounts the
    `tunnel_trust` config; cloudflared leaves `edge` for `tunnel` and `tunnel_egress`; the three objects it adds are exactly as
    designed, the trust content is the one subnet of `tunnel`. Anything else, in any service or at the top level, is reported."""
    if step in (1, 3):
        return model_paths(cur, cand)
    if step != 2:
        return [f"step {step} is not a rollout step"]
    cur, cand = copy.deepcopy(cur), copy.deepcopy(cand)
    project = cand.get("name")
    try:
        subnet = model_subnet(cand)
    except ValueError:
        return ["networks.tunnel.ipam.config"]
    problems = []
    problems += _added(cand, "networks", "tunnel", {"driver": "bridge", "internal": True, "ipam": {"config": [{"subnet": subnet}]}}, project)
    problems += _added(cand, "networks", "tunnel_egress", {"driver": "bridge"}, project)
    problems += _added(cand, "configs", "tunnel_trust", {"content": f"set_real_ip_from {subnet};\n"}, project)
    if "configs" in cand and not cand["configs"]:
        del cand["configs"]
    nginx_c, nginx_n = _attachments(cur, "nginx"), _attachments(cand, "nginx")
    if nginx_n.pop("tunnel", None) != {"aliases": [NGINX_ALIAS]}:
        problems.append("services.nginx.networks.tunnel")
    cf_c, cf_n = _attachments(cur, "cloudflared"), _attachments(cand, "cloudflared")
    if cf_c.pop("edge", None) is None:
        problems.append("services.cloudflared.networks.edge")
    for new in ("tunnel", "tunnel_egress"):
        if cf_n.pop(new, None) != {}:
            problems.append(f"services.cloudflared.networks.{new}")
    for model, svc, nets in ((cur, "nginx", nginx_c), (cand, "nginx", nginx_n), (cur, "cloudflared", cf_c), (cand, "cloudflared", cf_n)):
        _set_attachments(model, svc, nets)
    nginx_cur, nginx_cand = ((m.get("services") or {}).get("nginx") or {} for m in (cur, cand))
    if not _blank(nginx_cur.get("configs")) or nginx_cand.get("configs") != [{"source": "tunnel_trust", "target": TRUST_TARGET}]:
        problems.append("services.nginx.configs")
    nginx_cur.pop("configs", None)
    nginx_cand.pop("configs", None)
    return problems + model_paths(cur, cand)


def preflight(text: str, state: State, *, project=None, reserved=()):
    """Judge the administrator's value. Returns (network, conflicts); raises ValueError for an invalid value."""
    net = parse_candidate(text)
    return net, conflicts(net, state, reserved=reserved, project=project)


def main_model(args, stdin) -> int:
    """model-subnet | model-check | render-trust | wiring: judgements of the effective model read from stdin."""
    try:
        model = json.load(stdin)
        if not isinstance(model, dict):
            raise ValueError("not a JSON object")
    except (ValueError, OSError) as e:
        print(f"REFUSED: the Compose model on stdin is unreadable ({type(e).__name__})")
        return 2
    if args.cmd == "model-delta":
        cur, cand = model.get("current"), model.get("candidate")
        if not isinstance(cur, dict) or not isinstance(cand, dict):
            print("REFUSED: stdin must be {\"current\": MODEL, \"candidate\": MODEL}")
            return 2
        problems = step_delta_problems(cur, cand, args.step)
        for p in problems:
            print(f"REFUSED: {p} differs from what step {args.step} may change")
        if problems:
            return 2
        print(f"OK: the candidate model is exactly the change of step {args.step}")
        return 0
    if args.cmd == "image":
        print(((model.get("services") or {}).get(args.service) or {}).get("image") or "")
        return 0
    if args.cmd == "wiring":
        print(*sorted(((model.get("services") or {}).get(args.service) or {}).get("networks") or {}), sep=chr(10))
        return 0
    if args.cmd == "render-trust":
        content = ((model.get("configs") or {}).get("tunnel_trust") or {}).get("content")
        if not content:
            print("REFUSED: the model has no `tunnel_trust` config")
            return 2
        sys.stdout.write(content)
        return 0
    try:
        subnet = model_subnet(model)
    except ValueError as e:
        print(f"REFUSED: {e}")
        return 2
    if args.cmd == "model-subnet":
        print(subnet)
        return 0
    confs = {}
    for name in ("nginx.conf", "search_api.conf"):
        path = pathlib.Path(args.conf_dir) / name
        try:
            confs[name] = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"REFUSED: cannot read {path} ({type(e).__name__})")
            return 2
    problems = wiring_problems(model) + single_source_problems(model, confs, subnet, allow_legacy=args.allow_legacy)
    for p in problems:
        print(f"REFUSED: {p}")
    if problems:
        return 2
    print(f"OK: one source {subnet}; wiring as designed")
    return 0


def main(argv=None, runner: Runner | None = None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("model-subnet", "model-check", "model-delta", "render-trust", "wiring", "image"):
        p = sub.add_parser(name)
        if name == "model-delta":
            p.add_argument("--step", type=int, required=True, choices=(1, 2, 3), help="the rollout step whose exact change is allowed")
        if name == "model-check":
            p.add_argument("--conf-dir", required=True, help="the nginx/conf directory of the candidate")
            p.add_argument("--allow-legacy", action="store_true", help="tolerate the old trust line 172.16.0.0/12 (rollout step 2)")
        if name in ("wiring", "image"):
            p.add_argument("service")
    for name in ("allocate", "check"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=(name == "allocate"), help="Compose project of the installation")
        p.add_argument("--routes", choices=("host", "docker"), default="host")
        p.add_argument("--helper-image", default=None)
        p.add_argument("--helper-network", default="host", help="network of the helper container (default: the daemon's own; tests use container:NAME)")
        p.add_argument("--reserved", action="append", default=[], metavar="CIDR", help="a range the host or the site uses (LAN, VPN, VPC)")
        if name == "check":
            p.add_argument("cidr")
    args = ap.parse_args(argv)
    if args.cmd in ("model-subnet", "model-check", "model-delta", "render-trust", "wiring", "image"):
        return main_model(args, stdin or sys.stdin)
    try:
        state = gather(runner or run_cmd, routes=args.routes, helper_image=args.helper_image, helper_network=args.helper_network)
    except Unverifiable as e:
        print(f"REFUSED (cannot verify): {e}")
        return 3
    except Exception as e:  # noqa: BLE001 - whatever else goes wrong while reading the host is still "cannot verify", never a crash
        print(f"REFUSED (cannot verify): unexpected {type(e).__name__} while reading the host state")
        return 3
    try:
        if args.cmd == "allocate":
            net = allocate(args.project, state, reserved=args.reserved)
            print(f"EDU_AI_TUNNEL_SUBNET={net}")
            return 0
        net, found = preflight(args.cidr, state, project=args.project, reserved=args.reserved)
    except (ValueError, NoFreeSubnet) as e:
        print(f"REFUSED: {e}")
        return 2
    if found:
        for c in found:
            print(f"REFUSED {c.kind}: {c.detail}")
        return 2
    print(f"OK: {net} is free in everything this tool can see (re-run on every create/update)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
