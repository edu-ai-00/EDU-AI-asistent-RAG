"""Rule-based question variants for the retrieval measurement (no language model).

Each generator takes one question and returns [(technique, label, text)]. The
rules are deliberately plain data: a curator can read them and see why a
variant exists. Czech only — the other registry languages (en/de/es) get no
variants, and measure.py says so instead of guessing.

  B1 synonyms      synonym word groups (syn/dcera/dítě/žák, přijímačky…)
  B2 no diacritics the whole question typed without háčky a čárky
  B3 typos         one deterministic typo per variant (i/y, dropped, swapped)
  B4 negation      smím ↔ nesmím … — a NEGATIVE test: the same curated answer
                   must not come back as confidently as for the original
"""

from __future__ import annotations

import random
import re
import unicodedata

# ── B1: synonym substitutions ────────────────────────────────────────────────
# (label, {surface form: replacement}). One variant per label: every form of
# the group is replaced at once. Case of the first letter is preserved.
# Grammatical agreement of the rest of the sentence is NOT fixed — a user
# typing "Co má dcera vzít" is exactly what retrieval has to survive.
_ROLE_FORMS = {
    # nominative, genitive/accusative, dative, instrumental, plural nom., plural gen.
    "uchazeč":  ["uchazeč", "uchazeče", "uchazeči", "uchazečem"],
    "syn":      ["syn", "syna", "synovi", "synem"],
    "dcera":    ["dcera", "dceru", "dceři", "dcerou"],
    "dítě":     ["dítě", "dítě", "dítěti", "dítětem"],
    "žák":      ["žák", "žáka", "žákovi", "žákem"],
    "student":  ["student", "studenta", "studentovi", "studentem"],
}
# "uchazeči" is also the plural nominative; the dative reading wins here, the
# variant is then merely ungrammatical, which is fine for a query.


def _role_map(src: str, dst: str) -> dict[str, str]:
    return dict(zip(_ROLE_FORMS[src], _ROLE_FORMS[dst]))


SYNONYM_RULES: list[tuple[str, dict[str, str]]] = [
    *[(f"{s}→{d}", _role_map(s, d))
      for s in ("uchazeč", "žák", "syn", "dcera")
      for d in ("syn", "dcera", "dítě", "žák", "student")
      if s != d],
    ("přijímačky→přijímací zkouška", {
        "přijímačky": "přijímací zkoušky", "přijímaček": "přijímacích zkoušek",
        "přijímačkách": "přijímacích zkouškách", "přijímačkám": "přijímacím zkouškám",
        "přijímačka": "přijímací zkouška", "přijímačku": "přijímací zkoušku",
        "přijímačce": "přijímací zkoušce"}),
    ("přijímací zkouška→přijímačky", {
        "přijímací zkoušky": "přijímačky", "přijímacích zkoušek": "přijímaček",
        "přijímacích zkouškách": "přijímačkách", "přijímacím zkouškám": "přijímačkám",
        "přijímací zkouška": "přijímačka", "přijímací zkoušku": "přijímačku",
        "přijímací zkoušce": "přijímačce", "přijímací zkouškou": "přijímačkou"}),
    ("matematika→matika", {
        "matematika": "matika", "matematiky": "matiky", "matematiku": "matiku",
        "matematice": "matice", "matematikou": "matikou"}),
    ("čeština→čj", {
        "čeština": "čj", "češtiny": "čj", "češtinu": "čj", "češtině": "čj",
        "češtinou": "čj", "český jazyk": "čj", "českého jazyka": "čj",
        "český jazyk a literatura": "čj"}),
    ("obědy→stravování", {"obědy": "stravování", "obědů": "stravování",
                          "oběd": "stravování", "obědech": "stravování"}),
    ("dvojka→chvalitebný", {"dvojka": "chvalitebná", "dvojku": "chvalitebnou",
                            "dvojky": "chvalitebné", "dvojkou": "chvalitebnou"}),
]


def _sub_words(text: str, mapping: dict[str, str]) -> str:
    # longest key first, so "přijímací zkoušky" wins over a shorter overlap
    keys = sorted(mapping, key=len, reverse=True)
    rx = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, keys)) + r")(?!\w)", re.I)

    def rep(m: re.Match) -> str:
        src = m.group(0)
        dst = mapping[src.lower()]
        return dst[:1].upper() + dst[1:] if src[:1].isupper() else dst

    return rx.sub(rep, text)


