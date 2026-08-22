import { request } from '@/utils/request';

export interface DatasetColumn {
  name: string;
  dtype?: string;
  non_null?: number;
  total?: number;
  sample?: string[];
}

export interface ExperimentDataset {
  id: string;
  code: string;
  name: string;
  description?: string;
  file_type: string;
  file_size?: number | null;
  storage_type: string;
  sheet_names: string[];
  row_counts: Record<string, number>;
  column_schema: Record<string, DatasetColumn[]>;
  domain?: string;
  tags: string[];
  agent_codes: string[];
  is_public: boolean;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface DatasetUpdateInput {
  name?: string;
  description?: string;
  domain?: string;
  tags?: string[];
  agent_codes?: string[];
  is_public?: boolean;
  status?: string;
}

export interface DatasetPreviewResponse {
  dataset_id: string;
  name: string;
  storage_type: string;
  rows_preview: number;
  sheets: Record<string, Record<string, any>[]>;
}

export interface DatasetUploadInput extends DatasetUpdateInput {
  code: string;
  file: File;
}

const qs = (params: Record<string, any> = {}) => {
  const cleaned = Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '')
  );
  const query = new URLSearchParams(cleaned as Record<string, string>).toString();
  return query ? `?${query}` : '';
};

export const DatasetAdminAPI = {
  list: (params: Record<string, any> = {}) =>
    request(`/datasets${qs(params)}`) as Promise<{ total: number; datasets: ExperimentDataset[] }>,

  get: (id: string) => request(`/datasets/${id}`) as Promise<ExperimentDataset>,

  update: (id: string, data: DatasetUpdateInput) =>
    request(`/datasets/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }) as Promise<ExperimentDataset>,

  upload: (data: DatasetUploadInput) => {
    const form = new FormData();
    form.append('file', data.file);
    form.append('code', data.code);
    form.append('name', data.name || data.file.name);
    form.append('description', data.description || '');
    form.append('domain', data.domain || '');
    form.append('tags', JSON.stringify(data.tags || []));
    form.append('agent_codes', JSON.stringify(data.agent_codes || []));
    form.append('is_public', String(data.is_public ?? true));
    return request('/datasets/upload', {
      method: 'POST',
      body: form,
    }) as Promise<ExperimentDataset>;
  },

  preview: (id: string, params: Record<string, any> = {}) =>
    request(`/datasets/${id}/preview${qs(params)}`) as Promise<DatasetPreviewResponse>,

  refresh: (id: string) =>
    request(`/datasets/${id}/refresh`, { method: 'POST' }) as Promise<{ message: string; dataset: ExperimentDataset }>,

  delete: (id: string, deleteFile = false) =>
    request(`/datasets/${id}?delete_file=${deleteFile}`, { method: 'DELETE' }) as Promise<{ message: string; id: string }>,
};
