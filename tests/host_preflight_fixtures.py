"""Fakes for tests/test_host_preflight.py: a Host that never touches Docker, a Compose model and a synthetic tree.

Nothing here runs a real command. FakeHost goes through the real whitelist (Host.run), so a test that makes the code issue
a command off the list fails, and every issued command is kept in `calls`.
"""
import dataclasses
import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "scripts"))

import eduai_layout as L  # noqa: E402
import host_preflight as H  # noqa: E402
import install  # noqa: E402

# Distinct values that must never appear in any output; the tests search for them by name.
SENTINELS = {"POSTGRES_PASSWORD": "SENTINEL-pg-0001", "PGADMIN_DEFAULT_PASSWORD": "SENTINEL-pga-0002",
             "LANGFLOW_SUPERUSER_PASSWORD": "SENTINEL-lfsu-0003", "OPENAI_API_KEY": "SENTINEL-openai-0004",
             "CLOUDFLARE_TUNNEL_TOKEN": "SENTINEL-cf-0005"}
SECRET_KEY = "SENTINEL-fernet-key-0006="
SUBNET = "10.77.3.0/29"
GIB = 1024 ** 3

SERVICES = {   # service: (container_name, image, built here, [(published, host_ip)])
    "nginx": ("edu_ai_nginx", "nginx:1.30.5-alpine", False, [("80", ""), ("8443", ""), ("8444", "")]),
    "edu_ai_api": ("edu_ai_api", "edu_ai_api:1.0", True, []),
    "edu_ai": ("edu_ai", "edu_ai:1.1.0", True, []),
    "bifrost": ("bifrost", "maximhq/bifrost:v2.2.6", False, [("8099", "127.0.0.1")]),
    "cloudflared": ("cloudflared", "cloudflare/cloudflared:2026.8.1", False, []),
    "postgres": ("edu_ai_postgres", "edu_ai_postgres:cs", True, []),
    "pgadmin": ("edu_ai_pgadmin", "dpage/pgadmin4:9.18.0", False, []),
    "edu_ai_search": ("edu_ai_search", "edu_ai_search:1.0", True, []),
    "edu_ai_web_chat": ("edu_ai_web_chat", "edu_ai_web_chat:0.1", True, []),
    "edu_ai_qa_admin": ("edu_ai_qa_admin", "edu_ai_qa_admin:0.1", True, []),
}
NETWORKS = ("edge", "app", "data", "bifrost_egress", "tunnel", "tunnel_egress")
VOLUMES = ("postgres_data", "pgadmin_data", "reranker_models", "qa_admin_state")


def make_model(project="edu_ai", subnet=SUBNET):
    """What `docker compose config --format json` renders for docker-compose.yml, as far as the tool reads it."""
    services = {}
    for service, (cname, image, built, ports) in SERVICES.items():
        spec = {"container_name": cname, "image": image,
                "environment": {"POSTGRES_PASSWORD": SENTINELS["POSTGRES_PASSWORD"], "LANGFLOW_SECRET_KEY": SECRET_KEY}}
        if built:
            spec["build"] = {"context": "/src", "dockerfile": "Dockerfile"}
        if ports:
            spec["ports"] = [{"mode": "ingress", **({"host_ip": ip} if ip else {}), "target": int(pub), "published": pub,
                              "protocol": "tcp"} for pub, ip in ports]
        services[service] = spec
    networks = {key: {"name": f"{project}_{key}", "driver": "bridge"} for key in NETWORKS}
    networks["tunnel"]["internal"] = True
    networks["tunnel"]["ipam"] = {"config": [{"subnet": subnet}]}
    return {"name": project, "services": services, "networks": networks,
            "volumes": {key: {"name": f"{project}_{key}"} for key in VOLUMES},
            "configs": {"tunnel_trust": {"name": f"{project}_tunnel_trust", "content": f"set_real_ip_from {subnet};\n"}}}


def ss_text(*listeners):
    """`ss -H -ltn` lines for (address, port) pairs."""
    lines = []
    for addr, port in listeners:
        local = f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"
        lines.append(f"LISTEN 0      4096   {local:>24}      {'[::]:*' if ':' in addr else '0.0.0.0:*'}")
    return "\n".join(lines) + ("\n" if lines else "")


