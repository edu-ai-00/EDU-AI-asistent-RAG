"""Rule-based neutral rewriting of curated Czech answers — no LLM.

Goal: an answer must fit ANY phrasing of its question and
any asker. So it does not open with "Ano/Ne", does not address one person in
the familiar form, does not speak as "I", names the asker neutrally (default
"uchazeč") and points to where to look or ask. Polite "vykání" stays — "Co
nejdříve kontaktujte školu." reads human and fits every asker.

Every rule records what it did. Two kinds:

  auto   a mechanical fix that is safe as a proposal ("by jste" -> "byste")
  check  a proposal that needs a person: Czech agreement (gender, case,
         number) cannot be repaired by a table, so the change is offered and
         flagged rather than trusted

and a third, `flag`, for a problem the rules can see but not fix. Nothing here
is applied to data: the curator approves or edits every proposal in the UI.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# ── rule catalogue (shown as the legend in the UI) ──────────────────────────
RULES = {
    "mezery":       ("Typografie", "Zdvojené mezery, mezera před interpunkcí, tečka na konci věty."),
    "smajlik":      ("Smajlík", "Smajlíky a emoji do kurátorované odpovědi nepatří."),
    "by_jste":      ("Pravopis by/aby", "„by jste“ → „byste“, „by jsme“ → „bychom“, „aby jste“ → „abyste“."),
    "prvni_osoba":  ("1. osoba", "Odpověď nemluví za sebe („doporučuji“ → „doporučuje se“)."),
    "tykani":       ("Tykání → vykání", "Zdvořilé vykání sedí na žáka, rodiče i učitele."),
    "ano_ne":       ("Ano/Ne na začátku", "Odpověď má dávat smysl na jakoukoli formulaci otázky."),
    "role":         ("Slovo pro roli", "syn, dcera, dítě … → neutrální podmět (výchozí „uchazeč“)."),
    "odkaz":        ("Chybí odkaz", "U „kde najdu…“ nebo odkazu na web musí mít odpověď zdroj s adresou "
                                     "konkrétní stránky. Chat zdroje zobrazuje pod odpovědí, do textu se adresa nepíše."),
    "chybi_info":   ("Chybějící informace", "Místo „nevím“ říct, že informace chybí, a kde se doptat."),
}

AUTO, CHECK = "auto", "check"


@dataclass
class Change:
    rule: str
    kind: str          # auto | check
    before: str
    after: str
    note: str = ""


@dataclass
class Flag:
    rule: str
    note: str


def _match_case(src: str, repl: str) -> str:
    """Carry the capitalisation of the replaced word over to its replacement."""
    if src[:1].isupper() and repl:
        return repl[:1].upper() + repl[1:]
    return repl


class _Run:
    """One answer's rewrite: the text plus everything the rules did to it."""

    def __init__(self, text: str):
        self.text = text
        self.changes: list[Change] = []
        self.flags: list[Flag] = []

    def sub(self, pattern, repl, rule: str, kind: str, note: str = "", flags=0):
        """re.sub that records each replacement; `repl` is str or callable."""
        rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

        def _do(m):
            new = repl(m) if callable(repl) else m.expand(repl)
            if new is None or new == m.group(0):
                return m.group(0)
            self.changes.append(Change(rule, kind, m.group(0), new, note))
            return new
        self.text = rx.sub(_do, self.text)

    def flag(self, rule: str, note: str):
        self.flags.append(Flag(rule, note))


# ── individual rules ────────────────────────────────────────────────────────
def _typography(r: _Run):
    before = r.text
    t = re.sub(r"[ \t ]{2,}", " ", r.text)
    t = re.sub(r"[ \t]+([.,;:!?])", r"\1", t)
    t = t.strip()
    if t != before:
        r.changes.append(Change("mezery", AUTO, "", "",
                                "Zdvojené mezery nebo mezera před interpunkcí."))
        r.text = t


_SMILEY = re.compile(r"\s*(?:[:;=8]-?[()DPpOo/\\|]+|[\U0001F300-\U0001FAFF☀-➿]+)(?=\s|$)")


def _smileys(r: _Run):
    r.sub(_SMILEY, "", "smajlik", AUTO, "Smajlík odstraněn.")


