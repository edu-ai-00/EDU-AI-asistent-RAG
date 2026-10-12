"""host_preflight (B06-2): host checks, safe handoff to bootstrap, pin-env. Offline: no Docker, no network, synthetic trees.

Run: python tests/test_host_preflight.py   (add -v for names)

Every command goes through the real whitelist of FakeHost, so an off-list command fails the test that provoked it. Tests that
need symlinks, POSIX modes or Linux behaviour skip on platforms that lack them and say so; the rest run everywhere.
"""
import ast
import contextlib
import io
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from host_preflight_fixtures import (  # noqa: E402
    GIB, POSIX, REPO, SECRET_KEY, SENTINELS, SERVICES, SUBNET, SYMLINKS, FakeActions, FakeHost, Proc, Tree, World, key_of,
    make_model, ss_text)
import bootstrap  # noqa: E402
import eduai_layout as L  # noqa: E402
import envfile  # noqa: E402
import host_preflight as H  # noqa: E402

OK, NOTE, REFUSED, UNVERIFIED = H.OK, H.NOTE, H.REFUSED, H.UNVERIFIED
SOURCE = (REPO / "scripts" / "host_preflight.py").read_text(encoding="utf-8")
SUB = H.TUNNEL_SCRIPT


def text_of(report):
    return "\n".join(report.lines())


class WhitelistTests(unittest.TestCase):
    PROJECT = ["-p", "edu_ai", "--project-directory", "/srv/src", "--env-file", "/srv/cfg/.env"]

    def allowed(self):
        return [
            ["docker", "info", "--format", "{{json .}}"], ["docker", "compose", "version", "--short"],
            ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], ["docker", "volume", "ls", "--format", "{{json .}}"],
            ["docker", "network", "ls", "--no-trunc", "--format", "{{json .}}"], ["docker", "image", "ls", "--format", "{{json .}}"],
            ["ss", "-H", "-ltn"], ["uname", "-m"],
            ["docker", "compose", *self.PROJECT, "config", "--format", "json"],
            ["docker", "compose", "--project-directory", "/s", "--env-file", "/e", "config", "--format", "json"],
            [sys.executable, str(SUB), "check", SUBNET, "--project", "edu_ai", "--routes", "host", "--reserved", "192.168.0.0/16"],
            [sys.executable, str(SUB), "allocate", "--project", "edu_ai", "--routes", "host"]]

    def test_the_read_only_commands_are_allowed(self):
        for argv in self.allowed():
            with self.subTest(argv=argv):
                self.assertIsNone(H.deny_reason(argv))

    def test_destructive_and_unlisted_commands_are_refused(self):
        bad = [
            ["docker", "compose", "down"], ["docker", "compose", "down", "-v"], ["docker", "compose", *self.PROJECT, "down"],
            ["docker", "compose", *self.PROJECT, "down", "--remove-orphans"], ["docker", "compose", *self.PROJECT, "up", "-d"],
            ["docker", "compose", *self.PROJECT, "build"], ["docker", "compose", *self.PROJECT, "run", "x"],
            ["docker", "compose", *self.PROJECT, "exec", "x", "sh"], ["docker", "compose", *self.PROJECT, "rm", "-f"],
            ["docker", "compose", *self.PROJECT, "config", "--format", "json", "--remove-orphans"],
            ["docker", "compose", *self.PROJECT, "config", "--format", "json", "-v"],
            ["docker", "compose", *self.PROJECT, "config", "--format", "yaml"],
            ["docker", "compose", "config", "--format", "json"], ["docker", "compose", "-p", "edu_ai", "config", "--format", "json"],
            ["docker", "compose", "-p", "Bad Name", "--project-directory", "/s", "--env-file", "/e", "config", "--format", "json"],
            ["docker", "volume", "rm", "edu_ai_postgres_data"], ["docker", "network", "rm", "edu_ai_edge"],
            ["docker", "volume", "prune"], ["docker", "system", "prune", "-af"], ["docker", "image", "prune"],
            ["docker", "rm", "-f", "edu_ai"], ["docker", "stop", "edu_ai"], ["docker", "exec", "edu_ai", "sh"],
            ["docker", "run", "--rm", "alpine"], ["docker", "info"], ["docker", "ps"], ["docker", "ps", "-a"],
            ["docker", "network", "inspect", "x"], ["docker", "image", "ls"], ["docker", "compose", "version"],
            ["ss", "-ltn"], ["ss", "-K"], ["uname", "-a"], ["rm", "-rf", "/"], ["sh", "-c", "docker compose down"],
            [sys.executable, str(SUB), "allocate", "--project", "edu_ai", "--routes", "docker", "--helper-image", "x"],
            [sys.executable, str(SUB), "check", SUBNET, "--routes", "docker"],
            [sys.executable, str(SUB), "check", SUBNET, "--helper-image", "x"], [sys.executable, str(SUB), "model-check"],
            [sys.executable, str(SUB), "allocate"], [sys.executable, str(SUB), "check"],
            [sys.executable, str(SUB), "check", "not-a-cidr"], [sys.executable, str(SUB), "check", SUBNET, "--reserved", "x"],
            [sys.executable, "-c", "print(1)"], [sys.executable, str(HERE / "x.py"), "check", SUBNET], [], [""], ["docker", None]]
        for argv in bad:
            with self.subTest(argv=argv):
                self.assertIsNotNone(H.deny_reason(argv))

    def test_the_real_host_refuses_before_anything_runs(self):
        host = H.Host()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("a command was started")):
            for argv in (["docker", "compose", "down"], ["docker", "volume", "rm", "x"], ["docker", "system", "prune"]):
                with self.subTest(argv=argv), self.assertRaises(H.Forbidden):
                    host.run(argv)

    def test_a_fake_host_cannot_bypass_the_whitelist(self):
        with self.assertRaises(H.Forbidden):
            FakeHost().run(["docker", "compose", "down"])

    def test_the_child_environment_is_fixed(self):
        parent = {"PATH": "/usr/bin", "HOME": "/root", "DOCKER_HOST": "tcp://x", "DOCKER_CONTEXT": "c", "COMPOSE_FILE": "x",
                  "EDU_AI_TUNNEL_SUBNET": "x", "POSTGRES_PASSWORD": "SENTINEL", "OPENAI_API_KEY": "SENTINEL"}
        env = H.child_env(parent)
        self.assertEqual(sorted(env), sorted(["PATH", "HOME", "LC_ALL", "LANG", "NO_COLOR", "DOCKER_CLI_HINTS"]))
        self.assertNotIn("SENTINEL", json.dumps(env))

    def test_a_real_child_does_not_inherit_docker_or_compose_variables(self):
        dirty = {"DOCKER_HOST": "tcp://127.0.0.1:1", "COMPOSE_FILE": "x", "POSTGRES_PASSWORD": "SENTINEL"}
        code = "import json,os;print(json.dumps(sorted(os.environ)))"
        with mock.patch.dict(os.environ, dirty):
            proc = H.Host()._exec([sys.executable, "-c", code], None, 30)       # python only: never a docker command
        names = json.loads(proc.stdout)
        self.assertFalse({"DOCKER_HOST", "COMPOSE_FILE", "POSTGRES_PASSWORD"} & set(names), names)

    def test_bootstrap_is_the_only_other_command_and_only_with_layout_flags(self):
        good = [sys.executable, "/s/scripts/bootstrap.py", "--source-root", "/s", "--config-dir", "/c", "--state-dir", "/t", "--project", "p"]
        H.check_bootstrap_argv(good)
        for argv in (["python3", "/s/scripts/bootstrap.py"], [sys.executable, "/s/scripts/other.py"], good + ["--force"],
                     good + ["--no-input", "x"], [sys.executable, "/s/scripts/bootstrap.py", "--build", "x"], good[:-1], []):
            with self.subTest(argv=argv), self.assertRaises(H.Forbidden):
                H.check_bootstrap_argv(argv)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("started")), self.assertRaises(H.Forbidden):
            H.Actions().bootstrap(["rm", "-rf", "/"], "/", {})


class SourceHygieneTests(unittest.TestCase):
    """The source itself: no destructive word as a command argument, nothing removed or replaced, one place per process."""

    def tree(self):
        return ast.parse(SOURCE)

    def test_no_destructive_word_is_a_string_in_the_code_except_the_deny_list(self):
        tree = self.tree()
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "FORBIDDEN_WORDS" for t in node.targets):
                skip.update(id(n) for n in ast.walk(node))
        for node in ast.walk(tree):                     # dict.get("build") reads a Compose key, it is not a command word
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                skip.update(id(a) for a in node.args)
        words = {"down", "rm", "prune", "-v", "--volumes", "--remove-orphans", "up", "build", "run", "exec", "kill", "stop", "create"}
        seen = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
                and n.value in words]
        self.assertEqual(seen, [])

    def test_the_deny_list_covers_the_words_the_brief_names(self):
        for word in ("down", "rm", "prune", "-v", "--remove-orphans", "up", "build", "run", "exec"):
            self.assertIn(word, H.FORBIDDEN_WORDS)

    def test_nothing_is_removed_renamed_or_replaced(self):
        names = {"remove", "unlink", "rmdir", "removedirs", "rename", "renames", "replace", "rmtree", "move", "truncate", "ftruncate"}
        calls = []
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names:
                base = node.func.value
                if isinstance(base, ast.Name) and base.id in ("os", "shutil", "pathlib"):
                    calls.append(f"{base.id}.{node.func.attr}")
        self.assertEqual(calls, [])

    def test_processes_start_in_exactly_two_places(self):
        starts = []
        for fn in ast.walk(self.tree()):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                            and node.func.value.id in ("subprocess", "os") and node.func.attr in (
                            "run", "Popen", "call", "check_call", "check_output", "system", "spawn", "execv", "execvp", "popen"):
                        starts.append(fn.name)
        self.assertEqual(sorted(set(starts)), ["_exec", "bootstrap"])

    def test_no_network_or_docker_modules_are_imported(self):
        imported = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(imported & {"socket", "urllib", "http", "requests", "docker", "ssl", "ftplib", "smtplib", "paramiko"})

    def test_the_source_has_lf_endings_and_no_nul(self):
        raw = (REPO / "scripts" / "host_preflight.py").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertNotIn(b"\0", raw)


