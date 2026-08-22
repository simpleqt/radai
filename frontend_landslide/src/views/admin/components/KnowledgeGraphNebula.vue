<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue';
import {
  Loader2, Maximize2, Minimize2, RefreshCw, RotateCcw, Search, Sparkles, X, ZoomIn, ZoomOut,
} from 'lucide-vue-next';
import { KBDetailAPI, type KnowledgeGraphResponse } from '@/api/kb';

const props = defineProps<{
  kbId: string;
  kbName?: string;
  fullPage?: boolean;
}>();

type ApiNode = KnowledgeGraphResponse['nodes'][number];

interface StarNode extends ApiNode {
  x: number;
  y: number;
  z: number;
  px: number;
  py: number;
  ps: number;
  visible: boolean;
  radius: number;
  color: string;
  coreColor: string;
  phase: number;
  twinkle: number;
  hub: boolean;
}

interface StarEdge {
  s: StarNode;
  t: StarNode;
  bend: number;
  phase: number;
}

interface LegendItem {
  type: string;
  color: string;
  count: number;
}

/* 诗云式星色:按实体类型出现频次依序取色,最常见类型是暖金主星 */
const STAR_PALETTE = [
  '#ffd991', '#8fd0ff', '#ffa8d2', '#c4b5fd', '#a7f3d0',
  '#fde68a', '#99f6e4', '#f5d0fe', '#fca5a5', '#bae6fd',
];

const NEBULA_CLOUDS = [
  { x: 0.26, y: 0.32, r: 0.55, rgb: '109,66,224', alpha: 0.14 },
  { x: 0.74, y: 0.22, r: 0.48, rgb: '56,116,246', alpha: 0.11 },
  { x: 0.64, y: 0.78, r: 0.62, rgb: '20,150,180', alpha: 0.08 },
  { x: 0.16, y: 0.80, r: 0.44, rgb: '196,64,166', alpha: 0.07 },
  { x: 0.50, y: 0.50, r: 0.95, rgb: '28,36,86', alpha: 0.16 },
];

/* 背景画布四周多渲染一圈,供镜头视差平移采样 */
const BG_PAD = 96;
/* 视口剔除的屏幕外余量(px) */
const CULL_PAD = 90;

/*
 * 渲染架构(性能关键,改动前先读):
 * 三个叠放的 DOM canvas,各自独立刷新,浏览器合成器负责叠加——
 *   bg    背景星云/星场:仅 resize 时重绘,视差经 CSS transform 由合成器执行;
 *   graph 基线边+星体+标签:隔 6~12 帧全量重绘,中间帧用 CSS transform
 *         跟随镜头(平移缩放由 GPU 合成,零绘制成本),重绘时归零 transform;
 *   fx    光子/悬停高亮等逐帧动画:元素恒定少量,每帧重绘 <1ms。
 * 刻意避免:离屏 canvas 每帧 drawImage 回主画布(触发 GPU/CPU 位图迁移,
 * 实测单次重绘可达 300ms+),全屏 backdrop-filter(逐帧回读+模糊),
 * 每帧重建径向渐变,以及逐节点精灵 drawImage(星等分级只给大星发光精灵)。
 */
const bgCanvasRef = ref<HTMLCanvasElement | null>(null);
const graphCanvasRef = ref<HTMLCanvasElement | null>(null);
const fxCanvasRef = ref<HTMLCanvasElement | null>(null);
const wrapRef = ref<HTMLDivElement | null>(null);
const shellRef = ref<HTMLElement | null>(null);

const loading = ref(false);
const errorMsg = ref('');
const graphMeta = ref<Pick<KnowledgeGraphResponse,
  'total_nodes' | 'total_edges' | 'sampled_nodes' | 'sampled_edges' | 'truncated' | 'cache_hit'> | null>(null);
const legend = ref<LegendItem[]>([]);
const legendOverflow = ref(0);
const starCount = ref(0);
const hoveredNode = shallowRef<StarNode | null>(null);
const selectedNode = shallowRef<StarNode | null>(null);
const activeType = ref<string | null>(null);
const nodeLimit = ref(5000);
const edgeLimit = ref(10000);
const searchText = ref('');
const searchMiss = ref(false);
const zoomLabel = ref('1.00');
const dragging = ref(false);
const isFullscreen = ref(false);

/* 渲染状态保持在响应式系统之外:5000 节点 × 60fps 的读写不应过 Vue 代理 */
let starNodes: StarNode[] = [];
let starEdges: StarEdge[] = [];
let drawOrder: StarNode[] = [];
let hubNodes: StarNode[] = [];
let neighborIds = new Set<string>();
const spriteCache = new Map<string, HTMLCanvasElement>();

let frameId = 0;
let resizeObserver: ResizeObserver | null = null;
let canvasWidth = 900;
let canvasHeight = 560;
let reduceMotion = false;
let searchMissTimer = 0;

/* graph 层重绘时的视图快照;中间帧据此计算 CSS 跟随变换 */
const graphView = { zoom: 1, x: 0, y: 0 };
let graphFrame = -999;
let frameIndex = 0;
let fxDirty = false;
let fxForce = false;
/* 帧耗时滑动平均,驱动自适应降级 */
let frameCostEma = 8;
/* 当前活动实体(悬停/选中)的关联边缓存:只在活动实体变化时扫一次全边表 */
let activeEdges: StarEdge[] = [];
let activeEdgesFor = '';
const ACTIVE_EDGE_CAP = 120;

const view = { zoom: 1, x: 0, y: 0 };
const viewTarget = { zoom: 1, x: 0, y: 0 };
const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
const hoverPoint = { x: 0, y: 0, fresh: false };
let drag = { active: false, moved: false, x: 0, y: 0 };
let rotation = 0;
let rotationSpeed = 0;
let lastFrameTime = 0;
let labelThreshold = Number.POSITIVE_INFINITY;

let yawCos = 1;
let yawSin = 0;
let pitchCos = 1;
let pitchSin = 0;
let cameraDistance = 1200;

const totalLabel = computed(() => {
  const meta = graphMeta.value;
  if (!meta) return '暂无图谱数据';
  return `${meta.sampled_nodes}/${meta.total_nodes || meta.sampled_nodes} 节点 · ${meta.sampled_edges}/${meta.total_edges || meta.sampled_edges} 关系`;
});

const detailNode = computed(() => hoveredNode.value || selectedNode.value);

/*
 * 呈现质量自适应:JS 帧成本探测不到合成器瓶颈(无 GPU 的远程桌面/虚拟机里
 * 软件合成是天花板),因此监测 rAF 帧距;持续掉帧就降低渲染分辨率,
 * 把软件合成的像素量成倍砍掉。有 GPU 的机器永远停留在最高档。
 */
