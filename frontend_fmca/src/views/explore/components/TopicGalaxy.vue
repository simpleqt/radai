<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { Search, Loader2, Sparkles, RefreshCw, ArrowRight } from 'lucide-vue-next';
import { ExploreAPI, type ExploreTopic } from '@/api/explore';
import TopicBubble from './TopicBubble.vue';

const props = defineProps<{ kbIds: string[] }>();
const emit = defineEmits<{ (e: 'start', topic: string): void }>();

const input = ref('');
const topics = ref<ExploreTopic[]>([]);
const loadingTopics = ref(false);
const mounted = ref(false);
const hoveredIndex = ref<number | null>(null);
const viewportWidth = ref(0);
const viewportHeight = ref(0);

const parallaxX = ref(0);
const parallaxY = ref(0);

const isCompactViewport = computed(() => viewportWidth.value < 1360 || viewportHeight.value < 760);
const isVeryCompactViewport = computed(() => viewportWidth.value < 1100 || viewportHeight.value < 700);
const visibleTopics = computed(() => (
  isVeryCompactViewport.value
    ? topics.value.slice(0, 6)
    : isCompactViewport.value
      ? topics.value.slice(0, 10)
      : topics.value
));

function updateViewport() {
  if (typeof window === 'undefined') return;
  viewportWidth.value = window.innerWidth;
  viewportHeight.value = window.innerHeight;
}

function handleMouseMove(e: MouseEvent) {
  const { clientWidth, clientHeight } = document.documentElement;
  const x = (e.clientX / clientWidth - 0.5) * 2;
  const y = (e.clientY / clientHeight - 0.5) * 2;
  parallaxX.value = x * 12;
  parallaxY.value = y * 12;
}

function hash(n: number): number {
  const x = Math.sin(n * 999.13) * 43758.5453;
  return x - Math.floor(x);
}

// 闪烁繁星数据生成器
const stars = computed(() => {
  const arr = [];
  for (let i = 0; i < 35; i++) {
    arr.push({
      left: hash(i * 3) * 100,
      top: hash(i * 3 + 1) * 100,
      size: 1 + hash(i * 3 + 2) * 1.5,
      delay: `${hash(i * 7) * 4}s`,
      duration: `${3 + hash(i * 11) * 3}s`
    });
  }
  return arr;
});

