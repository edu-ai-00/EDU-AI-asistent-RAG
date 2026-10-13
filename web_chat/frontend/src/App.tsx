import {
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useLocalRuntime,
  type ChatModelAdapter,
  type ChatModelRunResult,
} from "@assistant-ui/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import { DomainPicker, useDomains } from "./DomainPicker";
import { SchoolPicker, useSchool } from "./SchoolPicker";

type Outcome =
  | "answer"
  | "suggest"
  | "blocked"
  | "self_harm"
  | "unusable"
  | "no_answer"
  | "role_required"
  | "llm_unavailable";
type UserRole = "zak" | "ucitel" | "rodic";

const ROLE_STORAGE_KEY = "edu_ai_chat_role";

function readStoredValue<T extends string>(key: string, allowed: readonly T[]): T | null {
  const stored = window.localStorage.getItem(key);
  return (allowed as readonly string[]).includes(stored ?? "") ? (stored as T) : null;
}

type ChatApiResponse = {
  message_id: string;
  conversation_id: string;
  answer: string;
  outcome: Outcome;
  answer_source: string;
  // kind is a string, not a union — the server may add a kind, and a build that
  // does not know it must not break. The same reason as OUTCOME_LABELS below.
  suggestions: Array<{
    kind: string;
    entry_id: string;
    question: string;
    options: Array<{ label: string; question: string }>;
  }>;
  sources: Array<{ title: string; page?: number | null; url?: string | null }>;
  language: string;
  duration_ms: number;
  // What search ACTUALLY ran with. Optional: an older BFF does not send it.
  effective_scope?: {
    legal_entity?: string | null;
    role?: string | null;
    school_grade?: string | null;
    language?: string | null;
    domains?: string[] | null;
  } | null;
};

type ChatErrorResponse = {
  // `fields` and `retryable` are additive (BFF, 2026-09-06). `fields` is what
  // separates "your question is too long" from "your chosen area is gone" —
  // both used to arrive as one sentence.
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
    fields?: Array<{ field?: string; message?: string }>;
  };
};

// Record<Outcome, …> on purpose: adding to the union without a label is then a
// compile error. The lookup below still tolerates a value this build has never
// heard of — /search may grow outcomes, and a wrong heading over the server's own
// text is worse than none, so an unknown one renders no heading at all.
const OUTCOME_LABELS: Record<Outcome, string> = {
  answer: "Odpověď",
  suggest: "Možná jste mysleli",
  blocked: "Tento dotaz nemohu zpracovat",
  self_harm: "Důležitá pomoc",
  unusable: "Dotaz nelze použít",
  no_answer: "Odpověď nebyla nalezena",
  role_required: "Vyberte prosím svoji roli",
  llm_unavailable: "Dočasný technický problém",
};

function outcomeLabel(outcome: string): string {
  return OUTCOME_LABELS[outcome as Outcome] ?? "";
}

function lastUserText(messages: Parameters<ChatModelAdapter["run"]>[0]["messages"]): string {
  const message = [...messages].reverse().find((item) => item.role === "user");
  return (
    message?.content
      .filter((part) => part.type === "text")
      .map((part) => part.text)
      .join("\n") ?? ""
  );
}

function chatErrorResult(message: string): ChatModelRunResult {
  // Expected refusals belong in the inline error UI. Throwing also rejects the
  // composer's send promise in assistant-ui, even after it records this status.
  return {
    content: [],
    status: { type: "incomplete", reason: "error", error: { code: "unknown", message } },
  };
}

