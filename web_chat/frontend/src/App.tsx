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
} from "@assistant-ui/react";
import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

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
  // kind je string, ne union — server může přidat druh a build, který o něm
  // neví, se nemá rozbít. Stejný důvod jako u OUTCOME_LABELS níž.
  suggestions: Array<{
    kind: string;
    entry_id: string;
    question: string;
    options: Array<{ label: string; question: string }>;
  }>;
  sources: Array<{ title: string; page?: number | null; url?: string | null }>;
  language: string;
  duration_ms: number;
};

type ChatErrorResponse = {
  error?: { message?: string };
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

function createAdapter(
  gradeRef: React.RefObject<number | null>,
  roleRef: React.RefObject<UserRole | null>,
  entityRef: React.RefObject<string | null>,
  conversationIdRef: React.RefObject<string>,
): ChatModelAdapter {
  return {
    async run({ messages, abortSignal }) {
      const response = await fetch("/chat/api/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        signal: abortSignal,
        body: JSON.stringify({
          message: lastUserText(messages),
          school_grade: gradeRef.current,
          user_role: roleRef.current,
          // ignored by the BFF unless CHAT_ENTITY_PICKER is on
          legal_entity: entityRef.current,
          conversation_id: conversationIdRef.current,
        }),
      });

      const data = (await response.json()) as ChatApiResponse | ChatErrorResponse;
      if (!response.ok) {
        const errorData = data as ChatErrorResponse;
        throw new Error(errorData.error?.message || "Odpověď se nepodařilo načíst.");
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
            // clarify je jediný druh, jehož question se posílat NESMÍ: je to
            // výzva k upřesnění, posílatelný text nesou options.
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

function ChatThread() {
  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <AuiIf condition={(state) => state.thread.isEmpty}>
          <section className="welcome">
            <h2>Na co se chcete zeptat?</h2>
            <p>Odpovědi vycházejí z dostupných školních podkladů.</p>
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
                generic "Zkontrolujte zadanou otázku a ročník" — never "too long". */}
            <ComposerPrimitive.Input
              className="composer-input"
              placeholder="Napište otázku…"
              aria-label="Otázka"
              submitMode="enter"
              unstable_insertNewlineOnTouchEnter
              rows={1}
              maxLength={2000}
            />
            <ComposerPrimitive.Send className="send-button">Odeslat</ComposerPrimitive.Send>
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
  // Test-only organisation picker. The list arrives from the BFF and is empty
  // unless CHAT_ENTITY_PICKER is on, so the dropdown never shows up in a build
  // that would ignore the value anyway.
  const [entities, setEntities] = useState<{ code: string; name: string }[]>([]);
  const [entity, setEntity] = useState<string | null>(null);
  useEffect(() => {
    fetch("/chat/api/config", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .then((cfg) => {
        if (!cfg?.entity_picker) return;
        setEntities(cfg.entities ?? []);
        setEntity(cfg.default || cfg.entities?.[0]?.code || null);
      })
      .catch(() => undefined);
  }, []);

  const gradeRef = useRef<number | null>(grade);
  const roleRef = useRef<UserRole | null>(role);
  const entityRef = useRef<string | null>(entity);
  const conversationIdRef = useRef(crypto.randomUUID());
  gradeRef.current = grade;
  roleRef.current = role;
  entityRef.current = entity;
  const adapter = useMemo(
    () => createAdapter(gradeRef, roleRef, entityRef, conversationIdRef),
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
            {entities.length > 0 && (
              <label>
                Organizace (test)
                <select value={entity ?? ""} onChange={(event) => setEntity(event.target.value || null)}>
                  {entities.map((item) => (
                    <option key={item.code} value={item.code}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <NewChatButton onNewChat={() => (conversationIdRef.current = crypto.randomUUID())} />
          </div>
        </header>
        <ChatThread />
      </main>
    </AssistantRuntimeProvider>
  );
}
