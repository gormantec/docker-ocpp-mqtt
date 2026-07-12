import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: '/webapp/ocpp-mqtt/',
  plugins: [react()],
  server: {
    proxy: {
      '/webapp/ocpp-mqtt/debug': {
        target: 'http://localhost:9094',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/webapp\/ocpp-mqtt/, ''),
      },
      '/debug': 'http://localhost:9094',
      '/health': 'http://localhost:9094',
    },
  },
  build: {
    outDir: 'dist',
  },
});
