"""setup.py wizard: order, stopping, refusals, ownership of an existing config, the picker, output hygiene.

Offline: every child tool (install.py, host_preflight.py, bootstrap.py, db_roles.py), docker and git is answered by a FakeHost;
the file system is a real temporary tree. Tests that need symlinks skip where the platform cannot make them
(Windows without developer mode); run them on Linux. Run: python tests/test_setup_wizard.py   (add -v for names)
"""
import contextlib
import hashlib
import io
import os
import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import json
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import install  # noqa: E402
import setup as W  # noqa: E402
from install_fixtures import STUB_VALUES  # noqa: E402
import install_fixtures as fx  # noqa: E402

SHA = "a" * 40
OTHER_SHA = "b" * 40
SECRET = STUB_VALUES["LANGFLOW_SECRET_KEY"]
PG_SECRET = STUB_VALUES["POSTGRES_PASSWORD"]
POOLS_LINE = "NETWORK_POOLS: UNVERIFIED the auto-allocated networks get their addresses from the built-in defaults; NOT measured\n"
ROLES_OK = "DB_CONTRACT: MATCH\n"


def pools_preflight(*extra, accepted=False):
    """Use the real report renderer, including its RESULT summary and marker."""
    report = W.HP.Report()
    report.add("LAYOUT", W.HP.OK, "paths and links are consistent")
    report.add("PERIMETER", W.HP.NOTE, "firewall and public exposure are not checked")
    report.add("NETWORK_POOLS", W.HP.NOTE if accepted else W.HP.UNVERIFIED,
               "accepted by the operator" if accepted else "automatic address pools are not measured")
    for code, level in extra:
        report.add(code, level, "synthetic additional finding")
    return W.Proc(report.exit_code(), "\n".join(report.lines()) + "\n", "")


class PreflightOutputTests(unittest.TestCase):
    def test_real_report_with_only_unverified_pools_can_be_accepted(self):
        self.assertTrue(W.only_pools(pools_preflight()))

    def test_real_report_with_another_problem_cannot_be_accepted(self):
        for code, level in (("MODEL", W.HP.UNVERIFIED), ("PORTS", W.HP.REFUSED)):
            with self.subTest(code=code):
                self.assertFalse(W.only_pools(pools_preflight((code, level))))

    def test_summary_without_a_pools_finding_cannot_be_accepted(self):
        self.assertFalse(W.only_pools(W.Proc(3,
            "RESULT: UNVERIFIED exit=3 refused=0 unverified=1 ok=0 notes=0\n", "")))

    def test_a_different_exit_status_cannot_be_accepted(self):
        report = pools_preflight()
        for rc in (0, 1, 2, 126):
            with self.subTest(rc=rc):
                self.assertFalse(W.only_pools(report._replace(rc=rc)))


def _symlinks_possible() -> bool:
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(d, os.path.join(d, "l"))
            return True
        except (OSError, NotImplementedError):
            return False


SYMLINKS = _symlinks_possible()
needs_links = unittest.skipUnless(SYMLINKS, "needs symlinks (run on Linux)")


SYN_TEMPLATE = "COMPOSE_PROJECT_NAME=edu_ai\nLANGFLOW_SECRET_KEY=\nPOSTGRES_PASSWORD=\nCHAT_LEGAL_ENTITY=CHANGE_ME_legal_entity_code\n"


