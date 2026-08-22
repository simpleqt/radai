<script setup lang="ts">
import { computed } from 'vue';
import type { KvViewData, KvViewRow } from '../types';

const props = defineProps<{ data: Record<string, unknown> }>();

const rows = computed<KvViewRow[]>(() => ((props.data as unknown as KvViewData)?.rows || []));

const valueClass = (row: KvViewRow) => {
  if (row.tone === 'error') return 'text-rose-600 dark:text-rose-400';
  if (row.tone === 'warning') return 'text-amber-600 dark:text-amber-400';
  if (row.tone === 'success') return 'text-emerald-600 dark:text-emerald-400';
  return 'text-text-primary';
};
</script>

<template>
  <dl v-if="rows.length" class="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
    <div v-for="row in rows" :key="row.label" class="flex min-w-0 items-baseline gap-2 text-xs leading-5">
      <dt class="shrink-0 text-text-tertiary">{{ row.label }}</dt>
      <dd class="min-w-0 flex-1 truncate font-medium" :class="valueClass(row)" :title="row.value">{{ row.value }}</dd>
    </div>
  </dl>
</template>
