import { computed, reactive, ref } from 'vue';
import { AgentAPI, ChatMessage, AgentInfo, type AgentProcessEvent, type ResearchPlanPayload, type BackgroundTaskInfo, type FollowUpSuggestion } from '@/api/agent-v2';
import { ConversationAPI } from '@/api/conversations';
import { authState } from '@/utils/request';
import {
  getOrCreateGuestDeviceId,
  loadGuestMessages,
  saveGuestConversation,
} from '@/services/storage/guestIndexedDb';

const PENDING_STREAM_STORAGE_KEY = 'agent_v2_pending_stream';
const PENDING_STREAM_TTL_MS = 10 * 60 * 1000;
const MAX_PENDING_RESUME_ATTEMPTS = 2;
const FOLLOW_UP_SUGGESTIONS_ENABLED = import.meta.env.VITE_FOLLOW_UP_SUGGESTIONS !== 'false';
const FOLLOW_UP_SUGGESTIONS_TIMEOUT_MS = 12000;

const hashSuggestionId = (text: string) => {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) - hash) + text.charCodeAt(i);
    hash |= 0;
  }
  return `sug_${Math.abs(hash).toString(36)}`;
};

const mapFollowUpSuggestions = (items: string[] | FollowUpSuggestion[]): FollowUpSuggestion[] => {
  return (items || [])
    .map((item) => {
      if (typeof item === 'string') {
        const text = item.trim();
        if (!text) return null;
        return { id: hashSuggestionId(text), text };
      }
      const text = String(item?.text || '').trim();
      if (!text) return null;
      return {
        id: String(item.id || hashSuggestionId(text)),
        text,
        category: item.category,
      };
    })
    .filter(Boolean) as FollowUpSuggestion[];
};

const hidePreviousAssistantSuggestions = (source: ChatMessage[]) => {
  for (let i = source.length - 1; i >= 0; i -= 1) {
    const msg = source[i];
    if (msg.role !== 'assistant') continue;
    if (msg.followUpSuggestions?.length || msg.suggestionsLoading) {
      msg.suggestionsHidden = true;
      msg.suggestionsLoading = false;
    }
    break;
  }
};

type SendMessageOptions = {
  threadId?: string;
  researchMode?: 'normal' | 'deep_research';
  selectedSkills?: string[];
  thinking?: boolean;
  kbIds?: string[];
  useSearch?: boolean;
  intentMode?: 'auto' | 'manual';
  draftId?: string;
  confirmationStatus?: 'pending' | 'confirmed' | 'revised' | 'cancelled';
  researchPlan?: ResearchPlanPayload;
  model?: string;
  appendUserMessage?: boolean;
  fileIds?: string[];
  metadata?: Record<string, unknown>;
};

type PendingStreamState = {
  threadId: string;
  question: string;
  researchMode: 'normal' | 'deep_research';
  selectedSkills: string[];
  kbIds: string[];
  useSearch: boolean;
  intentMode?: 'auto' | 'manual';
  draftId?: string;
  confirmationStatus?: 'pending' | 'confirmed' | 'revised' | 'cancelled';
  researchPlan?: ResearchPlanPayload;
  model?: string;
  metadata?: Record<string, unknown>;
  savedAt: number;
  resumeAttempts: number;
};

const isUserRole = (role: string) => role === 'human' || role === 'user';
const isAssistantRole = (role: string) => role === 'ai' || role === 'assistant';

const looksLikeIntermediateContext = (content: string) => {
  const text = (content || '').trim();
  if (!text) return true;

  return (
    text.startsWith('# 检索到的上下文') ||
    text.includes('Image Content Analysis') ||
    text.includes('Visual Analysis:') ||
    text.includes('Footnotes: None')
  );
};

const extractWorkflowStatusFromIntermediate = (content: string) => {
  const text = (content || '').trim();
  if (!text) return null;

  if (text.startsWith('# 检索到的上下文')) {
    return '在知识库中搜索';
  }
  if (text.includes('Image Content Analysis') || text.includes('Visual Analysis:')) {
    return '正在分析检索结果';
  }
  if (text.includes('/mnt/skills/')) {
    return '加载深度研究技能';
  }
  if (text.includes('save_deep_research_report') || text.includes('研究报告')) {
    return '创建深度研究报告';
  }

  return '工具调用完成';
};

const normalizeDisplayedUserQuestion = (content: string): string => {
  const text = String(content || '');
  if (!text.trim()) return '';

  let cleaned = text;
  cleaned = cleaned.replace(/<runtime_current_time>[\s\S]*?<\/runtime_current_time>\s*/gi, '');
  cleaned = cleaned.replace(/\[ConfirmedResearchPlan\][\s\S]*?\[\/ConfirmedResearchPlan\]\s*/gi, '');
  cleaned = cleaned.replace(/\n{3,}/g, '\n\n').trim();

  return cleaned || text.trim();
};

