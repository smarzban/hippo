import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/chat": "http://127.0.0.1:8000",
      "/ingest": "http://127.0.0.1:8000",
      "/documents": "http://127.0.0.1:8000",
      "/sources": "http://127.0.0.1:8000",
    },
  },
});