const QUALITY_DPR = [1.5, 1, 0.75];
let quality = 0;
let qualityHold = 0;
let frameGapEma = 16;

/* 高分屏渲染上限:发光星云对超采样不敏感,1.5x 之上只烧 GPU */
const renderDpr = () => Math.min(window.devicePixelRatio || 1, QUALITY_DPR[quality]);

function hashText(text: string) {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
  }
  return hash;
}

const hexToRgb = (hex: string): [number, number, number] => {
  const value = hex.replace('#', '');
  return [
    parseInt(value.slice(0, 2), 16),
    parseInt(value.slice(2, 4), 16),
    parseInt(value.slice(4, 6), 16),
  ];
};

const mixWhite = (hex: string, ratio: number) => {
  const [r, g, b] = hexToRgb(hex);
  const mix = (channel: number) => Math.round(channel + (255 - channel) * ratio);
  return `rgb(${mix(r)},${mix(g)},${mix(b)})`;
};

/* 预渲染星辉精灵:白核 → 类型色 → 透明 */
const starSprite = (color: string) => {
  const cached = spriteCache.get(color);
  if (cached) return cached;
  const size = 64;
  const sprite = document.createElement('canvas');
  sprite.width = size;
  sprite.height = size;
  const ctx = sprite.getContext('2d')!;
  const [r, g, b] = hexToRgb(color);
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  gradient.addColorStop(0, 'rgba(255,255,255,0.95)');
  gradient.addColorStop(0.16, `rgba(${r},${g},${b},0.9)`);
  gradient.addColorStop(0.38, `rgba(${r},${g},${b},0.32)`);
  gradient.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);
  spriteCache.set(color, sprite);
  return sprite;
};

/* 背景画布只在尺寸变化时重绘:渐变星云 + 静态星场 */
const renderBgCanvas = () => {
  const canvas = bgCanvasRef.value;
  if (!canvas) return;
  const dpr = renderDpr();
  const width = canvasWidth + BG_PAD * 2;
  const height = canvasHeight + BG_PAD * 2;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext('2d')!;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  ctx.fillStyle = '#04050c';
  ctx.fillRect(0, 0, width, height);

  NEBULA_CLOUDS.forEach((cloud) => {
    const cx = cloud.x * width;
    const cy = cloud.y * height;
    const radius = cloud.r * Math.max(width, height);
    const gradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
    gradient.addColorStop(0, `rgba(${cloud.rgb},${cloud.alpha})`);
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, width, height);
  });

  const count = Math.max(140, Math.min(420, Math.floor((width * height) / 4200)));
  ctx.fillStyle = '#dbe4ff';
  for (let i = 0; i < count; i += 1) {
    const seed = hashText(`bg:${i}`);
    const x = ((seed % 10000) / 10000) * width;
    const y = (((seed >> 7) % 10000) / 10000) * height;
    const size = 0.35 + ((seed >> 3) % 100) / 100;
    ctx.globalAlpha = 0.1 + ((seed >> 5) % 100) / 300;
    ctx.fillRect(x, y, size, size);
  }
  ctx.globalAlpha = 1;
};

const sizeLayerCanvas = (canvas: HTMLCanvasElement | null) => {
  if (!canvas) return;
  const dpr = renderDpr();
  canvas.width = Math.floor(canvasWidth * dpr);
  canvas.height = Math.floor(canvasHeight * dpr);
  canvas.style.width = `${canvasWidth}px`;
  canvas.style.height = `${canvasHeight}px`;
};

const resizeCanvas = () => {
  const wrap = wrapRef.value;
  if (!wrap) return;
  const rect = wrap.getBoundingClientRect();
  canvasWidth = Math.max(320, Math.floor(rect.width));
  canvasHeight = Math.max(400, Math.floor(rect.height));
  sizeLayerCanvas(graphCanvasRef.value);
  sizeLayerCanvas(fxCanvasRef.value);
  renderBgCanvas();
  graphFrame = -999;
  if (starNodes.length) layoutGraph(false);
};

/* 旋臂星系布局:度数高的实体沉入星核,普通实体沿三条旋臂散开成盘 */
const layoutGraph = (resetView = true, data?: KnowledgeGraphResponse) => {
  const source = data || null;
  const rawNodes: ApiNode[] = source ? source.nodes : starNodes;
  const rawEdges = source ? source.edges : null;

  const sorted = [...rawNodes].sort((a, b) => (b.degree || 0) - (a.degree || 0));
  const maxDegree = Math.max(1, ...sorted.map((node) => node.degree || 0));
  const centerX = canvasWidth / 2;
  const centerY = canvasHeight / 2;
  const spreadX = canvasWidth * 0.5;
  const spreadY = canvasHeight * 0.46;
  const depthSpan = Math.min(canvasWidth, canvasHeight);
  const densityScale = sorted.length > 2400 ? 0.6 : sorted.length > 1200 ? 0.75 : 1;
  labelThreshold = sorted[Math.min(13, Math.max(0, sorted.length - 1))]?.degree ?? Number.POSITIVE_INFINITY;

  const typeCounts = new Map<string, number>();
  sorted.forEach((node) => {
    const key = node.entity_type || 'unknown';
    typeCounts.set(key, (typeCounts.get(key) || 0) + 1);
  });
  const orderedTypes = [...typeCounts.entries()].sort((a, b) => b[1] - a[1]);
  const typeColor = new Map<string, string>();
  orderedTypes.forEach(([type], index) => {
    typeColor.set(type, STAR_PALETTE[index % STAR_PALETTE.length]);
  });
  legend.value = orderedTypes.slice(0, 8).map(([type, count]) => ({
    type,
    count,
    color: typeColor.get(type)!,
  }));
  legendOverflow.value = Math.max(0, orderedTypes.length - 8);

  starNodes = sorted.map((node, index) => {
    const degreeRatio = (node.degree || 0) / maxDegree;
    const radial = 0.03 + Math.pow((index + 1) / Math.max(1, sorted.length), 0.68) * 0.95;
    const arm = hashText(`${node.entity_type || node.id}:arm`) % 3;
    const armJitter = ((hashText(`${node.id}:jitter`) % 1000) / 1000 - 0.5) * 0.52;
    const angle = arm * ((Math.PI * 2) / 3) + radial * 4.6 + armJitter;
    const thickness = ((hashText(`${node.id}:thick`) % 1000) / 1000 - 0.5) * Math.max(10, radial * 46);
    const color = typeColor.get(node.entity_type || 'unknown') || STAR_PALETTE[hashText(node.entity_type || 'unknown') % STAR_PALETTE.length];

    return {
      ...node,
      x: centerX + Math.cos(angle) * radial * spreadX + thickness,
      y: centerY + Math.sin(angle) * radial * spreadY + thickness * 0.36,
      z: ((hashText(`${node.id}:depth`) % 1000) / 1000 - 0.5) * depthSpan * (0.14 + radial * 0.5),
      px: centerX,
      py: centerY,
      ps: 1,
      visible: true,
      radius: Math.max(0.7, Math.min(5.4, 0.75 + Math.sqrt(node.degree || 1) * 0.44 + degreeRatio * 2.6) * densityScale),
      color,
      coreColor: mixWhite(color, 0.62),
      phase: (hashText(`${node.id}:phase`) % 628) / 100,
      twinkle: 0.7 + ((hashText(`${node.id}:tw`) % 100) / 100) * 1.1,
      hub: (node.degree || 0) >= labelThreshold,
    };
  });
  starCount.value = starNodes.length;
  drawOrder = [...starNodes];
  hubNodes = starNodes.filter((node) => node.hub);

  const lookup = new Map(starNodes.map((node) => [node.id, node]));
  if (rawEdges) {
    starEdges = [];
    rawEdges.forEach((edge) => {
      const s = lookup.get(edge.source);
      const t = lookup.get(edge.target);
      if (!s || !t) return;
      const seed = hashText(`${edge.source}»${edge.target}`);
      starEdges.push({
        s,
        t,
        bend: ((seed % 200) - 100) / 100,
        phase: (seed % 628) / 100,
      });
    });
  } else {
    starEdges = starEdges
      .map((edge) => ({ ...edge, s: lookup.get(edge.s.id)!, t: lookup.get(edge.t.id)! }))
      .filter((edge) => edge.s && edge.t);
  }

  /* 重建后旧节点对象失效,按 id 找回选中/悬停引用,关联边缓存同步重建 */
  if (selectedNode.value) selectedNode.value = lookup.get(selectedNode.value.id) || null;
  if (hoveredNode.value) hoveredNode.value = lookup.get(hoveredNode.value.id) || null;
  rebuildNeighborSet();
  activeEdgesFor = '\u0000stale';
  rebuildActiveEdges();
  graphFrame = -999;

  if (resetView) {
    view.zoom = 1;
    view.x = 0;
    view.y = 0;
    viewTarget.zoom = 1;
    viewTarget.x = 0;
    viewTarget.y = 0;
    selectedNode.value = null;
    hoveredNode.value = null;
    activeType.value = null;
    rebuildNeighborSet();
  }
};

