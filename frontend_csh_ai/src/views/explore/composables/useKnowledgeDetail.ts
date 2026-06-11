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
}

export function useKnowledgeDetail() {
  const content = ref('');
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

  function restoreFromCache(key: string) {
    const cached = detailCache.get(key);
    if (!cached?.completed) return false;
    content.value = cached.content;
    sources.value = [...cached.sources];
    error.value = cached.error;
    isStreaming.value = false;
    status.value = '';
    return true;
  }

  async function load(nodeTitle: string, path: string[], kbIds: string[], force = false) {
    const key = buildNodeCacheKey(nodeTitle, path, kbIds);
    activeKey = key;

    if (!force && restoreFromCache(key)) {
      stop();
      return;
    }

    stop();
    content.value = '';
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
        onEvent: (evt) => {
          if (activeKey !== key) return;
          if (evt?.kind === 'sources' && Array.isArray(evt?.metadata?.sources)) {
            sources.value = evt.metadata.sources;
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
              sources: [...sources.value],
              error: error.value,
              completed: true,
            });
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

  return { content, status, isStreaming, sources, error, load, stop };
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
