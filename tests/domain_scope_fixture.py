#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-06).
"""Deterministic synthetic fixture for the domain-scope tests.

    docker compose run --rm --entrypoint python edu_ai_search tests/domain_scope_fixture.py build
    docker compose run --rm --entrypoint python edu_ai_search tests/domain_scope_fixture.py drop
    docker compose run --rm --entrypoint python edu_ai_search tests/domain_scope_fixture.py show

RUNS INSIDE THE SEARCH IMAGE, and that is not a convenience: the QA rows carry
norm_text / lemma_text / stem_text, and qa_exact matches the space-joined query
tokens against those columns for EQUALITY. Built by hand they would be a guess
about what the normalizer produces, the guess would be wrong for one form, and
the test would pass by never reaching the branch it claims to test. So the
fixture builds its reduced forms with the SAME eduai_normalize the cascade uses,
through the SAME active settings row.

EVERYTHING HERE IS INVENTED. No production text, no production identity: the
entities are the seed's fictional Nová Ves / Lipová schools, the questions are
about a school that does not exist, and every row is prefixed `ds_`/`ds-` so
`drop` can remove exactly what `build` created and nothing else.

WHAT IT COVERS, and why each row is here rather than being a variation of another:

  packs
    ds-attendance   one domain. The baseline: reachable in ATTENDANCE, not in CATERING.
    ds-mixed        pack labelled {ATTENDANCE, CATERING}, entries labelled ONE each.
                    This is the row that proves the ENTRY filter exists: a
                    manifest-only filter would admit both entries in either area.
    ds-untagged     domain_ids NULL. Reachable unrestricted, NEVER under an
                    explicit filter — an unclassified source is not "in every area".
    ds-off          is_active false. Never, in any area.
    ds-foreign      another LEGAL PERSON, labelled with the GLOBAL ATTENDANCE id.
                    Cross-tenant: choosing ATTENDANCE must not reach it.
    ds-org-global   the ORGANISATION's pack, labelled with the GLOBAL id only, and
                    visibility=organization. The id-resolution case from spec §4:
                    a ZŠ caller picks ATTENDANCE, whose id in their own catalog is
                    a different number, and must still reach this.
    ds-teacher      labelled ATTENDANCE, audience_roles {TEACHER}. A PARENT
                    choosing ATTENDANCE must still not see it — the area is a
                    preference, not a permission.
    ds-school-only  labelled with DS_ONLY_NOVA_VES_ZS, a domain that exists in
                    ONE school's catalog and nowhere else.

  documents (one metadata row + chunks each)
    ds-doc-long     ONE long document with THREE areas and three sections. Not
                    split, and its chunks inherit all three labels — so the
                    document is admissible in each area and the existing ranking
                    picks the passage. The test asserts admissibility and passage
                    choice SEPARATELY, because this version has no per-chunk
                    topical label and pretending otherwise would test a fiction.
    ds-doc-catering CATERING only, for the negative half of the same question.
    ds-doc-untagged no domain at all.
    ds-doc-private  visibility private, owner ds_user_owner.
    ds-doc-expired  valid_to in the past.
    ds-doc-stale    an OLD processing_uuid whose metadata row is is_active=false,
                    plus an active one — the superseded-version case.
    ds-doc-foreign  the other legal person's document.

Vectors are hand-built unit vectors (one hot dimension each), so cosine distance
is arithmetic rather than an API call: the vector steps can be tested with no
OPENAI_API_KEY and with an exactly predictable neighbour order.
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config                                    # noqa: E402
from app.db import get_cursor                             # noqa: E402
from app.settings import get_active_setting               # noqa: E402
from eduai_normalize import build_forms                   # noqa: E402

SCHEMA = config.PG_SCHEMA

# ── the fixture's names, all prefixed so teardown is exact ──────────────────
ORG = "SKOLA_NOVA_VES"
ZS = "SKOLA_NOVA_VES_ZS"
SOS = "SKOLA_NOVA_VES_SOS"
OTHER = "ZAKLADNI_SKOLA_LIPOVA"          # a different LEGAL PERSON
OWNER = "ds_user_owner"
STRANGER = "ds_user_stranger"

# The school-only domain. `DS_` prefix so it cannot collide with a real code, and
# owned by the ZŠ so resolved_taxonomy_rules gives it to that school ALONE.
ONLY_ZS_CODE = "DS_ONLY_NOVA_VES_ZS"

PACKS = ("ds-attendance", "ds-mixed", "ds-untagged", "ds-off", "ds-foreign",
         "ds-org-global", "ds-teacher", "ds-school-only")

# One embedding dimension per fixture row, so "nearest" is decidable on paper.
DIM = 1536


def _vec(slot: int) -> str:
    """A unit vector with 1.0 in `slot`. Distance to another slot is exactly 1."""
    v = ["0"] * DIM
    v[slot] = "1"
    return "[" + ",".join(v) + "]"


def _cfg() -> dict:
    """The ACTIVE settings config — the same normalization the cascade will use."""
    return get_active_setting(ZS)["config"]


def forms(text: str, lang: str = "cs") -> tuple[str, str, str]:
    """(norm_text, lemma_text, stem_text) exactly as prepare() would build them.

    The WHOLE settings config goes in, not its `normalization` sub-dict:
    eduai_normalize reads cfg["normalization"]["stopwords_by_lang"] and
    cfg["engines"] itself, and get_active_setting is what injects the stoplist
    from edu_ai.qa_stopword. Handing it the sub-dict raises — which is the same
    trap /v1/explain fell into by passing no cfg at all.
    """
    f = build_forms(text, _cfg(), lang)
    return (" ".join(f["norm"]), " ".join(f["lemma"]), " ".join(f["stem"]))


# ── deterministic ids ───────────────────────────────────────────────────────
def _u(name: str) -> str:
    """A stable UUID per fixture name, so build/drop and reruns agree."""
    return str(uuid.uuid5(uuid.UUID("00000000-0000-0000-0000-0000000d5c09"), name))


# ── the QA content ──────────────────────────────────────────────────────────
# Questions a fictional school could ask, each with a DISTINCTIVE content word so
# the lexical ranking cannot confuse two of them. `area` is what a curator would
# have decided; `pack` is where the row lives.
QA_ROWS = [
    # (key, pack, question, answer, entry areas or None = inherit pack)
    ("ds-att-1", "ds-attendance",
     "Jak omluvit vyšinutou absenci hlodavcem?",
     "Absenci omluvíte do tří dnů zápisem v žákovské knížce. (ds-att-1)", ["ATTENDANCE"]),
    ("ds-att-2", "ds-attendance",
     "Kdy se zamyká vrátnice pro pozdní příchod?",
     "Vrátnice se zamyká v 8:05. (ds-att-2)", ["ATTENDANCE"]),
    # THE MIXED PACK. Two entries, two DIFFERENT areas, one pack labelled both.
    ("ds-mix-att", "ds-mixed",
     "Kolik zameškaných hodin znamená kázeňské opatření?",
     "Nad 25 % zameškaných hodin následuje komisionální zkouška. (ds-mix-att)",
     ["ATTENDANCE"]),
    ("ds-mix-cat", "ds-mixed",
     "Do kdy se odhlašuje polévka z jídelníčku?",
     "Polévku odhlásíte do 7:30 předchozího dne. (ds-mix-cat)", ["CATERING"]),
    ("ds-untag-1", "ds-untagged",
     "Kde se odevzdává zapomenutá pláštěnka?",
     "Pláštěnku odevzdejte na sekretariátu. (ds-untag-1)", None),
    ("ds-off-1", "ds-off",
     "Jak se hlásí porouchaný kolotoč na hřišti?",
     "Nikdy se nesmí zobrazit — pack je neaktivní. (ds-off-1)", ["ATTENDANCE"]),
    ("ds-foreign-1", "ds-foreign",
     "Jak omluvit absenci v Lipové?",
     "Cizí organizace — nikdy se nesmí zobrazit. (ds-foreign-1)", ["ATTENDANCE"]),
    ("ds-orgglob-1", "ds-org-global",
     "Jaká je celoorganizační zásada pro omluvenky?",
     "Celá organizace omlouvá do tří dnů. (ds-orgglob-1)", ["ATTENDANCE"]),
    ("ds-teach-1", "ds-teacher",
     "Jak učitel eviduje neomluvenou hodinu v systému?",
     "Pouze pro učitele. (ds-teach-1)", ["ATTENDANCE"]),
    ("ds-only-1", "ds-school-only",
     "Jak se přihlásit na chodbový mikroskop?",
     "Mikroskop rezervujete u koordinátorky. (ds-only-1)", [ONLY_ZS_CODE]),
]

# ── the document content ────────────────────────────────────────────────────
# ds-doc-long is ONE document with three sections and THREE areas — the "velké
# dokumenty zůstávají celé" case. Each chunk carries all three labels.
LONG_SECTIONS = [
    ("Docházka a omluvenky",
     "Zákonný zástupce omlouvá nepřítomnost žáka nejpozději do tří kalendářních "
     "dnů. Neomluvená hodina se zapisuje do systému týž den. (ds-doc-long/dochazka)"),
    ("Školní jídelna",
     "Obědy se odhlašují nejpozději do sedmi třiceti předchozího pracovního dne. "
     "Neodhlášený oběd se účtuje v plné výši. (ds-doc-long/jidelna)"),
    ("Hodnocení a klasifikace",
     "Souhrnné hodnocení vychází z průběžné klasifikace za celé pololetí. "
     "Opravná zkouška se koná v posledním srpnovém týdnu. (ds-doc-long/hodnoceni)"),
]

DOCS = [
    # (key, entity, visibility, owner, roles, areas, valid_to, active, sections)
    ("ds-doc-long", ZS, "entity", None, ["STUDENT", "TEACHER", "PARENT"],
     ["ATTENDANCE", "CATERING", "GRADING"], None, True, LONG_SECTIONS),
    ("ds-doc-catering", ZS, "entity", None, ["STUDENT", "TEACHER", "PARENT"],
     ["CATERING"], None, True,
     [("Objednávání obědů",
       "Oběd si objednáte přes portál nejpozději den předem. (ds-doc-catering)")]),
    ("ds-doc-untagged", ZS, "entity", None, ["STUDENT", "TEACHER", "PARENT"],
     None, None, True,
     [("Ztráty a nálezy",
       "Nalezené věci ukládáme na sekretariát po dobu jednoho měsíce. "
       "(ds-doc-untagged)")]),
    ("ds-doc-private", ZS, "private", OWNER, None,
     ["ATTENDANCE"], None, True,
     [("Soukromá poznámka k docházce",
       "Soukromá poznámka o omluvenkách jednoho žáka. (ds-doc-private)")]),
    ("ds-doc-expired", ZS, "entity", None, ["STUDENT", "TEACHER", "PARENT"],
     ["ATTENDANCE"], "2020-01-01", True,
     [("Neplatná směrnice o docházce",
       "Zrušená pravidla omlouvání nepřítomnosti. (ds-doc-expired)")]),
    ("ds-doc-foreign", OTHER, "entity", None, ["STUDENT", "TEACHER", "PARENT"],
     ["ATTENDANCE"], None, True,
     [("Docházka v Lipové",
       "Cizí organizace omlouvá nepřítomnost jinak. (ds-doc-foreign)")]),
]


def _tax_id(cur, code: str, entity: str | None) -> int | None:
    cur.execute(
        f"SELECT id FROM {SCHEMA}.taxonomy_rules "
        "WHERE taxonomy_type_code = 'DOMAIN' AND is_active AND type_code = %s "
        "  AND legal_entity_code IS NOT DISTINCT FROM %s", (code, entity))
    row = cur.fetchone()
    return int(row[0]) if row else None


def domain_ids(cur, codes, entity: str | None) -> list[int] | None:
    """The ids of `codes` OWNED BY `entity` (None = the global rows)."""
    if codes is None:
        return None
    out = []
    for c in codes:
        i = _tax_id(cur, c, entity)
        if i is None:
            raise SystemExit(f"fixture: domain {c!r} has no active row for "
                             f"owner {entity!r} — cannot build")
        out.append(i)
    return out


def build() -> None:
    # CLEAN SLATE FIRST. Eight ON CONFLICT clauses would each have to name a
    # constraint that happens to exist on that table (qa_variants has none on
    # entry_id), and a rerun that silently skipped one row would leave the
    # fixture half-old — the least debuggable kind of test failure. drop() is
    # exact, so this is cheap.
    drop()
    with get_cursor() as cur:
        # ── the school-only domain ──────────────────────────────────────────
        # sort_order high so it cannot disturb the order of the seeded rows.
        cur.execute(
            f"INSERT INTO {SCHEMA}.taxonomy_rules "
            "(taxonomy_type_code, type_code, legal_entity_code, labels, "
            " llm_instruction, sort_order, is_active, search_role) "
            "VALUES ('DOMAIN', %s, %s, %s::jsonb, %s, 9001, true, 'normal')",
            (ONLY_ZS_CODE, ZS,
             '{"cs": "Chodbový mikroskop (fixture)", '
             '"en": "Corridor microscope (fixture)"}',
             "FIXTURE ONLY — never assign this domain; it exists so a test can "
             "prove a school-local code is invisible to other schools."))

        # ── users: an owner for the private document, and a stranger ────────
        for uid, name in ((OWNER, "Fixture Owner"), (STRANGER, "Fixture Stranger")):
            cur.execute(
                f"INSERT INTO {SCHEMA}.staff_user "
                "(user_id, legal_entity_code, display_name, is_active) "
                "VALUES (%s, %s, %s, true)",
                (uid, ZS, name))

        _zs_att = _tax_id(cur, "ATTENDANCE", ZS)
        _zs_cat = _tax_id(cur, "CATERING", ZS)
        _gl_att = _tax_id(cur, "ATTENDANCE", None)
        if not (_zs_att and _zs_cat and _gl_att):
            raise SystemExit("fixture: the seed's DOMAIN catalog is not as expected")
        # THE POINT OF THE WHOLE ID EXERCISE, asserted at build time so a
        # mis-seeded database fails here rather than as a puzzling test result.
        if _zs_att == _gl_att:
            raise SystemExit("fixture: ATTENDANCE has the same id globally and in "
                             "the ZŠ catalog — the id-resolution case cannot be tested")

        # ── packs ───────────────────────────────────────────────────────────
        # dom_owner = WHOSE taxonomy rows this pack's labels come from. It is a
        # separate axis from the pack's entity, and the fixture needs it to be:
        # SKOLA_NOVA_VES_ZS has its own DOMAIN catalog, while Lipová and the
        # umbrella organisation inherit the GLOBAL one (nearest-owner-exclusive),
        # so "the ATTENDANCE row of this entity" simply does not exist for them.
        # That asymmetry is the production shape and the reason the resolver has
        # to expand a code to several ids.
        # audience_roles is a HARD filter in scope.where — "an untagged row is a
        # defect, not for everyone" (migration 127) — so every pack that should
        # be reachable names all three roles explicitly. ds-teacher names one, and
        # that is the case the test needs: a PARENT choosing ATTENDANCE must not
        # reach it, because an area is a preference and never a permission.
        ALL_ROLES = ["STUDENT", "TEACHER", "PARENT"]
        packs = {
            #                  entity  visibility      owner  roles        dom_owner  domains              active
            "ds-attendance":  (ZS,    "entity",       None,  ALL_ROLES,   ZS,        [_zs_att],           True),
            "ds-mixed":       (ZS,    "entity",       None,  ALL_ROLES,   ZS,        [_zs_att, _zs_cat],  True),
            "ds-untagged":    (ZS,    "entity",       None,  ALL_ROLES,   ZS,        None,                True),
            "ds-off":         (ZS,    "entity",       None,  ALL_ROLES,   ZS,        [_zs_att],           False),
            "ds-foreign":     (OTHER, "entity",       None,  ALL_ROLES,   None,      [_gl_att],           True),
            "ds-org-global":  (ORG,   "organization", None,  ALL_ROLES,   None,      [_gl_att],           True),
            "ds-teacher":     (ZS,    "entity",       None,  ["TEACHER"], ZS,        [_zs_att],           True),
            "ds-school-only": (ZS,    "entity",       None,  ALL_ROLES,   ZS,
                               domain_ids(cur, [ONLY_ZS_CODE], ZS),                                       True),
        }
        for pack_id, (ent, vis, owner, roles, _dow, doms, active) in packs.items():
            cur.execute(
                f"INSERT INTO {SCHEMA}.qa_pack_manifest "
                "(pack_id, lang, version, schema_version, t_answer, t_suggest, "
                " m_margin, entry_count, is_active, legal_entity_code, visibility, "
                " owner_user_id, audience_roles, domain_ids) "
                "VALUES (%s, 'cs', 1, 1, 0.62, 0.35, 0.12, 0, %s, %s, %s, %s, %s, %s)",
                (pack_id, active, ent, vis, owner, roles, doms))

        # ── entries + variants + embeddings ─────────────────────────────────
        for slot, (key, pack_id, question, answer, areas) in enumerate(QA_ROWS):
            ent, vis, owner, roles, dom_owner, pack_doms, active = packs[pack_id]
            # the ENTRY's own areas, resolved against the pack's DOMAIN OWNER —
            # so the mixed pack's two entries carry that school's own two ids and
            # the foreign pack's entry carries the global one
            eds = domain_ids(cur, areas, dom_owner) if areas else pack_doms
            eid = _u(key)
            norm, lemma, stem = forms(question)
            cur.execute(
                f"INSERT INTO {SCHEMA}.qa_entries "
                "(entry_id, pack_id, entry_key, canonical_q, answer_md, is_active, "
                " legal_entity_code, visibility, owner_user_id, domain_ids, tags) "
                "VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s::jsonb)",
                (eid, pack_id, key, question, answer, ent, vis, owner, eds,
                 '{"category": "ds-fixture"}'))
            cur.execute(
                f"INSERT INTO {SCHEMA}.qa_variants "
                "(entry_id, phrasing, norm_text, lemma_text, stem_text, is_active, "
                " legal_entity_code, visibility, owner_user_id, audience_roles, "
                " domain_ids) "
                "VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s)",
                (eid, question, norm, lemma, stem, ent, vis, owner, roles, eds))
            cur.execute(
                f"SELECT variant_id FROM {SCHEMA}.qa_variants WHERE entry_id = %s "
                "ORDER BY variant_id LIMIT 1", (eid,))
            vid = cur.fetchone()[0]
            cur.execute(
                f"INSERT INTO {SCHEMA}.qa_embedding "
                "(variant_id, entry_id, embedding, embedding_model, is_active, "
                " legal_entity_code, visibility, owner_user_id, audience_roles, "
                " domain_ids, embedding_dimension) "
                "VALUES (%s, %s, %s::vector, %s, true, %s, %s, %s, %s, %s, %s)",
                (vid, eid, _vec(slot), config.EMBEDDING_MODEL, ent, vis, owner,
                 roles, eds, DIM))

        cur.execute(
            f"UPDATE {SCHEMA}.qa_pack_manifest m SET entry_count = ("
            f"  SELECT count(*) FROM {SCHEMA}.qa_entries e WHERE e.pack_id = m.pack_id)"
            " WHERE m.pack_id = ANY(%s)", (list(PACKS),))

        # ── documents ───────────────────────────────────────────────────────
        slot = 100
        for key, ent, vis, owner, roles, areas, valid_to, active, sections in DOCS:
            # ZŠ documents carry the ZŠ's own ids; Lipová has no catalog of its
            # own and carries the GLOBAL ones (see the dom_owner note above)
            eds = domain_ids(cur, areas, ZS if ent in (ZS, SOS) else None)
            fu, pu = _u(key + "/file"), _u(key + "/proc")
            cur.execute(
                f"INSERT INTO {SCHEMA}.document_metadata "
                "(file_uuid, processing_uuid, is_active, legal_entity_code, "
                " visibility, owner_user_id, original_file_name, source_url, "
                " document_domain_user, document_type_user, document_language_user) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'SKOLNI_RAD', 'cs')",
                (fu, pu, active, ent, vis, owner, key + ".md",
                 f"https://example.invalid/{key}.md", eds))
            for idx, (heading, text) in enumerate(sections):
                slot += 1
                n, l, s_ = forms(text)
                hn, hl, hs = forms(heading)
                cur.execute(
                    f"INSERT INTO {SCHEMA}.document_chunks "
                    "(processing_uuid, file_uuid, file_item_uuid, output_row_number, "
                    " chunk_index, chunk_text, chunk_norm_text, chunk_lemma_text, "
                    " chunk_stem_text, section_heading, section_heading_norm, "
                    " section_heading_lemma, section_heading_stem, embedding, "
                    " embedding_model, legal_entity_code, visibility, owner_user_id, "
                    " audience_roles, document_domain_id, valid_to, file_name, "
                    " langdetect_result) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "        %s::vector, %s, %s, %s, %s, %s, %s, %s, %s, 'cs')",
                    (pu, fu, _u(f"{key}/item/{idx}"), idx + 1, idx, text,
                     n, l, s_, heading, hn, hl, hs, _vec(slot),
                     config.EMBEDDING_MODEL, ent, vis, owner,
                     roles or ["STUDENT", "TEACHER", "PARENT"], eds, valid_to,
                     key + ".md"))

        # ── the superseded-version case ─────────────────────────────────────
        # ONE file, TWO processing runs: the active metadata row plus an OLD one
        # whose chunks must never be joined to it. This is what the
        # file_uuid-only join in route._corpus_topics used to allow.
        key = "ds-doc-stale"
        eds = domain_ids(cur, ["ATTENDANCE"], ZS)
        fu = _u(key + "/file")
        for tag, active, text in (("proc-old", False,
                                   "STARÁ verze: omluvenky se hlásily telefonem. "
                                   "(ds-doc-stale/old)"),
                                  ("proc-new", True,
                                   "Nová verze: omluvenky se hlásí v portálu. "
                                   "(ds-doc-stale/new)")):
            pu = _u(f"{key}/{tag}")
            cur.execute(
                f"INSERT INTO {SCHEMA}.document_metadata "
                "(file_uuid, processing_uuid, is_active, legal_entity_code, "
                " visibility, original_file_name, document_domain_user, "
                " document_type_user, document_language_user) "
                "VALUES (%s, %s, %s, %s, 'entity', %s, %s, 'SKOLNI_RAD', 'cs')",
                (fu, pu, active, ZS, key + ".md", eds))
            slot += 1
            n, l, s_ = forms(text)
            hn, hl, hs = forms("Ohlašování omluvenek")
            cur.execute(
                f"INSERT INTO {SCHEMA}.document_chunks "
                "(processing_uuid, file_uuid, file_item_uuid, output_row_number, "
                " chunk_index, chunk_text, chunk_norm_text, chunk_lemma_text, "
                " chunk_stem_text, section_heading, section_heading_norm, "
                " section_heading_lemma, section_heading_stem, embedding, "
                " embedding_model, legal_entity_code, visibility, audience_roles, "
                " document_domain_id, file_name, langdetect_result) "
                "VALUES (%s, %s, %s, 1, 0, %s, %s, %s, %s, %s, %s, %s, %s, "
                "        %s::vector, %s, %s, 'entity', %s, %s, %s, 'cs')",
                (pu, fu, _u(f"{key}/{tag}/item"), text, n, l, s_,
                 "Ohlašování omluvenek", hn, hl, hs, _vec(slot),
                 config.EMBEDDING_MODEL, ZS, ["STUDENT", "TEACHER", "PARENT"],
                 eds, key + ".md"))

    print("fixture: built")
    show()


def drop() -> None:
    """Remove EXACTLY what build() created. Safe to run twice."""
    keys = [k for k, *_ in QA_ROWS]
    entry_ids = [_u(k) for k in keys]
    doc_files = [_u(k + "/file") for k, *_ in DOCS] + [_u("ds-doc-stale/file")]
    with get_cursor() as cur:
        cur.execute(f"DELETE FROM {SCHEMA}.qa_embedding WHERE entry_id::text = ANY(%s)",
                    (entry_ids,))
        cur.execute(f"DELETE FROM {SCHEMA}.qa_variants WHERE entry_id::text = ANY(%s)",
                    (entry_ids,))
        cur.execute(f"DELETE FROM {SCHEMA}.qa_entries WHERE entry_id::text = ANY(%s)",
                    (entry_ids,))
        cur.execute(f"DELETE FROM {SCHEMA}.qa_pack_manifest WHERE pack_id = ANY(%s)",
                    (list(PACKS),))
        cur.execute(f"DELETE FROM {SCHEMA}.document_chunks WHERE file_uuid::text = ANY(%s)",
                    (doc_files,))
        cur.execute(f"DELETE FROM {SCHEMA}.document_metadata WHERE file_uuid::text = ANY(%s)",
                    (doc_files,))
        cur.execute(f"DELETE FROM {SCHEMA}.taxonomy_rules "
                    "WHERE taxonomy_type_code = 'DOMAIN' AND type_code = %s",
                    (ONLY_ZS_CODE,))
        cur.execute(f"DELETE FROM {SCHEMA}.staff_user WHERE user_id = ANY(%s)",
                    ([OWNER, STRANGER],))
        # the fixture's own query-log rows (the API tests write them)
        cur.execute(f"DELETE FROM {SCHEMA}.question_answer_log "
                    "WHERE query_id LIKE 'ds-fixture-%%'")
        cur.execute(f"DELETE FROM {SCHEMA}.idempotency_key WHERE key LIKE 'ds-fixture-%%'")
    print("fixture: dropped")


def show() -> None:
    with get_cursor() as cur:
        cur.execute(
            f"SELECT m.pack_id, m.is_active, m.legal_entity_code, m.visibility, "
            f"       m.audience_roles, m.domain_ids, count(e.entry_id) "
            f"FROM {SCHEMA}.qa_pack_manifest m "
            f"LEFT JOIN {SCHEMA}.qa_entries e ON e.pack_id = m.pack_id "
            "WHERE m.pack_id = ANY(%s) GROUP BY 1,2,3,4,5,6 ORDER BY 1",
            (list(PACKS),))
        print("\npack                 act entity                 vis          roles      domains        n")
        for r in cur.fetchall():
            print(f"{r[0]:20} {str(r[1])[0]:3} {str(r[2]):22} {str(r[3]):12} "
                  f"{str(r[4] or '-'):10} {str(r[5] or '-'):14} {r[6]}")
        cur.execute(
            f"SELECT m.original_file_name, m.is_active, m.legal_entity_code, "
            f"       m.visibility, m.document_domain_user, count(c.id) "
            f"FROM {SCHEMA}.document_metadata m "
            f"LEFT JOIN {SCHEMA}.document_chunks c "
            "  ON c.file_uuid = m.file_uuid AND c.processing_uuid = m.processing_uuid "
            "WHERE m.original_file_name LIKE 'ds-doc-%%' "
            "GROUP BY 1,2,3,4,5 ORDER BY 1,2")
        print("\ndocument                 act entity                 vis        domains         chunks")
        for r in cur.fetchall():
            print(f"{r[0]:24} {str(r[1])[0]:3} {str(r[2]):22} {str(r[3]):10} "
                  f"{str(r[4] or '-'):15} {r[5]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    {"build": build, "drop": drop, "show": show}[cmd]()
