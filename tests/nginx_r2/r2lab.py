#!/usr/bin/env python3
"""R2 lab: who may tell nginx the client address. Standard library only; needs Docker (except `selfcheck`, `render`).

    python tests/nginx_r2/r2lab.py selfcheck              no Docker: the checks' own failure handling and the offline tests
    python tests/nginx_r2/r2lab.py static                 nginx -t and the compose/config delta of the proposal
    python tests/nginx_r2/r2lab.py run [--out FILE] [--asgi-image api=IMAGE ...]
    python tests/nginx_r2/r2lab.py cleanup --run-id ID [--dry-run]   remove what run ID owns (labels verified), nothing else
    python tests/nginx_r2/r2lab.py cleanup --list-runs                    list run ids found on labelled resources (read-only)

What is under test (see docs/plans/r2-trust-boundary-proposal.md):
  baseline    the committed files production runs: set_real_ip_from 172.16.0.0/12 on port 81, XFF appended
  candidate   the proposal (tests/nginx_r2/candidate.py derives it from git, never hand-edited): the trusted network's CIDR is
              the installation's own setting EDU_AI_TUNNEL_SUBNET, allocated and checked by tunnel_subnet.py, and Compose gives
              it to the network AND to nginx's trust range (one source)
  transition  rollout steps (both ranges trusted) and mut_* (deliberately wrong candidates: the checks must notice)

Every lab resource carries the labels edu_ai.purpose / edu_ai.run / edu_ai.project and is removed only after those labels (and for
Compose resources the exact project of the run) have been verified (ownership.py); `cleanup` needs a concrete --run-id.

Four layers are recorded for every case and never confused with each other:
  TCP peer      the container address nginx actually sees (proved by a paired request WITHOUT the header)
  headers       what the sender put on the request
  nginx client  $remote_addr (the first field of nginx's own access log line)
  application   what the upstream receives (echo stub) and, for ASGI, what real uvicorn makes of it

No production access, no .env, no key, no tunnel credential, no LLM: everything runs with an EMPTY env file, synthetic
upstreams, the production nginx image and own labelled networks/containers. A probe that fails, a missing answer or a test
that could not run is ERROR / NOT_RUN, never PASS. Exit status 0 only when every check is PASS.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tests" / "nginx_s2"))
sys.path.insert(0, str(HERE))
import labkit as L  # noqa: E402  (S2 lab helpers: docker run/exec, strict probes, echo parsing)
import candidate as C  # noqa: E402
import ownership as OW  # noqa: E402
import tunnel_subnet as TS  # noqa: E402
import offline_tests as OFF  # noqa: E402

PURPOSE = OW.PURPOSE
PROD_NGINX_IMAGE_ID = os.environ.get("EDU_AI_LAB_NGINX_ID", "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10")  # production unless rehearsing a bump
NGINX_NAME = "edu_ai_nginx"          # the DNS name every client and the tunnel ingress use
SPOOF_CF = "203.0.113.77"            # TEST-NET-3 addresses: nothing in the lab has them
LEGIT_CF = "203.0.113.9"
VICTIM = "198.51.100.50"
FAKE_CHAIN = "198.51.100.8, 198.51.100.9"
LEGACY_RANGE = "172.16.0.0/12"       # the old trust line, tolerated only in rollout steps 1 and 2


class Refused(L.LabError):
    """The lab refused to continue (a collision, a foreign holder of a planned name): nothing was created or touched."""


def lab_runner(cmd, check=False, **kw):
    return L.run(cmd, check=check, **kw)


class NotRun(Exception):
    """A check that could not run (missing image, ...). Never a PASS."""


# ── report ─────────────────────────────────────────────────────────────
class Report:
    def __init__(self, quiet=False):
        self.rows, self.obs, self.quiet = [], [], quiet

    def check(self, cid, desc, fn):
        t0 = time.time()
        try:
            detail, status = fn(), "PASS"
            detail = "" if detail is None else str(detail)
        except AssertionError as e:
            detail, status = str(e), "FAIL"
        except NotRun as e:
            detail, status = str(e), "NOT_RUN"
        except Exception as e:  # noqa: BLE001 - anything else is a lab problem, never a pass
            detail, status = f"{type(e).__name__}: {e}", "ERROR"
            if not isinstance(e, (L.LabError, subprocess.TimeoutExpired, OSError, ValueError, KeyError, json.JSONDecodeError)):
                detail += " | " + traceback.format_exc().strip().splitlines()[-3].strip()
        row = {"id": cid, "desc": desc, "status": status, "detail": (detail or "")[:900], "seconds": round(time.time() - t0, 1)}
        self.rows.append(row)
        if not self.quiet:
            print(f"  {status:7} {cid}  {desc}" + (f"\n          {row['detail']}" if status != "PASS" or row["detail"] else ""), flush=True)
        return status == "PASS"

    def observe(self, oid, desc, value):
        self.obs.append({"id": oid, "desc": desc, "value": value})
        if not self.quiet:
            print(f"  OBS     {oid}  {desc}: {value}", flush=True)

    def summary(self):
        c = collections.Counter(r["status"] for r in self.rows)
        return {k: c.get(k, 0) for k in ("PASS", "FAIL", "ERROR", "NOT_RUN")} | {"total": len(self.rows)}

    def ok(self):
        return all(r["status"] == "PASS" for r in self.rows) and bool(self.rows)


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── pure judgement helpers (exercised by selfcheck without Docker) ──────
def echo_view(res):
    """The echo stub's view of a forwarded request, from a client result.

    A transport failure raises TransportError (ERROR), a non-200 raises AssertionError (FAIL): there is
    no path on which a missing or failed answer is read as a value."""
    if "error" in res:
        raise L.TransportError(f"case {res.get('id')}: {res['error']}")
    must(res.get("status") == 200, f"case {res.get('id')}: status {res.get('status')} != 200")
    return L.upstream_saw(res)


def hdr(echo, name):
    return L.header_values(echo, name)


def real_ip(echo):
    v = hdr(echo, "x-real-ip")
    must(len(v) == 1, f"expected exactly one X-Real-IP at the upstream, got {v}")
    return v[0]


def xff_chain(echo):
    v = hdr(echo, "x-forwarded-for")
    must(len(v) == 1, f"expected exactly one X-Forwarded-For at the upstream, got {v}")
    return [x.strip() for x in v[0].split(",")]


def is_429(status):
    return status == 429


# ── docker plumbing ────────────────────────────────────────────────────
def net_ips(container):
    p = L.run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container])
    nets = json.loads(p.stdout)
    return {n: v["IPAddress"] for n, v in nets.items() if v.get("IPAddress")}


def resolve(container, name):
    """IPv4 addresses `name` resolves to INSIDE `container` (a failed lookup raises)."""
    p = L.docker_exec(container, "python", "-c",
                      "import socket,sys;print(' '.join(sorted({a[4][0] for a in socket.getaddrinfo(sys.argv[1],None,socket.AF_INET)})))",
                      name)
    ips = p.stdout.split()
    if not ips:
        raise L.LabError(f"{container}: {name} did not resolve")
    return ips


class R2Lab:
    def __init__(self, ref=C.S2_BASE, reserved=()):
        self.run_id = time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]
        assert OW.valid_run_id(self.run_id), self.run_id
        self.prefix = f"b04r2_{self.run_id}"
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix=self.prefix + "_"))
        self.empty_env = self.tmp / "empty.env"
        self.empty_env.write_text("")
        self.files = C.variants(ref)
        self.reserved = list(reserved)
        self.containers, self.variants = [], []

    # naming and labels -------------------------------------------------------------
    def labels(self, project=None):
        out = ["--label", f"{OW.LABEL_PURPOSE}={PURPOSE}", "--label", f"{OW.LABEL_RUN}={self.run_id}"]
        if project:
            out += ["--label", f"{OW.LABEL_PROJECT}={project}"]
        return out

    def label_map(self, project):
        return {OW.LABEL_PURPOSE: PURPOSE, OW.LABEL_RUN: self.run_id, OW.LABEL_PROJECT: project}

    def name(self, what):
        return f"{self.prefix}_{what}"

    def project_for(self, instance):
        return self.name("p_" + instance).lower()

    # the per-installation subnet ---------------------------------------------------------
    def subnet_state(self):
        return TS.gather(lab_runner, routes="docker", helper_image=L.NGINX_IMAGE, helper_labels=self.labels())

    def allocate_subnet(self, project):
        return TS.allocate(project, self.subnet_state(), reserved=self.reserved)

    def preflight_subnet(self, project, text):
        net, found = TS.preflight(text, self.subnet_state(), project=project, reserved=self.reserved)
        if found:
            raise Refused("subnet preflight refused " + text + ": " + "; ".join(f"{c.kind}: {c.detail}" for c in found))
        return net

    def variant(self, key, name=None, subnet=None):
        """A started-later installation of the variant `key` under the instance name `name`. A variant whose compose file needs
        EDU_AI_TUNNEL_SUBNET gets its OWN subnet, allocated and preflighted against the real host state first: a refusal raises
        Refused before any resource of this installation exists."""
        name = name or key
        files = self.files[key]
        project = self.project_for(name)
        sub = None
        if C.uses_subnet_variable(files):
            sub = subnet or str(self.allocate_subnet(project))
            self.preflight_subnet(project, sub)
        v = RVariant(self, name, files, subnet=sub)
        self.variants.append(v)
        return v

    # removal: only what the labels prove to be ours ----------------------------------------
    def safe_rm(self, name):
        """Remove ONE container this run owns, found by its exact name. A foreign holder of the name raises Refused and stays."""
        return OW.safe_remove(lab_runner, "container", name, self.run_id)

    def rm(self, *containers):
        for c in containers:
            try:
                self.safe_rm(c)
            finally:
                if c in self.containers:
                    self.containers.remove(c)

    def finish_variant(self, vname):
        """Remove the stand-ins of one installation, then its Compose project (frees its subnet; other installations stay)."""
        err = None
        for c in [c for c in self.containers if c.startswith(f"{self.prefix}_{vname}_")]:
            try:
                self.rm(c)
            except OW.OwnershipError as e:
                err = err or str(e)
        for v in self.variants:
            if v.name == vname:
                e = v.down()
                err = err or e
        return err

    def leftovers(self):
        """What THIS run still owns (by labels only)."""
        found = {}
        for kind in OW.ORDER:
            ids = OW.list_ids(lab_runner, kind, self.run_id)
            if ids:
                found[kind + "s"] = ids
        return found

    def teardown(self):
        errors = []
        for v in reversed(self.variants):
            e = v.down()
            if e:
                errors.append(e)
        res = OW.cleanup_run(lab_runner, self.run_id)
        if not res.ok():
            errors.append(f"cleanup kept or failed: kept={res.kept} errors={res.errors}")
        left = self.leftovers()
        try:
            L.rmtree(self.tmp)
        except OSError as e:
            errors.append(f"temp directory not removed: {e}")
        if left or errors:
            raise L.LabError(f"teardown left resources: {left} {errors}")


class RVariant:
    """One installation under test: the REAL `nginx` service of the variant's docker-compose.yml, started with
    `docker compose up --no-deps nginx`. Only the container name, the (absent) host ports, the DNS alias edu_ai_nginx and the
    lab's labels are overridden; networks, subnets and `internal` come from the file under test, and EDU_AI_TUNNEL_SUBNET (when
    the file needs it) is this installation's own value, passed in a per-installation env file (never a real .env)."""

    def __init__(self, lab, name, files, subnet=None):
        self.lab, self.name, self.subnet = lab, name, subnet
        self.dir = lab.tmp / name
        for path, data in files.items():
            dest = self.dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        (self.dir / ".env").write_text("")
        self.project = lab.project_for(name)
        self.container = lab.name("nginx_" + name)
        self.env_file = lab.tmp / f"{name}.env"
        self.env_file.write_text(f"{C.SUBNET_VAR}={subnet}\n" if subnet else "")
        cfg = self.config(override=False)
        self.nginx_nets = list((cfg["services"]["nginx"].get("networks") or {}).keys())
        self.cf_nets = list((cfg["services"]["cloudflared"].get("networks") or {}).keys())
        self.declared = list((cfg.get("networks") or {}).keys())
        self.override = lab.tmp / f"override_{name}.yaml"
        lbl = "".join(f'      {k}: "{v}"\n' for k, v in lab.label_map(self.project).items())
        aliases = "".join(f"      {n}:\n        aliases: [{NGINX_NAME}]\n" for n in self.nginx_nets)
        nets = "".join(f"  {n}:\n    labels:\n{lbl}" for n in self.declared)
        self.override.write_text("services:\n  nginx:\n"
                                 f"    container_name: {self.container}\n"
                                 "    ports: !override []\n"
                                 f"    labels:\n{lbl}"
                                 f"    networks: !override\n{aliases}"
                                 f"networks:\n{nets}")
        lab.containers.append(self.container)

    def compose(self, *args, override=True, check=True, env=True):
        cmd = ["docker", "compose", "-p", self.project, "--project-directory", self.dir,
               "--env-file", self.env_file if env else self.lab.empty_env, "-f", self.dir / "docker-compose.yml"]
        if override and hasattr(self, "override"):
            cmd += ["-f", self.override]
        return L.run(cmd + list(args), check=check, timeout=300)

    def config(self, override=True):
        return json.loads(self.compose("config", "--format", "json", override=override).stdout)

    def net(self, key):
        return f"{self.project}_{key}"

    def preflight_names(self):
        """A planned name that is already taken is NOT adopted and NOT touched: the lab refuses before it creates anything."""
        for key in self.declared:
            if L.run(["docker", "network", "inspect", self.net(key)], check=False).returncode == 0:
                raise Refused(f"the planned network name {self.net(key)} already exists: refusing, it is not touched")
        if L.run(["docker", "inspect", "--type", "container", self.container], check=False).returncode == 0:
            raise Refused(f"the planned container name {self.container} already exists: refusing, it is not touched")

    def up(self):
        self.preflight_names()
        self.compose("up", "-d", "--no-deps", "nginx")
        self.ensure_networks()

    def ensure_networks(self):
        """`up --no-deps nginx` creates only the networks nginx itself uses. The stand-ins need the others (`app`,
        `tunnel_egress`, ...), so they are created here from the EFFECTIVE model (driver, internal, subnet) with the labels
        Compose uses plus the lab's. A network that exists must already be ours (labels verified), or the lab refuses."""
        for key, spec in (self.config(override=False).get("networks") or {}).items():
            name = self.net(key)
            if L.run(["docker", "network", "inspect", name], check=False).returncode == 0:
                ok, why = OW.owned(OW.get_labels(lab_runner, "network", name), self.lab.run_id)
                if not ok:
                    raise Refused(f"network {name} exists but is not owned by this run ({why}): not used, not touched")
                continue
            cmd = ["docker", "network", "create", "--driver", spec.get("driver") or "bridge"]
            if spec.get("internal"):
                cmd.append("--internal")
            for c in (spec.get("ipam") or {}).get("config") or []:
                if c.get("subnet"):
                    cmd += ["--subnet", c["subnet"]]
            cmd += ["--label", f"{OW.COMPOSE_PROJECT}={self.project}", "--label", f"com.docker.compose.network={key}",
                    *self.lab.labels(self.project), name]
            L.run(cmd)

    def down(self):
        """`compose down` for THIS project, but only after every container, network and volume that carries the project label
        has been verified as owned by this run. Anything else leaves the project untouched and is reported."""
        err = None
        if self.dir.exists():
            for kind, ls in (("container", ["docker", "ps", "-aq"]), ("network", ["docker", "network", "ls", "-q"]),
                             ("volume", ["docker", "volume", "ls", "-q"])):
                p = L.run(ls + ["--filter", f"label={OW.COMPOSE_PROJECT}={self.project}"], check=False)
                if p.returncode != 0:
                    return f"cannot list {kind}s of project {self.project}: compose down not run"
                for ident in p.stdout.split():
                    try:
                        ok, why = OW.owned(OW.get_labels(lab_runner, kind, ident), self.lab.run_id)
                    except (OW.OwnershipError, FileNotFoundError) as e:
                        return f"cannot verify {kind} {ident} of project {self.project} ({e}): compose down not run"
                    if not ok:
                        return f"{kind} {ident} carries project {self.project} but is not owned by this run ({why}): compose down not run"
            p = self.compose("down", "-v", "--remove-orphans", "-t", "2", check=False)
            if p.returncode != 0:
                err = f"compose down {self.project}: {p.stderr.strip()[-200:]}"
        try:
            self.lab.safe_rm(self.container)
        except OW.OwnershipError as e:
            err = err or str(e)
        return err

    def ips(self):
        return net_ips(self.container)

    def nginx_ip(self, key):
        return self.ips()[self.net(key)]

    def log_lines(self):
        p = L.run(["docker", "logs", self.container], check=False)
        return (p.stdout + p.stderr).splitlines()

    def remote_addr_logged(self, marker):
        """nginx's own client address ($remote_addr, first access-log field) for the request carrying ?m=<marker>."""
        for line in reversed(self.log_lines()):
            if f"m={marker} " in line or f"m={marker}&" in line or f"m={marker}\"" in line:
                m = re.match(r"^(\S+) - \S+ \[", line)
                if m:
                    return m.group(1)
        raise L.LabError(f"no access-log line for marker {marker}")


