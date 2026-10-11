"""eduai_layout: the one answer to where an installation lives, and its use by install, bootstrap and smoke.

Run: python tests/test_layout.py   (add -v for names)

Offline, synthetic directories only; no Docker, no network. Tests that need symlinks skip where the
platform will not make them (Windows without the privilege) and say so.
"""
import argparse
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import eduai_layout as L  # noqa: E402
import bootstrap  # noqa: E402
import install  # noqa: E402
import smoke  # noqa: E402


def can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as td:
        try:
            os.symlink(td, os.path.join(td, "probe"))
            return True
        except (OSError, NotImplementedError):
            return False


SYMLINKS = can_symlink()


class Tree(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        base = pathlib.Path(os.path.realpath(self._td.name))
        self.src, self.cfg, self.state = base / "src", base / "cfg", base / "state"
        for d in (self.src, self.cfg, self.state):
            d.mkdir()
        (self.src / "docker-compose.yml").write_text("services: {}\n")
        (self.src / ".env.example").write_text("A=1\n")
        (self.cfg / ".env").write_text("A=1\n")
        for name in L.STATE_DIRS:
            (self.state / name).mkdir()

    def parse(self, *argv, environ=None):
        parser = argparse.ArgumentParser()
        L.add_arguments(parser)
        return L.resolve(parser.parse_args(list(argv)), environ or {}, self.src)

    def link_all(self):
        os.symlink(self.cfg / ".env", self.src / ".env")
        for name in L.STATE_DIRS:
            os.symlink(self.state / name, self.src / name)


class ResolveTests(Tree):
    def test_default_is_the_legacy_in_checkout_layout(self):
        lay = self.parse()
        self.assertEqual((lay.source_root, lay.config_dir, lay.state_dir, lay.project),
                         (self.src, self.src, self.src, None))
        self.assertFalse(lay.pinned)
        self.assertEqual(lay.compose_prefix(), ["docker", "compose"])
        self.assertEqual(lay.argv(), [])
        self.assertEqual(lay.env_path, self.src / ".env")
        self.assertEqual(lay.links(), [])

    def test_precedence_flag_then_variable_then_default(self):
        lay = self.parse("--config-dir", str(self.cfg), environ={"EDU_AI_CONFIG_DIR": str(self.state),
                                                                   "EDU_AI_STATE_DIR": str(self.state)})
        self.assertEqual(lay.config_dir, self.cfg)
        self.assertEqual(lay.state_dir, self.state)
        self.assertEqual(lay.origin, {"source_root": "default", "config_dir": "flag",
                                      "state_dir": "env", "project": "default"})

    def test_unsafe_values_are_refused_by_code_and_the_value_is_not_in_the_error(self):
        secret_like = "relative/syn-value-123"
        for field, flag, value, code in (
                ("config_dir", "--config-dir", secret_like, "LAYOUT_PATH_NOT_ABSOLUTE"),
                ("state_dir", "--state-dir", str(self.state) + "/../x", "LAYOUT_PATH_UNSAFE"),
                ("state_dir", "--state-dir", str(self.state) + "\nX", "LAYOUT_PATH_UNSAFE"),
                ("source_root", "--source-root", "", "LAYOUT_PATH_UNSAFE"),
                ("project", "--project", "Bad Name", "LAYOUT_PROJECT_INVALID"),
                ("project", "--project", "-lead", "LAYOUT_PROJECT_INVALID")):
            with self.subTest(flag=flag, value=value):
                with self.assertRaises(L.LayoutError) as ctx:
                    self.parse(f"{flag}={value}")
                self.assertEqual((ctx.exception.code, ctx.exception.field), (code, field))
                self.assertNotIn("syn-value-123", str(ctx.exception))

    def test_pinned_compose_names_project_directory_and_env_file(self):
        lay = self.parse("--project", "edu_ai_lab", "--config-dir", str(self.cfg))
        self.assertEqual(lay.compose_prefix(), ["docker", "compose", "-p", "edu_ai_lab", "--project-directory",
                                                str(self.src), "--env-file", str(self.cfg / ".env")])
        only_project = self.parse("--project", "edu_ai_lab")
        self.assertEqual(only_project.compose_prefix()[:4], ["docker", "compose", "-p", "edu_ai_lab"])

    def test_argv_hands_a_child_the_same_layout(self):
        lay = self.parse("--config-dir", str(self.cfg), "--state-dir", str(self.state), "--project", "p1")
        again = self.parse(*lay.argv())
        self.assertEqual((again.config_dir, again.state_dir, again.project),
                         (lay.config_dir, lay.state_dir, lay.project))

    def test_state_path_follows_the_state_dir_only_for_state_dirs(self):
        lay = self.parse("--state-dir", str(self.state))
        self.assertEqual(lay.state_path("uploads"), self.state / "uploads")
        self.assertEqual(lay.state_path("pgadmin_exports"), self.src / "pgadmin_exports")

    def test_install_keeps_its_names_and_accepts_the_layout_names(self):
        sha = "a" * 40
        old = install.build_parser().parse_args(["plan", "--source-sha", sha, "--target-dir", "/x", "--repo", "/y"])
        new = install.build_parser().parse_args(["plan", "--source-sha", sha, "--config-dir", "/x", "--source-root", "/y"])
        self.assertEqual((old.target_dir, old.repo), (new.target_dir, new.repo))
        for names in L.FLAGS.values():
            if len(names) > 1:
                for option in names:
                    self.assertIn(option, install.build_parser()._subparsers._group_actions[0]
                                  .choices["plan"]._option_string_actions)


@unittest.skipUnless(SYMLINKS, "cannot create symlinks on this platform")
class ValidateTests(Tree):
    def external(self):
        return self.parse("--config-dir", str(self.cfg), "--state-dir", str(self.state))

    def test_complete_links_are_ok(self):
        self.link_all()
        lay = self.external()
        self.assertEqual({link.state for link in lay.links()}, {"OK"})
        self.assertEqual(L.validate(lay), [])
        self.assertEqual(L.verdict([]), L.OK)

    def test_missing_links_refuse_unless_the_caller_will_create_them(self):
        lay = self.external()
        codes = {f.code for f in L.validate(lay)}
        self.assertEqual(codes, {"LINK_MISSING"})
        self.assertEqual(L.validate(lay, links="allow-missing"), [])

    def test_a_real_file_or_directory_is_a_conflict_and_never_allowed(self):
        (self.src / ".env").write_text("A=other\n")
        (self.src / "uploads").mkdir()
        for mode in ("require", "allow-missing"):
            findings = L.validate(self.external(), links=mode)
            self.assertIn("LINK_CONFLICT", {f.code for f in findings})
            self.assertEqual(L.verdict(findings), L.REFUSE)

    def test_wrong_target_and_missing_target(self):
        self.link_all()
        (self.src / ".env").unlink()
        os.symlink(self.state / "uploads", self.src / ".env")            # points at the wrong thing
        (self.cfg / ".env").unlink()
        states = {link.name: link.state for link in self.external().links()}
        self.assertEqual(states[".env"], "WRONG_TARGET")
        (self.src / ".env").unlink()
        os.symlink(self.cfg / ".env", self.src / ".env")                 # right place, nothing there
        self.assertEqual({l.name: l.state for l in self.external().links()}[".env"], "TARGET_MISSING")

    def test_an_unreadable_name_is_unverified_not_free(self):
        self.link_all()
        real = os.lstat

        def deny(path, *a, **k):
            if str(path).endswith(os.sep + "uploads"):
                raise PermissionError("syn")
            return real(path, *a, **k)

        with mock.patch.object(os, "lstat", deny):
            findings = L.validate(self.external())
        self.assertEqual([(f.level, f.code) for f in findings], [(L.UNVERIFIED, "LINK_UNVERIFIABLE")])
        self.assertEqual(L.verdict(findings), L.UNVERIFIED)

    def test_config_or_state_inside_the_checkout_is_refused(self):
        inside = self.src / "cfg"
        inside.mkdir()
        lay = self.parse("--config-dir", str(inside))
        self.assertIn("CONFIG_DIR_INSIDE_CHECKOUT", {f.code for f in L.validate(lay, links="allow-missing")})

    def test_a_directory_that_is_not_a_checkout_is_refused(self):
        (self.src / "docker-compose.yml").unlink()
        self.assertIn("SOURCE_NOT_A_CHECKOUT", {f.code for f in L.validate(self.parse())})

    def test_missing_config_dir_is_refused(self):
        lay = self.parse("--config-dir", str(self.cfg / "absent"))
        self.assertIn("CONFIG_DIR_MISSING", {f.code for f in L.validate(lay, links="allow-missing")})


class ConsumerTests(Tree):
    """bootstrap and smoke take the same layout and pass the same pins to Compose."""

    def setUp(self):
        super().setUp()
        saved = {name: getattr(bootstrap, name) for name in ("ROOT", "ENV_PATH", "ENV_EXAMPLE", "STATE_DIR", "_layout")}
        self.addCleanup(lambda: [setattr(bootstrap, k, v) for k, v in saved.items()])
        smoke_saved = {name: getattr(smoke, name) for name in ("ROOT", "ENV_PATH", "_layout")}
        self.addCleanup(lambda: [setattr(smoke, k, v) for k, v in smoke_saved.items()])

    def args(self, *argv):
        parser = argparse.ArgumentParser()
        L.add_arguments(parser)
        return parser.parse_args(list(argv))

    def captured(self, module, *command):
        with mock.patch.object(module.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            module.run(list(command)) if module is smoke else module.compose(*command[2:])
            return run.call_args.args[0]

    def test_without_a_layout_nothing_changes(self):
        self.assertTrue(bootstrap._apply_layout(self.args()))
        self.assertIsNone(bootstrap._layout)
        self.assertEqual(self.captured(bootstrap, "docker", "compose", "ps"), ["docker", "compose", "ps"])

    @unittest.skipUnless(SYMLINKS, "cannot create symlinks on this platform")
    def test_bootstrap_pins_compose_and_moves_the_data_dirs(self):
        self.link_all()
        self.assertTrue(bootstrap._apply_layout(self.args(
            "--source-root", str(self.src), "--config-dir", str(self.cfg), "--state-dir", str(self.state),
            "--project", "edu_ai_lab")))
        self.assertEqual((bootstrap.ROOT, bootstrap.ENV_PATH, bootstrap.STATE_DIR),
                         (self.src, self.cfg / ".env", self.state))
        self.assertEqual(bootstrap._data_path("langflow_data"), self.state / "langflow_data")
        self.assertEqual(bootstrap._data_path("pgadmin_exports"), self.src / "pgadmin_exports")
        command = self.captured(bootstrap, "docker", "compose", "ps")
        self.assertEqual(command[:4], ["docker", "compose", "-p", "edu_ai_lab"])
        self.assertIn("--env-file", command)
        self.assertEqual(command[-1], "ps")

    def test_bootstrap_refuses_missing_links_before_touching_anything(self):
        before = os.listdir(self.src)
        with mock.patch.object(bootstrap, "report"):
            ok = bootstrap._apply_layout(self.args("--source-root", str(self.src), "--config-dir", str(self.cfg)))
        self.assertFalse(ok)
        self.assertEqual(os.listdir(self.src), before)
        self.assertIsNone(bootstrap._layout)
        bootstrap._failed = False

    def test_bootstrap_passes_the_layout_to_smoke(self):
        bootstrap._layout = L.resolve(self.args("--project", "p1"), {}, self.src)
        with mock.patch.object(bootstrap.subprocess, "run") as run, mock.patch.object(bootstrap, "relay"), \
                contextlib.redirect_stdout(io.StringIO()):
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            bootstrap.step_smoke("http://127.0.0.1:1")
        self.assertEqual(run.call_args.args[0][-2:], ["--project", "p1"])

    def test_smoke_prefixes_only_compose_commands(self):
        smoke._layout = L.resolve(self.args("--project", "p1"), {}, self.src)
        self.assertEqual(self.captured(smoke, "docker", "compose", "ps")[:4], ["docker", "compose", "-p", "p1"])
        self.assertEqual(self.captured(smoke, "docker", "ps"), ["docker", "ps"])


if __name__ == "__main__":
    unittest.main()
