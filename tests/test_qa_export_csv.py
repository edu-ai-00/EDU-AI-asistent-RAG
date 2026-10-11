"""Unit tests for the /v1/qa-export byte format (search/app/qa_csv.py).

Stdlib-only on purpose: app.qa_csv has no DB and no FastAPI import, so this
file imports it directly without the search runtime (langchain, Postgres are
not installed on this host) — see CONTRACT.md 5.1 "Oddělovač" (v1.4.0).
"""
import csv
import importlib.util
import io
import json
import os
import unittest

# Loaded BY PATH, not via sys.path: search/app and qa_portal/backend/app are
# both packages called "app", so whichever is imported first in a shared
# pytest session wins and the other one's submodule is not found. Running this
# file together with qa_portal/tests/test_csv_cross_format.py used to fail
# exactly that way.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_QA_CSV_PATH = os.path.join(_REPO_ROOT, "search", "app", "qa_csv.py")
_spec = importlib.util.spec_from_file_location("search_app_qa_csv", _QA_CSV_PATH)
_qa_csv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_qa_csv)
HEADER = _qa_csv.HEADER
rows_to_csv_bytes = _qa_csv.rows_to_csv_bytes


def _sample_rows():
    # Row 1: a comma AND a semicolon in answer_md, to prove the comma is the
    # real delimiter and a literal ';' in a cell does not split anything.
    row1 = ("k1", "Q1, with a comma?", "Answer, with a comma; and a semicolon.",
            "cat1", "variant a\nvariant b", "Title | http://example.test/a",
            json.dumps(["provoz"]))
    # Row 2: a '"' quote and a multi-line answer.
    row2 = ("k2", 'Q2 with a "quoted" word?', "Line one\nLine two with \"quotes\"",
            "cat2", "", "", json.dumps([]))
    return [row1, row2]


class RowsToCsvBytesTest(unittest.TestCase):
    def test_bom_header_crlf_and_round_trip(self):
        data = rows_to_csv_bytes(_sample_rows())

        # BOM
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        text = data.decode("utf-8-sig")

        # comma-delimited header, legacy 8-column set
        first_line = text.split("\r\n", 1)[0]
        self.assertEqual(first_line, ",".join(HEADER))
        self.assertEqual(HEADER, ["entry_key", "canonical_q", "answer_md",
                                   "cat", "variants", "sources", "is_active",
                                   "domains"])

        # CRLF line endings
        self.assertIn("\r\n", text)
        self.assertNotIn("\r\r\n", text)

        # round trip: re-reading with a comma reader returns every field
        # byte-identical (minus the is_active column, which the writer
        # hardcodes to "TRUE")
        reader = csv.reader(io.StringIO(text), delimiter=",")
        rows = list(reader)
        self.assertEqual(rows[0], HEADER)

        expected = _sample_rows()
        for i, r in enumerate(expected):
            entry_key, canonical_q, answer_md, cat, variants, sources_cell, domains_json = r
            got = rows[i + 1]
            self.assertEqual(got, [entry_key, canonical_q, answer_md, cat,
                                    variants, sources_cell, "TRUE", domains_json])

    def test_old_semicolon_split_is_gone(self):
        """Failure case: the header line no longer splits on ';'."""
        data = rows_to_csv_bytes(_sample_rows())
        text = data.decode("utf-8-sig")
        first_line = text.split("\r\n", 1)[0]
        parts = first_line.split(";")
        self.assertEqual(len(parts), 1)


if __name__ == "__main__":
    unittest.main()