def start_peer(lab, v, role, nets, aliases=(), image=None, cmd=("sleep", "infinity"), extra=(), mounts=()):
    """A container on the installation's networks (first via --network, the rest via `network connect`). A leftover of THIS run under
    the same name is replaced; a foreign holder of the name makes the lab refuse (safe_rm), it is never removed."""
    cname = lab.name(f"{v.name}_{role}")
    lab.safe_rm(cname)
    first = ["--network", v.net(nets[0]), *sum((["--network-alias", a] for a in aliases), [])]
    # The Compose project label is set EXPLICITLY to this installation's project: an image built by Compose (edu_ai_api:1.0) hands its
    # own `com.docker.compose.project` to every container started from it, and ownership rightly refuses a project that is not ours.
    L.run(["docker", "run", "-d", "--name", cname, *first, *lab.labels(v.project), "--label", f"{OW.COMPOSE_PROJECT}={v.project}",
           *extra, *mounts, image or L.PY_IMAGE, *cmd])
    lab.containers.append(cname)
    for n in nets[1:]:
        L.run(["docker", "network", "connect", *sum((["--alias", a] for a in aliases), []), v.net(n), cname])
    return cname


def start_client(lab, v, role, nets, aliases=()):
    cname = start_peer(lab, v, role, nets, aliases)
    L.run(["docker", "cp", str(L.HERE / "client.py"), f"{cname}:/client.py"])
    return cname


def start_stubs(lab, v, group):
    """Synthetic upstreams (the S2 echo stub): one container per group, every upstream name as a network alias on `edge`."""
    groups = {"main": ["edu_ai", "edu_ai_pgadmin", "edu_ai_web_chat", "edu_ai_qa_admin"], "ab": ["edu_ai_api", "edu_ai_search"]}
    names = groups[group]
    aliases = list(names) + (["pgadmin"] if "edu_ai_pgadmin" in names else [])
    script = " & ".join(f"python /stub.py {n} {L.UPSTREAMS[n][1]}" for n in names) + " & wait"
    cname = start_peer(lab, v, f"stubs_{group}", ["edge"], aliases,
                       cmd=("sh", "-c", script), mounts=["--mount", f"type=bind,source={L.HERE / 'stub.py'},target=/stub.py,readonly"])
    for n in names:
        deadline = time.time() + 20
        while True:
            p = L.run(["docker", "exec", cname, "python", "-c",
                       f"import socket;socket.create_connection(('127.0.0.1',{L.UPSTREAMS[n][1]}),2)"], check=False)
            if p.returncode == 0:
                break
            if time.time() > deadline:
                raise L.LabError(f"stub {n} not listening: {p.stderr.strip()[:200]}")
            time.sleep(0.4)
    return cname


def cases(*cs, pace=0):
    return {"cases": list(cs), "pace": pace}


def case(cid, target, port, path, headers=(), method="GET", body=None):
    hs = [list(h) for h in headers]
    if not any(h[0].lower() == "host" for h in hs):
        hs.insert(0, ["Host", "edu.example"])
    c = {"id": cid, "target": target, "port": port, "method": method, "path": path, "headers": hs, "timeout": 20}
    if body:
        c["body"] = body
    return c


def cli(lab, cname, *cs, pace=0):
    p = L.docker_exec(cname, "python", "/client.py", "cases", input=json.dumps(cases(*cs, pace=pace)) + "\n")
    try:
        return json.loads(p.stdout)
    except Exception as e:  # noqa: BLE001
        raise L.LabError(f"client output is not JSON ({e}): {p.stdout[:200]!r} {p.stderr[:200]!r}")


def one(lab, cname, target, port, path, headers=(), **kw):
    return cli(lab, cname, case("c", target, port, path, headers, **kw))[0]


def wait_serving(lab, cname, timeout=45):
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            r = one(lab, cname, NGINX_NAME, 80, "/health")
            if r.get("status") == 200:
                return
            last = r
        except L.LabError as e:
            last = str(e)
        time.sleep(0.7)
    raise L.NotHealthy(f"nginx did not answer /health in {timeout}s: {last}")


