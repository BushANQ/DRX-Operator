import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'react', test: /\/node_modules\/(react|react-dom|scheduler)\//, priority: 30 },
            { name: 'graph', test: /\/node_modules\/@xyflow\//, priority: 20 },
            { name: 'icons', test: /\/node_modules\/@phosphor-icons\//, priority: 10 },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:7300',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:7300',
        ws: true,
      },
    },
  },
})
