"""Fingerprint normalization and manifest consistency (B03e), no database needed.

Run: python tests/test_migrate_fingerprint.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from eduai_migrate import fingerprint as fp  # noqa: E402
from eduai_migrate import manifest, registry  # noqa: E402

LAYOUT = manifest.Layout(ROOT)

SCHEMA_DUMP = """--
-- PostgreSQL database dump
--

-- Dumped from database version 16.3 (Debian 16.3-1.pgdg120+1)
-- Dumped by pg_dump version 16.3 (Debian 16.3-1.pgdg120+1)

\\restrict AbC123

--
-- Name: f(); Type: FUNCTION; Schema: edu_ai; Owner: -
--

CREATE FUNCTION edu_ai.f() RETURNS int
    LANGUAGE plpgsql
    AS $$
BEGIN
  -- keep this comment, it is part of the function body
  RETURN 1;
END;
$$;

\\unrestrict AbC123

-- PostgreSQL database dump complete
"""

CONFIG_DUMP = """--
-- Data for Name: t; Type: TABLE DATA; Schema: edu_ai; Owner: -
--

INSERT INTO edu_ai.t (id, label, created_at, updated_at) OVERRIDING SYSTEM VALUE VALUES (2, 'b, (x)', '2026-01-02 03:04:05+00', NULL);
INSERT INTO edu_ai.t (id, label, created_at, updated_at) OVERRIDING SYSTEM VALUE VALUES (1, 'a
multi-line; ''quoted''', '2026-01-01 00:00:00+00', '2026-02-02 00:00:00+00');
SELECT pg_catalog.setval('edu_ai.t_id_seq', 2, true);
"""


class NormalizationTest(unittest.TestCase):
    def test_schema_dump_drops_only_pg_dump_noise(self):
        out = fp.normalize_schema_dump(SCHEMA_DUMP)
        self.assertNotIn("Dumped", out)
        self.assertNotIn("restrict", out)
        self.assertNotIn("-- Name:", out)
        self.assertIn("-- keep this comment", out)
        self.assertIn("CREATE FUNCTION edu_ai.f()", out)

    def test_schema_fingerprint_ignores_version_and_token_changes(self):
        other = SCHEMA_DUMP.replace("16.3 (Debian 16.3-1.pgdg120+1)", "16.9").replace("AbC123", "ZzZ999")
        self.assertEqual(fp.schema_fingerprint(SCHEMA_DUMP), fp.schema_fingerprint(other))

    def test_banner_lookalikes_inside_bodies_are_content_not_metadata(self):
        def dump(label):
            return ("--\n-- Name: f(); Type: FUNCTION; Schema: edu_ai; Owner: -\n--\n\n"
                    "CREATE FUNCTION edu_ai.f() RETURNS int LANGUAGE plpgsql AS $$\nBEGIN\n"
                    f"-- Name: {label}; Type: FUNCTION\n--\n-- Dumped by pg_dump version {label}\n"
                    "  RETURN 1;\nEND;\n$$;\n")
        a, b = dump("one"), dump("two")
        self.assertNotEqual(fp.schema_fingerprint(a), fp.schema_fingerprint(b))
        self.assertIn("-- Name: one; Type: FUNCTION", fp.normalize_schema_dump(a))
        self.assertIn("-- Dumped by pg_dump version one", fp.normalize_schema_dump(a))
        # the real banner above the function is still removed
        self.assertNotIn("Name: f();", fp.normalize_schema_dump(a))

    def test_banner_lookalikes_inside_string_literals_and_identifiers_are_kept(self):
        def dump(label):
            return ("COMMENT ON TABLE edu_ai.t IS 'first line\n-- Name: " + label + "\n\\restrict " + label +
                    "\n--\nlast line';\nCREATE TABLE edu_ai.\"odd\n-- Name: " + label + "\" (a int);\n")
        self.assertNotEqual(fp.schema_fingerprint(dump("x")), fp.schema_fingerprint(dump("y")))
        self.assertIn("\\restrict x", fp.normalize_schema_dump(dump("x")))

    def test_schema_fingerprint_sees_a_body_change(self):
        other = SCHEMA_DUMP.replace("RETURN 1;", "RETURN 2;")
        self.assertNotEqual(fp.schema_fingerprint(SCHEMA_DUMP), fp.schema_fingerprint(other))

    def test_config_masks_timestamps_and_ignores_row_order(self):
        a = fp.config_fingerprint(CONFIG_DUMP)
        shifted = CONFIG_DUMP.replace("2026-01-02 03:04:05+00", "2030-12-12 12:12:12+00") \
                             .replace("2026-02-02", "2031-01-01")
        self.assertEqual(a, fp.config_fingerprint(shifted))
        lines = CONFIG_DUMP.split("\n")
        reordered = "\n".join([lines[3]] + lines[4:6] + [lines[6]] + lines[7:])
        self.assertEqual(a, fp.config_fingerprint(reordered))

    def test_config_sees_a_value_change_and_keeps_multiline_text(self):
        a = fp.config_fingerprint(CONFIG_DUMP)
        self.assertNotEqual(a, fp.config_fingerprint(CONFIG_DUMP.replace("'b, (x)'", "'b, (y)'")))
        norm = fp.normalize_config_dump(CONFIG_DUMP)
        self.assertIn("a\nmulti-line; ''quoted''", norm)
        self.assertIn("VALUES (1, 'a", norm)
        self.assertIn("'<masked>'", norm)

    def test_config_refuses_an_unrecognised_insert(self):
        with self.assertRaises(ValueError):
            fp.normalize_config_dump("INSERT INTO edu_ai.t SELECT 1;")

    def test_config_refuses_a_column_value_mismatch(self):
        with self.assertRaises(ValueError):
            fp.normalize_config_dump("INSERT INTO edu_ai.t (a, b) VALUES (1);")

    def test_file_hash_folds_crlf_but_not_other_changes(self):
        self.assertEqual(fp.file_sha256(b"a\nb\n"), fp.file_sha256(b"a\r\nb\r\n"))
        self.assertNotEqual(fp.file_sha256(b"a\nb\n"), fp.file_sha256(b"a\nc\n"))

    def test_registry_is_excluded_from_the_schema_dump(self):
        self.assertIn("--exclude-table=edu_ai.schema_migration", fp.SCHEMA_DUMP_ARGS)


class RepositoryConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.base = manifest.load_baseline(LAYOUT)

    def test_seed_tables_equal_the_generator_list(self):
        sh = (ROOT / "scripts" / "gen_clean_install.sh").read_text(encoding="utf-8")
        listed = re.search(r'SEED_TABLES="(.*?)"', sh, re.S).group(1).split()
        self.assertEqual(list(fp.SEED_TABLES), listed)

    def test_generator_does_not_recommend_the_whole_migration_history(self):
        sh = (ROOT / "scripts" / "gen_clean_install.sh").read_text(encoding="utf-8")
        self.assertNotIn("init/*.sql", sh)
        self.assertNotIn("align_existing_db", sh)
        self.assertNotIn("volume rm edu_ai_postgres_data", sh)  # no advice that touches the dev stack
        self.assertIn("ONLY the named migrations", sh)

    def test_spec_depends_on_tracked_files_only(self):
        spec = (ROOT / "specs" / "active" / "spec.migration-registry.md").read_text(encoding="utf-8")
        self.assertNotIn("docs/plans", spec)
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True).stdout.split()
        for line in spec.split("---")[1].splitlines():
            if line.startswith("  - "):
                path = line[4:].strip()
                self.assertTrue(path in tracked or (ROOT / path).exists(), path)

    def test_dockerfile_is_pinned_and_matches_the_manifest(self):
        self.assertEqual(manifest.dockerfile_base_image(LAYOUT), self.base["postgres"]["base_image"])

    def test_manifest_file_hashes_match_the_installation_files(self):
        self.assertEqual([], manifest.verify_init_clean(LAYOUT, self.base))

    @unittest.skipUnless((ROOT / ".git").exists(), "needs the Git history: a distribution copy has none; checked in the source checkout")
    def test_manifest_file_hashes_match_the_committed_blobs(self):
        for f in self.base["files"]:
            blob = manifest.git_blob(LAYOUT, "HEAD", f["path"])
            self.assertEqual(f["sha256"], fp.file_sha256(blob), f["path"])

    def test_changed_installation_file_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(LAYOUT.init_clean, Path(tmp) / "postgres" / "init_clean")
            copy = manifest.Layout(Path(tmp))
            target = copy.init_clean / "01_schema.sql"
            target.write_bytes(target.read_bytes() + b"-- tampered\n")
            problems = manifest.verify_init_clean(copy, self.base)
            self.assertEqual(1, len(problems))
            self.assertIn("01_schema.sql", problems[0])

    def test_extra_or_missing_installation_file_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(LAYOUT.init_clean, Path(tmp) / "postgres" / "init_clean")
            copy = manifest.Layout(Path(tmp))
            (copy.init_clean / "04_extra.sql").write_text("SELECT 1;\n")
            (copy.init_clean / "03_school_operations_safety_domains.sql").unlink()
            joined = " ".join(manifest.verify_init_clean(copy, self.base))
            self.assertIn("04_extra.sql", joined)
            self.assertIn("03_school_operations_safety_domains.sql: file is missing", joined)

    def test_manifest_is_canonical_json_without_timestamps(self):
        raw = LAYOUT.baseline_manifest.read_bytes()
        self.assertEqual(manifest.dump_json(self.base).encode("utf-8"), raw)
        self.assertNotIn(b"\r", raw)
        self.assertNotRegex(raw.decode(), r"generated_at|\d{4}-\d\d-\d\dT")

    @unittest.skipUnless((ROOT / ".git").exists(), "needs the Git history: a distribution copy has none; checked in the source checkout")
    def test_base_commit_exists(self):
        commit = self.base["source"]["base_commit"]
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        out = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "--batch-check=%(objecttype)"],
            input=commit + "\n", capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        # Public and shallow checkouts may omit this historical generation input.
        # The manifest's file hashes remain the authoritative identity check.
        if out.stdout.strip() == commit + " missing":
            self.skipTest("historical generation commit absent; file hashes checked separately")
        self.assertEqual("commit", out.stdout.strip())

    def test_registry_expected_definition_covers_the_ddl(self):
        for needle in ("schema_migration_pkey", "schema_migration_one_baseline", "schema_migration_kind_check",
                       "registry v1"):
            self.assertTrue(any(needle in line for line in registry.EXPECTED_V1), needle)
        self.assertEqual(7, sum(line.startswith("column ") for line in registry.EXPECTED_V1))


class CliTest(unittest.TestCase):
    def test_release_name_cannot_escape_the_releases_directory(self):
        from eduai_migrate import commands
        rc = commands.main(["status", "--user", "u", "--db", "d", "--release", "../x"])
        self.assertEqual(commands.EXIT_USAGE, rc)

    def test_readonly_sessions_set_the_postgres_option(self):
        from eduai_migrate import pg
        argv, _ = pg.Target(db="d", user="u", container="c").psql_argv(readonly=True)
        self.assertIn("PGOPTIONS=-c default_transaction_read_only=on", argv)
        argv, _ = pg.Target(db="d", user="u", container="c").psql_argv(readonly=False)
        self.assertFalse(any(a.startswith("PGOPTIONS") for a in argv))


if __name__ == "__main__":
    unittest.main(verbosity=1)
