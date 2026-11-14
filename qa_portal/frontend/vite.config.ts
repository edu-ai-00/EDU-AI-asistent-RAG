import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Mirrors web_chat/frontend/vite.config.ts. The base is the address the SPA is
// served from (CONTRACT.md §14.8: /knowledge/); the Dockerfile passes it as
// VITE_BASE. The dev proxy targets the edu_ai_qa_admin container port (8082).
export default defineConfig({
  base: process.env.VITE_BASE || "/knowledge/",
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/qa-api": "http://127.0.0.1:8082",
    },
  },
});