const positions = computed(() => {
  const layoutTopics = visibleTopics.value;
  if (layoutTopics.length === 0) return [];
  
  // 磨砂卡片的核心包围边界及安全间距
  const cardLeft = isCompactViewport.value ? 28 : 26;
  const cardRight = isCompactViewport.value ? 72 : 74;
  const cardTop = isCompactViewport.value ? 26 : 24;
  const cardBottom = isCompactViewport.value ? 74 : 76;
  const minX = isCompactViewport.value ? 13 : 6;
  const maxX = isCompactViewport.value ? 87 : 94;
  const minY = isCompactViewport.value ? 12 : 10;
  const maxY = isCompactViewport.value ? 88 : 90;

  // 将节点投影到带安全边界的卡片区域之外
  const projectOutsideCard = (x: number, y: number, w: number, h: number) => {
    // 增加适当的安全边界，保证文字气泡绝对不遮挡卡片
    const padX = w / 2 + 2.5;
    const padY = h / 2 + 2.0;
    const L = cardLeft - padX;
    const R = cardRight + padX;
    const T = cardTop - padY;
    const B = cardBottom + padY;
    
    if (x > L && x < R && y > T && y < B) {
      const dx = x - 50 || 0.1;
      const dy = y - 50 || 0.1;
      
      // 计算沿射线的缩放比例使其落在外包围盒边缘
      const k_x = dx > 0 ? (R - 50) / dx : (L - 50) / dx;
      const k_y = dy > 0 ? (B - 50) / dy : (T - 50) / dy;
      const k = Math.min(k_x, k_y);
      
      return {
        x: 50 + k * dx,
        y: 50 + k * dy
      };
    }
    return { x, y };
  };

  // 1. 初始化节点位置 (使用黄金角螺旋线作为基础分布)
  const nodes = layoutTopics.map((topic, i) => {
    const angle = i * 2.399963 - 0.4;
    const radius = 0.72 + hash(i) * 0.22;
    const left = 50 + Math.cos(angle) * radius * 46 + (hash(i + 7) - 0.5) * 2.4;
    const top = 50 + Math.sin(angle) * radius * 39 + (hash(i + 13) - 0.5) * 2.4;
    
    // 估算节点的百分比宽度和高度
    const textLen = topic.title.length;
    const w = Math.max(11, textLen * 0.72 + 7.2);
    const h = 5.7;
    
    // 初始位置即确保投影在卡片外
    const projected = projectOutsideCard(left, top, w, h);
    
    return {
      id: topic.id,
      x: projected.x,
      y: projected.y,
      origX: projected.x,
      origY: projected.y,
      w,
      h,
      size: hash(i + 3) > 0.65 ? 'lg' as const : 'md' as const,
    };
  });
  
  // 2. 迭代碰撞松弛 (物理排斥算法解决重叠)
  const ticks = 80;
  for (let t = 0; t < ticks; t++) {
    // A. 节点与节点之间的相互排斥
    for (let i = 0; i < nodes.length; i++) {
      const u = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const v = nodes[j];
        
        const dx = u.x - v.x;
        const dy = u.y - v.y;
        
        const minDistX = (u.w + v.w) / 2 + 2.0;
        const minDistY = (u.h + v.h) / 2 + 1.5;
        
        if (Math.abs(dx) < minDistX && Math.abs(dy) < minDistY) {
          const overlapX = minDistX - Math.abs(dx);
          const overlapY = minDistY - Math.abs(dy);
          
          const forceX = overlapX * 0.12 * (dx === 0 ? 1 : Math.sign(dx));
          const forceY = overlapY * 0.12 * (dy === 0 ? 1 : Math.sign(dy));
          
          u.x += forceX;
          u.y += forceY;
          v.x -= forceX;
          v.y -= forceY;
        }
      }
    }
    
    // B. 对中央卡片的碰撞排斥（防止节点被其他节点挤进卡片内部）
    for (let i = 0; i < nodes.length; i++) {
      const u = nodes[i];
      const projected = projectOutsideCard(u.x, u.y, u.w, u.h);
      if (projected.x !== u.x || projected.y !== u.y) {
        // 向安全投影位置移动
        u.x += (projected.x - u.x) * 0.35;
        u.y += (projected.y - u.y) * 0.35;
      }
      
      // C. 引力机制：向各自的安全初始位置靠拢，维持星系形态
      const dOrigX = u.origX - u.x;
      const dOrigY = u.origY - u.y;
      u.x += dOrigX * 0.08;
      u.y += dOrigY * 0.08;
      
      // D. 视口边界限位约束
      u.x = Math.min(maxX, Math.max(minX, u.x));
      u.y = Math.min(maxY, Math.max(minY, u.y));
    }
  }
  
  // 3. 最终强制校正，100% 杜绝因迭代残留导致任何细微遮挡
  for (let i = 0; i < nodes.length; i++) {
    const u = nodes[i];
    const projected = projectOutsideCard(u.x, u.y, u.w, u.h);
    u.x = projected.x;
    u.y = projected.y;
    
    u.x = Math.min(maxX, Math.max(minX, u.x));
    u.y = Math.min(maxY, Math.max(minY, u.y));
  }
  
  return nodes.map((n, i) => ({
    left: n.x,
    top: n.y,
    delay: `${i * 0.03}s`,
    size: n.size,
  }));
});



const featuredTopics = computed(() => (
  isVeryCompactViewport.value
    ? topics.value.slice(0, 3)
    : isCompactViewport.value
      ? topics.value.slice(0, 4)
      : topics.value.slice(0, 5)
));

