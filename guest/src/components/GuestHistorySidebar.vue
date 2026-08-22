<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

const scrollContainer = ref(null);
const loadMoreSentinel = ref(null);
let loadMoreObserver = null;
const props = defineProps({
  conversations: {
    type: Array,
    default: () => [],
  },
  loadingConversations: {
    type: Boolean,
    default: false,
  },
  loadingMore: { type: Boolean, default: false },
  hasMore: { type: Boolean, default: false },
  activeThreadId: {
    type: String,
    default: '',
  },
  locked: { type: Boolean, default: false },
});

const emit = defineEmits(['select', 'new-chat', 'delete', 'locked-click', 'load-more']);

const handleSelect = (threadId) => {
  emit('select', threadId);
};

const handleNewChat = () => {
  emit('new-chat');
};

const handleHistoryClick = () => emit('locked-click');

const handleDelete = (event, threadId) => {
  event.stopPropagation();
  emit('delete', threadId);
};

const handleScroll = (event) => {
  if (!props.hasMore || props.loadingMore || props.loadingConversations || props.locked) return;
  const element = event.currentTarget;
  const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
  if (remaining <= 80) emit('load-more');
};

const requestMoreIfNeeded = async () => {
  await nextTick();
  const element = scrollContainer.value;
  if (
    !element
    || !props.hasMore
    || props.loadingMore
    || props.loadingConversations
    || props.locked
  ) return;
  if (element.scrollHeight <= element.clientHeight + 80) emit('load-more');
};

onMounted(() => {
  loadMoreObserver = new IntersectionObserver(
    (entries) => {
      if (
        entries.some((entry) => entry.isIntersecting)
        && props.hasMore
        && !props.loadingMore
        && !props.loadingConversations
        && !props.locked
      ) emit('load-more');
    },
    { root: scrollContainer.value, rootMargin: '0px 0px 120px 0px' },
  );
  if (loadMoreSentinel.value) loadMoreObserver.observe(loadMoreSentinel.value);
  requestMoreIfNeeded();
});

watch(
  () => [props.conversations.length, props.loadingMore, props.hasMore],
  requestMoreIfNeeded,
);
watch(loadMoreSentinel, (current, previous) => {
  if (!loadMoreObserver) return;
  if (previous) loadMoreObserver.unobserve(previous);
  if (current) loadMoreObserver.observe(current);
});

onBeforeUnmount(() => loadMoreObserver?.disconnect());
</script>

<template>
  <div class="flex h-full min-h-0 flex-col bg-white dark:bg-[#0f172a]">
    <div class="p-4 md:p-6 border-b border-slate-100 dark:border-slate-800 md:border-b-0">
      <button
        @click="handleNewChat"
        class="w-full flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3.5 rounded-2xl shadow-lg shadow-indigo-200 dark:shadow-none transition-all active:scale-[0.98]"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>
        开启新会话
      </button>
    </div>

    <div ref="scrollContainer" class="min-h-0 flex-1 overflow-y-auto px-4 space-y-1 custom-scrollbar pb-4" :class="locked ? 'cursor-pointer opacity-45 grayscale' : ''" @click="locked && handleHistoryClick()" @scroll.passive="handleScroll">
      <div class="px-2 mb-2 pt-2">
        <span class="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">历史记录</span>
      </div>

      <div v-if="loadingConversations" class="flex flex-col gap-2 px-2 mt-4">
        <div v-for="i in 5" :key="i" class="skeleton-shimmer h-10 bg-slate-100 dark:bg-slate-800/50 rounded-lg animate-pulse"></div>
      </div>

      <template v-else-if="!locked">
        <TransitionGroup name="list-fade" tag="div" class="relative space-y-1">
        <div
          v-for="conv in conversations"
          :key="conv.threadId"
          @click="handleSelect(conv.threadId)"
          :class="[
            'group flex items-center gap-3 px-3 py-3 rounded-xl cursor-pointer transition-all duration-200 border border-transparent active:scale-[0.99]',
            activeThreadId === conv.threadId
              ? 'bg-indigo-50 dark:bg-indigo-500/10 border-indigo-100 dark:border-indigo-500/20 text-indigo-700 dark:text-indigo-400 shadow-sm'
              : 'text-slate-600 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800/50'
          ]"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="opacity-50 shrink-0">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
          </svg>
          <span class="flex-1 text-sm font-medium truncate">{{ conv.title || '新对话' }}</span>
          <button type="button" title="删除记录" aria-label="删除记录" class="rounded-lg p-1.5 text-slate-300 opacity-0 transition hover:bg-rose-50 hover:text-rose-500 group-hover:opacity-100" @click="handleDelete($event, conv.threadId)">
            <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v5M14 11v5"/></svg>
          </button>
        </div>
        </TransitionGroup>
      </template>

      <div v-if="loadingMore" class="py-3 text-center text-[11px] text-slate-400">正在加载更多记录…</div>
      <div v-if="hasMore && !locked" ref="loadMoreSentinel" class="h-px" aria-hidden="true"></div>

      <div v-if="locked" class="rounded-xl border border-dashed border-slate-300 px-3 py-8 text-center text-xs font-medium text-slate-500">登录或注册后查看历史记录</div>

      <div v-if="!locked && !loadingConversations && conversations.length === 0" class="text-center py-10">
        <p class="text-[11px] text-slate-400">暂无历史记录</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.custom-scrollbar::-webkit-scrollbar {
  width: 4px;
}

.custom-scrollbar::-webkit-scrollbar-track {
  background: transparent;
}

.custom-scrollbar::-webkit-scrollbar-thumb {
  background: #e2e8f0;
  border-radius: 10px;
}

.dark .custom-scrollbar::-webkit-scrollbar-thumb {
  background: #1e293b;
}
</style>
