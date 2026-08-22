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
  <div v-if="reasoning" class="overflow-hidden rounded-2xl border border-border-subtle bg-surface/75 backdrop-blur-sm transition-all duration-300">
    <button 
      @click="toggle" 
      class="flex w-full items-center justify-between gap-4 px-4 py-3 text-left transition-colors hover:bg-surface-hover"
    >
      <div class="flex min-w-0 items-center gap-3 text-text-secondary">
        <div class="grid h-5 w-5 shrink-0 place-items-center text-accent">
          <!-- 思考中/已思考图标叠放于同一格，交叉淡入切换 -->
          <Transition name="icon-swap">
            <svg v-if="isStreaming && !hasAnswer" class="col-start-1 row-start-1 h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="9" stroke="currentColor" stroke-width="3" />
              <path class="opacity-75" d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
            </svg>
            <svg v-else class="col-start-1 row-start-1 h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6 9 17l-5-5" />
            </svg>
          </Transition>
        </div>
        <div class="min-w-0">
          <span class="block truncate text-sm font-semibold text-text-secondary">{{ statusText }}</span>
          <span class="mt-0.5 block truncate text-xs text-text-tertiary">
            {{ hasAnswer ? '正文已开始输出，思维过程已自动折叠' : '正在展开模型返回的思维过程' }}
          </span>
        </div>
      </div>
      
      <div class="shrink-0 text-text-tertiary">
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
    
    <div class="collapse-rows" :data-open="isExpanded" :inert="!isExpanded">
      <div>
        <div class="border-t border-border-subtle bg-surface-sunken px-5 py-4">
          <pre class="whitespace-pre-wrap break-words border-l-2 border-accent/30 pl-4 font-mono text-xs leading-6 text-text-secondary">{{ smoothReasoning }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.icon-swap-enter-active,
.icon-swap-leave-active {
  transition:
    opacity 200ms ease,
    transform 200ms ease;
}
.icon-swap-enter-from {
  opacity: 0;
  transform: scale(0.6);
}
.icon-swap-leave-to {
  opacity: 0;
  transform: scale(0.6);
}
</style>
