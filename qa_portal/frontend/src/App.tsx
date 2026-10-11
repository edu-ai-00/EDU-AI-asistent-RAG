import { useEffect, useMemo, useState } from "react";
import type { ApiClient } from "./api/client";
import { createLiveClient } from "./api/liveClient";
import { createMockClient } from "./api/mockClient";
import type { Category, Context, Domain, EntryPage, Pack, PackMetadata, School } from "./api/types";
import { API_BASE_URL, DEFAULT_UI_LANG, USE_FIXTURES } from "./config";
import { strings } from "./strings.cs";
import { useKeyedResource, type AsyncState } from "./hooks/useKeyedResource";
import { useEntries } from "./hooks/useEntries";
import {
  blankDraft,
  draftFromEntry,
  ensureDraft,
  hasUnsavedChanges,
  NEW_ENTRY_PREFIX,
  removeDraft,
  setDraftField,
  type DraftMap,
  type EntryDraft,
} from "./lib/drafts";
import { guardSwitch } from "./lib/switchGuard";
import { EntryList } from "./components/EntryList";
import { EntryEditor } from "./components/EntryEditor";
import { PublishPanel } from "./components/PublishPanel";
import { ImportDialog } from "./components/ImportDialog";
import { ExportDialog } from "./components/ExportDialog";
import { NewPackDialog } from "./components/NewPackDialog";
import { DeleteDialog } from "./components/DeleteDialog";

// Import/export/new-pack dialogs plus mobile layout and dialog keyboard
// handling (points 7-8 of the original brief) are wired below through
// Dialog.tsx. src/lib/*, src/hooks/useEntries.ts and src/api/* are already
// written and are only wired into the components below, not modified.

function createClient(): ApiClient {
  return USE_FIXTURES ? createMockClient() : createLiveClient(API_BASE_URL);
}

