import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AskPreviewDrawer } from "../components/AskPreviewDrawer";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import type { AskPreviewBody, AskPreviewResult, DocumentDetail, School } from "../api/types";
import { sourceKindOf, verdictOf, cascadeRows, qaLexicalDetail } from "../lib/askDebug";
import { strings } from "../strings.cs";

const t = strings.ask;
const d = strings.askDebug;

const SCHOOL: School = {
  code: "ZS_ONE",
  name: "ZŠ Jedna",
  permissions: { read: true, publish: true, ingest: true, deactivate: true, delete_permanently: true },
};

const OPTIONS = {
  audience_roles: [
    { code: "TEACHER", label: "Učitel" },
    { code: "PARENT", label: "Rodič" },
    { code: "STUDENT", label: "Žák" },
  ],
  school_grades: [{ code: "5", label: "5. ročník" }],
};

const DEBUG_RESULT: AskPreviewResult = {
  answer: "Našel jsem podobnou otázku.",
  outcome: "suggest",
  answer_source: "qa_lexical",
  delivered_by: "online",
  sources: [
    { rank: 1, title: "Školní řád", url: "https://zs.example/rad", file_uuid: "doc-1" },
    { rank: 2, title: "Provoz.pdf", original_file_name: "Provoz.pdf", file_uuid: "doc-2" },
  ],
  suggestions: [{ kind: "qa", question: "Kolik stojí družina?", entry_id: "entry-1", score: 0.52 }],
  matched_entry_id: "entry-1",
  match_score: 0.48,
  duration_ms: 64,
  retrieval_only: false,
  language: "cs",
  effective_scope: { legal_entity: "ZS_ONE", role: "PARENT", school_grade: "5", language: "cs", domains: null, identified: true },
  strategies: ["qa_lexical"],
  key_words_used: ["omluvenka"],
  answer_parts: [
    { index: 0, lead_in: "Řád", text: "Omluvenka do tří dnů.", source_rank: 1, title: "Školní řád", file_uuid: "doc-1" },
    { index: 1, lead_in: null, text: "Družina do 17:00.", source_rank: null, title: null, file_uuid: null },
  ],
  trace: [
    { step: "resolve_context", status: "skipped", note: "no prior turns" },
    { step: "route", status: "ran", ms: 4 },
    {
      step: "qa_lexical",
      status: "ran",
      ms: 11,
      decision: "B-deferred",
      match_score: 0.48,
      detail: {
        t_answer: 0.6,
        t_suggest: 0.3,
        concepts: [
          { c: "zacina|zacinat", w: 0.41, hit: true },
          { c: "rok", w: 0.47, hit: false },
        ],
        ngram_chip: { entry_id: "entry-1", score: 0.52, min_sim: 0.4 },
      },
    },
    { step: "mystery_step", status: "ran", ms: 2, decision: "Z-9" },
    { step: "document_lexical", status: "ran", ms: 20, pool_added: 3 },
    { step: "rag_keywords", status: "error", note: "timeout" },
    { step: "rag_vector", status: "disabled", note: "enabled=false" },
    { step: "answer", status: "ran", ms: 3, resolved: true, outcome: "suggest", stopped_here: true },
    { step: "document_siblings", status: "skipped", note: "after the stop" },
  ],
  explain: {
    query_lemma_tokens: ["kolik", "stat", "druzina"],
    query_absent_concepts: ["xyz"],
    query_in_corpus: false,
  },
};

const PLAIN_RESULT: AskPreviewResult = {
  answer: "Prostá odpověď.",
  outcome: "answer",
  answer_source: "qa_exact",
  delivered_by: "online",
  sources: [],
  suggestions: [],
  matched_entry_id: null,
  match_score: null,
  duration_ms: 5,
  retrieval_only: false,
};