def make_config(cfg, sha=SHA, project="edu_ai", sources=None, source_root=None, **extra):
    """A config set exactly as init-config leaves it (names, record, key fingerprint); the values are synthetic."""
    cfg = pathlib.Path(cfg)
    cfg.mkdir(parents=True)
    env = {"COMPOSE_PROJECT_NAME": project, "LANGFLOW_SECRET_KEY": SECRET, "POSTGRES_PASSWORD": PG_SECRET,
           "CHAT_LEGAL_ENTITY": "CHANGE_ME_legal_entity_code", **extra}
    (cfg / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    os.chmod(cfg / ".env", 0o600)
    record = {"schema": install.RECORD_SCHEMA, "source_sha": sha, "langflow_secret_key_sha256": install.sha256_hex(SECRET)}
    if sources is not None:
        record["sources"] = sources
    if source_root is not None:
        record["source_root"] = str(source_root)
    (cfg / "install-record.json").write_text(__import__("json").dumps(record), encoding="utf-8")


def snapshot(*roots):
    seen = {}
    for root in roots:
        for base, dirs, files in os.walk(root):
            for name in dirs:
                seen[os.path.join(base, name)] = "dir"
            for name in files:
                path = os.path.join(base, name)
                seen[path] = hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() if not os.path.islink(path) else "link"
    return seen


class FakeHost(W.Host):
    """Answers every outside call from a script; records them in order. Defaults describe a healthy fresh host."""

    def __init__(self, home, root, *, tty=False, answers=(), plat="linux"):
        self.environ = {"HOME": str(home)}
        self.home_dir, self.root = pathlib.Path(home), pathlib.Path(root)
        self.cfg = self.home_dir / "edu-ai" / "config"
        self.calls, self.keys, self.lines, self.asked = [], [], [], []
        self.tty, self.answers, self.plat = tty, list(answers), plat
        self.heads, self.dirty = [SHA], ""
        self.replies = {}                       # key -> Proc | list of Procs (consumed in order) | callable(argv) -> Proc
        self.ip = "192.168.50.7"
        self.container_files = {}               # target -> bytes (a file inside the container); absent -> unreadable
        self.container_reads = []

    # -- recording ------------------------------------------------------------
    def key_of(self, argv):
        argv = [str(a) for a in argv]
        if argv[0] == "docker":
            if argv[1] == "compose" and "config" in argv:
                return "resume-model"
            return "docker-" + argv[1]
        script, rest = pathlib.Path(argv[1]).name, argv[2:]
        if script == "host_preflight.py" and rest[0] == "handoff":
            if "--env-link-only" in rest:
                return "handoff-link"
            return "handoff-accept" if "--accept-unmeasured-network-pools" in rest else "handoff"
        return {"install.py": rest[0] if rest else "", "host_preflight.py": rest[0] if rest else "",
                "bootstrap.py": "bootstrap", "db_roles.py": "roles"}[script]

    def run(self, argv, *, capture=True, timeout=0):
        argv = [str(a) for a in argv]
        key = self.key_of(argv)
        self.calls.append((key, argv, capture))
        self.keys.append(key)
        reply = self.replies.get(key)
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if callable(reply):
            reply = reply(argv)
        return reply if reply is not None else self.default(key, argv)

    def default(self, key, argv):
        if key == "docker-context":
            return W.Proc(0, W.HP.DEFAULT_SOCKET + "\n", "")
        if key == "docker-info":
            return W.Proc(0, "29.9.0\n", "")
        if key == "docker-ps":
            return W.Proc(0, "", "")
        if key == "docker-port":
            return W.Proc(0, "0.0.0.0:80\n", "")
        if key == "resume-model":
            return W.Proc(0, json.dumps(self.resume_model()), "")
        if key == "docker-inspect":
            return W.Proc(0, json.dumps(self.resume_container()) + "\n", "")
        if key == "docker-volume":
            return W.Proc(0, json.dumps({W.HP.PROJECT_LABEL: "edu_ai"}), "")
        if key == "preflight":
            return W.Proc(0, "OFFLINE_CHECKS: OK (sources and target only)\n", "")
        if key == "init-config":
            make_config(argv[argv.index("--target-dir") + 1])
            return W.Proc(0, "CONFIG_PREPARED\n", "")
        if key == "pin-env":                     # the documented contract: only ABSENT keys are appended
            path = self.cfg / ".env"
            have = {line.split("=", 1)[0] for line in path.read_text(encoding="utf-8").splitlines() if "=" in line}
            add = [argv[i + 1] for i, a in enumerate(argv) if a == "--set" and argv[i + 1].split("=", 1)[0] not in have]
            with path.open("a", encoding="utf-8") as handle:
                handle.writelines(pair + "\n" for pair in add)
            return W.Proc(0, "PIN_ENV_ADDED: OK EDU_AI_TUNNEL_SUBNET\n", "")
        if key == "handoff-link":
            link = self.root / ".env"
            if not os.path.lexists(link):
                os.symlink(self.cfg / ".env", link)
            return pools_preflight(accepted="--accept-unmeasured-network-pools" in argv)
        if key == "handoff":
            return pools_preflight()
        if key == "handoff-accept":
            return pools_preflight(accepted=True)
        if key == "bootstrap":
            return W.Proc(0, "", "")
        if key == "roles":
            return W.Proc(0, ROLES_OK, "")
        raise AssertionError(f"unexpected call {key}")

    def resume_model(self):
        return {"name": "edu_ai", "services": {"postgres": {
            "container_name": W.POSTGRES_CONTAINER, "image": "synthetic:1", "networks": {"data": {}},
            "volumes": [{"type": "bind", "source": str(self.home_dir / "edu-ai/state/uploads"),
                         "target": "/synthetic/uploads", "read_only": False},
                        {"type": "volume", "source": "postgres_data", "target": "/synthetic/pg", "read_only": False}]}},
            "volumes": {"postgres_data": {"name": "edu_ai_postgres_data"}}, "networks": {"data": {"name": "edu_ai_data"}}}

    def resume_container(self):
        return {"id": "abc123abc123" + "0" * 52, "name": "/" + W.POSTGRES_CONTAINER,
                "labels": {W.HP.PROJECT_LABEL: "edu_ai", "com.docker.compose.project.working_dir": str(self.root),
                           "com.docker.compose.service": "postgres"},
                "mounts": [{"Type": "bind", "Source": str(self.home_dir / "edu-ai/state/uploads"),
                            "Destination": "/synthetic/uploads", "RW": True},
                           {"Type": "volume", "Name": "edu_ai_postgres_data", "Destination": "/synthetic/pg", "RW": True}],
                "networks": {"edu_ai_data": {}}}

    def container_file(self, container_id, target, limit=1 << 16):
        self.container_reads.append((container_id, target))
        return self.container_files.get(target)

    # -- the outside world ----------------------------------------------------
    def git(self, root, *args):
        if args[:2] == ("rev-parse", "HEAD"):
            head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            return W.Proc(0, head + "\n", "") if head else W.Proc(128, "", "not a git repository")
        return W.Proc(0, self.dirty, "")

    def interactive(self):
        return self.tty

    def ask(self, prompt):
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def say(self, line=""):
        self.lines.append(line)

    def platform(self):
        return self.plat

    def lan_ip(self):
        return self.ip

    @property
    def text(self):
        return "\n".join(self.lines)

    def argv_of(self, key):
        return [argv for k, argv, _ in self.calls if k == key]


class WizardCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = pathlib.Path(self._tmp.name)
        self.home, self.root = base / "home", base / "src"
        self.home.mkdir()
        self.root.mkdir()
        (self.root / "docker-compose.yml").write_text("name: syn\n", encoding="utf-8")
        (self.root / ".env.example").write_text(SYN_TEMPLATE, encoding="utf-8")
        self.host = FakeHost(self.home, self.root)

    def reset(self):
        """Start the same case again on a fresh tree (for sub-tests)."""
        self._tmp.cleanup()
        self.setUp()

    def run_cli(self, *argv, host=None):
        host = host or self.host
        with contextlib.redirect_stdout(io.StringIO()):
            rc = W.main(list(argv), host=host, root=self.root)
        return rc

    def install(self, *extra, host=None):
        return self.run_cli("install", "--no-input", *extra, host=host)

    def started(self, host=None):
        return "bootstrap" in (host or self.host).keys

    def prepare_own(self, **env):
        """An own configuration set at the default place, as an interrupted run would leave it."""
        (self.home / "edu-ai").mkdir(exist_ok=True)
        sources = {rel: install.sha256_hex((self.root / rel).read_bytes()) for rel in (install.COMPOSE_PATH, install.TEMPLATE_PATH)}
        make_config(self.host.cfg, sources=sources, source_root=self.root, **env)


class PlanTests(WizardCase):
    @needs_links
    def test_plan_writes_and_starts_nothing(self):
        before = snapshot(self.home, self.root)
        rc = self.run_cli("plan")
        self.assertEqual(rc, 0, self.host.text)
        self.assertEqual(snapshot(self.home, self.root), before)
        self.assertFalse((self.home / "edu-ai").exists())
        self.assertLessEqual(set(self.host.keys), {"docker-context", "docker-info", "docker-ps", "preflight"})
        self.assertIn("READ_ONLY", self.host.text)
        self.assertIn(SHA, self.host.text)

    def test_plan_of_an_existing_config_is_read_only_too(self):
        self.prepare_own()
        before = snapshot(self.home, self.root)
        self.run_cli("plan")
        self.assertEqual(snapshot(self.home, self.root), before)
        self.assertLessEqual(set(self.host.keys), {"docker-context", "docker-info", "docker-ps"})
        self.assertNotIn(SECRET, self.host.text)
        self.assertNotIn(PG_SECRET, self.host.text)

    def test_plan_reports_refusals_with_status_and_still_writes_nothing(self):
        self.host.dirty = " M docker-compose.yml\n"
        self.assertEqual(self.run_cli("plan"), 2)
        self.assertIn("SOURCE_TREE_NOT_CLEAN", self.host.text)
        self.assertFalse((self.home / "edu-ai").exists())


class OrderAndStopTests(WizardCase):
    @needs_links
    def test_happy_path_runs_the_existing_tools_in_order_and_keeps_chat_separate(self):
        rc = self.install("--accept-unmeasured-network-pools")
        self.assertEqual(rc, 0, self.host.text)
        order = [k for k in self.host.keys if k not in ("docker-context", "docker-info", "docker-ps", "preflight", "docker-port")]
        self.assertEqual(order, ["init-config", "pin-env", "handoff-link", "handoff-accept", "bootstrap", "roles"])
        text = self.host.text
        self.assertIn("BACKEND_READY: yes", text)
        self.assertIn("CHAT_AWAITS_SCHOOL_SELECTION", text)
        self.assertIn("http://localhost/chat/", text)
        self.assertIn("http://192.168.50.7/chat/", text)
        self.assertIn("choose a school", text)
        self.assertIn(SHA, text)

    @needs_links
    def test_bootstrap_is_the_real_one_without_skip_smoke_and_no_value_travels_in_arguments(self):
        self.install("--accept-unmeasured-network-pools")
        (argv,) = self.host.argv_of("bootstrap")
        self.assertIn("--no-input", argv)
        self.assertNotIn("--skip-smoke", argv)
        for _, argv, _ in self.host.calls:
            joined = " ".join(argv)
            self.assertNotIn(SECRET, joined)
            self.assertNotIn(PG_SECRET, joined)
        self.assertNotIn(SECRET, self.host.text)

    @needs_links
    def test_skip_smoke_is_not_an_option(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            W.build_parser().parse_args(["install", "--skip-smoke"])

    @needs_links
    def test_a_failing_child_stops_everything_after_it(self):
        chain = ["init-config", "pin-env", "handoff-link", "handoff-accept", "bootstrap"]
        for index, failing in enumerate(chain):
            with self.subTest(step=failing):
                self.reset()
                self.host.replies[failing] = W.Proc(2 if failing != "bootstrap" else 1, "REFUSED SOMETHING\n", "")
                rc = self.install("--accept-unmeasured-network-pools")
                self.assertEqual(rc, 2 if failing != "bootstrap" else 1)
                self.assertIn("BACKEND_READY: no", self.host.text)
                self.assertNotIn("BACKEND_READY: yes", self.host.text)
                for later in chain[index + 1:] + ["roles"]:
                    self.assertNotIn(later, self.host.keys, f"{later} ran after {failing} failed")

    @needs_links
    def test_a_handoff_refusal_other_than_pools_starts_no_bootstrap(self):
        self.host.replies["handoff-link"] = W.Proc(2, "EXISTING_CONTAINERS: REFUSED already exist (collisions only are listed): edu_ai\n", "")
        self.assertEqual(self.install(), 2)
        self.assertFalse(self.started())

    @needs_links
    def test_a_refused_full_handoff_starts_no_bootstrap_and_keeps_the_state(self):
        self.host.replies["handoff-accept"] = W.Proc(2, "STATE_DIR: REFUSED not empty\n", "")
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 2)
        self.assertFalse(self.started())
        self.assertIn("STATE_KEPT", self.host.text)
        self.assertTrue(self.host.cfg.exists())

    @needs_links
    def test_bootstrap_failure_is_never_relabelled_as_success(self):
        self.host.replies["bootstrap"] = W.Proc(1, "", "")
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 1)
        self.assertNotIn("BACKEND_READY: yes", self.host.text)
        self.assertNotIn("roles", self.host.keys)

    @needs_links
    def test_role_mismatch_is_not_ready(self):
        self.host.replies["roles"] = W.Proc(1, "DB_CONTRACT: 2 deviation(s)\n", "")
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 1)
        self.assertNotIn("BACKEND_READY: yes", self.host.text)

    @needs_links
    def test_head_moving_during_the_run_stops_before_bootstrap(self):
        self.host.heads = [SHA, OTHER_SHA]            # the plan reads HEAD once, the guard before bootstrap again
        rc = self.install("--accept-unmeasured-network-pools")
        self.assertEqual(rc, 2)
        self.assertFalse(self.started())
        self.assertIn("SOURCE_CHANGED_DURING_INSTALL", self.host.text)


