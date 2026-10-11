"""Lab toolkit for the nginx S2 checks. Standard library only; needs Docker.

Everything the lab creates carries the labels
    edu_ai.purpose=b04s2-nginx-lab   edu_ai.run=<run id>
and teardown removes ONLY what this run created. No production access, no secrets:
the compose files under test are run with an EMPTY env file, the upstreams are the
synthetic stubs from stub.py.

The checks here are strict on purpose. A check that cannot run, an unhealthy
container, a failed `docker exec` or a failed HTTP client is an ERROR — never a
PASS, never "CLOSED", never "the expected 404". tests/nginx_s2/run_s2.py selfcheck
proves that with fault injection.
"""
from __future__ import annotations

import atexit
import base64
import collections
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

REPO = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
PURPOSE = "b04s2-nginx-lab"
NGINX_IMAGE = "nginx:1.27-alpine"       # the tag docker-compose.yml uses
PY_IMAGE = "python:3.12-slim"

# upstream name used by nginx.conf -> (short name, port the real service listens on)
UPSTREAMS = {
    "edu_ai_api": ("api", 8080),
    "edu_ai": ("langflow", 7860),
    "edu_ai_pgadmin": ("pgadmin", 80),
    "edu_ai_search": ("search", 8090),
    "edu_ai_web_chat": ("chat", 8081),
    "edu_ai_qa_admin": ("qa", 8082),
}


class LabError(Exception):
    """The lab itself failed (environment, docker, malformed output)."""


class ExecError(LabError):
    """`docker exec` did not run the command (no container, not running, no binary)."""


class NotHealthy(LabError):
    """A container is not (yet) healthy: unhealthy, no healthcheck, not running, timeout."""


class TransportError(LabError):
    """The HTTP client got no HTTP response, so there is no status to judge."""


# ── process helpers ────────────────────────────────────────────────────
def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    env["MSYS_NO_PATHCONV"] = "1"
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    return env


# Every lab subprocess starts in an EMPTY directory, never in the repository root: a
# `docker compose` that is missing -f/--project-directory/--env-file would otherwise
# find the real docker-compose.yml, .env and COMPOSE_* of the checkout.
def rmtree(path):
    """Remove a directory tree, also git clones (their object files are read-only on Windows)."""
    def writable_then_retry(func, p, *_):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, **({"onexc": writable_then_retry} if sys.version_info >= (3, 12) else {"onerror": writable_then_retry}))


NEUTRAL_CWD = pathlib.Path(tempfile.mkdtemp(prefix="b04s2_cwd_"))
atexit.register(lambda: shutil.rmtree(NEUTRAL_CWD, ignore_errors=True))


def run(cmd, *, check=True, input=None, timeout=300, cwd=None, env=None):
    p = subprocess.run([str(c) for c in cmd], input=input, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env=_env() if env is None else env, cwd=str(cwd or NEUTRAL_CWD))
    if check and p.returncode != 0:
        raise LabError(f"{' '.join(str(c) for c in cmd[:5])} ... rc={p.returncode}: {(p.stderr or p.stdout)[-500:]}")
    return p


DOCKER_LEVEL_FAILURE = re.compile(
    r"No such container|is not running|is paused|is restarting|executable file not found|"
    r"OCI runtime exec failed|cannot exec|Error response from daemon|no such file or directory: unknown",
    re.I)


def docker_exec(container, *cmd, input=None, ok_codes=(0,), timeout=120):
    """Run a command in a container. Docker-level failures raise ExecError; the
    command's own exit status must be in `ok_codes` or ExecError is raised too."""
    args = ["docker", "exec"] + (["-i"] if input is not None else []) + [container, *cmd]
    p = run(args, check=False, input=input, timeout=timeout)
    if p.returncode != 0 and DOCKER_LEVEL_FAILURE.search((p.stderr or "") + (p.stdout or "")):
        raise ExecError(f"docker exec {container} {cmd[:3]}: {(p.stderr or p.stdout).strip()[:300]}")
    if p.returncode not in ok_codes:
        raise ExecError(f"docker exec {container} {cmd[:3]} rc={p.returncode}: {(p.stderr or p.stdout).strip()[:300]}")
    return p


