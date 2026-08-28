#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-08-21).
"""Build QA/probe_cases.json — what to ask, as whom, and which pack must answer.

    python3 QA/build_probe.py

Every case forbids the four tags that are NOT the caller's, so a leak across
schools or units fails wherever it happens. Families:

  exact   one per curated entry — proves every entry is reachable at all
  norm    the diacritics-free variant — the `norm` reduced form
  para    a phrasing the pack does NOT contain — qa_lexical / qa_vector
  typo    a misspelling — the pg_trgm suggestion path (never a confident answer)
  scope   proximity, org reach, sibling/tenant isolation, ročník, level, role
  rag     questions the documents answer — QA must beat RAG, and not swallow it
"""
from __future__ import annotations
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from packs_spec import ENTITIES, PROVOZ, PROVOZ_ONLY
from packs_spec_prijimacky import PRIJIMACKY, PRIJIMACKY_SOS

HERE = os.path.dirname(os.path.abspath(__file__))
TAGS = {c: f"[{e['tag']}]" for c, e in ENTITIES.items()}
# The ročník a caller of this entity uses when asking an admissions question.
ADM_GRADE = {"SKOLA_NOVA_VES": None, "SKOLA_NOVA_VES_ZS": 9, "SKOLA_NOVA_VES_SOS": 13,
             "ZAKLADNI_SKOLA_BOROVA": 9, "ZAKLADNI_SKOLA_LIPOVA": 9}

# Phrasings deliberately absent from every pack, so qa_exact must miss and the
# ranked stages have to carry the question.
PARAPHRASE = {
    "Jak se omlouvá absence?": "Jakým způsobem se u vás dokládá, že dítě chybělo?",
    "Můžu mít ve škole mobilní telefon?": "Jak to máte nastavené s telefony během vyučování?",
    "Jak funguje odhlašování obědů?": "Když je syn nemocný, jak mu zruším stravování?",
    "Kdy začíná a končí vyučování?": "V kolik hodin se ráno otevírá a kdy si mám dítě vyzvednout?",
    "Jak požádat o uvolnění z výuky na několik dní?": "Chystáme rodinný výjezd na dva týdny, jak to úředně zařídit?",
    "Kdo je pověřenec pro ochranu osobních údajů?": "Na koho se obrátit, když chci vědět, kdo hlídá osobní data dětí?",
    "Kdy se konají jednotné přijímací zkoušky?": "V jakém měsíci se letos píšou přijímačky na střední?",
    "Jakou váhu má jednotná přijímací zkouška?": "Kolik procent z celkového hodnocení dělá výsledek testů?",
    "Kolik škol si můžu dát do přihlášky?": "Na kolik oborů se dá letos hlásit najednou?",
    "Jak se mám připravit na přijímačky?": "Poraďte mi, jak se co nejlíp nachystat na zkoušky na střední.",
    "Můžu u přijímaček používat kalkulačku?": "Smím si k matematice vzít nějakou pomůcku na počítání?",
    "Existuje ještě zápisový lístek?": "Musíme po přijetí ještě někam něco nosit a potvrzovat?",
}

# Misspellings a pupil actually types. Expect a suggestion, never a foreign pack.
TYPO = {
    "Jak se omlouvá absence?": "jak se omlouva absnce",
    "Můžu mít ve škole mobilní telefon?": "muzu mit ve skole mobinlí telefon",
    "Kdy se konají jednotné přijímací zkoušky?": "kdy jsou jednotne prijmaci zkousky",
    "Jak funguje odhlašování obědů?": "jak funguje odhlasovani obedu",
    "Jak se mám připravit na přijímačky?": "jak se mam pripravit na prijmacky",
    "Kdo řeší neomluvenou absenci?": "kdo resi neomluvenu absenci",
}


def case(cid, query, entity, also_forbid=(), forbid_only=None, **kw):
    """A case forbids every tag that is not the caller's own, plus whatever
    `also_forbid` names — that is how a scoping case says which of the caller's
    OWN packs must stay out. An expected tag is never forbidden.

    `forbid_only` replaces the default set, for the one case where a sibling
    unit's pack answering is legitimate rather than a leak.
    """
    c = dict(id=cid, query=query, legal_entity=entity)
    c.update({k: v for k, v in kw.items() if v is not None})
    forbid = set(forbid_only) if forbid_only is not None else \
        {t for e, t in TAGS.items() if e != entity}
    forbid |= set(also_forbid)
    c["forbid_answer_contains"] = sorted(forbid - set(c.get("expect_answer_contains") or []))
    return c


