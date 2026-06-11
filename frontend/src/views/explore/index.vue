<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import { ArrowLeft, Database, Loader2, RotateCcw } from 'lucide-vue-next';
import NavRail from '@/components/NavRail.vue';
import { useExploreSession } from './composables/useExploreSession';
import TopicGalaxy from './components/TopicGalaxy.vue';
import MindMapCanvas from './components/MindMapCanvas.vue';
import KnowledgePanel from './components/KnowledgePanel.vue';
import type { MapNode } from './composables/useExploreSession';

const session = useExploreSession();
const route = useRoute();
const {
  view, currentTopic, kbId, kbIds, availableKbs, root, selectedNode,
  loadingMap, errorMsg,
  loadAgentInfo, startTopic, toggleExpand, selectNode, closePanel, backToGalaxy, reset, getPath,
} = session;

const selectedPath = computed(() => (selectedNode.value ? getPath(selectedNode.value.id) : []));
const isMobileLayout = computed(() => route.path === '/m/explore');

// 知识面板拉伸调整宽度状态
const panelWidth = ref(540); // 默认初始宽度扩大到 540px，更易于阅读
const isDragging = ref(false);

function startResize(e: MouseEvent) {
  e.preventDefault();
  isDragging.value = true;
  
  const startX = e.clientX;
  const startWidth = panelWidth.value;
  
  function doResize(moveEvent: MouseEvent) {
    if (!isDragging.value) return;
    const dx = startX - moveEvent.clientX; // 向左拖拽时宽度增加
    const newWidth = startWidth + dx;
    // 约束宽度在 380px 至 75% 视口宽度之间
    const maxWidth = window.innerWidth * 0.75;
    panelWidth.value = Math.min(maxWidth, Math.max(380, newWidth));
  }
  
  function stopResize() {
    isDragging.value = false;
    window.removeEventListener('mousemove', doResize);
    window.removeEventListener('mouseup', stopResize);
  }
  
  window.addEventListener('mousemove', doResize);
  window.addEventListener('mouseup', stopResize);
}

function handleStart(topic: string) {
  startTopic(topic);
}

function handleSelect(node: MapNode) {
  selectNode(node);
}

onMounted(async () => {
  await loadAgentInfo();
});
</script>

