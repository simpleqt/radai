import { request } from '@/utils/request';
import type { QBImage } from '@/api/question-bank';

const BASE = '/api/v2/practice';

export interface PracticeQuestion {
  id: string;
  question_type: string;
  stem: string;
  options: { key: string; text: string }[];
  difficulty: string | null;
  chapter: string | null;
  knowledge_points: string[];
  images: QBImage[];
  status?: string;
  source_name?: string | null;
  answer?: string | null;
  analysis?: string | null;
  last_wrong_at?: string;
}

export interface PracticePage {
  items: PracticeQuestion[];
  total: number;
  limit: number;
  offset: number;
}

export interface VisualGrading {
  verdict: 'correct' | 'partial' | 'incorrect';
  score: number | null;
  comment: string;
  explanation: string;
  image_count: number;
}

export interface CheckResult {
  question_id: string;
  question_type: string;
  is_self_review: boolean;
  is_correct: boolean | null;
  answer: string | null;
  analysis: string | null;
  checked_at: string;
  visual_grading?: VisualGrading | null;
}

export interface MyAttempt {
  id: string;
  question_id: string;
  answer: string;
  is_correct: boolean | null;
  mode: string;
  created_at: string | null;
}

export const PracticeAPI = {
  questions: (params: Record<string, unknown> = {}) =>
    request(`${BASE}/questions${qs(params)}`) as Promise<PracticePage>,

  question: (id: string, withSolution = false) =>
    request(
      `${BASE}/questions/${encodeURIComponent(id)}${qs({ with_solution: withSolution ? 1 : '' })}`,
    ) as Promise<PracticeQuestion>,

  check: (id: string, answer: string, selfReview?: boolean) =>
    request(`${BASE}/questions/${encodeURIComponent(id)}/check`, {
      method: 'POST',
      body: JSON.stringify({ answer, self_review: selfReview ?? null }),
    }) as Promise<CheckResult>,

  checkVisual: async (id: string, answer: string, images: File[]) => {
    const form = new FormData();
    form.append('answer', answer);
    images.forEach((file) => form.append('images', file, file.name));
    return request(`${BASE}/questions/${encodeURIComponent(id)}/check-visual`, {
      method: 'POST',
      body: form,
    }) as Promise<CheckResult>;
  },

  attempts: (params: Record<string, unknown> = {}) =>
    request(`${BASE}/attempts${qs(params)}`) as Promise<{
      items: MyAttempt[];
      total: number;
      limit: number;
      offset: number;
    }>,

  wrongQuestions: (limit = 50) =>
    request(`${BASE}/attempts/wrong${qs({ limit })}`) as Promise<{
      items: PracticeQuestion[];
      total: number;
      limit: number;
    }>,
};

function qs(params: Record<string, unknown>): string {
  const entries = Object.entries(params).filter(
    ([, value]) => value !== undefined && value !== null && value !== '',
  );
  if (entries.length === 0) return '';
  const search = new URLSearchParams(entries.map(([key, value]) => [key, String(value)])).toString();
  return `?${search}`;
}
