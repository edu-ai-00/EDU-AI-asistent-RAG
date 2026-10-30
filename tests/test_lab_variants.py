"""Unit tests for the variant generators (tools/lab/variants/generators.py).

Stdlib-only. measure.py needs the search image and the database and is not
covered here. The module is loaded BY PATH (see test_qa_export_csv.py for why).
"""
import importlib.util
import os
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "lab_variants_generators", os.path.join(_REPO_ROOT, "tools", "lab", "variants", "generators.py"))
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


class Synonyms(unittest.TestCase):
    def test_role_forms_and_case(self):
        out = {lbl: v for _t, lbl, v in gen.synonyms("Uchazeč přinese průkaz, uchazeči se vrátí.")}
        self.assertEqual(out["uchazeč→dcera"], "Dcera přinese průkaz, dceři se vrátí.")

    def test_longest_phrase_first(self):
        out = {lbl: v for _t, lbl, v in gen.synonyms("Kdy jsou přijímací zkoušky?")}
        self.assertEqual(out["přijímací zkouška→přijímačky"], "Kdy jsou přijímačky?")

    def test_no_match_no_variant(self):
        self.assertEqual(gen.synonyms("Kdy začíná škola?"), [])


class Diacritics(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(gen.no_diacritics("Kdy začíná JPZ?"), [("B2", "bez diakritiky", "kdy zacina jpz?")])


class Typos(unittest.TestCase):
    def test_reproducible_and_one_edit(self):
        a = gen.typos("Co si přinést k přijímací zkoušce?", "001")
        self.assertEqual(a, gen.typos("Co si přinést k přijímací zkoušce?", "001"))
        self.assertEqual({lbl for _t, lbl, _v in a}, set(gen.TYPO_KINDS))
        for _t, _l, v in a:
            self.assertNotEqual(v, "Co si přinést k přijímací zkoušce?")

    def test_short_words_untouched(self):
        self.assertEqual(gen.typos("Co je to?", "x"), [])


class Negation(unittest.TestCase):
    def test_flip_both_ways(self):
        self.assertEqual(gen.negation("Smím mít kalkulačku?")[0][2], "Nesmím mít kalkulačku?")
        self.assertEqual(gen.negation("Co když pozvánka nepřijde?")[0][2], "Co když pozvánka přijde?")
        self.assertEqual(gen.negation("Je možné psát JPZ dvakrát?")[0][2], "Není možné psát JPZ dvakrát?")

    def test_only_first(self):
        self.assertEqual(gen.negation("Musím, nebo můžu?")[0][2], "Nemusím, nebo můžu?")


if __name__ == "__main__":
    unittest.main()
