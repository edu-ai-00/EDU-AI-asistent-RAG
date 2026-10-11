#!/usr/bin/env python3
"""Offline tests of the judgements of scripts/nginx_r2_switch.sh and scripts/nginx_s2_guard.sh. No Docker daemon, standard library only.

    python tests/nginx_r2/switch_offline.py          run all, exit 0 only when every test passes
    (offline_tests.py and `r2lab.py selfcheck` run the same tests)

The scripts run for real (bash, git, the real `docker compose config` client) against a scratch server: a git repository with the
deployed S2 commit, a tools commit and the three step commits derived by candidate.py. `docker` is fakedocker.py: it answers from a small
world and records every call, so "nothing was changed" is measured: HEAD, tracked files, state directory and the list of mutating docker calls.

  * R2-R4: a step-2 candidate that adds a port, a mount, a command, an environment variable, a capability, `privileged`, a top-level object,
    a wider trust or another alias is refused before any change; so is a commit that touches a file outside its step, or a fast-forward that
    brings application code. The first reviewed script (59c1e02) accepts the port and `privileged` and the extra file: the tests catch it.
  * R2-R5: a wrong step number, a tools or documentation commit, the commit of another step, a commit that disagrees with the record,
    a sibling commit that is not in HEAD, a damaged record: refused with exit 2 before any change. The first script reverted the tools commit.
  * the S2 guard refuses whenever any R2 sign is present (each alone) OR it cannot verify that none is (a failed or malformed docker inspect of either container,
    a failed or contradicting container list, an unreadable file or state directory, a down daemon), but not for a container PROVEN absent; it runs the unchanged
    S2 script only when R2 is verified not active, and writes nothing. The first guard (8751bff) skipped a failed inspection: the tests run it too and catch it.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))
import candidate as C  # noqa: E402

SWITCH = REPO / "scripts" / "nginx_r2_switch.sh"
TOOL = REPO / "scripts" / "tunnel_subnet.py"
GUARD = REPO / "scripts" / "nginx_s2_guard.sh"
OLD_GUARD = "8751bff03b95bef7ec5620f991512be18667b5e2"          # the first guard: it skipped a failed inspection (Codex, 8751bff)
OLD_SWITCH = "59c1e0251994a3056b0af50934978a21085d4ee9"          # the reviewed candidate whose preflight and return accepted too much
SUBNET = "10.70.221.160/29"
NGINX_ID, CF_ID = "sha256:" + "a" * 64, "sha256:" + "b" * 64
CF_LOG = ('2026-08-27T15:27:24Z INF Updated to new configuration config="{\\"ingress\\":[{\\"hostname\\":\\"l88.edu-ai.eu\\",\\"originRequest\\":{},'
          '\\"service\\":\\"http://bifrost:8080\\"},{\\"hostname\\":\\"rag2.edu-ai.eu\\",\\"originRequest\\":{},\\"service\\":\\"http://edu_ai_nginx:81\\"},'
          '{\\"originRequest\\":{},\\"service\\":\\"http_status:404\\"}],\\"warp-routing\\":{\\"enabled\\":false}}" version=4\n')
WORLDS = {
    "s2": {"nginx": ["edge"], "cf": ["app", "edge"], "trust": None},
    "r2": {"nginx": ["edge", "tunnel"], "cf": ["app", "tunnel", "tunnel_egress"], "trust": f"set_real_ip_from {SUBNET};"},
}


class NotRunnable(RuntimeError):
    pass


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


def p(path) -> str:
    return pathlib.Path(path).resolve().as_posix()


def bash_path() -> str:
    for c in ([r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"] if os.name == "nt" else []) + [shutil.which("bash")]:
        if c and pathlib.Path(c).exists():
            return c
    raise NotRunnable("no bash: the scripts cannot run (ERROR, never PASS)")


def real_docker() -> str:
    d = shutil.which("docker")
    if not d:
        raise NotRunnable("no `docker` client: `docker compose config` cannot render the models (ERROR, never PASS)")
    return d


def rmtree(path):
    def fix(fn, target, _):
        os.chmod(target, stat.S_IWRITE)
        fn(target)
    if pathlib.Path(path).is_file():                    # a test may have replaced a directory by a file
        pathlib.Path(path).unlink()
        return
    shutil.rmtree(path, onerror=fix)


def git(cwd, *args, check=True, input=None):
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                        "-C", str(cwd), *args], capture_output=True, input=input, text=input is None or isinstance(input, str), encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()[-300:]}")
    return r


class Lab:
    """The scratch area of one process: the template repository, the fake docker and the shared Compose render cache."""
    _inst = None

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        bash_path(), real_docker()
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="r2sw_"))
        self.srv = self.root / "srv"                   # ONE path for every server: the rendered models then repeat and the cache hits
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "tmp").mkdir()
        (self.bin / "docker").write_bytes(f'#!/bin/sh\nexec "{p(sys.executable)}" "{p(HERE / "fakedocker.py")}" "$@"\n'.encode())
        self.cache = self.root / "cache"
        self.tool = self.root / "tool" / "tunnel_subnet.py"           # the tool as the operator copies it, wrapped: the host inventory is a stub
        self.tool.parent.mkdir()
        shutil.copy(TOOL, self.tool.parent / "real_tunnel_subnet.py")
        self.tool.write_text("import subprocess, sys\nif sys.argv[1] == 'check':\n    print('OK: host inventory stubbed (these tests judge models and commits)')\n    sys.exit(0)\n"
                             f"sys.exit(subprocess.call([sys.executable, r'{p(self.tool.parent / 'real_tunnel_subnet.py')}'] + sys.argv[1:]))\n")
        self.old_switch = self.root / "old_switch.sh"
        old = subprocess.run(["git", "-C", str(REPO), "show", f"{OLD_SWITCH}:scripts/nginx_r2_switch.sh"], capture_output=True, check=True).stdout
        # only so that the tool, a Windows Python here, can read the temporary directory (the fixed script does this itself); no logic is touched
        self.old_switch.write_bytes(old.replace(b'--conf-dir "$TMP/cand/nginx/conf"', b'--conf-dir "$(abs "$TMP/cand/nginx/conf")"'))
        self.old_guard = self.root / "old_guard.sh"
        self.old_guard.write_bytes(subprocess.run(["git", "-C", str(REPO), "show", f"{OLD_GUARD}:scripts/nginx_s2_guard.sh"], capture_output=True, check=True).stdout)
        self.template = self.root / "template"
        self.sha = self.build_template()

    def build_template(self):
        t = self.template
        t.mkdir()
        git(t, "init", "-q", "-b", "main")
        base = C.baseline(C.S2_BASE)
        files = {k: v for k, v in base.items()}
        files["scripts/nginx_s2_switch.sh"] = subprocess.run(["git", "-C", str(REPO), "show", f"{C.S2_BASE}:scripts/nginx_s2_switch.sh"], capture_output=True, check=True).stdout
        sha = {}

        def commit(name, changes, msg):
            for path, data in changes.items():
                (t / path).parent.mkdir(parents=True, exist_ok=True)
                (t / path).write_bytes(data)
            git(t, "add", "-A")
            git(t, "commit", "-q", "-m", msg)
            sha[name] = git(t, "rev-parse", "HEAD").stdout.strip()
        commit("s2", files, "S2")
        commit("tools", {"scripts/nginx_r2_switch.sh": SWITCH.read_bytes().replace(b"\r\n", b"\n"), "scripts/tunnel_subnet.py": TOOL.read_bytes().replace(b"\r\n", b"\n"),
                         "tests/nginx_r2/README.md": b"tests\n", "tests/README.md": b"tests\n", "docs/plans/note.md": b"note\n"}, "tools, tests, docs")
        for n in (1, 2, 3):
            commit(f"step{n}", C.step_content(n), f"R2 step {n}")
        return sha

    # ── one server: a copy of the template at a commit, an untracked .env, a fake-docker world ──
    def server(self, at="s2", world="s2"):
        if self.srv.exists():
            rmtree(self.srv)
        shutil.copytree(self.template, self.srv)
        git(self.srv, "checkout", "-q", "-B", "main", self.sha[at])
        (self.srv / ".env").write_bytes(b"COMPOSE_FILE=docker-compose.yml\nCOMPOSE_PROJECT_NAME=edu_ai\n")
        return Server(self, world)


class Server:
    def __init__(self, lab, world):
        self.lab, self.dir = lab, lab.srv
        self.state = lab.root / "state"
        if self.state.exists():
            rmtree(self.state)
        self.state.mkdir()
        self.calls_file, self.world_file = lab.root / "calls.jsonl", lab.root / "world.json"
        self.calls_file.write_text("")
        self.set_world(world)
        self.sha = dict(lab.sha)

    def set_world(self, world="s2", **over):
        w = WORLDS[world]
        data = {"project": "edu_ai", "images": {"nginx:1.27-alpine": NGINX_ID, "cloudflare/cloudflared:2026.8.1": CF_ID}, "cf_log": CF_LOG,
                "containers": {"edu_ai_nginx": {"status": "running", "health": "healthy", "image": NGINX_ID, "networks": w["nginx"], "mounts": ["/etc/nginx"] + (["/etc/edu_ai/tunnel/trust.conf"] if w["trust"] else []), "trust_conf": w["trust"]},
                               "cloudflared": {"status": "running", "health": "none", "image": CF_ID, "networks": w["cf"], "mounts": []}}}
        for k, v in over.items():
            data["containers"][k].update(v)
        self.world_file.write_text(json.dumps(data), encoding="utf-8")

    def world_edit(self, fn):
        """Change the fake docker's world in place: fn(world dict)."""
        data = json.loads(self.world_file.read_text(encoding="utf-8"))
        fn(data)
        self.world_file.write_text(json.dumps(data), encoding="utf-8")

    def env(self, extra=None):
        e = dict(os.environ, PATH=str(self.lab.bin) + os.pathsep + os.environ["PATH"], REAL_DOCKER=real_docker(), FAKE_DOCKER_WORLD=p(self.world_file),
                 FAKE_DOCKER_CALLS=p(self.calls_file), FAKE_DOCKER_CACHE=p(self.lab.cache), R2_STATE_DIR=p(self.state), R2_SUBNET_TOOL=p(self.lab.tool),
                 R2_PYTHON=p(sys.executable), EDU_AI_TUNNEL_SUBNET=SUBNET, R2_SMOKE_DEADLINE="2", HOME=p(self.lab.root), PYTHONIOENCODING="utf-8",
                 TMPDIR=p(self.lab.root / "tmp"))
        e.update(extra or {})
        return e

    def run(self, script, *args, env=None, timeout=600):
        r = subprocess.run([bash_path(), p(script), *map(str, args)], cwd=str(self.dir), env=self.env(env), capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")

    def sw(self, *args, **kw):
        return self.run(SWITCH, *args, **kw)

    def old(self, *args, **kw):
        return self.run(self.lab.old_switch, *args, **kw)

    def source(self, snippet, env=None):
        """Run shell code with the functions of the switch script loaded (R2_SOURCE_ONLY): for the pure judgements."""
        e = self.env({"R2_SOURCE_ONLY": "1", **(env or {})})
        r = subprocess.run([bash_path(), "-c", f'source "{p(SWITCH)}"; {snippet}'], cwd=str(self.dir), env=e, capture_output=True, timeout=120)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")

    def guard(self, *args, env=None):
        return self.run(GUARD, *args, env=env, timeout=120)

    # ── observation ──
    def calls(self):
        return [json.loads(l) for l in self.calls_file.read_text().splitlines() if l.strip()]

    def mutations(self):
        return [" ".join(c["argv"][:4]) for c in self.calls() if c["mutating"]]

    def snapshot(self):
        g = lambda *a: git(self.dir, *a).stdout   # noqa: E731
        files = {f: hashlib.sha256((self.dir / f).read_bytes()).hexdigest()[:12] for f in g("ls-files").split() if (self.dir / f).is_file()}
        return {"head": g("rev-parse", "HEAD").strip(), "branch": g("rev-parse", "--abbrev-ref", "HEAD").strip(), "status": g("status", "--porcelain"),
                "files": files, "mutations": self.mutations(), "log": g("log", "--format=%H", "-n", "30")}

    def unchanged(self, before, what):
        after = self.snapshot()
        diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        must(not diff, f"{what} changed something: {json.dumps(diff, default=str)[:500]}")

    def commit_on(self, parent, edits, msg="variant", chmod=()):
        """A commit on top of `parent` made in a scratch worktree (the server's checkout is not touched). edits: path -> fn(bytes) -> bytes."""
        wt = self.lab.root / "wt"
        if wt.exists():
            rmtree(wt)
        git(self.dir, "worktree", "add", "-q", "--detach", str(wt), parent)
        try:
            for path, fn in edits.items():
                f = wt / path
                old = f.read_bytes() if f.exists() else b""
                new = fn(old)
                must(new != old, f"the edit of {path} changed nothing")
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(new)
            for path in chmod:
                git(wt, "update-index", "--add", "--chmod=+x", path)
            git(wt, "add", "-A")
            git(wt, "commit", "-q", "-m", msg)
            return git(wt, "rev-parse", "HEAD").stdout.strip()
        finally:
            git(self.dir, "worktree", "remove", "--force", str(wt), check=False)


def sub(old, new, count=1):
    def fn(data: bytes) -> bytes:
        t = data.decode("utf-8")
        must(t.count(old) == count, f"{old!r}: {t.count(old)} matches, expected {count}")
        return t.replace(old, new).encode("utf-8")
    return fn


def add(text):
    return lambda data: data + text.encode("utf-8")


def after_line(anchor, text, nth=1):
    """Insert `text` (with its own newline) after the nth line that starts with `anchor`."""
    def fn(data: bytes) -> bytes:
        lines, seen, out = data.decode("utf-8").splitlines(keepends=True), 0, []
        for line in lines:
            out.append(line)
            if line.startswith(anchor):
                seen += 1
                if seen == nth:
                    out.append(text)
        must(seen >= nth, f"anchor {anchor!r} not found")
        return "".join(out).encode("utf-8")
    return fn


# ── R2-R4: the exact change of step 2 ────────────────────────────────────────────────────────────────
FORBIDDEN = {      # name: (edit of the step-2 docker-compose.yml, a path the refusal must name)
    "nginx_port": (sub('- "80:80"         #', '- "9080:80"       #'), "services.nginx.ports"),
    "nginx_privileged": (after_line("    container_name: edu_ai_nginx", "    privileged: true\n"), "services.nginx.privileged"),
    "nginx_mount": (sub("        read_only: true\n        bind:\n          create_host_path: false\n    networks:\n      edge: {}",
                        "        read_only: true\n        bind:\n          create_host_path: false\n      - /var/run/docker.sock:/var/run/docker.sock\n    networks:\n      edge: {}"),
                    "services.nginx.volumes"),
    "nginx_command": (after_line("    container_name: edu_ai_nginx", '    command: ["sleep", "3600"]\n'), "services.nginx.command"),
    "nginx_cap_add": (after_line("    container_name: edu_ai_nginx", "    cap_add: [NET_ADMIN]\n"), "services.nginx.cap_add"),
    "cloudflared_command": (sub("command: tunnel --no-autoupdate run", "command: tunnel --no-autoupdate run --loglevel debug"), "services.cloudflared.command"),
    "cloudflared_env": (sub("      TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:-}\n", "      TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:-}\n      TUNNEL_EXTRA: x\n"), "services.cloudflared.environment"),
    "cloudflared_ports": (after_line("    container_name: cloudflared", '    ports:\n      - "9999:9999"\n'), "services.cloudflared.ports"),
    "cloudflared_cap_add": (after_line("    container_name: cloudflared", "    cap_add: [NET_ADMIN]\n"), "services.cloudflared.cap_add"),
    "cloudflared_mount": (after_line("    container_name: cloudflared", "    volumes:\n      - /:/host:ro\n"), "services.cloudflared.volumes"),
    "other_service": (sub("\n  edu_ai_search:\n", '\n  edu_ai_search:\n    labels:\n      touched: "1"\n'), "services.edu_ai_search"),
    "toplevel_extension": (add("\nx-note: hello\n"), "x-note"),
    "other_network": (sub("  tunnel_egress:\n    driver: bridge", "  tunnel_egress:\n    driver: bridge\n    internal: true"), "networks.tunnel_egress"),
    "extra_alias": (sub("          - edu_ai_nginx\n", "          - edu_ai_nginx\n          - evil\n"), "services.nginx.networks.tunnel"),
    "wider_trust": (sub("      set_real_ip_from ${EDU_AI_TUNNEL_SUBNET};\n", "      set_real_ip_from ${EDU_AI_TUNNEL_SUBNET};\n      set_real_ip_from 0.0.0.0/0;\n"), "configs.tunnel_trust.content"),
    "nginx_on_app": (sub("      edge: {}          # the upstreams; cannot reach the data tier\n", "      edge: {}          # the upstreams; cannot reach the data tier\n      app: {}\n"), "services.nginx.networks.app"),
}


def at_step1():
    return Lab.get().server(at="step1", world="s2")


def t_delta_function_on_real_models():
    """The pure judgement on models rendered by the REAL Compose client: the designed change passes, every forbidden one is named by path."""
    import tunnel_subnet as TS
    srv = at_step1()
    # the S2 model is the current one (HEAD content = step 1 = S2 compose); the candidate is the step-2 compose, rendered in the SAME directory
    cur = json.loads(subprocess.run([real_docker(), "compose", "config", "--format", "json"], cwd=str(srv.dir), env=srv.env(), capture_output=True, check=True).stdout)
    step2 = (srv.dir.parent / "step2.yml")
    step2.write_bytes(subprocess.run(["git", "-C", str(srv.dir), "show", f"{srv.sha['step2']}:docker-compose.yml"], capture_output=True, check=True).stdout)

    def render(compose_bytes):
        r = subprocess.run([real_docker(), "compose", "-f", "-", "config", "--format", "json"], cwd=str(srv.dir), env=srv.env(), input=compose_bytes, capture_output=True)
        must(r.returncode == 0, f"compose: {r.stderr.decode()[-300:]}")
        return json.loads(r.stdout)
    good = render(step2.read_bytes())
    must(TS.step_delta_problems(cur, good, 2) == [], f"the designed change was refused: {TS.step_delta_problems(cur, good, 2)}")
    must(TS.step_delta_problems(cur, cur, 1) == [] and TS.step_delta_problems(cur, cur, 3) == [], "steps 1 and 3 refuse an unchanged model")
    must(TS.step_delta_problems(cur, good, 1) != [] and TS.step_delta_problems(cur, good, 3) != [], "steps 1 and 3 accept the step-2 change")
    named = []
    for name, (edit, path) in FORBIDDEN.items():
        bad = render(edit(step2.read_bytes()))
        got = TS.step_delta_problems(cur, bad, 2)
        must(got and any(g == path or g.startswith(path) for g in got), f"{name}: the refusal does not name {path}: {got}")
        named.append(name)
    secret = json.loads(json.dumps(good))
    secret["services"]["cloudflared"]["environment"] = {"TUNNEL_TOKEN": "s3cr3t-token-value"}
    must("s3cr3t" not in json.dumps(TS.step_delta_problems(cur, secret, 2)), "a value of the model was printed")
    return f"the designed change is exact; {len(named)} forbidden changes are each refused and named by path: {', '.join(named)}; no value is printed"


def t_preflight_accepts_the_designed_step_and_refuses_everything_else():
    srv = at_step1()
    rc, out = srv.sw("preflight", 2, srv.sha["step2"])
    must(rc == 0 and "preflight OK" in out, f"the designed step 2 was refused (rc {rc}): {out[-600:]}")
    must(not srv.mutations(), f"the preflight issued mutating docker calls: {srv.mutations()}")
    before = srv.snapshot()
    refused = []
    for name, (edit, path) in FORBIDDEN.items():
        step2 = subprocess.run(["git", "-C", str(srv.dir), "show", f"{srv.sha['step2']}:docker-compose.yml"], capture_output=True, check=True).stdout
        sha = srv.commit_on(srv.sha["step1"], {"docker-compose.yml": lambda d, e=edit, s=step2: e(s)}, msg=name)
        rc, out = srv.sw("preflight", 2, sha)
        by_path = "changes more than step 2 may" in out and path in out
        by_hash = "must change exactly cloudflared and nginx" in out and path.split(".")[1] in out          # another service: the hashes already say so
        must(rc == 2 and (by_path or by_hash), f"{name}: rc {rc}, wanted a refusal naming {path}: {out[-500:]}")
        must("phase: merged" not in out and "recreating" not in out, f"{name}: the script went past the preflight")
        srv.unchanged(before, f"the refusal of {name}")
        refused.append(name)
    return f"the designed step 2 passes; {len(refused)} candidates that also change a port, mount, command, capability, privileged, environment, another service or a top-level object are refused with exit 2 before any change (HEAD, tracked files, mutating docker calls: none)"


def t_first_script_accepted_the_forbidden_changes():
    """The reviewed first version (59c1e02) accepted a port change and `privileged` (R2-R4): the tests above would have caught it."""
    srv = at_step1()
    step2 = subprocess.run(["git", "-C", str(srv.dir), "show", f"{srv.sha['step2']}:docker-compose.yml"], capture_output=True, check=True).stdout
    seen = []
    for name in ("nginx_port", "nginx_privileged", "nginx_mount"):
        sha = srv.commit_on(srv.sha["step1"], {"docker-compose.yml": lambda d, e=FORBIDDEN[name][0]: e(step2)}, msg=name)
        rc, out = srv.old("preflight", 2, sha)
        new_rc, _ = srv.sw("preflight", 2, sha)
        must(rc == 0 and "preflight OK" in out, f"{name}: the first version no longer reproduces the finding (rc {rc}): {out[-300:]}")
        must(new_rc == 2, f"{name}: the fixed script accepts it (rc {new_rc})")
        seen.append(name)
    return f"the first version answered preflight OK (exit 0) for {', '.join(seen)}; the fixed script refuses each with exit 2"


# ── R2-R4: the scope of the commit and of the fast-forward ─────────────────────────────────────────────
def t_commit_and_range_scope():
    srv = at_step1()
    before = srv.snapshot()
    step2 = subprocess.run(["git", "-C", str(srv.dir), "show", f"{srv.sha['step2']}:docker-compose.yml"], capture_output=True, check=True).stdout
    s2c = lambda d: step2   # noqa: E731
    cases = {
        "extra_app_file": ({"docker-compose.yml": s2c, "search/app/evil.py": add("print('x')\n")}, (), "does not change exactly the files of step 2"),
        "extra_script": ({"docker-compose.yml": s2c, "scripts/nginx_r2_switch.sh": add("# changed\n")}, (), "does not change exactly the files of step 2"),
        "mode_change": ({"docker-compose.yml": s2c}, ("docker-compose.yml",), "regular files"),
    }
    seen = []
    for name, (edits, chmod, frag) in cases.items():
        sha = srv.commit_on(srv.sha["step1"], edits, msg=name, chmod=chmod)
        rc, out = srv.sw("preflight", 2, sha)
        must(rc == 2 and frag in out, f"{name}: rc {rc}, wanted {frag!r}: {out[-400:]}")
        srv.unchanged(before, name)
        seen.append(name)
    # the whole commit, not a path filter: the first script accepted the commit that also adds application code
    sha = srv.commit_on(srv.sha["step1"], {"docker-compose.yml": s2c, "search/app/evil.py": add("print('x')\n")}, msg="old")
    rc, out = srv.old("preflight", 2, sha)
    must(rc == 0 and "preflight OK" in out, f"the first version no longer accepts a commit with an extra file (rc {rc}): {out[-300:]}")
    # a fast-forward that brings application code (an intermediate commit) is refused, documentation and tool changes are not
    srv2 = Lab.get().server(at="s2", world="s2")
    before = srv2.snapshot()
    evil = srv2.commit_on(srv2.sha["s2"], {"search/app/evil.py": add("print('x')\n")}, msg="app code")
    s1 = srv2.commit_on(evil, {k: (lambda d, v=v: v) for k, v in C.step_content(1).items()}, msg="step 1 on top of app code")
    rc, out = srv2.sw("preflight", 1, s1)
    must(rc == 2 and "outside the approved scope" in out and "search/app/evil.py" in out, f"intermediate app code: rc {rc}: {out[-400:]}")
    srv2.unchanged(before, "the intermediate app-code commit")
    docs = srv2.commit_on(srv2.sha["s2"], {"docs/plans/more.md": add("x\n"), "tests/nginx_r2/new_test.py": add("x\n"), "scripts/nginx_s2_guard.sh": add("x\n")}, msg="docs and tools")
    s1 = srv2.commit_on(docs, {k: (lambda d, v=v: v) for k, v in C.step_content(1).items()}, msg="step 1 on top of docs")
    rc, out = srv2.sw("preflight", 1, s1)
    must(rc == 0 and "preflight OK" in out, f"documentation, tests and tools in front of the step were refused (rc {rc}): {out[-400:]}")
    # the files are the right ones, but the commit does not move the deployment from step 1 to step 2 (step-2 content built on S2)
    wrong = srv2.commit_on(srv2.sha["s2"], {k: (lambda d, v=v: v) for k, v in C.step_content(2).items()}, msg="step 2 content on S2")
    rc, out = srv2.sw("preflight", 2, wrong)
    must(rc == 2 and "do not move the deployment from step 1 to step 2" in out, f"a step-2 commit built on S2: rc {rc}: {out[-300:]}")
    srv2.unchanged(before, "the step-2 commit built on S2")
    return (f"{', '.join(seen)}: refused (the WHOLE commit is judged, not a path filter; the first script accepted the extra file); application code in a commit in front of the "
            "step refused, documentation/tests/tools accepted; step content of another step refused; every refusal changed nothing")


# ── R2-R5: the step and the commit of a return ───────────────────────────────────────────────────────
def applied_server(upto=1, record=True):
    """A server whose checkout is at `upto` (HEAD = that step commit, as after `apply`), with the record of the state directory."""
    srv = Lab.get().server(at=f"step{upto}", world="s2" if upto == 1 else "r2")
    if record:
        lines = [f"{n} {srv.sha[f'step{n}']} {srv.sha['tools'] if n == 1 else srv.sha[f'step{n - 1}']}\n" for n in range(1, upto + 1)]
        (srv.state / "steps.log").write_text("".join(lines))
    return srv


def t_rollback_inputs_are_refused_before_any_change():
    srv = applied_server(1)
    before = srv.snapshot()
    state_before = (srv.state / "steps.log").read_text()
    # a SIBLING of the step-1 commit: the same parent, the step's files plus a comment (looks like step 1, is not the recorded commit)
    side = srv.commit_on(srv.sha["tools"], {k: (lambda d, v=v: v + b"# a sibling\n") for k, v in C.step_content(1).items()}, msg="sibling")
    bad = {
        "step 0": ("0", srv.sha["step1"], "must be 1, 2 or 3"),
        "step 99": ("99", srv.sha["step1"], "must be 1, 2 or 3"),
        "step text": ("one", srv.sha["step1"], "must be 1, 2 or 3"),
        "unknown commit": ("1", "0123456789abcdef0123456789abcdef01234567", "is not a commit"),
        "the tools commit": ("1", srv.sha["tools"], "does not change exactly the files of step 1"),
        "the s2 commit": ("1", srv.sha["s2"], "merge commit"),
        "a documentation commit": ("1", srv.commit_on(srv.sha["step1"], {"docs/plans/x.md": add("x\n")}, msg="docs"), "does not change exactly the files of step 1"),
        "step 2 commit for step 1": ("1", srv.sha["step2"], "does not change exactly the files of step 1"),
        "step 1 commit for step 2": ("2", srv.sha["step1"], "does not change exactly the files of step 2"),
        "step 1 commit for step 3": ("3", srv.sha["step1"], "does not change exactly the files of step 3"),
        "step 3 commit for step 1": ("1", srv.sha["step3"], "does not change exactly the files of step 1"),
        "a sibling that is not the record": ("1", side, "applied from"),
    }
    for name, (step, sha, frag) in bad.items():
        rc, out = srv.sw("rollback", step, sha)
        must(rc == 2 and frag in out, f"{name}: rc {rc}, wanted exit 2 and {frag!r}: {out[-400:]}")
        must(not srv.calls(), f"{name}: docker was called before the refusal: {srv.calls()[:2]}")
        srv.unchanged(before, name)
        must((srv.state / "steps.log").read_text() == state_before, f"{name}: the record changed")
    # without a record the commit is judged on its content alone, and a sibling of the step that is not in HEAD is still refused
    (srv.state / "steps.log").unlink()
    rc, out = srv.sw("rollback", 1, side)
    must(rc == 2 and "is not in HEAD" in out, f"a sibling without a record: rc {rc}: {out[-400:]}")
    rc, out = srv.sw("rollback", 1, srv.sha["tools"])
    must(rc == 2, f"the tools commit without a record: rc {rc}")
    srv.unchanged(before, "the refusals without a record")
    # the order: step 1 is not returned while step 2 is applied
    srv2 = applied_server(2)
    before2 = srv2.snapshot()
    rc, out = srv2.sw("rollback", 1, srv2.sha["step1"])
    must(rc == 2 and "return step 2 first" in out and not srv2.calls(), f"the order of the return: rc {rc}: {out[-300:]}")
    srv2.unchanged(before2, "returning step 1 under step 2")
    # rollback-all with a damaged record
    (srv2.state / "steps.log").write_text(f"1 {srv2.sha['step1']} x\n2 {srv2.sha['tools']} y\n")
    rc, out = srv2.sw("rollback-all")
    must(rc == 2 and "record of step 2" in out and not srv2.calls(), f"a damaged record: rc {rc}: {out[-300:]}")
    srv2.unchanged(before2, "rollback-all with a damaged record")
    (srv2.state / "steps.log").write_text(f"1 {srv2.sha['step1']} x\n2 {srv2.sha['step1']} y\n")
    rc, out = srv2.sw("rollback-all")
    must(rc == 2 and "record of step 2" in out, f"a record that holds the commit of another step: rc {rc}: {out[-300:]}")
    srv2.unchanged(before2, "rollback-all with the commit of another step in the record")
    (srv2.state / "steps.log").write_text(f"3 {srv2.sha['step1']} y\n")                 # a valid step-1 commit, recorded as step 3
    rc, out = srv2.sw("rollback", 1, srv2.sha["step1"])
    must(rc == 2 and "is recorded as step 3, not as step 1" in out and not srv2.calls(), f"a commit recorded for another step: rc {rc}: {out[-300:]}")
    srv2.unchanged(before2, "rollback of a commit recorded for another step")
    return (f"{len(bad)} wrong step/commit pairs and 5 more without a record, under a step, with a damaged record or one recorded for another step: exit 2 before any docker call, "
            "HEAD, tracked files and the record unchanged")


def t_first_script_reverted_the_tools_commit():
    srv = applied_server(1)
    before = srv.snapshot()
    rc, out = srv.old("rollback", 1, srv.sha["tools"])
    after = srv.snapshot()
    gone = [f for f in before["files"] if f not in after["files"]]
    must(after["head"] != before["head"] and "scripts/nginx_r2_switch.sh" in gone,
         f"the first version no longer reverts the tools commit (rc {rc}); head changed: {after['head'] != before['head']}, files gone: {gone}")
    srv2 = applied_server(1)
    rc2, out2 = srv2.old("rollback", 99, srv2.sha["step1"])
    rc3, out3 = srv2.sw("rollback", 99, srv2.sha["step1"])
    rs, outs = srv2.source("rollback_step 99; echo rc=$?")
    st = srv2.snapshot()
    return (f"the first version: `rollback 1 <tools commit>` rewrote HEAD and removed {len(gone)} files ({', '.join(gone[:2])} ...) before any runtime check; `rollback 99` ended with exit {rc2}; "
            f"the fixed script: exit {rc3}; `rollback_step 99` -> {outs.strip().splitlines()[-1]}; head after: {st['head'][:10]}")


def t_rollback_step_refuses_an_invalid_step():
    srv = applied_server(1)
    before = srv.snapshot()
    rc, out = srv.source("rollback_step 99; echo rc=$?")
    must("rc=1" in out and "must be 1, 2 or 3" in out, f"rollback_step 99: {out[-300:]}")
    rc, out = srv.source("rollback_step ''; echo rc=$?")
    must("rc=1" in out, f"rollback_step '': {out[-300:]}")
    must(not srv.calls(), f"docker was called: {srv.calls()[:2]}")
    srv.unchanged(before, "rollback_step with an invalid step")
    return "rollback_step with step 99 or an empty step returns 1 and does nothing (it returned 0 / status=returned before)"


def t_valid_returns_are_accepted():
    """The legitimate cases stay possible: a return from a record, a repeated return, the completion of an interrupted one, a retry commit."""
    def accepted(srv, step, sha, state=None):
        rc, out = srv.source(f"validate_return_commit {step} {sha} && validate_return_state {step}; echo rc=$?")
        must("rc=0" in out, f"step {step} {sha[:10]} was refused: {out[-400:]}")
        return out
    notes = []
    srv = applied_server(3)
    for n in (3, 2, 1):                                                                # the order of a return: the highest step first (state is judged at step 3)
        rc, out = srv.source(f"validate_return_commit {n} {srv.sha[f'step{n}']}; echo rc=$?")
        must("rc=0" in out, f"step {n} with its record: {out[-300:]}")
    accepted(srv, 3, srv.sha["step3"])
    notes.append("each recorded step is accepted, step 3 also against the checkout at step 3")
    # a repeated return: HEAD holds the step AND its revert, the record is gone
    srv = applied_server(1, record=False)
    git(srv.dir, "revert", "--no-edit", srv.sha["step1"])
    out = accepted(srv, 1, srv.sha["step1"])
    must("no record of step 1" in out and "accepted on its content" in out, f"the missing record is not stated: {out[-300:]}")
    notes.append("a repeated return after the revert without a record is accepted on its content (and says so)")
    # the completion of an interrupted return: the revert is committed, the record is still there
    srv = applied_server(2)
    git(srv.dir, "revert", "--no-edit", srv.sha["step2"])
    accepted(srv, 2, srv.sha["step2"])
    notes.append("an interrupted return (revert committed, record kept) is completed")
    # the revert commit itself is not a step commit
    rv = git(srv.dir, "rev-parse", "HEAD").stdout.strip()
    rc, out = srv.source(f"validate_return_commit 2 {rv}; echo rc=$?")
    must("rc=1" in out, f"a revert commit was accepted as the step: {out[-300:]}")
    # a retry: the step cherry-picked onto the current HEAD
    srv = applied_server(1, record=False)
    git(srv.dir, "revert", "--no-edit", srv.sha["step1"])
    git(srv.dir, "cherry-pick", srv.sha["step1"])
    retry = git(srv.dir, "rev-parse", "HEAD").stdout.strip()
    must(retry != srv.sha["step1"], "the retry commit is the original")
    accepted(srv, 1, retry)
    notes.append("the cherry-picked retry commit is accepted for the step")
    return "; ".join(notes)


def t_step_from_agrees_between_tree_and_checkout():
    srv = Lab.get().server(at="s2", world="s2")
    seen = []
    for name, want in (("s2", 0), ("tools", 0), ("step1", 1), ("step2", 2), ("step3", 3)):
        rc, out = srv.source(f"echo $(tree_step {srv.sha[name]})")
        must(out.strip().splitlines()[-1] == str(want), f"tree_step({name}) = {out.strip()[-40:]}, wanted {want}")
        git(srv.dir, "checkout", "-q", srv.sha[name])
        rc, out = srv.source("echo $(observed_step)")
        must(out.strip().splitlines()[-1] == str(want), f"observed_step at {name} = {out.strip()[-40:]}, wanted {want}")
        seen.append(f"{name}={want}")
    return "the step shown by a commit (git show) and by the checkout (files) is the same function: " + ", ".join(seen)


# ── the S2 guard ───────────────────────────────────────────────────────────────────────────────────────
S2_STUB = '#!/usr/bin/env bash\necho "S2 STUB RAN: $*" >> "$STUB_LOG"\nexit 7\n'


def guard_server(files="s2", world="s2", state=None):
    lab = Lab.get()
    srv = lab.server(at=files, world=world)
    if state:
        (srv.state / "steps.log").write_text(state)
    stub = lab.root / "s2-stub.sh"
    stub.write_text(S2_STUB)
    srv.stub, srv.stub_log = stub, lab.root / "stub.log"
    srv.stub_log.write_text("")
    return srv


def guard_env(srv):
    return {"S2_SCRIPT": p(srv.stub), "STUB_LOG": p(srv.stub_log), "R2_STATE_DIR": p(srv.state)}


def tree_digest(*roots):
    h = hashlib.sha256()
    for root in roots:
        for f in sorted(pathlib.Path(root).rglob("*")):
            if f.is_file() and ".git" not in f.parts:
                h.update(f.relative_to(root).as_posix().encode() + f.read_bytes())
    return h.hexdigest()


def t_guard_refuses_each_r2_sign_alone():
    cases = {
        "the checkout carries step 1": dict(files="step1"),
        "the checkout carries step 2": dict(files="step2"),
        "the checkout carries step 3": dict(files="step3"),
        "the state directory records a step": dict(state="1 abc def\n"),
        "nginx is on the tunnel network": dict(over={"edu_ai_nginx": {"networks": ["edge", "tunnel"]}}),
        "nginx mounts the trust file": dict(over={"edu_ai_nginx": {"mounts": ["/etc/nginx", "/etc/edu_ai/tunnel/trust.conf"]}}),
        "cloudflared is on the tunnel networks": dict(over={"cloudflared": {"networks": ["app", "tunnel", "tunnel_egress"]}}),
        "cloudflared is not on edge": dict(over={"cloudflared": {"networks": ["app"]}}),
        "a half-made return: cloudflared back on edge, nginx still on tunnel": dict(over={"edu_ai_nginx": {"networks": ["edge", "tunnel"]}, "cloudflared": {"networks": ["app", "edge"]}}),
    }
    refused = []
    for name, spec in cases.items():
        srv = guard_server(files=spec.get("files", "s2"), state=spec.get("state"))
        if spec.get("over"):
            srv.set_world("s2", **spec["over"])
        env = guard_env(srv)
        before = (tree_digest(srv.dir, srv.state, srv.stub.parent / "s2-stub.sh"), srv.snapshot())
        for args in (("rollback", srv.sha["s2"]), ("deploy", srv.sha["s2"]), ("check",)):
            rc, out = srv.guard(*args, env=env)
            must(rc == 2 and "REFUSED: R2 is active" in out, f"{name} / {args[0]}: rc {rc}: {out[-400:]}")
            must("rollback-all" in out, f"{name}: the refusal does not say what to do first")
        must(srv.stub_log.read_text() == "", f"{name}: the S2 script ran")
        must((tree_digest(srv.dir, srv.state, srv.stub.parent / "s2-stub.sh"), srv.snapshot()) == before, f"{name}: the guard changed something")
        refused.append(name)
    return f"{len(refused)} R2 signs, each ALONE, refuse rollback, deploy and check with exit 2 and the instruction `rollback-all` first; the S2 script never ran; nothing was written"


def t_guard_runs_the_unchanged_s2_script_when_r2_is_not_active():
    srv = guard_server()
    env = guard_env(srv)
    before = tree_digest(srv.dir, srv.state)
    rc, out = srv.guard("check", env=env)
    must(rc == 0 and "R2 is verified not active" in out, f"check: rc {rc}: {out[-300:]}")
    rc, out = srv.guard("rollback", srv.sha["s2"], env=env)
    must(rc == 7 and "NOTE: the S2 script could not be compared" in out, f"rollback: rc {rc} (the stub returns 7): {out[-300:]}")
    must(srv.stub_log.read_text().strip() == f"S2 STUB RAN: rollback {srv.sha['s2']}", f"the S2 script did not receive the arguments unchanged: {srv.stub_log.read_text()!r}")
    must(tree_digest(srv.dir, srv.state) == before, "the guard wrote something")
    # the real S2 script is recognised as the committed original (blob comparison against the S2 commit); `preflight` is read-only and handed on
    real = srv.lab.root / "s2-real.sh"
    real.write_bytes(subprocess.run(["git", "-C", str(REPO), "show", f"{C.S2_BASE}:scripts/nginx_s2_switch.sh"], capture_output=True, check=True).stdout)
    s2state = srv.lab.root / "s2state"
    rc, out = srv.guard("preflight", srv.sha["s2"], env={**env, "S2_SCRIPT": p(real), "S2_STATE_DIR": p(s2state)})
    must("is the committed original of" in out, f"the original S2 script was not recognised: {out[:300]}")
    must(git(srv.dir, "rev-parse", "HEAD").stdout.strip() == srv.sha["s2"], "the S2 preflight moved HEAD")
    return "R2 not active: check exit 0; `rollback` handed to the S2 script with the same arguments and its exit status returned (7); the original S2 script is recognised by its blob; the guard writes nothing"


def t_guard_read_only_commands_pass_while_r2_is_active_and_other_inputs_fail():
    srv = guard_server(files="step3", world="r2", state="3 abc def\n")
    env = guard_env(srv)
    for cmd in ("smoke", "preflight"):
        rc, out = srv.guard(cmd, srv.sha["s2"], env=env)
        must(rc == 7, f"{cmd} while R2 is active: rc {rc}: {out[-200:]}")
    must(srv.stub_log.read_text().count("S2 STUB RAN") == 2, "the read-only commands did not reach the S2 script")
    rc, out = srv.guard("frobnicate", env=env)
    must(rc == 2, f"an unknown command: rc {rc}")
    rc, out = srv.guard(env=env)
    must(rc == 2, f"no command: rc {rc}")
    rc, out = srv.guard("rollback", srv.sha["s2"], env={**env, "R2_STATE_DIR": p(srv.state / "none")})
    must(rc == 2, f"the checkout and the runtime still say R2 is active: rc {rc}")
    clean = guard_server()
    rc, out = clean.guard("rollback", clean.sha["s2"], env={**guard_env(clean), "S2_SCRIPT": p(clean.lab.root / "missing.sh")})
    must(rc == 2 and "not readable" in out, f"a missing S2 script: rc {rc}: {out[-200:]}")
    nodir = Lab.get().root / "empty"
    nodir.mkdir(exist_ok=True)
    r = subprocess.run([bash_path(), p(GUARD), "rollback", "x"], cwd=str(nodir), env=clean.env(guard_env(clean)), capture_output=True)
    must(r.returncode == 2 and b"deployment checkout" in r.stdout + r.stderr, f"not a deployment checkout: rc {r.returncode}")
    return "smoke and preflight (read-only) are handed on while R2 is active; unknown or missing commands, a missing S2 script and a directory that is not the checkout are refused with 2"


def t_guard_allows_the_s2_return_after_the_full_r2_return():
    srv = applied_server(3)
    git(srv.dir, "revert", "--no-edit", srv.sha["step3"])
    git(srv.dir, "revert", "--no-edit", srv.sha["step2"])
    git(srv.dir, "revert", "--no-edit", srv.sha["step1"])
    (srv.state / "steps.log").write_text("")                       # `rollback-all` leaves an empty record
    srv.set_world("s2")
    stub = srv.lab.root / "s2-stub.sh"
    stub.write_text(S2_STUB)
    log = srv.lab.root / "stub.log"
    log.write_text("")
    env = {"S2_SCRIPT": p(stub), "STUB_LOG": p(log), "R2_STATE_DIR": p(srv.state)}
    rc, out = srv.guard("rollback", srv.sha["s2"], env=env)
    must(rc == 7 and log.read_text().strip() == f"S2 STUB RAN: rollback {srv.sha['s2']}", f"after the full return: rc {rc}: {out[-300:]}")
    srv.set_world("s2", cloudflared={"networks": ["app"]})                     # but not while cloudflared is still off edge
    rc, out = srv.guard("rollback", srv.sha["s2"], env=env)
    must(rc == 2, f"cloudflared off edge after the file return: rc {rc}")
    return "after the three R2 returns (files at S2, empty record, S2 wiring) the guard hands the S2 return on; with cloudflared still off edge it refuses"


# ── the S2 guard must not skip what it cannot read ──────────────────────────────────────────────────
def injected(rc=1, stderr="", stdout=""):
    return {"rc": rc, "stderr": stderr, "stdout": stdout}


# how one `docker inspect` can go wrong: the daemon is unreachable, no permission, an odd status, an empty or a malformed answer
INSPECT_FAILURES = {
    "daemon unreachable": injected(1, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"),
    "permission denied": injected(1, "permission denied while trying to connect to the Docker daemon socket"),
    "status 125": injected(125, "docker: internal error"),
    "empty answer": injected(0, "", ""),
    "no separator": injected(0, "", "garbage\n"),
    "two separators": injected(0, "", "a|b|c\n"),
}


def guard_refusal(srv, what):
    """check, rollback and deploy all refuse with 2 and say they cannot verify; the S2 script never runs; nothing is written or changed."""
    env = guard_env(srv)
    before = (tree_digest(srv.dir, srv.state, srv.stub.parent / "s2-stub.sh"), srv.snapshot())
    for args in (("check",), ("rollback", srv.sha["s2"]), ("deploy", srv.sha["s2"])):
        rc, out = srv.guard(*args, env=env)
        must(rc == 2 and "REFUSED: cannot verify that R2 is not active" in out, f"{what} / {args[0]}: rc {rc}, wanted a refusal that says it cannot verify: {out[-500:]}")
    must(srv.stub_log.read_text() == "", f"{what}: the S2 script ran")
    must((tree_digest(srv.dir, srv.state, srv.stub.parent / "s2-stub.sh"), srv.snapshot()) == before, f"{what}: the guard changed something")
    return out


def t_guard_refuses_what_it_cannot_verify():
    """The finding on 8751bff: S2 files, no record, and a failed inspection used to give `check` exit 0 and let the S2 script run."""
    seen = []
    for container in ("edu_ai_nginx", "cloudflared"):
        for name, spec in INSPECT_FAILURES.items():
            srv = guard_server()
            srv.world_edit(lambda w, c=container, s=spec: w.setdefault("inspect_fail", {}).update({c: s}))
            out = guard_refusal(srv, f"{container} inspect: {name}")
            must(container in out, f"the refusal does not name {container}: {out[-300:]}")
            seen.append(f"{container}: {name}")
    srv = guard_server()
    srv.world_edit(lambda w: w.update({"daemon_down": True}))
    guard_refusal(srv, "the daemon is down")
    srv = guard_server()
    srv.world_edit(lambda w: w.update({"no_docker": True}))
    guard_refusal(srv, "no docker binary")
    # a failed inspect proves absence only together with a SUCCESSFUL list that lacks the container
    for name, spec in (("the list fails", injected(1, "Error response from daemon: list failed")), ("the list exits 125", injected(125, "", ""))):
        srv = guard_server()
        srv.world_edit(lambda w, s=spec: (w.setdefault("inspect_fail", {}).update({"edu_ai_nginx": injected(1, "Error: No such object: edu_ai_nginx")}), w.update({"ps_fail": s})))
        guard_refusal(srv, f"nginx looks absent but {name}")
    srv = guard_server()                                  # inspect says "no such object" but the list holds it: contradiction, nothing is proven
    srv.world_edit(lambda w: w.setdefault("inspect_fail", {}).update({"cloudflared": injected(1, "Error: No such object: cloudflared")}))
    guard_refusal(srv, "cloudflared looks absent but the list holds it")
    # the files and the state directory
    srv = guard_server()
    (srv.dir / "nginx" / "conf" / "nginx.conf").unlink()
    (srv.dir / "nginx" / "conf" / "nginx.conf").mkdir()                      # present but not readable as a file
    guard_refusal(srv, "nginx.conf is not a readable file")
    srv = guard_server()
    (srv.state / "steps.log").mkdir()
    guard_refusal(srv, "steps.log is not a readable file")
    srv = guard_server()
    shutil.rmtree(srv.state)
    srv.state.write_text("not a directory")
    guard_refusal(srv, "the state directory is not a directory")
    srv = guard_server()
    (srv.dir / "docker-compose.yml").unlink()
    (srv.dir / "docker-compose.yml").mkdir()
    rc, out = srv.guard("rollback", srv.sha["s2"], env=guard_env(srv))
    must(rc == 2 and srv.stub_log.read_text() == "", f"docker-compose.yml is a directory: rc {rc}: {out[-300:]}")
    return (f"{len(seen)} failed or malformed inspections (each of the two containers x 6 ways), a down daemon, no docker, a failed or contradicting container list, unreadable files and "
            "state directory: check, rollback and deploy refuse with 2 and `cannot verify`, the S2 script never runs, nothing is written")


def t_the_first_guard_accepted_what_it_could_not_read():
    """8751bff: the same cases made `check` answer 0 and `rollback` run the S2 script (reproduced here: the new tests would have caught it)."""
    seen = []
    for name, edit in (("nginx inspect fails", lambda w: w.setdefault("inspect_fail", {}).update({"edu_ai_nginx": INSPECT_FAILURES["daemon unreachable"]})),
                       ("cloudflared inspect fails", lambda w: w.setdefault("inspect_fail", {}).update({"cloudflared": INSPECT_FAILURES["permission denied"]})),
                       ("the daemon is down", lambda w: w.update({"daemon_down": True}))):
        srv = guard_server()
        srv.world_edit(edit)
        env = guard_env(srv)
        rc, out = srv.run(srv.lab.old_guard, "check", env=env)
        must(rc == 0, f"{name}: the first guard no longer answers 0 (rc {rc}): {out[-300:]}")
        rc, out = srv.run(srv.lab.old_guard, "rollback", srv.sha["s2"], env=env)
        must(rc == 7 and srv.stub_log.read_text().strip() == f"S2 STUB RAN: rollback {srv.sha['s2']}", f"{name}: the first guard did not run the S2 script: rc {rc}: {out[-300:]}")
        srv.stub_log.write_text("")
        new_rc, _ = srv.guard("rollback", srv.sha["s2"], env=env)
        must(new_rc == 2 and srv.stub_log.read_text() == "", f"{name}: the fixed guard does not refuse (rc {new_rc})")
        seen.append(name)
    return f"the first guard (8751bff) said `check` 0 and ran the S2 script for: {', '.join(seen)}; the fixed guard refuses each with 2 and does not run it"


def t_the_guard_still_allows_the_recovery_of_a_missing_container():
    """A container PROVEN absent (inspect fails, the list succeeds without it) is what the S2 return exists to recreate."""
    def gone(*names):
        return lambda w: [w["containers"].pop(n) for n in names]
    notes = []
    for label, edit in (("nginx is absent", gone("edu_ai_nginx")), ("cloudflared is absent", gone("cloudflared")), ("both are absent", gone("edu_ai_nginx", "cloudflared"))):
        srv = guard_server()
        srv.world_edit(edit)
        env = guard_env(srv)
        rc, out = srv.guard("check", env=env)
        must(rc == 0 and "proven absent" in out, f"{label}: check: rc {rc}: {out[-300:]}")
        rc, out = srv.guard("rollback", srv.sha["s2"], env=env)
        must(rc == 7 and srv.stub_log.read_text().strip() == f"S2 STUB RAN: rollback {srv.sha['s2']}", f"{label}: the S2 script was not run: rc {rc}: {out[-300:]}")
        notes.append(label)
    # absence is not a licence while another sign is there, or while the other container cannot be read
    srv = guard_server()
    srv.world_edit(lambda w: (w["containers"].pop("edu_ai_nginx"), w["containers"]["cloudflared"].update({"networks": ["app", "tunnel", "tunnel_egress"]})))
    rc, out = srv.guard("rollback", srv.sha["s2"], env=guard_env(srv))
    must(rc == 2 and "R2 is active" in out and srv.stub_log.read_text() == "", f"nginx absent, cloudflared on the tunnel: rc {rc}: {out[-300:]}")
    srv = guard_server()
    srv.world_edit(lambda w: (w["containers"].pop("edu_ai_nginx"), w.setdefault("inspect_fail", {}).update({"cloudflared": INSPECT_FAILURES["daemon unreachable"]})))
    rc, out = srv.guard("rollback", srv.sha["s2"], env=guard_env(srv))
    must(rc == 2 and "cannot verify" in out and "cloudflared" in out and srv.stub_log.read_text() == "", f"nginx absent, cloudflared unreadable: rc {rc}: {out[-300:]}")
    srv = guard_server(files="step2")
    srv.world_edit(gone("edu_ai_nginx", "cloudflared"))
    rc, out = srv.guard("rollback", srv.sha["s2"], env=guard_env(srv))
    must(rc == 2 and "R2 is active" in out, f"both absent but the checkout carries step 2: rc {rc}")
    return (f"{', '.join(notes)}: the S2 return is handed on (exit 7 of the stub) and `check` says `proven absent`; but an absent nginx next to cloudflared on the tunnel, "
            "an unreadable cloudflared or a checkout at step 2 still refuse")


TESTS = [
    ("r2switch.delta_on_real_models", "R2-R4: the exact change of step 2 on models rendered by the real Compose; every other change is named by path", t_delta_function_on_real_models),
    ("r2switch.preflight_exact_step", "R2-R4: preflight accepts the designed step 2 and refuses a port, mount, command, privilege, environment, another service ...", t_preflight_accepts_the_designed_step_and_refuses_everything_else),
    ("r2switch.first_script_accepted", "R2-R4: the first reviewed script accepted a changed port, privileged and a mount (reproduced and caught)", t_first_script_accepted_the_forbidden_changes),
    ("r2switch.commit_scope", "R2-R4: the whole commit and the whole fast-forward are limited to the step, docs, tools and tests", t_commit_and_range_scope),
    ("r2switch.return_inputs", "R2-R5: a wrong step, the tools commit, another step's commit, a record mismatch or a damaged record is refused before any change", t_rollback_inputs_are_refused_before_any_change),
    ("r2switch.first_script_reverted", "R2-R5: the first script reverted the tools commit and returned 0 for step 99 (reproduced and caught)", t_first_script_reverted_the_tools_commit),
    ("r2switch.rollback_step_guard", "R2-R5: rollback_step refuses an invalid step", t_rollback_step_refuses_an_invalid_step),
    ("r2switch.valid_returns", "R2-R5: a return from the record, a repeated return, an interrupted one and a retry commit stay possible", t_valid_returns_are_accepted),
    ("r2switch.step_from", "the step a commit shows and the step a checkout shows are one function", t_step_from_agrees_between_tree_and_checkout),
    ("s2guard.refuses", "S2 guard: every R2 sign, each alone, refuses the S2 return and names what to do first", t_guard_refuses_each_r2_sign_alone),
    ("s2guard.passes_through", "S2 guard: with R2 not active the unchanged S2 script runs with the same arguments; the guard writes nothing", t_guard_runs_the_unchanged_s2_script_when_r2_is_not_active),
    ("s2guard.read_only_and_inputs", "S2 guard: read-only commands pass, bad inputs are refused", t_guard_read_only_commands_pass_while_r2_is_active_and_other_inputs_fail),
    ("s2guard.after_full_return", "S2 guard: after the full R2 return the S2 return is handed on", t_guard_allows_the_s2_return_after_the_full_r2_return),
    ("s2guard.unverifiable_refuses", "S2 guard: a failed or malformed inspection, a failed list, an unreadable file or a down daemon refuses before the S2 script runs", t_guard_refuses_what_it_cannot_verify),
    ("s2guard.first_version", "S2 guard: the first version (8751bff) accepted a failed inspection (reproduced and caught)", t_the_first_guard_accepted_what_it_could_not_read),
    ("s2guard.absent_recovery", "S2 guard: a container proven absent does not block the S2 return that recreates it", t_the_guard_still_allows_the_recovery_of_a_missing_container),
]


def run_all(quiet=False):
    results = []
    for tid, desc, fn in TESTS:
        try:
            detail, status = fn(), "PASS"
        except AssertionError as e:
            detail, status = str(e), "FAIL"
        except Exception as e:  # noqa: BLE001
            detail, status = f"{type(e).__name__}: {e}", "ERROR"
        results.append((tid, desc, status, detail))
        if not quiet:
            print(f"  {status:5} {tid}  {desc}\n        {detail}", flush=True)
    return results


if __name__ == "__main__":
    res = run_all()
    bad = [r for r in res if r[2] != "PASS"]
    print(f"\n{len(res) - len(bad)}/{len(res)} PASS")
    sys.exit(1 if bad else 0)
