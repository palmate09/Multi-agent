import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In dev the Vite server proxies /api to the backend so the browser sees a
// single origin and we never have to deal with CORS during development.
// /preview goes to the same place: the generated app is served by the backend's
// gateway, and the SPA iframe must reach it on the same origin for the session
// cookie to ride along.
const BACKEND = process.env.VITE_BACKEND_URL ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/health': { target: BACKEND, changeOrigin: true },
      '/preview': { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Split React out of the app chunk. It does not reduce the first-load
        // payload -- react-dom is ~140 KB of the 169 KB main bundle, which is
        // why lazy-loading the three panels moved it by only 5 KB -- but it
        // means a UI change no longer invalidates React in every returning
        // visitor's cache.
        manualChunks: id =>
          id.includes('node_modules/react') || id.includes('node_modules/scheduler')
            ? 'react'
            : undefined,
      },
    },
  },
});
