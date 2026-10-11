# Renderování architektonických diagramů

Editovatelné zdroje jsou v [architecture/diagrams](../architecture/diagrams/).
[Architektonický přehled](../architecture/deployment.md) vkládá jejich SVG; PNG slouží pro nástroje,
které SVG nepodporují. Zdrojový Mermaid je uložený samostatně, proto GitHub nemusí renderovat
Mermaid bloky přímo v Markdownu.

Volitelný [lokální helper](render_architecture.cjs) používá již dostupné Mermaid a Playwright.
Neinstaluje balíčky, nemění stack a nepotřebuje Docker ani přístup k VM. Pro tuto sadu byl použit
Mermaid 11.12.1 a Playwright 1.62.1 s lokálním Chrome 154.0.8037.99. Jiná verze nebo fonty
mohou změnit rozměry; po každé regeneraci je nutná vizuální kontrola.

Z kořene checkoutu, s vlastními lokálními cestami:

```bash
node docs/tools/render_architecture.cjs \
  --mermaid-js /path/to/mermaid.min.js \
  --playwright-module /path/to/node_modules/playwright \
  --browser-executable /path/to/chrome
```

Poslední argument lze vynechat, pokud Playwright má svůj browser. Helper regeneruje čtyři
SVG a PNG do stávajícího adresáře; renderer ani závislosti se nekopírují do runtime image.
SVG používají nativní text bez externích obrázků a bez executable promptů. Síťové SVG má
přidaný title/desc, protože block-beta parser Mermaid tyto direktivy dosud nepřijímá.

Po regeneraci otevřete všechny čtyři PNG/SVG, zkontrolujte překryvy textu a šipek,
spusťte `make docs-check` a prohlédněte `git diff --check`. SVG a PNG neupravujte ručně.

Diagramy se aktualizují při změně Compose sítí/portů, nginx cest nebo logiky executoru.
Aktivní organizace může mít jinou pipeline; neprezentujte zdrojový diagram jako výpis živé konfigurace.
