// last change in code by Jan Drozd (2026-09-06).
//
// The AREA picker: which thematic areas of the school's material a question
// searches in. Its own file because almost none of it is markup — the useful
// part is a lifecycle with four ways to go wrong, each of which was named in
// spec.search-domain-scope §7 before it was written:
//
//   1. A LATE CATALOG must not overwrite a newer one. The user switches school
//      twice; the first fetch finishes last and the picker ends up showing the
//      first school's areas while the search runs in the second. Guarded by a
//      per-request generation counter, not by an AbortController alone — an
//      aborted fetch can still resolve.
//   2. A STORED CHOICE must be VERIFIED, never trusted. Codes are per school and
//      a curator can retire one; a remembered code that no longer exists would
//      be sent and answered with a 422. So the selection is intersected with the
//      catalog on arrival, and what fell away is REPORTED rather than dropped
//      silently — the user chose it, so they get told it is gone. And REPORTING
//      IS NOT ENOUGH: with every stored code retired the selection falls back to
//      null, which means all areas, so sending is BLOCKED (`needsChoice`) until
//      the user chooses or confirms. Storage is rewritten only by an explicit
//      choice, never by this effect — see the note at the intersection.
//   3. A CATALOG THAT CANNOT BE READ is an error with a retry, never an empty
//      list. An empty list renders as "this school has no areas", and the filter
//      would quietly become "everything" — the silent reset the spec forbids.
//   4. NOTHING IS SENT WHILE THE CATALOG IS UNKNOWN. Until it has loaded and
//      the stored choice has been checked against it, the composer is not
//      allowed to send a scoped question.
//
// Storage is keyed BY ENTITY (`edu_ai_chat_domains:<CODE>`), so switching school
// does not carry the previous school's codes over. The entity used for the key
// is the one the BFF says it resolved — with the picker off it ignores whatever
// the browser claims, and keying on the claim would write under a school that
// was never searched.
//
// "Všechny oblasti" is a REAL, explicit choice, not the absence of one. It is
// the default, it is always visible, and choosing it means `domains: null`.

import { useCallback, useEffect, useRef, useState } from "react";

export type Domain = { code: string; labels: Record<string, string> };

export type DomainCatalog = {
  legal_entity: string | null;
  entity_picker: boolean;
  domains: Domain[];
};

export type DomainState =
  | { status: "idle" }       // nothing requested yet: the school is not settled
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; entity: string | null; domains: Domain[] };

const STORAGE_PREFIX = "edu_ai_chat_domains:";

function storageKey(entity: string | null): string {
  return `${STORAGE_PREFIX}${entity ?? ""}`;
}

/** The label for the UI language, falling back to the code so a missing
 *  translation shows something a curator can recognise rather than "undefined". */
export function domainLabel(d: Domain, lang = "cs"): string {
  return d.labels?.[lang] || d.labels?.cs || d.labels?.en || d.code;
}

