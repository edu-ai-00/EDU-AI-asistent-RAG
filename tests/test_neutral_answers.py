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
import json
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
log_queue = _load("log_queue")
topics = _load("topics")
neutralize = rules.neutralize


def _rules_of(result, kind=None):
    return {c["rule"] for c in result["changes"] if kind is None or c["kind"] == kind}


class RulesTest(unittest.TestCase):

    def test_example_role_and_agreement(self):
        r = neutralize("Syn by jste měli zabalit penál a sešity")
        self.assertEqual(r["proposed"], "Uchazeč by měl zabalit penál a sešity.")
        self.assertIn("role", _rules_of(r, rules.CHECK))
        self.assertIn("by_jste", _rules_of(r, rules.AUTO))

    def test_address_ty_turns_vykani_into_tykani(self):
        r = neutralize("Zkuste se podívat na web. Pokud nevíte, zeptejte se a omluvte se.", address="ty")
        self.assertEqual(r["proposed"], "Zkus se podívat na web. Pokud nevíš, zeptej se a omluv se.")
        self.assertIn("vykani", _rules_of(r, rules.AUTO))
        self.assertNotIn("tykani", _rules_of(r))

    def test_address_ty_unsure_forms_are_offered_for_a_check(self):
        r = neutralize("Najdete to v řádu, který vám pošle škola. Doporučujeme, abyste přišli včas.", address="ty")
        self.assertEqual(r["proposed"], "Najdeš to v řádu, který ti pošle škola. Doporučuje se, abys přišel včas.")
        checked = {(c["before"], c["after"]) for c in r["changes"] if c["kind"] == rules.CHECK}
        self.assertIn(("vám", "ti"), checked)
        self.assertIn(("abys přišli", "abys přišel"), checked)

    def test_address_beze_keeps_the_address(self):
        r = neutralize("Ano, můžeš. Kontaktujte školu.", address="beze")
        self.assertEqual(r["proposed"], "Můžeš. Kontaktujte školu.")
        self.assertFalse(_rules_of(r) & {"tykani", "vykani"})

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


class LogQueueTest(unittest.TestCase):
    """Not-answered questions from a replay, grouped for the curator."""

    LOOKUP = {
        "questions": {"1": {"q": "Kdy jsou náhradní termíny?", "turn": 1, "entity": "S1"},
                      "2": {"q": "kdy jsou nahradni terminy", "turn": 1, "entity": "S1"},
                      "3": {"q": "A v kolik hodin?", "turn": 2, "entity": "S1"},
                      "4": {"q": "Kdy jsou náhradní termíny?", "turn": 1, "entity": "S2"},
                      "5": {"q": "Kdy je JPZ?", "turn": 1, "entity": "S1"}},
        "entries": {"e1": {"q": "Kdy se konají náhradní termíny JPZ?", "entity": "S1",
                           "pack": "jpz", "active": True},
                    "e2": {"q": "Kdy vzniká nárok na náhradní termín?", "entity": "S1",
                           "pack": "jpz", "active": True},
                    "old": {"q": "Staré", "entity": "S1", "pack": "jpz", "active": False}},
    }

    def _rows(self):
        lem = ["kdy", "nahradni", "termin"]
        return [
            {"id": 1, "entity": "S1", "stage": "before_answer", "decision": "B-deferred", "lemma": lem,
             "prod_source": "qa_exact", "prod_entry": "old",
             "offered": [{"entry": "e1", "score": 0.75}, {"entry": "e2", "score": 0.62},
                         {"entry": "old", "score": 0.9}]},
            {"id": 2, "entity": "S1", "stage": "before_answer", "decision": "B-deferred",
             "lemma": list(reversed(lem)), "offered": [{"entry": "e1", "score": 0.8}]},
            {"id": 3, "entity": "S1", "stage": "before_answer", "decision": "C", "lemma": ["kolik"]},
            {"id": 4, "entity": "S2", "stage": "before_answer", "decision": "C", "lemma": lem},
            {"id": 5, "entity": "S1", "stage": "answered", "answer_source": "qa_exact", "lemma": ["kdy", "jpz"]},
        ]

    def test_groups_same_school_same_lemmas(self):
        q = log_queue.build_queue(self._rows(), self.LOOKUP)
        self.assertEqual([(g["entity"], g["count"]) for g in q], [("S1", 2), ("S2", 1)])
        self.assertEqual(q[0]["wordings"][0], "Kdy jsou náhradní termíny?")
        self.assertEqual(q[0]["outcome"], "nabídka")
        self.assertEqual(q[0]["prod"], {"qa_exact": 1, "?": 1})
        # answered once by an entry that is switched off now
        self.assertEqual(q[0]["earlier_inactive"], ["Staré [jpz]"])

    def test_offered_best_score_inactive_dropped(self):
        off = log_queue.build_queue(self._rows(), self.LOOKUP)[0]["offered"]
        self.assertEqual([(o["entry"], o["score"]) for o in off], [("e1", 0.8), ("e2", 0.62)])

    def test_follow_ups_and_answered_are_left_out(self):
        ids = {i for g in log_queue.build_queue(self._rows(), self.LOOKUP) for i in g["log_ids"]}
        self.assertEqual(ids, {1, 2, 4})
        self.assertTrue(log_queue.is_follow_up("A kdy to je?", 1))
        self.assertFalse(log_queue.is_follow_up("jídelna", 1))