const normalizeArtifactDownloadUrl = (url: string) => {
  const value = String(url || '').trim();
  if (!value) return '';
  if (value.startsWith('/')) return value;

  try {
    const parsed = new URL(value);
    if (parsed.pathname.includes('/files/')) {
      return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
  } catch {
    return value;
  }

  return value;
};

const mapThreadArtifactToViewModel = (item: any) => ({
  id: item.file_id,
  file_id: item.file_id,
  type: 'file',
  title: item.display_name,
  url: normalizeArtifactDownloadUrl(item.download_url),
  mime_type: item.mime_type,
  metadata: {
    url: normalizeArtifactDownloadUrl(item.download_url),
    mime_type: item.mime_type,
    size_bytes: item.size_bytes,
    source_type: item.source_type,
    thread_id: item.thread_id,
    filename: item.display_name,
  },
});

const upsertArtifact = (artifactList: any[], nextArtifact: any) => {
  const artId = nextArtifact?.id || nextArtifact?.file_id;
  if (!artId) return artifactList;

  const existingIdx = artifactList.findIndex((item) => (item.id || item.file_id) === artId);
  if (existingIdx >= 0) {
    artifactList[existingIdx] = {
      ...artifactList[existingIdx],
      ...nextArtifact,
      metadata: {
        ...(artifactList[existingIdx]?.metadata || {}),
        ...(nextArtifact?.metadata || {}),
      },
    };
  } else {
    artifactList.push(nextArtifact);
  }
  return artifactList;
};

const normalizeHistoryMessages = (history: any[]): ChatMessage[] => {
  const normalized: ChatMessage[] = [];
  let pendingAssistant: ChatMessage | null = null;
  let pendingStatuses: string[] = [];

  const appendStatus = (status: string) => {
    const prev = pendingStatuses[pendingStatuses.length - 1];
    if (status && status !== prev) {
      pendingStatuses.push(status);
    }
  };

  const flushPendingAssistant = () => {
    if (!pendingAssistant) return;
    pendingAssistant.statusHistory = [...pendingStatuses];
    normalized.push(pendingAssistant);
    pendingAssistant = null;
    pendingStatuses = [];
  };

  for (const msg of history || []) {
    const role = String(msg?.role || '').toLowerCase();
    const content = String(msg?.content || '');
    const id = msg?.id || Math.random().toString();

    if (isUserRole(role)) {
      flushPendingAssistant();

      normalized.push({
        id,
        role: 'user',
        content: normalizeDisplayedUserQuestion(content),
      });
      continue;
    }

    if (isAssistantRole(role)) {
      if (looksLikeIntermediateContext(content)) {
        const status = extractWorkflowStatusFromIntermediate(content);
        if (status) appendStatus(status);
        continue;
      }

      pendingAssistant = {
        id,
        role: 'assistant',
        content,
        reasoning: msg?.metadata?.reasoning || '',
        statusHistory: [...pendingStatuses],
        processEvents: [],
        followUpSuggestions: mapFollowUpSuggestions(
          msg?.metadata?.follow_up_suggestions || msg?.metadata?.followUpSuggestions || [],
        ),
      };

      appendStatus('回答生成完成');
    }
  }

  flushPendingAssistant();

  return normalized;
};

const trimMessagesByUserTurns = (source: ChatMessage[], keepUserTurns: number): ChatMessage[] => {
  if (keepUserTurns <= 0) return [];

  const trimmed: ChatMessage[] = [];
  let seenUserTurns = 0;

  for (const msg of source) {
    if (msg.role === 'user') {
      if (seenUserTurns >= keepUserTurns) break;
      seenUserTurns += 1;
      trimmed.push(msg);
      continue;
    }

    if (seenUserTurns === 0) continue;
    trimmed.push(msg);
  }

  return trimmed;
};

const readPendingStreamState = (): PendingStreamState | null => {
  if (typeof window === 'undefined') return null;
  try {
    const raw = sessionStorage.getItem(PENDING_STREAM_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PendingStreamState;
    if (!parsed?.threadId || !parsed?.question || !parsed?.savedAt) {
      sessionStorage.removeItem(PENDING_STREAM_STORAGE_KEY);
      return null;
    }
    if (Date.now() - parsed.savedAt > PENDING_STREAM_TTL_MS) {
      sessionStorage.removeItem(PENDING_STREAM_STORAGE_KEY);
      return null;
    }
    return parsed;
  } catch {
    sessionStorage.removeItem(PENDING_STREAM_STORAGE_KEY);
    return null;
  }
};

const writePendingStreamState = (state: PendingStreamState) => {
  if (typeof window === 'undefined') return;
  sessionStorage.setItem(PENDING_STREAM_STORAGE_KEY, JSON.stringify(state));
};

const clearPendingStreamState = (threadId?: string) => {
  if (typeof window === 'undefined') return;
  const current = readPendingStreamState();
  if (!current) return;
  if (threadId && current.threadId !== threadId) return;
  sessionStorage.removeItem(PENDING_STREAM_STORAGE_KEY);
};

const normalizeSelectedSkills = (skills?: string[]): string[] => {
  if (!Array.isArray(skills)) return [];
  return skills.filter((skill) => skill && skill !== 'nuclear-electronics' && skill !== 'csh-ai');
};

export function useAgentChat(config: { agentCode?: string } = {}) {
  const agentCode = config.agentCode || 'csh';
  const messages = ref<ChatMessage[]>([]);
  const isBusy = ref(false);
  const status = ref('');
  const agentInfo = ref<AgentInfo | null>(null);
  const selectedModel = ref<string>('');
  const selectedKbs = ref<string[]>([]);
  const searchEnabled = ref(false);
  const selectedSkills = ref<string[]>([]);
  const researchMode = computed({
    get: () => selectedSkills.value.includes('deep-research') ? 'deep_research' : 'normal',
    set: (mode) => {
      const idx = selectedSkills.value.indexOf('deep-research');
      if (mode === 'deep_research') {
        if (idx === -1) selectedSkills.value.push('deep-research');
      } else {
        if (idx !== -1) selectedSkills.value.splice(idx, 1);
      }
    }
  });
  const artifacts = ref<any[]>([]);
  const isGuestMode = computed(() => !authState.value.token);
  const activeThreadId = ref('');
  let historyLoadSeq = 0;

  // ── 后台任务状态 ──────────────────────────────────────
  const activeBackgroundTask = ref<BackgroundTaskInfo | null>(null);
  const backgroundTaskStreaming = ref(false);
  let taskUnsubscribe: (() => void) | null = null;

  // ── 普通流中断控制 ──────────────────────────────────────
  let streamAbortController: AbortController | null = null;

  const extractAssistantContent = (rawContent: unknown): string => {
    if (typeof rawContent === 'string') return rawContent;
    if (Array.isArray(rawContent)) {
      return rawContent
        .map((block: any) => typeof block === 'string' ? block : block?.text || '')
        .filter(Boolean)
        .join('');
    }
    return rawContent == null ? '' : String(rawContent);
  };

  const refreshThreadArtifacts = async (threadId: string) => {
    if (!threadId || isGuestMode.value) return;
    const threadArtifacts = await ConversationAPI.getThreadArtifacts(threadId);
    artifacts.value = threadArtifacts.map((item: any) => mapThreadArtifactToViewModel(item));
  };

  const recoverAssistantContentFromHistory = async (
    threadId: string,
    assistantMsg: ChatMessage,
    options?: {
      replaceContent?: boolean;
      refreshArtifacts?: boolean;
    },
  ): Promise<boolean> => {
    const normalizedThreadId = String(threadId || '').trim();
    if (!normalizedThreadId || isGuestMode.value) return false;
    if (activeThreadId.value && activeThreadId.value !== normalizedThreadId) return false;

    const historyMessages = await ConversationAPI.getMessages(normalizedThreadId);
    if (!historyMessages || historyMessages.length === 0) return false;

    const lastAssistant = historyMessages
      .filter((m: any) => m.role === 'assistant')
      .pop();
    const recoveredContent = extractAssistantContent(lastAssistant?.content);
    if (!recoveredContent) return false;

    if (options?.replaceContent || recoveredContent.length > (assistantMsg.content || '').length) {
      assistantMsg.content = recoveredContent;
    }

    if (options?.refreshArtifacts) {
      await refreshThreadArtifacts(normalizedThreadId);
    }

    return true;
  };

  const persistPendingStreamState = (question: string, options?: SendMessageOptions) => {
    const threadId = String(options?.threadId || '').trim();
    if (!threadId || isGuestMode.value) return;

    writePendingStreamState({
      threadId,
      question,
      researchMode: options?.researchMode ?? researchMode.value,
      selectedSkills: normalizeSelectedSkills(options?.selectedSkills ?? selectedSkills.value),
      kbIds: [...(options?.kbIds ?? selectedKbs.value)],
      useSearch: options?.useSearch ?? searchEnabled.value,
      intentMode: options?.intentMode,
      draftId: options?.draftId,
      confirmationStatus: options?.confirmationStatus,
      researchPlan: options?.researchPlan,
      model: options?.model || selectedModel.value || undefined,
      metadata: options?.metadata,
      savedAt: Date.now(),
      resumeAttempts: 0,
    });
  };

  const getPendingInterruptedThreadId = (): string | null => {
    if (isGuestMode.value) return null;
    return readPendingStreamState()?.threadId || null;
  };

  /** 订阅后台任务事件流 */
  const subscribeToBackgroundTask = (taskId: string, threadId: string, assistantMsg: ChatMessage) => {
    // 先取消之前的订阅
    if (taskUnsubscribe) {
      taskUnsubscribe();
      taskUnsubscribe = null;
    }

    backgroundTaskStreaming.value = true;

    taskUnsubscribe = AgentAPI.subscribeTaskStream(
      taskId,
      threadId,
      (textChunk) => {
        assistantMsg.content = (assistantMsg.content || '') + textChunk;
      },
      (statusMsg) => {
        status.value = statusMsg;
        if (statusMsg) {
          const history = assistantMsg.statusHistory || [];
          if (statusMsg !== history[history.length - 1]) {
            history.push(statusMsg);
            assistantMsg.statusHistory = history;
          }
        }
      },
      (event) => {
        if (event.kind === 'artifact_update' && event.metadata) {
          upsertArtifact(artifacts.value, event.metadata as any);
          return;
        }

        const events = assistantMsg.processEvents || [];
        if (event.id && events.some(e => e.id === event.id)) return;
        events.push(event);
        assistantMsg.processEvents = events;
        
        if (event.kind === 'artifact' && event.metadata) {
          const art = event.metadata as any;
          console.log('[useAgentChat] New background task artifact:', art);
          upsertArtifact(artifacts.value, art);
        }
      },
      async () => {
        // 任务完成
        backgroundTaskStreaming.value = false;
        isBusy.value = false;
        status.value = '';
        assistantMsg.isStreaming = false;
        activeBackgroundTask.value = null;
        taskUnsubscribe = null;
        // 静默补齐最终内容与产物，避免用整段历史重载造成界面闪动。
        if (threadId) {
          try {
            await recoverAssistantContentFromHistory(threadId, assistantMsg, {
              replaceContent: true,
              refreshArtifacts: true,
            });
          } catch (e) {
            console.error('[useAgentChat] onDone recover history error:', e);
          }
        }
      },
      (error) => {
        console.error('[useAgentChat] Background task SSE error:', error);
        // Don't immediately mark as failed — the task may still be running on the server.
        // Show a transient warning and attempt auto-recovery after a short delay.
        const currentTaskId = taskId;
        const currentThreadId = threadId;

        assistantMsg.content += `\n\n⚠️ 连接暂时中断 (${error})，正在尝试自动恢复...`;
        status.value = '正在尝试恢复连接...';

        // Auto-recovery: wait 5s then check if the task is still active on the server
        if (currentTaskId && currentThreadId) {
          setTimeout(async () => {
            try {
              const result = await AgentAPI.getThreadActiveTask(currentThreadId);
              if (result.has_active_task && result.task) {
                // Task is still running — re-subscribe
                console.log('[useAgentChat] Auto-recovery: task still active, re-subscribing');
                activeBackgroundTask.value = result.task;
                assistantMsg.isStreaming = true;
                isBusy.value = true;
                // Remove the warning message since we're recovering
                assistantMsg.content = assistantMsg.content.replace(/\n\n⚠️ 连接暂时中断.*正在尝试自动恢复\.\.\./, '');
                subscribeToBackgroundTask(result.task.task_id, currentThreadId, assistantMsg);
              } else {
                // Task 已结束，静默补齐最终内容，避免触发整页历史重载。
                console.log('[useAgentChat] Auto-recovery: task already finished, recovering latest content');
                backgroundTaskStreaming.value = false;
                isBusy.value = false;
                status.value = '';
                assistantMsg.isStreaming = false;
                activeBackgroundTask.value = null;
                taskUnsubscribe = null;
                // Replace warning with recovery message
                assistantMsg.content = assistantMsg.content.replace(/\n\n⚠️ 连接暂时中断.*正在尝试自动恢复\.\.\./, '\n\n✅ 连接已恢复，任务已完成');
                if (currentThreadId) {
                  await recoverAssistantContentFromHistory(currentThreadId, assistantMsg, {
                    replaceContent: true,
                    refreshArtifacts: true,
                  });
                }
              }
            } catch (recoveryError) {
              console.error('[useAgentChat] Auto-recovery failed:', recoveryError);
              backgroundTaskStreaming.value = false;
              isBusy.value = false;
              status.value = '';
              assistantMsg.isStreaming = false;
              // Replace transient warning with final error
              assistantMsg.content = assistantMsg.content.replace(/\n\n⚠️ 连接暂时中断.*正在尝试自动恢复\.\.\./, `\n\n[后台任务错误: ${error}，自动恢复失败。请稍后重试或重新进入当前会话查看结果。]`);
              activeBackgroundTask.value = null;
              taskUnsubscribe = null;
            }
          }, 5000);
        } else {
          // No task info available for recovery
          backgroundTaskStreaming.value = false;
          isBusy.value = false;
          status.value = '';
          assistantMsg.isStreaming = false;
          assistantMsg.content += `\n\n[后台任务错误: ${error}，请稍后重试或重新进入当前会话查看结果。]`;
          activeBackgroundTask.value = null;
          taskUnsubscribe = null;
        }
      },
      (thinkingText) => {
        assistantMsg.reasoning = (assistantMsg.reasoning || '') + thinkingText;
      },
    );
  };

  /** 中断当前普通流式回答 */
  const cancelStream = () => {
    if (streamAbortController) {
      streamAbortController.abort();
      streamAbortController = null;
    }
  };

  /** 取消后台任务 */
  const cancelBackgroundTask = async () => {
    const task = activeBackgroundTask.value;
    if (!task) return;
    try {
      await AgentAPI.cancelTask(task.task_id, task.thread_id);
      if (taskUnsubscribe) {
        taskUnsubscribe();
        taskUnsubscribe = null;
      }
      backgroundTaskStreaming.value = false;
      isBusy.value = false;
      status.value = '';
      activeBackgroundTask.value = null;
    } catch (e) {
      console.error('[useAgentChat] Failed to cancel task:', e);
    }
  };

  /** 检查并恢复后台任务（刷新后调用） */
  const checkAndResumeBackgroundTask = async (threadId: string): Promise<boolean> => {
    if (isGuestMode.value) return false;
    try {
      const result = await AgentAPI.getThreadActiveTask(threadId);
      if (result.has_active_task && result.task) {
        activeBackgroundTask.value = result.task;
        // 找到最后一条 assistant 消息并订阅
        const lastMsg = messages.value[messages.value.length - 1];
        if (lastMsg && lastMsg.role === 'assistant') {
          lastMsg.isStreaming = true;
          isBusy.value = true;
          subscribeToBackgroundTask(result.task.task_id, threadId, lastMsg);
        }
        return true;
      }
    } catch (e) {
      console.error('[useAgentChat] Failed to check background task:', e);
    }
    return false;
  };

  const checkAndResumeInterruptedStream = async (threadId: string): Promise<boolean> => {
    if (isGuestMode.value) return false;

    const pending = readPendingStreamState();
    if (!pending || pending.threadId !== threadId) return false;

    const lastMsg = messages.value[messages.value.length - 1];
    const normalizedQuestion = normalizeDisplayedUserQuestion(pending.question);

    if (!lastMsg || lastMsg.role !== 'user' || lastMsg.content !== normalizedQuestion) {
      clearPendingStreamState(threadId);
      return false;
    }

    if (pending.resumeAttempts >= MAX_PENDING_RESUME_ATTEMPTS) {
      status.value = '检测到上次回答中断，请手动重新发送问题。';
      return false;
    }

    writePendingStreamState({
      ...pending,
      savedAt: Date.now(),
      resumeAttempts: pending.resumeAttempts + 1,
    });

    status.value = '检测到页面刷新导致回答中断，正在自动恢复...';
    const userTurns = messages.value.filter((msg) => msg.role === 'user').length;
    if (userTurns <= 0) {
      clearPendingStreamState(threadId);
      return false;
    }

    await rollbackConversation(threadId, userTurns - 1);
    await sendMessage(pending.question, {
      threadId,
      researchMode: pending.researchMode,
      selectedSkills: pending.selectedSkills,
      kbIds: pending.kbIds,
      useSearch: pending.useSearch,
      intentMode: pending.intentMode,
      draftId: pending.draftId,
      confirmationStatus: pending.confirmationStatus,
      researchPlan: pending.researchPlan,
      model: pending.model,
      metadata: pending.metadata,
      appendUserMessage: true,
    });
    return true;
  };

  const loadAgentInfo = async () => {
    try {
      agentInfo.value = await AgentAPI.getInfo(agentCode);
      const availableModels = agentInfo.value?.available_models || [];
      const defaultId = (agentInfo.value?.default_model || agentInfo.value?.model || '').trim();
      const ids = new Set(availableModels.map((m) => m.name));
      if (defaultId && ids.has(defaultId)) {
        selectedModel.value = defaultId;
      } else if (agentInfo.value?.model && ids.has(agentInfo.value.model)) {
        selectedModel.value = agentInfo.value.model;
      } else if (availableModels.length > 0) {
        selectedModel.value = availableModels[0].name;
      } else {
        selectedModel.value = '';
      }
      searchEnabled.value = false; // 默认关闭搜索
      researchMode.value = 'normal';
      if (isGuestMode.value) {
        selectedSkills.value = [];
      }
      const allowedKbIds = agentInfo.value?.kb_ids || [];
      if (allowedKbIds.length > 0) {
        // 保留用户已选中的有效 ID；若当前没有选择则默认全选
        const validSelected = selectedKbs.value.filter((id) => allowedKbIds.includes(id));
        selectedKbs.value = validSelected.length > 0 ? validSelected : [...allowedKbIds];
      }
    } catch (e) {
      console.error('Failed to load agent info', e);
    }
  };

  const loadHistory = async (threadId: string) => {
    const normalizedThreadId = String(threadId || '').trim();
    const seq = ++historyLoadSeq;

    // 取消之前的后台任务订阅（避免状态串扰）
    if (taskUnsubscribe) {
      taskUnsubscribe();
      taskUnsubscribe = null;
    }
    backgroundTaskStreaming.value = false;
    activeBackgroundTask.value = null;

    if (!threadId) {
      activeThreadId.value = '';
      messages.value = [];
      artifacts.value = [];
      isBusy.value = false;
      return;
    }

    // Clear stale UI immediately when switching conversations.
    activeThreadId.value = normalizedThreadId;
    messages.value = [];
    artifacts.value = [];
    
    isBusy.value = true;
    status.value = '正在加载历史记录...';
    try {
      if (isGuestMode.value) {
        const guestMessages = await loadGuestMessages(normalizedThreadId);
        if (seq !== historyLoadSeq || activeThreadId.value !== normalizedThreadId) return;
        messages.value = guestMessages;
      } else {
        const history = await ConversationAPI.getMessages(normalizedThreadId);
        if (seq !== historyLoadSeq || activeThreadId.value !== normalizedThreadId) return;
        messages.value = normalizeHistoryMessages(history);

        const threadArtifacts = await ConversationAPI.getThreadArtifacts(normalizedThreadId);
        if (seq !== historyLoadSeq || activeThreadId.value !== normalizedThreadId) return;
        artifacts.value = threadArtifacts.map((item) => mapThreadArtifactToViewModel(item));
      }
    } catch (e) {
      console.error('Failed to load history', e);
    } finally {
      if (seq === historyLoadSeq) {
        isBusy.value = false;
        status.value = '';
        // 检查是否有后台任务需要恢复
        const resumedBackground = await checkAndResumeBackgroundTask(normalizedThreadId);
        if (!resumedBackground) {
          await checkAndResumeInterruptedStream(normalizedThreadId);
        }
      }
    }
  };

  const sendMessage = async (
    question: string,
    options?: SendMessageOptions
  ) => {
    if (!question.trim() || isBusy.value) return;

    const shouldAppendUser = options?.appendUserMessage !== false;

    if (shouldAppendUser) {
      hidePreviousAssistantSuggestions(messages.value);
      const displayQuestion = normalizeDisplayedUserQuestion(question);
      const userMsg: ChatMessage = {
        id: Date.now().toString(),
        role: 'user',
        content: displayQuestion,
      };
      messages.value.push(userMsg);
    }

    // 对于草案修订/确认/取消操作，找到并更新原有的 assistant 消息（包含 research_draft_created 事件）
    // 而不是创建新消息，这样用户可以在同一个窗口中继续编辑
    const isDraftAction = options?.confirmationStatus && ['revised', 'confirmed', 'cancelled'].includes(options.confirmationStatus);
    let assistantMsg: ChatMessage | null = null;

    if (isDraftAction && options?.draftId) {
      // 从后往前查找包含对应 draft_id 的 assistant 消息
      for (let i = messages.value.length - 1; i >= 0; i--) {
        const msg = messages.value[i];
        if (msg.role === 'assistant' && msg.processEvents) {
          const hasMatchingDraft = msg.processEvents.some(
            (evt) =>
              (evt.kind === 'research_draft_created' || evt.kind === 'research_draft_updated') &&
              evt.metadata?.draft_id === options.draftId
          );
          if (hasMatchingDraft) {
            assistantMsg = msg;
            // 清空旧的事件和内容，准备接收新的草案
            assistantMsg.content = '';
            assistantMsg.reasoning = '';
            assistantMsg.processEvents = [];
            assistantMsg.statusHistory = [];
            assistantMsg.isStreaming = true;
            break;
          }
        }
      }
    }

    // 如果没有找到原有的消息，创建新的 assistant 消息
    if (!assistantMsg) {
      const assistantId = (Date.now() + 1).toString();
      assistantMsg = reactive<ChatMessage>({
        id: assistantId,
        role: 'assistant',
        content: '',
        reasoning: '',
        statusHistory: [],
        processEvents: [],
        isStreaming: true,
      });
      // 新创建的消息直接 push 到 messages 数组
      messages.value.push(assistantMsg);
    }

    let pendingChunk = '';
    let pendingThinking = '';
    let pendingStatus: string | null = null;
    let pendingEvents: AgentProcessEvent[] = [];
    let pendingDraftChunk = '';
    const seenEventIds = new Set<string>();

    const flushPendingUpdates = () => {
      // 优先处理状态和事件
      if (pendingEvents.length > 0) {
        const events = assistantMsg.processEvents || [];
        events.push(...pendingEvents);
        pendingEvents = [];
        assistantMsg.processEvents = events;
      }

      if (pendingStatus !== null) {
        const statusMsg = pendingStatus;
        pendingStatus = null;
        if (statusMsg) {
          const history = assistantMsg.statusHistory || [];
          const lastStatus = history[history.length - 1];
          if (statusMsg !== lastStatus) {
            history.push(statusMsg);
            assistantMsg.statusHistory = history;
          }
        }
        status.value = statusMsg || '';
      }

      // 处理草案流式内容：将收到的草案片段写入 processEvents 中
      if (pendingDraftChunk) {
        const events = assistantMsg.processEvents || [];
        // 查找或创建 research_draft_streaming 事件
        let streamingEvent = events.find(e => e.kind === 'research_draft_streaming');
        if (!streamingEvent) {
          streamingEvent = {
            id: `draft_stream_${Date.now()}`,
            kind: 'research_draft_streaming',
            title: '正在生成研究方案...',
            detail: '',
            level: 'info',
            metadata: { draft_content: '' },
          };
          events.push(streamingEvent);
        }
        streamingEvent.metadata = streamingEvent.metadata || {};
        (streamingEvent.metadata as any).draft_content = ((streamingEvent.metadata as any).draft_content || '') + pendingDraftChunk;
        assistantMsg.processEvents = events;
        pendingDraftChunk = '';
      }

      if (pendingThinking) {
        assistantMsg.reasoning = (assistantMsg.reasoning || '') + pendingThinking;
        pendingThinking = '';
      }

      // 移除人工打字机效果，直接将缓存的流式内容合并
      // 保持原生的大模型输出节奏，避免因长度除以15带来的变速卡顿感
      if (pendingChunk) {
        assistantMsg.content = (assistantMsg.content || '') + pendingChunk;
        pendingChunk = '';
      }
    };

    /** SSE 每来一条即刷入 UI，不做定时合并延迟 */
    const flushImmediately = () => {
      flushPendingUpdates();
    };

    // assistantMsg 已在上面的 if (!assistantMsg) 分支中 push 到 messages 数组
    // 对于草案修订场景，assistantMsg 是 messages 中的引用，不需要再次 push
    // 不需要再次 push，避免重复

    isBusy.value = true;
    status.value = '思考中...';
    const threadId = options?.threadId;
    const streamThreadId = String(threadId || '').trim();

    // 创建本次请求的 AbortController（普通流中断）
    const controller = new AbortController();
    streamAbortController = controller;
    if (streamThreadId) {
      activeThreadId.value = streamThreadId;
    }
    const effectiveResearchMode = isGuestMode.value
      ? 'normal'
      : (options?.researchMode ?? researchMode.value);

    // 访客固定轻量模式：不携带技能，关闭思考链
    const finalSkills = isGuestMode.value
      ? []
      : normalizeSelectedSkills(options?.selectedSkills ?? selectedSkills.value);
    const effectiveThinking = isGuestMode.value ? false : options?.thinking;

    let suggestionsTimeout: ReturnType<typeof setTimeout> | null = null;

    const clearSuggestionsTimeout = () => {
      if (suggestionsTimeout) {
        clearTimeout(suggestionsTimeout);
        suggestionsTimeout = null;
      }
    };

    const applyFollowUpSuggestions = (rawSuggestions: string[]) => {
      clearSuggestionsTimeout();
      assistantMsg.followUpSuggestions = mapFollowUpSuggestions(rawSuggestions);
      assistantMsg.suggestionsLoading = false;
    };

    const scheduleSuggestionsTimeout = () => {
      if (!FOLLOW_UP_SUGGESTIONS_ENABLED) return;
      clearSuggestionsTimeout();
      suggestionsTimeout = setTimeout(() => {
        if (assistantMsg.suggestionsLoading) {
          assistantMsg.suggestionsLoading = false;
        }
      }, FOLLOW_UP_SUGGESTIONS_TIMEOUT_MS);
    };

    try {
      persistPendingStreamState(question, options);
      console.log('[useAgentChat] Starting query:', question);
      const result = await AgentAPI.queryStream(
        question,
        (textChunk) => {
          pendingChunk += textChunk;
          flushImmediately();
        },
        (statusMsg) => {
          pendingStatus = statusMsg || '';
          flushImmediately();
        },
        {
          threadId,
          researchMode: effectiveResearchMode,
          thinking: effectiveThinking,
          kbIds: options?.kbIds || selectedKbs.value,
          useSearch: options?.useSearch ?? searchEnabled.value,
          selectedSkills: finalSkills,
          intentMode: options?.intentMode,
          draftId: options?.draftId,
          confirmationStatus: options?.confirmationStatus,
          researchPlan: options?.researchPlan,
          model: options?.model || selectedModel.value || undefined,
          token: authState.value.token || undefined,
          endpoint: isGuestMode.value ? '/api/v2/guest/query' : '/api/v2/agent-runtime/query',
          deviceId: isGuestMode.value ? getOrCreateGuestDeviceId() : undefined,
          signal: controller.signal,
          fileIds: options?.fileIds,
          agentCode,
          metadata: options?.metadata,
        },
        (event) => {
          if (streamThreadId && activeThreadId.value && streamThreadId !== activeThreadId.value) {
            return;
          }

          if (event.kind === 'artifact_update' && event.metadata) {
            upsertArtifact(artifacts.value, event.metadata as any);
            return;
          }

          const events = assistantMsg.processEvents || [];

          // 根据 id 去重防止后端重复发送导致的渲染重复
          if (event.id && (seenEventIds.has(event.id) || events.some(e => e.id === event.id))) {
            console.log('[useAgentChat] Ignoring duplicate event:', event.id);
            return;
          }

          if (event.id) {
            seenEventIds.add(event.id);
          }

          pendingEvents.push(event);
          flushImmediately();

          if (event.kind === 'artifact' && event.metadata) {
            const art = event.metadata as any;
            console.log('[useAgentChat] New artifact:', art);
            upsertArtifact(artifacts.value, art);
          }
        },
        (thinkingText) => {
          pendingThinking += thinkingText;
          flushImmediately();
        },
        (draftChunk) => {
          // 处理研究方案流式输出
          pendingDraftChunk += draftChunk;
          flushImmediately();
        },
        (rawSuggestions) => {
          applyFollowUpSuggestions(rawSuggestions);
        },
      );
      flushImmediately();

      if (
        FOLLOW_UP_SUGGESTIONS_ENABLED
        && assistantMsg.content?.trim()
        && !result.backgroundTaskId
        && effectiveResearchMode !== 'deep_research'
        && !finalSkills.includes('deep-research')
        && !assistantMsg.followUpSuggestions?.length
      ) {
        assistantMsg.suggestionsLoading = true;
        scheduleSuggestionsTimeout();
      }

      // 如果返回了 backgroundTaskId，说明深度研究已转为后台任务
      if (result.backgroundTaskId) {
        console.log('[useAgentChat] Background task started:', result.backgroundTaskId);
        clearPendingStreamState(streamThreadId);
        
        // 立即订阅后台任务事件流（在获取任务信息之前），避免错过事件
        assistantMsg.isStreaming = true;
        isBusy.value = true;
        subscribeToBackgroundTask(result.backgroundTaskId, streamThreadId, assistantMsg);
        
        // 异步获取任务信息（不阻塞订阅）
        try {
          const taskStatus = await AgentAPI.getTaskStatus(result.backgroundTaskId, streamThreadId);
          activeBackgroundTask.value = {
            task_id: taskStatus.task_id,
            thread_id: taskStatus.thread_id,
            user_id: '',
            question: question,
            draft_id: null,
            status: taskStatus.status as any,
            created_at: taskStatus.created_at,
            started_at: taskStatus.started_at,
            finished_at: taskStatus.finished_at,
            error_message: taskStatus.error_message,
          };
        } catch (e) {
          console.error('[useAgentChat] Failed to get task info:', e);
        }
        
        return;
      }

      // 用户主动中断
      if (result.aborted) {
        flushImmediately();
        console.log('[useAgentChat] Stream aborted by user');
        clearPendingStreamState(streamThreadId);
        isBusy.value = false;
        status.value = '';
        assistantMsg.isStreaming = false;
        assistantMsg.suggestionsLoading = false;
        streamAbortController = null;
        if (isGuestMode.value && threadId) {
          await saveGuestConversation(threadId, messages.value);
        }
        return;
      }

      // 检查流是否被中断（未收到 [DONE]）
      if (result.interrupted && streamThreadId && !isGuestMode.value) {
        console.warn('[useAgentChat] Stream was interrupted, attempting to recover from history...');
        status.value = '连接中断，正在从历史记录恢复完整结果...';
        try {
          const recovered = await recoverAssistantContentFromHistory(streamThreadId, assistantMsg);
          if (recovered) {
            console.log('[useAgentChat] Recovered content from history');
          }
        } catch (recoveryErr) {
          console.error('[useAgentChat] Failed to recover from history:', recoveryErr);
          // 恢复失败不影响主流程，只是内容可能不完整
        }
      }

      console.log('[useAgentChat] Stream completed successfully');
      clearSuggestionsTimeout();
      clearPendingStreamState(streamThreadId);
      isBusy.value = false;
      status.value = '';
      assistantMsg.isStreaming = false;
      if (assistantMsg.suggestionsLoading && !assistantMsg.followUpSuggestions?.length) {
        assistantMsg.suggestionsLoading = false;
      }
      streamAbortController = null;

      if (isGuestMode.value && threadId) {
        await saveGuestConversation(threadId, messages.value);
      }
    } catch (e: any) {
      flushImmediately();
      clearSuggestionsTimeout();
      streamAbortController = null;

      // 用户主动取消（fetch 被 abort 时会抛 AbortError）
      if (e?.name === 'AbortError') {
        console.log('[useAgentChat] Fetch aborted by user');
        clearPendingStreamState(streamThreadId);
        isBusy.value = false;
        status.value = '';
        assistantMsg.isStreaming = false;
        if (isGuestMode.value && threadId) {
          await saveGuestConversation(threadId, messages.value);
        }
        return;
      }

      console.error('[useAgentChat] Query failed:', e);
      // 网络错误时尝试从历史恢复
      if (
        streamThreadId &&
        !isGuestMode.value &&
        (
          e.message?.includes('network') ||
          e.message?.includes('fetch') ||
          e.message?.includes('Failed to query')
        )
      ) {
        try {
          const recovered = await recoverAssistantContentFromHistory(streamThreadId, assistantMsg);
          if (recovered) {
            console.log('[useAgentChat] Recovered content after error');
          }
        } catch (_) { /* 恢复失败不影响主流程 */ }
      }
      if (!assistantMsg.content || assistantMsg.content.length < 10) {
        assistantMsg.content += `\n\n[发生错误: ${e.message}]`;
      } else {
        assistantMsg.content += `\n\n---\n*注：连接中断，以上内容可能不完整。可稍后重新进入当前会话查看服务端已保存的完整结果。*`;
      }
      isBusy.value = false;
      status.value = '';
      assistantMsg.isStreaming = false;
    }
  };

  const clearMessages = () => {
    if (taskUnsubscribe) {
      taskUnsubscribe();
      taskUnsubscribe = null;
    }
    messages.value = [];
    status.value = '';
    artifacts.value = [];
    activeThreadId.value = '';
    clearPendingStreamState();
    isBusy.value = false;
    backgroundTaskStreaming.value = false;
    activeBackgroundTask.value = null;
    
    // Resume to default modes for a fresh chat experience
    researchMode.value = 'normal';
    searchEnabled.value = false;
  };

  const setResearchMode = (mode: 'normal' | 'deep_research') => {
    researchMode.value = mode;
  };

  const toggleResearchMode = () => {
    researchMode.value = researchMode.value === 'deep_research' ? 'normal' : 'deep_research';
  };

  const rollbackConversation = async (threadId: string | undefined, keepUserTurns: number) => {
    if (!threadId) {
      messages.value = trimMessagesByUserTurns(messages.value, keepUserTurns);
      return;
    }

    if (!isGuestMode.value) {
      await ConversationAPI.rollback(threadId, keepUserTurns);
    }
    messages.value = trimMessagesByUserTurns(messages.value, keepUserTurns);
    if (isGuestMode.value) {
      await saveGuestConversation(threadId, messages.value);
    }
    artifacts.value = [];
  };

  return {
    messages,
    isBusy,
    status,
    agentInfo,
    selectedModel,
    selectedKbs,
    searchEnabled,
    selectedSkills,
    researchMode,
    setResearchMode,
    toggleResearchMode,
    artifacts,
    isGuestMode,
    activeBackgroundTask,
    backgroundTaskStreaming,
    loadAgentInfo,
    loadHistory,
    sendMessage,
    clearMessages,
    rollbackConversation,
    cancelStream,
    cancelBackgroundTask,
    checkAndResumeBackgroundTask,
    checkAndResumeInterruptedStream,
    getPendingInterruptedThreadId,
  };
}
