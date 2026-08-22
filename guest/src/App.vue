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

const GUEST_ACTIVE_THREAD_PREFIX = 'guest_active_thread';

const resolveUserScope = () => {
  const user = authState.value.user || {};
  return String(user.user_id || user.id || user.username || 'default');
};

const readActiveThreadId = () => {
  try {
    return sessionStorage.getItem(`${GUEST_ACTIVE_THREAD_PREFIX}:${resolveUserScope()}`) || '';
  } catch {
    return '';
  }
};

const writeActiveThreadId = (threadId) => {
  const key = `${GUEST_ACTIVE_THREAD_PREFIX}:${resolveUserScope()}`;
  try {
    if (threadId) {
      sessionStorage.setItem(key, threadId);
    } else {
      sessionStorage.removeItem(key);
    }
  } catch {
    // ignore storage failures
  }
};

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
const loadingMoreConversations = ref(false);
const hasMoreConversations = ref(true);
const nextConversationOffset = ref(0);
const HISTORY_PAGE_SIZE = 20;
const conversations = ref([]);
const showMobileSidebar = ref(false);
const showLoginModal = ref(false);
const loginPrompt = ref('');
const isLoggedIn = computed(() => Boolean(authState.value.token));
const showHistorySidebar = computed(() => true);

const requireLogin = (feature = '') => {
  loginPrompt.value = feature ? `${feature}需完成注册或登录后方可使用` : '';
  showLoginModal.value = true;
};

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
  if (!localThreadId.value) {
    localThreadId.value = createThreadId();
  }
  writeActiveThreadId(localThreadId.value);
  await sendMessage(text, {
    threadId: localThreadId.value,
    researchMode: 'normal',
    kbIds: selectedKbs.value,
    useSearch: searchEnabled.value,
    selectedSkills: [],
    thinking: false,
    model: selectedModel.value || undefined,
  });
  await loadConversations();
};

const loadConversations = async (append = false) => {
  if (append && (
    loadingConversations.value
    || loadingMoreConversations.value
    || !hasMoreConversations.value
    || !authState.value.token
  )) return;
  if (append) {
    loadingMoreConversations.value = true;
  } else {
    loadingConversations.value = true;
  }
  try {
    if (!authState.value.token) {
      conversations.value = await listGuestConversations();
      nextConversationOffset.value = 0;
      hasMoreConversations.value = false;
      return;
    }

    const userConvs = await ConversationAPI.list({
      limit: HISTORY_PAGE_SIZE,
      offset: append ? nextConversationOffset.value : 0,
    });
    const nextPage = userConvs.map((item) => ({
      threadId: item.thread_id,
      title: item.title,
    }));
    if (append) {
      const existingIds = new Set(conversations.value.map((item) => item.threadId));
      conversations.value.push(...nextPage.filter((item) => !existingIds.has(item.threadId)));
    } else {
      conversations.value = nextPage;
    }
    nextConversationOffset.value = (append ? nextConversationOffset.value : 0) + userConvs.length;
    hasMoreConversations.value = userConvs.length === HISTORY_PAGE_SIZE;
  } catch (err) {
    console.error('Failed to load conversations', err);
    if (!append) conversations.value = [];
  } finally {
    if (append) {
      loadingMoreConversations.value = false;
    } else {
      loadingConversations.value = false;
    }
  }
};

const handleSelectConversation = async (threadId) => {
  if (!isLoggedIn.value) {
    requireLogin('历史记录');
    return;
  }
  localThreadId.value = threadId;
  writeActiveThreadId(threadId);
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
  writeActiveThreadId('');
  draftQuestion.value = '';
  closeMobileSidebar();
  await loadConversations();
};

const handleLoginSuccess = async () => {
  showLoginModal.value = false;
  loginPrompt.value = '';
  loadAgentInfo();
  searchEnabled.value = false;
  selectedSkills.value = [];
  researchMode.value = 'normal';
  await loadConversations();
  await restoreActiveConversation();
};

const handleLogout = () => {
  writeActiveThreadId('');
  logout({ redirectTo: window.location.pathname || '/' });
  clearMessages();
  localThreadId.value = '';
  conversations.value = [];
  searchEnabled.value = false;
};

const handleDeleteConversation = async (threadId) => {
  if (!isLoggedIn.value) {
    requireLogin('历史记录');
    return;
  }
  if (!window.confirm('确定删除这条历史记录吗？删除后无法恢复。')) return;
  try {
    await ConversationAPI.delete(threadId);
    if (localThreadId.value === threadId) {
      clearMessages();
      localThreadId.value = '';
      writeActiveThreadId('');
    }
    await loadConversations();
  } catch (err) {
    window.alert(err?.message || '删除失败，请稍后重试');
  }
};

const restoreActiveConversation = async () => {
  const savedThreadId = readActiveThreadId();
  if (!savedThreadId) return;
  localThreadId.value = savedThreadId;
  await loadHistory(savedThreadId);
};

onMounted(async () => {
  if (authState.value.token) {
    loadAgentInfo();
    searchEnabled.value = false;
    selectedSkills.value = [];
    researchMode.value = 'normal';
    await loadConversations();
    await restoreActiveConversation();
  } else {
    conversations.value = [];
    searchEnabled.value = false;
  }
});
</script>

