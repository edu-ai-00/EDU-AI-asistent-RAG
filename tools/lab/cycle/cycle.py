#!/usr/bin/env python3
"""CLI of the lab backup/update/restore driver (see cycle_core.py and README.md in this directory).

Every command first proves the layout is the verified stack's own (hostname, default engine, project labels,
working directory, mounts, links). Layout comes from flags or EDU_AI_* variables, never from defaults.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cycle_core as C  # noqa: E402
from cycle_core import Abort, Evidence, Layout  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FP_SQL = os.path.join(HERE, "cycle_fp.sql")


def mount_of(L: Layout, label: str) -> str:
    if label == "config":
        return L.cfg
    if label == "src":
        return L.src
    if label == "pgadmin_exports":
        return L.src + "/pgadmin_exports"
    if label in C.STATE_LABELS:
        return "%s/%s" % (L.state, label)
    return "%s_%s" % (L.project, label)


def exclude_of(label: str):
    return "pgadmin_exports" if label == "src" else None


def write_secret(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def git_state(src: str) -> dict:
    """Read-only: GIT_OPTIONAL_LOCKS=0 keeps `git status` from rewriting .git/index, which is part of the compared files."""
    env = dict(C.clean_env(), GIT_OPTIONAL_LOCKS="0")
    head = C.run_text(["git", "-C", src, "rev-parse", "HEAD"], env=env).stdout.strip()
    dirty = C.run_text(["git", "-C", src, "status", "--porcelain"], env=env).stdout.strip()
    idx = hashlib.sha256(C.run_text(["git", "-C", src, "ls-files", "-s"]).stdout.encode()).hexdigest()
    return {"head": head, "clean": not dirty, "index_sha256": idx}


def dir_attrs(paths) -> dict:
    out = {}
    for p in paths:
        st = os.stat(p)
        out[p] = [st.st_mode & 0o7777, st.st_uid, st.st_gid]
    return out


def unique_images(L: Layout) -> dict:
    """tag -> ID of every image used by the stack's containers; the tag must still point at the running ID."""
    res = {}
    for name, c in C.image_ids(L).items():
        cur = C.run_text(["docker", "image", "inspect", c["tag"], "--format", "{{.ID}}"]).stdout.strip()
        if cur != c["id"]:
            raise Abort("tag %s no longer points at the image of container %s" % (c["tag"], name))
        res[c["tag"]] = c["id"]
    return res


def toc_entries(L: Layout, path: str):
    with open(path, "rb") as f:
        r = C.run_stream(["docker", "exec", "-i", L.pg_container, "pg_restore", "-l"], stdin=f, stdout=subprocess.PIPE, check=False)
    return r.returncode, sum(1 for x in r.stdout.decode(errors="replace").splitlines() if x and not x.startswith(";"))


def start_stack(L: Layout, ev: Evidence, src=None, recreate=False) -> None:
    others = [s for s in C.services(L, src) if s != "postgres"]
    ev.step("egress_closed_before_start", all(C.egress_ensure(L).values()), "edge and bifrost_egress DROP rules present")
    args = ["up", "-d", "--no-build", "--pull", "never", "--wait"] + (["--force-recreate"] if recreate else ["--no-recreate"])
    C.compose(L, *args, src=src, timeout=900)
    ev.step("stack_started", True, "%d services" % len(others + ["postgres"]))


# ================================================================ backup

