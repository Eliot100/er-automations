import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API client (src/api.ts) uses same-origin paths by default, so in dev we
// proxy the backend routes to the FastAPI server. Override the target with
// VITE_API_TARGET if the backend runs on a different host/port.
const target = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8800'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/healthz': target,
      '/automations': target,
      '/runs': target,
    },
  },
})
