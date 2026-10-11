import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Mirrors web_chat/frontend/vite.config.ts. base=/qa/ matches the nginx
// location this SPA is served from (CONTRACT.md §10); dev proxy targets the
// future edu_ai_qa_admin container port (8082), not any port on the host.
export default defineConfig({
  base: "/qa/",
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/qa-api": "http://127.0.0.1:8082",
    },
  },
});
