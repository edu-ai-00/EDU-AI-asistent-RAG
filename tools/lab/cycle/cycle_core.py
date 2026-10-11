"""Backup / update / restore driver for ONE fixed EDU-AI lab layout.

Lab driver, not a general updater and not for production or school installations. It refuses any
target it cannot prove is its own stack, checks a backup completely before touching anything, and
starts the applications only in a separate step that needs the success marker of the restore.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tarfile
import time

FORMAT = "cycle-bundle/1"
DBS = ("edu_ai", "langflow")
STATE_LABELS = ("uploads", "langflow_data", "sqllite_export", "bifrost_data")
VOLUME_LABELS = ("pgadmin_data", "qa_admin_state")
CACHE_VOLUMES = ("reranker_models",)
FILE_LABELS = ("config", "src", "pgadmin_exports") + STATE_LABELS + VOLUME_LABELS
EGRESS_NETWORKS = ("edge", "bifrost_egress")
EGRESS_TAG = "cycle-egress"
PATH_RE = re.compile(r"^/[A-Za-z0-9._@+/_-]+$")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
TOOL_FLAGS = ["--rm", "--network", "none", "--user", "0", "--security-opt", "no-new-privileges"]
REQUIRED_BUNDLE_FILES = (
    ["MANIFEST.json", "images.tar", "images.json", "pg/globals.sql", "pg/databases.txt", "pg/settings.txt",
     "pg/roles_summary.txt", "pg/role_verify.txt", "meta/identity.json", "meta/classification.json"]
    + [f"pg/{d}.dump" for d in DBS] + [f"pg/schema_{d}.sql" for d in DBS] + [f"pg/fp_{d}.txt" for d in DBS]
    + [f"files/{lab}.tgz" for lab in FILE_LABELS] + [f"files/{lab}.manifest.txt" for lab in FILE_LABELS])
MANIFEST_CMD = (r"cd /s && find . {prune} -printf '%y|%m|%u:%g|%U:%G|%s|%T@|%p|%l\n' | LC_ALL=C sort; echo '#HASHES'; "
                r"find . {prune} -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum")


class Abort(Exception):
    """A check failed: nothing further is done (and nothing is destroyed if it happened early)."""


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}


def run_text(argv, *, input=None, check=True, env=None, cwd=None, timeout=None):
    try:
        r = subprocess.run(argv, input=input, capture_output=True, text=True, env=env or clean_env(), cwd=cwd, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Abort("command unavailable (type=%s)" % type(exc).__name__) from None
    if check and r.returncode != 0:
        # Arguments, output and exception messages can contain credentials or
        # restored row values. Only the result code crosses the evidence boundary.
        raise Abort("command failed (rc=%d)" % r.returncode)
    return r


def run_stream(argv, *, stdin=None, stdout=None, check=True, env=None):
    try:
        r = subprocess.run(argv, stdin=stdin, stdout=stdout if stdout is not None else subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env or clean_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Abort("command unavailable (type=%s)" % type(exc).__name__) from None
    if check and r.returncode != 0:
        raise Abort("command failed (rc=%d)" % r.returncode)
    return r


def identifier(value: str, label: str) -> str:
    """One bounded path segment, also safe in marker SQL and shell arguments."""
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise Abort("unsafe %s identifier" % label)
    return value


def count_value(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def layout_identity(L) -> dict:
    return {key: getattr(L, key) for key in ("host", "project", "src", "cfg", "state")}


def bundle_path(L, name: str) -> str:
    return os.path.join(L.backup_root, identifier(name, "bundle"))


def invalidate_marker(ev) -> None:
    marker = os.path.join(ev.dir, "restore_ok.json")
    if os.path.lexists(marker):
        os.remove(marker)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Evidence:
    """Anonymised step log: names, counts, hashes only. Written next to a JSON result."""

    def __init__(self, directory: str, name: str):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.dir, self.name, self.t0 = directory, name, time.time()
        self.steps: dict = {}
        self.extra: dict = {}

    def mark(self, msg: str):
        line = "%s +%ds %s" % (time.strftime("%H:%M:%S", time.gmtime()), int(time.time() - self.t0), msg)
        print(line, flush=True)
        with open(os.path.join(self.dir, self.name + ".timeline.txt"), "a") as f:
            f.write(line + "\n")

    def step(self, name: str, ok: bool, detail: str = "", fatal: bool = True):
        self.steps[name] = {"result": "PASS" if ok else "FAIL", "detail": detail, "at": now()}
        self.mark("%-38s %s %s" % (name, "PASS" if ok else "FAIL", detail))
        if not ok and fatal:
            self.save()
            raise Abort("%s failed: %s" % (name, detail))

    def save(self):
        with open(os.path.join(self.dir, self.name + ".json"), "w") as f:
            json.dump({"steps": self.steps, **self.extra}, f, indent=1)


@dataclasses.dataclass(frozen=True)
class Layout:
    src: str
    cfg: str
    state: str
    project: str
    host: str
    backup_root: str
    docker_root: str = "/var/lib/docker"
    pg_container: str = "edu_ai_postgres"
    pg_admin: str = "edu_ai_admin"

    @property
    def env_path(self) -> str:
        return self.cfg + "/.env"


def validate_paths(L: Layout) -> None:
    named = {"src": L.src, "cfg": L.cfg, "state": L.state, "backup_root": L.backup_root}
    for k, p in named.items():
        if not PATH_RE.match(p) or posixpath.normpath(p) != p or p.count("/") < 3:
            raise Abort("unsafe or non-canonical path: %s" % k)
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", L.project):
        raise Abort("unsafe project name")
    items = list(named.items())
    for i, (ka, a) in enumerate(items):
        for kb, b in items[i + 1:]:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                raise Abort("paths overlap or nest: %s / %s" % (ka, kb))


def dc(L: Layout, src: str | None = None) -> list:
    s = src or L.src
    return ["docker", "compose", "-p", L.project, "--project-directory", s, "--env-file", L.env_path, "-f", s + "/docker-compose.yml"]


def compose(L: Layout, *args, src=None, check=True, timeout=None):
    return run_text(dc(L, src) + list(args), cwd=src or L.src, check=check, timeout=timeout)


def services(L: Layout, src=None) -> list:
    return [s for s in compose(L, "config", "--services", src=src).stdout.split() if s != "cloudflared"]


def read_env(path: str) -> dict:
    d = {}
    for line in slurp(path).splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def slurp(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- identity gate

def containers(L: Layout) -> list:
    out = run_text(["docker", "ps", "-a", "--no-trunc", "--format",
                    '{{.ID}}|{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.service"}}']).stdout
    rows = []
    for line in out.splitlines():
        cid, name, proj, wd, svc = line.split("|")
        rows.append({"id": cid, "name": name, "project": proj, "wd": wd, "service": svc})
    return rows


def gate(L: Layout, *, expect_wd: str | None = None, allow_foreign: bool = False, need_stack: bool = True) -> dict:
    """Prove that host, engine, paths and containers are the layout's own; return identity (no secrets)."""
    validate_paths(L)
    if os.uname().nodename != L.host:
        raise Abort("hostname %s is not the expected host" % os.uname().nodename)
    for v in ("DOCKER_HOST", "DOCKER_CONTEXT"):
        if os.environ.get(v):
            raise Abort("%s is set" % v)
    if run_text(["docker", "context", "show"]).stdout.strip() != "default":
        raise Abort("docker context is not default")
    name, root = run_text(["docker", "info", "--format", "{{.Name}}|{{.DockerRootDir}}"]).stdout.strip().split("|")
    if name != L.host or root != L.docker_root:
        raise Abort("docker engine is not this host's default engine")
    for p in (L.src, L.cfg, L.state):
        if not os.path.isdir(p) or os.path.realpath(p) != p:
            raise Abort("not an existing canonical directory: %s" % p)
    if os.path.realpath(L.backup_root) != L.backup_root:
        raise Abort("backup root is not canonical")
    if not os.path.isfile(L.src + "/docker-compose.yml") or not os.path.isfile(L.env_path):
        raise Abort("compose file or .env missing")
    env_link = L.src + "/.env"
    if not os.path.islink(env_link) or os.readlink(env_link) != L.env_path:
        raise Abort("src/.env is not a link to the config .env")
    for d in STATE_LABELS:
        link = "%s/%s" % (L.src, d)
        if not os.path.islink(link) or os.readlink(link) != "%s/%s" % (L.state, d):
            raise Abort("link %s does not point to the state directory" % d)
    cs = containers(L)
    foreign = [c["name"] for c in cs if c["project"] != L.project]
    if foreign and not allow_foreign:
        raise Abort("foreign containers on this engine: %d" % len(foreign))
    own = [c for c in cs if c["project"] == L.project]
    if need_stack and not own:
        raise Abort("no container of project %s" % L.project)
    want_wd = expect_wd or L.src
    if not PATH_RE.fullmatch(want_wd) or not os.path.isdir(want_wd) or os.path.realpath(want_wd) != want_wd:
        raise Abort("container working directory is not canonical")
    roots = [L.src, L.cfg, L.state, want_wd]
    for c in own:
        if c["wd"] != want_wd:
            raise Abort("container %s was created from another directory" % c["name"])
        mounts = run_text(["docker", "inspect", c["id"], "--format", "{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Source}} {{end}}"]).stdout.split()
        for m in mounts:
            typ, vname, src = m.split("|")
            if typ == "bind":
                real = os.path.realpath(src)
                if not any(real == r or real.startswith(r + "/") for r in roots):
                    raise Abort("container %s mounts a path outside the stack: %s" % (c["name"], real))
            elif typ == "volume" and not vname.startswith(L.project + "_"):
                raise Abort("container %s mounts a foreign volume" % c["name"])
    return {"host": L.host, "project": L.project, "containers": len(own), "foreign": len(foreign), "wd": want_wd}


