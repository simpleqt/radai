<script setup lang="ts">
import { computed } from 'vue';
import { FileText } from 'lucide-vue-next';
import type { ArtifactViewData } from '../types';

const props = defineProps<{ data: Record<string, unknown> }>();

const artifact = computed<ArtifactViewData | null>(() => {
  const value = props.data as unknown as ArtifactViewData;
  return value?.title ? value : null;
});
</script>

<template>
  <component
    :is="artifact?.url ? 'a' : 'div'"
    v-if="artifact"
    :href="artifact.url"
    :target="artifact.url ? '_blank' : undefined"
    :rel="artifact.url ? 'noopener noreferrer' : undefined"
    class="inline-flex max-w-full items-center gap-2 rounded-md border border-border-subtle bg-surface-sunken px-2.5 py-1.5 text-[11px] text-text-secondary"
    :class="artifact.url ? 'transition-colors hover:border-border-strong hover:text-text-primary' : ''"
  >
    <FileText class="size-3.5 shrink-0" />
    <span class="min-w-0 flex-1 truncate font-medium text-text-primary" :title="artifact.title">
      {{ artifact.title }}
    </span>
    <span v-if="artifact.kind" class="shrink-0 uppercase opacity-70">{{ artifact.kind }}</span>
    <span v-if="artifact.size" class="shrink-0 opacity-70">{{ artifact.size }}</span>
  </component>
</template>
