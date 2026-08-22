<script setup lang="ts">
import { computed } from 'vue';

const props = defineProps<{ data: Record<string, unknown> }>();

const content = computed(() => String(props.data?.text || ''));
</script>

<template>
  <p v-if="content" class="whitespace-pre-wrap break-words text-xs leading-5 text-text-secondary">
    {{ content }}
  </p>
</template>
