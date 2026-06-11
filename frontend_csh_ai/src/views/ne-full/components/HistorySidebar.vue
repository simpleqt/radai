<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { ConversationAPI, type Conversation } from '@/api/conversations';
import { authState } from '@/utils/request';
import {
  clearGuestConversations,
  deleteGuestConversation,
  listGuestConversations,
  renameGuestConversation,
} from '@/services/storage/guestIndexedDb';

const router = useRouter();
const route = useRoute();
const emit = defineEmits<{
  (e: 'new-chat'): void;
  (e: 'auth-action'): void;
}>();
type UiConversation = {
  threadId: string;
  title: string;
};

const conversations = ref<UiConversation[]>([]);
const loading = ref(false);
const basePath = () => {
  if (route.path === '/groundwater' || route.path.startsWith('/groundwater/')) {
    return '/groundwater';
  }
  return route.path === '/m' || route.path.startsWith('/m/') ? '/m' : '/app';
};
const isGuestMode = computed(() => !authState.value.token);
const displayName = computed(() => {
  const user = authState.value.user || {};
  return String(user.display_name || user.name || user.username || '').trim();
});
const displayBadge = computed(() => {
  const text = displayName.value;
  if (!text) return 'U';
  return text.slice(0, 2).toUpperCase();
});

const loadConversations = async () => {
  loading.value = true;
  try {
    if (isGuestMode.value) {
      const guestConvs = await listGuestConversations();
      conversations.value = guestConvs.map((item) => ({
        threadId: item.threadId,
        title: item.title,
      }));
    } else {
      const userConvs = await ConversationAPI.list();
      conversations.value = userConvs.map((item: Conversation) => ({
        threadId: item.thread_id,
        title: item.title,
      }));
    }
  } catch (err) {
    console.error('Failed to load conversations', err);
  } finally {
    loading.value = false;
  }
};

const selectConversation = (threadId: string) => {
  router.push(`${basePath()}/${threadId}`);
};

const newChat = () => {
  emit('new-chat');
  if (route.path !== basePath() || !!route.params.threadId) {
    router.push(basePath());
  }
};

const handleAuthAction = () => {
  emit('auth-action');
};

const handleDelete = async (e: Event, threadId: string) => {
  e.stopPropagation();
  if (!confirm('确定要删除这个会话吗？')) return;
  try {
    if (isGuestMode.value) {
      await deleteGuestConversation(threadId);
    } else {
      await ConversationAPI.delete(threadId);
    }
    await loadConversations();
    if (route.params.threadId === threadId) {
      router.push(basePath());
    }
  } catch (err) {
    alert('删除失败');
  }
};

const handleRename = async (e: Event, conv: UiConversation) => {
  e.stopPropagation();
  const newTitle = prompt('请输入新的标题', conv.title);
  if (newTitle && newTitle !== conv.title) {
    try {
      if (isGuestMode.value) {
        await renameGuestConversation(conv.threadId, newTitle);
      } else {
        await ConversationAPI.rename(conv.threadId, newTitle);
      }
      await loadConversations();
    } catch (err) {
      alert('修改失败');
    }
  }
};

const handleClearAllGuest = async () => {
  if (!confirm('确定清空本地全部访客会话吗？')) return;
  try {
    await clearGuestConversations();
    await loadConversations();
    if (route.params.threadId) {
      router.push(basePath());
    }
  } catch (e) {
    alert('清空失败');
  }
};

onMounted(() => {
  loadConversations();
});

watch(
  () => authState.value.token,
  () => {
    loadConversations();
  },
);

defineExpose({
  loadConversations
});
</script>