class RefusalBeforeAnyWriteTests(WizardCase):
    def assert_untouched(self, rc, code, expect_rc=2):
        self.assertEqual(rc, expect_rc, self.host.text)
        self.assertIn(code, self.host.text)
        self.assertIn("BACKEND_READY: no", self.host.text)
        for step in ("init-config", "pin-env", "handoff-link", "handoff", "handoff-accept", "bootstrap", "roles"):
            self.assertNotIn(step, self.host.keys)
        self.assertFalse((self.home / "edu-ai" / "state").exists())

    def test_docker_group_needs_a_new_login_and_sudo_is_not_suggested(self):
        self.host.replies["docker-info"] = W.Proc(1, "", "permission denied while trying to connect to the Docker daemon socket")
        self.assert_untouched(self.install(), "DOCKER_PERMISSION")
        self.assertIn("log out and in again", self.host.text)
        self.assertFalse(any(a[0] == "sudo" for _, a, _ in self.host.calls))

    def test_missing_docker_points_at_the_host_preparation(self):
        self.host.replies["docker-info"] = W.Proc(127, "", "not found")
        self.assert_untouched(self.install(), "DOCKER_MISSING")

    def test_a_dirty_tree_and_a_non_checkout_are_refused(self):
        self.host.dirty = "?? stray\n"
        self.assert_untouched(self.install(), "SOURCE_TREE_NOT_CLEAN")
        self.reset()
        self.host.heads = [""]
        self.assert_untouched(self.install(), "SOURCE_NOT_A_GIT_CHECKOUT")

    def test_a_non_linux_host_is_refused(self):
        self.host.plat = "win32"
        self.assert_untouched(self.install(), "UNSUPPORTED_PLATFORM")

    def test_a_checkout_that_already_has_a_real_env_is_refused_not_overwritten(self):
        (self.root / ".env").write_text("OPENAI_API_KEY=local-dev\n", encoding="utf-8")
        before = (self.root / ".env").read_bytes()
        self.assert_untouched(self.install(), "LINK_CONFLICT")
        self.assertEqual((self.root / ".env").read_bytes(), before)

    def test_without_a_terminal_and_without_no_input_nothing_is_asked_or_written(self):
        rc = self.run_cli("install")
        self.assertEqual(rc, 2)
        self.assertIn("NOT_INTERACTIVE", self.host.text)
        self.assertNotIn("init-config", self.host.keys)

    def test_a_config_dir_inside_the_checkout_is_refused(self):
        self.assert_untouched(self.install("--config-dir", str(self.root / "cfg")), "CONFIG_DIR_INSIDE_CHECKOUT")

    def test_relative_paths_are_refused(self):
        self.assert_untouched(self.install("--config-dir", "cfg"), "LAYOUT_PATH_NOT_ABSOLUTE")

    def test_inherited_layout_variables_do_not_decide(self):
        self.host.environ["EDU_AI_CONFIG_DIR"] = str(self.root / "evil")
        self.run_cli("plan")
        self.assertNotIn("evil", self.host.text)


