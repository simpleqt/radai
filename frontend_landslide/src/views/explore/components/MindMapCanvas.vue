<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { Plus, Minus, Maximize2 } from 'lucide-vue-next';
import type { MapNode } from '../composables/useExploreSession';
import MindMapNode from './MindMapNode.vue';

const props = defineProps<{ root: MapNode; selectedId: string | null }>();
const emit = defineEmits<{
  (e: 'select', node: MapNode): void;
  (e: 'toggle', node: MapNode): void;
}>();

const NODE_W = 190;
const NODE_H = 56;
const V_GAP = 16;
const H_GAP = 72;
const PAD = 40;

interface Positioned { node: MapNode; x: number; y: number; }
interface Edge { id: string; x1: number; y1: number; x2: number; y2: number; }

/** 用户拖动产生的节点偏移（相对自动布局位置） */
const nodeOffsets = ref<Record<string, { dx: number; dy: number }>>({});
/** 节点实际渲染高度（含 summary 时高于 NODE_H） */
const nodeHeights = ref<Record<string, number>>({});

function heightOf(id: string) {
  return nodeHeights.value[id] || NODE_H;
}

function collectSubtree(node: MapNode): MapNode[] {
  const nodes = [node];
  for (const child of node.children) nodes.push(...collectSubtree(child));
  return nodes;
}

function buildLayout() {
  const base: Positioned[] = [];
  let cursorY = PAD;

  const visibleChildren = (n: MapNode) => (n.expanded ? n.children : []);

  const place = (node: MapNode): number => {
    const h = heightOf(node.id);
    const x = PAD + node.depth * (NODE_W + H_GAP);
    const children = visibleChildren(node);
    let y: number;
    if (children.length === 0) {
      y = cursorY + h / 2;
      cursorY += h + V_GAP;
    } else {
      const childY: number[] = [];
      for (const child of children) childY.push(place(child));
      y = (childY[0] + childY[childY.length - 1]) / 2;
    }
    base.push({ node, x, y: y - h / 2 });
    return y;
  };

  place(props.root);

  const positioned = base.map((p) => {
    const off = nodeOffsets.value[p.node.id] || { dx: 0, dy: 0 };
    return { node: p.node, x: p.x + off.dx, y: p.y + off.dy };
  });
  const posMap = new Map(positioned.map((p) => [p.node.id, p]));

  const edges: Edge[] = [];
  const addEdges = (node: MapNode) => {
    const children = visibleChildren(node);
    const parent = posMap.get(node.id);
    if (parent) {
      const ph = heightOf(parent.node.id);
      for (const child of children) {
        const cp = posMap.get(child.id);
        if (cp) {
          const ch = heightOf(cp.node.id);
          edges.push({
            id: `${node.id}->${child.id}`,
            x1: parent.x + NODE_W,
            y1: parent.y + ph / 2,
            x2: cp.x,
            y2: cp.y + ch / 2,
          });
        }
      }
    }
    for (const child of children) addEdges(child);
  };
  addEdges(props.root);

  let maxRight = PAD;
  let maxBottom = PAD;
  for (const p of positioned) {
    const h = heightOf(p.node.id);
    maxRight = Math.max(maxRight, p.x + NODE_W + PAD);
    maxBottom = Math.max(maxBottom, p.y + h + PAD);
  }

  return {
    positioned,
    edges,
    width: Math.max(maxRight, 200),
    height: Math.max(maxBottom, 200),
  };
}

const layout = computed(buildLayout);
const edgePaths = ref<Edge[]>([]);

watch(
  () => [layout.value.edges, nodeOffsets.value, nodeHeights.value, props.root] as const,
  () => { edgePaths.value = layout.value.edges.map((e) => ({ ...e })); },
  { immediate: true, deep: true },
);

function edgePath(e: Edge): string {
  const mx = (e.x1 + e.x2) / 2;
  return `M ${e.x1} ${e.y1} C ${mx} ${e.y1}, ${mx} ${e.y2}, ${e.x2} ${e.y2}`;
}

function onNodeResize(id: string, height: number) {
  if (nodeHeights.value[id] === height) return;
  nodeHeights.value = { ...nodeHeights.value, [id]: height };
}

// 切换根节点时清空拖动偏移
watch(() => props.root.id, () => { nodeOffsets.value = {}; });

// 平移 + 缩放
const scale = ref(1);
const tx = ref(0);
const ty = ref(0);
const dragging = ref(false);
const panActive = ref(false);
let startX = 0, startY = 0, startTx = 0, startTy = 0;

// 节点拖动（含子树联动）
const nodeDragging = ref(false);
const dragNodeId = ref<string | null>(null);
let dragNode: MapNode | null = null;
let dragSubtree: MapNode[] = [];
let dragOrigOffsets: Record<string, { dx: number; dy: number }> = {};
let dragStartX = 0;
let dragStartY = 0;

function onNodeDragStart(node: MapNode, e: PointerEvent) {
  dragNode = node;
  dragSubtree = collectSubtree(node);
  dragNodeId.value = node.id;
  nodeDragging.value = false;
  dragStartX = e.clientX;
  dragStartY = e.clientY;
  dragOrigOffsets = {};
  for (const n of dragSubtree) {
    dragOrigOffsets[n.id] = nodeOffsets.value[n.id] || { dx: 0, dy: 0 };
  }
  window.addEventListener('pointermove', onNodeDragMove);
  window.addEventListener('pointerup', onNodeDragEnd);
}

