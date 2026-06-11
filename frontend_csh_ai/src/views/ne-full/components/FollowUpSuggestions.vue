<script setup lang="ts">
import { renderTextWithInlineMath } from '@/utils/renderInlineMath';
import type { FollowUpSuggestion } from '@/api/agent-v2';

interface Props {
  suggestions: FollowUpSuggestion[];
  loading?: boolean;
  disabled?: boolean;
}

withDefaults(defineProps<Props>(), {
  loading: false,
  disabled: false,
});

const emit = defineEmits<{
  (e: 'select', text: string): void;
}>();

const handleSelect = (text: string) => {
  emit('select', text);
};

const renderSuggestionText = (text: string) => renderTextWithInlineMath(text);
</script>

<template>
  <div class="w-full max-w-3xl pt-1">
    <div
      v-if="loading"
      class="flex flex-col gap-2"
      aria-busy="true"
      aria-label="正在生成追问建议"
    >
      <div
        v-for="idx in 3"
        :key="idx"
        class="h-10 w-full max-w-xl animate-pulse rounded-full bg-slate-100/90 dark:bg-slate-800/60"
      />
    </div>

    <div v-else-if="suggestions.length > 0" class="flex flex-col gap-2">
      <button
        v-for="item in suggestions"
        :key="item.id"
        type="button"
        :disabled="disabled"
        class="w-fit max-w-full rounded-full bg-slate-100 px-4 py-2.5 text-left text-[13px] leading-snug text-slate-800 transition-colors hover:bg-slate-200 disabled:cursor-not-allowed disabled:opacity-50 sm:text-[14px] dark:bg-slate-800/80 dark:text-slate-200 dark:hover:bg-slate-700"
        @click="handleSelect(item.text)"
      >
        <span class="follow-up-text" v-html="renderSuggestionText(item.text)" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.follow-up-text :deep(.katex) {
  font-size: 1em;
}
</style>