# ── the scenarios ──────────────────────────────────────────────────────
class Scene:
    """A started variant with its stand-ins; every method returns a detail string or raises."""

    def __init__(self, lab, v):
        self.lab, self.v = lab, v
        self.trust_net = "tunnel" if "tunnel" in v.cf_nets else "edge"     # where the real proxy reaches nginx
        v.up()
        self.stubs = [start_stubs(lab, v, "main"), start_stubs(lab, v, "ab")]
        self.edge = start_client(lab, v, "attacker_edge", ["edge"])          # any container on edge: API, Langflow, ...
        self.app = start_client(lab, v, "attacker_app", ["app"])             # a container on app only
        self.cf = start_client(lab, v, "cloudflared", v.cf_nets, aliases=["cloudflared"])   # stand-in, networks from the file
        wait_serving(lab, self.edge)
        L.wait_healthy(v.container, timeout=60)
        self.peers = {"nginx": v.ips(), "attacker_edge": net_ips(self.edge), "attacker_app": net_ips(self.app), "cloudflared": net_ips(self.cf)}

    def ip(self, who, key):
        return self.peers[who][self.v.net(key)]

    # a request and its four layers -------------------------------------------------
    def sees(self, client, port, path, headers=(), marker=None, target=NGINX_NAME):
        marker = marker or uuid.uuid4().hex[:8]
        sep = "&" if "?" in path else "?"
        echo = echo_view(one(self.lab, client, target, port, f"{path}{sep}m={marker}", headers))
        return echo, marker

    def peer_probe(self, client, key, port=81):
        """Paired request WITHOUT any proxy header: what nginx records is the real TCP peer. Must equal the address Docker
        gave that container on `key`; otherwise every conclusion below about 'the peer' would be an assumption."""
        echo, m = self.sees(client, port, "/search/health", target=NGINX_NAME)
        got = real_ip(echo)
        want = net_ips(client).get(self.v.net(key))
        must(want is not None and got == want, f"peer probe: nginx saw {got}, docker says {want}")
        logged = self.v.remote_addr_logged(m)
        must(logged == got, f"access log client {logged} != upstream X-Real-IP {got}")
        return got

    # positive: the real proxy keeps the client address ------------------------------
    def proxy_keeps_client(self, client=None, key=None):
        client, key = client or self.cf, key or self.trust_net
        peer = self.peer_probe(client, key)
        echo, m = self.sees(client, 81, "/search/health", [("CF-Connecting-IP", LEGIT_CF), ("X-Forwarded-For", LEGIT_CF)])
        must(real_ip(echo) == LEGIT_CF, f"CF-Connecting-IP {LEGIT_CF} from the real proxy (peer {peer}) was not honoured: X-Real-IP {real_ip(echo)}")
        must(self.v.remote_addr_logged(m) == LEGIT_CF, "access log did not record the client address")
        return f"peer {peer} -> nginx client {LEGIT_CF}; upstream X-Forwarded-For {hdr(echo, 'x-forwarded-for')}"

    def spoof_81_from_edge(self, expect_honoured):
        peer = self.peer_probe(self.edge, "edge")
        echo, m = self.sees(self.edge, 81, "/search/health", [("CF-Connecting-IP", SPOOF_CF)])
        got = real_ip(echo)
        if expect_honoured:
            must(got == SPOOF_CF, f"R2 not reproduced: peer {peer} sent CF-Connecting-IP {SPOOF_CF}, upstream X-Real-IP {got}")
            return f"REPRODUCED: peer {peer} (edge member) forged client {SPOOF_CF}; nginx logged {self.v.remote_addr_logged(m)}"
        must(got == peer, f"forged CF-Connecting-IP {SPOOF_CF} from edge peer {peer} changed the client: X-Real-IP {got}")
        must(self.v.remote_addr_logged(m) == peer, "access log recorded a forged address")
        return f"peer {peer} forged {SPOOF_CF}: ignored, client stays {got}"

    def spoof_other_listeners(self):
        out = []
        for port, path in ((80, "/search/health"), (8443, "/pgadmin/"), (8444, "/")):
            echo, _ = self.sees(self.edge, port, path, [("CF-Connecting-IP", SPOOF_CF), ("X-Real-IP", "198.51.100.9")])
            got = real_ip(echo)
            must(got == self.peers["attacker_edge"][self.v.net("edge")], f":{port} forged headers changed X-Real-IP to {got}")
            out.append(f":{port} ok")
        return ", ".join(out)

    def xff_chain(self, expect_kept):
        peer = self.peers["attacker_edge"][self.v.net("edge")]
        echo, _ = self.sees(self.edge, 80, "/search/health", [("X-Forwarded-For", FAKE_CHAIN)])
        chain = xff_chain(echo)
        if expect_kept:
            must(chain[:2] == ["198.51.100.8", "198.51.100.9"], f"baseline did not keep the caller's chain: {chain}")
            return f"caller-controlled prefix reaches the application: {chain}"
        must(chain == [peer], f"X-Forwarded-For is not exactly the TCP peer {peer}: {chain}")
        return f"chain dropped, X-Forwarded-For = {chain}"

    def xff_via_proxy(self, expect_single):
        echo, _ = self.sees(self.cf, 81, "/search/health", [("CF-Connecting-IP", LEGIT_CF), ("X-Forwarded-For", LEGIT_CF)])
        chain = xff_chain(echo)
        if expect_single:
            must(chain == [LEGIT_CF], f"expected the single client address, got {chain}")
        return f"X-Forwarded-For {chain}"

    def proto_and_real_ip_overwritten(self):
        out = []
        for port, path in ((80, "/search/health"), (81, "/search/health"), (8443, "/pgadmin/")):
            echo, _ = self.sees(self.edge, port, path, [("X-Forwarded-Proto", "https"), ("X-Real-IP", "198.51.100.9")])
            must(hdr(echo, "x-forwarded-proto") == ["http"], f":{port} X-Forwarded-Proto reached the upstream as {hdr(echo, 'x-forwarded-proto')}")
            must(real_ip(echo) == self.peers["attacker_edge"][self.v.net("edge")], f":{port} caller X-Real-IP survived: {real_ip(echo)}")
            out.append(f":{port}")
        return "X-Forwarded-Proto is nginx's scheme (http) and X-Real-IP is nginx's own on " + ", ".join(out)

    def passthrough_observation(self):
        echo, _ = self.sees(self.edge, 80, "/search/health", [("CF-Connecting-IP", SPOOF_CF), ("Forwarded", "for=198.51.100.10"), ("True-Client-IP", "198.51.100.11")])
        return {"CF-Connecting-IP": hdr(echo, "cf-connecting-ip"), "Forwarded": hdr(echo, "forwarded"), "True-Client-IP": hdr(echo, "true-client-ip")}

    # impact on the rate limit --------------------------------------------------------
    def flood(self, client, headers_for, n, port=81):
        res = cli(self.lab, client, *[case(f"f{i}", NGINX_NAME, port, "/search/health", headers_for(i)) for i in range(n)])
        bad = [r for r in res if "error" in r]
        must(not bad, f"{len(bad)} flood requests failed: {bad[0].get('error') if bad else ''}")
        statuses = collections.Counter(r["status"] for r in res)
        must(set(statuses) <= {200, 429}, f"unexpected statuses in the flood: {dict(statuses)}")
        return statuses

    def rate_limit_rotating(self, expect_bypass):
        try:
            st = self.flood(self.edge, lambda i: [("CF-Connecting-IP", f"203.0.113.{100 + i % 100}")], 100)
        finally:
            time.sleep(3)           # the limit refills at 10 r/s; later checks must start from a full bucket
        if expect_bypass:
            must(st.get(429, 0) == 0, f"expected the per-IP limit to be bypassed by rotating forged addresses, got {dict(st)}")
            return f"REPRODUCED: 100 requests from ONE peer with 100 forged client addresses, no 429 ({dict(st)})"
        must(st.get(429, 0) >= 10, f"forged rotating addresses still bypass the limit: {dict(st)}")
        return f"limit holds for the real peer: {dict(st)}"

    def victim_lockout(self, expect_locked):
        self.flood(self.edge, lambda i: [("CF-Connecting-IP", VICTIM)], 100)
        try:
            # the 5 probes go back to back right after the flood, so the bucket cannot refill between them
            res = cli(self.lab, self.cf, *[case(f"v{i}", NGINX_NAME, 81, "/search/health", [("CF-Connecting-IP", VICTIM)]) for i in range(5)])
        finally:
            time.sleep(3)
        bad = [r for r in res if "error" in r]
        must(not bad, f"victim probes failed: {bad[0].get('error') if bad else ''}")
        st = collections.Counter(r["status"] for r in res)
        must(set(st) <= {200, 429}, f"unexpected statuses for the victim: {dict(st)}")
        if expect_locked:
            must(st.get(429, 0) >= 1, f"expected the victim {VICTIM} to be rate limited by the forger, got {dict(st)}")
            return f"REPRODUCED: a legitimate client {VICTIM} got {dict(st)} after the forger exhausted its bucket"
        must(st.get(429, 0) == 0, f"a forger on another peer rate limited the victim: {dict(st)}")
        return f"victim unaffected: {dict(st)}"

    # topology -------------------------------------------------------------------------
    def app_network_cannot_reach_nginx(self):
        kinds = {p: L.probe_tcp(self.app, NGINX_NAME, p) for p in (80, 81, 8443, 8444)}
        must(all(k in ("DNS_FAIL", "TIMEOUT", "UNREACHABLE") for k in kinds.values()), f"a client on `app` only reaches nginx: {kinds}")
        return f"app-only client -> nginx: {kinds}"

    def tunnel_unreachable_from_edge(self):
        ip = self.ip("nginx", "tunnel")
        kind = L.probe_tcp(self.edge, ip, 81)
        must(kind in ("TIMEOUT", "UNREACHABLE"), f"edge member -> nginx tunnel address {ip}:81 is {kind}")
        return f"edge member -> {ip}:81: {kind}"

    def dns_from_proxy(self, expect):
        got = resolve(self.cf, NGINX_NAME)
        must(got == [expect], f"{NGINX_NAME} resolves to {got}, expected only {expect}")
        return f"{NGINX_NAME} -> {got} inside the proxy"

    def proxy_on_wrong_network_not_trusted(self):
        cf_edge = start_client(self.lab, self.v, "cf_edge_only", ["edge"], aliases=["cloudflared"])
        try:
            peer = self.peer_probe(cf_edge, "edge")
            echo, _ = self.sees(cf_edge, 81, "/search/health", [("CF-Connecting-IP", LEGIT_CF)])
            must(real_ip(echo) == peer, f"a proxy on `edge` (peer {peer}) was trusted: X-Real-IP {real_ip(echo)}")
            return f"proxy at {peer} on `edge`: header ignored, client {real_ip(echo)} (fails safe: shared limit, never a forged address)"
        finally:
            self.lab.rm(cf_edge)

    def dual_network_observation(self):
        """Observation, not a verdict: a proxy left on BOTH `edge` and `tunnel`. Shows why the proposal takes cloudflared off `edge`:
        the name edu_ai_nginx then has an address on each shared network, and which one a connection uses decides whether the
        header is trusted."""
        dual = start_client(self.lab, self.v, "cf_dual", ["edge", "tunnel"], aliases=["cloudflared"])
        try:
            ips = resolve(dual, NGINX_NAME)
            seen = collections.Counter()
            for i in range(12):
                echo, _ = self.sees(dual, 81, "/search/health", [("CF-Connecting-IP", LEGIT_CF)])
                seen["trusted" if real_ip(echo) == LEGIT_CF else "ignored"] += 1
            return f"{NGINX_NAME} resolves to {ips} for a proxy on edge+tunnel; 12 requests: {dict(seen)}"
        finally:
            self.lab.rm(dual)

    def proxy_replaced_by_other_ip(self):
        key = self.trust_net
        old_ip = self.ip("cloudflared", key)
        new = start_client(self.lab, self.v, "cloudflared_new", self.v.cf_nets, aliases=["cloudflared"])      # started while the old one lives
        new_ip = net_ips(new)[self.v.net(key)]
        must(new_ip != old_ip, f"replacement got the same address {new_ip}; the test would prove nothing")
        self.lab.rm(self.cf)
        self.cf, self.peers["cloudflared"] = new, net_ips(new)
        detail = self.proxy_keeps_client(new, key)
        return f"proxy replaced: {old_ip} -> {new_ip} on {key}; {detail}"

    def tunnel_off(self):
        self.lab.rm(self.cf)
        L.wait_healthy(self.v.container, timeout=30)
        rc = int(L.run(["docker", "inspect", "--format", "{{.RestartCount}}", self.v.container]).stdout.strip())
        must(rc == 0, f"nginx restarted {rc}x")
        must(one(self.lab, self.edge, NGINX_NAME, 80, "/health").get("status") == 200, "nginx /health on 80 failed with the tunnel off")
        out = [f"nginx healthy, RestartCount 0, :80 /health 200"]
        if "tunnel" in self.v.nginx_nets:
            members = json.loads(L.run(["docker", "network", "inspect", "--format", "{{json .Containers}}", self.v.net("tunnel")]).stdout)
            names = sorted(m["Name"] for m in members.values())
            must(names == [self.v.container], f"tunnel network members with the proxy gone: {names}")
            out.append("tunnel network has no member but nginx")
            echo, _ = self.sees(self.edge, 81, "/search/health", [("CF-Connecting-IP", SPOOF_CF)])
            must(real_ip(echo) == self.peers["attacker_edge"][self.v.net("edge")], "forged header honoured with the tunnel off")
            out.append("forged CF-Connecting-IP from edge still ignored")
        return "; ".join(out)

    # the CIDR: one source, read back from the RUNNING system ------------------------------
    def trusted_ranges(self):
        """Every set_real_ip_from nginx actually runs with (`nginx -T` follows the include of the mounted Compose config)."""
        p = L.docker_exec(self.v.container, "nginx", "-T")
        return sorted(set(re.findall(r"^\s*set_real_ip_from\s+(\S+);", p.stdout + p.stderr, re.M)))

    def tunnel_subnet_actual(self):
        p = L.run(["docker", "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", self.v.net("tunnel")])
        return p.stdout.split()

    def cidr_agreement(self, legacy_ok=False):
        want, actual, trusted = self.v.subnet, self.tunnel_subnet_actual(), self.trusted_ranges()
        if legacy_ok:
            trusted = [x for x in trusted if x != LEGACY_RANGE]
        must(actual == [want], f"the Docker network `tunnel` has subnet {actual}, the installation's setting is {want}")
        must(trusted == [want], f"nginx trusts {trusted}, the installation's setting is {want}")
        return f"setting {want} == Docker network subnet {actual[0]} == range nginx runs with {trusted[0]} (read from the running nginx)"

    # equivalence ----------------------------------------------------------------------
    def snapshot(self):
        roles = {}
        for who in ("nginx", "attacker_edge", "attacker_app", "cloudflared"):
            for net, ip in self.peers[who].items():
                roles[ip] = f"<{who}>"
        roles[LEGIT_CF] = "<client>"
        reqs = [("edge", 80, "GET", "/health"), ("edge", 80, "GET", "/api/health"), ("edge", 80, "GET", "/api"), ("edge", 80, "GET", "/search/health"),
                ("edge", 80, "POST", "/search/v1/ask"), ("edge", 80, "GET", "/search/v1/query-log"), ("edge", 80, "GET", "/search"),
                ("edge", 80, "GET", "/chat"), ("edge", 80, "GET", "/chat/health"), ("edge", 80, "GET", "/qa"), ("edge", 80, "GET", "/qa/"),
                ("edge", 80, "GET", "/qa-api/v1/health"), ("edge", 80, "GET", "/nothing-here"),
                ("edge", 81, "GET", "/search/health"), ("edge", 81, "POST", "/search/v1/ask"), ("edge", 81, "GET", "/qa/"), ("edge", 81, "GET", "/api/"),
                ("edge", 81, "GET", "/health"), ("edge", 81, "GET", "/search"), ("edge", 8443, "GET", "/pgadmin/"), ("edge", 8444, "GET", "/"),
                ("cf", 81, "GET", "/search/health"), ("cf", 81, "GET", "/search/v1/ask"), ("cf", 81, "GET", "/qa/")]
        snap = {}
        for who, port, method, path in reqs:
            client = self.cf if who == "cf" else self.edge
            headers = [("CF-Connecting-IP", LEGIT_CF)] if who == "cf" else []
            body = {"len": 20} if method == "POST" else None
            r = one(self.lab, client, NGINX_NAME, port, path, headers, method=method, body=body)
            if "error" in r:
                raise L.TransportError(f"{who} {port} {path}: {r['error']}")
            heads = sorted((k.lower(), v) for k, v in r["headers"] if k.lower() not in ("date", "content-length"))
            text = L.b64_text(r)
            sig = json.dumps({"status": r["status"], "headers": heads, "body": text}, sort_keys=True)
            for ip, tag in sorted(roles.items(), key=lambda kv: -len(kv[0])):
                sig = sig.replace(ip, tag)
            snap[f"{who}:{port} {method} {path}"] = sig
        return snap


def compare_snapshots(a, b):
    must(a.keys() == b.keys(), "snapshot keys differ")
    diff = [k for k in a if a[k] != b[k]]
    must(not diff, f"{len(diff)} of {len(a)} requests differ: {diff[:4]}")
    return f"{len(a)} requests identical (status, all headers but Date, whole body; lab addresses normalised)"


