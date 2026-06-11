import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '../frontend/src'),
      '@guest': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: true,
    port: 8085,
    allowedHosts: true,
    proxy: {
      '/api': {
        target: process.env.GUEST_BACKEND_URL || 'http://localhost:8083',
        changeOrigin: true,
      },
    },
  },
});
