<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue';
import { Loader2, RefreshCw, FileText, ChevronDown } from 'lucide-vue-next';
import MarkdownRenderer from '@/views/ne-full/components/MarkdownRenderer.vue';
import { useKnowledgeDetail } from '../composables/useKnowledgeDetail';

const props = defineProps<{ nodeTitle: string; path: string[]; kbIds: string[] }>();

const { content, status, isStreaming, sources, error, load, stop } = useKnowledgeDetail();
const showSources = ref(false);

function reload() {
  load(props.nodeTitle, props.path, props.kbIds, true);
}

watch(
  () => props.nodeTitle,
  (t) => { if (t) reload(); },
  { immediate: true },
);

onUnmounted(stop);
</script>

<template>
  <div class="h-full flex flex-col">
    <div class="flex-1 overflow-y-auto custom-scrollbar px-1 py-2">
      <div v-if="status && !content" class="flex items-center gap-2 text-sm text-slate-400 px-4 py-6">
        <Loader2 class="w-4 h-4 animate-spin" />
        {{ status }}
      </div>

      <div v-if="error && !content" class="text-sm text-rose-500 px-4 py-6">{{ error }}</div>

      <MarkdownRenderer
        v-if="content"
        :content="content"
        :is-streaming="isStreaming"
      />

      <span v-if="isStreaming && content" class="inline-block w-2 h-4 ml-4 bg-teal-500 animate-pulse align-middle" />

      <!-- 来源引用 -->
      <div v-if="sources.length > 0" class="mx-4 mt-6 rounded-xl border border-slate-200 dark:border-slate-700 overflow-hidden">
        <button
          type="button"
          class="w-full flex items-center justify-between px-4 py-2.5 bg-slate-50 dark:bg-slate-800/50 text-sm font-medium text-slate-600 dark:text-slate-300"
          @click="showSources = !showSources"
        >
          <span class="inline-flex items-center gap-2">
            <FileText class="w-4 h-4 text-teal-500" />
            知识库来源（{{ sources.length }}）
          </span>
          <ChevronDown class="w-4 h-4 transition-transform" :class="showSources ? 'rotate-180' : ''" />
        </button>
        <div v-if="showSources" class="divide-y divide-slate-100 dark:divide-slate-800">
          <div v-for="(s, i) in sources" :key="i" class="px-4 py-3">
            <p class="text-xs font-semibold text-slate-700 dark:text-slate-200 truncate">{{ s.doc_name || '知识库文档' }}</p>
            <p class="text-[11px] text-slate-400 mt-1 line-clamp-2">{{ s.snippet }}</p>
          </div>
        </div>
      </div>
    </div>

    <div class="shrink-0 px-4 py-2 border-t border-slate-100 dark:border-slate-800 flex justify-end">
      <button
        type="button"
        class="inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-teal-600 dark:hover:text-teal-400 px-3 py-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
        :disabled="isStreaming"
        @click="reload"
      >
        <RefreshCw class="w-3.5 h-3.5" :class="isStreaming ? 'animate-spin' : ''" />
        重新生成
      </button>
    </div>
  </div>
</template>

<style scoped>
.custom-scrollbar::-webkit-scrollbar { width: 5px; }
.custom-scrollbar::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 10px; }
.dark .custom-scrollbar::-webkit-scrollbar-thumb { background: #334155; }
</style>