def cmd_backup(L: Layout, name: str, ev: Evidence, keep_tags: bool, wd: str | None = None) -> None:
    ident = C.gate(L, expect_wd=wd)
    ev.step("gate_own_stack", True, json.dumps(ident))
    b = C.bundle_path(L, name)
    if os.path.exists(b):
        raise Abort("backup path exists")
    os.makedirs(L.backup_root, mode=0o700, exist_ok=True)
    if os.stat(L.backup_root).st_mode & 0o077:
        raise Abort("backup root is accessible to group/others")
    os.umask(0o077)
    imgs = unique_images(L)
    size = sum(int(C.run_text(["docker", "image", "inspect", i, "--format", "{{.Size}}"]).stdout) for i in set(imgs.values()))
    free = shutil.disk_usage(L.backup_root).free
    ev.step("disk_reserve", free > size * 1.3 + 3 * 2**30, "free %.1f GiB, images %.1f GiB" % (free / 2**30, size / 2**30))
    before = C.image_ids(L)
    if any(c["status"] != "running" for c in before.values()):
        raise Abort("stack is not fully running before backup")
    for d in ("pg", "files", "meta"):
        os.makedirs(os.path.join(b, d), mode=0o700, exist_ok=True)
    os.chmod(b, 0o700)
    man = {"format": C.FORMAT, "status": "INCOMPLETE", "name": name, "created_utc": C.now(),
           "layout": {"project": L.project, "src": L.src, "cfg": L.cfg, "state": L.state, "host": L.host}}
    write_secret(os.path.join(b, "MANIFEST.json"), json.dumps(man))
    os.chmod(os.path.join(b, "MANIFEST.json"), 0o600)
    tool = C.tool_image(L)
    env = C.read_env(L.env_path)
    ident_doc = {"containers": before, "git": git_state(L.src), "dirs": dir_attrs([L.src, L.cfg, L.state, L.backup_root]),
                 "env_keys": sorted(env), "env_sha256": C.sha256_file(L.env_path),
                 "install_record_sha256": C.sha256_file(L.cfg + "/install-record.json"),
                 "docker": C.run_text(["docker", "version", "--format", "{{.Server.Version}}"]).stdout.strip(),
                 "compose": C.run_text(["docker", "compose", "version", "--short"]).stdout.strip(),
                 "egress_rules_before": C.egress_present(L), "tool_image": tool}
    ev.step("source_clean_git", ident_doc["git"]["clean"], ident_doc["git"]["head"])
    # images first: they are immutable, so the freeze window does not have to cover them
    ev.mark("saving images")
    C.run_text(["docker", "save", "-o", os.path.join(b, "images.tar")] + sorted(imgs), timeout=3600)
    os.chmod(os.path.join(b, "images.tar"), 0o600)
    images_sha = C.sha256_file(os.path.join(b, "images.tar"))
    write_secret(os.path.join(b, "images.json"), json.dumps({"tags": imgs, "containers": {n: {"tag": c["tag"], "id": c["id"]} for n, c in before.items()}}, indent=1))
    if keep_tags:
        for tag, iid in imgs.items():
            repo = tag.rsplit(":", 1)[0]
            C.run_text(["docker", "tag", iid, "%s:cycle-%s" % (repo, name.lower())])
    ev.step("images_saved", True, "%d tags, %.1f GiB, sha256 %s" % (len(imgs), os.path.getsize(os.path.join(b, "images.tar")) / 2**30, images_sha[:16]))
    # freeze: every writer of the stack stopped; only postgres stays up, with no application session
    t_freeze = time.time()
    ev.mark("FREEZE: stopping writers")
    frozen, restarted = True, False
    try:
        others = [s for s in C.services(L) if s != "postgres"]
        C.compose(L, "stop", "-t", "60", *others, timeout=600)
        after = C.image_ids(L)
        live = [n for n, c in after.items() if c["service"] != "postgres" and c["status"] == "running"]
        ev.step("writers_stopped", not live and after[L.pg_container]["status"] == "running", "running after stop: %s" % live)
        sess = "?"
        for _ in range(10):
            sess = C.count_value(C.pg_psql(L, "postgres", "select count(*) from pg_stat_activity where backend_type='client backend' and usename<>'%s'" % L.pg_admin))
            if sess == 0:
                break
            time.sleep(2)
        ev.step("no_application_sessions", sess == 0, "non-admin client sessions: %s" % sess)
        fps = {db: C.fingerprint(L, db, FP_SQL) for db in C.DBS}
        for db in C.DBS:
            write_secret(os.path.join(b, "pg", "fp_%s.txt" % db), fps[db])
        write_secret(os.path.join(b, "pg", "roles_summary.txt"), C.roles_summary(L))
        vr = C.run_text(["python3", "scripts/db_roles.py", "verify", "--container", L.pg_container], cwd=L.src, check=False)
        write_secret(os.path.join(b, "pg", "role_verify.txt"), (vr.stdout + vr.stderr) or "no output\n")
        ev.step("role_contract_at_backup", vr.returncode == 0 and "MATCH" in vr.stdout, "verify rc=%d" % vr.returncode)
        write_secret(os.path.join(b, "pg", "databases.txt"), C.pg_psql(L, "postgres",
                     "select datname||'|'||pg_get_userbyid(datdba)||'|'||pg_encoding_to_char(encoding)||'|'||datcollate||'|'||datctype from pg_database where datname in ('edu_ai','langflow') order by 1") + "\n")
        write_secret(os.path.join(b, "pg", "settings.txt"), C.pg_psql(L, "postgres",
                     "select coalesce(d.datname,'-')||'|'||coalesce(r.rolname,'-')||'|'||s.setconfig::text from pg_db_role_setting s left join pg_database d on d.oid=s.setdatabase left join pg_roles r on r.oid=s.setrole order by 1") + "\n")
        for db in C.DBS:
            for kind, flags in (("dump", ["-Fc", "-Z", "5"]), ("schema", ["-s"])):
                dest = os.path.join(b, "pg", db + ".dump" if kind == "dump" else "schema_%s.sql" % db)
                with open(dest, "wb") as f:
                    os.fchmod(f.fileno(), 0o600)
                    C.run_stream(["docker", "exec", L.pg_container, "pg_dump", "-U", L.pg_admin] + flags + ["-d", db], stdout=f)
        with open(os.path.join(b, "pg", "globals.sql"), "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            C.run_stream(["docker", "exec", L.pg_container, "pg_dumpall", "-U", L.pg_admin, "--globals-only"], stdout=f)
        ev.step("databases_dumped", True, ", ".join("%s %.1f MiB" % (d, os.path.getsize(os.path.join(b, "pg", d + ".dump")) / 2**20) for d in C.DBS))
        classification = {"postgres_data": "covered by logical dumps (pg_dump + globals); physical PGDATA only copied aside at restore",
                          "reranker_models": "cache (not backed up)"}
        for lab in C.FILE_LABELS:
            mount, ex = mount_of(L, lab), exclude_of(lab)
            m_before = C.manifest_text(tool, mount, ex)
            write_secret(os.path.join(b, "files", lab + ".manifest.txt"), m_before)
            C.tar_to_file(tool, mount, os.path.join(b, "files", lab + ".tgz"), ex)
            m_arch = C.archive_manifest(tool, os.path.join(b, "files", lab + ".tgz"), ex)
            same = C.norm_manifest(m_before) == C.norm_manifest(m_arch)
            n = sum(1 for x in m_before.splitlines() if x and x[0] in "dfl" and "|" in x)
            ev.step("files_" + lab, same, "%d entries, archive == live (modes, owners, sha256)" % n)
            classification[lab] = "irreplaceable (backed up, %d entries)" % n
        cache_sz = C.run_text(["sudo", "du", "-sb", "/var/lib/docker/volumes/%s_reranker_models/_data" % L.project], check=False).stdout.split()[0:1]
        classification["reranker_models_bytes"] = cache_sz[0] if cache_sz else "?"
        write_secret(os.path.join(b, "meta", "classification.json"), json.dumps(classification, indent=1))
        # bifrost SQLite (WAL state): integrity on a copy inside a throwaway container
        search_img = next(c["id"] for c in before.values() if c["service"] == "edu_ai_search")
        code = ("import glob,os,shutil,sqlite3,tempfile,json;d=tempfile.mkdtemp();r={}\n"
                "for f in glob.glob('/s/*.db*'):shutil.copy(f,d)\n"
                "for f in sorted(glob.glob(d+'/*.db')):\n c=sqlite3.connect(f);r[os.path.basename(f)]=c.execute('pragma integrity_check').fetchone()[0];c.close()\n"
                "r['wal_files']=sorted(os.path.basename(x) for x in glob.glob('/s/*.db-wal'))\nprint(json.dumps(r))")
        sq = json.loads(C.run_text(["docker", "run", "--rm", "--network", "none", "--user", "0", "-v", mount_of(L, "bifrost_data") + ":/s:ro", "--entrypoint", "python3", search_img, "-c", code]).stdout)
        db_results = [v for k, v in sq.items() if k.endswith(".db")]
        ev.step("bifrost_sqlite_integrity", bool(db_results) and all(v == "ok" for v in db_results),
                "databases=%d integrity_ok=%d wal_files=%d" %
                (len(db_results), sum(v == "ok" for v in db_results), len(sq.get("wal_files") or [])))
        # the freeze window must have been one point: second fingerprint and manifests identical
        for db in C.DBS:
            c2 = C.compare_fp(fps[db], C.fingerprint(L, db, FP_SQL))
            ev.step("raw_baseline_stable_" + db, c2["ok"], "tables %d/%d rows %d seq %d/%d" % (c2["tables_equal"], c2["tables_total"], c2["rows_total"], c2["sequences_equal"], c2["sequences_total"]))
        for lab in C.FILE_LABELS:
            again = C.manifest_text(tool, mount_of(L, lab), exclude_of(lab))
            ok = C.norm_manifest(again) == C.norm_manifest(C.slurp(os.path.join(b, "files", lab + ".manifest.txt")))
            ev.step("files_stable_" + lab, ok, "unchanged during the window")
        write_secret(os.path.join(b, "meta", "identity.json"), json.dumps(ident_doc, indent=1))
        write_secret(os.path.join(b, "meta", "env_keys.txt"), "\n".join(ident_doc["env_keys"]) + "\n")
        man.update({"status": "COMPLETE", "images_tar_sha256": images_sha, "freeze_started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_freeze)),
                    "databases": list(C.DBS), "labels": list(C.FILE_LABELS), "git": ident_doc["git"]})
        os.remove(os.path.join(b, "MANIFEST.json"))
        write_secret(os.path.join(b, "MANIFEST.json"), json.dumps(man, indent=1))
        sums = {}
        for root, _, files in os.walk(b):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), b)
                if rel != "SHA256SUMS":
                    sums[rel] = images_sha if rel == "images.tar" else C.sha256_file(os.path.join(root, fn))
        write_secret(os.path.join(b, "SHA256SUMS"), "".join("%s  %s\n" % (h, r) for r, h in sorted(sums.items())))
        ev.step("bundle_written", True, "%d files" % (len(sums) + 1))
    except BaseException:
        ev.mark("FAILURE inside the freeze window: restarting the original services")
        raise
    finally:
        if frozen and not restarted:
            try:
                start_stack(L, ev)
                restarted = True
                t_up = time.time()
                ev.extra["downtime_seconds"] = int(t_up - t_freeze)
                ev.mark("services restarted; downtime %ds" % ev.extra["downtime_seconds"])
            except Exception as exc:  # noqa: BLE001
                raise Abort("original services restart failed (type=%s)" % type(exc).__name__) from None
    after = C.image_ids(L)
    ev.step("images_unchanged_after_restart", {n: c["id"] for n, c in before.items()} == {n: c["id"] for n, c in after.items()}, "container image IDs identical")
    ev.extra.update({"bundle": b, "images": imgs})


