# last change in code by Jan Drozd (2026-08-21).
"""Content spec for the QA test packs — questions, variants, per-entity answers.

Answers are TEST data: every one carries a [SCOPE] tag so a probe can tell which
pack answered. The questions and topics are real, taken from what production has
ingested (školní řád, o-studiu, termíny akcí, MŠMT šikana, Cermat/MŠMT/EduIn
přijímačky); the answer bodies are deliberately synthetic and short.
"""

# The five scopes on production. `tag` is what the probe asserts on; grades are
# code_list(school_grade) CODES of that entity (see init_clean/02_seed_config.sql).
ENTITIES = {
    "SKOLA_NOVA_VES": dict(
        key="org", tag="CELA ORGANIZACE", user_id="ded501eb-24b6-4571-8bee-4992f9a77324",
        level=None, grades_all=["9999"], grades_adm=["9999"], domains=False),
    "SKOLA_NOVA_VES_ZS": dict(
        key="zs", tag="ZAKLADNI SKOLA", user_id="066d9206-cfe2-4f60-9f02-79bfea57c2bb",
        level="ZS", grades_all=["9999"], grades_adm=["5", "7", "9"], domains=True),
    "SKOLA_NOVA_VES_SOS": dict(
        key="sos", tag="STREDNI SKOLA", user_id="3cd81a92-957e-4208-90aa-dcf0f7207286",
        level="SS", grades_all=["9999"], grades_adm=["12", "13"], domains=True),
    "ZAKLADNI_SKOLA_BOROVA": dict(
        key="bor", tag="BOROVA", user_id="c2bedc46-6ad2-4fbc-a49f-ee4f101f0c7b",
        level="ZS", grades_all=["9999"], grades_adm=["5", "7", "9"], domains=False),
    "ZAKLADNI_SKOLA_LIPOVA": dict(
        key="lip", tag="LIPOVA", user_id="be164723-a616-4a8b-a900-c9c555cb8326",
        level="ZS", grades_all=["9999"], grades_adm=["5", "7", "9"], domains=False),
}

# Only the two Nová Ves units have per-entity DOMAIN taxonomy rows; the org and
# the two foreign schools have none, so their packs declare no domains.
DOMAINS_PROVOZ = ["ATTENDANCE", "GRADING", "CATERING", "BULLYING", "DATA_PROTECTION",
                  "SCHOOL_EVENTS", "SPECIAL_NEEDS"]
DOMAINS_ADM_ZS = ["SECONDARY_ADMISSIONS", "MULTIYEAR_GYMNASIUM_ADMISSIONS"]
DOMAINS_ADM_SOS = ["GRADUATION_EXAM", "UNIVERSITY_ADMISSIONS"]


class Q:
    """One curated question: shared phrasing, per-entity answer bodies."""

    def __init__(self, cat, q, variants, **answers):
        self.cat, self.q, self.variants, self.answers = cat, q, variants, answers