# ── ASGI: what real uvicorn makes of it ─────────────────────────────────
def start_asgi(lab, v, role, image, alias, port, env_star):
    mounts = ["--mount", f"type=bind,source={HERE / 'asgi_echo.py'},target=/lab/asgi_echo.py,readonly", "--workdir", "/lab"]
    extra = ["--entrypoint", "python"] + (["-e", "FORWARDED_ALLOW_IPS=*"] if env_star else [])
    cname = start_peer(lab, v, role, ["edge"], [alias], image=image, extra=extra, mounts=mounts,
                       cmd=("-m", "uvicorn", "asgi_echo:app", "--host", "0.0.0.0", "--port", str(port)))
    deadline = time.time() + 40
    while True:
        p = L.run(["docker", "exec", cname, "python", "-c", f"import socket;socket.create_connection(('127.0.0.1',{port}),2)"], check=False)
        if p.returncode == 0:
            return cname
        if time.time() > deadline:
            logs = L.run(["docker", "logs", cname], check=False)
            raise L.LabError(f"asgi container {role} not listening: {(logs.stdout + logs.stderr)[-300:]}")
        time.sleep(0.5)


def retry_view(fn, timeout=20):
    """nginx re-resolves upstream names every 5 s (resolver valid=5s): right after a container with the alias appears, the first
    answers can be 502/503/504. Retry on exactly those and on transport errors; anything else is judged at once."""
    deadline = time.time() + timeout
    while True:
        try:
            return fn()
        except (L.TransportError, AssertionError):
            if time.time() > deadline:
                raise
            time.sleep(1.0)


def asgi_view(res):
    if "error" in res:
        raise L.TransportError(f"case {res.get('id')}: {res['error']}")
    must(res.get("status") == 200, f"case {res.get('id')}: status {res.get('status')}")
    return json.loads(L.b64_text(res))


def asgi_checks(lab, rep, scene, vname, label, image, which):
    """Real uvicorn, started like the production image starts it. `which`: api (via /api/ on :80) ."""
    v = scene.v
    if L.run(["docker", "image", "inspect", image], check=False).returncode != 0:
        rep.check(f"{vname}.asgi.{label}", f"real uvicorn from image {image}", lambda: (_ for _ in ()).throw(NotRun(f"image {image} not available locally")))
        return
    lab.rm(scene.stubs[1])
    nginx_edge = scene.ip("nginx", "edge")
    attacker = scene.ip("attacker_edge", "edge")
    try:
        for star in (False, True):
            env = "FORWARDED_ALLOW_IPS=*" if star else "default env"
            app = start_asgi(lab, v, f"asgi_{label}_{'star' if star else 'def'}", image, "edu_ai_api", 8080, star)
            api_ip = net_ips(app)[v.net("edge")]
            ver = retry_view(lambda: asgi_view(one(lab, scene.edge, NGINX_NAME, 80, "/api/x", [])))["uvicorn"]
            spoof = [("X-Forwarded-For", FAKE_CHAIN), ("X-Forwarded-Proto", "https"), ("X-Real-IP", "198.51.100.9"), ("CF-Connecting-IP", SPOOF_CF)]

            def via_nginx(expect_forgeable):
                a = retry_view(lambda: asgi_view(one(lab, scene.edge, NGINX_NAME, 80, "/api/x", spoof)))
                host, scheme = a["client"][0], a["scheme"]
                if expect_forgeable:
                    must(host == "198.51.100.8", f"expected the forged leftmost address to become request.client, got {host}")
                    return f"uvicorn {a['uvicorn']} {env}: request.client {host} (FORGED chain accepted), scheme {scheme}"
                must(host in (nginx_edge, attacker) and host != "198.51.100.8", f"request.client {host}")
                must(scheme == "http", f"scheme {scheme}")
                return f"uvicorn {a['uvicorn']} {env}: request.client {host} ({'nginx' if host == nginx_edge else 'caller'}), scheme {scheme}"

            def direct():
                a = asgi_view(one(lab, scene.edge, "edu_ai_api", 8080, "/x", spoof))
                host, scheme = a["client"][0], a["scheme"]
                if star:
                    must(host == "198.51.100.8" and scheme == "https", f"expected direct caller to forge identity under '*', got {host}/{scheme}")
                    return f"uvicorn {a['uvicorn']} {env}: a direct edge caller FORGES client {host} and scheme {scheme} (residual risk of '*', whatever nginx does)"
                must(host == attacker and scheme == "http", f"direct caller: client {host}, scheme {scheme}")
                return f"uvicorn {a['uvicorn']} {env}: direct call, client {host} (real peer), scheme {scheme}: forged headers ignored"

            if star:
                rep.check(f"{vname}.asgi.{label}.star.via_nginx", f"{image} with '*' behind nginx ({vname})",
                          lambda: via_nginx(expect_forgeable=(vname == "baseline")))
            else:
                rep.check(f"{vname}.asgi.{label}.default.via_nginx", f"{image} default env behind nginx ({vname})", lambda: via_nginx(False))
            rep.check(f"{vname}.asgi.{label}.{'star' if star else 'default'}.direct", f"{image} {env}, direct edge caller ({vname})", direct)
            rep.observe(f"{vname}.asgi.{label}.version", "uvicorn version in the image", ver)
            lab.rm(app)
    finally:
        lab.rm(*[c for c in lab.containers if c.startswith(f"{lab.prefix}_{vname}_asgi_")])


# ── drivers ────────────────────────────────────────────────────────────
def prerequisites(rep):
    def docker_ok():
        p = L.run(["docker", "version", "--format", "{{.Server.Version}}"], check=False)
        if p.returncode != 0:
            raise L.LabError("the Docker daemon is not reachable: " + (p.stderr or p.stdout).strip()[:200])
        comp = L.run(["docker", "compose", "version", "--short"]).stdout.strip()
        return f"Docker {p.stdout.strip()}, Compose {comp}"
    ok = rep.check("env.docker", "Docker daemon and Compose available", docker_ok)

    def nginx_id():
        got = L.run(["docker", "image", "inspect", L.NGINX_IMAGE, "--format", "{{.Id}}"]).stdout.strip()
        must(got == PROD_NGINX_IMAGE_ID, f"{L.NGINX_IMAGE} is {got}, production runs {PROD_NGINX_IMAGE_ID} (see tests/nginx_s2/README.md for how to pull it)")
        ver = L.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "nginx", L.NGINX_IMAGE, "-v"]).stderr.strip()
        return f"{got[:19]}… {ver}"
    ok = rep.check("env.nginx_image", "nginx image is exactly the production image", nginx_id) and ok

    def py_ok():
        return L.run(["docker", "image", "inspect", L.PY_IMAGE, "--format", "{{.Id}}"]).stdout.strip()[:19] + "…"
    return rep.check("env.python_image", "python image for stubs and clients present", py_ok) and ok


def static_checks(lab, rep):
    base, cand = lab.variant("baseline"), lab.variant("candidate")

    def nginx_t(v, trust_file=None):
        def f():
            mounts = ["--mount", f"type=bind,source={v.dir / 'nginx' / 'conf'},target=/etc/nginx,readonly"]
            if trust_file:
                mounts += ["--mount", f"type=bind,source={trust_file},target={C.CONFIG_TARGET},readonly"]
            p = L.run(["docker", "run", "--rm", "--network", "none", *lab.labels(), *mounts, "--entrypoint", "nginx", L.NGINX_IMAGE, "-t"], check=False)
            out = p.stdout + p.stderr
            must(p.returncode == 0 and "test is successful" in out, f"nginx -t: {out.strip()[-300:]}")
            return "syntax ok, test successful, no network, no upstream present" + (", trust file mounted" if trust_file else "")
        return f
    rep.check("static.nginx_t.baseline", "nginx -t on the committed files", nginx_t(base))
    rep.check("static.nginx_t.candidate_no_trust_file", "nginx -t on the proposal when NO trust file is mounted (nothing is trusted, nginx still starts)", nginx_t(cand))
    trust = lab.tmp / "trust.conf"
    trust.write_text(cand.config(override=False)["configs"]["tunnel_trust"]["content"], encoding="utf-8", newline="\n")
    rep.check("static.nginx_t.candidate_with_trust_file", "nginx -t on the proposal with the Compose-provided trust file mounted", nginx_t(cand, trust))

    def conf_delta():
        def norm(s):
            return " ".join(s.split())
        removed, added = [], []
        for f in ("nginx/conf/nginx.conf", "nginx/conf/search_api.conf"):
            r, a = L.config_delta((base.dir / f).read_text(encoding="utf-8"), (cand.dir / f).read_text(encoding="utf-8"))
            removed += [norm(s) for _, s in r]
            added += [norm(s) for _, s in a]
        want_r = sorted(["set_real_ip_from 172.16.0.0/12"] + ["proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for"] * 7)
        want_a = sorted([C.INCLUDE_LINE.rstrip(";")] + ["proxy_set_header X-Forwarded-For $remote_addr"] * 7)
        must(sorted(removed) == want_r, f"removed directives differ from the design: {sorted(removed)}")
        must(sorted(added) == want_a, f"added directives differ from the design: {sorted(added)}")
        return "the trust line is replaced by one include (no range written in nginx.conf) and seven X-Forwarded-For lines change; every other directive and location is identical"
    rep.check("static.conf_delta", "the proposal changes exactly the designed nginx directives", conf_delta)

    def compose_delta():
        def scrub(o, v):
            s = json.dumps(o, sort_keys=True).replace(str(v.dir).replace("\\", "\\\\"), "<DIR>").replace(str(v.dir).replace("\\", "/"), "<DIR>")
            return json.loads(s.replace(v.project, "<PROJECT>"))
        a, b = scrub(base.config(override=False), base), scrub(cand.config(override=False), cand)
        a.pop("name", None), b.pop("name", None)
        changed = [svc for svc in sorted(set(a["services"]) | set(b["services"])) if a["services"].get(svc) != b["services"].get(svc)]
        must(changed == ["cloudflared", "nginx"], f"services that differ: {changed}")
        sa, sb = dict(a["services"]["nginx"]), dict(b["services"]["nginx"])
        for key in ("networks", "configs"):
            sa.pop(key, None), sb.pop(key, None)
        must(sa == sb, "service nginx differs in more than its networks and its trust config")
        sa, sb = dict(a["services"]["cloudflared"]), dict(b["services"]["cloudflared"])
        sa.pop("networks"), sb.pop("networks")
        must(sa == sb, "service cloudflared differs in more than its networks")
        must(set(b["services"]["nginx"]["networks"]) == {"edge", "tunnel"}, f"nginx networks {list(b['services']['nginx']['networks'])}")
        must(set(b["services"]["cloudflared"]["networks"]) == {"app", "tunnel", "tunnel_egress"}, f"cloudflared networks {list(b['services']['cloudflared']['networks'])}")
        new_nets = sorted(set(b["networks"]) - set(a["networks"]))
        must(new_nets == ["tunnel", "tunnel_egress"], f"new networks {new_nets}")
        must(sorted(b.get("configs") or {}) == ["tunnel_trust"] and not a.get("configs"), f"configs {sorted(b.get('configs') or {})}")
        must({k: v for k, v in a["networks"].items()} == {k: v for k, v in b["networks"].items() if k in a["networks"]}, "an existing network changed")
        return "effective model differs only in the networks of nginx and cloudflared, nginx's trust config, two new networks and one config"
    rep.check("static.compose_delta", "the proposal changes only the networks of nginx and cloudflared (+2 networks, +1 config)", compose_delta)

    def subnet_required():
        p = cand.compose("config", "--format", "json", override=False, env=False, check=False)
        must(p.returncode != 0 and C.SUBNET_VAR in (p.stderr + p.stdout), f"the model rendered without {C.SUBNET_VAR} (rc={p.returncode}): a default value hides a constant")
        return f"without {C.SUBNET_VAR} Compose refuses to render the model: " + p.stderr.strip().splitlines()[-1][:160]
    rep.check("static.subnet_required", "no default: the model cannot be rendered without the installation's own subnet", subnet_required)

    confs_of = lambda v: {"nginx.conf": (v.dir / "nginx/conf/nginx.conf").read_text(encoding="utf-8"),
                          "search_api.conf": (v.dir / "nginx/conf/search_api.conf").read_text(encoding="utf-8")}   # noqa: E731

    def single_source_ok():
        problems = TS.single_source_problems(cand.config(override=False), confs_of(cand), cand.subnet)
        must(problems == [], "; ".join(problems))
        return f"ONE source: {C.SUBNET_VAR}={cand.subnet} gives the subnet of `tunnel` and nginx's trust range; nginx.conf carries no copy; tunnel members cloudflared + nginx"
    rep.check("static.single_source.candidate", "the CIDR has exactly one source (Compose derives network and trust range from one variable)", single_source_ok)

    for mut, what in (("mut_literal", "nginx.conf keeps its own copy of the range"), ("mut_cidr", "the Compose config carries a different literal than the network's variable")):
        def detected(mut=mut, what=what):
            mv = lab.variant(mut)
            problems = TS.single_source_problems(mv.config(override=False), confs_of(mv), mv.subnet)
            must(problems, f"MUTANT NOT DETECTED: {what}, yet the single-source check found no problem")
            return "detected: " + problems[0]
        rep.check(f"static.single_source.detects.{mut}", f"a second constant is detected ({what})", detected)

    def legacy_only_in_rollout():
        t2 = lab.variant("transition2")
        m, c = t2.config(override=False), confs_of(t2)
        must(TS.single_source_problems(m, c, t2.subnet, allow_legacy=True) == [], "rollout step 2 has problems although the legacy line is allowed")
        must(TS.single_source_problems(m, c, t2.subnet) != [], "the legacy line is tolerated outside the rollout")
        return "the old range is tolerated in rollout steps 1-2 and refused in the final state"
    rep.check("static.single_source.rollout", "the old literal range is tolerated only while rolling out", legacy_only_in_rollout)


