<script setup lang="ts">
import { computed } from 'vue';
import {
  Activity,
  BarChart3,
  CircleDot,
  Cpu,
  Database,
  FlaskConical,
  Link2,
  Network,
  RadioTower,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  Waves,
  Zap,
} from 'lucide-vue-next';
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
  'text-accent bg-accent-soft',
  'text-blue-600 bg-blue-500/10',
  'text-fuchsia-600 bg-fuchsia-500/10',
  'text-accent bg-accent-soft',
  'text-sky-600 bg-sky-500/10',
  'text-accent bg-accent-soft',
];

const dotClass = computed(() => DOT_COLORS[props.index % DOT_COLORS.length]);
const iconSet = [
  Zap,
  Database,
  CircleDot,
  ShieldCheck,
  Activity,
  Cpu,
  Waves,
  Link2,
  FlaskConical,
  BarChart3,
  ScanSearch,
  Network,
  RadioTower,
  Sparkles,
];
const bubbleIcon = computed(() => iconSet[props.index % iconSet.length]);

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
    <span class="absolute -inset-2 rounded-2xl bg-accent/0 opacity-0 blur-xl transition-all duration-500 group-hover:bg-accent-soft group-hover:opacity-100" />
    
    <span
      class="relative flex items-center gap-2.5 min-w-0 rounded-2xl border border-border-subtle bg-surface/90 backdrop-blur-2xl transition-all duration-300 group-hover:border-accent/80 group-hover:bg-surface dark:border-white/10"
      :class="size === 'lg' ? 'px-[18px] py-3' : 'px-4 py-2.5'"
    >
      <span
        class="relative grid shrink-0 place-items-center rounded-xl"
        :class="[dotClass, size === 'lg' ? 'h-7 w-7' : 'h-6 w-6']"
      >
        <component :is="bubbleIcon" :class="size === 'lg' ? 'h-4 w-4' : 'h-3.5 w-3.5'" />
      </span>
      
      <span
        class="max-w-[190px] truncate whitespace-nowrap font-semibold text-text-secondary transition-colors group-hover:text-accent"
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
