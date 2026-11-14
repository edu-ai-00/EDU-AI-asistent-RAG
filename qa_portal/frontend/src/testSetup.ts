import { afterEach, beforeEach } from "vitest";

// The selected school is remembered in localStorage; tests must not leak it.
function clearStorage() {
  try {
    window.localStorage.clear();
  } catch {
    // jsdom always has storage; stay quiet if a test replaced it.
  }
}

beforeEach(clearStorage);
afterEach(clearStorage);
