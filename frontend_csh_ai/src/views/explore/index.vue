<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ArrowLeft, Database, Loader2, RotateCcw } from 'lucide-vue-next';
import AppShell from '@/components/layout/AppShell.vue';
import { useIsDesktop } from '@/composables/useMediaQuery';
import { authState, logout } from '@/utils/request';
import { useExploreSession } from './composables/useExploreSession';
import TopicGalaxy from './components/TopicGalaxy.vue';
import MindMapCanvas from './components/MindMapCanvas.vue';
import KnowledgePanel from './components/KnowledgePanel.vue';
import type { MapNode } from './composables/useExploreSession';

const session = useExploreSession();
const router = useRouter();
const {
  view, currentTopic, kbId, kbIds, availableKbs, root, selectedNode,
  loadingMap, errorMsg,
  loadAgentInfo, startTopic, toggleExpand, selectNode, closePanel, backToGalaxy, reset, getPath,
} = session;

const selectedPath = computed(() => (selectedNode.value ? getPath(selectedNode.value.id) : []));
const isDesktop = useIsDesktop();

// 知识面板拉伸调整宽度状态
const panelWidth = ref(540);
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

const handleAuthAction = () => {
  if (!authState.value.token) {
    router.push('/login');
    return;
  }
  logout();
};

onMounted(async () => {
  await loadAgentInfo();
});
</script>

<template>
  <AppShell
    title="探索式学习"
    brand-name="成理智学助手"
    brand-icon="Waves"
    @auth-action="handleAuthAction"
  >
    <template #actions>
      <div
        v-if="availableKbs.length > 0"
        class="hidden items-center gap-1.5 rounded-(--radius-control) border border-border-subtle px-2 py-1 @cozy/main:flex"
      >
        <Database class="size-3.5 shrink-0 text-accent" />
        <select
          v-model="kbId"
          class="max-w-40 cursor-pointer bg-transparent text-xs text-text-secondary focus:outline-none"
        >
          <option v-for="kb in availableKbs" :key="kb.id" :value="kb.id">{{ kb.label }}</option>
        </select>
      </div>

      <button
        v-if="view === 'map'"
        type="button"
        class="inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs font-medium text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary"
        title="返回主题列表"
        @click="backToGalaxy"
      >
        <ArrowLeft class="size-4" />
        <span class="hidden @cozy/main:inline">返回</span>
      </button>

      <button
        v-if="view === 'map'"
        type="button"
        class="inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-xs font-medium text-text-secondary transition-colors hover:bg-surface-hover hover:text-rose-500"
        title="重新开始"
        @click="reset"
      >
        <RotateCcw class="size-3.5" />
        <span class="hidden @wide/main:inline">重新开始</span>
      </button>
    </template>

    <div class="flex min-h-0 flex-1 overflow-hidden">
      <main class="relative min-h-0 min-w-0 flex-1 overflow-hidden bg-surface-sunken">
        <TopicGalaxy v-if="view === 'galaxy'" :kb-ids="kbIds" @start="handleStart" />

        <div
          v-else-if="loadingMap && !root"
          class="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-surface-sunken text-text-tertiary"
        >
          <span class="flex size-14 items-center justify-center rounded-2xl bg-accent-soft">
            <Loader2 class="size-7 animate-spin text-accent" />
          </span>
          <div class="text-center">
            <p class="text-sm font-medium text-text-secondary">正在构建学习图谱</p>
            <p class="mt-1 text-xs text-text-tertiary">「{{ currentTopic }}」</p>
          </div>
        </div>

        <MindMapCanvas
          v-else-if="root"
          :root="root"
          :selected-id="selectedNode?.id || null"
          @select="handleSelect"
          @toggle="toggleExpand"
        />

        <p
          v-if="errorMsg"
          class="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-xl border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-600 dark:border-rose-800 dark:bg-rose-900/30 dark:text-rose-300"
        >
          {{ errorMsg }}
        </p>
      </main>

      <!-- 知识面板（宽屏：右侧列，可拖动拉伸） -->
      <aside
        v-if="selectedNode && isDesktop"
        class="relative flex shrink-0 border-l border-border-subtle"
        :class="{ 'select-none': isDragging }"
        :style="{ width: `${panelWidth}px` }"
      >
        <div
          class="group absolute inset-y-0 left-0 z-50 flex w-1.5 cursor-col-resize items-center justify-center transition-colors hover:bg-accent/30 active:bg-accent/50"
          @mousedown="startResize"
        >
          <span
            class="h-8 w-0.5 rounded-full bg-border-strong opacity-0 transition-opacity group-hover:opacity-100"
          />
        </div>

        <KnowledgePanel
          class="h-full w-full"
          :node-title="selectedNode.title"
          :path="selectedPath"
          :kb-ids="kbIds"
          @close="closePanel"
        />
      </aside>
    </div>

    <!-- 知识面板（窄屏：底部 Sheet） -->
    <Transition name="sheet">
      <div
        v-if="selectedNode && !isDesktop"
        class="fixed inset-0 z-[60] flex flex-col justify-end"
        role="dialog"
        aria-modal="true"
        aria-label="知识节点详情"
        @click.self="closePanel"
      >
        <div class="absolute inset-0 bg-black/40" @click="closePanel" />
        <div class="relative h-[85%] overflow-hidden rounded-t-2xl shadow-panel">
          <KnowledgePanel
            :node-title="selectedNode.title"
            :path="selectedPath"
            :kb-ids="kbIds"
            @close="closePanel"
          />
        </div>
      </div>
    </Transition>
  </AppShell>
</template>

<style scoped>
.sheet-enter-active,
.sheet-leave-active {
  transition: opacity 0.2s ease;
}
.sheet-enter-from,
.sheet-leave-to {
  opacity: 0;
}
</style>
