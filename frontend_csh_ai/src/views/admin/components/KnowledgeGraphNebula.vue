<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { Loader2, Maximize2, Minimize2, RefreshCw, Search, ZoomIn, ZoomOut } from 'lucide-vue-next';
import { KBDetailAPI, type KnowledgeGraphResponse } from '@/api/kb';

const props = defineProps<{
  kbId: string;
  kbName?: string;
  fullPage?: boolean;
}>();

type GraphNode = KnowledgeGraphResponse['nodes'][number] & {
  x: number;
  y: number;
  z: number;
  projectedX: number;
  projectedY: number;
  projectedScale: number;
  radius: number;
  color: string;
  drift: number;
  phase: number;
};

type GraphEdge = KnowledgeGraphResponse['edges'][number];

const canvasRef = ref<HTMLCanvasElement | null>(null);
const wrapRef = ref<HTMLDivElement | null>(null);
const shellRef = ref<HTMLElement | null>(null);
const loading = ref(false);
const errorMsg = ref('');
const graph = ref<KnowledgeGraphResponse | null>(null);
const nodes = ref<GraphNode[]>([]);
const edges = ref<GraphEdge[]>([]);
const selectedNode = ref<GraphNode | null>(null);
const hoveredNode = ref<GraphNode | null>(null);
const nodeLimit = ref(5000);
const edgeLimit = ref(10000);
const searchText = ref('');
const zoom = ref(1);
const isFullscreen = ref(false);

let nodeLookup = new Map<string, GraphNode>();
let frameId = 0;
let resizeObserver: ResizeObserver | null = null;
let canvasWidth = 900;
let canvasHeight = 520;
let pan = { x: 0, y: 0 };
let drag = { active: false, moved: false, x: 0, y: 0 };
let pointer = { x: 0, y: 0, targetX: 0, targetY: 0 };
let reduceMotion = false;
let currentGalaxyRotation = 0;
let labelDegreeThreshold = Number.POSITIVE_INFINITY;
let projectionYawCos = 1;
let projectionYawSin = 0;
let projectionPitchCos = 1;
let projectionPitchSin = 0;
let projectionCameraDistance = 1000;

const palette = ['#f8d477', '#61e5b0', '#f4f1df', '#f58ab8', '#42cfa2', '#f0ae49', '#b8f5d9'];

const totalLabel = computed(() => {
  const data = graph.value;
  if (!data) return '暂无图谱数据';
  return `${data.sampled_nodes}/${data.total_nodes || data.sampled_nodes} 节点 · ${data.sampled_edges}/${data.total_edges || data.sampled_edges} 关系`;
});

const typeLegend = computed(() => {
  const counts = new Map<string, { color: string; count: number }>();
  nodes.value.forEach((node) => {
    const key = node.entity_type || 'unknown';
    const current = counts.get(key) || { color: node.color, count: 0 };
    current.count += 1;
    counts.set(key, current);
  });
  return Array.from(counts.entries())
    .map(([type, item]) => ({ type, ...item }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 6);
});

const detailNode = computed(() => hoveredNode.value || selectedNode.value);

function hashSeed(text: string) {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
  }
  return hash;
}

const hashText = (text: string) => {
  return hashSeed(text);
};

const colorForType = (type: string) => palette[hashText(type || 'unknown') % palette.length];

const resizeCanvas = () => {
  const canvas = canvasRef.value;
  const wrap = wrapRef.value;
  if (!canvas || !wrap) return;

  const rect = wrap.getBoundingClientRect();
  canvasWidth = Math.max(320, Math.floor(rect.width));
  canvasHeight = Math.max(440, Math.floor(rect.height));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(canvasWidth * dpr);
  canvas.height = Math.floor(canvasHeight * dpr);
  canvas.style.width = `${canvasWidth}px`;
  canvas.style.height = `${canvasHeight}px`;

  if (nodes.value.length) {
    layoutGraph(false);
  }
};