class ParserTests(unittest.TestCase):
    def test_ss_reads_every_address_family(self):
        text = ss_text(("0.0.0.0", 80), ("::", 80), ("::1", 8444), ("127.0.0.1", 8099), ("127.0.0.53%lo", 53)) \
            + "LISTEN 0 128 *:9000 *:*\nLISTEN 0 128 [fe80::1%eth0]:22 [::]:*\n"
        self.assertEqual(H.parse_ss(text), {80, 8444, 8099, 53, 9000, 22})
        self.assertEqual(H.parse_ss(""), set())
        self.assertEqual(H.parse_ss("\n\n"), set())

    def test_ss_that_is_not_understood_is_none_never_an_empty_set(self):
        for text in ("garbage", "State Recv-Q Send-Q Local Peer", "LISTEN 0 4096 0.0.0.0:99999 0.0.0.0:*", "LISTEN 0 4096 0.0.0.0:x 0.0.0.0:*",
                     "LISTEN 0 4096", "ESTAB 0 0 1.2.3.4:80 5.6.7.8:1234", "LISTEN 0 4096 0.0.0.0:80 0.0.0.0:*\nbroken line",
                     "LISTEN 0 4096 0.0.0.0:0 0.0.0.0:*", "\x00\xff"):
            with self.subTest(text=text):
                self.assertIsNone(H.parse_ss(text))

    def test_the_model_gives_names_images_and_ports(self):
        m = H.parse_model(make_model(), "edu_ai")
        self.assertEqual(set(m.containers), {c for c, *_ in SERVICES.values()})
        self.assertEqual({p.number for p in m.ports}, {80, 8443, 8444, 8099})
        self.assertEqual(m.networks["edu_ai_tunnel"], "tunnel")
        self.assertEqual(set(m.volumes), {"edu_ai_postgres_data", "edu_ai_pgadmin_data", "edu_ai_reranker_models", "edu_ai_qa_admin_state"})
        self.assertTrue(m.images["edu_ai_postgres:cs"])
        self.assertFalse(m.images["nginx:1.30.5-alpine"])
        self.assertEqual(m.tunnel_subnet, SUBNET)

    def test_a_model_of_another_project_or_a_wrong_shape_is_refused(self):
        for data, code in ((make_model("other"), "MODEL_NAME"), ({}, "MODEL_SHAPE"), ({"name": "edu_ai", "services": {}}, "MODEL_SHAPE"),
                           ([], "MODEL_SHAPE"), ({"name": "edu_ai", "services": {"a": "x"}}, "MODEL_SHAPE"),
                           ({"name": "edu_ai", "services": {"a": {"container_name": "bad name"}}}, "MODEL_SHAPE"),
                           ({"name": "edu_ai", "services": {"a": {"ports": ["80:80"]}}}, "MODEL_SHAPE"),
                           ({"name": "edu_ai", "services": {"a": {"ports": [{"published": "abc"}]}}}, "port"),
                           ({"name": "edu_ai", "services": {"a": {"ports": [{"published": "70000"}]}}}, "port range"),
                           ({"name": "edu_ai", "services": {"a": {}}, "networks": {"n": {"name": "bad name"}}}, "MODEL_SHAPE")):
            with self.subTest(data=str(data)[:60]), self.assertRaises(ValueError) as ctx:
                H.parse_model(data, "edu_ai")
            self.assertEqual(str(ctx.exception), code)

    def test_ranges_udp_dynamic_external_and_a_missing_image_name(self):
        data = {"name": "p", "services": {
            "a": {"ports": [{"published": "9000-9002", "protocol": "tcp"}, {"published": "53", "protocol": "udp"}, {"target": 80}]}},
            "networks": {"n": {"external": True, "name": "shared"}, "m": None}, "volumes": {}}
        m = H.parse_model(data, "p")
        self.assertEqual([p.number for p in m.ports], [9000, 9001, 9002])
        self.assertEqual(m.dynamic_or_udp, ["a"])
        self.assertEqual(m.networks, {"p_m": "m"})
        self.assertEqual(list(m.images), ["p-a:latest"])

    def test_the_fixture_model_has_not_drifted_from_docker_compose_yml(self):
        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"container_name:\s*(\S+)", compose)), {c for c, *_ in SERVICES.values()})
        for service, (cname, image, built, ports) in SERVICES.items():
            self.assertIn(f"image: {image}", compose, service)
            self.assertEqual(built, bool(re.search(rf"^  {service}:\n(?:(?!^  \S).*\n)*?    build:", compose, re.M)), service)
        for published in ("80:80", "8443:8443", "8444:8444"):
            self.assertIn(f'"{published}"', compose)
        self.assertIn("${BIFROST_BIND:-127.0.0.1}:${BIFROST_PORT:-8099}:8080", compose)
        for key in ("edge", "app", "data", "bifrost_egress", "tunnel", "tunnel_egress"):
            self.assertRegex(compose, rf"(?m)^  {key}:\n    driver: bridge", key)
        for key in ("postgres_data", "pgadmin_data", "reranker_models", "qa_admin_state"):
            self.assertRegex(compose, rf"(?m)^  {key}:", key)

    def test_the_project_pattern_matches_the_layout_one_and_state_dirs_have_a_mode(self):
        self.assertEqual(H._PROJECT_RE.pattern, L._PROJECT_RE.pattern)
        self.assertTrue(set(L.STATE_DIRS) <= set(bootstrap.DATA_DIRS))


