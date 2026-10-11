"""XSS / Markdown-sanitisation flow against a LIVE portal (STATUS §17.5 item 4).

Deliberately NOT mocked: exercises the real MarkdownPreview component
(rehype-sanitize) in a real browser, the same way test_portal_browser.py
exercises the rest of the SPA. Mock-only checks are not accepted as proof here.

    python test_portal_xss.py BASE_URL [SCHOOL]

BASE_URL must be the portal origin the browser will use (e.g. the isolated
dev stack's nginx listener). Phase 1 uses the first existing entry in the
first pack for the school and never persists anything. Phase 2 additionally
publishes ONE throwaway pack, so point this only at an isolated stack.
"""
import re
import sys
import uuid

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
SCHOOL = sys.argv[2] if len(sys.argv) > 2 else "ZAKLADNI_SKOLA_BOROVA"
PACK = f"xss-{uuid.uuid4().hex[:8]}"

failures: list[str] = []
checks: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (checks if ok else failures).append(f"{name}{(' — ' + detail) if detail else ''}")
    print(("PASS  " if ok else "FAIL  ") + name + ((" — " + detail) if detail else ""))


def select_school(page, code: str) -> None:
    page.locator("select").first.select_option(code)
    page.wait_for_timeout(1200)


# name, payload, sentinel var it would set if it executed, substring that must
# still be visible (escaped) somewhere in the preview so a blank preview
# cannot pass by accident, and whether CONTRACT.md §9 requires the
# html-stripped notice instead (raw HTML the sanitizer drops entirely,
# leaving nothing of the payload for react-markdown to render as text).
PAYLOADS = [
    ("1 img onerror", '<img src=x onerror="window.__xss_img=1">', "__xss_img", "onerror", True),
    ("2 script tag", "<script>window.__xss_script=1</script>", "__xss_script", "script", True),
    ("3 javascript link", "[klikni](javascript:window.__xss_link=1)", "__xss_link", "klikni", False),
    ("4 onclick anchor", '<a href="#" onclick="window.__xss_click=1">klik</a>', "__xss_click", "klik", False),
    ("5 iframe js src", '<iframe src="javascript:window.__xss_frame=1"></iframe>', "__xss_frame", "iframe", True),
]

dialogs_seen: list[str] = []
console_errors: list[str] = []


