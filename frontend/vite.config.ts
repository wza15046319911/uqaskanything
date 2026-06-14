import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 开发期把 /api 代理到 FastAPI(默认 127.0.0.1:8077),前端同源调用、免 CORS。
// docker 里经 VITE_PROXY_TARGET 指向 backend 服务(http://backend:8077)。
// 后端启动:cd backend && uvicorn app.main:app --port 8077
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