class QueueReviewTest(unittest.TestCase):
    """The review app's queue tab: decisions per group of logged questions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        q = log_queue.build_queue(LogQueueTest()._rows(), LogQueueTest.LOOKUP)
        self.queue = os.path.join(self.tmp.name, "queue.json")
        with open(self.queue, "w", encoding="utf-8") as fh:
            json.dump({"groups": q}, fh, ensure_ascii=False)
        self.key = q[0]["key"]
        self.xlsx = os.path.join(self.tmp.name, "faq.xlsx")
        _xlsx(self.xlsx, [["canonical_q", "answer"], ["Kdy je JPZ?", "JPZ je v dubnu."]])
        self.state = os.path.join(self.tmp.name, "d.decisions.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _app(self):
        return server.App(argparse.Namespace(input=self.xlsx, sheet=None, state=self.state,
                                             subject="uchazeč", queue=self.queue))

    def test_alternative_to_an_offered_entry_persists(self):
        app = self._app()
        d = app.queue.decide({"key": self.key, "status": "alternative", "entry": "e1"})
        self.assertEqual((d["status"], d["wording"]), ("alternative", "Kdy jsou náhradní termíny?"))
        self.assertEqual(d["entry_q"], "Kdy se konají náhradní termíny JPZ?")
        again = self._app().queue.decision(self.key)
        self.assertEqual(again["entry"], "e1")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "d.decisions.queue.json")))
        self.assertEqual(app.payload()["meta"]["queue"], 2)

    def test_alternative_can_also_mark_the_answer_missing(self):
        app = self._app()
        d = app.queue.decide({"key": self.key, "status": "alternative", "entry": "e1",
                              "missing_too": True, "note": "chybí termín pro rok 2027"})
        self.assertTrue(d["missing_too"])
        self.assertEqual(d["note"], "chybí termín pro rok 2027")
        store = app.queue.payload()["store"]
        self.assertEqual((store["file"], store["decided"]), ("d.decisions.queue.json", 1))

    def test_only_offered_entries_are_accepted(self):
        app = self._app()
        with self.assertRaises(ValueError):
            app.queue.decide({"key": self.key, "status": "alternative", "entry": "zzz"})
        with self.assertRaises(ValueError):
            app.queue.decide({"key": self.key, "status": "ambiguous", "entries": ["e1", "zzz"]})
        with self.assertRaises(ValueError):
            app.queue.decide({"key": "nope", "status": "skip"})

    def test_ambiguous_missing_and_reset(self):
        app = self._app()
        d = app.queue.decide({"key": self.key, "status": "ambiguous", "entries": ["e1", "e2"],
                              "note": "chybí kontakt na školu"})
        self.assertEqual((d["entries"], d["note"]), (["e1", "e2"], "chybí kontakt na školu"))
        self.assertEqual(app.queue.decide({"key": self.key, "status": "pending"})["status"], "pending")

    def test_without_queue_no_tab(self):
        app = server.App(argparse.Namespace(input=self.xlsx, sheet=None, state=self.state,
                                            subject="uchazeč"))
        self.assertIsNone(app.queue)
        self.assertIsNone(app.payload()["meta"]["queue"])



def _g(key, lemmas, count, question, offered=()):
    return {"key": key, "entity": "S1", "lemmas": lemmas, "count": count, "question": question,
            "wordings": [question], "outcome": "nic", "prod": {}, "earlier_inactive": [],
            "offered": [{"entry": e, "q": f"Odpověď {e}", "pack": "p", "score": s} for e, s in offered],
            "log_ids": []}


class TopicsTest(unittest.TestCase):
    """topics.py: the search's own reduction groups wordings of ONE question."""

    SYN = {"prijimacky": "syn_jpz", "prijimaci": "syn_jpz", "zkouska": "syn_jpz"}

    def test_concepts_drop_coverage_stopwords_and_join_synonyms(self):
        self.assertEqual(topics.concepts(["kdy", "byt", "prijimacky"], self.SYN, {"byt"}), ["kdy", "syn_jpz"])

    def test_similarity_needs_both_sides(self):
        w = {"a": 1.0, "b": 1.0, "c": 1.0}
        self.assertEqual(topics.similarity(["a", "b"], ["a", "b"], w), 1.0)
        # a short question inside a long one is not the same question
        self.assertEqual(topics.similarity(["a"], ["a", "b", "c"], w), 1 / 3)

    def test_cluster_joins_spellings_keeps_other_words_apart(self):
        c = {"k1": ["absence", "omlouvat"], "k2": ["absence", "omlouvat"],      # „omlouva“ / „omlouvá“
             "k3": ["dolozit", "dite", "prijit"]}                              # same meaning, other words
        w = topics.idf_weights(list(c.values()))
        self.assertEqual(sorted(map(sorted, topics.cluster(["k1", "k2", "k3"], c, w))), [["k1", "k2"], ["k3"]])

    def test_build_manual_first_then_most_similar_else_unsorted(self):
        gs = [_g("S1|a", ["lyzak", "kdy"], 5, "kdy je lyžák"), _g("S1|b", ["lyzak", "kdy", "byt"], 2, "kdy bude lyžák"),
              _g("S1|c", ["jidelna"], 1, "jídelna")]
        res = topics.build(gs, {"S1|a": {"area": "Provoz", "title": "Lyžák"}})
        self.assertEqual(res["assign"]["S1|a"], "Provoz|Lyžák")
        self.assertEqual(res["assign"]["S1|b"], "Provoz|Lyžák")
        self.assertEqual(res["assign"]["S1|c"], topics.UNSORTED)

    def test_build_without_manual_makes_topics_of_subgroups(self):
        gs = [_g("S1|a", ["lyzak", "kdy"], 5, "kdy je lyžák"), _g("S1|b", ["lyzak", "kdy"], 2, "kdy je lyzak"),
              _g("S1|c", ["jidelna"], 1, "jídelna")]
        res = topics.build(gs)
        self.assertEqual(res["assign"]["S1|a"], res["assign"]["S1|b"])
        self.assertEqual(res["topics"][res["assign"]["S1|a"]]["title"], "kdy je lyžák")
        self.assertNotEqual(res["assign"]["S1|c"], res["assign"]["S1|a"])