class ExistingConfigTests(WizardCase):
    """Never replace, never regenerate, never adopt what is not provably this installation's own."""

    def foreign(self, how):
        (self.home / "edu-ai").mkdir()
        cfg = self.host.cfg
        if how == "empty":
            cfg.mkdir()
        elif how == "foreign-dir":
            cfg.mkdir()
            (cfg / "notes.txt").write_text("mine", encoding="utf-8")
        elif how == "partial":
            cfg.mkdir()
            (cfg / ".env").write_text("A=1\n", encoding="utf-8")
        elif how == "file":
            cfg.write_text("x", encoding="utf-8")
        elif how == "other-project":
            make_config(cfg, project="another")
        elif how == "key-mismatch":
            make_config(cfg)
            (cfg / ".env").write_text("LANGFLOW_SECRET_KEY=" + "x" * 43 + "=\n", encoding="utf-8")

    def test_every_foreign_or_unprovable_config_is_refused_and_left_byte_identical(self):
        for how in ("empty", "foreign-dir", "partial", "file", "other-project", "key-mismatch"):
            with self.subTest(how=how):
                self.reset()
                self.foreign(how)
                before = snapshot(self.home)
                rc = self.install("--accept-unmeasured-network-pools")
                self.assertEqual(rc, 2, self.host.text)
                self.assertEqual(snapshot(self.home), before)
                for step in ("init-config", "pin-env", "handoff-link", "handoff-accept", "bootstrap"):
                    self.assertNotIn(step, self.host.keys)
                self.assertNotIn("BACKEND_READY: yes", self.host.text)

    @needs_links
    def test_resume_never_regenerates_secrets_and_keeps_them_out_of_output(self):
        self.prepare_own()
        before = (self.host.cfg / ".env").read_bytes()
        rc = self.install("--accept-unmeasured-network-pools")
        self.assertEqual(rc, 0, self.host.text)
        self.assertNotIn("init-config", self.host.keys)
        self.assertTrue((self.host.cfg / ".env").read_bytes().startswith(before))      # only appended to, every value kept
        self.assertNotIn(SECRET, self.host.text)
        self.assertNotIn(PG_SECRET, self.host.text)

    def set_recorded_sha(self, value):
        record = self.host.cfg / "install-record.json"
        data = json.loads(record.read_text(encoding="utf-8"))
        data["source_sha"] = value
        record.write_text(json.dumps(data), encoding="utf-8")

    @needs_links
    def test_config_from_an_earlier_commit_of_the_same_installation_continues(self):
        """Editing a file and committing changes the SHA; the installation proven by its own key, project and model is not locked out."""
        for mode in ("plan", "install"):
            with self.subTest(mode=mode):
                self.reset(); self.prepare_own(); self.set_recorded_sha(OTHER_SHA)
                before, env = snapshot(self.home), (self.host.cfg / ".env").read_bytes()
                record = (self.host.cfg / "install-record.json").read_bytes()
                rc = self.run_cli("plan") if mode == "plan" else self.install("--accept-unmeasured-network-pools")
                self.assertEqual(rc, 0, self.host.text)
                self.assertIn("CONFIG_RECORDED_COMMIT: " + OTHER_SHA, self.host.text)
                self.assertNotIn("CONFIG_SOURCES_CHANGED", self.host.text)
                self.assertNotIn("init-config", self.host.keys)
                if mode == "plan":
                    self.assertEqual(snapshot(self.home), before)
                else:                                                  # the fake pin-env appends only absent keys
                    self.assertTrue((self.host.cfg / ".env").read_bytes().startswith(env))
                    self.assertEqual((self.host.cfg / "install-record.json").read_bytes(), record)
                self.assertNotIn(SECRET, self.host.text)

    @needs_links
    def test_an_unusable_recorded_commit_is_never_printed(self):
        for value in (OTHER_SHA + "\nBACKEND_READY: yes", "not-a-sha", 42, None, OTHER_SHA.upper()):
            with self.subTest(value=value):
                self.reset(); self.prepare_own(); self.set_recorded_sha(value)
                self.assertEqual(self.run_cli("plan"), 0, self.host.text)
                self.assertNotIn("CONFIG_RECORDED_COMMIT", self.host.text)
                self.assertNotIn("BACKEND_READY: yes", self.host.text)

    @needs_links
    def test_keys_a_later_template_added_are_appended_and_secrets_generated(self):
        self.prepare_own()
        self.set_recorded_sha(OTHER_SHA)
        (self.root / ".env.example").write_text(SYN_TEMPLATE + "NEW_SETTING=1\nBIFROST_SETUP_TOKEN=CHANGE_ME_bifrost_setup_token\n",
                                                encoding="utf-8")
        env_path = self.host.cfg / ".env"
        before = env_path.read_bytes()
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 0, self.host.text)
        self.assertIn("CONFIG_KEYS_ADDED: BIFROST_SETUP_TOKEN, NEW_SETTING (generated: BIFROST_SETUP_TOKEN)", self.host.text)
        after = env_path.read_text(encoding="utf-8")
        self.assertTrue(after.encode("utf-8").startswith(before.rstrip(b"\n")))      # every earlier line kept as it was
        values, _, _ = install.parse_assignments(after)
        self.assertEqual(values["NEW_SETTING"], "1")
        self.assertTrue(install.valid_generated("BIFROST_SETUP_TOKEN", values["BIFROST_SETUP_TOKEN"]))
        self.assertNotIn(values["BIFROST_SETUP_TOKEN"], self.host.text)
        self.assertEqual((values["LANGFLOW_SECRET_KEY"], values["POSTGRES_PASSWORD"]), (SECRET, PG_SECRET))
        self.assertNotIn("init-config", self.host.keys)

    @needs_links
    def test_plan_names_the_keys_it_would_add_and_writes_nothing(self):
        self.prepare_own()
        (self.root / ".env.example").write_text(SYN_TEMPLATE + "NEW_SETTING=1\n", encoding="utf-8")
        before = snapshot(self.home)
        self.assertEqual(self.run_cli("plan"), 0, self.host.text)
        self.assertIn("WILL_ADD_KEYS: NEW_SETTING", self.host.text)
        self.assertEqual(snapshot(self.home), before)

    @needs_links
    def test_a_configuration_of_another_checkout_is_refused_and_untouched(self):
        self.prepare_own()
        record = self.host.cfg / "install-record.json"
        data = json.loads(record.read_text(encoding="utf-8"))
        data["source_root"] = str(self.root.parent / "other-clone")
        record.write_text(json.dumps(data), encoding="utf-8")
        before = snapshot(self.home)
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 2, self.host.text)
        self.assertIn("CONFIG_FROM_ANOTHER_CHECKOUT", self.host.text)
        self.assertEqual(snapshot(self.home), before)
        self.assertNotIn("pin-env", self.host.keys)

    @needs_links
    def test_an_unusable_source_root_is_refused(self):
        for value in ("", "relative/src", 42, None):
            with self.subTest(value=value):
                self.reset(); self.prepare_own()
                record = self.host.cfg / "install-record.json"
                data = json.loads(record.read_text(encoding="utf-8"))
                data["source_root"] = value
                record.write_text(json.dumps(data), encoding="utf-8")
                before = snapshot(self.home)
                self.assertEqual(self.run_cli("plan"), 2, self.host.text)
                self.assertIn("CONFIG_FROM_ANOTHER_CHECKOUT", self.host.text)
                self.assertEqual(snapshot(self.home), before)

    @needs_links
    def test_a_record_without_source_root_still_needs_the_same_commit(self):
        for recorded, rc in ((SHA, 0), (OTHER_SHA, 2)):
            with self.subTest(recorded=recorded):
                self.reset(); self.prepare_own()
                record = self.host.cfg / "install-record.json"
                data = json.loads(record.read_text(encoding="utf-8"))
                data.pop("source_root"); data["source_sha"] = recorded
                record.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(self.run_cli("plan"), rc, self.host.text)
                if rc:
                    self.assertIn("CONFIG_FROM_ANOTHER_SOURCE", self.host.text)

    @needs_links
    def test_an_added_key_keeps_the_template_quoting_and_a_dollar_is_refused(self):
        self.prepare_own()
        (self.root / ".env.example").write_text(SYN_TEMPLATE + 'QUOTED="a b # c"\n', encoding="utf-8")
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 0, self.host.text)
        text = (self.host.cfg / ".env").read_text(encoding="utf-8")
        self.assertIn('QUOTED="a b # c"\n', text)
        self.assertIn("Added by scripts/setup.py", text)
        self.assertEqual(oct(os.stat(self.host.cfg / ".env").st_mode & 0o777), "0o600")
        self.reset(); self.prepare_own()
        (self.root / ".env.example").write_text(SYN_TEMPLATE + "WITH_DOLLAR=a$b\n", encoding="utf-8")
        before = snapshot(self.home)
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 2, self.host.text)
        self.assertIn("TEMPLATE_VALUE_HAS_DOLLAR: WITH_DOLLAR", self.host.text)
        self.assertEqual(snapshot(self.home), before)

    @needs_links
    def test_changed_sources_with_every_template_key_continue_with_a_note(self):
        self.prepare_own()
        self.set_recorded_sha(OTHER_SHA)
        (self.root / "docker-compose.yml").write_text("name: syn\n# nginx bumped\n", encoding="utf-8")
        (self.root / ".env.example").write_text("# comment only\n" + SYN_TEMPLATE, encoding="utf-8")
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 0, self.host.text)
        self.assertIn("CONFIG_SOURCES_CHANGED: docker-compose.yml, .env.example", self.host.text)

    @needs_links
    def test_resume_with_the_stack_and_links_in_place_repeats_only_bootstrap(self):
        self.prepare_own()
        (self.home / "edu-ai" / "state").mkdir()
        os.symlink(self.host.cfg / ".env", self.root / ".env")
        for name in ("uploads", "langflow_data", "sqllite_export", "bifrost_data"):
            (self.home / "edu-ai" / "state" / name).mkdir()
            os.symlink(self.home / "edu-ai" / "state" / name, self.root / name)
        self.host.replies["docker-ps"] = W.Proc(0, "abc123abc123\n", "")
        rc = self.install()
        self.assertEqual(rc, 0, self.host.text)
        for step in ("init-config", "handoff-link", "handoff", "handoff-accept"):
            self.assertNotIn(step, self.host.keys)
        self.assertIn("bootstrap", self.host.keys)
        self.assertEqual(self.host.keys.count("docker-inspect"), 2)   # inspect and pre-bootstrap ownership proof


