// The SCHOOL picker: which school's material this browser asks about.
//
// The choice is a client scope, not a permission and not a server setting. It
// is made by the visitor in this browser, remembered here (localStorage, so it
// is per chat origin) and sent as `legal_entity` with every question; it never
// changes what any other visitor sees, and the BFF re-validates it against the
// active schools on every request. Three rules, each one a way this went wrong
// on paper before it was written:
//
//   1. NO SCHOOL IS CHOSEN FOR THE USER. A fresh chat with no explicit
//      deployment default waits for a choice — it does not take the first
//      school of the list. Until there is one, nothing is sent and no area
//      catalog is requested (App gates both on `canSend`).
//   2. A REMEMBERED CHOICE IS VERIFIED, NEVER TRUSTED. It is checked against the
//      fresh list on every load; a school that was deactivated or removed is
//      forgotten (and reported) and a new choice is required.
//   3. A LIST THAT CANNOT BE READ IS AN ERROR WITH A RETRY, never an empty
//      "configured" state. An empty or malformed list is not a finished setup.
//
// With the picker off the BFF fixes the school itself and ignores whatever the
// browser claims, so this hook reports `ready` with `entity === null` there.

import { useCallback, useEffect, useRef, useState } from "react";

export type SchoolEntity = { code: string; name: string };

export type SchoolState =
  | { status: "loading" }
  | { status: "error"; message: string; retryable: boolean }
  | { status: "ready"; picker: boolean; entities: SchoolEntity[] };

export const SCHOOL_STORAGE_KEY = "edu_ai_chat_school";

function readStoredSchool(): string | null {
  // localStorage throws in a private window and in some embedded webviews
  try {
    const value = window.localStorage.getItem(SCHOOL_STORAGE_KEY);
    return value && value.trim() ? value : null;
  } catch {
    return null;
  }
}

function writeStoredSchool(code: string | null): void {
  try {
    if (code) {
      window.localStorage.setItem(SCHOOL_STORAGE_KEY, code);
    } else {
      window.localStorage.removeItem(SCHOOL_STORAGE_KEY);
    }
  } catch {
    /* a remembered school is a convenience; losing it costs one click */
  }
}

/** Only well-formed rows survive; a malformed list is treated as no list. */
function parseEntities(raw: unknown): SchoolEntity[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const out: SchoolEntity[] = [];
  for (const row of raw) {
    const code = (row as { code?: unknown })?.code;
    const name = (row as { name?: unknown })?.name;
    if (typeof code !== "string" || !code.trim() || seen.has(code)) continue;
    seen.add(code);
    out.push({ code, name: typeof name === "string" && name.trim() ? name : code });
  }
  return out;
}

export type UseSchool = {
  state: SchoolState;
  /** The school to send as `legal_entity`; null until one is chosen (picker on). */
  entity: string | null;
  /** True when a question may be sent and the area catalog may be requested. */
  canSend: boolean;
  /** Why nothing may be sent yet, in words for the user; "" when canSend. */
  blockReason: string;
  /** A remembered school was found to be gone and has been forgotten. */
  forgotten: string | null;
  choose: (code: string | null) => void;
  reload: () => void;
};

