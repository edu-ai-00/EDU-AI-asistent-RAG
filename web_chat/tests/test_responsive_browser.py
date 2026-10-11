"""Isolated layout regression; mock API responses, no database or model calls.

Run with Playwright installed: python test_responsive_browser.py BASE_URL
"""
import sys
from playwright.sync_api import sync_playwright

base = sys.argv[1]
with sync_playwright() as p:
    browser = p.chromium.launch()
    for picker in (False, True):
        for width, height in ((320, 568), (390, 844), (640, 700), (768, 700), (1280, 800)):
            page = browser.new_page(viewport={"width": width, "height": height})
            page.route("**/chat/api/config", lambda route: route.fulfill(json={
                "entity_picker": picker, "default": "TEST",
                "entities": [{"code": "TEST", "name": "Škola s velmi dlouhým názvem — základní škola"}],
            }))
            page.route("**/chat/api/domains*", lambda route: route.fulfill(json={
                "legal_entity": "TEST", "entity_picker": picker,
                "domains": [{"code": "TEST_AREA", "labels": {"cs": "Přijímačky na střední školu"}}],
            }))
            page.route("**/chat/api/message", lambda route: route.fulfill(json={
                "message_id": "test", "conversation_id": "test", "answer": "Dlouhý odkaz " + "x" * 300,
                "outcome": "answer", "answer_source": "qa", "language": "cs", "duration_ms": 1,
                "suggestions": [{"kind": "related", "entry_id": "1", "question": "Jakým způsobem dostanu pozvánku k přijímací zkoušce?", "options": []}],
                "sources": [{"title": "Zdroj " + "z" * 200, "url": "https://example.com"}],
            }))
            page.goto(base + "/chat/")
            trigger = page.locator(".domain-picker button[aria-haspopup]")
            trigger.wait_for()
            page.wait_for_function("!document.querySelector('.domain-picker button').disabled")
            trigger.click()
            menu = page.locator(".domain-menu").bounding_box()
            assert menu and menu["x"] >= 0 and menu["x"] + menu["width"] <= width + 1
            trigger.click()
            for _ in range(3):
                page.locator(".composer-input").fill("Testovací otázka")
                page.locator(".send-button").click()
                page.locator(".message--assistant").nth(_).wait_for()
            page.locator(".sources summary").last.click()
            page.locator(".composer-input").scroll_into_view_if_needed()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (picker, width)
            for selector in (".header-actions", ".composer", ".send-button", ".message--assistant"):
                for box in page.locator(selector).all():
                    rect = box.bounding_box()
                    assert rect["x"] >= 0 and rect["x"] + rect["width"] <= width + 1, (width, selector, rect)
            rect = page.locator(".composer").bounding_box()
            assert rect["y"] >= 0 and rect["y"] + rect["height"] <= height + 1, rect
            print(f"PASS picker={picker} {width}x{height}")
            page.close()
    browser.close()
