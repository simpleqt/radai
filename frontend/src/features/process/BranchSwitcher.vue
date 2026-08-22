<script setup lang="ts">
import type { GroupState, StepGroup } from './types';

defineProps<{ groups: StepGroup[]; selectedId: string }>();

const emit = defineEmits<{ (e: 'select', groupId: string): void }>();

const stateDotClass = (state: GroupState) => {
  if (state === 'running') return 'bg-amber-500';
  if (state === 'completed') return 'bg-emerald-500';
  if (state === 'failed') return 'bg-rose-500';
  return 'bg-slate-400';
};

const stateTextClass = (state: GroupState) => {
  if (state === 'running') return 'text-amber-700 dark:text-amber-300';
  if (state === 'completed') return 'text-emerald-700 dark:text-emerald-300';
  if (state === 'failed') return 'text-rose-700 dark:text-rose-300';
  return 'text-text-secondary';
};

const stateLabel = (state: GroupState) => {
  if (state === 'running') return '运行中';
  if (state === 'completed') return '已完成';
  if (state === 'failed') return '异常';
  return '待机';
};
</script>

<template>
  <div class="-mx-1 overflow-x-auto pb-1">
    <div class="flex min-w-full gap-2 px-1">
      <button
        v-for="group in groups"
        :key="group.id"
        type="button"
        class="group inline-flex shrink-0 items-center gap-2 rounded-xl border px-3 py-2 text-left transition-all"
        :class="selectedId === group.id
          ? 'border-border-strong bg-surface-sunken text-text-primary shadow-sm'
          : 'border-border-subtle bg-surface/80 text-text-secondary hover:border-border-strong hover:bg-surface-hover'"
        @click="emit('select', group.id)"
      >
        <span class="size-2 rounded-full" :class="stateDotClass(group.state)" />
        <span class="max-w-[180px] truncate text-xs font-medium sm:text-sm">{{ group.title }}</span>
        <span class="rounded-md border border-border-subtle bg-surface-sunken px-1.5 py-0.5 text-[10px] text-text-secondary">
          {{ group.stepCount }}
        </span>
        <span class="text-[10px] font-medium" :class="stateTextClass(group.state)">{{ stateLabel(group.state) }}</span>
      </button>
    </div>
  </div>
</template>