def health_status(container):
    """healthy | unhealthy | starting | none (no healthcheck) | notrunning:<state>."""
    p = run(["docker", "inspect", "--format", "{{json .State}}", container], check=False)
    if p.returncode != 0:
        raise LabError(f"inspect {container}: {p.stderr.strip()[:200]}")
    state = json.loads(p.stdout)
    health = state.get("Health")
    if health is None:
        return "none" if state.get("Running") else f"notrunning:{state.get('Status')}"
    return health["Status"]


def wait_healthy(container, timeout=60, poll=1.0):
    """Return only when .State.Health.Status == healthy. unhealthy, no healthcheck,
    not running or a timeout raise NotHealthy."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        last = health_status(container)
        if last == "healthy":
            return
        if last != "starting":
            raise NotHealthy(f"{container}: {last}")
        time.sleep(poll)
    raise NotHealthy(f"{container}: still {last} after {timeout}s")


# ── HTTP result judgement (client results, see client.py) ───────────────
def b64_text(res):
    return base64.b64decode(res["body_b64"]).decode("utf-8", "replace")


def expect_http(res, status, *, envelope_code=None, header=None):
    """True only for a real HTTP response with exactly this status (and envelope /
    header if asked). A transport failure raises TransportError, a different status
    raises AssertionError. There is no path on which a failure returns True."""
    if "error" in res:
        raise TransportError(f"case {res.get('id')}: {res['error']}")
    if res["status"] != status:
        raise AssertionError(f"case {res.get('id')}: status {res['status']} != {status}")
    if envelope_code is not None:
        try:
            got = json.loads(b64_text(res))["error"]["code"]
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"case {res.get('id')}: body is not an error envelope ({e})")
        if got != envelope_code:
            raise AssertionError(f"case {res.get('id')}: envelope {got} != {envelope_code}")
    if header is not None:
        name, value = header
        values = [v for k, v in res["headers"] if k.lower() == name.lower()]
        if value not in values:
            raise AssertionError(f"case {res.get('id')}: header {name}={values} != {value}")
    return True


def upstream_saw_envelope(res):
    """error.code of an error-envelope body, or None when the body is not one."""
    try:
        return json.loads(b64_text(res))["error"]["code"]
    except Exception:  # noqa: BLE001
        return None


def upstream_saw(res):
    """The echo stub's view of the request nginx forwarded (dict), from a client result."""
    if "error" in res:
        raise TransportError(f"case {res.get('id')}: {res['error']}")
    return json.loads(b64_text(res))


def header_values(echo, name):
    return [v for k, v in echo["headers"] if k.lower() == name.lower()]


# ── nginx configuration tokenizer: effective-config comparison ──────────
def parse_config(text):
    """Directives of an nginx config as a Counter of (context, statement). Comments are
    dropped, quotes respected; a block header is recorded in its parent context."""
    tokens, buf, i, quote = [], "", 0, None
    while i < len(text):
        ch = text[i]
        if quote:
            buf += ch
            if ch == "\\" and i + 1 < len(text):
                i += 1
                buf += text[i]
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf += ch
        elif ch == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif ch in ";{}":
            if buf.strip():
                tokens.append(buf.strip())
            tokens.append(ch)
            buf = ""
        else:
            buf += ch
        i += 1
    out, ctx, stmt = collections.Counter(), [], None
    for t in tokens:
        if t == ";":
            out[(tuple(ctx), " ".join(stmt.split()))] += 1
            stmt = None
        elif t == "{":
            header = " ".join((stmt or "").split())
            out[(tuple(ctx), header + " {")] += 1
            ctx.append(header)
            stmt = None
        elif t == "}":
            ctx.pop()
            stmt = None
        else:
            stmt = (stmt + " " + t) if stmt else t
    return out


def config_delta(base_text, cand_text):
    base, cand = parse_config(base_text), parse_config(cand_text)
    return (sorted((base - cand).elements()), sorted((cand - base).elements()))


