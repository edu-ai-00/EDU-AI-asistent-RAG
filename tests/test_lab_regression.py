"""Unit tests for the regression set logic (tools/lab/regression/scoring.py, build_cases.py).

Stdlib-only. run.py needs the search image and the database and is not covered
here. Modules are loaded BY PATH (see test_lab_variants.py).
"""
import importlib.util
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "lab_regression_" + name, os.path.join(_ROOT, "tools", "lab", "regression", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = _load("scoring")
bc = _load("build_cases")
mn = _load("make_negatives")
xw = _load("xlsx_write")
bi = _load("build_index")
_rspec = importlib.util.spec_from_file_location(
    "lab_neutral_xlsx_read", os.path.join(_ROOT, "tools", "lab", "neutral_answers", "xlsx_read.py"))
xr = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(xr)


class Verdict(unittest.TestCase):
    def test_confident_right_and_wrong(self):
        self.assertEqual(sc.verdict("a", {"decision": "exact"}, "a"), "přesná shoda")
        self.assertEqual(sc.verdict("a", {"decision": "A"}, "a"), "odpověď")
        self.assertEqual(sc.verdict("a", {"decision": "A"}, "b"), "ŠPATNÁ odpověď")

    def test_offer_and_nothing(self):
        self.assertEqual(sc.verdict("a", {"decision": "B1"}, "b", {"a"}), "v nabídce")
        self.assertEqual(sc.verdict("a", {"decision": "B1"}, "b", {"c"}), "nabídka bez správné")
        self.assertEqual(sc.verdict("a", {"decision": "C"}, None, {"a"}), "nic (C) – v záloze")
        self.assertEqual(sc.verdict("a", {"decision": "C"}, None, set()), "nic (C)")

    def test_expect_none_means_no_confident_answer(self):
        self.assertEqual(sc.verdict(None, {"decision": "B1"}, "x", {"x"}), "správně bez jisté odpovědi")
        self.assertEqual(sc.verdict(None, {"decision": "C"}, None), "správně bez jisté odpovědi")
        self.assertEqual(sc.verdict(None, {"decision": "exact"}, "x"), "ŠPATNÁ odpověď")


class Score(unittest.TestCase):
    def test_confident_wrong_outweighs_right(self):
        self.assertEqual(sc.score_of(["odpověď", "odpověď", "odpověď", "ŠPATNÁ odpověď"]), 0.0)
        self.assertEqual(sc.score_of(["v nabídce", "nic (C)"]), 0.5)


def _cases():
    return [{"id": str(i), "set": "A" if i < 3 else "B", "query": f"q{i}", "category": "x"} for i in range(5)]


class Compare(unittest.TestCase):
    def test_regressions_improvements_and_new_wrong(self):
        base = {"0": {"verdict": "odpověď"}, "1": {"verdict": "v nabídce"},
                "2": {"verdict": "nic (C)"}, "3": {"verdict": "odpověď"}, "4": {"verdict": "odpověď"}}
        now = {"0": {"verdict": "ŠPATNÁ odpověď"}, "1": {"verdict": "odpověď"},
               "2": {"verdict": "nic (C)"}, "3": {"verdict": "v nabídce"}, "4": {"verdict": "odpověď"}}
        d = sc.compare(_cases(), base, now)
        self.assertEqual([r["id"] for r in d["regressions"]], ["0", "3"])   # worst first
        self.assertEqual([r["id"] for r in d["improvements"]], ["1"])
        self.assertEqual([r["id"] for r in d["new_confident_wrong"]], ["0"])

    def test_cases_on_one_side_only_are_not_compared(self):
        d = sc.compare(_cases(), {"0": {"verdict": "odpověď"}}, {"1": {"verdict": "odpověď"}})
        self.assertEqual((d["only_before"], d["only_now"]), (["0"], ["1"]))
        self.assertEqual(d["regressions"], [])

    def test_summary_per_set(self):
        res = {"0": {"verdict": "odpověď"}, "3": {"verdict": "ŠPATNÁ odpověď"}, "4": {"verdict": "v nabídce"}}
        s = sc.summarize(_cases(), res)
        self.assertEqual((s["A"]["cases"], s["A"]["confident_right"]), (1, 1))
        self.assertEqual((s["B"]["cases"], s["B"]["confident_wrong"], s["B"]["offered"]), (2, 1, 1))
        self.assertEqual(s["ALL"]["score"], round(1 - 3 + 0.5, 1))


class BuildCases(unittest.TestCase):
    Q = [{"idx": 0, "pack": "p", "entry_key": "kdy-zacina", "q": "Kdy začíná škola?", "alts": []},
         {"idx": 1, "pack": "p", "entry_key": "", "q": "Kde je šatna?", "alts": []}]

    def test_sets_extra_and_dedup(self):
        sets = {"A": [{"idx": 0, "category": "tvary", "text": "kdy zacina skola"},
                      {"idx": 0, "category": "tvary", "text": "kdy zacina skola"},
                      {"idx": 1, "category": "kratke", "text": "satna"},
                      {"idx": 9, "category": "kratke", "text": "unknown question"}]}
        extra = [{"set": "kurator", "pack": "p", "entry_key": None, "query": "ahoj"}]
        cases = bc.build(self.Q, sets, extra)
        self.assertEqual(len(cases), 3)
        self.assertEqual(cases[1]["entry_key"], "k1")            # no key → stable fallback
        self.assertIsNone(cases[2]["entry_key"])
        self.assertEqual(len({c["id"] for c in cases}), 3)

    def test_id_is_stable_and_depends_on_the_expectation(self):
        a = bc.case_id("A", "p", "k", "dotaz")
        self.assertEqual(a, bc.case_id("A", "p", "k", "dotaz"))
        self.assertNotEqual(a, bc.case_id("A", "p", None, "dotaz"))


class AcceptAndSplit(unittest.TestCase):
    def test_equivalent_answer_is_not_a_wrong_answer(self):
        self.assertEqual(sc.verdict("a", {"decision": "A"}, "b", accept=("b",)), "odpověď")
        self.assertEqual(sc.verdict("a", {"decision": "A"}, "c", accept=("b",)), "ŠPATNÁ odpověď")
        self.assertEqual(sc.verdict("a", {"decision": "B1"}, "x", {"b"}, accept=("b",)), "v nabídce")

    def test_split_is_deterministic_and_close_to_the_share(self):
        keys = [f"entry-{i}" for i in range(2000)]
        first = [bc.split_of(k) for k in keys]
        self.assertEqual(first, [bc.split_of(k) for k in keys])
        share = first.count("holdout") / len(first)
        self.assertAlmostEqual(share, bc.HOLDOUT_SHARE / 100, delta=0.05)

    def test_cases_carry_split_accept_and_negatives(self):
        q = [{"idx": 0, "pack": "p", "entry_key": "a", "q": "Q?", "alts": []},
             {"idx": 1, "pack": "p", "entry_key": "b", "q": "R?", "alts": []}]
        cases = bc.build(q, {"A": [{"idx": 0, "text": "x"}]}, negatives=[{"set": "N", "text": "mimo obsah"}],
                         equivalents=[["a", "b"]])
        by_set = {c["set"]: c for c in cases}
        self.assertEqual(by_set["A"]["accept"], ["b"])
        self.assertIsNone(by_set["N"]["entry_key"])
        self.assertIn(by_set["A"]["split"], ("train", "holdout"))
        self.assertEqual(by_set["A"]["split"], bc.split_of("a"))   # the split is the entry's

    def test_summary_by_split(self):
        cases = [{"id": "1", "set": "A", "split": "train"}, {"id": "2", "set": "A", "split": "holdout"}]
        res = {"1": {"verdict": "odpověď"}, "2": {"verdict": "ŠPATNÁ odpověď"}}
        s = sc.summarize(cases, res, by_split=True)
        self.assertEqual((s["A:train"]["score"], s["A:holdout"]["score"], s["A"]["score"], s["ALL"]["cases"]),
                         (1.0, -3.0, -2.0, 2))


class Bootstrap(unittest.TestCase):
    def test_clear_gain_is_significant_and_noise_is_not(self):
        cases = [{"id": str(i)} for i in range(200)]
        base = {c["id"]: {"verdict": "nic (C)"} for c in cases}
        gain = {c["id"]: {"verdict": "odpověď" if int(c["id"]) < 60 else "nic (C)"} for c in cases}
        b = sc.bootstrap_delta(cases, base, gain)
        self.assertTrue(b["significant"]) and self.assertEqual(b["delta"], 60.0)
        flip = {c["id"]: {"verdict": "odpověď" if int(c["id"]) % 2 else "ŠPATNÁ odpověď"} for c in cases}
        mixed = {c["id"]: {"verdict": "ŠPATNÁ odpověď" if int(c["id"]) % 2 else "odpověď"} for c in cases}
        swap = sc.bootstrap_delta(cases, flip, mixed)      # every case flips: +4 and −4 cancel out
        self.assertEqual(swap["delta"], 0.0)
        self.assertFalse(swap["significant"])

    def test_no_common_cases(self):
        self.assertEqual(sc.bootstrap_delta([{"id": "1"}], {}, {"1": {"verdict": "odpověď"}})["n"], 0)


class WorksheetsAndNegatives(unittest.TestCase):
    Q = [{"idx": 0, "pack": "p", "entry_key": "kdy-zacina-skola", "q": "Kdy začíná škola?", "alts": []}]

    def test_d_worksheet_uses_the_opaque_id_map(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as fh:
            fh.write("id;odpoved;otazka_1;otazka_2\nD001;Prvního září.;kdy jdem do školy;\n"
                     "BEZ;(x);kolik stojí lyžák;\nD002;neznámé;nic;\n")
        out = bc.read_d_csv(fh.name, self.Q, {"D001": "kdy-zacina-skola"})
        os.unlink(fh.name)
        # D001 maps to the entry, BEZ has no answer, D002 is not in the map and is dropped
        self.assertEqual([(c["entry_key"], c["query"]) for c in out],
                         [("kdy-zacina-skola", "kdy jdem do školy"), (None, "kolik stojí lyžák")])

    def test_equivalent_pairs_are_merged_into_groups(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as fh:
            fh.write("klic_A;klic_B;shodne (ano/ne)\na;b;ano\nb;c;ano\nx;y;ne\nm;n;\n")
        groups = bc.read_equiv_csv(fh.name)
        os.unlink(fh.name)
        self.assertEqual(groups, [["a", "b", "c"]])

    def test_negatives_are_cleaned_deduplicated_and_sampled_per_source(self):
        rows = {"Mat": [["Kolik má krychle hran a vrcholů?"], ["Kolik má krychle hran a vrcholů?"],
                        ["12 + 7 = 19"], ["https://example.org/x odkaz na úlohu"], ["a"], ["Co je to průměr čísel?"]],
                "ČJ": [["Příbuzné slovo od dřevěný"]], "Učitelé": [["", "", "Jak hodnotit děti bez testů?"]]}
        out = mn.collect(rows, per_source=5, seed=1)
        self.assertEqual(sorted(n["text"] for n in out if n["category"] == "matematika"),
                         ["Co je to průměr čísel?", "Kolik má krychle hran a vrcholů?"])
        self.assertEqual([n["category"] for n in out if n["category"] != "matematika"], ["cestina", "ucitele"])
        self.assertTrue(all(n["set"] == "N" for n in out))


class Fixes(unittest.TestCase):
    def test_drop_and_relabel_wrong_labels(self):
        q = [{"idx": 0, "pack": "p1", "entry_key": "a", "q": "A?", "alts": []},
             {"idx": 1, "pack": "p2", "entry_key": "b", "q": "B?", "alts": []}]
        cases = bc.build(q, {}, negatives=[{"set": "N", "text": "x?"}, {"set": "N", "text": "y?"}])
        fixed = bc.apply_fixes(cases, {"drop": [{"set": "N", "query": "x?"}],
                                       "relabel": [{"set": "N", "query": "y?", "entry_key": "b"}]}, q)
        self.assertEqual([(c["query"], c["entry_key"], c["pack"]) for c in fixed], [("y?", "b", "p2")])
        self.assertEqual(fixed[0]["split"], bc.split_of("b"))
        self.assertEqual(fixed[0]["id"], bc.case_id("N", "p2", "b", "y?"))


class BuildIndex(unittest.TestCase):
    CSV = ("entry_key,canonical_q,answer_md,cat,variants,sources,is_active\n"
           'kdy-zacina,Kdy začíná škola?,Prvního září.,Provoz,"Kdy se jde do školy?\nZačátek školního roku",,TRUE\n'
           "vypnuta,Vypnutá otázka?,x,Provoz,,,FALSE\n"
           "kde-je-satna,Kde je šatna?,V přízemí.,Provoz,,,TRUE\n")

    def _dir(self, files):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        for n, t in files.items():
            with open(os.path.join(d, n), "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(t)
        return d

    def test_pack_is_the_file_name_variants_split_and_inactive_skipped(self):
        idx, stat = bi.build(self._dir({"faq-x.csv": self.CSV}))
        self.assertEqual([(e["pack"], e["entry_key"]) for e in idx], [("faq-x", "kdy-zacina"), ("faq-x", "kde-je-satna")])
        self.assertEqual(idx[0]["alts"], ["Kdy se jde do školy?", "Začátek školního roku"])
        self.assertEqual(len(stat["new"]), 2)

    def test_like_keeps_old_idx_and_appends_new_entries(self):
        old = [{"idx": 7, "pack": "faq-x", "entry_key": "kdy-zacina", "q": "Kdy začíná škola?",
                "alts": ["Kdy se jde do školy?", "Začátek školního roku"]},
               {"idx": 8, "pack": "faq-x", "entry_key": "mizi", "q": "Zmizí?", "alts": []}]
        idx, stat = bi.build(self._dir({"faq-x.csv": self.CSV}), old)
        self.assertEqual({(e["entry_key"]): e["idx"] for e in idx}, {"kdy-zacina": 7, "kde-je-satna": 9})
        self.assertEqual([k[1] for k in stat["removed"]], ["mizi"])
        self.assertEqual([k[1] for k in stat["new"]], ["kde-je-satna"])
        self.assertEqual(stat["changed"], [])

    def test_changed_wording_and_key_collision_between_packs(self):
        old = [{"idx": 0, "pack": "faq-x", "entry_key": "kdy-zacina", "q": "Jiné znění?", "alts": []}]
        idx, stat = bi.build(self._dir({"faq-x.csv": self.CSV, "faq-y.csv": self.CSV}), old)
        self.assertEqual([k[1] for k in stat["changed"]], ["kdy-zacina"])
        self.assertIn("kdy-zacina", stat["collisions"])


class CsvEncodings(unittest.TestCase):
    def _write(self, text, enc):
        import tempfile
        f = tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False)
        f.write(text.encode(enc)); f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_windows_1250_csv_and_header_suffix(self):
        q = [{"idx": 0, "pack": "p", "entry_key": "k1", "q": "Kdy začíná škola?", "alts": []}]
        text = "id;odpoved;otazka_1 (normálně);otazka_2 (krátce / hovorově)\nD001;Prvního září.;kdy začíná škola;začátek školy\n"
        for enc in ("cp1250", "utf-8-sig"):
            out = bc.read_d_csv(self._write(text, enc), q, {"D001": "k1"})
            self.assertEqual([c["query"] for c in out], ["kdy začíná škola", "začátek školy"], enc)


class Xlsx(unittest.TestCase):
    def _tmp(self):
        import tempfile
        f = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_roundtrip_keeps_commas_semicolons_quotes_newlines_and_diacritics(self):
        rows = [["id", "odpoved", "otazka_1"],
                ["D001", 'Ano, může; "ano" & <ne>\nDruhý řádek: příští týden', ""],
                ["BEZ", "x", "kolik stojí lyžák?"]]
        path = self._tmp()
        xw.write_xlsx(path, rows, widths=[8, 60, 30], input_cols={2}, sheet="Sada D")
        back = xr.read_rows(path, "Sada D")
        self.assertEqual(back[0], rows[0])
        self.assertEqual(back[1][:2], rows[1][:2])                 # an empty trailing cell may be dropped
        self.assertEqual(back[2], rows[2])

    def test_d_worksheet_and_equivalents_are_read_from_xlsx(self):
        q = [{"idx": 0, "pack": "p", "entry_key": "kdy-zacina-skola", "q": "Kdy začíná škola?", "alts": []}]
        path = self._tmp()
        xw.write_xlsx(path, [["id", "odpoved", "otazka_1", "otazka_2"],
                             ["D001", "Prvního září, v pondělí.", "kdy jdem do školy", "zacatek skoly"],
                             ["BEZ", "(x)", "kolik stojí lyžák", ""]])
        out = bc.read_d_csv(path, q, {"D001": "kdy-zacina-skola"})
        self.assertEqual([(c["entry_key"], c["query"]) for c in out],
                         [("kdy-zacina-skola", "kdy jdem do školy"), ("kdy-zacina-skola", "zacatek skoly"),
                          (None, "kolik stojí lyžák")])
        eq = self._tmp()
        xw.write_xlsx(eq, [["klic_A", "klic_B", "shodne (ano/ne)"], ["a", "b", "ano"], ["c", "d", "ne"]])
        self.assertEqual(bc.read_equiv_csv(eq), [["a", "b"]])


if __name__ == "__main__":
    unittest.main()
