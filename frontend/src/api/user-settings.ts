import { request } from '@/utils/request';

export interface CurrentUser {
  user_id: string;
  username: string;
  display_name?: string | null;
  email?: string | null;
  role: string;
  is_active: boolean;
  auth_provider?: 'password' | 'cas' | string;
  created_at?: string | null;
  updated_at?: string | null;
  last_login?: string | null;
}

export const UserSettingsAPI = {
  me: () => request('/auth/me') as Promise<{ user: CurrentUser }>,

  updateMe: (payload: { display_name?: string | null; email?: string | null }) =>
    request('/auth/me', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }) as Promise<{ user: CurrentUser }>,

  changePassword: (payload: { current_password: string; new_password: string }) =>
    request('/auth/me/change-password', {
      method: 'POST',
      body: JSON.stringify(payload),
    }) as Promise<{ success: boolean; message?: string | null }>,
};
