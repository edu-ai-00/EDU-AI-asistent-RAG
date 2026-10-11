import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Separate from vite.config.ts on purpose: the app config carries the SPA
// base/proxy, which vitest does not need and should not depend on.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    // The App-level flows wait on the mock client's 30 ms delays dozens of
    // times, so they land near vitest's 5 s default and flake under load.
    testTimeout: 20000,
  },
});