_BY = {"by jste": "byste", "by jsme": "bychom", "by jsi": "bys",
       "aby jste": "abyste", "aby jsme": "abychom", "aby jsi": "abys",
       "kdyby jste": "kdybyste", "kdyby jsme": "kdybychom", "kdyby jsi": "kdybys"}


def _by_jste(r: _Run):
    rx = re.compile(r"\b(" + "|".join(sorted(_BY, key=len, reverse=True)) + r")\b", re.I)
    r.sub(rx, lambda m: _match_case(m.group(0), _BY[m.group(0).lower()]),
          "by_jste", AUTO, "Spisovně se píše dohromady.")


_FIRST_PERSON_FIX = {"doporučuji": "doporučuje se", "doporučujeme": "doporučuje se",
                     "radím": "doporučuje se", "radíme": "doporučuje se"}
_FIRST_PERSON_FLAG = re.compile(
    r"\b(nevím|nezodpovídám|odpovídám|myslím|jsem|můžu|mohu|nemůžu|nemohu|"
    r"umím|neumím|pomůžu|pomohu|znám|neznám)\b", re.I)


def _first_person(r: _Run):
    rx = re.compile(r"\b(" + "|".join(_FIRST_PERSON_FIX) + r")\b", re.I)
    r.sub(rx, lambda m: _match_case(m.group(0), _FIRST_PERSON_FIX[m.group(0).lower()]),
          "prvni_osoba", CHECK, "Neosobně místo 1. osoby — zkontrolujte slovosled.")
    hits = sorted({m.group(0) for m in _FIRST_PERSON_FLAG.finditer(r.text)})
    if hits:
        r.flag("prvni_osoba", "Odpověď mluví v 1. osobě: " + ", ".join(f"„{h}“" for h in hits)
               + ". Přeformulujte neosobně.")


# 2nd person singular -> polite plural. A TABLE for the forms that are certain;
# the generic ending rule below catches the rest but is marked `check`.
_TY_PRONOUNS = {
    "ty": "vy", "tě": "vás", "tebe": "vás", "tobě": "vám", "tebou": "vámi",
    "tvůj": "váš", "tvoje": "vaše", "tvá": "vaše", "tvé": "vaše", "tvého": "vašeho",
    "tvému": "vašemu", "tvém": "vašem", "tvým": "vaším", "tvou": "vaši", "tví": "vaši",
    "tvých": "vašich", "tvými": "vašimi",
}
_TY_VERBS = {
    "jsi": "jste", "nejsi": "nejste", "bys": "byste", "abys": "abyste", "kdybys": "kdybyste", "máš": "máte", "nemáš": "nemáte",
    "můžeš": "můžete", "nemůžeš": "nemůžete", "musíš": "musíte", "nemusíš": "nemusíte",
    "budeš": "budete", "nebudeš": "nebudete", "chceš": "chcete", "nechceš": "nechcete",
    "víš": "víte", "nevíš": "nevíte", "smíš": "smíte", "nesmíš": "nesmíte",
    "zvládneš": "zvládnete", "najdeš": "najdete", "dostaneš": "dostanete",
    "potřebuješ": "potřebujete", "nepotřebuješ": "nepotřebujete",
    # imperatives
    "zkus": "zkuste", "nezkoušej": "nezkoušejte", "podívej": "podívejte", "uč": "učte",
    "nauč": "naučte", "kontaktuj": "kontaktujte", "zeptej": "zeptejte", "napiš": "napište",
    "přijď": "přijďte", "vezmi": "vezměte", "využij": "využijte", "projdi": "projděte",
    "sleduj": "sledujte", "připrav": "připravte", "nezapomeň": "nezapomeňte",
    "měj": "mějte", "pošli": "pošlete", "zjisti": "zjistěte", "ověř": "ověřte",
    "zkontroluj": "zkontrolujte", "přečti": "přečtěte", "počkej": "počkejte",
    "obrať": "obraťte", "navštiv": "navštivte", "procvič": "procvičte",
    "vyplň": "vyplňte", "odevzdej": "odevzdejte", "podej": "podejte",
    "dej": "dejte", "nech": "nechte", "řekni": "řekněte", "vyber": "vyberte",
}
# generic present tense: -eš/-íš/-áš -> -ete/-íte/-áte (only longer words;
# "náš"/"váš" are pronouns and three letters long)
_TY_GENERIC = re.compile(r"\b(\w{3,})(eš|íš|áš)\b")
_GENERIC_END = {"eš": "ete", "íš": "íte", "áš": "áte"}
_NOT_VERBS = {"koláš", "guláš", "karabáš", "plíš"}


