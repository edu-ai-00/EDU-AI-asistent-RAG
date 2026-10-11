"""F. Deployment and return rehearsal for S2, driven through the REAL scripts/nginx_s2_switch.sh.

Everything happens in throw-away git clones inside the lab directory and against lab containers
(synthetic upstreams, labelled resources, no published ports); the real checkout, its .env and the
existing stacks are never touched. A "server" here is a clone started at the production SHA 38a0bef,
with an nginx container made from ITS docker-compose.yml. The release the server fetches is the REAL path
production would take: 38a0bef -> 3a2b1d6 -> c2355ae (the baseline) -> tools commit -> S2 commit, the last
two built from the files of the working tree. No scenario uses reset, clean, stash or a force push; the
checks assert that the reflog contains no reset.

A Linux production checkout is modelled: core.autocrlf=false and LF everywhere (the Windows CRLF
hazards are covered by the static checks of the files themselves).
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import cases as C
import labkit as L
from labkit import REPO, run

PROD_START = "38a0bef"                       # the SHA production runs (read from production, 2026-10-01)
SCRIPT = "scripts/nginx_s2_switch.sh"
TOOLS_PREFIXES = ("tests/nginx_s2/", "scripts/nginx_s2_switch.sh", "tests/README.md")
OLD_MOUNTS = ["/etc/nginx/nginx.conf", "/etc/nginx/search_api.conf"]
OLD_MOUNTS_RELATIVE = ["nginx/nginx.conf", "nginx/search_api.conf"]
TRAILER = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
NEW_MOUNTS = ["/etc/nginx"]


def posix(p):
    return pathlib.Path(p).as_posix()


def find_bash():
    cands = [os.environ.get("S2_BASH"), shutil.which("bash")]
    if os.name == "nt":
        cands += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]
    probe = "docker version --format x >/dev/null && git --version >/dev/null && timeout 1 true && echo S2BASHOK"
    for c in filter(None, cands):
        try:
            p = subprocess.run([c, "-c", probe], capture_output=True, text=True, timeout=60)
        except OSError:
            continue
        if "S2BASHOK" in p.stdout:
            return c
    raise L.LabError("no usable bash (needs docker, git and GNU coreutils on PATH; set S2_BASH)")


def candidate_paths():
    """(tools, s2): the files of the candidate, taken from the working tree on top of HEAD. Only explicit
    candidate locations are included, never the rest of the untracked tree (docs/plans, QA, ...)."""
    git = lambda *a: run(["git", "-C", REPO, *a]).stdout  # noqa: E731
    changed = [x for x in git("diff", "--name-status", "HEAD").splitlines() if x]
    bad = [x for x in changed if not x.startswith("M")]
    if bad:
        raise L.LabError(f"the candidate may only MODIFY tracked files, found: {bad}")
    paths = [x.split("\t", 1)[1] for x in changed]
    paths += [x for x in git("ls-files", "--others", "--exclude-standard", "--", "nginx/conf", "tests/nginx_s2", SCRIPT).splitlines()
              if "__pycache__" not in x and not x.endswith(".pyc")]
    tools = sorted(p for p in paths if p.startswith(TOOLS_PREFIXES))
    s2 = sorted(p for p in paths if not p.startswith(TOOLS_PREFIXES))
    return tools, s2


def gaps(samples, path, t_from, t_to, interval):
    """Longest stretch without a successful answer, from the START times of the samples of `path`."""
    def good(x):
        return x["error"] is None and x["status"] == 200
    ok = sorted(x["t"] for x in samples if x["path"] == path and good(x))
    pairs = [(a, b) for a, b in zip(ok, ok[1:]) if b >= t_from and a <= t_to]
    worst = max(pairs, key=lambda ab: ab[1] - ab[0], default=None)
    failed = [x for x in samples if x["path"] == path and not good(x) and t_from <= x["t"] <= t_to]
    kinds = {}
    for x in failed:
        k = ("ERR:" + x["error"].split(":")[0]) if x["error"] else str(x["status"])
        kinds[k] = kinds.get(k, 0) + 1
    return {"failed_samples": len(failed), "kinds": kinds, "interval_s": interval,
            "longest_gap_between_answers_s": None if worst is None else round(worst[1] - worst[0], 2)}


class Result:
    def __init__(self, rc, out, seconds):
        self.rc, self.out, self.seconds = rc, out, seconds


class Running:
    """A script run that is still going: wait() collects its result, kill_signals() delivers real signals to it."""

    def __init__(self, server, cmd, env, proc):
        self.server, self.cmd, self.env, self.proc, self.t0 = server, cmd, env, proc, time.time()

    def wait(self, timeout=900):
        out, _ = self.proc.communicate(timeout=timeout)
        res = Result(self.proc.returncode, out or "", time.time() - self.t0)
        self.server.rh.logs.setdefault(self.server.name, []).append({"cmd": self.cmd, "env": self.env, "rc": res.rc,
                                                                      "seconds": round(res.seconds, 1), "output_tail": res.out[-2500:]})
        return res

    def wait_for_phase(self, phase, timeout=120):
        deadline = time.time() + timeout
        while self.server.phase() != phase or not self.server.script_pid():
            if self.proc.poll() is not None:
                raise AssertionError(f"the script ended (rc={self.proc.returncode}) before phase {phase}")
            if time.time() > deadline:
                raise AssertionError(f"phase {phase} not reached in {timeout}s (at {self.server.phase()!r})")
            time.sleep(0.2)

    def signal(self, *names):
        """Deliver REAL signals (kill -s NAME <pid of the script>) from a separate shell process."""
        pid = self.server.script_pid()
        assert pid, "the script wrote no pid"
        for n in names:
            p = subprocess.run([self.server.rh.bash, "-c", f"kill -s {n} {pid}"], capture_output=True, text=True)
            assert p.returncode == 0, f"kill -s {n} {pid} failed: {p.stderr.strip()}"
            time.sleep(0.3)


class Server:
    """A clone at the production SHA plus (optionally) the lab nginx container made from its compose file."""

    def __init__(self, rh, name, *, container=None, args_file=None, with_variant=True):
        self.rh, self.name = rh, name
        self.srv = rh.root / f"srv_{name}"
        self.state = rh.root / f"state_{name}"
        run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", "--no-hardlinks", str(rh.rel), str(self.srv)])
        for k, v in (("user.name", "lab"), ("user.email", "lab@example.invalid"), ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            self.git("config", k, v)
        self.git("checkout", "-q", "-B", "main", rh.prod)
        # what a real server has besides git: ignored and untracked material that nothing here may touch
        (self.srv / ".env").write_text("COMPOSE_FILE=docker-compose.yml\nREHEARSAL_SENTINEL=keep\n")
        (self.srv / "nginx" / ".htpasswd").write_text("stale-lab-file\n")
        (self.srv / "nginx" / "auth_keys.conf").write_text("stale-lab-file\n")
        (self.srv / "uploads").mkdir()
        (self.srv / "uploads" / "marker").write_text("keep me\n")
        self.released = False
        self.variant = rh.lab.variant(name, self.srv, project=rh.lab.name("p_rh")) if with_variant else None
        rh.servers.append(self)
        self.container = container or (self.variant.container if self.variant else None)
        self.script = rh.root / f"{name}-switch.sh"
        self.script.write_bytes(rh.script_bytes)
        if self.variant:
            self.args_file = self.write_args(f"{name}-args.txt")
        else:
            self.args_file = args_file

    def write_args(self, filename, *, nginx_extra=(), keep_ports=False):
        """The Compose arguments of this server, as the script reads them (`@BASE@` marks the base compose file).
        `nginx_extra` adds lines to the nginx service of the override, `keep_ports` drops the lab's `ports: !override []`
        (so the effective model shows the ports); either way a separate override file is written."""
        v = self.variant
        override = v.override
        if nginx_extra or keep_ports:
            text = v.override.read_text(encoding="utf-8")
            if keep_ports:
                assert "    ports: !override []\n" in text
                text = text.replace("    ports: !override []\n", "")
            extra = "".join(f"    {line}\n" for line in nginx_extra)
            text = text.replace("  nginx:\n", "  nginx:\n" + extra, 1)
            override = self.rh.root / f"{self.name}-{filename}.override.yaml"
            override.write_text(text, encoding="utf-8", newline="\n")
        args = ["-p", v.project, "--project-directory", posix(v.checkout), "--env-file", posix(self.rh.lab.empty_env),
                "-f", "@BASE@", "-f", posix(override)]
        path = self.rh.root / filename
        path.write_text("\n".join(args) + "\n", encoding="utf-8", newline="\n")
        return path

    # git
    def git(self, *a, check=True):
        return run(["git", "-C", self.srv, *a], check=check)

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def status(self):
        return sorted(self.git("status", "--porcelain", "--ignored").stdout.splitlines())

    def untracked_ignored(self):
        return [x for x in self.status() if x.startswith(("!!", "??"))]

    def reflog_kinds(self):
        return [x.split(":")[0] for x in self.git("reflog", "--format=%gs").stdout.splitlines()]

    def subject(self, rev="HEAD"):
        return self.git("log", "-1", "--format=%s", rev).stdout.strip()

    def tracked(self, path, rev="HEAD"):
        return self.git("cat-file", "-e", f"{rev}:{path}", check=False).returncode == 0

    # container
    def cinfo(self):
        p = run(["docker", "inspect", "--format", "{{.Id}} {{.State.StartedAt}} {{.State.Status}}", self.container], check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def cid(self):
        return (self.cinfo() or "").split(" ")[0]

    def inspect(self):
        return self.variant.inspect()

    def mount_dests(self):
        return sorted(m["Destination"] for m in self.inspect()["Mounts"])

    def mounts_are_files(self):
        """Every old mount source is a regular FILE, seen from inside the container (a missing source becomes a directory)."""
        for d in OLD_MOUNTS:
            if L.docker_exec(self.container, "test", "-f", d, ok_codes=(0, 1)).returncode != 0:
                return False
        return True

    def health(self):
        return (self.inspect()["State"].get("Health") or {}).get("Status")

    def has_healthcheck(self):
        return bool(self.inspect()["Config"].get("Healthcheck"))

    def project_containers(self):
        return sorted(run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.variant.project}"]).stdout.split())

    def snap(self):
        return {"head": self.head(), "status": self.status(), "container": self.cinfo(), "reflog": len(self.reflog_kinds())}

    def release(self):
        """Remove this server's nginx container and the project's networks/volumes (same explicit definition as `up`)."""
        if self.variant and not self.released:
            self.released = True
            err = self.variant.teardown()
            assert err is None, err

    def start(self):
        self.variant.up()
        self.rh.su.wait_serving(self.variant)

    # the script
    def sw(self, cmd, sha=None, **env):
        return self.sw_async(cmd, sha, **env).wait()

    def sw_async(self, cmd, sha=None, **env):
        e = L._env()
        e.update({"S2_CONTAINER": self.container, "S2_STATE_DIR": posix(self.state), "S2_COMPOSE_ARGS_FILE": posix(self.args_file),
                  "S2_HEALTH_DEADLINE": "90", "S2_SMOKE_DEADLINE": "20", "S2_UP_DEADLINE": "120",
                  "S2_EVIDENCE_REPO": f"{self.rh.lab.prefix}_evidence/nginx"})
        e.update({k: str(v) for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
        return Running(self, cmd, {k: str(v) for k, v in env.items()},
                       subprocess.Popen([self.rh.bash, posix(self.script), cmd] + ([sha] if sha else []), cwd=str(self.srv), env=e,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"))

    def phase(self):
        p = self.state / "phase"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def script_pid(self):
        p = self.state / "pid"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def smoke_ok(self):
        r = self.sw("smoke")
        assert r.rc == 0, r.out[-400:]


class Rehearsal:
    def __init__(self, lab, rep, su):
        self.lab, self.rep, self.su = lab, rep, su
        self.root = lab.tmp / "rehearsal"
        self.root.mkdir()
        self.bash = find_bash()
        self.prod = run(["git", "-C", REPO, "rev-parse", PROD_START]).stdout.strip()
        self.logs = {}
        self.data = rep.data.setdefault("rehearsal", {"timings": {}, "outage": {}, "logs": self.logs})
        self.sha = {}
        self.servers = []

    # ── the release: the real intermediate commits, then tools and S2 built from the working tree ──
    def build_release(self):
        head = run(["git", "-C", REPO, "rev-parse", "HEAD"]).stdout.strip()
        if head != self.lab.baseline_sha:
            raise L.LabError(f"the candidate must sit on top of the baseline: HEAD {head[:10]} != baseline {self.lab.baseline_sha[:10]}")
        self.tools_paths, self.s2_paths = candidate_paths()
        self.rel = self.root / "rel"
        run(["git", "-c", "core.autocrlf=false", "clone", "--quiet", "--no-hardlinks", str(REPO), str(self.rel)])
        g = self.g
        for k, v in (("user.name", "lab"), ("user.email", "lab@example.invalid"), ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            g("config", k, v)
        base = self.lab.baseline_sha
        # authorship of the two exported commits: S2_PATCH_AUTHOR ("Name <email>", set by clean_clone.py from the real checkout, whose copy
        # in a clean clone has no git identity), else this checkout's git identity, else the lab's
        given = os.environ.get("S2_PATCH_AUTHOR", "")
        if given.endswith(">") and " <" in given:
            who = [given.split(" <")[0], given.rsplit(" <", 1)[1][:-1]]
        else:
            who = [run(["git", "-C", REPO, "config", k], check=False).stdout.strip() for k in ("user.name", "user.email")]
        identity = (who[0] or "lab", who[1] or "lab@example.invalid")

        def commit(branch, parent, *, copy=(), edits=None, remove=(), msg, release=False):
            g("checkout", "-q", "-B", branch, parent)
            touched = list(copy)
            for p in copy:
                dst = self.rel / p
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes((REPO / p).read_bytes().replace(b"\r\n", b"\n"))
            for p, fn in (edits or {}).items():
                path = self.rel / p
                text = path.read_bytes().decode("utf-8")
                new = fn(text)
                assert new != text, f"edit of {p} for {branch} changed nothing"
                path.write_bytes(new.encode("utf-8"))
                touched.append(p)
            for p in remove:
                g("rm", "-q", p)
            if touched:
                g("add", "--", *touched)
            if release:      # the two commits that are exported as patches: authored by the person whose git identity this checkout has
                env = dict(L._env(), GIT_AUTHOR_NAME=identity[0], GIT_AUTHOR_EMAIL=identity[1], GIT_COMMITTER_NAME=identity[0], GIT_COMMITTER_EMAIL=identity[1])
                g("commit", "-q", "-m", msg, "-m", TRAILER, env=env)
            else:
                g("commit", "-q", "-m", msg)
            sha = g("rev-parse", "HEAD").stdout.strip()
            self.sha[branch] = sha
            return sha

        commit("tools", base, copy=self.tools_paths, release=True,
               msg="test(nginx): S2 lab, deploy/return script and clean-clone runner")
        commit("s2", "tools", copy=self.s2_paths, release=True,
               msg="feat(nginx): S2 directory mount, run-time DNS for upstreams, healthcheck, no depends_on")
        changed = sorted(g("diff", "--name-only", base, "s2").stdout.split())
        assert changed == sorted(self.tools_paths + self.s2_paths), "the rehearsal release does not hold exactly the candidate files"
        assert g("show", "s2:nginx/nginx.conf").stdout == g("show", f"{base}:nginx/nginx.conf").stdout, "S2 must leave nginx/nginx.conf alone"
        export = os.environ.get("S2_EXPORT_PATCHES")
        if export:                      # the exact two commits the rehearsal deploys, as patches (nothing is committed in the real repository)
            out = pathlib.Path(export)
            out.mkdir(parents=True, exist_ok=True)
            for old_patch in out.glob("*.patch"):
                old_patch.unlink()
            g("format-patch", "-q", "-o", str(out), f"{base}..s2")
            (out / "diffstat.txt").write_text(g("diff", "--stat", base, "s2").stdout, encoding="utf-8", newline="\n")

        def nl(text):
            return "\r\n" if "\r\n" in text else "\n"

        def replace_once(old, new):
            def fn(text):
                assert text.count(old) == 1, f"{old!r} occurs {text.count(old)}x"
                return text.replace(old, new)
            return fn

        def on_s2(name, edits, msg):
            # a variant of the S2 commit: the same candidate files with ONE targeted fault
            return commit(name, "tools", copy=self.s2_paths, edits=edits, msg=msg)

        def add_label(text):
            n = nl(text)
            block = n + "  edu_ai_search:" + n          # the service itself, not a `depends_on` entry of the same name
            assert text.count(block) == 1, f"the edu_ai_search service header occurs {text.count(block)}x"
            assert "labels:" not in text, "the compose file already has labels"
            return text.replace(block, block + "    labels:" + n + '      rehearsal.touch: "1"' + n)

        on_s2("bad_conf", {"nginx/conf/nginx.conf": replace_once("listen 80;", "listen 80 nonsense_flag;")}, "rehearsal: S2 with a broken nginx.conf")
        on_s2("bad_compose", {"docker-compose.yml": add_label}, "rehearsal: S2 that also changes another service")
        on_s2("bad_health", {"nginx/conf/nginx.conf": replace_once("return 200 '{\"status\":\"ok\"}';", "return 500 'down';")}, "rehearsal: S2 whose /health fails")
        on_s2("bad_smoke", {"nginx/conf/search_api.conf": replace_once("proxy_pass http://search_service/;", "return 503 '{\"x\":1}';")},
              "rehearsal: S2 whose /search/ fails")
        on_s2("bad_mount", {"docker-compose.yml": replace_once("source: ./nginx/conf", "source: ./nginx/does-not-exist")},
              "rehearsal: S2 with a missing mount source")
        on_s2("bad_daemon", {"nginx/conf/nginx.conf": lambda text: replace_once("events {", "daemon off;" + nl(text) + "events {")(text)},
              "rehearsal: S2 whose nginx.conf has `daemon off;` (a duplicate of the image's own -g 'daemon off;')")
        on_s2("bad_mount_wide", {"docker-compose.yml": replace_once("source: ./nginx/conf", "source: ./nginx")}, "rehearsal: S2 that mounts all of nginx/")
        on_s2("bad_image", {"docker-compose.yml": replace_once("image: nginx:1.27-alpine", "image: python:3.12-slim")}, "rehearsal: S2 that changes the image")
        on_s2("bad_ports", {"docker-compose.yml": replace_once('- "80:80"', '- "8080:80"')}, "rehearsal: S2 that changes a published port")
        on_s2("bad_healthcheck", {"docker-compose.yml": replace_once('http://127.0.0.1/health"]', 'http://127.0.0.1/healthz"]')},
              "rehearsal: S2 whose healthcheck asks another path")
        # an earlier commit changes the OLD nginx definition: not an S2-only deployment
        commit("pre_touch", "tools", edits={"nginx/nginx.conf": lambda t: t + nl(t) + "# touched before S2" + nl(t)}, msg="rehearsal: earlier change to nginx.conf")
        commit("pre_touch", "pre_touch", copy=self.s2_paths, msg="rehearsal: S2 on top of it")
        # later releases, on top of S2
        commit("l1", "s2", edits={"specs/active/spec.migration-registry.md": lambda t: t + nl(t) + "Later release L1 touched this line." + nl(t),
                                  "docker-compose.yml": add_label}, msg="rehearsal: later release L1 (other files, other service)")
        def tune_healthcheck(text):
            key = 'http://127.0.0.1/health"]' + nl(text) + "      interval: 15s"
            assert text.count(key) == 1, "the nginx healthcheck was not found exactly once"
            return text.replace(key, key.replace("15s", "10s"))

        def blank_inside_nginx(text):
            key = "      start_period: 5s" + nl(text) + "    restart: unless-stopped"
            assert text.count(key) == 1, f"the end of the nginx healthcheck was found {text.count(key)}x"
            return text.replace(key, "      start_period: 5s" + nl(text) + nl(text) + "    restart: unless-stopped")

        commit("l2", "s2", edits={"docker-compose.yml": lambda t: blank_inside_nginx(add_label(tune_healthcheck(t)))},
               msg="rehearsal: later release L2 tunes the S2 healthcheck (conflicts with a revert of S2)")
        commit("l3", "s2", remove=OLD_MOUNTS_RELATIVE, msg="rehearsal: later release L3 (S2b) removes the superseded nginx files")
        self.script_bytes = subprocess.run(["git", "-C", str(self.rel), "show", f"s2:{SCRIPT}"], capture_output=True, check=True).stdout
        g("checkout", "-q", "tools")

    def g(self, *a, **k):
        return run(["git", "-C", self.rel, *a], **k)

    def new_server(self, name):
        for s in self.servers:            # the project is shared: one nginx container at a time
            s.release()
        return Server(self, name)

    def stub_ids(self):
        return {a: run(["docker", "inspect", "--format", "{{.Id}}", self.lab.stub_name(a)]).stdout.strip() for a in L.UPSTREAMS}

    def measure(self, server, fn):
        """Run fn() while a client samples /health (answered by nginx) and /search/health (proxied) every 0.1 s."""
        ser = L.Series(self.su.c172, {"target": server.container, "port": 80, "path": "/health", "extra_paths": ["/search/health"],
                                      "interval": 0.1, "timeout": 2, "headers": C.BASE_HEADERS, "max_seconds": 400})
        time.sleep(1.5)
        ser.mark("begin")
        try:
            res = fn()
        finally:
            ser.mark("end")
            time.sleep(3)
            data = ser.stop()
        m = {x["label"]: x["t"] for x in data["marks"]}
        return res, {p: gaps(data["samples"], p, m["begin"], m["end"] + 3, 0.1) for p in ("/health", "/search/health")}

    def scenario(self, fn):
        label = fn.__name__
        print(f" - scenario {label}: {(fn.__doc__ or '').strip().splitlines()[0] if fn.__doc__ else ''}")
        try:
            fn(self)
        except Exception as e:  # noqa: BLE001 - a scenario that cannot run is an ERROR, never silently skipped
            self.rep.items.append({"name": f"{label} aborted", "status": "ERROR", "detail": f"{type(e).__name__}: {e}"})
            print(f"  ERROR {label} aborted - {type(e).__name__}: {e}")
        finally:
            for s in self.servers:
                try:
                    s.release()
                except Exception as e:  # noqa: BLE001
                    self.rep.items.append({"name": f"{label}: release of {s.name} failed", "status": "ERROR", "detail": str(e)})
            self.servers.clear()


def ok(res, want_rc, *fragments):
    assert res.rc == want_rc, f"rc={res.rc}, wanted {want_rc}: {res.out[-700:]}"
    for f in fragments:
        assert f in res.out, f"{f!r} not in output: {res.out[-700:]}"


def old_definition(s):
    assert s.mount_dests() == OLD_MOUNTS, s.mount_dests()
    assert not s.has_healthcheck(), "the pre-S2 definition has no healthcheck"


def new_definition(s):
    assert s.mount_dests() == NEW_MOUNTS and all(not m["RW"] for m in s.inspect()["Mounts"]), s.inspect()["Mounts"]
    assert s.has_healthcheck() and s.health() == "healthy", s.health()


def no_reset(s):
    bad = [k for k in s.reflog_kinds() if k.startswith(("reset", "clean", "stash"))]
    assert not bad, f"the reflog shows {bad}"


def no_scratch_worktree(s):
    n = len(s.git("worktree", "list").stdout.strip().splitlines())
    assert n == 1, f"{n} worktrees registered: the scratch worktree of the return was not removed"


def one_nginx_container(s):
    n = len(s.project_containers())
    assert n == 1, f"{n} containers in the project (a renamed or half-created nginx is left over)"


# ═══ scenarios ═════════════════════════════════════════════════════════
def scn_deploy_and_return(rh):
    """deploy from 38a0bef over the real intermediate commits, then a revert-only return with a later commit on top"""
    rep, prod, s2, tools = rh.rep, rh.prod, rh.sha["s2"], rh.sha["tools"]
    s = rh.new_server("main")
    s.start()
    stubs0, ignored0, snap0 = rh.stub_ids(), s.untracked_ignored(), s.snap()

    def start_state():
        assert s.head() == prod
        old_definition(s)
        s.smoke_ok()
        return f"checkout at {prod[:7]}; nginx made from ITS compose file (two file mounts, no healthcheck); the 11-request smoke list passes"
    rep.check("F1 start state: checkout at the production SHA 38a0bef, pre-S2 definition serves", start_state)

    def real_path():
        path = rh.g("rev-list", "--reverse", f"{prod}..{s2}").stdout.split()
        subjects = [rh.g("log", "-1", "--format=%h %s", c).stdout.strip() for c in path]
        assert path[-2:] == [tools, s2] and len(path) >= 3, subjects
        assert path[:-2] == rh.g("rev-list", "--reverse", f"{prod}..{rh.lab.baseline_sha}").stdout.split()
        return "the release crosses " + str(len(path) - 2) + " intermediate commit(s): " + "; ".join(subjects)
    rep.check("F2 the release is the real path: intermediate commits, then tools, then S2", real_path)

    def preflight():
        r = s.sw("preflight", s2)
        ok(r, 0, "preflight OK", "changes only the nginx service", "current smoke OK")
        assert s.snap() == snap0, "preflight changed something"
        return "preflight validated the candidate (nginx -t in the RUNNING image with no network, Compose hashes: only nginx, images equal, current smoke) and changed nothing"
    rep.check("F3 preflight changes nothing", preflight)

    holder = {}

    def deploy():
        r, outage = rh.measure(s, lambda: s.sw("deploy", s2))
        ok(r, 0, "S2_RESULT status=ok")
        m = re.search(r"up_seconds=([\d.]+) first_answer_seconds=([\d.]+) healthy_and_smoke_seconds=([\d.]+)", r.out)
        assert m, r.out[-400:]
        assert s.head() == s2
        assert s.cid() != snap0["container"].split(" ")[0], "nginx was not recreated"
        assert rh.stub_ids() == stubs0, "an upstream container changed"
        new_definition(s)
        assert s.untracked_ignored() == ignored0, "ignored/untracked files changed"
        no_reset(s)
        assert not [x for x in s.status() if not x.startswith(("!!", "??"))], "tracked files modified after the deploy"
        rh.data["timings"]["deploy"] = {"total_s": round(r.seconds, 1), "compose_up_s": float(m[1]), "first_answer_s": float(m[2]), "healthy_and_smoke_s": float(m[3])}
        rh.data["outage"]["deploy"] = outage
        holder["outage"] = outage
        return (f"exit 0 in {r.seconds:.1f} s: compose up {m[1]} s, first answer {m[2]} s after the start of up, healthy+smoke {m[3]} s; "
                f"longest stretch without an answer: /health {outage['/health']['longest_gap_between_answers_s']} s, "
                f"/search/health {outage['/search/health']['longest_gap_between_answers_s']} s "
                f"(samples every 0.1 s; local Docker Desktop with stub upstreams, NOT a production figure)")
    rep.check("F4 deploy: only nginx recreated, healthy, smoke OK, upstreams and ignored files untouched, outage measured", deploy)

    def after_deploy():
        assert s.tracked("nginx/nginx.conf") and s.tracked("nginx/search_api.conf"), "the superseded files must stay until S2b"
        assert all(s.git("merge-base", "--is-ancestor", c, "HEAD", check=False).returncode == 0
                   for c in rh.g("rev-list", f"{prod}..{rh.lab.baseline_sha}").stdout.split())
        return "HEAD is the S2 commit; the superseded nginx files remain in git; the intermediate commits are in the history"
    rep.check("F5 after the deploy the tree is clean and holds the superseded files", after_deploy)

    s.git("merge", "--ff-only", "-q", rh.sha["l1"])        # a later, unrelated release lands on the server

    def revert_return():
        head0 = s.head()
        r, outage = rh.measure(s, lambda: s.sw("rollback", s2))
        ok(r, 0, "mode=revert", "S2_RESULT status=returned")
        assert s.git("rev-list", "--count", f"{head0}..HEAD").stdout.strip() == "1" and s.subject().startswith("Revert"), s.subject()
        assert s.git("rev-parse", "HEAD~1").stdout.strip() == head0, "the revert is not directly on top of the later release"
        assert "Later release L1" in (s.srv / "specs" / "active" / "spec.migration-registry.md").read_text(encoding="utf-8"), "L1 was lost"
        assert 'rehearsal.touch: "1"' in (s.srv / "docker-compose.yml").read_text(encoding="utf-8"), "L1's change to another service was lost"
        assert not s.tracked("nginx/conf/nginx.conf") and not (s.srv / "nginx" / "conf" / "nginx.conf").exists(), "S2 files linger"
        ga = (s.srv / ".gitattributes").read_text(encoding="utf-8")
        assert "nginx/conf/*" not in ga and "postgres/baseline/*.json text eol=lf" in ga, "the revert removed more or less than the S2 lines"
        assert (s.srv / "scripts" / "nginx_s2_switch.sh").exists() and (s.srv / "scripts" / "migrate.py").exists(), "tools or intermediate work lost"
        old_definition(s)
        s.smoke_ok()
        assert rh.stub_ids() == stubs0 and s.untracked_ignored() == ignored0
        no_reset(s)
        no_scratch_worktree(s)
        one_nginx_container(s)
        rh.data["timings"]["return_revert"] = {"total_s": round(r.seconds, 1)}
        rh.data["outage"]["return_revert"] = outage
        return (f"exit 0 in {r.seconds:.1f} s, mode=revert: one new commit directly on top of L1; L1, the intermediate commits and the tools commit stay; "
                f"old definition serving; longest stretch without an answer /health {outage['/health']['longest_gap_between_answers_s']} s, "
                f"/search/health {outage['/search/health']['longest_gap_between_answers_s']} s")
    rep.check("F6 return after a later release: `git revert` of the S2 commit only, old definition back, smoke OK", revert_return)

    def idempotent():
        head0 = s.head()
        r = s.sw("rollback", s2)
        ok(r, 0, "mode=already-absent")
        assert s.head() == head0
        old_definition(s)
        return "a second rollback changes nothing in git and leaves the old definition serving"
    rep.check("F7 a repeated rollback is harmless", idempotent)


def scn_refusals(rh):
    """every preflight refusal leaves no trace: HEAD, tree, container and state directory unchanged"""
    rep, s2 = rh.rep, rh.sha["s2"]
    s = rh.new_server("refuse")
    s.start()
    base = s.snap()
    stopped = rh.lab.name("not_running")
    rh.lab.containers.append(stopped)
    run(["docker", "create", "--name", stopped, *rh.lab.labels(), L.NGINX_IMAGE])
    args_image = s.write_args("args-image.txt", nginx_extra=["image: python:3.12-slim"])      # the S2-R7 reproduction: an override changes the image
    args_ports = s.write_args("args-ports.txt", keep_ports=True)                            # the effective model shows the ports

    def mutate_commit(c):
        (c.srv / "LOCAL.txt").write_text("a local commit\n")
        c.git("add", "LOCAL.txt")
        c.git("commit", "-q", "-m", "local commit")

    def mutate_dirty(c):
        with open(c.srv / "docker-compose.yml", "a", newline="\n") as f:
            f.write("# local edit\n")

    def mutate_collision(c):
        (c.srv / "nginx" / "conf").mkdir()
        (c.srv / "nginx" / "conf" / "nginx.conf").write_text("untracked\n")

    def stub_down(alias):
        return lambda c: run(["docker", "stop", rh.lab.stub_name(alias)])

    def stub_up(alias):
        def f(c):
            run(["docker", "start", rh.lab.stub_name(alias)])
            rh.lab.wait_stubs([alias])
        return f

    no_args = posix(rh.root / "no-such-args-file.txt")
    cases = [
        ("unknown commit", None, "0123456789abcdef0123456789abcdef01234567", {}, "is not a commit"),
        ("local commit: not a fast-forward", mutate_commit, s2, {}, "not an ancestor"),
        ("tracked file of the merge modified locally", mutate_dirty, s2, {}, "modified locally"),
        ("untracked file where S2 adds one", mutate_collision, s2, {}, "exists in the checkout"),
        ("nginx -t fails on the candidate", None, rh.sha["bad_conf"], {}, "nginx -t failed"),
        ("Compose changes another service too", None, rh.sha["bad_compose"], {}, "other than nginx"),
        ("an earlier commit changes the old nginx files", None, rh.sha["pre_touch"], {}, "commits before S2"),
        ("nginx container does not exist", None, s2, {"S2_CONTAINER": "b04s2_no_such_container"}, "not found"),
        ("nginx container exists but is not running", None, s2, {"S2_CONTAINER": stopped}, "is created"),
        ("the CURRENT nginx fails the smoke list: search upstream down", (stub_down("edu_ai_search"), stub_up("edu_ai_search")), s2, {},
         ("fails the smoke list", "/search/health")),
        ("the CURRENT nginx fails the smoke list: API upstream down", (stub_down("edu_ai_api"), stub_up("edu_ai_api")), s2, {},
         ("fails the smoke list", "/api/health")),
        ("image changed in the candidate's compose file", None, rh.sha["bad_image"], {}, ("differs beyond", "image")),
        ("image changed by an override (effective model, same arguments as up)", None, s2, {"S2_COMPOSE_ARGS_FILE": posix(args_image)},
         ("running container uses", "python:3.12-slim")),
        ("published port changed", None, rh.sha["bad_ports"], {"S2_COMPOSE_ARGS_FILE": posix(args_ports)}, ("differs beyond", "ports")),
        ("mount source is not nginx/conf (missing directory)", None, rh.sha["bad_mount"], {}, "mount source"),
        ("mount source is all of nginx/ (would expose the retired secrets files)", None, rh.sha["bad_mount_wide"], {}, "mount source"),
        ("healthcheck asks another path", None, rh.sha["bad_healthcheck"], {}, "healthcheck lacks"),
        ("the Compose arguments file does not exist", None, s2, {"S2_COMPOSE_ARGS_FILE": no_args}, "not readable"),
        ("`daemon off;` in nginx.conf: the image starts nginx with -g 'daemon off;', so the file would crash-loop the container",
         None, rh.sha["bad_daemon"], {}, ("nginx -t failed", "daemon")),
    ]
    for i, (name, mut, sha, env, expect) in enumerate(cases):
        def case(i=i, name=name, mut=mut, sha=sha, env=env, expect=expect):
            c = Server(rh, f"refuse{i}", container=s.container, args_file=s.args_file, with_variant=False)
            down, up = (mut if isinstance(mut, tuple) else (mut, None))
            if down:
                down(c)
            before = c.snap()
            try:
                r = c.sw("deploy", sha, **env)
            finally:
                if up:
                    up(c)
            ok(r, 2, *((expect,) if isinstance(expect, str) else expect))
            assert c.snap() == before, "the refused deploy changed the clone"
            assert not (c.state / "bundle").exists() and not (c.state / "phase").exists(), "the refused deploy got past preflight"
            assert s.snap() == base, "the running container or the main clone changed"
            return f"refused (exit 2): {expect if isinstance(expect, str) else ' + '.join(expect)}; HEAD, tree, reflog, container and state directory unchanged"
        rep.check(f"F8.{i + 1} refused before any change: {name}", case)

    def moved_tag():
        tag = f"{rh.lab.prefix}_probe:nginx"          # a private mutable tag, never one that another stack uses
        run(["docker", "tag", L.NGINX_IMAGE, tag])
        try:
            args = s.write_args("args-tag.txt", nginx_extra=[f"image: {tag}"])
            c = Server(rh, "refuse_tag", container=s.container, args_file=args, with_variant=False)
            ok(c.sw("preflight", s2), 0, "preflight OK")                       # positive control: the tag still is the running image
            run(["docker", "tag", "python:3.12-slim", tag])                      # the mutable tag moves to another image
            before = c.snap()
            r = c.sw("preflight", s2)
            ok(r, 2, "running container uses", "moved tag")
            assert c.snap() == before and s.snap() == base
        finally:
            run(["docker", "rmi", tag], check=False)
        return "a mutable tag that moved to another image after the container was created is refused (positive control passes before the move)"
    rep.check(f"F8.{len(cases) + 1} refused before any change: the image tag moved to another image", moved_tag)

    def api_smoke_alone():
        stub = rh.lab.stub_name("edu_ai_api")
        run(["docker", "stop", stub])
        try:
            r = s.sw("smoke", S2_SMOKE_DEADLINE=3)
            assert r.rc == 2 and "/api/health" in r.out, f"rc={r.rc}: {r.out[-300:]}"
        finally:
            run(["docker", "start", stub])
            rh.lab.wait_stubs(["edu_ai_api"])
        s.smoke_ok()
        return "with only the API upstream down the default smoke list fails on 80/api/health, and passes again when it is back"
    rep.check(f"F8.{len(cases) + 2} smoke: an unavailable API upstream fails the smoke list", api_smoke_alone)


def after_merge_failure(rh, key, branch, expect, env=None):
    rep, prod, bad = rh.rep, rh.prod, rh.sha[branch]
    s = rh.new_server(f"f_{key}")
    s.start()
    stubs0, ignored0, c0 = rh.stub_ids(), s.untracked_ignored(), s.cid()

    def returns():
        r = s.sw("deploy", bad, **(env or {}))
        ok(r, 4, expect, "S2_RESULT status=returned mode=revert")
        assert s.git("rev-parse", "HEAD~1").stdout.strip() == bad and s.subject().startswith("Revert"), s.subject()
        old_definition(s)
        s.smoke_ok()
        one_nginx_container(s)
        assert rh.stub_ids() == stubs0 and s.untracked_ignored() == ignored0
        no_reset(s)
        assert all(s.git("merge-base", "--is-ancestor", c, "HEAD", check=False).returncode == 0
                   for c in rh.g("rev-list", f"{prod}..{rh.lab.baseline_sha}").stdout.split())
        rh.data["timings"][f"failed_{key}"] = {"deploy_to_old_definition_serving_s": round(r.seconds, 1)}
        return f"exit 4 after {r.seconds:.1f} s: {expect!r}; returned by one revert commit on top of the failed S2; old definition serving, one nginx container, smoke OK"
    return s, returns


def scn_failures_after_merge(rh):
    """after the merge: failures at every stage return to the old definition automatically"""
    rep, s2 = rh.rep, rh.sha["s2"]
    for key, branch, expect, env, label in [
            ("health", "bad_health", "container is unhealthy", None, "the recreated nginx turns unhealthy"),
            ("smoke", "bad_smoke", "smoke failed after 10s", {"S2_SMOKE_DEADLINE": 10}, "healthy, but a proxied path fails the smoke list"),
            ("deadline", "s2", "docker compose up failed or exceeded 0.3s", {"S2_UP_DEADLINE": 0.3}, "`compose up` is killed by its deadline in the middle of the recreate")]:
        s, returns = after_merge_failure(rh, key, branch, expect, env)
        rep.check(f"F9.{key} {label}: automatic return", returns)

    def changed():
        """the checkout is changed between the validation and the recreate"""
        s = rh.new_server("f_changed")
        s.start()
        run_ = s.sw_async("deploy", s2, S2_TEST_SLEEP_AFTER="merged:25")
        run_.wait_for_phase("merged")
        with open(s.srv / "nginx" / "conf" / "search_api.conf", "a", newline="\n") as f:
            f.write("# changed after the validation\n")
        r = run_.wait()
        ok(r, 4, "the checkout differs from the validated candidate", "S2_RESULT status=returned")
        old_definition(s)
        s.smoke_ok()
        one_nginx_container(s)
        no_reset(s)
        no_scratch_worktree(s)
        return ("a file of the candidate was edited in the window after the merge: the script noticed before `up`, returned (the local edit blocks the "
                "revert's fast-forward, so the nginx block was put back instead) and the old definition serves")
    rep.check("F9.changed the checkout changes between validation and recreate: automatic return", changed)

    def image_id(ref):
        return run(["docker", "image", "inspect", "--format", "{{.Id}}", ref]).stdout.strip()

    def evidence(s):
        out = {}
        path = s.state / "images.env"
        assert path.exists(), "the deploy saved no image evidence"
        for line in path.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            out[k] = v
        return out

    def image_moved_and_stays():
        """the image tag moves after the merge and is NEVER moved back: nothing outside the script helps the return"""
        tag = f"{rh.lab.prefix}_move:nginx"
        run(["docker", "tag", L.NGINX_IMAGE, tag])
        nginx_id, python_id = image_id(L.NGINX_IMAGE), image_id("python:3.12-slim")
        try:
            s = rh.new_server("f_moved")
            s.args_file = s.write_args("args-moved.txt", nginx_extra=[f"image: {tag}"])
            s.start()
            ingress_before = s.sw("smoke")
            assert ingress_before.rc == 0
            run_ = s.sw_async("deploy", s2, S2_TEST_SLEEP_AFTER="merged:15")
            run_.wait_for_phase("merged")
            run(["docker", "tag", "python:3.12-slim", tag])           # the tag moves to another image and stays there until the very end
            r = run_.wait()
            ok(r, 4, "no longer is the validated image", "S2_RESULT status=returned")
            assert image_id(tag) == python_id, "the test must leave the tag moved"
            old_definition(s)
            assert s.inspect()["Image"] == nginx_id, f"the return runs {s.inspect()['Image']}, not the captured original image {nginx_id}"
            ev = evidence(s)
            assert ev["ORIGINAL_IMAGE_ID"] == nginx_id and ev["VALIDATED_IMAGE_ID"] == nginx_id, ev
            s.smoke_ok()
            one_nginx_container(s)
            # a LATER process (the evidence is all it has), the tag still moved
            r2 = s.sw("rollback", s2)
            ok(r2, 0, "from the saved evidence", "mode=already-absent")
            assert s.inspect()["Image"] == nginx_id and image_id(tag) == python_id
            s.smoke_ok()
            tags = run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]).stdout.split()
            assert f"{rh.lab.prefix}_evidence/nginx:original-{nginx_id.removeprefix('sha256:')}" in tags, "the private tag that keeps the original image alive is missing"
        finally:
            run(["docker", "rmi", tag], check=False)
        return (f"the tag moved to another image ({python_id[7:19]}) after the validation and stayed there to the end: the script noticed before `up`, "
                f"returned, and the container runs the captured original image ({nginx_id[7:19]}) by ID; /health and the smoke list pass; "
                "a second process (`rollback`, evidence only) changed nothing")
    rep.check("F9.image_moved the tag moves after the merge and stays moved: the return runs the captured image ID", image_moved_and_stays)

    def moved_tag_before_late_rollback():
        """the deploy succeeds, the tag is moved afterwards (a pull, a retag), and a NEW process rolls back"""
        tag = f"{rh.lab.prefix}_late:nginx"
        run(["docker", "tag", L.NGINX_IMAGE, tag])
        nginx_id, python_id = image_id(L.NGINX_IMAGE), image_id("python:3.12-slim")
        try:
            s = rh.new_server("f_late_moved")
            s.args_file = s.write_args("args-late.txt", nginx_extra=[f"image: {tag}"])
            s.start()
            ok(s.sw("deploy", s2), 0, "S2_RESULT status=ok")
            assert s.inspect()["Image"] == nginx_id
            run(["docker", "tag", "python:3.12-slim", tag])
            r = s.sw("rollback", s2)                                  # a new process, hours later
            ok(r, 0, "from the saved evidence", "mode=revert")
            assert image_id(tag) == python_id
            old_definition(s)
            assert s.inspect()["Image"] == nginx_id, "the late return runs the moved tag instead of the captured original image"
            s.smoke_ok()
            one_nginx_container(s)
        finally:
            run(["docker", "rmi", tag], check=False)
        return ("deploy succeeded on the validated image ID; the tag was moved afterwards; a manual `rollback` in a new process returned by revert and "
                "recreated nginx from the captured original image ID, not from the moved tag; /health and the smoke list pass")
    rep.check("F9.late_rollback the tag moves after a successful deploy: a manual rollback in a new process restores the original image", moved_tag_before_late_rollback)

    def crash_loop():
        """the new container really crash-loops (nginx exits at every start): the script sees the restarts and returns"""
        s = rh.new_server("f_crash")
        s.start()
        run_ = s.sw_async("deploy", s2, S2_TEST_SLEEP_AFTER="recreated:15")
        run_.wait_for_phase("recreated")
        assert s.mount_dests() == NEW_MOUNTS, "the container in the window must be the S2 one"
        conf = s.srv / "nginx" / "conf" / "nginx.conf"
        # the image starts nginx as `nginx -g 'daemon off;'`; a second `daemon` directive in the file makes every start fail
        conf.write_text("daemon off;\n" + conf.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        run(["docker", "restart", "-t", "2", s.container])
        deadline, restarts, logs = time.time() + 30, 0, ""
        while time.time() < deadline:
            restarts = int(run(["docker", "inspect", "--format", "{{.RestartCount}}", s.container]).stdout.strip() or 0)
            logs = run(["docker", "logs", s.container], check=False)
            logs = (logs.stdout or "") + (logs.stderr or "")
            if restarts >= 2 and '"daemon" directive is duplicate' in logs:
                break
            time.sleep(1)
        assert restarts >= 2 and '"daemon" directive is duplicate' in logs, f"no real crash loop: RestartCount={restarts}, logs: {logs[-300:]}"
        r = run_.wait()
        ok(r, 4, "crash loop", "S2_RESULT status=returned")
        old_definition(s)
        s.smoke_ok()
        one_nginx_container(s)
        no_reset(s)
        no_scratch_worktree(s)
        return (f"the S2 container started, then crash-looped for real (RestartCount {restarts}, nginx: `\"daemon\" directive is duplicate` at every start): "
                "the script saw the restarts, refused to call it healthy, returned (exit 4) and the old definition serves")
    rep.check("F9.crash_loop a new container that really crash-loops: detected by RestartCount, automatic return", crash_loop)

    def no_auto():
        s = rh.new_server("f_manual")
        s.start()
        bad = rh.sha["bad_smoke"]
        r = s.sw("deploy", bad, S2_AUTO_ROLLBACK=0, S2_SMOKE_DEADLINE=10)
        ok(r, 3, "left as is", f"rollback {bad}")
        assert s.head() == bad and s.mount_dests() == NEW_MOUNTS, "state after the failed deploy is not the merged S2"
        r = s.sw("rollback", bad)
        ok(r, 0, "mode=revert")
        old_definition(s)
        s.smoke_ok()
        return "with S2_AUTO_ROLLBACK=0 the failed deploy stops (exit 3) and names the command; that command returns by revert (exit 0), old definition serving"
    rep.check("F9.manual the same failure with the automatic return off, then the manual rollback", no_auto)


def scn_interruptions(rh):
    """the process stops after the merge or after the recreate: the old container still restarts, a re-run converges"""
    rep, s2 = rh.rep, rh.sha["s2"]

    def stop_after_merge():
        s = rh.new_server("i_merged")
        s.start()
        c0 = s.cid()
        r = s.sw("deploy", s2, S2_TEST_STOP_AFTER="merged")
        ok(r, 99, "TEST HOOK")
        assert s.head() == s2 and s.cid() == c0, "after the merge the old container must be untouched"
        old_definition(s)
        s.smoke_ok()
        # the S2-merged tree must still hold every file the OLD container mounts, or a restart would make directories of them
        run(["docker", "restart", "-t", "2", s.container])
        rh.su.wait_serving(s.variant)
        old_definition(s)
        assert s.mounts_are_files(), "an old mount source is not a file any more"
        s.smoke_ok()
        r = s.sw("deploy", s2)
        ok(r, 0, "S2_RESULT status=ok")
        new_definition(s)
        return ("stopped between merge and recreate (exit 99): the old container keeps serving and RESTARTS cleanly over the merged tree "
                "(its two file mounts still exist); a re-run of deploy then completes (exit 0)")
    rep.check("F10.1 interruption after the merge, before the recreate", stop_after_merge)

    def stop_after_recreate():
        s = rh.new_server("i_recreated")
        s.start()
        r = s.sw("deploy", s2, S2_TEST_STOP_AFTER="recreated")
        ok(r, 99, "TEST HOOK")
        assert s.head() == s2 and s.mount_dests() == NEW_MOUNTS
        r = s.sw("deploy", s2)
        ok(r, 0, "S2_RESULT status=ok")
        new_definition(s)
        s.smoke_ok()
        s.release()                      # one nginx container per project at a time
        s2b = rh.new_server("i_recreated_rb")
        s2b.start()
        ok(s2b.sw("deploy", s2, S2_TEST_STOP_AFTER="recreated"), 99)
        r = s2b.sw("rollback", s2)
        ok(r, 0, "mode=revert")
        old_definition(s2b)
        s2b.smoke_ok()
        return "stopped after the recreate (exit 99): a re-run of deploy converges (exit 0); a rollback from that state returns by revert (exit 0)"
    rep.check("F10.2 interruption after the recreate, before health and smoke", stop_after_recreate)


def scn_return_fallbacks(rh):
    """the return when git cannot revert: conflict, local changes, retired files, a missing upstream"""
    rep, prod, s2 = rh.rep, rh.prod, rh.sha["s2"]

    def deployed(name):
        s = rh.new_server(name)
        s.start()
        ok(s.sw("deploy", s2), 0, "S2_RESULT status=ok")
        return s

    def nginx_block(text):
        m = re.search(r"^  nginx:\s*\n((?:    .*\n|\s*\n)*?)(?=^  \S|\Z)", text.replace("\r\n", "\n"), re.M)
        assert m, "no nginx block"
        return m.group(0)

    def is_old_block(s):
        text = (s.srv / "docker-compose.yml").read_text(encoding="utf-8")
        old = rh.g("show", f"{prod}:docker-compose.yml").stdout
        return nginx_block(text).strip() == nginx_block(old).strip()

    def conflict():
        s = deployed("r_conflict")
        s.git("merge", "--ff-only", "-q", rh.sha["l2"])
        head0 = s.head()
        r = s.sw("rollback", s2)
        ok(r, 0, "git revert not possible", "conflicting files: docker-compose.yml", "mode=restore")
        assert s.head() == head0, "a failed revert must leave no commit"
        assert [x for x in s.status() if not x.startswith(("!!", "??"))] == [" M docker-compose.yml"], s.status()
        assert is_old_block(s), "the nginx block is not the pre-S2 one"
        assert 'rehearsal.touch: "1"' in (s.srv / "docker-compose.yml").read_text(encoding="utf-8"), "another service's later change was lost"
        saved = (s.state / "overwritten" / "docker-compose.yml").read_text(encoding="utf-8")
        assert "interval: 10s" in saved, "the overwritten file was not saved"
        old_definition(s)
        s.smoke_ok()
        one_nginx_container(s)
        no_scratch_worktree(s)
        no_reset(s)
        return ("git revert conflicted on docker-compose.yml (L2 tuned the S2 healthcheck): `revert --abort` left no trace, then ONLY the nginx service block was "
                "put back (hash-checked against the pre-S2 service), L2's change to another service survived, the overwritten file is saved, "
                "tree shows one modified file, old definition serving")
    rep.check("F11.1 conflict state: revert impossible, nginx block restored, rest of the file kept", conflict)

    def dirty():
        s = deployed("r_dirty")
        with open(s.srv / "docker-compose.yml", "a", newline="\n") as f:
            f.write("# local edit on the server\n")
        head0 = s.head()
        r = s.sw("rollback", s2)
        ok(r, 0, "could not be fast-forwarded", "mode=restore")
        assert s.head() == head0 and "# local edit on the server" in (s.srv / "docker-compose.yml").read_text(encoding="utf-8"), "the local edit was lost"
        assert is_old_block(s)
        old_definition(s)
        s.smoke_ok()
        no_scratch_worktree(s)
        no_reset(s)
        return ("uncommitted local edit in docker-compose.yml: the revert, made in a scratch worktree, cannot be fast-forwarded over the local edit; "
                "the nginx block is restored and the local edit is kept")
    rep.check("F11.2 conflict state: local uncommitted change in the file", dirty)

    def late():
        s = deployed("r_late")
        s.git("merge", "--ff-only", "-q", rh.sha["l3"])
        assert not s.tracked("nginx/nginx.conf")
        r = s.sw("rollback", s2)
        ok(r, 0, "mode=revert", "restored nginx/nginx.conf", "restored nginx/search_api.conf")
        old_definition(s)
        assert s.mounts_are_files(), "an old mount source is not a file"
        s.smoke_ok()
        untracked = s.git("ls-files", "--others", "--exclude-standard").stdout.split()
        assert "nginx/nginx.conf" in untracked and "nginx/search_api.conf" in untracked, f"the restored files are not untracked files: {untracked}"
        return ("late return after S2b removed the old files: revert succeeds, the two mount sources are restored as untracked files "
                "(not directories), old definition serving")
    rep.check("F11.3 late return after the superseded files were retired (S2b)", late)

    def missing_upstream():
        s = deployed("r_upstream")
        stub = rh.lab.stub_name("edu_ai_qa_admin")
        run(["docker", "stop", stub])
        try:
            r = s.sw("rollback", s2, S2_HEALTH_DEADLINE=25, S2_SMOKE_DEADLINE=10)
            ok(r, 5, "THE RETURN FAILED")
            state = s.cinfo()
            assert s.sw("smoke", S2_SMOKE_DEADLINE=5).rc != 0, "ingress should not answer in this state"
        finally:
            run(["docker", "start", stub])
            rh.lab.wait_stubs(["edu_ai_qa_admin"])
        r = s.sw("rollback", s2)
        ok(r, 0, "mode=already-absent")
        old_definition(s)
        s.smoke_ok()
        return (f"the pre-S2 nginx cannot start while an upstream name does not resolve: the return reported failure (exit 5, state {state.split(' ')[-1]}); "
                f"after the upstream was back the same command finished it (exit 0). This is the old definition's own start-up dependency, not a new one")
    rep.check("F11.4 the return when an upstream is missing: reported, and finishes once it is back", missing_upstream)


def deployed_server(rh, name, sha):
    s = rh.new_server(name)
    s.start()
    ok(s.sw("deploy", sha), 0, "S2_RESULT status=ok")
    return s


def compose_block(text):
    """The nginx service block of a compose file as a list of lines; blank lines inside count, trailing ones do not."""
    lines = text.replace("\r\n", "\n").split("\n")
    i = next(k for k, line in enumerate(lines) if line.rstrip() == "  nginx:")
    out = [lines[i]]
    for line in lines[i + 1:]:
        if line.startswith("    ") or not line.strip():
            out.append(line)
        else:
            break
    while out and not out[-1].strip():
        out.pop()
    return out


def scn_splice_guard(rh):
    """the fallback that puts the nginx block back: blank lines inside blocks, and a guard that refuses to write a wrong block"""
    rep, prod, s2 = rh.rep, rh.prod, rh.sha["s2"]
    old_text = rh.g("show", f"{prod}:docker-compose.yml").stdout
    old_block = compose_block(old_text)

    def conflict_server(name):
        s = deployed_server(rh, name, s2)
        s.git("merge", "--ff-only", "-q", rh.sha["l2"])          # L2 conflicts with a revert, so the return takes the fallback
        return s

    def edit_bundle(s, fn):
        path = s.state / "bundle" / "docker-compose.yml"
        text = path.read_text(encoding="utf-8")
        new = fn(text)
        assert new != text
        path.write_text(new, encoding="utf-8", newline="\n")
        return text

    image_line = "    image: nginx:1.27-alpine\n"

    def blank_inside_current_and_bundle():
        s = conflict_server("g_blank")
        current = (s.srv / "docker-compose.yml").read_text(encoding="utf-8")
        assert "\n\n    restart: unless-stopped" in current, "L2 should have put a blank line inside the nginx block of the current file"
        edit_bundle(s, lambda t: t.replace(image_line, image_line + "\n", 1))        # and one inside the pre-S2 block
        r = s.sw("rollback", s2)
        ok(r, 0, "git revert not possible", "mode=restore")
        new = (s.srv / "docker-compose.yml").read_text(encoding="utf-8")
        got = compose_block(new)
        assert [x for x in got if x.strip()] == [x for x in old_block if x.strip()], "the restored nginx block is not the pre-S2 one"
        assert "" in got[1:], "the blank line inside the pre-S2 block was not carried over"
        assert 'rehearsal.touch: "1"' in new and "restart: unless-stopped" in new
        old_definition(s)
        s.smoke_ok()
        return ("a blank line inside the nginx block of the file on disk (L2) and one inside the pre-S2 block: the whole block was replaced, nothing was left "
                "behind, the result hashes to the pre-S2 nginx; the old definition serves")
    rep.check("F12.1 fallback with blank lines inside the YAML block (valid input is handled)", blank_inside_current_and_bundle)

    def truncated_block_is_refused():
        s = conflict_server("g_truncate")
        before = (s.srv / "docker-compose.yml").read_bytes()
        original_bundle = edit_bundle(s, lambda t: t.replace(image_line, image_line + "  # a comment at the indentation of the service names\n", 1))
        head0 = s.head()
        r = s.sw("rollback", s2)
        ok(r, 5, "differs from the pre-S2 one; nothing was changed", "THE RETURN FAILED")
        assert (s.srv / "docker-compose.yml").read_bytes() == before, "the refused splice changed docker-compose.yml"
        assert not (s.state / "overwritten" / "docker-compose.yml").exists(), "a file was saved as overwritten although nothing was written"
        assert s.head() == head0
        new_definition(s)                                            # the S2 container was not touched either
        no_reset(s)
        no_scratch_worktree(s)
        # the way out: the cause is removed, the same command finishes the return
        (s.state / "bundle" / "docker-compose.yml").write_text(original_bundle, encoding="utf-8", newline="\n")
        r = s.sw("rollback", s2)
        ok(r, 0, "mode=restore")
        old_definition(s)
        s.smoke_ok()
        return ("an extraction that cuts the pre-S2 block short (a comment at 2-space indentation inside it) produces a model whose nginx hash differs: "
                "the guard refused, docker-compose.yml was byte-identical afterwards, nothing was saved as overwritten, ingress stayed on S2 (exit 5); "
                "after the cause was removed the same command finished the return")
    rep.check("F12.2 fallback guard: a wrongly extracted block is refused, the original file is not overwritten", truncated_block_is_refused)


def bash_unit(rh, code, *, cwd=None, env=None):
    script = rh.root / "unit-switch.sh"
    if not script.exists():
        script.write_bytes(rh.script_bytes)
    e = L._env()
    e.update({"S2_SOURCE_ONLY": "1", "S2_STATE_DIR": posix(rh.root / "unit-state"), })
    e.update(env or {})
    p = subprocess.run([rh.bash, "-c", f"source {posix(script)}; {code}"], cwd=str(cwd or rh.root), env=e, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def scn_script_units(rh):
    """the guard primitives of the script, with malformed inputs and a fake docker (no containers involved)"""
    rep = rh.rep
    work = rh.root / "units"
    work.mkdir()

    def put(name, text):
        (work / name).write_text(text, encoding="utf-8", newline="\n")
        return posix(work / name)

    def svc_hash_guard():
        good = put("h_good", "bifrost a1\nnginx 4c14b73e\nnginx2 b2\n")
        rc, out, _ = bash_unit(rh, f"svc_hash {good} nginx")
        assert rc == 0 and out == "4c14b73e", (rc, out)
        bad = {"empty file": "", "no nginx line": "bifrost a1\nnginx2 b2\nxnginx c3\n", "nginx without a hash": "nginx\n",
               "nginx with a blank hash": "nginx \n", "two nginx lines": "nginx 1\nnginx 2\n", "hash in the first column": "4c14 nginx\n",
               "the shape that the old guard grepped for": "x nginx deadbeef\n"}
        for label, text in bad.items():
            f = put("h_bad", text)
            rc, out, _ = bash_unit(rh, f"svc_hash {f} nginx")
            assert rc != 0 and out == "", f"{label}: svc_hash returned rc={rc} out={out!r}"
        return f"svc_hash accepts exactly one non-empty `nginx <hash>` line and refuses {len(bad)} malformed shapes (including the empty-versus-empty case)"
    rep.check("F13.1 hash guard: exactly one non-empty nginx hash, found by exact service name", svc_hash_guard)

    def eff_hash_guard():
        f = put("compose.yml", "services: {}\n")
        out_file = posix(work / "eff.out")
        for label, fake in (("compose fails", "docker() { return 1; }"), ("compose prints nothing", "docker() { return 0; }")):
            rc, _, _ = bash_unit(rh, f"{fake}; eff_hash_file {f} {out_file}")
            assert rc != 0, f"{label}: eff_hash_file reported success"
        rc, _, _ = bash_unit(rh, f"docker() {{ printf 'nginx b\\nbifrost a\\n'; }}; eff_hash_file {f} {out_file}")
        assert rc == 0 and (work / "eff.out").read_text(encoding="utf-8") == "bifrost a\nnginx b\n"
        return "a failing Compose command, or one that prints nothing, is a failure; real output is accepted and sorted"
    rep.check("F13.2 hash guard: the success of the Compose command is required", eff_hash_guard)

    def wait_ready_guard():
        fake = ('docker() { case "$*" in *RestartCount*) echo "$RC" ;; *State.Status*) echo "$ST" ;; *) return 1 ;; esac; }')
        cases = [("running|healthy", 0, 0, "healthy"), ("running|healthy", 3, 1, "crash loop"), ("running|unhealthy", 0, 1, "unhealthy"),
                 ("running|none", 0, 1, "no healthcheck"), ("exited|none", 0, 1, "state exited"), ("restarting|unhealthy", 0, 1, "restarting"),
                 ("running|starting", 0, 1, "not ready within")]
        for st, restarts, want_rc, fragment in cases:
            rc, out, err = bash_unit(rh, f"{fake}; HEALTH_DEADLINE=2; wait_ready 1", env={"ST": st, "RC": str(restarts), "S2_STATE_DIR": posix(work / "wr")})
            assert rc == want_rc, f"{st} restarts={restarts}: rc={rc}, wanted {want_rc}: {out} {err}"
            if want_rc:
                assert fragment in out, f"{st}: {fragment!r} not in {out!r}"
        # during the RETURN (wait_ready 0) a restarting container may still be coming back: it is waited for until the deadline, not failed at once
        rc, out, err = bash_unit(rh, f"{fake}; HEALTH_DEADLINE=2; t0=$SECONDS; wait_ready 0; echo \"rc=$? waited=$((SECONDS - t0))\"",
                                 env={"ST": "restarting|none", "RC": "7", "S2_STATE_DIR": posix(work / "wr")})
        assert ("rc=1 waited=2" in out or "rc=1 waited=3" in out) and "not ready within" in out, out      # the deadline is 2 s; the loop ticks once a second
        return ("a new container passes only when healthy with RestartCount 0; crash loop, unhealthy, no healthcheck, exited, restarting and `still starting at the deadline` "
                "all fail; a container that is restarting during the return is waited for until the deadline")
    rep.check("F13.3 health wait: crash loops and every non-healthy state are failures", wait_ready_guard)

    def args_file_guard():
        bad = {"missing": None, "empty": "", "no marker": "-p\nx\n-f\ndocker-compose.yml\n", "two markers": "-f\n@BASE@\n-f\n@BASE@\n"}
        for label, text in bad.items():
            path = work / f"args_{label.replace(' ', '_')}.txt"
            if text is not None:
                path.write_text(text, encoding="utf-8", newline="\n")
            rc, out, _ = bash_unit(rh, "( load_compose_args )", env={"S2_COMPOSE_ARGS_FILE": posix(path)})
            assert rc == 2, f"{label}: rc={rc} {out}"
        good = work / "args_good.txt"
        good.write_text("-p\nx\n-f\n@BASE@\n-f\noverride.yaml\n", encoding="utf-8", newline="\n")
        rc, out, _ = bash_unit(rh, "load_compose_args; build_args -; printf '%s ' \"${A[@]}\"", env={"S2_COMPOSE_ARGS_FILE": posix(good)})
        assert rc == 0 and out.strip() == "-p x -f - -f override.yaml", (rc, out)
        return "a missing, empty or marker-less arguments file is refused (exit 2) instead of silently using defaults; the marker becomes `-` for validation"
    rep.check("F13.4 Compose arguments: a missing/empty/unmarked file is refused, the base file marker is substituted", args_file_guard)

    def default_mode_guard():
        for label, env_text, env_var, want in (("nothing pinned", "", None, 2), ("pinned in .env", "COMPOSE_FILE=docker-compose.yml\n", None, 0),
                                              ("pinned in the environment", "", "docker-compose.yml", 0),
                                              ("an override file is loaded too", "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml\n", None, 2)):
            d = work / ("mode_" + label.replace(" ", "_"))
            d.mkdir()
            (d / ".env").write_text(env_text, encoding="utf-8", newline="\n")
            e = {"COMPOSE_FILE": env_var} if env_var else {}
            rc, out, _ = bash_unit(rh, "( check_default_mode )", cwd=d, env=e)
            assert rc == want, f"{label}: rc={rc} {out}"
        return "without an arguments file the checkout must pin COMPOSE_FILE=docker-compose.yml (an auto-loaded override or an unpinned default is refused)"
    rep.check("F13.5 default mode: COMPOSE_FILE must be pinned to docker-compose.yml", default_mode_guard)

    def return_pin_selection():
        fake = ('docker() { case "$*" in "image inspect"*) [ "${HAVE_IMG:-1}" = 1 ]; return ;; "inspect --format {{.Image}}"*) '
                '[ -n "${RUNNING:-}" ] && echo "$RUNNING"; [ -n "${RUNNING:-}" ]; return ;; esac; return 1; }')
        orig, running = "sha256:" + "a" * 64, "sha256:" + "c" * 64
        d = work / "pin"
        d.mkdir()

        def case(label, evidence, *, cand="cand1", have=1, running_id="", want_rc=0, want_pin=None, fragment=""):
            sd = d / label.replace(" ", "_").replace(":", "").replace("'", "").replace(",", "")
            sd.mkdir()
            if evidence is not None:
                (sd / "images.env").write_text(evidence, encoding="utf-8", newline="\n")
            rc, out, err = bash_unit(rh, f"{fake}; CAND={cand}; load_return_pin; echo \"rc=$? pin=${{PIN_ID:-none}}\"",
                                     env={"S2_STATE_DIR": posix(sd), "HAVE_IMG": str(have), "RUNNING": running_id})
            assert f"rc={want_rc} pin={want_pin or 'none'}" in out, f"{label}: {out} {err}"
            assert fragment in out, f"{label}: {fragment!r} not in {out!r}"
            if want_pin:
                assert (sd / "image-pin.yaml").read_text(encoding="utf-8") == f"services:\n  nginx:\n    image: {want_pin}\n", label
            else:
                assert not (sd / "image-pin.yaml").exists(), label

        mine = (f"ORIGINAL_IMAGE_ID={orig}\nVALIDATED_IMAGE_ID={orig}\nIMAGE_REF=nginx:1.27-alpine\nCANDIDATE=cand1\nPRE=p\nCONTAINER=c\n")
        case("evidence of this candidate wins over the running container", mine, running_id=running, want_pin=orig, fragment="from the saved evidence")
        case("evidence of another candidate is not used", mine.replace("cand1", "cand9"), running_id=running, want_pin=running,
             fragment="no evidence of a deploy of this candidate")
        case("no evidence: the running container's image", None, running_id=running, want_pin=running, fragment="running container")
        case("no evidence and no container: a warning, no pin", None, running_id="", want_rc=0, want_pin=None, fragment="WARNING")
        case("the original image is gone", mine, running_id=running, have=0, want_rc=1, want_pin=None, fragment="not present locally")
        # the arguments of `up`: the pin is the LAST file, in both modes; without a pin nothing is added
        fake_up = 'timeout() { shift; "$@"; }; docker() { printf "%s " "$@"; echo; }'
        for label, args_file, extra, want in (
                ("arguments file", "-p\nx\n-f\n@BASE@\n-f\nover.yaml\n", "write_pin sha256:abc", "-f over.yaml -f "),
                ("default mode", None, "write_pin sha256:abc", "compose -f "),
                ("no pin", "-p\nx\n-f\n@BASE@\n", ":", "compose -p x -f ")):
            key = "up_" + label.replace(" ", "_")
            env = {"S2_STATE_DIR": posix(work / key)}
            if args_file is not None:
                af = work / (key + ".args")
                af.write_text(args_file, encoding="utf-8", newline="\n")
                env["S2_COMPOSE_ARGS_FILE"] = posix(af)
            rc, out, err = bash_unit(rh, f"{fake_up}; load_compose_args; {extra}; compose_up 5 up -d --no-deps --pull never nginx", env=env)
            assert rc == 0 and want in out, f"{label}: rc={rc} {out} {err}"
            if "write_pin" in extra:
                assert out.split("up -d")[0].rstrip().endswith("image-pin.yaml"), f"{label}: the pin is not the last file: {out}"
            else:
                assert "image-pin" not in out, f"{label}: {out}"
        return ("the return pin comes from the evidence of THIS candidate (else from the running container, else nothing, with a warning), a missing original "
                "image fails the return; `up` gets the pin as its last -f in both modes and nothing when there is none")
    rep.check("F13.8 image pin: evidence-based selection and the arguments of `up`", return_pin_selection)

    def belongs_guard():
        fake = ('docker() { case "$*" in "inspect --format "*) [ "${LBLRC:-0}" = 1 ] && return 1; echo "$LBL"; return 0 ;; esac; return 1; }')
        cases = [("project and service match", "p1|nginx", 0, "p1", 0), ("another project, service nginx", "p2|nginx", 0, "p1", 1),
                 ("this project, another service", "p1|web", 0, "p1", 1), ("another project and service", "p2|web", 0, "p1", 1),
                 ("no labels", "|", 0, "p1", 1), ("a prefix of the project is not the project", "p10|nginx", 0, "p1", 1),
                 ("inspect fails (the container is gone or unreadable)", "", 1, "p1", 1), ("an empty expected project never matches", "|nginx", 0, "", 1)]
        for label, lbl, lbl_rc, project, want in cases:
            rc, out, err = bash_unit(rh, f"{fake}; container_belongs somecontainer '{project}'; echo \"rc=$?\"", env={"LBL": lbl, "LBLRC": str(lbl_rc)})
            assert f"rc={want}" in out, f"{label}: {out} {err}"
        # the expected project is the name of the effective model, read from Compose and never from a container
        fake_cfg = 'docker() { case "$1" in compose) printf "name: lab_p_1\\nservices:\\n  nginx: {}\\n" ;; esac; }'
        rc, out, err = bash_unit(rh, f"{fake_cfg}; expected_project")
        assert out.strip() == "lab_p_1", (rc, out, err)
        return ("a container is this project's nginx only when BOTH labels match (project and service nginx); a prefix, another project, another service, no labels, "
                "an unreadable container and an empty project all fail; the expected project comes from the effective model")
    rep.check("F13.9 label check: only project AND service nginx make a container removable", belongs_guard)

    def extract_block_shapes():
        sample = ("services:\n  edu_ai_api:\n    image: x\n\n  nginx:\n    image: n\n\n    ports:\n      - 80\n    restart: always\n\n"
                  "  # comment of the next service\n  other:\n    image: y\n")
        eof = "services:\n  nginx:\n    image: n\n    restart: always\n\n"
        cut = "services:\n  nginx:\n    image: n\n  # comment at the indentation of a service\n    ports:\n      - 80\n"
        want = {"sample": (sample, "  nginx:\n    image: n\n\n    ports:\n      - 80\n    restart: always\n"),
                "block at the end of the file": (eof, "  nginx:\n    image: n\n    restart: always\n"),
                "2-space comment inside the block (not supported: the block is cut, the hash guard refuses the result)": (cut, "  nginx:\n    image: n\n")}
        for label, (text, expected) in want.items():
            f = put("block.yml", text)
            rc, out, _ = bash_unit(rh, f"extract_block {f}")
            assert rc == 0 and out == expected, f"{label}: {out!r}"
        return "blank lines inside the block are kept, trailing blanks and the next service's comment are not; the one unsupported shape is cut (and then refused by the guard, F12.2)"
    rep.check("F13.6 block extraction: blank lines inside, end of file, and the unsupported shape", extract_block_shapes)


def scn_signals(rh):
    """real INT/HUP/TERM delivered to the running script: ignored after the merge and during the return, fatal and harmless before"""
    rep, s2 = rh.rep, rh.sha["s2"]
    platform = subprocess.run([rh.bash, "-c", "uname -sr; echo $BASH_VERSION"], capture_output=True, text=True).stdout.split("\n")
    rh.data["signals_platform"] = {"python": sys.platform, "uname": platform[0], "bash": platform[1] if len(platform) > 1 else ""}
    where = f"{platform[0]} / bash {platform[1] if len(platform) > 1 else '?'}"

    def after_merge_and_recreate():
        s = rh.new_server("sig_deploy")
        s.start()
        run_ = s.sw_async("deploy", s2, S2_TEST_SLEEP_AFTER="merged:12,recreated:12")
        run_.wait_for_phase("merged")
        run_.signal("INT", "HUP", "TERM")
        run_.wait_for_phase("recreated")
        run_.signal("INT", "HUP", "TERM")
        r = run_.wait()
        ok(r, 0, "S2_RESULT status=ok")
        assert s.head() == s2
        new_definition(s)
        s.smoke_ok()
        return f"INT, HUP and TERM delivered after the merge and again after the recreate did not stop the deploy (exit 0, healthy, smoke OK) [{where}]"
    rep.check("F14.1 real signals after the merge and after the recreate are ignored; the deploy completes", after_merge_and_recreate)

    def during_return():
        s = deployed_server(rh, "sig_return", s2)
        run_ = s.sw_async("rollback", s2, S2_TEST_SLEEP_AFTER="returning:12")
        run_.wait_for_phase("returning")
        run_.signal("INT", "HUP", "TERM")
        r = run_.wait()
        ok(r, 0, "mode=revert", "S2_RESULT status=returned")
        old_definition(s)
        s.smoke_ok()
        no_scratch_worktree(s)
        return f"INT, HUP and TERM delivered at the start of the return did not stop it (exit 0, revert, old definition serving) [{where}]"
    rep.check("F14.2 real signals during the return are ignored; the return completes", during_return)

    def before_the_merge():
        s = rh.new_server("sig_pre")
        s.start()
        before = s.snap()
        run_ = s.sw_async("deploy", s2, S2_TEST_SLEEP_AFTER="preflight:30")
        run_.wait_for_phase("preflight")
        t_sig = time.time()
        run_.signal("TERM")
        while run_.proc.poll() is None and time.time() - t_sig < 10:      # the script itself must end at once; the `sleep` of the test hook may keep the pipe open
            time.sleep(0.2)
        assert run_.proc.poll() is not None, "the script was still running 10 s after TERM"
        r = run_.wait(timeout=60)
        assert r.rc not in (0, 2, 99), f"the script was not stopped by TERM before the merge (rc={r.rc})"
        assert s.snap() == before, "a script stopped before the merge changed something"
        assert not (s.state / "bundle").exists()
        r = s.sw("deploy", s2)
        ok(r, 0, "S2_RESULT status=ok")
        new_definition(s)
        return f"TERM before the merge stops the script (exit {run_.proc.returncode}) with HEAD, tree and container unchanged; a new deploy then succeeds [{where}]"
    rep.check("F14.3 a real TERM before the merge stops the script and changes nothing", before_the_merge)


def scn_name_collision(rh):
    """a container that holds the name of nginx is removed only when its labels prove it is THIS project's nginx"""
    rep, s2 = rh.rep, rh.sha["s2"]

    def holder(s, labels):
        """a container of the lab that takes the name of nginx, with the given Compose labels (and no network)"""
        label_args = [a for k, v in labels.items() for a in ("--label", f"{k}={v}")]
        run(["docker", "run", "-d", "--name", s.container, *rh.lab.labels(), *label_args, "--network", "none",
             "--entrypoint", "sleep", L.NGINX_IMAGE, "infinity"])
        return run(["docker", "inspect", "--format", "{{.Id}}", s.container]).stdout.strip()

    def our_labels(s, service="nginx"):
        return {"com.docker.compose.project": s.variant.project, "com.docker.compose.service": service}

    def prepared(name):
        s = deployed_server(rh, name, s2)
        run(["docker", "rm", "-f", s.container])         # nginx is gone and the name is free to be taken
        return s

    def own_half_made():
        s = prepared("c_own")
        held = holder(s, our_labels(s))                    # labels of this project and the nginx service, but not a container Compose recognises
        r = s.sw("rollback", s2)
        ok(r, 0, "S2_RESULT status=returned", f"removing container {held[:12]} that holds the name", f"(project {s.variant.project}, service nginx)")
        assert run(["docker", "inspect", held], check=False).returncode != 0, "this project's own half-made container was not removed"
        old_definition(s)
        s.smoke_ok()
        one_nginx_container(s)
        return ("an nginx container of this project and service that only holds the name (what a killed `up` can leave) was removed after its labels were "
                "checked, and the return finished (exit 0, old definition serving on the captured image)")
    rep.check("F15.1 own half-made nginx holding the name: removed after the label check, the return completes", own_half_made)

    def own_renamed_and_half_made():
        s = deployed_server(rh, "c_renamed", s2)
        old_id = run(["docker", "inspect", "--format", "{{.Id}}", s.container]).stdout.strip()
        run(["docker", "stop", "-t", "2", s.container])
        run(["docker", "rename", s.container, f"{old_id[:12]}_{s.container}"])       # what a recreate that was killed half-way leaves: the old one, renamed and stopped
        held = holder(s, our_labels(s))                                               # and a half-made new one that holds the name
        listed = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={s.variant.project}", "--filter", "label=com.docker.compose.service=nginx"]).stdout.split()
        assert old_id[:12] in listed, "the renamed container must still be listed for the service through its Compose labels"
        r = s.sw("rollback", s2)
        ok(r, 0, "S2_RESULT status=returned", f"removing container {old_id[:12]} (project {s.variant.project}, service nginx)",
           f"removing container {held[:12]} that holds the name")
        for gone in (old_id, held):
            assert run(["docker", "inspect", gone], check=False).returncode != 0, f"{gone[:12]} was not removed"
        old_definition(s)
        s.smoke_ok()
        one_nginx_container(s)
        return ("the renamed, stopped nginx of this project (found through its Compose labels) and a half-made one holding the name were both removed after the label check; "
                "the first `up` had failed on the name, the second created nginx from the captured image; exit 0")
    rep.check("F15.6 own renamed and half-made nginx containers after a killed recreate: both removed through their labels, the return completes", own_renamed_and_half_made)

    cases = [
        ("another project, another service", lambda s: {"com.docker.compose.project": f"{rh.lab.prefix}_foreign", "com.docker.compose.service": "foreign-service"}),
        ("another project, service nginx (an nginx of a different stack)", lambda s: {"com.docker.compose.project": f"{rh.lab.prefix}_foreign", "com.docker.compose.service": "nginx"}),
        ("this project, another service", lambda s: our_labels(s, "not-nginx")),
        ("no Compose labels at all (cannot be verified)", lambda s: {"edu_ai.note": "plain container"}),
    ]
    for i, (name, labels) in enumerate(cases):
        def collision(i=i, name=name, labels=labels):
            s = prepared(f"c_foreign{i}")
            held = holder(s, labels(s))
            before = run(["docker", "inspect", "--format", "{{.Id}} {{.State.Status}} {{.Config.Image}}", held]).stdout.strip()
            r = s.sw("rollback", s2)
            ok(r, 5, "NAME COLLISION", "it was NOT removed", "THE RETURN FAILED")
            assert held[:12] in r.out, "the message does not name the container that holds the name"
            assert "removing container" not in r.out, f"a container was removed: {r.out[-500:]}"
            after = run(["docker", "inspect", "--format", "{{.Id}} {{.State.Status}} {{.Config.Image}}", held], check=False)
            assert after.returncode == 0 and after.stdout.strip() == before, f"the holder of the name changed: {before!r} -> {after.stdout.strip()!r}"
            for needle in [v for v in labels(s).values() if v.startswith(rh.lab.prefix) or v in ("foreign-service", "not-nginx")]:
                assert needle in r.out, f"the holder's label {needle!r} is not in the message"
            # the way out: the owner removes the foreign container, the same command finishes the return
            run(["docker", "rm", "-f", held])
            r2 = s.sw("rollback", s2)
            ok(r2, 0, "S2_RESULT status=returned")
            old_definition(s)
            s.smoke_ok()
            return (f"holder = {name}: the return stopped with exit 5 and a NAME COLLISION message that names the container and its labels; the container "
                    "(same ID, same state) was not touched; after the owner removed it the same command finished the return")
        rep.check(f"F15.{i + 2} a foreign or unverifiable holder of the name is kept and reported: {name}", collision)


def run_rehearsal(lab, rep, su):
    print("F. rehearsal of the deployment and return through scripts/nginx_s2_switch.sh on throw-away clones (never the real repository)")
    rh = Rehearsal(lab, rep, su)
    rep.check("F0 release built: real intermediate commits + tools + S2 + one-fault variants + later releases",
              lambda: rh.build_release() or f"{len(rh.tools_paths)} tool files, {len(rh.s2_paths)} S2 files; baseline {lab.baseline_sha[:7]}")
    only = [x for x in os.environ.get("S2_REHEARSAL_ONLY", "").split(",") if x]     # debugging aid: a subset of scenarios
    for fn in (scn_script_units, scn_deploy_and_return, scn_refusals, scn_failures_after_merge, scn_interruptions, scn_return_fallbacks,
               scn_splice_guard, scn_name_collision, scn_signals):
        if not only or fn.__name__ in only:
            rh.scenario(fn)
