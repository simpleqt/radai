<script setup lang="ts">
import { computed } from 'vue';
import { formatDuration } from './humanize';
import StepIcon from './StepIcon.vue';
import type { DisplayChip, NormalizedStep } from './types';
import { resolveViewComponent } from './views';

const props = defineProps<{ step: NormalizedStep }>();

const viewComponent = computed(() => resolveViewComponent(props.step.view));
const durationText = computed(() => formatDuration(props.step.durationMs));
const hasViewContent = computed(() => Object.keys(props.step.data || {}).length > 0);

const chipClass = (chip: DisplayChip) => {
  if (chip.tone === 'error') {
    return 'border-rose-200 bg-rose-50/80 text-rose-700 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-200';
  }
  if (chip.tone === 'warning') {
    return 'border-amber-200 bg-amber-50/80 text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200';
  }
  if (chip.tone === 'success') {
    return 'border-emerald-200 bg-emerald-50/80 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200';
  }
  return 'border-border-subtle bg-surface/80 text-text-secondary';
};
</script>

<template>
  <div
    class="animate-in fade-in slide-in-from-bottom-1 flex items-start gap-2 text-[15px] duration-300"
    :class="step.tone === 'error'
      ? 'rounded-lg border border-rose-200 bg-rose-50/70 px-2 py-1.5 text-rose-800 dark:border-rose-900/70 dark:bg-rose-950/30 dark:text-rose-200'
      : 'text-text-secondary'"
  >
    <StepIcon
      :name="step.icon"
      class="mt-1 size-[15px] shrink-0"
      :class="step.tone === 'error' ? 'text-rose-500 dark:text-rose-400' : 'text-text-secondary'"
    />

    <div class="min-w-0 flex-1 space-y-1">
      <p class="leading-6" :class="step.tone === 'error' ? 'font-medium' : ''">{{ step.label }}</p>

      <div v-if="step.chips.length" class="flex flex-wrap gap-1.5">
        <span
          v-for="chip in step.chips"
          :key="`${step.id}-${chip.text}`"
          class="inline-flex items-center rounded-full border px-2 py-0.5 text-[11px]"
          :class="chipClass(chip)"
        >
          {{ chip.text }}
        </span>
      </div>

      <p
        v-if="step.statusText"
        class="whitespace-pre-wrap break-words text-xs leading-5"
        :class="step.tone === 'error' ? 'text-rose-700 dark:text-rose-300' : 'text-text-secondary'"
      >
        {{ step.statusText }}
      </p>

      <component :is="viewComponent" v-if="hasViewContent" :data="step.data" />

      <div v-if="step.path" class="border-l border-border-subtle pl-3 text-xs">
        <span class="inline-block rounded bg-surface-sunken px-2 py-0.5 text-text-secondary">{{ step.path }}</span>
      </div>

      <p
        v-if="durationText"
        class="text-[11px]"
        :class="step.tone === 'error' ? 'text-rose-500 dark:text-rose-400' : 'text-text-tertiary'"
      >
        耗时 {{ durationText }}
      </p>
    </div>
  </div>
</template>
