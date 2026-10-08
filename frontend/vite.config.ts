import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the Vite server proxies /api to the backend so the browser sees a
// single origin and we never have to deal with CORS during development.
const BACKEND = process.env.VITE_BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/health": { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
});