<script setup lang="ts">
import { computed } from 'vue';

interface Props {
  status: string;
  active?: boolean;
}

const props = defineProps<Props>();

const normalizedStatus = computed(() => props.status.toLowerCase());

const toolName = computed(() => {
  if (normalizedStatus.value.includes('multi_kb_search')) return '知识库检索';
  if (normalizedStatus.value.includes('web_search')) return '联网搜索';
  if (normalizedStatus.value.includes('task')) return '子智能体派遣';
  if (normalizedStatus.value.includes('tool') && normalizedStatus.value.includes('done')) return '工具调用完成';
  if (normalizedStatus.value.includes('answer')) return '获取到答案';
  if (normalizedStatus.value.includes('thinking') || normalizedStatus.value.includes('思考') || normalizedStatus.value.includes('分析')) return '智能体思考';
  return props.status;
});

const isDone = computed(() => {
  const status = normalizedStatus.value;
  return status.includes('done') || status.includes('完成') || status.includes('finish') || status.includes('success') || status.includes('获取到答案');
});
</script>

<template>
  <div
    class="inline-flex items-center space-x-1.5 px-2.5 py-1 rounded-full border text-xs font-medium mr-2 mb-2 animate-in fade-in slide-in-from-left-2 duration-300 transition-all"
    :class="[ active
        ? 'border-accent/30 bg-accent-soft text-accent'
        : 'border-border-subtle bg-surface-sunken text-text-secondary'
    ]"
  >
    <div
      class="w-1.5 h-1.5 rounded-full"
      :class="[ active ? 'bg-accent animate-pulse' : isDone ? 'bg-emerald-500' : 'bg-slate-400'
      ]"
    ></div>
    <span class="capitalize">{{ toolName }}</span>
  </div>
</template>
