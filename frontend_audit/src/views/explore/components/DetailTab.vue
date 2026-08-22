<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue';
import { Loader2, RefreshCw, FileText, ChevronDown, Lightbulb, Send, Square } from 'lucide-vue-next';
import MarkdownRenderer from '@/views/ne-full/components/MarkdownRenderer.vue';
import { useKnowledgeDetail } from '../composables/useKnowledgeDetail';
import { ExploreAPI, type ExploreChatMessage, type ExploreResource } from '@/api/explore';

const props = defineProps<{ nodeTitle: string; path: string[]; kbIds: string[] }>();

const { content, reasoning, status, isStreaming, sources, error, load, stop } = useKnowledgeDetail();
const showSources = ref(false);
const followupInput = ref('');
const followupMessages = ref<Array<ExploreChatMessage & {
  id: string;
  pending?: boolean;
  error?: string;
  sources?: ExploreResource[];
}>>([]);
const followupStreaming = ref(false);
const followupError = ref('');
const followupAbort = ref<AbortController | null>(null);
let followupSeq = 0;

function stopFollowup() {
  if (followupAbort.value) {
    followupAbort.value.abort();
    followupAbort.value = null;
  }
  followupStreaming.value = false;
  for (let i = followupMessages.value.length - 1; i >= 0; i -= 1) {
    if (followupMessages.value[i].pending) {
      followupMessages.value[i].pending = false;
      break;
    }
  }
}

function resetFollowup() {
  stopFollowup();
  followupInput.value = '';
  followupMessages.value = [];
  followupError.value = '';
}

function reload() {
  resetFollowup();
  load(props.nodeTitle, props.path, props.kbIds, true);
}

function buildHistory(): ExploreChatMessage[] {
  return followupMessages.value
    .filter((item) => !item.pending && item.content.trim())
    .slice(-8)
    .map((item) => ({ role: item.role, content: item.content }));
}

async function sendFollowup() {
  const question = followupInput.value.trim();
  if (!question || !content.value.trim() || isStreaming.value || followupStreaming.value) return;

  const history = buildHistory();
  const userMessage = {
    id: `user-${Date.now()}-${followupSeq++}`,
    role: 'user' as const,
    content: question,
  };
  const assistantMessage = {
    id: `assistant-${Date.now()}-${followupSeq++}`,
    role: 'assistant' as const,
    content: '',
    pending: true,
    error: '',
    sources: [] as ExploreResource[],
  };

  followupInput.value = '';
  followupError.value = '';
  const assistantIndex = followupMessages.value.length + 1;
  followupMessages.value.push(userMessage, assistantMessage);
  followupStreaming.value = true;

  const controller = new AbortController();
  followupAbort.value = controller;

  try {
    await ExploreAPI.detailChat(
      {
        nodeTitle: props.nodeTitle,
        path: props.path,
        kbIds: props.kbIds,
        question,
        detailContent: content.value,
        history,
      },
      {
        onChunk: (text) => {
          const item = followupMessages.value[assistantIndex];
          if (item) item.content += text;
        },
        onEvent: (event) => {
          const eventSources = event?.metadata?.sources;
          if (event?.kind === 'sources' && Array.isArray(eventSources)) {
            const item = followupMessages.value[assistantIndex];
            if (item) item.sources = eventSources;
          }
        },
        onError: (message) => {
          const item = followupMessages.value[assistantIndex];
          const errorMessage = message || '追问回答生成失败';
          if (item) item.error = errorMessage;
          followupError.value = errorMessage;
        },
      },
      controller.signal,
    );
  } finally {
    if (followupAbort.value === controller) followupAbort.value = null;
    followupStreaming.value = false;
    const item = followupMessages.value[assistantIndex];
    if (item) item.pending = false;
  }
}

watch(
  () => [props.nodeTitle, props.path.join('>'), props.kbIds.join(',')],
  ([t]) => { if (t) reload(); },
  { immediate: true },
);

onUnmounted(() => {
  stop();
  stopFollowup();
});
</script>

