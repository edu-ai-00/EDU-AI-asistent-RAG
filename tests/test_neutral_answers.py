"""Unit tests for the neutral answers lab (tools/lab/neutral_answers).

Stdlib-only: the rules, the xlsx reader and the review app's decision/export
logic run without the search image. The machine view needs eduai_normalize and
is therefore absent here — the app must degrade to machine=None, which is tested
too. Modules are loaded BY PATH (see test_qa_export_csv.py for why).
"""
import argparse
import csv
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import zipfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LAB = os.path.join(_REPO_ROOT, "tools", "lab", "neutral_answers")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"neutral_answers_{name}",
                                                  os.path.join(_LAB, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses look their module up
    spec.loader.exec_module(mod)
    return mod


rules = _load("rules")
xlsx_read = _load("xlsx_read")
server = _load("server")
neutralize = rules.neutralize


def _rules_of(result, kind=None):
    return {c["rule"] for c in result["changes"] if kind is None or c["kind"] == kind}


class RulesTest(unittest.TestCase):

    def test_example_role_and_agreement(self):
        r = neutralize("Syn by jste měli zabalit penál a sešity")
        self.assertEqual(r["proposed"], "Uchazeč by měl zabalit penál a sešity.")
        self.assertIn("role", _rules_of(r, rules.CHECK))
        self.assertIn("by_jste", _rules_of(r, rules.AUTO))

    def test_possessive_dropped_with_role_word(self):
        r = neutralize("Váš syn si s sebou může vzít svačinu.")
        self.assertEqual(r["proposed"], "Uchazeč si s sebou může vzít svačinu.")

    def test_subject_word_is_configurable(self):
        r = neutralize("Dcera musí přinést přihlášku.", subject="žák")
        self.assertEqual(r["proposed"], "Žák musí přinést přihlášku.")

    def test_ano_removed_and_tykani_becomes_vykani(self):
        r = neutralize("Ano, můžeš se účastnit zkoušek, ale bude ti snížen počet bodů.")
        self.assertEqual(r["proposed"], "Můžete se účastnit zkoušek, ale bude vám snížen počet bodů.")
        # "ti" is ambiguous (also "ti žáci") -> offered for a check, not trusted
        self.assertTrue(any(c["before"] == "ti" and c["kind"] == rules.CHECK for c in r["changes"]))

    def test_ano_ale_drops_the_contrast_word(self):
        r = neutralize("Ano, ale musíš pamatovat na to, abys stihl vše.")
        self.assertEqual(r["proposed"], "Musíte pamatovat na to, abyste stihl vše.")

    def test_ne_removed_keeps_meaning_when_sentence_is_negative(self):
        r = neutralize("Ne. Přijetí se už zápisovým lístkem nepotvrzuje.")
        self.assertEqual(r["proposed"], "Přijetí se už zápisovým lístkem nepotvrzuje.")
        self.assertEqual(r["flags"], [])

    def test_ne_removed_flags_lost_negation(self):
        r = neutralize("Ne, budou pouze formou testů.")
        self.assertEqual(r["proposed"], "Budou pouze formou testů.")
        self.assertTrue(any("zápor" in f["note"] for f in r["flags"]))

    def test_bare_ano_is_flagged_not_rewritten(self):
        r = neutralize("Ano")
        self.assertTrue(any(f["rule"] == "ano_ne" for f in r["flags"]))

    def test_short_first_sentence_is_flagged(self):
        r = neutralize("Ano, můžeš. Pak se už ale nesmíš vrátit zpět.")
        self.assertEqual(r["proposed"], "Můžete. Pak se už ale nesmíte vrátit zpět.")
        self.assertTrue(any("krátká" in f["note"] for f in r["flags"]))

    def test_imperatives(self):
        self.assertEqual(neutralize("Zkus se zeptat svého třídního učitele.")["proposed"],
                         "Zkuste se zeptat svého třídního učitele.")

    def test_pronoun_nas_is_not_a_verb(self):
        r = neutralize("Náš web uvádí termíny.")
        self.assertNotIn("tykani", _rules_of(r))

    def test_first_person(self):
        r = neutralize("Záleží na konkrétních školách. Doporučuji projít webové stránky.")
        self.assertIn("Doporučuje se projít", r["proposed"])
        self.assertTrue(any(f["rule"] == "chybi_info" for f in r["flags"]))

    def test_non_answer_gets_template_with_link(self):
        r = neutralize("Filozofické otázky zatím nezodpovídám. :(",
                       sources="Cermat | https://prijimacky.cermat.cz/")
        self.assertTrue(r["proposed"].startswith("Tato informace zatím není k dispozici."))
        self.assertIn("https://prijimacky.cermat.cz/", r["proposed"])
        self.assertIn("smajlik", _rules_of(r))

    def test_link_is_never_written_into_the_answer(self):
        """The chat shows the sources; a "Více informací: <url>" only repeated them."""
        src = "Cermat | https://prijimacky.cermat.cz/\nSystém DiPSy | https://dipsy.gov.cz/prihlaska/"
        r = neutralize("Přihlášku podáte v systému DiPSy", ["Kde najdu přihlášku?"], src)
        self.assertNotIn("http", r["proposed"])
        self.assertNotIn("odkaz", _rules_of(r))
        self.assertFalse(any(f["rule"] == "odkaz" for f in r["flags"]))   # a real page

    def test_home_page_only_is_flagged(self):
        r = neutralize("Cvičné testy jsou k dispozici online.", ["Kde najdu cvičné testy JPZ?"],
                       "Cermat | https://prijimacky.cermat.cz/")
        self.assertNotIn("http", r["proposed"])
        self.assertTrue(any(f["rule"] == "odkaz" and "úvodní" in f["note"] for f in r["flags"]))

    def test_no_source_address_is_flagged(self):
        r = neutralize("Najdete je na webu.", ["Kde najdu výsledky?"], "")
        self.assertTrue(any(f["rule"] == "odkaz" for f in r["flags"]))

    def test_no_link_for_a_place_question(self):
        r = neutralize("JPZ se koná na jedné ze škol z přihlášky.", ["Kde uchazeč píše JPZ?"],
                       "Cermat | https://prijimacky.cermat.cz/")
        self.assertNotIn("odkaz", _rules_of(r))

    def test_neutral_answer_is_left_alone(self):
        text = "Uchazeč si místo konání sám měnit nemůže."
        r = neutralize(text)
        self.assertEqual(r["proposed"], text)
        self.assertEqual(r["changes"], [])
        self.assertEqual(r["flags"], [])


def _xlsx(path, rows):
    """Minimal workbook with inline strings — enough for the reader."""
    def cell(ci, ri, v):
        ref = chr(65 + ci) + str(ri)
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    sheet = "".join(f'<row r="{ri}">' + "".join(cell(ci, ri, v) for ci, v in enumerate(row) if v) + "</row>"
                    for ri, row in enumerate(rows, 1))
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rns}><sheets>'
                                      f'<sheet name="FAQ" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{sheet}</sheetData></worksheet>")


