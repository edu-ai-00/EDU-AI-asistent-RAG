"""Bootstrap flow repeat/resume against a synthetic Langflow, without HTTP/Docker."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap  # noqa: E402
import envfile  # noqa: E402

BASE, KEY = "http://synthetic-langflow.invalid", "synthetic-langflow-key"


class FlowPreservationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("import_flows_under_test", ROOT / "scripts/import_flows.py")
        self.importer = importlib.util.module_from_spec(spec)
        # Importing this script normally reads .env. This test never reads one.
        with mock.patch.object(envfile, "read_env", return_value={}), \
                mock.patch.dict(os.environ, {"LANGFLOW_URL": BASE, "LANGFLOW_API_KEY": KEY}):
            spec.loader.exec_module(self.importer)
        self.vendor = [json.loads((self.importer.ROOT / rel).read_text(encoding="utf-8"))
                       for rel in self.importer.FLOW_FILES]
        self.flows, self.writes = [], []
        self.paginated = False
        self.fail_create_after = None
        self.created = 0
        self.read_error = False
        saved_failed = bootstrap._failed
        self.addCleanup(setattr, bootstrap, "_failed", saved_failed)
        bootstrap._failed = False

    def install_existing(self, count=None):
        self.flows = [dict(copy.deepcopy(flow), id=f"existing-{i}",
                           data={"nodes": [{"id": "user-edited-node", "secret": "synthetic-owned-graph"}]},
                           updated_at="preserve-this-timestamp", folder_id="user-folder")
                      for i, flow in enumerate(self.vendor[:count])]
        self.flows += [
            {"id": "custom", "endpoint_name": "my-custom-flow", "data": {"nodes": ["custom-node"]}},
            {"id": "draft", "endpoint_name": None, "data": {"nodes": ["draft-node"]}},
        ]

    def snapshot(self):
        return json.dumps(self.flows, sort_keys=True, separators=(",", ":")).encode()

    def call(self, method, path, body=None):
        if method == "GET":
            self.assertTrue(path.startswith("/api/v1/flows/?page="))
            if self.read_error:
                raise SystemExit("synthetic inventory unavailable")
            if self.paginated:
                page = int(path.split("page=", 1)[1].split("&", 1)[0])
                return {"items": copy.deepcopy(self.flows[(page - 1) * 2:page * 2]),
                        "pages": max(1, (len(self.flows) + 1) // 2)}
            return copy.deepcopy(self.flows)
        if method == "POST":
            self.assertEqual(path, "/api/v1/flows/")
            if self.fail_create_after is not None and self.created >= self.fail_create_after:
                raise SystemExit("synthetic interrupted creation")
            self.created += 1
            flow = dict(copy.deepcopy(body), id=f"created-{self.created}")
            self.flows.append(flow)
            self.writes.append((method, path))
            return copy.deepcopy(flow)
        if method == "PATCH":
            flow = next(flow for flow in self.flows if path == f"/api/v1/flows/{flow['id']}")
            flow.update(copy.deepcopy(body))
            self.writes.append((method, path))
            return copy.deepcopy(flow)
        self.fail(f"unexpected mutation {method} {path}")

    def import_directly(self, *args):
        with mock.patch.object(self.importer, "call", side_effect=self.call), \
                mock.patch.object(sys, "argv", ["scripts/import_flows.py", *args]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.importer.main()

    def run_bootstrap_flows(self):
        def child(command, *, env):
            self.assertEqual(env["LANGFLOW_URL"], BASE)
            self.assertEqual(env["LANGFLOW_API_KEY"], KEY)
            try:
                self.import_directly(*command[2:])
            except SystemExit as exc:
                return subprocess.CompletedProcess(command, 1, "", str(exc))
            return subprocess.CompletedProcess(command, 0, "", "")
        output = io.StringIO()
        with mock.patch.object(bootstrap, "run", side_effect=child), \
                mock.patch.object(bootstrap, "read_env", return_value={}), \
                contextlib.redirect_stdout(output):
            bootstrap.step_flows(BASE, KEY)
        return output.getvalue()

    def test_repeated_bootstrap_preserves_all_existing_aliases_custom_graphs_and_drafts(self):
        self.install_existing()
        before = self.snapshot()
        self.run_bootstrap_flows()
        self.assertFalse(bootstrap._failed)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.snapshot(), before)

    def test_first_bootstrap_creates_all_aliases_then_repeats_without_writes(self):
        self.run_bootstrap_flows()
        self.assertFalse(bootstrap._failed)
        self.assertEqual(len(self.writes), len(self.vendor))
        self.assertEqual({flow["endpoint_name"] for flow in self.flows},
                         {flow["endpoint_name"] for flow in self.vendor})
        for original, actual in zip(self.vendor, self.flows):
            self.assertEqual(actual["data"], original["data"])
        before = self.snapshot()
        self.writes.clear()
        self.run_bootstrap_flows()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.snapshot(), before)

    def test_partial_bootstrap_only_fills_missing_aliases(self):
        self.install_existing(3)
        before = copy.deepcopy(self.flows)
        self.run_bootstrap_flows()
        self.assertFalse(bootstrap._failed)
        self.assertEqual(self.writes, [("POST", "/api/v1/flows/")] * 3)
        self.assertEqual(self.flows[:len(before)], before)
        self.assertEqual({flow["endpoint_name"] for flow in self.flows if flow.get("endpoint_name")}
                         - {"my-custom-flow"}, {flow["endpoint_name"] for flow in self.vendor})

    def test_interrupted_first_import_can_resume_without_overwriting_a_later_user_edit(self):
        self.fail_create_after = 1
        self.run_bootstrap_flows()
        self.assertTrue(bootstrap._failed)
        self.assertEqual(len(self.flows), 1)
        self.flows[0]["data"] = {"nodes": ["user-edited-after-interruption"]}
        preserved = copy.deepcopy(self.flows[0])
        self.fail_create_after = None
        bootstrap._failed = False
        self.writes.clear()
        self.run_bootstrap_flows()
        self.assertFalse(bootstrap._failed)
        self.assertEqual(self.flows[0], preserved)
        self.assertEqual(self.writes, [("POST", "/api/v1/flows/")] * 5)

    def test_paginated_inventory_preserves_aliases_on_every_page(self):
        self.install_existing()
        self.paginated = True
        before = self.snapshot()
        self.run_bootstrap_flows()
        self.assertFalse(bootstrap._failed)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_existing_aliases_fail_before_creating_any_missing_flow(self):
        self.install_existing(3)
        self.flows.append(dict(copy.deepcopy(self.flows[0]), id="duplicate-existing-alias"))
        before = self.snapshot()
        self.run_bootstrap_flows()
        self.assertTrue(bootstrap._failed)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.snapshot(), before)

    def test_unavailable_inventory_fails_without_mutations(self):
        self.install_existing()
        self.read_error = True
        before = self.snapshot()
        self.run_bootstrap_flows()
        self.assertTrue(bootstrap._failed)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.snapshot(), before)

    def test_unrecognized_inventory_is_not_treated_as_a_fresh_install(self):
        with mock.patch.object(self.importer, "call", return_value={"detail": "unexpected inventory"}) as call, \
                mock.patch.object(sys, "argv", ["scripts/import_flows.py", "--missing-only"]), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.importer.main()
        self.assertTrue(all(args.args[0] == "GET" for args in call.call_args_list))

    def test_inventory_without_page_count_is_not_assumed_complete(self):
        self.install_existing(3)
        before = self.snapshot()

        def incomplete_inventory(method, path, body=None):
            if method == "GET":
                return {"items": [copy.deepcopy(self.flows[0])]}
            return self.call(method, path, body)

        with mock.patch.object(self.importer, "call", side_effect=incomplete_inventory), \
                mock.patch.object(sys, "argv", ["scripts/import_flows.py", "--missing-only"]), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.importer.main()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.snapshot(), before)

    def test_failed_bootstrap_recommends_the_same_preserving_import_command(self):
        self.install_existing()
        self.read_error = True
        output = self.run_bootstrap_flows()
        self.assertTrue(bootstrap._failed)
        self.assertIn("run `python3 scripts/import_flows.py --missing-only`", output)
        self.assertNotIn("run `python3 scripts/import_flows.py`", output)

    def test_malformed_inventory_items_or_pagination_fail_before_writes(self):
        cases = (["not a flow"], [{"endpoint_name": "alias-without-id"}],
                 {"items": [], "pages": "2"}, {"items": [], "pages": 2})
        for response in cases:
            with self.subTest(response=response), \
                    mock.patch.object(self.importer, "call", return_value=response) as call, \
                    mock.patch.object(sys, "argv", ["scripts/import_flows.py", "--missing-only"]), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                self.importer.main()
            self.assertTrue(all(args.args[0] == "GET" for args in call.call_args_list))

    def test_standalone_recovery_keeps_its_explicit_update_behavior(self):
        self.install_existing()
        self.import_directly()
        self.assertEqual([method for method, _ in self.writes], ["PATCH"] * len(self.vendor))
        for original, actual in zip(self.vendor, self.flows):
            self.assertEqual(actual["data"], original["data"])
        self.assertEqual(self.flows[-1]["id"], "draft")


if __name__ == "__main__":
    unittest.main()
