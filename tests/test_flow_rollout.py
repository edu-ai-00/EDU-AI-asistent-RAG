"""flow_rollout: targeted A -> B rollout against a fake Langflow API (loopback, in memory, no Docker, no real .env).

Run: python3 tests/test_flow_rollout.py
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import flow_rollout as fr  # noqa: E402

KEY = "sentinel-api-key-never-print"


def node(node_id, node_type, code, **extra):
    template = {"code": {"value": code, "type": "code", "name": "code"}}
    template.update(extra.pop("template", {}))
    return {"id": node_id, "type": "genericNode", "position": {"x": 0, "y": 0},
            "data": {"id": node_id, "type": node_type, "node": {"template": template, "description": extra.get("description", "d"),
                                                                 "metadata": extra.get("metadata", {}), "display_name": node_id, "field_order": extra.get("field_order", [])}}}


def release(b):
    n1 = node("n1", "OfficeToMarkdown" if b else "HtmlToMarkdown", "class A: pass\n")
    n2 = node("n2", "APIRequest", "class R: pass\n", template={"timeout": {"value": 300 if b else 30, "type": "int", "name": "timeout"}})
    extra = {"legal_entity": {"value": "", "type": "str", "name": "legal_entity", "required": True}} if b else {}
    n3 = node("n3", "ExportQAPack", "class E2: pass\n" if b else "class E1: pass\n", template=extra,
              description="new" if b else "old", metadata={"code_hash": "2" if b else "1"},
              field_order=["code", "legal_entity"] if b else ["code"])
    n4 = node("n4", "ChatOutput", "class C: pass\n")
    return {"name": "F", "endpoint_name": "alias-f", "data": {"nodes": [n1, n2, n3, n4], "edges": [{"id": "e1", "source": "n1", "target": "n2"}]}}


class FakeLangflow:
    def __init__(self):
        self.flows = {}
        self.patches = []
        self.seen_keys = []
        self.reads = {}
        self.after_read = None
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, obj, code=200):
                payload = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                outer.seen_keys.append(self.headers.get("x-api-key"))
                if self.path.startswith("/api/v1/flows/?"):
                    return self._send([{"id": i, "name": f["name"], "endpoint_name": f.get("endpoint_name")} for i, f in outer.flows.items()])
                fid = self.path.rsplit("/", 1)[-1]
                snapshot = copy.deepcopy(outer.flows[fid])
                outer.reads[fid] = outer.reads.get(fid, 0) + 1
                if outer.after_read:
                    outer.after_read(fid, outer.reads[fid])      # a concurrent editor saves right after this read
                self._send(snapshot)

            def do_PATCH(self):
                fid = self.path.rsplit("/", 1)[-1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.patches.append(fid)
                outer.flows[fid]["data"] = body["data"]
                self._send({"id": fid})

            def log_message(self, *a):
                pass
        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Rollout(unittest.TestCase):
    def setUp(self):
        self.api = FakeLangflow()
        self.addCleanup(self.api.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = pathlib.Path(self.tmp.name, "a"), pathlib.Path(self.tmp.name, "b")
        for d, flag in ((self.a, False), (self.b, True)):
            d.mkdir()
            (d / "F.json").write_text(json.dumps(release(flag)), encoding="utf-8")
        self.api.flows["id-f"] = {"id": "id-f", "name": "F", "endpoint_name": "alias-f", "data": copy.deepcopy(release(False)["data"])}
        self.api.flows["id-draft"] = {"id": "id-draft", "name": "USER DRAFT", "endpoint_name": None, "data": {"nodes": [node("d1", "HtmlToMarkdown", "x = 1\n")], "edges": []}}
        self.draft_before = copy.deepcopy(self.api.flows["id-draft"])

    def run_tool(self, mode="--apply", window=True):
        out = io.StringIO()
        extra = ["--confirm-maintenance-window"] if mode == "--apply" and window else []
        with mock.patch.dict(os.environ, {"LANGFLOW_URL": self.api.url, "LANGFLOW_API_KEY": KEY}), contextlib.redirect_stdout(out):
            code = fr.main(["--from-dir", str(self.a), "--to-dir", str(self.b), mode, *extra])
        text = out.getvalue()
        self.assertNotIn(KEY, text)
        return code, text

    def live(self, node_id):
        return {n["id"]: n for n in self.api.flows["id-f"]["data"]["nodes"]}[node_id]

    def test_apply_without_the_maintenance_window_reads_and_writes_nothing(self):
        code, text = self.run_tool(window=False)
        self.assertEqual((code, self.api.patches, self.api.seen_keys), (1, [], []))
        self.assertIn("--confirm-maintenance-window", text)

    def test_a_flow_saved_by_someone_else_during_the_rollout_is_refused_and_not_overwritten(self):
        def editor(fid, nth):
            if fid == "id-f" and nth == 1:                      # saves right after the rollout read the flow
                self.api.flows[fid]["data"]["nodes"].append(node("n5", "ChatOutput", "class Mine: pass\n"))
        self.api.after_read = editor
        code, text = self.run_tool()
        self.assertEqual((code, self.api.patches), (1, []))
        self.assertIn("changed while the rollout was planned", text)
        self.assertEqual([n["id"] for n in self.api.flows["id-f"]["data"]["nodes"]][-1], "n5")     # the other editor's work is intact

    def test_a_changed_updated_at_alone_is_also_refused(self):
        self.api.flows["id-f"]["updated_at"] = "t0"
        def editor(fid, nth):
            if fid == "id-f" and nth == 1:
                self.api.flows[fid]["updated_at"] = "t1"
        self.api.after_read = editor
        self.assertEqual((self.run_tool()[0], self.api.patches), (1, []))

    def test_check_writes_nothing(self):
        code, text = self.run_tool("--check")
        self.assertEqual((code, self.api.patches), (0, []))
        self.assertIn("WOULD_PATCH 3 node(s)", text)

    def test_apply_patches_exactly_the_differing_keys_and_nothing_else(self):
        before = copy.deepcopy(self.api.flows["id-f"]["data"])
        code, text = self.run_tool()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.live("n1")["data"]["type"], "OfficeToMarkdown")
        self.assertEqual(self.live("n2")["data"]["node"]["template"]["timeout"]["value"], 300)
        n3 = self.live("n3")["data"]
        self.assertEqual((n3["node"]["template"]["code"]["value"], n3["node"]["description"], n3["node"]["metadata"]),
                         ("class E2: pass\n", "new", {"code_hash": "2"}))
        self.assertIn("legal_entity", n3["node"]["template"])
        self.assertEqual(n3["node"]["field_order"], ["code", "legal_entity"])
        after = self.api.flows["id-f"]["data"]
        self.assertEqual({n["id"]: n for n in before["nodes"]}["n4"], {n["id"]: n for n in after["nodes"]}["n4"])
        self.assertEqual(before["edges"], after["edges"])
        self.assertEqual(self.api.flows["id-draft"], self.draft_before)           # drafts are never written
        self.assertEqual(self.api.patches, ["id-f"])

    def test_second_run_changes_nothing(self):
        self.run_tool()
        self.api.patches.clear()
        code, text = self.run_tool()
        self.assertEqual((code, self.api.patches), (0, []))
        self.assertIn("ALREADY_B", text)

    def test_user_field_order_refuses_the_whole_flow_without_writes(self):
        self.live("n3")["data"]["node"]["field_order"] = ["legal_entity", "code"]
        before = copy.deepcopy(self.api.flows)
        code, text = self.run_tool()
        self.assertEqual((code, self.api.patches), (1, []))
        self.assertIn("GUARD_FAIL node.field_order", text)
        self.assertEqual(self.api.flows, before)

    def test_field_order_already_at_b_does_not_block_other_exporter_changes(self):
        self.live("n3")["data"]["node"]["field_order"] = ["code", "legal_entity"]
        code, text = self.run_tool()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.live("n3")["data"]["node"]["field_order"], ["code", "legal_entity"])
        self.assertEqual(self.live("n3")["data"]["node"]["template"]["code"]["value"], "class E2: pass\n")

    def test_real_exporter_input_and_field_order_are_planned_and_applied_together(self):
        path = ROOT / "langflow_flows" / "ingestion" / "EDU_AI_EXPORT_QA_PACK.json"
        b_flow = json.loads(path.read_text(encoding="utf-8"))
        a_flow = copy.deepcopy(b_flow)
        a_node = next(n for n in a_flow["data"]["nodes"] if n["id"] == "ExportQAPack-qa010")
        b_node = next(n for n in b_flow["data"]["nodes"] if n["id"] == "ExportQAPack-qa010")
        a_node["data"]["node"]["template"].pop("legal_entity")
        a_node["data"]["node"]["field_order"].remove("legal_entity")
        keys = ["template.legal_entity", "node.field_order"]
        self.assertEqual(fr.plan_for(a_flow, b_flow), [("ExportQAPack-qa010", keys)])
        live = copy.deepcopy(a_node)
        self.assertEqual(fr.node_state(live, a_node, b_node, keys), ("APPLY", keys))
        for key in keys:
            fr.apply_key(live, key, a_node, b_node)
        self.assertEqual(live, b_node)
        self.assertEqual(fr.node_state(live, a_node, b_node, keys), ("ALREADY_B", []))

    def test_user_edit_of_a_targeted_code_refuses_the_whole_flow(self):
        self.live("n3")["data"]["node"]["template"]["code"]["value"] += "# user edit\n"
        snapshot = copy.deepcopy(self.api.flows["id-f"])
        code, text = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("n3: GUARD_FAIL template.code", text)
        self.assertIn("REFUSED, flow left untouched", text)
        self.assertEqual((self.api.flows["id-f"], self.api.patches), (snapshot, []))

    def test_unknown_drift_of_a_setting_is_refused_but_a_user_value_equal_to_b_is_accepted(self):
        self.live("n2")["data"]["node"]["template"]["timeout"]["value"] = 60
        code, text = self.run_tool()
        self.assertEqual((code, self.api.patches), (1, []))
        self.live("n2")["data"]["node"]["template"]["timeout"]["value"] = 300           # the user already chose B's value
        code, text = self.run_tool()
        self.assertEqual(code, 0, text)
        self.assertIn("n2: ALREADY_B", text)

    def test_type_drift_missing_node_and_missing_flow(self):
        self.live("n1")["data"]["type"] = "SomethingElse"
        self.assertEqual(self.run_tool()[0], 1)
        self.live("n1")["data"]["type"] = "HtmlToMarkdown"
        del self.api.flows["id-f"]["data"]["nodes"][3]
        self.api.flows["id-f"]["data"]["nodes"] = [n for n in self.api.flows["id-f"]["data"]["nodes"] if n["id"] != "n3"]
        self.assertIn("node missing", self.run_tool()[1])
        del self.api.flows["id-f"]
        code, text = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("not installed", text)

    def test_canonical_files_that_change_more_than_this_tool_patches_are_refused(self):
        changed = json.loads((self.b / "F.json").read_text())
        changed["data"]["edges"].append({"id": "e2", "source": "n2", "target": "n3"})
        (self.b / "F.json").write_text(json.dumps(changed))
        with self.assertRaises(SystemExit):
            self.run_tool()
        changed = release(True)
        changed["data"]["nodes"][3]["data"]["node"]["display_name"] = "Renamed"
        (self.b / "F.json").write_text(json.dumps(changed))
        with self.assertRaises(SystemExit):
            self.run_tool()

    def test_unreachable_langflow_is_unverified_and_the_key_stays_secret(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LANGFLOW_URL": "http://127.0.0.1:9", "LANGFLOW_API_KEY": KEY}), contextlib.redirect_stdout(out):
            code = fr.main(["--from-dir", str(self.a), "--to-dir", str(self.b), "--apply", "--confirm-maintenance-window"])
        self.assertEqual(code, 3)
        self.assertNotIn(KEY, out.getvalue())

    def test_the_real_ingestion_flows_are_a_valid_b_input(self):
        """Canonical flows are not edited: a synthetic older timeout tests node-level planning."""
        real = ROOT / "langflow_flows" / "ingestion"
        a_dir = pathlib.Path(self.tmp.name, "real_a")
        a_dir.mkdir()
        planned = {}
        for p in sorted(real.glob("*.json")):
            flow = json.loads(p.read_text(encoding="utf-8"))
            a_flow = copy.deepcopy(flow)
            if p.name in ("EDU_AI_LOAD_OFFICE.json", "EDU_AI_LOAD_WEB_PAGE.json"):
                for n in a_flow["data"]["nodes"]:
                    if n["data"]["type"] == "APIRequest":
                        # Pick a known synthetic source setting distinct from the retained
                        # canonical value. This test does not impose C2's 300s flow change.
                        current = n["data"]["node"]["template"]["timeout"]["value"]
                        n["data"]["node"]["template"]["timeout"]["value"] = 60 if current == 30 else 30
            (a_dir / p.name).write_text(json.dumps(a_flow), encoding="utf-8")
            planned[p.name] = fr.plan_for(a_flow, flow)
        self.assertEqual({k: [(i, k2) for i, k2 in v] for k, v in planned.items() if v},
                         {"EDU_AI_LOAD_OFFICE.json": [("APIRequest-aCOJ9-OFF", ["template.timeout"])],
                          "EDU_AI_LOAD_WEB_PAGE.json": [("APIRequest-aCOJ9-WEB", ["template.timeout"])]})

    def test_user_code_on_timeout_target_refuses_whole_flow(self):
        self.live("n2")["data"]["node"]["template"]["code"]["value"] += "# user executable edit\n"
        before = copy.deepcopy(self.api.flows)
        code, text = self.run_tool()
        self.assertEqual((code, self.api.patches), (1, []))
        self.assertEqual(self.api.flows, before)

    def test_user_type_on_timeout_target_refuses_whole_flow(self):
        self.live("n2")["data"]["type"] = "UserCustomType"
        before = copy.deepcopy(self.api.flows)
        code, text = self.run_tool()
        self.assertEqual((code, self.api.patches), (1, []))
        self.assertEqual(self.api.flows, before)

    def test_duplicate_alias_refuses_before_any_write(self):
        self.api.flows["duplicate"] = copy.deepcopy(self.api.flows["id-f"])
        self.api.flows["duplicate"]["id"] = "duplicate"
        before = copy.deepcopy(self.api.flows)
        code, text = self.run_tool()
        self.assertEqual((code, self.api.patches), (1, []))
        self.assertEqual(self.api.flows, before)


if __name__ == "__main__":
    unittest.main()
