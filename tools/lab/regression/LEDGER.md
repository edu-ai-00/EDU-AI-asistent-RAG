# Deník pokusů — co jsme zkusili a jak to dopadlo

*Každý pokus (přijatý i zamítnutý) s čísly, aby se nezkoušel znovu bez nového důvodu. Skóre: jistě správně +1, správně v nabídce +0,5, jistě špatně −3. Pokud není uvedeno jinak, jde o napsané sady A (1 470) a B (1 240), jen QA.*

| Datum | Pokus | Výsledek | Rozhodnutí |
|---|---|---|---|
| 21.–22. 10. | zápor v lemmatu, kontrola shody záporu, stop-slova podle původního tvaru, doplnění diakritiky | přehrání logu: nikdy jiná odpověď z QA, +21 správných | přijato (větev) |
| 26. 10. | jen kmen místo lemmatu | log: −33 správných, +51 omylů („pořadí“ → „dohled“) | zamítnuto |
| 26. 10. | hovorové koncovky („testama“) | A +10, B +1,5: přeučení | zamítnuto |
| 26. 10. | pravidlo remíz podle rozdílu shody (`coverage_gap`) | skóre dolů, log +7/−9 | zamítnuto |
| 26. 10. | `coverage_reverse`, `stem_backoff` | B 398 (↓) / 444 (↓ pod lemma) | zamítnuto |
| 27. 10. | **219** QA hledá jen podle lemmatu | B 400,5 → 409, jisté omyly 13 → 9; A 742 → 732 | přijato |
| 27. 10. | **218** zdvořilostní a výplňová slova mimo pokrytí | B 409 → 433, A 732 → 816,5 | přijato |
| 27. 10. | **220** oprava překlepů v dotazu | B 433 → 462, A 816,5 → 833,5 | přijato |
| 27. 10. | synonyma uzpůsobení, český | celkem 1 295,5 → 1 295,0 (šum), +1 jistý omyl; přínos +20 z logu byl z týchž dotazů | zamítnuto (soubor mimo repozitář) |
| 27. 10. | prahy `t_answer` 0,45–0,8 | dnešní 0,60 téměř optimum; 0,55: B +12, ale A +8 jistých omylů | beze změny |
| 27. 10. | `m_margin` 0–0,2 | 0,04: +3 (šum); 0: 51 jistých omylů na A | beze změny |
| 27. 10. | `t_suggest` 0,2 / 0,4 | skóre +56 na B jen přesunem „záloha → nabídka“, „uvidí“ +1 bod | beze změny |
| 27. 10. | **221** `suggest_top_n` 3 → 5 | uvidí správnou odpověď A 84 → 87 %, B 74 → 77 %, jisté odpovědi se nemění | přijato |
| 27. 10. | hledání i v textu odpovědi | odpovědi ~138 znaků; sdílí slovo 58 z 276 zmeškaných, v top 5 jen 18 | odloženo |
| 27. 10. | lokální model `multilingual-e5-small` jako doplněk nabídky (bez limitu tlačítek) | uvidí B 77 → 87 %, A 86 → 96 %; jako rozhodovač jisté odpovědi nestačí | pokračovat (fáze 3) |
| 27. 10. | sady N, C, D a dělení train/holdout | N: 443 z 445 otázek mimo obsah bez jisté odpovědi; C: jen 17 použitelných kliků | základ pro fáze 2–4 |
| 27. 10. | **sada D** (221 otázek psaných člověkem podle odpovědí) — první měření, stav po migracích 218–221 | uvidí 49 % (A 86 %, B 77 %), jistě správně 13 %, jistě špatně 2,9 %; krátké hovorové otázky jen 39 % | A a B nadhodnocovaly; cíle přepsány |
| 27. 10. | opravy štítků po prohlídce jistých omylů (`sada_fixes.json`) | D: 2 otázky „BEZ“ o jídelně přeznačeny na existující odpověď; N: 1 otázka o šikaně vyřazena (škola odpověď má) | jen chybné štítky, výsledky se neupravovaly |
