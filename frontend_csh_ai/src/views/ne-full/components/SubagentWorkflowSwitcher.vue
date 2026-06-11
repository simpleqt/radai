<script setup lang="ts">
export type SubagentGroupState = 'running' | 'completed' | 'failed' | 'idle';

export interface SubagentWorkflowGroupMeta {
  id: string;
  title: string;
  state: SubagentGroupState;
  eventCount: number;
  lastUpdated: number;
}

interface Props {
  groups: SubagentWorkflowGroupMeta[];
  selectedTaskId: string;
}

defineProps<Props>();

const emit = defineEmits<{
  (e: 'select', taskId: string): void;
}>();

const stateDotClass = (state: SubagentGroupState) => {
  if (state === 'running') return 'bg-amber-500';
  if (state === 'completed') return 'bg-emerald-500';
  if (state === 'failed') return 'bg-rose-500';
  return 'bg-slate-400';
};

const stateTextClass = (state: SubagentGroupState) => {
  if (state === 'running') return 'text-amber-700 dark:text-amber-300';
  if (state === 'completed') return 'text-emerald-700 dark:text-emerald-300';
  if (state === 'failed') return 'text-rose-700 dark:text-rose-300';
  return 'text-slate-600 dark:text-slate-300';
};

const stateLabel = (state: SubagentGroupState) => {
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
        :class="[
          selectedTaskId === group.id
            ? 'border-slate-500 bg-slate-100 text-slate-900 shadow-sm dark:border-slate-500 dark:bg-slate-800/70 dark:text-slate-100'
            : 'border-stone-300 bg-white/80 text-slate-700 hover:border-stone-400 hover:bg-stone-100/70 dark:border-stone-700 dark:bg-stone-900/45 dark:text-slate-300 dark:hover:border-stone-500 dark:hover:bg-stone-800/60'
        ]"
        @click="emit('select', group.id)"
      >
        <span class="size-2 rounded-full" :class="stateDotClass(group.state)" />
        <span class="max-w-[180px] truncate text-xs font-medium sm:text-sm">{{ group.title }}</span>
        <span class="rounded-md border border-stone-300 bg-stone-100 px-1.5 py-0.5 text-[10px] text-stone-700 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-300">
          {{ group.eventCount }}
        </span>
        <span class="text-[10px] font-medium" :class="stateTextClass(group.state)">
          {{ stateLabel(group.state) }}
        </span>
      </button>
    </div>
  </div>
</template>