# ---------------------------------------------------------------- docker helpers

def image_ids(L: Layout) -> dict:
    """Container name -> image ID, plus tag -> ID for every image used by the stack."""
    out = {}
    for c in containers(L):
        if c["project"] != L.project:
            continue
        info = run_text(["docker", "inspect", c["id"], "--format", "{{.Config.Image}}|{{.Image}}|{{.State.Status}}|{{.State.StartedAt}}"]).stdout.strip().split("|")
        out[c["name"].lstrip("/")] = {"tag": info[0], "id": info[1], "status": info[2], "started": info[3], "service": c["service"]}
    return out


def tool_image(L: Layout) -> str:
    return run_text(["docker", "inspect", L.pg_container, "--format", "{{.Image}}"]).stdout.strip()


def pg_psql(L: Layout, db: str, sql=None, stdin=None, check=True) -> str:
    argv = ["docker", "exec", "-i", L.pg_container, "psql", "-X", "-At", "-U", L.pg_admin, "-d", db, "-v", "ON_ERROR_STOP=1"]
    argv += ["-c", sql] if sql is not None else ["-f", "-"]
    return run_text(argv, input=stdin, check=check).stdout.strip()


def fingerprint(L: Layout, db: str, sql_path: str) -> str:
    script = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n" + slurp(sql_path) + "\nCOMMIT;\n"
    return run_text(["docker", "exec", "-i", L.pg_container, "psql", "-X", "-q", "-U", L.pg_admin, "-d", db, "-v", "ON_ERROR_STOP=1", "-f", "-"],
                    input=script).stdout