function readStored(entity: string | null): string[] {
  // localStorage throws in a private window and in some embedded webviews, and
  // a picker that cannot render is worse than one that forgets.
  try {
    const raw = window.localStorage.getItem(storageKey(entity));
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function writeStored(entity: string | null, codes: string[]): void {
  try {
    if (codes.length === 0) {
      window.localStorage.removeItem(storageKey(entity));
    } else {
      window.localStorage.setItem(storageKey(entity), JSON.stringify(codes));
    }
  } catch {
    /* a remembered filter is a convenience; losing it costs nothing */
  }
}

export type UseDomains = {
  state: DomainState;
  /** The codes to SEND: null = every area. Never a code outside the catalog. */
  selected: string[] | null;
  /** True while the catalog (or the check of a stored choice) is outstanding. */
  pending: boolean;
  /** Codes that were remembered but are no longer in this school's catalog. */
  dropped: string[];
  /**
   * A NEW CHOICE IS REQUIRED and nothing may be sent until it is made.
   *
   * True while `dropped` is non-empty. Rule 2 said "report what fell away", and
   * reporting it was not enough: when EVERY stored code had gone the selection
   * became `null`, which means **all areas**, and the next question searched
   * the whole corpus while the user believed a filter was on. That is the
   * silent widening spec §7 forbids ("dotaz automaticky neopakuje s širším
   * scope") — arrived at by a different route than a retry, with the same
   * result.
   *
   * Cleared by an explicit act only: acknowledging the notice, or choosing an
   * area / "all areas". Time or a re-render never clears it.
   */
  needsChoice: boolean;
  toggle: (code: string) => void;
  selectAll: () => void;
  reload: () => void;
  dismissDropped: () => void;
};

export function useDomains(
  entity: string | null,
  onScopeChange: () => void,
  // False until the school is settled (App's school hook). While false NOTHING is
  // fetched: a catalog for "no school yet" is a request for the wrong scope.
  enabled = true,
): UseDomains {
  const [state, setState] = useState<DomainState>({ status: enabled ? "loading" : "idle" });
  const [selected, setSelected] = useState<string[] | null>(null);
  const [dropped, setDropped] = useState<string[]>([]);
  // The generation guard for rule 1. An AbortController is not enough on its
  // own: abort() rejects the fetch but a response already in flight can still
  // land, and either way the SECOND fetch may finish first.
  const generation = useRef(0);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    const mine = ++generation.current;
    const controller = new AbortController();
    // Cleared while loading, so a question cannot be sent with the PREVIOUS
    // school's codes during the gap.
    setSelected(null);
    setDropped([]);
    if (!enabled) {
      setState({ status: "idle" });
      return () => controller.abort();
    }
    setState({ status: "loading" });

    const url = entity
      ? `/chat/api/domains?legal_entity=${encodeURIComponent(entity)}`
      : "/chat/api/domains";
    fetch(url, { credentials: "same-origin", signal: controller.signal })
      .then(async (r) => {
        if (!r.ok) throw new Error(String(r.status));
        return (await r.json()) as DomainCatalog;
      })
      .then((cat) => {
        if (mine !== generation.current) return; // a newer request owns the UI
        const domains = Array.isArray(cat.domains) ? cat.domains : [];
        // THE ENTITY THE SERVER RESOLVED, not the one we asked about: with the
        // picker off the BFF ignores the claim, and storing under the claim
        // would key the choice to a school that was never searched.
        const owner = cat.legal_entity ?? null;
        const codes = new Set(domains.map((d) => d.code));
        const stored = readStored(owner);
        const kept = stored.filter((c) => codes.has(c));
        const gone = stored.filter((c) => !codes.has(c));
        setState({ status: "ready", entity: owner, domains });
        // `kept.length > 0 ? kept : null` — and when it IS null because
        // everything was retired, `dropped` is non-empty and `needsChoice`
        // blocks sending. Without that block this line quietly turns a
        // narrowed search into an unrestricted one.
        setSelected(kept.length > 0 ? kept : null);
        setDropped(gone);
        // STORAGE IS NOT REWRITTEN HERE, and that is the fix for a bug the
        // browser test caught: this effect runs TWICE in the normal
        // configuration — once with `entity` still null on the first render,
        // then again when /chat/api/config resolves and sets it. Both runs
        // resolve to the same owner. The first version rewrote storage to the
        // survivors, so run 1 consumed the retired codes and run 2 re-read an
        // empty list, found nothing gone, and cleared the notice — the warning
        // flashed for one frame and the send-block never engaged at all. In
        // other words the whole of finding 3's fix was inert in the only
        // configuration production uses.
        //
        // So the stored value is the user's last EXPLICIT choice and only an
        // explicit choice rewrites it (`commit`, `dismissDropped`). The effect
        // derives kept/gone from it on every run and reaches the same answer,
        // so the notice survives a re-render. A retired code lingering in
        // storage is harmless: `selected` is set from `kept`, never from the
        // raw stored list, so an invalid code can never be sent.
      })
      .catch((e) => {
        if (mine !== generation.current) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        // rule 3: an error with a retry, NOT an empty catalog
        setState({
          status: "error",
          message: "Seznam oblastí se nepodařilo načíst.",
        });
        setSelected(null);
      });

    return () => controller.abort();
  }, [entity, nonce, enabled]);

  const owner = state.status === "ready" ? state.entity : null;

  const commit = useCallback(
    (next: string[] | null) => {
      setSelected(next);
      writeStored(owner, next ?? []);
      // An explicit choice IS the confirmation the notice was asking for.
      setDropped([]);
      // A CHANGE OF SCOPE STARTS A NEW CONVERSATION, and the caller does that —
      // not this component. Changing the dropdown sends NO question (spec §6:
      // "Pouhá změna dropdownu žádnou otázku neodešle"); it only resets what the
      // next question will belong to.
      onScopeChange();
    },
    [owner, onScopeChange],
  );

  const toggle = useCallback(
    (code: string) => {
      const cur = selected ?? [];
      const next = cur.includes(code) ? cur.filter((c) => c !== code) : [...cur, code];
      // Deselecting the last area is "all areas", not an empty filter: an empty
      // array is a 422, and a picker must not be able to build one.
      commit(next.length === 0 ? null : next);
    },
    [selected, commit],
  );

  const selectAll = useCallback(() => commit(null), [commit]);
  /** Confirming the notice IS a choice: keep the survivors and persist them, so
   *  the warning does not return on the next load. Without the write, storage
   *  would still hold the retired codes and the notice — and the send-block —
   *  would come back every time. */
  const dismissDropped = useCallback(() => {
    setDropped([]);
    writeStored(owner, selected ?? []);
  }, [owner, selected]);

  return {
    state,
    selected,
    // Block the first render of a new picker school before its effect clears
    // the old catalog. Fixed builds request entity=null and use the BFF owner.
    pending: state.status === "loading" || state.status === "idle"
      || (entity !== null && state.status === "ready" && state.entity !== entity),
    dropped,
    needsChoice: dropped.length > 0,
    toggle,
    selectAll,
    reload,
    dismissDropped,
  };
}

