/** 探索式学习：按知识点缓存已生成内容，避免切换节点后重复请求。 */

export function buildNodeCacheKey(
  nodeTitle: string,
  kbIds: string[],
  path?: string[],
): string {
  const title = String(nodeTitle || '').trim();
  const pathPart = (path || []).map((p) => String(p || '').trim()).join('\0');
  const kbPart = (kbIds || []).map((id) => String(id || '').trim()).filter(Boolean).join(',');
  return `${title}\0${pathPart}\0${kbPart}`;
}

export function buildQuizCacheKey(
  nodeTitle: string,
  kbIds: string[],
  typeFilter: string,
  difficulty: string,
): string {
  return `${buildNodeCacheKey(nodeTitle, kbIds)}\0${typeFilter}\0${difficulty}`;
}

const MAX_CACHE_ENTRIES = 40;

function trimCache<K, V>(cache: Map<K, V>, max = MAX_CACHE_ENTRIES) {
  while (cache.size > max) {
    const first = cache.keys().next().value;
    if (first === undefined) break;
    cache.delete(first);
  }
}

export interface DetailCacheEntry {
  content: string;
  reasoning?: string;
  sources: Array<{
    doc_id?: string;
    doc_name?: string;
    snippet?: string;
    score?: number;
  }>;
  error: string;
  completed: boolean;
}

export interface ResourcesCacheEntry {
  resources: import('@/api/explore').ExploreResource[];
  grounded: boolean;
  error: string;
}

export interface QuizCacheEntry {
  questions: import('@/api/explore').QuizQuestion[];
  userAnswers: Record<string, string>;
  grades: Record<string, import('@/api/explore').GradeResult>;
  page: number;
  error: string;
}

export const detailCache = new Map<string, DetailCacheEntry>();
export const resourcesCache = new Map<string, ResourcesCacheEntry>();
export const quizCache = new Map<string, QuizCacheEntry>();

export function setDetailCache(key: string, entry: DetailCacheEntry) {
  detailCache.set(key, entry);
  trimCache(detailCache);
}

export function setResourcesCache(key: string, entry: ResourcesCacheEntry) {
  resourcesCache.set(key, entry);
  trimCache(resourcesCache);
}

export function setQuizCache(key: string, entry: QuizCacheEntry) {
  quizCache.set(key, entry);
  trimCache(quizCache);
}

export function clearExploreNodeCaches() {
  detailCache.clear();
  resourcesCache.clear();
  quizCache.clear();
}