function makeClient(over: Partial<ApiClient> = {}) {
  const askPreview = vi.fn(async (_school: string, body: AskPreviewBody) => (body.debug === false ? PLAIN_RESULT : DEBUG_RESULT));
  const getEntryById = vi.fn(async () => ({ pack_id: "faq", entry_key: "k1", canonical_q: "Kolik stojí družina?", is_active: true }));
  const getDocument = vi.fn(
    async (_school: string, fileUuid: string): Promise<DocumentDetail> => ({
      document: {
        file_uuid: fileUuid,
        title: "Školní řád",
        source_kind: "web",
        source_url: null,
        original_file_name: null,
        document_type: null,
        document_language: "cs",
        school_subject: null,
        school_grade_codes: [],
        domain_codes: [],
        audience_roles: [],
        visibility: "entity",
        valid_from: null,
        valid_to: "2020-01-01",
        user_name: null,
        ingested_at: "2026-01-01T10:00:00Z",
        is_active: fileUuid === "doc-1",
        validity_state: "expired",
        version_count: 1,
        chunk_count: 3,
        file_format: "pdf",
      },
      versions: [],
    }),
  );
  const client = {
    askPreview,
    getEntryById,
    getDocument,
    getDocumentOptions: vi.fn(async () => OPTIONS),
    ...over,
  } as unknown as ApiClient;
  return { client, askPreview, getEntryById, getDocument };
}

type Hooks = {
  onOpenEntry?: (pack: string, key: string) => void;
  onOpenDocument?: (uuid: string) => void;
  onCreateQa?: (question: string) => "ok" | "no-pack";
};

async function ask(client: ApiClient, hooks: Hooks = {}, question = "Kolik stojí družina?") {
  render(<AskPreviewDrawer client={client} school={SCHOOL} open onClose={() => {}} {...hooks} />);
  await screen.findByRole("option", { name: "Rodič" });
  fireEvent.change(screen.getByLabelText(t.question), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: t.submit }));
  return screen.findByRole("region", { name: d.heading });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("debug toggle", () => {
  it("is on by default and sends debug: true; off sends debug: false and shows no panel", async () => {
    const { client, askPreview } = makeClient();
    await ask(client);
    expect((screen.getByRole("checkbox", { name: d.toggle }) as HTMLInputElement).checked).toBe(true);
    expect(askPreview.mock.calls[0][1].debug).toBe(true);
    cleanup();

    const second = makeClient();
    render(<AskPreviewDrawer client={second.client} school={SCHOOL} open onClose={() => {}} />);
    await screen.findByRole("option", { name: "Rodič" });
    fireEvent.click(screen.getByRole("checkbox", { name: d.toggle }));
    fireEvent.change(screen.getByLabelText(t.question), { target: { value: "Dotaz" } });
    fireEvent.click(screen.getByRole("button", { name: t.submit }));
    await screen.findByText("Prostá odpověď.");
    expect(second.askPreview.mock.calls[0][1].debug).toBe(false);
    expect(screen.queryByRole("region", { name: d.heading })).toBeNull();
  });
});

describe("debug panel – rows, ⓘ and legend", () => {
  it("gives every row an ⓘ button whose one-sentence explanation opens and closes, Escape closing only it", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    const infos = within(panel).getAllByRole("button", { name: /^Vysvětlení:/ });
    expect(infos.length).toBeGreaterThanOrEqual(12);

    const first = within(panel).getByRole("button", { name: d.infoLabel(d.rowOutcome("Návrh")) });
    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(within(panel).queryByRole("note")).toBeNull();
    fireEvent.click(first);
    expect(first.getAttribute("aria-expanded")).toBe("true");
    const note = within(panel).getByRole("note");
    expect(note.textContent).toBe(d.info.outcome);
    expect(first.getAttribute("aria-describedby")).toBe(note.id);

    fireEvent.keyDown(first, { key: "Escape" });
    expect(within(panel).queryByRole("note")).toBeNull();
    expect(screen.getByRole("dialog", { name: t.title })).toBeTruthy();

    fireEvent.click(first);
    fireEvent.click(first);
    expect(within(panel).queryByRole("note")).toBeNull();
  });

  it("opens a legend that lists every icon used", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    expect(within(panel).queryByRole("group", { name: d.legendHeading })).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: d.legend }));
    const legend = within(panel).getByRole("group", { name: d.legendHeading });
    expect(within(legend).getAllByRole("listitem")).toHaveLength(d.legendItems.length);
    for (const icon of ["✅", "💡", "⛔", "📋", "🔤", "🧭", "📄", "🏫", "⏱", "🌐", "👤", "🎓", "🔑", "🧩", "❓", "✔️", "⏭️", "⏸️", "⚠️", "🏁"]) {
      expect(legend.textContent).toContain(icon);
    }
    fireEvent.click(within(panel).getByRole("button", { name: d.legendHide }));
    expect(within(panel).queryByRole("group", { name: d.legendHeading })).toBeNull();
  });

  it("collapses and expands a section", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    const toggle = within(panel).getByRole("button", { name: new RegExp(d.sections.summary) });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const body = document.getElementById(toggle.getAttribute("aria-controls")!) as HTMLElement;
    expect(body.hidden).toBe(false);
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(body.hidden).toBe(true);
    fireEvent.click(toggle);
    expect(body.hidden).toBe(false);
  });
});