# ── Cited sources (CSV column `sources` → qa_entries.asset_refs) ─────────────
# Every URL here is one production has ACTUALLY ingested (../reingest_sources.json).
# Nothing is invented: the answer bodies are synthetic test data, but a source is
# a claim about the real world, and a made-up link would be indistinguishable
# from a hallucination the moment it appears under "Zdroje" in the chat.
SRC = {
    # Nová Ves — skola-nova-ves.example, i.e. the org and BOTH its units
    "rad":      ("Školní řád ZŠ a SŠ",
                 "https://www.skola-nova-ves.example/upload/Martin/pdf/2024-2025/skolni_rad_zs_ss.pdf"),
    "studium":  ("O studiu", "https://www.skola-nova-ves.example/index.php?p=o-studiu"),
    "terminy":  ("Termíny akcí", "https://www.skola-nova-ves.example/index.php?p=terminy-akci"),
    "scio":     ("SCIO", "https://www.skola-nova-ves.example/scio"),
    # Základní škola Lipová — its own pages
    "svo":      ("Základní škola Lipová", "https://www.skola-lipova.example/"),
    "svo_prip": ("Jak se připravit na přijímací zkoušky",
                 "https://skola-lipova.example/jak-se-pripravit-na-prijimaci-zkousky-tipy-a-strategie-k-uspechu/"),
    "svo_mob":  ("Jak využít chytré telefony ke studiu",
                 "https://skola-lipova.example/jak-vyuzit-chytre-telefony-ke-studiu-efektivne-a-produktivne"),
    # national — the same document answers the topic for every school
    "msmt_sikana":     ("Metodický pokyn MŠMT k šikaně", "https://msmt.gov.cz/file/65358_2_1/"),
    "cermat_jpz":      ("Jednotná přijímací zkouška",
                        "https://prijimacky.cermat.cz/menu/jednotna-prijimaci-zkouska"),
    "cermat_pruvodce": ("Průvodce pro uchazeče k přijímacímu řízení na SŠ 2026",
                        "https://prijimacky.cermat.cz/files/files/PR%C5%AEVODCE_PRO_UCHAZE%C4%8CE_K_P%C5%98IJ%C3%8DMAC%C3%8DMU_%C5%98%C3%8DZEN%C3%8D_NA_ST%C5%98EDN%C3%8D_%C5%A0KOLU_2026.pdf"),
    "cermat_schema":   ("Schéma JPZ 2026",
                        "https://prijimacky.cermat.cz/files/files/JPZ_schema_2026.pdf"),
    "cermat_spec":     ("Specifikace požadavků k JPZ",
                        "https://prijimacky.cermat.cz/menu/specifikace-pozadavku-k-jpz"),
    "cermat_testy":    ("Testová zadání k procvičování",
                        "https://prijimacky.cermat.cz/menu/testova-zadani-k-procvicovani"),
    "cermat_upravy":   ("Úpravy podmínek přijímacího řízení",
                        "https://prijimacky.cermat.cz/menu/upravy-podminek-prijimaciho-rizeni"),
    "eduin_pruvodce":  ("Průvodce přijímačkami 2025/2026 pro žáky",
                        "https://www.eduin.cz/clanky/pruvodce-prijimackami-2025-2026-pro-zaky/"),
    "eduin_kariera":   ("Kariérový rozvoj", "https://www.eduin.cz/clanky/karierovy-rozvoj"),
    "eduin_mobily":    ("Komentář: mobily ve škole",
                        "https://www.eduin.cz/clanky/komentar-musuta-mobily"),
}

# By category, for every school alike.
NATIONAL = {
    "Mobily":            ["eduin_mobily"],
    "Šikana":            ["msmt_sikana"],
    "Termíny":           ["cermat_jpz", "cermat_schema"],
    "Testy":             ["cermat_spec", "cermat_testy"],
    "Přihláška":         ["cermat_pruvodce"],
    "Kritéria":          ["cermat_pruvodce"],
    "Uzpůsobení":        ["cermat_upravy"],
    "Odvolání":          ["cermat_pruvodce"],
    "Příprava":          ["cermat_testy", "eduin_pruvodce"],
    "Víceletá gymnázia": ["cermat_jpz"],
    "Vysoká škola":      ["eduin_kariera"],
    # Maturita deliberately has none: production has ingested nothing about it,
    # and an entry with no source is a case the rendering has to survive too.
}

# The categories a school answers out of its OWN rules. The "*" fallback below
# fires only here: an admissions question is answered by Cermat, and letting the
# fallback reach it cited the school's own řád as the source of the JPZ dates.
PROVOZ_CATS = {"Docházka", "Hodnocení", "Mobily", "Pomůcky", "Jídelna", "Šikana",
               "GDPR", "Akce", "Podpora", "Provoz", "Zápis", "Družina", "Praxe"}

# What each school publishes ITSELF, by category; "*" is the provoz fallback. A
# school's own document rides ONLY on its own answers — attaching Nová Ves's
# školní řád to a Borová answer would be precisely the tenant leak these packs
# exist to catch, only wearing a link instead of a scope tag.
OWN = {
    # the three Nová Ves scopes share one website, and the školní řád PDF is
    # itself the ZŠ+SŠ one
    "org": {"*": ["rad"], "Provoz": ["studium"], "Akce": ["terminy"],
            "Hodnocení": ["rad", "studium"], "Příprava": ["scio"]},
    "zs":  {"*": ["rad"], "Provoz": ["studium"], "Akce": ["terminy"],
            "Hodnocení": ["rad", "studium"], "Zápis": ["terminy"],
            "Příprava": ["scio"]},
    "sos": {"*": ["rad"], "Provoz": ["studium"], "Akce": ["terminy"],
            "Hodnocení": ["rad", "studium"], "Praxe": ["studium"],
            "Maturita": ["studium"], "Vysoká škola": ["studium"]},
    "svo": {"*": ["svo"], "Mobily": ["svo_mob"], "Příprava": ["svo_prip"]},
    # Borová has nothing of its own on production, so its entries carry only
    # the national sources — and most of its provoz pack none at all.
    "jed": {},
}