export default function App() {
  const client = useMemo(createClient, []);

  const { state: contextState } = useKeyedResource<Context>("context", (_key, signal) => client.getContext(signal));
  const { state: schoolsState } = useKeyedResource<School[]>("schools", (_key, signal) => client.listSchools(signal));
  const [selectedSchool, setSelectedSchool] = useState<string | null>(null);
  const [selectedPack, setSelectedPack] = useState<string | null>(null);
  const [selectedEntryKey, setSelectedEntryKey] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const [drafts, setDrafts] = useState<DraftMap>({});
  const [baselines, setBaselines] = useState<Record<string, EntryDraft>>({});

  // A pack created via "+ Nový balíček…" has no server state until its first
  // publish (CONTRACT.md §2.4a) — this remembers the confirmed metadata for
  // that one pending pack so PublishPanel can attach it to the preview call.
  // `published` flips the moment its first publish succeeds: the pack now HAS
  // server state (so entries load and no metadata may be sent any more), but the
  // selection must stay on it until the refreshed pack list actually lists it.
  const [pendingNewPack, setPendingNewPack] =
    useState<{ packId: string; metadata: PackMetadata; published?: boolean } | null>(null);

  useEffect(() => {
    if (schoolsState.status === "ready" && schoolsState.value.length > 0 && selectedSchool === null) {
      setSelectedSchool(schoolsState.value[0].code);
    }
  }, [schoolsState, selectedSchool]);

  const { state: packsState, loadedKey: packsLoadedFor, reload: reloadPacks } = useKeyedResource<Pack[]>(
    selectedSchool,
    (school, signal) => client.listPacks(school, signal),
  );
  const [activeDialog, setActiveDialog] = useState<"import" | "export" | "newPack" | "deletePack" | null>(null);

  useEffect(() => {
    if (packsState.status !== "ready") return;
    // The list still belongs to the school we just switched away from: picking
    // its first pack here would ask the NEW school for a FOREIGN pack (404).
    if (packsLoadedFor !== selectedSchool) return;
    if (pendingNewPack !== null) {
      if (packsState.value.some((p) => p.pack_id === pendingNewPack.packId)) {
        // Published: it now has a real manifest row, so it is safe to stop
        // treating it as pending and fall through to the normal checks below
        // (selectedPack already matches it, so nothing else changes here).
        setPendingNewPack(null);
      } else if (selectedPack === pendingNewPack.packId) {
        // Still no manifest row yet — never bounce the selection away from
        // a pack that only exists as this pending, unpublished state.
        return;
      }
    }
    if (packsState.value.length === 0) {
      if (selectedPack !== null) setSelectedPack(null);
      return;
    }
    if (!packsState.value.some((p) => p.pack_id === selectedPack)) {
      setSelectedPack(packsState.value[0].pack_id);
    }
  }, [packsState, packsLoadedFor, selectedSchool, selectedPack, pendingNewPack]);

  const { state: domainsState } = useKeyedResource<Domain[]>(selectedSchool, (school, signal) =>
    client.listSchoolDomains(school, signal),
  );

  const { state: categoriesState, reload: reloadCategories } = useKeyedResource<Category[]>("categories", (_key, signal) =>
    client.listCategories({ include_inactive: true }, signal),
  );

  const mayManageCategories = contextState.status === "ready" && Boolean(contextState.value.capabilities.may_manage_categories);
  const uiDefaultLang = contextState.status === "ready" ? contextState.value.ui_default_lang || DEFAULT_UI_LANG : DEFAULT_UI_LANG;

  const selectionIsPendingPack = pendingNewPack !== null && selectedPack === pendingNewPack.packId;
  const isPendingPack = selectionIsPendingPack && !pendingNewPack!.published;
  // A pending pack has no server state — GET .../entries would 404. Skip the
  // fetch entirely and show an empty, still-editable list instead.
  const { state: entriesStateFromApi, reload: reloadEntries } = useEntries(
    client,
    selectedSchool,
    isPendingPack ? null : selectedPack,
    query,
  );
  const entriesState: AsyncState<EntryPage> | { status: "empty" } = isPendingPack
    ? { status: "ready", value: { entries: [], next_cursor: null, total: 0, pack_etag: "", pack_version: 0, metadata_uniform: true } }
    : entriesStateFromApi;

  // Opens (or re-uses) a draft for the selected entry whenever the entry list
  // resolves and something is selected. ensureDraft leaves any in-progress
  // edit to this same key untouched — this is what "switching the SELECTED
  // question keeps its edits" means (drafts.ts).
  useEffect(() => {
    if (entriesState.status !== "ready" || selectedEntryKey === null) return;
    const entry = entriesState.value.entries.find((e) => e.entry_key === selectedEntryKey);
    if (!entry) return;
    const base = draftFromEntry(entry);
    setBaselines((prev) => (Object.prototype.hasOwnProperty.call(prev, selectedEntryKey) ? prev : { ...prev, [selectedEntryKey]: base }));
    setDrafts((prev) => ensureDraft(prev, selectedEntryKey, base));
  }, [entriesState, selectedEntryKey]);

  // Guards against a silent loss of unpublished work (CONTRACT.md §10): any
  // still-open browser tab/close needs the native confirm, since we cannot
  // intercept it otherwise.
  useEffect(() => {
    function handler(event: BeforeUnloadEvent) {
      if (!hasUnsavedChanges(drafts, baselines)) return;
      event.preventDefault();
      event.returnValue = "";
    }
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [drafts, baselines]);

  function handleSelectSchool(nextSchool: string) {
    if (nextSchool === selectedSchool) return;
    const { proceed, value } = guardSwitch({
      hasUnsavedChanges: hasUnsavedChanges(drafts, baselines),
      current: selectedSchool,
      next: nextSchool,
      confirm: (message) => window.confirm(message),
      message: strings.switchGuard.confirmMessage,
    });
    if (!proceed) return;
    setSelectedSchool(value);
    setSelectedPack(null);
    setSelectedEntryKey(null);
    setPendingNewPack(null);
  }

  function handleSelectPack(nextPack: string) {
    if (nextPack === selectedPack) return;
    const { proceed, value } = guardSwitch({
      hasUnsavedChanges: hasUnsavedChanges(drafts, baselines),
      current: selectedPack,
      next: nextPack,
      confirm: (message) => window.confirm(message),
      message: strings.switchGuard.confirmMessage,
    });
    if (!proceed) return;
    setSelectedPack(value);
    setSelectedEntryKey(null);
  }

  function handleDraftChange<K extends keyof EntryDraft>(field: K, value: EntryDraft[K]) {
    if (selectedEntryKey === null) return;
    setDrafts((prev) => setDraftField(prev, selectedEntryKey, field, value));
  }

  // "+ Přidat otázku": opens a fresh, local-only draft. The server assigns
  // the real entry_key on publish — this key only identifies the draft in
  // THIS browser tab and is never sent as entry_key (see buildEntryWrite).
  function handleAddQuestion() {
    if (selectedSchool === null || selectedPack === null) return;
    const key = `${NEW_ENTRY_PREFIX}${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    setDrafts((prev) => ({ ...prev, [key]: blankDraft() }));
    setSelectedEntryKey(key);
  }

  // Called only once pollOperationUntilDone reports status "completed":
  // drops the published drafts/baselines so a re-open of the same entry
  // starts clean from the refreshed server state, never from stale text.
  function handlePublished(publishedDraftKeys: string[]) {
    setDrafts((prev) => publishedDraftKeys.reduce((acc, key) => removeDraft(acc, key), prev));
    setBaselines((prev) => {
      const next = { ...prev };
      for (const key of publishedDraftKeys) delete next[key];
      return next;
    });
    if (selectedEntryKey !== null && publishedDraftKeys.includes(selectedEntryKey)) {
      setSelectedEntryKey(null);
    }
    // The pending pack now has a manifest row on the server. Do NOT drop the
    // pending state here: listPacks has not answered yet, so the effect above
    // would find the pack missing from the stale list and bounce the selection
    // to another pack. Mark it published and let that effect clear it once the
    // refreshed list really contains it.
    if (pendingNewPack !== null && selectedPack === pendingNewPack.packId) {
      setPendingNewPack({ ...pendingNewPack, published: true });
      reloadPacks();
    }
  }

  const newEntryKeys = Object.keys(drafts).filter((key) => key.startsWith(NEW_ENTRY_PREFIX));
  const pendingNewEntries = newEntryKeys.map((key) => ({ key, canonical_q: drafts[key].canonical_q }));

  const selectedEntry = entriesState.status === "ready" ? entriesState.value.entries.find((e) => e.entry_key === selectedEntryKey) ?? null : null;
  const selectedDraft = selectedEntryKey !== null ? drafts[selectedEntryKey] ?? null : null;
  const packEtag = entriesState.status === "ready" ? entriesState.value.pack_etag : null;

  return (
    <main className="app-shell">
      <header className="app-header">
        <div className="app-brand">
          <span className="app-brand-mark">
            EDU<small>AI</small>
          </span>
          <div>
            <h1>{strings.header.title}</h1>
            <p className="qa-muted">{strings.header.subtitle}</p>
          </div>
        </div>
        <span className="admin-badge">{strings.header.adminBadge}</span>
      </header>

      <section className="toolbar">
        <label className="field">
          <span>{strings.toolbar.school}</span>
          {schoolsState.status === "loading" && <span role="status">{strings.toolbar.loadingSchools}</span>}
          {schoolsState.status === "error" && <span role="alert">{strings.toolbar.schoolsError}</span>}
          {schoolsState.status === "ready" && (
            <select value={selectedSchool ?? ""} onChange={(event) => handleSelectSchool(event.target.value)}>
              {schoolsState.value.map((school) => (
                <option key={school.code} value={school.code}>
                  {school.name}
                </option>
              ))}
            </select>
          )}
        </label>

        <label className="field">
          <span>{strings.toolbar.pack}</span>
          {selectedSchool === null && <span className="qa-muted">{strings.editor.selectQuestionHint}</span>}
          {packsState.status === "loading" && <span role="status">{strings.toolbar.loadingPacks}</span>}
          {packsState.status === "error" && <span role="alert">{strings.toolbar.packsError}</span>}
          {packsState.status === "ready" && (
            <select value={selectedPack ?? ""} onChange={(event) => handleSelectPack(event.target.value)}>
              {selectionIsPendingPack && !packsState.value.some((p) => p.pack_id === pendingNewPack!.packId) && (
                <option value={pendingNewPack!.packId}>{pendingNewPack!.packId}</option>
              )}
              {packsState.value.map((pack) => (
                <option key={pack.pack_id} value={pack.pack_id}>
                  {pack.pack_id} · v{pack.version}
                </option>
              ))}
            </select>
          )}
        </label>

        <div className="toolbar-actions">
          <button type="button" disabled={selectedSchool === null} onClick={() => setActiveDialog("newPack")}>
            {strings.toolbar.newPack}
          </button>
          <button type="button" disabled={selectedSchool === null} onClick={() => setActiveDialog("import")}>
            {strings.toolbar.import}
          </button>
          <button type="button" disabled={selectedSchool === null || packsState.status !== "ready"} onClick={() => setActiveDialog("export")}>
            {strings.toolbar.export}
          </button>
          {/* Destructive, so it needs a real pack selected and its own dialog
              — never a bare confirm() (CONTRACT.md §13.2). */}
          <button
            type="button"
            className="danger"
            disabled={selectedSchool === null || selectedPack === null || pendingNewPack !== null}
            onClick={() => setActiveDialog("deletePack")}
          >
            {strings.deletePermanent.deletePackButton}
          </button>
        </div>
      </section>

      {activeDialog === "newPack" && selectedSchool !== null && (
        <NewPackDialog
          client={client}
          school={selectedSchool}
          onClose={() => setActiveDialog(null)}
          onCreated={(createdPackId, metadata) => {
            setPendingNewPack({ packId: createdPackId, metadata });
            setSelectedPack(createdPackId);
            setSelectedEntryKey(null);
            setActiveDialog(null);
          }}
        />
      )}

      {activeDialog === "deletePack" && selectedSchool !== null && selectedPack !== null && (
        <DeleteDialog
          client={client}
          target={{
            kind: "pack",
            school: selectedSchool,
            pack: selectedPack,
            entryCount:
              packsState.status === "ready"
                ? packsState.value.find((p) => p.pack_id === selectedPack)?.entry_count ?? 0
                : 0,
          }}
          onClose={() => setActiveDialog(null)}
          onDeleted={() => {
            setActiveDialog(null);
            setSelectedPack(null);
            setSelectedEntryKey(null);
            reloadPacks();
          }}
        />
      )}

      {activeDialog === "import" && selectedSchool !== null && (
        <ImportDialog
          client={client}
          school={selectedSchool}
          packs={packsState.status === "ready" ? packsState.value : []}
          initialPackId={selectedPack}
          onClose={() => setActiveDialog(null)}
          onImported={() => {
            reloadPacks();
            reloadEntries();
          }}
        />
      )}

      {activeDialog === "export" && selectedSchool !== null && packsState.status === "ready" && (
        <ExportDialog
          client={client}
          school={selectedSchool}
          packs={packsState.value}
          initialPackId={selectedPack}
          onClose={() => setActiveDialog(null)}
        />
      )}

      <p className="qa-muted work-area-note">
        {contextState.status === "ready"
          ? `Kontrakt ${contextState.value.contract_version} · režim ${contextState.value.mode}`
          : "Načítám kontext…"}
      </p>

      <section className="work-area two-pane">
        <EntryList
          state={entriesState}
          query={query}
          onQueryChange={setQuery}
          selectedEntryKey={selectedEntryKey}
          onSelect={setSelectedEntryKey}
          pendingNewEntries={pendingNewEntries}
          onAddQuestion={handleAddQuestion}
          domains={domainsState.status === "ready" ? domainsState.value : []}
        />
        <div className="entry-editor-column">
          <EntryEditor
            entry={selectedEntry}
            draft={selectedDraft}
            domainsState={domainsState}
            categoriesState={categoriesState}
            onChange={handleDraftChange}
            client={client}
            mayManageCategories={mayManageCategories}
            uiDefaultLang={uiDefaultLang}
            onCategoryCreated={() => reloadCategories()}
            school={selectedSchool}
            pack={selectedPack}
            onEntryDeleted={() => {
              // The entry is gone for good: drop the selection and reload the
              // list, never leave the editor pointing at a deleted row.
              setSelectedEntryKey(null);
              reloadEntries();
            }}
          />
          <PublishPanel
            client={client}
            school={selectedSchool}
            pack={selectedPack}
            packEtag={packEtag}
            drafts={drafts}
            baselines={baselines}
            onReload={reloadEntries}
            onPublished={handlePublished}
            pendingPackMetadata={isPendingPack ? pendingNewPack!.metadata : undefined}
          />
        </div>
      </section>
    </main>
  );
}
