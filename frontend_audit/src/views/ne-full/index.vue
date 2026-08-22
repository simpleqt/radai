<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import ChatBox from './components/ChatBox.vue';
import MessageList from './components/MessageList.vue';
import ArtifactSidebar from './components/ArtifactSidebar.vue';
import ConversationExportMenu from './components/ConversationExportMenu.vue';
import MyFilesPickerDialog from './components/MyFilesPickerDialog.vue';
import AppShell from '@/components/layout/AppShell.vue';
import { useAgentChat } from './composables/useAgentChat';
import { useFileUpload } from './composables/useFileUpload';
import { authState, logout } from '@/utils/request';
import type { ResearchPlanPayload } from '@/api/agent-v2';
import { PanelRight } from 'lucide-vue-next';

const props = withDefaults(defineProps<{
  agentCode?: string;
  basePath?: string;
  brandName?: string;
  brandIcon?: string;
  qaOnly?: boolean;
  emptyLogoSrc?: string;
  emptyLogoAlt?: string;
  emptyTitle?: string;
  emptyDescription?: string;
  emptySuggestions?: string[];
  emptySuggestionStyle?: 'technical' | 'audit';
  emptyLogoCompact?: boolean;
  emptyLogoDarkInvert?: boolean;
}>(), {
  agentCode: 'audit',
  basePath: '/audit',
  brandName: '高校审计智能助手',
  brandIcon: 'BadgeCheck',
  qaOnly: false,
  emptyLogoSrc: '/university-audit-logo.png',
  emptyLogoAlt: '高校审计智能体',
  emptyTitle: '你好，我是高校审计智能助手',
  emptyDescription: '我可以帮你查询审计制度、分析风险线索，辅助审计方案与报告编制',
  emptySuggestions: () => [],
  emptySuggestionStyle: 'technical',
  emptyLogoCompact: false,
  emptyLogoDarkInvert: true,
});

const route = useRoute();
const router = useRouter();
const { messages, isBusy, status, agentInfo, selectedModel, selectedKbs, searchEnabled, deepRetrievalEnabled, selectedSkills, researchMode, artifacts, isGuestMode, activeBackgroundTask, backgroundTaskStreaming, loadAgentInfo, loadHistory, sendMessage, clearMessages, rollbackConversation, cancelStream, cancelBackgroundTask, checkAndResumeBackgroundTask, getPendingInterruptedThreadId } = useAgentChat({ agentCode: props.agentCode });

const { attachments, isUploading, uploadParseMode, addFiles, uploadPendingFiles, removeAttachment, clearAttachments, getReadyFileIds, addReadyAttachment, setUploadParseMode } = useFileUpload();
const showArtifacts = ref(false);
const artifactResetKey = ref(0);
const shellRef = ref<InstanceType<typeof AppShell> | null>(null);
const localThreadId = ref<string | null>(null);
const draftQuestion = ref('');
const showMyFilesPicker = ref(false);

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

const handleAuthAction = () => {
  if (isGuestMode.value) {
    router.push({ path: '/login', query: { redirect: route.fullPath } });
    return;
  }
  logout();
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
      path: `${props.basePath}/${pendingThreadId}`,
      query: route.query,
      hash: route.hash,
    });
    return;
  }
  if (route.params.threadId) {
    loadHistory(route.params.threadId as string);
    localThreadId.value = route.params.threadId as string;
  }
});

