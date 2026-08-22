<script setup lang="ts">
import { ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { X, BookOpen, Link2, ClipboardList, Route } from 'lucide-vue-next';
import DetailTab from './DetailTab.vue';
import ResourcesTab from './ResourcesTab.vue';
import QuizTab from './QuizTab.vue';
import { buildNodeCacheKey, detailCache, resourcesCache, quizCache } from '../composables/exploreNodeCache';

const props = defineProps<{
  nodeTitle: string;
  path: string[];
  kbIds: string[];
}>();

defineEmits<{ (e: 'close'): void }>();

type TabKey = 'detail' | 'resources' | 'quiz';
type LearningIntent = 'target' | 'prerequisite' | 'review';
const router = useRouter();
const activeTab = ref<TabKey>('detail');

const TABS: { key: TabKey; label: string; icon: any }[] = [
  { key: 'detail', label: '知识点详解', icon: BookOpen },
  { key: 'resources', label: '关联资源', icon: Link2 },
  { key: 'quiz', label: '习题练习', icon: ClipboardList },
];

const LEARNING_INTENTS: { key: LearningIntent; label: string }[] = [
  { key: 'target', label: '作为目标' },
  { key: 'prerequisite', label: '作为前置' },
  { key: 'review', label: '复习补强' },
];
const CUSTOM_LEARNING_EXPLORE_SNAPSHOT_KEY = 'custom_learning_explore_snapshot_v1';

function goalForIntent(intent: LearningIntent) {
  const root = props.path[0] || props.nodeTitle;
  if (intent === 'prerequisite') {
    return `把“${props.nodeTitle}”作为前置知识补齐，服务于“${root}”的后续学习`;
  }
  if (intent === 'review') {
    return `围绕“${props.nodeTitle}”做复习补强，定位薄弱点并完成练习检测`;
  }
  return props.path.length > 1
    ? `围绕“${props.nodeTitle}”建立学习路径，理解它在“${root}”中的位置`
    : `系统学习“${props.nodeTitle}”`;
}

function createCustomPlanFromNode(intent: LearningIntent) {
  const prefix = '';
  const detailKey = buildNodeCacheKey(props.nodeTitle, props.kbIds, props.path);
  const resourceKey = buildNodeCacheKey(props.nodeTitle, props.kbIds);
  const quizPrefix = buildNodeCacheKey(props.nodeTitle, props.kbIds);
  const quizEntry = Array.from(quizCache.entries()).find(([key]) => key.startsWith(`${quizPrefix}\0`))?.[1];
  const detailEntry = detailCache.get(detailKey);
  const resourceEntry = resourcesCache.get(resourceKey);
  try {
    sessionStorage.setItem(CUSTOM_LEARNING_EXPLORE_SNAPSHOT_KEY, JSON.stringify({
      source: 'explore',
      intent,
      node_title: props.nodeTitle,
      path: props.path,
      kb_ids: props.kbIds,
      detail: detailEntry?.completed ? {
        content: detailEntry.content,
        reasoning: detailEntry.reasoning,
        sources: detailEntry.sources,
      } : null,
      resources: resourceEntry?.resources || [],
      questions: quizEntry?.questions || [],
      captured_at: new Date().toISOString(),
    }));
  } catch {
    // 快照失败不影响跳转，后续仍可按目标重新生成。
  }
  router.push({
    path: `${prefix}/custom-learning`,
    query: {
      goal: goalForIntent(intent),
      kb_ids: props.kbIds.join(','),
      from: 'explore',
      intent,
      snapshot: '1',
    },
  });
}

// 切换节点时回到详解 Tab
watch(() => props.nodeTitle, () => { activeTab.value = 'detail'; });
</script>

<template>
  <div class="h-full flex flex-col bg-surface dark:bg-[#0f172a]">
    <!-- 头部：对齐场地污染前端的柔和渐变格式（颜色走 accent token，非绿色） -->
    <div class="shrink-0 border-b border-border-subtle bg-gradient-to-r from-surface via-accent-soft to-surface-sunken px-4 py-3.5 flex items-center justify-between">
      <div class="min-w-0">
        <h2 class="text-base font-bold text-text-primary truncate dark:text-white">{{ nodeTitle }}</h2>
        <p class="text-[11px] text-accent/80 truncate">{{ path.join(' / ') }}</p>
      </div>
      <button type="button" class="shrink-0 w-8 h-8 rounded-xl hover:bg-accent-soft text-text-secondary hover:text-accent flex items-center justify-center transition-colors dark:text-white dark:hover:bg-white/10" @click="$emit('close')">
        <X class="w-5 h-5" />
      </button>
    </div>

    <div class="shrink-0 border-b border-border-subtle bg-surface px-3 py-2">
      <div class="mb-2 flex items-center gap-2 text-xs font-semibold text-accent">
        <Route class="h-3.5 w-3.5" />
        加入自定义学习
      </div>
      <div class="grid grid-cols-3 gap-2">
      <button
        v-for="intent in LEARNING_INTENTS"
        :key="intent.key"
        type="button"
        class="inline-flex min-h-9 items-center justify-center rounded-lg border border-accent/30 bg-accent-soft px-2 text-xs font-semibold text-accent transition-colors hover:border-accent hover:bg-accent hover:text-white"
        @click="createCustomPlanFromNode(intent.key)"
      >
        {{ intent.label }}
      </button>
      </div>
    </div>

    <!-- Tabs -->
    <div class="shrink-0 flex border-b border-accent/70 px-2 bg-surface/86">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        type="button"
        class="relative flex items-center gap-1.5 px-3.5 py-3 text-[13px] font-medium transition-colors"
        :class="activeTab === tab.key ? 'text-accent' : 'text-text-tertiary hover:text-text-secondary'"
        @click="activeTab = tab.key"
      >
        <component :is="tab.icon" class="w-4 h-4" />
        {{ tab.label }}
        <span
          v-if="activeTab === tab.key"
          class="animate-in fade-in absolute bottom-0 left-2 right-2 h-0.5 rounded-full bg-accent duration-200"
        />
      </button>
    </div>

    <!-- 内容：KeepAlive 缓存各 Tab，切换时不销毁已生成内容；tab 间交叉淡入 -->
    <div class="flex-1 min-h-0">
      <Transition name="fade" mode="out-in">
      <KeepAlive :max="3">
        <DetailTab
          v-if="activeTab === 'detail'"
          :key="`detail-${nodeTitle}`"
          :node-title="nodeTitle"
          :path="path"
          :kb-ids="kbIds"
        />
        <ResourcesTab
          v-else-if="activeTab === 'resources'"
          :key="`resources-${nodeTitle}`"
          :node-title="nodeTitle"
          :kb-ids="kbIds"
        />
        <QuizTab
          v-else
          :key="`quiz-${nodeTitle}`"
          :node-title="nodeTitle"
          :kb-ids="kbIds"
        />
      </KeepAlive>
      </Transition>
    </div>
  </div>
</template>
