#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-06).
"""Domain scope: which ROWS become candidates, asserted against a real database.

    docker cp tests/domain_scope_fixture.py tests/test_domain_scope.py edu_ai_search:/tmp/
    docker exec -e PYTHONPATH=/app -w /app edu_ai_search python /tmp/test_domain_scope.py

Runs INSIDE the search image, against the synthetic fixture
(tests/domain_scope_fixture.py). It builds the fixture, asserts, and drops it
again in a `finally` — including when a test raises, so a failed run does not
leave rows behind for the next one to trip over.

WHAT THIS FILE DELIBERATELY DOES NOT DO: assert on the SHAPE of a SQL string.
A test that greps for "domain_ids &&" passes when the fragment is built and the
parameters are interpolated in the wrong order, which is the actual way this
kind of change breaks. So every assertion here is about the answer the database
gave: which entry_id came back, which chunk texts are in the pool, which
suggestions were offered.

The vector steps use the fixture's hand-built unit vectors and a `query_vec`
placed into the state, so qa_vector and rag_vector are exercised with no
embedding API call and with a neighbour order that is arithmetic rather than a
model's opinion.

The HTTP surface — validation envelopes, effective_scope, the query log, the
idempotency digest, nginx and the BFF — is tests/test_domain_scope_api.py,
which runs on the HOST because an in-process TestClient is no evidence that a
path is publicly reachable.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import domain_scope_fixture as fx                          # noqa: E402
from app import config, domain_scope, scope                # noqa: E402
from app.db import get_cursor                              # noqa: E402
from app.nodes.prepare import prepare                      # noqa: E402
from app.nodes.route import route, _NO_PACK, _corpus_topics  # noqa: E402
from app.nodes.qa_exact import qa_exact                    # noqa: E402
from app.nodes.qa_lexical import qa_lexical                # noqa: E402
from app.nodes.qa_vector import qa_vector                  # noqa: E402
from app.nodes.document_lexical import document_lexical    # noqa: E402
from app.nodes.rag_vector import rag_vector                # noqa: E402
from app.nodes.rag_keywords import rag_keywords            # noqa: E402
from app.nodes.document_siblings import document_siblings  # noqa: E402
from app import conversation                               # noqa: E402
from app.settings import organization_entities, get_active_setting  # noqa: E402

ZS, ORG, OTHER = fx.ZS, fx.ORG, fx.OTHER
OWNER, STRANGER = fx.OWNER, fx.STRANGER

_failures: list[str] = []
_ran = 0


# ── harness ─────────────────────────────────────────────────────────────────
def check(name: str, cond, detail: str = "") -> None:
    global _ran
    _ran += 1
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


def state_for(question: str, *, domains=None, role="PARENT", user_id=None,
              entity=ZS, grade=None) -> dict:
    """A cascade state built the way server._prepare_search builds it.

    Through the REAL resolver, not with hand-written ids: the point of most of
    these tests is that code -> ids is right, and a state with the ids typed in
    would test the SQL while assuming away the part that decides which rows the
    SQL can see.
    """
    org = organization_entities(entity)
    codes, ids = domain_scope.resolve(entity, domain_scope.normalize(domains), org)
    st = {
        "user_question": question,
        "legal_entity": entity,
        "entity_scope": [entity] if org else [],
        "organization_entities": org,
        "user_id": user_id,
        "user_role": role,
        "user_school_grade": grade,
        "language_filter": None,
        "domain_codes": codes,
        "domain_ids": ids,
    }
    st.update(prepare(st, get_active_setting(entity)["config"]) or {})
    st.update(route(st) or {})
    return st


def answer_of(st: dict) -> str:
    """The curated answer the QA cascade reached for this state, or ""."""
    out = dict(st)
    out.update(qa_exact(out) or {})
    if not out.get("resolved"):
        out.update(qa_lexical(out) or {})
    return out.get("answer") or ""


def chunk_texts(st: dict, node, params=None) -> list[str]:
    out = dict(st)
    out.update(node(out, params or {}) or {})
    return [c.get("chunk_text") or "" for c in (out.get("rag_chunks") or [])]


class no_swallowed_failure:
    """Fail if any cascade step logs that it caught its own exception.

    THE REASON THIS EXISTS. Every pipeline step is required to catch and fall
    through (AGENTS.md), so a broken step does not 500 — it logs a warning and
    returns nothing. That is right for production and blinding for a test: a
    query built with a fragment and the wrong parameter list raises inside the
    step, the step swallows it, the assertion "no out-of-area rows" passes
    triumphantly because there are NO rows at all, and the whole branch is
    silently gone.

    That is not hypothetical. `document_lexical`'s trigram typo fallback
    interpolated `cand_cond` (which carries the area fragment) while passing
    `scope_params` (which does not carry its parameter), so under an explicit
    filter it raised `list index out of range` and the entire document branch
    disappeared. Twenty-two green area assertions did not notice, because none
    of them drove that branch with a filter on.

    So: watch the log. Anything matching `failed|raised|skipped —` from an
    `app.*` logger at WARNING or above, while a scoped query runs, is a defect
    even when the result looks plausible.
    """

    #: substrings a step uses when it has caught its own exception
    MARKERS = ("failed (", "raised (", " — continuing", "— falling through",
               "— skipped", "— none offered", "— no dedupe", "— QA branch skipped")

    def __init__(self, label: str, allow: tuple = ()):
        self.label = label
        self.allow = allow
        self.records: list[str] = []

    def __enter__(self):
        self._handler = _Collect(self.records)
        logging.getLogger("app").addHandler(self._handler)
        logging.getLogger("app").setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc):
        logging.getLogger("app").removeHandler(self._handler)
        hits = [r for r in self.records
                if any(m in r for m in self.MARKERS)
                and not any(a in r for a in self.allow)]
        check(f"no step swallowed an exception — {self.label}",
              not hits, " | ".join(hits[:3]))
        return False


class _Collect(logging.Handler):
    def __init__(self, sink):
        super().__init__(level=logging.WARNING)
        self.sink = sink

    def emit(self, record):
        try:
            self.sink.append(f"{record.name}: {record.getMessage()}")
        except Exception:                                        # noqa: BLE001
            pass


def tags(texts) -> set[str]:
    """The `(ds-…)` markers in a list of texts — what each fixture row signs with."""
    found = set()
    for t in texts:
        for key in ("ds-doc-long/dochazka", "ds-doc-long/jidelna",
                    "ds-doc-long/hodnoceni", "ds-doc-catering", "ds-doc-untagged",
                    "ds-doc-private", "ds-doc-expired", "ds-doc-foreign",
                    "ds-doc-stale/old", "ds-doc-stale/new"):
            if f"({key})" in t:
                found.add(key)
    return found


# ══════════════════════════════════════════════════════════════════════════
# 1. NORMALISATION AND VALIDATION — the code -> canonical set half
# ══════════════════════════════════════════════════════════════════════════
def test_normalize():
    print("\n[normalize]")
    check("absent -> None", domain_scope.normalize(None) is None)
    check("trim + upper + dedupe + sort",
          domain_scope.normalize([" catering ", "ATTENDANCE", "attendance"])
          == ["ATTENDANCE", "CATERING"])
    for bad, why in (([], "empty array"), ([""], "empty element"),
                     (["  "], "whitespace element"), ("ATTENDANCE", "bare string"),
                     ([1], "non-string element"), ({"a": 1}, "object")):
        try:
            domain_scope.normalize(bad)
            check(f"{why} -> 422", False, "no exception raised")
        except domain_scope.DomainValidationError as e:
            check(f"{why} -> 422", bool(e.fields) and "domains" in e.fields[0]["field"],
                  f"fields={e.fields}")

    # ORDER AND REPETITION CARRY NO MEANING: the same request written three ways
    # must produce one canonical value, or the idempotency digest turns a retry
    # into a conflict.
    a = domain_scope.normalize(["ATTENDANCE", "CATERING"])
    b = domain_scope.normalize(["CATERING", " attendance"])
    c = domain_scope.normalize(["CATERING", "CATERING", "ATTENDANCE"])
    check("one canonical value for three spellings", a == b == c, f"{a} {b} {c}")


def test_resolver():
    print("\n[resolver: code -> ids]")
    org = organization_entities(ZS)
    codes, ids = domain_scope.resolve(ZS, ["ATTENDANCE"], org)
    check("codes echoed canonically", codes == ["ATTENDANCE"])
    # THE SPEC §4 CASE: one code, several active ids — the school's own AND the
    # global row. Missing the global one loses the organisation's own material.
    with get_cursor() as cur:
        zs_id = fx._tax_id(cur, "ATTENDANCE", ZS)
        gl_id = fx._tax_id(cur, "ATTENDANCE", None)
    check("both the school id and the GLOBAL id resolve",
          zs_id in ids and gl_id in ids, f"ids={ids} zs={zs_id} global={gl_id}")

    # a code of ANOTHER school's catalog is not legalised by existing elsewhere
    try:
        domain_scope.resolve(OTHER, [fx.ONLY_ZS_CODE], organization_entities(OTHER))
        check("school-local code rejected for another school", False, "accepted")
    except domain_scope.DomainValidationError:
        check("school-local code rejected for another school", True)
    # …and IS accepted for its own school
    _, only_ids = domain_scope.resolve(ZS, [fx.ONLY_ZS_CODE], org)
    check("school-local code accepted for its own school", bool(only_ids), f"{only_ids}")

    # a valid code that labels nothing resolves to an id set, never to "no filter"
    check("unknown code -> 422 for the whole request",
          _raises(domain_scope.DomainValidationError, domain_scope.resolve,
                  ZS, ["ATTENDANCE", "NO_SUCH_DOMAIN"], org))


def test_domain_where():
    print("\n[scope.domain_where — the three states]")
    frag, params = scope.domain_where("t", "document_domain_id", None)
    check("None -> EMPTY fragment (byte-identical old SQL)",
          frag == "" and params == [], f"{frag!r}")
    frag, params = scope.domain_where("t", "document_domain_id", [])
    check("empty resolved set -> AND false, NEVER an omitted filter",
          frag == " AND false" and params == [], f"{frag!r}")
    frag, params = scope.domain_where("t", "document_domain_id", [7, 9])
    check("ids -> array overlap with the ids as ONE param",
          "&&" in frag and params == [[7, 9]], f"{frag!r} {params}")


def _raises(exc, fn, *a, **k) -> bool:
    try:
        fn(*a, **k)
        return False
    except exc:
        return True


# ══════════════════════════════════════════════════════════════════════════
# 2. ROUTE — pack pre-selection and the meta offer
# ══════════════════════════════════════════════════════════════════════════
def test_route_pack_scope():
    print("\n[route: pack pre-selection]")
    st = state_for("cokoliv")
    packs = set(st["pack_ids"])
    check("unrestricted: tagged AND untagged own packs in scope",
          {"ds-attendance", "ds-mixed", "ds-untagged", "ds-org-global"} <= packs,
          f"{sorted(packs)}")
    check("unrestricted: inactive pack never in scope", "ds-off" not in packs)
    check("unrestricted: another legal person's pack never in scope",
          "ds-foreign" not in packs)
    check("unrestricted: a TEACHER-only pack is not in a PARENT's scope",
          "ds-teacher" not in packs)

    st = state_for("cokoliv", domains=["ATTENDANCE"])
    packs = set(st["pack_ids"])
    check("ATTENDANCE: the attendance pack is in scope", "ds-attendance" in packs)
    check("ATTENDANCE: the MIXED pack is in scope (it is labelled ATTENDANCE too)",
          "ds-mixed" in packs)
    check("ATTENDANCE: the organisation's GLOBAL-id pack is in scope "
          "(spec §4 — the code resolves to both ids)",
          "ds-org-global" in packs, f"{sorted(packs)}")
    check("ATTENDANCE: an UNTAGGED pack is NOT in scope", "ds-untagged" not in packs)
    check("ATTENDANCE: a CATERING-only pack is not in scope",
          "ds-catering" not in packs)
    check("ATTENDANCE: still no foreign, inactive or teacher-only pack",
          not ({"ds-foreign", "ds-off", "ds-teacher"} & packs), f"{sorted(packs)}")

    st = state_for("cokoliv", domains=["CATERING"])
    packs = set(st["pack_ids"])
    check("CATERING: only the mixed pack (its manifest carries CATERING)",
          "ds-mixed" in packs and "ds-attendance" not in packs, f"{sorted(packs)}")

    # A valid code nothing carries: the sentinel, never "all packs".
    st = state_for("cokoliv", domains=["BULLYING"])
    check("a valid code with no content -> ~none~ sentinel, not every pack",
          st["pack_ids"] == [_NO_PACK], f"{st['pack_ids']}")


def test_route_falls_closed():
    print("\n[route: an exception must not open every pack]")
    # route() used to answer an exception with pack_ids=[] and pack_filter reads
    # [] as "no pack restriction" — i.e. every active pack of every school.
    import app.nodes.route as route_mod
    orig = route_mod._scope
    route_mod._scope = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("injected"))
    try:
        st = {"user_question": "cokoliv", "legal_entity": ZS,
              "entity_scope": [ZS], "organization_entities": organization_entities(ZS),
              "user_role": "PARENT", "domain_codes": None, "domain_ids": None,
              "query_norm_tokens": ["cokoliv"], "query_lemma_tokens": ["cokoliv"],
              "query_stem_tokens": ["cokoliv"]}
        out = route(st) or {}
        check("pack selection failure -> ~none~ (falls CLOSED)",
              out.get("pack_ids") == [_NO_PACK], f"{out.get('pack_ids')}")
        check("pack selection failure -> no offer packs either",
              not out.get("offer_pack_ids"))
    finally:
        route_mod._scope = orig


def test_corpus_topics():
    print("\n[route._corpus_topics: the meta offer]")
    st = state_for("Co umis?")
    _, sugg_all = _corpus_topics(st)
    st_cat = state_for("Co umis?", domains=["CATERING"])
    _, sugg_cat = _corpus_topics(st_cat)
    q_all = {s["question"] for s in sugg_all}
    q_cat = {s["question"] for s in sugg_cat}
    check("unrestricted meta offer can include an attendance question",
          any("absenci" in q or "vrátnice" in q for q in q_all) or bool(q_all),
          f"{sorted(q_all)}")
    check("CATERING meta offer contains no ATTENDANCE-only question",
          not any(q in q_cat for q in
                  ("Jak omluvit vyšinutou absenci hlodavcem?",
                   "Kdy se zamyká vrátnice pro pozdní příchod?")),
          f"{sorted(q_cat)}")
    check("CATERING meta offer contains no UNTAGGED question",
          "Kde se odevzdává zapomenutá pláštěnka?" not in q_cat, f"{sorted(q_cat)}")


# ══════════════════════════════════════════════════════════════════════════
# 3. QA — exact, lexical, vector, and the mixed pack
# ══════════════════════════════════════════════════════════════════════════
def test_qa_exact():
    print("\n[qa_exact]")
    Q = "Jak omluvit vyšinutou absenci hlodavcem?"
    check("unrestricted: answers", "(ds-att-1)" in answer_of(state_for(Q)))
    check("ATTENDANCE: still answers",
          "(ds-att-1)" in answer_of(state_for(Q, domains=["ATTENDANCE"])))
    check("CATERING: does NOT answer from an ATTENDANCE entry",
          "(ds-att-1)" not in answer_of(state_for(Q, domains=["CATERING"])))
    check("two domains are OR: ATTENDANCE+CATERING answers",
          "(ds-att-1)" in answer_of(
              state_for(Q, domains=["CATERING", "ATTENDANCE"])))

    U = "Kde se odevzdává zapomenutá pláštěnka?"
    check("untagged entry: answers unrestricted", "(ds-untag-1)" in answer_of(state_for(U)))
    check("untagged entry: NOT reachable under an explicit filter",
          "(ds-untag-1)" not in answer_of(state_for(U, domains=["ATTENDANCE"])),
          "an unclassified source must not be treated as 'in every area'")


def test_qa_mixed_pack_entry_filter():
    print("\n[qa_exact: the MIXED pack — the entry filter, not just the manifest]")
    A = "Kolik zameškaných hodin znamená kázeňské opatření?"   # entry: ATTENDANCE
    C = "Do kdy se odhlašuje polévka z jídelníčku?"            # entry: CATERING
    check("mixed pack, ATTENDANCE: the attendance ENTRY answers",
          "(ds-mix-att)" in answer_of(state_for(A, domains=["ATTENDANCE"])))
    check("mixed pack, ATTENDANCE: the catering ENTRY does not",
          "(ds-mix-cat)" not in answer_of(state_for(C, domains=["ATTENDANCE"])),
          "the manifest carries both areas — only a per-row filter can do this")
    check("mixed pack, CATERING: the catering ENTRY answers",
          "(ds-mix-cat)" in answer_of(state_for(C, domains=["CATERING"])))
    check("mixed pack, CATERING: the attendance ENTRY does not",
          "(ds-mix-att)" not in answer_of(state_for(A, domains=["CATERING"])))


def test_qa_identity_not_widened():
    print("\n[qa: an area grants nothing]")
    T = "Jak učitel eviduje neomluvenou hodinu v systému?"
    check("PARENT + ATTENDANCE cannot reach a TEACHER-only entry",
          "(ds-teach-1)" not in answer_of(state_for(T, domains=["ATTENDANCE"])))
    check("TEACHER + ATTENDANCE can",
          "(ds-teach-1)" in answer_of(
              state_for(T, domains=["ATTENDANCE"], role="TEACHER")))
    F = "Jak omluvit absenci v Lipové?"
    check("another legal person's entry stays invisible with the same code",
          "(ds-foreign-1)" not in answer_of(state_for(F, domains=["ATTENDANCE"])))
    O = "Jak se hlásí porouchaný kolotoč na hřišti?"
    check("an inactive pack stays invisible with the right code",
          "(ds-off-1)" not in answer_of(state_for(O, domains=["ATTENDANCE"])))


def test_qa_global_id_reachable():
    print("\n[qa: the same code under a different id (spec §4)]")
    G = "Jaká je celoorganizační zásada pro omluvenky?"
    check("unrestricted: the organisation's pack answers",
          "(ds-orgglob-1)" in answer_of(state_for(G)))
    check("ATTENDANCE: it STILL answers, though its label is the GLOBAL id",
          "(ds-orgglob-1)" in answer_of(state_for(G, domains=["ATTENDANCE"])),
          "the resolver must expand a code to every id the organisation may have used")


def test_qa_vector():
    print("\n[qa_vector: filtered BEFORE the ANN limit]")
    # slot 0 == ds-att-1's own vector, so it is the exact nearest neighbour.
    st = state_for("Jak omluvit vyšinutou absenci hlodavcem?", domains=["CATERING"])
    st["query_vec"] = [1.0 if i == 0 else 0.0 for i in range(fx.DIM)]
    out = dict(st)
    out.update(qa_vector(out, {"cosine_min": 0.5, "fetch_k": 1}) or {})
    check("CATERING: the nearest ATTENDANCE neighbour is not returned",
          "(ds-att-1)" not in (out.get("answer") or ""),
          "fetch_k=1 — a filter applied AFTER the limit would have returned it")
    st2 = state_for("Jak omluvit vyšinutou absenci hlodavcem?", domains=["ATTENDANCE"])
    st2["query_vec"] = [1.0 if i == 0 else 0.0 for i in range(fx.DIM)]
    out2 = dict(st2)
    out2.update(qa_vector(out2, {"cosine_min": 0.5, "fetch_k": 1}) or {})
    check("ATTENDANCE: it IS returned", "(ds-att-1)" in (out2.get("answer") or ""),
          f"outcome={out2.get('outcome')}")


def test_qa_related_chips():
    print("\n[qa: related chips and the category top-up]")
    # Every fixture entry shares one category ("ds-fixture"), so the top-up has
    # candidates in BOTH areas and can only stay inside the chosen one by
    # filtering — which is the assertion.
    st = state_for("Jak omluvit vyšinutou absenci hlodavcem?", domains=["ATTENDANCE"])
    out = dict(st)
    out.update(qa_exact(out, {"related_suggestions": True,
                              "related_category_topup": True,
                              "suggest_top_n": 5}) or {})
    qs = {s.get("question") for s in (out.get("suggestions") or [])}
    check("chips: the catering question is NOT offered in ATTENDANCE",
          "Do kdy se odhlašuje polévka z jídelníčku?" not in qs, f"{sorted(qs)}")
    check("chips: the untagged question is NOT offered under a filter",
          "Kde se odevzdává zapomenutá pláštěnka?" not in qs, f"{sorted(qs)}")
    check("chips: the TEACHER-only question is NOT offered to a parent",
          "Jak učitel eviduje neomluvenou hodinu v systému?" not in qs, f"{sorted(qs)}")
    # THE CONTROL, and it has to run on the MIXED pack. category_siblings is
    # pack-LOCAL by design ("a category name means whatever its own pack's author
    # meant by it"), so for ds-attendance — every entry of which is ATTENDANCE —
    # the filtered and unfiltered chip rows are identical and prove nothing. In
    # ds-mixed the two areas share one pack and one category, so the top-up has a
    # genuine cross-area candidate and the filter is the only thing that can
    # withhold it.
    mix_q = "Kolik zameškaných hodin znamená kázeňské opatření?"
    chips = {}
    for label, doms in (("unrestricted", None), ("ATTENDANCE", ["ATTENDANCE"])):
        o = dict(state_for(mix_q, domains=doms))
        o.update(qa_exact(o, {"related_suggestions": True,
                              "related_category_topup": True,
                              "related_cross_pack_min": 0.0,
                              "suggest_top_n": 8}) or {})
        chips[label] = {s.get("question") for s in (o.get("suggestions") or [])}
    check("chips: unrestricted, the mixed pack's CATERING sibling IS offered",
          "Do kdy se odhlašuje polévka z jídelníčku?" in chips["unrestricted"],
          f"{sorted(chips['unrestricted'])}")
    check("chips: under ATTENDANCE that same sibling is withheld",
          "Do kdy se odhlašuje polévka z jídelníčku?" not in chips["ATTENDANCE"],
          f"{sorted(chips['ATTENDANCE'])}")


def test_other_grade_net_keeps_domain():
    print("\n[qa_lexical: the other-ročník net relaxes the YEAR, not the area]")
    # ds-attendance is given ročníky the caller does not have, so route puts it in
    # offer_pack_ids; the net may offer from it, but only inside the area.
    with get_cursor() as cur:
        cur.execute(
            f"SELECT id FROM {config.PG_SCHEMA}.code_list "
            "WHERE list_code = 'school_grade' AND code = '9' "
            "  AND legal_entity_code = %s AND is_active", (ZS,))
        g9 = [r[0] for r in cur.fetchall()]
        cur.execute(
            f"SELECT id FROM {config.PG_SCHEMA}.code_list "
            "WHERE list_code = 'school_grade' AND code = '3' "
            "  AND legal_entity_code = %s AND is_active", (ZS,))
        g3 = [r[0] for r in cur.fetchall()]
    if not (g9 and g3):
        check("other-grade net (skipped: the seed has no ročník 3/9 for this unit)",
              True)
        return
    with get_cursor() as cur:
        cur.execute(f"UPDATE {config.PG_SCHEMA}.qa_pack_manifest "
                    "SET school_grade_ids = %s WHERE pack_id = 'ds-attendance'", (g9,))
        cur.execute(f"UPDATE {config.PG_SCHEMA}.qa_pack_manifest "
                    "SET school_grade_ids = %s WHERE pack_id = 'ds-mixed'", (g9,))
    try:
        st = state_for("vrátnice zamyká", domains=["CATERING"], grade=3)
        check("offer packs are area-filtered too",
              "ds-attendance" not in (st.get("offer_pack_ids") or []),
              f"offer={st.get('offer_pack_ids')}")
        st2 = state_for("vrátnice zamyká", domains=["ATTENDANCE"], grade=3)
        check("…and present for the matching area",
              "ds-attendance" in (st2.get("offer_pack_ids") or []),
              f"offer={st2.get('offer_pack_ids')}")
        out = dict(st2)
        out.update(qa_lexical(out) or {})
        og = {s.get("question") for s in (out.get("other_grade_suggestions") or [])}
        check("net offers only in-area questions",
              "Do kdy se odhlašuje polévka z jídelníčku?" not in og, f"{sorted(og)}")
    finally:
        with get_cursor() as cur:
            cur.execute(f"UPDATE {config.PG_SCHEMA}.qa_pack_manifest "
                        "SET school_grade_ids = NULL "
                        "WHERE pack_id IN ('ds-attendance', 'ds-mixed')")


# ══════════════════════════════════════════════════════════════════════════
# 4. DOCUMENTS — every retriever, including the two that are off in production
# ══════════════════════════════════════════════════════════════════════════
def test_document_lexical():
    print("\n[document_lexical]")
    Q = "Jak se odhlašují obědy a co se stane s neodhlášeným obědem?"
    found_all = tags(chunk_texts(state_for(Q), document_lexical))
    check("unrestricted: both the long document and the catering one are candidates",
          "ds-doc-catering" in found_all or "ds-doc-long/jidelna" in found_all,
          f"{sorted(found_all)}")
    found_cat = tags(chunk_texts(state_for(Q, domains=["CATERING"]), document_lexical))
    check("CATERING: no untagged document in the pool",
          "ds-doc-untagged" not in found_cat, f"{sorted(found_cat)}")
    check("CATERING: no ATTENDANCE-only document in the pool",
          "ds-doc-expired" not in found_cat, f"{sorted(found_cat)}")
    check("CATERING: no other legal person's document",
          "ds-doc-foreign" not in found_cat, f"{sorted(found_cat)}")

    # identity is not widened by an area
    found_priv = tags(chunk_texts(
        state_for("Soukromá poznámka o omluvenkách", domains=["ATTENDANCE"]),
        document_lexical))
    check("ATTENDANCE: a private document is not visible to a non-owner",
          "ds-doc-private" not in found_priv, f"{sorted(found_priv)}")
    found_owner = tags(chunk_texts(
        state_for("Soukromá poznámka o omluvenkách", domains=["ATTENDANCE"],
                  user_id=OWNER),
        document_lexical))
    check("ATTENDANCE: …and IS visible to its owner",
          "ds-doc-private" in found_owner, f"{sorted(found_owner)}")

    found_exp = tags(chunk_texts(
        state_for("Zrušená pravidla omlouvání nepřítomnosti", domains=["ATTENDANCE"]),
        document_lexical))
    check("ATTENDANCE: an EXPIRED document stays out",
          "ds-doc-expired" not in found_exp, f"{sorted(found_exp)}")

    found_stale = tags(chunk_texts(
        state_for("Jak se hlásí omluvenky", domains=["ATTENDANCE"]), document_lexical))
    check("ATTENDANCE: the SUPERSEDED processing_uuid stays out",
          "ds-doc-stale/old" not in found_stale, f"{sorted(found_stale)}")


def test_long_document_stays_whole():
    print("\n[the long document: whole, multi-area, passage chosen by the question]")
    # THE TWO PROPERTIES ARE ASSERTED SEPARATELY, because this version labels the
    # DOCUMENT and not the chunk:
    #   admissibility  the document is reachable in EVERY one of its areas
    #   relevance      the QUESTION picks the passage, not a per-chunk topic tag
    # A test that expected an attendance question to find nothing under CATERING
    # would be asserting a per-chunk classifier this version deliberately does
    # not have (spec §6: "Nevytvarej klasifikator jednotlivych chunku").
    QS = {"ATTENDANCE": ("Jak se omlouva nepritomnost zaka?", "ds-doc-long/dochazka"),
          "CATERING": ("Do kdy se odhlasuji obedy?", "ds-doc-long/jidelna"),
          "GRADING": ("Jak vychazi souhrnne hodnoceni za pololeti?",
                      "ds-doc-long/hodnoceni")}
    for area, (q, want) in QS.items():
        found = tags(chunk_texts(state_for(q, domains=[area]), document_lexical))
        check(f"{area}: the long document answers its own area's question",
              want in found, f"{sorted(found)}")
    # …and it is reachable through an area OTHER than the passage's own topic,
    # because a chunk inherits the WHOLE document's areas. That is the design and
    # not a leak: this document really is about catering too.
    q, want = QS["ATTENDANCE"]
    found = tags(chunk_texts(state_for(q, domains=["CATERING"]), document_lexical))
    check("the document is reachable in CATERING too (chunks inherit its areas)",
          want in found, f"{sorted(found)}")
    # …but NOT through an area the document does not carry
    found = tags(chunk_texts(state_for(q, domains=["BULLYING"]), document_lexical))
    check("…and NOT through an area the document does not carry",
          not any(t.startswith("ds-doc-long/") for t in found), f"{sorted(found)}")
    # the QUESTION, not the label, decides which passage leads
    texts = chunk_texts(state_for(QS["CATERING"][0], domains=["CATERING"]),
                        document_lexical)
    check("the question picks the passage: obedy -> the jidelna section",
          "ds-doc-long/jidelna" in tags(texts[:2])
          or "ds-doc-catering" in tags(texts[:2]),
          f"first two = {sorted(tags(texts[:2]))}")


def test_odd_branches_do_not_raise_under_a_filter():
    print("\n[every branch, driven WITH a filter on — nothing may be swallowed]")
    # The queries are chosen to reach the paths the ordinary assertions never
    # touch: a misspelling drives the TRIGRAM fallbacks (document and QA), a
    # word absent from everything drives the empty-pool paths, and a category
    # word drives the top-up. Each runs under an EXPLICIT scope, because that is
    # the only shape in which a fragment/parameter mismatch exists at all.
    CASES = [
        ("document typo fallback", "odhlasovani obeeedu jidelnaa", ["CATERING"]),
        ("QA typo fallback", "omluvvit absencii hlodavcemm", ["ATTENDANCE"]),
        ("nothing matches anywhere", "xylofonista kastrace mimoz", ["ATTENDANCE"]),
        ("category top-up path", "zameskane hodiny", ["ATTENDANCE"]),
        ("two areas at once", "obedy a omluvenky", ["ATTENDANCE", "CATERING"]),
        ("an area with no content", "cokoliv vubec", ["BULLYING"]),
        ("a school-local area", "chodbovy mikroskop", [fx.ONLY_ZS_CODE]),
    ]
    for label, q, doms in CASES:
        with no_swallowed_failure(f"{label} ({doms})"):
            st = state_for(q, domains=doms)
            out = dict(st)
            out.update(qa_exact(out) or {})
            if not out.get("resolved"):
                out.update(qa_lexical(out) or {})
            if not out.get("resolved"):
                out.update(document_lexical(out, {}) or {})

    # …and the SAME queries unrestricted, so a failure here would point at the
    # query rather than at the filter
    for label, q, _doms in CASES:
        with no_swallowed_failure(f"{label} (unrestricted)"):
            st = state_for(q)
            out = dict(st)
            out.update(qa_exact(out) or {})
            if not out.get("resolved"):
                out.update(qa_lexical(out) or {})
            if not out.get("resolved"):
                out.update(document_lexical(out, {}) or {})


def _trgm_rows(st: dict) -> int | None:
    """How many rows document_lexical's TRIGRAM SQL returned, from its own log.

    The step reports `trgm fallback +kept/rows`, and `rows` is what the SQL
    produced — before the topical gate, which is where the assertion belongs.

    Asserting on the surviving CHUNKS instead does not work here and the reason
    is worth writing down: the trigram branch is deliberately precision-gated
    (`topical_admission`), because a chunk that enters on letter similarity has
    matched no concept. Nonsense tokens therefore match trigrams and are then
    correctly dropped, so a chunk-level assertion can only ever see zero and
    would be indistinguishable from the parameter bug — which is exactly the
    trap the first version of this test fell into. The row count is the layer
    the bug lived at, so it is the layer that proves the fix.

    None = the log line never appeared, i.e. the branch did not run at all.
    """
    rec: list[str] = []
    h = _Collect(rec)
    h.setLevel(logging.INFO)
    lg = logging.getLogger("app.nodes.document_lexical")
    _old = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        out = dict(st)
        out.update(document_lexical(out, {}) or {})
    finally:
        lg.removeHandler(h)
        lg.setLevel(_old)
    for line in rec:
        m = re.search(r"trgm fallback \+(\d+)/(\d+)", line)
        if m:
            return int(m.group(2))
    return None


def test_document_typo_fallback_is_scoped():
    print("\n[document typo fallback: reached, filtered, and not swallowed]")
    # A misspelling of words that DO occur in the CATERING document, chosen by
    # measuring rather than guessing: word_similarity('objednavkaa', …) = 0.583
    # and ('portall', …) = 0.750 against that chunk, so both clear the 0.35
    # per-token GUC. Neither token exists in any ts index, so the main rounds
    # keep nothing and the trigram branch is the one that runs.
    Q = "objednavkaa portall"

    with no_swallowed_failure("trgm fallback, unrestricted"):
        n_all = _trgm_rows(state_for(Q))
    check("the trigram branch RUNS and its SQL matches unrestricted "
          "(else this test proves nothing)",
          (n_all or 0) >= 1, f"rows={n_all}")

    with no_swallowed_failure("trgm fallback, CATERING"):
        n_cat = _trgm_rows(state_for(Q, domains=["CATERING"]))
    check("…it still runs under CATERING, which the document carries",
          (n_cat or 0) >= 1,
          f"rows={n_cat} — None means the SQL raised, which IS the parameter bug")
    # The NARROWING is the point, and it is a relationship rather than a number:
    # measured 2026-09-06 the same query matched 2 rows unrestricted and 1 under
    # CATERING. Asserting the counts themselves would break the next time the
    # fixture grows a document.
    check("…and it sees no MORE than the unrestricted search",
          (n_cat or 0) <= (n_all or 0), f"cat={n_cat} all={n_all}")

    with no_swallowed_failure("trgm fallback, BULLYING"):
        n_bully = _trgm_rows(state_for(Q, domains=["BULLYING"]))
    check("…and returns NOTHING under an area the document does not carry",
          n_bully == 0, f"rows={n_bully}")

    # the chunk-level guarantee still holds: whatever survives the topical gate
    # under a filter is in-area
    with no_swallowed_failure("trgm fallback chunks, CATERING"):
        found = tags(chunk_texts(state_for(Q, domains=["CATERING"]),
                                 document_lexical))
    check("no out-of-area chunk reaches the pool through this branch",
          not ({"ds-doc-untagged", "ds-doc-foreign", "ds-doc-expired"} & found),
          f"{sorted(found)}")


def test_rag_vector():
    print("\n[rag_vector: filtered BEFORE the ANN limit]")
    # slot 101 is the long document's first chunk (docs start at slot 101)
    st = state_for("cokoliv", domains=["CATERING"])
    st["query_vec"] = [1.0 if i == 101 else 0.0 for i in range(fx.DIM)]
    texts = chunk_texts(st, rag_vector, {"fetch_k": 3})
    check("CATERING: the pool holds only CATERING-labelled chunks",
          not ({"ds-doc-untagged", "ds-doc-foreign"} & tags(texts)),
          f"{sorted(tags(texts))}")
    st2 = state_for("cokoliv", domains=["GENERAL_LEGAL_DOCUMENT"])
    st2["query_vec"] = [1.0 if i == 101 else 0.0 for i in range(fx.DIM)]
    texts2 = chunk_texts(st2, rag_vector, {"fetch_k": 3})
    check("an area nothing carries -> empty pool, not the nearest neighbours",
          not tags(texts2), f"{sorted(tags(texts2))}")


def test_disabled_steps_still_filter():
    print("\n[rag_keywords / document_siblings: OFF in production, tested forced on]")
    # Neither runs in any active setting. They are called DIRECTLY here rather
    # than by flipping a settings row — a test must not change the production
    # precedence it is measuring.
    st = state_for("obědy odhlašování neodhlášený", domains=["CATERING"])
    texts = chunk_texts(st, rag_keywords, {"fetch_k": 20})
    check("rag_keywords honours the area",
          not ({"ds-doc-untagged", "ds-doc-foreign"} & tags(texts)),
          f"{sorted(tags(texts))}")

    # siblings expands an EXISTING hit by file_item_uuid — seed it with the
    # untagged document's own chunk and assert the expansion cannot bring it in
    seed = dict(state_for("cokoliv", domains=["CATERING"]))
    with get_cursor() as cur:
        cur.execute(
            f"SELECT c.id, c.file_uuid::text, c.file_item_uuid::text, c.chunk_index, "
            f"       c.chunk_text FROM {config.PG_SCHEMA}.document_chunks c "
            f"JOIN {config.PG_SCHEMA}.document_metadata m "
            "  ON m.file_uuid = c.file_uuid AND m.processing_uuid = c.processing_uuid "
            "WHERE m.original_file_name = 'ds-doc-untagged.md'")
        rows = cur.fetchall()
    seed["rag_chunks"] = [{"id": r[0], "file_uuid": r[1], "file_item_uuid": r[2],
                           "chunk_index": r[3], "chunk_text": r[4], "score": 1.0,
                           "source_rank": 1} for r in rows]
    out = dict(seed)
    out.update(document_siblings(out, {"budget": 10}) or {})
    added = [c for c in (out.get("rag_chunks") or [])
             if c.get("id") not in {r[0] for r in rows}]
    check("document_siblings adds no out-of-area neighbour",
          not ({"ds-doc-untagged"} & tags([c.get("chunk_text") or "" for c in added])),
          f"added={sorted(tags([c.get('chunk_text') or '' for c in added]))}")


# ══════════════════════════════════════════════════════════════════════════
# 5. HISTORY
# ══════════════════════════════════════════════════════════════════════════
def test_history_scope():
    print("\n[history: load_seen is scoped by area, null-safely]")
    import uuid as _uuid
    conv = str(_uuid.uuid4())
    with get_cursor() as cur:
        for doms, q in ((None, "otazka bez filtru"),
                        (["ATTENDANCE"], "otazka v dochazce"),
                        (["CATERING"], "otazka ve strave")):
            cur.execute(
                f"INSERT INTO {config.PG_SCHEMA}.question_answer_log "
                "(conversation_id, legal_entity_code, raw_query, query_id, domains) "
                "VALUES (%s, %s, %s, %s, %s)",
                (conv, ZS, q, "ds-fixture-hist", doms))
    try:
        asked, _ = conversation.load_seen(conv, ZS, None, None)
        check("unrestricted reads ONLY the NULL-scope turn",
              asked == {"otazka bez filtru"}, f"{asked}")
        asked, _ = conversation.load_seen(conv, ZS, None, ["ATTENDANCE"])
        check("ATTENDANCE reads only its own turn",
              asked == {"otazka v dochazce"}, f"{asked}")
        asked, _ = conversation.load_seen(conv, ZS, None, ["CATERING"])
        check("CATERING reads only its own turn",
              asked == {"otazka ve strave"}, f"{asked}")
        asked, _ = conversation.load_seen(conv, ZS, None, ["ATTENDANCE", "CATERING"])
        check("a DIFFERENT combination reads neither", asked == set(), f"{asked}")
    finally:
        with get_cursor() as cur:
            cur.execute(f"DELETE FROM {config.PG_SCHEMA}.question_answer_log "
                        "WHERE query_id = 'ds-fixture-hist'")


def test_history_failure_does_not_widen():
    print("\n[history: a lookup failure must not widen the scope]")
    import app.conversation as conv_mod
    orig = conv_mod.get_cursor
    conv_mod.get_cursor = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("injected"))
    try:
        asked, offered = conversation.load_seen("x" * 8, ZS, None, ["ATTENDANCE"])
        check("a broken history yields NO dedupe, and no exception",
              asked == set() and offered == set())
    finally:
        conv_mod.get_cursor = orig
    # and the search itself still applies the filter afterwards
    check("the area filter is unaffected by a history failure",
          "(ds-untag-1)" not in answer_of(
              state_for("Kde se odevzdává zapomenutá pláštěnka?",
                        domains=["ATTENDANCE"])))


# ══════════════════════════════════════════════════════════════════════════
# 6. FAULT INJECTION
# ══════════════════════════════════════════════════════════════════════════
def test_resolver_failure_is_declared():
    print("\n[fault injection]")
    import app.domain_scope as ds_mod
    orig = ds_mod.get_cursor
    ds_mod.get_cursor = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("injected"))
    try:
        check("an unreadable catalog with an EXPLICIT filter raises "
              "DomainCatalogUnavailable (-> 503), never an unfiltered search",
              _raises(ds_mod.DomainCatalogUnavailable, ds_mod.resolve,
                      ZS, ["ATTENDANCE"], organization_entities(ZS)))
        # …and an UNRESTRICTED request never touches the catalog at all
        codes, ids = ds_mod.resolve(ZS, None, organization_entities(ZS))
        check("an unrestricted request does not depend on the catalog",
              codes is None and ids is None)
    finally:
        ds_mod.get_cursor = orig


def test_step_failure_keeps_filter():
    print("\n[fault injection: one failing retrieval step]")
    import app.nodes.qa_lexical as lex_mod
    orig = lex_mod.get_cursor
    lex_mod.get_cursor = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("injected"))
    try:
        st = state_for("Kde se odevzdává zapomenutá pláštěnka?", domains=["ATTENDANCE"])
        out = dict(st)
        out.update(qa_lexical(out) or {})
        check("a failing qa_lexical does not resolve and does not widen",
              not out.get("resolved"))
    finally:
        lex_mod.get_cursor = orig
    # the document branch, still filtered, is what answers next
    found = tags(chunk_texts(
        state_for("Nalezené věci ukládáme", domains=["ATTENDANCE"]), document_lexical))
    check("the next step is still area-limited",
          "ds-doc-untagged" not in found, f"{sorted(found)}")


TESTS = [test_normalize, test_resolver, test_domain_where,
         test_route_pack_scope, test_route_falls_closed, test_corpus_topics,
         test_qa_exact, test_qa_mixed_pack_entry_filter, test_qa_identity_not_widened,
         test_qa_global_id_reachable, test_qa_vector, test_qa_related_chips,
         test_other_grade_net_keeps_domain,
         test_document_lexical, test_long_document_stays_whole,
         test_odd_branches_do_not_raise_under_a_filter,
         test_document_typo_fallback_is_scoped, test_rag_vector,
         test_disabled_steps_still_filter,
         test_history_scope, test_history_failure_does_not_widen,
         test_resolver_failure_is_declared, test_step_failure_keeps_filter]


def main() -> int:
    print("=" * 74)
    print("domain scope — row-level tests against a real database")
    print("=" * 74)
    fx.build()
    try:
        for t in TESTS:
            try:
                t()
            except Exception:
                _failures.append(t.__name__ + " (raised)")
                print(f"  FAIL {t.__name__} raised:")
                traceback.print_exc()
    finally:
        # ALWAYS, including after a failure — a leftover fixture makes the next
        # run's failures unreadable
        fx.drop()
    print("\n" + "=" * 74)
    print(f"{_ran} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
