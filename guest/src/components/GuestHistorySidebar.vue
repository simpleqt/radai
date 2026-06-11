<script setup>
defineProps({
  conversations: {
    type: Array,
    default: () => [],
  },
  loadingConversations: {
    type: Boolean,
    default: false,
  },
  activeThreadId: {
    type: String,
    default: '',
  },
});

const emit = defineEmits(['select', 'new-chat']);

const handleSelect = (threadId) => {
  emit('select', threadId);
};

const handleNewChat = () => {
  emit('new-chat');
};
</script>

<template>
  <div class="h-full flex flex-col bg-white dark:bg-[#0f172a]">
    <div class="p-4 md:p-6 border-b border-slate-100 dark:border-slate-800 md:border-b-0">
      <button
        @click="handleNewChat"
        class="w-full flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3.5 rounded-2xl shadow-lg shadow-indigo-200 dark:shadow-none transition-all active:scale-[0.98]"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>
        开启新会话
      </button>
    </div>

    <div class="flex-1 overflow-y-auto px-4 space-y-1 custom-scrollbar pb-4">
      <div class="px-2 mb-2 pt-2">
        <span class="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">历史记录</span>
      </div>

      <div v-if="loadingConversations" class="flex flex-col gap-2 px-2 mt-4">
        <div v-for="i in 5" :key="i" class="h-10 bg-slate-100 dark:bg-slate-800/50 rounded-lg animate-pulse"></div>
      </div>

      <template v-else>
        <div
          v-for="conv in conversations"
          :key="conv.threadId"
          @click="handleSelect(conv.threadId)"
          :class="[
            'group flex items-center gap-3 px-3 py-3 rounded-xl cursor-pointer transition-all duration-200 border border-transparent',
            activeThreadId === conv.threadId
              ? 'bg-indigo-50 dark:bg-indigo-500/10 border-indigo-100 dark:border-indigo-500/20 text-indigo-700 dark:text-indigo-400 shadow-sm'
              : 'text-slate-600 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800/50'
          ]"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="opacity-50 shrink-0">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
          </svg>
          <span class="flex-1 text-sm font-medium truncate">{{ conv.title || '新对话' }}</span>
        </div>
      </template>

      <div v-if="!loadingConversations && conversations.length === 0" class="text-center py-10">
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