ROLES_SQL = """
select 'role|'||rolname||'|'||rolsuper||'|'||rolinherit||'|'||rolcreaterole||'|'||rolcreatedb||'|'||rolcanlogin||'|'||rolreplication||'|'||rolbypassrls||'|'||rolconnlimit||'|'||coalesce(rolvaliduntil::text,'')||'|'||(rolpassword is not null) from pg_authid order by rolname;
select 'member|'||r.rolname||'|'||m.rolname||'|'||g.admin_option||'|'||g.inherit_option||'|'||g.set_option from pg_auth_members g join pg_roles r on r.oid=g.roleid join pg_roles m on m.oid=g.member order by 1;
select 'setting|'||coalesce(d.datname,'-')||'|'||coalesce(r.rolname,'-')||'|'||s.setconfig::text from pg_db_role_setting s left join pg_database d on d.oid=s.setdatabase left join pg_roles r on r.oid=s.setrole order by 1;
select 'db|'||datname||'|'||pg_get_userbyid(datdba)||'|'||pg_encoding_to_char(encoding)||'|'||datcollate||'|'||datctype||'|'||coalesce(datacl::text,'')||'|'||coalesce(shobj_description(oid,'pg_database'),'') from pg_database where datname not like 'template%' order by datname;
"""


def roles_summary(L: Layout) -> str:
    return pg_psql(L, "postgres", stdin=ROLES_SQL) + "\n"