class CheckTests(Tree):
    def expect(self, report, code, level):
        self.assertIn(level, self.level(report, code), "\n" + text_of(report))

    def test_a_clean_host_passes_and_says_what_it_checked(self):
        r = self.check()
        self.assertEqual(r.exit_code(), 0, text_of(r))
        for code in ("PLATFORM", "ARCH_HOST", "DOCKER_DAEMON", "DOCKER_CONTEXT", "DOCKER_OS", "ARCH_DOCKER", "COMPOSE_VERSION", "CONFIG_DIR",
                     "KEY_FINGERPRINT", "COMPOSE_FILE", "PROJECT", "TUNNEL_SUBNET_PIN", "MODEL", "MODEL_OVERRIDE", "EXISTING_CONTAINERS", "EXISTING_VOLUMES",
                     "EXISTING_NETWORKS", "EXISTING_IMAGES", "STATE_DIR", "PORTS", "TUNNEL_SUBNET", "DISK_DOCKER_ROOT", "DISK_STATE_DIR"):
            self.assertEqual(self.level(r, code), [OK], code)
        lines = r.lines()
        self.assertRegex(lines[-2], r"^RESULT: CHECKS_PASSED exit=0 ")
        self.assertEqual(lines[-1], "INSTALLATION_NOT_STARTED")
        self.assertIn("MISSING_MANUAL:OPENAI_API_KEY", text_of(r))        # the record's open items are shown by name

    def test_the_unmeasured_network_pools_make_the_check_unverified_unless_the_operator_accepts_it(self):
        r = self.check(opts=H.Options())
        self.assertEqual(r.exit_code(), 3)
        self.expect(r, "NETWORK_POOLS", UNVERIFIED)
        self.assertIn("unmeasured", text_of(r))
        self.assertEqual([i.code for i in r.items if i.level == UNVERIFIED], ["NETWORK_POOLS"])
        self.expect(self.check(), "NETWORK_POOLS", NOTE)

    def test_the_commands_issued_are_the_whitelisted_ones_and_come_from_the_model(self):
        self.check()
        self.assertTrue(all(H.deny_reason(argv) is None for argv in self.host.calls))
        keys = [key_of(a) for a in self.host.calls]
        for key in ("uname", "info", "compose_version", "config", "ps", "volume", "network", "image", "ss", "tunnel_check"):
            self.assertIn(key, keys)
        self.assertEqual(self.host.calls[keys.index("config")],
                         ["docker", "compose", "-p", "edu_ai", "--project-directory", str(self.src), "--env-file", str(self.cfg / ".env"),
                          "config", "--format", "json"])
        pinned = self.layout("--project", "edu_ai")
        self.assertEqual(self.host.calls[keys.index("config")], pinned.compose_prefix() + ["config", "--format", "json"])

    def test_ports_names_and_tags_follow_the_model_not_constants(self):
        model = make_model()
        model["services"]["nginx"]["ports"] = [{"target": 80, "published": "8123", "protocol": "tcp"}]
        model["services"]["nginx"]["container_name"] = "custom_front"
        self.world.model = model
        self.world.ss = ss_text(("0.0.0.0", 80))                  # the old constant is irrelevant now
        self.assertEqual(self.check().exit_code(), 0)
        self.world.ss = ss_text(("::", 8123))
        r = self.check()
        self.expect(r, "PORTS", REFUSED)
        self.assertIn("8123", text_of(r))
        self.world.ss = ss_text()
        self.world.containers = [{"Names": "custom_front", "Labels": ""}]
        self.expect(self.check(), "EXISTING_CONTAINERS", REFUSED)

    # architecture, docker, compose
    def test_architecture(self):
        for uname, level in (("aarch64\n", REFUSED), ("armv7l\n", REFUSED), ("i686\n", REFUSED), ("amd64\n", OK), ("", UNVERIFIED),
                             ("x86_64 extra words\n", UNVERIFIED), ("x86_64\nx86_64\n", UNVERIFIED)):
            with self.subTest(uname=uname):
                self.world.uname = uname
                self.expect(self.check(), "ARCH_HOST", level)
        for arch, level in (("aarch64", REFUSED), (None, UNVERIFIED), ("", UNVERIFIED), (5, UNVERIFIED), ("amd64", OK)):
            with self.subTest(docker=arch):
                self.world.info = {**World().info, "Architecture": arch}
                self.expect(self.check(), "ARCH_DOCKER", level)

    def test_a_platform_that_is_not_linux_is_refused(self):
        for plat in ("win32", "darwin"):
            self.host.plat = plat
            self.expect(self.check(), "PLATFORM", REFUSED)

    def test_the_daemon(self):
        self.world.override["info"] = Proc(1, "", "Cannot connect to the Docker daemon")
        r = self.check()
        for code in ("DOCKER_DAEMON", "DISK_DOCKER_ROOT", "NETWORK_POOLS"):
            self.expect(r, code, UNVERIFIED)
        self.assertNotIn("DOCKER_DAEMON: OK", text_of(r))
        self.assertNotIn("Cannot connect", text_of(r))                # child output is never relayed
        self.world.override["info"] = Proc(0, json.dumps({**World().info, "OSType": "windows"}), "")
        self.expect(self.check(), "DOCKER_OS", REFUSED)
        self.world.override["info"] = Proc(0, json.dumps({**World().info, "ClientInfo": {"Context": "remote"}}), "")
        self.expect(self.check(), "DOCKER_CONTEXT", UNVERIFIED)
        self.world.override["info"] = Proc(0, json.dumps({k: v for k, v in World().info.items() if k != "ClientInfo"}), "")
        self.expect(self.check(), "DOCKER_CONTEXT", UNVERIFIED)

    def test_a_docker_target_other_than_the_default_is_unverified(self):
        for env, ok in (({"DOCKER_HOST": "tcp://10.0.0.5:2375"}, False), ({"DOCKER_HOST": "ssh://root@elsewhere"}, False),
                        ({"DOCKER_CONTEXT": "prod"}, False), ({"DOCKER_CONTEXT": "default"}, True), ({"DOCKER_CONTEXT": ""}, True),
                        ({"DOCKER_HOST": "unix:///var/run/docker.sock"}, True)):
            with self.subTest(env=env):
                self.host.env = {"PATH": "/usr/bin", **env}
                r = self.check()
                self.assertEqual(self.level(r, "DOCKER_TARGET") == [], ok)
                if not ok:
                    self.assertEqual(r.exit_code(), 3)
                    self.assertIn("default daemon", text_of(r))

    def test_inherited_variables_that_beat_the_env_file_are_named_not_shown(self):
        self.host.env = {"PATH": "/usr/bin", "COMPOSE_FILE": "x.yml", "POSTGRES_PASSWORD": "SENTINEL-shell", "EDU_AI_TUNNEL_SUBNET": "10.1.1.0/29",
                         "UNRELATED": "1", "DOCKER_CONFIG": "/x"}
        r = self.check()
        self.expect(r, "INHERITED_ENV", UNVERIFIED)
        out = text_of(r)
        for name in ("COMPOSE_FILE", "POSTGRES_PASSWORD", "EDU_AI_TUNNEL_SUBNET", "DOCKER_CONFIG"):
            self.assertIn(name, out)
        self.assertNotIn("UNRELATED", out)
        self.assertNotIn("SENTINEL-shell", out)

    def test_the_compose_version(self):
        for text, level in (("5.3.1\n", OK), ("v2.24.0\n", OK), ("2.40.3-desktop.1\n", OK), ("2.23.9\n", REFUSED), ("1.29.2\n", REFUSED),
                            ("dev\n", UNVERIFIED), ("", UNVERIFIED), ("2.x\n", UNVERIFIED), ("5.3\n", UNVERIFIED)):
            with self.subTest(text=text):
                self.world.compose_version = text
                self.expect(self.check(), "COMPOSE_VERSION", level)
        self.world.override["compose_version"] = Proc(125, "", "")
        self.expect(self.check(), "COMPOSE_VERSION", UNVERIFIED)
        self.world.override.clear()
        self.world.compose_version = "2.30.0\n"
        self.expect(self.check(opts=H.Options(min_compose=(2, 31, 0), accept_unmeasured_pools=True)), "COMPOSE_VERSION", REFUSED)
        self.assertIn("PROVISIONAL", text_of(self.check()))

    def test_buildx_is_required_because_the_images_are_built_here(self):
        self.expect(self.check(), "BUILDX", OK)
        self.world.override["buildx"] = Proc(1, "", "docker: 'buildx' is not a docker command")
        self.expect(self.check(), "BUILDX", REFUSED)
        self.world.override["buildx"] = OSError("no docker")
        self.expect(self.check(), "BUILDX", UNVERIFIED)

    # the external configuration set
    def test_the_configuration_set_must_be_complete_private_and_owned(self):
        (self.cfg / "install-record.json").unlink()
        r = self.check()
        self.expect(r, "CONFIG_FILES", REFUSED)
        self.expect(r, "KEY_FINGERPRINT", UNVERIFIED)
        self.assertEqual(r.exit_code(), 2)
        self.write_set()
        (self.cfg / "stray.txt").write_text("x")
        self.expect(self.check(), "CONFIG_FILES", REFUSED)
        (self.cfg / "stray.txt").unlink()
        (self.cfg / ".env").unlink()
        self.expect(self.check(), "CONFIG_FILES", REFUSED)

    def test_modes_and_owner_of_the_set(self):
        self.host.fake_modes = True
        for path, mode, uid, code in ((self.cfg, 0o755, None, "CONFIG_DIR"), (self.cfg, 0o750, None, "CONFIG_DIR"),
                                      (self.cfg / ".env", 0o644, None, "CONFIG_FILES"), (self.cfg / ".env", 0o640, None, "CONFIG_FILES"),
                                      (self.cfg / "install-record.json", 0o660, None, "CONFIG_FILES"),
                                      (self.cfg, 0o700, 4242, "CONFIG_DIR"), (self.cfg / ".env", 0o600, 4242, "CONFIG_FILES")):
            with self.subTest(path=path.name, mode=oct(mode), uid=uid):
                self.host.modes = {str(path): (mode, uid)}
                self.expect(self.check(), code, REFUSED)
        self.host.modes = {}
        self.assertEqual(self.check().exit_code(), 0)

    def test_modes_cannot_be_proven_on_a_platform_without_them(self):
        with mock.patch.object(self.host, "posix", return_value=False):
            self.expect(self.check(), "CONFIG_DIR", UNVERIFIED)

    def test_the_key_fingerprint(self):
        good = self.env_values
        cases = {
            "MISMATCH": dict(record={"schema": H.install.RECORD_SCHEMA, "langflow_secret_key_sha256": "0" * 64}, level=REFUSED),
            "UNVERIFIABLE-no-field": dict(record={"schema": H.install.RECORD_SCHEMA}, level=UNVERIFIED),
            "UNVERIFIABLE-schema": dict(record={"schema": "other/9", "langflow_secret_key_sha256": "0" * 64}, level=UNVERIFIED),
            "UNVERIFIABLE-not-hex": dict(record={"schema": H.install.RECORD_SCHEMA, "langflow_secret_key_sha256": "XYZ"}, level=UNVERIFIED),
            "UNVERIFIABLE-list": dict(record=[1, 2], level=UNVERIFIED),
            "UNVERIFIABLE-no-key-in-env": dict(env="COMPOSE_PROJECT_NAME=edu_ai\n", level=UNVERIFIED)}
        for name, case in cases.items():
            with self.subTest(name):
                self.write_set(env_text=case.get("env"), record=case.get("record"))
                r = self.check()
                self.expect(r, "KEY_FINGERPRINT", case["level"])
                self.assertNotEqual(r.exit_code(), 0)
                self.assertNotIn(SECRET_KEY, text_of(r))
        self.write_set(record={})
        self.expect(self.check(), "KEY_FINGERPRINT", UNVERIFIED)
        (self.cfg / "install-record.json").write_text("{not json", newline="\n")
        self.expect(self.check(), "KEY_FINGERPRINT", UNVERIFIED)
        self.write_set()
        self.assertEqual(self.level(self.check(), "KEY_FINGERPRINT"), [OK])
        self.assertIn("MATCH", text_of(self.check()))

    def test_an_unreadable_set_is_unverified(self):
        real = self.host.read_bytes
        for victim in ("install-record.json", ".env"):
            def deny(path, limit, victim=victim):
                if str(path).endswith(victim):
                    raise PermissionError("syn")
                return real(path, limit)
            with self.subTest(victim=victim), mock.patch.object(self.host, "read_bytes", deny):
                r = self.check()
                self.expect(r, "CONFIG_FILES", UNVERIFIED)
                self.expect(r, "KEY_FINGERPRINT", UNVERIFIED)
                if victim == ".env":
                    self.expect(r, "ENV_PINS", UNVERIFIED)           # the pins of an unreadable .env are unknown, not fine
                    self.assertNotIn(OK, self.level(r, "COMPOSE_FILE") + self.level(r, "PROJECT"))
                self.assertEqual(r.exit_code(), 3)

    # pins
    def test_compose_file_must_be_pinned_with_the_exact_reason(self):
        for text in ("COMPOSE_PROJECT_NAME=edu_ai\nEDU_AI_TUNNEL_SUBNET=%s\n" % SUBNET, "COMPOSE_FILE=\nCOMPOSE_PROJECT_NAME=edu_ai\n",
                     "#COMPOSE_FILE=docker-compose.yml\nCOMPOSE_PROJECT_NAME=edu_ai\n"):
            with self.subTest(text=text):
                self.write_set(env_text=text + f"LANGFLOW_SECRET_KEY={SECRET_KEY}\n")
                r = self.check()
                self.expect(r, "COMPOSE_FILE", REFUSED)
                self.assertIn("docker-compose.override.yml would be loaded automatically", text_of(r))
        for value, level in (("docker-compose.yml", OK), ("./docker-compose.yml", OK), ("docker-compose.yml:docker-compose.override.yml", REFUSED),
                             ("docker-compose.override.yml", REFUSED), ("other.yml", REFUSED), ("docker-compose.yml:extra.yml", OK)):
            with self.subTest(value=value):
                self.env_values["COMPOSE_FILE"] = value
                self.write_set()
                self.expect(self.check(), "COMPOSE_FILE", level)

    def test_the_project(self):
        self.env_values["COMPOSE_PROJECT_NAME"] = "Bad Name"
        self.write_set()
        self.expect(self.check(), "PROJECT", REFUSED)
        self.env_values["COMPOSE_PROJECT_NAME"] = "edu_ai"
        self.write_set()
        r = self.check(layout=self.layout("--project", "edu_ai_lab"))
        self.expect(r, "PROJECT", REFUSED)
        del self.env_values["COMPOSE_PROJECT_NAME"]
        self.write_set()
        r = self.check()
        self.expect(r, "PROJECT", UNVERIFIED)
        self.expect(r, "MODEL", UNVERIFIED)
        self.world = World("edu_ai_lab")
        self.host = FakeHost(self.world)
        r = self.check(layout=self.layout("--project", "edu_ai_lab"))
        self.assertEqual(self.level(r, "PROJECT"), [OK])
        self.assertEqual(r.exit_code(), 0, text_of(r))

    def test_the_tunnel_subnet_pin(self):
        del self.env_values["EDU_AI_TUNNEL_SUBNET"]
        self.write_set()
        r = self.check()
        self.expect(r, "TUNNEL_SUBNET_PIN", UNVERIFIED)
        self.expect(r, "MODEL", UNVERIFIED)
        self.assertIn("pin-env --allocate-tunnel-subnet", text_of(r))
        for bad in ("10.77.3.0/24", "8.8.8.0/29", "10.77.3.5/29", "garbage", "2001:db8::/64"):
            with self.subTest(bad=bad):
                self.env_values["EDU_AI_TUNNEL_SUBNET"] = bad
                self.write_set()
                self.expect(self.check(), "TUNNEL_SUBNET_PIN", REFUSED)

    # the model
    def test_a_model_that_cannot_render_is_unverified_and_names_only_the_variable(self):
        stderr = "error: required variable EDU_AI_TUNNEL_SUBNET is missing a value: SENTINEL-in-stderr and env file /x/.env not found"
        self.world.override["config"] = Proc(1, "", stderr)
        r = self.check()
        self.expect(r, "MODEL", UNVERIFIED)
        out = text_of(r)
        self.assertIn("EDU_AI_TUNNEL_SUBNET", out)
        self.assertNotIn("SENTINEL-in-stderr", out)
        for code in ("EXISTING_CONTAINERS", "EXISTING_VOLUMES", "EXISTING_NETWORKS", "EXISTING_IMAGES", "PORTS"):
            self.expect(r, code, UNVERIFIED)
        for proc in (Proc(0, "not json", ""), Proc(0, "{}", ""), Proc(0, "[]", ""), Proc(0, json.dumps(make_model("other")), ""),
                     Proc(0, "", ""), Proc(124, "", ""), H.ProbeFailed("FileNotFoundError")):
            with self.subTest(proc=str(proc)[:40]):
                self.world.override["config"] = proc
                r = self.check()
                self.expect(r, "MODEL", UNVERIFIED)
                self.assertEqual(r.exit_code(), 3)

    def test_the_rendered_subnet_must_equal_the_pinned_one(self):
        self.world.model = make_model(subnet="10.88.4.0/29")
        self.expect(self.check(), "TUNNEL_SUBNET_MODEL", REFUSED)

    # G1/G3 existing resources
    def test_a_collision_is_refused_at_every_resource_kind(self):
        cases = (("containers", "EXISTING_CONTAINERS", {"Names": "edu_ai_postgres", "Labels": ""}),
                 ("containers", "EXISTING_CONTAINERS", {"Names": "other,edu_ai_nginx", "Labels": ""}),
                 ("containers", "EXISTING_CONTAINERS", {"Names": "/bifrost", "Labels": ""}),
                 ("volumes", "EXISTING_VOLUMES", {"Name": "edu_ai_postgres_data", "Labels": ""}),
                 ("networks", "EXISTING_NETWORKS", {"Name": "edu_ai_edge", "Labels": ""}),
                 ("networks", "EXISTING_NETWORKS", {"Name": "edu_ai_tunnel", "Labels": ""}),
                 ("images", "EXISTING_IMAGES", {"Repository": "edu_ai_postgres", "Tag": "cs"}),
                 ("images", "EXISTING_IMAGES", {"Repository": "edu_ai", "Tag": "1.1.0"}))
        for attr, code, row in cases:
            with self.subTest(code=code, row=row):
                self.world = World()
                self.host = FakeHost(self.world)
                setattr(self.world, attr, [row])
                r = self.check()
                self.expect(r, code, REFUSED)
                self.assertEqual(r.exit_code(), 2)

    def test_a_project_label_alone_is_a_collision_and_foreign_names_are_not_listed(self):
        label = "a=b,com.docker.compose.project=edu_ai,c=d"
        self.world.containers = [{"Names": "edu_ai-leftover-1", "Labels": label}, {"Names": "somebody_elses_db", "Labels": "x=y"}]
        self.world.volumes = [{"Name": "mystery_volume", "Labels": label}, {"Name": "unrelated", "Labels": ""}]
        self.world.networks = [{"Name": "edu_ai_default", "Labels": label}]
        r = self.check()
        out = text_of(r)
        for code in ("EXISTING_CONTAINERS", "EXISTING_VOLUMES", "EXISTING_NETWORKS"):
            self.expect(r, code, REFUSED)
        self.assertIn("edu_ai-leftover-1", out)
        self.assertIn("mystery_volume", out)
        self.assertNotIn("somebody_elses_db", out)
        self.assertNotIn("unrelated", out)

    def test_other_projects_and_near_names_are_not_collisions(self):
        self.world.containers = [{"Names": "edu_ai_nginx2", "Labels": "com.docker.compose.project=edu_ai_lab"}]
        self.world.volumes = [{"Name": "edu_ai_postgres_data_old", "Labels": ""}]
        self.world.networks = [{"Name": "edu_ai_edge2", "Labels": "com.docker.compose.project=edu_ai2"}]
        self.world.images = [{"Repository": "edu_ai_postgres", "Tag": "old"}, {"Repository": "<none>", "Tag": "<none>"}]
        self.assertEqual(self.check().exit_code(), 0)

    def test_a_public_image_already_on_the_host_is_a_note_unless_the_operator_is_strict(self):
        self.world.images = [{"Repository": "nginx", "Tag": "1.30.5-alpine"}, {"Repository": "maximhq/bifrost", "Tag": "v1.6.7"}]
        r = self.check()
        self.assertEqual(r.exit_code(), 0)
        self.expect(r, "IMAGES_PRESENT_PULLED", NOTE)
        strict = self.check(opts=H.Options(refuse_pulled_images=True, accept_unmeasured_pools=True))
        self.expect(strict, "EXISTING_IMAGES", REFUSED)
        self.world.images = [{"Repository": "docker.io/library/nginx", "Tag": "1.30.5-alpine"}]
        self.expect(self.check(opts=H.Options(refuse_pulled_images=True, accept_unmeasured_pools=True)), "EXISTING_IMAGES", REFUSED)

    def test_garbage_listings_are_unverified(self):
        garbage = ("not json\n", "[1]\n", '{"Names": 5}\n', '{"Other": "x"}\n', '{"Names": "a", "Labels": 7}\n', "{\n")
        for key, code in (("ps", "EXISTING_CONTAINERS"), ("volume", "EXISTING_VOLUMES"), ("network", "EXISTING_NETWORKS"),
                          ("image", "EXISTING_IMAGES")):
            for text in garbage:
                with self.subTest(code=code, text=text):
                    self.world.override[key] = Proc(0, text, "")
                    r = self.check()
                    self.assertNotIn(OK, self.level(r, code))
                    self.assertNotEqual(r.exit_code(), 0)
            self.world.override.clear()

    # state
    def test_the_state_directory_must_hold_no_data(self):
        cases = {"stray file": lambda: (self.state / "x.txt").write_text("x"),
                 "uploads data": lambda: ((self.state / "uploads").mkdir(), (self.state / "uploads" / "f").write_text("x")),
                 "langflow_data data": lambda: ((self.state / "langflow_data").mkdir(), (self.state / "langflow_data" / "key").write_text("x")),
                 "bifrost_data a file": lambda: (self.state / "bifrost_data").write_text("x"),
                 "stray directory": lambda: (self.state / "pgdata").mkdir()}
        for name, make in cases.items():
            with self.subTest(name):
                self.setUp()
                make()
                r = self.check()
                self.expect(r, "STATE_DIR", REFUSED)
                self.assertEqual(r.exit_code(), 2)
        self.setUp()
        (self.state / "langflow_data").mkdir()
        (self.state / "langflow_data" / "k").write_text("x")
        self.assertIn("langflow_data is not empty", text_of(self.check()))

    def test_empty_prepared_directories_and_a_missing_state_directory_are_fine(self):
        for name in L.STATE_DIRS:
            (self.state / name).mkdir()
        self.assertEqual(self.level(self.check(), "STATE_DIR"), [OK])
        for name in L.STATE_DIRS:
            (self.state / name).rmdir()
        self.state.rmdir()
        r = self.check()
        self.assertEqual(self.level(r, "STATE_DIR"), [OK])
        self.assertEqual(self.level(r, "DISK_STATE_DIR"), [OK])

    def test_a_state_directory_whose_parent_is_missing_cannot_be_created_by_handoff(self):
        r = self.check(layout=self.layout("--state-dir", str(self.state.parent / "no" / "state"), state=False))
        self.expect(r, "STATE_DIR", REFUSED)

    @unittest.skipUnless(SYMLINKS, "cannot create symlinks on this platform")
    def test_a_state_directory_that_is_a_symlink_is_refused(self):
        real = self.state.parent / "elsewhere"
        real.mkdir()
        self.state.rmdir()
        os.symlink(real, self.state)
        self.expect(self.check(), "STATE_DIR", REFUSED)

    def test_an_unreadable_state_directory_is_unverified(self):
        real = self.host.listdir

        def deny(path):
            if str(path) == str(self.state):
                raise PermissionError("syn")
            return real(path)
        with mock.patch.object(self.host, "listdir", deny):
            r = self.check()
        self.expect(r, "STATE_DIR", UNVERIFIED)
        self.assertEqual(r.exit_code(), 3)

    def test_the_legacy_in_checkout_state_is_checked_too(self):
        (self.src / "uploads").mkdir()
        (self.src / "uploads" / "f").write_text("x")
        r = self.check(layout=self.layout(state=False))
        self.expect(r, "STATE_DIR", REFUSED)

    # layout
    def test_a_regular_env_in_the_checkout_is_refused_and_never_read(self):
        (self.src / ".env").write_text("OLD_SECRET=SENTINEL-old\n")
        r = self.check()
        self.expect(r, "LAYOUT_LINK_CONFLICT", REFUSED)
        self.assertEqual(r.exit_code(), 2)
        self.assertNotIn("SENTINEL-old", text_of(r))
        self.assertNotIn(str(self.src / ".env"), [c for call in self.host.calls for c in call])

    def test_layout_conflicts_wrong_targets_and_missing_pieces_are_refused(self):
        (self.src / "uploads").mkdir()
        self.expect(self.check(), "LAYOUT_LINK_CONFLICT", REFUSED)
        import argparse
        parser = argparse.ArgumentParser()
        L.add_arguments(parser)
        legacy = L.resolve(parser.parse_args(["--source-root", str(self.src)]), {}, self.src)
        self.expect(self.check(layout=legacy), "CONFIG_NOT_EXTERNAL", REFUSED)
        (self.src / "docker-compose.yml").unlink()
        self.expect(self.check(), "LAYOUT_SOURCE_NOT_A_CHECKOUT", REFUSED)

    @unittest.skipUnless(SYMLINKS, "cannot create symlinks on this platform")
    def test_a_link_to_the_wrong_target_is_refused(self):
        os.symlink(self.state, self.src / ".env")
        self.expect(self.check(), "LAYOUT_LINK_WRONG_TARGET", REFUSED)

    # ports
    def test_a_listener_on_any_address_of_any_family_is_refused(self):
        for listeners, port in ((("0.0.0.0", 80),), 80), ((("::", 8443),), 8443), ((("::1", 8444),), 8444), ((("127.0.0.1", 8099),), 8099), \
                               ((("::1", 8099),), 8099), ((("192.168.1.5", 80),), 80), ((("10.0.0.1", 8443), ("::", 22)), 8443):
            with self.subTest(listeners=listeners):
                self.world.ss = ss_text(*listeners)
                r = self.check()
                self.expect(r, "PORTS", REFUSED)
                self.assertIn(str(port), text_of(r))
                self.assertEqual(r.exit_code(), 2)

    def test_the_ports_of_an_auto_loaded_override_would_be_caught_from_the_model(self):
        model = make_model()
        model["services"]["postgres"]["ports"] = [{"host_ip": "127.0.0.1", "target": 5432, "published": "5432", "protocol": "tcp"}]
        self.world.model = model
        self.world.ss = ss_text(("127.0.0.1", 5432))
        self.expect(self.check(), "PORTS", REFUSED)

    def test_a_model_that_carries_the_override_ports_is_refused_even_when_nothing_listens(self):
        model = make_model()
        model["services"]["edu_ai_search"]["ports"] = [{"host_ip": "127.0.0.1", "target": 8090, "published": "8090", "protocol": "tcp"}]
        self.world.model = model
        r = self.check()
        self.expect(r, "MODEL_OVERRIDE", REFUSED)
        self.assertIn("8090", text_of(r))
        self.expect(self.check(opts=H.Options(accept_unmeasured_pools=True)), "PORTS", OK)
        (self.src / "docker-compose.override.yml").write_bytes(b"\xff\xfe\x00")
        self.world.model = make_model()
        self.expect(self.check(), "MODEL_OVERRIDE", UNVERIFIED)
        (self.src / "docker-compose.override.yml").unlink()
        self.assertEqual(self.level(self.check(), "MODEL_OVERRIDE"), [])

    def test_ss_that_cannot_be_read_or_understood_is_unverified_never_free(self):
        for proc in (Proc(1, "", "boom"), Proc(127, "", ""), Proc(0, "garbage line\n", ""), Proc(0, "LISTEN 0 1 x\n", ""),
                     H.ProbeFailed("FileNotFoundError"), H.ProbeFailed("TimeoutExpired")):
            with self.subTest(proc=str(proc)[:40]):
                self.world.override["ss"] = proc
                r = self.check()
                self.expect(r, "PORTS", UNVERIFIED)
                self.assertEqual(r.exit_code(), 3)
        self.world.override["ss"] = Proc(0, "", "")
        self.assertEqual(self.level(self.check(), "PORTS"), [OK])          # an empty listing is a listing

    def test_udp_ports_cannot_be_checked_by_a_tcp_listing(self):
        model = make_model()
        model["services"]["nginx"]["ports"].append({"target": 53, "published": "53", "protocol": "udp"})
        self.world.model = model
        self.expect(self.check(), "PORTS_UNLISTED", UNVERIFIED)

    # networks
    def test_the_tunnel_subnet_uses_the_exit_codes_of_tunnel_subnet_py_as_they_are(self):
        for proc, level in ((Proc(0, "OK: x\n", ""), OK), (Proc(2, "REFUSED HOST_ROUTE: 10.77.3.0/29 overlaps the route 'x'\nREFUSED RESERVED: y\n", ""), REFUSED),
                            (Proc(2, "REFUSED: INVALID: 'x'\n", ""), REFUSED), (Proc(3, "REFUSED (cannot verify): ip\n", ""), UNVERIFIED),
                            (Proc(1, "Traceback", "boom"), UNVERIFIED), (Proc(4, "", ""), UNVERIFIED), (Proc(-9, "", ""), UNVERIFIED),
                            (H.ProbeFailed("TimeoutExpired"), UNVERIFIED)):
            with self.subTest(proc=str(proc)[:50]):
                self.world.tunnel_check = proc
                self.world.override["tunnel_check"] = proc
                r = self.check()
                self.expect(r, "TUNNEL_SUBNET", level)
        self.world.override["tunnel_check"] = Proc(2, "REFUSED HOST_ROUTE: detail-with-route 10.0.0.0\n", "")
        out = text_of(self.check())
        self.assertIn("HOST_ROUTE", out)
        self.assertNotIn("detail-with-route", out)

    def test_reserved_ranges_and_the_project_reach_tunnel_subnet_py(self):
        self.check(opts=H.Options(reserved=("192.168.0.0/16", "10.20.0.0/14"), accept_unmeasured_pools=True))
        argv = next(a for a in self.host.calls if key_of(a) == "tunnel_check")
        self.assertEqual(argv[2:], ["check", SUBNET, "--project", "edu_ai", "--routes", "host", "--reserved", "192.168.0.0/16",
                                    "--reserved", "10.20.0.0/14"])

    def test_the_pools_of_the_daemon_are_listed(self):
        self.world.info = {**World().info, "DefaultAddressPools": [{"Base": "10.10.0.0/16", "Size": 24}]}
        r = self.check(opts=H.Options())
        self.assertIn("10.10.0.0/16/24", text_of(r))
        self.assertIn("Docker's behaviour on a collision is unmeasured", text_of(r))
        self.assertIn("edge", text_of(r))

    # disk
    def test_disk_space(self):
        self.host.disk["/var/lib/docker"] = 5 * GIB
        r = self.check()
        self.expect(r, "DISK_DOCKER_ROOT", REFUSED)
        self.assertIn("provisional", text_of(r))
        self.host.disk["/var/lib/docker"] = 20 * GIB
        self.expect(self.check(), "DISK_DOCKER_ROOT", OK)
        self.expect(self.check(opts=H.Options(min_free_gb=21, accept_unmeasured_pools=True)), "DISK_DOCKER_ROOT", REFUSED)
        self.host.disk[str(self.state)] = 1 * GIB
        self.expect(self.check(), "DISK_STATE_DIR", REFUSED)
        self.host.disk[str(self.state)] = FileNotFoundError("syn")
        self.expect(self.check(), "DISK_STATE_DIR", UNVERIFIED)
        self.host.disk["/var/lib/docker"] = PermissionError("syn")
        r = self.check()
        self.expect(r, "DISK_DOCKER_ROOT", UNVERIFIED)
        self.assertEqual(r.exit_code(), 3)

    def test_a_docker_root_inside_a_vm_cannot_be_measured(self):
        self.world.info = {**World().info, "OperatingSystem": "Docker Desktop"}
        self.expect(self.check(), "DISK_DOCKER_ROOT", UNVERIFIED)
        for root in (None, "", "relative/path", 7):
            self.world.info = {**World().info, "DockerRootDir": root}
            self.expect(self.check(), "DISK_DOCKER_ROOT", UNVERIFIED)

    def test_a_missing_state_directory_is_measured_at_its_parent(self):
        self.state.rmdir()
        self.host.disk[str(self.state.parent)] = 2 * GIB
        self.expect(self.check(), "DISK_STATE_DIR", REFUSED)


