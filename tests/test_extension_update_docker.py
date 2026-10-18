"""Docker: bootstrap step 8b on a real database whose image was replaced by the current Dockerfile.postgres.

    EDU_AI_EXT_UPDATE_FROM=<git rev of the previous image> python tests/test_extension_update_docker.py

The revision must have the same PostgreSQL major version. The database is initialised from that revision's Dockerfile.postgres and init_clean, then started on the
current image on the same volume. step_extensions must update the extensions, keep BM25 and vector search
working for edu_ai_search and leave the role contract MATCH (pending Langflow tables aside). Synthetic
data only; everything is named ext_update_test_* and removed afterwards.
"""
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap  # noqa: E402
import db_roles  # noqa: E402

FROM = os.environ.get("EDU_AI_EXT_UPDATE_FROM", "")
NAME = "ext_update_test"
OLD, NEW = f"{NAME}_old:pg", f"{NAME}_new:pg"
PASSWORDS = ("POSTGRES_PASSWORD", "POSTGRES_INGEST_PASSWORD", "POSTGRES_SEARCH_PASSWORD",
             "POSTGRES_LANGFLOW_PASSWORD", "POSTGRES_MIGRATOR_PASSWORD")


def sh(*args, check=True, **kw):
    p = subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if check and p.returncode != 0:
        raise AssertionError(f"{' '.join(args[:4])} failed: {p.stderr.strip()[-400:]}")
    return p


def psql(sql):
    return sh("docker", "exec", "-i", NAME, "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1",
              "-U", "edu_ai_admin", "-d", "edu_ai", input=sql).stdout.strip()


@unittest.skipUnless(FROM and shutil.which("docker"), "set EDU_AI_EXT_UPDATE_FROM=<rev> and have Docker")
class ExtensionUpdateOnARealDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(cls.tmp.name)
        with open(tmp / "old.tar", "wb") as out:
            subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", FROM, "Dockerfile.postgres",
                            "postgres/init_clean"], stdout=out, check=True)
        with tarfile.open(tmp / "old.tar") as t:
            t.extractall(tmp / "old", filter="data")
        with mock.patch.object(bootstrap, "ROOT", tmp / "old"):
            old_major = bootstrap._dockerfile_pg_major()
        if old_major != bootstrap._dockerfile_pg_major():
            cls.tmp.cleanup()
            raise unittest.SkipTest(f"{FROM} is PostgreSQL {old_major}: a major version moves by dump, not by step 8b")
        ctx = tmp / "ctx"
        ctx.mkdir()
        sh("docker", "build", "-q", "-f", str(tmp / "old" / "Dockerfile.postgres"), "-t", OLD, str(ctx))
        sh("docker", "build", "-q", "-f", str(ROOT / "Dockerfile.postgres"), "-t", NEW, str(ctx))
        cls.cleanup()
        sh("docker", "network", "create", "--internal", NAME)
        env = ["-e", "POSTGRES_USER=edu_ai_admin", "-e", "POSTGRES_DB=edu_ai"]
        for key in PASSWORDS:
            env += ["-e", f"{key}={secrets.token_hex(16)}"]
        cls.env = env
        cls.create(OLD)
        for f in sorted((tmp / "old" / "postgres" / "init_clean").iterdir()):   # LF, as the fixture does
            f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n"))
        sh("docker", "cp", str(tmp / "old" / "postgres" / "init_clean") + "/.", f"{NAME}:/docker-entrypoint-initdb.d/")
        sh("docker", "start", NAME)
        cls.wait()
        sh("docker", "stop", NAME)
        cls.create(NEW)
        sh("docker", "start", NAME)
        cls.wait()

    @classmethod
    def create(cls, image):
        sh("docker", "rm", "-f", NAME, check=False)
        sh("docker", "create", "--name", NAME, "--network", NAME, *cls.env,
           "-v", f"{NAME}_data:/var/lib/postgresql/data", image,
           "postgres", "-c", "shared_preload_libraries=pg_search")

    @classmethod
    def wait(cls):
        for _ in range(120):
            # pg_isready on TCP only answers once the init's socket-only server is gone
            if sh("docker", "exec", NAME, "pg_isready", "-h", "127.0.0.1", check=False).returncode == 0:
                try:
                    if int(psql("SELECT count(*) FROM edu_ai.taxonomy_rules;")) > 0:
                        return
                except (AssertionError, ValueError):
                    pass
            time.sleep(2)
        raise AssertionError(sh("docker", "logs", "--tail", "30", NAME, check=False).stderr)

    @classmethod
    def cleanup(cls):
        sh("docker", "rm", "-f", NAME, check=False)
        sh("docker", "volume", "rm", f"{NAME}_data", check=False)
        sh("docker", "network", "rm", NAME, check=False)

    @classmethod
    def tearDownClass(cls):
        cls.cleanup()
        sh("docker", "image", "rm", OLD, NEW, check=False)
        cls.tmp.cleanup()

    def test_step_8b_updates_and_keeps_search_and_the_contract(self):
        def in_container(args, **kw):  # `docker compose exec -T postgres …` -> this container
            if args[:5] == ["docker", "compose", "exec", "-T", "postgres"]:
                args = ["docker", "exec", "-i", NAME, *args[5:]]
            kw.setdefault("input", None)
            return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)

        before = dict(line.split("|") for line in psql(
            "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','pg_search');").splitlines())
        available = dict(line.split("|") for line in psql(
            "SELECT name, default_version FROM pg_available_extensions WHERE name IN ('vector','pg_search');").splitlines())
        self.assertNotEqual(before, available, f"{FROM} has the same extension versions; nothing to test")

        with mock.patch.object(bootstrap, "run", in_container), mock.patch.object(bootstrap, "_failed", False), \
                mock.patch.object(bootstrap, "ENV_PATH", pathlib.Path(self.tmp.name) / "no.env"):
            bootstrap.step_extensions(True)
            self.assertFalse(bootstrap._failed)

        after = dict(line.split("|") for line in psql(
            "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','pg_search');").splitlines())
        self.assertEqual(available, after)
        hard = [d for d in db_roles.verify(NAME, "edu_ai_admin", db_roles.load_matrix()) if not d.startswith("pending: ")]
        self.assertEqual([], hard)


if __name__ == "__main__":
    unittest.main()
