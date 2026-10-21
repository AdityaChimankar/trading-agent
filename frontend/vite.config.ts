import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API runs separately (uvicorn on :8000). Proxying /api here means the
// front end always calls same-origin paths, so the same code works in dev
// and when the built bundle is served behind the API.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
