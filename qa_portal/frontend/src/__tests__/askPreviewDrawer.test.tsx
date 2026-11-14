import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AskPreviewDrawer } from "../components/AskPreviewDrawer";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/types";
import type { AskPreviewResult, School } from "../api/types";
import { strings } from "../strings.cs";

const t = strings.ask;

const SCHOOL: School = {
  code: "ZS_ONE",
  name: "ZŠ Jedna",
  permissions: { read: true, publish: true, ingest: true, deactivate: true, delete_permanently: true },
};
const OTHER_SCHOOL: School = { ...SCHOOL, code: "ZS_TWO", name: "ZŠ Dvě" };

const OPTIONS = {
  audience_roles: [
    { code: "TEACHER", label: "Učitel" },
    { code: "PARENT", label: "Rodič" },
    { code: "STUDENT", label: "Žák" },
  ],
  school_grades: [
    { code: "1", label: "1. ročník" },
    { code: "2", label: "2. ročník" },
  ],
};

function result(overrides: Partial<AskPreviewResult> = {}): AskPreviewResult {
  return {
    answer: "Školní rok začíná **1. září**.",
    outcome: "answer",
    answer_source: "qa_exact",
    delivered_by: "online",
    sources: [],
    suggestions: [],
    matched_entry_id: null,
    match_score: null,
    duration_ms: 42,
    retrieval_only: false,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeClient(ask: (...args: unknown[]) => Promise<AskPreviewResult> = async () => result()) {
  const askPreview = vi.fn(ask);
  const getDocumentOptions = vi.fn(async () => OPTIONS);
  return { client: { askPreview, getDocumentOptions } as unknown as ApiClient, askPreview, getDocumentOptions };
}

async function renderOpen(client: ApiClient, extra: { school?: School; onClose?: () => void } = {}) {
  const onClose = extra.onClose ?? vi.fn();
  const view = render(<AskPreviewDrawer client={client} school={extra.school ?? SCHOOL} open onClose={onClose} />);
  await screen.findByRole("option", { name: "Rodič" });
  return { ...view, onClose };
}

const textarea = () => screen.getByLabelText(t.question) as HTMLTextAreaElement;
const submitButton = () => screen.getByRole("button", { name: t.submit }) as HTMLButtonElement;
const type = (value: string) => fireEvent.change(textarea(), { target: { value } });

afterEach(() => cleanup());

describe("AskPreviewDrawer open, close and focus", () => {
  it("renders nothing while closed and a labelled dialog with focus in the question when open", async () => {
    const { client } = makeClient();
    const { rerender } = render(<AskPreviewDrawer client={client} school={SCHOOL} open={false} onClose={() => {}} />);
    expect(screen.queryByRole("dialog")).toBeNull();

    rerender(<AskPreviewDrawer client={client} school={SCHOOL} open onClose={() => {}} />);
    const dialog = screen.getByRole("dialog", { name: t.title });
    expect(dialog.getAttribute("aria-modal")).toBe("false");
    expect(document.activeElement).toBe(textarea());
  });

  it("closes with Escape and with the close button", async () => {
    const { client } = makeClient();
    const { onClose } = await renderOpen(client);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: t.close }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("leaves Escape to a modal dialog on top", async () => {
    const { client } = makeClient();
    const { onClose } = await renderOpen(client);
    const modal = document.createElement("div");
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    document.body.appendChild(modal);
    fireEvent.keyDown(document, { key: "Escape" });
    modal.remove();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("does not react to Escape while closed", () => {
    const { client } = makeClient();
    const onClose = vi.fn();
    render(<AskPreviewDrawer client={client} school={SCHOOL} open={false} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps the draft when closed and opened again", async () => {
    const { client } = makeClient();
    const { rerender } = await renderOpen(client);
    type("Rozepsaná otázka");
    rerender(<AskPreviewDrawer client={client} school={SCHOOL} open={false} onClose={() => {}} />);
    rerender(<AskPreviewDrawer client={client} school={SCHOOL} open onClose={() => {}} />);
    await screen.findByRole("option", { name: "Rodič" });
    expect(textarea().value).toBe("Rozepsaná otázka");
  });

  it("uses a polite live region and accessible names for every control", async () => {
    const { client } = makeClient();
    await renderOpen(client);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText(t.role)).toBeTruthy();
    expect(within(dialog).getByLabelText(t.grade)).toBeTruthy();
    expect(within(dialog).getByLabelText(t.retrievalOnly)).toBeTruthy();
    expect(dialog.querySelector('[aria-live="polite"]')).not.toBeNull();
  });
});

describe("AskPreviewDrawer form", () => {
  it("offers the school's roles and grades plus 'neuvedeno'", async () => {
    const { client, getDocumentOptions } = makeClient();
    await renderOpen(client);
    expect(getDocumentOptions).toHaveBeenCalledWith("ZS_ONE", undefined, expect.anything());
    const grade = screen.getByLabelText(t.grade) as HTMLSelectElement;
    expect(Array.from(grade.options).map((o) => o.textContent)).toEqual([t.gradeNone, "1. ročník", "2. ročník"]);
    expect(grade.value).toBe("");
    expect((screen.getByLabelText(t.role) as HTMLSelectElement).value).toBe("TEACHER");
  });

  it("disables the submit button until there is a non-blank question", async () => {
    const { client, askPreview } = makeClient();
    await renderOpen(client);
    expect(submitButton().disabled).toBe(true);
    type("   \n ");
    expect(submitButton().disabled).toBe(true);
    type("Kdy začíná rok?");
    expect(submitButton().disabled).toBe(false);
    expect(askPreview).not.toHaveBeenCalled();
  });

  it("limits the question to 2000 characters", async () => {
    const { client } = makeClient();
    await renderOpen(client);
    expect(textarea().maxLength).toBe(2000);
  });

  it("sends the trimmed question, role, grade and retrieval_only", async () => {
    const { client, askPreview } = makeClient();
    await renderOpen(client);
    type("  Kdy začíná rok?  ");
    fireEvent.change(screen.getByLabelText(t.role), { target: { value: "STUDENT" } });
    fireEvent.change(screen.getByLabelText(t.grade), { target: { value: "2" } });
    fireEvent.click(screen.getByLabelText(t.retrievalOnly));
    fireEvent.click(submitButton());
    await waitFor(() => expect(askPreview).toHaveBeenCalledTimes(1));
    expect(askPreview).toHaveBeenCalledWith(
      "ZS_ONE",
      { question: "Kdy začíná rok?", user_role: "STUDENT", user_school_grade: "2", retrieval_only: true, debug: true },
      expect.anything(),
    );
  });

  it("sends a null grade for 'neuvedeno' and retrieval_only false by default", async () => {
    const { client, askPreview } = makeClient();
    await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());
    await waitFor(() => expect(askPreview).toHaveBeenCalled());
    expect(askPreview.mock.calls[0][1]).toEqual({
      question: "Dotaz",
      user_role: "TEACHER",
      user_school_grade: null,
      retrieval_only: false,
      debug: true,
    });
  });

  it("submits with Ctrl+Enter but not with a plain Enter", async () => {
    const { client, askPreview } = makeClient();
    await renderOpen(client);
    type("Dotaz");
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(askPreview).not.toHaveBeenCalled();
    fireEvent.keyDown(textarea(), { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(askPreview).toHaveBeenCalledTimes(1));
  });

  it("does not submit with Ctrl+Enter while the question is blank", async () => {
    const { client, askPreview } = makeClient();
    await renderOpen(client);
    fireEvent.keyDown(textarea(), { key: "Enter", ctrlKey: true });
    expect(askPreview).not.toHaveBeenCalled();
  });
});

describe("AskPreviewDrawer result", () => {
  it("shows the empty state first", async () => {
    const { client } = makeClient();
    await renderOpen(client);
    expect(screen.getByText(t.empty)).toBeTruthy();
  });

  it("shows loading, disables the button, then the answer with chips and duration", async () => {
    const pending = deferred<AskPreviewResult>();
    const { client } = makeClient(() => pending.promise);
    await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());
    expect((await screen.findByRole("status")).textContent).toBe(t.loading);
    expect((screen.getByRole("button", { name: t.submitting }) as HTMLButtonElement).disabled).toBe(true);

    await act(async () => pending.resolve(result()));
    expect(screen.getByText("1. září").tagName).toBe("STRONG");
    expect(screen.getByText(t.chipOutcome("odpověď"))).toBeTruthy();
    expect(screen.getByText(t.chipSource("QA, přesná shoda"))).toBeTruthy();
    expect(screen.getByText(t.chipDelivered("online"))).toBeTruthy();
    expect(screen.getByText(t.duration(42))).toBeTruthy();
    expect(screen.queryByText(t.chipRetrievalOnly)).toBeNull();
  });

  it("renders sources as safe links or plain names, suggestions and the matched entry", async () => {
    const { client } = makeClient(async () =>
      result({
        sources: [
          { rank: 1, title: "Školní řád", url: "https://zs.example/rad" },
          { rank: 2, title: "uuid_provozni_rad.pdf", url: null, original_file_name: "provozni rad.pdf" },
          { rank: 3, title: "Zlý odkaz", url: "javascript:alert(1)" },
        ],
        suggestions: [
          { kind: "qa", question: "Kdy končí rok?", entry_id: null, score: 0.8 },
          { kind: "related", question: "Omlouvání nepřítomnosti" },
        ],
        matched_entry_id: "11111111-1111-4111-8111-111111111111",
        match_score: 0.87,
      }),
    );
    await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());

    const link = await screen.findByRole("link", { name: "Školní řád" });
    expect(link.getAttribute("href")).toBe("https://zs.example/rad");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(screen.getByText("provozni rad.pdf").closest("a")).toBeNull();
    expect(screen.getByText("Zlý odkaz").closest("a")).toBeNull();
    expect(screen.getAllByRole("link")).toHaveLength(1);

    expect(screen.getByRole("heading", { name: t.suggestionsHeading })).toBeTruthy();
    expect(screen.getByText("Kdy končí rok?")).toBeTruthy();
    expect(screen.getByText("Omlouvání nepřítomnosti")).toBeTruthy();
    expect(screen.getByText(t.matched("11111111-1111-4111-8111-111111111111", 0.87))).toBeTruthy();
  });

  it("never turns raw HTML in an answer into markup", async () => {
    const { client } = makeClient(async () => result({ answer: "Text <img src=x onerror=alert(1)> konec" }));
    await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());
    await screen.findByText(t.resultHeading);
    expect(document.querySelector("img")).toBeNull();
  });

  it("handles retrieval_only: no answer text, sources still listed", async () => {
    const { client } = makeClient(async () =>
      result({
        answer: "",
        retrieval_only: true,
        answer_source: "rag",
        sources: [{ rank: 1, title: "Školní řád", url: "https://zs.example/rad" }],
      }),
    );
    await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());
    expect(await screen.findByText(t.answerSkipped)).toBeTruthy();
    expect(screen.getByText(t.chipRetrievalOnly)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Školní řád" })).toBeTruthy();
  });

  it("shows an empty-sources note and tolerates unknown outcome codes and null fields", async () => {
    const { client } = makeClient(async () =>
      result({ answer: null, outcome: "new_outcome", answer_source: null, delivered_by: null, duration_ms: null }),
    );
    await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());
    expect(await screen.findByText(t.answerEmpty)).toBeTruthy();
    expect(screen.getByText(t.sourcesNone)).toBeTruthy();
    expect(screen.getByText(t.chipOutcome("new_outcome"))).toBeTruthy();
  });

  it("shows the server message and its fields on an error, and recovers on the next try", async () => {
    let calls = 0;
    const { client } = makeClient(async () => {
      calls += 1;
      if (calls === 1) {
        throw new ApiError(422, {
          code: "VALIDATION_ERROR",
          message: "The request body did not pass validation",
          retryable: false,
          fields: [{ field: "user_role", message: "unknown role code 'X'" }],
        });
      }
      return result();
    });
    await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(t.errorHeading);
    expect(alert.textContent).toContain("The request body did not pass validation");
    expect(alert.textContent).toContain("unknown role code 'X'");

    fireEvent.click(submitButton());
    expect(await screen.findByText(t.resultHeading)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a plain network error message", async () => {
    const { client } = makeClient(async () => {
      throw new Error("Failed to fetch");
    });
    await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());
    expect((await screen.findByRole("alert")).textContent).toContain("Failed to fetch");
  });
});

describe("AskPreviewDrawer history and school switch", () => {
  async function askMany(client: ApiClient, questions: string[]) {
    for (const question of questions) {
      type(question);
      fireEvent.click(submitButton());
      await waitFor(() => expect(screen.getByText(t.resultHeading)).toBeTruthy());
      await waitFor(() => expect(submitButton().disabled).toBe(false));
    }
    return client;
  }

  it("keeps the last 5 results, newest first, and restores one on click", async () => {
    const { client } = makeClient(async (_school, body) =>
      result({ answer: `Odpověď na ${(body as { question: string }).question}`, outcome: "answer" }),
    );
    await renderOpen(client);
    await askMany(client, ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"]);

    const items = within(screen.getByRole("region", { name: t.historyHeading })).getAllByRole("button");
    expect(items.map((b) => b.getAttribute("aria-label"))).toEqual(
      ["Q6", "Q5", "Q4", "Q3", "Q2"].map((q) => t.historyItem(q)),
    );

    fireEvent.click(screen.getByRole("button", { name: t.historyItem("Q3") }));
    expect(textarea().value).toBe("Q3");
    expect(screen.getByText("Odpověď na Q3")).toBeTruthy();
  });

  it("does not show a history section before the first answer", async () => {
    const { client } = makeClient();
    await renderOpen(client);
    expect(screen.queryByRole("region", { name: t.historyHeading })).toBeNull();
  });

  it("clears the result and history when the school changes, and ignores a late answer", async () => {
    const pending = deferred<AskPreviewResult>();
    let first = true;
    const { client, askPreview, getDocumentOptions } = makeClient(() => {
      if (first) {
        first = false;
        return Promise.resolve(result({ answer: "Odpověď první školy" }));
      }
      return pending.promise;
    });
    const { rerender } = await renderOpen(client);
    type("Dotaz");
    fireEvent.click(submitButton());
    expect(await screen.findByText("Odpověď první školy")).toBeTruthy();
    expect(screen.getByRole("region", { name: t.historyHeading })).toBeTruthy();

    // A second question is still in flight when the school changes.
    fireEvent.click(submitButton());
    await waitFor(() => expect(askPreview).toHaveBeenCalledTimes(2));
    rerender(<AskPreviewDrawer client={client} school={OTHER_SCHOOL} open onClose={() => {}} />);
    await waitFor(() => expect(getDocumentOptions).toHaveBeenCalledWith("ZS_TWO", undefined, expect.anything()));
    await act(async () => pending.resolve(result({ answer: "Pozdní odpověď" })));

    expect(screen.queryByText("Odpověď první školy")).toBeNull();
    expect(screen.queryByText("Pozdní odpověď")).toBeNull();
    expect(screen.queryByRole("region", { name: t.historyHeading })).toBeNull();
    expect(screen.getByText(t.empty)).toBeTruthy();
    await screen.findByRole("option", { name: "Rodič" });
    expect((screen.getByLabelText(t.role) as HTMLSelectElement).value).toBe("TEACHER");
  });
});