<template>
  <div class="w-full h-screen bg-white dark:bg-[#0b0f1a] relative overflow-hidden">
    <div class="flex flex-col h-screen w-full relative overflow-hidden">
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
            <h1 class="text-sm font-bold text-slate-800 dark:text-slate-100 tracking-tight truncate">核仪AI</h1>
          </div>
        </div>
        
        <div class="flex items-center gap-2 md:gap-3 shrink-0">
          <span class="hidden sm:inline text-xs font-medium text-slate-600 dark:text-slate-300 bg-slate-100 dark:bg-slate-800 px-2.5 py-1 rounded-full border border-slate-200 dark:border-slate-700">
            {{ isLoggedIn ? (authState.user?.display_name || authState.user?.username || '已登录') : '游客模式' }}
          </span>
          <button v-if="isLoggedIn"
            @click="handleLogout" 
            class="text-xs font-bold text-rose-500 hover:text-rose-600 bg-rose-50 hover:bg-rose-100 dark:bg-rose-950/30 dark:hover:bg-rose-900/40 px-3 py-1.5 rounded-xl border border-rose-100 dark:border-rose-900/55 transition-all"
          >
            退出
          </button>
          <button v-else type="button" @click="requireLogin()" class="text-xs font-bold text-indigo-600 bg-indigo-50 hover:bg-indigo-100 px-3 py-1.5 rounded-xl border border-indigo-100 transition-all">
            登录/注册
          </button>
        </div>
      </header>

      <Transition name="mobile-drawer">
        <div
          v-if="showMobileSidebar"
          class="fixed inset-0 z-[55] bg-black/35 md:hidden"
          @click.self="closeMobileSidebar"
        >
          <div class="mobile-drawer-panel absolute left-0 top-0 h-full w-[84%] max-w-[320px] shadow-2xl border-r border-slate-200 dark:border-slate-800">
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
              :loading-more="loadingMoreConversations"
              :has-more="hasMoreConversations"
              :active-thread-id="localThreadId"
              :locked="!isLoggedIn"
              @select="handleSelectConversation"
              @locked-click="requireLogin('历史记录')"
              @delete="handleDeleteConversation"
              @load-more="loadConversations(true)"
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
            :loading-more="loadingMoreConversations"
            :has-more="hasMoreConversations"
            :active-thread-id="localThreadId"
            :locked="!isLoggedIn"
            @select="handleSelectConversation"
            @locked-click="requireLogin('历史记录')"
            @delete="handleDeleteConversation"
            @load-more="loadConversations(true)"
            @new-chat="handleNewChat"
          />
        </aside>

        <!-- 命名容器供 MessageList 的 @cozy/main 等容器查询使用 -->
        <main class="@container/main flex-1 min-h-0 overflow-hidden flex flex-col relative w-full items-center">
          <MessageList
            :messages="messages"
            :is-busy="isBusy"
            empty-logo-src="/log.png?v=heyi-ai-20260628-05"
            empty-logo-alt="核仪AI"
            :show-empty-greeting="false"
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
                :search-locked="!isLoggedIn"
                @update:selected-kbs="selectedKbs = $event"
                @update:model-value="draftQuestion = $event"
                @update:search-enabled="searchEnabled = $event"
                @search-locked-click="requireLogin('联网搜索')"
                @send="handleSend"
                @abort="handleAbort"
              />
            </div>
          </div>
        </main>
      </div>
    </div>

    <Transition name="pop">
    <div v-if="showLoginModal" class="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-sm" @click.self="showLoginModal = false">
      <div class="pop-panel relative max-h-[94vh] w-full max-w-md overflow-y-auto rounded-[28px] bg-white shadow-2xl dark:bg-slate-900">
        <button type="button" class="absolute right-4 top-4 z-20 flex h-9 w-9 items-center justify-center rounded-full bg-slate-100 text-xl text-slate-500 hover:bg-slate-200" aria-label="关闭" @click="showLoginModal = false">×</button>
        <p v-if="loginPrompt" class="absolute left-6 right-16 top-5 z-20 text-xs font-semibold text-amber-600">{{ loginPrompt }}</p>
        <GuestLogin :embedded="true" @login-success="handleLoginSuccess" />
      </div>
    </div>
    </Transition>
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

/* 移动端抽屉：遮罩淡入淡出，面板自左侧滑入 */
.mobile-drawer-enter-active,
.mobile-drawer-leave-active {
  transition: opacity 0.2s ease;
}
.mobile-drawer-enter-active .mobile-drawer-panel {
  transition: transform 0.25s cubic-bezier(0.32, 0.72, 0, 1);
}
.mobile-drawer-leave-active .mobile-drawer-panel {
  transition: transform 0.2s ease;
}

.mobile-drawer-enter-from,
.mobile-drawer-leave-to {
  opacity: 0;
}
.mobile-drawer-enter-from .mobile-drawer-panel,
.mobile-drawer-leave-to .mobile-drawer-panel {
  transform: translateX(-100%);
}
</style>
