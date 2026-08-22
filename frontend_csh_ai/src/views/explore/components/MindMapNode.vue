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
      :class="[ dragging ? 'cursor-grabbing shadow-sm z-10' : 'cursor-grab',
        isRoot
          ? 'bg-accent border-transparent text-white shadow-sm dark:shadow-none'
          : selected
            ? 'bg-accent-soft border-accent ring-2 ring-accent/60'
            : 'bg-surface border-border-subtle hover:border-accent/30 hover:shadow-sm',
      ]"
      @pointerdown.stop="onPointerDown"
    >
      <div class="flex items-center gap-2">
        <span
          class="text-[13px] font-semibold leading-snug line-clamp-2 flex-1"
          :class="isRoot ? 'text-white' : 'text-text-primary'"
        >{{ node.title }}</span>

        <!-- 展开/折叠 -->
        <button
          v-if="hasChildren || canExpand"
          type="button"
          class="shrink-0 w-6 h-6 rounded-lg flex items-center justify-center transition-all"
          :class="isRoot ? 'bg-white/20 hover:bg-white/30 text-white' : 'bg-surface-sunken hover:bg-accent-soft text-text-secondary'"
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
        class="text-[11px] mt-1 leading-snug line-clamp-2 text-text-tertiary"
      >{{ node.summary }}</p>
    </div>
  </div>
</template>
