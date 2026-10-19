import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The API runs separately (uvicorn on :8000). Proxying /api here means the
// front end always calls same-origin paths, so the same code works in dev
// and when the built bundle is served behind the API.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8000',
          changeOrigin: true,
        },
      },
    },
  }
})
