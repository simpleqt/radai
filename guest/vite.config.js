import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';

const extraAllowedHosts = (process.env.VITE_ALLOWED_HOSTS || '')
  .split(',')
  .map((host) => host.trim())
  .filter(Boolean);

function viteSecurityPlugin() {
  const blockedPathPatterns = [
    /^\/etc\//,
    /^\/proc\//,
    /^\/sys\//,
    /^\/var\//,
    /^\/root\//,
    /^\/home\//,
    /^\/usr\//,
    /^\/boot\//,
    /^\/dev\//,
    /^\/tmp\//,
    /\.env/,
    /\.(pem|crt|key)(\?|$)/,
    /passwd/,
    /shadow/,
  ];

  return {
    name: 'vite-security-guard',
    enforce: 'pre',
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const url = req.url || '';
        const rawPath = url.split('?')[0];
        let decodedPath;
        try {
          decodedPath = decodeURIComponent(rawPath);
        } catch {
          _res.statusCode = 400;
          _res.end('Bad Request');
          return;
        }
        const normalizedPath = path.posix.normalize(decodedPath);

        for (const pattern of blockedPathPatterns) {
          if (pattern.test(normalizedPath) || pattern.test(decodedPath)) {
            _res.statusCode = 403;
            _res.end('Forbidden');
            return;
          }
        }

        if (/\/@fs\//.test(url)) {
          const fsPath = url.replace(/.*\/@fs/, '');
          const allowedRoots = [
            path.resolve(__dirname),
            path.resolve(__dirname, '../frontend/src'),
          ];
          const resolvedFsPath = path.resolve(fsPath.split('?')[0]);
          const isAllowed = allowedRoots.some(root => resolvedFsPath.startsWith(root));
          if (!isAllowed) {
            _res.statusCode = 403;
            _res.end('Forbidden');
            return;
          }
        }

        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [viteSecurityPlugin(), vue(), tailwindcss()],
  define: {
    'import.meta.env.VITE_FORCE_GUEST_MODE': JSON.stringify('true'),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '../frontend/src'),
      '@guest': path.resolve(__dirname, './src'),
    },
  },
  build: {
    assetsInlineLimit: 0,
  },
  server: {
    host: true,
    port: 8085,
    allowedHosts: [
      'radai2.cdut.edu.cn',
      '202.115.138.57',
      'localhost',
      '127.0.0.1',
      'fpitekai.com',
      ...extraAllowedHosts,
    ],
    fs: {
      strict: true,
      allow: [
        path.resolve(__dirname),
        path.resolve(__dirname, '../frontend/src'),
      ],
      deny: [
        '.env',
        '.env.*',
        '*.{crt,pem,key}',
        '**/node_modules/.vite/**',
      ],
    },
    proxy: {
      '/api': {
        target: process.env.GUEST_BACKEND_URL || 'http://127.0.0.1:8083',
        changeOrigin: true,
      },
    },
  },
});