function createAdapter(
  gradeRef: React.RefObject<number | null>,
  roleRef: React.RefObject<UserRole | null>,
  entityRef: React.RefObject<string | null>,
  conversationIdRef: React.RefObject<string>,
  // The chosen areas and whether the catalog is still outstanding. REFS, not
  // props: the adapter is memoised once (a new adapter would reset the thread),
  // so a captured value would be the one from the first render forever — which
  // is how the grade and role are already handled here.
  domainsRef: React.RefObject<string[] | null>,
  domainsPendingRef: React.RefObject<boolean>,
  domainsBlockReasonRef: React.RefObject<string>,
  // Non-empty while no school is settled: the reason, in words for the user.
  schoolBlockReasonRef: React.RefObject<string>,
  // Called when the server says the school is gone, so the list is re-read.
  schoolRejectedRef: React.RefObject<() => void>,
): ChatModelAdapter {
  return {
    async run({ messages, abortSignal }) {
      // FIRST AND UNCONDITIONAL: without a settled school a question has no
      // scope. The composer is disabled too; this is the backstop for anything
      // that reaches the adapter another way (a suggestion chip, a stale tab).
      if (schoolBlockReasonRef.current) {
        return chatErrorResult(schoolBlockReasonRef.current);
      }
      // NOTHING GOES OUT WHILE THE SCOPE IS UNSETTLED — three cases, and the
      // third is the one that was missing. Until the catalog has loaded and a
      // remembered choice has been checked against it, a scoped question would
      // either carry a retired code (a 422) or silently search everything; and
      // when a retired code has been DROPPED, the selection falls back to
      // "all areas", which is the same silent widening arriving by another
      // route. Spec §7: until the check completes, no query is sent with stale
      // codes, and a query is never repeated automatically with a wider scope.
      if (domainsPendingRef.current) {
        return chatErrorResult(
          domainsBlockReasonRef.current ||
            "Ještě načítám oblasti hledání. Zkuste to prosím za chvíli.",
        );
      }
      // What this request belongs to. A school change starts a new conversation
      // (new id, same epoch bump as the area picker), so a different id after the
      // await means the answer belongs to a scope the user has already left.
      const startedIn = conversationIdRef.current;
      const body: Record<string, unknown> = {
        message: lastUserText(messages),
        school_grade: gradeRef.current,
        user_role: roleRef.current,
        // ignored by the BFF unless CHAT_ENTITY_PICKER is on; required when it is
        legal_entity: entityRef.current,
        conversation_id: conversationIdRef.current,
      };
      // OMITTED for "all areas", not sent as null: the body then matches what
      // this client sent before the feature existed.
      if (domainsRef.current && domainsRef.current.length > 0) {
        body.domains = domainsRef.current;
      }
      const response = await fetch("/chat/api/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        // the runtime's own signal: cancelling a question in flight (the user
        // switches area, or presses stop) aborts the request rather than
        // letting a late answer arrive in a conversation it does not belong to
        signal: abortSignal,
        body: JSON.stringify(body),
      });

      const data = (await response.json()) as ChatApiResponse | ChatErrorResponse;
      // A late answer from the previous school/conversation is dropped, never
      // shown in the new one (abort is requested too; this is the guarantee).
      if (abortSignal.aborted || conversationIdRef.current !== startedIn) {
        throw new DOMException("scope changed", "AbortError");
      }
      if (!response.ok) {
        const errorData = data as ChatErrorResponse;
        if (
          errorData.error?.code === "invalid_school" ||
          errorData.error?.code === "school_required"
        ) {
          schoolRejectedRef.current();
        }
        // NO AUTOMATIC RETRY, and specifically not one with domains omitted.
        // Falling back to "all areas" would answer a question the user did not
        // ask and present it as the one they did (spec §6). The message tells
        // them to choose again; the picker is where they do it.
        const detail = errorData.error?.fields?.[0]?.message;
        return chatErrorResult(
          errorData.error?.message ||
            detail ||
            "Odpověď se nepodařilo načíst.",
        );
      }

      const result = data as ChatApiResponse;
      return {
        content: [
          { type: "text", text: result.answer },
          { type: "data", name: "edu-ai-details", data: result },
        ],
      };
    },
  };
}