// 输入框占位提示基于已加载的预制主题动态生成，自动适配不同学科
const placeholder = computed(() => {
  const examples = topics.value.slice(0, 2).map((t) => t.title).join('、');
  return examples ? `例如：${examples}…` : '输入或选择学习主题…';
});

async function loadTopics() {
  loadingTopics.value = true;
  try {
    const data = await ExploreAPI.topics(props.kbIds, 24);
    topics.value = data.topics || [];
  } catch (e) {
    console.warn('[Explore] 加载主题失败', e);
    topics.value = [];
  } finally {
    loadingTopics.value = false;
  }
}

function submit() {
  const t = input.value.trim();
  if (!t) return;
  emit('start', t);
}

function pick(topic: ExploreTopic) {
  emit('start', topic.title);
}

onMounted(async () => {
  updateViewport();
  await loadTopics();
  requestAnimationFrame(() => { mounted.value = true; });
  window.addEventListener('mousemove', handleMouseMove);
  window.addEventListener('resize', updateViewport);
});

onUnmounted(() => {
  window.removeEventListener('mousemove', handleMouseMove);
  window.removeEventListener('resize', updateViewport);
});

defineExpose({ loadTopics });
</script>

<template>
  <div class="galaxy relative w-full h-full overflow-hidden bg-surface-sunken dark:bg-[#080b13]">
    <!-- 背景氛围 (带鼠标 3D 视差偏移) -->
    <div
      class="galaxy-bg absolute inset-0 pointer-events-none transition-transform duration-500 ease-out"
      :style="{ transform: `translate3d(${parallaxX * 0.6}px, ${parallaxY * 0.6}px, 0)` }"
    />
    <div
      class="galaxy-mesh absolute inset-0 pointer-events-none transition-transform duration-500 ease-out"
      :style="{ transform: `translate3d(${parallaxX * 0.4}px, ${parallaxY * 0.4}px, 0)` }"
    />
    <div
      class="galaxy-grid absolute inset-0 pointer-events-none transition-transform duration-500 ease-out"
      :style="{ transform: `translate3d(${parallaxX * 0.25}px, ${parallaxY * 0.25}px, 0)` }"
    />
    
    <!-- 闪烁繁星背景 -->
    <div class="absolute inset-0 pointer-events-none overflow-hidden">
      <span
        v-for="(star, idx) in stars"
        :key="idx"
        class="absolute rounded-full bg-accent opacity-0 animate-twinkle"
        :style="{
          left: `${star.left}%`,
          top: `${star.top}%`,
          width: `${star.size}px`,
          height: `${star.size}px`,
          animationDelay: star.delay,
          animationDuration: star.duration
        }"
      />
    </div>

    <div
      class="galaxy-orbit galaxy-orbit--1 absolute pointer-events-none transition-transform duration-500 ease-out"
      :style="{ transform: `translate(-50%, -50%) translate3d(${parallaxX * 0.2}px, ${parallaxY * 0.2}px, 0)` }"
    />
    <div
      class="galaxy-orbit galaxy-orbit--2 absolute pointer-events-none transition-transform duration-500 ease-out"
      :style="{ transform: `translate(-50%, -50%) translate3d(${parallaxX * 0.15}px, ${parallaxY * 0.15}px, 0)` }"
    />
    <div
      class="galaxy-orbit galaxy-orbit--3 absolute pointer-events-none transition-transform duration-500 ease-out"
      :style="{ transform: `translate(-50%, -50%) translate3d(${parallaxX * 0.1}px, ${parallaxY * 0.1}px, 0)` }"
    />
    <div class="galaxy-orbit galaxy-orbit--4 absolute pointer-events-none transition-transform duration-500 ease-out" />
    <div class="galaxy-dots absolute pointer-events-none" />
    <div class="galaxy-hub absolute pointer-events-none" />

    <!-- 主题节点 (强制使用 100% 全屏无 padding 绝对定位容器，与背景绝对契合) -->
    <div class="absolute inset-0 hidden w-full h-full pointer-events-none md:block" style="left: 0 !important; top: 0 !important;">
      <div
        v-for="(t, i) in visibleTopics"
        :key="t.id"
        class="absolute pointer-events-auto -translate-x-1/2 -translate-y-1/2 galaxy-node"
        :class="mounted ? 'galaxy-node--in' : ''"
        :style="{ left: `${positions[i]?.left}%`, top: `${positions[i]?.top}%`, transitionDelay: positions[i]?.delay }"
      >
        <TopicBubble
          :topic="t"
          :index="i"
          :size="positions[i]?.size"
          @select="pick"
          @hover="(active) => hoveredIndex = active ? i : null"
        />
      </div>
    </div>

    <!-- 中央探索区 -->
    <div class="absolute inset-0 flex items-center justify-center px-4 py-4 pointer-events-none sm:px-6">
      <div
        class="relative z-10 w-full max-w-[min(640px,calc(100vw-2rem))] pointer-events-auto galaxy-hero xl:max-w-[720px]"
        :class="mounted ? 'galaxy-hero--in' : ''"
      >
        <div class="rounded-[28px] border border-border-subtle bg-surface/82 px-5 py-6 backdrop-blur-3xl transition-all duration-500 hover:border-accent/30 md:px-8 md:py-7 xl:rounded-[32px] xl:px-11 xl:py-10 dark:border-white/10">
          <div class="mb-4 flex items-center justify-center xl:mb-5">
            <span class="inline-flex items-center gap-1.5 rounded-full border border-accent/30 bg-accent-soft px-3.5 py-1.5 text-[12px] font-semibold tracking-wide text-accent shadow-sm">
              <Sparkles class="w-3.5 h-3.5" />
              AI 探索引擎
            </span>
          </div>

          <h1 class="text-center text-3xl font-extrabold leading-tight text-text-primary md:text-4xl xl:text-[42px]">
            开启你的
            <span class="bg-accent bg-clip-text text-transparent animate-shimmer bg-[length:200%_auto]">探索式学习</span>
            之旅
          </h1>
          <p class="mx-auto mb-5 mt-3 max-w-[520px] text-center text-sm leading-6 text-text-secondary md:text-[15px] xl:mb-8 xl:leading-7">
            输入或选择主题，AI 将为你生成知识图谱、深度解析与关联内容，并生成针对性练习，助你系统掌握。
          </p>

          <div class="relative group">
            <input
              v-model="input"
              type="text"
              :placeholder="placeholder"
              class="h-14 w-full rounded-2xl border border-accent/30 bg-surface/88 pl-5 pr-16 text-[14px] text-text-primary transition-all placeholder:text-text-tertiary focus:border-accent focus:bg-surface focus:outline-none focus:ring-4 focus:ring-accent/10 xl:h-16 xl:pl-6 xl:pr-20 xl:text-[15px] dark:border-white/10"
              @keydown.enter="submit"
            />
            <button
              type="button"
              class="absolute right-2 top-1/2 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-2xl bg-accent text-white transition-all duration-300 hover:scale-[1.03] hover:from-violet-500 hover:to-indigo-500 active:scale-95 xl:right-2.5 xl:h-12 xl:w-12"
              @click="submit"
            >
              <Search class="h-5 w-5" />
            </button>
          </div>

          <div v-if="featuredTopics.length" class="mt-5 xl:mt-6">
            <p class="mb-3 text-center text-[12px] font-semibold text-text-secondary">热门探索</p>
            <div class="flex flex-wrap items-center justify-center gap-2">
              <button
                v-for="t in featuredTopics"
                :key="t.id"
                type="button"
                class="inline-flex max-w-[170px] items-center gap-1.5 rounded-full border border-border-subtle bg-surface/70 px-3 py-1.5 text-[12px] font-semibold text-text-secondary shadow-sm transition-all duration-300 hover:scale-[1.03] hover:border-accent/30 hover:bg-surface hover:text-accent active:scale-[0.97] xl:max-w-[190px] xl:px-3.5 xl:py-2 dark:border-white/10 dark:bg-white/5"
                @click="pick(t)"
              >
                <span class="truncate">{{ t.title }}</span>
                <ArrowRight class="h-3 w-3 shrink-0 opacity-60 transition-transform group-hover:translate-x-0.5" />
              </button>
            </div>
          </div>

          <div class="mt-4 flex items-center justify-center xl:mt-5">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-xl px-4 py-2 text-xs font-semibold text-text-secondary transition-all duration-300 hover:bg-accent-soft hover:text-accent disabled:cursor-wait disabled:opacity-70"
              :disabled="loadingTopics"
              @click="loadTopics"
            >
              <Loader2 v-if="loadingTopics" class="w-3.5 h-3.5 animate-spin" />
              <RefreshCw v-else class="w-3.5 h-3.5" />
              换一批推荐主题
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.galaxy-bg {
  background:
    radial-gradient(ellipse 72% 58% at 50% 48%, rgba(139, 92, 246, 0.20), transparent 62%),
    radial-gradient(ellipse 42% 36% at 8% 18%, rgba(59, 130, 246, 0.09), transparent 58%),
    radial-gradient(ellipse 38% 34% at 92% 82%, rgba(217, 70, 239, 0.10), transparent 56%),
    linear-gradient(180deg, #fbfbff 0%, #f4f0ff 54%, #fafbff 100%);
}
:global(.dark) .galaxy-bg {
  background:
    radial-gradient(ellipse 70% 55% at 50% -5%, rgba(99, 102, 241, 0.22), transparent 55%),
    radial-gradient(ellipse 50% 45% at 90% 90%, rgba(139, 92, 246, 0.12), transparent 50%),
    radial-gradient(ellipse 40% 40% at 5% 70%, rgba(59, 130, 246, 0.09), transparent 45%);
}

.galaxy-mesh {
  background-image:
    radial-gradient(at 18% 32%, rgba(99, 102, 241, 0.12) 0, transparent 44%),
    radial-gradient(at 82% 25%, rgba(139, 92, 246, 0.11) 0, transparent 40%),
    radial-gradient(at 60% 86%, rgba(56, 189, 248, 0.08) 0, transparent 44%);
}

.galaxy-grid {
  background-image: radial-gradient(circle, rgba(124, 58, 237, 0.13) 1px, transparent 1px);
  background-size: 18px 18px;
  mask-image: radial-gradient(ellipse 43% 38% at 50% 53%, black 10%, transparent 75%);
}
:global(.dark) .galaxy-grid {
  background-image: radial-gradient(circle, rgba(129, 140, 248, 0.09) 1px, transparent 1px);
}

/* Twinkle Stars */
@keyframes twinkle {
  0%, 100% { opacity: 0.12; transform: scale(0.8); }
  50% { opacity: 0.85; transform: scale(1.3); }
}
.animate-twinkle {
  animation-name: twinkle;
  animation-iteration-count: infinite;
  animation-timing-function: ease-in-out;
}

/* Central card shimmer gradient */
@keyframes shimmer {
  0% { background-position: 0% 50%; }
  50% { background-position: 100% 50%; }
  100% { background-position: 0% 50%; }
}
.animate-shimmer {
  animation: shimmer 5s ease infinite;
}

.galaxy-orbit {
  left: 50%;
  top: 50%;
  border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, 0.76);
  box-shadow: 0 0 0 1px rgba(124, 58, 237, 0.035), inset 0 0 24px rgba(255, 255, 255, 0.26);
  transition: all 0.5s ease-out;
}
:global(.dark) .galaxy-orbit {
  border-color: rgba(129, 140, 248, 0.13);
}
.galaxy-orbit--1 { width: min(560px, 70vw); height: min(360px, 45vw); opacity: 0.86; }
.galaxy-orbit--2 { width: min(820px, 92vw); height: min(520px, 58vw); opacity: 0.66; }
.galaxy-orbit--3 { width: min(1060px, 112vw); height: min(660px, 70vw); opacity: 0.46; }
.galaxy-orbit--4 { left: 50%; top: 50%; width: min(1260px, 128vw); height: min(760px, 82vw); transform: translate(-50%, -50%); opacity: 0.30; }