watch(() => route.params.threadId, (newId) => {
  const threadId = (newId as string) || '';
  if (threadId) {
    if (threadId !== localThreadId.value || messages.value.length === 0) {
      loadHistory(threadId);
    }
    localThreadId.value = threadId;
  } else {
    messages.value = [];
    localThreadId.value = null;
  }
  // 切换会话时清空附件，避免把上一个会话的文件带入新会话
  clearAttachments();
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

  // 先上传 pending 文件；有失败/解析错误就中止发送——问题不带 file_ids
  // 发出去会让模型"失明"作答，错误附件保留在输入区展示原因。
  if (attachments.value.some((a) => a.status === 'pending') && currentThreadId) {
    await uploadPendingFiles(currentThreadId);
    if (attachments.value.some((a) => a.status === 'error')) {
      return;
    }
  }

  // 绑定从个人文件夹添加的已有文件到当前会话
  const token = authState.value.token;
  const readyFilesFromLibrary = attachments.value.filter((a) => a.status === 'ready' && a.id.startsWith('uf_'));
  for (const a of readyFilesFromLibrary) {
    try {
      const res = await fetch(`/api/v1/sessions/${currentThreadId}/files/attach`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': token ? `Bearer ${token}` : '',
        },
        body: JSON.stringify({ file_id: a.id }),
      });
      if (!res.ok) {
        // 绑定失败不能静默当作已绑定：从本次发送中剔除该文件。
        console.error('绑定文件到会话失败:', res.status);
        attachments.value = attachments.value.filter((x) => x.id !== a.id);
      }
    } catch (err) {
      console.error('绑定文件到会话失败:', err);
      attachments.value = attachments.value.filter((x) => x.id !== a.id);
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

  // 发送完成后只清除 pending 状态的附件，保留已就绪文件以支持多轮对话；
  // error 附件保留到用户手动移除——静默丢弃会掩盖"模型其实没收到文件"。
  attachments.value = attachments.value.filter((a) => a.status !== 'pending');

  if (isFirstMessageInNewThread) {
    shellRef.value?.reloadConversations();
  }
};

const handleNewChat = async () => {
  clearMessages();
  clearAttachments();
  draftQuestion.value = '';
  localThreadId.value = null;

  if (route.params.threadId) {
    await router.push(props.basePath);
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

// 回滚要先 await 网络请求，这段窗口里 isBusy 仍是 false、按钮仍可点；用一个
// 飞行中标记挡住重复触发，否则连点会回滚两次并发出两次请求。
let rollbackInFlight = false;

const handleEditAndResend = async (payload: { index: number; content: string }) => {
  if (rollbackInFlight || isBusy.value || payload.index < 0 || payload.index >= messages.value.length) return;

  rollbackInFlight = true;
  try {
    const keepUserTurns = countUserTurnsBeforeIndex(payload.index);
    await rollbackConversation(getCurrentThreadId(), keepUserTurns);
    draftQuestion.value = payload.content;
  } finally {
    rollbackInFlight = false;
  }
};

/** 一键重新生成：回滚到该回答对应的问题，并按当前 composer 设置自动重发。 */
const handleRegenerate = async (payload: { index: number }) => {
  if (rollbackInFlight || isBusy.value || payload.index <= 0 || payload.index >= messages.value.length) return;

  let userIndex = -1;
  for (let i = payload.index - 1; i >= 0; i -= 1) {
    if (messages.value[i]?.role === 'user') {
      userIndex = i;
      break;
    }
  }
  if (userIndex < 0) return;

  const question = String(messages.value[userIndex].content || '').trim();
  if (!question) return;

  const keepUserTurns = countUserTurnsBeforeIndex(userIndex);
  rollbackInFlight = true;
  try {
    await rollbackConversation(getCurrentThreadId(), keepUserTurns);
  } finally {
    rollbackInFlight = false;
  }
  await handleSend(question);
};

const handleResearchDraftSubmit = async (payload: {
  question: string;
  draftId: string;
  confirmationStatus: 'confirmed' | 'revised' | 'cancelled';
  researchPlan: ResearchPlanPayload;
}) => {
  const question = String(payload.question || '').trim();
  if (!question || isBusy.value) return;

  await sendMessage(question, {
    threadId: getCurrentThreadId(),
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
  <AppShell
    ref="shellRef"
    :brand-name="props.brandName"
    :brand-icon="props.brandIcon"
    :base-path="props.basePath"
    :qa-only="props.qaOnly"
    :conversation-agent-code="props.agentCode"
    :panel-open="showArtifacts"
    @new-chat="handleNewChat"
    @auth-action="handleAuthAction"
  >
    <template #actions>
      <span
        v-if="agentInfo?.subagent_enabled"
        class="hidden rounded bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium text-accent @cozy/main:inline"
      >专家模式</span>
      <span
        v-if="isGuestMode"
        class="hidden rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-600 @cozy/main:inline dark:bg-amber-900/30 dark:text-amber-300"
      >访客模式</span>

      <ConversationExportMenu
        :messages="messages"
        :agent-name="props.brandName"
        :disabled="isBusy"
      />

      <button
        type="button"
        class="relative inline-flex size-8 items-center justify-center rounded-lg transition-colors hover:bg-surface-hover"
        :class="showArtifacts ? 'text-accent' : 'text-text-tertiary hover:text-text-primary'"
        title="工件预览"
        @click="showArtifacts = !showArtifacts"
      >
        <PanelRight class="size-4.5" />
        <span
          v-if="artifacts.length > 0 && !showArtifacts"
          class="absolute right-1 top-1 size-1.5 rounded-full bg-accent"
        />
      </button>
    </template>

    <MessageList
      class="min-h-0 w-full flex-1"
      :messages="messages"
      :is-busy="isBusy || isUploading"
      :thread-id="String(route.params.threadId || localThreadId || '')"
      :agent-code="props.agentCode"
      :model-name="selectedModel"
      :empty-logo-src="props.emptyLogoSrc"
      :empty-logo-alt="props.emptyLogoAlt"
      :empty-title="props.emptyTitle"
      :empty-description="props.emptyDescription"
      :empty-suggestions="props.emptySuggestions.length ? props.emptySuggestions : undefined"
      :empty-suggestion-style="props.emptySuggestionStyle"
      :empty-logo-compact="props.emptyLogoCompact"
      :empty-logo-dark-invert="props.emptyLogoDarkInvert"
      @edit-user-message="handleEditAndResend"
      @regenerate-answer="handleRegenerate"
      @research-draft-submit="handleResearchDraftSubmit"
      @select-suggestion="handleSelectSuggestion"
      @process-action-send="handleSelectSuggestion"
      @process-action-fill="(value: string) => { draftQuestion = value; }"
    />

    <div class="shrink-0 bg-surface px-3 pb-3 pt-1 @cozy/main:px-6 @cozy/main:pb-4">
      <div class="mx-auto w-full min-w-0 max-w-(--content-max)">
        <ChatBox
          :is-busy="isBusy || isUploading"
          :status="isUploading ? '正在上传文件...' : status"
          :search-enabled="searchEnabled"
          :deep-retrieval="deepRetrievalEnabled"
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
          @update:deep-retrieval="deepRetrievalEnabled = $event"
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

    <template #panel>
      <ArtifactSidebar
        :artifacts="artifacts"
        :reset-key="artifactResetKey"
        :show="showArtifacts"
        @close="showArtifacts = false"
      />
    </template>

    <!-- fixed 定位弹窗，放在 AppShell 内以保持路由组件单根（路由过渡要求） -->
    <MyFilesPickerDialog
      :show="showMyFilesPicker"
      @close="showMyFilesPicker = false"
      @confirm="handleConfirmMyFiles"
    />
  </AppShell>
</template>
