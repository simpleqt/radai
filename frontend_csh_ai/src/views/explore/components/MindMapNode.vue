<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { ChevronRight, Loader2, Plus } from 'lucide-vue-next';
import type { MapNode } from '../composables/useExploreSession';

const props = defineProps<{
  node: MapNode;
  selected: boolean;
  dragging?: boolean;
  x: number;
  y: number;
  width: number;
}>();

const emit = defineEmits<{
  (e: 'drag-start', node: MapNode, event: PointerEvent): void;
  (e: 'toggle', node: MapNode): void;
  (e: 'resize', id: string, height: number): void;
}>();

const rootEl = ref<HTMLElement | null>(null);
let ro: ResizeObserver | null = null;

const isRoot = computed(() => props.node.depth === 0);
const hasChildren = computed(() => props.node.children.length > 0);
const canExpand = computed(() => props.node.expandable && !hasChildren.value);

function reportHeight() {
  if (!rootEl.value) return;
  emit('resize', props.node.id, rootEl.value.offsetHeight);
}

function onPointerDown(e: PointerEvent) {
  if ((e.target as HTMLElement).closest('button')) return;
  emit('drag-start', props.node, e);
}

onMounted(() => {
  reportHeight();
  ro = new ResizeObserver(reportHeight);
  if (rootEl.value) ro.observe(rootEl.value);
});

onUnmounted(() => ro?.disconnect());
</script>

<template>
  <div
    ref="rootEl"
    class="absolute"
    data-node
    :style="{ left: `${x}px`, top: `${y}px`, width: `${width}px` }"
  >
    <div
      class="group relative rounded-xl border px-3 py-2.5 select-none touch-none"
      :class="[
        dragging ? 'cursor-grabbing shadow-lg z-10' : 'cursor-grab',
        isRoot
          ? 'bg-gradient-to-br from-indigo-600 to-violet-600 border-transparent text-white shadow-lg shadow-indigo-200/60 dark:shadow-none'
          : selected
            ? 'bg-indigo-50 dark:bg-indigo-500/15 border-indigo-400 dark:border-indigo-500 ring-2 ring-indigo-300/60'
            : 'bg-white dark:bg-slate-800 border-slate-200 dark:border-slate-700 hover:border-indigo-300 dark:hover:border-indigo-500/60 hover:shadow-md',
      ]"
      @pointerdown.stop="onPointerDown"
    >
      <div class="flex items-center gap-2">
        <span
          class="text-[13px] font-semibold leading-snug line-clamp-2 flex-1"
          :class="isRoot ? 'text-white' : 'text-slate-800 dark:text-slate-100'"
        >{{ node.title }}</span>

        <!-- 展开/折叠 -->
        <button
          v-if="hasChildren || canExpand"
          type="button"
          class="shrink-0 w-6 h-6 rounded-lg flex items-center justify-center transition-all"
          :class="isRoot ? 'bg-white/20 hover:bg-white/30 text-white' : 'bg-slate-100 dark:bg-slate-700 hover:bg-indigo-100 dark:hover:bg-indigo-500/20 text-slate-500 dark:text-slate-300'"
          @click.stop="emit('toggle', node)"
          :title="hasChildren ? (node.expanded ? '折叠' : '展开') : '生成子知识点'"
        >
          <Loader2 v-if="node.loading" class="w-3.5 h-3.5 animate-spin" />
          <ChevronRight
            v-else-if="hasChildren"
            class="w-3.5 h-3.5 transition-transform"
            :class="node.expanded ? 'rotate-90' : ''"
          />
          <Plus v-else class="w-3.5 h-3.5" />
        </button>
      </div>
      <p
        v-if="node.summary && !isRoot"
        class="text-[11px] mt-1 leading-snug line-clamp-2 text-slate-400 dark:text-slate-500"
      >{{ node.summary }}</p>
    </div>
  </div>
</template>
