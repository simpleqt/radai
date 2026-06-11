<script setup lang="ts">
import { computed } from 'vue';
import type { ExploreTopic } from '@/api/explore';

const props = defineProps<{
  topic: ExploreTopic;
  index: number;
  size?: 'md' | 'lg';
}>();

defineEmits<{
  (e: 'select', topic: ExploreTopic): void;
  (e: 'hover', hovered: boolean): void;
}>();

const DOT_COLORS = [
  'from-indigo-500 to-violet-500 ring-indigo-500/20',
  'from-violet-500 to-purple-500 ring-violet-500/20',
  'from-blue-500 to-indigo-500 ring-blue-500/20',
  'from-indigo-400 to-blue-500 ring-indigo-400/20',
  'from-purple-500 to-indigo-600 ring-purple-500/20',
  'from-sky-500 to-indigo-500 ring-sky-500/20',
];

const dotClass = computed(() => DOT_COLORS[props.index % DOT_COLORS.length]);

const floatStyle = computed(() => {
  const delay = (props.index % 7) * -0.65; // Use negative delay so it starts floating immediately
  const duration = 5.0 + (props.index % 5) * 1.0;
  return {
    animationDelay: `${delay}s`,
    animationDuration: `${duration}s`,
  };
});
</script>

<template>
  <button
    type="button"
    class="bubble group relative inline-flex items-center text-left transition-all duration-300 hover:-translate-y-1 hover:scale-[1.04] active:scale-[0.97]"
    :style="floatStyle"
    :title="topic.hint || topic.title"
    @click="$emit('select', topic)"
    @mouseenter="$emit('hover', true)"
    @mouseleave="$emit('hover', false)"
  >
    <!-- 悬停霓虹发光底色 -->
    <span class="absolute -inset-1.5 rounded-2xl bg-gradient-to-r from-indigo-500/0 to-purple-500/0 opacity-0 group-hover:from-indigo-500/12 group-hover:to-purple-500/12 blur-xl transition-all duration-500 group-hover:opacity-100" />
    
    <!-- 气泡本体玻璃态 -->
    <span
      class="relative flex items-center gap-2 min-w-0 rounded-2xl border border-white/70 dark:border-slate-800/80 bg-white/75 dark:bg-slate-900/60 backdrop-blur-xl shadow-sm shadow-indigo-100/30 dark:shadow-none transition-all duration-300 group-hover:border-indigo-400/60 dark:group-hover:border-indigo-500/50 group-hover:shadow-lg group-hover:shadow-indigo-500/10 dark:group-hover:shadow-indigo-500/5"
      :class="size === 'lg' ? 'px-4.5 py-3' : 'px-4 py-2.5'"
    >
      <!-- 核心脉冲指示器 -->
      <span class="relative flex h-2 w-2 shrink-0">
        <span
          class="animate-ping absolute inline-flex h-full w-full rounded-full opacity-60"
          :class="[dotClass.split(' ')[0]]"
        />
        <span
          class="relative inline-flex rounded-full bg-gradient-to-br shadow-inner ring-4"
          :class="[dotClass, size === 'lg' ? 'w-2 h-2' : 'w-2 h-2']"
        />
      </span>
      
      <!-- 主题标签文本 -->
      <span
        class="font-medium text-slate-700 dark:text-slate-200 whitespace-nowrap group-hover:text-indigo-600 dark:group-hover:text-indigo-300 transition-colors tracking-wide"
        :class="size === 'lg' ? 'text-[13px]' : 'text-[12px]'"
      >{{ topic.title }}</span>
    </span>
  </button>
</template>

<style scoped>
.bubble {
  animation-name: floaty;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-direction: alternate;
  padding: 0;
  background: none;
  border: none;
}
@keyframes floaty {
  from { transform: translateY(0); }
  to { transform: translateY(-7px); }
}
.group:hover { animation-play-state: paused; }

@media (prefers-reduced-motion: reduce) {
  .bubble { animation: none; }
}
</style>
