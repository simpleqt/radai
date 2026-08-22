<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
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
import { GroundwaterAPI, type GroundwaterProject } from '@/api/groundwater';
import { Database, Loader2, PanelRight, RefreshCw } from 'lucide-vue-next';

const props = withDefaults(defineProps<{
  agentCode?: string;
  projectMode?: boolean;
}>(), {
  agentCode: 'csh',
  projectMode: false,
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
const groundwaterProjects = ref<GroundwaterProject[]>([]);
const selectedProjectId = ref('');
const projectsLoading = ref(false);
const projectError = ref('');

const chatBasePath = computed(() => (props.projectMode ? '/groundwater' : '/app'));

const loadProjects = async () => {
  if (!props.projectMode || isGuestMode.value) return;
  projectsLoading.value = true;
  projectError.value = '';
  try {
    groundwaterProjects.value = await GroundwaterAPI.listProjects();
    selectedProjectId.value = '';
  } catch (error: any) {
    projectError.value = error?.message || '项目列表加载失败';
  } finally {
    projectsLoading.value = false;
  }
};

watch(selectedProjectId, (projectId) => {
  if (!props.projectMode || !projectId) return;
  status.value = '';
});

const handleConfirmMyFiles = (selectedFiles: any[]) => {
  for (const f of selectedFiles) {
    addReadyAttachment({
      file_id: f.file_id,
      display_name: f.display_name,
      size_bytes: f.size_bytes,
      mime_type: f.mime_type,
      file_category: f.file_category,
      strategy: f.strategy,
      table_analysis_enabled: f.table_analysis_enabled,
    });
  }
};

const handleAuthAction = () => {
  if (isGuestMode.value) {
    router.push('/login');
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
  await loadProjects();
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
  if (props.projectMode && !selectedProjectId.value) {
    status.value = projectError.value || '当前账号尚未获得地下水项目权限';
    return;
  }
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
  const effectiveKbIds = props.projectMode
    ? [...(agentInfo.value?.kb_ids || [])]
    : selectedKbs.value;

  artifactResetKey.value += 1;

  await sendMessage(text, {
    threadId: currentThreadId,
    researchMode: researchMode.value,
    kbIds: effectiveKbIds,
    useSearch: searchEnabled.value,
    fileIds: fileIds.length > 0 ? fileIds : undefined,
    metadata: props.projectMode && selectedProjectId.value
      ? { groundwater_project_id: selectedProjectId.value }
      : undefined,
  });

  // 首轮发送期间线程 ID 仅存在于组件内存。完成后把它写入路由，
  // 使浏览器刷新能够通过会话 API 恢复项目问答记录。
  if (isFirstMessageInNewThread && currentThreadId && !route.params.threadId) {
    await router.replace({
      path: `${chatBasePath.value}/${currentThreadId}`,
      query: route.query,
      hash: route.hash,
    });
  }

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

  const currentThreadId = getCurrentThreadId();
  const effectiveKbIds = props.projectMode
    ? [...(agentInfo.value?.kb_ids || [])]
    : selectedKbs.value;
  await sendMessage(question, {
    threadId: currentThreadId,
    researchMode: 'deep_research',
    kbIds: effectiveKbIds,
    useSearch: searchEnabled.value,
    intentMode: 'manual',
    draftId: payload.draftId,
    confirmationStatus: payload.confirmationStatus,
    researchPlan: payload.researchPlan,
    model: selectedModel.value || undefined,
    appendUserMessage: false,
    metadata: props.projectMode && selectedProjectId.value
      ? { groundwater_project_id: selectedProjectId.value }
      : undefined,
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
    brand-name="成理智学助手"
    brand-icon="Waves"
    :panel-open="showArtifacts"
    @new-chat="handleNewChat"
    @auth-action="handleAuthAction"
  >
    <template #actions>
      <ConversationExportMenu
        :messages="messages"
        :agent-name="'成理智学助手'"
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

    <template v-if="props.projectMode" #subbar>
      <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-border-subtle bg-surface px-3 py-2">
        <Database class="size-4 shrink-0 text-accent" />
        <label for="groundwater-project" class="text-xs font-medium text-text-secondary">当前项目</label>
        <select
          id="groundwater-project"
          v-model="selectedProjectId"
          :disabled="projectsLoading || groundwaterProjects.length === 0"
          class="h-8 min-w-0 max-w-full flex-1 rounded-(--radius-control) border border-border-subtle bg-surface px-2.5 text-sm text-text-primary outline-none focus:border-accent @cozy/main:max-w-96 @cozy/main:flex-none"
        >
          <option value="" disabled>{{ projectsLoading ? '正在加载项目...' : '请选择已授权项目' }}</option>
          <option v-for="project in groundwaterProjects" :key="project.project_id" :value="project.project_id">
            {{ project.name }}（v{{ project.data_version }}）
          </option>
        </select>
        <button
          type="button"
          class="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-primary disabled:opacity-50"
          :disabled="projectsLoading"
          title="刷新项目列表"
          @click="loadProjects"
        >
          <Loader2 v-if="projectsLoading" class="size-4 animate-spin" />
          <RefreshCw v-else class="size-4" />
        </button>
        <span v-if="projectError" class="text-xs text-rose-600">{{ projectError }}</span>
        <span v-else-if="!isGuestMode && !projectsLoading && groundwaterProjects.length === 0" class="text-xs text-amber-600">
          当前账号尚未被授权项目，请联系管理员
        </span>
      </div>
    </template>

    <MessageList
      class="min-h-0 w-full flex-1"
      :messages="messages"
      :is-busy="isBusy || isUploading"
      :thread-id="String(route.params.threadId || localThreadId || '')"
      :agent-code="props.agentCode"
      :model-name="selectedModel"
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
          :show-kb-selector="!props.projectMode"
          :show-skill-selector="!props.projectMode"
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