.galaxy-dots {
  left: 50%;
  top: 50%;
  width: min(1120px, 118vw);
  height: min(680px, 78vw);
  transform: translate(-50%, -50%);
  background:
    radial-gradient(circle at 18% 50%, rgba(124, 58, 237, 0.42) 0 3px, transparent 4px),
    radial-gradient(circle at 32% 18%, rgba(124, 58, 237, 0.34) 0 3px, transparent 4px),
    radial-gradient(circle at 66% 84%, rgba(124, 58, 237, 0.32) 0 3px, transparent 4px),
    radial-gradient(circle at 85% 52%, rgba(255, 255, 255, 0.88) 0 4px, transparent 5px),
    radial-gradient(circle at 50% 90%, rgba(255, 255, 255, 0.86) 0 4px, transparent 5px);
  filter: drop-shadow(0 5px 10px rgba(124, 58, 237, 0.16));
}

.galaxy-hub {
  left: 50%;
  top: 50%;
  width: min(760px, 72vw);
  height: min(460px, 52vw);
  transform: translate(-50%, -50%);
  background:
    radial-gradient(ellipse at center, rgba(139, 92, 246, 0.20) 0%, rgba(139, 92, 246, 0.10) 36%, transparent 72%);
  filter: blur(18px);
}

.galaxy-lines {
  width: 100% !important;
  height: 100% !important;
  left: 0 !important;
  top: 0 !important;
}

