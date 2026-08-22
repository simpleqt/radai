import { request } from '@/utils/request';

export interface AdminModel {
  id: number;
  name: string;
  display_name: string;
  description?: string | null;
  provider: string;
  use: string;
  model: string;
  api_key: string;
  base_url: string;
  api_key_env_var?: string | null;
  base_url_env_var?: string | null;
  request_timeout: number;
  max_retries: number;
  max_tokens: number;
  max_input_tokens: number;
  temperature: number;
  supports_vision: boolean;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  supports_streaming: boolean;
  is_active: boolean;
  is_default: boolean;
  sort_order: number;
  [key: string]: any;
}

export interface AdminAgent {
  agent_code: string;
  agent_name: string;
  description?: string | null;
  is_active: boolean;
  sort_order: number;
  kb_bindings: Array<{ kb_id: string; kb_name?: string | null; sort_order: number }>;
  has_display_record?: boolean;
  legacy_agent_code?: string | null;
}

export interface AdminConversation {
  id: string;
  thread_id: string;
  user_id: string;
  username: string;
  title: string;
  metadata?: Record<string, any> | null;
  is_deleted: boolean;
  created_at: string;
  updated_at: string;
}

const qs = (params: Record<string, any> = {}) => {
  const values = Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== '' && value !== undefined && value !== null),
  );
  const query = new URLSearchParams(values as Record<string, string>).toString();
  return query ? `?${query}` : '';
};

export const AdminPlatformAPI = {
  models: () => request('/models') as Promise<AdminModel[]>,
  createModel: (payload: Record<string, any>) =>
    request('/models', { method: 'POST', body: JSON.stringify(payload) }) as Promise<AdminModel>,
  updateModel: (id: number, payload: Record<string, any>) =>
    request(`/models/${id}`, { method: 'PUT', body: JSON.stringify(payload) }) as Promise<AdminModel>,
  toggleModel: (id: number, active: boolean) =>
    request(`/models/${id}/toggle?is_active=${active}`, { method: 'POST' }),
  deleteModel: (id: number) => request(`/models/${id}`, { method: 'DELETE' }),

  agents: () => request('/agents') as Promise<AdminAgent[]>,
  upsertAgent: (code: string, payload: Record<string, any>) =>
    request(`/agents/${encodeURIComponent(code)}`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }) as Promise<AdminAgent>,
  setAgentKbs: (code: string, kbIds: string[]) =>
    request(`/agents/${encodeURIComponent(code)}/kbs`, {
      method: 'PUT',
      body: JSON.stringify({ kb_ids: kbIds }),
    }) as Promise<AdminAgent>,

  conversationStats: () => request('/admin/conversations/stats') as Promise<Record<string, any>>,
  conversations: (params: Record<string, any>) =>
    request(`/admin/conversations${qs(params)}`) as Promise<{
      items: AdminConversation[];
      total: number;
      limit: number;
      offset: number;
    }>,
  conversation: (userId: string, threadId: string) =>
    request(`/admin/conversations/${encodeURIComponent(userId)}/${encodeURIComponent(threadId)}/messages`) as Promise<{
      conversation: AdminConversation;
      messages: Array<{ role: string; content: string; metadata?: Record<string, any>; id?: string }>;
    }>,
  deleteConversation: (userId: string, threadId: string) =>
    request(`/admin/conversations/${encodeURIComponent(userId)}/${encodeURIComponent(threadId)}`, { method: 'DELETE' }),
};
