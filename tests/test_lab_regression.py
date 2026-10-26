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


if __name__ == "__main__":
    unittest.main()
