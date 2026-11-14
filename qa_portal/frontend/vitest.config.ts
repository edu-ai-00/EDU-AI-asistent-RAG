import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Separate from vite.config.ts on purpose: the app config carries the dev
// proxy and an env-driven base, which vitest should not depend on.
export default defineConfig({
  // Page paths are built from the base, so tests run under the real address.
  base: "/knowledge/",
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    setupFiles: ["src/testSetup.ts"],
    // The App-level flows wait on the mock client's 30 ms delays dozens of
    // times, so they land near vitest's 5 s default and flake under load.
    testTimeout: 20000,
  },
});
