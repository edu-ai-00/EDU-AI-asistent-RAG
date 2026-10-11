"""Approved clean organization source and generated reference SQL, no database needed."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import seed_clean_organizations as seed


class OrganizationSeedTest(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(seed.DEFAULT_SOURCE.read_text(encoding="utf-8"))

    def reject(self, change):
        change(self.data)
        # Refresh only the checksum so each case tests the actual field guard.
        self.data["source_rows_sha256"] = seed.rows_hash(self.data["rows"])
        with self.assertRaises(ValueError):
            seed.render(self.data)

    def test_approved_source_and_hierarchy(self):
        seed.validate(self.data)
        self.assertEqual(self.data["source_sha"], "4a8aff1920fefd57f3aaf0a91647ca74fc3e7290")
        self.assertEqual(self.data["source_rows_sha256"], "a0b408e04c60fb8d8025f53b7a06b72bf6b9f5ea6e5a2974e44664289f00353a")
        rows = {r["legal_entity_code"]: r for r in self.data["rows"]}
        self.assertEqual(len(rows), 5)
        for code, level in (("ZS_KAVCI_HORY_ZS", "ZS"), ("ZS_KAVCI_HORY_SOS", "SS")):
            self.assertEqual(rows[code]["parent_code"], "ZS_KAVCI_HORY")
            self.assertEqual(rows[code]["kind"], "unit")
            self.assertEqual(rows[code]["level"], level)
            self.assertIsNone(rows[code]["grade_levels"])
        parent = rows["ZS_KAVCI_HORY"]
        self.assertEqual(parent["grade_levels"], {"ZS": [str(i) for i in range(1, 10)], "SS": [str(i) for i in range(10, 14)]})
        for code in ("SVOBODNA_SKOLA_PRAHA", "ZS_JEDOVNICE"):
            self.assertEqual(rows[code]["grade_levels"], {"ZS": [str(i) for i in range(1, 10)]})
        self.assertEqual(set(self.data["deactivate_codes"]), {"SKOLA_NOVA_VES", "SKOLA_NOVA_VES_ZS", "SKOLA_NOVA_VES_SOS", "ZAKLADNI_SKOLA_BOROVA", "ZAKLADNI_SKOLA_LIPOVA"})

    def test_unapproved_field_is_rejected(self):
        self.reject(lambda d: d["rows"][0].update(notes="not approved"))

    def test_duplicate_code_is_rejected(self):
        self.reject(lambda d: d["rows"].append(copy.deepcopy(d["rows"][0])))

    def test_missing_parent_is_rejected(self):
        self.reject(lambda d: d["rows"][-1].update(parent_code="MISSING"))

    def test_non_owner_parent_is_rejected(self):
        self.reject(lambda d: d["rows"][-1].update(parent_code="ZS_KAVCI_HORY_SOS"))

    def test_overlap_with_demo_is_rejected(self):
        self.reject(lambda d: d["deactivate_codes"].append(d["rows"][0]["legal_entity_code"]))

    def test_inactive_source_is_rejected(self):
        self.reject(lambda d: d["rows"][0].update(is_active=False))

    def test_invalid_grades_are_rejected(self):
        self.reject(lambda d: d["rows"][0].update(grade_levels={"ZS": [1]}))

    def test_checksum_drift_is_rejected(self):
        self.data["rows"][0]["display_name"] += " changed"
        with self.assertRaisesRegex(ValueError, "checksum"):
            seed.render(self.data)

    def test_timestamp_requires_utc(self):
        self.reject(lambda d: d.update(seed_created_at="2026-10-11T00:00:00"))

    def test_cli_matches_deterministic_utf8_render(self):
        sql = seed.render(self.data).encode("utf-8")
        for _ in range(2):
            out = subprocess.check_output([sys.executable, str(ROOT / "scripts/seed_clean_organizations.py")])
            self.assertEqual(out, sql)
        self.assertIn("Škola Kavčí hory".encode("utf-8"), sql)
        self.assertIn(b"2026-10-11T00:00:00+00:00", sql)
        self.assertIn(b"ON CONFLICT (legal_entity_code) DO NOTHING", sql)
        self.assertNotIn(b"DISABLE TRIGGER", sql)


if __name__ == "__main__":
    unittest.main()
