import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// In dev, proxy /api to FastAPI (default 127.0.0.1:8077) so the frontend calls
// same-origin and avoids CORS.
// In docker, VITE_PROXY_TARGET points to the backend service (http://backend:8077).
// Start the backend: cd backend && uvicorn app.main:app --port 8077
const proxyTarget = process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8077'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': proxyTarget,
    },
  },
})
