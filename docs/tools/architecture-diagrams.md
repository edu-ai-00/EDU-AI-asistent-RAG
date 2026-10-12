# Rendering the architecture diagrams

The editable sources are in [architecture/diagrams](../architecture/diagrams/).
The [architecture overview](../architecture/deployment.md) embeds their SVGs; the PNGs serve tools
that do not support SVG. The Mermaid source is stored separately, so GitHub may not render
Mermaid blocks directly in Markdown.

The optional [local helper](render_architecture.cjs) uses the already available Mermaid and Playwright.
It installs no packages, does not change the stack and needs neither Docker nor access to a VM. For this set,
Mermaid 11.12.1 and Playwright 1.62.1 with a local Chrome 154.0.8037.99 were used. A different version or fonts
may change the dimensions; a visual check is necessary after every regeneration.

From the checkout root, with your own local paths:

```bash
node docs/tools/render_architecture.cjs \
  --mermaid-js /path/to/mermaid.min.js \
  --playwright-module /path/to/node_modules/playwright \
  --browser-executable /path/to/chrome
```

The last argument can be omitted if Playwright has its own browser. The helper regenerates the four
SVGs and PNGs into the existing directory; neither the renderer nor the dependencies are copied into the runtime image.
The SVGs use native text without external images and without executable prompts. The network SVG has
an added title/desc, because the Mermaid block-beta parser does not accept these directives yet.

After regeneration, open all four PNG/SVG files, check the overlaps of text and arrows,
run `make docs-check` and inspect `git diff --check`. Do not edit the SVG and PNG by hand.

The diagrams are updated when the Compose networks/ports, the nginx paths or the executor logic change.
An active organisation may have a different pipeline; do not present the source diagram as a dump of the live configuration.
