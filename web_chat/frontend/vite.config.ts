import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/chat/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/chat/api": "http://127.0.0.1:8081",
      "/chat/health": "http://127.0.0.1:8081",
    },
  },
});