class PropertyTests(Tree):
    """Whatever a probe does, the answer is never OK: it raises, exits non-zero or prints garbage."""
    CODES = {"uname": ["ARCH_HOST"], "info": ["DOCKER_DAEMON"], "compose_version": ["COMPOSE_VERSION"], "ps": ["EXISTING_CONTAINERS"],
             "volume": ["EXISTING_VOLUMES"], "network": ["EXISTING_NETWORKS"], "image": ["EXISTING_IMAGES"], "ss": ["PORTS"],
             "config": ["MODEL"], "tunnel_check": ["TUNNEL_SUBNET"]}
    EXCEPTIONS = (RuntimeError("boom"), OSError("syn"), H.ProbeFailed("FileNotFoundError"), subprocess.TimeoutExpired("x", 1),
                  ValueError("syn"), KeyError("k"), UnicodeDecodeError("utf-8", b"", 0, 1, "syn"), PermissionError("syn"))
    BAD = (Proc(1, "", ""), Proc(2, "x86_64\n", "x"), Proc(125, '{"ServerVersion": "1"}', ""), Proc(127, "", "not found"),
           Proc(0, "garbage words here", ""), Proc(0, "{", ""), Proc(0, "[1]", ""), Proc(0, "\x00\xff\x00", ""), Proc(0, "null", ""),
           Proc(0, '"str"', ""), Proc(0, "{}", ""), Proc(0, "12345", ""))

    def assert_not_ok(self, key, response):
        self.world.override = {key: response}
        r = self.check()
        for code in self.CODES[key]:
            levels = self.level(r, code)
            self.assertTrue(levels and OK not in levels, f"{key}/{code}/{response!r}: {levels}\n{text_of(r)}")
        self.assertNotEqual(r.exit_code(), 0)

    def test_probes_that_raise_are_unverified(self):
        for key in self.CODES:
            for exc in self.EXCEPTIONS:
                with self.subTest(key=key, exc=type(exc).__name__):
                    self.assert_not_ok(key, exc)

    def test_probes_that_exit_non_zero_or_print_garbage_are_not_ok(self):
        for key in self.CODES:
            for proc in self.BAD:
                if key == "tunnel_check" and proc.returncode == 0:
                    continue                       # for tunnel_subnet.py the exit status is the answer, not the text
                with self.subTest(key=key, proc=str(proc)[:40]):
                    self.assert_not_ok(key, proc)

    def test_probes_that_blow_up_never_escape_as_a_traceback(self):
        for key in self.CODES:
            self.world.override = {key: RuntimeError("boom")}
            self.assertIsNotNone(self.check())

    def test_file_system_probes_that_fail_are_unverified(self):
        def failing(name, code, *args, exc=PermissionError("syn")):
            with mock.patch.object(self.host, name, side_effect=exc):
                r = self.check()
            self.assertNotIn(OK, self.level(r, code), (name, code, text_of(r)))
            self.assertNotEqual(r.exit_code(), 0)
        failing("lstat", "CONFIG_DIR")
        failing("platform", "PLATFORM", exc=RuntimeError("syn"))
        failing("disk_free", "DISK_STATE_DIR", exc=OSError("syn"))
        failing("environ", "DOCKER_TARGET", exc=RuntimeError("syn"))
        failing("uid", "CONFIG_DIR", exc=RuntimeError("syn"))

    def test_exit_code_and_result_line_always_agree_with_the_items(self):
        for key in self.CODES:
            for proc in (Proc(1, "", ""), Proc(0, "garbage", "")):
                self.world.override = {key: proc}
                r = self.check()
                lines = r.lines()
                bad = [l for l in lines if re.match(r"^\w+: (REFUSED|UNVERIFIED)\b", l)]
                self.assertEqual(r.exit_code() == 0, not bad)
                self.assertTrue(lines[-2].startswith("RESULT: "))
                self.assertEqual(lines[-1], "INSTALLATION_NOT_STARTED")
                if r.exit_code():
                    self.assertNotIn("CHECKS_PASSED", lines[-2])

    def test_a_failing_or_unverified_state_never_says_ready_free_or_installed(self):
        word = re.compile(r"\b(ready|free|installed)\b", re.I)
        scenarios = [lambda: None]
        for key in self.CODES:
            scenarios.append(lambda key=key: self.world.override.update({key: Proc(1, "", "")}))
            scenarios.append(lambda key=key: self.world.override.update({key: Proc(0, "garbage", "")}))
        for prepare in scenarios:
            self.world = World()
            self.host = FakeHost(self.world)
            prepare()
            r = self.check(opts=H.Options())
            for line in r.lines():
                if re.match(r"^\w+: (REFUSED|UNVERIFIED)\b", line) or line.startswith("RESULT:"):
                    self.assertIsNone(word.search(line), line)
            if r.exit_code():
                self.assertNotIn("CHECKS_PASSED", text_of(r))


