import { request } from '@/utils/request';

const BASE = '/api/v2/teacher/question-bank';

export interface QBImage {
  object_name: string;
  caption: string;
  seq: number;
  url: string;
}

export interface QBIssue {
  level: 'error' | 'warning';
  message: string;
}

export interface QBQuestion {
  id: string;
  question_type: string;
  stem: string;
  options: { key: string; text: string }[];
  answer?: string | null;
  analysis?: string | null;
  score?: string | null;
  difficulty: string | null;
  chapter: string | null;
  knowledge_points: string[];
  images: QBImage[];
  status: 'published' | 'disabled';
  source_batch_id?: string | null;
  source_name?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface QBQuestionPage {
  items: QBQuestion[];
  total: number;
  limit: number;
  offset: number;
}

export interface QBParsedItem extends QBQuestion {
  seq?: number;
  issues: QBIssue[];
  has_error: boolean;
}

export interface QBBatch {
  id: string;
  file_name: string;
  status: 'parsed' | 'confirmed' | 'failed';
  total_count: number;
  valid_count: number;
  error?: string | null;
  created_by?: string;
  created_at: string | null;
  confirmed_at?: string | null;
  parser?: string;
  questions?: QBParsedItem[];
}

export interface QBKnowledgePoint {
  id: string;
  name: string;
  chapter: string | null;
  parent_id: string | null;
  sort: number;
}

export const QUESTION_TYPES = ['选择题', '填空题', '判断题', '计算题', '作图题', '简答题'] as const;
export const DIFFICULTIES = [
  { value: 'easy', label: '简单' },
  { value: 'medium', label: '中等' },
  { value: 'hard', label: '较难' },
] as const;

export function difficultyLabel(value: string | null | undefined): string {
  return DIFFICULTIES.find((item) => item.value === value)?.label ?? (value || '未标注');
}

export const QuestionBankAPI = {
  templateUrl: () => `${BASE}/template`,

  uploadBatch: async (file: File): Promise<QBBatch> => {
    const form = new FormData();
    form.append('file', file);
    return request(`${BASE}/batches`, { method: 'POST', body: form }) as Promise<QBBatch>;
  },

  batch: (id: string) =>
    request(`${BASE}/batches/${encodeURIComponent(id)}`) as Promise<QBBatch>,

  confirmBatch: (id: string) =>
    request(`${BASE}/batches/${encodeURIComponent(id)}/confirm`, { method: 'POST' }) as Promise<{
      success: boolean;
      inserted: number;
      skipped_duplicates: number;
      batch: QBBatch;
    }>,

  deleteBatch: (id: string) =>
    request(`${BASE}/batches/${encodeURIComponent(id)}`, { method: 'DELETE' }) as Promise<{ success: boolean }>,

  questions: (params: Record<string, unknown> = {}) =>
    request(`${BASE}/questions${qs(params)}`) as Promise<QBQuestionPage>,

  createQuestion: (payload: Record<string, unknown>) =>
    request(`${BASE}/questions`, { method: 'POST', body: JSON.stringify(payload) }) as Promise<QBQuestion>,

  question: (id: string) =>
    request(`${BASE}/questions/${encodeURIComponent(id)}`) as Promise<QBQuestion>,

  updateQuestion: (id: string, payload: Record<string, unknown>) =>
    request(`${BASE}/questions/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }) as Promise<QBQuestion>,

  deleteQuestion: (id: string) =>
    request(`${BASE}/questions/${encodeURIComponent(id)}`, { method: 'DELETE' }) as Promise<{ success: boolean }>,

  addImage: async (id: string, file: File, caption: string) => {
    const form = new FormData();
    form.append('file', file);
    return request(
      `${BASE}/questions/${encodeURIComponent(id)}/images${qs({ caption })}`,
      { method: 'POST', body: form },
    ) as Promise<QBImage>;
  },

  removeImage: (id: string, seq: number) =>
    request(`${BASE}/questions/${encodeURIComponent(id)}/images/${seq}`, {
      method: 'DELETE',
    }) as Promise<{ success: boolean }>,

  knowledgePoints: () =>
    request(`${BASE}/knowledge-points`) as Promise<{ items: QBKnowledgePoint[] }>,

  createKnowledgePoint: (payload: { name: string; chapter?: string | null }) =>
    request(`${BASE}/knowledge-points`, { method: 'POST', body: JSON.stringify(payload) }) as Promise<QBKnowledgePoint>,

  updateKnowledgePoint: (id: string, payload: Record<string, unknown>) =>
    request(`${BASE}/knowledge-points/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }) as Promise<QBKnowledgePoint>,

  deleteKnowledgePoint: (id: string) =>
    request(`${BASE}/knowledge-points/${encodeURIComponent(id)}`, { method: 'DELETE' }) as Promise<{ success: boolean }>,
};

function qs(params: Record<string, unknown>): string {
  const entries = Object.entries(params).filter(
    ([, value]) => value !== undefined && value !== null && value !== '',
  );
  if (entries.length === 0) return '';
  const search = new URLSearchParams(entries.map(([key, value]) => [key, String(value)])).toString();
  return `?${search}`;
}