def synonyms(q: str) -> list[tuple[str, str, str]]:
    out, seen = [], {q}
    for label, mapping in SYNONYM_RULES:
        v = _sub_words(q, mapping)
        if v not in seen:
            seen.add(v)
            out.append(("B1", label, v))
    return out


# ── B2: without diacritics ───────────────────────────────────────────────────
def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def no_diacritics(q: str) -> list[tuple[str, str, str]]:
    v = strip_diacritics(q).lower()
    return [("B2", "bez diakritiky", v)] if v != q.lower() else []


# ── B3: typos ────────────────────────────────────────────────────────────────
_IY = {"i": "y", "y": "i", "í": "ý", "ý": "í"}


def _typo(word: str, kind: str, rnd: random.Random) -> str | None:
    if kind == "i/y":
        pos = [i for i, c in enumerate(word) if c.lower() in _IY]
        if not pos:
            return None
        i = rnd.choice(pos)
        return word[:i] + _IY[word[i].lower()] + word[i + 1:]
    if kind == "vynechané písmeno":
        i = rnd.randrange(1, len(word))          # never the first letter
        return word[:i] + word[i + 1:]
    if kind == "prohozená písmena":
        i = rnd.randrange(1, len(word) - 1)
        if word[i] == word[i + 1]:
            return None
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    return None


TYPO_KINDS = ("i/y", "vynechané písmeno", "prohozená písmena")


def typos(q: str, seed: str) -> list[tuple[str, str, str]]:
    """One variant per typo kind, in a content word (≥ 5 letters).

    Seeded by the question id, so a rerun produces the same variants and two
    measurements can be compared row by row.
    """
    words = [(m.start(), m.group(0)) for m in re.finditer(r"\w+", q)
             if len(m.group(0)) >= 5 and m.group(0).isalpha()]
    if not words:
        return []
    out = []
    for kind in TYPO_KINDS:
        rnd = random.Random(f"{seed}|{kind}")
        cands = words[:]
        rnd.shuffle(cands)
        for start, w in cands:
            t = _typo(w, kind, rnd)
            if t and t != w:
                out.append(("B3", kind, q[:start] + t + q[start + len(w):]))
                break
    return out


# ── B4: negation flip ────────────────────────────────────────────────────────
# positive ↔ negative, surface forms. Only the FIRST match is flipped.
_NEG_PAIRS = [
    ("smím", "nesmím"), ("smí", "nesmí"), ("smíme", "nesmíme"), ("smíte", "nesmíte"),
    ("smějí", "nesmějí"),
    ("můžu", "nemůžu"), ("mohu", "nemohu"), ("může", "nemůže"), ("můžeme", "nemůžeme"),
    ("můžete", "nemůžete"), ("mohou", "nemohou"),
    ("musím", "nemusím"), ("musí", "nemusí"), ("musíme", "nemusíme"), ("musíte", "nemusíte"),
    ("mám", "nemám"), ("má", "nemá"), ("máme", "nemáme"), ("máte", "nemáte"), ("mají", "nemají"),
    ("lze", "nelze"), ("jde", "nejde"), ("platí", "neplatí"),
    ("potřebuji", "nepotřebuji"), ("potřebuju", "nepotřebuju"), ("potřebuje", "nepotřebuje"),
    ("stihnu", "nestihnu"), ("stihne", "nestihne"), ("dostaví", "nedostaví"),
    ("přijde", "nepřijde"), ("udělá", "neudělá"), ("udělám", "neudělám"),
    ("je možné", "není možné"), ("je nutné", "není nutné"), ("je potřeba", "není potřeba"),
    ("dá se", "nedá se"),
]
_FLIP = {**{p: n for p, n in _NEG_PAIRS}, **{n: p for p, n in _NEG_PAIRS}}
_FLIP_RX = re.compile(r"(?<!\w)(" + "|".join(
    map(re.escape, sorted(_FLIP, key=len, reverse=True))) + r")(?!\w)", re.I)


def negation(q: str) -> list[tuple[str, str, str]]:
    m = _FLIP_RX.search(q)
    if not m:
        return []
    src = m.group(0)
    dst = _FLIP[src.lower()]
    if src[:1].isupper():
        dst = dst[:1].upper() + dst[1:]
    label = "kladná → záporná" if dst.lower().startswith("ne") else "záporná → kladná"
    return [("B4", label, q[:m.start()] + dst + q[m.end():])]


def all_variants(q: str, seed: str) -> list[tuple[str, str, str]]:
    return [*synonyms(q), *no_diacritics(q), *typos(q, seed), *negation(q)]