function onNodeDragMove(e: PointerEvent) {
  if (!dragNode) return;
  const dx = (e.clientX - dragStartX) / scale.value;
  const dy = (e.clientY - dragStartY) / scale.value;
  if (!nodeDragging.value && Math.hypot(dx, dy) < 4) return;
  nodeDragging.value = true;

  const updates = { ...nodeOffsets.value };
  for (const n of dragSubtree) {
    const orig = dragOrigOffsets[n.id];
    updates[n.id] = { dx: orig.dx + dx, dy: orig.dy + dy };
  }
  nodeOffsets.value = updates;
  edgePaths.value = buildLayout().edges.map((edge) => ({ ...edge }));
}

function onNodeDragEnd() {
  if (dragNode && !nodeDragging.value) {
    emit('select', dragNode);
  }
  dragNode = null;
  dragSubtree = [];
  dragOrigOffsets = {};
  dragNodeId.value = null;
  nodeDragging.value = false;
  window.removeEventListener('pointermove', onNodeDragMove);
  window.removeEventListener('pointerup', onNodeDragEnd);
}

onUnmounted(() => {
  window.removeEventListener('pointermove', onNodeDragMove);
  window.removeEventListener('pointerup', onNodeDragEnd);
});

function onPointerDown(e: PointerEvent) {
  if ((e.target as HTMLElement).closest('[data-node]')) return;
  panActive.value = true;
  dragging.value = false;
  startX = e.clientX; startY = e.clientY;
  startTx = tx.value; startTy = ty.value;
}
function onPointerMove(e: PointerEvent) {
  if (!panActive.value) return;
  const dx = e.clientX - startX;
  const dy = e.clientY - startY;
  if (!dragging.value && Math.hypot(dx, dy) < 4) return;
  dragging.value = true;
  tx.value = startTx + dx;
  ty.value = startTy + dy;
}
function onPointerUp() { panActive.value = false; dragging.value = false; }

function zoomIn() { scale.value = Math.min(1.6, scale.value + 0.15); }
function zoomOut() { scale.value = Math.max(0.5, scale.value - 0.15); }
function resetView() { scale.value = 1; tx.value = 0; ty.value = 0; }

function onWheel(e: WheelEvent) {
  if (!e.ctrlKey && !e.metaKey) return;
  e.preventDefault();
  if (e.deltaY < 0) zoomIn(); else zoomOut();
}
</script>

<template>
  <div
    class="relative w-full h-full overflow-hidden bg-surface-sunken touch-none"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointerleave="onPointerUp"
    @wheel="onWheel"
    :class="dragging || nodeDragging ? 'cursor-grabbing' : 'cursor-grab'"
  >
    <div class="absolute inset-0 pointer-events-none bg-[radial-gradient(circle,rgba(124,58,237,0.13)_1px,transparent_1px)] bg-[length:22px_22px] dark:bg-[radial-gradient(circle,#33415533_1px,transparent_1px)]" />
    <div class="absolute inset-0 pointer-events-none bg-[radial-gradient(ellipse_70%_58%_at_50%_42%,rgba(139,92,246,0.14),transparent_62%)]" />

    <div
      class="absolute origin-top-left"
      :style="{ transform: `translate(${tx}px, ${ty}px) scale(${scale})`, width: `${layout.width}px`, height: `${layout.height}px` }"
    >
      <svg
        class="absolute top-0 left-0 pointer-events-none overflow-visible"
        :width="layout.width"
        :height="layout.height"
      >
        <path
          v-for="e in edgePaths"
          :key="e.id"
          :d="edgePath(e)"
          fill="none"
          stroke="currentColor"
          class="text-accent/80"
          stroke-width="2"
        />
      </svg>

      <MindMapNode
        v-for="p in layout.positioned"
        :key="p.node.id"
        :node="p.node"
        :selected="p.node.id === selectedId"
        :dragging="nodeDragging && dragNodeId === p.node.id"
        :x="p.x"
        :y="p.y"
        :width="NODE_W"
        @drag-start="onNodeDragStart"
        @toggle="emit('toggle', $event)"
        @resize="onNodeResize"
      />
    </div>

    <div class="absolute bottom-4 left-4 flex items-center gap-1 rounded-2xl border border-border-subtle bg-surface/86 p-1 backdrop-blur-xl">
      <button type="button" class="w-8 h-8 rounded-xl flex items-center justify-center text-text-secondary hover:bg-accent-soft hover:text-accent" @click="zoomOut" title="缩小"><Minus class="w-4 h-4" /></button>
      <span class="text-xs text-text-tertiary w-10 text-center tabular-nums">{{ Math.round(scale * 100) }}%</span>
      <button type="button" class="w-8 h-8 rounded-xl flex items-center justify-center text-text-secondary hover:bg-accent-soft hover:text-accent" @click="zoomIn" title="放大"><Plus class="w-4 h-4" /></button>
      <div class="w-px h-5 bg-surface-sunken mx-0.5" />
      <button type="button" class="w-8 h-8 rounded-xl flex items-center justify-center text-text-secondary hover:bg-accent-soft hover:text-accent" @click="resetView" title="重置视图"><Maximize2 class="w-4 h-4" /></button>
    </div>
  </div>
</template>
