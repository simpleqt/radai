<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import ChatBox from './components/ChatBox.vue';
import MessageList from './components/MessageList.vue';
import HistorySidebar from './components/HistorySidebar.vue';
import ArtifactSidebar from './components/ArtifactSidebar.vue';
import MyFilesPickerDialog from './components/MyFilesPickerDialog.vue';
import NavRail from '@/components/NavRail.vue';
import { useAgentChat } from './composables/useAgentChat';
import { useFileUpload } from './composables/useFileUpload';
import { authState, logout } from '@/utils/request';
import { useUiMode } from '@/composables/useUiMode';
import type { ResearchPlanPayload } from '@/api/agent-v2';

const route = useRoute();
const router = useRouter();
const { messages, isBusy, status, agentInfo, selectedModel, selectedKbs, searchEnabled, selectedSkills, researchMode, artifacts, isGuestMode, activeBackgroundTask, backgroundTaskStreaming, loadAgentInfo, loadHistory, sendMessage, clearMessages, rollbackConversation, cancelStream, cancelBackgroundTask, checkAndResumeBackgroundTask, getPendingInterruptedThreadId } = useAgentChat();

const { attachments, isUploading, uploadParseMode, addFiles, uploadPendingFiles, removeAttachment, clearAttachments, getReadyFileIds, addReadyAttachment, setUploadParseMode } = useFileUpload();
const showArtifacts = ref(false);
const artifactResetKey = ref(0);
const sidebarRef = ref<any>(null);
const mobileSidebarRef = ref<any>(null);
const localThreadId = ref<string | null>(null);
const draftQuestion = ref('');
const showMobileSidebar = ref(false);
const showMyFilesPicker = ref(false);
const { mode, effectiveMode } = useUiMode();

const handleConfirmMyFiles = (selectedFiles: any[]) => {
  for (const f of selectedFiles) {
    addReadyAttachment({
      file_id: f.file_id,
      display_name: f.display_name,
      size_bytes: f.size_bytes,
      mime_type: f.mime_type,
      file_category: f.file_category,
      strategy: f.strategy,
    });
  }
};

const isMobileLayout = computed(() => route.path.startsWith('/m'));
const chatBasePath = computed(() => (isMobileLayout.value ? '/m' : '/app'));
// showHistorySidebar can always be true, CSS 'hidden md:flex' handles responsiveness
const showHistorySidebar = computed(() => true);

const getTargetChatPath = (targetMode: 'mobile' | 'desktop') => {
  const targetBase = targetMode === 'mobile' ? '/m' : '/app';
  const threadId = (route.params.threadId as string | undefined) || localThreadId.value || undefined;
  return threadId ? `${targetBase}/${threadId}` : targetBase;
};

const syncLayoutRoute = async (targetMode: 'mobile' | 'desktop') => {
  const targetPath = getTargetChatPath(targetMode);
  if (route.path === targetPath) return;

  await router.replace({
    path: targetPath,
    query: route.query,
    hash: route.hash,
  });
};

const handleAuthAction = () => {
  showMobileSidebar.value = false;
  if (isGuestMode.value) {
    router.push('/login');
    return;
  }
  logout();
};

const toggleMobileSidebar = () => {
  showMobileSidebar.value = !showMobileSidebar.value;
};

