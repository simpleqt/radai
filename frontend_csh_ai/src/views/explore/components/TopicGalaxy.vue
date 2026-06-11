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

const parallaxX = ref(0);
const parallaxY = ref(0);

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
  if (topics.value.length === 0) return [];
  
  // 1. 初始化节点位置 (使用黄金角螺旋线作为基础分布)
  const nodes = topics.value.map((topic, i) => {
    const angle = i * 2.399963;
    const radius = 0.55 + hash(i) * 0.30;
    const left = 50 + Math.cos(angle) * radius * 40 + (hash(i + 7) - 0.5) * 3;
    const top = 50 + Math.sin(angle) * radius * 35 + (hash(i + 13) - 0.5) * 3;
    
    // 估算节点的百分比宽度和高度
    const textLen = topic.title.length;
    const w = Math.max(9, textLen * 0.65 + 5.5);
    const h = 4.8;
    
    return {
      id: topic.id,
      x: left,
      y: top,
      origX: left,
      origY: top,
      w,
      h,
      size: hash(i + 3) > 0.65 ? 'lg' as const : 'md' as const,
    };
  });
  
  // 2. 迭代碰撞松弛 (物理排斥算法解决重叠)
  const ticks = 80;
  for (let t = 0; t < ticks; t++) {
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
    
    const cardLeft = 28, cardRight = 72, cardTop = 22, cardBottom = 78;
    
    for (let i = 0; i < nodes.length; i++) {
      const u = nodes[i];
      if (u.x > cardLeft && u.x < cardRight && u.y > cardTop && u.y < cardBottom) {
        const dx = u.x - 50 || 0.1;
        const dy = u.y - 50 || 0.1;
        const dist = Math.hypot(dx, dy * 1.1) || 0.1;
        u.x += (dx / dist) * 0.25;
        u.y += (dy / dist) * 0.25;
      }
      
      const dOrigX = u.origX - u.x;
      const dOrigY = u.origY - u.y;
      u.x += dOrigX * 0.08;
      u.y += dOrigY * 0.08;
      
      u.x = Math.min(93, Math.max(7, u.x));
      u.y = Math.min(90, Math.max(10, u.y));
    }
  }
  
  return nodes.map((n, i) => ({
    left: n.x,
    top: n.y,
    delay: `${i * 0.03}s`,
    size: n.size,
  }));
});



const featuredTopics = computed(() => topics.value.slice(0, 4));

// 输入框占位提示基于已加载的预制主题动态生成，自动适配不同学科
const placeholder = computed(() => {
  const examples = topics.value.slice(0, 2).map((t) => t.title).join('、');
  return examples ? `例如：${examples}…` : '输入你想探究的主题…';
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
  await loadTopics();
  requestAnimationFrame(() => { mounted.value = true; });
  window.addEventListener('mousemove', handleMouseMove);
});

onUnmounted(() => {
  window.removeEventListener('mousemove', handleMouseMove);
});

defineExpose({ loadTopics });
</script>

