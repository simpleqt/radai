import { ref, shallowRef } from 'vue';
import { ExploreAPI, type ExploreResource } from '@/api/explore';
import {
  buildNodeCacheKey,
  detailCache,
  resourcesCache,
  setDetailCache,
  setResourcesCache,
} from './exploreNodeCache';

export interface DetailSource {
  doc_id?: string;
  doc_name?: string;
  snippet?: string;
  score?: number;
  kb_id?: string | null;
  download_url?: string;
}

function firstKbId(kbIds: string[]) {
  return (kbIds || []).map((id) => String(id || '').trim()).find(Boolean) || null;
}

function buildResourceDownloadUrl(kbId: string | null, docId: string) {
  if (!kbId || !docId) return '';
  return `/api/v2/explore/resources/download?kb_id=${encodeURIComponent(kbId)}&doc_id=${encodeURIComponent(docId)}`;
}

function detailSourcesToResources(sourceList: DetailSource[], kbIds: string[]): ExploreResource[] {
  const kbId = firstKbId(kbIds);
  const seen = new Set<string>();
  const resources: ExploreResource[] = [];
  (sourceList || []).forEach((source, index) => {
    const docId = String(source.doc_id || '').trim();
    const docName = String(source.doc_name || '').trim() || '知识库文档';
    const key = docId || docName || String(index);
    if (seen.has(key)) return;
    seen.add(key);
    resources.push({
      doc_id: docId,
      doc_name: docName,
      snippet: String(source.snippet || '').trim() || '该资源与当前知识点相关，可作为补充阅读材料。',
      score: typeof source.score === 'number' ? source.score : 0,
      kb_id: source.kb_id ?? kbId,
      download_url: source.download_url || buildResourceDownloadUrl(source.kb_id ?? kbId, docId),
    });
  });
  return resources;
}

function seedResourcesCacheFromDetailSources(resourceKey: string, sourceList: DetailSource[], kbIds: string[]) {
  const resourceList = detailSourcesToResources(sourceList, kbIds);
  if (!resourceList.length) return;
  setResourcesCache(resourceKey, {
    resources: resourceList,
    grounded: true,
    error: '',
  });
}

function findDetailSourcesForResource(nodeTitle: string, kbIds: string[]) {
  const title = String(nodeTitle || '').trim();
  const kbPart = (kbIds || []).map((id) => String(id || '').trim()).filter(Boolean).join(',');
  const prefix = `${title}\0`;
  const suffix = `\0${kbPart}`;
  const entries = Array.from(detailCache.entries()).reverse();
  return entries.find(([key, entry]) => (
    key.startsWith(prefix)
    && key.endsWith(suffix)
    && entry.completed
    && entry.sources?.length
  ))?.[1]?.sources || [];
}

export function useKnowledgeDetail() {
  const content = ref('');
  const reasoning = ref('');
  const status = ref('');
  const isStreaming = ref(false);
  const sources = ref<DetailSource[]>([]);
  const error = ref('');
  const abortController = shallowRef<AbortController | null>(null);
  let activeKey = '';

  function stop() {
    if (abortController.value) {
      abortController.value.abort();
      abortController.value = null;
    }
    isStreaming.value = false;
  }

  function restoreFromCache(key: string, resourceKey: string, kbIds: string[]) {
    const cached = detailCache.get(key);
    if (!cached?.completed) return false;
    content.value = cached.content;
    reasoning.value = cached.reasoning || '';
    sources.value = [...cached.sources];
    seedResourcesCacheFromDetailSources(resourceKey, sources.value, kbIds);
    error.value = cached.error;
    isStreaming.value = false;
    status.value = '';
    return true;
  }

  async function load(nodeTitle: string, path: string[], kbIds: string[], force = false) {
    const key = buildNodeCacheKey(nodeTitle, kbIds, path);
    const resourceKey = buildNodeCacheKey(nodeTitle, kbIds);
    activeKey = key;

    if (!force && restoreFromCache(key, resourceKey, kbIds)) {
      stop();
      return;
    }

    stop();
    content.value = '';
    reasoning.value = '';
    sources.value = [];
    error.value = '';
    status.value = '';
    isStreaming.value = true;
    const controller = new AbortController();
    abortController.value = controller;

    await ExploreAPI.detail(
      { nodeTitle, path, kbIds },
      {
        onStatus: (s) => {
          if (activeKey === key) status.value = s;
        },
        onChunk: (t) => {
          if (activeKey === key) content.value += t;
        },
        onThinking: (t) => {
          if (activeKey === key) reasoning.value += t;
        },
        onEvent: (evt) => {
          if (activeKey !== key) return;
          if (evt?.kind === 'sources' && Array.isArray(evt?.metadata?.sources)) {
            sources.value = evt.metadata.sources;
            seedResourcesCacheFromDetailSources(resourceKey, sources.value, kbIds);
          }
        },
        onError: (msg) => {
          if (activeKey === key) error.value = msg;
        },
        onDone: () => {
          if (activeKey !== key) return;
          isStreaming.value = false;
          status.value = '';
          if (content.value.trim()) {
            setDetailCache(key, {
              content: content.value,
              reasoning: reasoning.value,
              sources: [...sources.value],
              error: error.value,
              completed: true,
            });
            seedResourcesCacheFromDetailSources(resourceKey, sources.value, kbIds);
          }
        },
      },
      controller.signal,
    );

    if (activeKey === key) {
      isStreaming.value = false;
      abortController.value = null;
    }
  }

  return { content, reasoning, status, isStreaming, sources, error, load, stop };
}

export function useResources() {
  const resources = ref<ExploreResource[]>([]);
  const loading = ref(false);
  const grounded = ref(true);
  const error = ref('');

  async function load(nodeTitle: string, kbIds: string[], force = false) {
    const key = buildNodeCacheKey(nodeTitle, kbIds);

    if (!force) {
      const cached = resourcesCache.get(key);
      if (cached) {
        resources.value = [...cached.resources];
        grounded.value = cached.grounded;
        error.value = cached.error;
        loading.value = false;
        return;
      }
      const detailSources = findDetailSourcesForResource(nodeTitle, kbIds);
      if (detailSources.length) {
        const fromDetail = detailSourcesToResources(detailSources, kbIds);
        resources.value = fromDetail;
        grounded.value = true;
        error.value = '';
        loading.value = false;
        setResourcesCache(key, {
          resources: [...fromDetail],
          grounded: true,
          error: '',
        });
        return;
      }
    }

    loading.value = true;
    error.value = '';
    try {
      const data = await ExploreAPI.resources(nodeTitle, kbIds);
      resources.value = data.resources || [];
      grounded.value = data.grounded;
      setResourcesCache(key, {
        resources: [...resources.value],
        grounded: grounded.value,
        error: '',
      });
    } catch (e: any) {
      error.value = e?.message || '加载关联资源失败';
      resources.value = [];
    } finally {
      loading.value = false;
    }
  }

  return { resources, loading, grounded, error, load };
}