def tool_run(image: str, mount: str, script: str, *, stdin=None, stdout=None, ro=True, extra=()):
    argv = ["docker", "run"] + (["-i"] if stdin else []) + TOOL_FLAGS + ["-v", "%s:/s%s" % (mount, ":ro" if ro else "")] + list(extra) \
        + ["--entrypoint", "", image, "sh", "-c", script]
    if stdin is not None or stdout is not None:
        return run_stream(argv, stdin=stdin, stdout=stdout)
    return run_text(argv)


def manifest_text(image: str, mount: str, exclude: str | None = None) -> str:
    prune = "-path ./%s -prune -o" % exclude if exclude else ""
    return tool_run(image, mount, MANIFEST_CMD.format(prune=prune)).stdout


def norm_manifest(text: str) -> list:
    """Compare form (sorted): directory sizes and non-file mtimes are filesystem artefacts, and the sub-second mtime
    that the raw sort order depends on is not preserved by tar."""
    out = []
    for line in text.splitlines():
        p = line.split("|")
        if len(p) >= 8 and p[0] in "dfl":
            p[5] = p[5].split(".")[0] if p[0] == "f" else "-"
            if p[0] == "d":
                p[4] = "-"
        out.append("|".join(p))
    return sorted(out)


def tar_to_file(image: str, mount: str, dest: str, exclude: str | None = None) -> None:
    ex = " --exclude=./%s" % exclude if exclude else ""
    with open(dest, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        tool_run(image, mount, "tar --numeric-owner -p%s -czf - -C /s ." % ex, stdout=f)


def untar_from_file(image: str, mount: str, src: str) -> None:
    with open(src, "rb") as f:
        tool_run(image, mount, "tar --numeric-owner -xpzf - -C /s", stdin=f, ro=False)


def archive_manifest(image: str, src: str, exclude: str | None = None) -> str:
    """Manifest of what extraction of the archive yields (the target will see exactly this)."""
    prune = "-path ./%s -prune -o" % exclude if exclude else ""
    script = "mkdir /s && tar --numeric-owner -xpzf - -C /s && " + MANIFEST_CMD.format(prune=prune)
    argv = ["docker", "run", "-i"] + TOOL_FLAGS + ["--entrypoint", "", image, "sh", "-c", script]
    with open(src, "rb") as f:
        r = run_stream(argv, stdin=f, stdout=subprocess.PIPE)
    return r.stdout.decode(errors="replace")


# ---------------------------------------------------------------- pure comparisons

ACL_RE = re.compile(r"\{([^{}]*=[^{}]*)\}")


def norm_acl(line: str) -> str:
    """ACL arrays are sets: the order of their elements depends on the order of the GRANTs, not on the privileges."""
    return ACL_RE.sub(lambda m: "{" + ",".join(sorted(m.group(1).split(","))) + "}", line)


def schema_dump_equal(a: str, b: str) -> bool:
    """pg_dump -s texts: identical except that GRANT/REVOKE statements are compared as a set (their order follows ACL order)."""
    def split(x):
        lines = x.splitlines()
        acl = sorted(l for l in lines if l.startswith(("GRANT ", "REVOKE ")))
        return [l for l in lines if not l.startswith(("GRANT ", "REVOKE "))], acl
    return split(a) == split(b)


def parse_fp(text: str):
    t, q, lo, s = {}, {}, None, set()
    for line in text.splitlines():
        if line.startswith("T|"):
            _, n, c, h = line.split("|", 3)
            t[n] = (int(c), h)
        elif line.startswith("Q|"):
            _, n, v = line.split("|", 2)
            q[n] = v
        elif line.startswith("L|"):
            lo = line
        elif line.startswith("S|"):
            s.add(norm_acl(line))
    return t, q, lo, s


def compare_fp(base: str, now_: str) -> dict:
    a, b = parse_fp(base), parse_fp(now_)
    only_a, only_b = sorted(set(a[0]) - set(b[0])), sorted(set(b[0]) - set(a[0]))
    diff = sorted(k for k in set(a[0]) & set(b[0]) if a[0][k] != b[0][k])
    qdiff = sorted(k for k in set(a[1]) | set(b[1]) if a[1].get(k) != b[1].get(k))
    ia, ib = sorted(a[3] - b[3]), sorted(b[3] - a[3])
    r = {"tables_total": len(a[0]), "tables_equal": len(a[0]) - len(diff) - len(only_a), "tables_only_in_baseline": only_a,
         "tables_only_in_target": only_b, "tables_different": diff, "rows_total": sum(v[0] for v in a[0].values()),
         "sequences_total": len(a[1]), "sequences_equal": len(a[1]) - len(qdiff), "sequences_different": qdiff,
         "large_objects_equal": a[2] == b[2], "schema_items_total": len(a[3]), "schema_items_equal": len(a[3]) - len(ia),
          "schema_items_only_in_baseline": [hashlib.sha256(x.encode()).hexdigest() for x in ia[:20]],
          "schema_items_only_in_target": [hashlib.sha256(x.encode()).hexdigest() for x in ib[:20]],
         "schema_fingerprint_baseline": hashlib.sha256("\n".join(sorted(a[3])).encode()).hexdigest(),
         "schema_fingerprint_target": hashlib.sha256("\n".join(sorted(b[3])).encode()).hexdigest()}
    r["ok"] = not (only_a or only_b or diff or qdiff or ia or ib) and r["large_objects_equal"] and bool(a[0])
    return r


# ---------------------------------------------------------------- bundle verification (no docker)

def read_sums(path: str) -> dict:
    sums = {}
    for line in slurp(path).splitlines():
        h, _, rel = line.partition("  ")
        if not re.fullmatch(r"[0-9a-f]{64}", h) or not rel:
            raise Abort("malformed SHA256SUMS line")
        sums[rel] = h
    return sums


def check_tar_members(path: str) -> int:
    """Refuse absolute names, '..', links out of the tree, devices and writes through a symlink."""
    links, n = [], 0
    with tarfile.open(path, "r:gz") as t:
        members = t.getmembers()
    for m in members:
        n += 1
        name = m.name
        if name.startswith("/") or ".." in name.split("/"):
            raise Abort("unsafe member name in %s" % os.path.basename(path))
        if m.isdev() or m.isfifo() or m.islnk():
            raise Abort("device, fifo or hard link in %s" % os.path.basename(path))
        if m.issym():
            links.append(posixpath.normpath(name))
    for m in members:
        p = posixpath.normpath(m.name)
        if any(p.startswith(l + "/") for l in links):
            raise Abort("member written through a symlink in %s" % os.path.basename(path))
    return n


def verify_bundle(path: str, *, check_hashes: bool = True, expect_project: str | None = None) -> dict:
    """Completeness, integrity and safety of a bundle. Raises Abort before anything is changed."""
    if not os.path.isdir(path) or os.path.islink(path):
        raise Abort("bundle directory missing")
    if os.stat(path).st_mode & 0o077:
        raise Abort("bundle directory is accessible to group/others")
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            if os.path.islink(os.path.join(root, n)):
                raise Abort("symlink inside the bundle")
    missing = [f for f in REQUIRED_BUNDLE_FILES if not os.path.isfile(os.path.join(path, f)) or os.path.getsize(os.path.join(path, f)) == 0]
    if missing:
        raise Abort("bundle incomplete, missing/empty: %s" % ", ".join(missing[:6]))
    if not os.path.isfile(os.path.join(path, "SHA256SUMS")):
        raise Abort("SHA256SUMS missing")
    man = json.loads(slurp(os.path.join(path, "MANIFEST.json")))
    if man.get("format") != FORMAT or man.get("status") != "COMPLETE":
        raise Abort("bundle is not a COMPLETE %s" % FORMAT)
    if expect_project and man.get("layout", {}).get("project") != expect_project:
        raise Abort("bundle belongs to another project")
    sums = read_sums(os.path.join(path, "SHA256SUMS"))
    on_disk = set()
    for root, _, files in os.walk(path):
        for n in files:
            rel = os.path.relpath(os.path.join(root, n), path)
            if rel != "SHA256SUMS":
                on_disk.add(rel)
    if set(sums) != on_disk:
        raise Abort("SHA256SUMS does not list exactly the files present (missing %d, extra %d)" % (len(on_disk - set(sums)), len(set(sums) - on_disk)))
    for rel in sums:
        if rel.startswith("/") or ".." in rel.split("/"):
            raise Abort("unsafe path in SHA256SUMS")
    bad = []
    if check_hashes:
        bad = [rel for rel, h in sums.items() if sha256_file(os.path.join(path, rel)) != h]
        if bad:
            raise Abort("checksum mismatch in %d file(s): %s" % (len(bad), ", ".join(bad[:5])))
    members = {lab: check_tar_members(os.path.join(path, "files", lab + ".tgz")) for lab in FILE_LABELS}
    return {"files": len(sums), "hashes_checked": check_hashes, "tar_members": members, "manifest": man,
            "sums_sha256": sha256_file(os.path.join(path, "SHA256SUMS"))}


# ---------------------------------------------------------------- egress

def default_iface() -> str:
    m = re.search(r"\bdev (\S+)", run_text(["ip", "route", "show", "default"]).stdout)
    if not m:
        raise Abort("no default route interface")
    return m.group(1)


def net_subnet(L: Layout, net: str) -> str:
    return run_text(["docker", "network", "inspect", "%s_%s" % (L.project, net), "--format", "{{(index .IPAM.Config 0).Subnet}}"]).stdout.strip()


def egress_rules(sn: str, iface: str) -> list:
    return [["-s", sn, "-o", iface, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-m", "comment", "--comment", EGRESS_TAG, "-j", "RETURN"],
            ["-s", sn, "-o", iface, "-m", "comment", "--comment", EGRESS_TAG, "-j", "DROP"]]


def egress_present(L: Layout) -> dict:
    iface, res = default_iface(), {}
    for net in EGRESS_NETWORKS:
        sn = net_subnet(L, net)
        res[net] = all(run_text(["sudo", "iptables", "-C", "DOCKER-USER"] + r, check=False).returncode == 0 for r in egress_rules(sn, iface))
    return res


def egress_ensure(L: Layout) -> dict:
    iface = default_iface()
    for net in EGRESS_NETWORKS:
        sn = net_subnet(L, net)
        for pos, r in enumerate(egress_rules(sn, iface), start=1):
            if run_text(["sudo", "iptables", "-C", "DOCKER-USER"] + r, check=False).returncode != 0:
                run_text(["sudo", "iptables", "-I", "DOCKER-USER", str(pos)] + r)
    state = egress_present(L)
    if not all(state.values()):
        raise Abort("egress block is not in place: %s" % state)
    return state


def egress_probes(L: Layout) -> dict:
    """Connect attempts from inside own containers (must fail) and from a plain bridge container (control must succeed)."""
    code = "import socket;s=socket.socket();s.settimeout(5)\ntry:\n s.connect(('1.1.1.1',443));print('CONNECTED')\nexcept Exception as e:\n print('BLOCKED')"
    res = {}
    for ct in ("edu_ai_search", "edu_ai"):
        r = run_text(["docker", "exec", ct, "python3", "-c", code], check=False)
        res[ct] = "BLOCKED" if "BLOCKED" in r.stdout and "CONNECTED" not in r.stdout else ("CONNECTED" if "CONNECTED" in r.stdout else "ERROR")
    r = run_text(["docker", "exec", "bifrost", "wget", "-T", "5", "-q", "-O", "/dev/null", "http://1.1.1.1/"], check=False)
    res["bifrost"] = "CONNECTED" if r.returncode == 0 else "BLOCKED"
    c = run_text(["docker", "run", "--rm", "--entrypoint", "", "alpine:3", "wget", "-T", "5", "-q", "-O", "/dev/null", "http://1.1.1.1/"], check=False)
    res["control_default_bridge"] = "CONNECTED" if c.returncode == 0 else "BLOCKED"
    return res
