"""Browser flows for the QA portal, against a LIVE portal (CONTRACT.md §10, §12).

Deliberately NOT mocked: the point of this file is to prove the real SPA talks
to the real API. Mock-only checks already live in the vitest suite.

    python test_portal_browser.py BASE_URL [SCHOOL]

BASE_URL must be the portal origin the browser will use, e.g. the dev stack's
nginx VPN listener. Runs read-only flows plus ONE publish of a throwaway pack,
so point it at an isolated stack, never at production.
"""
import json
import sys
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
SCHOOL = sys.argv[2] if len(sys.argv) > 2 else "ZAKLADNI_SKOLA_BOROVA"
PACK = f"browser-{uuid.uuid4().hex[:8]}"

failures: list[str] = []
checks: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (checks if ok else failures).append(f"{name}{(' — ' + detail) if detail else ''}")
    print(("PASS  " if ok else "FAIL  ") + name + ((" — " + detail) if detail else ""))


def select_school(page, code: str) -> None:
    page.locator("select").first.select_option(code)
    page.wait_for_timeout(1200)


def dialog_scroll_checks(page, width: int, height: int, letters: str) -> None:
    """Replaces the old 12a/12b/12c false positives (CONTRACT.md §10.1): those
    read a CSS declaration and used scroll_into_view_if_needed()+is_visible()/
    bounding_box(), which stay green even when an ancestor's `overflow:hidden`
    makes the control genuinely unreachable. Every check here either measures
    real scroll state after a real interaction, or performs a real click/press
    and lets Playwright's own hit-testing decide reachability.

    `letters` supplies 8 distinct ids (e.g. "abcefghi", skipping "d" — the
    original standalone "12d no horizontal overflow" check keeps that literal
    id and is called separately, once per viewport, right after this helper)
    so results at the two viewports this regression was reported at
    (390x640, 1280x620) don't overlap in the report.
    """
    a, b, c, e, f, g, h, i = letters

    page.set_viewport_size({"width": width, "height": height})
    page.get_by_role("button", name="+ Nový balíček…").click()
    page.wait_for_selector(".dialog-body", timeout=10000)
    # The grade options arrive from /metadata-options after the dialog paints;
    # counting before they land measured nothing and passed by luck.
    page.wait_for_selector(".dialog-body .checkbox-grid input[type=checkbox]", timeout=20000)
    tag = f"{width}x{height}"

    def dims():
        return page.evaluate(
            "() => { const b = document.querySelector('.dialog-body');"
            " return b ? {sh: b.scrollHeight, ch: b.clientHeight, st: b.scrollTop} : null; }"
        )

    def set_scroll_top(value):
        page.evaluate(
            "(v) => { const b = document.querySelector('.dialog-body'); if (b) b.scrollTop = v; }",
            value,
        )

    # ── real overflow: proof there is something to scroll ─────────────
    d0 = dims()
    has_overflow = bool(d0) and d0["sh"] > d0["ch"]
    check(f"12{a} dialog body has real overflow at {tag}", has_overflow, str(d0))

    # ── programmatic scrollTop actually moves, and reaches the bottom ──
    set_scroll_top(999999)
    d1 = dims()
    max_scroll = (d1["sh"] - d1["ch"]) if d1 else 0
    programmatic_ok = bool(d1) and d1["st"] > 0 and abs(d1["st"] - max_scroll) <= 2
    check(f"12{b} programmatic scrollTop reaches the bottom at {tag}", programmatic_ok, str(d1))
    set_scroll_top(0)

    # ── wheel: a real wheel event over the body moves it ───────────────
    body_box = page.locator(".dialog-body").bounding_box()
    if body_box:
        page.mouse.move(body_box["x"] + body_box["width"] / 2, body_box["y"] + body_box["height"] / 2)
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(200)
    wheel_top = dims()
    wheel_ok = bool(wheel_top) and wheel_top["st"] > 0
    check(f"12{c} mouse wheel scrolls the dialog body at {tag}", wheel_ok, str(wheel_top))
    set_scroll_top(0)

    # ── keyboard: focus the scroll region (tabindex=0), press End ──────
    page.evaluate("() => { const b = document.querySelector('.dialog-body'); if (b) b.focus(); }")
    page.keyboard.press("End")
    page.wait_for_timeout(150)
    key_top = dims()
    key_ok = bool(key_top) and key_top["st"] > 0
    check(f"12{e} keyboard End scrolls the focused dialog body at {tag}", key_ok, str(key_top))
    set_scroll_top(0)

    # ── touch ───────────────────────────────────────────────────────────
    # A pressed-and-dragged MOUSE does not scroll an overflow container in
    # Chromium — it selects text — so mouse.down()/move()/up() would fail here
    # for a reason that has nothing to do with the dialog. The honest way to
    # test a finger is CDP's synthesizeScrollGesture with a touch source: it
    # produces a real, trusted touch scroll the compositor handles like a
    # user's. A failure of the CDP call itself is reported as a failure, not
    # swallowed, so a broken harness can never read as a passing check.
    drag_detail = ""
    if body_box:
        try:
            cx = int(body_box["x"] + body_box["width"] / 2)
            cy = int(body_box["y"] + body_box["height"] / 2)
            cdp = page.context.new_cdp_session(page)
            # A real finger: touchStart, a run of touchMoves, touchEnd, sent as
            # trusted browser input over CDP. Measured alternatives that do NOT
            # work here and must not be reused: a pressed mouse drag (it selects
            # text, it does not scroll) and Input.synthesizeScrollGesture with
            # gestureSourceType "touch" (it left scrollTop at 0). The context
            # must be created with has_touch=True or the events are dropped.
            cdp.send("Input.dispatchTouchEvent", {
                "type": "touchStart",
                "touchPoints": [{"x": cx, "y": cy + 150}]})
            for dy in range(20, 181, 20):
                cdp.send("Input.dispatchTouchEvent", {
                    "type": "touchMove",
                    "touchPoints": [{"x": cx, "y": cy + 150 - dy}]})
                page.wait_for_timeout(16)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd",
                                                  "touchPoints": []})
            page.wait_for_timeout(400)
        except Exception as exc:          # noqa: BLE001 — reported, not hidden
            drag_detail = f"CDP touch gesture failed: {exc}"
    drag_top = dims()
    drag_ok = not drag_detail and bool(drag_top) and drag_top["st"] > 0
    check(f"12{f} a touch drag scrolls the dialog body at {tag}", drag_ok,
          drag_detail or str(drag_top))
    set_scroll_top(0)

    # ── reachability of the LAST grade checkbox, by a real click ───────
    grades = page.locator(".dialog-body .checkbox-grid input[type=checkbox]")
    grade_count = grades.count()
    last_checked = False
    click_detail = f"{grade_count} checkboxes"
    if grade_count:
        last = grades.last
        try:
            last.click(timeout=5000)
            last_checked = last.is_checked()
        except Exception as exc:  # noqa: BLE001 - reachability failure is the finding
            click_detail = f"{grade_count} checkboxes, click failed: {exc}"
    check(f"12{g} the last grade checkbox is reachable by a real click at {tag}",
          grade_count > 0 and last_checked, click_detail)
    set_scroll_top(0)

    # ── confirm button lives in the fixed footer, reachable without scroll ──
    confirm = page.locator(".dialog-footer button", has_text="Vytvořit balíček").first
    in_body = confirm.evaluate("(el) => !!el.closest('.dialog-body')")
    box0 = confirm.bounding_box()
    box0_inside = bool(box0) and box0["x"] >= 0 and box0["y"] >= 0 \
        and box0["x"] + box0["width"] <= width + 1 and box0["y"] + box0["height"] <= height + 1
    check(f"12{h} confirm button is outside the scroll body and visible without scrolling at {tag}",
          (not in_body) and box0_inside, f"in_body={in_body} box={box0}")

    # ── and its position is unchanged once the body is scrolled to the bottom ──
    set_scroll_top(999999)
    page.wait_for_timeout(150)
    box1 = confirm.bounding_box()
    unchanged = bool(box0) and bool(box1) and abs(box0["x"] - box1["x"]) < 1 and abs(box0["y"] - box1["y"]) < 1
    check(f"12{i} confirm button position is unchanged after scrolling the body at {tag}",
          unchanged, f"before={box0} after={box1}")
    set_scroll_top(0)

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check(f"12d no horizontal overflow with the dialog open at {tag}", overflow <= 1,
          f"overflow={overflow}px")

    page.keyboard.press("Escape")
    page.wait_for_timeout(300)


