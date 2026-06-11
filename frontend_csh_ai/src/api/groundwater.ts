import { authState } from '@/utils/request';

export interface GroundwaterProject {
  project_id: string;
  name: string;
  region: string;
  data_version: number;
  quality_status: string;
  record_counts: Record<string, number>;
  read_only: boolean;
  authorized_user_count?: number;
}

export interface GroundwaterUser {
  user_id: string;
  username: string;
  email?: string | null;
  role: string;
}

async function groundwaterRequest(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers || {});
  if (authState.value.token) headers.set('Authorization', `Bearer ${authState.value.token}`);
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(`/api/v2${path}`, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  return data;
}

export const GroundwaterAPI = {
  listProjects: async (): Promise<GroundwaterProject[]> =>
    (await groundwaterRequest('/groundwater/projects')).projects || [],
  listAdminProjects: async (): Promise<GroundwaterProject[]> =>
    (await groundwaterRequest('/groundwater/admin/projects')).projects || [],
  listUsers: async (): Promise<GroundwaterUser[]> =>
    (await groundwaterRequest('/groundwater/admin/users')).users || [],
  listBindings: async (projectId: string) =>
    (await groundwaterRequest(`/groundwater/admin/projects/${encodeURIComponent(projectId)}/bindings`)).bindings || [],
  createProject: (data: Record<string, string | undefined>) =>
    groundwaterRequest('/groundwater/admin/projects', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateProject: (projectId: string, data: Record<string, string | undefined>) =>
    groundwaterRequest(`/groundwater/admin/projects/${encodeURIComponent(projectId)}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteProject: (projectId: string) =>
    groundwaterRequest(`/groundwater/admin/projects/${encodeURIComponent(projectId)}`, {
      method: 'DELETE',
    }),
  uploadPackage: (projectId: string, file: File) => {
    const body = new FormData();
    body.append('file', file);
    return groundwaterRequest(`/groundwater/admin/projects/${encodeURIComponent(projectId)}/upload`, {
      method: 'POST',
      body,
    });
  },
  replaceBindings: (projectId: string, userIds: string[]) =>
    groundwaterRequest(`/groundwater/admin/projects/${encodeURIComponent(projectId)}/bindings`, {
      method: 'PUT',
      body: JSON.stringify({ user_ids: userIds }),
    }),
};