.galaxy-line {
  opacity: 0;
  animation: line-in 1.2s ease forwards;
  transition: stroke 0.4s ease, stroke-width 0.4s ease, stroke-dasharray 0.4s ease;
}
@keyframes line-in {
  to { opacity: 1; }
}

.galaxy-line--active {
  stroke-dashoffset: 15;
  animation: flow-line 0.8s linear infinite !important;
  filter: drop-shadow(0 0 4px rgba(139, 92, 246, 0.4));
  opacity: 1 !important;
}

@keyframes flow-line {
  to { stroke-dashoffset: 0; }
}

.galaxy-node {
  opacity: 0;
  transform: translate(-50%, -50%) scale(0.85);
  transition: opacity 0.55s ease, transform 0.55s cubic-bezier(0.34, 1.56, 0.64, 1), left 0.6s cubic-bezier(0.25, 0.8, 0.25, 1), top 0.6s cubic-bezier(0.25, 0.8, 0.25, 1);
}
.galaxy-node--in {
  opacity: 1;
  transform: translate(-50%, -50%) scale(1);
}

.galaxy-hero {
  opacity: 0;
  transform: translateY(16px);
  transition: opacity 0.6s ease 0.08s, transform 0.6s cubic-bezier(0.22, 1, 0.36, 1) 0.08s;
}
.galaxy-hero--in {
  opacity: 1;
  transform: translateY(0);
}

.animate-pulse-slow {
  animation: pulse-slow 3s ease-in-out infinite;
}
@keyframes pulse-slow {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.92; transform: scale(0.98); }
}

@media (prefers-reduced-motion: reduce) {
  .galaxy-node, .galaxy-hero, .galaxy-line, .animate-twinkle, .animate-shimmer, .animate-pulse-slow { animation: none !important; transition: none !important; opacity: 1 !important; transform: none !important; }
}
</style>