class OutputTests(Tree):
    def scenarios(self):
        leak = " ".join(list(SENTINELS.values()) + [SECRET_KEY])
        yield "clean", lambda: None
        yield "info-garbage", lambda: self.world.override.update({"info": Proc(0, leak, leak)})
        yield "info-fails", lambda: self.world.override.update({"info": Proc(1, leak, leak)})
        yield "config-fails", lambda: self.world.override.update({"config": Proc(1, leak, f"required variable X is missing {leak}")})
        yield "ps-garbage", lambda: self.world.override.update({"ps": Proc(0, leak, leak)})
        yield "ss-garbage", lambda: self.world.override.update({"ss": Proc(0, leak, leak)})
        yield "tunnel-refused", lambda: self.world.override.update({"tunnel_check": Proc(2, f"REFUSED HOST_ROUTE: {leak}\n", leak)})
        yield "uname", lambda: setattr(self.world, "uname", leak)
        yield "version", lambda: setattr(self.world, "compose_version", leak)
        yield "collisions", lambda: (setattr(self.world, "containers", [{"Names": "edu_ai_nginx", "Labels": leak}]),
                                     setattr(self.world, "volumes", [{"Name": "edu_ai_postgres_data", "Labels": leak}]))
        yield "mismatch", lambda: self.write_set(record={"schema": H.install.RECORD_SCHEMA, "langflow_secret_key_sha256": "0" * 64})
        yield "no-pins", lambda: (self.env_values.pop("COMPOSE_FILE"), self.write_set())
        yield "env-shadowed", lambda: setattr(self.host, "env", {"PATH": "x", "POSTGRES_PASSWORD": leak})

    def test_no_secret_value_is_ever_printed(self):
        keys = list(SENTINELS.values()) + [SECRET_KEY, __import__("hashlib").sha256(SECRET_KEY.encode()).hexdigest()]
        for name, prepare in self.scenarios():
            with self.subTest(name):
                self.setUp()
                prepare()
                out = text_of(self.check(opts=H.Options()))
                for secret in keys:
                    self.assertNotIn(secret, out, name)
                self.assertNotIn("SENTINEL", out)

    def test_nothing_that_looks_like_name_equals_value_is_printed(self):
        looks = re.compile(r"\b[A-Z][A-Z0-9_]{3,}=\S")
        for name, prepare in self.scenarios():
            with self.subTest(name):
                self.setUp()
                prepare()
                for line in self.check(opts=H.Options()).lines():
                    self.assertIsNone(looks.search(line), line)

    def test_every_line_has_the_stable_shape(self):
        shape = re.compile(r"^([A-Z][A-Z0-9_]+): (OK|NOTE|REFUSED|UNVERIFIED)( .*)?$|^MODE: [a-z-]+$|^RESULT: (CHECKS_PASSED|REFUSED|UNVERIFIED) exit=\d .*$|^INSTALLATION_NOT_STARTED$")
        for name, prepare in self.scenarios():
            with self.subTest(name):
                self.setUp()
                prepare()
                for line in self.check(opts=H.Options()).lines():
                    self.assertRegex(line, shape)

    def test_control_characters_and_long_details_are_neutralised(self):
        r = H.Report()
        r.add("X", REFUSED, "a\x1b[31m\nb\r" + "z" * 1000)
        line = r.lines()[1]
        self.assertNotRegex(line, r"[\x00-\x1f]")
        self.assertLess(len(line), 450)

    def test_a_path_the_terminal_cannot_encode_does_not_cut_the_result_short(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="ascii", write_through=True)
        r = H.Report()
        r.add("LAYOUT_X", REFUSED, "/srv/škola")
        r.emit(out)
        text = raw.getvalue().decode("ascii")
        self.assertIn("RESULT: REFUSED", text)
        self.assertTrue(text.rstrip().endswith("INSTALLATION_NOT_STARTED"))

    def test_a_dead_pipe_does_not_change_the_exit_status(self):
        class Dead(io.StringIO):
            def write(self, *_):
                raise BrokenPipeError()
        r = self.check()
        r.emit(Dead())
        self.assertEqual(r.exit_code(), 0)


