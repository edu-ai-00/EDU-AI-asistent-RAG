"""School/domain selection must not move controls, menu or transcript."""
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    for width in (390, 700, 960, 1280):
        page = browser.new_page(viewport={"width": width, "height": 844})
        page.route("**/chat/api/config", lambda r: r.fulfill(json={
            "entity_picker": True, "default": "ONE", "entities": [
                {"code": "ONE", "name": "První škola"},
                {"code": "TWO", "name": "Škola s dlouhým názvem — základní škola a střední odborná škola"},
            ],
        }))
        catalog = {"legal_entity": "ONE", "entity_picker": True, "domains": [
            {"code": "SHORT", "labels": {"cs": "Výuka"}},
            {"code": "LONG", "labels": {"cs": "Přijímačky na střední školu"}},
        ]}
        pending = []
        def domains(r):
            if "TWO" in r.request.url:
                pending.append(r)
            else:
                r.fulfill(json=catalog)
        page.route("**/chat/api/domains*", domains)
        page.goto(sys.argv[1] + "/chat/")
        trigger = page.locator(".domain-picker > button")
        page.wait_for_function("document.querySelector('.domain-picker button')?.getAttribute('aria-haspopup')")
        def geometry():
            return page.evaluate("""() => ['.app-header', '.thread', '.composer',
                '.header-actions > label:nth-of-type(3)', '.domain-picker'].map(s => {
                    const r = document.querySelector(s).getBoundingClientRect();
                    return [r.x, r.y, r.width, r.height];
                })""")
        initial = geometry()
        states = []
        trigger.click()
        menu_before = page.locator('.domain-menu').bounding_box()
        page.locator('.domain-option input[type=checkbox]').nth(1).check()
        states.append(geometry())
        menu_after = page.locator('.domain-menu').bounding_box()
        page.locator('.domain-option input[type=checkbox]').nth(0).check()
        states.append(geometry())
        page.locator('.domain-option input[type=radio]').check()
        states.append(geometry())
        page.keyboard.press('Escape')
        page.locator('.header-actions > label:nth-of-type(3) select').select_option('TWO')
        page.wait_for_timeout(100)
        states.append(geometry())  # loading label must not resize the header
        assert pending
        pending.pop().fulfill(json={**catalog, "legal_entity": "TWO"})
        page.wait_for_function("document.querySelector('.domain-picker button')?.getAttribute('aria-haspopup')")
        states.append(geometry())
        drift = max(abs(a-b) for state in states for old, new in zip(initial, state) for a,b in zip(old,new))
        print(f'{width}px: largest layout shift {drift:.1f}px', flush=True)
        assert drift < 2, (initial, states)
        assert abs(menu_before['x'] - menu_after['x']) < 2
        assert abs(menu_before['y'] - menu_after['y']) < 2
        page.close()
    browser.close()
