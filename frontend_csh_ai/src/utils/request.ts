import { ref } from 'vue';

const API_BASE = '/api/v1';
const AUTH_VERIFY_INTERVAL_MS = 60 * 1000;
const AUTH_TOKEN_KEY = 'auth_token';
const USER_INFO_KEY = 'user_info';

let authMonitorStarted = false;
let authVerifyTimer: number | null = null;

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
  setAuth('', null);
  const redirectTo =
    options && typeof options === 'object' && 'redirectTo' in options
      ? options.redirectTo
      : undefined;
  const inferredPath = window.location.pathname.startsWith('/admin')
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

    if (response.status === 401 || response.status === 403) {
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
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`;
  const headers = new Headers(options.headers || {});
  
  if (authState.value.token) {
    headers.set('Authorization', `Bearer ${authState.value.token}`);
  }
  
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(url, { ...options, headers });
  
  if (response.status === 401 || response.status === 403) {
    logout();
    throw new Error('认证失败，请重新登录');
  }
  
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || data.detail || `HTTP Error ${response.status}`);
  }
  
  return data;
}
