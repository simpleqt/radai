<script setup lang="ts">
import { computed, ref } from 'vue';
import { ChevronDown, Clock3, Database, Lightbulb, ListChecks, Search, WandSparkles } from 'lucide-vue-next';
import { type ChatMessage, type AgentProcessEvent } from '@/api/agent-v2';

interface Props {
  message: ChatMessage;
}

const props = defineProps<Props>();
const isExpanded = ref(true);

const eventIcon = (event: AgentProcessEvent) => {
  if (event.kind === 'tool_start' || event.kind === 'tool_end' || event.kind === 'tool_result') {
    if (event.tool?.includes('multi_kb_search')) return Database;
    if (event.tool?.includes('web_search')) return Search;
    return WandSparkles;
  }
  if (event.kind === 'task_start' || event.kind === 'task_end') return ListChecks;
  if (event.kind === 'thinking') return Lightbulb;
  return WandSparkles;
};

const eventTone = (event: AgentProcessEvent) => {
  switch (event.level) {
    case 'active':
      return 'border-indigo-200 bg-indigo-50/90 text-indigo-700 dark:border-indigo-800/60 dark:bg-indigo-950/30 dark:text-indigo-300';
    case 'success':
      return 'border-emerald-200 bg-emerald-50/90 text-emerald-700 dark:border-emerald-800/60 dark:bg-emerald-950/30 dark:text-emerald-300';
    case 'warning':
      return 'border-amber-200 bg-amber-50/90 text-amber-700 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-300';
    case 'error':
      return 'border-rose-200 bg-rose-50/90 text-rose-700 dark:border-rose-800/60 dark:bg-rose-950/30 dark:text-rose-300';
    default:
      return 'border-slate-200 bg-slate-50/90 text-slate-700 dark:border-slate-800/70 dark:bg-slate-900/50 dark:text-slate-300';
  }
};

const eventLabel = (event: AgentProcessEvent) => {
  if (event.kind === 'tool_start') return '工具启动';
  if (event.kind === 'tool_end') return '工具完成';
  if (event.kind === 'tool_result') return '结果摘要';
  if (event.kind === 'task_start') return '任务派发';
  if (event.kind === 'task_end') return '任务完成';
  if (event.kind === 'thinking') return '推理';
  return event.title;
};

const cleanPlainEventText = (value?: string) => {
  if (!value) return '';
  let text = String(value);

  text = text
    .replace(/\u200b|\u200c|\u200d|\ufeff/gi, '')
    .replace(/&amp;lt;/gi, '&lt;')
    .replace(/&amp;gt;/gi, '&gt;');

  const normalizeTag = (tagName: 'sup' | 'sub') => {
    const letters = tagName.split('').join('[\\s\\u00A0\\u200B\\u200C\\u200D]*');
    const open = new RegExp(`(?:<|&lt;)\\s*${letters}\\s*(?:>|&gt;)`, 'gi');
    const close = new RegExp(`(?:<|&lt;)\\s*\\/\\s*${letters}\\s*(?:>|&gt;)`, 'gi');

    text = text
      .replace(open, `<${tagName}>`)
      .replace(close, `</${tagName}>`)
      .replace(new RegExp(`</${tagName}>\\s*(?:>|&gt;)`, 'gi'), `</${tagName}>`);
  };

  normalizeTag('sup');
  normalizeTag('sub');

  return text
    .replace(/<sup>\s*([^<\n]{1,16})\s*<\/sup>/gi, '^$1')
    .replace(/<sub>\s*([^<\n]{1,16})\s*<\/sub>/gi, '_$1');
};

const events = computed(() => props.message.processEvents || []);
const totalEvents = computed(() => events.value.length);
const activeEvents = computed(() => events.value.filter((event) => event.level === 'active').length);

const toggle = () => {
  isExpanded.value = !isExpanded.value;
};
</script>

<template>
  <section
    v-if="totalEvents > 0"
    class="overflow-hidden rounded-[30px] border border-slate-200/80 bg-white/90 shadow-[0_18px_60px_-30px_rgba(15,23,42,0.24)] backdrop-blur-sm dark:border-slate-800/80 dark:bg-slate-950/35"
  >
    <div class="flex items-start justify-between gap-4 border-b border-slate-200/70 px-5 py-4 dark:border-slate-800/70">
      <div class="space-y-1">
        <p class="text-[11px] font-semibold uppercase tracking-[0.32em] text-slate-400 dark:text-slate-500">过程日志</p>
        <div class="flex flex-wrap items-center gap-2">
          <h3 class="text-sm font-semibold text-slate-900 dark:text-slate-100">结构化事件流</h3>
          <span class="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] font-medium text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">{{ totalEvents }} 条</span>
          <span v-if="activeEvents > 0" class="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-600 dark:border-indigo-800/60 dark:bg-indigo-950/30 dark:text-indigo-300">{{ activeEvents }} 条进行中</span>
        </div>
      </div>

      <button
        type="button"
        class="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white/80 px-3 py-1.5 text-[11px] font-semibold text-slate-600 transition-colors hover:border-slate-300 hover:text-slate-900 dark:border-slate-700 dark:bg-slate-900/60 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:text-slate-100"
        @click="toggle"
      >
        <ChevronDown class="size-3.5 transition-transform duration-200" :class="isExpanded ? 'rotate-180' : ''" />
        {{ isExpanded ? '收起' : '展开' }}
      </button>
    </div>

    <div v-show="isExpanded" class="space-y-3 px-5 py-5">
      <div
        v-for="(event, index) in events"
        :key="`${message.id}-event-${index}-${event.kind}`"
        class="rounded-[24px] border px-4 py-4 transition-all duration-200"
        :class="eventTone(event)"
      >
        <div class="flex items-start gap-3">
          <div class="flex size-10 shrink-0 items-center justify-center rounded-2xl border border-white/70 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-950">
            <component :is="eventIcon(event)" class="size-4" />
          </div>

          <div class="min-w-0 flex-1 space-y-2">
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-sm font-semibold text-slate-900 dark:text-slate-100">{{ eventLabel(event) }}</span>
              <span v-if="event.tool" class="rounded-full bg-white/70 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500 dark:bg-slate-950/60 dark:text-slate-400">{{ event.tool }}</span>
              <span v-if="event.duration_ms !== undefined" class="inline-flex items-center gap-1 rounded-full bg-white/70 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500 dark:bg-slate-950/60 dark:text-slate-400">
                <Clock3 class="size-3" />
                {{ event.duration_ms }}ms
              </span>
            </div>

            <p class="text-sm leading-relaxed text-slate-600 dark:text-slate-300">{{ cleanPlainEventText(event.detail) }}</p>

            <p v-if="event.summary" class="rounded-2xl border border-white/60 bg-white/65 px-3 py-2 text-[12px] leading-relaxed text-slate-500 dark:border-slate-800/60 dark:bg-slate-950/30 dark:text-slate-400">
              {{ cleanPlainEventText(event.summary) }}
            </p>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
