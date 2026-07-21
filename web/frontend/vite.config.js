import { defineConfig } from "vite";
import { resolve } from "node:path";

const backend = "http://127.0.0.1:8300";

export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/status": backend,
      "/account": backend,
      "/trades": backend,
      "/events": backend,
      "/hedge_stats": backend,
      "/api": backend,
      "/control": backend,
      "/webhook": backend,
    },
  },
  build: {
    outDir: resolve(import.meta.dirname, "../backend/static"),
    emptyOutDir: true,
  },
});
