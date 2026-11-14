import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import type { ApiClient } from "../api/client";
import type { DocumentOptions } from "../api/types";
import { DocumentUploadDialog } from "../components/DocumentUploadDialog";
import { DocumentDeleteDialog } from "../components/DocumentDeleteDialog";
import { strings } from "../strings.cs";
import optionsFixture from "../fixtures/documentOptions.json";

afterEach(cleanup);

const OPTIONS = optionsFixture as unknown as DocumentOptions;

function UploadHarness({ onClose }: { onClose: (sent: boolean) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Otevřít nahrávání
      </button>
      {open && (
        <DocumentUploadDialog
          client={{} as ApiClient}
          school="ZS_KAVCI_HORY"
          options={OPTIONS}
          uiDefaultLang="cs"
          onClose={(sent) => {
            onClose(sent);
            setOpen(false);
          }}
        />
      )}
    </div>
  );
}

describe("document dialogs — keyboard and labelling", () => {
  it("upload dialog: focus moves in, tabs are a real tablist, Tab is trapped, Escape closes and restores focus", async () => {
    const onClose = vi.fn();
    render(<UploadHarness onClose={onClose} />);
    const trigger = screen.getByText("Otevřít nahrávání");
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: strings.documentUpload.title });
    expect(dialog.contains(document.activeElement)).toBe(true);

    const tabs = within(dialog).getAllByRole("tab");
    expect(tabs).toHaveLength(2);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    tabs[0].focus();
    fireEvent.keyDown(tabs[0], { key: "ArrowRight" });
    const urlsTab = within(dialog).getByRole("tab", { name: /Webové stránky/ });
    expect(urlsTab.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(urlsTab);
    expect(within(dialog).getByRole("tabpanel", { name: /Webové stránky/ })).toBeTruthy();

    const buttons = within(dialog).getAllByRole("button");
    buttons[buttons.length - 1].focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onClose).toHaveBeenCalledWith(false);
    expect(document.activeElement).toBe(trigger);
  });

  it("delete dialog: labelled by its title, focus inside, Escape closes", () => {
    const onClose = vi.fn();
    render(
      <DocumentDeleteDialog
        client={{ getDocument: () => new Promise(() => {}) } as unknown as ApiClient}
        school="ZS_KAVCI_HORY"
        schoolName="ZŠ Kavčí hory"
        fileUuid="0b6f2c1e-6a4d-4c1b-9a51-2d1f3e5a7b01"
        onClose={onClose}
        onDeleted={() => {}}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: strings.documentDelete.title });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
