import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };

const apiTarget = process.env.A2A_DASHBOARD_PROXY_TARGET ?? "http://api.127-0-0-1.nip.io";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.indexOf("/node_modules/") !== -1) return "vendor";
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/v1": {
        target: apiTarget,
        changeOrigin: true,
      },
      "/healthz": apiTarget,
    },
  },
});
