import { ref } from 'vue';

const API_BASE = '/api/v1';
const KNOWLEDGE_API_BASE = '/knowledge-api/api/v1';
const AUTH_VERIFY_INTERVAL_MS = 60 * 1000;
const AUTH_TOKEN_KEY = 'auth_token';
const USER_INFO_KEY = 'user_info';

let authMonitorStarted = false;
let authVerifyTimer: number | null = null;

export class RequestError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly requestId: string | null;

  constructor(
    message: string,
    options: { status: number; detail?: unknown; requestId?: string | null },
  ) {
    super(message);
    this.name = 'RequestError';
    this.status = options.status;
    this.detail = options.detail;
    this.requestId = options.requestId || null;
  }
}

function createRequestId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch {
    // Older browsers or restricted contexts may not expose crypto.randomUUID.
  }
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function getSessionToken(): string | null {
  return localStorage.getItem(AUTH_TOKEN_KEY);
}

function getSessionUser(): any | null {
  try {
    return JSON.parse(localStorage.getItem(USER_INFO_KEY) || 'null');
  } catch {
    return null;
  }
}

export const authState = ref({
  token: getSessionToken(),
  user: getSessionUser(),
});

export function setAuth(token: string, user: any) {
  authState.value.token = token;
  authState.value.user = user;
  if (token) {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
  } else {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  }
  // 清理历史版本遗留的 API Key 前端持久化
  localStorage.removeItem('api_key');

  if (user) {
    localStorage.setItem(USER_INFO_KEY, JSON.stringify(user));
  } else {
    localStorage.removeItem(USER_INFO_KEY);
  }
}

export function logout(options?: { redirectTo?: string } | Event) {
  // 服务端撤销（删 Redis token）：UI 登出若只清本地态，token 在服务端
  // 仍有效至过期，公共机房的「退出」不产生任何保护。fire-and-forget。
  const token = authState.value.token;
  if (token) {
    void fetch('/api/v1/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      // 页面即将跳转/销毁，keepalive 保证撤销请求不被中途丢弃
      keepalive: true,
    }).catch(() => undefined);
  }
  setAuth('', null);
  const redirectTo =
    options && typeof options === 'object' && 'redirectTo' in options
      ? options.redirectTo
      : undefined;
  const inferredPath = import.meta.env.VITE_FORCE_GUEST_MODE === 'true'
    ? (window.location.pathname || '/')
    : window.location.pathname.startsWith('/admin')
      ? '/admin/login'
      : '/login';
  window.location.href = redirectTo || inferredPath;
}

async function verifyTokenAndAutoLogout() {
  if (!authState.value.token) {
    return;
  }

  const headers = new Headers();
  headers.set('Authorization', `Bearer ${authState.value.token}`);

  try {
    const response = await fetch(`${API_BASE}/auth/verify`, {
      method: 'GET',
      headers,
    });

    if (response.status === 401) {
      logout();
    }
  } catch {
    // 网络抖动时不主动登出，避免误踢。
  }
}

export function startAuthAutoLogoutMonitor() {
  if (authMonitorStarted || typeof window === 'undefined') {
    return;
  }

  authMonitorStarted = true;

  void verifyTokenAndAutoLogout();

  authVerifyTimer = window.setInterval(() => {
    void verifyTokenAndAutoLogout();
  }, AUTH_VERIFY_INTERVAL_MS);

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      void verifyTokenAndAutoLogout();
    }
  });
}

export async function request(path: string, options: RequestInit = {}) {
  const knowledgeApiPrefixes = [
    '/knowledge_bases',
    '/uploads',
    '/documents',
    '/keywords',
    '/ingestion',
    '/admin/self-ingestion',
    '/health/self-ingestion',
    '/rag',
  ];
  const usesKnowledgeApi = knowledgeApiPrefixes.some(
    (prefix) => path === prefix || path.startsWith(`${prefix}/`) || path.startsWith(`${prefix}?`),
  );
  const url = path.startsWith('http') || path.startsWith('/api/')
    ? path
    : `${usesKnowledgeApi ? KNOWLEDGE_API_BASE : API_BASE}${path}`;
  const headers = new Headers(options.headers || {});

  if (authState.value.token) {
    headers.set('Authorization', `Bearer ${authState.value.token}`);
  }

  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (!headers.has('X-Request-ID')) {
    headers.set('X-Request-ID', createRequestId());
  }

  const response = await fetch(url, { ...options, headers });

  const data = await response.json().catch(() => ({}));
  const requestId = response.headers.get('X-Request-ID') || headers.get('X-Request-ID');
  const isAuthLoginRequest = /\/auth\/(?:login|cas\/exchange)$/.test(path);
  if (response.status === 401) {
    // A failed login is a normal form error; only an expired session should
    // clear the current user and redirect to the login page.
    if (!isAuthLoginRequest) logout();
    const detail = data.detail;
    const message =
      data.message ||
      (typeof detail === 'string' ? detail : detail?.message) ||
      (isAuthLoginRequest ? '用户名或密码错误' : '登录状态已过期，请重新登录');
    throw new RequestError(message, { status: 401, detail, requestId });
  }
  if (response.status === 403) {
    const detail = data.detail;
    const message = data.message || (typeof detail === 'string' ? detail : detail?.message) || '没有执行此操作的权限';
    throw new RequestError(message, { status: 403, detail, requestId });
  }

  if (!response.ok) {
    const detail = data.detail;
    const message =
      data.message ||
      (typeof detail === 'string' ? detail : detail?.message) ||
      `HTTP Error ${response.status}`;
    throw new RequestError(message, { status: response.status, detail, requestId });
  }

  return data;
}

function filenameFromContentDisposition(header: string | null): string | null {
  if (!header) return null;
  const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }
  const basicMatch = header.match(/filename="?([^";]+)"?/i);
  return basicMatch?.[1]?.trim() || null;
}

export async function downloadWithAuth(
  url: string,
  options?: { filename?: string; openInNewTab?: boolean },
) {
  const token = authState.value.token;
  if (!token) {
    throw new Error('请先登录后再下载资源');
  }
  // 同源硬断言：本函数会把 Bearer token 放进请求头，绝不允许发往其它源。
  if (new URL(url, window.location.origin).origin !== window.location.origin) {
    throw new Error('仅支持下载本站资源');
  }

  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
  });

  if (response.status === 401) {
    logout();
    throw new RequestError('登录状态已过期，请重新登录', { status: 401 });
  }

  if (response.status === 403) {
    throw new RequestError('没有下载此资源的权限', { status: 403 });
  }

  if (!response.ok) {
    let message = `下载失败 (${response.status})`;
    try {
      const data = await response.json();
      if (typeof data?.detail === 'string') message = data.detail;
    } catch {
      // ignore non-json error bodies
    }
    throw new RequestError(message, { status: response.status });
  }

  const blob = await response.blob();
  const filename =
    options?.filename
    || filenameFromContentDisposition(response.headers.get('Content-Disposition'))
    || 'download';
  const objectUrl = URL.createObjectURL(blob);

  if (options?.openInNewTab) {
    window.open(objectUrl, '_blank', 'noopener,noreferrer');
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    return;
  }

  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}
