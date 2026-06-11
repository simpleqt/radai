<script setup lang="ts">
import { ref, watch } from 'vue';
import { X, BookOpen, Link2, ClipboardList } from 'lucide-vue-next';
import DetailTab from './DetailTab.vue';
import ResourcesTab from './ResourcesTab.vue';
import QuizTab from './QuizTab.vue';

const props = defineProps<{
  nodeTitle: string;
  path: string[];
  kbIds: string[];
}>();

defineEmits<{ (e: 'close'): void }>();

type TabKey = 'detail' | 'resources' | 'quiz';
const activeTab = ref<TabKey>('detail');

const TABS: { key: TabKey; label: string; icon: any }[] = [
  { key: 'detail', label: '知识点详解', icon: BookOpen },
  { key: 'resources', label: '关联资源', icon: Link2 },
  { key: 'quiz', label: '习题练习', icon: ClipboardList },
];

// 切换节点时回到详解 Tab
watch(() => props.nodeTitle, () => { activeTab.value = 'detail'; });
</script>

<template>
  <div class="h-full flex flex-col bg-white dark:bg-[#0f172a]">
    <!-- 头部 -->
    <div class="shrink-0 bg-gradient-to-r from-indigo-600 to-violet-600 px-4 py-3.5 flex items-center justify-between">
      <div class="min-w-0">
        <h2 class="text-base font-bold text-white truncate">{{ nodeTitle }}</h2>
        <p class="text-[11px] text-indigo-100/80 truncate">{{ path.join(' / ') }}</p>
      </div>
      <button type="button" class="shrink-0 w-8 h-8 rounded-lg hover:bg-white/20 text-white flex items-center justify-center transition-colors" @click="$emit('close')">
        <X class="w-5 h-5" />
      </button>
    </div>

    <!-- Tabs -->
    <div class="shrink-0 flex border-b border-slate-200 dark:border-slate-800 px-2">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        type="button"
        class="relative flex items-center gap-1.5 px-3.5 py-3 text-[13px] font-medium transition-colors"
        :class="activeTab === tab.key ? 'text-indigo-600 dark:text-indigo-400' : 'text-slate-400 hover:text-slate-600 dark:hover:text-slate-200'"
        @click="activeTab = tab.key"
      >
        <component :is="tab.icon" class="w-4 h-4" />
        {{ tab.label }}
        <span v-if="activeTab === tab.key" class="absolute bottom-0 left-2 right-2 h-0.5 bg-indigo-600 dark:bg-indigo-400 rounded-full" />
      </button>
    </div>

    <!-- 内容：KeepAlive 缓存各 Tab，切换时不销毁已生成内容 -->
    <div class="flex-1 min-h-0">
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
    </div>
  </div>
</template>