<template>
  <aside class="w-72 h-full flex flex-col bg-white dark:bg-[#0f172a] border-r border-slate-200 dark:border-slate-800 transition-all duration-300">
    <!-- Header -->
    <div class="p-6">
      <button 
        @click="newChat"
        class="w-full flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3.5 rounded-2xl shadow-lg shadow-indigo-200 dark:shadow-none transition-all active:scale-[0.98]"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>
        开启新会话
      </button>

      <div v-if="isGuestMode" class="mt-3 grid grid-cols-1 gap-2">
        <button
          type="button"
          @click="handleClearAllGuest"
          class="text-xs py-2 rounded-xl border border-rose-200 dark:border-rose-700 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-900/20"
        >
          清空访客会话
        </button>
      </div>
    </div>

    <!-- History List -->
    <div class="flex-1 overflow-y-auto px-4 space-y-1 custom-scrollbar pb-4">
      <div v-if="!isGuestMode" class="mb-3">
        <div
          @click="router.push('/my-files')"
          class="flex items-center gap-3 px-3 py-2.5 rounded-xl cursor-pointer transition-all duration-200 border border-transparent text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800/50 hover:text-indigo-600 dark:hover:text-indigo-400 font-semibold"
          :class="{ 'bg-indigo-50 dark:bg-indigo-500/10 border-indigo-100 dark:border-indigo-500/20 text-indigo-700 dark:text-indigo-400 shadow-sm': route.path === '/my-files' }"
        >
          <svg class="w-4.5 h-4.5 shrink-0 text-slate-400 group-hover:text-indigo-500 dark:text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
          </svg>
          <span class="text-sm">个人文件库</span>
        </div>
      </div>

      <div class="px-2 mb-2">
        <span class="text-[10px] font-black uppercase tracking-[0.2em] text-slate-400">历史记录</span>
      </div>

      <div v-if="loading" class="flex flex-col gap-2 px-2 mt-4">
        <div v-for="i in 5" :key="i" class="h-10 bg-slate-100 dark:bg-slate-800/50 rounded-lg animate-pulse"></div>
      </div>

      <div 
        v-else
        v-for="conv in conversations" 
        :key="conv.threadId"
        @click="selectConversation(conv.threadId)"
        :class="[
          'group flex items-center gap-3 px-3 py-3 rounded-xl cursor-pointer transition-all duration-200 border border-transparent',
          route.params.threadId === conv.threadId 
            ? 'bg-indigo-50 dark:bg-indigo-500/10 border-indigo-100 dark:border-indigo-500/20 text-indigo-700 dark:text-indigo-400 shadow-sm' 
            : 'text-slate-600 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800/50'
        ]"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="opacity-50 shrink-0">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
        </svg>
        
        <span class="flex-1 text-sm font-medium truncate">{{ conv.title || '新对话' }}</span>

        <!-- Actions -->
        <div class="hidden group-hover:flex items-center gap-1">
          <button @click="handleRename($event, conv)" class="p-1 hover:text-indigo-600 dark:hover:text-indigo-400">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>
          </button>
          <button @click="handleDelete($event, conv.threadId)" class="p-1 hover:text-rose-500">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
          </button>
        </div>
      </div>

      <div v-if="!loading && conversations.length === 0" class="text-center py-10">
        <p class="text-[11px] text-slate-400">暂无历史记录</p>
      </div>
    </div>

    <!-- User Info (Bottom Left) -->
    <div class="px-4 py-5 border-t border-slate-200 dark:border-slate-800 bg-slate-50/80 dark:bg-slate-900/70">
      <div class="rounded-3xl border border-slate-200/90 dark:border-slate-700/90 bg-white/95 dark:bg-slate-900/95 px-3.5 py-3 shadow-sm">
        <div class="flex items-center gap-3">
          <div class="h-11 w-11 shrink-0 rounded-full border border-slate-200 dark:border-slate-700 bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-300 flex items-center justify-center">
            <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M18 20a6 6 0 0 0-12 0"/>
              <circle cx="12" cy="10" r="4"/>
            </svg>
          </div>

          <div class="flex-1 min-w-0">
            <p class="text-base font-semibold tracking-wide text-slate-800 dark:text-slate-100 truncate">{{ isGuestMode ? '访客模式' : displayName }}</p>
            <p class="text-[11px] text-slate-400 dark:text-slate-500 truncate">{{ isGuestMode ? '本地存储' : '在线' }}</p>
          </div>

          <button
            type="button"
            @click="handleAuthAction"
            class="shrink-0 rounded-full border px-4 py-1.5 text-sm transition-colors"
            :class="isGuestMode ? 'border-indigo-200 dark:border-indigo-700 text-indigo-600 dark:text-indigo-300 hover:bg-indigo-50 dark:hover:bg-indigo-900/20' : 'border-slate-300 dark:border-slate-600 text-slate-500 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800'"
          >
            {{ isGuestMode ? '登录' : '退出' }}
          </button>
        </div>
      </div>
    </div>
  </aside>
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