@unittest.skipUnless(SYMLINKS, "cannot create symlinks on this platform")
class HandoffTests(Tree):
    def run_handoff(self, layout=None, **kw):
        out = io.StringIO()
        opts = kw.pop("opts", H.Options(accept_unmeasured_pools=True))
        rc = H.handoff(layout or self.layout(), self.host, self.actions, opts, out=out, **kw)
        return rc, out.getvalue()

    def base(self):
        return self.state.parent

    def test_a_clean_check_creates_only_the_missing_links_and_directories(self):
        rc, out = self.run_handoff()
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.readlink(self.src / ".env"), str(self.cfg / ".env"))
        for name in L.STATE_DIRS:
            self.assertTrue((self.state / name).is_dir(), name)
            self.assertEqual(os.path.realpath(self.src / name), os.path.realpath(self.state / name))
        self.assertEqual({op for op, *_ in self.actions.log}, {"mkdir", "symlink"})
        self.assertEqual(L.validate(self.layout(), links="require"), [])
        self.assertTrue(out.rstrip().endswith("INSTALLATION_NOT_STARTED"))
        self.assertEqual(self.actions.bootstrap_calls, [])                     # print-only by default

    @unittest.skipUnless(POSIX, "modes need POSIX")
    def test_the_directories_get_the_modes_bootstrap_expects(self):
        self.run_handoff()
        for name in L.STATE_DIRS:
            self.assertEqual(stat.S_IMODE(os.stat(self.state / name).st_mode), bootstrap.DATA_DIRS[name][2], name)

    def test_the_printed_command_resolves_to_the_same_layout_and_carries_no_secret(self):
        rc, out = self.run_handoff()
        line = next(l for l in out.splitlines() if l.startswith("NEXT_COMMAND:"))
        argv = shlex.split(line.split(" NOTE ", 1)[1])
        self.assertEqual(argv[:2], ["python3", str(self.src / "scripts" / "bootstrap.py")])
        import argparse
        parser = argparse.ArgumentParser()
        L.add_arguments(parser)
        again = L.resolve(parser.parse_args(argv[2:]), {}, self.src)
        lay = self.layout()
        self.assertEqual((again.source_root, again.config_dir, again.state_dir), (lay.source_root, lay.config_dir, lay.state_dir))
        for secret in list(SENTINELS.values()) + [SECRET_KEY]:
            self.assertNotIn(secret, out)
        self.assertEqual(argv[2:], lay.argv())

    def test_a_second_run_changes_nothing(self):
        self.run_handoff()
        before = self.snapshot(self.base())
        self.actions.log.clear()
        rc, out = self.run_handoff()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.actions.log, [])
        self.assertEqual(self.snapshot(self.base()), before)
        self.assertEqual(self.check().exit_code(), 0)

    def test_nothing_is_created_unless_the_check_is_clean(self):
        cases = {"port busy": lambda: setattr(self.world, "ss", ss_text(("0.0.0.0", 80))),
                 "ss garbage": lambda: self.world.override.update({"ss": Proc(0, "???", "")}),
                 "docker down": lambda: self.world.override.update({"info": Proc(1, "", "")}),
                 "model cannot render": lambda: self.world.override.update({"config": Proc(1, "", "")}),
                 "collision": lambda: setattr(self.world, "containers", [{"Names": "edu_ai", "Labels": ""}]),
                 "wrong arch": lambda: setattr(self.world, "uname", "aarch64"),
                 "state not empty": lambda: (self.state / "x").write_text("x"),
                 "pools not accepted": lambda: None}
        for name, prepare in cases.items():
            with self.subTest(name):
                self.setUp()
                prepare()
                before = self.snapshot(self.base())
                opts = H.Options() if name == "pools not accepted" else None
                rc, out = self.run_handoff(opts=opts)
                self.assertIn(rc, (2, 3), out)
                self.assertEqual(self.snapshot(self.base()), before)
                self.assertEqual(self.actions.log, [])
                self.assertIn("HANDOFF: NOTE not performed", out)
                self.assertNotIn("NEXT_COMMAND", out)

    def test_a_conflicting_name_is_never_replaced(self):
        for name, make, code in ((".env", lambda: (self.src / ".env").write_text("PRECIOUS=1\n"), "LINK_CONFLICT"),
                                 ("uploads", lambda: ((self.src / "uploads").mkdir(), (self.src / "uploads" / "f").write_text("data")), "LINK_CONFLICT"),
                                 ("langflow_data", lambda: (self.src / "langflow_data").write_text("x"), "LINK_CONFLICT")):
            with self.subTest(name):
                self.setUp()
                make()
                before = self.snapshot(self.base())
                rc, out = self.run_handoff()
                self.assertEqual(rc, 2, out)
                self.assertEqual(self.snapshot(self.base()), before)
                self.assertEqual(self.actions.log, [])
                self.assertIn(code, out)

    def test_a_name_that_appears_after_the_check_is_not_replaced(self):
        real = FakeActions.symlink

        def racing(actions, target, path):
            if path.endswith(".env"):
                pathlib.Path(path).write_text("RACED=1\n")             # someone creates the name between check and link
            real(actions, target, path)
        with mock.patch.object(FakeActions, "symlink", racing):
            rc, out = self.run_handoff()
        self.assertEqual(rc, 2, out)
        self.assertEqual((self.src / ".env").read_text(), "RACED=1\n")
        self.assertFalse((self.src / ".env").is_symlink())
        self.assertIn("nothing was replaced or removed", out)

    def test_a_failure_part_way_keeps_what_was_made_and_removes_nothing(self):
        self.actions.fail["symlink"] = PermissionError("syn")
        rc, out = self.run_handoff()
        self.assertEqual(rc, 3, out)
        self.assertTrue((self.state / "uploads").is_dir())                 # made, valid, left in place
        self.assertIn("what was made stays", out)
        self.assertNotIn("NEXT_COMMAND", out)
        self.actions.fail.clear()
        rc, out = self.run_handoff()                                         # and the next run finishes the job
        self.assertEqual(rc, 0, out)

    def test_a_failed_directory_creation_is_unverified(self):
        self.actions.fail["mkdir"] = PermissionError("syn")
        rc, out = self.run_handoff()
        self.assertEqual(rc, 3, out)

    def test_the_env_link_is_the_one_step_that_may_precede_a_clean_check(self):
        def render(argv):
            if not os.path.lexists(self.src / ".env"):
                return Proc(1, "", f"env file {self.src}/.env not found")
            return Proc(0, json.dumps(self.world.model), "")
        self.world.override["config"] = render
        first = self.check()
        self.expect_level(first, "MODEL", UNVERIFIED)
        rc, out = self.run_handoff(env_link_only=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.src / ".env").is_symlink())
        for name in L.STATE_DIRS:
            self.assertFalse((self.src / name).exists() or (self.src / name).is_symlink(), name)
        self.assertIn("only the .env link was created", out)
        self.assertEqual([op for op, *_ in self.actions.log], ["symlink"])
        rc, out = self.run_handoff()
        self.assertEqual(rc, 0, out)
        self.assertEqual(L.validate(self.layout(), links="require"), [])

    def expect_level(self, report, code, level):
        self.assertIn(level, self.level(report, code), text_of(report))

    def test_the_env_link_is_not_created_when_anything_is_refused(self):
        self.world.override["config"] = Proc(1, "", "env file not found")
        (self.state / "stray").write_text("x")
        rc, out = self.run_handoff(env_link_only=True)
        self.assertEqual(rc, 2, out)
        self.assertFalse(os.path.lexists(self.src / ".env"))
        self.assertEqual(self.actions.log, [])

    def test_the_env_link_is_not_created_when_something_else_is_unverified(self):
        self.world.override["config"] = Proc(1, "", "env file not found")
        self.world.override["info"] = Proc(1, "", "")
        rc, out = self.run_handoff(env_link_only=True)
        self.assertEqual(rc, 3, out)
        self.assertFalse(os.path.lexists(self.src / ".env"))

    def test_exec_runs_bootstrap_with_the_layout_flags_and_a_fixed_environment(self):
        self.host.env = {"PATH": "/usr/bin", "HOME": "/root", "CI": "true", "LEAK": "SENTINEL-leak"}
        rc, out = self.run_handoff(do_exec=True)
        self.assertEqual(rc, 0, out)
        (argv, cwd, env), = self.actions.bootstrap_calls
        self.assertEqual(argv, [sys.executable, str(self.src / "scripts" / "bootstrap.py"), *self.layout().argv()])
        self.assertEqual(cwd, str(self.src))
        self.assertEqual(env["CI"], "true")                                    # the rule that hides generated secrets survives
        self.assertNotIn("LEAK", env)
        self.assertNotIn("SENTINEL-leak", json.dumps(env))
        self.assertLess(out.index("INSTALLATION_NOT_STARTED"), out.index("BOOTSTRAP_LAUNCHED"))
        self.assertIn("BOOTSTRAP_EXIT: 0", out)

    def test_exec_maps_the_exit_status_of_bootstrap_without_calling_it_nothing_changed(self):
        for rc_boot, expect, word in ((3, 3, "repeat later"), (1, 3, "partial changes"), (0, 0, "finished")):
            with self.subTest(rc=rc_boot):
                self.setUp()
                self.actions.bootstrap_rc = rc_boot
                rc, out = self.run_handoff(do_exec=True)
                self.assertEqual(rc, expect, out)
                self.assertIn(word, out)

    def test_exec_never_starts_bootstrap_when_the_check_is_not_clean(self):
        self.world.ss = ss_text(("0.0.0.0", 80))
        rc, out = self.run_handoff(do_exec=True)
        self.assertEqual(rc, 2)
        self.assertEqual(self.actions.bootstrap_calls, [])

    def test_exec_never_starts_bootstrap_after_only_the_env_link(self):
        self.world.override["config"] = Proc(1, "", "env file not found")
        rc, out = self.run_handoff(do_exec=True, env_link_only=True)
        self.assertEqual(self.actions.bootstrap_calls, [])