<template>
  <div class="h-full flex flex-col">
    <div class="flex-1 overflow-y-auto custom-scrollbar px-1 py-2">
      <div
        v-if="reasoning || (isStreaming && !content)"
        class="mx-4 mb-3 rounded-xl border border-teal-100 dark:border-teal-500/20 bg-teal-50/50 dark:bg-teal-900/10 overflow-hidden"
      >
        <div class="flex items-center gap-2 px-3 py-2 text-xs font-semibold text-teal-700 dark:text-teal-300">
          <Lightbulb class="w-3.5 h-3.5" />
          <span>{{ content ? '思考过程' : '正在思考并检索知识库' }}</span>
          <Loader2 v-if="isStreaming && !content" class="w-3.5 h-3.5 animate-spin ml-auto" />
        </div>
        <pre class="max-h-44 overflow-y-auto custom-scrollbar whitespace-pre-wrap break-words px-3 pb-3 text-[12px] leading-5 text-text-secondary">{{ reasoning || '正在分析知识脉络、检索相关资料并组织详解结构...' }}</pre>
      </div>

      <div v-if="status && !content" class="flex items-center gap-2 text-sm text-text-tertiary px-4 py-6">
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
      <div v-if="sources.length > 0" class="mx-4 mt-6 rounded-xl border border-border-subtle overflow-hidden">
        <button
          type="button"
          class="w-full flex items-center justify-between px-4 py-2.5 bg-surface-sunken text-sm font-medium text-text-secondary"
          @click="showSources = !showSources"
        >
          <span class="inline-flex items-center gap-2">
            <FileText class="w-4 h-4 text-teal-500" />
            知识库来源（{{ sources.length }}）
          </span>
          <ChevronDown class="w-4 h-4 transition-transform" :class="showSources ? 'rotate-180' : ''" />
        </button>
        <div v-if="showSources" class="divide-y divide-border-subtle">
          <div v-for="(s, i) in sources" :key="i" class="px-4 py-3">
            <p class="text-xs font-semibold text-text-secondary truncate">{{ s.doc_name || '知识库文档' }}</p>
            <p class="text-[11px] text-text-tertiary mt-1 line-clamp-2">{{ s.snippet }}</p>
          </div>
        </div>
      </div>

      <div v-if="followupMessages.length" class="mx-4 mt-6 mb-4 rounded-xl border border-accent/30 bg-surface overflow-hidden">
        <div class="flex items-center justify-between gap-3 px-4 py-3 border-b border-border-subtle">
          <span class="text-sm font-semibold text-text-secondary">继续追问</span>
          <Loader2 v-if="followupStreaming" class="w-4 h-4 animate-spin text-accent" />
        </div>

        <div class="px-4 py-3 space-y-3">
          <div
            v-for="message in followupMessages"
            :key="message.id"
            class="flex"
            :class="message.role === 'user' ? 'justify-end' : 'justify-start'"
          >
            <div
              class="max-w-[86%] rounded-2xl px-3 py-2 text-sm leading-6"
              :class="message.role === 'user' ? 'bg-accent text-white'
                : 'bg-surface-sunken text-text-secondary'"
            >
              <p v-if="message.role === 'user'" class="whitespace-pre-wrap break-words">{{ message.content }}</p>
              <MarkdownRenderer
                v-else-if="message.content"
                :content="message.content"
                :is-streaming="Boolean(message.pending)"
              />
              <div v-else class="inline-flex items-center gap-2 text-text-tertiary">
                <Loader2 class="w-3.5 h-3.5 animate-spin" />
                生成中
              </div>
              <p v-if="message.error" class="mt-1 text-xs text-rose-500">{{ message.error }}</p>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-if="content" class="shrink-0 border-t border-accent/80 bg-surface/95 px-4 py-3">
      <div class="flex items-end gap-2">
        <textarea
          v-model="followupInput"
          rows="2"
          class="min-h-[42px] flex-1 resize-none rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm text-text-secondary outline-none focus:border-accent"
          placeholder="继续追问这个知识点..."
          :disabled="isStreaming || followupStreaming"
          @keydown.enter.exact.prevent="sendFollowup"
        />
        <button
          v-if="followupStreaming"
          type="button"
          class="h-[42px] w-[42px] inline-flex items-center justify-center rounded-lg border border-border-subtle text-text-secondary hover:text-accent transition-colors"
          title="停止"
          @click="stopFollowup"
        >
          <Square class="w-4 h-4" />
        </button>
        <button
          v-else
          type="button"
          class="h-[42px] w-[42px] inline-flex items-center justify-center rounded-lg bg-accent text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50 transition-colors"
          title="发送"
          :disabled="!followupInput.trim() || isStreaming || !content.trim()"
          @click="sendFollowup"
        >
          <Send class="w-4 h-4" />
        </button>
      </div>
      <p v-if="followupError" class="mt-2 text-xs text-rose-500">{{ followupError }}</p>
    </div>

    <div class="shrink-0 px-4 py-2 border-t border-border-subtle flex justify-end">
      <button
        type="button"
        class="inline-flex items-center gap-1.5 text-xs text-text-secondary hover:text-teal-600 dark:hover:text-teal-400 px-3 py-1.5 rounded-lg hover:bg-surface-hover transition-colors"
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