<template>
  <div class="galaxy relative w-full h-full overflow-hidden bg-slate-50 dark:bg-[#080b13]">
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
        class="absolute rounded-full bg-indigo-200 dark:bg-indigo-300 opacity-0 animate-twinkle"
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
    <div class="galaxy-hub absolute pointer-events-none" />

    <!-- 主题节点 (强制使用 100% 全屏无 padding 绝对定位容器，与背景绝对契合) -->
    <div class="absolute inset-0 w-full h-full pointer-events-none" style="left: 0 !important; top: 0 !important;">
      <div
        v-for="(t, i) in topics"
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
    <div class="absolute inset-0 flex items-center justify-center px-4 pointer-events-none">
      <div
        class="relative z-10 w-full max-w-xl pointer-events-auto galaxy-hero"
        :class="mounted ? 'galaxy-hero--in' : ''"
      >
        <!-- 高端磨砂玻璃拟态卡片 (半透明融合背景，拒绝突兀白框) -->
        <div class="rounded-3xl border border-white/20 dark:border-white/10 bg-white/25 dark:bg-slate-950/35 backdrop-blur-3xl shadow-[0_20px_60px_rgba(99,102,241,0.04)] dark:shadow-[0_30px_70px_rgba(0,0,0,0.6)] px-6 py-8 md:px-9 md:py-9 transition-all duration-500 hover:border-white/35 dark:hover:border-white/15">
          <div class="flex items-center justify-center mb-5 animate-pulse-slow">
            <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-[11px] font-semibold tracking-wide text-indigo-600 dark:text-indigo-400 bg-indigo-50/60 dark:bg-indigo-500/10 border border-indigo-100/50 dark:border-indigo-500/20 shadow-sm">
              <Sparkles class="w-3.5 h-3.5" />
              探索式学习
            </span>
          </div>

          <h1 class="text-center text-2xl md:text-[2rem] leading-snug font-extrabold text-slate-800 dark:text-slate-100 mb-3 tracking-tight">
            开启你的
            <span class="bg-gradient-to-r from-indigo-500 via-purple-500 to-pink-500 bg-clip-text text-transparent animate-shimmer bg-[length:200%_auto]">知识探索</span>
            之旅
          </h1>
          <p class="text-center text-[13px] md:text-sm leading-relaxed text-slate-600/90 dark:text-slate-400 max-w-sm mx-auto mb-7">
            输入或点选主题，AI 将构建知识图谱、逐级拆解概念，并生成详解与配套练习。
          </p>

          <div class="relative group">
            <input
              v-model="input"
              type="text"
              :placeholder="placeholder"
              class="w-full pl-5 pr-16 py-4 rounded-2xl text-sm text-slate-800 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500 bg-white/40 dark:bg-slate-950/40 border border-white/30 dark:border-slate-800/60 focus:outline-none focus:border-indigo-500 focus:bg-white/70 dark:focus:bg-slate-950/60 focus:ring-4 focus:ring-indigo-500/10 dark:focus:ring-indigo-500/5 transition-all shadow-inner"
              @keydown.enter="submit"
            />
            <button
              type="button"
              class="absolute right-2 top-1/2 -translate-y-1/2 w-11 h-11 rounded-xl flex items-center justify-center text-white bg-gradient-to-r from-indigo-500 to-purple-600 hover:from-indigo-600 hover:to-purple-700 shadow-md shadow-indigo-200/40 dark:shadow-none transition-all duration-300 active:scale-95 hover:scale-[1.03]"
              @click="submit"
            >
              <Search class="w-4.5 h-4.5" />
            </button>
          </div>

          <div v-if="featuredTopics.length" class="mt-6">
            <p class="text-[11px] font-semibold text-slate-400 dark:text-slate-500 mb-3 text-center tracking-wider uppercase">热门探索</p>
            <div class="flex flex-wrap items-center justify-center gap-2">
              <button
                v-for="t in featuredTopics"
                :key="t.id"
                type="button"
                class="inline-flex items-center gap-1 px-3.5 py-1.5 rounded-full text-[12px] font-medium text-slate-600 dark:text-slate-300 bg-white/45 dark:bg-slate-950/30 border border-white/30 dark:border-slate-800/50 hover:border-indigo-400 dark:hover:border-indigo-500/50 hover:bg-white/60 dark:hover:bg-slate-950/50 hover:text-indigo-600 dark:hover:text-indigo-300 transition-all duration-300 hover:scale-[1.03] active:scale-[0.97]"
                @click="pick(t)"
              >
                {{ t.title }}
                <ArrowRight class="w-3 h-3 opacity-60 group-hover:translate-x-0.5 transition-transform" />
              </button>
            </div>
          </div>

          <div class="mt-6 flex items-center justify-center">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-semibold text-slate-500 dark:text-slate-400 hover:text-indigo-600 dark:hover:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-500/10 transition-all duration-300"
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
    radial-gradient(ellipse 70% 55% at 50% -5%, rgba(99, 102, 241, 0.15), transparent 55%),
    radial-gradient(ellipse 50% 45% at 90% 90%, rgba(139, 92, 246, 0.08), transparent 50%),
    radial-gradient(ellipse 40% 40% at 5% 70%, rgba(59, 130, 246, 0.06), transparent 45%);
}
:global(.dark) .galaxy-bg {
  background:
    radial-gradient(ellipse 70% 55% at 50% -5%, rgba(99, 102, 241, 0.22), transparent 55%),
    radial-gradient(ellipse 50% 45% at 90% 90%, rgba(139, 92, 246, 0.12), transparent 50%),
    radial-gradient(ellipse 40% 40% at 5% 70%, rgba(59, 130, 246, 0.09), transparent 45%);
}

.galaxy-mesh {
  background-image:
    radial-gradient(at 20% 30%, rgba(99, 102, 241, 0.08) 0, transparent 50%),
    radial-gradient(at 80% 20%, rgba(139, 92, 246, 0.07) 0, transparent 45%),
    radial-gradient(at 60% 80%, rgba(79, 70, 229, 0.05) 0, transparent 40%);
}

.galaxy-grid {
  background-image: radial-gradient(circle, rgba(99, 102, 241, 0.08) 1px, transparent 1px);
  background-size: 32px 32px;
  mask-image: radial-gradient(ellipse 75% 75% at 50% 50%, black 20%, transparent 75%);
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
  border: 1px solid rgba(99, 102, 241, 0.09);
  transition: all 0.5s ease-out;
}
:global(.dark) .galaxy-orbit {
  border-color: rgba(129, 140, 248, 0.13);
}
.galaxy-orbit--1 { width: min(480px, 68vw); height: min(480px, 68vw); opacity: 0.7; }
.galaxy-orbit--2 { width: min(680px, 88vw); height: min(680px, 88vw); opacity: 0.45; }
.galaxy-orbit--3 { width: min(860px, 105vw); height: min(860px, 105vw); opacity: 0.22; }

.galaxy-hub {
  left: 50%;
  top: 50%;
  width: 380px;
  height: 380px;
  transform: translate(-50%, -50%);
  background: radial-gradient(circle, rgba(99, 102, 241, 0.09) 0%, transparent 70%);
  filter: blur(28px);
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
