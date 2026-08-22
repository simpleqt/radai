<script setup lang="ts">
import { Menu, PanelLeftOpen } from 'lucide-vue-next';

defineProps<{
  title?: string;
  sidebarState: 'expanded' | 'collapsed';
  isOverlay: boolean;
}>();

const emit = defineEmits<{ (e: 'toggle-sidebar'): void }>();
</script>

<template>
  <header
    class="sticky top-0 z-30 flex h-(--topbar-h) shrink-0 items-center gap-2 border-b border-border-subtle bg-surface px-3"
  >
    <button
      v-if="isOverlay || sidebarState === 'collapsed'"
      type="button"
      class="animate-in fade-in slide-in-from-left-1 inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-text-secondary transition-colors duration-200 hover:bg-surface-hover hover:text-text-primary"
      :title="isOverlay ? '打开菜单' : '展开侧栏'"
      @click="emit('toggle-sidebar')"
    >
      <Menu v-if="isOverlay" class="size-4.5" />
      <PanelLeftOpen v-else class="size-4.5" />
    </button>

    <h1 v-if="title" class="min-w-0 flex-1 truncate text-sm font-semibold text-text-primary">
      {{ title }}
    </h1>
    <span v-else class="min-w-0 flex-1" />

    <div class="flex shrink-0 items-center gap-1">
      <slot name="actions" />
    </div>
  </header>
</template>
