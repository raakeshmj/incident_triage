/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// The dashboard talks only to the Incident Intelligence API; in development
// Vite proxies /api to it (same origin, no CORS). API_PORT follows .env.
const apiTarget = process.env.DASHBOARD_API_URL ?? `http://localhost:${process.env.API_PORT ?? "8000"}`;

export default defineConfig({
  plugins: [react()],
  // Carbon's Sass references IBM Plex as `~@ibm/plex/...` (webpack style):
  // serve the fonts from the local package, never a CDN.
  resolve: { alias: [{ find: /^~@ibm\//, replacement: fileURLToPath(new URL("./node_modules/@ibm/", import.meta.url)) }] },
  server: { port: 5173, strictPort: true, proxy: { "/api": apiTarget } },
  preview: { port: 4173, strictPort: true, proxy: { "/api": apiTarget } },
  css: { preprocessorOptions: { scss: { quietDeps: true, silenceDeprecations: ["import", "global-builtin", "legacy-js-api"] } } },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
