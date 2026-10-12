"""Offline C2 fixture and ownership guards. Every Docker/psql call is mocked."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import migrate_test_env as env
import test_migrate_runner as runner


NAME = "b03e_guard_test"
VOLUME = f"{NAME}_data"
CID = "a" * 64


class FixtureTests(unittest.TestCase):
    def setUp(self):
        labels = {env.PURPOSE_KEY: env.PURPOSE, env.OWNER_KEY: NAME}
        self.resources = {
            ("network", env.NETWORK): {"Name": env.NETWORK, "Labels": {env.PURPOSE_KEY: env.PURPOSE}, "Internal": True},
            ("volume", VOLUME): {"Name": VOLUME, "Labels": dict(labels)},
            ("container", NAME): {
                "Id": CID, "Name": f"/{NAME}", "Config": {"Labels": dict(labels)},
                "Mounts": [{"Type": "volume", "Name": VOLUME, "Destination": env.DATA_TARGET, "RW": True}],
                "NetworkSettings": {"Networks": {env.NETWORK: {}}}, "HostConfig": {"PortBindings": {}},
            },
        }
        self.present = {("network", env.NETWORK), ("image", env.IMAGE)}
        self.commands, self.copied, self.replies = [], {}, {}
        self.users = NAME + "\n"
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        init = self.root / "postgres" / "init_clean"
        init.mkdir(parents=True)
        self.init_names = {p.name for p in (env.ROOT / "postgres" / "init_clean").iterdir()
                           if p.suffix in (".sql", ".psql")}
        for name in self.init_names:
            (init / name).write_bytes(b"-- synthetic fixture\r\nSELECT 1;\r\n")
        (init / "not-an-init-file.txt").write_text("ignored", encoding="utf-8")
        for target, value in (("docker", mock.Mock(side_effect=self.docker)),
                              ("exists", mock.Mock(side_effect=lambda kind, name: (kind, name) in self.present)),
                              ("psql", mock.Mock(return_value="1")), ("ROOT", self.root)):
            patcher = mock.patch.object(env, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.passwords = [f"{number:048x}" for number in range(5)]
        token_patcher = mock.patch.object(env.secrets, "token_hex", side_effect=self.passwords)
        self.tokens = token_patcher.start()
        self.addCleanup(token_patcher.stop)

    def docker(self, *args, **kwargs):
        self.commands.append(args)
        if args in self.replies:
            return self.replies[args]
        if len(args) > 1 and args[1] == "inspect":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.resources[(args[0], args[-1])]), "")
        if args[:2] == ("volume", "create"):
            self.present.add(("volume", args[-1]))
        if args[:2] == ("network", "create"):
            self.present.add(("network", env.NETWORK))
        if args[0] == "cp":
            self.copied = {p.name: p.read_bytes() for p in Path(args[1]).iterdir()}
        return subprocess.CompletedProcess(args, 0, self.users if args[0] == "ps" else "", "")

    def mutations(self):
        return [args for args in self.commands
                if args[0] in ("create", "cp", "start", "rm", "build")
                or args[:2] in (("network", "create"), ("volume", "create"), ("volume", "rm"))]

    def call(self, action):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            action(NAME)
        return output.getvalue()

    def assert_down_refused(self):
        with self.assertRaises(SystemExit):
            self.call(env.down)
        self.assertEqual(self.mutations(), [])

    def test_up_uses_c2_admin_and_five_distinct_synthetic_passwords_without_printing_them(self):
        output = self.call(env.up)
        create = next(args for args in self.commands if args[0] == "create")
        values = dict(create[index + 1].split("=", 1) for index, arg in enumerate(create[:-1]) if arg == "-e")
        self.assertEqual(values["POSTGRES_USER"], "edu_ai_admin")
        self.assertEqual(values["POSTGRES_DB"], "edu_ai")
        self.assertEqual(runner.USER, env.USER)
        passwords = [values[key] for key in env.PASSWORD_ENV]
        self.assertEqual(len(passwords), 5)
        self.assertEqual(len(set(passwords)), 5)
        self.assertTrue(all(len(value) >= 16 for value in passwords))
        self.assertTrue(all(value not in output for value in passwords))
        self.assertIn(f"{env.OWNER_KEY}={NAME}", create)
        self.assertIn(f"{env.OWNER_KEY}={NAME}", next(args for args in self.commands if args[:2] == ("volume", "create")))
        self.assertTrue(all(call.kwargs.get("quiet") is True for call in env.docker.call_args_list
                            if len(call.args) > 1 and call.args[1] == "inspect"))

    def test_up_copies_all_sql_and_psql_includes_with_lf_only(self):
        self.call(env.up)
        self.assertEqual(set(self.copied), self.init_names)
        self.assertEqual(sum(name.endswith(".psql") for name in self.copied), 4)
        self.assertTrue(all(b"\r" not in value for value in self.copied.values()))
        self.assertIn("04_role_contract.sql", self.copied)

    def test_duplicate_or_short_passwords_stop_before_any_resource_creation(self):
        for values in (["0" * 48] * 5, ["short", *self.passwords[1:]]):
            with self.subTest(lengths=[len(value) for value in values]):
                self.commands.clear()
                self.tokens.side_effect = values
                with self.assertRaises(SystemExit):
                    self.call(env.up)
                self.assertEqual(self.mutations(), [])

    def test_existing_container_or_volume_is_never_adopted(self):
        for kind, name in (("container", NAME), ("volume", VOLUME)):
            with self.subTest(kind=kind):
                self.commands.clear()
                self.present.add((kind, name))
                with self.assertRaises(SystemExit):
                    self.call(env.up)
                self.assertEqual(self.mutations(), [])
                self.present.remove((kind, name))

    def test_existing_network_must_have_exact_purpose_and_internal_true(self):
        valid = copy.deepcopy(self.resources[("network", env.NETWORK)])
        for updates in ({"Internal": False}, {"Internal": "true"}, {"Labels": {}},
                        {"Labels": {env.PURPOSE_KEY: env.PURPOSE + "-foreign"}}, {"Name": "foreign"}):
            with self.subTest(updates=updates):
                self.commands.clear()
                self.tokens.side_effect = list(self.passwords)
                self.resources[("network", env.NETWORK)] = {**valid, **updates}
                with self.assertRaises(SystemExit):
                    self.call(env.up)
                self.assertEqual(self.mutations(), [])

    def test_missing_network_is_created_internal_and_labelled_before_any_volume(self):
        self.present.remove(("network", env.NETWORK))
        self.call(env.up)
        creation = next(args for args in self.commands if args[:2] == ("network", "create"))
        self.assertIn("--internal", creation)
        self.assertIn(env.LABEL, creation)
        self.assertLess(self.commands.index(creation), next(index for index, args in enumerate(self.commands)
                                                          if args[:2] == ("volume", "create")))

    def test_unreadable_or_malformed_inspection_stops_without_deleting_anything(self):
        for kind, name in (("container", NAME), ("network", env.NETWORK), ("volume", VOLUME)):
            for result in ((1, ""), (0, "not JSON"), (0, "[]")):
                with self.subTest(kind=kind, result=result):
                    self.commands.clear()
                    key = (kind, "inspect", "--format", "{{json .}}", name)
                    self.replies = {key: subprocess.CompletedProcess(key, result[0], result[1], "")}
                    self.assert_down_refused()

    def test_down_requires_exact_container_purpose_owner_identity_and_isolation(self):
        valid = copy.deepcopy(self.resources[("container", NAME)])
        changes = ({"Config": {"Labels": {}}},
                   {"Config": {"Labels": {env.PURPOSE_KEY: env.PURPOSE + "-foreign", env.OWNER_KEY: NAME}}},
                   {"Config": {"Labels": {env.PURPOSE_KEY: env.PURPOSE, env.OWNER_KEY: "b03e_someone_else"}}},
                   {"Config": []}, {"Name": "/foreign"}, {"Id": "unknown"},
                   {"NetworkSettings": {"Networks": {"bridge": {}}}},
                   {"HostConfig": None}, {"HostConfig": []}, {"HostConfig": {}},
                   {"HostConfig": {"PortBindings": {"5432/tcp": [{"HostPort": "5432"}]}}})
        for change in changes:
            with self.subTest(change=change):
                self.commands.clear()
                self.resources[("container", NAME)] = {**valid, **change}
                self.assert_down_refused()

    def test_down_requires_exact_single_writable_owned_volume_mount(self):
        container = self.resources[("container", NAME)]
        valid = dict(container["Mounts"][0])
        bad_mounts = ([], [valid, valid], ["not a mount"],
                      [{**valid, "Type": "bind"}], [{**valid, "Name": "foreign_data"}],
                      [{**valid, "Destination": "/foreign"}], [{**valid, "RW": False}])
        for mounts in bad_mounts:
            with self.subTest(mounts=mounts):
                self.commands.clear()
                container["Mounts"] = mounts
                self.assert_down_refused()

    def test_down_checks_volume_purpose_and_owner_before_removing_container(self):
        valid = copy.deepcopy(self.resources[("volume", VOLUME)])
        for updates in ({"Name": "foreign"}, {"Labels": {}},
                        {"Labels": {env.PURPOSE_KEY: env.PURPOSE + "-foreign", env.OWNER_KEY: NAME}},
                        {"Labels": {env.PURPOSE_KEY: env.PURPOSE, env.OWNER_KEY: "b03e_someone_else"}}):
            with self.subTest(updates=updates):
                self.commands.clear()
                self.resources[("volume", VOLUME)] = {**valid, **updates}
                self.assert_down_refused()

    def test_down_refuses_a_volume_shared_with_another_container(self):
        self.users += "foreign_container\n"
        self.assert_down_refused()

    def test_down_refuses_a_foreign_or_non_internal_network(self):
        value = self.resources[("network", env.NETWORK)]
        for changes in ({"Internal": False}, {"Labels": {}},
                        {"Labels": {env.PURPOSE_KEY: env.PURPOSE + "-foreign"}}):
            with self.subTest(changes=changes):
                self.commands.clear()
                self.resources[("network", env.NETWORK)] = {**value, **changes}
                self.assert_down_refused()

    def test_up_refuses_a_volume_with_wrong_owner_before_creating_a_container(self):
        self.resources[("volume", VOLUME)]["Labels"][env.OWNER_KEY] = "b03e_someone_else"
        with self.assertRaises(SystemExit):
            self.call(env.up)
        self.assertFalse(any(args[0] in ("create", "cp", "start", "rm") for args in self.commands))

    def test_down_removes_only_the_inspected_container_id_and_its_volume(self):
        output = self.call(env.down)
        self.assertEqual(self.mutations(), [("rm", "-f", CID), ("volume", "rm", VOLUME)])
        self.assertIn(NAME, output)

    def test_invalid_names_stop_before_any_docker_command(self):
        for name in ("edu_ai_postgres", "b03e_", "b03e_x/foreign", "--name", "b03e_x\nforeign"):
            for action in (env.up, env.down):
                with self.subTest(name=name, action=action.__name__), self.assertRaises(SystemExit):
                    action(name)
                self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main()
