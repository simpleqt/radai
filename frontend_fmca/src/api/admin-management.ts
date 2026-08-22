import { request } from '@/utils/request';

export interface AdminUser {
  user_id: string;
  username: string;
  email?: string | null;
  role: string;
  is_active: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  last_login?: string | null;
}

export interface AdminApiKey {
  id: number;
  api_key: string;
  user_id?: string | null;
  username?: string | null;
  key_name?: string | null;
  access_scope: 'full' | 'mcp_only';
  is_active: boolean;
  rate_limit: number;
  created_at?: string | null;
  expires_at?: string | null;
  last_used_at?: string | null;
  usage_count: number;
}

export interface AdminAuditLog {
  audit_id: number;
  actor_user_id?: string | null;
  actor_username?: string | null;
  action: string;
  target_type: string;
  target_id?: string | null;
  status: string;
  method?: string | null;
  path?: string | null;
  ip_address?: string | null;
  user_agent?: string | null;
  detail_json?: Record<string, any> | null;
  created_at?: string | null;
}

export interface AdminTask {
  task_id: string;
  doc_id: string;
  filename?: string | null;
  user_id?: string | null;
  username?: string | null;
  batch_id?: string | null;
  status: string;
  progress: number;
  priority: number;
  retry_count: number;
  max_retries: number;
  error?: string | null;
  error_detail?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface SystemComponent {
  status: string;
  detail: string;
  metadata: Record<string, any>;
}

export interface SelfIngestionHealthComponent {
  name: string;
  status: string;
  detail?: string | null;
}

export interface SelfIngestionHealth {
  status: string;
  queue_depth: number;
  components: SelfIngestionHealthComponent[];
}

export interface SelfIngestionJob {
  job_id: string;
  kb_id?: string | null;
  file_id?: string | null;
  doc_id?: string | null;
  status: string;
  stage?: string | null;
  error_message?: string | null;
  chunk_count: number;
  vector_count: number;
  retry_count: number;
  staging_uri?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
}

export interface SelfIngestionJobStats {
  total: number;
  by_status: Record<string, number>;
  failed_count: number;
  partial_count: number;
  completed_count: number;
  processing_count: number;
}

export interface SelfIngestionQualityStats {
  total_candidates: number;
  total_dropped: number;
  by_reason: Array<{
    kind: string;
    item_kind: string;
    reason: string;
    count: number;
  }>;
}

export interface SelfIngestionQualityFact {
  id: number;
  kind: 'candidate' | 'dropped';
  kb_id: string;
  job_id?: string | null;
  doc_id: string;
  item_kind: string;
  item_id: string;
  item_type?: string | null;
  confidence: number;
  reason: string;
  auto_action: string;
  requires_manual_review: boolean;
  payload?: Record<string, any> | any[] | string | number | boolean | null;
  created_at?: string | null;
}

export interface SelfIngestionQualityFactList {
  items: SelfIngestionQualityFact[];
  total: number;
  limit: number;
  offset: number;
}

const qs = (params: Record<string, any> = {}) => {
  const cleaned = Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '')
  );
  const query = new URLSearchParams(cleaned as Record<string, string>).toString();
  return query ? `?${query}` : '';
};

