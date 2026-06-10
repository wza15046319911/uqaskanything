import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发期把 /api 代理到 FastAPI(127.0.0.1:8077),前端同源调用、免 CORS。
// 后端启动:cd backend && uvicorn app.main:app --port 8077
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8077',
    },
  },
})