const rebuildNeighborSet = () => {
  neighborIds = new Set();
  const pinned = selectedNode.value;
  if (!pinned) return;
  starEdges.forEach((edge) => {
    if (edge.s.id === pinned.id) neighborIds.add(edge.t.id);
    else if (edge.t.id === pinned.id) neighborIds.add(edge.s.id);
  });
};

/* 活动实体变化时缓存其关联边(上限截断),避免每帧 O(E) 扫描 */
const rebuildActiveEdges = () => {
  const activeId = (hoveredNode.value || selectedNode.value)?.id || '';
  if (activeId === activeEdgesFor) return;
  activeEdgesFor = activeId;
  activeEdges = [];
  if (!activeId) return;
  for (let i = 0; i < starEdges.length && activeEdges.length < ACTIVE_EDGE_CAP; i += 1) {
    const edge = starEdges[i];
    if (edge.s.id === activeId || edge.t.id === activeId) activeEdges.push(edge);
  }
};

/* 选中改变全场明暗(dim),需要重绘大图层;悬停只影响 fx 高亮层,
   绝不触发 5000 节点全量重绘——这是点击/悬停响应流畅的关键 */
watch(selectedNode, () => {
  rebuildNeighborSet();
  rebuildActiveEdges();
  graphFrame = -999;
  fxForce = true;
});
watch(hoveredNode, () => {
  rebuildActiveEdges();
  fxForce = true;
});
watch(activeType, () => {
  graphFrame = -999;
});

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
    graphMeta.value = {
      total_nodes: data.total_nodes,
      total_edges: data.total_edges,
      sampled_nodes: data.sampled_nodes,
      sampled_edges: data.sampled_edges,
      truncated: data.truncated,
      cache_hit: data.cache_hit,
    };
    await nextTick();
    resizeCanvas();
    layoutGraph(true, data);
  } catch (e: any) {
    errorMsg.value = e?.message || '知识图谱加载失败';
    graphMeta.value = null;
    starNodes = [];
    starEdges = [];
    drawOrder = [];
    hubNodes = [];
    starCount.value = 0;
    legend.value = [];
    legendOverflow.value = 0;
  } finally {
    loading.value = false;
  }
};

const projectNode = (node: StarNode) => {
  const centerX = canvasWidth / 2;
  const centerY = canvasHeight / 2;
  const localX = node.x - centerX;
  const localY = node.y - centerY;
  const rotatedX = localX * yawCos + node.z * yawSin;
  const yawDepth = -localX * yawSin + node.z * yawCos;
  const rotatedY = localY * pitchCos - yawDepth * pitchSin;
  const depth = localY * pitchSin + yawDepth * pitchCos;
  const perspective = Math.min(1.85, Math.max(0.5, cameraDistance / (cameraDistance + depth)));
  return {
    x: centerX + rotatedX * perspective,
    y: centerY + rotatedY * perspective,
    scale: perspective,
  };
};

const updateCamera = () => {
  const yaw = rotation + pointer.x * 0.24;
  const pitch = 0.3 + pointer.y * 0.14;
  yawCos = Math.cos(yaw);
  yawSin = Math.sin(yaw);
  pitchCos = Math.cos(pitch);
  pitchSin = Math.sin(pitch);
  cameraDistance = Math.max(canvasWidth, canvasHeight) * 1.2;
};

const quadPoint = (p0: number, c: number, p1: number, t: number) => {
  const inv = 1 - t;
  return inv * inv * p0 + 2 * inv * t * c + t * t * p1;
};

const nodeDimFactor = (node: StarNode, pinnedId: string | undefined) => {
  let factor = 1;
  if (activeType.value && (node.entity_type || 'unknown') !== activeType.value) factor *= 0.1;
  if (pinnedId && node.id !== pinnedId) {
    factor *= neighborIds.has(node.id) ? 0.95 : 0.16;
  }
  return factor;
};

/* graph 层刷新间隔:自转约 0.66°/s,10 帧间隔的位移仍是亚像素级;
   闪烁动画的有效刷新率随之降到 6-10hz,慢波闪烁视觉无损 */
