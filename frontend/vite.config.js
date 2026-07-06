import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Proxy API calls to the FastAPI backend during development.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5199,
    proxy: {
      '/api': 'http://localhost:8077',
      '/healthz': 'http://localhost:8077',
      '/auth': 'http://localhost:8077',
    },
  },
})
