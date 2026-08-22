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

  async getGuestSmsCaptcha(): Promise<{
    captcha_id: string;
    image_data: string;
    expires_in: number;
  }> {
    return request('/guest-sms/captcha', {
      method: 'GET',
    });
  },

  async sendGuestSmsCode(payload: {
    telephone: string;
    captcha_id: string;
    captcha_answer: string;
  }): Promise<{
    success: boolean;
    message?: string;
    error?: string;
    refresh_captcha?: boolean;
    retry_after?: number;
  }> {
    return request('/guest-sms/send-code', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async guestSmsLogin(telephone: string, code: string): Promise<LoginResponse & { error?: string }> {
    return request('/guest-sms/login', {
      method: 'POST',
      body: JSON.stringify({ telephone, code }),
    });
  },

  async guestSmsRegister(payload: {
    telephone: string;
    username?: string;
    code: string;
    password: string;
    confirm_password: string;
    display_name?: string;
  }): Promise<LoginResponse & { error?: string }> {
    return request('/guest-sms/register', {
      method: 'POST',
      body: JSON.stringify(payload),
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
  },

  async exchangeCasCode(code: string): Promise<LoginResponse> {
    return request('/auth/cas/exchange', {
      method: 'POST',
      body: JSON.stringify({ code }),
    });
  }
};
