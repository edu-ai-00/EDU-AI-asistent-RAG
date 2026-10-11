"""Offline tests of the lab backup/update/restore driver (tools/lab/cycle). No Docker, no database, standard library.

Run on Linux: python3 tests/test_cycle_driver.py   (the path/bundle/comparison tests also run elsewhere)
Covers refusal of foreign targets and unsafe paths, incomplete or tampered bundles, and the ordering guarantees of
the restore: an early error destroys nothing, a late error never starts the applications.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch
import contextlib
from pathlib import Path

sys.path.insert(0, os.environ.get("CYCLE_DRIVER_DIR") or str(Path(__file__).resolve().parents[1] / "tools" / "lab" / "cycle"))
import cycle_core as C  # noqa: E402

LINUX = hasattr(os, "uname")
FP = "T|public.a|3|aaa\nT|public.b|0|-\nQ|public.s|5\nL|large_objects|0|-\nS|ext|vector|0.7.4\nS|rel|public.a|r|x\n"


ORIG = {n: getattr(C, n) for n in ("run_text", "run_stream", "fingerprint", "manifest_text", "norm_manifest", "untar_from_file", "tool_run",
                                    "egress_present", "egress_ensure", "roles_summary")}


def layout(root, **kw):
    d = dict(src=root + "/ac/src", cfg=root + "/ac/config", state=root + "/ac/state", project="p1", host="h1", backup_root=root + "/bk")
    d.update(kw)
    return C.Layout(**d)


class Paths(unittest.TestCase):
    def test_valid(self):
        C.validate_paths(layout("/home/u"))

    def test_rejections(self):
        for kw in ({"src": "ac/src"}, {"src": "/home/u/../x/src"}, {"cfg": "/home/u/ac/con fig"}, {"state": "/home/u/ac/st:ate"},
                   {"backup_root": "/home/u/ac/state/bk"}, {"state": "/home/u/ac/src/state"}, {"backup_root": "/home/u/ac"},
                   {"project": "Bad Name"}, {"src": "/x"}, {"cfg": "/home/u/ac/src"}):
            with self.assertRaises(C.Abort, msg=str(kw)):
                C.validate_paths(layout("/home/u", **kw))


class Compare(unittest.TestCase):
    def test_equal(self):
        self.assertTrue(C.compare_fp(FP, FP)["ok"])

    def test_differences(self):
        for old, new in (("T|public.a|3|aaa", "T|public.a|4|aaa"), ("T|public.a|3|aaa", "T|public.a|3|bbb"), ("Q|public.s|5", "Q|public.s|6"),
                         ("L|large_objects|0|-", "L|large_objects|1|zz"), ("S|rel|public.a|r|x", "S|rel|public.a|r|y"), ("T|public.b|0|-\n", "")):
            r = C.compare_fp(FP, FP.replace(old, new))
            self.assertFalse(r["ok"], old)

    def test_extra_table_and_item(self):
        self.assertFalse(C.compare_fp(FP, FP + "T|public.c|1|x\n")["ok"])
        self.assertFalse(C.compare_fp(FP, FP + "S|rel|public.z|r|x\n")["ok"])

    def test_acl_order_is_not_a_difference_but_a_privilege_is(self):
        a = FP + "S|schema|x|o|{o=UC/o,a=U/o,b=U/o}\n"
        self.assertTrue(C.compare_fp(a, FP + "S|schema|x|o|{o=UC/o,b=U/o,a=U/o}\n")["ok"])
        self.assertFalse(C.compare_fp(a, FP + "S|schema|x|o|{o=UC/o,b=U/o,a=C/o}\n")["ok"])
        self.assertFalse(C.compare_fp(a, FP + "S|schema|x|o|{o=UC/o,b=U/o}\n")["ok"])

    def test_schema_dump_grant_order_only(self):
        base = "CREATE TABLE a (x int);\nGRANT USAGE ON SCHEMA s TO a;\nGRANT USAGE ON SCHEMA s TO b;\nALTER TABLE a OWNER TO o;\n"
        swapped = "CREATE TABLE a (x int);\nGRANT USAGE ON SCHEMA s TO b;\nGRANT USAGE ON SCHEMA s TO a;\nALTER TABLE a OWNER TO o;\n"
        self.assertTrue(C.schema_dump_equal(base, swapped))
        self.assertFalse(C.schema_dump_equal(base, swapped.replace("TO a;", "TO c;")))
        self.assertFalse(C.schema_dump_equal(base, base.replace("(x int)", "(x bigint)")))
        self.assertFalse(C.schema_dump_equal(base, base + "GRANT USAGE ON SCHEMA s TO c;\n"))

    def test_empty_is_not_ok(self):
        self.assertFalse(C.compare_fp("", "")["ok"])

    def test_manifest_normalisation(self):
        a = "d|755|u:g|1:1|4096|1.5|./x||\nf|644|u:g|1:1|3|1700000000.9|./x/f||\n#HASHES\nabc  ./x/f"
        b = "d|755|u:g|1:1|8192|9.9|./x||\nf|644|u:g|1:1|3|1700000000.1|./x/f||\n#HASHES\nabc  ./x/f"
        self.assertEqual(C.norm_manifest(a), C.norm_manifest(b))
        self.assertNotEqual(C.norm_manifest(a), C.norm_manifest(b.replace("644", "600")))
        self.assertNotEqual(C.norm_manifest(a), C.norm_manifest(b.replace("1700000000.1", "1700000001.1")))
        self.assertNotEqual(C.norm_manifest(a), C.norm_manifest(b.replace("abc", "abd")))
        c = "f|644|u:g|1:1|3|1700000000.5|./a||\nf|644|u:g|1:1|3|1700000000.1|./b||\n#HASHES"
        d = "f|644|u:g|1:1|3|1700000000.0|./b||\nf|644|u:g|1:1|3|1700000000.0|./a||\n#HASHES"
        self.assertEqual(C.norm_manifest(c), C.norm_manifest(d))


def tgz(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "f":
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
            elif kind == "d":
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                t.addfile(ti)
            elif kind == "l":
                ti.type = tarfile.SYMTYPE
                ti.linkname = data
                t.addfile(ti)
            elif kind == "h":
                ti.type = tarfile.LNKTYPE
                ti.linkname = data
                t.addfile(ti)
            elif kind == "b":
                ti.type = tarfile.CHRTYPE
                t.addfile(ti)


def make_bundle(root, name="B1", project="p1", status="COMPLETE"):
    b = Path(root) / name
    (b / "pg").mkdir(parents=True)
    (b / "files").mkdir()
    (b / "meta").mkdir()
    os.chmod(b, 0o700)
    for rel in C.REQUIRED_BUNDLE_FILES:
        p = b / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if rel == "files/config.tgz":
            tgz(str(p), [(".", "d", b""), ("./.env", "f", b"POSTGRES_ADMIN_PASSWORD=x\nK=v\n"), ("./install-record.json", "f", b"{}")])
        elif rel == "files/src.tgz":
            tgz(str(p), [(".", "d", b""), ("./docker-compose.yml", "f", b"services: {}")])
        elif rel.endswith(".tgz"):
            tgz(str(p), [(".", "d", b""), ("./x", "f", b"data")])
        elif rel.endswith("fp_edu_ai.txt") or rel.endswith("fp_langflow.txt"):
            p.write_text(FP)
        elif rel == "pg/databases.txt":
            p.write_text("edu_ai|o|UTF8|c|c\nlangflow|o|UTF8|c|c\n")
        elif rel.endswith(".manifest.txt"):
            p.write_text("M")
        elif rel != "MANIFEST.json":
            p.write_text("content of " + rel)
    (b / "MANIFEST.json").write_text(json.dumps({"format": C.FORMAT, "status": status, "layout": {"project": project}}))
    sums = {str(p.relative_to(b)): hashlib.sha256(p.read_bytes()).hexdigest() for p in b.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    (b / "SHA256SUMS").write_text("".join("%s  %s\n" % (h, r) for r, h in sorted(sums.items())))
    return b


@unittest.skipUnless(LINUX, "needs POSIX permissions")
class Bundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_complete_bundle_passes(self):
        v = C.verify_bundle(str(make_bundle(self.tmp)), expect_project="p1")
        self.assertEqual(v["manifest"]["status"], "COMPLETE")

    def refuse(self, b, **kw):
        with self.assertRaises(C.Abort):
            C.verify_bundle(str(b), **kw)

    def test_missing_files(self):
        for rel in ("images.tar", "pg/globals.sql", "files/uploads.tgz", "pg/fp_langflow.txt", "meta/identity.json"):
            b = make_bundle(self.tmp, "m_" + rel.replace("/", "_"))
            (b / rel).unlink()
            self.refuse(b)

    def test_empty_required_file(self):
        b = make_bundle(self.tmp)
        (b / "pg" / "edu_ai.dump").write_text("")
        self.refuse(b)

    def test_corrupted_content(self):
        b = make_bundle(self.tmp)
        (b / "pg" / "langflow.dump").write_text("tampered")
        self.refuse(b)

    def test_sums_do_not_list_exactly_the_files(self):
        b = make_bundle(self.tmp, "x1")
        (b / "extra.txt").write_text("x")
        self.refuse(b)
        b = make_bundle(self.tmp, "x2")
        lines = (b / "SHA256SUMS").read_text().splitlines()
        (b / "SHA256SUMS").write_text("\n".join(lines[1:]) + "\n")
        self.refuse(b)

    def test_missing_or_malformed_sums(self):
        b = make_bundle(self.tmp, "s1")
        (b / "SHA256SUMS").unlink()
        self.refuse(b)
        b = make_bundle(self.tmp, "s2")
        (b / "SHA256SUMS").write_text("nothex  images.tar\n")
        self.refuse(b)

    def test_incomplete_status_and_foreign_project(self):
        self.refuse(make_bundle(self.tmp, "i1", status="INCOMPLETE"))
        self.refuse(make_bundle(self.tmp, "i2", project="other"), expect_project="p1")

    def test_permissions_and_symlink(self):
        b = make_bundle(self.tmp, "p1x")
        os.chmod(b, 0o755)
        self.refuse(b)
        b = make_bundle(self.tmp, "p2x")
        os.symlink("/etc/passwd", str(b / "meta" / "link"))
        self.refuse(b)

    def test_unsafe_archive_members(self):
        cases = {"abs": [("/etc/x", "f", b"x")], "dotdot": [("../x", "f", b"x")], "nested_dotdot": [("a/../../x", "f", b"x")],
                 "hardlink": [("./a", "f", b"x"), ("./b", "h", "./a")], "device": [("./dev", "b", b"")],
                 "through_symlink": [("./l", "l", "/etc"), ("./l/passwd", "f", b"x")]}
        for n, members in cases.items():
            b = make_bundle(self.tmp, "t_" + n)
            tgz(str(b / "files" / "src.tgz"), members)
            sums = {str(p.relative_to(b)): hashlib.sha256(p.read_bytes()).hexdigest() for p in b.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
            (b / "SHA256SUMS").write_text("".join("%s  %s\n" % (h, r) for r, h in sorted(sums.items())))
            with self.assertRaises(C.Abort, msg=n):
                C.verify_bundle(str(b))


class Fake:
    """Records every command; answers just enough for the gate and the restore."""

    def __init__(self, L, containers=(), fail=None):
        self.L, self.calls, self.fail, self.containers = L, [], fail, list(containers)
        self.volumes = {"%s_%s" % (L.project, v) for v in ("postgres_data",) + C.VOLUME_LABELS}
        self.apps_started = False
        self.bundle = None

    def text(self, argv, **kw):
        self.calls.append(list(argv))
        j = " ".join(argv)
        out, rc = "", 0
        if argv[:3] == ["docker", "context", "show"]:
            out = "default\n"
        elif argv[:2] == ["docker", "info"]:
            out = "%s|%s\n" % (self.L.host, self.L.docker_root)
        elif argv[:3] == ["docker", "ps", "-a"]:
            out = "".join("%s|%s|%s|%s|%s\n" % (c["id"], c["name"], c["project"], c["wd"], c.get("service", "x")) for c in self.containers)
        elif argv[:2] == ["docker", "inspect"]:
            c = next((c for c in self.containers if argv[2] in (c["id"], c["name"])), None)
            if c and ".Config.Image" in argv[-1]:
                tag = "edu_ai_postgres:cs" if c["service"] == "postgres" else "edu_ai:1"
                out = "%s|%s|running|now\n" % (tag, self.images[tag])
            elif c and argv[-1] == "{{.Image}}":
                out = self.images["edu_ai_postgres:cs"] + "\n"
        elif argv[:3] == ["docker", "volume", "inspect"]:
            if argv[3] in self.volumes:
                out = self.L.project + "\n"
            else:
                rc = 1
        elif argv[:3] == ["docker", "volume", "rm"]:
            self.volumes.discard(argv[3])
        elif argv[:3] == ["docker", "volume", "create"]:
            self.volumes.add(argv[-1])
        elif argv[:2] == ["docker", "rm"]:
            self.containers = [c for c in self.containers if c["id"] not in argv[2:]]
        elif argv[:3] == ["docker", "image", "inspect"]:
            out = self.images[argv[3]] + "\n" if argv[3] in self.images else ""
            rc = 0 if argv[3] in self.images else 1
        elif argv[:2] == ["sudo", "install"]:
            os.makedirs(argv[-1], exist_ok=True)
        elif "pg_dump" in argv and "-s" in argv:
            out = (self.bundle / "pg" / ("schema_%s.sql" % argv[-1])).read_text()
        elif argv[:2] == ["docker", "compose"]:
            if "up" in argv and "postgres" in argv and not any(c["service"] == "postgres" for c in self.containers):
                self.containers.append({"id": "cpg", "name": self.L.pg_container, "project": self.L.project,
                                        "wd": self.L.src, "service": "postgres"})
            if "up" in argv and "postgres" not in argv:
                self.apps_started = True
            if "up" in argv and "--force-recreate" in argv:
                self.apps_started = True
        elif "pg_isready" in argv:
            out = "accepting"
        elif "db_roles.py" in j and "verify" in argv:
            out = "MATCH\n" if self.fail != "roles" else "MISMATCH\n"
            rc = 0 if self.fail != "roles" else 1
        elif "db_roles.py" in j and "reapply" in argv:
            rc = 0 if self.fail != "reapply" else 1
        elif argv[:2] == ["docker", "load"]:
            rc = 0 if self.fail != "load" else 1
        if rc and kw.get("check", True):
            raise C.Abort("fake command failed: " + " ".join(argv[:4]))
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def stream(self, argv, **kw):
        self.calls.append(list(argv))
        rc = 1 if (self.fail == "pg_restore" and "pg_restore" in argv and "--exit-on-error" in argv) else 0
        if rc and kw.get("check", True):
            raise C.Abort("fake stream failed")
        return types.SimpleNamespace(returncode=rc, stdout=b"1 entry\n", stderr=b"")


@unittest.skipUnless(LINUX, "needs POSIX")
class Restore(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("cycle", None)
        import cycle as CY
        self.CY = CY
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.L = layout(self.tmp, host=os.uname().nodename, backup_root=self.tmp + "/bk")
        for d in (self.L.src, self.L.cfg, self.L.state):
            os.makedirs(d)
        Path(self.L.src, "docker-compose.yml").write_text("services: {}")
        Path(self.L.cfg, ".env").write_text("POSTGRES_ADMIN_PASSWORD=x\nK=v\n")
        Path(self.L.cfg, "install-record.json").write_text("{}")
        os.symlink(self.L.env_path, self.L.src + "/.env")
        for d in C.STATE_LABELS:
            os.makedirs("%s/%s" % (self.L.state, d))
            os.symlink("%s/%s" % (self.L.state, d), "%s/%s" % (self.L.src, d))
        os.makedirs(self.tmp + "/bk", mode=0o700)
        self.b = make_bundle(self.tmp + "/bk", "B1", project="p1")
        man = json.loads((self.b / "MANIFEST.json").read_text())
        man["layout"] = {"project": "p1", "src": self.L.src, "cfg": self.L.cfg, "state": self.L.state, "host": self.L.host}
        (self.b / "MANIFEST.json").write_text(json.dumps(man))
        self.tags = {"edu_ai_postgres:cs": "sha256:pg", "edu_ai:1": "sha256:ed"}
        (self.b / "images.json").write_text(json.dumps({"tags": self.tags, "containers": {}}))
        (self.b / "meta" / "identity.json").write_text(json.dumps({
            "dirs": {self.L.src: [0o755, os.getuid(), os.getgid()], self.L.cfg: [0o700, os.getuid(), os.getgid()], self.L.state: [0o755, os.getuid(), os.getgid()], self.L.backup_root: [0o700, os.getuid(), os.getgid()]},
            "env_sha256": C.sha256_file(self.L.env_path), "install_record_sha256": C.sha256_file(self.L.cfg + "/install-record.json"), "env_keys": ["K", "POSTGRES_ADMIN_PASSWORD"],
            "git": {"head": "h", "clean": True, "index_sha256": "i"}}))
        links = [(".", "d", b""), ("./docker-compose.yml", "f", b"services: {}"), ("./.env", "l", self.L.env_path)]
        links += [("./" + d, "l", "%s/%s" % (self.L.state, d)) for d in C.STATE_LABELS]
        tgz(str(self.b / "files" / "src.tgz"), links)
        self.reseal()
        self.fake_install()

    def reseal(self):
        sums = {str(p.relative_to(self.b)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.b.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
        (self.b / "SHA256SUMS").write_text("".join("%s  %s\n" % (h, r) for r, h in sorted(sums.items())))

    def fake_install(self, fail=None):
        cont = [{"id": "c1", "name": self.L.pg_container, "project": "p1", "wd": self.L.src, "service": "postgres"}]
        self.fake = Fake(self.L, cont, fail)
        self.fake.images = dict(self.tags)
        self.fake.bundle = self.b
        C.run_text, C.run_stream = self.fake.text, self.fake.stream
        C.fingerprint = lambda L, db, p: FP
        C.manifest_text = lambda image, mount, exclude=None: "M"
        C.norm_manifest = lambda t: [t]
        C.untar_from_file = self.untar
        C.tool_run = lambda *a, **k: types.SimpleNamespace(stdout="", returncode=0)
        C.egress_present = lambda L: {"edge": True, "bifrost_egress": True}
        C.egress_ensure = lambda L: {"edge": True, "bifrost_egress": True}
        C.roles_summary = lambda L: (self.b / "pg" / "roles_summary.txt").read_text()
        self.CY.git_state = lambda src: {"head": "h", "clean": True, "index_sha256": "i"}
        self.addCleanup(self.restore_real)
        self.ev = C.Evidence(self.tmp + "/ev", "restore")

    @staticmethod
    def untar(image, mount, src):
        if os.path.isdir(mount):
            with tarfile.open(src, "r:gz") as t:
                t.extractall(mount, filter="fully_trusted")

    @staticmethod
    def restore_real():
        for n, f in ORIG.items():
            setattr(C, n, f)

    def destructive(self):
        return [c for c in self.fake.calls if c[:2] in (["docker", "rm"], ["docker", "load"]) or c[:3] == ["docker", "volume", "rm"] or
                (c[:2] == ["docker", "compose"] and "stop" in c)]

    def test_corrupted_bundle_destroys_nothing(self):
        (self.b / "pg" / "edu_ai.dump").write_text("tampered")
        with self.assertRaises(C.Abort):
            self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.assertEqual(self.destructive(), [])
        self.assertTrue(os.path.isdir(self.L.src) and os.path.isdir(self.L.state))

    def test_refused_without_confirmation(self):
        with self.assertRaises(C.Abort):
            self.CY.cmd_restore(self.L, "B1", self.ev, False)
        self.assertEqual(self.fake.calls, [])

    def test_foreign_container_refused(self):
        self.fake.containers.append({"id": "c9", "name": "other", "project": "other", "wd": "/x", "service": "s"})
        with self.assertRaises(C.Abort):
            self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.assertEqual(self.destructive(), [])

    def test_container_from_other_directory_refused(self):
        self.fake.containers[0]["wd"] = "/home/someone/else"
        with self.assertRaises(C.Abort):
            self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.assertEqual(self.destructive(), [])

    def test_wrong_host_refused(self):
        L = layout(self.tmp, host="not-this-host", backup_root=self.tmp + "/bk")
        with self.assertRaises(C.Abort):
            self.CY.cmd_restore(L, "B1", self.ev, True)
        self.assertEqual(self.fake.calls, [])

    def test_bundle_of_another_layout_refused(self):
        man = json.loads((self.b / "MANIFEST.json").read_text())
        man["layout"]["state"] = "/home/u/other/state"
        (self.b / "MANIFEST.json").write_text(json.dumps(man))
        self.reseal()
        with self.assertRaises(C.Abort):
            self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.assertEqual(self.destructive(), [])

    def late(self, fail):
        self.fake_install(fail)
        with self.assertRaises(C.Abort):
            self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.assertFalse(self.fake.apps_started, "applications were started after a failed restore")
        self.assertFalse(os.path.exists(self.tmp + "/ev/restore_ok.json"))
        with self.assertRaises((C.Abort, FileNotFoundError)):
            self.CY.cmd_start_apps(self.L, "B1", self.ev)
        self.assertFalse(self.fake.apps_started)

    def test_pg_restore_failure_never_starts_applications(self):
        self.late("pg_restore")

    def test_image_load_failure_never_starts_applications(self):
        self.late("load")

    def test_role_reapply_failure_never_starts_applications(self):
        self.late("reapply")

    def test_role_verify_mismatch_never_starts_applications(self):
        self.late("roles")

    def test_raw_baseline_difference_never_starts_applications(self):
        self.fake_install()
        C.fingerprint = lambda L, db, p: FP.replace("T|public.a|3|aaa", "T|public.a|2|aaa")
        with self.assertRaises(C.Abort):
            self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.assertFalse(self.fake.apps_started)
        self.assertFalse(os.path.exists(self.tmp + "/ev/restore_ok.json"))

    def test_successful_restore_writes_marker_and_still_starts_no_application(self):
        self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.assertTrue(os.path.exists(self.tmp + "/ev/restore_ok.json"))
        self.assertFalse(self.fake.apps_started)
        self.CY.cmd_start_apps(self.L, "B1", self.ev)
        self.assertTrue(self.fake.apps_started)

    def test_verify_restore_refuses_while_an_application_runs(self):
        self.fake.containers[0]["service"] = "edu_ai"
        self.fake.text_inspect_running = True
        orig = C.image_ids
        C.image_ids = lambda L: {"n": {"service": "edu_ai", "status": "running", "id": "x", "tag": "t", "started": ""}}
        self.addCleanup(setattr, C, "image_ids", orig)
        with self.assertRaises(C.Abort):
            self.CY.cmd_verify_restore(self.L, "B1", self.ev)
        self.assertFalse(os.path.exists(self.tmp + "/ev/restore_ok.json"))

    def test_verify_restore_writes_marker_only_when_everything_equal(self):
        orig = C.image_ids
        C.image_ids = lambda L: {L.pg_container: {"service": "postgres", "status": "running", "id": "sha256:pg", "tag": "edu_ai_postgres:cs", "started": ""}}
        self.addCleanup(setattr, C, "image_ids", orig)
        self.CY.cmd_verify_restore(self.L, "B1", self.ev)
        self.assertTrue(os.path.exists(self.tmp + "/ev/restore_ok.json"))
        os.remove(self.tmp + "/ev/restore_ok.json")
        C.fingerprint = lambda L, db, p: FP.replace("Q|public.s|5", "Q|public.s|6")
        with self.assertRaises(C.Abort):
            self.CY.cmd_verify_restore(self.L, "B1", self.ev)
        self.assertFalse(os.path.exists(self.tmp + "/ev/restore_ok.json"))

    def test_marker_of_another_bundle_does_not_start_applications(self):
        self.CY.cmd_restore(self.L, "B1", self.ev, True)
        marker = self.tmp + "/ev/restore_ok.json"
        d = json.load(open(marker))
        d["sums_sha256"] = "0" * 64
        json.dump(d, open(marker, "w"))
        self.fake.apps_started = False
        with self.assertRaises(C.Abort):
            self.CY.cmd_start_apps(self.L, "B1", self.ev)
        self.assertFalse(self.fake.apps_started)

    def test_stale_configuration_consumes_marker_and_never_starts(self):
        self.CY.cmd_restore(self.L, "B1", self.ev, True)
        Path(self.L.env_path).write_text("POSTGRES_ADMIN_PASSWORD=synthetic-changed\nK=v\n")
        with self.assertRaises(C.Abort):
            self.CY.cmd_start_apps(self.L, "B1", self.ev)
        self.assertFalse(self.fake.apps_started)
        self.assertFalse(os.path.exists(self.ev.dir + "/restore_ok.json"))

    def test_changed_image_tag_consumes_marker_and_never_starts(self):
        self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.fake.images["edu_ai:1"] = "sha256:changed"
        with self.assertRaises(C.Abort):
            self.CY.cmd_start_apps(self.L, "B1", self.ev)
        self.assertFalse(self.fake.apps_started)
        self.assertFalse(os.path.exists(self.ev.dir + "/restore_ok.json"))

    def test_changed_database_consumes_marker_and_never_starts(self):
        self.CY.cmd_restore(self.L, "B1", self.ev, True)
        C.fingerprint = lambda L, db, p: FP.replace("T|public.a|3|aaa", "T|public.a|4|bbb")
        with self.assertRaises(C.Abort):
            self.CY.cmd_start_apps(self.L, "B1", self.ev)
        self.assertFalse(self.fake.apps_started)
        self.assertFalse(os.path.exists(self.ev.dir + "/restore_ok.json"))

    def test_marker_of_another_layout_never_starts(self):
        self.CY.cmd_restore(self.L, "B1", self.ev, True)
        marker = Path(self.ev.dir, "restore_ok.json")
        mk = json.loads(marker.read_text()); mk["layout"]["src"] += "-other"
        marker.write_text(json.dumps(mk))
        with self.assertRaises(C.Abort):
            self.CY.cmd_start_apps(self.L, "B1", self.ev)
        self.assertFalse(self.fake.apps_started)

    def test_success_marker_is_single_use(self):
        self.CY.cmd_restore(self.L, "B1", self.ev, True)
        self.CY.cmd_start_apps(self.L, "B1", self.ev)
        self.assertFalse(os.path.exists(self.ev.dir + "/restore_ok.json"))
        with self.assertRaises(C.Abort):
            self.CY.cmd_start_apps(self.L, "B1", self.ev)

    def test_failed_update_invalidates_previous_marker(self):
        self.CY.cmd_restore(self.L, "B1", self.ev, True)
        with self.assertRaises(C.Abort):
            self.CY.cmd_update(self.L, self.L.src, "B1", "h", self.ev)
        self.assertFalse(os.path.exists(self.ev.dir + "/restore_ok.json"))


class DiagnosticBoundary(unittest.TestCase):
    SENTINEL = "SYNTHETIC_PRIVATE_CREDENTIAL_AND_QA_CONTENT"

    def test_wrappers_do_not_include_arguments_or_output(self):
        for name, binary in (("run_text", False), ("run_stream", True)):
            with self.subTest(name=name):
                raw = self.SENTINEL.encode() if binary else self.SENTINEL
                result = types.SimpleNamespace(returncode=1, stdout=raw, stderr=raw)
                with patch.object(C.subprocess, "run", return_value=result):
                    with self.assertRaises(C.Abort) as ctx:
                        getattr(C, name)(["synthetic", self.SENTINEL])
                self.assertNotIn(self.SENTINEL, str(ctx.exception))
                self.assertIn("rc=1", str(ctx.exception))

    def test_timeout_and_oserror_messages_are_not_public(self):
        for name in ("run_text", "run_stream"):
            for error in (C.subprocess.TimeoutExpired(["synthetic", self.SENTINEL], 1,
                                                     output=self.SENTINEL, stderr=self.SENTINEL), OSError(self.SENTINEL)):
                with self.subTest(name=name, error=type(error).__name__):
                    with patch.object(C.subprocess, "run", side_effect=error):
                        with self.assertRaises(C.Abort) as ctx:
                            getattr(C, name)(["synthetic", self.SENTINEL])
                    self.assertNotIn(self.SENTINEL, str(ctx.exception))

    def test_stream_without_output_destination_captures_output(self):
        with patch.object(C.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
            C.run_stream(["synthetic"])
        self.assertEqual(run.call_args.kwargs["stdout"], C.subprocess.PIPE)

    def test_schema_difference_is_a_hash_not_a_default_value(self):
        result = C.compare_fp(FP, FP + "S|col|public.a|password|'" + self.SENTINEL + "'\n")
        self.assertNotIn(self.SENTINEL, json.dumps(result))
        self.assertFalse(result["ok"])

    def test_check_summaries_allow_only_counts_and_booleans(self):
        import cycle_checks as checks
        bad = types.SimpleNamespace(returncode=1, stdout=self.SENTINEL, stderr=self.SENTINEL)
        good = types.SimpleNamespace(returncode=0, stdout=json.dumps({"decrypted": 2, "failed": 0, "plain": 3,
                                    "app_db_connect": True, "error": self.SENTINEL}), stderr="")
        for summary in (checks.decrypt_summary(bad), checks.decrypt_summary(good), checks.export_summary(bad)):
            self.assertNotIn(self.SENTINEL, json.dumps(summary))
        self.assertEqual(checks.decrypt_summary(good), {"decrypted": 2, "failed": 0, "plain": 3, "app_db_connect": True})


class CliGates(unittest.TestCase):
    COMMANDS = (["egress", "ensure"], ["egress", "check"], ["egress", "probe"], ["backup", "B1"],
                ["verify-backup", "B1"], ["restore", "B1", "--confirm-overwrite-own-stack"],
                ["verify-restore", "B1"], ["start-apps", "B1"], ["update", "--from-src", "/home/u/from", "--backup", "B1", "--sha", "h"],
                ["markers", "put", "M1"], ["markers", "check", "M1"], ["compare-live", "B1"])

    def argv(self, ev, command):
        return ["--src", "/home/u/ac/src", "--cfg", "/home/u/ac/config", "--state", "/home/u/ac/state",
                "--project", "p1", "--expect-host", "not-this-host", "--backup-root", "/home/u/bk", "--evidence-dir", ev] + command

    def test_every_command_refuses_wrong_host_before_any_subprocess(self):
        import cycle as CY
        for command in self.COMMANDS:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                output = io.StringIO()
                with patch.object(C.os, "uname", return_value=types.SimpleNamespace(nodename="actual-host"), create=True), \
                     patch.object(C, "run_text") as run, contextlib.redirect_stdout(output):
                    self.assertEqual(CY.main(self.argv(tmp + "/ev", command)), 2)
                run.assert_not_called()

    def test_unsafe_identifier_never_creates_evidence_or_runs_gate(self):
        import cycle as CY
        for bad in ("../escape", "/tmp/escape", "x' y", "x;cat_env", "x\nprivate", ".", "", "x" * 65):
            for command in (["backup", bad], ["markers", "put", bad], ["compare-live", "B1", "--label", bad]):
                with self.subTest(command=command), patch.object(CY, "Evidence") as evidence, patch.object(C, "gate") as gate, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(CY.main(self.argv("unused", command)), 2)
                evidence.assert_not_called(); gate.assert_not_called()

    def test_unexpected_exception_cannot_leak_into_console_or_evidence(self):
        import cycle as CY
        sentinel = DiagnosticBoundary.SENTINEL
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with patch.object(C, "gate", side_effect=RuntimeError(sentinel)), contextlib.redirect_stdout(output):
                self.assertEqual(CY.main(self.argv(tmp + "/ev", ["egress", "ensure"])), 2)
            evidence = "".join(p.read_text() for p in Path(tmp + "/ev").iterdir())
            self.assertNotIn(sentinel, output.getvalue() + evidence)

    def test_export_helper_refuses_before_access(self):
        import cycle_export as export
        args = ["helper", "--src", "/home/u/src", "--cfg", "/home/u/cfg", "--state", "/home/u/state",
                "--project", "p1", "--expect-host", "wrong-host", "--backup-root", "/home/u/bk"]
        with patch.object(sys, "argv", args), patch.object(C, "gate", side_effect=C.Abort("wrong host")), patch.object(export, "sh") as sh:
            with self.assertRaises(C.Abort): export.main()
        sh.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