describe("debug panel – Souhrn", () => {
  it("shows outcome, source, duration and language", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    expect(within(panel).getByText(d.rowOutcome("Návrh"))).toBeTruthy();
    expect(within(panel).getByText(d.rowSource("QA – podobné znění"))).toBeTruthy();
    expect(within(panel).getByText(d.rowDuration(64))).toBeTruthy();
    expect(within(panel).getByText(d.rowLanguage("cs"))).toBeTruthy();
    expect(panel.textContent).toContain("💡");
    expect(panel.textContent).toContain("🔤");
  });

  it("draws the score bar with a marker for each threshold and a one-line verdict", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    const bar = within(panel).getByRole("img", { name: d.scoreBarLabel("0,48", "0,30", "0,60") });
    expect((bar.querySelector(".dbg-bar-fill") as HTMLElement).style.width).toBe("48%");
    expect((bar.querySelector(".is-suggest") as HTMLElement).style.left).toBe("30%");
    expect((bar.querySelector(".is-answer") as HTMLElement).style.left).toBe("60%");
    expect(within(panel).getByText(d.verdictSuggest)).toBeTruthy();
    expect(within(panel).getByText(`${d.markerAnswer} 0,60`)).toBeTruthy();
  });

  it("picks the verdict from the thresholds and the outcome", () => {
    expect(verdictOf(0.7, 0.6, 0.3, "answer")).toBe("answer");
    expect(verdictOf(0.48, 0.6, 0.3, "suggest")).toBe("suggest");
    expect(verdictOf(0.48, 0.6, 0.3, "answer")).toBe("belowAnswer");
    expect(verdictOf(0.1, 0.6, 0.3, "no_answer")).toBe("belowSuggest");
    expect(verdictOf(0.5, null, null, "answer")).toBeNull();
    expect(verdictOf(null, 0.6, 0.3, "answer")).toBeNull();
  });

  it("maps answer_source and strategies to the five source kinds", () => {
    expect(sourceKindOf({ answer_source: "qa_exact" })).toBe("qa_exact");
    expect(sourceKindOf({ answer_source: "qa_vector" })).toBe("qa_vector");
    expect(sourceKindOf({ answer_source: "rag" })).toBe("documents");
    expect(sourceKindOf({ answer_source: "meta" })).toBe("school");
    expect(sourceKindOf({ answer_source: null, strategies: ["document_lexical"] })).toBe("documents");
    expect(sourceKindOf({ answer_source: null, strategies: ["retrieval"] })).toBe("documents");
    expect(sourceKindOf({ answer_source: "none" })).toBe("none");
    expect(sourceKindOf({ answer_source: "weird" })).toBe("other");
  });

  it("highlights the scope that differs from what was asked and names what was asked", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    const scope = within(panel).getByRole("list", { name: d.rowScope });
    const diffs = scope.querySelectorAll("li.is-diff");
    // Asked: TEACHER, no grade; search ran with PARENT and grade 5. The school matches.
    expect(diffs).toHaveLength(2);
    expect(scope.textContent).toContain(d.scopeDiffers(d.scopeRole));
    expect(scope.textContent).toContain(d.scopeAsked("Učitel"));
    expect(scope.textContent).toContain(d.scopeDiffers(d.scopeGrade));
    expect(scope.textContent).not.toContain(d.scopeDiffers(d.scopeSchool));
  });

  it("does not highlight anything when the scope is what was asked", async () => {
    const same = { ...DEBUG_RESULT, effective_scope: { legal_entity: "zs_one", role: "teacher", school_grade: null } };
    const { client } = makeClient({ askPreview: vi.fn(async () => same) } as Partial<ApiClient>);
    const panel = await ask(client);
    expect(within(panel).getByRole("list", { name: d.rowScope }).querySelectorAll("li.is-diff")).toHaveLength(0);
  });
});