def detects(fn):
    """True when the check FAILS (the mutant was noticed). An ERROR is not detection and propagates."""
    try:
        fn()
    except AssertionError:
        return True
    return False


def run_scene(lab, rep, vname, asgi_images):
    print(f"\n== variant {vname}", flush=True)
    v = lab.variant(vname)
    sc = Scene(lab, v)
    rep.observe(f"{vname}.peers", "addresses (nginx / attacker_edge / attacker_app / cloudflared stand-in)",
                {k: {n.replace(v.project + '_', ''): ip for n, ip in d.items()} for k, d in sc.peers.items()})
    rep.observe(f"{vname}.networks", "networks of nginx / of the proxy stand-in (from the compose file)", {"nginx": v.nginx_nets, "cloudflared": v.cf_nets})
    if v.subnet:
        rep.observe(f"{vname}.subnet", "the installation's own EDU_AI_TUNNEL_SUBNET (allocated and preflighted)", v.subnet)
    P = f"{vname}."
    snap = None
    if vname == "baseline":
        rep.check(P + "positive", "the real proxy keeps the client address", lambda: sc.proxy_keeps_client())
        rep.check(P + "R2.spoof81", "R2 reproduced: an `edge` member forges the client on :81", lambda: sc.spoof_81_from_edge(True))
        rep.check(P + "other_listeners", "forged headers ignored on :80, :8443, :8444", sc.spoof_other_listeners)
        rep.check(P + "xff.kept", "caller-supplied X-Forwarded-For chain reaches the application (reproduced)", lambda: sc.xff_chain(True))
        rep.check(P + "proto", "X-Forwarded-Proto and X-Real-IP are nginx's own on :80, :81, :8443", sc.proto_and_real_ip_overwritten)
        rep.check(P + "ratelimit.bypass", "impact: forged rotating addresses bypass the per-IP limit (reproduced)", lambda: sc.rate_limit_rotating(True))
        rep.check(P + "ratelimit.victim", "impact: a forger exhausts another client's bucket (reproduced)", lambda: sc.victim_lockout(True))
        rep.check(P + "app_net", "a client on `app` only cannot reach nginx", sc.app_network_cannot_reach_nginx)
        rep.check(P + "dns.proxy", f"{NGINX_NAME} from the proxy resolves to nginx's `edge` address", lambda: sc.dns_from_proxy(sc.ip("nginx", "edge")))
        rep.check(P + "replace", "proxy replaced by a container with another address still works", sc.proxy_replaced_by_other_ip)
        rep.check(P + "passthrough", "observation: which caller headers nginx forwards untouched", lambda: json.dumps(sc.passthrough_observation()))
        snap = sc.snapshot()
        for label, image in asgi_images:
            asgi_checks(lab, rep, sc, vname, label, image, "api")
    elif vname == "candidate":
        rep.check(P + "cidr_agreement", "the setting, the Docker network and the range nginx runs with are one value", sc.cidr_agreement)
        rep.check(P + "positive", "the real proxy (tunnel network only) keeps the client address", lambda: sc.proxy_keeps_client())
        rep.check(P + "dns.proxy", f"{NGINX_NAME} from the proxy resolves ONLY to nginx's `tunnel` address (no dashboard change)", lambda: sc.dns_from_proxy(sc.ip("nginx", "tunnel")))
        rep.check(P + "R2.spoof81", "an `edge` member cannot forge the client on :81", lambda: sc.spoof_81_from_edge(False))
        rep.check(P + "other_listeners", "forged headers ignored on :80, :8443, :8444", sc.spoof_other_listeners)
        rep.check(P + "xff.dropped", "caller-supplied X-Forwarded-For chain is dropped on :80", lambda: sc.xff_chain(False))
        rep.check(P + "xff.proxy", "through the proxy X-Forwarded-For is the single client address", lambda: sc.xff_via_proxy(True))
        rep.check(P + "proto", "X-Forwarded-Proto and X-Real-IP are nginx's own on :80, :81, :8443", sc.proto_and_real_ip_overwritten)
        rep.check(P + "ratelimit.bypass", "forged rotating addresses no longer bypass the per-IP limit", lambda: sc.rate_limit_rotating(False))
        rep.check(P + "ratelimit.victim", "a forger cannot exhaust another client's bucket", lambda: sc.victim_lockout(False))
        rep.check(P + "app_net", "a client on `app` only cannot reach nginx", sc.app_network_cannot_reach_nginx)
        rep.check(P + "tunnel_isolated", "an `edge` member cannot reach nginx's tunnel address", sc.tunnel_unreachable_from_edge)
        rep.check(P + "wrong_network", "a proxy-like container on `edge` is NOT trusted (fails safe)", sc.proxy_on_wrong_network_not_trusted)
        rep.check(P + "dual_network", "observation: a proxy left on `edge` AND `tunnel` (why cloudflared leaves `edge`)", sc.dual_network_observation)
        rep.check(P + "replace", "proxy replaced by a container with another address still works", sc.proxy_replaced_by_other_ip)
        rep.check(P + "passthrough", "observation: which caller headers nginx forwards untouched", lambda: json.dumps(sc.passthrough_observation()))
        snap = sc.snapshot()
        # the tunnel goes off BEFORE the ASGI phase: that phase removes the synthetic search/api stubs, and the
        # requests of tunnel_off need them
        rep.check(P + "tunnel_off", "tunnel off: nginx unaffected, nobody else on the tunnel network", sc.tunnel_off)
        for label, image in asgi_images:
            asgi_checks(lab, rep, sc, vname, label, image, "api")
    elif vname in ("transition", "transition2"):
        rep.check(P + "positive", f"the proxy on `{sc.trust_net}` keeps the client address during the rollout", lambda: sc.proxy_keeps_client())
        rep.check(P + "R2.residual", "residual (documented): an `edge` member can still forge on :81 while the old range is kept",
                  lambda: sc.spoof_81_from_edge(True))
        if vname == "transition2":
            rep.check(P + "cidr_agreement", "step 2: the new range equals the setting and the Docker network (the old range is the only extra)",
                      lambda: sc.cidr_agreement(legacy_ok=True))
    elif vname.startswith("mut_"):
        def not_detected(what):
            raise AssertionError(f"MUTANT NOT DETECTED: {what} passed on a deliberately wrong proposal")

        def need(*pairs):
            def f():
                for what, fn in pairs:
                    if not detects(fn):
                        not_detected(what)
                return "detected by " + ", ".join(w for w, _ in pairs)
            return f
        mut = {"mut_broad": ("sensitivity: the spoof check notices a re-broadened trust range", need(("the spoof check", lambda: sc.spoof_81_from_edge(False)))),
               "mut_notrust": ("sensitivity: the positive check notices that nothing is trusted", need(("the positive check", lambda: sc.proxy_keeps_client()))),
               "mut_xff": ("sensitivity: the X-Forwarded-For check notices the append form", need(("the XFF check", lambda: sc.xff_chain(False)))),
               "mut_literal": ("sensitivity: the CIDR checks notice a second, independent constant in nginx.conf",
                               need(("the CIDR agreement check", lambda: sc.cidr_agreement()), ("the positive check", lambda: sc.proxy_keeps_client()))),
               "mut_cidr": ("sensitivity: the CIDR checks notice a trust config that differs from the network's subnet",
                            need(("the CIDR agreement check", lambda: sc.cidr_agreement()), ("the positive check", lambda: sc.proxy_keeps_client())))}[vname]
        rep.check(P + "detected", mut[0], mut[1])
    return snap


# ── subnet preflight against the real daemon ──────────────────────────────────
def make_network(lab, name, subnet, project=None, extra_labels=None, labelled=True):
    cmd = ["docker", "network", "create", "--internal", "--subnet", subnet]
    if labelled:
        cmd += lab.labels(project)
    for k, v in (extra_labels or {}).items():
        cmd += ["--label", f"{k}={v}"]
    L.run(cmd + [name])
    return L.run(["docker", "network", "inspect", "--format", "{{.Id}}", name]).stdout.strip()


def resource_count(lab):
    """Containers and networks that exist right now on the daemon (a refusal must not change this number)."""
    c = len(L.run(["docker", "ps", "-aq"]).stdout.split())
    n = len(L.run(["docker", "network", "ls", "-q"]).stdout.split())
    return c, n


SHIM_PATH = "/shim:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def read_scene(lab, rep):
    """R2-R3 against the real helper image and kernel: every routing table is read, and a failed read is never completed by the other.
    Nothing here touches the daemon's own routes: a policy-table route lives in the network namespace of a throw-away container."""
    tool, ns = [sys.executable, str(HERE / "tunnel_subnet.py")], ["--routes", "docker", "--helper-image", L.NGINX_IMAGE]
    helper_cmd = lambda network="host", extra=(): ["docker", "run", "--rm", "--network", network, *lab.labels(), *extra,   # noqa: E731
                                                  "--entrypoint", "sh", L.NGINX_IMAGE, "-c"]
    norm = lambda t: {" ".join(l.split()) for l in t.splitlines() if l.strip()}          # noqa: E731

    def all_tables():
        plain, every = norm(L.run(helper_cmd() + ["ip -4 route show"]).stdout), norm(L.run(helper_cmd() + [" ".join(TS.ROUTE_CMD)]).stdout)
        extra = every - plain
        must(plain <= every and any("table local" in l for l in extra), f"`table all` does not show more than main: only-in-all={sorted(extra)[:3]}")
        st_raw = [raw for _, raw in lab.subnet_state().routes]
        must(any("table local" in r for r in st_raw), "the state read through the helper has no local-table route")
        return (f"helper image {L.NGINX_IMAGE} (BusyBox) accepts `{' '.join(TS.ROUTE_CMD)}`: {len(plain)} main routes, {len(every)} routes in all tables, "
                f"{len(extra)} more than a main-only read; the gathered state carries them")
    rep.check("subnet.read.all_tables_live", "the helper image honours `table all`: the daemon's inventory has routes a main-only read cannot see", all_tables)

    holder, free = lab.name("policy_holder"), lab.allocate_subnet(lab.project_for("policy_demo"))
    state = {}

    def setup():
        lab.safe_rm(holder)
        L.run(["docker", "run", "-d", "--name", holder, "--network", "none", "--cap-add", "NET_ADMIN", *lab.labels(), "--entrypoint", "sh", L.NGINX_IMAGE, "-c",
               f"ip route add {free} dev lo table 100 && touch /tmp/ready; sleep 3600"])
        lab.containers.append(holder)
        deadline = time.time() + 20
        while L.run(["docker", "exec", holder, "test", "-f", "/tmp/ready"], check=False).returncode != 0:
            if time.time() > deadline:
                raise L.LabError("the policy route could not be created: " + L.run(["docker", "logs", holder], check=False).stderr.strip()[:200])
            time.sleep(0.4)
        in_ns = helper_cmd(f"container:{holder}")
        state["main"], state["all"] = L.run(in_ns + ["ip -4 route show"]).stdout, L.run(in_ns + [" ".join(TS.ROUTE_CMD)]).stdout
        must(str(free) not in state["main"] and f"{free} dev lo table 100" in " ".join(state["all"].split()), f"the namespace does not look as built: {state}")
        return f"throw-away namespace of {holder} (own netns, no host change): {free} dev lo table 100; a main-only read shows nothing of it, `table all` shows it"
    try:
        if rep.check("subnet.read.policy_table.setup", "a route that exists only in a non-main table of an isolated namespace", setup):
            inside = ["--helper-network", f"container:{holder}"]

            def refused():
                outside = L.run(tool + ["check", str(free)] + ns, check=False, cwd=str(HERE))
                got = L.run(tool + ["check", str(free)] + ns + inside, check=False, cwd=str(HERE))
                must(outside.returncode == 0, f"the control (the daemon's own namespace, where {free} is free) was refused: {outside.stdout.strip()[:200]}")
                must(got.returncode == 2 and "HOST_ROUTE" in got.stdout and "table 100" in got.stdout, f"rc={got.returncode} {got.stdout.strip()[:300]}")
                return f"{free}: exit 0 in the daemon's namespace (control), exit 2 in the namespace with the table-100 route: {got.stdout.strip().splitlines()[0][:160]}"
            rep.check("subnet.read.policy_table.check", "a collision that exists only in a policy table is found by the command line (exit 2)", refused)

            def avoided():
                control = L.run(tool + ["allocate", "--project", lab.project_for("policy_demo")] + ns, check=False, cwd=str(HERE))
                got = L.run(tool + ["allocate", "--project", lab.project_for("policy_demo")] + ns + inside, check=False, cwd=str(HERE))
                must(control.returncode == 0 and control.stdout.strip() == f"EDU_AI_TUNNEL_SUBNET={free}", f"the control did not allocate {free}: {control.stdout.strip()[:200]}")
                chosen = got.stdout.strip().split("=", 1)[1] if got.returncode == 0 and "=" in got.stdout else None
                must(chosen and not TS.ipaddress.ip_network(chosen).overlaps(free), f"allocate chose {chosen} (rc={got.returncode}) although {free} is routed in table 100")
                return f"the daemon's namespace allocates {free}; with the table-100 route in view allocate chose {chosen}"
            rep.check("subnet.read.policy_table.allocate", "allocate does not hand out a block that is routed in a policy table", avoided)
    finally:
        try:
            lab.rm(holder)
        except OW.OwnershipError as e:
            rep.check("subnet.read.policy_table.removed", "the throw-away namespace holder was removed", lambda e=e: (_ for _ in ()).throw(e))

    shims = {}
    for name, body in (("route", 'case "$*" in *route*) echo "simulated route read failure" >&2; exit 1;; esac'),
                       ("addr", 'case "$*" in *addr*) echo "simulated address read failure" >&2; exit 1;; esac')):
        d = lab.tmp / f"shim_{name}"
        d.mkdir(exist_ok=True)
        (d / "ip").write_bytes(f"#!/bin/sh\n{body}\nexec /bin/busybox ip \"$@\"\n".encode())
        shims[name] = ["--mount", f"type=bind,source={d},target=/shim,readonly", "-e", f"PATH={SHIM_PATH}"]

    def failing(which, status, words):
        def f():
            try:
                TS.gather(lab_runner, routes="docker", helper_image=L.NGINX_IMAGE, helper_labels=lab.labels(), helper_args=shims[which])
            except TS.Unverifiable as e:
                must(f"rc={status}" in str(e) and words in str(e) and "simulated" in str(e), f"refused, but not as designed: {e}")
                msg = str(e)
            else:
                raise AssertionError("the host state was accepted although a mandatory read failed")
            old = L.run(helper_cmd("host", shims[which]) + [OFF.OLD_HELPER_SCRIPT], check=False) if which == "route" else None
            if old is not None:
                must(old.returncode == 0 and TS.SEPARATOR in old.stdout and "inet " in old.stdout, f"the first version's script no longer hides the failure: rc={old.returncode}")
            return msg[:200] + ("; the first version's script under the same fault: exit 0, address list printed, route list empty (the finding, reproduced)" if old is not None else "")
        return f
    rep.check("subnet.read.route_failure_live", "the real helper script: a failed route read behind a good address read is refused (rc 11)", failing("route", 11, "the route read"))
    rep.check("subnet.read.address_failure_live", "the real helper script: a failed address read behind a good route read is refused (rc 12)", failing("addr", 12, "the address read"))

    def cli_unreadable():
        got = L.run(tool + ["check", str(free), "--routes", "docker", "--helper-image", L.PY_IMAGE], check=False, cwd=str(HERE))     # an image without `ip`
        alloc = L.run(tool + ["allocate", "--project", lab.project_for("policy_demo"), "--routes", "docker", "--helper-image", L.PY_IMAGE], check=False, cwd=str(HERE))
        must(got.returncode == 3 and "the route read" in got.stdout and alloc.returncode == 3 and "EDU_AI_TUNNEL_SUBNET=" not in alloc.stdout,
             f"check rc={got.returncode} {got.stdout.strip()[:200]} / allocate rc={alloc.returncode} {alloc.stdout.strip()[:200]}")
        return f"helper image without `ip`: check exit {got.returncode}, allocate exit {alloc.returncode}, no subnet printed: {got.stdout.strip()[:120]}"
    rep.check("subnet.read.cli_unreadable_live", "the command line refuses (exit 3) when the route read cannot run in the helper", cli_unreadable)