class World:
    """What the fake daemon and host answer. Every field can be replaced by a test; `override` wins over everything."""

    def __init__(self, project="edu_ai"):
        self.project = project
        self.uname = "x86_64\n"
        self.info = {"ServerVersion": "29.6.2", "OSType": "linux", "Architecture": "x86_64", "DockerRootDir": "/var/lib/docker",
                     "OperatingSystem": "Ubuntu 24.04.4 LTS", "ClientInfo": {"Context": "default"}}
        self.compose_version = "5.3.1\n"
        self.model = make_model(project)
        self.containers, self.volumes, self.networks, self.images = [], [], [], []
        self.ss = ss_text(("0.0.0.0", 22), ("::", 22), ("127.0.0.53%lo", 53))
        self.tunnel_check = Proc(0, "OK: free in everything this tool can see\n", "")
        self.allocate = Proc(0, f"EDU_AI_TUNNEL_SUBNET={SUBNET}\n", "")
        self.override = {}                                # key -> Proc | Exception | callable(argv) -> Proc

    def proc(self, key, argv):
        if key in self.override:
            value = self.override[key]
            if callable(value):
                value = value(argv)
            if isinstance(value, BaseException):
                raise value
            return value
        if key == "uname":
            return Proc(0, self.uname, "")
        if key == "info":
            return Proc(0, json.dumps(self.info), "")
        if key == "compose_version":
            return Proc(0, self.compose_version, "")
        if key == "buildx":
            return Proc(0, "github.com/docker/buildx v0.37.2\n", "")
        if key == "config":
            return Proc(0, json.dumps(self.model), "")
        if key == "ss":
            return Proc(0, self.ss, "")
        if key == "tunnel_check":
            return self.tunnel_check
        if key == "tunnel_allocate":
            return self.allocate
        rows = {"ps": self.containers, "volume": self.volumes, "network": self.networks, "image": self.images}[key]
        return Proc(0, "".join(json.dumps(row) + "\n" for row in rows), "")


Proc = H.Proc


def key_of(argv):
    if argv[0] == "uname":
        return "uname"
    if argv[0] == "ss":
        return "ss"
    if argv[0] == sys.executable:
        return "tunnel_" + argv[2]
    if "config" in argv:
        return "config"
    if argv[1] == "buildx":
        return "buildx"
    return {"info": "info", "version": "compose_version", "ps": "ps", "volume": "volume", "network": "network",
            "image": "image"}[argv[2] if argv[1] == "compose" else argv[1]]