const graphInterval = () => {
  if (reduceMotion) return 20;
  if (frameCostEma > 26) return 12;
  if (frameCostEma > 15) return 9;
  return 6;
};

/* graph 层全量重绘:基线边 + 高亮光束 + 星等分级星体 + 标签 */
const renderGraphLayer = (time: number) => {
  const canvas = graphCanvasRef.value;
  if (!canvas) return;
  const ctx = canvas.getContext('2d')!;
  const dpr = renderDpr();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvasWidth, canvasHeight);

  ctx.save();
  ctx.translate(view.x, view.y);
  ctx.scale(view.zoom, view.zoom);
  ctx.globalCompositeOperation = 'lighter';

  /* 大图层只依赖选中(dim 全场明暗)与类型筛选;悬停高亮全部在 fx 层 */
  const pinned = selectedNode.value;
  const pinnedId = pinned?.id;

  /* 基线边:单路径批量描边(实测万条曲线 ~1ms,不是瓶颈) */
  if (starEdges.length) {
    const baseAlpha = Math.min(0.05, 300 / Math.max(1, starEdges.length)) * (activeType.value ? 0.45 : 1);
    ctx.globalAlpha = baseAlpha;
    ctx.strokeStyle = '#8ea7e8';
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    for (let i = 0; i < starEdges.length; i += 1) {
      const edge = starEdges[i];
      if (!edge.s.visible && !edge.t.visible) continue;
      const dx = edge.t.px - edge.s.px;
      const dy = edge.t.py - edge.s.py;
      /* 曼哈顿距离近似跨度,省一次 sqrt */
      const span = Math.abs(dx) + Math.abs(dy) || 1;
      const bend = edge.bend * Math.min(46, span * 0.14);
      const cx = (edge.s.px + edge.t.px) / 2 - (dy / span) * bend;
      const cy = (edge.s.py + edge.t.py) / 2 + (dx / span) * bend;
      ctx.moveTo(edge.s.px, edge.s.py);
      ctx.quadraticCurveTo(cx, cy, edge.t.px, edge.t.py);
    }
    ctx.stroke();
  }

  /* 星体深度排序:层刷新时排一次即可 */
  drawOrder.sort((a, b) => a.ps - b.ps);
  const twinkleAmp = reduceMotion || frameCostEma > 30 ? 0 : 0.22;
  const TAU = Math.PI * 2;
  /* 星等分级:drawImage 的单次调用开销是 fillRect 的数十倍,
     只有大星/枢纽/活动星享受发光精灵,中星用圆核,海量远星用方点 */
  for (let i = 0; i < drawOrder.length; i += 1) {
    const node = drawOrder[i];
    const isPinned = node.id === pinnedId;
    if (!node.visible && !isPinned) continue;
    const depthFade = Math.min(1, Math.max(0.16, (node.ps - 0.5) * 1.35));
    const dim = nodeDimFactor(node, pinnedId);
    const size = node.radius * node.ps;
    const isMajor = isPinned || node.hub || size > 2.2;
    const twinkle = isMajor && twinkleAmp > 0
      ? 1 - twinkleAmp + twinkleAmp * Math.sin(time * 0.0012 * node.twinkle + node.phase)
      : 1;
    const alpha = depthFade * twinkle * dim;
    if (alpha < 0.02) continue;

    if (isMajor) {
      const radius = size + (isPinned ? 1.6 : 0);
      const glowSize = radius * (isPinned ? 11 : node.hub ? 7.5 : 5.5);
      ctx.globalAlpha = Math.min(1, alpha * (isPinned ? 1 : node.hub ? 0.8 : 0.66));
      ctx.drawImage(starSprite(node.color), node.px - glowSize / 2, node.py - glowSize / 2, glowSize, glowSize);

      ctx.globalAlpha = Math.min(1, alpha * 1.05);
      ctx.fillStyle = isPinned ? '#ffffff' : node.coreColor;
      ctx.beginPath();
      ctx.arc(node.px, node.py, Math.max(0.8, radius * 0.6), 0, TAU);
      ctx.fill();
    } else if (size > 1.15) {
      ctx.globalAlpha = Math.min(1, alpha * 0.92);
      ctx.fillStyle = node.coreColor;
      ctx.beginPath();
      ctx.arc(node.px, node.py, size * 0.75, 0, TAU);
      ctx.fill();
    } else {
      ctx.globalAlpha = Math.min(1, alpha * 0.8);
      ctx.fillStyle = node.coreColor;
      const dot = Math.max(1, size * 1.4);
      ctx.fillRect(node.px - dot / 2, node.py - dot / 2, dot, dot);
    }
  }

  /* 星名:枢纽实体常亮(度数优先 + 防碰撞),选中/悬停实体高亮 */
  ctx.globalCompositeOperation = 'source-over';
  ctx.textAlign = 'center';
  const placedLabels: { x: number; y: number }[] = [];
  const collideX = 118 / view.zoom;
  const collideY = 26 / view.zoom;
  const collides = (x: number, y: number) =>
    placedLabels.some((p) => Math.abs(p.x - x) < collideX && Math.abs(p.y - y) < collideY);

  if (pinned) {
    const label = pinned.label.length > 20 ? `${pinned.label.slice(0, 20)}…` : pinned.label;
    ctx.globalAlpha = 1;
    ctx.font = '700 13px system-ui, sans-serif';
    ctx.fillStyle = '#fdf6e3';
    ctx.shadowColor = pinned.color;
    ctx.shadowBlur = 10;
    ctx.fillText(label, pinned.px, pinned.py - pinned.radius * pinned.ps - 10);
    ctx.shadowBlur = 0;
    placedLabels.push({ x: pinned.px, y: pinned.py });
  }

  ctx.font = '600 10.5px system-ui, sans-serif';
  ctx.fillStyle = '#c7d2ee';
  for (let i = 0; i < hubNodes.length; i += 1) {
    const node = hubNodes[i];
    if (node.id === pinnedId || !node.visible) continue;
    const dim = nodeDimFactor(node, pinnedId);
    if (dim < 0.5) continue;
    if (collides(node.px, node.py)) continue;
    const depthFade = Math.min(1, Math.max(0.16, (node.ps - 0.5) * 1.35));
    const label = node.label.length > 20 ? `${node.label.slice(0, 20)}…` : node.label;
    ctx.globalAlpha = 0.6 * depthFade * dim;
    ctx.fillText(label, node.px, node.py - node.radius * node.ps - 7);
    placedLabels.push({ x: node.px, y: node.py });
  }
  ctx.textAlign = 'start';
  ctx.restore();

  graphView.zoom = view.zoom;
  graphView.x = view.x;
  graphView.y = view.y;
  graphFrame = frameIndex;
  canvas.style.transform = '';
};