def _tykani(r: _Run):
    table = {**_TY_PRONOUNS, **_TY_VERBS}
    rx = re.compile(r"\b(" + "|".join(sorted(table, key=len, reverse=True)) + r")\b", re.I)
    r.sub(rx, lambda m: _match_case(m.group(0), table[m.group(0).lower()]),
          "tykani", AUTO, "Tykání převedeno na zdvořilé vykání.")
    # "ti" is also the plural demonstrative ("ti žáci") — offered, not trusted
    r.sub(r"\bti\b", lambda m: _match_case(m.group(0), "vám"), "tykani", CHECK,
          "„ti“ může být i „ti žáci“ — zkontrolujte.", flags=re.I)

    def _generic(m):
        if m.group(0).lower() in _NOT_VERBS:
            return None
        return m.group(1) + _GENERIC_END[m.group(2)]
    r.sub(_TY_GENERIC, _generic, "tykani", CHECK,
          "Odhad podle koncovky slovesa — zkontrolujte tvar.")


_NEGATION = re.compile(r"\b(ne\w{2,}|není|nelze|nikdy|nikde|nic|žádn\w*|bez)\b", re.I)


def _ano_ne(r: _Run):
    if re.fullmatch(r"\s*(Ano|Ne)\s*[.!]?\s*", r.text, re.I):
        r.flag("ano_ne", "Odpověď je jen „Ano/Ne“ — napište celou větu, která sedí na jakoukoli otázku.")
        return
    # "Ano, ale musíš…" -> "Musíte…": the "ale" only contrasted with the "Ano"
    m = re.match(r"\s*(Ano|Ne)\s*(?:[.,!–—-]\s*)+(?:ale\s+)?", r.text)
    if not m:
        return
    word = m.group(1)
    rest = r.text[m.end():]
    if not rest:
        r.flag("ano_ne", f"Odpověď je jen „{word}“ — doplňte celou větu.")
        return
    rest = rest[:1].upper() + rest[1:]
    r.changes.append(Change("ano_ne", AUTO, m.group(0).strip(), "",
                            f"„{word}“ na začátku odstraněno; věta má stát sama."))
    r.text = rest
    first = re.split(r"(?<=[.!?])\s", rest, maxsplit=1)[0]
    if word == "Ne" and not _NEGATION.search(first):
        r.flag("ano_ne", "Odstraněné „Ne“ neslo zápor — doplňte ho do první věty.")
    if len(first.split()) <= 2:
        r.flag("ano_ne", f"První věta „{first}“ je po odstranění příliš krátká — "
                         "napište, co přesně lze nebo nelze.")


# role words -> the neutral subject, by case. The case of an ambiguous form
# (dcery = gen. sg. or nom. pl.) is the most common reading; always `check`,
# because gender/number agreement around it ("dcera byla nemocná") changes too.
_SUBJECTS = {
    "uchazeč": {"nom": "uchazeč", "gen": "uchazeče", "dat": "uchazeči", "acc": "uchazeče",
                "loc": "uchazeči", "ins": "uchazečem", "nom_pl": "uchazeči",
                "gen_pl": "uchazečů", "dat_pl": "uchazečům", "acc_pl": "uchazeče",
                "loc_pl": "uchazečích", "ins_pl": "uchazeči"},
    "žák": {"nom": "žák", "gen": "žáka", "dat": "žákovi", "acc": "žáka", "loc": "žákovi",
            "ins": "žákem", "nom_pl": "žáci", "gen_pl": "žáků", "dat_pl": "žákům",
            "acc_pl": "žáky", "loc_pl": "žácích", "ins_pl": "žáky"},
}
_ROLE_FORMS = {
    "syn": "nom", "syna": "gen", "synovi": "dat", "synem": "ins", "synové": "nom_pl",
    "synů": "gen_pl", "synům": "dat_pl", "syny": "acc_pl", "synech": "loc_pl",
    "dcera": "nom", "dcery": "gen", "dceři": "dat", "dceru": "acc", "dcerou": "ins",
    "dcer": "gen_pl", "dcerám": "dat_pl", "dcerách": "loc_pl", "dcerami": "ins_pl",
    "dítě": "nom", "dítěte": "gen", "dítěti": "dat", "dítětem": "ins",
    "děti": "nom_pl", "dětí": "gen_pl", "dětem": "dat_pl", "dětech": "loc_pl", "dětmi": "ins_pl",
    "potomek": "nom", "potomka": "gen", "potomkovi": "dat",
}
_POSSESSIVE = (r"(?:(?:můj|moje|mé|mého|mému|mém|mým|mou|mojí|náš|naše|našeho|našemu|"
               r"našem|naším|naši|váš|vaše|vašeho|vašemu|vašem|vaším|vaši)\s+)?")


