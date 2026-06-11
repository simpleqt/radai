import { request } from '@/utils/request';

export const AuthAPI = {
  login: (username: string, password: string) => request('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password })
  }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  verify: () => request('/auth/verify')
};

export const KnowledgeBaseAPI = {
  list: (params: Record<string, any> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/knowledge_bases${qs ? '?' + qs : ''}`);
  },
  create: (data: any) => request('/knowledge_bases', {
    method: 'POST',
    body: JSON.stringify(data)
  }),
  get: (kbId: string) => request(`/knowledge_bases/${kbId}`),
  update: (kbId: string, data: any) => request(`/knowledge_bases/${kbId}`, {
    method: 'PUT',
    body: JSON.stringify(data)
  }),
  delete: (kbId: string) => request(`/knowledge_bases/${kbId}`, { method: 'DELETE' }),
  documents: (kbId: string, params: Record<string, any> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/knowledge_bases/${kbId}/documents${qs ? '?' + qs : ''}`);
  }
};
