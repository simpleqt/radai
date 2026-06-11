<script setup lang="ts">
import { computed, ref, toRefs, watch } from 'vue';
import { useSmoothStreamText } from '@/composables/useSmoothStreamText';

interface Props {
  reasoning: string;
  isStreaming?: boolean;
  hasAnswer?: boolean;
}

const props = defineProps<Props>();
const { reasoning, isStreaming, hasAnswer } = toRefs(props);

/** 思考阶段平滑展示；正文开始后不再动画，直接显示完整 reasoning */
const { displayText: smoothReasoning } = useSmoothStreamText(
  () => reasoning.value || '',
  () => Boolean(isStreaming.value && !hasAnswer.value),
  () => true,
);
const isExpanded = ref(Boolean(props.isStreaming && !props.hasAnswer));

const statusText = computed(() => {
  if (props.isStreaming && !props.hasAnswer) return '思考中';
  return '已思考';
});

watch(
  () => props.hasAnswer,
  (hasAnswer) => {
    if (hasAnswer) {
      isExpanded.value = false;
    }
  }
);

watch(
  () => props.reasoning,
  () => {
    if (props.isStreaming && !props.hasAnswer) {
      isExpanded.value = true;
    }
  }
);

const toggle = () => {
  isExpanded.value = !isExpanded.value;
};
</script>

<template>
  <div v-if="reasoning" class="overflow-hidden rounded-2xl border border-slate-200/70 bg-white/75 shadow-[0_10px_34px_-26px_rgba(15,23,42,0.45)] backdrop-blur-sm transition-all duration-300 dark:border-slate-800/70 dark:bg-slate-900/45">
    <button 
      @click="toggle" 
      class="flex w-full items-center justify-between gap-4 px-4 py-3 text-left transition-colors hover:bg-slate-50/80 dark:hover:bg-slate-800/50"
    >
      <div class="flex min-w-0 items-center gap-3 text-slate-500 dark:text-slate-400">
        <div class="flex h-5 w-5 shrink-0 items-center justify-center text-indigo-500 dark:text-indigo-300">
          <svg v-if="isStreaming && !hasAnswer" class="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle class="opacity-25" cx="12" cy="12" r="9" stroke="currentColor" stroke-width="3" />
            <path class="opacity-75" d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
          </svg>
          <svg v-else class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 6 9 17l-5-5" />
          </svg>
        </div>
        <div class="min-w-0">
          <span class="block truncate text-sm font-semibold text-slate-600 dark:text-slate-300">{{ statusText }}</span>
          <span class="mt-0.5 block truncate text-xs text-slate-400 dark:text-slate-500">
            {{ hasAnswer ? '正文已开始输出，思维过程已自动折叠' : '正在展开模型返回的思维过程' }}
          </span>
        </div>
      </div>
      
      <div class="shrink-0 text-slate-400 dark:text-slate-500">
        <svg 
          xmlns="http://www.w3.org/2000/svg" 
          width="16" 
          height="16" 
          viewBox="0 0 24 24" 
          fill="none" 
          stroke="currentColor" 
          stroke-width="2.5" 
          stroke-linecap="round" 
          stroke-linejoin="round"
          :class="['transition-transform duration-500', isExpanded ? 'rotate-180' : '']"
        >
          <path d="m6 9 6 6 6-6"/>
        </svg>
      </div>
    </button>
    
    <div 
      v-show="isExpanded" 
      class="border-t border-slate-100 bg-slate-50/55 px-5 py-4 dark:border-slate-800/60 dark:bg-slate-950/30"
    >
      <pre class="whitespace-pre-wrap break-words border-l-2 border-indigo-200 pl-4 font-mono text-xs leading-6 text-slate-500 dark:border-indigo-900/70 dark:text-slate-400">{{ smoothReasoning }}</pre>
    </div>
  </div>
</template>
