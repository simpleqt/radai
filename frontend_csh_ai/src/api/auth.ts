import { request } from '@/utils/request';

export interface LoginResponse {
  success: boolean;
  token?: string;
  user?: any;
  message?: string;
}

export const AuthAPI = {
  /**
   * User login
   */
  async login(username: string, password: string): Promise<LoginResponse> {
    return request('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
  },

  /**
   * User logout
   */
  async logout(): Promise<{ success: boolean }> {
    return request('/auth/logout', {
      method: 'POST'
    });
  },

  /**
   * Verify token
   */
  async verify(): Promise<{ valid: boolean; user?: any }> {
    return request('/auth/verify', {
      method: 'GET'
    });
  },

  /**
   * Build CAS login URL (browser redirect endpoint)
   */
  getCasLoginUrl(targetUrl?: string): string {
    if (!targetUrl) {
      return '/api/v1/auth/cas/login';
    }
    return `/api/v1/auth/cas/login?targetUrl=${encodeURIComponent(targetUrl)}`;
  }
};