export function useSchool(onScopeChange: () => void): UseSchool {
  const [state, setState] = useState<SchoolState>({ status: "loading" });
  const [entity, setEntity] = useState<string | null>(null);
  const [forgotten, setForgotten] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  // generation guard: a late config response must not overwrite a newer one
  const generation = useRef(0);
  // Keep the last usable scope while a reload is unsettled. A replacement by
  // the deployment default is a school change too, even without a menu click.
  const settledEntity = useRef<string | null>(null);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    const mine = ++generation.current;
    const controller = new AbortController();
    setState({ status: "loading" });
    setEntity(null);

    const fail = (message: string, retryable = true) => {
      if (mine !== generation.current) return;
      setState({ status: "error", message, retryable });
    };

    fetch("/chat/api/config", { credentials: "same-origin", signal: controller.signal })
      .then(async (r) => {
        if (!r.ok) throw new Error(String(r.status));
        return (await r.json()) as Record<string, unknown>;
      })
      .then((cfg) => {
        if (mine !== generation.current) return;
        if (!cfg || typeof cfg !== "object") throw new Error("config");

        if (!cfg.entity_picker) {
          if (cfg.setup === "unconfigured") {
            fail("Chat není nastavený — chybí škola, ve které se má hledat. Obraťte se na správce.", false);
            return;
          }
          // the BFF fixes the school itself and ignores the browser's claim
          if (settledEntity.current !== null) onScopeChange();
          settledEntity.current = null;
          setState({ status: "ready", picker: false, entities: [] });
          return;
        }

        const entities = parseEntities(cfg.entities);
        if (cfg.setup === "entities_unavailable") {
          fail("Seznam škol se nepodařilo načíst.");
          return;
        }
        if (entities.length === 0) {
          fail("Nejsou k dispozici žádné aktivní školy.");
          return;
        }
        const codes = new Set(entities.map((e) => e.code));
        const stored = readStoredSchool();
        let chosen: string | null = null;
        if (stored && codes.has(stored)) {
          chosen = stored;
        } else {
          if (stored) {
            // deactivated or removed since the choice was made: forget it
            writeStoredSchool(null);
            setForgotten(stored);
          }
          // only an explicit, valid deployment default may stand in; the BFF
          // returns it solely when it is an active school
          const dflt = typeof cfg.default === "string" ? cfg.default : "";
          if (dflt && codes.has(dflt)) chosen = dflt;
        }
        if (chosen !== null) {
          if (settledEntity.current !== null && settledEntity.current !== chosen) {
            onScopeChange();
          }
          settledEntity.current = chosen;
        }
        setEntity(chosen);
        setState({ status: "ready", picker: true, entities });
      })
      .catch((e) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        fail("Nastavení chatu se nepodařilo načíst.");
      });

    return () => controller.abort();
  }, [nonce, onScopeChange]);

  const choose = useCallback(
    (code: string | null) => {
      if (state.status !== "ready" || !state.picker) return;
      if (code && !state.entities.some((e) => e.code === code)) return;
      const next = code || null;
      if (next === entity) return;
      settledEntity.current = next;
      setEntity(next);
      setForgotten(null);
      writeStoredSchool(next);
      // a different school is a different scope: the caller starts a new
      // conversation and the area catalog is reloaded for it
      onScopeChange();
    },
    [state, entity, onScopeChange],
  );

  const canSend = state.status === "ready" && (!state.picker || entity !== null);
  const blockReason =
    state.status === "loading"
      ? "Ještě načítám nastavení chatu. Zkuste to prosím za chvíli."
      : state.status === "error"
        ? state.message
        : canSend
          ? ""
          : "Nejdřív vyberte školu.";

  return { state, entity, canSend, blockReason, forgotten, choose, reload };
}

export function SchoolPicker({ s }: { s: UseSchool }) {
  const { state } = s;
  if (state.status === "ready" && !state.picker) return null;

  if (state.status === "loading") {
    return (
      <label className="school-picker">
        Škola
        <select disabled aria-busy="true" value="">
          <option value="">Načítám školy…</option>
        </select>
      </label>
    );
  }

  if (state.status === "error") {
    return (
      <div className="school-picker">
        <span className="domain-label">Škola</span>
        {state.retryable ? (
          <button className="secondary-button domain-error" type="button" onClick={s.reload}>
            {state.message} Zkusit znovu
          </button>
        ) : (
          <span className="domain-error" role="alert">
            {state.message}
          </span>
        )}
      </div>
    );
  }

  return (
    <label className="school-picker">
      Škola
      <select
        value={s.entity ?? ""}
        onChange={(event) => s.choose(event.target.value || null)}
        aria-label="Škola"
      >
        <option value="" disabled={s.entity !== null}>
          Vyberte školu
        </option>
        {state.entities.map((item) => (
          <option key={item.code} value={item.code}>
            {item.name}
          </option>
        ))}
      </select>
    </label>
  );
}