def subnet_scene(lab, rep):
    print("\n== subnet allocation and preflight against the real daemon", flush=True)
    created = []

    def refused(cidr, project, *, kinds, what=None):
        before = resource_count(lab)
        try:
            lab.preflight_subnet(project, cidr)
        except Refused as e:
            msg = str(e)
            must(all(k in msg for k in kinds), f"refused, but not for {kinds}: {msg[:300]}")
            must(resource_count(lab) == before, "a refusal changed the number of containers or networks")
            return f"{cidr}: refused ({', '.join(kinds)}), nothing created"
        raise AssertionError(f"{cidr} was NOT refused")

    def state_read():
        st = lab.subnet_state()
        must(st.networks and (st.routes or st.addrs), "no network or route could be read")
        return (f"read through the daemon's network namespace: {len(st.networks)} Docker networks, {len(st.pools)} address pools, "
                f"{len(st.routes)} routes of all tables, {len(st.addrs)} interface addresses")
    rep.check("subnet.state", "the host state (Docker networks, pools, routes of all tables, addresses) can be read", state_read)
    read_scene(lab, rep)

    proj = lab.project_for("subnet_demo")

    def allocate():
        n = lab.allocate_subnet(proj)
        must(n.prefixlen == 29 and n.subnet_of(TS.ipaddress.ip_network("10.0.0.0/8")), f"allocated {n}")
        lab.preflight_subnet(proj, str(n))
        must(not any(n.overlaps(TS.ipaddress.ip_network(b)) for b, _ in TS.DENY), f"{n} is in a well-known range")
        return f"{n} for project {proj}: a canonical /29 inside 10/8, outside the well-known list, free on this daemon"
    rep.check("subnet.allocate", "a free, canonical private /29 is allocated for an installation", allocate)

    try:
        x = lab.allocate_subnet(lab.project_for("subnet_victim"))
        d1 = lab.name("decoy_tunnel_x")
        created.append(make_network(lab, d1, str(x), lab.project_for("subnet_other")))
        rep.check("subnet.refuse.docker_network", "a subnet that overlaps ANOTHER installation's/Docker network is refused before anything is created",
                  lambda: refused(str(x), lab.project_for("subnet_newcomer"), kinds=["DOCKER_NETWORK"]))

        own_proj = lab.project_for("own_demo")
        own = lab.allocate_subnet(own_proj)
        created.append(make_network(lab, f"{own_proj}_tunnel", str(own), own_proj))

        def own_ok():
            lab.preflight_subnet(own_proj, str(own))
            return f"the installation's own network {own_proj}_tunnel with the same subnet {own} is not a collision (re-checking an existing setup)"
        rep.check("subnet.own_network_ok", "re-checking an installation's own existing network with the same subnet is accepted", own_ok)

        def mismatch():
            other = TS.allocate(own_proj + "_other", lab.subnet_state(), reserved=lab.reserved)
            before = L.run(["docker", "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}", f"{own_proj}_tunnel"]).stdout.strip()
            msg = refused(str(other), own_proj, kinds=["NETWORK_MISMATCH"])
            after = L.run(["docker", "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}", f"{own_proj}_tunnel"]).stdout.strip()
            must(before == after == str(own), f"the existing network was touched: {before} -> {after}")
            return msg + f"; the existing network keeps {after}"
        rep.check("subnet.refuse.network_mismatch", "a setting that differs from the installation's existing network is refused and the network is not touched", mismatch)

        def own_all_tables():
            nid = L.run(["docker", "network", "inspect", "--format", "{{.Id}}", f"{own_proj}_tunnel"]).stdout.strip()
            bridge = f"br-{nid[:12]}"
            mine = [raw for _, raw in lab.subnet_state().routes if f"dev {bridge}" in raw]
            must(any("table local" in r for r in mine) and any("table" not in r for r in mine), f"the inventory lacks the bridge's connected or local-table routes: {mine}")
            for _ in range(3):                                                  # the preflight runs on every create/update
                lab.preflight_subnet(own_proj, str(own))
            tool, ns = [sys.executable, str(HERE / "tunnel_subnet.py")], ["--routes", "docker", "--helper-image", L.NGINX_IMAGE]
            checked = [L.run(tool + ["check", str(own), "--project", own_proj] + ns, check=False, cwd=str(HERE)) for _ in range(2)]
            alloc = L.run(tool + ["allocate", "--project", own_proj] + ns, check=False, cwd=str(HERE))
            must(all(c.returncode == 0 for c in checked), f"the CLI refused the running installation: {[c.stdout.strip()[:200] for c in checked]}")
            must(alloc.returncode == 0 and alloc.stdout.strip() == f"EDU_AI_TUNNEL_SUBNET={own}", f"allocate did not keep the existing subnet: rc={alloc.returncode} {alloc.stdout.strip()}")
            try:
                lab.preflight_subnet(lab.project_for("own_other"), str(own))
            except Refused as e:
                must("HOST_ROUTE" in str(e) and "DOCKER_NETWORK" in str(e), f"another installation was refused for the wrong reasons: {str(e)[:300]}")
                other = sorted({k for k in ("DOCKER_NETWORK", "HOST_ROUTE", "HOST_ADDRESS") if k in str(e)})
            else:
                raise AssertionError("another installation was allowed to take the running installation's subnet")
            return (f"{own_proj}_tunnel ({own}): {len(mine)} routes on {bridge} in the inventory ({sum('table local' in r for r in mine)} in the local table); "
                    f"3 library preflights and 2 CLI checks accepted, allocate keeps {own}; another project refused ({', '.join(other)})")
        rep.check("subnet.read.own_installation_live", "a running installation passes repeated preflights with its local-table and bridge routes in the inventory", own_all_tables)
    except Exception as e:  # noqa: BLE001
        rep.check("subnet.decoys", "decoy networks for the collision tests could be created", lambda e=e: (_ for _ in ()).throw(e))

    for cidr, kinds in (("172.18.0.0/29", ["DENYLIST", "DOCKER_POOL"]), ("192.168.65.0/29", ["DENYLIST"]), ("10.66.0.0/29", ["DENYLIST"]),
                        ("10.130.0.0/29", ["DENYLIST"]), ("169.254.1.0/29", ["NOT_PRIVATE"])):
        def well_known(cidr=cidr, kinds=kinds):
            try:
                return refused(cidr, lab.project_for("subnet_wk"), kinds=kinds)
            except ValueError as e:
                must(all(k in str(e) for k in kinds), str(e))
                return f"{cidr}: refused ({', '.join(kinds)}), nothing created"
        rep.check(f"subnet.refuse.well_known.{cidr.split('/')[0]}", f"{cidr} (Docker pool / VPN / cloud / link-local range) is refused", well_known)

    def invalid():
        out = []
        for bad in ("10.77.3.5/29", "10.77.3.0/24", "8.8.8.0/29", "not-a-cidr", ""):
            try:
                lab.preflight_subnet(proj, bad)
            except ValueError as e:
                out.append(str(e).split(":")[0])
                continue
            raise AssertionError(f"{bad!r} was accepted")
        return "refused as INVALID/NOT_PRIVATE: host bits, /24, public address, junk, empty"
    rep.check("subnet.refuse.invalid", "invalid or non-private values are refused", invalid)

    def real_route():
        st = lab.subnet_state()
        for dest, raw in st.routes:
            if dest.prefixlen <= 28 and dest.network_address.is_private and not dest.network_address.is_loopback and not dest.network_address.is_link_local:
                cand = TS.ipaddress.ip_network((int(dest.network_address), 29))
                found = TS.conflicts(cand, st)
                must("HOST_ROUTE" in kinds_of(found), f"{cand} lies inside the real route {raw!r} but HOST_ROUTE was not reported: {found}")
                return f"{cand} inside the route read from the daemon ({raw[:70]}): " + ", ".join(sorted(kinds_of(found)))
        raise NotRun("the daemon's network namespace has no private route wide enough to build a route-overlap case from")
    rep.check("subnet.refuse.real_route", "a subnet inside a real host route read from the daemon is refused as HOST_ROUTE", real_route)

    def cli_live():
        py = sys.executable
        tool = str(HERE / "tunnel_subnet.py")
        base = [py, tool, "check", "--routes", "docker", "--helper-image", L.NGINX_IMAGE]
        n = lab.allocate_subnet(lab.project_for("cli_demo"))
        free = L.run(base + [str(n)], check=False, cwd=str(HERE))
        taken = L.run(base + ["172.18.0.0/29"], check=False, cwd=str(HERE))
        broken = L.run([py, tool, "check", "--routes", "docker", "--helper-image", "no-such-image:none", str(n)], check=False, cwd=str(HERE))
        must(free.returncode == 0, f"a free subnet: rc={free.returncode} {free.stdout.strip()[:200]}")
        must(taken.returncode == 2, f"a well-known range: rc={taken.returncode} {taken.stdout.strip()[:200]}")
        must(broken.returncode == 3, f"an uninspectable host: rc={broken.returncode} {broken.stdout.strip()[:200]}")
        return f"exit {free.returncode} for {n}, exit {taken.returncode} for 172.18.0.0/29 (refused), exit {broken.returncode} when the host cannot be read (refused, unverifiable)"
    rep.check("subnet.cli.live", "the command line allocates/checks against the real daemon and refuses with 2 / 3", cli_live)

    for ident in created:
        try:
            OW.safe_remove(lab_runner, "network", ident, lab.run_id)
        except OW.OwnershipError as e:
            rep.check("subnet.decoys_removed", "the decoy networks of this scene were removed", lambda e=e: (_ for _ in ()).throw(e))


def kinds_of(found):
    return {c.kind for c in found}