class FakeHost(H.Host):
    """Host with the real whitelist and the real read-only file system, but every command answered by a World."""

    def __init__(self, world=None, *, env=None, plat="linux", fake_modes=None):
        self.world = world or World()
        self.calls = []
        self.env = {"PATH": "/usr/bin", "HOME": "/root"} if env is None else env
        self.plat = plat
        self.fake_modes = (os.name != "posix") if fake_modes is None else fake_modes
        self.modes = {}          # path -> (mode bits, uid); used only when fake_modes
        self.disk = {}           # str(path) -> bytes available | Exception
        self.default_disk = 100 * GIB
        self.read_hooks = []     # callables (path, nth_read) -> bytes | None, for races

    def _exec(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        return self.world.proc(key_of(argv), argv)

    def platform(self):
        return self.plat

    def environ(self):
        return dict(self.env)

    def posix(self):
        return True

    def uid(self):
        return os.geteuid() if hasattr(os, "geteuid") else 1000

    def lstat(self, path):
        real = os.lstat(path)
        if not self.fake_modes:
            return real
        mode, uid = self.modes.get(str(path), (None, None))
        if mode is None:
            mode = 0o700 if stat.S_ISDIR(real.st_mode) else 0o600
        return os.stat_result((stat.S_IFMT(real.st_mode) | mode, real.st_ino, real.st_dev, real.st_nlink,
                               self.uid() if uid is None else uid, real.st_gid, real.st_size, int(real.st_atime),
                               int(real.st_mtime), int(real.st_ctime)))

    def read_bytes(self, path, limit):
        self.reads = getattr(self, "reads", {})
        self.reads[str(path)] = self.reads.get(str(path), 0) + 1
        for hook in self.read_hooks:
            out = hook(str(path), self.reads[str(path)])
            if out is not None:
                return out
        return super().read_bytes(path, limit)

    def disk_free(self, path):
        value = self.disk.get(str(path), self.default_disk)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeActions(H.Actions):
    """Records every write; `fail` maps an operation name to an exception raised instead of doing it."""

    def __init__(self):
        self.log, self.fail, self.bootstrap_calls, self.bootstrap_rc = [], {}, [], 0

    def _do(self, name, *args):
        self.log.append((name, *args))
        if name in self.fail:
            raise self.fail[name]

    def mkdir(self, path, mode):
        self._do("mkdir", str(path), mode)
        super().mkdir(path, mode)

    def symlink(self, target, path):
        self._do("symlink", str(target), str(path))
        super().symlink(target, path)

    def write_env(self, path, data):
        self._do("write_env", str(path))
        super().write_env(path, data)

    def bootstrap(self, argv, cwd, env):
        H.check_bootstrap_argv(argv)                        # the real validation, then record instead of launching
        self.bootstrap_calls.append((list(argv), cwd, dict(env)))
        return self.bootstrap_rc


def can_symlink():
    with tempfile.TemporaryDirectory() as td:
        try:
            os.symlink(td, os.path.join(td, "probe"))
            return True
        except (OSError, NotImplementedError):
            return False


SYMLINKS = can_symlink()
POSIX = os.name == "posix"


class Tree(unittest.TestCase):
    """A synthetic installation: checkout, external configuration set, state directory. Never real data."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        base = pathlib.Path(os.path.realpath(self._td.name))
        self.src, self.cfg, self.state = base / "src", base / "cfg", base / "state"
        for d in (self.src, self.cfg, self.state):
            d.mkdir()
        (self.src / "docker-compose.yml").write_bytes((REPO / "docker-compose.yml").read_bytes())
        (self.src / "docker-compose.override.yml").write_bytes((REPO / "docker-compose.override.yml").read_bytes())
        (self.src / ".env.example").write_text("A=1\n", newline="\n")
        self.env_values = {"COMPOSE_PROJECT_NAME": "edu_ai", "COMPOSE_FILE": "docker-compose.yml", "EDU_AI_TUNNEL_SUBNET": SUBNET,
                           "LANGFLOW_SECRET_KEY": SECRET_KEY, **SENTINELS}
        self.write_set()
        self.world, self.host, self.actions = World(), None, FakeActions()
        self.host = FakeHost(self.world)

    def write_set(self, env_text=None, record=None):
        text = env_text if env_text is not None else "".join(f"{k}={v}\n" for k, v in self.env_values.items())
        rec = record if record is not None else {
            "schema": install.RECORD_SCHEMA, "langflow_secret_key_sha256": hashlib.sha256(SECRET_KEY.encode()).hexdigest(),
            "blockers": ["HOST_NOT_CHECKED", "MISSING_MANUAL:OPENAI_API_KEY"]}
        (self.cfg / ".env").write_bytes(text.encode())
        (self.cfg / "install-record.json").write_text(json.dumps(rec), newline="\n")
        if POSIX:
            os.chmod(self.cfg, 0o700)
            os.chmod(self.cfg / ".env", 0o600)
            os.chmod(self.cfg / "install-record.json", 0o600)

    def layout(self, *extra, state=True, environ=None):
        import argparse
        parser = argparse.ArgumentParser()
        L.add_arguments(parser)
        argv = ["--source-root", str(self.src), "--config-dir", str(self.cfg)] + (["--state-dir", str(self.state)] if state else []) + list(extra)
        return L.resolve(parser.parse_args(argv), environ or {}, self.src)

    def check(self, *, opts=None, host=None, layout=None):
        opts = opts or H.Options(accept_unmeasured_pools=True)
        return H.Preflight(layout or self.layout(), host or self.host, opts).run()

    @staticmethod
    def level(report, code):
        return [i.level for i in report.items if i.code == code]

    def snapshot(self, root):
        """Every name under root with its kind, link target or content hash: a before/after comparison."""
        out = {}
        for path in sorted(pathlib.Path(root).rglob("*")):
            rel = str(path.relative_to(root))
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("dir",)
            else:
                out[rel] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        return out
