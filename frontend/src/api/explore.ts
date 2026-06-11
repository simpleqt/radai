import { authState, logout } from '@/utils/request';
import { streamSSE, type SSEHandlers } from '@/utils/sseClient';

const API_V2_BASE = '/api/v2';
// 领域标识：决定学科、预制题库与详解所用智能体。RadAI=ne，CSH-AI=csh。
const EXPLORE_DOMAIN = 'ne';

/** 与 utils/request.ts 同构的轻量封装，但 base 指向 /api/v2（探索端点）。 */
async function requestV2(path: string, options: RequestInit = {}) {
  const url = path.startsWith('http') ? path : `${API_V2_BASE}${path}`;
  const headers = new Headers(options.headers || {});
  if (authState.value.token) {
    headers.set('Authorization', `Bearer ${authState.value.token}`);
  }
  if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await fetch(url, { ...options, headers });
  if (response.status === 401 || response.status === 403) {
    logout();
    throw new Error('认证失败，请重新登录');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || data.detail || `HTTP Error ${response.status}`);
  }
  return data;
}

// ── 类型 ──────────────────────────────────────────────

export interface ExploreTopic {
  id: string;
  title: string;
  hint?: string;
}

export interface ExploreNode {
  id: string;
  title: string;
  summary?: string;
  expandable?: boolean;
}

export interface ExploreMap {
  root: { id: string; title: string; summary?: string };
  children: ExploreNode[];
  kb_id?: string | null;
  source?: string;
}

export interface ExploreResource {
  doc_id: string;
  doc_name: string;
  file_path?: string;
  snippet: string;
  score: number;
  kb_id?: string | null;
  download_url?: string;
}

export type QuizType = 'choice' | 'multi' | 'judge' | 'short' | 'calc';
export type QuizDifficulty = 'easy' | 'medium' | 'hard';

export interface QuizQuestion {
  id: string;
  type: QuizType;
  difficulty: QuizDifficulty;
  stem: string;
  options?: { key: string; text: string }[];
  answer?: string;
  reference_answer?: string;
  scoring_points?: string[];
  explanation?: string;
  source_refs?: string[];
}

export interface GradeResult {
  score: number;
  verdict: string;
  feedback: string;
  missing_points: string[];
}

// ── API ──────────────────────────────────────────────

export const ExploreAPI = {
  topics(kbIds: string[], count = 20, seed = ''): Promise<{ topics: ExploreTopic[]; source: string }> {
    return requestV2('/explore/topics', {
      method: 'POST',
      body: JSON.stringify({ kb_ids: kbIds, count, seed, domain: EXPLORE_DOMAIN }),
    });
  },

  map(topic: string, kbIds: string[], maxChildren = 6): Promise<ExploreMap> {
    return requestV2('/explore/map', {
      method: 'POST',
      body: JSON.stringify({ topic, kb_ids: kbIds, max_children: maxChildren, domain: EXPLORE_DOMAIN }),
    });
  },

  expand(nodeTitle: string, path: string[], kbIds: string[], maxChildren = 5): Promise<{ children: ExploreNode[]; source: string }> {
    return requestV2('/explore/expand', {
      method: 'POST',
      body: JSON.stringify({ node_title: nodeTitle, path, kb_ids: kbIds, max_children: maxChildren, domain: EXPLORE_DOMAIN }),
    });
  },

  resources(nodeTitle: string, kbIds: string[], topK = 8): Promise<{ resources: ExploreResource[]; grounded: boolean }> {
    return requestV2('/explore/resources', {
      method: 'POST',
      body: JSON.stringify({ node_title: nodeTitle, kb_ids: kbIds, top_k: topK, domain: EXPLORE_DOMAIN }),
    });
  },

  quiz(payload: {
    nodeTitle: string;
    kbIds: string[];
    types: QuizType[];
    difficulty: 'all' | QuizDifficulty;
    count: number;
  }): Promise<{ questions: QuizQuestion[]; source: string }> {
    return requestV2('/explore/quiz', {
      method: 'POST',
      body: JSON.stringify({
        node_title: payload.nodeTitle,
        kb_ids: payload.kbIds,
        types: payload.types,
        difficulty: payload.difficulty,
        count: payload.count,
        domain: EXPLORE_DOMAIN,
      }),
    });
  },

  grade(question: QuizQuestion, userAnswer: string): Promise<GradeResult> {
    return requestV2('/explore/quiz/grade', {
      method: 'POST',
      body: JSON.stringify({ question, user_answer: userAnswer, domain: EXPLORE_DOMAIN }),
    });
  },

  /** 知识点详解（SSE 流式）。 */
  detail(
    payload: { nodeTitle: string; path: string[]; kbIds: string[] },
    handlers: SSEHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    return streamSSE(
      `${API_V2_BASE}/explore/detail`,
      { node_title: payload.nodeTitle, path: payload.path, kb_ids: payload.kbIds, domain: EXPLORE_DOMAIN },
      handlers,
      signal,
    );
  },
};
