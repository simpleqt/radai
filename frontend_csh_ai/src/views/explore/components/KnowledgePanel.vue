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
  <div class="h-full flex flex-col bg-surface dark:bg-[#0f172a]">
    <!-- 头部 -->
    <div class="shrink-0 border-b border-border-subtle bg-gradient-to-r from-surface via-accent-soft to-surface-sunken px-4 py-3.5 flex items-center justify-between">
      <div class="min-w-0">
        <h2 class="text-base font-bold text-text-primary truncate dark:text-white">{{ nodeTitle }}</h2>
        <p class="text-[11px] text-accent/80 truncate">{{ path.join(' / ') }}</p>
      </div>
      <button type="button" class="shrink-0 w-8 h-8 rounded-xl hover:bg-accent-soft text-text-secondary hover:text-accent flex items-center justify-center transition-colors dark:text-white dark:hover:bg-white/10" @click="$emit('close')">
        <X class="w-5 h-5" />
      </button>
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