def cmd_verify_backup(L: Layout, name: str, ev: Evidence, wd=None) -> dict:
    b = C.bundle_path(L, name)
    C.gate(L, expect_wd=wd)
    v = C.verify_bundle(b, expect_project=L.project)
    ev.step("bundle_files_sha256", True, "%d files hashed, all %d archives safe" % (v["files"], len(v["tar_members"])))
    for db in C.DBS:
        rc, n = toc_entries(L, os.path.join(b, "pg", db + ".dump"))
        ev.step("dump_toc_" + db, rc == 0 and n > 0, "%d TOC entries" % n)
    meta = json.loads(C.slurp(os.path.join(b, "images.json")))
    listing = C.run_text(["tar", "-tf", os.path.join(b, "images.tar")]).stdout.splitlines()
    ev.step("images_tar_readable", bool(listing) and any(x.endswith("index.json") or x.endswith("manifest.json") for x in listing), "%d entries, %d tags" % (len(listing), len(meta["tags"])))
    return v


# ================================================================ restore

def cmd_restore(L: Layout, name: str, ev: Evidence, confirm: bool, wd: str | None = None) -> None:
    if not confirm:
        raise Abort("restore overwrites this stack's database, files and images: pass --confirm-overwrite-own-stack")
    b = C.bundle_path(L, name)
    ident = C.gate(L, need_stack=False, expect_wd=wd)
    ev.step("gate_own_stack", True, json.dumps(ident))
    C.invalidate_marker(ev)
    v = C.verify_bundle(b, expect_project=L.project)
    man = v["manifest"]
    ev.step("bundle_verified_before_any_change", True, "%d files sha256-checked, archives safe" % v["files"])
    require_bundle_layout(L, v, ev)
    for db in C.DBS:
        rc, n = toc_entries(L, os.path.join(b, "pg", db + ".dump"))
        ev.step("dump_toc_" + db, rc == 0 and n > 0, "pg_restore -l readable, %d entries" % n)
    imgs_doc = json.loads(C.slurp(os.path.join(b, "images.json")))
    tool_tag = next(t for t in imgs_doc["tags"] if t.startswith("edu_ai_postgres:"))
    tool_id = imgs_doc["tags"][tool_tag]
    free = shutil.disk_usage(L.backup_root).free
    need = 3 * 2**30
    ev.step("disk_reserve", free > need, "free %.1f GiB" % (free / 2**30))
    ident_doc = json.loads(C.slurp(os.path.join(b, "meta", "identity.json")))
    pre = os.path.join(L.backup_root, "pre-restore-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    os.makedirs(pre, mode=0o700)
    # ---- point of no return: from here a failure leaves the applications stopped
    ev.mark("DESTRUCTIVE PART STARTS (applications stay down until start-apps)")
    t_down = time.time()
    own = [c for c in C.containers(L) if c["project"] == L.project]
    if own:
        C.compose(L, "stop", "-t", "60", timeout=900)
        C.run_text(["docker", "rm"] + [c["id"] for c in own])
    ev.step("stack_containers_removed", not [c for c in C.containers(L) if c["project"] == L.project], "%d removed (networks and cache volume kept)" % len(own))
    # safety copies of what is about to be replaced (moves on one filesystem, PGDATA and volumes as tar)
    for vol in ("postgres_data",) + C.VOLUME_LABELS:
        full = "%s_%s" % (L.project, vol)
        if C.run_text(["docker", "volume", "inspect", full], check=False).returncode == 0:
            lab = C.run_text(["docker", "volume", "inspect", full, "--format", '{{index .Labels "com.docker.compose.project"}}']).stdout.strip()
            if lab != L.project:
                raise Abort("volume %s does not belong to the project" % full)
            with open(os.path.join(pre, vol + ".tar"), "wb") as f:
                os.fchmod(f.fileno(), 0o600)
                C.tool_run(tool_id, full, "tar --numeric-owner -p -cf - -C /s .", stdout=f)
    for p, nm in ((L.src, "src"), (L.cfg, "config"), (L.state, "state")):
        if os.path.exists(p):
            os.rename(p, os.path.join(pre, nm))
    ev.step("pre_restore_copies", all(os.path.exists(os.path.join(pre, x)) for x in ("src", "config", "state", "postgres_data.tar")), "kept in %s" % os.path.basename(pre))
    for vol in ("postgres_data",) + C.VOLUME_LABELS:
        C.run_text(["docker", "volume", "rm", "%s_%s" % (L.project, vol)], check=False)
    # images
    C.run_text(["docker", "load", "-i", os.path.join(b, "images.tar")], timeout=3600)
    bad = {t: C.run_text(["docker", "image", "inspect", t, "--format", "{{.ID}}"], check=False).stdout.strip() for t in imgs_doc["tags"]}
    bad = {t: i for t, i in bad.items() if i != imgs_doc["tags"][t]}
    ev.step("images_loaded_with_original_ids", not bad, "%d tags verified against the backup (mismatch: %s)" % (len(imgs_doc["tags"]), sorted(bad)))
    # files, config, source
    attrs = ident_doc["dirs"]
    def make_dir(path, mode, uid, gid):
        C.run_text(["sudo", "install", "-d", "-m", "%o" % mode, "-o", str(uid), "-g", str(gid), path])
    make_dir(L.cfg, *attrs[L.cfg])
    make_dir(L.state, *attrs[L.state])
    make_dir(L.src, *attrs[L.src])
    for lab in C.FILE_LABELS:
        mount = mount_of(L, lab)
        if lab in C.VOLUME_LABELS:
            C.run_text(["docker", "volume", "create", "--label", "com.docker.compose.project=" + L.project, "--label", "com.docker.compose.volume=" + lab, mount])
        elif lab in C.STATE_LABELS + ("pgadmin_exports",):
            C.run_text(["sudo", "install", "-d", "-m", "700", "-o", "root", "-g", "root", mount])
        C.untar_from_file(tool_id, mount, os.path.join(b, "files", lab + ".tgz"))
    ev.step("files_extracted", True, "%d labels restored with numeric owners" % len(C.FILE_LABELS))
    # PostgreSQL: fresh cluster from the original image, no init scripts, no network
    env = C.read_env(L.env_path)
    vol = "%s_postgres_data" % L.project
    C.run_text(["docker", "volume", "create", "--label", "com.docker.compose.project=" + L.project, "--label", "com.docker.compose.volume=postgres_data", vol])
    helper = "cycle_pg_restore"
    C.run_text(["docker", "rm", "-f", helper], check=False)
    e = dict(C.clean_env(), POSTGRES_PASSWORD=env["POSTGRES_ADMIN_PASSWORD"])
    C.run_text(["docker", "run", "-d", "--name", helper, "--network", "none", "-e", "POSTGRES_USER=" + L.pg_admin, "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=postgres",
              "-v", vol + ":/var/lib/postgresql/data", tool_id, "postgres", "-c", "shared_preload_libraries=pg_search"], env=e)
    for _ in range(120):
        if C.run_text(["docker", "exec", helper, "pg_isready", "-U", L.pg_admin], check=False).returncode == 0:
            time.sleep(3)
            if C.run_text(["docker", "exec", helper, "pg_isready", "-U", L.pg_admin], check=False).returncode == 0:
                break
        time.sleep(1)
    else:
        raise Abort("restore helper did not become ready")
    def hpsql(db, **kw):
        return C.run_text(["docker", "exec", "-i", helper, "psql", "-X", "-At", "-U", L.pg_admin, "-d", db, "-v", "ON_ERROR_STOP=1"] + (["-c", kw["sql"]] if "sql" in kw else ["-f", "-"]), input=kw.get("stdin"))
    g = C.run_text(["docker", "exec", "-i", helper, "psql", "-X", "-q", "-U", L.pg_admin, "-d", "postgres"], input=C.slurp(os.path.join(b, "pg", "globals.sql")), check=False)
    unexpected = [x for x in g.stderr.splitlines() if "ERROR" in x and 'role "%s" already exists' % L.pg_admin not in x]
    ev.step("globals_applied", not unexpected, "unexpected errors: %d" % len(unexpected))
    for line in C.slurp(os.path.join(b, "pg", "databases.txt")).splitlines():
        db, owner, enc, coll, ctype = line.split("|")
        hpsql("postgres", sql="CREATE DATABASE \"%s\" OWNER \"%s\" ENCODING '%s' LC_COLLATE '%s' LC_CTYPE '%s' TEMPLATE template0" % (db, owner, enc, coll, ctype))
        with open(os.path.join(b, "pg", db + ".dump"), "rb") as f:
            r = C.run_stream(["docker", "exec", "-i", helper, "pg_restore", "-U", L.pg_admin, "-d", db, "--exit-on-error"], stdin=f, stdout=subprocess.PIPE, check=False)
        ev.step("pg_restore_" + db, r.returncode == 0, "rc=%d" % r.returncode)
    hpsql("postgres", sql="CHECKPOINT")
    C.run_text(["docker", "stop", "-t", "60", helper])
    C.run_text(["docker", "rm", helper])
    # postgres only, from the restored source; roles before any application
    C.compose(L, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "postgres", timeout=300)
    for _ in range(60):
        if C.run_text(["docker", "exec", L.pg_container, "pg_isready", "-U", L.pg_admin], check=False).returncode == 0:
            break
        time.sleep(2)
    ra = C.run_text(["python3", "scripts/db_roles.py", "reapply", "--container", L.pg_container, "--confirm-isolated"], cwd=L.src, check=False)
    ev.step("role_reapply", ra.returncode == 0, "rc=%d" % ra.returncode)
    rv = C.run_text(["python3", "scripts/db_roles.py", "verify", "--container", L.pg_container], cwd=L.src, check=False)
    ev.step("role_verify_match", rv.returncode == 0 and "MATCH" in rv.stdout, "rc=%d" % rv.returncode)
    restore_verify(L, name, ev, b, v, t_down, pre)


def require_bundle_layout(L: Layout, v: dict, ev: Evidence) -> None:
    lay = v["manifest"].get("layout") or {}
    ev.step("bundle_matches_layout", all(lay.get(k) == value for k, value in C.layout_identity(L).items()),
            "project/paths/host equal")


def restore_verify(L: Layout, name: str, ev: Evidence, b: str, v: dict, t_down=None, pre=None, *, write_marker=True) -> None:
    """Everything that must hold before applications may start; writes the success marker only if all of it passes."""
    marker = os.path.join(ev.dir, "restore_ok.json")
    ident_doc = json.loads(C.slurp(os.path.join(b, "meta", "identity.json")))
    imgs_doc = json.loads(C.slurp(os.path.join(b, "images.json")))
    tool_id = imgs_doc["tags"][next(t for t in imgs_doc["tags"] if t.startswith("edu_ai_postgres:"))]
    require_bundle_layout(L, v, ev)
    running = C.image_ids(L)
    ev.step("applications_stopped", not any(c["service"] != "postgres" and c["status"] == "running"
                                              for c in running.values()), "no application is running")
    pg = running.get(L.pg_container) or {}
    ev.step("postgres_original_image", pg.get("status") == "running" and pg.get("id") == tool_id,
            "original PostgreSQL image is running")
    mismatch = sum(C.run_text(["docker", "image", "inspect", tag, "--format", "{{.ID}}"], check=False).stdout.strip() != iid
                   for tag, iid in imgs_doc["tags"].items())
    ev.step("original_image_tags", mismatch == 0, "tags=%d mismatches=%d" % (len(imgs_doc["tags"]), mismatch))
    # raw comparison against the backup, before any application starts
    cmp_all = {}
    for db in C.DBS:
        c = C.compare_fp(C.slurp(os.path.join(b, "pg", "fp_%s.txt" % db)), C.fingerprint(L, db, FP_SQL))
        cmp_all[db] = c
        ev.step("raw_compare_" + db, c["ok"], "tables %d/%d rows %d seq %d/%d items %d/%d lo=%s" % (c["tables_equal"], c["tables_total"], c["rows_total"], c["sequences_equal"], c["sequences_total"], c["schema_items_equal"], c["schema_items_total"], c["large_objects_equal"]), fatal=False)
        ev.extra.setdefault("raw_compare", {})[db] = c
    ev.step("raw_compare_all", all(c["ok"] for c in cmp_all.values()), "both databases")
    for db in C.DBS:
        sch = C.run_text(["docker", "exec", L.pg_container, "pg_dump", "-U", L.pg_admin, "-s", "-d", db]).stdout
        ev.step("schema_dump_" + db, C.schema_dump_equal(sch, C.slurp(os.path.join(b, "pg", "schema_%s.sql" % db))), "pg_dump -s equal to the backup (GRANT/REVOKE compared as a set)", fatal=False)
    rs_now, rs_then = C.norm_acl(C.roles_summary(L)), C.norm_acl(C.slurp(os.path.join(b, "pg", "roles_summary.txt")))
    ev.step("roles_settings_databases", rs_now == rs_then, "roles, memberships, role/db settings, database ACL and comment equal", fatal=False)
    # files, config, source, images
    for lab in C.FILE_LABELS:
        m = C.manifest_text(tool_id, mount_of(L, lab), exclude_of(lab))
        same = C.norm_manifest(m) == C.norm_manifest(C.slurp(os.path.join(b, "files", lab + ".manifest.txt")))
        ev.step("files_compare_" + lab, same, "modes, numeric owners, mtimes and sha256 of every file", fatal=False)
    ev.step("files_all_equal", all(ev.steps["files_compare_" + lab]["result"] == "PASS" for lab in C.FILE_LABELS), "all %d labels" % len(C.FILE_LABELS))
    g = C.read_env(L.env_path)
    ev.step("config_identity", C.sha256_file(L.env_path) == ident_doc["env_sha256"] and C.sha256_file(L.cfg + "/install-record.json") == ident_doc["install_record_sha256"] and sorted(g) == ident_doc["env_keys"], ".env and install-record sha256 and key names equal")
    now_git = git_state(L.src)
    ev.step("source_identity", now_git == ident_doc["git"], "HEAD %s clean=%s tracked index equal" % (now_git["head"][:12], now_git["clean"]))
    ev.step("schema_state_all_equal", all(ev.steps[k]["result"] == "PASS" for k in ev.steps if k.startswith(("schema_dump_", "roles_settings"))), "schema dumps and role/database state")
    ev.step("egress_still_closed", all(C.egress_present(L).values()), "DROP rules present")
    t_ready = time.time()
    if write_marker:
        write_secret(marker, json.dumps({"bundle": name, "sums_sha256": v["sums_sha256"],
                                         "layout": C.layout_identity(L), "at": C.now()}))
    if t_down:
        ev.extra["apps_down_seconds_until_restore_verified"] = int(t_ready - t_down)
    ev.extra["pre_restore_dir"] = pre
    ev.mark("RESTORE VERIFIED; applications are still stopped" + (" (run start-apps)" if write_marker else " (prestart comparison)"))




def cmd_verify_restore(L: Layout, name: str, ev: Evidence, wd: str | None = None) -> None:
    """Resume after a restore whose destructive part finished but whose comparison did not: no application may run."""
    b = C.bundle_path(L, name)
    ev.step("gate_own_stack", True, json.dumps(C.gate(L, need_stack=False, expect_wd=wd)))
    C.invalidate_marker(ev)
    running = [c for c in C.image_ids(L).values() if c["service"] != "postgres" and c["status"] == "running"]
    ev.step("applications_stopped", not running, "no application container is running")
    v = C.verify_bundle(b, expect_project=L.project)
    restore_verify(L, name, ev, b, v)


def cmd_start_apps(L: Layout, name: str, ev: Evidence, wd: str | None = None) -> None:
    b = C.bundle_path(L, name)
    mpath = os.path.join(ev.dir, "restore_ok.json")
    if not os.path.exists(mpath):
        raise Abort("no success marker of a verified restore: applications are not started")
    mk = json.loads(C.slurp(mpath))
    v = C.verify_bundle(b, expect_project=L.project)
    ev.step("restore_marker_matches_bundle", mk["sums_sha256"] == v["sums_sha256"] and mk["bundle"] == name, "marker from this bundle's verified restore")
    ev.step("restore_marker_matches_layout", mk.get("layout") == C.layout_identity(L), "marker identifies this exact layout")
    C.gate(L, need_stack=False, expect_wd=wd)
    # A success marker is one-use. Full comparisons catch manual edits, changed
    # tags or stale verification before any application can start.
    C.invalidate_marker(ev)
    restore_verify(L, name, ev, b, v, write_marker=False)
    start_stack(L, ev, recreate=True)


# ================================================================ update A -> B

def cmd_update(L: Layout, from_src: str, backup_name: str, sha: str, ev: Evidence) -> None:
    """L describes the TARGET checkout; containers must still come from from_src."""
    ident = C.gate(L, expect_wd=from_src)
    ev.step("gate_own_stack", True, json.dumps(ident))
    C.invalidate_marker(ev)
    git = git_state(L.src)
    ev.step("target_is_exact_sha", git["head"] == sha and git["clean"], "HEAD %s clean=%s" % (git["head"][:12], git["clean"]))
    b = C.bundle_path(L, backup_name)
    v = C.verify_bundle(b, expect_project=L.project)
    man = v["manifest"]
    running = C.image_ids(L)
    want = json.loads(C.slurp(os.path.join(b, "images.json")))["containers"]
    ev.step("backup_describes_running_A", {n: c["id"] for n, c in running.items()} == {n: c["id"] for n, c in want.items()} and man["git"]["head"] == git_state(from_src)["head"],
            "running image IDs and source HEAD equal the backup's")
    ev.step("egress_closed_before_build", all(C.egress_present(L).values()), "DROP rules present")
    fa, fb = from_src + "/langflow_flows/ingestion", L.src + "/langflow_flows/ingestion"
    def flow_hashes(d):
        return {f: C.sha256_file(os.path.join(d, f)) for f in sorted(os.listdir(d))}
    ev.step("canonical_flows_A_vs_B", True, "A==B: %s" % (flow_hashes(fa) == flow_hashes(fb)))
    fr = C.run_text(["python3", "scripts/flow_rollout.py", "--from-dir", fa, "--to-dir", fb, "--check"], cwd=L.src, check=False)
    out = fr.stdout
    guard = "GUARD_FAIL" in out or "REFUSED" in out
    ev.step("flow_rollout_check", fr.returncode == 0 and not guard,
            "rc=%d lines=%d guard_refused=%s" % (fr.returncode, len(out.splitlines()), guard))
    ev.step("flow_rollout_needs_patch", "WOULD_PATCH" not in out, "no node needs patching" if "WOULD_PATCH" not in out else "PATCH NEEDED", fatal=False)
    t0 = time.time()
    C.compose(L, "build", timeout=3600)
    ev.step("build_from_B", True, "%ds" % int(time.time() - t0))
    ev.step("egress_closed_before_up", all(C.egress_ensure(L).values()), "DROP rules present")
    t0 = time.time()
    C.compose(L, "up", "-d", "--wait", "--force-recreate", "--no-build", "--pull", "never", timeout=1800)
    ev.step("up_force_recreate", True, "%ds" % int(time.time() - t0))
    new = C.image_ids(L)
    bad = []
    for n, c in new.items():
        cur = C.run_text(["docker", "image", "inspect", c["tag"], "--format", "{{.ID}}"]).stdout.strip()
        if cur != c["id"]:
            bad.append(n)
    wd = {c["name"]: c["wd"] for c in C.containers(L) if c["project"] == L.project}
    ev.step("containers_from_B", not bad and set(wd.values()) == {L.src}, "working_dir == target for all %d; tag==running image ID (mismatch: %s)" % (len(wd), bad))
    ev.extra["images_after"] = {n: {"tag": c["tag"], "id": c["id"], "status": c["status"]} for n, c in new.items()}


# ================================================================ markers, live compare

def api_call(L: Layout, method: str, path: str, body=None):
    code = ("import json,os,sys,urllib.request\nenv=dict(l.split('=',1) for l in open('/proc/self/environ').read().split('\\0') if '=' in l)\n"
            "b=json.loads(sys.stdin.read() or 'null')\nreq=urllib.request.Request('http://127.0.0.1:7860'+%r,method=%r,data=None if b is None else json.dumps(b).encode(),"
            "headers={'Content-Type':'application/json','x-api-key':env['LANGFLOW_API_KEY']})\nr=urllib.request.urlopen(req,timeout=60)\nprint(r.status)" % (path, method))
    return C.run_text(["docker", "exec", "-i", "edu_ai", "python3", "-c", code], input=json.dumps(body)).stdout.strip()


def markers(L: Layout, action: str, mid: str, ev: Evidence, wd=None) -> dict:
    C.identifier(mid, "marker")
    C.gate(L, expect_wd=wd)
    if action == "put":
        C.invalidate_marker(ev)
    tool = C.tool_image(L)
    res = {}
    names = [(lab, mount_of(L, lab)) for lab in ("config", "src", "pgadmin_exports") + C.STATE_LABELS + C.VOLUME_LABELS]
    fn = ".cycle_marker_" + mid
    for lab, mount in names:
        path_in = "/s/" + (("CYCLE_MARKER_" + mid) if lab == "src" else fn)
        if action == "put":
            C.tool_run(tool, mount, "echo 'synthetic marker %s' > %s" % (mid, path_in), ro=False)
        r = C.tool_run(tool, mount, "test -e %s && echo present || echo absent" % path_in).stdout.strip()
        res["file_" + lab] = r if r in ("present", "absent") else "error"
    for db in C.DBS:
        if action == "put":
            C.pg_psql(L, db, "select lo_from_bytea(0, 'synthetic marker %s'::bytea)" % mid)
        n = C.count_value(C.pg_psql(L, db, "select count(*) from pg_largeobject_metadata"))
        res["db_large_objects_" + db] = n
    ev.extra.setdefault("markers", {})[mid + "_" + action] = res
    ev.mark("markers %s %s: %s" % (action, mid, json.dumps(res)))
    return res


def live_compare(L: Layout, name: str, ev: Evidence, label: str, wd=None) -> dict:
    b = C.bundle_path(L, name)
    C.identifier(label, "comparison label")
    C.gate(L, expect_wd=wd)
    out = {}
    for db in C.DBS:
        c = C.compare_fp(C.slurp(os.path.join(b, "pg", "fp_%s.txt" % db)), C.fingerprint(L, db, FP_SQL))
        out[db] = {k: c[k] for k in ("ok", "tables_total", "tables_equal", "tables_different", "rows_total", "sequences_different", "large_objects_equal", "schema_items_equal", "schema_items_total")}
        ev.mark("%s %s: equal=%s tables_different=%d sequences_different=%d lo_equal=%s" % (label, db, c["ok"], len(c["tables_different"]), len(c["sequences_different"]), c["large_objects_equal"]))
    tool = C.tool_image(L)
    fdiff = {}
    for lab in C.FILE_LABELS:
        m = C.manifest_text(tool, mount_of(L, lab), exclude_of(lab))
        a, bm = C.norm_manifest(m), C.norm_manifest(C.slurp(os.path.join(b, "files", lab + ".manifest.txt")))
        fdiff[lab] = len(set(a) ^ set(bm))
    ev.mark("%s files: differing manifest lines per label %s" % (label, json.dumps(fdiff)))
    ev.extra[label] = {"db": out, "files_differing_lines": fdiff}
    return ev.extra[label]


# ================================================================ main

def build_layout(a) -> Layout:
    def pick(v, env):
        r = v or os.environ.get(env)
        if not r:
            raise SystemExit("missing %s / --%s" % (env, env.split("_")[-1].lower()))
        return r
    return Layout(src=pick(a.src, "EDU_AI_SOURCE_ROOT"), cfg=pick(a.cfg, "EDU_AI_CONFIG_DIR"), state=pick(a.state, "EDU_AI_STATE_DIR"),
                  project=pick(a.project, "EDU_AI_COMPOSE_PROJECT"), host=pick(a.expect_host, "CYCLE_EXPECT_HOST"), backup_root=pick(a.backup_root, "CYCLE_BACKUP_ROOT"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for k in ("src", "cfg", "state", "project", "expect-host", "backup-root"):
        ap.add_argument("--" + k)
    ap.add_argument("--evidence-dir", required=True)
    ap.add_argument("--running-from", help="working directory the running containers were created from (default: --src)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gate")
    p = sub.add_parser("egress"); p.add_argument("action", choices=["ensure", "check", "probe"])
    p = sub.add_parser("backup"); p.add_argument("name"); p.add_argument("--no-keep-tags", action="store_true")
    p = sub.add_parser("verify-backup"); p.add_argument("name")
    p = sub.add_parser("restore"); p.add_argument("name"); p.add_argument("--confirm-overwrite-own-stack", action="store_true")
    p = sub.add_parser("verify-restore"); p.add_argument("name")
    p = sub.add_parser("start-apps"); p.add_argument("name")
    p = sub.add_parser("update"); p.add_argument("--from-src", required=True); p.add_argument("--backup", required=True); p.add_argument("--sha", required=True)
    p = sub.add_parser("markers"); p.add_argument("action", choices=["put", "check"]); p.add_argument("id")
    p = sub.add_parser("compare-live"); p.add_argument("name"); p.add_argument("--label", default="live")
    a = ap.parse_args(argv)
    L = build_layout(a)
    try:
        for attr in ("name", "backup", "id", "label"):
            value = getattr(a, attr, None)
            if value is not None:
                C.identifier(value, attr)
    except Abort as exc:
        print("ABORT: %s" % exc, file=sys.stderr)
        return 2
    ev = Evidence(a.evidence_dir, a.cmd + (("_" + getattr(a, "name", "")) if getattr(a, "name", None) else ""))
    try:
        # Every entry point proves ownership before stack access or mutations.
        C.gate(L, expect_wd=a.from_src if a.cmd == "update" else a.running_from,
               need_stack=a.cmd not in ("restore", "verify-restore", "start-apps"))
        if a.cmd == "gate":
            ev.step("gate_own_stack", True, json.dumps(C.gate(L, expect_wd=a.running_from)))
        elif a.cmd == "egress":
            if a.action == "ensure":
                ev.step("egress_ensure", True, json.dumps(C.egress_ensure(L)))
            elif a.action == "check":
                ev.step("egress_check", all(C.egress_present(L).values()), json.dumps(C.egress_present(L)))
            else:
                pr = C.egress_probes(L)
                ev.step("egress_probes", all(v == "BLOCKED" for k, v in pr.items() if not k.startswith("control")) and pr["control_default_bridge"] == "CONNECTED", json.dumps(pr))
        elif a.cmd == "backup":
            cmd_backup(L, a.name, ev, not a.no_keep_tags, a.running_from)
        elif a.cmd == "verify-backup":
            cmd_verify_backup(L, a.name, ev, a.running_from)
        elif a.cmd == "restore":
            cmd_restore(L, a.name, ev, a.confirm_overwrite_own_stack, a.running_from)
        elif a.cmd == "verify-restore":
            cmd_verify_restore(L, a.name, ev, a.running_from)
        elif a.cmd == "start-apps":
            cmd_start_apps(L, a.name, ev, a.running_from)
        elif a.cmd == "update":
            cmd_update(L, a.from_src, a.backup, a.sha, ev)
        elif a.cmd == "markers":
            markers(L, a.action, a.id, ev, a.running_from)
        elif a.cmd == "compare-live":
            live_compare(L, a.name, ev, a.label, a.running_from)
    except Abort as exc:
        ev.mark("ABORT: %s" % exc)
        ev.extra["abort"] = str(exc)
        ev.save()
        return 2
    except Exception as exc:
        # Third-party exception messages and reprs may contain secrets. Keep
        # only a type identifier and leave applications stopped after failures.
        ev.mark("ABORT: unexpected failure (type=%s)" % type(exc).__name__)
        ev.extra["abort"] = "unexpected failure (type=%s)" % type(exc).__name__
        ev.save()
        return 2
    ev.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