const createThreadId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `t_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
};

onMounted(async () => {
  loadAgentInfo();
  const pendingThreadId = getPendingInterruptedThreadId();
  if (!route.params.threadId && pendingThreadId) {
    localThreadId.value = pendingThreadId;
    await router.replace({
      path: `${chatBasePath.value}/${pendingThreadId}`,
      query: route.query,
      hash: route.hash,
    });
    return;
  }
  if (route.params.threadId) {
    loadHistory(route.params.threadId as string);
    localThreadId.value = route.params.threadId as string;
  }
  if (mode.value === 'auto') {
    await syncLayoutRoute(effectiveMode.value);
  }
});

watch(() => route.params.threadId, (newId) => {
  const threadId = (newId as string) || '';
  if (threadId) {
    loadHistory(threadId);
    localThreadId.value = threadId;
    showMobileSidebar.value = false;
  } else {
    messages.value = [];
    localThreadId.value = null;
  }
  // 切换会话时清空附件，避免把上一个会话的文件带入新会话
  clearAttachments();
});

watch(effectiveMode, async (nextMode) => {
  if (mode.value !== 'auto') return;
  await syncLayoutRoute(nextMode);
});

const shouldAutoOpenArtifacts = (items: any[]) => {
  return items.some((item) => {
    const type = String(item?.type || '').trim().toLowerCase();
    const hasContent = Boolean(String(item?.content || '').trim());
    const autoOpen = Boolean(item?.metadata?.auto_open);
    const isStreamingArtifact = Boolean(item?.metadata?.is_streaming);
    return autoOpen && (hasContent || isStreamingArtifact || ['markdown', 'html', 'code', 'text'].includes(type));
  });
};

watch(
  () => artifacts.value.map((item) => {
    const artId = item?.id || item?.file_id || '';
    const contentLength = String(item?.content || '').length;
    const streamingFlag = item?.metadata?.is_streaming ? '1' : '0';
    const autoOpenFlag = item?.metadata?.auto_open ? '1' : '0';
    const type = String(item?.type || '');
    return `${artId}:${type}:${contentLength}:${streamingFlag}:${autoOpenFlag}`;
  }).join('|'),
  (nextSnapshot, prevSnapshot) => {
    if (!nextSnapshot || nextSnapshot === prevSnapshot) return;
    if (shouldAutoOpenArtifacts(artifacts.value)) {
      showArtifacts.value = true;
    }
  }
);

const handleFilesSelected = (files: File[]) => {
  addFiles(files);
};

const handleSend = async (text: string) => {
  const routeThreadId = route.params.threadId as string | undefined;
  if (!routeThreadId && !localThreadId.value) {
    localThreadId.value = createThreadId();
  }

  const currentThreadId = routeThreadId || localThreadId.value || undefined;
  const isFirstMessageInNewThread = !routeThreadId && !!localThreadId.value && messages.value.length === 0;

  // 先上传 pending 文件
  if (attachments.value.some((a) => a.status === 'pending') && currentThreadId) {
    await uploadPendingFiles(currentThreadId);
  }

  // 绑定从个人文件夹添加的已有文件到当前会话
  const token = authState.value.token;
  const readyFilesFromLibrary = attachments.value.filter((a) => a.status === 'ready' && a.id.startsWith('uf_'));
  for (const a of readyFilesFromLibrary) {
    try {
      await fetch(`/api/v1/sessions/${currentThreadId}/files/attach`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': token ? `Bearer ${token}` : '',
        },
        body: JSON.stringify({ file_id: a.id }),
      });
    } catch (err) {
      console.error('绑定文件到会话失败:', err);
    }
  }

  const fileIds = getReadyFileIds();

  artifactResetKey.value += 1;

  await sendMessage(text, {
    threadId: currentThreadId,
    researchMode: researchMode.value,
    kbIds: selectedKbs.value,
    useSearch: searchEnabled.value,
    fileIds: fileIds.length > 0 ? fileIds : undefined,
  });

  // 发送完成后只清除 pending/error 状态的附件，保留已就绪文件以支持多轮对话
  attachments.value = attachments.value.filter((a) => a.status === 'ready');
  
  if (isFirstMessageInNewThread && sidebarRef.value) {
    sidebarRef.value.loadConversations();
  }
  if (isFirstMessageInNewThread && mobileSidebarRef.value) {
    mobileSidebarRef.value.loadConversations();
  }
};

const handleClear = () => {
  clearMessages();
  clearAttachments();
  draftQuestion.value = '';
  if (!route.params.threadId) {
    localThreadId.value = null;
  }
};

const handleNewChat = async () => {
  clearMessages();
  clearAttachments();
  draftQuestion.value = '';
  localThreadId.value = null;
  showMobileSidebar.value = false;

  if (route.params.threadId) {
    await router.push(chatBasePath.value);
  }
};

const countUserTurnsBeforeIndex = (messageIndex: number): number => {
  if (messageIndex <= 0) return 0;
  return messages.value
    .slice(0, messageIndex)
    .filter((msg) => msg.role === 'user').length;
};

const getCurrentThreadId = (): string | undefined => {
  const routeThreadId = route.params.threadId as string | undefined;
  return routeThreadId || localThreadId.value || undefined;
};

const handleEditAndResend = async (payload: { index: number; content: string }) => {
  if (isBusy.value || payload.index < 0 || payload.index >= messages.value.length) return;

  const keepUserTurns = countUserTurnsBeforeIndex(payload.index);
  await rollbackConversation(getCurrentThreadId(), keepUserTurns);
  draftQuestion.value = payload.content;
};

const handleResearchDraftSubmit = async (payload: {
  question: string;
  draftId: string;
  confirmationStatus: 'confirmed' | 'revised' | 'cancelled';
  researchPlan: ResearchPlanPayload;
}) => {
  const question = String(payload.question || '').trim();
  if (!question || isBusy.value) return;

  const currentThreadId = getCurrentThreadId();
  await sendMessage(question, {
    threadId: currentThreadId,
    researchMode: 'deep_research',
    kbIds: selectedKbs.value,
    useSearch: searchEnabled.value,
    intentMode: 'manual',
    draftId: payload.draftId,
    confirmationStatus: payload.confirmationStatus,
    researchPlan: payload.researchPlan,
    model: selectedModel.value || undefined,
    appendUserMessage: false,
  });
};

const handleSelectSuggestion = (question: string) => {
  if (!question.trim() || isBusy.value) return;
  void handleSend(question.trim());
};

const handleAbort = () => {
  if (activeBackgroundTask.value) {
    cancelBackgroundTask();
  } else {
    cancelStream();
  }
};
</script>

<template>
  <div class="flex flex-col h-screen bg-white dark:bg-[#0b0f1a] relative overflow-hidden">
    <!-- Premium Backdrop Accent -->
    <div class="absolute inset-0 bg-[radial-gradient(circle_at_50%_-10%,#eff6ff,transparent_50%)] dark:bg-[radial-gradient(circle_at_50%_-10%,#1e1b4b,transparent_60%)] pointer-events-none opacity-30"></div>
    <!-- Header -->
    <header class="h-14 border-b border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-900/85 backdrop-blur-md px-4 md:px-6 flex items-center justify-between sticky top-0 z-50">
      <div class="flex items-center space-x-3 min-w-0">
        <button
          @click="toggleMobileSidebar"
          class="md:hidden inline-flex items-center justify-center w-8 h-8 rounded-lg border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-300"
          title="会话列表"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/>
          </svg>
        </button>

        <div class="hidden sm:flex items-center space-x-1.5">
          <div class="w-2.5 h-2.5 rounded-full bg-emerald-500"></div>
          <h1 class="text-sm font-bold text-slate-800 dark:text-slate-100 tracking-tight">核电子学智能助手</h1>
        </div>
        <div class="h-4 w-px bg-slate-200 dark:bg-slate-800 hidden md:block"></div>
        <div class="text-[11px] font-medium text-slate-400 uppercase tracking-widest hidden md:flex items-center space-x-2">
          <span v-if="agentInfo?.subagent_enabled" class="bg-indigo-50 dark:bg-indigo-900/40 text-indigo-500 px-1.5 py-0.5 rounded text-[9px]">专家模式</span>
          <span v-if="isGuestMode" class="bg-amber-50 dark:bg-amber-900/30 text-amber-600 dark:text-amber-300 px-1.5 py-0.5 rounded text-[9px]">访客模式</span>
        </div>
      </div>
      
      <div class="flex items-center space-x-2 md:space-x-3">
        <div class="h-4 w-px bg-slate-200 dark:bg-slate-800 hidden sm:block"></div>

        <button 
          @click="showArtifacts = !showArtifacts"
          class="p-2 transition-all duration-300 hover:scale-105 active:scale-95 relative"
          :class="[showArtifacts ? 'text-indigo-600 dark:text-indigo-400' : 'text-slate-400 hover:text-slate-600 dark:hover:text-slate-200']"
          title="Toggle Artifacts"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/>
          </svg>
          <span v-if="artifacts.length > 0 && !showArtifacts" class="absolute top-1 right-1 w-2 h-2 bg-indigo-500 rounded-full border-2 border-white dark:border-slate-900"></span>
        </button>

      </div>
    </header>

    <Transition name="mobile-drawer">
      <div
        v-if="showMobileSidebar"
        class="fixed inset-0 z-[55] bg-black/35 md:hidden"
        @click.self="showMobileSidebar = false"
      >
        <div class="absolute left-0 top-0 h-full w-[84%] max-w-[320px] bg-white dark:bg-[#0f172a] shadow-2xl">
          <HistorySidebar
            ref="mobileSidebarRef"
            @new-chat="handleNewChat"
            @auth-action="handleAuthAction"
          />
        </div>
      </div>
    </Transition>

    <!-- Main Content Layout -->
    <div class="flex-1 min-h-0 flex overflow-hidden relative z-10">
      <NavRail />

      <HistorySidebar
        v-if="showHistorySidebar"
        class="hidden md:flex"
        ref="sidebarRef"
        @new-chat="handleNewChat"
        @auth-action="handleAuthAction"
      />

      <!-- Chat Area -->
      <main class="flex-1 min-h-0 overflow-hidden flex flex-col relative w-full items-center">
        <MessageList
          :messages="messages"
          :is-busy="isBusy || isUploading"
          @edit-user-message="handleEditAndResend"
          @research-draft-submit="handleResearchDraftSubmit"
          @select-suggestion="handleSelectSuggestion"
        />
        
        <div class="w-full bg-transparent pointer-events-none sticky bottom-0 flex justify-center pb-1 md:pb-8">
          <div class="w-full max-w-4xl relative pointer-events-auto">
            <ChatBox
              :is-busy="isBusy || isUploading"
              :status="isUploading ? '正在上传文件...' : status"
              :search-enabled="searchEnabled"
              :research-mode="researchMode"
              :selected-skills="selectedSkills"
              :selected-kbs="selectedKbs"
              :guest-mode="isGuestMode"
              :model-value="draftQuestion"
              :agent-info="agentInfo"
              :selected-model="selectedModel"
              :attachments="attachments"
              :upload-parse-mode="uploadParseMode"
              @update:search-enabled="searchEnabled = $event"
              @update:research-mode="researchMode = $event"
              @update:selected-skills="selectedSkills = $event"
              @update:selected-kbs="selectedKbs = $event"
              @update:model-value="draftQuestion = $event"
              @update:selected-model="selectedModel = $event"
              @update:upload-parse-mode="setUploadParseMode"
              @send="handleSend"
              @abort="handleAbort"
              @files-selected="handleFilesSelected"
              @remove-attachment="removeAttachment"
              @open-library-picker="showMyFilesPicker = true"
            />
          </div>
        </div>
      </main>

      <!-- ArtifactSidebar -->
      <ArtifactSidebar 
        :artifacts="artifacts" 
        :reset-key="artifactResetKey"
        :show="showArtifacts" 
        @close="showArtifacts = false" 
      />
    </div>
  </div>

  <!-- 个人文件库选择弹窗 -->
  <MyFilesPickerDialog
    :show="showMyFilesPicker"
    @close="showMyFilesPicker = false"
    @confirm="handleConfirmMyFiles"
  />
</template>

<style scoped>
/* Scrolled content fade out top/bottom if needed */
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
