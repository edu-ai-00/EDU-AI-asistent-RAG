#!/usr/bin/env node
// Optional documentation tooling; not used by the runtime or installer.
// Supply existing local Mermaid and Playwright installations explicitly.
const fs = require('node:fs/promises');
const path = require('node:path');

async function main() {
  const args = process.argv.slice(2);
  const options = {};
  for (let i = 0; i < args.length; i += 2) {
    if (!['--mermaid-js', '--playwright-module', '--browser-executable'].includes(args[i]) || !args[i + 1]) {
      throw new Error('Use --mermaid-js PATH --playwright-module PATH [--browser-executable PATH]');
    }
    options[args[i]] = path.resolve(args[i + 1]);
  }
  if (!options['--mermaid-js'] || !options['--playwright-module']) {
    throw new Error('Local Mermaid bundle and Playwright module paths are required.');
  }
  const { chromium } = require(options['--playwright-module']);
  const browser = await chromium.launch({
    headless: true,
    ...(options['--browser-executable'] ? { executablePath: options['--browser-executable'] } : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1800, height: 1400 }, deviceScaleFactor: 1 });
    await page.setContent('<html><body></body></html>');
    await page.addScriptTag({ path: options['--mermaid-js'] });
    await page.evaluate(() => mermaid.initialize({
      startOnLoad: false, securityLevel: 'strict', theme: 'base', htmlLabels: false,
      deterministicIds: true, deterministicIDSeed: 'edu-ai-architecture',
      fontFamily: 'Segoe UI, Arial, sans-serif',
      themeVariables: {
        fontSize: '18px', primaryColor: '#edf2fa', primaryTextColor: '#16364a',
        primaryBorderColor: '#466489', lineColor: '#527086', edgeLabelBackground: '#ffffff',
        clusterBkg: '#f8fafc', clusterBorder: '#b0c4d1',
      },
      flowchart: { htmlLabels: false, curve: 'linear', nodeSpacing: 30, rankSpacing: 48, padding: 16 },
    }));
    const output = path.resolve(__dirname, '../architecture/diagrams');
    for (const name of ['components', 'networks', 'perimeter', 'query-flow']) {
      const source = await fs.readFile(path.join(output, name + '.mmd'), 'utf8');
      let svg = await page.evaluate(async ({ source, id }) => (await mermaid.render(id, source)).svg,
        { source, id: name.replaceAll('-', '') });
      if (name === 'networks') {
        // Mermaid's block parser does not accept accTitle/accDescr yet.
        svg = svg.replace(/(<svg\b[^>]*>)/,
          '$1<title>Členství služeb v sítích Docker Compose</title><desc>Šest definovaných sítí a úplné členství služeb. App, data a tunnel jsou internal; Cloudflare je volitelný.</desc>');
      }
      await page.evaluate(svg => {
        document.body.innerHTML = svg;
        document.body.style.margin = '0';
        document.body.style.background = 'white';
        const el = document.querySelector('svg');
        const vb = el.viewBox.baseVal;
        el.setAttribute('width', vb.width);
        el.setAttribute('height', vb.height);
        el.style.maxWidth = 'none';
      }, svg);
      const box = await page.locator('svg').boundingBox();
      await page.setViewportSize({ width: Math.ceil(box.width), height: Math.ceil(box.height) });
      svg = await page.locator('svg').evaluate(el => new XMLSerializer().serializeToString(el));
      await fs.writeFile(path.join(output, name + '.svg'), svg);
      await page.locator('svg').screenshot({ path: path.join(output, name + '.png') });
      process.stdout.write(name + ': SVG + PNG (' + Math.ceil(box.width) + ' x ' + Math.ceil(box.height) + ')\n');
    }
  } finally {
    await browser.close();
  }
}
main().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