# ── the lab ────────────────────────────────────────────────────────────
class Lab:
    def __init__(self, baseline_ref="HEAD", keep_tmp=False):
        self.run_id = time.strftime("%m%d%H%M") + uuid.uuid4().hex[:4]
        self.prefix = f"b04s2_{self.run_id}"
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix=self.prefix + "_"))
        self.keep_tmp = keep_tmp
        self.baseline_ref = baseline_ref
        self.baseline_sha = run(["git", "-C", REPO, "rev-parse", baseline_ref]).stdout.strip()
        self.containers, self.networks, self.volumes, self.variants = [], [], [], []
        self.subnets = {}
        self.empty_env = self.tmp / "empty.env"
        self.empty_env.write_text("")

    # labels / naming
    def labels(self):
        return ["--label", f"edu_ai.purpose={PURPOSE}", "--label", f"edu_ai.run={self.run_id}"]

    def name(self, what):
        return f"{self.prefix}_{what}"

    # baseline checkout: exactly what git has at the baseline ref, nothing else
    def extract_baseline(self):
        dest = self.tmp / "baseline"
        dest.mkdir()
        tar = subprocess.run(["git", "-C", str(REPO), "archive", self.baseline_sha, "docker-compose.yml", "nginx"],
                             capture_output=True, check=True, env=_env()).stdout
        subprocess.run(["tar", "-x", "-C", str(dest)], input=tar, check=True, env=_env())
        (dest / ".env").write_text("")   # compose reads env_file: .env; the lab never reads a real one
        return dest

    # candidate: a COPY of the working-tree files (docker-compose.yml, nginx/conf), byte-verified,
    # so the lab never reads the repository's real .env and never writes into the repository
    def extract_candidate(self):
        import hashlib
        dest = self.tmp / "candidate"
        (dest / "nginx" / "conf").mkdir(parents=True)
        compose = (REPO / "docker-compose.yml").read_bytes().replace(b"\r\n", b"\n")
        (dest / "docker-compose.yml").write_bytes(compose)
        for f in (REPO / "nginx" / "conf").iterdir():
            (dest / "nginx" / "conf" / f.name).write_bytes(f.read_bytes())
            assert (dest / "nginx" / "conf" / f.name).read_bytes() == f.read_bytes()
        (dest / ".env").write_text("")
        return dest

    # networks: 172.16/12 (trusted by set_real_ip_from) and 192.168/16 (not trusted)
    def create_networks(self):
        plan = {"n172": [f"172.{o}.{k}.0/24" for o in range(29, 15, -1) for k in (200, 211, 222, 233, 244)],
                "n192": [f"192.168.{k}.0/24" for k in range(200, 251, 7)]}
        for key, candidates in plan.items():
            name = self.name(key)
            for subnet in candidates:
                p = run(["docker", "network", "create", "--subnet", subnet, *self.labels(), name], check=False)
                if p.returncode == 0:
                    self.networks.append(name)
                    self.subnets[key] = subnet
                    break
            else:
                raise LabError(f"no free subnet for {key}")

    def net(self, key):
        return self.name(key)

    # stubs and clients
    def stub_name(self, alias):
        return self.name("stub_" + UPSTREAMS[alias][0])

    def start_stub(self, alias):
        short, port = UPSTREAMS[alias]
        cname = self.stub_name(alias)
        run(["docker", "rm", "-f", cname], check=False)
        extra = ["--network-alias", "pgadmin"] if alias == "edu_ai_pgadmin" else []
        run(["docker", "run", "-d", "--name", cname, "--network", self.net("n172"), "--network-alias", alias, *extra,
             *self.labels(), "--mount", f"type=bind,source={HERE / 'stub.py'},target=/stub.py,readonly",
             PY_IMAGE, "python", "/stub.py", alias, str(port)])
        if cname not in self.containers:
            self.containers.append(cname)
        return cname

    def start_all_stubs(self, skip=()):
        for alias in UPSTREAMS:
            if alias not in skip:
                self.start_stub(alias)
        self.wait_stubs([a for a in UPSTREAMS if a not in skip])

    def wait_stubs(self, aliases, timeout=20):
        for alias in aliases:
            cname, port = self.stub_name(alias), UPSTREAMS[alias][1]
            deadline = time.time() + timeout
            while True:
                p = run(["docker", "exec", cname, "python", "-c",
                         f"import socket;socket.create_connection(('127.0.0.1',{port}),2)"], check=False)
                if p.returncode == 0:
                    break
                if time.time() > deadline:
                    raise LabError(f"stub {alias} not listening: {p.stderr.strip()[:200]}")
                time.sleep(0.4)

    def start_client(self, key):
        cname = self.name("client_" + key)
        run(["docker", "run", "-d", "--name", cname, "--network", self.net(key), *self.labels(),
             PY_IMAGE, "sleep", "infinity"])
        self.containers.append(cname)
        run(["docker", "cp", str(HERE / "client.py"), f"{cname}:/client.py"])
        return cname

    def client(self, cname, mode, params, timeout=300, extra_stdin=""):
        p = docker_exec(cname, "python", "/client.py", mode, input=json.dumps(params) + "\n" + extra_stdin, timeout=timeout)
        try:
            return json.loads(p.stdout)
        except Exception as e:  # noqa: BLE001
            raise LabError(f"client output is not JSON ({e}): {p.stdout[:200]!r} {p.stderr[:200]!r}")

    # nginx variants: the REAL docker-compose.yml service, only name/ports/network overridden
    def variant(self, name, checkout, project=None):
        return Variant(self, name, pathlib.Path(checkout), project=project)

    def leftovers(self):
        """Own resources that still exist: containers, networks and VOLUMES, found both by the
        run label and by the run's name prefix."""
        def ids(kind, *flt):
            out = []
            for f in flt:
                cmd = {"containers": ["docker", "ps", "-aq"], "networks": ["docker", "network", "ls", "-q"],
                       "volumes": ["docker", "volume", "ls", "-q"]}[kind]
                out += run(cmd + ["--filter", f], check=False).stdout.split()
            return sorted(set(out))
        found = {kind: ids(kind, f"label=edu_ai.run={self.run_id}", f"name={self.prefix}_")
                 for kind in ("containers", "networks", "volumes")}
        found["images"] = self.image_tags()
        return {k: v for k, v in found.items() if v}

    def image_tags(self):
        """Image TAGS this run made (the deployment script keeps its image evidence alive under `<prefix>_evidence/nginx:...`)."""
        out = run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], check=False).stdout.split()
        return sorted(x for x in out if x.startswith(f"{self.prefix}_"))

    def verify_clean(self):
        left = self.leftovers()
        if left:
            raise LabError(f"own resources left: {left}")

    def teardown(self):
        errors = []
        for v in reversed(self.variants):          # the SAME explicit definition that started them
            e = v.teardown()
            if e:
                errors.append(e)
        for c in reversed(self.containers):
            run(["docker", "rm", "-f", c], check=False)
        for n in self.networks:
            run(["docker", "network", "rm", n], check=False)
        for vol in self.volumes:
            run(["docker", "volume", "rm", "-f", vol], check=False)
        for tag in self.image_tags():                  # tags only: the images themselves stay under their own names
            run(["docker", "rmi", tag], check=False)
        left = self.leftovers()
        if not self.keep_tmp:
            try:
                rmtree(self.tmp)
            except OSError as e:
                errors.append(f"temp directory {self.tmp} not removed: {e}")
        if left or errors:
            raise LabError(f"teardown left resources: {left} errors={errors}")


