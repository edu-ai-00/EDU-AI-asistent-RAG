"""B06-1 install.py: preflight and plan are offline checks of one explicit commit and one explicit directory.

Run: python tests/test_install_preflight.py   (standard library + git; synthetic repositories only, no Docker, no network)
"""
import ast
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_fixtures as fx  # noqa: E402
from install_fixtures import install  # noqa: E402

import envfile  # noqa: E402  (scripts/ is on sys.path through install_fixtures)

MARKERS = ("HOST_NOT_CHECKED", "INSTALLATION_NOT_STARTED")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo, self.sha = fx.make_repo(self.tmp / "repo")
        self.parent = self.tmp / "cfg"
        self.parent.mkdir()
        if install.IS_POSIX:
            os.chmod(self.parent, 0o700)
        self.target = self.parent / "set"

    def run_cmd(self, cmd="preflight", sha=None, target=None):
        return fx.cli(cmd, self.sha if sha is None else sha, self.target if target is None else target, self.repo)

    def refused_code(self, lines):
        return next((l.split()[1].rstrip(":") for l in lines if l.startswith(("REFUSED", "UNVERIFIED"))), None)


class ArgumentTests(Base):
    def test_every_command_requires_explicit_sha_and_target(self):
        for cmd in ("preflight", "plan", "init-config"):
            for argv in ([cmd], [cmd, "--source-sha", self.sha], [cmd, "--target-dir", str(self.target)]):
                with self.subTest(argv=argv), mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
                    install.main(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_no_default_for_the_commit(self):
        parser = install.build_parser()
        for cmd in ("preflight", "plan", "init-config"):
            args = parser.parse_args([cmd, "--source-sha", "x", "--target-dir", "/x"])
            self.assertEqual(args.source_sha, "x")
        for action in parser._subparsers._group_actions[0].choices.values():
            self.assertIsNone(next(a for a in action._actions if a.dest == "source_sha").default)
            self.assertIsNone(next(a for a in action._actions if a.dest == "target_dir").default)

    def test_default_repo_is_the_checkout_of_the_script_not_the_cwd(self):
        self.assertEqual(Path(install.ROOT), Path(__file__).resolve().parents[1])

    def test_no_force_rotate_or_reveal_option(self):
        parser = install.build_parser()
        flags = {s for sp in parser._subparsers._group_actions[0].choices.values() for a in sp._actions for s in a.option_strings}
        # --config-dir and --source-root are the layout names (scripts/eduai_layout.py) for --target-dir and --repo
        self.assertEqual(flags, {"-h", "--help", "--source-sha", "--target-dir", "--config-dir", "--repo", "--source-root"})
        self.assertEqual(set(parser._subparsers._group_actions[0].choices), {"preflight", "plan", "init-config"})

    def test_sha_must_be_full_lowercase_hex_and_git_is_not_even_called(self):
        bad = ["HEAD", "main", "a19f971", self.sha.upper(), self.sha[:39], self.sha + "0", self.sha + "\n",
               "0" * 64, "", " " + self.sha, "refs/heads/master", self.sha[:20] + "~1"]
        with mock.patch.object(install, "_git", side_effect=AssertionError("git was called")):
            for sha in bad:
                with self.subTest(sha=sha):
                    code, lines = self.run_cmd(sha=sha)
                    self.assertEqual(code, 2)
                    self.assertEqual(self.refused_code(lines), "SOURCE_SHA_NOT_FULL_LOWERCASE_HEX")

    def test_target_must_be_absolute_without_dotdot_and_not_root(self):
        root = os.path.abspath(os.sep)
        for target, code in (("relative/dir", "TARGET_NOT_ABSOLUTE_OR_HAS_DOTDOT"), ("", "TARGET_NOT_ABSOLUTE_OR_HAS_DOTDOT"),
                             (str(self.parent / ".." / "x"), "TARGET_NOT_ABSOLUTE_OR_HAS_DOTDOT"), (root, "TARGET_IS_ROOT")):
            with self.subTest(target=target):
                got, lines = self.run_cmd(target=target)
                self.assertEqual((got, self.refused_code(lines)), (2, code))


class SourceTests(Base):
    def test_valid_commit_passes_and_says_what_was_not_checked(self):
        code, lines = self.run_cmd()
        self.assertEqual(code, 0, lines)
        self.assertTrue(any(l.startswith("OFFLINE_CHECKS: OK") for l in lines))
        self.assertEqual(lines[-2:], list(MARKERS))
        self.assertFalse(any("READY" in l or "CONFIG_PREPARED" in l for l in lines))

    def test_unknown_tag_tree_and_missing_commit_are_refused(self):
        fx.git(self.repo, "tag", "-a", "-m", "t", "v1")
        for sha in ("1" * 40, fx.git(self.repo, "rev-parse", "v1"), fx.git(self.repo, "rev-parse", "HEAD^{tree}")):
            with self.subTest(sha=sha):
                code, lines = self.run_cmd(sha=sha)
                self.assertEqual((code, self.refused_code(lines)), (2, "SOURCE_SHA_NOT_A_LOCAL_COMMIT"))

    def test_the_commit_is_read_not_the_working_tree_and_not_head(self):
        old_sha = self.sha
        (self.repo / "docker-compose.yml").write_text(fx.SYN_COMPOSE.replace("${SYN_SUBNET:?SYN_SUBNET is not set}", "x"), encoding="utf-8")
        fx.git(self.repo, "add", "-A")
        fx.git(self.repo, "commit", "-q", "-m", "second")
        (self.repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")   # dirty worktree
        (self.repo / ".env").write_text("LANGFLOW_SECRET_KEY=SENTINEL_REAL_ENV\n", encoding="utf-8")
        _, first = self.run_cmd("plan", sha=old_sha)
        _, second = self.run_cmd("plan", sha=fx.git(self.repo, "rev-parse", "HEAD"))
        self.assertIn("SYN_SUBNET", fx.field(first, "UNRESOLVED_REQUIRED"))
        self.assertNotIn("SYN_SUBNET", fx.field(second, "UNRESOLVED_REQUIRED") or "")
        self.assertFalse(any("SENTINEL_REAL_ENV" in l for l in first + second))

    def test_missing_files_at_the_commit(self):
        for files, missing in (({".env.example": fx.SYN_TEMPLATE}, "docker-compose.yml"), ({"docker-compose.yml": fx.SYN_COMPOSE}, ".env.example")):
            repo, sha = fx.make_repo(self.tmp / ("r_" + missing), files)
            code, lines = fx.cli("preflight", sha, self.target, repo)
            with self.subTest(missing=missing):
                self.assertEqual((code, self.refused_code(lines)), (2, "SOURCE_FILE_MISSING"))
                self.assertIn(missing, " ".join(lines))

    def test_bad_blobs(self):
        cases = {"lone CR": (b"a\rb\n", 3, "SOURCE_FILE_BAD_CHARACTERS"), "NUL": (b"a\x00b\n", 3, "SOURCE_FILE_BAD_CHARACTERS"),
                 "not UTF-8": (b"\xff\xfe\x00bad\n", 3, "SOURCE_FILE_NOT_UTF8"),
                 "too large": (b"# x\n" + b"#" * (install.MAX_BLOB + 1), 2, "SOURCE_FILE_TOO_LARGE")}
        for name, (blob, want_code, want_reason) in cases.items():
            repo, sha = fx.make_repo(self.tmp / ("b_" + name.replace(" ", "_")), {"docker-compose.yml": blob, ".env.example": fx.SYN_TEMPLATE})
            code, lines = fx.cli("preflight", sha, self.target, repo)
            with self.subTest(case=name):
                self.assertEqual((code, self.refused_code(lines)), (want_code, want_reason))

    def test_crlf_blobs_are_accepted_and_scanned(self):
        repo, sha = fx.make_repo(self.tmp / "crlf", {"docker-compose.yml": fx.SYN_COMPOSE.replace("\n", "\r\n"),
                                                     ".env.example": fx.SYN_TEMPLATE.replace("\n", "\r\n")})
        code, lines = fx.cli("plan", sha, self.target, repo)
        self.assertEqual(code, 0, lines)
        self.assertIn("SYN_SUBNET", fx.field(lines, "UNRESOLVED_REQUIRED"))

    def test_partial_clone_configuration_is_refused_because_it_could_fetch(self):
        for key, value in (("extensions.partialclone", "origin"), ("remote.origin.promisor", "true")):
            fx.git(self.repo, "config", key, value)
            with self.subTest(key=key):
                code, lines = self.run_cmd()
                self.assertEqual((code, self.refused_code(lines)), (2, "PARTIAL_CLONE_UNSUPPORTED"))
            fx.git(self.repo, "config", "--unset", key)

    def test_git_unavailable_or_not_a_repository_cannot_be_verified(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            code, lines = self.run_cmd()
        self.assertEqual((code, self.refused_code(lines)), (3, "GIT_UNAVAILABLE"))
        plain = self.tmp / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            code, lines = fx.cli("preflight", self.sha, self.target, plain)
        self.assertEqual((code, self.refused_code(lines)), (3, "GIT_FAILED"))
        code, lines = fx.cli("preflight", self.sha, self.target, self.tmp / "nope")
        self.assertEqual((code, self.refused_code(lines)), (2, "REPO_NOT_A_DIRECTORY"))

    def test_environment_cannot_redirect_git_to_another_repository(self):
        other, _ = fx.make_repo(self.tmp / "other", {"docker-compose.yml": "x: 1\n", ".env.example": "A=b\n"})
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            code, lines = self.run_cmd("plan")
        self.assertEqual(code, 0, lines)
        self.assertIn("SYN_SUBNET", fx.field(lines, "UNRESOLVED_REQUIRED"))


class ScannerTests(unittest.TestCase):
    def kinds(self, text):
        scan = install.scan_compose(text)
        return {k: sorted(v) for k, v in scan.kinds.items()}, [c for _, c in scan.problems]

    def test_every_supported_form(self):
        got, problems = self.kinds("a: ${P}\nb: ${D:-x}\nc: ${D2-x}\nd: ${R:?e}\ne: ${R2?e}\nf: ${A:+y}\ng: ${A2+y}\nh: $BARE\ni: $$ESC ${PLAIN}\n")
        self.assertEqual(problems, [])
        self.assertEqual(got, {"P": ["plain"], "D": ["default"], "D2": ["default"], "R": ["required"], "R2": ["required"], "A": ["alt"],
                               "A2": ["alt"], "BARE": ["plain"], "PLAIN": ["plain"]})

    def test_required_wins_over_other_forms_of_the_same_name(self):
        got, _ = self.kinds("a: ${V:-x}\nb: ${V:?e}\nc: ${V}\n")
        self.assertEqual(got, {"V": ["default", "plain", "required"]})
        self.assertEqual(install.scan_compose("a: ${V:-x}\nb: ${V:?e}\n").required(), ["V"])

    def test_comments_are_ignored_but_literal_hash_in_quotes_and_blocks_is_not(self):
        text = "# ${C1:?x}\nk: v  # ${C2:?x}\nq: \"a # ${Q:?x}\"\ns: 'it # ${S:?x}'\nblock: |\n  # ${B:?x}\n  line ${B2:?x}\nafter: ${A:-x}\n# ${C3:?x}\n"
        got, problems = self.kinds(text)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(got), ["A", "B", "B2", "Q", "S"])

    def test_block_ends_at_the_next_key_and_folded_scalars_count_too(self):
        got, _ = self.kinds("a: >-\n  ${F:?x}\nb: c # ${GONE:?x}\n")
        self.assertEqual(sorted(got), ["F"])

    def test_unsupported_or_unclear_constructs_are_reported_not_guessed(self):
        cases = {"nested": "a: ${A:-${B}}", "nested required": "a: ${A:-${B:?x}}", "lone dollar": "a: $-x", "bad name": "a: ${1X}",
                 "bad operator": "a: ${A#p}", "unterminated": "a: ${A:-x", "empty op": "a: ${A:}", "multiline quote": 'a: "x\n  y"',
                 "include": "include:\n  - other.yml", "extends": "services:\n  s:\n    extends:\n      file: x.yml", "default holds dollar": "a: ${A:-$B}"}
        for name, text in cases.items():
            with self.subTest(case=name):
                self.assertTrue(install.scan_compose(text).problems, text)

    def test_no_problem_for_apostrophes_in_plain_scalars_or_escapes(self):
        self.assertEqual(install.scan_compose("a: it's fine ${X:-1}\nb: 'it''s' # ${C:?x}\nc: \"q\\\" # ${E:?x}\"\n").problems, [])

    def test_no_references_means_no_claim_of_completeness(self):
        scan = install.scan_compose("services: {}\n")
        self.assertEqual((scan.kinds, scan.required()), ({}, []))


class TemplateTests(Base):
    TEXT = ("# c\nexport A=1\nB = 'two words'\nC=\"q\"\n#D=off\nE=\nF=x=y\n\n  G=  spaced  \n")

    def test_parser_is_the_same_as_envfile_read_env(self):
        path = self.tmp / "t.env"
        path.write_text(self.TEXT, encoding="utf-8")
        values, dups, bad = install.parse_assignments(self.TEXT)
        self.assertEqual(values, envfile.read_env(path))
        self.assertEqual((dups, bad), ([], []))

    def test_duplicates_unparseable_lines_and_dollars(self):
        values, dups, bad = install.parse_assignments("A=1\nA=2\nnot an assignment\n")
        self.assertEqual((dups, bad), (["A"], [3]))
        for text, code in (("A=1\nA=2\n", "TEMPLATE_DUPLICATE_KEY"), ("A=1\nnope\n", "TEMPLATE_UNPARSEABLE_LINE")):
            tmpl = fx.SYN_TEMPLATE + text
            repo, sha = fx.make_repo(self.tmp / ("t_" + code), {"docker-compose.yml": fx.SYN_COMPOSE, ".env.example": tmpl})
            got, lines = fx.cli("preflight", sha, self.target, repo)
            self.assertEqual((got, self.refused_code(lines)), (3, code))

    def test_the_four_secrets_must_be_present_placeholders_and_values_literal(self):
        base = fx.SYN_TEMPLATE
        cases = {"TEMPLATE_LACKS_GENERATED_KEY": base.replace("POSTGRES_PASSWORD=CHANGE_ME_pg\n", ""),
                 "TEMPLATE_GENERATED_KEY_NOT_PLACEHOLDER": base.replace("LANGFLOW_SECRET_KEY=CHANGE_ME_lf_secret", "LANGFLOW_SECRET_KEY=real"),
                 "TEMPLATE_VALUE_HAS_DOLLAR": base + "X=${Y}\n"}
        for code, template in cases.items():
            repo, sha = fx.make_repo(self.tmp / ("s_" + code), {"docker-compose.yml": fx.SYN_COMPOSE, ".env.example": template})
            got, lines = fx.cli("preflight", sha, self.target, repo)
            with self.subTest(code=code):
                self.assertEqual((got, self.refused_code(lines)), (2, code))

    def test_inputs_are_named_never_invented(self):
        code, lines = self.run_cmd("plan")
        self.assertEqual(code, 0, lines)
        self.assertEqual(fx.field(lines, "MISSING_MANUAL"), "OPENAI_API_KEY")
        self.assertEqual(fx.field(lines, "SERVICE_ISSUED_LATER"), "LANGFLOW_API_KEY")
        self.assertEqual(fx.field(lines, "UNRESOLVED_REQUIRED"), "SYN_SUBNET")      # IN_BLOCK is in the template
        self.assertTrue(fx.field(lines, "INPUT_GAPS").startswith("2 "))
        self.assertIn("SYN_OPTIONAL", fx.field(lines, "OPTIONAL_NOT_IN_TEMPLATE"))
        self.assertNotIn("COMMENTED_ONLY", " ".join(lines))
        self.assertEqual(fx.field(lines, "GENERATED_ONCE"), ", ".join(install.GENERATED))

    def test_a_required_name_given_in_the_template_is_not_unresolved(self):
        repo, sha = fx.make_repo(self.tmp / "given", {"docker-compose.yml": fx.SYN_COMPOSE, ".env.example": fx.SYN_TEMPLATE + "SYN_SUBNET=10.1.2.0/29\n"})
        _, lines = fx.cli("plan", sha, self.target, repo)
        self.assertIsNone(fx.field(lines, "UNRESOLVED_REQUIRED"))
        self.assertEqual(fx.field(lines, "INPUT_GAPS").split()[0], "1")

    def test_zero_gaps_still_does_not_claim_a_ready_installation(self):
        repo, sha = fx.make_repo(self.tmp / "none", {"docker-compose.yml": "services: {}\n", ".env.example": fx.SYN_TEMPLATE.replace("OPENAI_API_KEY=CHANGE_ME_openai", "OPENAI_API_KEY=set")})
        code, lines = fx.cli("plan", sha, self.target, repo)
        self.assertEqual(code, 0)
        self.assertTrue(fx.field(lines, "INPUT_GAPS").startswith("0 "))
        self.assertEqual(lines[-2:], list(MARKERS))
        self.assertFalse(any("READY" in l or "COMPLETE" in l for l in lines))


class TargetTests(Base):
    def test_absent_target_with_existing_parent_passes(self):
        self.assertEqual(self.run_cmd()[0], 0)

    def test_missing_parent_is_refused_and_not_created(self):
        code, lines = self.run_cmd(target=self.parent / "no" / "set")
        self.assertEqual((code, self.refused_code(lines)), (2, "TARGET_PARENT_MISSING"))
        self.assertFalse((self.parent / "no").exists())

    def test_every_kind_of_existing_target_is_refused_and_left_untouched(self):
        def empty(p): p.mkdir()
        def foreign(p): p.mkdir(); (p / "x.txt").write_text("keep")
        def partial_env(p): fx.write_config_set(p, "k", with_record=False)
        def partial_record(p): fx.write_config_set(p, "k", with_env=False)
        def a_file(p): p.write_text("keep")
        def a_set(p): fx.write_config_set(p, "key-1")
        cases = {"empty-directory": empty, "foreign-directory": foreign, "partial-config": partial_env, "partial-config (record only)": partial_record,
                 "file": a_file, "config-set": a_set}
        for name, make in cases.items():
            target = self.parent / re.sub(r"\W", "_", name)
            make(target)
            before = fx.snapshot(self.parent)
            for cmd in ("preflight", "plan"):
                with self.subTest(case=name, cmd=cmd):
                    code, lines = self.run_cmd(cmd, target=target)
                    self.assertEqual(code, 2)
                    self.assertEqual(self.refused_code(lines), "TARGET_EXISTS_" + name.split()[0].upper().replace("-", "_"))
                    self.assertFalse(any(l.startswith("WILL_CREATE") for l in lines))
            self.assertEqual(fx.snapshot(self.parent), before)

    def test_symlinks_are_never_followed(self):
        real = self.parent / "real"
        real.mkdir()
        (real / "sentinel").write_text("keep")
        try:
            os.symlink(real, self.parent / "to_dir")
            os.symlink(self.parent / "missing", self.parent / "dangling")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        before = fx.snapshot(self.parent)
        for name in ("to_dir", "dangling"):
            code, lines = self.run_cmd(target=self.parent / name)
            with self.subTest(link=name):
                self.assertEqual((code, self.refused_code(lines)), (2, "TARGET_EXISTS_SYMLINK"))
        self.assertEqual(fx.snapshot(self.parent), before)

    def test_existing_set_reports_the_key_fingerprint_without_the_key(self):
        secret = "SENTINEL-KEY-VALUE-do-not-print"
        wrong = hashlib_hex("something else")
        cases = {"MATCH": {}, "MISMATCH": {"record_key_sha": wrong}, "UNVERIFIABLE": {"record_key_sha": "short"}}
        for expected, extra in cases.items():
            target = self.parent / ("set_" + expected)
            fx.write_config_set(target, secret, **extra)
            code, lines = self.run_cmd("plan", target=target)
            with self.subTest(expected=expected):
                self.assertEqual(code, 2)
                self.assertEqual(fx.field(lines, "EXISTING_KEY_FINGERPRINT"), expected)
                self.assertFalse(any(secret in l for l in lines))

    def test_target_inside_the_repository_is_refused(self):
        (self.repo / "sub").mkdir()
        for target in (self.repo / "sub" / "set", self.repo):
            code, lines = self.run_cmd(target=target)
            with self.subTest(target=str(target)):
                self.assertEqual((code, self.refused_code(lines)), (2, "TARGET_INSIDE_REPOSITORY"))

    def test_symlinked_parent_resolves_to_the_real_directory(self):
        try:
            os.symlink(self.parent, self.tmp / "link_parent")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        code, _ = self.run_cmd(target=self.tmp / "link_parent" / "set")
        self.assertEqual(code, 0)

    @unittest.skipUnless(install.IS_POSIX and os.geteuid() != 0, "POSIX modes, not as root")
    def test_parent_writable_by_others_or_owned_by_another_user_is_refused(self):
        os.chmod(self.parent, 0o770)
        self.assertEqual(self.refused_code(self.run_cmd()[1]), "TARGET_PARENT_WRITABLE_BY_OTHERS")
        os.chmod(self.parent, 0o1777)
        self.assertEqual(self.run_cmd()[0], 0)
        os.chmod(self.parent, 0o700)
        with mock.patch.object(install.os, "geteuid", return_value=4242424):
            self.assertEqual(self.refused_code(self.run_cmd()[1]), "TARGET_PARENT_OWNED_BY_ANOTHER_USER")

    def test_stale_staging_is_reported_and_left_alone(self):
        stale = self.parent / ".set.staging-abc123"
        stale.mkdir()
        (stale / ".env").write_text("keep")
        before = fx.snapshot(self.parent)
        code, lines = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertTrue(any(l.startswith("STALE_STAGING") and ".set.staging-abc123" in l for l in lines))
        self.assertEqual(fx.snapshot(self.parent), before)


def hashlib_hex(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


class OutputTests(Base):
    def test_every_result_carries_both_markers_last(self):
        (self.parent / "exists").mkdir()
        runs = [self.run_cmd("preflight"), self.run_cmd("plan"), self.run_cmd("plan", sha="bad"), self.run_cmd(target=self.parent / "exists")]
        for code, lines in runs:
            self.assertEqual(lines[-2:], list(MARKERS))
        self.assertEqual([c for c, _ in runs], [0, 0, 2, 2])

    def test_no_command_other_than_a_successful_init_config_says_config_prepared(self):
        for code, lines in (self.run_cmd("preflight"), self.run_cmd("plan")):
            self.assertNotIn("CONFIG_PREPARED", lines)


class ReadOnlyAndStaticTests(Base):
    def test_preflight_and_plan_change_nothing_and_use_only_read_only_git(self):
        calls = []
        real = install._git

        def spy(repo, *args):
            calls.append(args)
            return real(repo, *args)

        before = (fx.snapshot(self.repo), fx.snapshot(self.parent))
        with mock.patch.object(install, "_git", spy):
            for cmd in ("preflight", "plan"):
                self.assertEqual(self.run_cmd(cmd)[0], 0)
        self.assertEqual((fx.snapshot(self.repo), fx.snapshot(self.parent)), before)
        self.assertTrue(calls)
        for args in calls:
            self.assertIn(args[0], {"rev-parse", "cat-file", "config"})
        with self.assertRaises(ValueError):
            real(str(self.repo), "fetch", "origin")
        with self.assertRaises(ValueError):
            real(str(self.repo), "cat-file", "blob", "x", "y")

    def test_source_has_no_network_docker_ssh_or_database_and_one_subprocess_call(self):
        tree = ast.parse(Path(install.__file__).read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        banned = {"socket", "ssl", "urllib", "http", "ftplib", "smtplib", "requests", "psycopg2", "sqlite3", "docker", "paramiko", "asyncio",
                  "bootstrap", "create_api_key", "import_flows", "smoke", "migrate"}
        self.assertEqual(imported & banned, set())
        self.assertTrue(imported <= set(sys.stdlib_module_names) | {"envfile"}, imported - set(sys.stdlib_module_names))
        sub_calls = [(fn.name, n.lineno) for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                     for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess" and n.func.attr in {"run", "Popen", "call", "check_call", "check_output"}]
        self.assertEqual([name for name, _ in sub_calls], ["_git"])
        argv = [n for n in ast.walk(tree) if isinstance(n, ast.List) and n.elts and isinstance(n.elts[0], ast.Constant) and n.elts[0].value == "git"]
        self.assertEqual(len(argv), 1)

    def test_secrets_are_never_read_from_the_checkout(self):
        source = Path(install.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ENV_PATH", source)
        (self.repo / ".env").write_text("OPENAI_API_KEY=SENTINEL_REAL_ENV\n", encoding="utf-8")
        for cmd in ("preflight", "plan"):
            self.assertFalse(any("SENTINEL_REAL_ENV" in l for l in self.run_cmd(cmd)[1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