def sources_for(cat: str, key: str):
    """[(title, url), …] cited by one question in one entity's pack.

    Own documents first (the school's own word outranks the ministry's on how
    the school runs), then the national ones; deduplicated, order kept.
    """
    own = OWN.get(key, {})
    fallback = own.get("*", []) if cat in PROVOZ_CATS else []
    ids = list(own.get(cat, fallback)) + list(NATIONAL.get(cat, []))
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(SRC[i])
    return out


# ── Pack A: provoz — the school-life spine, present in ALL five packs ─────────
# The same canonical question everywhere, so the proximity tier (own school before
# the organisation before anyone else) is the only thing that can pick a winner.
PROVOZ = [
    Q("Docházka", "Jak se omlouvá absence?",
      ["Jakým způsobem omluvím nepřítomnost žáka?",
       "Jak mám omluvit absenci?",
       "kde se omlouva absence",
       "Co dělat, když dítě chybí ve škole?"],
      org="absence se omlouvá podle školního řádu příslušné součásti do tří kalendářních dnů.",
      zs="absenci omlouvá zákonný zástupce písemně v žákovské knížce nebo přes systém do tří kalendářních dnů.",
      sos="absenci omlouvá zákonný zástupce, u zletilého žáka si ji omlouvá student sám, vždy do tří kalendářních dnů.",
      jed="absenci omlouvá zákonný zástupce do tří dnů, nejlépe telefonicky do kanceláře školy.",
      svo="absenci zapisuje průvodce do docházkového listu, rodič ji potvrdí v aplikaci do tří dnů."),
    Q("Docházka", "Do kdy musím nahlásit nepřítomnost?",
      ["Jaká je lhůta pro nahlášení nepřítomnosti?",
       "Dokdy volat, když dítě nepřijde?",
       "do kdy nahlasit ze nebudu ve skole"],
      org="nepřítomnost se hlásí do 8:00 prvního dne.",
      zs="nepřítomnost hlaste nejpozději do 8:00 prvního dne, ideálně předem.",
      sos="nepřítomnost hlaste do 8:00 prvního dne, zletilý žák hlásí sám.",
      jed="nepřítomnost hlaste ráno do 8:00 do kanceláře.",
      svo="nepřítomnost stačí napsat průvodci do 8:30, kdy začíná ranní kruh."),
    Q("Docházka", "Jak požádat o uvolnění z výuky na několik dní?",
      ["Jaký je postup pro uvolnění žáka na více dní?",
       "Chceme jet na dovolenou, co musíme udělat?",
       "uvolneni z vyuky na 14 dni",
       "Kdo schvaluje delší uvolnění?"],
      org="o uvolnění se žádá písemně u ředitelky organizace.",
      zs="o uvolnění na více než dva dny žádá zákonný zástupce písemně ředitelku školy, s doporučením třídního učitele.",
      sos="o uvolnění na více než dva dny žádá student písemně ředitele, s vyjádřením třídního učitele.",
      jed="o uvolnění žádejte písemně ředitelku školy.",
      svo="o uvolnění se domlouváte přímo s průvodcem, delší absenci schvaluje ředitel."),
    Q("Docházka", "Kdo řeší neomluvenou absenci?",
      ["Kdo se zabývá neomluvenými hodinami?",
       "S kým se řeší záškoláctví?",
       "kdo resi neomluvenou absenci"],
      org="neomluvenou absenci řeší vedení příslušné součásti.",
      zs="neomluvenou absenci řeší třídní učitel, nad deset hodin výchovná komise se zákonným zástupcem.",
      sos="neomluvenou absenci řeší třídní učitel, opakovanou pak ředitel se studentem a zákonným zástupcem.",
      jed="neomluvenou absenci řeší třídní učitelka a ředitelka školy.",
      svo="neomluvenou absenci probírá průvodce s rodinou na společné schůzce."),
    Q("Docházka", "Počítá se pozdní příchod do hodiny jako absence?",
      ["Je pozdní příchod absence?",
       "Co se stane, když přijdu pozdě na hodinu?",
       "pozdni prichod do skoly"],
      org="pozdní příchody upravuje školní řád příslušné součásti.",
      zs="pozdní příchod se zapisuje do třídní knihy, opakované příchody se řeší jako neomluvená hodina.",
      sos="pozdní příchod se zapisuje, tři pozdní příchody se počítají jako jedna neomluvená hodina.",
      jed="pozdní příchod zapisuje vyučující do třídní knihy.",
      svo="pozdní příchod se nezapisuje jako absence, řeší se domluvou v ranním kruhu."),
    Q("Docházka", "Jak uvolním dítě z tělocviku?",
      ["Jak požádat o uvolnění z tělesné výchovy?",
       "Dítě nemůže cvičit, co s tím?",
       "uvolneni z telocviku"],
      org="o uvolnění z tělesné výchovy rozhoduje ředitelka příslušné součásti na základě lékařského doporučení.",
      zs="krátkodobě stačí písemná omluvenka od rodiče, dlouhodobě lékařské doporučení a žádost ředitelce školy.",
      sos="krátkodobě stačí omluvenka, na celé pololetí je nutné lékařské doporučení a žádost řediteli.",
      jed="uvolnění z tělocviku vyřizuje kancelář školy na základě potvrzení od lékaře.",
      svo="uvolnění z pohybového bloku domlouvá rodič s průvodcem, dlouhodobě s doporučením lékaře."),
    Q("Hodnocení", "Jak se hodnotí písemné práce?",
      ["Jakou váhu mají písemné práce v klasifikaci?",
       "Jak se známkují testy?",
       "jaka vaha ma ctvrtletka"],
      org="hodnocení se řídí klasifikačním řádem příslušné součásti.",
      zs="písemné práce se hodnotí známkou 1–5, váha je uvedena v klasifikačním řádu.",
      sos="písemné práce se hodnotí body převedenými na známku, čtvrtletní práce má dvojnásobnou váhu.",
      jed="písemné práce se hodnotí známkou 1–5 podle klasifikačního řádu.",
      svo="písemné práce se nehodnotí známkou, žák dostává slovní zpětnou vazbu."),
    Q("Hodnocení", "Kdy se uzavírá klasifikace?",
      ["K jakému datu se uzavírá hodnocení za období?",
       "Dokdy můžu opravit známku?",
       "kdy se zaviraji znamky"],
      org="klasifikace se uzavírá podle harmonogramu školního roku.",
      zs="klasifikace se uzavírá týden před vydáním vysvědčení.",
      sos="klasifikace se uzavírá tři pracovní dny před klasifikační poradou.",
      jed="klasifikace se uzavírá poslední týden před vysvědčením.",
      svo="hodnoticí zpráva se uzavírá na konci trimestru, týden před tripartitou."),
    Q("Hodnocení", "Jak se hodnotí prospěch žáků?",
      ["Podle čeho se určuje celkový prospěch?",
       "Jak se počítá vysvědčení?",
       "jak se hodnoti prospech"],
      org="prospěch se hodnotí podle klasifikačního řádu příslušné součásti.",
      zs="prospěch se hodnotí známkami 1–5 z každého předmětu a na vysvědčení i celkovým hodnocením.",
      sos="prospěch se hodnotí známkami 1–5, celkově pak prospěl s vyznamenáním, prospěl, nebo neprospěl.",
      jed="prospěch se hodnotí známkami 1–5 v každém předmětu.",
      svo="prospěch popisuje slovní hodnocení v každé vzdělávací oblasti."),
    Q("Hodnocení", "Co když nesouhlasím se známkou na vysvědčení?",
      ["Jak se odvolat proti známce?",
       "Můžu žádat přezkoumání hodnocení?",
       "nesouhlasim se znamkou co delat"],
      org="o přezkoumání výsledků hodnocení se žádá u ředitelky příslušné součásti.",
      zs="zákonný zástupce může do tří pracovních dnů požádat ředitelku školy o komisionální přezkoušení.",
      sos="student nebo zákonný zástupce může do tří pracovních dnů požádat ředitele o komisionální přezkoušení.",
      jed="o přezkoumání známky žádejte písemně ředitelku školy do tří pracovních dnů.",
      svo="nesouhlas s hodnocením se řeší na tripartitě s průvodcem a ředitelem."),
    Q("Provoz", "Kdy začíná a končí vyučování?",
      ["Jaký je začátek a konec vyučování?",
       "V kolik se jde do školy?",
       "kdy konci skola",
       "Jak dlouho trvá výuka?"],
      org="vyučování začíná v 8:00, konec se liší podle součásti a rozvrhu.",
      zs="vyučování začíná v 8:00, první stupeň končí nejpozději ve 12:35, druhý stupeň podle rozvrhu.",
      sos="vyučování začíná v 8:00 a podle rozvrhu končí nejpozději v 15:35.",
      jed="vyučování začíná v 7:55 a končí podle rozvrhu třídy.",
      svo="den začíná ranním kruhem v 8:30 a končí ve 14:30, odpolední dílny do 16:00."),
    Q("Provoz", "Kdy jsou konzultační hodiny?",
      ["Kdy je možné konzultovat s vyučujícím?",
       "Kdy si můžu přijít popovídat s učitelem?",
       "konzultacni hodiny kdy"],
      org="konzultace se sjednávají s příslušným pedagogem.",
      zs="konzultace jsou po dohodě s třídním učitelem, obvykle ve čtvrtek odpoledne.",
      sos="konzultace jsou po dohodě s vyučujícím, obvykle v úterý od 14:00.",
      jed="konzultace domlouvejte přímo s vyučujícím přes kancelář školy.",
      svo="konzultace probíhají na tripartitě třikrát ročně, jinak po domluvě s průvodcem."),
    Q("Provoz", "Kde můžeme být o přestávkách?",
      ["Kam smíme o přestávce?",
       "Můžeme o přestávce ven ze třídy?",
       "kde muzeme byt o prestavkach"],
      org="pohyb o přestávkách upravuje školní řád příslušné součásti.",
      zs="o přestávkách jsou žáci ve třídě nebo na chodbě svého patra, budovu opouštět nesmí.",
      sos="o přestávkách se studenti pohybují po budově, velkou přestávku mohou trávit i venku v areálu.",
      jed="o přestávkách jsou žáci ve třídě nebo na chodbě, za dozoru vyučujícího.",
      svo="o přestávkách je otevřená zahrada i klidová zóna, děti si volí samy."),
    Q("Provoz", "Kdy začíná školní rok?",
      ["Kdy je první školní den?",
       "Kdy jdeme poprvé do školy?",
       "kdy zacina skolni rok"],
      org="školní rok začíná podle harmonogramu MŠMT, obvykle 1. září.",
      zs="školní rok začíná 1. září, první den je zkrácený a končí po třídnické hodině.",
      sos="školní rok začíná 1. září zahájením v kmenových učebnách.",
      jed="školní rok začíná 1. září slavnostním zahájením před školou.",
      svo="školní rok otevíráme 1. září společným setkáním celé školy na zahradě."),
    Q("Provoz", "Kdy jsou podzimní prázdniny?",
      ["Termín podzimních prázdnin?",
       "Kdy máme volno na podzim?",
       "kdy jsou podzimni prazdniny"],
      org="termíny prázdnin se řídí harmonogramem MŠMT, viz kalendář akcí.",
      zs="podzimní prázdniny jsou podle harmonogramu MŠMT, přesný termín najdeš v kalendáři akcí školy.",
      sos="podzimní prázdniny jsou podle harmonogramu MŠMT, termín je v kalendáři akcí.",
      jed="podzimní prázdniny vyhlašuje MŠMT, termín zveřejňujeme na webu školy.",
      svo="podzimní prázdniny držíme podle MŠMT, termín je v rodičovském kalendáři."),
    Q("Mobily", "Můžu mít ve škole mobilní telefon?",
      ["Jaká jsou pravidla pro používání mobilních telefonů?",
       "Můžu mít o přestávce mobil?",
       "berou ve skole telefony",
       "Smí se ve škole telefonovat?"],
      org="pravidla pro telefony určuje školní řád příslušné součásti.",
      zs="telefon musí být během výuky vypnutý a uložený v aktovce, používat ho nesmíš ani o přestávkách.",
      sos="telefon musí být během výuky vypnutý, o přestávkách ho používat můžeš.",
      jed="telefon se před vyučováním odkládá do třídní schránky a vydává se po skončení výuky.",
      svo="telefony necháváme na začátku dne v boxu, výjimku domlouvá průvodce."),
    Q("Mobily", "Co se stane, když mi učitel zabaví telefon?",
      ["Může učitel zabavit mobil?",
       "Kdy dostanu telefon zpátky?",
       "ucitel mi vzal mobil"],
      org="postup při odebrání telefonu upravuje školní řád příslušné součásti.",
      zs="telefon se ukládá u třídního učitele a vydává se zákonnému zástupci na konci vyučování.",
      sos="telefon se ukládá u vyučujícího a vydává se studentovi na konci vyučovacího dne.",
      jed="telefon si vyzvedne zákonný zástupce v kanceláři školy.",
      svo="telefon se vrací na konci dne, opakované porušení řeší průvodce s rodinou."),
    Q("Pomůcky", "Co dělat, když zapomenu pomůcky?",
      ["Jak postupovat při zapomenutých pomůckách?",
       "Zapomněl jsem si věci, co teď?",
       "zapomnel jsem ucebnici"],
      org="postup určuje školní řád příslušné součásti.",
      zs="zapomenuté pomůcky nahlas na začátku hodiny vyučujícímu, opakované zapomínání se zapisuje.",
      sos="zapomenuté pomůcky hlas vyučujícímu na začátku hodiny, jinak jde o nepřipravenost.",
      jed="zapomenuté pomůcky hlas vyučujícímu hned na začátku hodiny.",
      svo="chybějící pomůcku si domluvíš s průvodcem, škola má náhradní sadu."),
    Q("Pomůcky", "Jaké pomůcky si mám přinést do školy?",
      ["Jaký je seznam pomůcek?",
       "Co si mám přinýst do školy?",
       "seznam pomucek na zacatek roku"],
      org="seznam pomůcek vydává příslušná součást na začátku školního roku.",
      zs="seznam pomůcek dostaneš od třídního učitele a je i na webu školy v sekci o studiu.",
      sos="seznam pomůcek pro obor dostaneš od třídního učitele na začátku roku.",
      jed="seznam pomůcek rozdávají třídní učitelé první školní den.",
      svo="seznam pomůcek posíláme rodičům e-mailem v srpnu."),
    Q("Jídelna", "Jak funguje odhlašování obědů?",
      ["Jaký je postup pro odhlášení oběda?",
       "Jak odhlásit obědy, když je dítě nemocné?",
       "odhlaseni obedu do kolika",
       "Můžu si vyzvednout oběd první den nemoci?"],
      org="školní jídelna je společná pro celou organizaci, odhlašování do 8:00.",
      zs="obědy se odhlašují do 8:00 téhož dne přes systém nebo telefonicky v jídelně.",
      sos="obědy se odhlašují do 8:00 téhož dne přes systém, jídelnu sdílíme se základní školou.",
      jed="obědy odhlašujte do 7:30 telefonicky u vedoucí školní jídelny.",
      svo="obědy odhlašujte do 8:00 v aplikaci, jídlo vaří externí dodavatel."),
    Q("Jídelna", "Jak se platí obědy?",
      ["Jakým způsobem se hradí stravné?",
       "Kdy se strhává platba za obědy?",
       "jak platit obedy"],
      org="stravné se hradí bezhotovostně na účet školní jídelny.",
      zs="stravné se hradí inkasem nebo převodem vždy do 20. dne předchozího měsíce.",
      sos="stravné se hradí inkasem nebo převodem do 20. dne předchozího měsíce, stejně jako na ZŠ.",
      jed="stravné se platí inkasem k 15. dni v měsíci.",
      svo="stravné je součástí měsíční platby školného, vyúčtování posíláme čtvrtletně."),
    Q("Jídelna", "Co když má dítě dietu nebo alergii?",
      ["Jak řešit dietní stravování?",
       "Vaříte bezlepkovou stravu?",
       "dieta alergie ve skolni jidelne"],
      org="dietní stravování se domlouvá s vedoucí školní jídelny organizace.",
      zs="dietu nahlas vedoucí jídelny a dolož potvrzením od lékaře, jídelna nabízí bezlepkovou variantu.",
      sos="dietu nahlas vedoucí jídelny s potvrzením od lékaře, nabídka je stejná jako pro ZŠ.",
      jed="dietní stravu řeší vedoucí jídelny individuálně na základě lékařského potvrzení.",
      svo="dietu domluvíte s dodavatelem stravy přes kancelář školy."),
    Q("Šikana", "Co mám dělat, když se mi někdo ve škole posmívá?",
      ["Kam se obrátit, když mě někdo šikanuje?",
       "Někdo mi ubližuje ve škole, co mám dělat?",
       "kdyz me nekdo sikanuje"],
      org="obrať se na kteréhokoli pedagoga, postup upravuje metodický pokyn k prevenci šikany.",
      zs="řekni to třídnímu učiteli, metodikovi prevence nebo komukoli z dospělých ve škole, škola to musí začít řešit.",
      sos="obrať se na třídního učitele nebo metodika prevence, můžeš přijít i sám bez rodičů.",
      jed="přijď za třídní učitelkou nebo do kanceláře, ředitelka to řeší osobně.",
      svo="řekni to svému průvodci nebo napiš do schránky důvěry, řešíme to v kruhu."),
    Q("Šikana", "Jak škola předchází šikaně?",
      ["Jaká je prevence šikany?",
       "Co škola dělá proti šikaně?",
       "prevence sikany ve skole"],
      org="prevence vychází z metodického pokynu MŠMT a z minimálního preventivního programu součástí.",
      zs="škola má minimální preventivní program, metodika prevence a pravidelné třídnické hodiny zaměřené na vztahy.",
      sos="škola má minimální preventivní program, metodika prevence a adaptační kurz pro první ročníky.",
      jed="škola má preventivní program a spolupracuje s pedagogicko-psychologickou poradnou.",
      svo="prevence stojí na denním kruhu, mediaci mezi dětmi a práci s pravidly komunity."),
    Q("Šikana", "Komu můžu nahlásit šikanu anonymně?",
      ["Existuje schránka důvěry?",
       "Můžu nahlásit šikanu anonymně?",
       "schranka duvery"],
      org="anonymní podnět lze podat schránkou důvěry v každé součásti.",
      zs="ve vestibulu je schránka důvěry a na webu školy anonymní formulář pro metodika prevence.",
      sos="ve vestibulu je schránka důvěry, podnět lze poslat i anonymně metodikovi prevence.",
      jed="schránka důvěry je na chodbě u kanceláře školy.",
      svo="schránka důvěry visí u vstupu, vybírá ji ředitel jednou týdně."),
    Q("GDPR", "Kdo je pověřenec pro ochranu osobních údajů?",
      ["Kdo je ochránce osobních údajů?",
       "Na koho se obrátit ohledně GDPR?",
       "poverenec gdpr skola"],
      org="pověřence pro ochranu osobních údajů má organizace společného, kontakt je na webu.",
      zs="pověřence má organizace společného pro všechny součásti, kontakt najdeš na webu školy.",
      sos="pověřence má organizace společného pro všechny součásti, kontakt je na webu.",
      jed="pověřence pro ochranu osobních údajů zajišťuje zřizovatel, kontakt je na webu školy.",
      svo="pověřence pro ochranu osobních údajů má škola smluvně, kontakt je v zápatí webu."),
    Q("GDPR", "Kde najdu pravidla pro zpracování osobních údajů žáků?",
      ["Kde je GDPR dokumentace školy?",
       "Jak škola nakládá s osobními údaji dětí?",
       "gdpr pravidla zpracovani udaju"],
      org="informace o zpracování osobních údajů zveřejňuje organizace na svém webu.",
      zs="informace o zpracování osobních údajů jsou na webu školy v sekci dokumenty, včetně souhlasů s fotografiemi.",
      sos="informace o zpracování osobních údajů jsou na webu v sekci dokumenty.",
      jed="pravidla pro zpracování osobních údajů jsou na webu školy mezi dokumenty.",
      svo="pravidla pro zpracování osobních údajů dostáváte při zápisu a jsou i na webu."),
    Q("Akce", "Kdy je lyžařský výcvik?",
      ["Termín lyžařského kurzu?",
       "Kdy jedeme na lyžák?",
       "kdy je lyzarsky vycvik"],
      org="termíny výcviků a kurzů zveřejňuje příslušná součást v kalendáři akcí.",
      zs="lyžařský výcvik je pro sedmý ročník, termín najdeš v kalendáři akcí školy.",
      sos="lyžařský kurz je pro první ročník, termín je v kalendáři akcí.",
      jed="lyžařský výcvik pořádáme pro sedmý ročník, termín oznamujeme v prosinci.",
      svo="zimní pobyt na horách je pro celou školu, termín je v rodičovském kalendáři."),
    Q("Akce", "Kdy je škola v přírodě?",
      ["Termín školy v přírodě?",
       "Kdy jedeme na školu v přírodě?",
       "skola v prirode termin"],
      org="školy v přírodě organizuje příslušná součást, termíny jsou v kalendáři akcí.",
      zs="škola v přírodě je pro první stupeň na jaře, konkrétní termín oznamuje třídní učitel.",
      sos="škola v přírodě se na střední škole nekoná, místo ní je adaptační kurz prvních ročníků.",
      jed="školu v přírodě jezdíme na jaře, termín upřesňujeme v březnu.",
      svo="expediční týden jezdíme dvakrát ročně, termíny jsou v kalendáři."),
    Q("Podpora", "Jak požádat o individuální vzdělávací plán?",
      ["Jak získat IVP?",
       "Co je potřeba pro individuální vzdělávací plán?",
       "zadost o ivp"],
      org="o individuální vzdělávací plán se žádá u ředitelky příslušné součásti na základě doporučení poradny.",
      zs="o IVP žádá zákonný zástupce ředitelku školy a přikládá doporučení školského poradenského zařízení.",
      sos="o IVP žádá student nebo zákonný zástupce ředitele, s doporučením poradny nebo z důvodu sportu.",
      jed="o IVP žádejte ředitelku školy, nutné je doporučení pedagogicko-psychologické poradny.",
      svo="individuální plán sestavuje průvodce s rodinou, doporučení poradny je vítané, ne povinné."),
    Q("Podpora", "Kdo je školní psycholog nebo speciální pedagog?",
      ["Máte ve škole psychologa?",
       "Na koho se obrátit s výukovými potížemi?",
       "skolni psycholog kontakt"],
      org="školní poradenské pracoviště je společné, kontakty jsou na webu organizace.",
      zs="školní poradenské pracoviště tvoří výchovný poradce, metodik prevence a speciální pedagog, kontakty jsou na webu.",
      sos="školní poradenské pracoviště tvoří výchovný poradce a metodik prevence, kontakty jsou na webu.",
      jed="poradenství zajišťuje výchovná poradkyně, kontakt je na webu školy.",
      svo="ve škole působí psycholožka jeden den v týdnu, termín domluvíte přes kancelář."),
]

