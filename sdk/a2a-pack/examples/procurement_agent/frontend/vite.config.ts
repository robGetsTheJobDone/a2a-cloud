import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const agent = process.env.A2A_DEV_AGENT_URL || "http://127.0.0.1:8000";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    proxy: {
      "/config.json": agent,
      "/a2a-client.js": agent,
      "/_a2a": agent,
      "/.well-known": agent,
      "/invoke": agent,
      "/auth": agent,
    },
  },
});