describe("debug panel – Jak otázku pochopil", () => {
  it("lists lemma tokens, weighted concepts with hit or miss, and words missing from the school's content", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    for (const token of ["kolik", "stat", "druzina"]) expect(within(panel).getByText(token)).toBeTruthy();

    const hit = within(panel).getByRole("img", { name: d.understanding.conceptHit });
    const miss = within(panel).getByRole("img", { name: d.understanding.conceptMiss });
    expect(hit.textContent).toBe("✔");
    expect(miss.textContent).toBe("✖");
    expect(hit.closest("li")?.textContent).toContain("zacina / zacinat");
    expect(hit.closest("li")?.textContent).toContain(d.understanding.conceptWeight("0,41"));
    expect(miss.closest("li")?.textContent).toContain(d.understanding.conceptWeight("0,47"));

    expect(within(panel).getByText("xyz")).toBeTruthy();
    expect(within(panel).getByText(d.understanding.inCorpusNo)).toBeTruthy();
    expect(within(panel).getByText("omluvenka")).toBeTruthy();
  });

  it("says so when explain failed, and still shows the rest", async () => {
    const noExplain = { ...DEBUG_RESULT, explain: null };
    const { client } = makeClient({ askPreview: vi.fn(async () => noExplain) } as Partial<ApiClient>);
    const panel = await ask(client);
    expect(within(panel).getByText(d.understanding.explainMissing)).toBeTruthy();
    expect(within(panel).getByText(d.rowOutcome("Návrh"))).toBeTruthy();
  });
});

describe("debug panel – Průchod kaskádou", () => {
  it("walks the trace up to the stop with Czech step names, glosses, +N passages and status icons", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    const c = d.cascade;
    for (const name of [c.steps.resolve_context, c.steps.route, c.steps.qa_lexical, "mystery_step", c.steps.document_lexical, c.steps.rag_keywords, c.steps.rag_vector, c.steps.answer]) {
      expect(within(panel).getByText(name)).toBeTruthy();
    }
    // Default: the path up to the stop – the step after it is hidden.
    expect(within(panel).queryByText(c.steps.document_siblings)).toBeNull();

    expect(within(panel).getByText(c.decisions["B-deferred"])).toBeTruthy();
    expect(within(panel).getByText("Z-9")).toBeTruthy(); // an unknown code stays raw
    expect(within(panel).getByText(c.added(3))).toBeTruthy();
    expect(within(panel).getByText(c.ms(11))).toBeTruthy();

    expect(within(panel).getByRole("img", { name: c.statuses.error }).textContent).toBe("⚠️");
    expect(within(panel).getByRole("img", { name: c.statuses.disabled }).textContent).toBe("⏸️");
    expect(within(panel).getByRole("img", { name: c.statuses.skipped }).textContent).toBe("⏭️");
    expect(within(panel).getAllByRole("img", { name: c.statuses.ran })[0].textContent).toBe("✔️");
    expect(within(panel).getByRole("img", { name: c.stopped }).textContent).toBe("🏁");
  });

  it("shows every step on request and goes back", async () => {
    const { client } = makeClient();
    const panel = await ask(client);
    fireEvent.click(within(panel).getByRole("button", { name: d.cascade.showAll }));
    expect(within(panel).getByText(d.cascade.steps.document_siblings)).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: d.cascade.showPath }));
    expect(within(panel).queryByText(d.cascade.steps.document_siblings)).toBeNull();
  });

  it("offers no 'show all' when the walk never stopped, and pluralizes passages in Czech", () => {
    expect(cascadeRows([{ step: "route" }, { step: "answer" }], false)).toEqual({ rows: [{ step: "route" }, { step: "answer" }], hiddenCount: 0 });
    expect(cascadeRows([{ step: "a", stopped_here: true }, { step: "b" }], false).hiddenCount).toBe(1);
    expect(d.cascade.added(1)).toBe("+1 pasáž");
    expect(d.cascade.added(3)).toBe("+3 pasáže");
    expect(d.cascade.added(7)).toBe("+7 pasáží");
  });

  it("reads thresholds, concepts and the n-gram chip defensively", () => {
    expect(qaLexicalDetail(undefined)).toEqual({ t_answer: null, t_suggest: null, concepts: [], ngram_chip: null });
    expect(qaLexicalDetail([{ step: "qa_lexical", detail: { t_answer: "x", concepts: "bad" } }]).concepts).toEqual([]);
    expect(qaLexicalDetail(DEBUG_RESULT.trace).t_suggest).toBe(0.3);
  });
});

