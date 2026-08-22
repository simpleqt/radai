import { request } from '@/utils/request';

const BASE = '/api/v2/teacher/analytics';

export interface QuestionRecord {
  id: string;
  user_id: string;
  username: string;
  agent_code: string;
  thread_id: string;
  question: string;
  question_len: number;
  workflow: string | null;
  model_name: string | null;
  is_guest: boolean;
  created_at: string | null;
}

export interface QuestionPage {
  items: QuestionRecord[];
  total: number;
  limit: number;
  offset: number;
}

export interface Overview {
  agent_code: string;
  total: number;
  today: number;
  last_7_days: number;
  last_30_days: number;
  active_students_30_days: number;
}

export interface TrendPoint {
  date: string;
  count: number;
}

export interface NamedCount {
  question?: string;
  keyword?: string;
  label?: string;
  count: number;
}

export interface Cluster {
  label: string;
  count: number;
  ids: string[];
  keywords: string[];
  /** 簇内代表问题原文（生成时截断到 200 字），旧报告可能没有该字段 */
  samples?: string[];
}

export interface ReportResult {
  trend: TrendPoint[];
  top_questions: NamedCount[];
  top_keywords: NamedCount[];
  unique_questions: number;
  clusters: Cluster[];
  summary: string | null;
  llm_status: 'ok' | 'degraded';
  /** 语义聚类是否基于抽样数据（趋势/高频统计仍是全量） */
  sampled?: boolean;
  sampled_total?: number;
  practice_summary?: PracticeStats;
  cross_insights?: CrossInsight[];
  recommended_exercises?: RecommendedExercise[];
}

export interface ReportItem {
  id: string;
  agent_code: string;
  created_by: string;
  range_start: string | null;
  range_end: string | null;
  question_count: number;
  status: 'running' | 'done' | 'failed';
  model_name: string | null;
  error: string | null;
  created_at: string | null;
  finished_at: string | null;
}

export interface ConversationAnswer {
  content: string;
  /** answer=最终回答；process=多轮工具调用的过程播报；error=兜底报错文案 */
  kind: 'answer' | 'process' | 'error';
}

export interface ConversationTurn {
  /** 学生提问原文；对话防御性开头可能为空串 */
  question: string;
  /** 连续重复发送同一问题的次数（失败重试） */
  repeat_count: number;
  has_answer: boolean;
  answers: ConversationAnswer[];
}

export interface ConversationDetail {
  thread_id: string;
  user_id: string;
  username: string;
  title: string | null;
  turns: ConversationTurn[];
}

export interface PracticeKPStat {
  knowledge_point: string;
  attempts: number;
  wrong: number;
  error_rate: number;
}

export interface PracticeWrongQuestion {
  question_id: string;
  stem: string;
  knowledge_points: string[];
  difficulty: string | null;
  question_type: string;
  wrong_count: number;
}

export interface PracticeStats {
  attempt_count: number;
  graded_count: number;
  correct_rate: number | null;
  browse_count?: number;
  active_students: number;
  trend: { date: string; attempts: number; correct: number }[];
  by_knowledge_point: PracticeKPStat[];
  by_difficulty: { difficulty: string; attempts: number; wrong: number; error_rate: number | null }[];
  top_wrong_questions: PracticeWrongQuestion[];
  empty: boolean;
}

export interface CrossInsight {
  knowledge_point: string;
  category: 'asked_and_wrong' | 'wrong_only' | 'asked_only';
  attempts: number;
  error_rate: number;
  question_mentions: number;
}

export interface RecommendedExercise {
  knowledge_point: string;
  questions: {
    id: string;
    question_type: string;
    difficulty: string | null;
    stem: string;
  }[];
}

export interface Insights {
  days: number;
  overview: Overview;
  trend: TrendPoint[];
  top_questions: NamedCount[];
  top_keywords: NamedCount[];
  unique_questions: number;
  question_count: number;
  practice?: PracticeStats;
}

export const TeacherAnalyticsAPI = {
  questions: (params: Record<string, unknown> = {}) =>
    request(`${BASE}/questions${qs(params)}`) as Promise<QuestionPage>,

  overview: () => request(`${BASE}/overview`) as Promise<Overview>,

  insights: (days = 30) =>
    request(`${BASE}/insights${qs({ days })}`) as Promise<Insights>,

  conversation: (threadId: string, userId: string) =>
    request(
      `${BASE}/conversations/${encodeURIComponent(threadId)}${qs({ user_id: userId })}`,
    ) as Promise<ConversationDetail>,

  createReport: (payload: { from: string; to: string }) =>
    request(`${BASE}/reports`, {
      method: 'POST',
      body: JSON.stringify({ agent_code: 'fmca', ...payload }),
    }) as Promise<ReportItem>,

  reports: () =>
    request(`${BASE}/reports`) as Promise<{ items: ReportItem[] }>,

  report: (id: string) =>
    request(`${BASE}/reports/${encodeURIComponent(id)}`) as Promise<ReportItem & { result: ReportResult | null }>,

  deleteReport: (id: string) =>
    request(`${BASE}/reports/${encodeURIComponent(id)}`, { method: 'DELETE' }) as Promise<{ success: boolean }>,
};

function qs(params: Record<string, unknown>): string {
  const entries = Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '');
  if (entries.length === 0) return '';
  const search = new URLSearchParams(
    entries.map(([key, value]) => [key, String(value)]),
  ).toString();
  return `?${search}`;
}
