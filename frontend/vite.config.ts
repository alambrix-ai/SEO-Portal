import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

const threeVendor = fileURLToPath(
  new URL('./src/vendor/three/three.module.min.js', import.meta.url),
)

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // Bundled Three.js (MIT). Keep under src/ — Vite cannot import from public/.
      three: threeVendor,
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    // The API runs on 8000; proxying keeps the browser on one origin in
    // development, so CORS and cookie behaviour match production.
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