export const AdminManagementAPI = {
  overview: () => request('/admin/overview') as Promise<Record<string, any>>,

  users: (params: Record<string, any> = {}) =>
    request(`/admin/users${qs(params)}`) as Promise<{ items: AdminUser[]; total: number; limit: number; offset: number }>,

  createUser: (payload: {
    username: string;
    password?: string | null;
    email?: string | null;
    role: string;
    is_active: boolean;
    create_api_key: boolean;
  }) => request('/admin/users', { method: 'POST', body: JSON.stringify(payload) }) as Promise<AdminUser & {
    generated_password?: string | null;
    raw_api_key?: string | null;
  }>,

  updateUser: (userId: string, payload: Partial<Pick<AdminUser, 'email' | 'role' | 'is_active'>>) =>
    request(`/admin/users/${encodeURIComponent(userId)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }) as Promise<AdminUser>,

  resetPassword: (userId: string, newPassword?: string) =>
    request(`/admin/users/${encodeURIComponent(userId)}/reset-password`, {
      method: 'POST',
      body: JSON.stringify({ new_password: newPassword || null }),
    }) as Promise<{ success: boolean; generated_password?: string | null }>,

  setUserActive: (userId: string, active: boolean) =>
    request(`/admin/users/${encodeURIComponent(userId)}/${active ? 'enable' : 'disable'}`, {
      method: 'POST',
    }) as Promise<AdminUser>,

  apiKeys: (params: Record<string, any> = {}) =>
    request(`/admin/api-keys${qs(params)}`) as Promise<{ items: AdminApiKey[]; total: number; limit: number; offset: number }>,

  createApiKey: (userId: string, payload: {
    key_name?: string;
    access_scope: AdminApiKey['access_scope'];
    rate_limit: number;
    expires_at?: string | null;
  }) =>
    request(`/admin/users/${encodeURIComponent(userId)}/api-keys`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }) as Promise<AdminApiKey & { raw_api_key: string }>,

  updateApiKey: (keyId: number, payload: Partial<Pick<AdminApiKey, 'key_name' | 'access_scope' | 'is_active' | 'rate_limit' | 'expires_at'>>) =>
    request(`/admin/api-keys/${keyId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }) as Promise<AdminApiKey>,

  deleteApiKey: (keyId: number) => request(`/admin/api-keys/${keyId}`, { method: 'DELETE' }),

  rotateApiKey: (keyId: number) =>
    request(`/admin/api-keys/${keyId}/rotate`, { method: 'POST' }) as Promise<AdminApiKey & { raw_api_key: string }>,

  auditLogs: (params: Record<string, any> = {}) =>
    request(`/admin/audit-logs${qs(params)}`) as Promise<{ items: AdminAuditLog[]; total: number; limit: number; offset: number }>,

  auditLog: (auditId: number) =>
    request(`/admin/audit-logs/${auditId}`) as Promise<AdminAuditLog>,

  tasks: (params: Record<string, any> = {}) =>
    request(`/admin/tasks${qs(params)}`) as Promise<{ items: AdminTask[]; total: number; limit: number; offset: number }>,

  task: (taskId: string) =>
    request(`/admin/tasks/${encodeURIComponent(taskId)}`) as Promise<AdminTask>,

  retryTask: (taskId: string) =>
    request(`/admin/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST' }) as Promise<AdminTask>,

  cancelTask: (taskId: string) =>
    request(`/admin/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }) as Promise<AdminTask>,

  systemHealth: () => request('/admin/system/health') as Promise<Record<string, SystemComponent>>,

  selfIngestionHealth: () =>
    request('/admin/self-ingestion/health') as Promise<SelfIngestionHealth>,

  selfIngestionJobs: (params: Record<string, any> = {}) =>
    request(`/admin/self-ingestion/jobs${qs(params)}`) as Promise<{
      items: SelfIngestionJob[];
      total: number;
      limit: number;
      offset: number;
    }>,

  selfIngestionJobStats: (params: Record<string, any> = {}) =>
    request(`/admin/self-ingestion/jobs/stats${qs(params)}`) as Promise<SelfIngestionJobStats>,

  selfIngestionQualityStats: (params: Record<string, any> = {}) =>
    request(`/admin/self-ingestion/quality-facts/stats${qs(params)}`) as Promise<SelfIngestionQualityStats>,

  selfIngestionQualityFacts: (params: Record<string, any> = {}) =>
    request(`/admin/self-ingestion/quality-facts${qs(params)}`) as Promise<SelfIngestionQualityFactList>,

  selfIngestionQualityFact: (kind: SelfIngestionQualityFact['kind'], factId: number) =>
    request(`/admin/self-ingestion/quality-facts/${encodeURIComponent(kind)}/${factId}`) as Promise<SelfIngestionQualityFact>,

  repairSelfIngestionJob: (jobId: string) =>
    request(`/admin/self-ingestion/jobs/${encodeURIComponent(jobId)}/repair`, {
      method: 'POST',
    }) as Promise<Record<string, any>>,

  scanSelfIngestionRepair: (batchSize = 100) =>
    request(`/admin/self-ingestion/repair/scan${qs({ batch_size: batchSize })}`, {
      method: 'POST',
    }) as Promise<Record<string, any>>,

  shadowRebuildSelfIngestion: (payload: {
    kb_id?: string | null;
    kb_code?: string | null;
    limit?: number;
    dry_run?: boolean;
    reason?: string;
  }) =>
    request('/admin/self-ingestion/shadow-rebuild', {
      method: 'POST',
      body: JSON.stringify(payload),
    }) as Promise<Record<string, any>>,

  switchKbBackend: (
    kbId: string,
    payload: {
      ingestion_backend?: 'legacy_lightrag' | 'self_ingestion';
      retrieval_backend?: 'legacy_lightrag' | 'self_ingestion';
      self_ingestion_enabled?: boolean;
      self_retriever_enabled?: boolean;
      reason?: string;
    },
  ) =>
    request(`/admin/self-ingestion/kb/${encodeURIComponent(kbId)}/switch-backend`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }) as Promise<{
      kb_id: string;
      kb_code: string;
      ingestion_backend: string;
      retrieval_backend: string;
      self_ingestion_enabled: boolean;
      self_retriever_enabled: boolean;
      audit_note: string;
    }>,
};