# Entity-exclusive provoz questions: only this school answers them, so the same
# question from another school must fall through to RAG or no_answer.
PROVOZ_ONLY = {
    "SKOLA_NOVA_VES": [
        Q("Provoz", "Kdy je otevřena kancelář školy?",
          ["Jaká je úřední doba kanceláře?", "Kdy můžu přijít do kanceláře?",
           "otviraci doba kancelare"],
          org="kancelář organizace je otevřena v pracovní dny od 8:00 do 15:00."),
        Q("Provoz", "Komu patří školní jídelna?",
          ["Je jídelna společná pro ZŠ i SŠ?", "Kdo provozuje školní jídelnu?",
           "spolecna jidelna zs ss"],
          org="školní jídelna je jednou ze šesti zapsaných jednotek organizace a vaří pro ZŠ i SOŠ."),
    ],
    "SKOLA_NOVA_VES_ZS": [
        Q("Zápis", "Kdy je zápis do prvních tříd?",
          ["Termín zápisu do první třídy?", "Kdy proběhl zápis do prvních tříd?",
           "zapis do prvni tridy termin"],
          zs="zápis do prvních tříd je v dubnu, přesný termín a rezervační systém jsou na webu školy."),
        Q("Družina", "Jak funguje školní družina?",
          ["Do kolika je otevřená družina?", "Jak přihlásit dítě do družiny?",
           "skolni druzina provoz"],
          zs="družina je otevřená od 6:30 do 17:30, přihlášku odevzdáte vychovatelce první školní týden."),
    ],
    "SKOLA_NOVA_VES_SOS": [
        Q("Praxe", "Kdy probíhá odborná praxe?",
          ["Termín souvislé praxe?", "Kdy jdeme na praxi?", "odborna praxe termin"],
          sos="souvislá odborná praxe je ve druhém a třetím ročníku, vždy dva týdny na jaře."),
        Q("Provoz", "Můžu mít individuální studijní plán kvůli sportu?",
          ["Jde skloubit vrcholový sport se studiem?", "Individuální plán pro sportovce?",
           "isp sport stredni skola"],
          sos="ano, o individuální studijní plán ze sportovních důvodů žádáš ředitele a dokládáš potvrzení klubu."),
    ],
    "ZAKLADNI_SKOLA_BOROVA": [
        Q("Provoz", "Jak se dostanu do školy autobusem?",
          ["Jezdí do školy autobus?", "Jaké je spojení do školy?", "autobus do skoly borova"],
          jed="do školy jezdí linkové autobusy od okolních obcí, rozvrh spojů visí u vchodu."),
        Q("Akce", "Kdy je školní ples?",
          ["Termín školního plesu?", "Kdy pořádáte ples?", "skolni ples termin"],
          jed="školní ples pořádáme každoročně v únoru v sále kulturního domu."),
    ],
    "ZAKLADNI_SKOLA_LIPOVA": [
        Q("Zápis", "Jak probíhá zápis do školy Lipová?",
          ["Jak se k vám dostat?", "Jak přihlásit dítě k vám do školy?", "zapis skola lipova"],
          svo="zápis má tři kroky: informační schůzka, ukázkový den dítěte a rozhovor s rodinou."),
        Q("Provoz", "Co je to týdenní plán?",
          ["Jak funguje týdenní plán?", "K čemu slouží týdenní plán?",
           "tydenni plan skola lipova"],
          svo="týdenní plán je seznam úkolů, které si dítě samo rozvrhne do týdne a v pátek s průvodcem vyhodnotí."),
    ],
}