describe("debug panel – QA", () => {
  it("resolves the winner by id, offers 'Otevřít v QA' and shows the n-gram chip and the suggestions", async () => {
    const onOpenEntry = vi.fn();
    const { client, getEntryById } = makeClient();
    const panel = await ask(client, { onOpenEntry });
    expect(await within(panel).findByText("Kolik stojí družina?", { selector: "strong" })).toBeTruthy();
    expect(getEntryById).toHaveBeenCalledWith("ZS_ONE", "entry-1", expect.anything());
    expect(within(panel).getByText(/balíček faq/)).toBeTruthy();
    expect(within(panel).getByText(d.qa.ngram("0,52", "0,40"))).toBeTruthy();
    expect(within(panel).getByText(/skóre 0,52/, { selector: "span.qa-muted" })).toBeTruthy();

    fireEvent.click(within(panel).getByRole("button", { name: d.qa.open }));
    expect(onOpenEntry).toHaveBeenCalledWith("faq", "k1");
  });

  it("says the entry is gone when the lookup fails, and has no button without the hook", async () => {
    const getEntryById = vi.fn(async () => {
      throw new ApiError(404, { code: "NOT_FOUND", message: "x", retryable: false });
    });
    const { client } = makeClient({ getEntryById } as Partial<ApiClient>);
    const panel = await ask(client);
    expect(await within(panel).findByText(d.qa.gone)).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: d.qa.open })).toBeNull();
  });
});

describe("debug panel – Dokumenty", () => {
  it("lists the sources with format chip and active and validity badges and opens the detail", async () => {
    const onOpenDocument = vi.fn();
    const { client, getDocument } = makeClient();
    const panel = await ask(client, { onOpenDocument });
    const first = within(panel).getByText("Školní řád", { selector: ".dbg-source-title" }).closest("li") as HTMLElement;
    expect(await within(first).findByText(d.docs.activeBadge)).toBeTruthy();
    expect(within(first).getByText("PDF")).toBeTruthy();
    expect(within(first).getByText(strings.documents.validityExpired)).toBeTruthy();
    expect(getDocument).toHaveBeenCalledWith("ZS_ONE", "doc-1", expect.anything());

    const second = within(panel).getByText("Provoz.pdf", { selector: ".dbg-source-title" }).closest("li") as HTMLElement;
    expect(await within(second).findByText(d.docs.inactiveBadge)).toBeTruthy();

    fireEvent.click(within(first).getByRole("button", { name: `${d.docs.open}: Školní řád` }));
    expect(onOpenDocument).toHaveBeenCalledWith("doc-1");
  });

  it("shows answer_parts as 'odstavec N ← dokument', unattributed ones plainly", async () => {
    const onOpenDocument = vi.fn();
    const { client } = makeClient();
    const panel = await ask(client, { onOpenDocument });
    const parts = within(panel).getByText(d.docs.parts).closest("li") as HTMLElement;
    const items = within(parts).getAllByRole("listitem");
    expect(items[0].textContent).toContain(`${d.docs.part(1)} ← `);
    fireEvent.click(within(items[0]).getByRole("button", { name: "Školní řád" }));
    expect(onOpenDocument).toHaveBeenCalledWith("doc-1");
    expect(items[1].textContent).toBe(`${d.docs.part(2)} ← ${d.docs.unattributed}`);
  });
});