function ResponseDetails({ data }: { data: ChatApiResponse }) {
  return (
    <section className={`response-details response-details--${data.outcome}`}>
      {data.outcome !== "answer" && outcomeLabel(data.outcome) !== "" && (
        <strong>{outcomeLabel(data.outcome)}</strong>
      )}

      {data.suggestions.length > 0 && (
        <div className="suggestions" aria-label="Navrhované otázky">
          {data.suggestions.map((suggestion) =>
            // clarify is the only kind whose question must NOT be sent: it is a
            // request to clarify; the sendable text is in its options.
            suggestion.kind === "clarify" ? (
              // own block, not loose pills in the shared row: the options belong
              // to the prompt above them, and the row also carries independent
              // variant/related buttons that do not.
              <div className="clarify" key={suggestion.question}>
                <p className="clarify-prompt">{suggestion.question}</p>
                <div className="clarify-options">
                  {suggestion.options.map((option) => (
                    <ThreadPrimitive.Suggestion
                      className="suggestion-button"
                      key={option.question}
                      prompt={option.question}
                      send
                    >
                      {option.label}
                    </ThreadPrimitive.Suggestion>
                  ))}
                </div>
              </div>
            ) : (
              <ThreadPrimitive.Suggestion
                className="suggestion-button"
                key={suggestion.entry_id || suggestion.question}
                prompt={suggestion.question}
                send
              >
                {suggestion.question}
              </ThreadPrimitive.Suggestion>
            ),
          )}
        </div>
      )}

      {data.sources.length > 0 && (
        <details className="sources">
          <summary>Zdroje ({data.sources.length})</summary>
          <ol>
            {data.sources.map((source, index) => (
              <li key={`${source.title}-${index}`}>
                {source.url ? (
                  <a href={source.url} target="_blank" rel="noreferrer">
                    {source.title}
                  </a>
                ) : (
                  source.title
                )}
                {source.page ? `, strana ${source.page}` : ""}
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}

function MessageParts() {
  return (
    <MessagePrimitive.Parts>
      {({ part }) => {
        if (part.type === "text") {
          return (
            <div className="markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
                {part.text}
              </ReactMarkdown>
            </div>
          );
        }
        if (part.type === "data" && part.name === "edu-ai-details") {
          return <ResponseDetails data={part.data as ChatApiResponse} />;
        }
        return null;
      }}
    </MessagePrimitive.Parts>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="message-row message-row--user">
      <div className="message message--user">
        <MessageParts />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="message-row message-row--assistant">
      <div className="avatar" aria-hidden="true">
        AI
      </div>
      <div className="message message--assistant">
        <MessageParts />
        <MessagePrimitive.Error>
          <ErrorPrimitive.Root className="message-error" role="alert">
            <ErrorPrimitive.Message />
          </ErrorPrimitive.Root>
        </MessagePrimitive.Error>
      </div>
    </MessagePrimitive.Root>
  );
}

function NewChatButton({ onNewChat }: { onNewChat: () => void }) {
  const aui = useAui();
  return (
    <button
      className="secondary-button"
      type="button"
      onClick={() => {
        onNewChat();
        aui.thread().reset();
      }}
    >
      Nový chat
    </button>
  );
}

/** Starts a NEW conversation whenever the scope (school or areas) changes.
 *
 *  A separate component because `useAui` only works inside the runtime
 *  provider, while the pickers that change the scope sit in the header above
 *  it. It watches an epoch counter instead of the values themselves, so the
 *  reset rule lives in exactly one place and any future scope control gets it
 *  by bumping the same counter.
 *
 *  WHY RESET AT ALL. The server filters history by area as well (a client's new
 *  UUID cannot be relied upon — see conversation.load_seen), but the UI has its
 *  own reason: the visible transcript and the chips in it belong to the old
 *  scope. Leaving them on screen offers buttons that would search in an area
 *  the user has just left, which spec §6 forbids: old chips must not send
 *  queries in the new context.
 *
 *  The in-flight request is cancelled first, so a late answer cannot be
 *  appended to the new conversation.
 */
function ScopeResetter({ epoch }: { epoch: number }) {
  const aui = useAui();
  const seen = useRef(epoch);
  useEffect(() => {
    if (seen.current === epoch) return;   // first render is not a change
    seen.current = epoch;
    // reset() clears the transcript; the runtime aborts the running request's
    // signal as part of it, which is what the adapter's abortSignal is for
    aui.thread().reset();
  }, [epoch, aui]);
  return null;
}

function ChatThread({ gate }: { gate: { blocked: boolean; notice: string } }) {
  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <AuiIf condition={(state) => state.thread.isEmpty}>
          <section className="welcome">
            {gate.blocked ? (
              <>
                <h2>Nejdřív vyberte školu</h2>
                <p role="status">{gate.notice}</p>
              </>
            ) : (
              <>
                <h2>Na co se chcete zeptat?</h2>
                <p>Odpovědi vycházejí z dostupných školních podkladů.</p>
              </>
            )}
          </section>
        </AuiIf>

        <ThreadPrimitive.Messages>
          {({ message }) => (message.role === "user" ? <UserMessage /> : <AssistantMessage />)}
        </ThreadPrimitive.Messages>

        <AuiIf condition={(state) => state.thread.isRunning}>
          <div className="loading" role="status">
            Hledám odpověď…
          </div>
        </AuiIf>

        <ThreadPrimitive.ViewportFooter className="composer-footer">
          <ThreadPrimitive.ScrollToBottom className="scroll-button" aria-label="Přejít na konec">
            ↓
          </ThreadPrimitive.ScrollToBottom>
          <ComposerPrimitive.Root className="composer">
            {/* maxLength is the same 2000 the BFF and /v1/ask enforce. Without it
                the box takes a 3000-char paste and the only feedback is the BFF's
                generic "check the question and the grade" message — never "too long". */}
            <ComposerPrimitive.Input
              className="composer-input"
              placeholder={gate.blocked ? "Nejdřív vyberte školu…" : "Napište otázku…"}
              aria-label="Otázka"
              submitMode="enter"
              unstable_insertNewlineOnTouchEnter
              rows={1}
              maxLength={2000}
              disabled={gate.blocked}
            />
            <ComposerPrimitive.Send className="send-button" disabled={gate.blocked}>
              Odeslat
            </ComposerPrimitive.Send>
          </ComposerPrimitive.Root>
          <p className="composer-hint">Enter odešle, Shift+Enter vloží nový řádek.</p>
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
}

export default function App() {
  const [grade, setGrade] = useState<number | null>(null);
  const [role, setRole] = useState<UserRole | null>(() =>
    readStoredValue(ROLE_STORAGE_KEY, ["zak", "ucitel", "rodic"] as const),
  );
  const gradeRef = useRef<number | null>(grade);
  const roleRef = useRef<UserRole | null>(role);
  const entityRef = useRef<string | null>(null);
  const conversationIdRef = useRef(crypto.randomUUID());

  // A CHANGE OF SCOPE STARTS A NEW CONVERSATION. One counter, bumped by the
  // area picker and by the school picker, watched by ScopeResetter inside the
  // runtime provider. The conversation id changes here rather than in the
  // resetter so the very next request already carries the new one.
  const [scopeEpoch, setScopeEpoch] = useState(0);
  const newConversation = useCallback(() => {
    conversationIdRef.current = crypto.randomUUID();
    setScopeEpoch((e) => e + 1);
  }, []);

  // WHICH SCHOOL. Chosen by the visitor and remembered in this browser; with no
  // explicit deployment default nothing is chosen for them, and until it is
  // neither a question nor an area-catalog request goes out (rule 1 of
  // SchoolPicker.tsx).
  const school = useSchool(newConversation);
  const entity = school.entity;
  const domains = useDomains(entity, newConversation, school.canSend);
  const schoolBlockReasonRef = useRef<string>("");
  const schoolRejectedRef = useRef<() => void>(() => undefined);
  schoolBlockReasonRef.current = school.blockReason;
  schoolRejectedRef.current = school.reload;
  const domainsRef = useRef<string[] | null>(domains.selected);
  const domainsPendingRef = useRef<boolean>(domains.pending);
  // WHY it is blocked, so the composer's error names the thing to fix rather
  // than always claiming the catalog is still loading
  const domainsBlockReasonRef = useRef<string>("");
  domainsRef.current = domains.selected;
  // THREE reasons a scoped question must not go out, not one:
  //   pending       the catalog has not arrived, so a stored choice is unchecked
  //   error         there is no catalog to check it against at all
  //   needsChoice   a stored code was retired. Reporting that was not enough:
  //                 when every stored code had gone the selection fell back to
  //                 null — "all areas" — and the next question silently
  //                 searched the whole corpus. Blocked until the user makes or
  //                 confirms a choice.
  domainsPendingRef.current =
    domains.pending || domains.state.status === "error" || domains.needsChoice;
  domainsBlockReasonRef.current = domains.needsChoice
    ? "Vybraná oblast už pro tuto školu neexistuje. Zvolte prosím oblast znovu."
    : domains.state.status === "error"
      ? "Seznam oblastí se nepodařilo načíst. Zkuste to prosím znovu."
      : "Ještě načítám oblasti hledání. Zkuste to prosím za chvíli.";

  gradeRef.current = grade;
  roleRef.current = role;
  entityRef.current = entity;
  const adapter = useMemo(
    () => createAdapter(gradeRef, roleRef, entityRef, conversationIdRef,
                        domainsRef, domainsPendingRef, domainsBlockReasonRef,
                        schoolBlockReasonRef, schoolRejectedRef),
    [],
  );
  const runtime = useLocalRuntime(adapter);

  function updateRole(value: UserRole | null) {
    setRole(value);
    if (value) {
      window.localStorage.setItem(ROLE_STORAGE_KEY, value);
    } else {
      window.localStorage.removeItem(ROLE_STORAGE_KEY);
    }
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ScopeResetter epoch={scopeEpoch} />
      <main className="app-shell">
        <header className="app-header">
          <div>
            <h1>Edu AI chat</h1>
            <p>Jednoduché vyhledávání v podkladech</p>
          </div>
          <div className="header-actions">
            <label>
              Ročník
              <select
                value={grade ?? ""}
                onChange={(event) => setGrade(event.target.value ? Number(event.target.value) : null)}
              >
                <option value="">Neuveden</option>
                {Array.from({ length: 9 }, (_, index) => index + 1).map((value) => (
                  <option key={value} value={value}>
                    {value}.
                  </option>
                ))}
              </select>
            </label>
            <label>
              Role
              <select
                value={role ?? ""}
                onChange={(event) => updateRole(event.target.value ? (event.target.value as UserRole) : null)}
              >
                <option value="">Neuvedeno</option>
                <option value="zak">Žák</option>
                <option value="ucitel">Učitel</option>
                <option value="rodic">Rodič</option>
              </select>
            </label>
            <DomainPicker d={domains} />
            <SchoolPicker s={school} />
            <NewChatButton onNewChat={() => (conversationIdRef.current = crypto.randomUUID())} />
          </div>
        </header>
        <ChatThread
          gate={{
            blocked: !school.canSend,
            notice:
              school.forgotten && school.entity === null && school.state.status === "ready"
                ? "Dříve zvolená škola už není dostupná. Vyberte prosím školu v nabídce Škola."
                : school.state.status === "ready"
                  ? "Než budete moci psát, vyberte v nabídce Škola, ve které se má hledat."
                  : school.blockReason,
          }}
        />
      </main>
    </AssistantRuntimeProvider>
  );
}
