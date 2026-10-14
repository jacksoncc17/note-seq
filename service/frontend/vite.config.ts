import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The API is on 8100; in development Vite serves the page and proxies /api to it, so
// `npm run dev` and the built page behave the same way. In production FastAPI serves
// `dist/` itself and there is no proxy in the path at all.
export default defineConfig({
  // The page can be served at the root (its own host, or a tunnel) or under a path on
  // someone else's origin -- `VITE_BASE=/note-seq/` for the latter, which is what makes
  // it share that origin's login instead of needing one of its own.
  base: process.env.VITE_BASE ?? '/',
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: process.env.SERVICE_API ?? 'http://127.0.0.1:8100', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