def _roles(r: _Run, subject: str):
    forms = _SUBJECTS.get(subject)
    if not forms:
        return
    rx = re.compile(r"\b" + _POSSESSIVE + r"(" + "|".join(
        sorted(_ROLE_FORMS, key=len, reverse=True)) + r")\b", re.I)
    r.sub(rx, lambda m: _match_case(m.group(0), forms[_ROLE_FORMS[m.group(1).lower()]]),
          "role", CHECK, f"Neutrální podmět „{subject}“ — zkontrolujte pád a shodu "
                         "(rod, číslo slovesa a přídavných jmen).")
    _subject_agreement(r, forms["nom"])


# After "syn" became the subject, the verb next to it may still address the
# reader: "Syn by jste měli zabalit" -> "Uchazeč byste měli" -> "Uchazeč by měl".
# Only the verb RIGHT AFTER the subject (one word between at most) is touched.
_AGREE_VERB = {"byste": "by", "jste": "je", "nejste": "není", "máte": "má",
               "nemáte": "nemá", "můžete": "může", "nemůžete": "nemůže",
               "musíte": "musí", "nemusíte": "nemusí", "budete": "bude",
               "nebudete": "nebude", "smíte": "smí", "nesmíte": "nesmí",
               "chcete": "chce", "potřebujete": "potřebuje"}


def _singular_participle(word: str) -> str | None:
    """měli/měly/měla -> měl; None when the word is not an l-participle."""
    m = re.fullmatch(r"(\w+l)(i|y|a)", word, re.I)
    return m.group(1) if m else None


def _subject_agreement(r: _Run, nom: str):
    verbs = "|".join(sorted(_AGREE_VERB, key=len, reverse=True))
    rx = re.compile(r"\b(" + re.escape(nom) + r")\s+(?:(\w+)\s+)?(" + verbs + r")\b"
                    r"(?:\s+(\w+l[iya]))?", re.I)

    def _fix(m):
        subj, mid, verb, part = m.group(1), m.group(2), m.group(3), m.group(4)
        out = subj + (" " + mid if mid else "") + " " + _match_case(verb, _AGREE_VERB[verb.lower()])
        if part:
            out += " " + (_singular_participle(part) or part)
        return out
    r.sub(rx, _fix, "role", CHECK,
          "Sloveso převedeno do shody s podmětem (3. osoba jednotného čísla).")


_URL = re.compile(r"https?://[^\s|\"<>]+")
# "kde najdu / kde zjistím / kde stáhnu" asks for a PLACE TO LOOK, not "kde se
# píše zkouška" (a building) — only the first wants a link.
_WHERE_Q = re.compile(r"\bkde\s+(?:\w+\s+)?(?:najd\w*|najít|zjist\w*|dozv\w*|stáhn\w*|"
                      r"sežen\w*|vezm\w*|vzít|podívat|přečíst|je\s+k\s+dispozici)"
                      r"|\bodkaz\w*|\bweb\w*|\bstránk\w*", re.I)
_WEB_A = re.compile(r"\bna\s+(?:\w+\s+){0,2}(?:web\w*|stránk\w*)|\bv\s+systému\b|\bna\s+webu\b", re.I)


