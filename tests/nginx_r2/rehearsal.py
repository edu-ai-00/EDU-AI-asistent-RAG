#!/usr/bin/env python3
"""R2 rollout rehearsal: the REAL step commits and the REAL scripts/nginx_r2_switch.sh on throw-away servers. Standard library only.

Runs INSIDE the Linux executor started by rehearsal_linux.py (Docker CLI and Compose 5.3.1 against the host daemon); do not start it
by hand on a workstation. Every "server" is a clone of the release repository (the deployed S2 commit, on top of it the tools and the
three step commits) with a Compose stack made from the REAL docker-compose.yml at that commit, and a lab override that changes only
what a laboratory cannot have:

  * the eight services other than nginx and cloudflared become python stubs (stub.py echoes what nginx forwarded) under the SAME names,
    networks and Compose project, so "another service was rebuilt" means a changed container ID;
  * cloudflared becomes a stand-in container: it logs the tunnel's routes in the format of the real log and registered connections, and
    it is the client that the checks send CF-Connecting-IP from. NO tunnel token, no Cloudflare, no Access policy is involved;
  * container names get the run's prefix, the DNS name edu_ai_nginx is an alias on `edge` (the real file adds it on `tunnel`).

nginx, its image, its healthcheck, its mount of nginx/conf, the networks, `configs`, and every file of the steps are the real ones.
A stand-in is not evidence of real Cloudflare traffic: that stays unverified (see the results document).

    python tests/nginx_r2/rehearsal.py run --release DIR --out FILE [--only name,name]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import types
import uuid

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tests" / "nginx_s2"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))
import candidate as C  # noqa: E402
import labkit as L  # noqa: E402
import ownership as OW  # noqa: E402
import switch_offline as SW  # noqa: E402  (the forbidden step-2 changes of the offline tests are rehearsed live too)
import tunnel_subnet as TS  # noqa: E402
from r2lab import NotRun, Report, must  # noqa: E402

PURPOSE = OW.PURPOSE
PY = L.PY_IMAGE
NGX = L.NGINX_IMAGE
FORGED = "203.0.113.77"
LEGIT = "203.0.113.9"
# the routes of the real tunnel (hostname -> service), read from production metadata on 2026-10-04; the stand-in logs exactly these
INGRESS = [{"hostname": "l88.edu-ai.eu", "originRequest": {}, "service": "http://bifrost:8080"},
           {"hostname": "rag2.edu-ai.eu", "originRequest": {}, "service": "http://edu_ai_nginx:81"},
           {"originRequest": {}, "service": "http_status:404"}]
STUB_PORT = {"bifrost": 8080, "edu_ai": 7860, "edu_ai_api": 8080, "edu_ai_qa_admin": 8082, "edu_ai_search": 8090, "edu_ai_web_chat": 8081,
             "pgadmin": 80, "postgres": 5432}

STANDIN = r'''import json, os, sys, time
def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " INF " + msg, flush=True)
time.sleep(1)
if os.path.exists("/ctl/quiet"):
    log("Starting tunnel (stand-in, quiet: no route and no connection is reported)")
else:
    cfg = {"ingress": json.loads(os.environ["STANDIN_INGRESS"]), "warp-routing": {"enabled": False}}
    log('Updated to new configuration config="' + json.dumps(cfg, separators=(",", ":")).replace('"', '\\"') + '" version=4')
    for i in range(4):
        log("Registered tunnel connection connIndex=%d connection=00000000-0000-0000-0000-00000000000%d event=0 ip=198.41.200.%d location=fra0%d protocol=quic" % (i, i, i, i))
time.sleep(10 ** 9)
'''
CLIENT = r'''import json, sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1], headers=json.loads(sys.argv[2]))
try:
    r = urllib.request.urlopen(req, timeout=8); st = r.status; body = r.read()
except urllib.error.HTTPError as e:
    st = e.code; body = e.read()
except Exception as e:
    print(json.dumps({"error": type(e).__name__ + ": " + str(e)[:150]})); sys.exit(0)
print(json.dumps({"status": st, "body": body.decode("utf-8", "replace")}))
'''


def run(cmd, **kw):
    return L.run(cmd, **kw)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


class Result:
    def __init__(self, rc, out, seconds):
        self.rc, self.out, self.seconds = rc, out, seconds


# ── the rehearsal ────────────────────────────────────────────────────────────────────────────
class Rh:
    def __init__(self, release, rep):
        self.release, self.rep = pathlib.Path(release), rep
        self.run_id = os.environ.get("R2_RUN_ID") or (time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4])
        self.prefix = f"b04r2i_{self.run_id}"
        self.tmp = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / self.prefix
        self.tmp.mkdir(parents=True)
        self.empty_env = self.tmp / "empty.env"
        self.empty_env.write_text("")
        self.rel = self.tmp / "rel"                 # where the variants are made; the servers fetch from it
        run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", "--no-hardlinks", str(release), str(self.rel)])
        for k, v in (("user.name", "rehearsal"), ("user.email", "rehearsal@example.invalid"), ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            self.git("config", k, v)
        self.sha = {b: self.git("rev-parse", f"origin/{b}").stdout.strip() for b in ("s2", "tools", "step1", "step2", "step3")}
        self.sha["base1"] = self.git("rev-parse", "origin/step1^").stdout.strip()      # what step 1 sits on (the tools commit may be a later one)
        self.servers, self.obs = [], {}
        self.lab = ["--label", f"edu_ai.purpose={PURPOSE}", "--label", f"edu_ai.run={self.run_id}"]
        (self.tmp / "standin.py").write_text(STANDIN)
        (self.tmp / "client.py").write_text(CLIENT)
        self.stub = self.tmp / "stub.py"
        shutil.copy(REPO / "tests" / "nginx_s2" / "stub.py", self.stub)

    def git(self, *a, check=True):
        return run(["git", "-C", self.rel, *a], check=check)

    def variant(self, name, parent, edits, msg=None):
        """A commit on top of branch `parent` (a step commit with ONE targeted fault, or a different commit shape)."""
        self.git("checkout", "-q", "-B", f"v_{name}", self.sha[parent] if parent in self.sha else parent)
        for path, fn in edits.items():
            p = self.rel / path
            text = p.read_bytes().decode("utf-8") if p.exists() else ""
            new = fn(text)
            assert new != text, f"variant {name}: the edit of {path} changed nothing"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(new.encode("utf-8"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg or f"rehearsal variant {name}")
        self.sha[name] = self.git("rev-parse", "HEAD").stdout.strip()
        return self.sha[name]

    def step_variant(self, n, name, edits):
        """A SIBLING of the real step commit: the same parent, the real step's files, plus ONE targeted fault."""
        parent = {1: "base1", 2: "step1", 3: "step2"}[n]
        self.git("checkout", "-q", "-B", f"v_{name}", self.sha[parent])
        self.git("checkout", self.sha[f"step{n}"], "--", *C.STEP_FILES[n])
        for path, fn in edits.items():
            p = self.rel / path
            text = p.read_bytes().decode("utf-8") if p.exists() else ""
            new = fn(text)
            assert new != text, f"variant {name}: the edit of {path} changed nothing"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(new.encode("utf-8"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"rehearsal variant {name} of step {n}")
        self.sha[name] = self.git("rev-parse", "HEAD").stdout.strip()
        return self.sha[name]

    def snapshot_world(self):
        snap = {}
        for cid in run(["docker", "ps", "-aq"]).stdout.split():
            p = run(["docker", "inspect", "--format", "{{.Name}}|{{.Image}}|{{.Created}}|{{json .Config.Labels}}", cid], check=False)
            if p.returncode == 0:
                name, image, created, labels = p.stdout.strip().split("|", 3)
                if self.prefix not in name and f'"edu_ai.run":"{self.run_id}"' not in labels and "b04r2x_" not in name:
                    snap["c:" + cid] = (name, image, created)
        for nid in run(["docker", "network", "ls", "-q"]).stdout.split():
            p = run(["docker", "network", "inspect", "--format", "{{.Name}}|{{range .IPAM.Config}}{{.Subnet}} {{end}}", nid], check=False)
            if p.returncode == 0 and self.prefix not in p.stdout:
                snap["n:" + nid] = tuple(p.stdout.strip().split("|", 1))
        return snap

    def teardown(self):
        errs = []
        for s in reversed(self.servers):
            e = s.teardown()
            if e:
                errs.append(e)
        res = OW.cleanup_run(lambda cmd, check=False, **kw: L.run(cmd, check=False), self.run_id)
        if not res.ok():
            errs.append(f"cleanup kept or failed: kept={res.kept} errors={res.errors}")
        for kind, cmd in (("container", ["docker", "ps", "-aq"]), ("network", ["docker", "network", "ls", "-q"]), ("volume", ["docker", "volume", "ls", "-q"])):
            ids = run(cmd + ["--filter", f"label=com.docker.compose.project"], check=False).stdout.split()
            for i in ids:
                lbl = run(["docker", "inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}', i] if kind == "container"
                          else ["docker", kind, "inspect", "--format", '{{index .Labels "com.docker.compose.project"}}', i], check=False).stdout.strip()
                if lbl.startswith(self.prefix + "_"):
                    errs.append(f"{kind} {i} of {lbl} is left")
        shutil.rmtree(self.tmp, ignore_errors=True)
        if errs:
            raise L.LabError("teardown: " + "; ".join(errs))


class Server:
    """One installation: a clone at the deployed S2 commit, the tools copied out of the tools commit (as the operator does), a Compose
    stack with the lab override, its own state directory, subnet and project."""

    def __init__(self, rh, name, *, standin_quiet=False):
        self.rh, self.name = rh, name
        self.project = f"{rh.prefix}_{name}"
        self.dir = rh.tmp / f"srv_{name}"
        self.state = rh.tmp / f"state_{name}"
        self.tools = rh.tmp / f"tools_{name}"
        self.ctl = rh.tmp / f"ctl_{name}"
        self.ctl.mkdir()
        run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", "--no-hardlinks", str(rh.rel), str(self.dir)])
        for k, v in (("user.name", "lab"), ("user.email", "lab@example.invalid"), ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            self.git("config", k, v)
        self.git("checkout", "-q", "-B", "main", rh.sha["s2"])
        # what a real server has besides git, and nothing here may touch
        (self.dir / ".env").write_text("COMPOSE_FILE=docker-compose.yml\nREHEARSAL_SENTINEL=keep\n")
        (self.dir / "uploads").mkdir()
        (self.dir / "uploads" / "marker").write_text("keep me\n")
        self.tools.mkdir()
        for blob, dest in (("scripts/nginx_r2_switch.sh", "r2-switch.sh"), ("scripts/tunnel_subnet.py", "tunnel_subnet.py")):
            (self.tools / dest).write_bytes(subprocess.run(["git", "-C", str(rh.rel), "show", f"{rh.sha['tools']}:{blob}"], capture_output=True, check=True).stdout)
        self.nginx, self.cf, self.edge_member = f"{self.project}_nginx", f"{self.project}_cloudflared", f"{self.project}_edgeclient"
        st = TS.gather(lambda cmd, check=False, **kw: L.run(cmd, check=False), routes="docker", helper_image=NGX)
        self.subnet = str(TS.allocate(self.project, st))
        self.env_file = rh.tmp / f"{name}.env"
        self.env_file.write_text(f"EDU_AI_TUNNEL_SUBNET={self.subnet}\n")
        self.standin_quiet = standin_quiet
        self.override = rh.tmp / f"override_{name}.yaml"
        self.write_override()
        self.args_file = rh.tmp / f"{name}-args.txt"
        self.args_file.write_text("\n".join(["-p", self.project, "--project-directory", str(self.dir), "--env-file", str(self.env_file),
                                              "-f", "@BASE@", "-f", str(self.override)]) + "\n", encoding="utf-8")
        rh.servers.append(self)
        self.up_done = False
        self.shas = {}

    # ── the lab override, made from the effective model of THIS server's S2 file ──
    def write_override(self):
        p = run(["docker", "compose", "-p", self.project, "--project-directory", self.dir, "--env-file", self.rh.empty_env, "-f", self.dir / "docker-compose.yml",
                 "config", "--format", "json"])
        model = json.loads(p.stdout)
        lab = f'    labels:\n      edu_ai.purpose: "{PURPOSE}"\n      edu_ai.run: "{self.rh.run_id}"\n'
        handled = {"networks", "image", "container_name", "labels", "command", "entrypoint", "environment", "volumes", "healthcheck", "restart", "logging", "pull_policy"}
        out = ["services:"]
        for name, svc in model["services"].items():
            nets = list((svc.get("networks") or {}).keys())
            if name == "nginx":
                out += ["  nginx:", f"    container_name: {self.nginx}", "    ports: !override []", lab.rstrip("\n"), "    networks:", "      edge:", "        aliases: [edu_ai_nginx]"]
                continue
            alias = svc.get("container_name") or name
            out += [f"  {name}:", f"    container_name: {self.project}_{name}", "    image: " + PY, "    pull_policy: never", "    entrypoint: !reset null"]
            if name == "cloudflared":
                out += ['    command: !override ["python", "-u", "/standin.py"]',
                        "    environment: !override", f"      STANDIN_INGRESS: '{json.dumps(INGRESS)}'",
                        "    volumes: !override",
                        f"      - {{type: bind, source: {self.rh.tmp / 'standin.py'}, target: /standin.py, read_only: true}}",
                        f"      - {{type: bind, source: {self.ctl}, target: /ctl, read_only: true}}"]
            else:
                out += [f'    command: !override ["python", "/stub.py", "{alias}", "{STUB_PORT[name]}"]', "    environment: !override {}",
                        "    volumes: !override", f"      - {{type: bind, source: {self.rh.stub}, target: /stub.py, read_only: true}}", "    healthcheck: !reset null"]
                out += ["    networks:"] + [f"      {n}:\n        aliases: [{alias}]" for n in nets]
            for k in sorted(set(svc) - handled - {"depends_on"}):
                if k in ("ports", "expose", "build", "platform", "deploy", "env_file", "user", "read_only", "tmpfs", "cap_drop", "cap_add", "security_opt", "shm_size", "mem_limit", "cpus", "init"):
                    out.append(f"    {k}: !reset null")
            if name != "cloudflared":
                out.append("    depends_on: !reset null")
            out.append(lab.rstrip("\n"))
        self.override.write_text("\n".join(out) + "\n", encoding="utf-8")

    # ── commands ──
    def git(self, *a, check=True):
        return run(["git", "-C", self.dir, *a], check=check)

    def fetch(self, name):
        self.git("fetch", "-q", str(self.rh.rel), f"v_{name}:refs/rehearsal/{name}")

    def compose_args(self, base=None):
        return [x.replace("@BASE@", str(base or self.dir / "docker-compose.yml")) for x in self.args_file.read_text().splitlines()]

    def compose(self, *args, check=True, timeout=300):
        return run(["docker", "compose", *self.compose_args(), *args], check=check, timeout=timeout)

    def up(self):
        self.compose("up", "-d", "--pull", "never")
        L.wait_healthy(self.nginx, timeout=90)
        self.wait_standin()
        run(["docker", "run", "-d", "--name", self.edge_member, "--network", f"{self.project}_edge", *self.rh.lab, PY, "sleep", "infinity"])
        run(["docker", "cp", self.rh.tmp / "client.py", f"{self.edge_member}:/client.py"])
        self.up_done = True

    def wait_standin(self, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "Registered tunnel connection" in (run(["docker", "logs", self.cf], check=False).stdout + run(["docker", "logs", self.cf], check=False).stderr):
                return
            time.sleep(1)
        raise L.LabError(f"the stand-in {self.cf} did not log its routes and connections")

    def teardown(self):
        err = None
        for c in (self.edge_member,):
            run(["docker", "rm", "-f", c], check=False)
        for i in run(["docker", "ps", "-aq", "--filter", "label=r2.probe=1"], check=False).stdout.split():
            nets = run(["docker", "inspect", "--format", "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}", i], check=False).stdout
            if self.project in nets:
                run(["docker", "rm", "-f", i], check=False)
        if self.up_done or self.dir.exists():
            p = run(["docker", "compose", *self.compose_args(), "down", "-v", "--remove-orphans", "-t", "1"], check=False, timeout=300)
            if p.returncode != 0:
                err = f"compose down {self.project}: {p.stderr.strip()[-200:]}"
        for tag in run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], check=False).stdout.split():
            if tag.startswith(f"{self.rh.prefix}_evidence/"):
                run(["docker", "rmi", tag], check=False)
        return err

    def sw(self, cmd, step=None, sha=None, env=None, signals=None, timeout=900):
        e = L._env()
        e.update({"R2_NGINX_CONTAINER": self.nginx, "R2_CLOUDFLARED_CONTAINER": self.cf, "R2_STATE_DIR": str(self.state),
                  "R2_COMPOSE_ARGS_FILE": str(self.args_file), "R2_SUBNET_TOOL": str(self.tools / "tunnel_subnet.py"), "R2_PYTHON": "python3",
                  "R2_ROUTES_MODE": "docker", "R2_HELPER_IMAGE": NGX, "R2_HEALTH_DEADLINE": "90", "R2_SMOKE_DEADLINE": "15", "R2_UP_DEADLINE": "120",
                  "R2_RETURN_UP_DEADLINE": "120", "R2_INGRESS_DEADLINE": "25", "R2_RELOAD_DEADLINE": "20",
                  "R2_EVIDENCE_REPO": f"{self.rh.prefix}_evidence", "TMPDIR": os.environ.get("TMPDIR", "/tmp")})
        e.update({k: str(v) for k, v in (env or {}).items()})
        t0 = time.time()
        args = ["bash", str(self.tools / "r2-switch.sh"), cmd] + ([str(step)] if step else []) + ([sha] if sha else [])
        p = subprocess.Popen(args, cwd=str(self.dir), env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        if signals:
            signals(self, p)
        out, _ = p.communicate(timeout=timeout)
        return Result(p.returncode, out or "", time.time() - t0)

    def phase(self):
        f = self.state / "phase"
        return f.read_text().strip() if f.exists() else ""

    def pid(self):
        f = self.state / "pid"
        return f.read_text().strip() if f.exists() else ""

    # ── observation ──
    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def tree(self):
        return self.git("rev-parse", "HEAD^{tree}").stdout.strip()

    def deployed(self):
        """The files the deployment is made of: docker-compose.yml and nginx/ (what a step changes and a return must restore)."""
        return [self.git("rev-parse", "HEAD:docker-compose.yml").stdout.strip(), self.git("rev-parse", "HEAD:nginx").stdout.strip()]

    def dirty(self):
        return sorted(x for x in self.git("status", "--porcelain").stdout.splitlines() if not x.startswith("??"))

    def cinfo(self, c):
        p = run(["docker", "inspect", "--format", "{{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Status}} {{.RestartCount}}", c], check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def nets(self, c):
        p = run(["docker", "inspect", "--format", "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}", c], check=False)
        return sorted(x.replace(self.project + "_", "") for x in p.stdout.split())

    def project_nets(self):
        return sorted(run(["docker", "network", "ls", "--filter", f"label=com.docker.compose.project={self.project}", "--format", "{{.Name}}"]).stdout.split())

    def others(self):
        out = {}
        for cid in run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"]).stdout.split():
            name = run(["docker", "inspect", "--format", "{{.Name}}", cid]).stdout.strip().lstrip("/")
            if name not in (self.nginx, self.cf):
                out[name] = (cid, run(["docker", "inspect", "--format", "{{.State.StartedAt}}", cid]).stdout.strip())
        return out

    def nginx_t(self):
        p = run(["docker", "exec", self.nginx, "nginx", "-T"], check=False)
        return hashlib.sha256((p.stdout + p.stderr).encode()).hexdigest()[:16]

    def full(self):
        """Everything that a refusal must leave exactly as it was, container IDs included."""
        return {"head": self.head(), "dirty": self.dirty(), "nginx": self.cinfo(self.nginx), "cf": self.cinfo(self.cf), "others": self.others(),
                "nets": self.project_nets()}

    def wiring(self):
        """What a RETURN must restore: the content, not the identity of the containers that were recreated."""
        n, c = self.cinfo(self.nginx) or "", self.cinfo(self.cf) or ""
        return {"deployed": self.deployed(), "dirty": self.dirty(), "nginx_image": n.split(" ")[1] if n else None, "cf_image": c.split(" ")[1] if c else None,
                "nginx_nets": self.nets(self.nginx), "cf_nets": self.nets(self.cf), "nets": self.project_nets(), "others": self.others(), "nginx_T": self.nginx_t(),
                "trust_mounted": run(["docker", "exec", self.nginx, "test", "-e", "/etc/edu_ai/tunnel/trust.conf"], check=False).returncode == 0}

    def untracked(self):
        return sorted(x for x in self.git("status", "--porcelain", "--ignored").stdout.splitlines() if x.startswith(("??", "!!")))

    # ── behaviour, seen from the two kinds of client ──
    def echo(self, who, forged=None, xff=None, port=81, path="/search/health"):
        c = self.cf if who == "cf" else self.edge_member
        hdr = {}
        if forged:
            hdr["CF-Connecting-IP"] = forged
        if xff:
            hdr["X-Forwarded-For"] = xff
        if who == "cf":
            run(["docker", "cp", self.rh.tmp / "client.py", f"{c}:/client.py"])
        r = json.loads(L.docker_exec(c, "python", "/client.py", f"http://edu_ai_nginx:{port}{path}", json.dumps(hdr)).stdout)
        if "error" in r:
            raise L.TransportError(f"{who} -> nginx:{port}{path}: {r['error']}")
        body = json.loads(r["body"])
        hv = lambda n: [v for k, v in body["headers"] if k.lower() == n]                  # noqa: E731
        return {"status": r["status"], "real_ip": hv("x-real-ip"), "xff": hv("x-forwarded-for"), "cf": hv("cf-connecting-ip")}

    def ip(self, who):
        c = self.cf if who == "cf" else self.edge_member
        return L.docker_exec(c, "python", "-c", "import socket;print(socket.gethostbyname(socket.gethostname()))").stdout.strip()

    def tunnel_ip_of_cf(self):
        p = run(["docker", "inspect", "--format", "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}", self.cf]).stdout
        return dict(x.split("=") for x in p.split())


def ok(res, want, *frag):
    must(res.rc == want, f"rc={res.rc}, wanted {want}: {res.out[-900:]}")
    for f in frag:
        must(f in res.out, f"the output lacks {f!r}: {res.out[-900:]}")


def same(a, b, what):
    diff = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
    must(not diff, f"{what}: {json.dumps(diff, default=str)[:900]}")


# ── scenario: forward through the three steps, behaviour at every phase, return through all of them ──
def phase_expect(s, step, rh):
    """The behaviour the design promises at a rollout step, measured by clients and by the upstream's own view."""
    out = []
    pos = s.echo("cf", forged=LEGIT, xff="198.51.100.8, 198.51.100.9")
    must(pos["status"] == 200 and pos["real_ip"] == [LEGIT], f"step {step}: the tunnel client is not kept: {pos}")
    if step == 0:
        must(pos["xff"] == [f"198.51.100.8, 198.51.100.9, {LEGIT}"], f"S2 appends to the caller's chain: {pos}")
        out.append("tunnel client kept, caller's X-Forwarded-For chain APPENDED to (the S2 behaviour)")
    else:
        must(pos["xff"] == [LEGIT], f"step {step}: X-Forwarded-For is not the single client address: {pos}")
        out.append("tunnel client kept, X-Forwarded-For = that one address (the caller's chain dropped)")
    own = s.ip("edge")
    neg = s.echo("edge", forged=FORGED, xff="198.51.100.8")
    if step == 3:
        must(neg["real_ip"] == [own] and neg["xff"] == [own], f"step 3: a member of edge could still forge the client: {neg} (own {own})")
        out.append(f"a forged CF-Connecting-IP from `edge` is IGNORED (client stays {own})")
    else:
        must(neg["real_ip"] == [FORGED], f"step {step}: expected the old broad trust (R2 still open), got {neg}")
        out.append("a forged CF-Connecting-IP from `edge` is still honoured (R2 open: the old range is trusted until step 3)")
    return "; ".join(out)


def scn_forward_and_return(rh):
    """All three steps with the checks of each phase, then the return of every step in turn."""
    s = Server(rh, "a")
    rep = rh.rep
    rep.check("fwd.up", "the lab stack (real S2 compose, stubs, stand-in cloudflared) is up", lambda: (s.up(), f"project {s.project}, subnet {s.subnet}, nginx on {s.nets(s.nginx)}, cloudflared on {s.nets(s.cf)}")[1])
    st = {0: s.wiring()}
    rep.check("fwd.s0.behaviour", "S2 as deployed: R2 is reproduced (forged client honoured, caller's X-Forwarded-For chain appended)", lambda: phase_expect(s, 0, rh))
    rep.check("fwd.s0.smoke", "the smoke list passes on S2", lambda: (ok(s.sw("smoke"), 0), "smoke OK")[1])
    before = s.full()
    outage = {}

    def preflight(n):
        def f():
            r = s.sw("preflight", n, rh.sha[f"step{n}"])
            ok(r, 0, "preflight OK")
            same(before if n == 1 else s.full(), s.full(), f"preflight {n} changed something")
            return r.out.strip().splitlines()[-2][:200]
        return f
    rep.check("fwd.step1.preflight", "preflight of step 1 passes and changes nothing", preflight(1))
    for n in (1, 2, 3):
        pre = s.full()
        cf0, nx0 = s.cinfo(s.cf), s.cinfo(s.nginx)

        def apply(n=n):
            if n > 1:
                r = s.sw("preflight", n, rh.sha[f"step{n}"])
                ok(r, 0, "preflight OK")
                same(pre, s.full(), f"preflight {n} changed something")
            r = s.sw("apply", n, rh.sha[f"step{n}"])
            ok(r, 0, "R2_RESULT status=ok")
            m = re.search(r"seconds=([0-9.]+)", r.out)
            outage[n] = m.group(1) if m else "?"
            return f"step {n} applied in {outage[n]} s: " + ", ".join(l.split("phase: ")[1] for l in r.out.splitlines() if "phase: " in l)
        rep.check(f"fwd.step{n}.apply", f"step {n} is applied by the real script", apply)
        st[n] = s.wiring()

        def shape(n=n):
            w, p = st[n], st[n - 1]
            must(s.head() == rh.sha[f"step{n}"], f"HEAD {s.head()[:10]} is not the step commit")
            if n in (1, 3):
                must(s.cinfo(s.nginx) == nx0 and s.cinfo(s.cf) == cf0, "a reload-only step replaced a container")
                return "reload only: nginx and cloudflared are the very same containers"
            must(w["nginx_nets"] == ["edge", "tunnel"] and w["cf_nets"] == ["app", "tunnel", "tunnel_egress"], f"wiring {w['nginx_nets']} / {w['cf_nets']}")
            must(w["nginx_image"] == p["nginx_image"] and w["cf_image"] == p["cf_image"], "an image changed")
            must(w["trust_mounted"] and s.tunnel_ip_of_cf().get(f"{s.project}_tunnel", "").startswith(s.subnet.rsplit(".", 1)[0]), "the trust file or the tunnel address is wrong")
            must(set(w["others"]) == set(p["others"]) and all(w["others"][k] == p["others"][k] for k in w["others"]), "another service was rebuilt")
            return (f"nginx on {w['nginx_nets']}, cloudflared on {w['cf_nets']}; the SAME image IDs; the trust file is mounted; "
                    f"the other {len(w['others'])} services keep ID and start time")
        rep.check(f"fwd.step{n}.shape", f"after step {n}: the containers, wiring and images are as designed", shape)
        rep.check(f"fwd.step{n}.behaviour", f"after step {n}: the behaviour the design promises", lambda n=n: phase_expect(s, n, rh))
        rep.check(f"fwd.step{n}.smoke", f"after step {n}: smoke list and untouched files", lambda: (ok(s.sw("smoke"), 0), same({"u": s.untracked()}, {"u": [x for x in s.untracked()]}, "u"),
                                                                                                          "smoke OK; .env and uploads untouched" if (s.dir / ".env").read_text().endswith("keep\n") else "?")[2])

    def trust_single_source():
        w = st[3]
        t = run(["docker", "exec", s.nginx, "cat", "/etc/edu_ai/tunnel/trust.conf"]).stdout.strip()
        net = run(["docker", "network", "inspect", "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}", f"{s.project}_tunnel"]).stdout.strip()
        must(t == f"set_real_ip_from {s.subnet};" and net == s.subnet, f"trust file {t!r}, network {net}, setting {s.subnet}")
        return f"ONE source: the setting {s.subnet} = the subnet of the Docker network = the range nginx runs with"
    rep.check("fwd.single_source", "the setting, the Docker network and the trust file of the running nginx carry the same CIDR", trust_single_source)

    def proxy_ip_change():
        old = s.tunnel_ip_of_cf()[f"{s.project}_tunnel"]
        squat = f"{s.project}_squat"
        run(["docker", "stop", "-t", "1", s.cf])
        run(["docker", "run", "-d", "--name", squat, "--network", f"{s.project}_tunnel", "--ip", old, *rh.lab, PY, "sleep", "infinity"])
        try:
            run(["docker", "start", s.cf])
            new = s.tunnel_ip_of_cf()[f"{s.project}_tunnel"]
        finally:
            run(["docker", "rm", "-f", squat], check=False)
        s.wait_standin()
        r = s.echo("cf", forged=LEGIT)
        must(r["real_ip"] == [LEGIT], f"{r}")
        must(old != new, f"the proxy came back on the same address {old}: the test did not change it")
        return f"the proxy came back on another address ({old} -> {new}) and is trusted without a reload: membership, not an address"
    rep.check("fwd.proxy_ip_change", "a replaced proxy with another address on `tunnel` is still trusted", proxy_ip_change)

    def tunnel_off():
        run(["docker", "stop", "-t", "1", s.cf])
        try:
            must(s.cinfo(s.nginx).split(" ")[3] == "running", "nginx is not running")
            ok(s.sw("smoke"), 0)
            neg = s.echo("edge", forged=FORGED)
            must(neg["real_ip"] == [s.ip("edge")], f"{neg}")
            members = run(["docker", "network", "inspect", "--format", "{{range .Containers}}{{.Name}} {{end}}", f"{s.project}_tunnel"]).stdout.split()
            must(members == [s.nginx], f"members of tunnel: {members}")
        finally:
            run(["docker", "start", s.cf])
            s.wait_standin()
        return "tunnel stopped: nginx healthy and smoke OK, the only member of `tunnel` is nginx, a forged client from `edge` is still ignored"
    rep.check("fwd.tunnel_off", "with the tunnel switched off nginx keeps serving and nothing is trusted", tunnel_off)

    rep.check("fwd.targets", "every route target of the tunnel is reached from the network cloudflared uses (stand-in client)", lambda: targets(s))
    rh.obs["outage_seconds_per_step"] = outage

    # ── return ──
    for n in (3, 2, 1):
        def back(n=n):
            r = s.sw("rollback", n, rh.sha[f"step{n}"])
            ok(r, 0, "R2_RESULT status=returned")
            same(st[n - 1], s.wiring(), f"after the return of step {n} the state is not the state after step {n - 1}")
            return f"returned: deployed files, loaded nginx configuration, images, wiring, networks and the other services equal the state after step {n - 1}"
        rep.check(f"ret.step{n}", f"the return of step {n} restores the state that preceded it", back)
        rep.check(f"ret.step{n}.behaviour", f"after the return of step {n} the behaviour is that of step {n - 1}", lambda n=n: phase_expect(s, n - 1, rh))

    def repeated():
        r1 = s.sw("rollback-all")
        r2 = s.sw("rollback", 3, rh.sha["step3"])
        r3 = s.sw("rollback", 2, rh.sha["step2"])
        ok(r1, 0)
        ok(r2, 0)
        ok(r3, 0)
        same(st[0], s.wiring(), "a repeated return changed the state")
        return "rollback-all and a second return of steps 3 and 2 on an already returned installation: exit 0, nothing changed"
    rep.check("ret.repeated", "a repeated return is harmless", repeated)

    def final_equal():
        w = s.wiring()
        same(st[0], w, "the S2 state after the full return")
        must(w["nginx_T"] == st[0]["nginx_T"], "nginx -T differs")
        return "after the return of all three steps the deployed files, nginx -T, image IDs, wiring (nginx on edge, cloudflared on app + edge), networks and the other services are those of S2"
    rep.check("ret.equal_s2", "the full return restores the original wiring of nginx AND cloudflared", final_equal)
    rep.check("ret.behaviour_s2", "and the original behaviour (also the original problem: the evidence is not hidden)", lambda: phase_expect(s, 0, rh))
    rep.check("ret.targets", "every route target is reached again through the original wiring", lambda: targets(s))
    rh.obs["deployed_files_after_full_return_equal_s2"] = s.deployed() == [run(["git", "-C", s.dir, "rev-parse", f"{rh.sha['s2']}:docker-compose.yml"]).stdout.strip(),
                                                                              run(["git", "-C", s.dir, "rev-parse", f"{rh.sha['s2']}:nginx"]).stdout.strip()]


def targets(s):
    """The stand-in is where cloudflared is: from there every service of the tunnel's routes must answer."""
    res = []
    for r in INGRESS:
        svc = r["service"]
        if not svc.startswith("http://"):
            continue
        host = svc[7:].split("/")[0]
        out = json.loads(L.docker_exec(s.cf, "python", "/client.py", f"http://{host}/", "{}").stdout)
        must("error" not in out, f"{svc}: {out}")
        res.append(f"{r.get('hostname', '*')} -> {svc}: {out['status']}")
    return "; ".join(res)


# ── helpers shared by the scenarios ──────────────────────────────────────────────────────────
def step_commit(rh, n):
    return rh.sha[f"step{n}"]


def use(s, rh, n, base=None):
    """The commit to apply for step n. The first attempt applies the real step commit. After a return the checkout holds that commit AND its
    revert, so the same commit is no fast-forward any more: a retry is a NEW commit, the step cherry-picked onto the current HEAD in a scratch worktree."""
    real = base or step_commit(rh, n)
    if s.git("merge-base", "--is-ancestor", "HEAD", real, check=False).returncode == 0:
        sha = real
    else:                      # the operator's retry: the commit is made in a scratch worktree, the checked-out branch does not move
        wt = s.dir.parent / f"retry_{s.name}_{n}_{len(s.shas)}_{int(time.time() * 1000) % 100000}"
        s.git("worktree", "add", "-q", "--detach", str(wt), "HEAD")
        run(["git", "-C", wt, "cherry-pick", real])
        sha = run(["git", "-C", wt, "rev-parse", "HEAD"]).stdout.strip()
        s.git("worktree", "remove", "--force", str(wt))
    s.shas[n] = sha
    return sha


def apply_ok(s, rh, n, **kw):
    r = s.sw("apply", n, use(s, rh, n), **kw)
    ok(r, 0, "R2_RESULT status=ok")
    return r


def fail_line(r):
    lines = [l for l in r.out.splitlines() if "FAIL:" in l]
    return (lines[0].split("FAIL:", 1)[1].strip() if lines else r.out.strip().splitlines()[-1])[:230]


def kill_after_phase(phase_name, *sigs, then=None):
    """A `signals` callback for Server.sw: wait until the script reaches a phase, then deliver REAL signals to its pid."""
    def cb(srv, proc):
        def worker():
            deadline = time.time() + 150
            while srv.phase() != phase_name or not srv.pid():
                if proc.poll() is not None or time.time() > deadline:
                    return
                time.sleep(0.1)
            for sig in sigs:
                p = subprocess.run(["bash", "-c", f"kill -s {sig} {srv.pid()}"], capture_output=True, text=True)
                if p.returncode != 0:
                    print(f"kill -s {sig} failed: {p.stderr}")
                time.sleep(0.3)
            if then:
                then(srv, proc)
        threading.Thread(target=worker, daemon=True).start()
    return cb


# ── scenario: the return from EVERY phase, not only from the last ───────────────────────────
def scn_return_phases(rh):
    """Install up to step k (k = 1, 2, 3), then return everything with one command: the S2 state, every time."""
    for k in (1, 2, 3):
        s = Server(rh, f"p{k}")
        s.up()
        w0 = s.wiring()
        for n in range(1, k + 1):
            apply_ok(s, rh, n)

        def back(s=s, k=k, w0=w0):
            must(s.head() == step_commit(rh, k), "the server is not at the expected step")
            r = s.sw("rollback-all")
            ok(r, 0, "R2_RESULT status=returned")
            same(w0, s.wiring(), f"after returning from step {k}")
            phase_expect(s, 0, rh)
            targets(s)
            return f"returned from step {k}: the deployed files, nginx -T, images, wiring of BOTH services, networks and the other services are those of S2; behaviour and route targets as before"
        rh.rep.check(f"phases.from_step{k}", f"installed up to step {k}, then returned in one command: the S2 state", back)
        s.teardown()
        rh.servers.remove(s)


# The lab override replaces these fields of the real services by stand-in values (nginx ports, the command, environment and mounts of cloudflared), so a change of
# them in the base file is not visible in the effective model of the lab: their refusal is shown offline (switch_offline.py) on the real S2 compose.
LAB_MASKED = {"other_service", "nginx_port", "cloudflared_command", "cloudflared_env", "cloudflared_mount"}


# ── scenario: refusals BEFORE any change ──────────────────────────────────────────────────────
def scn_refusals(rh):
    rep = rh.rep
    add_label = lambda t: t.replace("\n  edu_ai_search:\n", "\n  edu_ai_search:\n    labels:\n      rehearsal.touch: \"1\"\n", 1)   # noqa: E731
    v = {
        "bad_conf1": rh.step_variant(1, "bad_conf1", {"nginx/conf/nginx.conf": lambda t: t.replace("listen 80;", "listen 80 nonsense_flag;", 1)}),
        "extra1": rh.step_variant(1, "extra1", {"docker-compose.yml": lambda t: t + "\n# changes a file that step 1 must not change\n"}),
        "other2": rh.step_variant(2, "other2", {"docker-compose.yml": add_label}),
        "image2": rh.step_variant(2, "image2", {"docker-compose.yml": lambda t: t.replace("image: nginx:1.27-alpine", "image: python:3.12-slim", 1)}),
        "wiring2": rh.step_variant(2, "wiring2", {"docker-compose.yml": lambda t: t.replace("      - tunnel_egress\n", "      - tunnel_egress\n      - edge\n", 1)}),
        "literal2": rh.step_variant(2, "literal2", {"docker-compose.yml": lambda t: t.replace("set_real_ip_from ${EDU_AI_TUNNEL_SUBNET};", "set_real_ip_from 10.250.250.0/29;", 1)}),
        "legacy3": rh.step_variant(3, "legacy3", {"nginx/conf/nginx.conf": lambda t: t.replace("include /etc/edu_ai/tunnel/*.conf;", "set_real_ip_from 172.16.0.0/12;\n        include /etc/edu_ai/tunnel/*.conf;", 1)}),
    }
    for name, (edit, _path) in SW.FORBIDDEN.items():        # R2-R4: step 2 that also changes a port, a mount, a command, a capability ... (another service: other2 above)
        if name not in LAB_MASKED:
            v["r4_" + name] = rh.step_variant(2, "r4_" + name, {"docker-compose.yml": lambda t, e=edit: e(t.encode("utf-8")).decode("utf-8")})
    v["scope2"] = rh.step_variant(2, "scope2", {"search/app/evil.py": lambda t: "print('application code')\n"})
    v["sib1"] = rh.step_variant(1, "sib1", {"nginx/conf/search_api.conf": lambda t: t + "# a sibling of step 1\n"})
    app_front = rh.variant("app_front", "s2", {"search/app/evil.py": lambda t: "print('application code')\n"})
    rh.git("checkout", "-q", "-B", "v_over_app", app_front)
    rh.git("checkout", rh.sha["step1"], "--", *C.STEP_FILES[1])
    rh.git("add", "-A")
    rh.git("commit", "-q", "-m", "step 1 on top of application code")
    v["over_app"] = rh.sha["over_app"] = rh.git("rev-parse", "HEAD").stdout.strip()
    rh.git("checkout", "-q", "-B", "v_merge1", rh.sha["base1"])
    (rh.rel / "docs_side.txt").write_text("side\n")
    rh.git("add", "-A")
    rh.git("commit", "-q", "-m", "side commit")
    rh.git("merge", "--no-ff", "-q", "-m", "a merge commit as a candidate", rh.sha["step1"])
    v["merge1"] = rh.sha["merge1"] = rh.git("rev-parse", "HEAD").stdout.strip()
    s = Server(rh, "r")
    s.up()
    for name in list(v) + []:
        run(["git", "-C", s.dir, "fetch", "-q", str(rh.rel), f"v_{name}:refs/rehearsal/{name}"])

    def refuse(label, desc, cmd, step, sha, frag, env=None, pre=None, undo=None):
        def f():
            if pre:
                pre()
            try:
                before = s.full()
                r = s.sw(cmd, step, sha, env=env)
                ok(r, 2, frag)
                same(before, s.full(), "a refusal changed something")
                must("phase: merged" not in r.out and "recreating" not in r.out, "the script went past the preflight")
            finally:
                if undo:
                    undo()
            return "exit 2, nothing changed (HEAD, tracked files, nginx, cloudflared, the other services, networks): " + fail_line(r)
        rep.check(f"refuse.{label}", desc, f)
    nf = "nginx/conf/nginx.conf"
    refuse("unknown_sha", "an unknown commit", "apply", 1, "0123456789abcdef0123456789abcdef01234567", "is not a commit")
    refuse("step2_first", "step 2 before step 1", "apply", 2, step_commit(rh, 2), "not a step-only deployment")
    refuse("step3_first", "step 3 before steps 1 and 2", "apply", 3, step_commit(rh, 3), "not a step-only deployment")
    refuse("nginx_t", "step 1 whose nginx.conf does not pass nginx -t", "apply", 1, v["bad_conf1"], "nginx -t failed")
    refuse("wrong_files", "a step-1 commit that also changes docker-compose.yml", "apply", 1, v["extra1"], "does not change exactly the files of step 1")
    refuse("merge_commit", "a merge commit as the candidate", "apply", 1, v["merge1"], "merge commit")
    refuse("scope_app_front", "R2-R4: a fast-forward that brings application code in front of step 1", "preflight", 1, v["over_app"], "outside the approved scope")
    refuse("dirty", "a tracked file that the merge changes is modified locally", "apply", 1, step_commit(rh, 1), "modified locally",
           pre=lambda: (s.dir / nf).write_text((s.dir / nf).read_text() + "\n# local edit\n"), undo=lambda: s.git("checkout", "--", nf))
    srch = f"{s.project}_edu_ai_search"
    refuse("smoke_fails", "the current nginx already fails the smoke list", "apply", 1, step_commit(rh, 1), "fails the smoke list before any change",
           pre=lambda: run(["docker", "stop", "-t", "1", srch]), undo=lambda: run(["docker", "start", srch]))
    refuse("args_file", "the Compose arguments file cannot be read", "apply", 1, step_commit(rh, 1), "is not readable", env={"R2_COMPOSE_ARGS_FILE": "/nonexistent/args"})
    rep.check("refuse.s0_intact", "after nine refusals the S2 server still behaves as S2 (the evidence of R2 is intact)", lambda: phase_expect(s, 0, rh))
    apply_ok(s, rh, 1)
    refuse("applied", "applying a step that is already applied", "apply", 1, step_commit(rh, 1), "HEAD already is the candidate")
    # R2-R5: a return is judged (step 1-3, the commit of THAT step, the record) before the checkout or a container is touched
    refuse("rb_step99", "R2-R5: a return of step 99", "rollback", 99, step_commit(rh, 1), "must be 1, 2 or 3")
    refuse("rb_tools_commit", "R2-R5: a return of step 1 that names the tools commit (the first script reverted it)", "rollback", 1, rh.sha["tools"],
           "does not change exactly the files of step 1")
    refuse("rb_other_step", "R2-R5: a return of step 1 that names the step-2 commit", "rollback", 1, step_commit(rh, 2), "does not change exactly the files of step 1")
    refuse("rb_sibling", "R2-R5: a return of step 1 that names a sibling commit, not the recorded one", "rollback", 1, v["sib1"], "applied from")
    alt = rh.tmp / "alt-args.txt"
    alt.write_text(s.args_file.read_text().replace(str(s.env_file), str(rh.empty_env)))
    refuse("subnet_unset", "step 2 without the installation's EDU_AI_TUNNEL_SUBNET", "apply", 2, step_commit(rh, 2), "EDU_AI_TUNNEL_SUBNET", env={"R2_COMPOSE_ARGS_FILE": alt})
    refuse("reserved", "the subnet overlaps a range the administrator reserved", "apply", 2, step_commit(rh, 2), "RESERVED", env={"R2_RESERVED": s.subnet})
    decoy = f"{s.project}_decoy"
    refuse("docker_network", "the subnet is taken by another Docker network", "apply", 2, step_commit(rh, 2), "DOCKER_NETWORK",
           pre=lambda: run(["docker", "network", "create", "--subnet", s.subnet, *rh.lab, decoy]), undo=lambda: run(["docker", "network", "rm", decoy], check=False))
    refuse("unverifiable", "the host state cannot be read (helper image missing): refused, not assumed free", "apply", 2, step_commit(rh, 2), "said 3",
           env={"R2_HELPER_IMAGE": "no-such-image:none"})
    proxy = s.tools / "proxy_tool.py"
    proxy.write_text("import os, subprocess, sys\nif sys.argv[1] == 'check':\n    sys.exit(int(os.environ['PROXY_RC']))\n"
                     f"sys.exit(subprocess.call([sys.executable, {str(s.tools / 'tunnel_subnet.py')!r}] + sys.argv[1:]))\n")
    for rc in (1, 2, 4):
        refuse(f"tool_rc{rc}", f"the preflight tool exits {rc}: only 0 lets the caller continue", "apply", 2, step_commit(rh, 2), f"said {rc}, not 0",
               env={"R2_SUBNET_TOOL": proxy, "PROXY_RC": rc})
    delta = "changes more than step 2 may"            # the exact-change check (R2-R4) comes before the older, descriptive ones
    for name, frag, desc in (("other2", "must change exactly cloudflared and nginx", "step 2 that also changes another service"),
                             ("image2", delta, "step 2 that changes the nginx image"),
                             ("wiring2", delta, "step 2 that leaves cloudflared on edge"),
                             ("literal2", delta, "step 2 whose trust config has its own copy of the CIDR"),
                             ("scope2", "does not change exactly the files of step 2", "R2-R4: a step-2 commit that also adds application code")):
        refuse(name, desc, "preflight", 2, v[name], frag)
    for name, (_edit, path) in SW.FORBIDDEN.items():          # `preflight`: a candidate that were wrongly accepted must not be applied and spoil the rest of the scenario
        if name not in LAB_MASKED:
            refuse("model_" + name, f"R2-R4: step 2 that also changes {path}", "preflight", 2, v["r4_" + name], delta)
    cfn = s.cf
    refuse("cf_wiring", "cloudflared is not on the S2 wiring", "apply", 2, step_commit(rh, 2), "cloudflared is on",
           pre=lambda: run(["docker", "network", "disconnect", f"{s.project}_edge", cfn]), undo=lambda: run(["docker", "network", "connect", f"{s.project}_edge", cfn]))

    def quiet(on):
        def f():
            (s.ctl / "quiet").write_text("1") if on else (s.ctl / "quiet").unlink()
            s.compose("up", "-d", "--no-deps", "--force-recreate", "--pull", "never", "cloudflared")
            if not on:
                s.wait_standin()
            else:
                time.sleep(3)
        return f
    refuse("no_routes", "no tunnel route can be read from the log: preserving them cannot be shown", "apply", 2, step_commit(rh, 2), "no tunnel routes can be read",
           pre=quiet(True), undo=quiet(False))
    rep.check("refuse.s1_intact", "after the step-2 refusals the server is still at step 1 and behaves so", lambda: phase_expect(s, 1, rh))
    apply_ok(s, rh, 2)
    refuse("rb_order", "R2-R5: step 1 is not returned while step 2 is applied", "rollback", 1, step_commit(rh, 1), "return step 2 first")
    record, saved = s.state / "steps.log", {}

    def damage():
        saved["text"] = record.read_text()
        record.write_text(f"1 {rh.sha['tools']} x\n2 {step_commit(rh, 2)} y\n")
    refuse("rb_damaged_record", "R2-R5: rollback-all with a record that does not hold the commit of its step", "rollback-all", None, None, "record of step 1",
           pre=damage, undo=lambda: record.write_text(saved["text"]))
    refuse("legacy3", "step 3 that still carries the old range (its files show step 2: the content check of the commit refuses it first)", "apply", 3, v["legacy3"],
           "do not move the deployment from step 2 to step 3")
    rep.check("refuse.s2_intact", "and at step 2 after the step-3 refusal", lambda: phase_expect(s, 2, rh))
    rep.check("refuse.untracked", "no refusal touched .env or the uploads", lambda: (must((s.dir / ".env").read_text().endswith("keep\n") and (s.dir / "uploads" / "marker").exists(), "a local file changed"), "untouched")[1])


# ── scenario: failures DURING a step: the automatic return, and a return that itself fails ────
def scn_failures(rh):
    rep = rh.rep
    s = Server(rh, "f")
    s.up()
    w0 = s.wiring()
    hz = lambda t: t.replace("return 200 '{\"status\":\"ok\"}';", "return 500 'down';", 1)   # noqa: E731
    bad1 = rh.step_variant(1, "bad_health1", {"nginx/conf/nginx.conf": hz})
    bad3 = rh.step_variant(3, "bad_health3", {"nginx/conf/nginx.conf": hz})
    for n, sha in ((1, bad1), (3, bad3)):
        run(["git", "-C", s.dir, "fetch", "-q", str(rh.rel), f"v_{'bad_health%d' % n}:refs/rehearsal/bad{n}"])

    def case(label, desc, step, sha, want, expect_state, env=None, signals=None):
        def f():
            r = s.sw("apply", step, use(s, rh, step, sha), env=env, signals=signals)
            ok(r, want)
            same(expect_state(), s.wiring(), "after the automatic return" if want == 4 else "state")
            return f"exit {want}: {fail_line(r)}"
        rep.check(label, desc, f)
    case("fail1.injected", "step 1: a failure after the reload is returned automatically (exit 4)", 1, None, 4, lambda: w0, env={"R2_TEST_FAIL_AT": "reloaded"})
    case("fail1.bad_health", "step 1: a configuration that passes nginx -t but breaks /health is returned automatically", 1, bad1, 4, lambda: w0)

    def left_as_is():
        r = s.sw("apply", 1, use(s, rh, 1), env={"R2_TEST_FAIL_AT": "reloaded", "R2_AUTO_ROLLBACK": "0"})
        ok(r, 3, "left as is")
        must(s.wiring()["deployed"] != w0["deployed"], "the failed step left no trace although it was not returned")
        r2 = s.sw("rollback", 1, s.shas[1])
        ok(r2, 0, "R2_RESULT status=returned")
        same(w0, s.wiring(), "after the manual return")
        return "R2_AUTO_ROLLBACK=0: exit 3 and the step stays; the manual `rollback` then restores the S2 state"
    rep.check("fail1.manual", "step 1 with the automatic return switched off, then the manual return", left_as_is)
    apply_ok(s, rh, 1)
    w1 = s.wiring()
    case("fail2.nginx", "step 2: a failure right after nginx was recreated: nginx and the networks go back (exit 4)", 2, None, 4, lambda: w1,
         env={"R2_TEST_FAIL_AT": "nginx_recreated"})
    case("fail2.cloudflared", "step 2: a failure after cloudflared moved: BOTH services go back to the S2 wiring (exit 4)", 2, None, 4, lambda: w1,
         env={"R2_TEST_FAIL_AT": "cloudflared_recreated"})

    def quiet_flow(srv, proc):
        def worker():
            while srv.phase() != "nginx_recreated" and proc.poll() is None:
                time.sleep(0.1)
            (srv.ctl / "quiet").write_text("1")
            while srv.phase() != "returning" and proc.poll() is None:
                time.sleep(0.05)
            (srv.ctl / "quiet").unlink()
        threading.Thread(target=worker, daemon=True).start()
    case("fail2.tunnel_silent", "step 2: the recreated cloudflared never reports its routes or connections: returned, routes verified again (exit 4)", 2, None, 4,
         lambda: w1, env={"R2_TEST_SLEEP_AFTER": "nginx_recreated:6"}, signals=quiet_flow)

    def return_fails():
        r = s.sw("apply", 2, use(s, rh, 2), env={"R2_TEST_FAIL_AT": "cloudflared_recreated", "R2_RETURN_UP_DEADLINE": "0.05"})
        ok(r, 5, "THE RETURN FAILED")
        r2 = s.sw("rollback", 2, s.shas[2])
        ok(r2, 0, "R2_RESULT status=returned")
        same(w1, s.wiring(), "after the recovery")
        return "a return that cannot finish says so (exit 5); the same command run again recovers the S2 wiring of both services"
    rep.check("fail2.return_fails", "step 2: the automatic return itself fails (exit 5) and a second run completes it", return_fails)
    apply_ok(s, rh, 2)
    w2 = s.wiring()
    case("fail3.injected", "step 3: a failure after the reload brings the old range back (exit 4)", 3, None, 4, lambda: w2, env={"R2_TEST_FAIL_AT": "reloaded"})
    case("fail3.bad_health", "step 3: a configuration that breaks /health is returned automatically", 3, bad3, 4, lambda: w2)
    case("fail3.late", "step 3: a failure at the very end (after every check) is returned as well", 3, None, 4, lambda: w2, env={"R2_TEST_FAIL_AT": "verified"})
    rep.check("fail.state_s2", "after nine failed or returned attempts the server is at step 2 and behaves so", lambda: phase_expect(s, 2, rh))


# ── scenario: interruption (real signals) and crashes ─────────────────────────────────────────
def scn_interrupts(rh):
    rep = rh.rep
    s = Server(rh, "i")
    s.up()
    w0 = s.wiring()
    apply_ok(s, rh, 1)
    w1 = s.wiring()

    def ignored():
        r = s.sw("apply", 2, use(s, rh, 2), env={"R2_TEST_SLEEP_AFTER": "nginx_recreated:8"}, signals=kill_after_phase("nginx_recreated", "TERM", "HUP", "INT"))
        ok(r, 0, "R2_RESULT status=ok")
        must(s.wiring()["nginx_nets"] == ["edge", "tunnel"] and s.wiring()["cf_nets"] == ["app", "tunnel", "tunnel_egress"], "step 2 is not complete")
        return "SIGTERM, SIGHUP and SIGINT during the step are ignored (an SSH drop must not stop it half-way): the step finished, exit 0"
    rep.check("sig.ignored", "TERM, HUP and INT after the merge do not stop the step", ignored)
    ok(s.sw("rollback", 2, s.shas[2]), 0)
    same(w1, s.wiring(), "after returning the step that survived the signals")

    def killed():
        r = s.sw("apply", 2, use(s, rh, 2), env={"R2_TEST_SLEEP_AFTER": "nginx_recreated:30"}, signals=kill_after_phase("nginx_recreated", "KILL"))
        must(r.rc != 0, f"the script survived SIGKILL: rc={r.rc}")
        part = s.wiring()
        must(part["nginx_nets"] == ["edge", "tunnel"] and part["cf_nets"] == ["app", "edge"], f"the partial state is not the expected one: {part['nginx_nets']} / {part['cf_nets']}")
        r2 = s.sw("rollback", 2, s.shas[2])
        ok(r2, 0, "R2_RESULT status=returned")
        same(w1, s.wiring(), "after the return of a killed step")
        return f"SIGKILL after nginx was recreated (rc {r.rc}): nginx on edge + tunnel, cloudflared still on app + edge; `rollback` restores the step-1 state"
    rep.check("sig.kill", "SIGKILL in the middle of step 2 leaves a state that the return restores", killed)

    def crashed():
        r = s.sw("apply", 2, use(s, rh, 2), env={"R2_TEST_STOP_AFTER": "cloudflared_recreated"})
        ok(r, 99)
        r2 = s.sw("rollback-all")
        ok(r2, 0, "R2_RESULT status=returned")
        same(w0, s.wiring(), "after rollback-all of a crashed step 2")
        return "a crash after both services were recreated: `rollback-all` returns step 2 and step 1, the S2 state"
    rep.check("sig.crash", "a crash after the last container change is returned by rollback-all (steps 2 and 1)", crashed)
    apply_ok(s, rh, 1)
    apply_ok(s, rh, 2)
    w2 = s.wiring()

    def kill_in_return():
        r = s.sw("rollback", 2, s.shas[2], env={"R2_TEST_SLEEP_AFTER": "returned_cloudflared:30"}, signals=kill_after_phase("returned_cloudflared", "KILL"))
        must(r.rc != 0, "the return survived SIGKILL")
        part = s.wiring()
        must(part["cf_nets"] == ["app", "edge"] and part["nginx_nets"] == ["edge", "tunnel"], f"unexpected partial return: {part['nginx_nets']} / {part['cf_nets']}")
        r2 = s.sw("rollback", 2, s.shas[2])
        ok(r2, 0, "R2_RESULT status=returned")
        same(w1, s.wiring(), "after the second return")
        return "the RETURN killed half-way (cloudflared back on edge, nginx not yet): running it again completes it"
    rep.check("sig.kill_return", "a return killed half-way is completed by running it again", kill_in_return)
    ok(s.sw("rollback", 1, s.shas[1]), 0)
    same(w0, s.wiring(), "the full return")


# ── scenario: two separate installations on one daemon ────────────────────────────────────────
def scn_two_installs(rh):
    rep = rh.rep
    a, b = Server(rh, "ia"), Server(rh, "ib")
    a.up()
    b.up()
    rep.check("two.subnets", "two installations, two projects, two different subnets", lambda: (must(a.project != b.project and a.subnet != b.subnet and not TS.ipaddress.ip_network(a.subnet).overlaps(TS.ipaddress.ip_network(b.subnet)), f"{a.subnet} {b.subnet}"),
                                                                                                 f"{a.project}: {a.subnet}; {b.project}: {b.subnet}")[1])
    wa0, wb0 = a.wiring(), b.wiring()
    for n in (1, 2, 3):
        apply_ok(a, rh, n)
    for n in (1, 2):
        apply_ok(b, rh, n)
    def overlap():
        c = Server(rh, "ic")
        c.up()
        try:
            c.env_file.write_text(f"EDU_AI_TUNNEL_SUBNET={a.subnet}\n")
            ok(c.sw("apply", 1, step_commit(rh, 1)), 0)
            snap = c.full()
            r = c.sw("apply", 2, step_commit(rh, 2))
            ok(r, 2, "DOCKER_NETWORK")
            same(snap, c.full(), "a refused installation changed")
            return "a third installation asking for A's subnet is refused before anything changes: " + fail_line(r)
        finally:
            c.teardown()
            rh.servers.remove(c)
    rep.check("two.overlap_refused", "an installation that asks for another installation's subnet is refused (exit 2) and untouched", overlap)
    apply_ok(b, rh, 3)

    def trust_each():
        ta = run(["docker", "exec", a.nginx, "cat", "/etc/edu_ai/tunnel/trust.conf"]).stdout.strip()
        tb = run(["docker", "exec", b.nginx, "cat", "/etc/edu_ai/tunnel/trust.conf"]).stdout.strip()
        must(ta == f"set_real_ip_from {a.subnet};" and tb == f"set_real_ip_from {b.subnet};", f"{ta} / {tb}")
        for x in (a, b):
            r = x.echo("cf", forged=LEGIT)
            must(r["real_ip"] == [LEGIT], f"{x.name}: {r}")
            neg = x.echo("edge", forged=FORGED)
            must(neg["real_ip"] == [x.ip("edge")], f"{x.name}: {neg}")
        return f"A trusts only {a.subnet}, B only {b.subnet}; in each the tunnel client is kept and a forged client from `edge` is ignored"
    rep.check("two.separate_trust", "each nginx trusts only its own installation's tunnel", trust_each)

    def cross():
        out = []
        for x, y in ((a, b), (b, a)):
            nip = y.nets(y.nginx) and json.loads(run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", y.nginx]).stdout)[f"{y.project}_tunnel"]["IPAddress"]
            p = L.docker_exec(x.cf, "python", "-c", f"import socket;s=socket.socket();s.settimeout(3)\ntry:\n s.connect(('{nip}',81));print('OPEN')\nexcept Exception as e:\n print(type(e).__name__)").stdout.strip()
            must(p != "OPEN", f"{x.name}'s proxy reaches {y.name}'s nginx on tunnel")
            out.append(f"{x.name} proxy -> {y.name} nginx tunnel address: {p}")
        return "; ".join(out)
    rep.check("two.cross_unreachable", "the proxy of one installation cannot reach the other installation's nginx", cross)

    def one_away():
        r = a.sw("rollback-all")
        ok(r, 0, "R2_RESULT status=returned")
        same(wa0, a.wiring(), "installation A after its return")
        phase_expect(a, 0, rh)
        must(b.wiring()["nginx_nets"] == ["edge", "tunnel"] and b.echo("edge", forged=FORGED)["real_ip"] == [b.ip("edge")], "installation B was affected")
        phase_expect(b, 3, rh)
        return "A returned to the S2 state; B is untouched (step 3 wiring and behaviour)"
    rep.check("two.return_one", "returning one installation leaves the other one at step 3", one_away)
    ok(b.sw("rollback-all"), 0)
    same(wb0, b.wiring(), "installation B after its return")


# ── scenario: the S2 return window next to R2 ──────────────────────────────────────────────────
def scn_s2_chain(rh):
    rep = rh.rep
    s = Server(rh, "s")
    s.up()
    w0 = s.wiring()
    s2script = s.tools / "s2-switch.sh"
    s2script.write_bytes(subprocess.run(["git", "-C", str(rh.rel), "show", f"{rh.sha['s2']}:scripts/nginx_s2_switch.sh"], capture_output=True, check=True).stdout)

    def s2(cmd, sha, state):
        e = L._env()
        e.update({"S2_CONTAINER": s.nginx, "S2_STATE_DIR": str(rh.tmp / state), "S2_COMPOSE_ARGS_FILE": str(s.args_file), "S2_HEALTH_DEADLINE": "60",
                  "S2_SMOKE_DEADLINE": "20", "S2_UP_DEADLINE": "120", "S2_RETURN_UP_DEADLINE": "120", "S2_EVIDENCE_REPO": f"{rh.prefix}_evidence/s2"})
        p = subprocess.run(["bash", str(s2script), cmd, sha], cwd=str(s.dir), env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        return Result(p.returncode, p.stdout + p.stderr, 0)

    def guard(srv, cmd, sha, state):
        """The S2 entry through scripts/nginx_s2_guard.sh (a copy taken out of the tools commit, as the operator does)."""
        e = s2_env(srv, state)
        e.update({"S2_SCRIPT": str(srv.tools / "s2-switch.sh"), "R2_NGINX_CONTAINER": srv.nginx, "R2_CLOUDFLARED_CONTAINER": srv.cf, "R2_STATE_DIR": str(srv.state)})
        p = subprocess.run(["bash", str(srv.tools / "s2-guard.sh"), cmd, sha], cwd=str(srv.dir), env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        return Result(p.returncode, p.stdout + p.stderr, 0)

    def s2_env(srv, state):
        e = L._env()
        e.update({"S2_CONTAINER": srv.nginx, "S2_STATE_DIR": str(rh.tmp / state), "S2_COMPOSE_ARGS_FILE": str(srv.args_file), "S2_HEALTH_DEADLINE": "60",
                  "S2_SMOKE_DEADLINE": "20", "S2_UP_DEADLINE": "120", "S2_RETURN_UP_DEADLINE": "120", "S2_EVIDENCE_REPO": f"{rh.prefix}_evidence/s2"})
        return e

    def tools_copy(srv):
        (srv.tools / "s2-guard.sh").write_bytes(subprocess.run(["git", "-C", str(rh.rel), "show", f"{rh.sha['tools']}:scripts/nginx_s2_guard.sh"], capture_output=True, check=True).stdout)

    def guard_refuses(srv, n):
        def f():
            before, h0 = srv.wiring(), srv.head()
            r = guard(srv, "rollback", rh.sha["s2"], f"s2state_guard_{srv.name}_{n}")
            must(r.rc == 2 and "REFUSED: R2 is active" in r.out and "rollback-all" in r.out, f"the guard at step {n}: rc {r.rc}: {r.out[-500:]}")
            same(before, srv.wiring(), f"the guard changed something at step {n}")
            must(srv.head() == h0, "HEAD moved")
            must(not (rh.tmp / f"s2state_guard_{srv.name}_{n}").exists(), "the S2 script ran (its state directory exists)")
            return f"R2 step {n} applied: exit 2 `R2 is active ... rollback-all first`; deployed files, wiring, images, networks and the other services unchanged; the S2 script did not run"
        rep.check(f"s2chain.guard_refuses_{srv.name}{n}", f"the S2 return through the guard is refused while R2 step {n} is applied and changes nothing", f)
    tools_copy(s)
    for n in (1, 2, 3):
        apply_ok(s, rh, n)
        guard_refuses(s, n)

    # What the S2 script does while R2 is applied is an OBSERVATION, not a verdict: it is not meant to be used then.
    r = s2("rollback", rh.sha["s2"], "s2state_a")
    after = {"nginx_nets": s.nets(s.nginx), "cf_nets": s.nets(s.cf), "deployed": s.deployed()}
    stat = s.sw("status")
    r2 = s.sw("rollback-all")
    repair = s.sw("rollback-all", env={"R2_FORCE_RESTORE": "1"})
    rh.obs["s2_return_while_r2_applied"] = {"s2_rc": r.rc, "s2_tail": [x[:200] for x in r.out.strip().splitlines()[-3:]], "state_after": after,
                                             "r2_status": [x for x in stat.out.splitlines() if x.startswith(("wiring", "NOTE"))],
                                             "r2_rollback_all_rc_afterwards": r2.rc, "r2_tail": [x[:200] for x in r2.out.strip().splitlines()[-2:]],
                                             "r2_rollback_all_with_R2_FORCE_RESTORE_rc": repair.rc}
    rep.observe("s2chain.while_r2_applied", "the S2 script's own `rollback` run while R2 step 3 is applied, then R2 status and rollback-all", rh.obs["s2_return_while_r2_applied"])
    rep.check("s2chain.hazard_is_reported", "R2 `status` reports that wiring as INCONSISTENT after the S2 script ran (exit 0, smoke OK) and warns against it",
              lambda: (must(any("INCONSISTENT" in x for x in stat.out.splitlines()) and r.rc == 0, f"S2 rc {r.rc}, status: {stat.out[-400:]}"), "S2 `rollback` exited 0 and left nginx on edge only with cloudflared on tunnel; `status` says INCONSISTENT")[1])

    def repaired():
        must(r2.rc == 5, f"rollback-all without the force option: rc={r2.rc}")
        must(repair.rc == 0, f"rollback-all with R2_FORCE_RESTORE=1: rc={repair.rc} {repair.out[-500:]}")
        w = s.wiring()
        for k in ("nginx_nets", "cf_nets", "nginx_image", "cf_image", "trust_mounted", "nets"):
            must(w[k] == w0[k], f"{k}: {w[k]} != {w0[k]}")
        must(set(w["others"]) == set(w0["others"]) and all(w["others"][k] == w0["others"][k] for k in w0["others"]), "another service changed")
        phase_expect(s, 0, rh)
        return ("without the force option the return refuses to overwrite the spliced compose file (exit 5); with R2_FORCE_RESTORE=1 it restores the saved pre-step copy "
                "(the spliced file is kept) and the S2 wiring of both services is back (tree stays modified relative to HEAD, as documented)")
    rep.check("s2chain.forced_repair", "the deliberate repair: R2_FORCE_RESTORE=1 returns R2 after the S2 script damaged docker-compose.yml", repaired)
    s.teardown()
    rh.servers.remove(s)
    t = Server(rh, "t")
    t.up()
    wt0 = t.wiring()
    tools_copy(t)
    for n in (1, 2, 3):
        apply_ok(t, rh, n)
    ok(t.sw("rollback-all"), 0)
    same(wt0, t.wiring(), "R2 fully returned")
    s2script = t.tools / "s2-switch.sh"
    s2script.write_bytes(subprocess.run(["git", "-C", str(rh.rel), "show", f"{rh.sha['s2']}:scripts/nginx_s2_switch.sh"], capture_output=True, check=True).stdout)
    s2_blob = s2script.read_bytes()

    def fails_closed():
        """A failed read of Docker must refuse BEFORE the S2 script runs (real daemon, a docker shim that fails every `inspect`)."""
        shim = rh.tmp / "docker-shim"
        shim.mkdir()
        real = shutil.which("docker")
        (shim / "docker").write_text(f'#!/bin/sh\nif [ "$1" = inspect ]; then echo "Cannot connect to the Docker daemon (injected)" >&2; exit 1; fi\nexec {real} "$@"\n', newline="\n")
        (shim / "docker").chmod(0o755)
        before, h0 = t.wiring(), t.head()
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{shim}:{old_path}"
        try:
            r = guard(t, "rollback", rh.sha["s2"], "s2state_t_closed")
            c = guard(t, "check", rh.sha["s2"], "s2state_t_closed")
        finally:
            os.environ["PATH"] = old_path
        must(r.rc == 2 and c.rc == 2 and "REFUSED: cannot verify that R2 is not active" in r.out + c.out, f"the guard with a failing inspect: rc {r.rc}/{c.rc}: {r.out[-400:]}")
        same(before, t.wiring(), "the guard changed something")
        must(t.head() == h0 and not (rh.tmp / "s2state_t_closed").exists(), "the S2 script ran")
        return "R2 fully returned, S2 wiring in place, but every `docker inspect` fails: rollback and check refuse with exit 2 `cannot verify`, the S2 script did not run, nothing changed"
    rep.check("s2chain.guard_fails_closed", "the guard refuses when Docker cannot be read, before the S2 script runs", fails_closed)

    def s2_after():
        run(["docker", "rm", "-f", t.nginx])               # R2 is returned and nginx is now MISSING: the S2 return exists to recreate it
        c = guard(t, "check", rh.sha["s2"], "s2state_t_check")
        must(c.rc == 0 and "R2 is verified not active" in c.out and "proven absent" in c.out, f"the guard with nginx proven absent: rc {c.rc}: {c.out[-400:]}")
        g = guard(t, "rollback", rh.sha["s2"], "s2state_t")
        p = types.SimpleNamespace(returncode=g.rc, stdout=g.out, stderr="")
        must(p.returncode == 0, f"the S2 return failed after the R2 return: rc={p.returncode} {(p.stdout + p.stderr)[-600:]}")
        must("is the committed original of" in g.out and s2script.read_bytes() == s2_blob, f"the guard did not recognise or kept changing the S2 script: {g.out[:400]}")
        mounts = sorted(m["Destination"] for m in json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", t.nginx]).stdout))
        must("/etc/nginx" not in mounts and "/etc/nginx/nginx.conf" in mounts, f"nginx is not on the pre-S2 definition: {mounts}")
        must(t.nets(t.cf) == ["app", "edge"], f"cloudflared {t.nets(t.cf)}")
        return (f"after R2 was fully returned and nginx was removed, the guard says nginx is proven absent and R2 verified not active and hands the S2 `rollback` over to the unchanged S2 script, "
                f"which recreates it (exit 0): nginx is on the pre-S2 definition (single-file mounts {mounts}), cloudflared on app + edge")
    rep.check("s2chain.s2_after_r2_return", "R2 returned first, then the S2 return through the guard: the chain works", s2_after)


# ── scenario: the script's own logic in PRODUCTION mode (no arguments file: .env pins COMPOSE_FILE) ──
UNIT_SH = r"""
set -u
export R2_SOURCE_ONLY=1 R2_STATE_DIR="$U/state" R2_COMPOSE_ARGS_FILE=
unset R2_COMPOSE_ARGS_FILE
mkdir -p "$U/bin" "$U/state" "$U/co/nginx/conf"
cat > "$U/bin/docker" <<'EOF'
#!/bin/sh
echo "$*" >> "$U_CALLS"
case "$1 $2" in
  "compose config") if [ "$3" = "--format" ]; then echo '{}'; else echo "name: edu_ai"; fi ;;
  "logs "*) printf '%s\n' "$U_LOG" ;;
esac
exit 0
EOF
chmod +x "$U/bin/docker"
export PATH="$U/bin:$PATH" U_CALLS="$U/calls"
cd "$U/co"
printf 'services: {}\n' > docker-compose.yml
check() { if [ "$2" = "$3" ]; then echo "OK: $1"; else echo "FAIL: $1 -- got '$2' wanted '$3'"; fi; }
source "$SCRIPT"
# 1. default mode without a pin in .env or the environment: refused (exit 2), before anything runs
printf 'POSTGRES_PASSWORD=not-printed\n' > .env
out=$( (load_compose_args; check_default_mode) 2>&1 ); rc=$?
check "default mode without COMPOSE_FILE is refused" "$rc" 2
case "$out" in *not-printed*) echo "FAIL: .env content printed" ;; *) echo "OK: the refusal does not print .env" ;; esac
printf 'COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml\n' > .env
( load_compose_args; check_default_mode ) > /dev/null 2>&1; check "a COMPOSE_FILE that adds an override is refused" "$?" 2
printf 'COMPOSE_PROJECT_NAME=edu_ai\nCOMPOSE_FILE=docker-compose.yml\n' > .env
( load_compose_args; check_default_mode ) > /dev/null 2>&1; check "COMPOSE_FILE=docker-compose.yml is accepted" "$?" 0
# 2. the exact argument vectors of production mode
load_compose_args; : > "$U_CALLS"
compose config --hash '*' > /dev/null
compose_with_base "$U/co/docker-compose.yml" config --hash '*' > /dev/null
write_pin sha256:aaa sha256:bbb
compose_up 5 up -d --no-deps --pull never nginx > /dev/null
mapfile -t calls < "$U_CALLS"
check "current model: no -f, Compose reads .env" "${calls[0]}" "compose config --hash *"
check "candidate model: the candidate on stdin, same environment" "${calls[1]}" "compose -f - config --hash *"
check "up: the checkout's file, then the image pin, last" "${calls[2]}" "compose -f $(abs .)/docker-compose.yml -f $(abs "$U/state")/image-pin.yaml up -d --no-deps --pull never nginx"
check "the pin holds exactly the two image IDs" "$(tr '\n' ' ' < "$U/state/image-pin.yaml")" "services:   nginx:     image: sha256:aaa   cloudflared:     image: sha256:bbb "
# 3. arguments-file mode (the rehearsal): the base file mark is replaced, the rest is passed on in order
printf -- '-p\nproj\n-f\n@BASE@\n-f\nover.yaml\n' > "$U/args"
R2_COMPOSE_ARGS_FILE="$U/args"; load_compose_args; : > "$U_CALLS"; PIN_ON=0
: > "$U/x.yml"
compose ps -a -q > /dev/null
compose_with_base "$U/x.yml" config > /dev/null
mapfile -t calls < "$U_CALLS"
check "args file: the mark becomes the checkout's file" "${calls[0]}" "compose -p proj -f $(abs .)/docker-compose.yml -f over.yaml ps -a -q"
check "args file: the mark becomes stdin for the candidate" "${calls[1]}" "compose -p proj -f - -f over.yaml config"
printf -- '-p\nproj\n' > "$U/args2"
( R2_COMPOSE_ARGS_FILE="$U/args2"; load_compose_args ) > /dev/null 2>&1; check "an args file without the base mark is refused" "$?" 2
( R2_COMPOSE_ARGS_FILE="$U/missing"; load_compose_args ) > /dev/null 2>&1; check "an unreadable args file is refused, not replaced by defaults" "$?" 2
# 4. the project, the routes of the tunnel (the real log format) and the workers
unset R2_COMPOSE_ARGS_FILE; COMPOSE_ARGS=()
check "the Compose project is read from the model" "$(expected_project)" "edu_ai"
export U_LOG='2026-08-27T15:27:24Z INF Updated to new configuration config="{\"ingress\":[{\"hostname\":\"l88.edu-ai.eu\",\"originRequest\":{},\"service\":\"http://bifrost:8080\"},{\"hostname\":\"rag2.edu-ai.eu\",\"originRequest\":{},\"service\":\"http://edu_ai_nginx:81\"},{\"originRequest\":{},\"service\":\"http_status:404\"}],\"warp-routing\":{\"enabled\":false}}" version=4'
check "tunnel routes: hostname and service only, in the real log format" "$(cf_ingress | tr '\n' ' ')" "*|http_status:404 l88.edu-ai.eu|http://bifrost:8080 rag2.edu-ai.eu|http://edu_ai_nginx:81 "
case "$(cf_ingress)" in *originRequest*|*warp*|*version*) echo "FAIL: more than hostname and service is kept" ;; *) echo "OK: nothing but hostname and service is kept from the log line" ;; esac
new_workers_only "10 11 " "12 13 " && echo "OK: new workers are recognised" || echo "FAIL: new workers"
new_workers_only "10 11 " "11 13 " && echo "FAIL: an old worker counted as new" || echo "OK: an old worker is not a reload"
new_workers_only "10 " "" && echo "FAIL: no workers counted" || echo "OK: no workers is not a reload"
# 5. which step the files say the checkout is at
mkdir -p nginx/conf
printf 'server {\n  set_real_ip_from 172.16.0.0/12;\n}\n' > nginx/conf/nginx.conf
check "step 0: the S2 files" "$(observed_step)" 0
printf 'server {\n  set_real_ip_from 172.16.0.0/12;   # step 1 and 2 only\n  include /etc/edu_ai/tunnel/*.conf;\n}\n' > nginx/conf/nginx.conf; check "step 1: include, old range kept" "$(observed_step)" 1
printf 'configs:\n  tunnel_trust:\n' > docker-compose.yml; check "step 2: the compose carries the trust config" "$(observed_step)" 2
printf 'server {\n  # set_real_ip_from 172.16.0.0/12;\n  include /etc/edu_ai/tunnel/*.conf;\n}\n' > nginx/conf/nginx.conf; check "step 3: the old range is only a comment" "$(observed_step)" 3
# 6. the scope of a step: its own files, and what a fast-forward may bring besides
check "the files of step 1" "$(step_set 1)" "nginx/conf/nginx.conf nginx/conf/search_api.conf "
check "a fast-forward may bring documentation" "$(extra_allowed docs/plans/x.md && echo yes || echo no)" yes
check "a fast-forward may not bring application code" "$(extra_allowed search/app/server.py && echo yes || echo no)" no
check "the step of a step number outside 1-3 is refused by the return" "$( (rollback_step 99) > /dev/null 2>&1; echo $?)" 1
"""


def scn_script_units(rh):
    """The script's own logic in the mode production uses (no arguments file), with a recording `docker`: exact argument vectors, the .env guard."""
    d = rh.tmp / "units"
    d.mkdir()
    script = rh.tmp / "unit-script.sh"
    script.write_bytes(subprocess.run(["git", "-C", str(rh.rel), "show", f"{rh.sha['tools']}:scripts/nginx_r2_switch.sh"], capture_output=True, check=True).stdout)
    unit = d / "unit.sh"
    unit.write_text(UNIT_SH.replace("\r", ""), newline="\n")
    e = L._env()
    e.update({"U": str(d), "SCRIPT": str(script), "HOME": str(d)})
    p = subprocess.run(["bash", str(unit)], capture_output=True, text=True, env=e, timeout=120)
    lines = [l for l in p.stdout.splitlines() if l.startswith(("OK:", "FAIL:"))]
    bad = [l for l in lines if l.startswith("FAIL:")]
    rh.rep.check("units.production_mode", f"production mode (no arguments file), {len(lines)} checks of the script's own logic with a recording docker",
                 lambda: (must(p.returncode == 0 and lines and not bad, f"rc={p.returncode} {bad or (p.stdout + p.stderr)[-800:]}"),
                          f"{len(lines)} checks: " + "; ".join(l[4:].strip()[:70] for l in lines[:6]) + " ...")[1])


SCENARIOS = {"forward_return": scn_forward_and_return, "return_phases": scn_return_phases, "refusals": scn_refusals, "failures": scn_failures,
             "interrupts": scn_interrupts, "two_installs": scn_two_installs, "s2_chain": scn_s2_chain, "script_units": scn_script_units}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--release", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--only", default="")
    args = ap.parse_args(argv)
    rep = Report()
    rh = Rh(args.release, rep)
    names = [x for x in args.only.split(",") if x] or list(SCENARIOS)
    before = rh.snapshot_world()
    print(f"R2 rehearsal {rh.run_id}: release {json.dumps(rh.sha)}")
    print(run(["docker", "compose", "version"]).stdout.strip())
    try:
        for n in names:
            print(f"\n== scenario {n}", flush=True)
            try:
                SCENARIOS[n](rh)
            except Exception as e:  # noqa: BLE001 - a scenario that cannot run is an ERROR
                rep.check(f"{n}.scene", f"scenario {n} could be built and exercised", lambda e=e: (_ for _ in ()).throw(e))
            finally:
                for s in list(rh.servers):
                    err = s.teardown()
                    if err:
                        rep.check(f"{n}.teardown", "the servers of the scenario were removed", lambda e=err: (_ for _ in ()).throw(L.LabError(e)))
                rh.servers.clear()
    finally:
        try:
            rh.teardown()
            rep.check("teardown", "everything of THIS run removed", lambda: "clean")
        except Exception as e:  # noqa: BLE001
            rep.check("teardown", "everything of THIS run removed", lambda e=e: (_ for _ in ()).throw(e))

    def untouched():
        after = rh.snapshot_world()
        gone = {k: v for k, v in before.items() if k not in after}
        changed = {k: (before[k], after[k]) for k in before if k in after and before[k] != after[k]}
        new = {k: v for k, v in after.items() if k not in before}
        must(not gone and not changed and not new, f"outside the rehearsal: gone={gone} changed={changed} new={new}")
        return f"{sum(1 for k in before if k.startswith('c:'))} pre-existing containers and {sum(1 for k in before if k.startswith('n:'))} networks unchanged"
    rep.check("final.nothing_else_changed", "everything on the daemon that is not the rehearsal's is exactly as before", untouched)
    s = rep.summary()
    print("\nsummary:", s)
    pathlib.Path(args.out).write_text(json.dumps({"summary": s, "release": rh.sha, "compose": run(["docker", "compose", "version", "--short"]).stdout.strip(),
                                                  "results": rep.rows, "observations": rep.obs + [{"id": k, "value": v} for k, v in rh.obs.items()]}, indent=1), encoding="utf-8")
    return 0 if rep.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