class PinEnvTests(Tree):
    def env_without(self, *keys):
        for key in keys:
            self.env_values.pop(key, None)
        self.write_set()
        return (self.cfg / ".env").read_bytes()

    def pin(self, **kw):
        report = H.pin_env(kw.pop("layout", None) or self.layout(), self.host, self.actions, **kw)
        return report

    def test_only_absent_keys_are_appended_and_every_old_byte_is_kept(self):
        old = self.env_without("COMPOSE_FILE", "EDU_AI_TUNNEL_SUBNET")
        r = self.pin(tunnel_subnet_value=SUBNET)
        new = (self.cfg / ".env").read_bytes()
        self.assertEqual(r.exit_code(), 0, text_of(r))
        self.assertTrue(new.startswith(old))
        self.assertEqual(new[len(old):].decode().splitlines()[-2:], ["COMPOSE_FILE=docker-compose.yml", f"EDU_AI_TUNNEL_SUBNET={SUBNET}"])
        values, dups, bad = H.install.parse_assignments(new.decode())
        self.assertEqual(dups, [])
        self.assertEqual(values["COMPOSE_PROJECT_NAME"], "edu_ai")
        self.assertEqual(new.count(b"COMPOSE_PROJECT_NAME"), 1)
        self.assertNotIn(b"\r", new)
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(self.cfg / ".env").st_mode), 0o600)
        self.assertEqual([op for op, *_ in self.actions.log], ["write_env"])

    def test_the_output_names_keys_and_never_values(self):
        self.env_without("COMPOSE_FILE")
        out = text_of(self.pin(tunnel_subnet_value="10.99.8.0/29", extra=("CHAT_LEGAL_ENTITY=ZS_SENTINEL_ENTITY",)))
        self.assertIn("COMPOSE_FILE", out)
        for value in ("10.99.8.0", "ZS_SENTINEL_ENTITY", "docker-compose.yml"):
            self.assertNotIn(value, out)
        self.assertNotIn("SENTINEL-", out)
        for line in out.splitlines():
            self.assertIsNone(re.search(r"\b[A-Z][A-Z0-9_]{3,}=\S", line), line)

    def test_an_existing_key_is_never_changed_even_when_it_differs_or_is_empty(self):
        text = "COMPOSE_FILE=other.yml\nCOMPOSE_PROJECT_NAME=\nEDU_AI_TUNNEL_SUBNET=10.1.2.0/29\nLANGFLOW_SECRET_KEY=SENTINEL-k\n"
        self.write_set(env_text=text)
        old = (self.cfg / ".env").read_bytes()
        r = self.pin(tunnel_subnet_value="10.9.9.0/29")
        self.assertEqual((self.cfg / ".env").read_bytes(), old)
        self.assertEqual(r.exit_code(), 0)
        self.assertIn("PIN_ENV_DIFFERS", text_of(r))
        self.assertEqual(self.actions.log, [])

    def test_a_second_run_is_a_no_op(self):
        self.env_without("COMPOSE_FILE", "EDU_AI_TUNNEL_SUBNET")
        self.pin(tunnel_subnet_value=SUBNET)
        after = (self.cfg / ".env").read_bytes()
        self.actions.log.clear()
        r = self.pin(tunnel_subnet_value=SUBNET)
        self.assertEqual((self.cfg / ".env").read_bytes(), after)
        self.assertEqual(self.actions.log, [])
        self.assertIn("nothing to add", text_of(r))

    def test_a_file_without_a_final_newline_and_an_empty_file(self):
        for body in ("COMPOSE_PROJECT_NAME=edu_ai", ""):
            with self.subTest(body=body):
                self.write_set(env_text=body)
                self.pin()
                new = (self.cfg / ".env").read_bytes().decode()
                values, dups, bad = H.install.parse_assignments(new)
                self.assertEqual(values["COMPOSE_FILE"], "docker-compose.yml")
                self.assertEqual(bad, [])
                if body:
                    self.assertTrue(new.startswith(body + "\n"))

    def test_the_project_is_pinned_from_the_layout_else_the_default(self):
        self.write_set(env_text="LANGFLOW_SECRET_KEY=x\n")
        self.pin(layout=self.layout("--project", "edu_ai_lab"))
        self.assertIn(b"COMPOSE_PROJECT_NAME=edu_ai_lab\n", (self.cfg / ".env").read_bytes())
        self.write_set(env_text="LANGFLOW_SECRET_KEY=x\n")
        self.pin()
        self.assertIn(b"COMPOSE_PROJECT_NAME=edu_ai\n", (self.cfg / ".env").read_bytes())

    def test_no_secret_is_generated_or_written(self):
        old = self.env_without("COMPOSE_FILE", "EDU_AI_TUNNEL_SUBNET", "COMPOSE_PROJECT_NAME")
        self.pin(tunnel_subnet_value=SUBNET)
        added = (self.cfg / ".env").read_bytes()[len(old):].decode()
        names = {l.split("=")[0] for l in added.splitlines() if "=" in l and not l.startswith("#")}
        self.assertEqual(names, {"COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "EDU_AI_TUNNEL_SUBNET"})

    def test_the_tunnel_subnet_comes_from_the_operator_or_from_the_read_only_allocator(self):
        old = self.env_without("EDU_AI_TUNNEL_SUBNET")
        r = self.pin()
        self.assertIn("PIN_ENV_ABSENT", text_of(r))
        self.assertNotIn(b"EDU_AI_TUNNEL_SUBNET", (self.cfg / ".env").read_bytes())
        r = self.pin(allocate=True, reserved=("192.168.0.0/16",))
        self.assertEqual(r.exit_code(), 0, text_of(r))
        self.assertIn(f"EDU_AI_TUNNEL_SUBNET={SUBNET}".encode(), (self.cfg / ".env").read_bytes())
        argv = next(a for a in self.host.calls if key_of(a) == "tunnel_allocate")
        self.assertEqual(argv[2:], ["allocate", "--project", "edu_ai", "--routes", "host", "--reserved", "192.168.0.0/16"])

    def test_a_failed_allocation_writes_nothing(self):
        old = self.env_without("COMPOSE_FILE", "EDU_AI_TUNNEL_SUBNET")
        for proc, level in ((Proc(2, "REFUSED: x", ""), 2), (Proc(3, "REFUSED (cannot verify): x", ""), 3), (Proc(1, "Traceback", ""), 3),
                            (Proc(0, "garbage", ""), 3), (Proc(0, "EDU_AI_TUNNEL_SUBNET=8.8.8.0/29\n", ""), 3),
                            (Proc(0, "EDU_AI_TUNNEL_SUBNET=10.1.1.0/24\n", ""), 3), (H.ProbeFailed("FileNotFoundError"), 3)):
            with self.subTest(proc=str(proc)[:40]):
                self.world.override["tunnel_allocate"] = proc
                r = self.pin(allocate=True)
                self.assertEqual(r.exit_code(), level, text_of(r))
                self.assertEqual((self.cfg / ".env").read_bytes(), old)
        self.assertEqual(self.actions.log, [])

    def test_allocation_is_not_asked_when_the_key_is_already_there(self):
        self.pin(allocate=True)
        self.assertFalse([a for a in self.host.calls if key_of(a) == "tunnel_allocate"])

    def test_an_unusable_subnet_is_refused_and_nothing_is_written(self):
        old = self.env_without("COMPOSE_FILE", "EDU_AI_TUNNEL_SUBNET")
        for bad in ("10.77.3.0/24", "8.8.8.0/29", "10.77.3.5/29", "garbage", "", "10.77.3.0/29\n$(x)"):
            with self.subTest(bad=bad):
                r = self.pin(tunnel_subnet_value=bad)
                self.assertEqual(r.exit_code(), 2, text_of(r))
                self.assertEqual((self.cfg / ".env").read_bytes(), old)

    def test_extra_keys_must_be_plain_names_and_values(self):
        old = self.env_without("COMPOSE_FILE")
        for bad in ("POSTGRES_PASSWORD=x", "MY_TOKEN=x", "X_KEY=1", "LANGFLOW_SECRET_KEY=x", "A_CREDENTIAL=1", "lower=x", "NOEQUALS",
                    "NAME=a$b", "NAME=a\nB=c", "NAME= lead", "NAME=trail ", "NAME=x #c", "=x", "N AME=x", "NAME=x\r"):
            with self.subTest(bad=bad):
                r = self.pin(extra=(bad,))
                self.assertEqual(r.exit_code(), 2, text_of(r))
                self.assertEqual((self.cfg / ".env").read_bytes(), old)
        r = self.pin(extra=("CHAT_LEGAL_ENTITY=ZS_TEST", "QA_PORTAL_ALLOWED_ORIGINS=http://localhost"))
        self.assertEqual(r.exit_code(), 0, text_of(r))
        text = (self.cfg / ".env").read_text()
        self.assertIn("CHAT_LEGAL_ENTITY=ZS_TEST\n", text)

    # atomicity
    def listing(self):
        return sorted(p.name for p in self.cfg.iterdir())

    def test_a_failed_replace_leaves_the_file_and_the_directory_untouched(self):
        old = self.env_without("COMPOSE_FILE")
        names = self.listing()
        with mock.patch.object(envfile.os, "replace", side_effect=OSError("syn")):
            r = self.pin()
        self.assertEqual(r.exit_code(), 3, text_of(r))
        self.assertEqual((self.cfg / ".env").read_bytes(), old)
        self.assertEqual(self.listing(), names)
        self.assertIn("byte-identical", text_of(r))

    def test_an_interrupt_before_the_replace_leaves_it_byte_identical(self):
        old = self.env_without("COMPOSE_FILE")
        names = self.listing()
        with mock.patch.object(envfile.os, "replace", side_effect=KeyboardInterrupt()):
            r = self.pin()
        self.assertEqual(r.exit_code(), 3)
        self.assertEqual((self.cfg / ".env").read_bytes(), old)
        self.assertEqual(self.listing(), names)

    def test_a_failure_while_writing_the_temporary_file_leaves_it_byte_identical(self):
        old = self.env_without("COMPOSE_FILE")
        names = self.listing()
        for exc in (OSError("disk full"), KeyboardInterrupt()):
            with self.subTest(exc=type(exc).__name__), mock.patch.object(envfile.os, "fsync", side_effect=exc):
                r = self.pin()
                self.assertEqual(r.exit_code(), 3)
                self.assertEqual((self.cfg / ".env").read_bytes(), old)
                self.assertEqual(self.listing(), names)

    def test_an_interrupt_after_the_replace_is_reported_from_the_disk_not_from_the_exception(self):
        self.env_without("COMPOSE_FILE")
        with mock.patch.object(envfile, "_fsync_dir", side_effect=KeyboardInterrupt()):
            r = self.pin()
        self.assertIn(b"COMPOSE_FILE=docker-compose.yml", (self.cfg / ".env").read_bytes())
        self.assertEqual(r.exit_code(), 0, text_of(r))
        self.assertIn("completed before", text_of(r))

    def test_a_write_that_cannot_be_classified_is_unverified(self):
        self.env_without("COMPOSE_FILE")
        real = envfile.write_atomic

        def corrupt(path, data):
            real(path, b"SOMETHING=else\n")
            raise OSError("syn")
        with mock.patch.object(FakeActions, "write_env", lambda self_, path, data: corrupt(path, data)):
            r = self.pin()
        self.assertEqual(r.exit_code(), 3)
        self.assertIn("unknown", text_of(r))

    def test_a_file_that_changes_during_the_run_is_not_overwritten(self):
        old = self.env_without("COMPOSE_FILE")
        seen = {"n": 0}

        def hook(path, nth):
            if path.endswith(".env") and nth == 2:
                return old + b"NEW_BY_SOMEONE=1\n"
            return None
        self.host.read_hooks.append(hook)
        r = self.pin()
        self.assertEqual(r.exit_code(), 3, text_of(r))
        self.assertEqual((self.cfg / ".env").read_bytes(), old)
        self.assertEqual(self.actions.log, [])

    # refusals
    def test_refuses_what_it_cannot_prove_safe(self):
        self.env_without("COMPOSE_FILE")
        old = (self.cfg / ".env").read_bytes()
        (self.cfg / ".env").write_bytes(old.replace(b"\n", b"\r\n"))
        crlf = (self.cfg / ".env").read_bytes()
        self.assertEqual(self.pin().exit_code(), 2)
        self.assertEqual((self.cfg / ".env").read_bytes(), crlf)
        (self.cfg / ".env").unlink()
        self.assertEqual(self.pin().exit_code(), 2)
        self.assertFalse((self.cfg / ".env").exists())

    def test_refuses_a_set_that_is_not_private_or_not_ours(self):
        old = self.env_without("COMPOSE_FILE")
        self.host.fake_modes = True
        for mode, uid in ((0o644, None), (0o640, None), (0o660, None), (0o600, 4242)):
            with self.subTest(mode=oct(mode), uid=uid):
                self.host.modes = {str(self.cfg / ".env"): (mode, uid)}
                r = self.pin()
                self.assertEqual(r.exit_code(), 2, text_of(r))
                self.assertEqual((self.cfg / ".env").read_bytes(), old)
        self.host.modes = {}
        with mock.patch.object(self.host, "posix", return_value=False):
            self.assertEqual(self.pin().exit_code(), 3)

    @unittest.skipUnless(SYMLINKS, "cannot create symlinks on this platform")
    def test_refuses_an_env_that_is_a_symlink(self):
        (self.cfg / ".env").rename(self.cfg / "real.env")
        os.symlink(self.cfg / "real.env", self.cfg / ".env")
        self.assertEqual(self.pin().exit_code(), 2)

    def test_refuses_without_an_external_configuration_set(self):
        import argparse
        parser = argparse.ArgumentParser()
        L.add_arguments(parser)
        legacy = L.resolve(parser.parse_args(["--source-root", str(self.src)]), {}, self.src)
        self.assertEqual(self.pin(layout=legacy).exit_code(), 2)

    def test_an_unreadable_env_is_unverified_and_nothing_is_written(self):
        self.env_without("COMPOSE_FILE")
        with mock.patch.object(self.host, "read_bytes", side_effect=PermissionError("syn")):
            r = self.pin()
        self.assertEqual(r.exit_code(), 3)
        self.assertEqual(self.actions.log, [])

    def test_the_pins_satisfy_the_check_afterwards(self):
        self.env_without("COMPOSE_FILE", "EDU_AI_TUNNEL_SUBNET")
        before = self.check()
        self.assertEqual(self.level(before, "COMPOSE_FILE"), [REFUSED])
        self.pin(tunnel_subnet_value=SUBNET)
        after = self.check()
        self.assertEqual(after.exit_code(), 0, text_of(after))
        self.assertEqual(self.level(after, "COMPOSE_FILE"), [OK])


class CliTests(Tree):
    def main(self, *argv, environ=None, host=None, actions=None):
        out = io.StringIO()
        rc = H.main(list(argv), host=host or self.host, actions=actions or self.actions, environ=environ if environ is not None else {}, out=out)
        return rc, out.getvalue()

    def flags(self):
        return ["--source-root", str(self.src), "--config-dir", str(self.cfg), "--state-dir", str(self.state)]

    def test_check_end_to_end_and_the_exit_codes(self):
        rc, out = self.main("check", *self.flags(), "--accept-unmeasured-network-pools")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.rstrip().endswith("INSTALLATION_NOT_STARTED"))
        rc, out = self.main("check", *self.flags())
        self.assertEqual(rc, 3, out)
        self.world.ss = ss_text(("0.0.0.0", 80))
        rc, out = self.main("check", *self.flags(), "--accept-unmeasured-network-pools")
        self.assertEqual(rc, 2, out)

    def test_the_layout_comes_from_flags_or_the_edu_ai_variables(self):
        env = {"EDU_AI_SOURCE_ROOT": str(self.src), "EDU_AI_CONFIG_DIR": str(self.cfg), "EDU_AI_STATE_DIR": str(self.state)}
        rc, out = self.main("check", "--accept-unmeasured-network-pools", environ=env)
        self.assertEqual(rc, 0, out)

    def test_an_invalid_layout_or_option_is_refused_without_echoing_the_value(self):
        rc, out = self.main("check", "--project", "Bad Name SENTINEL-proj", *self.flags())
        self.assertEqual(rc, 2)
        self.assertIn("LAYOUT_PROJECT_INVALID", out)
        self.assertNotIn("SENTINEL-proj", out)
        rc, out = self.main("check", *self.flags(), "--min-compose", "x.y")
        self.assertEqual(rc, 2)
        self.assertIn("OPTIONS", out)
        rc, out = self.main("check", *self.flags(), "--min-free-gb", "-1")
        self.assertEqual(rc, 2)
        self.assertEqual(self.host.calls, [])

    def test_the_options_reach_the_check(self):
        self.host.disk["/var/lib/docker"] = 30 * GIB
        rc, out = self.main("check", *self.flags(), "--accept-unmeasured-network-pools", "--min-free-gb", "40")
        self.assertEqual(rc, 2)
        self.assertIn("DISK_DOCKER_ROOT: REFUSED", out)
        rc, out = self.main("check", *self.flags(), "--accept-unmeasured-network-pools", "--min-compose", "9.0.0")
        self.assertIn("COMPOSE_VERSION: REFUSED", out)
        rc, out = self.main("check", *self.flags(), "--accept-unmeasured-network-pools", "--reserved", "172.20.0.0/14")
        argv = [a for a in self.host.calls if key_of(a) == "tunnel_check"][-1]
        self.assertIn("172.20.0.0/14", argv)

    def test_a_command_off_the_whitelist_is_a_bug_that_stops_the_run_and_runs_nothing(self):
        def bad(self_):
            self_.host.run(["docker", "compose", "down"])
        with mock.patch.object(H.Preflight, "step_arch_host", bad):
            rc, out = self.main("check", *self.flags())
        self.assertEqual(rc, 3)
        self.assertIn("INTERNAL_ERROR: UNVERIFIED", out)
        self.assertNotIn(["docker", "compose", "down"], self.host.calls)

    def test_a_bug_or_an_interrupt_is_reported_not_traced(self):
        with mock.patch.object(H.Preflight, "run", side_effect=RuntimeError("SENTINEL-in-bug")):
            rc, out = self.main("check", *self.flags())
        self.assertEqual((rc, "INTERNAL_ERROR: UNVERIFIED RuntimeError" in out, "SENTINEL" in out), (3, True, False))
        with mock.patch.object(H.Preflight, "run", side_effect=KeyboardInterrupt()):
            rc, out = self.main("check", *self.flags())
        self.assertEqual(rc, 3)
        self.assertIn("INTERRUPTED: UNVERIFIED", out)

    def test_pin_env_through_the_command_line(self):
        self.env_values.pop("COMPOSE_FILE")
        self.write_set()
        rc, out = self.main("pin-env", *self.flags())
        self.assertEqual(rc, 0, out)
        self.assertIn("PIN_ENV_ADDED: OK COMPOSE_FILE", out)
        self.assertIn(b"COMPOSE_FILE=docker-compose.yml", (self.cfg / ".env").read_bytes())

    def test_the_flag_set_has_no_destructive_or_rotating_option(self):
        parser = H.build_parser()
        options = set()
        for action in parser._subparsers._group_actions[0].choices.values():
            options.update(o for a in action._actions for o in a.option_strings)
        for forbidden in ("--force", "--down", "--remove", "--prune", "--volumes", "--rotate", "--reveal-key", "--yes", "--delete"):
            self.assertNotIn(forbidden, options)
        self.assertEqual(set(parser._subparsers._group_actions[0].choices), {"check", "handoff", "pin-env"})

    def test_layout_flags_are_the_shared_ones(self):
        parser = H.build_parser()
        check = parser._subparsers._group_actions[0].choices["check"]._option_string_actions
        for names in L.FLAGS.values():
            self.assertIn(names[0], check)


class RealHostTests(unittest.TestCase):
    """The real Host's file reads, on a synthetic directory. No command is started."""

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = pathlib.Path(self._td.name)

    def test_read_bytes_is_bounded_and_regular(self):
        (self.dir / "f").write_bytes(b"abc")
        self.assertEqual(H.Host().read_bytes(self.dir / "f", 10), b"abc")
        with self.assertRaises(ValueError):
            H.Host().read_bytes(self.dir / "f", 2)
        with self.assertRaises((OSError, ValueError)):          # a directory: refused at open on Windows, at fstat on Linux
            H.Host().read_bytes(self.dir, 10)

    @unittest.skipUnless(SYMLINKS and POSIX, "needs symlinks and O_NOFOLLOW")
    def test_read_bytes_does_not_follow_a_symlink(self):
        (self.dir / "f").write_bytes(b"abc")
        os.symlink(self.dir / "f", self.dir / "l")
        with self.assertRaises(OSError):
            H.Host().read_bytes(self.dir / "l", 10)

    def test_disk_free_is_a_positive_integer(self):
        free = H.Host().disk_free(self.dir)
        self.assertIsInstance(free, int)
        self.assertGreater(free, 0)

    def test_listdir_lstat_and_environ(self):
        (self.dir / "b").write_text("x")
        (self.dir / "a").write_text("x")
        host = H.Host()
        self.assertEqual(host.listdir(self.dir), ["a", "b"])
        self.assertTrue(stat.S_ISREG(host.lstat(self.dir / "a").st_mode))
        self.assertIsInstance(host.environ(), dict)
        self.assertEqual(host.platform(), sys.platform)


@unittest.skipUnless(sys.platform.startswith("linux"), "the real Actions use Linux file modes")
class RealActionsTests(unittest.TestCase):
    def test_mkdir_sets_the_mode_despite_the_umask_and_never_replaces(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / "d"
            old = os.umask(0o077)
            try:
                H.Actions().mkdir(target, 0o755)
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)
            with self.assertRaises(FileExistsError):
                H.Actions().mkdir(target, 0o700)
            os.symlink(target, pathlib.Path(td) / "l")
            with self.assertRaises(FileExistsError):
                H.Actions().symlink(td, pathlib.Path(td) / "l")
            with self.assertRaises(FileExistsError):
                H.Actions().symlink(td, target)


if __name__ == "__main__":
    unittest.main()