with sync_playwright() as p:
    browser = p.chromium.launch()
    # has_touch: without it Chromium reports maxTouchPoints=0 and simply
    # drops a synthesized TOUCH scroll gesture, so the finger could not be
    # tested at all. Mouse and keyboard behave the same either way.
    ctx = browser.new_context(viewport={"width": 1440, "height": 950},
                              has_touch=True)
    page = ctx.new_page()
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    # ── 1. shell loads from the real API ──────────────────────────────
    page.goto(f"{BASE}/qa/", wait_until="networkidle")
    selects = page.locator("select")
    school_options = selects.first.locator("option").count()
    check("1 shell loads with real schools", school_options >= 2, f"{school_options} schools")

    # ── 2. select school + pack, entries come from the database ───────
    select_school(page, SCHOOL)
    pack_select = selects.nth(1)
    pack_count = pack_select.locator("option").count()
    check("2 packs listed for the school", pack_count >= 1, f"{pack_count} packs")
    # A skipped check reads as a pass in the tally, so a missing precondition
    # is reported as the failure it is.
    if not pack_count:
        check("2b entry list renders", False, "no pack to select")
    else:
        pack_select.select_option(index=0)
        page.wait_for_timeout(1500)
        entries = page.get_by_role("listitem").count() or page.locator("li").count()
        check("2b entry list renders", entries >= 1, f"{entries} list items")

    # ── 3. editor opens and takes a draft edit ────────────────────────
    first_entry = page.locator("li button, li a").first
    check("3a there is an entry to open", first_entry.count() > 0)
    if first_entry.count():
        first_entry.click()
        page.wait_for_timeout(800)
        # Scoped to the editor's own labelled field: a stray textbox elsewhere
        # on the page must not make this pass.
        editor_answer = page.get_by_role("textbox", name="Odpověď", exact=False)
        check("3 editor opens for an entry", editor_answer.count() > 0)
    else:
        check("3 editor opens for an entry", False, "no entry to click")

    # ── 4. new pack dialog offers the real code lists (§2.4a) ─────────
    page.get_by_role("button", name="Nový balíček", exact=False).first.click()
    page.wait_for_timeout(1200)
    dialog = page.get_by_role("dialog")
    roles = dialog.get_by_role("checkbox")
    radios = dialog.get_by_role("radio")
    check("4 new-pack dialog lists roles from code lists", roles.count() >= 3, f"{roles.count()} checkboxes")
    check("4b visibility is an explicit choice", radios.count() == 2, f"{radios.count()} radios")

    # Exact label on purpose: a loose match falls back to the dialog's "x"
    # close button, which "passes" every assertion while testing nothing.
    create = dialog.get_by_role("button", name="Vytvořit balíček", exact=True)
    check("4c create is disabled before metadata is confirmed",
          create.count() == 1 and create.is_disabled())

    # ── 5. create the pack, add an entry, publish it for real ─────────
    dialog.get_by_role("textbox").first.fill(PACK)
    radios.first.check()
    roles.first.check()
    page.wait_for_timeout(200)
    check("5 create enabled once metadata is confirmed", not create.is_disabled())
    create.click()
    page.wait_for_selector("[role=dialog]", state="detached", timeout=20000)
    check("5b dialog closed after create", page.get_by_role("dialog").count() == 0)

    add = page.get_by_role("button", name="Přidat otázku", exact=False)
    add.first.wait_for(state="visible", timeout=15000)
    check("5d add-question button available on the empty new pack", add.count() >= 1)
    add.first.click()
    question = page.get_by_role("textbox", name="Otázka", exact=False)
    answer_box = page.get_by_role("textbox", name="Odpověď", exact=False)
    question.first.wait_for(state="visible", timeout=15000)
    if question.count() and answer_box.count():
        question.first.fill("Kdy je ředitelské volno?")
        answer_box.first.fill("Termíny zveřejňujeme na webu školy vždy měsíc předem.")
        page.wait_for_timeout(500)

    # ── 6. multi-domain selection, checked WHILE the editor is open: a
    #       successful publish closes the editor, so asserting afterwards
    #       measures the wrong moment.
    domain_boxes = page.get_by_role("checkbox")
    check("6 domain picker present in the editor", domain_boxes.count() >= 2,
          f"{domain_boxes.count()} checkboxes")
    if domain_boxes.count() >= 2:
        domain_boxes.nth(0).check()
        domain_boxes.nth(1).check()
        page.wait_for_timeout(300)
        check("6b two domains can be selected at once",
              domain_boxes.nth(0).is_checked() and domain_boxes.nth(1).is_checked())

    published = False
    prev = page.get_by_role("button", name="Zveřejnit změny", exact=True)
    if prev.count():
        prev.first.click()
        page.wait_for_timeout(3000)
        confirm = page.get_by_role("button", name="Potvrdit", exact=True)
        if confirm.count():
            confirm.first.click()
            for _ in range(45):
                page.wait_for_timeout(1000)
                if page.get_by_text("Dokončeno", exact=False).count():
                    published = True
                    break
    check("5c the editor reports the publish as completed", published)

    # A banner is not evidence. Ask the API whether the pack and the question
    # really exist now — this is what separates "the UI said Dokončeno" from
    # "the pack was written", and it is exactly the failure mode this project
    # already hit once (an operation that claimed success while nothing moved).
    persisted = False
    detail = ""
    try:
        with urllib.request.urlopen(
                f"{BASE}/qa-api/v1/schools/{SCHOOL}/packs/{PACK}/entries", timeout=30) as r:
            body = json.loads(r.read().decode())
        questions = [e.get("canonical_q") for e in body.get("entries", [])]
        persisted = "Kdy je ředitelské volno?" in questions
        detail = f"{len(questions)} entries, etag {str(body.get('pack_etag'))[:14]}…"
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the report
        detail = f"read-back failed: {exc}"
    check("5e the published pack really exists in the API afterwards", persisted, detail)

    # ── 7. import dialog asks for metadata of an unknown pack (§2.4c) ─
    imp_btn = page.get_by_role("button", name="Importovat", exact=True)
    imp_btn.first.wait_for(state="visible", timeout=15000)
    imp_btn.first.click()
    page.wait_for_timeout(1000)
    imp = page.get_by_role("dialog")
    check("7 import dialog opens", imp.count() == 1)
    if imp.count():
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

    # ── 8. export dialog reachable ────────────────────────────────────
    page.get_by_role("button", name="Exportovat", exact=True).first.click()
    page.wait_for_timeout(1000)
    check("8 export dialog opens", page.get_by_role("dialog").count() == 1)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # ── 9. narrow / mobile layout has no horizontal overflow ──────────
    for width, height in ((390, 844), (768, 1024)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(600)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check(f"9 no horizontal overflow at {width}px", overflow <= 1, f"overflow={overflow}px")

    # ── 10. keyboard focus is visible on the toolbar ──────────────────
    page.set_viewport_size({"width": 1440, "height": 950})
    page.keyboard.press("Tab")
    focused = page.evaluate("() => document.activeElement && document.activeElement.tagName")
    check("10 keyboard focus reaches a control", focused not in (None, "BODY"), str(focused))

    # ── 12. the "new pack" dialog must be usable on a small screen ────
    # Regression: the grades and the confirm buttons used to be clipped out of
    # reach, so nothing below the fold could be clicked at all. The user
    # reported this at both a phone width and a low desktop window, so both
    # are exercised here with the same real-interaction checks.
    dialog_scroll_checks(page, 390, 640, "abcefghi")
    dialog_scroll_checks(page, 1280, 620, "abcefghi")
    page.set_viewport_size({"width": 1440, "height": 950})

    # ── 13. areas render as Czech names, not as stable codes ──────────
    page.goto(f"{BASE}/qa/", wait_until="networkidle")
    select_school(page, SCHOOL)
    page.wait_for_timeout(1500)
    domains = json.loads(urllib.request.urlopen(
        f"{BASE}/qa-api/v1/schools/{SCHOOL}/domains", timeout=20).read().decode())["domains"]
    by_code = {d["code"]: d["label"] for d in domains}
    list_text = page.locator(".entry-list").inner_text()
    shown_codes = [c for c in by_code if c in list_text and by_code[c] != c]
    check("13a the entry list shows no raw area codes", not shown_codes,
          ", ".join(shown_codes[:3]))
    shown_labels = [lbl for lbl in by_code.values() if lbl in list_text]
    check("13b the entry list shows catalog labels", bool(shown_labels),
          "; ".join(shown_labels[:2]))

    # ── 14. no missing-translation hint when the translation exists ───
    page.locator(".entry-list li, .entry-list button").first.click()
    page.wait_for_timeout(1200)
    page.evaluate("() => document.querySelectorAll('details').forEach(d => d.open = true)")
    page.wait_for_timeout(400)
    editor_text = page.locator(".entry-editor-column").inner_text()
    check("14 no missing-translation hint while cs labels exist",
          "chybí popisek" not in editor_text,
          editor_text[:80])

    check("11 no console errors during the whole run", not console_errors,
          "; ".join(console_errors[:2]))

    browser.close()

print("\n--- SUMMARY ---")
print(f"passed: {len(checks)}   failed: {len(failures)}   pack: {PACK}")
for f in failures:
    print("FAILED:", f)
sys.exit(1 if failures else 0)