export function DomainPicker({ d, lang = "cs" }: { d: UseDomains; lang?: string }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click and on Escape — a panel that only closes by clicking
  // its own button traps keyboard users behind it.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (d.state.status === "idle") {
    return (
      <div className="domain-picker">
        <span className="domain-label">Oblast</span>
        <button className="secondary-button" type="button" disabled>
          Nejdřív vyberte školu
        </button>
      </div>
    );
  }

  if (d.state.status === "loading") {
    return (
      <div className="domain-picker">
        <span className="domain-label">Oblast</span>
        <button className="secondary-button" type="button" disabled aria-busy="true">
          Načítám oblasti…
        </button>
      </div>
    );
  }

  if (d.state.status === "error") {
    return (
      <div className="domain-picker">
        <span className="domain-label">Oblast</span>
        <button
          className="secondary-button domain-error"
          type="button"
          onClick={d.reload}
        >
          {d.state.message} Zkusit znovu
        </button>
      </div>
    );
  }

  const { domains } = d.state;
  const chosen = d.selected ?? [];
  const summary =
    chosen.length === 0
      ? "Všechny oblasti"
      : chosen.length === 1
        ? domainLabel(domains.find((x) => x.code === chosen[0]) ?? { code: chosen[0], labels: {} }, lang)
        : `${chosen.length} oblastí`;

  return (
    <div className="domain-picker" ref={boxRef}>
      <span className="domain-label" id="domain-picker-label">
        Oblast
      </span>
      <button
        className="secondary-button"
        type="button"
        aria-haspopup="true"
        aria-expanded={open}
        aria-labelledby="domain-picker-label"
        onClick={() => setOpen((o) => !o)}
        disabled={domains.length === 0}
        title={summary}
      >
        {domains.length === 0 ? "Bez oblastí" : summary} ▾
      </button>

      {d.dropped.length > 0 && (
        // Told, not swallowed — and BLOCKING. The user chose these and they are
        // gone, so the filter they think is on is not the filter that would be
        // applied. role="alert" rather than "status": it needs to interrupt,
        // because until it is answered no question can be sent.
        <p className="domain-dropped" role="alert">
          Tyto oblasti už škola nemá: {d.dropped.join(", ")}.{" "}
          {d.selected === null
            ? "Bez nové volby by se hledalo ve všech oblastech."
            : `Zbývá: ${d.selected.length}.`}{" "}
          Vyberte prosím oblast znovu, nebo potvrďte.{" "}
          <button type="button" className="link-button" onClick={d.dismissDropped}>
            {d.selected === null ? "Hledat ve všech oblastech" : "Rozumím"}
          </button>
        </p>
      )}

      {open && domains.length > 0 && (
        <div className="domain-menu" role="group" aria-labelledby="domain-picker-label">
          {/* "Všechny oblasti" is an item of the list, not the absence of a
              selection — it has to be clickable to get back to. */}
          <label className="domain-option">
            <input
              type="radio"
              name="domain-all"
              checked={chosen.length === 0}
              onChange={d.selectAll}
            />
            <span>Všechny oblasti</span>
          </label>
          <hr />
          {domains.map((dom) => (
            <label className="domain-option" key={dom.code}>
              <input
                type="checkbox"
                checked={chosen.includes(dom.code)}
                onChange={() => d.toggle(dom.code)}
              />
              <span>{domainLabel(dom, lang)}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