def run_payload_checks(page, label: str, sentinel: str, expect_visible: str,
                        expect_notice: bool = False) -> None:
    val = page.evaluate(f"window.{sentinel}")
    check(f"{label}: sentinel window.{sentinel} not set", val in (None, False), f"got {val!r}")

    preview = page.locator(".markdown-preview")
    check(f"{label}: preview container present", preview.count() > 0)

    bad_els = preview.locator("script, iframe, [onerror], [onclick]").count()
    check(f"{label}: no script/iframe/onerror/onclick element in preview DOM", bad_els == 0, f"{bad_els} matches")

    hrefs = preview.locator("a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    js_hrefs = [h for h in hrefs if h and h.strip().lower().startswith("javascript:")]
    check(f"{label}: no rendered anchor has a javascript: href", not js_hrefs, str(js_hrefs))

    if expect_notice:
        # CONTRACT.md §9: raw HTML that rehype-sanitize drops entirely (no
        # element survives) must not leave a silently blank preview — the
        # sibling notice must say so, visibly, with real text.
        notice = page.locator('[data-testid="markdown-html-stripped"]')
        check(f"{label}: html-stripped notice present", notice.count() > 0)
        if notice.count():
            check(f"{label}: html-stripped notice visible", notice.first.is_visible())
            notice_text = notice.first.inner_text().strip()
            check(f"{label}: html-stripped notice has non-empty text", bool(notice_text))
    else:
        html = preview.evaluate("el => el.innerHTML")
        text = preview.inner_text()
        visible = expect_visible in html or expect_visible in text
        check(f"{label}: payload text still visible (escaped), preview not blanked", visible,
              f"innerHTML={html[:200]!r}")


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    def on_dialog(dialog):
        dialogs_seen.append(f"{dialog.type}: {dialog.message}")
        dialog.dismiss()

    page.on("dialog", on_dialog)

    # ── PHASE 1: reuse the existing school/pack/entry-opening steps from
    #    test_portal_browser.py — nothing published, pure client-side render.
    page.goto(f"{BASE}/qa/", wait_until="networkidle")
    select_school(page, SCHOOL)
    selects = page.locator("select")
    pack_select = selects.nth(1)
    check("setup: packs listed for the school", pack_select.locator("option").count() >= 1)
    pack_select.select_option(index=0)
    page.wait_for_timeout(1500)

    first_entry = page.locator("li button, li a").first
    check("setup: an entry exists to open", first_entry.count() > 0)
    if first_entry.count():
        first_entry.click()
        page.wait_for_timeout(800)

    answer_box = page.get_by_role("textbox", name="Odpověď", exact=False)
    check("setup: editor answer field opens", answer_box.count() > 0)

    preview_btn = page.get_by_role("button", name="Náhled odpovědi", exact=True)
    back_btn = page.get_by_role("button", name="Zpět do editoru", exact=True)

    if answer_box.count():
        for label, payload, sentinel, expect_visible, expect_notice in PAYLOADS:
            answer_box.first.fill(payload)
            page.wait_for_timeout(200)
            preview_btn.first.click()
            page.wait_for_timeout(400)

            run_payload_checks(page, label, sentinel, expect_visible, expect_notice)

            # Payload 4 specifically asks us to click the rendered anchor,
            # if the sanitizer let any anchor with that text through.
            if label.startswith("4"):
                clickable = page.locator(".markdown-preview a", has_text="klik")
                if clickable.count():
                    clickable.first.click()
                    page.wait_for_timeout(300)
                val_after_click = page.evaluate(f"window.{sentinel}")
                check(f"{label}: sentinel still unset after clicking the rendered anchor",
                      val_after_click in (None, False), f"got {val_after_click!r}")

            back_btn.first.click()
            page.wait_for_timeout(200)

        check("no dialog (alert/confirm) appeared while injecting payloads",
              not dialogs_seen, "; ".join(dialogs_seen))

        # ── acceptance case: real markdown must still render, not just vanish
        answer_box.first.fill("**tučně** a odkaz: https://www.zsborova.cz/informace")
        page.wait_for_timeout(200)
        preview_btn.first.click()
        page.wait_for_timeout(400)
        preview = page.locator(".markdown-preview")
        strong_count = preview.locator("strong").count()
        check("acceptance: **tučně** still renders as <strong>", strong_count >= 1, f"{strong_count} <strong>")
        accept_hrefs = preview.locator("a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
        ok_https = any(h and h.startswith("https://") for h in accept_hrefs)
        check("acceptance: https link still renders as a working anchor", ok_https, str(accept_hrefs))
        accept_notice = page.locator('[data-testid="markdown-html-stripped"]')
        check("acceptance: no html-stripped notice for a plain answer with no raw HTML",
              accept_notice.count() == 0, f"{accept_notice.count()} notice(s) found")
        back_btn.first.click()
        page.wait_for_timeout(200)
    else:
        check("phase 1 payload loop skipped", False, "no answer textbox found — cannot proceed")

    check("no console errors after phase 1", not console_errors, "; ".join(console_errors[:3]))

    # ── PHASE 2: publish a throwaway pack carrying two payloads in its
    #    stored answer, reload, and re-check what comes back from the API.
    phase2_attempted = False
    try:
        phase2_attempted = True
        page.get_by_role("button", name="Nový balíček", exact=False).first.click()
        page.wait_for_timeout(1200)
        dialog = page.get_by_role("dialog")
        radios = dialog.get_by_role("radio")
        roles = dialog.get_by_role("checkbox")
        dialog.get_by_role("textbox").first.fill(PACK)
        radios.first.check()
        roles.first.check()
        page.wait_for_timeout(200)
        create = dialog.get_by_role("button", name="Vytvořit balíček", exact=True)
        check("phase2: create enabled once metadata confirmed", not create.is_disabled())
        create.click()
        page.wait_for_selector("[role=dialog]", state="detached", timeout=20000)

        add = page.get_by_role("button", name="Přidat otázku", exact=False)
        add.first.wait_for(state="visible", timeout=15000)
        add.first.click()
        question = page.get_by_role("textbox", name="Otázka", exact=False)
        stored_answer_box = page.get_by_role("textbox", name="Odpověď", exact=False)
        question.first.wait_for(state="visible", timeout=15000)
        stored_payload = '<img src=x onerror="window.__xss_img=1"><script>window.__xss_script=1</script>'
        question.first.fill("XSS stored payload check")
        stored_answer_box.first.fill(stored_payload)
        page.wait_for_timeout(500)

        publish = page.get_by_role("button", name="Zveřejnit změny", exact=True)
        check("phase2: publish button present", publish.count() > 0)
        published = False
        if publish.count():
            publish.first.click()
            page.wait_for_timeout(2000)
            confirm = page.get_by_role("button", name="Potvrdit", exact=True)
            if confirm.count():
                confirm.first.click()
                for _ in range(45):
                    page.wait_for_timeout(1000)
                    if page.get_by_text("Dokončeno", exact=False).count():
                        published = True
                        break
        check("phase2: throwaway pack publish completed", published)

        if published:
            # Force a real reload so what we see next came back from the API,
            # not the in-memory draft that was just typed.
            page.goto(f"{BASE}/qa/", wait_until="networkidle")
            select_school(page, SCHOOL)
            selects2 = page.locator("select")
            pack_select2 = selects2.nth(1)
            pack_select2.select_option(PACK)
            page.wait_for_timeout(1500)

            stored_entry = page.get_by_role("listitem").filter(has_text="XSS stored payload check")
            if not stored_entry.count():
                stored_entry = page.locator("li", has_text="XSS stored payload check")
            check("phase2: published entry visible after reload", stored_entry.count() > 0)
            if stored_entry.count():
                stored_entry.first.locator("button, a").first.click()
                page.wait_for_timeout(800)
                preview_btn2 = page.get_by_role("button", name="Náhled odpovědi", exact=True)
                preview_btn2.first.click()
                page.wait_for_timeout(400)
                run_payload_checks(page, "phase2 img (stored)", "__xss_img", "onerror", expect_notice=True)
                run_payload_checks(page, "phase2 script (stored)", "__xss_script", "script")
        else:
            check("phase2: cannot verify stored/rendered content — publish did not complete", False)
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the report
        check("phase2: completed without an unexpected error", False, f"{exc!r}")

    check("no console errors for the whole run", not console_errors, "; ".join(console_errors[:3]))

    browser.close()

print("\n--- SUMMARY ---")
print(f"passed: {len(checks)}   failed: {len(failures)}   phase2_pack: {PACK}")
for f in failures:
    print("FAILED:", f)
sys.exit(1 if failures else 0)