class ResumeOwnershipTests(WizardCase):
    def own_running(self):
        self.prepare_own()
        state = self.home / "edu-ai/state"
        state.mkdir()
        os.symlink(self.host.cfg / ".env", self.root / ".env")
        for name in W.L.STATE_DIRS:
            (state / name).mkdir()
            os.symlink(state / name, self.root / name)
        self.host.replies["docker-ps"] = W.Proc(0, "abc123abc123\n", "")

    def test_other_stored_compose_model_is_refused_and_left_unchanged(self):
        for value in ("/other/model.yml", "docker-compose.yml:extra.yml", "docker-compose.override.yml"):
            with self.subTest(value=value):
                self.reset(); self.prepare_own(COMPOSE_FILE=value)
                before = snapshot(self.home, self.root)
                self.assertEqual(self.install(), 2, self.host.text)
                self.assertIn("CONFIG_OTHER_COMPOSE_MODEL", self.host.text)
                self.assertEqual(snapshot(self.home, self.root), before)
                self.assertNotIn("pin-env", self.host.keys); self.assertFalse(self.started())

    @needs_links
    def test_foreign_container_checkout_project_service_or_mount_is_never_started(self):
        changes = (
            lambda c: c["labels"].update({"com.docker.compose.project.working_dir": "/other/checkout"}),
            lambda c: c["labels"].update({W.HP.PROJECT_LABEL: "another"}),
            lambda c: c["labels"].update({"com.docker.compose.service": "unknown"}),
            lambda c: c["mounts"][0].update(Source="/other/state/uploads"),
            lambda c: c["mounts"][0].update(RW=False),
            lambda c: c["mounts"][1].update(Name="another_postgres_data"),
            lambda c: c.update(networks={"another_data": {}}),
        )
        for change in changes:
            with self.subTest(change=change):
                self.reset(); self.own_running()
                value = self.host.resume_container(); change(value)
                self.host.replies["docker-inspect"] = W.Proc(0, json.dumps(value), "")
                self.assertEqual(self.install(), 2, self.host.text)
                self.assertNotIn("pin-env", self.host.keys)
                self.assertFalse(self.started())

    def inline_model(self, content="set_real_ip_from 10.0.0.0/29;\n"):
        """The Compose model of the synthetic service plus one inline-content config (as nginx has its trust range)."""
        model = self.host.resume_model()
        model["services"]["postgres"]["configs"] = [{"source": "trust", "target": "/synthetic/trust.conf"}]
        model["configs"] = {"trust": {"content": content}}
        self.host.replies["resume-model"] = W.Proc(0, json.dumps(model), "")
        return content

    @needs_links
    def test_inline_config_copied_into_the_container_is_proved_by_its_exact_content(self):
        """Compose 5.6.0 copies an inline config into the container: no mount, so only its content can prove it."""
        self.own_running()
        content = self.inline_model()
        self.host.container_files["/synthetic/trust.conf"] = content.encode("utf-8")
        self.assertEqual(self.install(), 0, self.host.text)
        self.assertTrue(self.started())
        self.assertEqual({target for _, target in self.host.container_reads}, {"/synthetic/trust.conf"})
        self.assertTrue(all(cid == "abc123abc123" + "0" * 52 for cid, _ in self.host.container_reads))

    @needs_links
    def test_inline_config_absent_unreadable_or_different_is_refused(self):
        for label, value in (("unreadable", None), ("empty", b""), ("other range", b"set_real_ip_from 10.9.9.0/29;\n"),
                             ("no newline", b"set_real_ip_from 10.0.0.0/29;"), ("extra line", b"set_real_ip_from 10.0.0.0/29;\nx\n")):
            with self.subTest(label):
                self.reset(); self.own_running(); self.inline_model()
                if value is not None:
                    self.host.container_files["/synthetic/trust.conf"] = value
                self.assertEqual(self.install(), 2, self.host.text)
                self.assertIn("RESUME_STACK_OTHER_LAYOUT", self.host.text)
                self.assertNotIn("pin-env", self.host.keys); self.assertFalse(self.started())

    @needs_links
    def test_inline_config_proof_does_not_hide_an_unexpected_extra_mount(self):
        self.own_running()
        content = self.inline_model()
        self.host.container_files["/synthetic/trust.conf"] = content.encode("utf-8")
        value = self.host.resume_container()
        value["mounts"].append({"Type": "bind", "Source": "/etc", "Destination": "/synthetic/extra", "RW": False})
        self.host.replies["docker-inspect"] = W.Proc(0, json.dumps(value), "")
        self.assertEqual(self.install(), 2, self.host.text)
        self.assertFalse(self.started())

    @needs_links
    def test_inline_config_materialised_as_a_read_only_bind_is_still_checked_by_content(self):
        """Earlier Compose versions: the file is mounted, so the old exact mount-and-content check applies."""
        for label, body, read_only, expected in (("same", "set_real_ip_from 10.0.0.0/29;\n", True, 0),
                                                 ("other content", "set_real_ip_from 10.9.9.0/29;\n", True, 2),
                                                 ("writable", "set_real_ip_from 10.0.0.0/29;\n", False, 2)):
            with self.subTest(label):
                self.reset(); self.own_running(); self.inline_model()
                content_file = self.home / "materialised.conf"
                content_file.write_text(body, encoding="utf-8", newline="\n")
                value = self.host.resume_container()
                value["mounts"].append({"Type": "bind", "Source": str(content_file), "Destination": "/synthetic/trust.conf",
                                        "RW": not read_only})
                self.host.replies["docker-inspect"] = W.Proc(0, json.dumps(value), "")
                self.assertEqual(self.install(), expected, self.host.text)
                self.assertEqual(self.started(), expected == 0)
                self.assertEqual(self.host.container_reads, [])      # a mounted config is never read through the container

    @needs_links
    def test_foreign_volume_label_is_never_adopted(self):
        self.own_running()
        self.host.replies["docker-volume"] = W.Proc(0, json.dumps({W.HP.PROJECT_LABEL: "another"}), "")
        self.assertEqual(self.install(), 2, self.host.text)
        self.assertNotIn("pin-env", self.host.keys)
        self.assertFalse(self.started())

    @needs_links
    def test_failed_or_malformed_runtime_metadata_is_unverified_without_leaking(self):
        for key in ("docker-ps", "resume-model", "docker-inspect", "docker-volume"):
            with self.subTest(key=key):
                self.reset(); self.own_running()
                self.host.replies[key] = W.Proc(1, "SYNTHETIC_PRIVATE", "SYNTHETIC_PRIVATE")
                self.assertEqual(self.install(), 3, self.host.text)
                self.assertFalse(self.started()); self.assertNotIn("pin-env", self.host.keys)
                self.assertNotIn("SYNTHETIC_PRIVATE", self.host.text)

    @needs_links
    def test_changed_ownership_after_pin_stops_before_bootstrap(self):
        self.own_running()
        good, bad = self.host.resume_container(), self.host.resume_container()
        bad["labels"]["com.docker.compose.project.working_dir"] = "/other/checkout"
        self.host.replies["docker-inspect"] = [W.Proc(0, json.dumps(good), ""), W.Proc(0, json.dumps(bad), "")]
        self.assertEqual(self.install(), 2, self.host.text)
        self.assertIn("pin-env", self.host.keys)
        self.assertFalse(self.started())

    @needs_links
    def test_plan_proves_resume_with_only_read_operations(self):
        self.own_running()
        before = snapshot(self.home, self.root)
        self.assertEqual(self.run_cli("plan"), 0, self.host.text)
        self.assertEqual(snapshot(self.home, self.root), before)
        self.assertLessEqual(set(self.host.keys), {"docker-context", "docker-info", "docker-ps", "resume-model", "docker-inspect", "docker-volume"})
        self.assertFalse(self.started())


