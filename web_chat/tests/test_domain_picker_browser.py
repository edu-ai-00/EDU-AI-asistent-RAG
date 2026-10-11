#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-06).
"""Real-browser check of the area picker. Playwright, Chromium, two viewports.

WHY IT EXISTS. `npm run build` type-checks and bundles, and
`tests/test_web_chat.py` covers the BFF — neither of them opens a page. What
only a browser shows: that the control renders at all, that it is reachable by
keyboard, that the summary states the current scope IN WORDS, that changing the
area clears the transcript, that the popover stays inside a 390 px viewport, and
above all **what the page actually PUTs on the wire** — the assertions here read
the intercepted request body rather than trusting the component.

Everything it asserts was a decision in spec.search-domain-scope §7, and two of
them are properties no unit test can express: "changing the dropdown sends no
question", and "an unreadable catalog must not silently read as all areas".

## Run

Needs the dev stack with the fixture loaded (tests/domain_scope_fixture.py) and
a web_chat whose CHAT_LEGAL_ENTITY is a school that EXISTS in that database —
the deployment default points at production, which a dev database does not have.

```bash
# 1) the fixture
docker cp tests/domain_scope_fixture.py edu_ai_search:/tmp/
docker exec -e PYTHONPATH=/app -w /app edu_ai_search         python /tmp/domain_scope_fixture.py build

# 2) a web_chat pointed at the fixture's school
docker run -d --name edu_ai_web_chat_uitest --network edu_ai_app   -e SEARCH_BASE_URL=http://edu_ai_search:8090   -e CHAT_LEGAL_ENTITY=SKOLA_NOVA_VES_ZS -e CHAT_ENTITY_PICKER=true   edu_ai_web_chat:0.1

# 3) the browser. `edu_ai_app` has NO internet by design, so install first on
#    the default bridge and attach afterwards.
docker run -d --name ui_runner -v "$PWD/web_chat/tests:/t" -w /t   mcr.microsoft.com/playwright/python:v1.49.0-noble sleep 3600
docker exec ui_runner pip install --quiet playwright==1.49.0
docker network connect edu_ai_app ui_runner
docker exec -w /t ui_runner python test_domain_picker_browser.py         http://edu_ai_web_chat_uitest:8081

# 4) tidy up
docker rm -f ui_runner edu_ai_web_chat_uitest
docker exec -e PYTHONPATH=/app -w /app edu_ai_search         python /tmp/domain_scope_fixture.py drop
```

On Windows put `MSYS_NO_PATHCONV=1` in front of each `docker` command.

Not wired into a runner: it needs three containers and a browser image, so it
is a deliberate pre-release check rather than something to run per commit.
Result 2026-09-06: 54 checks, 0 failed, at 1280 px and 390 px.
Extended the same day with the retired-stored-code block (finding 3): a code
the school has withdrawn must BLOCK sending until the user chooses again,
because the fallback selection is `null` — "all areas" — and letting that go
out is the silent widening the spec forbids.
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://edu_ai_web_chat_uitest:8081"
Q_ATT = "Jak omluvit vyšinutou absenci hlodavcem?"

failures = []
ran = 0


def check(name, cond, detail=""):
    global ran
    ran += 1
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" - {detail}" if detail else ""))
        failures.append(name)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width, label in ((1280, "desktop"), (390, "mobile 390px")):
            page = browser.new_page(viewport={"width": width, "height": 800})
            sent = []
            page.on("request", lambda r: sent.append(r) if "/chat/api/message" in r.url else None)
            console_errors = []
            page.on("console", lambda m: console_errors.append(m.text)
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))

            print(f"\n[{label}]")
            page.goto(f"{BASE}/chat/", wait_until="networkidle")

            # A ROLE FIRST. `search_requires_role` is on, so without one every
            # question short-circuits with outcome=role_required — which the area
            # picker must NOT bypass, and the first run of this script proved it
            # does not (all four content assertions came back with "Neznáme tvoji
            # roli"). Selecting one is how we reach the area assertions.
            page.locator("select").nth(1).select_option("rodic")
            check("a role can be selected",
                  page.locator("select").nth(1).input_value() == "rodic",
                  page.locator("select").nth(1).input_value())

            # ── the control exists and states the scope IN WORDS ──
            trigger = page.locator(".domain-picker button[aria-haspopup]")
            check("the area picker renders", trigger.count() == 1,
                  f"count={trigger.count()}")
            check("its label says the current scope in words, not a count of nothing",
                  "Všechny oblasti" in trigger.inner_text(),
                  repr(trigger.inner_text()))
            check("no loading state is left behind after the catalog arrives",
                  "Načítám" not in trigger.inner_text(), repr(trigger.inner_text()))

            # ── keyboard: it is a real button, focusable and Enter-activated ──
            trigger.focus()
            check("the trigger is focusable", trigger.evaluate(
                "el => el === document.activeElement"))
            page.keyboard.press("Enter")
            menu = page.locator(".domain-menu")
            check("Enter opens the menu", menu.count() == 1 and menu.is_visible())
            check("aria-expanded follows the menu",
                  trigger.get_attribute("aria-expanded") == "true",
                  trigger.get_attribute("aria-expanded"))
            check("the menu is inside the viewport at this width",
                  menu.bounding_box()["x"] >= 0
                  and menu.bounding_box()["x"] + menu.bounding_box()["width"] <= width + 1,
                  f"box={menu.bounding_box()} width={width}")

            # ── "all areas" is a real, present option ──
            opts = page.locator(".domain-option")
            labels = [opts.nth(i).inner_text().strip() for i in range(opts.count())]
            check("'Všechny oblasti' is an option in the list, not the absence of one",
                  "Všechny oblasti" in labels, f"{labels[:4]}")
            check("the school's own areas are listed with translated names",
                  any("Docházka" in x for x in labels), f"{labels[:6]}")
            check("the area labels come from the catalog (a school-local one is present)",
                  any("mikroskop" in x.lower() for x in labels), f"{labels}")

            # ── Escape closes it (a panel must not trap the keyboard) ──
            page.keyboard.press("Escape")
            check("Escape closes the menu", page.locator(".domain-menu").count() == 0)

            # ── choosing an area changes the summary, sends NOTHING ──
            trigger.click()
            page.locator(".domain-option", has_text="Docházka").first.locator(
                "input").check()
            page.keyboard.press("Escape")
            check("choosing an area updates the summary",
                  "Docházka" in trigger.inner_text(), repr(trigger.inner_text()))
            check("changing the dropdown sends NO question",
                  len(sent) == 0, f"{len(sent)} request(s)")

            # ── the question carries the codes ──
            page.locator(".composer-input").fill(Q_ATT)
            page.locator(".composer-input").press("Enter")
            page.wait_for_selector(".message--assistant .markdown", timeout=60_000)
            check("exactly one request was sent", len(sent) == 1, f"{len(sent)}")
            body = json.loads(sent[0].post_data)
            check("the sent body carries `domains`",
                  body.get("domains") == ["ATTENDANCE"], f"{body}")
            answer = page.locator(".message--assistant .markdown").last.inner_text()
            check("the in-area answer arrives in the page",
                  "(ds-att-1)" in answer, repr(answer[:90]))

            # ── a new area starts a NEW conversation: transcript cleared ──
            before = page.locator(".message-row").count()
            check("the transcript has messages before the scope change", before >= 2,
                  f"{before}")
            trigger.click()
            page.locator(".domain-option", has_text="Školní stravování").first.locator(
                "input").check()
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            check("changing the area clears the transcript (new conversation)",
                  page.locator(".message-row").count() == 0,
                  f"{page.locator('.message-row').count()} row(s) left")

            # ── an out-of-area question answers no_answer, never widens ──
            sent.clear()
            page.locator(".composer-input").fill(
                "Kde se odevzdává zapomenutá pláštěnka?")
            page.locator(".composer-input").press("Enter")
            page.wait_for_selector(".message--assistant .markdown", timeout=60_000)
            body2 = json.loads(sent[0].post_data)
            check("the second question carries BOTH chosen areas",
                  sorted(body2.get("domains") or []) == ["ATTENDANCE", "CATERING"],
                  f"{body2.get('domains')}")
            txt = page.locator(".message--assistant .markdown").last.inner_text()
            check("an untagged source is NOT answered under an explicit filter",
                  "(ds-untag-1)" not in txt, repr(txt[:90]))
            check("…and the page shows the no-answer heading rather than nothing",
                  page.locator(".response-details strong").count() >= 1
                  or "nemám odpověď" in txt, repr(txt[:90]))

            # ── back to "all areas" is one explicit click ──
            trigger.click()
            page.locator(".domain-option", has_text="Všechny oblasti").locator(
                "input").check()
            page.keyboard.press("Escape")
            check("'Všechny oblasti' is reachable again in one click",
                  "Všechny oblasti" in trigger.inner_text(),
                  repr(trigger.inner_text()))

            sent.clear()
            page.locator(".composer-input").fill(
                "Kde se odevzdává zapomenutá pláštěnka?")
            page.locator(".composer-input").press("Enter")
            page.wait_for_selector(".message--assistant .markdown", timeout=60_000)
            body3 = json.loads(sent[0].post_data)
            check("unrestricted OMITS the field (not domains:null)",
                  "domains" not in body3, f"{body3}")
            txt = page.locator(".message--assistant .markdown").last.inner_text()
            check("…and the untagged source now answers",
                  "(ds-untag-1)" in txt, repr(txt[:90]))

            check("no uncaught JS errors on the page",
                  not console_errors, f"{console_errors[:3]}")
            page.close()

        # ── a RETIRED stored code must block sending until a new choice ──
        # THE CASE: the browser remembers a code, the school retires it, and the
        # surviving selection is empty — so `selected` falls back to null, which
        # means ALL AREAS. Reporting that in a notice was not enough; the next
        # question went out unrestricted while the user believed a filter was
        # on. Two shapes, because they behave differently and both matter.
        for label, stored, expect_all in (
            ("every stored code retired", ["GONE_A", "GONE_B"], True),
            ("one of two retired", ["ATTENDANCE", "GONE_B"], False),
        ):
            print(f"\n[retired stored code — {label}]")
            page = browser.new_page()
            sent = []
            page.on("request",
                    lambda r: sent.append(r) if "/chat/api/message" in r.url else None)
            # seed localStorage for the entity the BFF will report, before the
            # app runs — that is the only way to arrive with a stale choice
            page.goto(f"{BASE}/chat/", wait_until="networkidle")
            entity = page.evaluate(
                "async () => (await (await fetch('/chat/api/domains')).json()).legal_entity")
            page.evaluate(
                "([e, v]) => localStorage.setItem('edu_ai_chat_domains:' + e,"
                " JSON.stringify(v))", [entity, stored])
            page.reload(wait_until="networkidle")
            page.locator("select").nth(1).select_option("rodic")

            notice = page.locator(".domain-dropped")
            check(f"[{label}] the retired code is REPORTED",
                  notice.count() == 1 and "GONE_B" in notice.inner_text(),
                  repr(notice.inner_text() if notice.count() else None))
            check(f"[{label}] the notice interrupts (role=alert)",
                  notice.get_attribute("role") == "alert",
                  notice.get_attribute("role"))
            if expect_all:
                check(f"[{label}] it says what would happen without a new choice",
                      "všech oblastech" in notice.inner_text().lower(),
                      repr(notice.inner_text()))

            # THE ASSERTION THIS WHOLE BLOCK EXISTS FOR
            page.locator(".composer-input").fill("Kdy zacina skolni rok?")
            page.locator(".composer-input").press("Enter")
            page.wait_for_timeout(1200)
            check(f"[{label}] NOTHING is sent while a new choice is required",
                  len(sent) == 0, f"{len(sent)} request(s) went out")
            err = page.locator(".message-error")
            check(f"[{label}] …and the user is told why, specifically",
                  err.count() >= 1 and "oblast" in err.first.inner_text().lower(),
                  repr(err.first.inner_text() if err.count() else None))

            # …and confirming it unblocks, with the scope the user can see
            notice.locator(".link-button").click()
            check(f"[{label}] the notice clears on confirmation",
                  page.locator(".domain-dropped").count() == 0)
            page.locator(".composer-input").fill("Kdy zacina skolni rok?")
            page.locator(".composer-input").press("Enter")
            page.wait_for_selector(".message--assistant .markdown", timeout=60_000)
            check(f"[{label}] after confirming, the question IS sent",
                  len(sent) == 1, f"{len(sent)}")
            body = json.loads(sent[0].post_data)
            if expect_all:
                check(f"[{label}] confirmed 'all areas' omits the field",
                      "domains" not in body, f"{body}")
            else:
                check(f"[{label}] the SURVIVING code is what gets sent",
                      body.get("domains") == ["ATTENDANCE"], f"{body}")
            page.close()

        # ── the ERROR state: a catalog that cannot be read ──
        print("\n[catalog error state]")
        page = browser.new_page()
        page.route("**/chat/api/domains*",
                   lambda route: route.fulfill(status=503, content_type="application/json",
                                               body='{"error":{"code":"domains_unavailable",'
                                                    '"message":"x","retryable":true}}'))
        page.goto(f"{BASE}/chat/", wait_until="networkidle")
        # the error branch renders its own button (a retry, not a popover
        # trigger), so it carries no aria-haspopup — addressed by its class
        trigger = page.locator(".domain-picker button.domain-error")
        check("an unreadable catalog shows an error with a retry, not an empty list",
              trigger.count() == 1 and "znovu" in trigger.inner_text().lower(),
              repr(trigger.inner_text() if trigger.count() else None))
        check("…and the picker does not silently read as 'all areas'",
              "Všechny oblasti" not in trigger.inner_text(),
              repr(trigger.inner_text()))
        page.close()
        browser.close()

    print(f"\n{ran} checks, {len(failures)} failed")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