class XlsxReaderTest(unittest.TestCase):

    def test_pack_layout(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "faq.xlsx")
            _xlsx(p, [["canonical_q", "answer", "sources", "Alternativa 1", "Alternativa 2"],
                      ["Kdy je JPZ?", "Ano. V dubnu.", '"Cermat | https://prijimacky.cermat.cz/"', "termín jpz", ""],
                      ["Bez odpovědi?", "", "", "", ""]])
            items = xlsx_read.load_items(p)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["alternatives"], ["termín jpz"])
        self.assertEqual(items[0]["sources"], "Cermat | https://prijimacky.cermat.cz/")

    def test_loose_layout(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "o.xlsx")
            _xlsx(p, [["Otázka?", "", "", "", "", "", "Ano, můžeš."], ["Jiná otázka", "", "", "", "", "", ""]])
            items = xlsx_read.load_items(p)
        self.assertEqual([(i["canonical_q"], i["answer"]) for i in items], [("Otázka?", "Ano, můžeš.")])


class PackCsvTest(unittest.TestCase):
    """A QA pack CSV read as the application reads it, and handed back unchanged."""

    HEADER9 = ["entry_key", "canonical_q", "answer_md", "cat", "variants", "sources",
               "is_active", "domains", "category_code"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "decisions.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows, delimiter=",", bom=True):
        p = os.path.join(self.tmp.name, "pack.csv")
        with open(p, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
            csv.writer(fh, delimiter=delimiter, lineterminator="\r\n").writerows(rows)
        return p

    def _app(self, path):
        return server.App(argparse.Namespace(input=path, sheet=None, state=self.state,
                                             subject="uchazeč"))

    def test_comma_and_semicolon(self):
        row = ["k1", "Kdy je JPZ?", "JPZ je v dubnu.", "Termíny", "termín jpz\nkdy jsou přijímačky",
               "Cermat | https://prijimacky.cermat.cz/", "TRUE", '["ADMISSIONS"]', "TERMS"]
        for delim in (",", ";"):
            items = xlsx_read.load_items(self._write([self.HEADER9, row], delim))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["alternatives"], ["termín jpz", "kdy jsou přijímačky"])
            self.assertEqual(items[0]["pack"]["domains"], '["ADMISSIONS"]')
            self.assertEqual(items[0]["pack"]["category_code"], "TERMS")

    def test_aliases_and_no_bom(self):
        items = xlsx_read.load_items(self._write([["question", "answer"], ["Otázka?", "Odpověď."]],
                                                 bom=False))
        self.assertEqual((items[0]["canonical_q"], items[0]["answer"]), ("Otázka?", "Odpověď."))
        self.assertIsNone(items[0]["pack"]["category_code"])     # column absent

    def test_not_a_qa_header(self):
        with self.assertRaises(ValueError):
            xlsx_read.load_items(self._write([["foo", "bar"], ["1", "2"]]))

    def test_round_trip_keeps_the_pack_columns(self):
        rows = [self.HEADER9,
                ["k1", "Lze odejít dřív?", "Ano, můžeš odejít dřív.", "Docházka", "Můžu odejít?",
                 "", "TRUE", '["ATTENDANCE"]', "ATT"],
                ["k2", "Kdy je JPZ?", "JPZ je v dubnu.", "", "", "", "TRUE", "", ""]]
        app = self._app(self._write(rows))
        app.decide({"id": "001", "status": "approved", "final": "Odejít dřív je možné."})
        out = list(csv.reader(io.StringIO(app.export("pack").decode("utf-8-sig"))))
        self.assertEqual(out[0], self.HEADER9)
        self.assertEqual(out[1], ["k1", "Lze odejít dřív?", "Odejít dřív je možné.", "Docházka",
                                  "Můžu odejít?", "", "TRUE", '["ATTENDANCE"]', "ATT"])
        self.assertEqual(out[2], rows[2])

    def test_without_category_code_column_none_is_written(self):
        app = self._app(self._write([self.HEADER9[:8], ["k1", "Otázka?", "Odpověď.", "", "", "", "TRUE", ""]]))
        out = list(csv.reader(io.StringIO(app.export("pack").decode("utf-8-sig"))))
        # an ABSENT column keeps a category; a present empty one would clear it
        self.assertEqual(out[0], server.PACK_HEADER)


class ReviewAppTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.xlsx = os.path.join(self.tmp.name, "faq.xlsx")
        _xlsx(self.xlsx, [["canonical_q", "answer", "sources", "Alternativa 1"],
                          ["Lze odejít dřív?", "Ano, můžeš odejít dřív.", "", "Můžu odejít?"],
                          ["Kdy je JPZ?", "JPZ je v dubnu.", "", ""]])
        self.state = os.path.join(self.tmp.name, "decisions.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _app(self):
        return server.App(argparse.Namespace(input=self.xlsx, sheet=None, state=self.state,
                                             subject="uchazeč"))

    def test_machine_view_degrades_without_normalizer(self):
        app = self._app()
        self.assertIsNone(app.items[0]["machine"])
        self.assertFalse(app.payload()["meta"]["machine"])

    def test_decisions_persist_and_drive_the_pack_export(self):
        app = self._app()
        app.decide({"id": "001", "status": "approved", "final": "Odejít dřív je možné."})
        app.decide({"id": "002", "status": "rejected"})
        reloaded = self._app()
        self.assertEqual(reloaded.decision(reloaded.by_id["001"])["status"], "approved")
        rows = list(csv.reader(io.StringIO(reloaded.export("pack").decode("utf-8-sig"))))
        self.assertEqual(rows[0], server.PACK_HEADER)
        self.assertEqual(len(rows), 3)                        # every answer, rejected too
        self.assertEqual(rows[1][2], "Odejít dřív je možné.")
        self.assertEqual(rows[1][4], "Můžu odejít?")
        self.assertEqual(rows[2][2], "JPZ je v dubnu.")       # rejected: the original

    def test_pack_never_carries_an_unreviewed_rewrite(self):
        app = self._app()                                     # nothing decided
        self.assertEqual(app.awaiting_review(), 1)            # 001 has a proposal
        rows = list(csv.DictReader(io.StringIO(app.export("pack").decode("utf-8-sig"))))
        self.assertEqual([r["answer_md"] for r in rows],
                         ["Ano, můžeš odejít dřív.", "JPZ je v dubnu."])

    def test_pack_from_a_workbook_inherits_areas_and_derives_keys(self):
        rows = list(csv.DictReader(io.StringIO(self._app().export("pack").decode("utf-8-sig"))))
        # "[]" would CLEAR the areas; an empty cell inherits the upload's
        self.assertEqual({r["domains"] for r in rows}, {""})
        # a row-position key duplicated entries once rows moved; empty = derived
        self.assertEqual({r["entry_key"] for r in rows}, {""})
        self.assertEqual({r["is_active"] for r in rows}, {"TRUE"})

    def test_pack_header_is_the_applications(self):
        spec = importlib.util.spec_from_file_location(
            "qa_csv_for_lab", os.path.join(_REPO_ROOT, "search", "app", "qa_csv.py"))
        qa_csv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qa_csv)
        self.assertEqual(server.PACK_HEADER, qa_csv.HEADER)
        raw = self._app().export("pack")
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))   # BOM, for Excel
        self.assertIn(b"\r\n", raw)

    def test_rejected_keeps_original_in_review_export(self):
        app = self._app()
        app.decide({"id": "001", "status": "rejected"})
        rows = list(csv.DictReader(io.StringIO(app.export("review").decode("utf-8-sig"))))
        self.assertEqual(rows[0]["vysledna"], "Ano, můžeš odejít dřív.")

    def test_changed_source_answer_makes_decision_stale(self):
        app = self._app()
        app.decide({"id": "001", "status": "approved", "final": "Upraveno."})
        _xlsx(self.xlsx, [["canonical_q", "answer"], ["Lze odejít dřív?", "Jiné znění odpovědi."]])
        d = self._app().decision(self._app().by_id["001"])
        self.assertTrue(d["stale"])
        self.assertEqual(d["status"], "pending")

    def test_invalid_decision_is_refused(self):
        app = self._app()
        with self.assertRaises(ValueError):
            app.decide({"id": "999", "status": "approved"})
        with self.assertRaises(ValueError):
            app.decide({"id": "001", "status": "maybe"})
        self.assertFalse(os.path.exists(self.state))


if __name__ == "__main__":
    unittest.main()
