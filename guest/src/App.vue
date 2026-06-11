<script setup>
import { computed, onMounted, ref } from 'vue';
import { useAgentChat } from '@/views/ne-full/composables/useAgentChat';
import MessageList from '@/views/ne-full/components/MessageList.vue';
import GuestChatBox from './components/GuestChatBox.vue';
import GuestHistorySidebar from './components/GuestHistorySidebar.vue';
import GuestLogin from './components/GuestLogin.vue';
import { listGuestConversations } from '@/services/storage/guestIndexedDb';
import { ConversationAPI } from '@/api/conversations';
import { authState, setAuth, logout } from '@/utils/request';

const {
  messages,
  isBusy,
  status,
  selectedKbs,
  agentInfo,
  selectedModel,
  searchEnabled,
  selectedSkills,
  researchMode,
  sendMessage,
  clearMessages,
  loadHistory,
  loadAgentInfo,
  rollbackConversation,
  cancelStream,
} = useAgentChat();

const draftQuestion = ref('');
const localThreadId = ref('');
const loadingConversations = ref(false);
const conversations = ref([]);
const showMobileSidebar = ref(false);
const showHistorySidebar = computed(() => true);

const createThreadId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `t_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
};

const closeMobileSidebar = () => {
  showMobileSidebar.value = false;
};

const toggleMobileSidebar = () => {
  showMobileSidebar.value = !showMobileSidebar.value;
};

const handleSend = async (text) => {
  const isFirstMessage = !localThreadId.value && messages.value.length === 0;
  if (!localThreadId.value) {
    localThreadId.value = createThreadId();
  }
  await sendMessage(text, {
    threadId: localThreadId.value,
    researchMode: 'normal',
    kbIds: selectedKbs.value,
    useSearch: searchEnabled.value,
    selectedSkills: [],
    thinking: false,
    model: selectedModel.value || undefined,
  });
  if (isFirstMessage) {
    await loadConversations();
  }
};

const loadConversations = async () => {
  loadingConversations.value = true;
  try {
    if (!authState.value.token) {
      conversations.value = await listGuestConversations();
    } else {
      const userConvs = await ConversationAPI.list();
      conversations.value = userConvs.map((item) => ({
        threadId: item.thread_id,
        title: item.title,
      }));
    }
  } catch (err) {
    console.error('Failed to load conversations', err);
  } finally {
    loadingConversations.value = false;
  }
};

const handleSelectConversation = async (threadId) => {
  localThreadId.value = threadId;
  closeMobileSidebar();
  await loadHistory(threadId);
};

const handleSelectSuggestion = (question) => {
  const text = String(question || '').trim();
  if (!text || isBusy.value) return;
  void handleSend(text);
};

const countUserTurnsBeforeIndex = (messageIndex) => {
  if (messageIndex <= 0) return 0;
  return messages.value
    .slice(0, messageIndex)
    .filter((msg) => msg.role === 'user').length;
};

const handleEditAndResend = async (payload) => {
  if (isBusy.value || payload.index < 0 || payload.index >= messages.value.length) return;

  const keepUserTurns = countUserTurnsBeforeIndex(payload.index);
  if (!localThreadId.value) return;
  await rollbackConversation(localThreadId.value, keepUserTurns);
  draftQuestion.value = payload.content;
};

const handleAbort = () => {
  cancelStream();
};

const handleNewChat = async () => {
  clearMessages();
  localThreadId.value = '';
  draftQuestion.value = '';
  closeMobileSidebar();
  await loadConversations();
};

const handleLoginSuccess = () => {
  loadAgentInfo();
  searchEnabled.value = false;
  selectedSkills.value = [];
  researchMode.value = 'normal';
  void loadConversations();
};

const handleLogout = () => {
  logout();
};

onMounted(() => {
  if (authState.value.token) {
    loadAgentInfo();
    searchEnabled.value = false;
    selectedSkills.value = [];
    researchMode.value = 'normal';
    void loadConversations();
  }
});
</script>

<template>
  <div class="w-full h-screen bg-white dark:bg-[#0b0f1a] relative overflow-hidden">
    <!-- Unauthenticated Login View -->
    <div v-if="!authState.token" class="w-full h-full flex items-center justify-center">
      <GuestLogin @login-success="handleLoginSuccess" />
    </div>

    <!-- Authenticated App View -->
    <div v-else class="flex flex-col h-screen w-full relative overflow-hidden">
      <div class="absolute inset-0 bg-[radial-gradient(circle_at_50%_-10%,#eff6ff,transparent_50%)] dark:bg-[radial-gradient(circle_at_50%_-10%,#1e1b4b,transparent_60%)] pointer-events-none opacity-30"></div>

      <header class="h-14 border-b border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-900/85 backdrop-blur-md px-4 md:px-6 flex items-center justify-between sticky top-0 z-50">
        <div class="flex items-center space-x-3 min-w-0">
          <button
            @click="toggleMobileSidebar"
            class="md:hidden inline-flex items-center justify-center w-9 h-9 rounded-xl border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-300 shrink-0"
            title="会话记录"
            aria-label="展开会话记录"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/>
            </svg>
          </button>

          <div class="flex items-center space-x-1.5 min-w-0">
            <div class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse shrink-0"></div>
            <h1 class="text-sm font-bold text-slate-800 dark:text-slate-100 tracking-tight truncate">核电子学智能助手</h1>
          </div>
        </div>
        
        <div class="flex items-center gap-2 md:gap-3 shrink-0">
          <span class="hidden sm:inline text-xs font-medium text-slate-600 dark:text-slate-300 bg-slate-100 dark:bg-slate-800 px-2.5 py-1 rounded-full border border-slate-200 dark:border-slate-700">
            {{ authState.user?.display_name || authState.user?.username || '已登录' }}
          </span>
          <button 
            @click="handleLogout" 
            class="text-xs font-bold text-rose-500 hover:text-rose-600 bg-rose-50 hover:bg-rose-100 dark:bg-rose-950/30 dark:hover:bg-rose-900/40 px-3 py-1.5 rounded-xl border border-rose-100 dark:border-rose-900/55 transition-all"
          >
            退出
          </button>
        </div>
      </header>

      <Transition name="mobile-drawer">
        <div
          v-if="showMobileSidebar"
          class="fixed inset-0 z-[55] bg-black/35 md:hidden"
          @click.self="closeMobileSidebar"
        >
          <div class="absolute left-0 top-0 h-full w-[84%] max-w-[320px] shadow-2xl border-r border-slate-200 dark:border-slate-800">
            <div class="h-14 px-4 flex items-center justify-between border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-[#0f172a]">
              <span class="text-sm font-bold text-slate-800 dark:text-slate-100">会话记录</span>
              <button
                @click="closeMobileSidebar"
                class="inline-flex items-center justify-center w-8 h-8 rounded-lg text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
                aria-label="关闭会话记录"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
              </button>
            </div>
            <GuestHistorySidebar
              class="h-[calc(100%-3.5rem)]"
              :conversations="conversations"
              :loading-conversations="loadingConversations"
              :active-thread-id="localThreadId"
              @select="handleSelectConversation"
              @new-chat="handleNewChat"
            />
          </div>
        </div>
      </Transition>

      <div class="flex-1 min-h-0 flex overflow-hidden relative z-10">
        <aside
          v-if="showHistorySidebar"
          class="hidden md:flex w-72 h-full flex-col border-r border-slate-200 dark:border-slate-800 transition-all duration-300"
        >
          <GuestHistorySidebar
            :conversations="conversations"
            :loading-conversations="loadingConversations"
            :active-thread-id="localThreadId"
            @select="handleSelectConversation"
            @new-chat="handleNewChat"
          />
        </aside>

        <main class="flex-1 min-h-0 overflow-hidden flex flex-col relative w-full items-center">
          <MessageList
            :messages="messages"
            :is-busy="isBusy"
            @select-suggestion="handleSelectSuggestion"
            @edit-user-message="handleEditAndResend"
          />
          <div class="w-full bg-transparent pointer-events-none sticky bottom-0 flex justify-center pb-1 md:pb-8">
            <div class="w-full max-w-4xl relative pointer-events-auto">
              <GuestChatBox
                :is-busy="isBusy"
                :status="status"
                :selected-kbs="selectedKbs"
                :model-value="draftQuestion"
                :search-enabled="searchEnabled"
                @update:selected-kbs="selectedKbs = $event"
                @update:model-value="draftQuestion = $event"
                @update:search-enabled="searchEnabled = $event"
                @send="handleSend"
                @abort="handleAbort"
              />
            </div>
          </div>
        </main>
      </div>
    </div>
  </div>
</template>

<style scoped>
main::before {
  content: "";
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 20px;
  background: linear-gradient(to bottom, #ffffff, transparent);
  z-index: 10;
  pointer-events: none;
}

.dark main::before {
  background: linear-gradient(to bottom, #0b0f1a, transparent);
}

.mobile-drawer-enter-active,
.mobile-drawer-leave-active {
  transition: opacity 0.2s ease;
}

.mobile-drawer-enter-from,
.mobile-drawer-leave-to {
  opacity: 0;
}
</style>