class ContainerFileTests(WizardCase):
    """Host.container_file: one regular file out of `docker cp ID:PATH -`, read-only, bounded, None on anything else."""

    @staticmethod
    def archive(*members):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name, body, kind in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = len(body)
                    tar.addfile(info, io.BytesIO(body))
                else:
                    tar.addfile(info)
        return buffer.getvalue()

    def read(self, stdout, returncode=0, **kwargs):
        with patch.dict(os.environ, {"HOME": str(self.home), "PATH": "/synthetic", "DOCKER_HOST": "tcp://remote.invalid:2375"}, clear=True), \
             patch.object(W.subprocess, "run", return_value=types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")) as run:
            value = W.Host().container_file("abc123", "/etc/x.conf", **kwargs)
        return value, run

    def test_one_regular_file_is_returned_and_the_command_is_read_only_and_local(self):
        value, run = self.read(self.archive(("x.conf", b"a\nb\n", tarfile.REGTYPE)))
        self.assertEqual(value, b"a\nb\n")
        self.assertEqual(run.call_args.args[0], ["docker", "cp", "abc123:/etc/x.conf", "-"])
        self.assertEqual(run.call_args.kwargs["env"]["DOCKER_HOST"], W.HP.DEFAULT_SOCKET)
        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_anything_but_exactly_one_small_regular_file_is_none(self):
        self.assertIsNone(self.read(b"", returncode=1)[0])
        self.assertIsNone(self.read(self.archive(("x.conf", b"a", tarfile.REGTYPE)), returncode=1)[0])
        self.assertIsNone(self.read(self.archive(("d", b"", tarfile.DIRTYPE)))[0])
        self.assertIsNone(self.read(self.archive(("l", b"", tarfile.SYMTYPE)))[0])
        self.assertIsNone(self.read(self.archive(("a", b"1", tarfile.REGTYPE), ("b", b"2", tarfile.REGTYPE)))[0])
        self.assertIsNone(self.read(self.archive(("big", b"x" * 100, tarfile.REGTYPE)), limit=10)[0])
        self.assertIsNone(self.read(b"not a tar archive" * 100)[0])

    def test_compressed_archive_is_not_a_docker_cp_response(self):
        import gzip
        payload = self.archive(("x.conf", b"same config", tarfile.REGTYPE))
        self.assertIsNone(self.read(gzip.compress(payload))[0])

    def test_a_missing_docker_binary_or_a_timeout_is_none(self):
        for error in (FileNotFoundError(), W.subprocess.TimeoutExpired("docker", 60)):
            with self.subTest(error=type(error).__name__), patch.object(W.subprocess, "run", side_effect=error):
                self.assertIsNone(W.Host().container_file("abc123", "/etc/x.conf"))


class ExecutionEnvironmentTests(WizardCase):
    def test_shell_docker_compose_and_interpolated_variables_are_refused_before_writes(self):
        for key, value in (("DOCKER_HOST", "tcp://remote.invalid:2375"), ("DOCKER_CONTEXT", "remote"),
                           ("COMPOSE_FILE", "/other.yml"), ("COMPOSE_PROFILES", "cloudflare"),
                           ("POSTGRES_PASSWORD", "SYNTHETIC_PRIVATE")):
            with self.subTest(key=key):
                self.reset()
                (self.root / "docker-compose.yml").write_text("services:\n  postgres:\n    environment:\n      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n")
                self.host.environ[key] = value
                self.assertEqual(self.install(), 3, self.host.text)
                self.assertNotIn("init-config", self.host.keys); self.assertFalse(self.started())
                self.assertNotIn(value, self.host.text)

    def test_default_docker_context_must_point_to_local_socket(self):
        self.host.replies["docker-context"] = W.Proc(0, "tcp://remote.invalid:2375\n", "")
        self.assertEqual(self.install(), 2, self.host.text)
        self.assertIn("DOCKER_TARGET_NOT_LOCAL", self.host.text)
        self.assertNotIn("init-config", self.host.keys); self.assertFalse(self.started())

    def test_real_host_passes_fixed_environment_and_timeout_to_both_child_modes(self):
        values = {"HOME": str(self.home), "PATH": "/synthetic", "DOCKER_HOST": "tcp://remote.invalid:2375",
                  "DOCKER_CONTEXT": "remote", "COMPOSE_FILE": "/other.yml", "POSTGRES_PASSWORD": "SYNTHETIC_PRIVATE"}
        for capture in (True, False):
            with self.subTest(capture=capture), patch.dict(os.environ, values, clear=True), \
                 patch.object(W.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
                W.Host().run(["synthetic"], capture=capture, timeout=17)
            child = run.call_args.kwargs["env"]
            self.assertEqual(child["DOCKER_HOST"], W.HP.DEFAULT_SOCKET)
            self.assertEqual(child["DOCKER_CONTEXT"], "default")
            self.assertNotIn("COMPOSE_FILE", child); self.assertNotIn("POSTGRES_PASSWORD", child)
            self.assertEqual(run.call_args.kwargs["timeout"], 17)

    @needs_links
    def test_final_git_status_failure_does_not_start_bootstrap(self):
        original = self.host.git
        calls = []
        def git(root, *args):
            if args[:2] == ("status", "--porcelain"):
                calls.append(args)
                if len(calls) > 1:
                    return W.Proc(1, "", "SYNTHETIC_PRIVATE")
            return original(root, *args)
        self.host.git = git
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 3, self.host.text)
        self.assertFalse(self.started())
        self.assertIn("SOURCE_STATUS_FAILED", self.host.text)
        self.assertNotIn("SYNTHETIC_PRIVATE", self.host.text)

    @needs_links
    def test_source_moving_during_successful_bootstrap_is_not_ready(self):
        self.host.heads = [SHA, SHA, OTHER_SHA]
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 2, self.host.text)
        self.assertTrue(self.started())
        self.assertNotIn("BACKEND_READY: yes", self.host.text)
        self.assertIn("SOURCE_CHANGED_DURING_INSTALL", self.host.text)
        self.assertIn("Services may be running", self.host.text)

    @needs_links
    def test_unreadable_source_after_successful_bootstrap_is_not_ready(self):
        original, calls = self.host.git, []
        def git(root, *args):
            if args[:2] == ("status", "--porcelain"):
                calls.append(args)
                if len(calls) > 2:
                    return W.Proc(1, "", "SYNTHETIC_PRIVATE")
            return original(root, *args)
        self.host.git = git
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 3, self.host.text)
        self.assertTrue(self.started())
        self.assertNotIn("BACKEND_READY: yes", self.host.text)
        self.assertIn("SOURCE_STATUS_FAILED", self.host.text)
        self.assertNotIn("SYNTHETIC_PRIVATE", self.host.text)

    def test_a_stack_without_the_matching_links_is_refused_not_adopted(self):
        self.prepare_own()
        self.host.replies["docker-ps"] = W.Proc(0, "abc123\n", "")
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 2)
        self.assertIn("STACK_WITHOUT_LINKS", self.host.text)
        self.assertNotIn("bootstrap", self.host.keys)

    def test_a_stack_of_the_project_without_any_config_is_refused(self):
        self.host.replies["docker-ps"] = W.Proc(0, "abc123\n", "")
        self.assertEqual(self.install(), 2)
        self.assertIn("STACK_WITHOUT_OWN_CONFIG", self.host.text)
        self.assertNotIn("init-config", self.host.keys)