# ── two concurrent installations ────────────────────────────────────────────────
def multi_scene(lab, rep):
    print("\n== two concurrent installations on one daemon", flush=True)
    state = {}

    def start():
        state["a"] = Scene(lab, lab.variant("candidate", name="inst_a"))
        state["b"] = Scene(lab, lab.variant("candidate", name="inst_b"))        # allocated while A's network exists
        a, b = state["a"].v, state["b"].v
        return f"A: project {a.project} subnet {a.subnet}; B: project {b.project} subnet {b.subnet}"
    if not rep.check("multi.start", "two installations with different project names start side by side, each with its own allocated subnet", start):
        for cid in ("projects_and_subnets", "trust_each_own", "positive_each", "forged_each", "cross_unreachable", "same_cidr_refused", "teardown_one_keeps_other"):
            rep.check(f"multi.{cid}", "needs both installations", lambda: (_ for _ in ()).throw(NotRun("the installations did not start")))
        return
    sa, sb = state["a"], state["b"]
    va, vb = sa.v, sb.v

    def distinct():
        na, nb = TS.ipaddress.ip_network(va.subnet), TS.ipaddress.ip_network(vb.subnet)
        must(va.project != vb.project and va.subnet != vb.subnet and not na.overlaps(nb), f"{va.subnet} vs {vb.subnet}")
        for v in (va, vb):
            must(sorted(sc_ for sc_ in (L.run(["docker", "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}", v.net("tunnel")]).stdout.split())) == [v.subnet],
                 f"{v.net('tunnel')} has the wrong subnet")
        must(va.net("tunnel") != vb.net("tunnel"), "the installations share a tunnel network")
        return f"{va.subnet} and {vb.subnet} do not overlap; networks {va.net('tunnel')} and {vb.net('tunnel')} are separate"
    rep.check("multi.projects_and_subnets", "different projects, non-overlapping subnets, separate tunnel networks", distinct)

    def each_own():
        ra, rb = sa.trusted_ranges(), sb.trusted_ranges()
        must(ra == [va.subnet] and rb == [vb.subnet], f"A trusts {ra}, B trusts {rb}")
        must(vb.subnet not in ra and va.subnet not in rb, "an installation trusts the other one's range")
        sa.cidr_agreement(), sb.cidr_agreement()
        return f"A trusts only {ra[0]}, B trusts only {rb[0]}"
    rep.check("multi.trust_each_own", "each nginx trusts ONLY its own installation's subnet (read from the running nginx)", each_own)
    rep.check("multi.positive_each", "each installation's proxy keeps the client address",
              lambda: "A: " + sa.proxy_keeps_client() + " | B: " + sb.proxy_keeps_client())
    rep.check("multi.forged_each", "in each installation an `edge` member cannot forge the client",
              lambda: "A: " + sa.spoof_81_from_edge(False) + " | B: " + sb.spoof_81_from_edge(False))

    def cross():
        out = []
        for name, src, dst_ip in (("A proxy -> B nginx (tunnel)", sa.cf, sb.ip("nginx", "tunnel")), ("B proxy -> A nginx (tunnel)", sb.cf, sa.ip("nginx", "tunnel")),
                                  ("A edge member -> B nginx (edge)", sa.edge, sb.ip("nginx", "edge")), ("B edge member -> A nginx (edge)", sb.edge, sa.ip("nginx", "edge"))):
            kind = L.probe_tcp(src, dst_ip, 81)
            must(kind in ("TIMEOUT", "UNREACHABLE"), f"{name}: {dst_ip}:81 is {kind}")
            out.append(f"{name}: {kind}")
        must(resolve(sa.cf, NGINX_NAME) == [sa.ip("nginx", "tunnel")] and resolve(sb.cf, NGINX_NAME) == [sb.ip("nginx", "tunnel")],
             "a proxy resolves edu_ai_nginx to something other than its own installation's nginx")
        return "; ".join(out) + "; each proxy resolves edu_ai_nginx only to its own nginx"
    rep.check("multi.cross_unreachable", "neither a proxy nor an edge member of one installation can reach the other installation's nginx", cross)

    def same_cidr():
        before = resource_count(lab)
        try:
            lab.preflight_subnet(lab.project_for("inst_c"), va.subnet)
        except Refused as e:
            must("DOCKER_NETWORK" in str(e), str(e))
            must(resource_count(lab) == before, "the refusal changed the resources")
            return f"a third installation asking for A's subnet {va.subnet} is refused (DOCKER_NETWORK) before anything is created"
        raise AssertionError("the same CIDR was accepted for a second installation")
    rep.check("multi.same_cidr_refused", "a third installation asking for an existing installation's CIDR is refused before anything is created", same_cidr)

    def one_away():
        err = lab.finish_variant("inst_a")
        must(err is None, f"removing installation A: {err}")
        left_a = [k for k in OW.ORDER for i in L.run({"container": ["docker", "ps", "-aq"], "network": ["docker", "network", "ls", "-q"], "volume": ["docker", "volume", "ls", "-q"]}[k]
                                                       + ["--filter", f"label={OW.COMPOSE_PROJECT}={va.project}"]).stdout.split()]
        a_stand_ins = [c for c in L.run(["docker", "ps", "-aq", "--filter", f"label={OW.LABEL_PROJECT}={va.project}"]).stdout.split()]
        must(not left_a and not a_stand_ins, f"installation A left resources: {left_a} {a_stand_ins}")
        L.wait_healthy(vb.container, timeout=30)
        net_b = L.run(["docker", "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}", vb.net("tunnel")]).stdout.strip()
        must(net_b == vb.subnet, f"B's tunnel network changed: {net_b}")
        pos = sb.proxy_keeps_client()
        lab.preflight_subnet(lab.project_for("inst_c"), va.subnet)                  # A's subnet is free again
        return f"A removed (no container, network or volume of project {va.project} left); B healthy, network {net_b} intact, positive check still passes ({pos}); A's subnet {va.subnet} is free again"
    rep.check("multi.teardown_one_keeps_other", "removing one installation leaves the other running and intact (and frees only its own subnet)", one_away)
    err = lab.finish_variant("inst_b")
    if err:
        rep.check("multi.teardown_b", "installation B removed", lambda: (_ for _ in ()).throw(L.LabError(err)))


# ── cleanup against the real daemon ──────────────────────────────────────────────
def new_run_id():
    rid = time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]
    assert OW.valid_run_id(rid)
    return rid


def make_resource(kind, name, labels, image=None):
    """A throw-away demo container for the cleanup scene (kind is always 'container'; networks use make_demo_network)."""
    assert kind == "container", kind
    lbl = sum((["--label", f"{k}={v}"] for k, v in (labels or {}).items()), [])
    L.run(["docker", "run", "-d", "--name", name, *lbl, image or L.PY_IMAGE, "sleep", "infinity"])
    return L.run(["docker", "inspect", "--type", "container", "--format", "{{.Id}}", name]).stdout.strip()


def make_demo_network(name, labels, subnet):
    lbl = sum((["--label", f"{k}={v}"] for k, v in (labels or {}).items()), [])
    L.run(["docker", "network", "create", "--internal", "--subnet", subnet, *lbl, name])
    return L.run(["docker", "network", "inspect", "--format", "{{.Id}}", name]).stdout.strip()


def exists(kind, ident):
    cmd = {"container": ["docker", "inspect", "--type", "container"], "network": ["docker", "network", "inspect"]}[kind] + [ident]
    return L.run(cmd, check=False).returncode == 0


def cleanup_scene(lab, rep):
    print("\n== cleanup: only what a run owns", flush=True)
    D, O = new_run_id(), new_run_id()
    while O == D:
        O = new_run_id()
    lab_labels = lambda run, project=None, recorded=None: {OW.LABEL_PURPOSE: PURPOSE, OW.LABEL_RUN: run, **({OW.COMPOSE_PROJECT: project} if project else {}),   # noqa: E731
                                                          **({OW.LABEL_PROJECT: recorded} if recorded else {})}
    own, keep, decoy = {}, {}, []                    # name -> (kind, id)
    state = {"subs": lab.allocate_subnet("cleanup_demo")}

    def next_subnet():
        base = int(state["subs"].network_address)
        state["subs"] = TS.ipaddress.ip_network((base + 8, 29))
        return str(state["subs"])
    try:
        def build():
            nm = lambda s: f"b04r2_{D}_{s}"       # noqa: E731
            own["c"] = ("container", make_resource("container", nm("demo_c"), lab_labels(D)))
            own["n"] = ("network", make_demo_network(nm("demo_n"), lab_labels(D), next_subnet()))
            own["pn"] = ("network", make_demo_network(nm("p_demo_edge"), lab_labels(D, f"b04r2_{D}_p_demo", f"b04r2_{D}_p_demo"), next_subnet()))
            keep["other_c"] = ("container", make_resource("container", f"b04r2_{O}_other_c", lab_labels(O)))
            keep["other_n"] = ("network", make_demo_network(f"b04r2_{O}_other_n", lab_labels(O), next_subnet()))
            keep["foreign_n"] = ("network", make_demo_network("operator_b04r2_shared", None, next_subnet()))
            keep["lookalike_c"] = ("container", make_resource("container", nm("lookalike"), None))
            keep["wrongproj_n"] = ("network", make_demo_network(nm("wrongproj_n"), lab_labels(D, f"b04r2_{O}_p_demo", f"b04r2_{O}_p_demo"), next_subnet()))
            keep["unrecorded_n"] = ("network", make_demo_network(nm("unrecorded_n"), lab_labels(D, f"b04r2_{D}_p_demo"), next_subnet()))
            decoy.extend(v for k, v in keep.items() if k != "other_c" and k != "other_n")
            return f"run {D} owns 3 resources; kept by design: run {O}'s container and network, a foreign `operator_b04r2_shared` network, an unlabelled container named b04r2_{D}_lookalike, a network whose Compose project is run {O}'s, a Compose network without the recorded project"
        if not rep.check("cleanup.setup", "demo resources: three owned, five that must survive", build):
            raise L.LabError("the cleanup scene could not be built")
        everything = {**own, **keep}

        def alive():
            return {k: exists(*v) for k, v in everything.items()}

        def dry():
            res = OW.cleanup_run(lab_runner, D, dry_run=True)
            short = lambda pairs: sorted((k, i[:12]) for k, i in pairs)   # noqa: E731  (`docker ps -q` prints 12 characters, `inspect` 64)
            must(short(res.removed) == short(own.values()), f"the plan {short(res.removed)} is not the three owned resources {short(own.values())}")
            must(all(alive().values()), f"a dry run removed something: {alive()}")
            return f"plan: {len(res.removed)} owned resources; {len(res.kept)} kept ({[k[2][:40] for k in res.kept]}); nothing removed"
        rep.check("cleanup.dry_run", "a dry run lists the plan and removes nothing", dry)

        def cli_refusals():
            py, tool = sys.executable, str(HERE / "r2lab.py")
            none = L.run([py, tool, "cleanup"], check=False, cwd=str(HERE))
            bad = L.run([py, tool, "cleanup", "--run-id", "b04r2_"], check=False, cwd=str(HERE))
            wild = L.run([py, tool, "cleanup", "--run-id", "*"], check=False, cwd=str(HERE))
            must(none.returncode == 2, f"cleanup without a run id: rc={none.returncode}")
            must(bad.returncode == 2 and wild.returncode == 2, f"a malformed run id: rc={bad.returncode}/{wild.returncode}")
            must(all(alive().values()), f"a refused cleanup touched something: {alive()}")
            return "no run id -> exit 2; `b04r2_` and `*` -> exit 2 (REFUSED); every demo resource still exists"
        rep.check("cleanup.cli_requires_run_id", "the command line refuses to run without a concrete, well-formed run id", cli_refusals)

        def real():
            res = OW.cleanup_run(lab_runner, D)
            a = alive()
            must(not any(a[k] for k in own), f"owned resources left: { {k: a[k] for k in own} }")
            must(all(a[k] for k in keep), f"something that must survive was removed: { {k: a[k] for k in keep if not a[k]} }")
            kept_names = sorted(k[2] for k in res.kept)
            must(len(res.kept) == 2 and not res.errors, f"kept={res.kept} errors={res.errors}")
            must(any("lookalike" in n for _, _, n in res.lookalikes), f"the lookalike was not reported: {res.lookalikes}")
            return (f"removed {len(res.removed)} owned resources by id; kept {len(res.kept)} (labelled for this run but with the wrong Compose project: {kept_names}); "
                    f"the lookalike container is only reported; run {O}, the foreign network and the unlabelled container are untouched")
        rep.check("cleanup.owned_only", "cleanup of one run removes exactly its owned resources and keeps another run, foreign and mismatching resources", real)

        def other_run():
            OW.cleanup_run(lab_runner, O)
            a = alive()
            must(not a["other_c"] and not a["other_n"], "run O's own resources survived its own cleanup")
            must(a["foreign_n"] and a["lookalike_c"] and a["wrongproj_n"] and a["unrecorded_n"], "cleanup of run O touched resources that are not its own")
            return f"cleanup of run {O} removed its container and network and nothing else"
        rep.check("cleanup.other_run_own_cleanup", "the other run's resources are removed only by that run's own cleanup", other_run)
    finally:
        # What carries a run's labels goes through the ownership-verified cleanup of that run (a second call is harmless).
        for rid in (D, O):
            try:
                OW.cleanup_run(lab_runner, rid)
            except OW.OwnershipError:
                pass
        # The ONLY removals in this harness that do not go through ownership verification: the four decoys this scene itself
        # created seconds ago (an unlabelled network, an unlabelled container and two networks with mismatching labels), by the
        # ids recorded at creation. They are the test's own fixtures; nothing is selected by a name or a prefix.
        for kind, ident in decoy:
            L.run({"container": ["docker", "rm", "-f", ident], "network": ["docker", "network", "rm", ident]}[kind], check=False)

    def leftovers():
        left = [(k, i) for k, i in list(own.values()) + list(keep.values()) if exists(k, i)]
        must(not left, f"cleanup scene left resources: {left}")
        return "the demo resources of this scene are all gone"
    rep.check("cleanup.scene_clean", "the scene removed every demo resource it created", leftovers)