const layoutGraph = (resetView = true) => {
  const rawNodes = graph.value?.nodes || [];
  const maxDegree = Math.max(1, ...rawNodes.map((node) => node.degree || 0));
  const centerX = canvasWidth / 2;
  const centerY = canvasHeight / 2;
  const spreadX = canvasWidth * 0.47;
  const spreadY = canvasHeight * 0.45;
  const sorted = [...rawNodes].sort((a, b) => (b.degree || 0) - (a.degree || 0));
  const densityScale = sorted.length > 2400 ? 0.55 : sorted.length > 1200 ? 0.72 : 1;
  labelDegreeThreshold = sorted[Math.min(11, Math.max(0, sorted.length - 1))]?.degree || Number.POSITIVE_INFINITY;

  nodes.value = sorted.map((node, index) => {
    const degreeRatio = (node.degree || 0) / maxDegree;
    const radial = 0.025 + Math.pow((index + 1) / Math.max(1, sorted.length), 0.72) * 0.94;
    const branch = hashText(`${node.entity_type || node.id}:branch`) % 4;
    const armOffset = ((hashText(`${node.id}:arm`) % 1000) / 1000 - 0.5) * 0.48;
    const angle = branch * (Math.PI / 2) + radial * 5.2 + armOffset;
    const thickness = ((hashText(`${node.id}:thickness`) % 1000) / 1000 - 0.5)
      * Math.max(8, radial * 42);

    return {
      ...node,
      x: centerX + Math.cos(angle) * radial * spreadX + thickness,
      y: centerY + Math.sin(angle) * radial * spreadY + thickness * 0.34,
      z: ((hashText(`${node.id}:depth`) % 1000) / 1000 - 0.5)
        * Math.min(canvasWidth, canvasHeight) * (0.22 + radial * 0.48),
      projectedX: centerX,
      projectedY: centerY,
      projectedScale: 1,
      radius: Math.max(0.65, Math.min(5.2, 0.7 + Math.sqrt(node.degree || 1) * 0.42 + degreeRatio * 2.4) * densityScale),
      color: colorForType(node.entity_type),
      drift: 1.5 + (hashText(`${node.id}:drift`) % 25) / 10,
      phase: (hashText(`${node.id}:phase`) % 628) / 100,
    };
  });

  edges.value = graph.value?.edges || [];
  nodeLookup = new Map(nodes.value.map((node) => [node.id, node]));

  if (resetView) {
    zoom.value = 1;
    pan = { x: 0, y: 0 };
    selectedNode.value = null;
    hoveredNode.value = null;
  }
};

const loadGraph = async (refresh = false) => {
  if (!props.kbId) return;
  loading.value = true;
  errorMsg.value = '';
  try {
    const data = await KBDetailAPI.graph(props.kbId, {
      node_limit: nodeLimit.value,
      edge_limit: edgeLimit.value,
      refresh,
    });
    graph.value = data;
    await nextTick();
    resizeCanvas();
    layoutGraph(true);
  } catch (e: any) {
    errorMsg.value = e?.message || '知识图谱加载失败';
    graph.value = null;
    nodes.value = [];
    edges.value = [];
  } finally {
    loading.value = false;
  }
};

const drawBackground = (ctx: CanvasRenderingContext2D) => {
  ctx.globalAlpha = 1;
  ctx.fillStyle = '#02040a';
  ctx.fillRect(0, 0, canvasWidth, canvasHeight);
};

const projectGraphNode = (node: GraphNode) => {
  const centerX = canvasWidth / 2;
  const centerY = canvasHeight / 2;
  const localX = node.x - centerX;
  const localY = node.y - centerY;
  const rotatedX = localX * projectionYawCos + node.z * projectionYawSin;
  const yawDepth = -localX * projectionYawSin + node.z * projectionYawCos;
  const rotatedY = localY * projectionPitchCos - yawDepth * projectionPitchSin;
  const depth = localY * projectionPitchSin + yawDepth * projectionPitchCos;
  const perspective = Math.min(1.75, Math.max(0.58, projectionCameraDistance / (projectionCameraDistance + depth)));
  return {
    x: centerX + rotatedX * perspective,
    y: centerY + rotatedY * perspective,
    scale: perspective,
  };
};

const updateProjectionCamera = () => {
  const yaw = currentGalaxyRotation * 0.72 + pointer.x * 0.3;
  const pitch = (selectedNode.value ? 0.44 : 0.12) + pointer.y * 0.2;
  projectionYawCos = Math.cos(yaw);
  projectionYawSin = Math.sin(yaw);
  projectionPitchCos = Math.cos(pitch);
  projectionPitchSin = Math.sin(pitch);
  projectionCameraDistance = Math.max(canvasWidth, canvasHeight) * 1.18;
};

