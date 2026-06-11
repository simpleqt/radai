<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { ChatMessage, ResearchPlanPayload } from '@/api/agent-v2';

interface Props {
  message: ChatMessage;
  disabled?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  disabled: false,
});

const emit = defineEmits<{
  (e: 'submit', payload: {
    question: string;
    draftId: string;
    confirmationStatus: 'confirmed' | 'revised' | 'cancelled';
    researchPlan: ResearchPlanPayload;
  }): void;
}>();

const draftEvent = computed(() => {
  const events = props.message.processEvents || [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.kind === 'research_draft_created' || event.kind === 'research_draft_updated') {
      return event;
    }
  }
  return null;
});

const streamingEvent = computed(() => {
  const events = props.message.processEvents || [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.kind === 'research_draft_streaming') {
      return event;
    }
  }
  return null;
});

const isStreaming = computed(() => {
  return streamingEvent.value !== null && draftEvent.value === null;
});

const draftMetadata = computed(() => {
  return (draftEvent.value?.metadata || {}) as Record<string, unknown>;
});

const isPending = computed(() => {
  const status = String(draftMetadata.value.confirmation_status || 'pending').trim();
  return status === 'pending';
});

const draftId = computed(() => {
  const value = String(draftMetadata.value.draft_id || '').trim();
  return value || `draft_${Date.now()}`;
});

const sourceQuestion = computed(() => {
  const value = String(draftMetadata.value.source_question || '').trim();
  return value || '';
});

const initialPlan = computed<ResearchPlanPayload>(() => {
  const raw = draftMetadata.value.plan_content;
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    return raw as ResearchPlanPayload;
  }
  return {};
});

const planText = ref('');

const stripThinkTags = (input: string) => {
  return String(input || '')
    .replace(/<(think|thought)>[\s\S]*?<\/\1>/gi, '')
    .replace(/<\/?(think|thought)>/gi, '')
    .trim();
};

watch(
  () => initialPlan.value,
  (nextPlan) => {
    const outlineText = String((nextPlan as any)?.outline_text || '').trim();
    const rawText = String((nextPlan as any)?.raw_text || '').trim();
    planText.value = stripThinkTags(outlineText || rawText || '');
  },
  { immediate: true, deep: true },
);

// 监听流式草案内容
watch(
  () => streamingEvent.value,
  (event) => {
    if (event?.metadata) {
      const content = String((event.metadata as any)?.draft_content || '');
      if (content) {
        planText.value = stripThinkTags(content);
      }
    }
  },
  { deep: true },
);

const parsePlan = (): ResearchPlanPayload => {
  const raw = stripThinkTags(planText.value);
  if (!raw) return { outline_text: '' } as ResearchPlanPayload;
  return {
    outline_text: raw,
  } as ResearchPlanPayload;
};

const submitConfirmed = () => {
  emit('submit', {
    question: sourceQuestion.value,
    draftId: draftId.value,
    confirmationStatus: 'confirmed',
    researchPlan: parsePlan(),
  });
};

const cancelResearch = () => {
  emit('submit', {
    question: sourceQuestion.value,
    draftId: draftId.value,
    confirmationStatus: 'cancelled',
    researchPlan: parsePlan(),
  });
};
</script>

<template>
  <section
    v-if="(draftEvent && isPending) || isStreaming"
    class="rounded-2xl border border-cyan-200 bg-cyan-50/70 p-4 text-slate-800 dark:border-cyan-800 dark:bg-cyan-950/20 dark:text-slate-100"
  >
    <div class="mb-3 flex items-center justify-between gap-2">
      <h3 class="text-sm font-semibold">
        {{ isStreaming ? '正在生成研究方案...' : '研究清单草案（待确认）' }}
      </h3>
      <span
        v-if="!isStreaming"
        class="rounded-full border border-cyan-300 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-cyan-700 dark:border-cyan-700 dark:text-cyan-300"
      >
        pending
      </span>
      <span
        v-else
        class="flex items-center gap-1.5 rounded-full border border-cyan-300 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-cyan-700 dark:border-cyan-700 dark:text-cyan-300"
      >
        <span class="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-600"></span>
        streaming
      </span>
    </div>

    <p class="mb-3 text-xs leading-5 text-slate-600 dark:text-slate-300">
      {{ isStreaming ? 'AI 正在为你生成研究方案，请稍候...' : '你可以直接编辑下方研究分点后提交，或不修改直接确认开始研究。' }}
    </p>

    <div
      v-if="isStreaming && !planText"
      class="mb-3 min-h-[180px] w-full rounded-xl border border-cyan-200 bg-white/90 px-3 py-2 text-sm leading-6 text-slate-400 dark:border-cyan-800 dark:bg-slate-900/70 dark:text-slate-500"
    >
      <div class="flex items-center gap-2">
        <span class="inline-block h-2 w-2 animate-ping rounded-full bg-cyan-500"></span>
        <span>正在生成中...</span>
      </div>
    </div>

    <textarea
      v-model="planText"
      :disabled="disabled || isStreaming"
      class="mb-3 min-h-[180px] w-full rounded-xl border border-cyan-200 bg-white/90 px-3 py-2 text-sm leading-6 text-slate-700 outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100 disabled:opacity-60 dark:border-cyan-800 dark:bg-slate-900/70 dark:text-slate-200 dark:focus:ring-cyan-900/40"
    ></textarea>

    <div v-if="!isStreaming" class="flex flex-wrap items-center gap-2">
      <button
        type="button"
        :disabled="disabled || !sourceQuestion"
        class="rounded-lg bg-cyan-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-cyan-700 disabled:opacity-50"
        @click="submitConfirmed"
      >
        确认开始研究
      </button>
      <button
        type="button"
        :disabled="disabled || !sourceQuestion"
        class="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
        @click="cancelResearch"
      >
        取消本次研究
      </button>
    </div>
  </section>
</template>
