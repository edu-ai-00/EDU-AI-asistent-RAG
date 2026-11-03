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



class Spelling(unittest.TestCase):
    def _by(self, q):
        return {lbl: v for _t, lbl, v in gen.spelling(q)}

    def test_every_y_as_i_and_no_variant_when_nothing_changes(self):
        self.assertEqual(self._by("Kdy je výjimka?")["všechna y jako i"], "Kdi je víjimka?")
        self.assertEqual(gen.spelling("Kdo je tam?"), [])

    def test_me_mne_both_ways_and_case(self):
        self.assertEqual(self._by("Co mě čeká?")["mě ↔ mně"], "Co mně čeká?")
        self.assertEqual(self._by("Mně to nevadí.")["mě ↔ mně"], "Mě to nevadí.")

    def test_s_z_at_the_start_of_a_word_changes_the_first_word_only(self):
        self.assertEqual(self._by("Kdy je shoda a zhoda?")["s ↔ z na začátku slova"], "Kdy je zhoda a zhoda?")
        self.assertNotIn("s ↔ z na začátku slova", self._by("Kdy se sejdeme?"))        # 'se': no consonant after s

    def test_u_acute_ring_swap_doubled_consonant_and_bje(self):
        self.assertEqual(self._by("Účet je v domě")["ú ↔ ů"], "Ůčet je v domě")
        self.assertEqual(self._by("Dům je tam")["ú ↔ ů"], "Dúm je tam")
        self.assertEqual(self._by("Ranní kolo")["zdvojená hláska jednou"], "Raní kolo")
        self.assertEqual(self._by("Co dělat při věci?")["bě/pě/vě jako bje/pje/vje"], "Co dělat při vjeci?")

    def test_combined_no_diacritics_and_y_as_i(self):
        self.assertEqual(self._by("Výjimka z přijímaček")["bez diakritiky a y jako i"], "Vijimka z prijimacek")

    def test_all_variants_include_b5_and_stay_deterministic(self):
        a = gen.all_variants("Kdy začíná výjimka v domě?", "001")
        self.assertIn("B5", {t for t, _l, _v in a})
        self.assertEqual(a, gen.all_variants("Kdy začíná výjimka v domě?", "001"))

if __name__ == "__main__":
    unittest.main()