class Variant:
    """One nginx under test, started with `docker compose up --no-deps nginx` from a
    checkout directory (the baseline extracted from git, or the working tree)."""

    def __init__(self, lab, name, checkout, project=None):
        self.lab, self.name, self.checkout = lab, name, checkout
        # `compose up --no-deps nginx` still creates every network and volume of the project (3 + 4 here),
        # so sequential variants may share one project name; they must never run at the same time then
        self.project = project or lab.name("p_" + name)
        self.container = lab.name("nginx_" + name)
        self.override = lab.tmp / f"override_{name}.yaml"
        self.override.write_text(
            "services:\n"
            "  nginx:\n"
            f"    container_name: {self.container}\n"
            "    ports: !override []\n"
            "    networks: !override [edge, lab192]\n"
            "networks:\n"
            "  edge: !override\n"
            f"    name: {lab.net('n172')}\n"
            "    external: true\n"
            "  lab192:\n"
            f"    name: {lab.net('n192')}\n"
            "    external: true\n")
        lab.variants.append(self)
        lab.containers.append(self.container)

    def compose(self, *args, check=True):
        return run(["docker", "compose", "-p", self.project, "--project-directory", self.checkout,
                    "--env-file", self.lab.empty_env, "-f", self.checkout / "docker-compose.yml",
                    "-f", self.override, *args], check=check, timeout=300)

    def config_json(self):
        return json.loads(self.compose("config", "--format", "json").stdout)

    def teardown(self):
        """`down` with exactly the definition `up` used (explicit -p/--project-directory/--env-file/-f,
        neutral cwd, no COMPOSE_*); returns an error text or None. The container is also removed by name."""
        err = None
        if self.checkout.exists() and self.override.exists():
            p = self.compose("down", "-v", "--remove-orphans", "-t", "2", check=False)
            if p.returncode != 0:
                err = f"compose down {self.project}: {p.stderr.strip()[-200:]}"
        run(["docker", "rm", "-f", self.container], check=False)
        return err

    def up(self):
        return self.compose("up", "-d", "--no-deps", "nginx")

    def container_id(self):
        return run(["docker", "inspect", "--format", "{{.Id}}", self.container]).stdout.strip()

    def exec(self, *cmd, **kw):
        return docker_exec(self.container, *cmd, **kw)

    def reload(self):
        self.exec("nginx", "-t")
        self.exec("nginx", "-s", "reload")
        time.sleep(0.6)

    def inspect(self):
        return json.loads(run(["docker", "inspect", self.container]).stdout)[0]