/* fx 高亮层:光束/光子/悬停发光/脉动环。元素恒定少量(≤150 个图元),
   悬停与点击的全部视觉反馈都在这里,大图层保持不动 */
const renderFxLayer = (time: number) => {
  const canvas = fxCanvasRef.value;
  const ctx = canvas?.getContext('2d');
  if (!canvas || !ctx) return;
  const dpr = renderDpr();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvasWidth, canvasHeight);

  const pinned = selectedNode.value;
  const hovered = hoveredNode.value;
  const active = hovered || pinned;
  if (!active) {
    fxDirty = false;
    return;
  }
  fxDirty = true;
  const activeId = active.id;

  ctx.save();
  ctx.translate(view.x, view.y);
  ctx.scale(view.zoom, view.zoom);
  ctx.globalCompositeOperation = 'lighter';

  /* 高亮光束 */
  if (activeEdges.length) {
    ctx.globalAlpha = 0.5;
    ctx.lineWidth = 0.9;
    for (let i = 0; i < activeEdges.length; i += 1) {
      const edge = activeEdges[i];
      const other = edge.s.id === activeId ? edge.t : edge.s;
      const dx = edge.t.px - edge.s.px;
      const dy = edge.t.py - edge.s.py;
      const span = Math.abs(dx) + Math.abs(dy) || 1;
      const bend = edge.bend * Math.min(46, span * 0.14);
      const cx = (edge.s.px + edge.t.px) / 2 - (dy / span) * bend;
      const cy = (edge.s.py + edge.t.py) / 2 + (dx / span) * bend;
      ctx.strokeStyle = other.color;
      ctx.beginPath();
      ctx.moveTo(edge.s.px, edge.s.py);
      ctx.quadraticCurveTo(cx, cy, edge.t.px, edge.t.py);
      ctx.stroke();
    }

    /* 光子巡游 */
    if (!reduceMotion && frameCostEma < 30 && frameGapEma < 70) {
      ctx.fillStyle = '#ffffff';
      const photonCount = Math.min(16, activeEdges.length);
      for (let i = 0; i < photonCount; i += 1) {
        const edge = activeEdges[i];
        const dx = edge.t.px - edge.s.px;
        const dy = edge.t.py - edge.s.py;
        const span = Math.abs(dx) + Math.abs(dy) || 1;
        const bend = edge.bend * Math.min(46, span * 0.14);
        const cx = (edge.s.px + edge.t.px) / 2 - (dy / span) * bend;
        const cy = (edge.s.py + edge.t.py) / 2 + (dx / span) * bend;
        const t = ((time / 1600) + edge.phase) % 1;
        ctx.globalAlpha = 0.85;
        ctx.beginPath();
        ctx.arc(quadPoint(edge.s.px, cx, edge.t.px, t), quadPoint(edge.s.py, cy, edge.t.py, t), 1.4, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  /* 悬停实体(未选中时)的即时发光:大图层不重绘,发光在此叠加 */
  if (hovered && hovered.id !== pinned?.id) {
    const radius = hovered.radius * hovered.ps + 1.6;
    const glowSize = radius * 11;
    ctx.globalAlpha = 0.95;
    ctx.drawImage(starSprite(hovered.color), hovered.px - glowSize / 2, hovered.py - glowSize / 2, glowSize, glowSize);
    ctx.globalAlpha = 1;
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(hovered.px, hovered.py, Math.max(0.8, radius * 0.6), 0, Math.PI * 2);
    ctx.fill();
  }

  /* 活动实体脉动环(动画元素,必须逐拍刷新所以放 fx 层) */
  const ringRadius = active.radius * active.ps + 1.6;
  ctx.globalAlpha = 0.55;
  ctx.strokeStyle = active.color;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(active.px, active.py, ringRadius + 6 + Math.sin(time * 0.004) * 1.2, 0, Math.PI * 2);
  ctx.stroke();

  /* 悬停标签(选中标签由大图层负责) */
  if (hovered && hovered.id !== pinned?.id) {
    ctx.globalCompositeOperation = 'source-over';
    ctx.textAlign = 'center';
    const label = hovered.label.length > 20 ? `${hovered.label.slice(0, 20)}…` : hovered.label;
    ctx.globalAlpha = 1;
    ctx.font = '700 13px system-ui, sans-serif';
    ctx.fillStyle = '#fdf6e3';
    ctx.shadowColor = hovered.color;
    ctx.shadowBlur = 10;
    ctx.fillText(label, hovered.px, hovered.py - hovered.radius * hovered.ps - 10);
    ctx.shadowBlur = 0;
    ctx.textAlign = 'start';
  }

  ctx.restore();
};

const findNodeAt = (worldX: number, worldY: number) => {
  const slack = 8 / view.zoom;
  for (let i = drawOrder.length - 1; i >= 0; i -= 1) {
    const node = drawOrder[i];
    const dx = worldX - node.px;
    const dy = worldY - node.py;
    const hit = node.radius * node.ps + slack;
    if (dx * dx + dy * dy <= hit * hit) return node;
  }
  return null;
};

const applyQuality = () => {
  sizeLayerCanvas(graphCanvasRef.value);
  sizeLayerCanvas(fxCanvasRef.value);
  renderBgCanvas();
  graphFrame = -999;
  fxDirty = false;
};

const adaptQuality = (rawDt: number) => {
  frameGapEma = frameGapEma * 0.92 + Math.min(200, rawDt) * 0.08;
  qualityHold += 1;
  if (qualityHold < 60) return;
  if (frameGapEma > 50 && quality < QUALITY_DPR.length - 1) {
    quality += 1;
    qualityHold = 0;
    applyQuality();
  } else if (frameGapEma < 24 && quality > 0) {
    quality -= 1;
    qualityHold = 0;
    applyQuality();
  }
};

const draw = (time = 0) => {
  const frameStart = performance.now();
  const rawDt = lastFrameTime ? time - lastFrameTime : 16;
  const dt = Math.min(48, rawDt);
  lastFrameTime = time;
  frameIndex += 1;
  adaptQuality(rawDt);

  /* 镜头缓动 + 选中时暂停星系自转(速度单位 rad/ms,约 0.66°/s 的慢速漂移)。
     所有缓动带收敛死区:静止时数值精确落定,层 transform 字符串不再变化,
     浏览器便可跳过整帧合成(对无 GPU 的软件合成环境是决定性优化) */
  const targetSpeed = reduceMotion || selectedNode.value ? 0 : 0.0000115;
  rotationSpeed += (targetSpeed - rotationSpeed) * 0.04;
  if (Math.abs(rotationSpeed) < 1e-7 && targetSpeed === 0) rotationSpeed = 0;
  rotation += rotationSpeed * dt;
  const ease = (current: number, target: number, factor: number, epsilon: number) => {
    const next = current + (target - current) * factor;
    return Math.abs(target - next) < epsilon ? target : next;
  };
  view.zoom = ease(view.zoom, viewTarget.zoom, 0.16, 0.0004);
  view.x = ease(view.x, viewTarget.x, 0.16, 0.03);
  view.y = ease(view.y, viewTarget.y, 0.16, 0.03);
  pointer.x = ease(pointer.x, pointer.tx, 0.04, 0.0008);
  pointer.y = ease(pointer.y, pointer.ty, 0.04, 0.0008);

  const nextZoomLabel = view.zoom.toFixed(2);
  if (nextZoomLabel !== zoomLabel.value) zoomLabel.value = nextZoomLabel;

  updateCamera();

  /* 背景视差:纯 CSS transform,由合成器执行;字符串不变时跳过赋值以保留合成缓存 */
  const bgCanvas = bgCanvasRef.value;
  if (bgCanvas) {
    const bx = Math.max(-BG_PAD, Math.min(BG_PAD, view.x * 0.05 + pointer.x * 6));
    const by = Math.max(-BG_PAD, Math.min(BG_PAD, view.y * 0.05 + pointer.y * 4));
    const bgTransform = `translate(${bx.toFixed(1)}px, ${by.toFixed(1)}px)`;
    if (bgCanvas.style.transform !== bgTransform) bgCanvas.style.transform = bgTransform;
  }

  /* graph 层调度:到期或缩放漂移超阈值(保底 4 帧)时全量重绘,否则 CSS 跟随 */
  const graphCanvas = graphCanvasRef.value;
  const zoomDrifted = Math.abs(view.zoom / graphView.zoom - 1) > 0.08;
  const due = graphFrame < 0
    || frameIndex - graphFrame >= graphInterval()
    || (zoomDrifted && frameIndex - graphFrame >= 4);
  if (due) {
    for (let i = 0; i < starNodes.length; i += 1) {
      const node = starNodes[i];
      const projected = projectNode(node);
      node.px = projected.x;
      node.py = projected.y;
      node.ps = projected.scale;
      const sx = projected.x * view.zoom + view.x;
      const sy = projected.y * view.zoom + view.y;
      node.visible = sx > -CULL_PAD && sx < canvasWidth + CULL_PAD && sy > -CULL_PAD && sy < canvasHeight + CULL_PAD;
    }
    renderGraphLayer(time);
  } else if (graphCanvas) {
    const scale = view.zoom / graphView.zoom;
    const tx = view.x - graphView.x * scale;
    const ty = view.y - graphView.y * scale;
    const graphTransform = `translate(${tx.toFixed(2)}px, ${ty.toFixed(2)}px) scale(${scale.toFixed(4)})`;
    if (graphCanvas.style.transform !== graphTransform) graphCanvas.style.transform = graphTransform;
  }

  /* fx 高亮层调度:悬停/选中变化当帧立即刷新保证响应,平时 2-3 帧一拍;
     无活动实体且已清空时零 damage */
  const hasActive = Boolean(hoveredNode.value || selectedNode.value);
  const fxInterval = frameGapEma > 45 || quality > 0 ? 3 : 2;
  if (hasActive ? (fxForce || frameIndex % fxInterval === 0) : fxDirty) {
    renderFxLayer(time);
    fxForce = false;
  }

  /* 悬停检测节流:每 2 帧一次,避免高频 pointermove 抢占主线程 */
  if (hoverPoint.fresh && !drag.active && frameIndex % 2 === 0) {
    hoverPoint.fresh = false;
    const worldX = (hoverPoint.x - view.x) / view.zoom;
    const worldY = (hoverPoint.y - view.y) / view.zoom;
    const found = findNodeAt(worldX, worldY);
    if (found?.id !== hoveredNode.value?.id) hoveredNode.value = found;
  }

  frameCostEma = frameCostEma * 0.9 + (performance.now() - frameStart) * 0.1;
  frameId = requestAnimationFrame(draw);
};

const screenPoint = (event: PointerEvent | WheelEvent) => {
  const rect = wrapRef.value?.getBoundingClientRect();
  return {
    x: event.clientX - (rect?.left || 0),
    y: event.clientY - (rect?.top || 0),
  };
};

const handlePointerDown = (event: PointerEvent) => {
  drag = { active: true, moved: false, x: event.clientX, y: event.clientY };
  dragging.value = true;
  wrapRef.value?.setPointerCapture(event.pointerId);
};

const handlePointerMove = (event: PointerEvent) => {
  const rect = wrapRef.value?.getBoundingClientRect();
  if (rect) {
    pointer.tx = ((event.clientX - rect.left) / rect.width - 0.5) * 2;
    pointer.ty = ((event.clientY - rect.top) / rect.height - 0.5) * 2;
  }
  if (drag.active) {
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) drag.moved = true;
    view.x += dx;
    view.y += dy;
    viewTarget.x = view.x;
    viewTarget.y = view.y;
    drag.x = event.clientX;
    drag.y = event.clientY;
    return;
  }
  const point = screenPoint(event);
  hoverPoint.x = point.x;
  hoverPoint.y = point.y;
  hoverPoint.fresh = true;
};

const handlePointerUp = (event: PointerEvent) => {
  if (!drag.moved) {
    const point = screenPoint(event);
    const target = findNodeAt((point.x - view.x) / view.zoom, (point.y - view.y) / view.zoom);
    if (target) {
      if (selectedNode.value?.id === target.id) {
        selectedNode.value = null;
      } else {
        focusNode(target);
      }
    } else {
      selectedNode.value = null;
    }
  }
  drag.active = false;
  dragging.value = false;
  wrapRef.value?.releasePointerCapture(event.pointerId);
};

const handleWheel = (event: WheelEvent) => {
  const point = screenPoint(event);
  const worldX = (point.x - view.x) / view.zoom;
  const worldY = (point.y - view.y) / view.zoom;
  const nextZoom = Math.min(3.2, Math.max(0.3, viewTarget.zoom * (event.deltaY > 0 ? 0.88 : 1.12)));
  viewTarget.x = point.x - worldX * nextZoom;
  viewTarget.y = point.y - worldY * nextZoom;
  viewTarget.zoom = nextZoom;
};

const focusNode = (node: StarNode) => {
  selectedNode.value = node;
  viewTarget.zoom = Math.max(1.7, viewTarget.zoom);
  viewTarget.x = canvasWidth / 2 - node.px * viewTarget.zoom;
  viewTarget.y = canvasHeight / 2 - node.py * viewTarget.zoom;
};

const focusSearch = () => {
  const query = searchText.value.trim().toLowerCase();
  if (!query) return;
  const node = starNodes.find((item) => item.label.toLowerCase().includes(query));
  if (node) {
    focusNode(node);
    return;
  }
  searchMiss.value = true;
  window.clearTimeout(searchMissTimer);
  searchMissTimer = window.setTimeout(() => { searchMiss.value = false; }, 1600);
};

const zoomBy = (factor: number) => {
  const nextZoom = Math.min(3.2, Math.max(0.3, viewTarget.zoom * factor));
  const cx = canvasWidth / 2;
  const cy = canvasHeight / 2;
  const wx = (cx - view.x) / view.zoom;
  const wy = (cy - view.y) / view.zoom;
  viewTarget.x = cx - wx * nextZoom;
  viewTarget.y = cy - wy * nextZoom;
  viewTarget.zoom = nextZoom;
};

const resetView = () => {
  viewTarget.zoom = 1;
  viewTarget.x = 0;
  viewTarget.y = 0;
  selectedNode.value = null;
  activeType.value = null;
};

const toggleType = (type: string) => {
  activeType.value = activeType.value === type ? null : type;
  if (activeType.value && selectedNode.value
    && (selectedNode.value.entity_type || 'unknown') !== activeType.value) {
    selectedNode.value = null;
  }
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
  window.clearTimeout(searchMissTimer);
  resizeObserver?.disconnect();
  document.removeEventListener('fullscreenchange', syncFullscreenState);
});
</script>

<template>
  <section
    ref="shellRef"
    class="relative overflow-hidden rounded-2xl border border-slate-800/70 bg-[#04050c] text-slate-200 shadow-xl shadow-slate-950/30"
    :class="[
      fullPage ? 'h-[calc(100vh-150px)] min-h-[640px]' : 'h-[640px]',
      isFullscreen ? 'fixed inset-0 z-[100] h-screen min-h-0 rounded-none border-0' : '',
    ]"
  >
    <div
      ref="wrapRef"
      class="absolute inset-0 touch-none"
      :class="dragging ? 'cursor-grabbing' : hoveredNode ? 'cursor-pointer' : 'cursor-grab'"
      @pointerdown="handlePointerDown"
      @pointermove="handlePointerMove"
      @pointerup="handlePointerUp"
      @pointerleave="hoveredNode = null"
      @wheel.prevent="handleWheel"
      @dblclick="resetView"
    >
      <!-- 三层画布:背景(视差) / 图层(隔帧重绘+CSS跟随) / 特效(逐帧) -->
      <canvas
        ref="bgCanvasRef"
        class="pointer-events-none absolute z-0"
        style="left: -96px; top: -96px; transform-origin: 0 0; will-change: transform;"
      ></canvas>
      <canvas
        ref="graphCanvasRef"
        class="pointer-events-none absolute left-0 top-0 z-[1]"
        style="transform-origin: 0 0; will-change: transform;"
      ></canvas>
      <canvas
        ref="fxCanvasRef"
        class="pointer-events-none absolute left-0 top-0 z-[2]"
      ></canvas>

      <!-- 暗角 -->
      <div
        class="pointer-events-none absolute inset-0 z-[3]"
        style="background: radial-gradient(ellipse at center, transparent 46%, rgba(2, 3, 10, 0.62) 100%);"
      ></div>

      <!-- 顶部:标题 + 操作 + 详情卡(同列排布,互不重叠) -->
      <div class="pointer-events-none absolute inset-x-0 top-0 z-10 flex flex-wrap items-start justify-between gap-3 p-4">
        <div class="pointer-events-auto min-w-0">
          <div class="flex items-center gap-2">
            <Sparkles class="h-3.5 w-3.5 text-violet-300/90" />
            <span class="text-[10px] font-bold uppercase tracking-[0.3em] text-violet-200/60">Knowledge Nebula</span>
          </div>
          <h3 class="mt-1 truncate text-lg font-extrabold tracking-wide text-slate-50" style="text-shadow: 0 0 26px rgba(139, 92, 246, 0.5);">
            {{ kbName || '当前知识库' }}
          </h3>
          <div class="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
            <span>{{ totalLabel }}</span>
            <span v-if="graphMeta?.cache_hit" class="rounded-md border border-emerald-400/20 bg-emerald-400/10 px-1.5 py-0.5 text-[10px] font-bold text-emerald-300">
              Redis 缓存
            </span>
          </div>
        </div>

        <div class="pointer-events-auto flex max-w-full flex-col items-end gap-2">
          <div class="flex flex-wrap items-center justify-end gap-2">
            <div class="relative">
              <Search class="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
              <input
                v-model="searchText"
                type="text"
                placeholder="定位实体"
                class="nebula-glass w-40 rounded-xl py-2 pl-8 pr-3 text-xs text-slate-100 placeholder-slate-500 outline-none transition-all focus:w-52 focus:border-violet-400/40 focus:ring-2 focus:ring-violet-400/20"
                :class="searchMiss ? 'border-rose-400/60 ring-2 ring-rose-400/25' : ''"
                @keyup.enter="focusSearch"
              />
              <span
                v-if="searchMiss"
                class="absolute -bottom-5 right-1 text-[10px] font-semibold text-rose-300"
              >未找到匹配实体</span>
            </div>
            <button class="nebula-btn" :disabled="loading" title="跳过缓存并刷新图谱" @click="loadGraph(true)">
              <Loader2 v-if="loading" class="h-4 w-4 animate-spin" />
              <RefreshCw v-else class="h-4 w-4" />
            </button>
            <button class="nebula-btn" title="放大" @click="zoomBy(1.2)"><ZoomIn class="h-4 w-4" /></button>
            <button class="nebula-btn" title="缩小" @click="zoomBy(0.84)"><ZoomOut class="h-4 w-4" /></button>
            <button class="nebula-btn" title="重置视图" @click="resetView"><RotateCcw class="h-4 w-4" /></button>
            <button class="nebula-btn" :title="isFullscreen ? '退出全屏' : '进入全屏'" @click="toggleFullscreen">
              <Minimize2 v-if="isFullscreen" class="h-4 w-4" />
              <Maximize2 v-else class="h-4 w-4" />
            </button>
          </div>
          <div class="flex items-center gap-2">
            <label class="nebula-glass flex items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-[11px] text-slate-400">
              <input
                v-model.number.lazy="nodeLimit"
                type="number"
                min="20"
                max="5000"
                step="100"
                class="w-14 bg-transparent text-right font-bold text-slate-200 outline-none"
                @change="nodeLimit = Math.min(5000, Math.max(20, Number(nodeLimit) || 5000))"
              />
              节点
            </label>
            <label class="nebula-glass flex items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-[11px] text-slate-400">
              <input
                v-model.number.lazy="edgeLimit"
                type="number"
                min="0"
                max="20000"
                step="500"
                class="w-16 bg-transparent text-right font-bold text-slate-200 outline-none"
                @change="edgeLimit = Math.min(20000, Math.max(0, Number(edgeLimit) || 0))"
              />
              关系
            </label>
          </div>

          <!-- 节点详情浮层:与控件同列流式排布,窄屏也不会互相压盖 -->
          <Transition name="fade">
            <div
              v-if="detailNode"
              class="nebula-glass w-72 max-w-full overflow-hidden rounded-2xl"
            >
              <div class="h-0.5 w-full" :style="{ background: `linear-gradient(90deg, ${detailNode.color}, transparent)` }"></div>
              <div class="max-h-[42vh] overflow-y-auto p-4 custom-scrollbar">
                <div class="flex items-start justify-between gap-2">
                  <div class="min-w-0">
                    <div class="break-words text-sm font-extrabold text-slate-50">{{ detailNode.label }}</div>
                    <div class="mt-1 flex items-center gap-1.5 text-[11px] text-slate-400">
                      <span class="h-1.5 w-1.5 rounded-full" :style="{ background: detailNode.color }"></span>
                      {{ detailNode.entity_type || 'unknown' }} · {{ detailNode.degree }} 条关系
                    </div>
                  </div>
                  <button
                    v-if="selectedNode && detailNode.id === selectedNode.id"
                    class="shrink-0 rounded-lg p-1 text-slate-500 transition-colors hover:bg-white/10 hover:text-slate-200"
                    title="取消选中"
                    @click="selectedNode = null"
                  >
                    <X class="h-3.5 w-3.5" />
                  </button>
                </div>
                <p v-if="detailNode.description" class="mt-3 text-xs leading-relaxed text-slate-300">
                  {{ detailNode.description }}
                </p>
                <div v-if="detailNode.file_path" class="mt-3 break-words border-t border-white/5 pt-2 text-[10px] leading-relaxed text-slate-500">
                  来源:{{ detailNode.file_path }}
                </div>
                <div v-else-if="detailNode.source_id" class="mt-3 break-words border-t border-white/5 pt-2 text-[10px] text-slate-500">
                  文档:{{ detailNode.source_id }}
                </div>
              </div>
            </div>
          </Transition>
        </div>
      </div>

      <!-- 实体类型图例(可点击筛选) -->
      <div v-if="legend.length" class="absolute bottom-14 left-4 z-10 flex flex-col gap-1">
        <span class="mb-0.5 text-[10px] font-bold uppercase tracking-[0.22em] text-slate-500">实体类型</span>
        <button
          v-for="item in legend"
          :key="item.type"
          type="button"
          class="nebula-glass group flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[11px] transition-all"
          :class="activeType === item.type
            ? 'border-violet-300/50 bg-violet-400/15 text-slate-50'
            : activeType
              ? 'text-slate-500 opacity-60 hover:opacity-100'
              : 'text-slate-300 hover:border-slate-500/50'"
          @click="toggleType(item.type)"
        >
          <span
            class="h-2 w-2 shrink-0 rounded-full transition-transform group-hover:scale-125"
            :style="{ background: item.color, boxShadow: `0 0 8px ${item.color}` }"
          ></span>
          <span class="max-w-32 truncate">{{ item.type }}</span>
          <span class="ml-auto pl-2 font-bold text-slate-500">{{ item.count }}</span>
        </button>
        <span v-if="legendOverflow" class="px-1 text-[10px] text-slate-600">其余 {{ legendOverflow }} 类未列出</span>
      </div>

      <!-- 底部提示栏 -->
      <div class="pointer-events-none absolute inset-x-0 bottom-0 z-10 flex items-center justify-between gap-3 px-4 py-3 text-[11px] text-slate-500">
        <div class="flex min-w-0 items-center gap-3">
          <span class="truncate">
            <span class="hidden md:inline">拖拽平移 · 滚轮缩放 · 点击星辰查看实体 · 双击重置</span>
            <span class="md:hidden">拖拽 · 缩放 · 点击星辰</span>
          </span>
          <span v-if="graphMeta?.truncated" class="nebula-glass pointer-events-auto hidden rounded-lg px-2 py-1 text-[10px] text-slate-400 sm:inline">
            当前为图谱切片,可上调节点/关系数量
          </span>
        </div>
        <span class="shrink-0 font-mono">缩放 ×{{ zoomLabel }}</span>
      </div>

      <!-- 加载 / 错误 / 空态 -->
      <div v-if="loading" class="absolute inset-0 z-20 flex flex-col items-center justify-center bg-[#04050c]/92">
        <Loader2 class="mb-3 h-8 w-8 animate-spin text-violet-300" />
        <span class="text-sm font-bold text-slate-300">正在汇聚星云…</span>
        <span class="mt-1 text-[11px] text-slate-500">大图谱首次生成可能需要数秒</span>
      </div>

      <div v-if="errorMsg" class="absolute inset-x-6 top-24 z-20 rounded-xl border border-rose-400/30 bg-rose-950/90 p-4 text-sm text-rose-100">
        {{ errorMsg }}
      </div>

      <div
        v-if="!loading && !errorMsg && starCount === 0"
        class="absolute inset-0 z-[5] flex flex-col items-center justify-center text-slate-500"
      >
        <Sparkles class="mb-3 h-8 w-8 opacity-60" />
        <span class="text-sm font-semibold">此知识库暂无图谱数据</span>
        <span class="mt-1 text-[11px]">完成文档入库并抽取实体后,星云将在此展开</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 玻璃浮层刻意不用 backdrop-filter:它强迫浏览器每帧对画布做全屏回读+模糊,
   软件渲染/远程桌面下直接把帧率打到个位数;深色实底在星空上视觉等效 */
.nebula-glass {
  background: rgba(13, 17, 32, 0.86);
  border: 1px solid rgba(148, 163, 184, 0.16);
}

.nebula-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0.5rem;
  border-radius: 0.75rem;
  color: rgb(148, 163, 184);
  background: rgba(13, 17, 32, 0.86);
  border: 1px solid rgba(148, 163, 184, 0.16);
  transition: all 180ms ease;
}
.nebula-btn:hover {
  color: rgb(226, 232, 240);
  border-color: rgba(167, 139, 250, 0.45);
  box-shadow: 0 0 18px rgba(139, 92, 246, 0.22);
}
.nebula-btn:disabled {
  opacity: 0.5;
  pointer-events: none;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 180ms ease, transform 180ms ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