def world_snapshot():
    """Everything on the daemon that is NOT this lab's (containers: id, image, created, restart policy; networks: id, subnets), taken
    before and after the run. The lab changes nothing outside its own labelled resources, and this is how that is shown."""
    snap = {}
    for cid in L.run(["docker", "ps", "-aq"]).stdout.split():
        p = L.run(["docker", "inspect", "--type", "container", "--format",
                    "{{.Name}}|{{.Image}}|{{.Created}}|{{.HostConfig.RestartPolicy.Name}}|{{json .Config.Labels}}", cid], check=False)
        if p.returncode == 0:
            name, image, created, restart, labels = p.stdout.strip().split("|", 4)
            if f'"{OW.LABEL_PURPOSE}":"{PURPOSE}"' not in labels:
                snap["c:" + cid] = (name, image, created, restart)
    for nid in L.run(["docker", "network", "ls", "-q"]).stdout.split():
        p = L.run(["docker", "network", "inspect", "--format", "{{.Name}}|{{range .IPAM.Config}}{{.Subnet}} {{end}}|{{json .Labels}}", nid], check=False)
        if p.returncode == 0:
            name, subs, labels = p.stdout.strip().split("|", 2)
            if f'"{OW.LABEL_PURPOSE}":"{PURPOSE}"' not in labels:
                snap["n:" + nid] = (name, subs)
    return snap


def cmd_run(args):
    rep = Report()
    print("R2 lab: prerequisites")
    if not prerequisites(rep):
        print("prerequisites missing: nothing else can run (all remaining checks are NOT_RUN)")
        return finish(rep, args, env={})
    before = world_snapshot()
    lab = R2Lab(args.ref, reserved=args.reserved or [])
    print(f"run id: {lab.run_id}   (after an interruption: python tests/nginx_r2/r2lab.py cleanup --run-id {lab.run_id})")
    asgi = [tuple(x.split("=", 1)) for x in (args.asgi_image or [])]
    snaps = {}

    def scene_error(name, e):
        rep.check(f"{name}.scene", f"scene {name} could be built and exercised", lambda e=e: (_ for _ in ()).throw(e))
    try:
        print("\n== static")
        static_checks(lab, rep)
        for vname in C.VARIANT_NAMES:
            try:
                snap = run_scene(lab, rep, vname, asgi)
            except Exception as e:  # noqa: BLE001 - a scene that cannot be built is ERROR, the others still run
                scene_error(vname, e)
                snap = None
            if snap is not None:
                snaps[vname] = snap
            err = lab.finish_variant(vname)
            if err:
                rep.check(f"{vname}.teardown", "variant removed", lambda e=err: (_ for _ in ()).throw(L.LabError(e)))
        if "baseline" in snaps and "candidate" in snaps:
            rep.check("equivalence", "candidate answers every non-forged request exactly like the baseline",
                      lambda: compare_snapshots(snaps["baseline"], snaps["candidate"]))
        else:
            rep.check("equivalence", "candidate answers every non-forged request exactly like the baseline",
                      lambda: (_ for _ in ()).throw(NotRun("a snapshot is missing (a variant could not run)")))
        for name, fn in (("subnet", subnet_scene), ("multi", multi_scene), ("cleanup", cleanup_scene)):
            try:
                fn(lab, rep)
            except Exception as e:  # noqa: BLE001
                scene_error(name, e)
                for v in ("inst_a", "inst_b"):
                    lab.finish_variant(v)
    finally:
        try:
            lab.teardown()
            rep.check("teardown", "every lab container, network and volume of THIS run removed (ownership verified)", lambda: "clean")
        except Exception as e:  # noqa: BLE001
            rep.check("teardown", "every lab container, network and volume of THIS run removed (ownership verified)", lambda e=e: (_ for _ in ()).throw(e))

    def untouched():
        after = world_snapshot()
        gone = {k: v for k, v in before.items() if k not in after}
        changed = {k: (before[k], after[k]) for k in before if k in after and before[k] != after[k]}
        new = {k: v for k, v in after.items() if k not in before}
        must(not gone and not changed and not new, f"outside the lab: gone={gone} changed={changed} new={new}")
        return f"{sum(1 for k in before if k.startswith('c:'))} pre-existing containers and {sum(1 for k in before if k.startswith('n:'))} networks: same ids, images, creation times, restart policies and subnets; nothing new left"
    rep.check("final.nothing_else_changed", "everything on the daemon that is not the lab's is exactly as before the run", untouched)
    env = {"run_id": lab.run_id, "ref": args.ref, "docker": L.run(["docker", "version", "--format", "{{.Server.Version}}"], check=False).stdout.strip(),
           "compose": L.run(["docker", "compose", "version", "--short"], check=False).stdout.strip(),
           "nginx_image": L.run(["docker", "image", "inspect", L.NGINX_IMAGE, "--format", "{{.Id}}"], check=False).stdout.strip(),
           "python_image": L.run(["docker", "image", "inspect", L.PY_IMAGE, "--format", "{{.Id}}"], check=False).stdout.strip(),
           "reserved_ranges": lab.reserved, "pre_existing_containers": sum(1 for k in before if k.startswith("c:")),
           "pre_existing_networks": sum(1 for k in before if k.startswith("n:"))}
    return finish(rep, args, env)


def finish(rep, args, env):
    s = rep.summary()
    print("\nsummary:", s)
    if getattr(args, "out", None):
        pathlib.Path(args.out).write_text(json.dumps({"summary": s, "environment": env, "results": rep.rows, "observations": rep.obs}, indent=1), encoding="utf-8")
        print("wrote", args.out)
    return 0 if rep.ok() else 1


def cmd_static(args):
    rep = Report()
    if not prerequisites(rep):
        return finish(rep, args, {})
    lab = R2Lab(args.ref, reserved=getattr(args, 'reserved', []) or [])
    try:
        static_checks(lab, rep)
    finally:
        try:
            lab.teardown()
        except Exception as e:  # noqa: BLE001
            rep.check("teardown", "lab resources removed", lambda e=e: (_ for _ in ()).throw(e))
    return finish(rep, args, {})


def cmd_cleanup(args):
    """`cleanup --run-id ID`: what run ID owns, verified by labels; never a name, never another run. `--list-runs` is read-only."""
    try:
        if args.list_runs:
            runs = OW.list_runs(lab_runner)
            print("run ids found on resources with the lab's purpose label: " + (", ".join(runs) if runs else "none"))
            return 0
        if not OW.valid_run_id(args.run_id):
            print(f"REFUSED: {args.run_id!r} is not a run id (MMDDhhmm + 4 hex digits); a prefix, a pattern or a name is not enough")
            return 2
        res = OW.cleanup_run(lab_runner, args.run_id, dry_run=args.dry_run)
    except OW.OwnershipError as e:
        print(f"REFUSED (cannot verify): {e}")
        return 3
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {len(res.removed)} resource(s) owned by run {args.run_id}: {res.removed}")
    for kind, ident, why in res.kept:
        print(f"KEPT {kind} {ident}: {why}")
    for kind, ident, name in res.lookalikes:
        print(f"NOT OURS (left untouched) {kind} {ident} {name}")
    for e in res.errors:
        print(f"ERROR {e}")
    return 0 if res.ok() else 1


# ── selfcheck: the checks' own failure handling, no Docker ──────────────
def cmd_selfcheck(args):
    rep = Report()

    def raises(exc, fn):
        try:
            fn()
        except exc:
            return True
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"raised {type(e).__name__} instead of {exc.__name__}")
        raise AssertionError("did not raise")

    ok_res = {"id": "x", "status": 200, "headers": [], "body_b64": __import__("base64").b64encode(json.dumps({"headers": [["X-Real-IP", "1.2.3.4"], ["X-Forwarded-For", "1.2.3.4, 5.6.7.8"]]}).encode()).decode()}
    rep.check("self.transport_error_is_not_a_value", "a failed client request raises TransportError", lambda: raises(L.TransportError, lambda: echo_view({"id": "x", "error": "OSError: refused"})) and "ok")
    rep.check("self.non_200_is_a_failure", "a 502/429 answer is a FAIL, not an echo", lambda: raises(AssertionError, lambda: echo_view({"id": "x", "status": 502, "headers": [], "body_b64": ""})) and "ok")
    rep.check("self.missing_header_is_a_failure", "a missing or duplicated X-Real-IP is a FAIL", lambda: raises(AssertionError, lambda: real_ip({"headers": []})) and raises(AssertionError, lambda: real_ip({"headers": [["X-Real-IP", "a"], ["X-Real-IP", "b"]]})) and "ok")
    rep.check("self.echo_values", "a good echo yields the values", lambda: "ok" if (real_ip(echo_view(ok_res)) == "1.2.3.4" and xff_chain(echo_view(ok_res)) == ["1.2.3.4", "5.6.7.8"]) else (_ for _ in ()).throw(AssertionError("wrong values")))
    rep.check("self.detects_semantics", "detects(): FAIL is detection, a pass is not, an ERROR propagates",
              lambda: (detects(lambda: must(False, "x")) is True and detects(lambda: None) is False and raises(L.LabError, lambda: detects(lambda: (_ for _ in ()).throw(L.LabError("e"))))) and "ok")
    rep.check("self.report_never_passes_an_error", "an unexpected exception is ERROR, a missing image NOT_RUN",
              lambda: _report_semantics())
    rep.check("self.compare_snapshots", "snapshot comparison fails on any difference and on missing keys",
              lambda: raises(AssertionError, lambda: compare_snapshots({"a": "1"}, {"a": "2"})) and raises(AssertionError, lambda: compare_snapshots({"a": "1"}, {"b": "1"})) and compare_snapshots({"a": "1"}, {"a": "1"}) and "ok")
    rep.check("self.exit_status", "no results, or any non-PASS, gives a non-zero exit", lambda: (not Report(quiet=True).ok()) and _exit_semantics() and "ok")
    for tid, desc, status, detail in OFF.run_all(quiet=True):
        def verdict(status=status, detail=detail):
            if status == "FAIL":
                raise AssertionError(detail)
            if status != "PASS":
                raise RuntimeError(detail)
            return detail
        rep.check(f"offline.{tid}", desc, verdict)
    return finish(rep, args, {})


def _report_semantics():
    r = Report(quiet=True)
    r.check("a", "pass", lambda: "x")
    r.check("b", "boom", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r.check("c", "missing", lambda: (_ for _ in ()).throw(NotRun("no image")))
    r.check("d", "fail", lambda: must(False, "no"))
    st = [x["status"] for x in r.rows]
    must(st == ["PASS", "ERROR", "NOT_RUN", "FAIL"], f"statuses {st}")
    must(not r.ok(), "a report with a non-PASS must not be ok")
    return "ok"


def _exit_semantics():
    r = Report(quiet=True)
    r.check("a", "p", lambda: "x")
    must(r.ok(), "all PASS must be ok")
    r.check("b", "f", lambda: must(False, "x"))
    must(not r.ok(), "one FAIL must not be ok")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("static", cmd_static), ("cleanup", cmd_cleanup), ("selfcheck", cmd_selfcheck)):
        p = sub.add_parser(name)
        if name != "cleanup":
            p.add_argument("--ref", default=C.S2_BASE, help="git ref the baseline is read from (default: the deployed S2 commit)")
            p.add_argument("--out", help="write a JSON record of the results")
        if name in ("run", "static"):
            p.add_argument("--reserved", action="append", default=[], metavar="CIDR",
                           help="a range the site uses (LAN, VPN, VPC) that no tunnel subnet may overlap; repeatable")
        if name == "run":
            p.add_argument("--asgi-image", action="append", metavar="LABEL=IMAGE",
                           help="a local image whose real uvicorn is exercised (e.g. api=edu_ai_api:1.0); repeatable")
        if name == "cleanup":
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--run-id", help="the run whose OWNED resources are removed (MMDDhhmm + 4 hex digits, printed by `run`)")
            g.add_argument("--list-runs", action="store_true", help="list the run ids found on labelled resources (read-only)")
            p.add_argument("--dry-run", action="store_true", help="show what would be removed, remove nothing")
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
