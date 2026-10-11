#!/usr/bin/env python3
"""Real-browser check of choosing the school in the chat. Playwright + Chromium.

Self-contained: starts fake_search_schools.py and four web_chat processes (picker
on without a default, picker on with an explicit default, fixed school, nothing
configured), then drives each through the page. What it reads is what the page
PUTS ON THE WIRE (intercepted requests) and what the fake search recorded —
never component state.

Run (needs the built frontend at <web_chat>/static and fastapi/uvicorn/httpx):

    python tests/test_school_selection_browser.py [path-to-web_chat-dir]

Offline: no database, no LLM, no real search. Not a VM acceptance.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
WEB = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE.parent
FAKE = "http://127.0.0.1:18090"
PLACEHOLDER = "CHANGE_ME_legal_entity_code"      # what .env.example hands to the container

BFFS = {   # name: (port, CHAT_ENTITY_PICKER, CHAT_LEGAL_ENTITY)
    "picker": (18101, "true", PLACEHOLDER),
    "default": (18102, "true", "SKOLA_B"),
    "fixed": (18103, "false", "SKOLA_A"),
    "unset": (18104, "false", ""),
}

failures: list[str] = []
ran = 0


def check(name, cond, detail=""):
    global ran
    ran += 1
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" - {detail}"))
    if not cond:
        failures.append(name)


def http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"null")


def wait_up(url):
    for _ in range(80):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"not up: {url}")


def reset_fake(**state):
    http("POST", f"{FAKE}/__ctl/reset")
    if state:
        http("POST", f"{FAKE}/__ctl/set", state)


def log(path):
    return [e for e in http("GET", f"{FAKE}/__ctl/log") if e["path"] == path]


class Page:
    """One fresh browser context (own localStorage) with request capture."""

    def __init__(self, browser, bff, init_storage=None):
        self.base = f"http://127.0.0.1:{BFFS[bff][0]}"
        self.ctx = browser.new_context()
        if init_storage is not None:
            # seeded before the app runs — the only way to arrive with a stale choice
            self.ctx.add_init_script(
                "if (!sessionStorage.getItem('seeded')) {"
                f" localStorage.setItem('edu_ai_chat_school', {json.dumps(init_storage)});"
                " sessionStorage.setItem('seeded','1'); }")
        self.page = self.ctx.new_page()
        self.requests: list = []
        self.page.on("request", lambda r: self.requests.append(r)
                     if "/chat/api/" in r.url else None)
        self.errors: list = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def open(self):
        self.page.goto(f"{self.base}/chat/", wait_until="networkidle")
        return self

    def reqs(self, part):
        return [r for r in self.requests if part in r.url]

    @property
    def select(self):
        return self.page.locator('select[aria-label="Škola"]')

    @property
    def input(self):
        return self.page.locator(".composer-input")

    @property
    def send(self):
        return self.page.locator(".send-button")

    def stored(self):
        return self.page.evaluate("localStorage.getItem('edu_ai_chat_school')")

    def ask(self, text):
        self.input.fill(text)
        self.input.press("Enter")

    def close(self):
        self.ctx.close()


def main(browser):
    # ── 1. no default: a visible menu, nothing chosen, nothing sent ──────────
    print("\n[picker on, no default]")
    reset_fake()
    p = Page(browser, "picker").open()
    names = [p.select.locator("option").nth(i).inner_text()
             for i in range(p.select.locator("option").count())]
    check("the school menu is visible", p.select.is_visible())
    check("it offers 'Vyberte školu' and the school NAMES (not codes)",
          names == ["Vyberte školu", "Základní škola Alfa", "Základní škola Beta"], names)
    check("no school is preselected", p.select.input_value() == "", p.select.input_value())
    check("the composer input is disabled", p.input.is_disabled())
    check("the send button is disabled", p.send.is_disabled())
    check("a waiting message says what to do",
          "vyberte školu" in p.page.locator(".welcome").inner_text().lower())
    p.page.wait_for_timeout(700)
    check("NO area-catalog request before a choice", p.reqs("/chat/api/domains") == [],
          [r.url for r in p.reqs("/chat/api/domains")])
    check("NO message request before a choice", p.reqs("/chat/api/message") == [])
    check("search was never asked for areas or answers",
          log("/v1/domains") == [] and log("/v1/ask") == [])
    check("the placeholder default is not used as a school", p.stored() is None)

    # ── 2. manual choice ─────────────────────────────────────────────────────
    print("\n[manual choice]")
    p.select.select_option("SKOLA_A")
    p.page.wait_for_selector(".domain-picker button[aria-haspopup]")
    p.input.fill("x")        # Send is disabled for an empty box whatever the school
    check("the composer is enabled after a valid choice",
          p.input.is_enabled() and not p.send.is_disabled())
    p.input.fill("")
    doms = p.reqs("/chat/api/domains")
    check("the areas are requested for the chosen school",
          len(doms) == 1 and "legal_entity=SKOLA_A" in doms[0].url, [r.url for r in doms])
    p.ask("Kdy začíná školní rok?")
    p.page.wait_for_selector(".message--assistant .markdown")
    body = json.loads(p.reqs("/chat/api/message")[0].post_data)
    check("the question carries legal_entity of the chosen school",
          body.get("legal_entity") == "SKOLA_A", body)
    check("search answered in that school",
          "SKOLA_A" in p.page.locator(".message--assistant .markdown").last.inner_text())
    check("the choice is remembered in this browser", p.stored() == "SKOLA_A", p.stored())
    first_conversation = body["conversation_id"]

    # ── 3. reload: remembered, but verified against a FRESH list ─────────────
    print("\n[reload]")
    before = len(log("/v1/entities"))
    p.requests.clear()
    p.page.reload(wait_until="networkidle")
    check("the remembered school is selected after reload",
          p.select.input_value() == "SKOLA_A", p.select.input_value())
    check("…and sending is possible", p.input.is_enabled())
    check("the list was fetched again (the choice was verified, not trusted)",
          len(log("/v1/entities")) > before)
    check("a remembered school is not sent anywhere before the user writes",
          p.reqs("/chat/api/message") == [])

    # ── 4. change of school: new scope, no leakage, late answer dropped ──────
    print("\n[change of school]")
    p.ask("pomalu prosím")
    p.page.wait_for_selector(".loading")
    p.select.select_option("SKOLA_B")
    check("switching school clears the transcript", p.page.locator(".message-row").count() == 0,
          p.page.locator(".message-row").count())
    p.page.wait_for_timeout(2600)       # the slow answer for SKOLA_A arrives meanwhile
    check("the late answer of the old school never shows up",
          "SKOLA_A" not in p.page.locator(".thread").inner_text()
          and p.page.locator(".message-row").count() == 0,
          p.page.locator(".thread").inner_text()[:120])
    doms = p.reqs("/chat/api/domains")
    check("the areas are reloaded for the new school", "legal_entity=SKOLA_B" in doms[-1].url,
          doms[-1].url)
    check("the old school's areas are not carried over",
          p.page.locator(".domain-picker button[aria-haspopup]").inner_text().startswith("Všechny"))
    n = len(p.reqs("/chat/api/message"))
    p.ask("Co je nového?")
    p.page.wait_for_selector(".message--assistant .markdown")
    body2 = json.loads(p.reqs("/chat/api/message")[n].post_data)
    check("the next question is scoped to the NEW school", body2["legal_entity"] == "SKOLA_B", body2)
    check("…in a NEW conversation", body2["conversation_id"] != first_conversation)
    p.close()

    # A send attempted before the new school's catalog arrives must stay local,
    # even when the previous school had an explicit area selection.
    print("\n[school changed while the new area catalog is loading]")
    reset_fake()
    p = Page(browser, "picker", init_storage="SKOLA_A").open()
    p.page.locator(".domain-picker button[aria-haspopup]").click()
    p.page.get_by_role("checkbox", name="Docházka").check()
    http("POST", f"{FAKE}/__ctl/set", {"domains_delay": 2.0})
    p.select.select_option("SKOLA_B")
    p.ask("Čekám na oblasti")
    p.page.wait_for_selector(".message-error")
    check("sending while the new catalog is pending is explained locally",
          "načítám oblasti" in p.page.locator(".message-error").first.inner_text())
    check("no old-area question reaches the BFF or search while the catalog loads",
          p.reqs("/chat/api/message") == [] and log("/v1/ask") == [])
    p.page.wait_for_selector(".domain-picker button[aria-haspopup]")
    p.ask("Ahoj")
    p.page.wait_for_selector(".message--assistant .markdown")
    settled_body = p.reqs("/chat/api/message")[-1].post_data_json
    check("the settled new school can search without inheriting the previous areas",
          settled_body["legal_entity"] == "SKOLA_B" and "domains" not in settled_body,
          settled_body)
    check("catalog gating produces no browser errors", not p.errors, p.errors)
    p.close()

    # ── 5. stored school removed / inactive ──────────────────────────────────
    for label, stored in (("deactivated", "SKOLA_A"), ("unknown", "SKOLA_ZMIZELA")):
        print(f"\n[stored school {label}]")
        reset_fake(entities=[("SKOLA_B", "Základní škola Beta")])
        p = Page(browser, "picker", init_storage=stored).open()
        check(f"[{label}] no school is selected", p.select.input_value() == "")
        check(f"[{label}] the stored value is forgotten", p.stored() is None, p.stored())
        check(f"[{label}] the user is told", "už není dostupná" in p.page.locator(".welcome").inner_text())
        check(f"[{label}] sending is blocked", p.input.is_disabled() and p.send.is_disabled())
        p.page.wait_for_timeout(500)
        check(f"[{label}] no catalog/message request", p.reqs("/chat/api/domains") == []
              and p.reqs("/chat/api/message") == [])
        p.select.select_option("SKOLA_B")
        check(f"[{label}] a new choice unblocks", p.input.is_enabled())
        p.close()

    # ── 6. registry unavailable / empty ──────────────────────────────────────
    print("\n[registry unavailable]")
    reset_fake(entities_fail=True)
    p = Page(browser, "picker").open()
    retry = p.page.locator("button.domain-error", has_text="Zkusit znovu")
    check("the failure has its own state with a retry", retry.count() >= 1 and retry.first.is_visible())
    check("there is no school menu to pick from", p.select.count() == 0)
    check("sending is blocked", p.input.is_disabled())
    check("the error is not presented as 'choose a school'",
          "nepodařilo načíst" in p.page.locator(".welcome").inner_text())
    http("POST", f"{FAKE}/__ctl/set", {"entities_fail": False})
    retry.first.click()
    p.page.wait_for_selector('select[aria-label="Škola"]')
    check("retry recovers into the normal waiting state",
          p.select.input_value() == "" and p.input.is_disabled())
    p.close()

    print("\n[registry empty]")
    reset_fake(entities=[])
    p = Page(browser, "picker").open()
    check("an empty list is an error state, not a finished setup",
          "žádné aktivní školy" in p.page.locator(".welcome").inner_text()
          and p.select.count() == 0 and p.input.is_disabled())
    check("…and nothing is sent", p.reqs("/chat/api/message") == [] and p.reqs("/chat/api/domains") == [])
    p.close()

    # ── 7. the server refuses a school that went away mid-session ────────────
    print("\n[school deactivated mid-session]")
    reset_fake()
    p = Page(browser, "picker").open()
    p.select.select_option("SKOLA_A")
    p.page.wait_for_selector(".domain-picker button[aria-haspopup]")
    http("POST", f"{FAKE}/__ctl/set", {"entities": [("SKOLA_B", "Základní škola Beta")]})
    p.ask("Ahoj")
    p.page.wait_for_selector(".message-error")
    check("the user is told the school is not available",
          "není dostupná" in p.page.locator(".message-error").first.inner_text())
    check("the search was not asked in any school", log("/v1/ask") == [])
    p.page.wait_for_function(
        "document.querySelector('select[aria-label=\"Škola\"]')?.value === ''")
    check("the menu falls back to 'Vyberte školu' and the memory is cleared",
          p.select.input_value() == "" and p.stored() is None, p.stored())
    p.close()

    # ── 8. explicit deployment default keeps its meaning ─────────────────────
    print("\n[explicit default]")
    reset_fake()
    p = Page(browser, "default").open()
    check("a valid explicit default is preselected", p.select.input_value() == "SKOLA_B")
    check("…and the composer works", p.input.is_enabled())
    p.close()
    p = Page(browser, "default", init_storage="SKOLA_A").open()
    check("a remembered valid choice wins over the default", p.select.input_value() == "SKOLA_A")
    p.ask("Ahoj")
    p.page.wait_for_selector(".message--assistant .markdown")
    old_conversation = p.reqs("/chat/api/message")[-1].post_data_json["conversation_id"]
    check("the remembered school's answer is present before replacement",
          "SKOLA_A" in p.page.locator(".message--assistant .markdown").last.inner_text())
    http("POST", f"{FAKE}/__ctl/set", {"entities": [("SKOLA_B", "Základní škola Beta")]})
    p.ask("Nová otázka")
    p.page.wait_for_function(
        "document.querySelector('select[aria-label=\"Škola\"]')?.value === 'SKOLA_B'")
    p.page.wait_for_function("document.querySelectorAll('.message-row').length === 0")
    p.page.wait_for_function("!document.querySelector('.composer-input').disabled")
    check("reload replaces the unavailable school with the valid explicit default",
          p.select.input_value() == "SKOLA_B" and p.stored() is None)
    check("automatic replacement clears the old school's transcript",
          p.page.locator(".message-row").count() == 0)
    check("the rejected question and config reload perform no automatic search",
          len(log("/v1/ask")) == 1 and log("/v1/ask")[0]["legal_entity"] == "SKOLA_A")
    p.ask("Ahoj")
    p.page.wait_for_selector(".message--assistant .markdown")
    next_body = p.reqs("/chat/api/message")[-1].post_data_json
    check("the default school's next question starts a different conversation",
          next_body["legal_entity"] == "SKOLA_B"
          and next_body["conversation_id"] != old_conversation, next_body)
    check("only the new school's answer is shown after replacement",
          "SKOLA_B" in p.page.locator(".message--assistant .markdown").last.inner_text()
          and "SKOLA_A" not in p.page.locator(".thread-viewport").inner_text())
    check("automatic school replacement produces no browser errors", not p.errors, p.errors)
    p.close()

    # ── 9. fixed builds: unchanged boundary ──────────────────────────────────
    print("\n[picker off]")
    reset_fake()
    p = Page(browser, "fixed").open()
    check("no school menu and the composer works", p.select.count() == 0 and p.input.is_enabled())
    p.ask("Ahoj")
    p.page.wait_for_selector(".message--assistant .markdown")
    check("the fixed school answered", "SKOLA_A" in p.page.locator(".message--assistant .markdown").last.inner_text())
    spoof = p.page.evaluate(
        "async () => { const r = await fetch('/chat/api/message', {method:'POST',"
        " headers:{'content-type':'application/json'}, body: JSON.stringify("
        "{message:'x', legal_entity:'SKOLA_B'})}); return (await r.json()).effective_scope; }")
    check("a spoofed school is ignored", spoof["legal_entity"] == "SKOLA_A", spoof)
    check("the list of other schools was never requested", log("/v1/entities") == [])
    p.close()

    print("\n[nothing configured, picker off]")
    reset_fake()
    p = Page(browser, "unset").open()
    check("it is reported as not configured, not ready",
          "není nastavený" in p.page.locator(".welcome").inner_text() and p.input.is_disabled())
    check("nothing is requested", p.reqs("/chat/api/message") == [] and log("/v1/ask") == [])
    p.close()


if __name__ == "__main__":
    procs = []
    try:
        fake_env = {**os.environ, "PYTHONPATH": str(HERE)}
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "fake_search_schools:app", "--port", "18090",
             "--log-level", "warning"], cwd=HERE, env=fake_env))
        for name, (port, picker, entity) in BFFS.items():
            env = {**os.environ, "SEARCH_BASE_URL": FAKE, "CHAT_ENTITY_PICKER": picker,
                   "CHAT_LEGAL_ENTITY": entity}
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "web_chat:app", "--port", str(port),
                 "--log-level", "warning"], cwd=WEB, env=env))
        wait_up(f"{FAKE}/openapi.json")
        for port, *_ in BFFS.values():
            wait_up(f"http://127.0.0.1:{port}/chat/health")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            main(browser)
            browser.close()
    finally:
        for proc in procs:
            proc.terminate()
    print(f"\n{ran} checks, {len(failures)} failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1 if failures else 0)
