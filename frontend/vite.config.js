import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Production build lands in the Python package so `pip install -e .` ships
// the dashboard without Node. Entry/CSS names are stable (no hash) because
// the server sends `Cache-Control: no-cache` for static files.
export default defineConfig(({ command }) => ({
  base: command === "build" ? "/static/" : "/",
  plugins: [svelte()],
  build: {
    target: "es2021",
    outDir: "../src/kusudaemon/dashboard/static",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: (info) =>
          (info.names || [info.name || ""]).some((n) => n.endsWith(".css")) ? "app.css" : "assets/[name]-[hash][extname]",
      },
    },
  },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: false } },
  },
}));
