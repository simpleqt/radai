<script setup lang="ts">
import { computed } from 'vue';
import { CircleAlert, TriangleAlert, Info } from 'lucide-vue-next';
import type { IssueEntry, IssuesViewData } from '../types';

const props = defineProps<{ data: Record<string, unknown> }>();

const items = computed<IssueEntry[]>(() => ((props.data as unknown as IssuesViewData)?.items || []));

const iconOf = (level: IssueEntry['level']) => {
  if (level === 'error') return CircleAlert;
  if (level === 'warning') return TriangleAlert;
  return Info;
};

const toneClass = (level: IssueEntry['level']) => {
  if (level === 'error') {
    return 'border-rose-200 bg-rose-50/70 text-rose-700 dark:border-rose-900/70 dark:bg-rose-950/30 dark:text-rose-200';
  }
  if (level === 'warning') {
    return 'border-amber-200 bg-amber-50/70 text-amber-700 dark:border-amber-900/70 dark:bg-amber-950/30 dark:text-amber-200';
  }
  return 'border-border-subtle bg-surface-sunken text-text-secondary';
};
</script>

<template>
  <ul v-if="items.length" class="space-y-1">
    <li
      v-for="(item, index) in items"
      :key="`${item.code || 'issue'}-${index}`"
      class="flex items-start gap-1.5 rounded-md border px-2 py-1.5 text-[11px] leading-5"
      :class="toneClass(item.level)"
    >
      <component :is="iconOf(item.level)" class="mt-0.5 size-3 shrink-0" />
      <span class="min-w-0 flex-1">
        <span v-if="item.code" class="mr-1 font-mono font-medium opacity-80">{{ item.code }}</span>
        <span class="break-words">{{ item.message }}</span>
        <span v-if="item.locator" class="ml-1 opacity-70">（{{ item.locator }}）</span>
      </span>
    </li>
  </ul>
</template>
