import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const BACKEND = process.env.SCHEMA_BACKEND ?? 'http://127.0.0.1:8420';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // REST and the /api/ws WebSocket both go to the FastAPI backend.
      // No changeOrigin: the backend checks the WebSocket Origin against Host, so Host must stay :5173.
      '/api': { target: BACKEND, ws: true },
    },
  },
  build: {
    target: 'es2022',
    // No source maps in the build: the server would hand the readable source to anyone who asks.
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
