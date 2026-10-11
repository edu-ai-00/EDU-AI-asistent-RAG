import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ApiClient } from "../api/client";
import { ApiError, type Category } from "../api/types";
import { strings } from "../strings.cs";
import { CategoryManager } from "../components/CategoryManager";

afterEach(() => {
  cleanup();
});

function notImplemented(name: string) {
  return (...args: unknown[]) => {
    throw new Error(`${name} not stubbed in this test (args: ${JSON.stringify(args)})`);
  };
}

function makeClient(overrides: Partial<ApiClient>): ApiClient {
  return {
    getContext: notImplemented("getContext"),
    listSchools: notImplemented("listSchools"),
    listSchoolDomains: notImplemented("listSchoolDomains"),
    listCategories: notImplemented("listCategories"),
    createCategory: notImplemented("createCategory"),
    listPacks: notImplemented("listPacks"),
    createPack: notImplemented("createPack"),
    getMetadataOptions: notImplemented("getMetadataOptions"),
    listEntries: notImplemented("listEntries"),
    getEntry: notImplemented("getEntry"),
    previewPackChanges: notImplemented("previewPackChanges"),
    publishPackChanges: notImplemented("publishPackChanges"),
    previewImport: notImplemented("previewImport"),
    publishImport: notImplemented("publishImport"),
    getOperation: notImplemented("getOperation"),
    createExport: notImplemented("createExport"),
    downloadExportUrl: () => "#",
    deleteEntry: notImplemented("deleteEntry"),
    deletePack: notImplemented("deletePack"),
    ...overrides,
  } as unknown as ApiClient;
}

describe("CategoryManager — empty catalog (CONTRACT.md §4.3)", () => {
  it("plainly explains that no global topic exists yet and none is created automatically", () => {
    const client = makeClient({ createCategory: notImplemented("createCategory") });
    render(
      <CategoryManager
        client={client}
        categoriesState={{ status: "ready", value: [] }}
        uiDefaultLang="cs"
        onClose={() => {}}
        onCreated={() => {}}
      />,
    );

    expect(screen.getByText(strings.newCategory.emptyExplanation)).toBeTruthy();
  });

  it("does not show the explanation once the catalog has at least one topic", () => {
    const client = makeClient({});
    const existing: Category[] = [{ code: "AFTER_SCHOOL_CARE", label: "Družina", is_active: true }];
    render(
      <CategoryManager
        client={client}
        categoriesState={{ status: "ready", value: existing }}
        uiDefaultLang="cs"
        onClose={() => {}}
        onCreated={() => {}}
      />,
    );

    expect(screen.queryByText(strings.newCategory.emptyExplanation)).toBeNull();
  });
});

describe("CategoryManager — creating a global topic", () => {
  it("pre-fills a row for ui_default_lang and calls createCategory with the typed code and labels", async () => {
    let created = false;
    const client = makeClient({
      createCategory: async (body) => {
        created = true;
        expect(body).toEqual({ code: "SCHOOL_TRIPS", labels: { cs: "Školní výlety" } });
        return {
          code: "SCHOOL_TRIPS",
          label: "Školní výlety",
          is_active: true,
          labels: { cs: "Školní výlety" },
        } as Category;
      },
    });

    const onCreated = (category: Category) => {
      expect(category.code).toBe("SCHOOL_TRIPS");
    };

    render(
      <CategoryManager
        client={client}
        categoriesState={{ status: "ready", value: [] }}
        uiDefaultLang="cs"
        onClose={() => {}}
        onCreated={onCreated}
      />,
    );

    // Pre-filled row for ui_default_lang.
    const langInputs = screen.getAllByLabelText(strings.newCategory.langLabel) as HTMLInputElement[];
    expect(langInputs).toHaveLength(1);
    expect(langInputs[0].value).toBe("cs");

    fireEvent.change(screen.getByLabelText(strings.newCategory.code), { target: { value: "SCHOOL_TRIPS" } });
    fireEvent.change(screen.getAllByLabelText(strings.newCategory.textLabel)[0], { target: { value: "Školní výlety" } });

    fireEvent.click(screen.getByRole("button", { name: strings.newCategory.create }));

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(created).toBe(true);
  });

  it("lets the admin add another language row", () => {
    const client = makeClient({});
    render(
      <CategoryManager
        client={client}
        categoriesState={{ status: "ready", value: [] }}
        uiDefaultLang="cs"
        onClose={() => {}}
        onCreated={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: strings.newCategory.addLanguage }));
    expect(screen.getAllByLabelText(strings.newCategory.langLabel)).toHaveLength(2);
  });

  it("shows the server's 409 error envelope message for a duplicate code, without swallowing it", async () => {
    const client = makeClient({
      createCategory: async () => {
        throw new ApiError(409, { code: "CATEGORY_CODE_EXISTS", message: "Kód SCHOOL_TRIPS už existuje.", retryable: false });
      },
    });

    render(
      <CategoryManager
        client={client}
        categoriesState={{ status: "ready", value: [] }}
        uiDefaultLang="cs"
        onClose={() => {}}
        onCreated={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText(strings.newCategory.code), { target: { value: "SCHOOL_TRIPS" } });
    fireEvent.change(screen.getAllByLabelText(strings.newCategory.textLabel)[0], { target: { value: "Školní výlety" } });
    fireEvent.click(screen.getByRole("button", { name: strings.newCategory.create }));

    expect(await screen.findByText("Kód SCHOOL_TRIPS už existuje.")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("keeps the create button disabled for an invalid code", () => {
    const client = makeClient({});
    render(
      <CategoryManager
        client={client}
        categoriesState={{ status: "ready", value: [] }}
        uiDefaultLang="cs"
        onClose={() => {}}
        onCreated={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText(strings.newCategory.code), { target: { value: "not valid!" } });
    fireEvent.change(screen.getAllByLabelText(strings.newCategory.textLabel)[0], { target: { value: "Školní výlety" } });

    const createButton = screen.getByRole("button", { name: strings.newCategory.create }) as HTMLButtonElement;
    expect(createButton.disabled).toBe(true);
  });
});