<template>
  <div class="flex flex-col h-screen bg-slate-50 dark:bg-[#0b0f1a] relative overflow-hidden">
    <!-- Header -->
    <header class="h-14 border-b border-slate-200/80 dark:border-slate-800/80 bg-white/85 dark:bg-slate-900/85 backdrop-blur-xl px-4 md:px-6 flex items-center justify-between sticky top-0 z-50">
      <div class="flex items-center gap-3 min-w-0">
        <button
          v-if="view === 'map'"
          type="button"
          class="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-indigo-600 dark:hover:text-indigo-400 px-2.5 py-1.5 rounded-lg hover:bg-indigo-50 dark:hover:bg-indigo-500/10 transition-colors"
          @click="backToGalaxy"
        >
          <ArrowLeft class="w-4 h-4" />
          返回
        </button>
        <div class="flex items-center gap-2">
          <div class="w-7 h-7 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shadow-sm shadow-indigo-200/50 dark:shadow-none">
            <span class="text-white text-[10px] font-bold">探</span>
          </div>
          <div>
            <h1 class="text-sm font-semibold text-slate-800 dark:text-slate-100 tracking-tight leading-none">探索式学习</h1>
            <p v-if="view === 'map' && currentTopic" class="text-[10px] text-slate-400 truncate max-w-[180px] mt-0.5">{{ currentTopic }}</p>
          </div>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <div v-if="availableKbs.length > 0" class="hidden sm:flex items-center gap-1.5 text-xs text-slate-400 bg-slate-100/80 dark:bg-slate-800/80 rounded-lg px-2.5 py-1.5 border border-slate-200/60 dark:border-slate-700/60">
          <Database class="w-3.5 h-3.5 text-indigo-600 dark:text-indigo-400" />
          <select
            v-model="kbId"
            class="text-xs bg-transparent text-slate-600 dark:text-slate-300 focus:outline-none max-w-[160px] cursor-pointer"
          >
            <option v-for="kb in availableKbs" :key="kb.id" :value="kb.id">{{ kb.label }}</option>
          </select>
        </div>
        <button
          v-if="view === 'map'"
          type="button"
          class="inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-rose-500 px-2.5 py-1.5 rounded-lg hover:bg-rose-50 dark:hover:bg-rose-500/10 transition-colors"
          @click="reset"
          title="重新开始"
        >
          <RotateCcw class="w-3.5 h-3.5" />
          <span class="hidden md:inline">重新开始</span>
        </button>
      </div>
    </header>

    <!-- Body -->
    <div class="flex-1 min-h-0 flex overflow-hidden relative z-10">
      <NavRail />

      <!-- 主区 -->
      <main class="flex-1 min-h-0 relative overflow-hidden">
        <!-- 发现态 -->
        <TopicGalaxy v-if="view === 'galaxy'" :kb-ids="kbIds" @start="handleStart" />

        <!-- 脑图加载中 -->
        <div v-else-if="loadingMap && !root" class="absolute inset-0 flex flex-col items-center justify-center gap-4 text-slate-400 bg-slate-50 dark:bg-[#0b0f1a]">
          <div class="relative">
            <div class="w-14 h-14 rounded-2xl bg-indigo-500/10 flex items-center justify-center">
              <Loader2 class="w-7 h-7 animate-spin text-indigo-600 dark:text-indigo-400" />
            </div>
          </div>
          <div class="text-center">
            <p class="text-sm font-medium text-slate-600 dark:text-slate-300">正在构建知识图谱</p>
            <p class="text-xs text-slate-400 mt-1">「{{ currentTopic }}」</p>
          </div>
        </div>

        <!-- 脑图态 -->
        <MindMapCanvas
          v-else-if="root"
          :root="root"
          :selected-id="selectedNode?.id || null"
          @select="handleSelect"
          @toggle="toggleExpand"
        />

        <div v-if="errorMsg" class="absolute bottom-4 left-1/2 -translate-x-1/2 bg-rose-50 dark:bg-rose-900/30 text-rose-600 dark:text-rose-300 text-sm px-4 py-2 rounded-xl border border-rose-200 dark:border-rose-800">
          {{ errorMsg }}
        </div>
      </main>

      <!-- 知识面板（桌面：右侧列，支持左右拖动拉伸） -->
      <aside
        v-if="selectedNode && !isMobileLayout"
        class="flex shrink-0 border-l border-slate-200 dark:border-slate-800 relative"
        :class="{ 'select-none': isDragging }"
        :style="{ width: `${panelWidth}px` }"
      >
        <!-- 边缘拖拽拉伸把手 -->
        <div
          class="absolute left-0 top-0 bottom-0 w-1.5 cursor-col-resize hover:bg-indigo-500/30 active:bg-indigo-500/50 transition-colors z-50 flex items-center justify-center group"
          @mousedown="startResize"
        >
          <!-- 悬停可见的小拖拽竖线指示器 -->
          <div class="w-[2px] h-8 rounded-full bg-slate-300 dark:bg-slate-700 group-hover:bg-indigo-500 group-active:bg-indigo-600 transition-colors opacity-0 group-hover:opacity-100" />
        </div>

        <KnowledgePanel
          class="w-full h-full"
          :node-title="selectedNode.title"
          :path="selectedPath"
          :kb-ids="kbIds"
          @close="closePanel"
        />
      </aside>
    </div>

    <!-- 知识面板（移动：底部 Sheet） -->
    <Transition name="sheet">
      <div v-if="selectedNode && isMobileLayout" class="fixed inset-0 z-[60] flex flex-col justify-end" @click.self="closePanel">
        <div class="absolute inset-0 bg-black/35" @click="closePanel" />
        <div class="relative h-[85%] rounded-t-2xl overflow-hidden shadow-2xl">
          <KnowledgePanel
            :node-title="selectedNode.title"
            :path="selectedPath"
            :kb-ids="kbIds"
            @close="closePanel"
          />
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.sheet-enter-active, .sheet-leave-active { transition: opacity 0.25s ease; }
.sheet-enter-from, .sheet-leave-to { opacity: 0; }
</style>