class PickerTests(WizardCase):
    def pin_args(self):
        (argv,) = self.host.argv_of("pin-env")
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--set"]

    @needs_links
    def test_a_new_configuration_gets_the_existing_picker_flag_and_no_school(self):
        self.install("--accept-unmeasured-network-pools")
        sets = self.pin_args()
        self.assertIn("CHAT_ENTITY_PICKER=true", sets)
        self.assertFalse([s for s in sets if s.startswith("CHAT_LEGAL_ENTITY")])

    @needs_links
    def test_an_existing_fixed_school_deployment_is_not_switched(self):
        for extra in ({"CHAT_ENTITY_PICKER": "false", "CHAT_LEGAL_ENTITY": "SKOLA_A"},
                      {"CHAT_LEGAL_ENTITY": "SKOLA_A"},                          # key absent, school fixed
                      {"CHAT_ENTITY_PICKER": "false"}):                          # the operator's explicit choice
            with self.subTest(extra=extra):
                self.reset()
                self.prepare_own(**extra)
                self.install("--accept-unmeasured-network-pools")
                self.assertNotIn("CHAT_ENTITY_PICKER=true", self.pin_args())

    @needs_links
    def test_an_interrupted_new_configuration_without_a_school_still_gets_the_picker(self):
        self.prepare_own()
        self.install("--accept-unmeasured-network-pools")
        self.assertIn("CHAT_ENTITY_PICKER=true", self.pin_args())

    @needs_links
    def test_fixed_school_is_reported_as_chat_ready_not_as_awaiting(self):
        self.prepare_own(CHAT_LEGAL_ENTITY="SKOLA_A", CHAT_ENTITY_PICKER="false")
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 0, self.host.text)
        self.assertIn("CHAT_READY", self.host.text)
        self.assertNotIn("CHAT_AWAITS_SCHOOL_SELECTION", self.host.text)

    def test_chat_state_follows_the_configuration(self):
        placeholder = "CHANGE_ME_legal_entity_code"
        self.assertEqual(W.chat_state({"CHAT_ENTITY_PICKER": "true", "CHAT_LEGAL_ENTITY": placeholder}), "CHAT_AWAITS_SCHOOL_SELECTION")
        self.assertEqual(W.chat_state({"CHAT_LEGAL_ENTITY": "SKOLA_A"}), "CHAT_READY")
        self.assertEqual(W.chat_state({"CHAT_LEGAL_ENTITY": placeholder}), "CHAT_NOT_CONFIGURED")


