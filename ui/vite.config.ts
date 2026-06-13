import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // listen on all interfaces (LAN/Tailscale access)
    allowedHosts: [".ts.net"], // MagicDNS names; backend stays localhost-only behind the proxy
    proxy: {
      "/chat": "http://127.0.0.1:8000",
      "/ingest": "http://127.0.0.1:8000",
      "/documents": "http://127.0.0.1:8000",
      "/sources": "http://127.0.0.1:8000",
      "/users": "http://127.0.0.1:8000",
      "/tokens": "http://127.0.0.1:8000",
      "/settings": "http://127.0.0.1:8000",
      "/me": "http://127.0.0.1:8000",
      "/auth": "http://127.0.0.1:8000",
    },
  },
});
