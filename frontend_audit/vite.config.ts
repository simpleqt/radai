import { defineConfig, type Connect, type Plugin, type ProxyOptions } from 'vite'
import http from 'node:http'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET?.trim() || 'http://127.0.0.1:8082'
const knowledgeApiProxyTarget = process.env.VITE_KNOWLEDGE_API_PROXY_TARGET?.trim() || apiProxyTarget
const filesProxyTarget = process.env.VITE_FILES_PROXY_TARGET?.trim() || 'http://202.115.138.101:9004'
const extraAllowedHosts = (process.env.VITE_ALLOWED_HOSTS || '')
  .split(',')
  .map((host) => host.trim())
  .filter(Boolean)

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
  ]

function viteSecurityMiddleware(req: Connect.IncomingMessage, res: http.ServerResponse, next: () => void) {
  const url = req.url || ''
  const rawPath = url.split('?')[0]

  let decodedPath: string
  try {
    decodedPath = decodeURIComponent(rawPath)
  } catch {
    // 非法百分号编码（扫描器/畸形请求）会导致 URIError，拒绝即可
    res.statusCode = 400
    res.end('Bad Request')
    return
  }

  const normalizedPath = path.posix.normalize(decodedPath)

  for (const pattern of blockedPathPatterns) {
    if (pattern.test(normalizedPath) || pattern.test(decodedPath)) {
      res.statusCode = 403
      res.end('Forbidden')
      return
    }
  }

  if (/\/@fs\//.test(url)) {
    const fsPath = url.replace(/.*\/@fs/, '')
    const projectRoot = path.resolve(__dirname)
    const resolvedFsPath = path.resolve(fsPath.split('?')[0])
    if (!resolvedFsPath.startsWith(projectRoot)) {
      res.statusCode = 403
      res.end('Forbidden')
      return
    }
  }

  next()
}

function viteSecurityPlugin(): Plugin {
  return {
    name: 'vite-security-guard',
    enforce: 'pre',
    configureServer(server) {
      server.middlewares.use(viteSecurityMiddleware)
    },
    configurePreviewServer(server) {
      server.middlewares.use(viteSecurityMiddleware)
    },
  }
}

// server（本地开发）与 preview（正式服务走构建产物）共用同一份反代与主机白名单。
const sharedAllowedHosts = [
      'radai2.cdut.edu.cn',
      '202.115.138.57',
      'localhost',
      '127.0.0.1',
      ...extraAllowedHosts,
    ]

const sharedProxy: Record<string, ProxyOptions> = {
      '/knowledge-api': {
        target: knowledgeApiProxyTarget,
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(/^\/knowledge-api/, ''),
      },
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      '/files': {
        target: filesProxyTarget,
        changeOrigin: true,
      }
    }

export default defineConfig({
  plugins: [viteSecurityPlugin(), vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    assetsInlineLimit: 0,
  },
  server: {
    host: true,
    port: 5177,
    allowedHosts: sharedAllowedHosts,
    fs: {
      strict: true,
      allow: [path.resolve(__dirname)],
      deny: [
        '.env',
        '.env.*',
        '*.{crt,pem,key}',
        '**/node_modules/.vite/**',
      ],
    },
    proxy: sharedProxy,
  },
  preview: {
    host: true,
    port: 5177,
    allowedHosts: sharedAllowedHosts,
    proxy: sharedProxy,
  },
})