class NetworkPoolsTests(WizardCase):
    @needs_links
    def test_no_input_without_the_explicit_flag_does_not_accept_and_starts_nothing(self):
        rc = self.install()
        self.assertEqual(rc, 3, self.host.text)
        self.assertIn("NETWORK_POOLS_NOT_ACCEPTED", self.host.text)
        self.assertNotIn("handoff-accept", self.host.keys)
        self.assertFalse(self.started())

    @needs_links
    def test_the_explicit_flag_is_passed_to_every_handoff_and_nothing_is_asked(self):
        self.assertEqual(self.install("--accept-unmeasured-network-pools"), 0)
        for argv in self.host.argv_of("handoff-link") + self.host.argv_of("handoff-accept"):
            self.assertIn("--accept-unmeasured-network-pools", argv)
        self.assertEqual(self.host.asked, [])

    @needs_links
    def test_interactive_acceptance_needs_an_explicit_yes(self):
        host = FakeHost(self.home, self.root, tty=True, answers=["", "ne"])
        self.assertEqual(self.run_cli("install", host=host), 3, host.text)
        self.assertFalse(self.started(host))
        self.assertIn("NETWORK_POOLS", host.text)

    @needs_links
    def test_interactive_yes_records_the_acceptance_and_continues(self):
        host = FakeHost(self.home, self.root, tty=True, answers=["", "yes"])
        self.assertEqual(self.run_cli("install", host=host), 0, host.text)
        self.assertIn("NETWORK_POOLS_ACCEPTED", host.text)
        self.assertIn("bootstrap", host.keys)
        self.assertEqual(len(host.asked), 2)  # plan, then the network-pools question

    @needs_links
    def test_interactive_ano_or_y_does_not_accept_the_pools_finding(self):
        # the question is English now; only the word `yes` accepts (`ano` and `y` do not)
        for answer in ("ano", "y"):
            with self.subTest(answer=answer):
                self.reset()
                host = FakeHost(self.home, self.root, tty=True, answers=["", answer])
                self.assertNotEqual(self.run_cli("install", host=host), 0, host.text)
                self.assertNotIn("NETWORK_POOLS_ACCEPTED", host.text)
                self.assertFalse(self.started(host))

    @needs_links
    def test_only_the_pools_finding_may_be_accepted_not_a_mixed_refusal(self):
        mixed = W.Proc(3, POOLS_LINE + "MODEL: UNVERIFIED the model could not be rendered\n", "")
        self.host.replies["handoff"] = mixed
        self.assertEqual(self.install(), 3)
        self.assertNotIn("handoff-accept", self.host.keys)

    @needs_links
    def test_real_report_with_other_problems_stops_before_the_acceptance_question(self):
        for step in ("handoff-link", "handoff"):
            for code, level in (("MODEL", W.HP.UNVERIFIED), ("PORTS", W.HP.REFUSED)):
                with self.subTest(step=step, code=code):
                    self.reset()
                    host = FakeHost(self.home, self.root, tty=True, answers=["", "yes"])
                    report = pools_preflight((code, level))
                    host.replies[step] = report
                    self.assertEqual(self.run_cli("install", host=host), report.rc, host.text)
                    self.assertEqual(len(host.asked), 1)  # only the plan
                    self.assertNotIn("handoff-accept", host.keys)
                    self.assertFalse(self.started(host))


class QuestionsTests(WizardCase):
    def test_declining_the_plan_and_changing_paths_uses_the_new_ones(self):
        other = self.home / "other"
        other.mkdir()
        host = FakeHost(self.home, self.root, tty=True, answers=["n", str(other / "cfg"), str(other / "data"), "", "n", "", "", "", "n", "", "", ""])
        rc = self.run_cli("install", host=host)
        self.assertEqual(rc, 2)                                                  # never confirmed: nothing created
        self.assertIn(str(other / "cfg"), host.text)
        self.assertNotIn("init-config", host.keys)
        self.assertIn("PLAN_NOT_CONFIRMED", host.text)


@unittest.skipUnless(sys.platform.startswith("linux"), "real init-config needs POSIX publication")
class NewProjectBoundaryTests(WizardCase):
    def real_init_host(self, tamper=False):
        # Use the actual committed template, including its default edu_ai project,
        # with the real init_config publisher and synthetic secret generators.
        template = (W.ROOT / ".env.example").read_text(encoding="utf-8")
        self.root, sha = fx.make_repo(self.home / "repo", {
            ".env.example": template, "docker-compose.yml": fx.SYN_COMPOSE})
        self.host = FakeHost(self.home, self.root)
        self.host.heads = [sha]
        def init(argv):
            report = install.init_config(sha, str(self.host.cfg), str(self.root), gen=fx.STUB_GEN)
            self.assertEqual(report.exit_code(), 0, report.lines)
            self.published_record = (self.host.cfg / "install-record.json").read_bytes()
            self.published_values = install.parse_assignments((self.host.cfg / ".env").read_text())[0]
            if tamper:
                W.set_env(self.host.cfg / ".env", {"LANGFLOW_SECRET_KEY": "synthetic-mismatch"})
            return W.Proc(0, "CONFIG_PREPARED\n", "")
        self.host.replies["init-config"] = init

    def test_custom_project_changes_only_new_template_project_after_real_init_config(self):
        self.real_init_host()
        self.assertEqual(self.published_values if hasattr(self, "published_values") else None, None)
        rc = self.install("--project", "chosen", "--accept-unmeasured-network-pools")
        self.assertEqual(rc, 0, self.host.text)
        self.assertEqual(self.published_values["COMPOSE_PROJECT_NAME"], "edu_ai")
        now = install.parse_assignments((self.host.cfg / ".env").read_text())[0]
        self.assertEqual(now["COMPOSE_PROJECT_NAME"], "chosen")
        self.assertEqual((self.host.cfg / "install-record.json").read_bytes(), self.published_record)
        for key, value in self.published_values.items():
            if key != "COMPOSE_PROJECT_NAME":
                self.assertEqual(now[key], value, key)
        self.assertEqual((self.host.cfg / ".env").stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.started())
        self.assertNotIn(SECRET, self.host.text); self.assertNotIn(PG_SECRET, self.host.text)

    def test_tampered_fresh_key_prevents_project_binding_and_bootstrap(self):
        self.real_init_host(tamper=True)
        self.assertEqual(self.install("--project", "chosen", "--accept-unmeasured-network-pools"), 3, self.host.text)
        now = install.parse_assignments((self.host.cfg / ".env").read_text())[0]
        self.assertEqual(now["COMPOSE_PROJECT_NAME"], "edu_ai")
        self.assertNotIn("pin-env", self.host.keys); self.assertFalse(self.started())


if __name__ == "__main__":
    unittest.main()
