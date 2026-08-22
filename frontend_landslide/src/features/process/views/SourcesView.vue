<script setup lang="ts">
import { computed } from 'vue';
import { Globe } from 'lucide-vue-next';
import type { SourceEntry, SourcesViewData } from '../types';

const props = defineProps<{ data: Record<string, unknown> }>();

const items = computed<SourceEntry[]>(() => ((props.data as unknown as SourcesViewData)?.items || []));
</script>

<template>
  <div v-if="items.length" class="space-y-1">
    <p class="text-[11px] text-emerald-700 dark:text-emerald-300">网页来源</p>
    <a
      v-for="site in items"
      :key="site.url"
      :href="site.url"
      target="_blank"
      rel="noopener noreferrer"
      class="flex items-start gap-1.5 rounded-md border border-emerald-200 bg-emerald-50/70 px-2 py-1.5 text-[11px] text-emerald-700 transition-colors hover:bg-emerald-100 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300 dark:hover:bg-emerald-900/50"
    >
      <Globe class="mt-0.5 size-3 shrink-0" />
      <span class="min-w-0 flex-1">
        <span v-if="site.title" class="block truncate font-medium" :title="site.title">{{ site.title }}</span>
        <span class="flex min-w-0 gap-1.5">
          <span class="shrink-0">{{ site.domain }}</span>
          <span class="truncate text-emerald-600/90 dark:text-emerald-400/90">{{ site.url }}</span>
        </span>
      </span>
    </a>
  </div>
</template>