def _pick_url(answer: str, sources: str) -> str | None:
    urls = [u.rstrip(".,;)") for u in _URL.findall(sources or "")]
    if not urls:
        return None
    for u in urls:                    # prefer a source the answer itself names
        host = re.sub(r"^https?://(www\.)?", "", u).split("/")[0].split(".")[0]
        if host and re.search(r"\b" + re.escape(host), answer, re.I):
            return u
    return urls[0]


def _is_home_page(url: str) -> bool:
    """https://prijimacky.cermat.cz/ — a whole site, not the page that answers."""
    rest = re.sub(r"^https?://[^/]+", "", url)
    return rest.strip("/") == "" or rest.startswith(("/?", "/#"))


def _link(r: _Run, questions, sources: str):
    """A "where do I find…" answer needs a SOURCE with the exact page.

    The address is NOT written into the answer: the chat shows an entry's
    sources under the answer (web_chat "Zdroje"), so a "Více informací: <url>"
    in the text only repeated it — and the sources of the JPZ set mostly name
    a site's home page, which is not where the information is. A curator
    rejected all 8 such additions (2026-10-22). The rule therefore only flags:
    no source with an address, or only home pages for a where-to-find question.
    """
    if _URL.search(r.text):
        return
    asks_where = any(_WHERE_Q.search(q or "") for q in questions)
    points_to_web = bool(_WEB_A.search(r.text))
    if not (asks_where or points_to_web):
        return
    urls = [u.rstrip(".,;)") for u in _URL.findall(sources or "")]
    if not urls:
        r.flag("odkaz", "Otázka se ptá „kde“ nebo odpověď odkazuje na web, ale ve zdrojích chybí "
                        "adresa. U přijímaček a maturity použijte CERMAT, jinak stránky školy, "
                        "a to konkrétní stránku, ne úvodní.")
    elif all(_is_home_page(u) for u in urls):
        r.flag("odkaz", "Zdroj vede jen na úvodní stránku webu (" + ", ".join(urls) + "). "
                        "Doplňte adresu stránky, kde informace opravdu je.")


_NON_ANSWER = re.compile(r"^\s*(odpověď není|nevím|zatím nevím|nezodpovídám|"
                         r"\S+\s+otázky\s+zatím\s+nezodpovídám|nemám\s+(tuto\s+)?informac\w*)",
                         re.I)
_DEPENDS = re.compile(r"\bzáleží na konkrétní\w*\b", re.I)


def _missing_info(r: _Run, sources: str):
    if _NON_ANSWER.search(r.text):
        url = _pick_url(r.text, sources)
        new = ("Tato informace zatím není k dispozici. Nejspolehlivěji ji poskytne "
               "přímo škola" + (f", případně {url}." if url else "."))
        r.changes.append(Change("chybi_info", CHECK, r.text, new,
                                "Místo obecného „nevím“ rada, kde se doptat."))
        r.text = new
        return
    if _DEPENDS.search(r.text) and not _URL.search(r.text):
        r.flag("chybi_info", "„Záleží na…“ bez odkazu — doplňte, kde konkrétně informaci najít.")


def _finish(r: _Run):
    t = r.text.strip()
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    if t and not re.search(r"[.!?…:)\]]$|https?://\S+$", t):
        r.changes.append(Change("mezery", AUTO, "", ".", "Doplněna tečka na konci."))
        t += "."
    r.text = t


def neutralize(answer: str, questions=(), sources: str = "",
               subject: str = "uchazeč") -> dict:
    """Propose a neutral version of one curated answer.

    Returns {"proposed", "changes": [...], "flags": [...]} — plain dicts, ready
    for JSON. `questions` (canonical + alternatives) only feed the link rule.
    """
    r = _Run(answer or "")
    _typography(r)
    _smileys(r)
    _by_jste(r)
    _first_person(r)
    _tykani(r)
    _ano_ne(r)
    _roles(r, subject)
    _missing_info(r, sources)
    _link(r, questions, sources)
    _finish(r)
    return {"proposed": r.text,
            "changes": [asdict(c) for c in r.changes],
            "flags": [asdict(f) for f in r.flags]}