describe("debug panel – Akce", () => {
  it("copies the whole response as JSON to the clipboard", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { client } = makeClient();
    const panel = await ask(client);
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: d.actions.copy }));
    });
    expect(writeText).toHaveBeenCalledTimes(1);
    const text = (writeText.mock.calls[0] as unknown as [string])[0];
    expect(JSON.parse(text)).toEqual(JSON.parse(JSON.stringify(DEBUG_RESULT)));
    expect(within(panel).getByText(d.actions.copied)).toBeTruthy();
  });

  it("tells when the clipboard refuses", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn(async () => Promise.reject(new Error("denied"))) },
      configurable: true,
    });
    const { client } = makeClient();
    const panel = await ask(client);
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: d.actions.copy }));
    });
    expect(within(panel).getByText(d.actions.copyFailed)).toBeTruthy();
  });

  it("compares the roles side by side with retrieval-only debug requests, one per role", async () => {
    const { client } = makeClient({
      askPreview: vi.fn(async (_s: string, body: AskPreviewBody) => {
        if (body.user_role === "STUDENT") throw new Error("boom");
        if (body.retrieval_only) {
          return { ...DEBUG_RESULT, outcome: body.user_role === "PARENT" ? "answer" : "no_answer", answer_source: body.user_role === "PARENT" ? "rag" : "none", match_score: 0.5, matched_entry_id: null };
        }
        return DEBUG_RESULT;
      }),
    } as Partial<ApiClient>);
    const panel = await ask(client);
    const spy = client.askPreview as unknown as ReturnType<typeof vi.fn>;
    spy.mockClear();
    fireEvent.click(within(panel).getByRole("button", { name: d.actions.compare }));
    const table = await within(panel).findByRole("table");

    const sent = spy.mock.calls.map((c) => c[1] as AskPreviewBody);
    expect(sent).toHaveLength(3);
    expect(sent.map((b) => b.user_role).sort()).toEqual(["PARENT", "STUDENT", "TEACHER"]);
    for (const body of sent) {
      expect(body).toMatchObject({ question: "Kolik stojí družina?", retrieval_only: true, debug: true, user_school_grade: null });
    }

    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent).filter(Boolean)).toEqual(["Učitel", "Rodič", "Žák"]);
    const outcomeRow = within(table).getByRole("rowheader", { name: d.actions.compareRowOutcome }).closest("tr") as HTMLElement;
    const cells = within(outcomeRow).getAllByRole("cell").map((c) => c.textContent);
    expect(cells[0]).toContain("Bez odpovědi");
    expect(cells[1]).toContain("Odpověď");
    expect(cells[2]).toBe(d.actions.compareFailed);
    const sourceRow = within(table).getByRole("rowheader", { name: d.actions.compareRowSource }).closest("tr") as HTMLElement;
    expect(within(sourceRow).getAllByRole("cell")[1].textContent).toContain("Dokumenty");
  });

  it("creates a QA question from the query through the hook and reports it", async () => {
    const onCreateQa = vi.fn(() => "ok" as const);
    const { client } = makeClient();
    const panel = await ask(client, { onCreateQa });
    fireEvent.click(within(panel).getByRole("button", { name: d.actions.createQa }));
    expect(onCreateQa).toHaveBeenCalledWith("Kolik stojí družina?");
    expect(within(panel).getByText(d.actions.createQaDone)).toBeTruthy();
  });

  it("asks to choose a pack when none is selected, and hides the action without the hook", async () => {
    const onCreateQa = vi.fn(() => "no-pack" as const);
    const { client } = makeClient();
    const panel = await ask(client, { onCreateQa });
    fireEvent.click(within(panel).getByRole("button", { name: d.actions.createQa }));
    expect(within(panel).getByText(d.actions.createQaNoPack)).toBeTruthy();
    cleanup();

    const again = makeClient();
    const panel2 = await ask(again.client);
    expect(within(panel2).queryByRole("button", { name: d.actions.createQa })).toBeNull();
  });
});

describe("history keeps the debug view", () => {
  it("shows the panel again when an earlier question is reopened", async () => {
    const { client } = makeClient();
    await ask(client);
    fireEvent.change(screen.getByLabelText(t.question), { target: { value: "Jiný dotaz" } });
    fireEvent.click(screen.getByRole("checkbox", { name: d.toggle }));
    fireEvent.click(screen.getByRole("button", { name: t.submit }));
    await screen.findByText("Prostá odpověď.");
    expect(screen.queryByRole("region", { name: d.heading })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: t.historyItem("Kolik stojí družina?") }));
    await waitFor(() => expect(screen.getByRole("region", { name: d.heading })).toBeTruthy());
  });
});