def main() -> int:
    cases = []

    # ── exact: every curated entry, asked by its own school ───────────────────
    for code, e in ENTITIES.items():
        k, tag, g = e["key"], TAGS[code], ADM_GRADE[code]
        provoz = [q for q in PROVOZ if k in q.answers] + PROVOZ_ONLY.get(code, [])
        for i, q in enumerate(provoz):
            cases.append(case(f"exact_provoz_{k}_{i:02d}", q.q, code, role="STUDENT",
                              expect_outcome="answer", expect_answer_source="qa_exact",
                              expect_answer_contains=[tag]))
        adm = PRIJIMACKY_SOS if k == "sos" else [q for q in PRIJIMACKY if k in q.answers]
        for i, q in enumerate(adm):
            cases.append(case(f"exact_adm_{k}_{i:02d}", q.q, code, role="STUDENT", grade=g,
                              expect_outcome="answer", expect_answer_source="qa_exact",
                              expect_answer_contains=[tag]))

    # ── norm: the diacritics-free variant, ZŠ unit only ───────────────────────
    zs, ztag = "SKOLA_NOVA_VES_ZS", TAGS["SKOLA_NOVA_VES_ZS"]
    plain = [(q, v) for q in PROVOZ + PRIJIMACKY if "zs" in q.answers
             for v in q.variants if v == v.lower() and not any(ch in v for ch in "?!")]
    for i, (q, v) in enumerate(plain):
        cases.append(case(f"norm_{i:02d}", v, zs, role="STUDENT", grade=9,
                          expect_outcome="answer", expect_answer_source="qa_exact",
                          expect_answer_contains=[ztag], note=f"variant of: {q.q}"))

    # ── para: not in the pack, so a ranked stage must carry it ────────────────
    for i, (canon, para) in enumerate(PARAPHRASE.items()):
        cases.append(case(f"para_{i:02d}", para, zs, role="PARENT", grade=9,
                          expect_outcome="answer",
                          expect_answer_source_in=["qa_lexical", "qa_vector", "rag"],
                          forbid_answer_source=["qa_exact"], note=f"paraphrase of: {canon}"))

    # ── typo: the trgm path may suggest, must never answer from another school ─
    for i, (canon, typo) in enumerate(TYPO.items()):
        cases.append(case(f"typo_{i:02d}", typo, zs, role="STUDENT", grade=9,
                          expect_outcome_in=["answer", "suggest"], note=f"typo of: {canon}"))

    # ── scope: the axes the packs exist to exercise ───────────────────────────
    org, sos, jed = "SKOLA_NOVA_VES", "SKOLA_NOVA_VES_SOS", "ZAKLADNI_SKOLA_BOROVA"
    otag, stag = TAGS[org], TAGS[sos]
    cases += [
        case("scope_proximity_zs", "Jak se omlouvá absence?", zs, role="STUDENT",
             expect_outcome="answer", expect_answer_source="qa_exact",
             expect_answer_contains=[ztag],
             note="own school before the organisation — the b127ea9 regression"),
        case("scope_proximity_sos", "Jak se omlouvá absence?", sos, role="STUDENT",
             expect_outcome="answer", expect_answer_source="qa_exact",
             expect_answer_contains=[stag], note="the sibling unit gets its own answer"),
        case("scope_proximity_org", "Jak se omlouvá absence?", org, role="STUDENT",
             expect_outcome="answer", expect_answer_source="qa_exact",
             expect_answer_contains=[otag], note="the organisation is its own nearest school"),
        case("scope_org_reach_zs", "Kdy je otevřena kancelář školy?", zs, role="PARENT",
             expect_outcome="answer", expect_answer_contains=[otag],
             note="an org-only question still reaches a unit caller"),
        case("scope_org_reach_sos", "Komu patří školní jídelna?", sos, role="PARENT",
             expect_outcome="answer", expect_answer_contains=[otag],
             note="same, from the other unit"),
        case("scope_sibling_isolation", "Kdy probíhá odborná praxe?", zs, role="STUDENT",
             note="a SOŠ-only question must not answer with the SOŠ pack"),
        case("scope_tenant_isolation_zs", "Jak se dostanu do školy autobusem?", zs,
             role="STUDENT", note="a Borová-only question, asked at Nová Ves"),
        case("scope_tenant_isolation_jed", "Kdy je zápis do prvních tříd?", jed,
             role="PARENT", note="a Nová Ves question, asked at Borová"),
        case("scope_grade_out", "Jak dlouho trvá test z matematiky?", zs, role="STUDENT",
             grade=6, expect_answer_contains=[otag], also_forbid=[ztag],
             note="prijimacky-zs is ročník 5/7/9 — a šesťák falls to the org pack"),
        case("scope_grade_in", "Jak dlouho trvá test z matematiky?", zs, role="STUDENT",
             grade=9, expect_outcome="answer", expect_answer_source="qa_exact",
             expect_answer_contains=[ztag], note="the same question inside the ročník scope"),
        case("scope_level_out", "Z čeho se skládá maturitní zkouška?", zs, role="STUDENT",
             grade=9, note="a SS-level pack must not answer a ZŠ caller"),
        case("scope_level_in", "Z čeho se skládá maturitní zkouška?", sos, role="STUDENT",
             grade=13, expect_outcome="answer", expect_answer_source="qa_exact",
             expect_answer_contains=[stag], note="the same question at the SOŠ"),
        # The org's OWN units may legitimately answer once the org pack is out on
        # role, so only the org tag and the two foreign schools are forbidden.
        case("scope_role_denied", "Kolik škol si můžu dát do přihlášky?", org,
             role="TEACHER",
             forbid_only=[otag, TAGS[jed], TAGS["ZAKLADNI_SKOLA_LIPOVA"]],
             note="prijimacky-org is STUDENT/PARENT only"),
        case("scope_role_allowed", "Kolik škol si můžu dát do přihlášky?", org,
             role="STUDENT", expect_outcome="answer", expect_answer_source="qa_exact",
             expect_answer_contains=[otag], note="the same question, an admitted role"),
    ]

    # ── rag: QA beats RAG where both know, and does not swallow what it does not ─
    cases += [
        case("rag_qa_wins_jpz", "Kdy se konají jednotné přijímací zkoušky?", zs,
             role="STUDENT", grade=9, expect_outcome="answer",
             expect_answer_source="qa_exact", expect_answer_contains=[ztag],
             note="Cermat documents answer this too — the curated answer must win"),
        case("rag_qa_wins_kriteria", "Co se hodnotí u přijímaček?", zs, role="STUDENT",
             grade=9, expect_outcome="answer", expect_answer_source="qa_exact",
             expect_answer_contains=[ztag], note="prod baseline Q4b, now curated"),
        case("rag_still_reaches_sikana", "Jaké jsou znaky šikany?", zs, role="TEACHER",
             expect_outcome="answer", expect_answer_source_in=["rag"],
             note="not in any pack — the MŠMT document must still answer (baseline Q3)"),
        case("rag_still_reaches_pomucky", "jaké pomůcky pro 6. ročník?", zs,
             role="STUDENT", grade=6, expect_outcome="answer",
             expect_answer_source_in=["rag"],
             note="grade-specific, only the documents have it (baseline Q9)"),
        case("rag_no_answer_holds", "Kdy je nejbližší škola v přírodě?", zs, role="PARENT",
             grade=5, expect_answer_contains=[ztag],
             note="baseline Q6 was no_answer; the pack now answers it deliberately"),
    ]

    path = os.path.join(HERE, "probe_cases.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"_about": "QA pack probe cases — see QA/README.md. Generated by "
                             "QA/build_probe.py, do not hand-edit.",
                   "cases": cases}, fh, ensure_ascii=False, indent=1)
    fam = {}
    for c in cases:
        fam[c["id"].split("_")[0]] = fam.get(c["id"].split("_")[0], 0) + 1
    print(f"  {len(cases)} cases → {path}")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(fam.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