# ── timed samples while the harness changes an upstream ─────────────────
class Series:
    """A client process that samples a path every `interval` seconds and receives
    `MARK <label>` lines from the harness, so every time is measured by ONE clock."""

    def __init__(self, client_container, params):
        self.proc = subprocess.Popen(
            ["docker", "exec", "-i", client_container, "python", "/client.py", "series"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", env=_env())
        self.proc.stdin.write(json.dumps(params) + "\n")
        self.proc.stdin.flush()

    def mark(self, label):
        self.proc.stdin.write(f"MARK {label}\n")
        self.proc.stdin.flush()

    def stop(self, timeout=60):
        self.proc.stdin.write("STOP\n")
        self.proc.stdin.flush()
        out, err = self.proc.communicate(timeout=timeout)
        if self.proc.returncode != 0:
            raise ExecError(f"series client rc={self.proc.returncode}: {err[-300:]}")
        try:
            return json.loads(out)
        except Exception as e:  # noqa: BLE001
            raise LabError(f"series output is not JSON ({e}): {out[:200]!r}")


def window_stats(samples, path, t0, t1):
    """Samples of `path` that STARTED in [t0, t1): count, status/error histogram, latency."""
    sel = [x for x in samples if x["path"] == path and t0 <= x["t"] < t1]
    kinds = collections.Counter(
        (f"{x['status']}" + (f"/{x['code']}" if x.get("code") else "")) if x["error"] is None
        else "ERR:" + x["error"].split(":")[0]
        for x in sel)
    el = sorted(x["elapsed"] for x in sel)
    return {"n": len(sel), "kinds": dict(kinds),
            "elapsed_min": el[0] if el else None, "elapsed_med": el[len(el) // 2] if el else None,
            "elapsed_max": el[-1] if el else None}


def first_ok_after(samples, path, t):
    """Start offset of the first status-200 sample of `path` that started at or after t."""
    for x in sorted(samples, key=lambda x: x["t"]):
        if x["path"] == path and x["t"] >= t and x["error"] is None and x["status"] == 200:
            return x["t"]
    return None


# ── TCP probes with a structured result (for "is X unreachable from Y" checks) ──
PROBE_PY = """
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 3)
    print("RESULT:OPEN")
except socket.gaierror:
    print("RESULT:DNS_FAIL")
except ConnectionRefusedError:
    print("RESULT:REFUSED")
except (socket.timeout, TimeoutError):
    print("RESULT:TIMEOUT")
except OSError as e:
    print("RESULT:UNREACHABLE")
"""


def probe_tcp(container, host, port, python="python"):
    """OPEN | DNS_FAIL | REFUSED | TIMEOUT | UNREACHABLE. If the probe itself could not run
    (no container, not running, no interpreter, no RESULT line) this raises ExecError —
    it is never reported as a closed port."""
    p = docker_exec(container, python, "-c", PROBE_PY, host, str(port))
    m = re.search(r"^RESULT:(\w+)", p.stdout, re.M)
    if not m:
        raise ExecError(f"probe in {container} printed no RESULT line: {p.stdout[:120]!r} {p.stderr[:120]!r}")
    return m.group(1)


def expect_closed(container, host, port, **kw):
    kind = probe_tcp(container, host, port, **kw)
    if kind == "OPEN":
        raise AssertionError(f"{container} -> {host}:{port} is OPEN")
    return kind