const draw = (time = 0) => {
  const canvas = canvasRef.value;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvasWidth, canvasHeight);
  pointer.x += (pointer.targetX - pointer.x) * 0.035;
  pointer.y += (pointer.targetY - pointer.y) * 0.035;
  const animationTime = reduceMotion ? 0 : time;
  currentGalaxyRotation = reduceMotion ? 0 : animationTime * 0.000012;
  updateProjectionCamera();
  drawBackground(ctx);

  ctx.save();
  ctx.translate(pan.x, pan.y);
  ctx.scale(zoom.value, zoom.value);

  ctx.globalCompositeOperation = 'lighter';
  nodes.value.forEach((node) => {
    const projected = projectGraphNode(node);
    node.projectedX = projected.x;
    node.projectedY = projected.y;
    node.projectedScale = projected.scale;
  });

  const activeId = hoveredNode.value?.id || selectedNode.value?.id;
  const pinnedId = selectedNode.value?.id;
  const activeColor = hoveredNode.value?.color || selectedNode.value?.color || '#f8d477';
  edges.value.forEach((edge, edgeIndex) => {
    const source = nodeLookup.get(edge.source);
    const target = nodeLookup.get(edge.target);
    if (!source || !target) return;
    const highlighted = Boolean(pinnedId && (edge.source === pinnedId || edge.target === pinnedId));
    const dx = target.projectedX - source.projectedX;
    const dy = target.projectedY - source.projectedY;
    const routeSeed = hashText(`${edge.source}:${edge.target}`);
    const bendRatio = 0.28 + (routeSeed % 440) / 1000;
    const bendX = source.projectedX + dx * bendRatio;
    const bendY = source.projectedY + dy * bendRatio;
    ctx.globalAlpha = highlighted ? (pinnedId ? 0.28 : 0.18) : 0.022;
    ctx.strokeStyle = highlighted ? activeColor : '#94a3b8';
    ctx.lineWidth = highlighted ? 0.72 : 0.32;
    ctx.lineJoin = 'miter';
    ctx.beginPath();
    ctx.moveTo(source.projectedX, source.projectedY);
    if (routeSeed % 2 === 0) {
      ctx.lineTo(bendX, source.projectedY);
      ctx.lineTo(bendX, target.projectedY);
    } else {
      ctx.lineTo(source.projectedX, bendY);
      ctx.lineTo(target.projectedX, bendY);
    }
    ctx.lineTo(target.projectedX, target.projectedY);
    ctx.stroke();

    if (highlighted && !reduceMotion) {
      const progress = ((animationTime / 1800) + edgeIndex * 0.19) % 1;
      const photonX = source.projectedX + dx * progress;
      const photonY = source.projectedY + dy * progress;
      ctx.globalAlpha = 0.65;
      ctx.fillStyle = '#ffffff';
      ctx.shadowColor = activeColor;
      ctx.shadowBlur = 6;
      ctx.beginPath();
      ctx.arc(photonX, photonY, 0.75, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  });

  nodes.value.forEach((node) => {
    const pulse = Math.sin(animationTime / 900 + node.phase) * node.drift;
    const x = node.projectedX + Math.cos(animationTime / 2400 + node.phase) * node.drift;
    const y = node.projectedY + Math.sin(animationTime / 2600 + node.phase) * node.drift;
    const active = node.id === activeId;
    const hub = (node.degree || 0) >= labelDegreeThreshold;
    const radius = node.radius * node.projectedScale + (active ? 2.5 : 0) + pulse * 0.08;

    if (active || hub) {
      const glowRadius = active ? Math.max(34, radius * 9.5) : radius * 5.2;
      ctx.globalAlpha = active ? 0.72 : 0.16;
      const glow = ctx.createRadialGradient(x, y, 0, x, y, glowRadius);
      if (active) glow.addColorStop(0, '#ffffff');
      glow.addColorStop(active ? 0.08 : 0, node.color);
      glow.addColorStop(active ? 0.12 : 0.22, node.color);
      glow.addColorStop(1, 'rgba(2, 4, 13, 0)');
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(x, y, glowRadius, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.globalAlpha = active ? 1 : (hub ? 0.92 : 0.72);
    if (active || hub) {
      const core = ctx.createRadialGradient(x - radius * 0.28, y - radius * 0.32, 0, x, y, radius);
      core.addColorStop(0, '#ffffff');
      core.addColorStop(active ? 0.34 : 0.16, node.color);
      core.addColorStop(1, `${node.color}bb`);
      ctx.fillStyle = core;
    } else {
      ctx.fillStyle = node.color;
    }
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();

    if (active) {
      ctx.globalAlpha = 1;
      ctx.fillStyle = '#fff8dc';
      ctx.textAlign = 'center';
      ctx.font = '700 13px system-ui, sans-serif';
      ctx.shadowColor = activeColor;
      ctx.shadowBlur = 8;
      ctx.fillText(node.label.slice(0, 24), x, y + 4);
      ctx.shadowBlur = 0;
      ctx.textAlign = 'start';
    }
  });
  ctx.globalCompositeOperation = 'source-over';

  ctx.restore();
  ctx.globalAlpha = 1;
  frameId = requestAnimationFrame(draw);
};

const screenToWorld = (event: PointerEvent | WheelEvent) => {
  const rect = canvasRef.value?.getBoundingClientRect();
  const screenX = event.clientX - (rect?.left || 0);
  const screenY = event.clientY - (rect?.top || 0);
  const scaledX = (screenX - pan.x) / zoom.value;
  const scaledY = (screenY - pan.y) / zoom.value;
  return {
    x: scaledX,
    y: scaledY,
    screenX,
    screenY,
  };
};

const findNode = (event: PointerEvent) => {
  const point = screenToWorld(event);
  for (let i = nodes.value.length - 1; i >= 0; i -= 1) {
    const node = nodes.value[i];
    const projected = projectGraphNode(node);
    const dx = point.x - projected.x;
    const dy = point.y - projected.y;
    if (Math.sqrt(dx * dx + dy * dy) <= node.radius + 6) {
      return node;
    }
  }
  return null;
};

const handlePointerDown = (event: PointerEvent) => {
  drag = { active: true, moved: false, x: event.clientX, y: event.clientY };
  canvasRef.value?.setPointerCapture(event.pointerId);
};

const handlePointerMove = (event: PointerEvent) => {
  const rect = canvasRef.value?.getBoundingClientRect();
  if (rect) {
    pointer.targetX = ((event.clientX - rect.left) / rect.width - 0.5) * 2;
    pointer.targetY = ((event.clientY - rect.top) / rect.height - 0.5) * 2;
  }
  if (drag.active) {
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    pan.x += dx;
    pan.y += dy;
    drag.x = event.clientX;
    drag.y = event.clientY;
    return;
  }
  hoveredNode.value = findNode(event);
};

const handlePointerUp = (event: PointerEvent) => {
  const target = findNode(event);
  if (!drag.moved) {
    if (target) {
      focusNode(target);
    } else {
      selectedNode.value = null;
    }
  }
  drag.active = false;
  canvasRef.value?.releasePointerCapture(event.pointerId);
};

const handleWheel = (event: WheelEvent) => {
  event.preventDefault();
  const point = screenToWorld(event);
  const nextZoom = Math.min(2.8, Math.max(0.35, zoom.value * (event.deltaY > 0 ? 0.9 : 1.1)));
  pan.x = point.screenX - point.x * nextZoom;
  pan.y = point.screenY - point.y * nextZoom;
  zoom.value = nextZoom;
};

const centerNode = (node: GraphNode) => {
  selectedNode.value = node;
  updateProjectionCamera();
  const centerX = canvasWidth / 2;
  const centerY = canvasHeight / 2;
  const projected = projectGraphNode(node);
  pan.x = centerX - projected.x * zoom.value;
  pan.y = centerY - projected.y * zoom.value;
};

const focusNode = (node: GraphNode) => {
  zoom.value = Math.max(1.55, zoom.value);
  centerNode(node);
};

const focusSearch = () => {
  const query = searchText.value.trim().toLowerCase();
  if (!query) return;
  const node = nodes.value.find((item) => item.label.toLowerCase().includes(query));
  if (node) focusNode(node);
};

const zoomBy = (factor: number) => {
  zoom.value = Math.min(2.8, Math.max(0.35, zoom.value * factor));
};

const resetView = () => {
  zoom.value = 1;
  pan = { x: 0, y: 0 };
  selectedNode.value = null;
};

const syncFullscreenState = async () => {
  isFullscreen.value = document.fullscreenElement === shellRef.value;
  await nextTick();
  resizeCanvas();
};

const toggleFullscreen = async () => {
  const shell = shellRef.value;
  if (!shell) return;

  try {
    if (document.fullscreenElement === shell) {
      await document.exitFullscreen();
    } else {
      await shell.requestFullscreen();
    }
  } catch (e: any) {
    errorMsg.value = e?.message || '无法进入全屏模式';
  }
};

watch(() => props.kbId, () => loadGraph());
watch([nodeLimit, edgeLimit], () => loadGraph());

onMounted(async () => {
  reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  await nextTick();
  resizeObserver = new ResizeObserver(resizeCanvas);
  if (wrapRef.value) resizeObserver.observe(wrapRef.value);
  document.addEventListener('fullscreenchange', syncFullscreenState);
  await loadGraph();
  frameId = requestAnimationFrame(draw);
});

onUnmounted(() => {
  if (frameId) cancelAnimationFrame(frameId);
  resizeObserver?.disconnect();
  document.removeEventListener('fullscreenchange', syncFullscreenState);
});
</script>

<template>
  <section
    ref="shellRef"
    class="bg-surface border border-border-subtle rounded-2xl shadow-sm overflow-hidden"
    :class="[ fullPage ? 'h-[calc(100vh-150px)] min-h-[680px] flex flex-col' : '',
      isFullscreen ? 'fixed inset-0 z-[100] h-screen min-h-0 rounded-none border-0 flex flex-col' : ''
    ]"
  >
    <div class="px-5 py-4 border-b border-border-subtle flex flex-col xl:flex-row xl:items-center justify-between gap-4 shrink-0">
      <div>
        <div class="text-xs font-bold text-text-tertiary uppercase tracking-wider">知识图谱星云</div>
        <h3 class="text-lg font-extrabold text-text-primary mt-1">{{ kbName || '当前知识库' }}</h3>
        <div class="flex items-center gap-2 mt-1">
          <p class="text-xs text-text-secondary">{{ totalLabel }}</p>
          <span
            v-if="graph?.cache_hit"
            class="px-1.5 py-0.5 rounded-md bg-emerald-50 dark:bg-emerald-950/50 text-[10px] font-bold text-emerald-600 dark:text-emerald-400"
          >Redis 缓存</span>
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <div class="relative">
          <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-tertiary" />
          <input
            v-model="searchText"
            type="text"
            placeholder="定位实体"
            class="w-44 pl-9 pr-3 py-2 bg-surface-sunken border border-border-subtle rounded-xl text-xs outline-none focus:ring-2 focus:ring-accent/30"
            @keyup.enter="focusSearch"
          />
        </div>
        <label class="flex items-center gap-1.5 px-2.5 py-1.5 bg-surface-sunken border border-border-subtle rounded-xl text-[11px] text-text-secondary">
          <input
            v-model.number.lazy="nodeLimit"
            type="number"
            min="20"
            max="5000"
            step="100"
            class="w-16 bg-transparent text-right font-bold text-text-secondary outline-none"
            @change="nodeLimit = Math.min(5000, Math.max(20, Number(nodeLimit) || 5000))"
          />
          节点
        </label>
        <label class="flex items-center gap-1.5 px-2.5 py-1.5 bg-surface-sunken border border-border-subtle rounded-xl text-[11px] text-text-secondary">
          <input
            v-model.number.lazy="edgeLimit"
            type="number"
            min="0"
            max="20000"
            step="500"
            class="w-20 bg-transparent text-right font-bold text-text-secondary outline-none"
            @change="edgeLimit = Math.min(20000, Math.max(0, Number(edgeLimit) || 0))"
          />
          关系
        </label>
        <button @click="loadGraph(true)" :disabled="loading" class="p-2 border border-border-subtle rounded-xl text-text-secondary hover:text-accent disabled:opacity-50" title="跳过缓存并刷新图谱">
          <Loader2 v-if="loading" class="w-4 h-4 animate-spin" />
          <RefreshCw v-else class="w-4 h-4" />
        </button>
        <button @click="zoomBy(1.15)" class="p-2 border border-border-subtle rounded-xl text-text-secondary hover:text-accent" title="放大">
          <ZoomIn class="w-4 h-4" />
        </button>
        <button @click="zoomBy(0.85)" class="p-2 border border-border-subtle rounded-xl text-text-secondary hover:text-accent" title="缩小">
          <ZoomOut class="w-4 h-4" />
        </button>
        <button @click="resetView" class="p-2 border border-border-subtle rounded-xl text-text-secondary hover:text-accent" title="重置视图">
          <Maximize2 class="w-4 h-4" />
        </button>
        <button
          @click="toggleFullscreen"
          class="p-2 border border-border-subtle rounded-xl text-text-secondary hover:text-accent"
          :title="isFullscreen ? '退出全屏' : '进入全屏'"
        >
          <Minimize2 v-if="isFullscreen" class="w-4 h-4" />
          <Maximize2 v-else class="w-4 h-4" />
        </button>
      </div>
    </div>

    <div
      class="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_260px]"
      :class="fullPage || isFullscreen ? 'flex-1 min-h-0' : ''"
    >
      <div
        ref="wrapRef"
        class="relative bg-surface"
        :class="fullPage || isFullscreen ? 'h-full min-h-[560px]' : 'h-[620px] min-h-[440px]'"
      >
        <div
          class="absolute inset-0 z-[1] pointer-events-none"
          style="background: radial-gradient(ellipse at center, transparent 42%, rgba(1, 2, 9, 0.58) 100%);"
        ></div>
        <canvas
          ref="canvasRef"
          class="absolute inset-0 z-[2] block cursor-grab active:cursor-grabbing"
          @pointerdown="handlePointerDown"
          @pointermove="handlePointerMove"
          @pointerup="handlePointerUp"
          @pointerleave="hoveredNode = null"
          @wheel="handleWheel"
        ></canvas>

        <div v-if="loading" class="absolute inset-0 z-20 flex flex-col items-center justify-center bg-surface text-text-tertiary">
          <Loader2 class="w-8 h-8 animate-spin mb-3 text-sky-300" />
          <span class="text-sm font-bold">正在生成星云视图</span>
        </div>

        <div v-if="errorMsg" class="absolute z-20 inset-x-6 top-6 p-4 rounded-xl bg-red-950/80 border border-red-500/30 text-red-100 text-sm">
          {{ errorMsg }}
        </div>

        <div v-if="graph?.truncated" class="absolute z-20 left-4 bottom-4 px-3 py-2 rounded-xl bg-surface border border-border-strong text-[11px] text-text-tertiary backdrop-blur">
          当前显示为图谱切片，可调整节点和关系数量查看更多。
        </div>
      </div>

      <aside class="p-5 bg-surface-sunken border-t xl:border-t-0 xl:border-l border-border-subtle overflow-y-auto">
        <div class="space-y-4">
          <div>
            <div class="text-xs font-bold text-text-tertiary uppercase tracking-wider mb-2">实体类型</div>
            <div class="space-y-2">
              <div v-for="item in typeLegend" :key="item.type" class="flex items-center justify-between text-xs">
                <div class="flex items-center min-w-0">
                  <span class="w-2.5 h-2.5 rounded-full mr-2 shrink-0" :style="{ background: item.color }"></span>
                  <span class="truncate text-text-secondary">{{ item.type }}</span>
                </div>
                <span class="font-bold text-text-tertiary ml-3">{{ item.count }}</span>
              </div>
            </div>
          </div>

          <div class="h-px bg-surface-sunken"></div>

          <div>
            <div class="text-xs font-bold text-text-tertiary uppercase tracking-wider mb-2">节点详情</div>
            <div v-if="detailNode" class="space-y-3">
              <div>
                <div class="text-sm font-extrabold text-text-primary break-words">{{ detailNode.label }}</div>
                <div class="text-xs text-text-tertiary mt-1">{{ detailNode.entity_type }} · {{ detailNode.degree }} 条关系</div>
              </div>
              <p
                v-if="detailNode.description"
                class="text-xs leading-relaxed text-text-secondary"
                :class="selectedNode ? 'max-h-48 overflow-y-auto' : 'line-clamp-4'"
              >
                {{ detailNode.description }}
              </p>
              <div v-if="detailNode.file_path" class="text-[11px] text-text-tertiary break-words max-h-24 overflow-y-auto">
                来源：{{ detailNode.file_path }}
              </div>
              <div v-else-if="detailNode.source_id" class="text-[11px] text-text-tertiary break-words">
                文档：{{ detailNode.source_id }}
              </div>
            </div>
            <div v-else class="text-xs leading-relaxed text-text-tertiary">
              点击节点可固定查看详情，拖拽平移，滚轮缩放。
            </div>
          </div>
        </div>
      </aside>
    </div>
  </section>
</template>
