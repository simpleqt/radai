<script setup lang="ts">
import { ref, watch } from 'vue';
import { Loader2, FileText, Inbox, AlertCircle, Download } from 'lucide-vue-next';
import { useResources } from '../composables/useKnowledgeDetail';
import { downloadWithAuth } from '@/utils/request';

const props = defineProps<{ nodeTitle: string; kbIds: string[] }>();
const { resources, loading, grounded, error, load } = useResources();
const downloadingKey = ref('');

function resourceKey(resource: { doc_id?: string; doc_name?: string }, index: number) {
  return String(resource.doc_id || resource.doc_name || index);
}

function getDownloadUrl(resource: { download_url?: string; kb_id?: string | null; doc_id?: string }) {
  return resource.download_url
    || (resource.kb_id && resource.doc_id
      ? `/api/v2/explore/resources/download?kb_id=${encodeURIComponent(resource.kb_id)}&doc_id=${encodeURIComponent(resource.doc_id)}`
      : '');
}

async function handleDownload(resource: { download_url?: string; kb_id?: string | null; doc_id?: string; doc_name?: string }, index: number) {
  const url = getDownloadUrl(resource);
  if (!url || downloadingKey.value) return;
  const key = resourceKey(resource, index);
  downloadingKey.value = key;
  try {
    await downloadWithAuth(url, { filename: resource.doc_name || '知识库文档' });
  } catch (err) {
    console.error('[Explore] 资源下载失败', err);
    alert(err instanceof Error ? err.message : '资源下载失败');
  } finally {
    downloadingKey.value = '';
  }
}

watch(
  () => props.nodeTitle,
  (t) => { if (t) load(t, props.kbIds); },
  { immediate: true },
);
</script>

<template>
  <div class="h-full overflow-y-auto custom-scrollbar px-4 py-3">
    <div v-if="loading" class="flex items-center gap-2 text-sm text-text-tertiary py-8 justify-center">
      <Loader2 class="w-4 h-4 animate-spin" /> 正在检索关联资源…
    </div>

    <div v-else-if="error" class="text-sm text-rose-500 py-8 text-center">{{ error }}</div>

    <template v-else>
      <div v-if="!grounded" class="mb-3 flex items-center gap-2 text-xs text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/20 rounded-lg px-3 py-2">
        <AlertCircle class="w-4 h-4 shrink-0" />
        知识库未命中相关内容，以下为空。
      </div>

      <div v-if="resources.length === 0 && grounded" class="flex flex-col items-center gap-2 text-text-tertiary py-12">
        <Inbox class="w-8 h-8" />
        <p class="text-sm">暂无关联资源</p>
      </div>

      <div class="space-y-2.5">
        <div
          v-for="(r, i) in resources"
          :key="i"
          class="group rounded-xl border border-border-subtle p-3.5 hover:border-teal-300 dark:hover:border-teal-500/60 hover:shadow-sm transition-all"
        >
          <div class="flex items-start gap-2.5">
            <div class="w-8 h-8 rounded-lg bg-teal-50 dark:bg-teal-500/10 flex items-center justify-center shrink-0">
              <FileText class="w-4 h-4 text-teal-500" />
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex items-center justify-between gap-3">
                <p class="text-[13px] font-semibold text-text-primary truncate flex-1" :title="r.doc_name">
                  {{ r.doc_name || '知识库文档' }}
                </p>
                <button
                  v-if="r.download_url || (r.kb_id && r.doc_id)"
                  type="button"
                  class="shrink-0 inline-flex items-center gap-1 text-[11px] font-semibold text-teal-600 dark:text-teal-400 hover:text-teal-700 dark:hover:text-teal-300 bg-teal-50 dark:bg-teal-500/10 hover:bg-teal-100 dark:hover:bg-teal-500/20 px-2.5 py-1.5 rounded-lg transition-all disabled:opacity-60"
                  :disabled="downloadingKey === resourceKey(r, i)"
                  title="下载此资源文件"
                  @click="handleDownload(r, i)"
                >
                  <Loader2 v-if="downloadingKey === resourceKey(r, i)" class="w-3 h-3 animate-spin" />
                  <Download v-else class="w-3 h-3" />
                  下载
                </button>
              </div>
              <p class="text-xs text-text-secondary mt-2 line-clamp-3 leading-relaxed">{{ r.snippet }}</p>
              <div class="mt-2.5 flex items-center gap-2">
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-sunken text-text-tertiary">相关度 {{ (r.score * 100).toFixed(0) }}%</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.custom-scrollbar::-webkit-scrollbar { width: 5px; }
.custom-scrollbar::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 10px; }
.dark .custom-scrollbar::-webkit-scrollbar-thumb { background: #334155; }
</style>