class QueueTopicsTest(unittest.TestCase):
    """Decisions per topic, moving a group, parking it, topics of one's own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        gs = [_g("S1|a", ["absence", "omlouvat"], 9, "Jak omluvit absenci?", [("e1", 0.7)]),
              _g("S1|b", ["absence", "omlouvat"], 3, "jak omluvit absenci", [("e1", 0.7)]),
              _g("S1|c", ["dolozit", "dite", "prijit"], 2, "Co doložit, když dítě nepřišlo?", [("e2", 0.4)]),
              _g("S1|d", ["lyzak"], 4, "lyžák")]
        self.queue = os.path.join(self.tmp.name, "queue.json")
        with open(self.queue, "w", encoding="utf-8") as fh:
            json.dump({"groups": gs}, fh, ensure_ascii=False)
        manual = {k: {"area": "Provoz", "title": "Absence"} for k in ("S1|a", "S1|b", "S1|c")}
        manual["S1|d"] = {"area": "Provoz", "title": "Lyžák"}
        self.topics = os.path.join(self.tmp.name, "topics.json")
        with open(self.topics, "w", encoding="utf-8") as fh:
            json.dump(topics.build(gs, manual), fh, ensure_ascii=False)
        self.xlsx = os.path.join(self.tmp.name, "faq.xlsx")
        _xlsx(self.xlsx, [["canonical_q", "answer"], ["Kdy je JPZ?", "JPZ je v dubnu."]])
        self.state = os.path.join(self.tmp.name, "d.decisions.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _q(self):
        return server.App(argparse.Namespace(input=self.xlsx, sheet=None, state=self.state, subject="žák",
                                             queue=self.queue, topics=self.topics)).queue

    def _rows(self, q):
        return {r["key"]: r for r in q.additions()}

    def test_topic_alternative_adds_one_wording_per_subgroup(self):
        q = self._q()
        st = q.decide_topic({"topic": "Provoz|Absence", "status": "alternative", "entry": "e2"})
        self.assertEqual(st["store"]["decided"], 3)
        rows = self._rows(q)
        # „omlouvá“ / „omlouva“: the search finds one from the other, one wording is enough
        self.assertEqual(rows["S1|a"]["wording"], "Jak omluvit absenci?")
        self.assertEqual(rows["S1|b"]["wording"], "")
        self.assertEqual(rows["S1|c"]["wording"], "Co doložit, když dítě nepřišlo?")
        self.assertEqual(rows["S1|c"]["entry_q"], "Odpověď e2")
        self.assertEqual(rows["S1|d"]["status"], "pending")
        q.decide_topic({"topic": "Provoz|Absence", "status": "alternative", "entry": "e1", "all_wordings": True})
        self.assertEqual(self._rows(self._q())["S1|b"]["wording"], "jak omluvit absenci")

    def test_group_decision_wins_and_later_leaves_the_topic(self):
        q = self._q()
        q.decide({"key": "S1|c", "status": "missing"})
        q.move({"key": "S1|b", "topic": server.LATER})
        q.decide_topic({"topic": "Provoz|Absence", "status": "alternative", "entry": "e1"})
        rows = self._rows(self._q())
        self.assertEqual((rows["S1|c"]["status"], rows["S1|c"]["where"]), ("missing", "group"))
        self.assertEqual((rows["S1|b"]["status"], rows["S1|b"]["topic"]), ("pending", "odloženo"))
        self.assertEqual(rows["S1|a"]["status"], "alternative")

    def test_moving_into_a_decided_topic_takes_its_decision(self):
        q = self._q()
        q.decide_topic({"topic": "Provoz|Lyžák", "status": "skip"})
        q.move({"key": "S1|c", "topic": "Provoz|Lyžák"})
        self.assertEqual(self._rows(q)["S1|c"]["status"], "skip")
        self.assertEqual(q.subgroups()["S1|c"] != q.subgroups()["S1|d"], True)

    def test_own_topics_create_rename_and_validate(self):
        q = self._q()
        tid = q.save_topic({"title": "Omluvenky"})["id"]
        with self.assertRaises(ValueError):
            q.save_topic({"title": "Omluvenky"})
        q.move({"key": "S1|c", "topic": tid})
        q.save_topic({"id": tid, "title": "Omlouvání dítěte"})
        q2 = self._q()                                   # persisted
        self.assertEqual(q2.topics()[tid]["title"], "Omlouvání dítěte")
        self.assertEqual(q2.assign()["S1|c"], tid)
        q2.save_topic({"id": "Provoz|Lyžák", "title": "Lyžařský výcvik"})
        self.assertEqual(q2.topics()["Provoz|Lyžák"]["area"], "Provoz")
        with self.assertRaises(ValueError):
            q2.move({"key": "S1|c", "topic": "nope"})
        with self.assertRaises(ValueError):
            q2.decide_topic({"topic": "Provoz|Absence", "status": "alternative", "entry": "zzz"})

    def test_missing_on_its_own_leaves_the_topic(self):
        q = self._q()
        q.decide({"key": "S1|c", "status": "missing"})          # marked, never moved
        self.assertEqual(q.assign()["S1|c"], server.ALONE)
        q.decide_topic({"topic": "Provoz|Absence", "status": "alternative", "entry": "e1"})
        rows = self._rows(q)
        self.assertEqual((rows["S1|c"]["topic"], rows["S1|c"]["status"]), ("vyjmuto ze skupiny", "missing"))
        q.decide({"key": "S1|c", "status": "pending"})          # back where it was
        self.assertEqual(q.assign()["S1|c"], "Provoz|Absence")
        q.move({"key": "S1|b", "topic": server.ALONE})
        self.assertEqual(self._rows(q)["S1|b"]["status"], "pending")   # no topic decision out there

    def test_own_topic_remembers_when_it_was_added_to(self):
        q = self._q()
        tid = q.save_topic({"title": "Omluvenky"})["id"]
        before = q.topics()[tid]["touched_utc"]
        q.move({"key": "S1|c", "topic": tid})
        self.assertGreater(q.topics()[tid]["touched_utc"], before)

    def test_topic_missing_keeps_the_topic_together(self):
        q = self._q()
        q.decide_topic({"topic": "Provoz|Absence", "status": "missing"})
        a = q.assign()
        self.assertEqual((a["S1|a"], a["S1|c"]), ("Provoz|Absence", "Provoz|Absence"))
        self.assertEqual(self._rows(q)["S1|a"]["status"], "missing")

    def test_export_csv(self):
        q = self._q()
        q.decide_topic({"topic": "Provoz|Absence", "status": "missing", "note": "chybí lhůta"})
        rows = list(csv.reader(io.StringIO(q.export_csv().decode("utf-8-sig"))))
        self.assertEqual(rows[0][:4], ["skupina", "pocet", "dotaz", "stav"])
        self.assertIn(["Absence", "9", "Jak omluvit absenci?", "missing", "skupinu", "", "", "", "ano", "chybí lhůta"], rows)

    def test_without_topics_file_the_reduction_alone(self):
        q = server.App(argparse.Namespace(input=self.xlsx, sheet=None, state=self.state, subject="žák",
                                          queue=self.queue)).queue
        a = q.assign()
        self.assertEqual(a["S1|a"], a["S1|b"])
        self.assertNotEqual(a["S1|a"], a["S1|c"])

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

    def test_approval_for_another_address_must_be_renewed(self):
        app = self._app()                                    # vykání (default)
        app.decide({"id": "001", "status": "approved"})
        app.decide({"id": "002", "status": "approved"})
        ty = server.App(argparse.Namespace(input=self.xlsx, sheet=None, state=self.state,
                                           subject="uchazeč", osloveni="beze"))
        d1, d2 = ty.decision(ty.by_id["001"]), ty.decision(ty.by_id["002"])
        # „můžeš“ stays now, the approved text said „můžete“: approve again
        self.assertEqual((d1["status"], d1["readdressed"]), ("pending", True))
        self.assertEqual(ty.pack_text(ty.by_id["001"]), "Ano, můžeš odejít dřív.")
        # nothing about the address in the second answer: the approval holds
        self.assertEqual((d2["status"], d2["readdressed"]), ("approved", False))
        self.assertEqual(ty.payload()["meta"]["address"], "beze změny")

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
