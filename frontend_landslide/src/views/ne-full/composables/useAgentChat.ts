import { computed, getCurrentInstance, onUnmounted, reactive, ref, watch } from 'vue';
import { AgentAPI, ChatMessage, AgentInfo, DEFAULT_AGENT_CODE, resolveAgentWorkflow, type AgentProcessEvent, type ResearchPlanPayload, type BackgroundTaskInfo, type FollowUpSuggestion } from '@/api/agent-v2';
import { ConversationAPI } from '@/api/conversations';
import { FeedbackAPI, feedbackKeysForMessages } from '@/api/feedback';
import { authState } from '@/utils/request';
import {
  getOrCreateGuestDeviceId,
  loadGuestMessages,
  saveGuestConversation,
} from '@/services/storage/guestIndexedDb';
import { readComposerPrefs, writeComposerPrefs } from './useComposerPrefs';
import {
  ensureNotificationPermission,
  flashTitleWhileHidden,
  notifyTaskFinished,
} from './useTaskNotifications';
const PENDING_STREAM_STORAGE_KEY = 'agent_v2_pending_stream';
const PENDING_STREAM_TTL_MS = 10 * 60 * 1000;
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

const processEventDedupeKey = (event: AgentProcessEvent): string => {
  const eventId = String(event.id || '').trim();
  if (!eventId) return '';

  const metadata = event.metadata || {};
  const rawInput = metadata.input;
  const input = rawInput && typeof rawInput === 'object' && !Array.isArray(rawInput)
    ? rawInput as Record<string, unknown>
    : {};
  const invocationKey = String(
    metadata.tool_run_id
      || metadata.tool_call_id
      || metadata.task_id
      || metadata.query
      || input.query
      || '',
  ).trim();
  const timestamp = event.timestamp == null ? '' : String(event.timestamp);

  // Older backends could reuse the same id for multiple web-search results.
  // Keep SSE replay protection without dropping a later, distinct invocation.
  return [eventId, invocationKey, timestamp].join(':');
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
  deepRetrieval?: boolean;
  intentMode?: 'auto' | 'manual';
  draftId?: string;
  confirmationStatus?: 'pending' | 'confirmed' | 'revised' | 'cancelled';
  researchPlan?: ResearchPlanPayload;
  model?: string;
  appendUserMessage?: boolean;
  fileIds?: string[];
};

type PendingStreamState = {
  threadId: string;
  question: string;
  researchMode: 'normal' | 'deep_research';
  selectedSkills: string[];
  kbIds: string[];
  useSearch: boolean;
  deepRetrieval?: boolean;
  intentMode?: 'auto' | 'manual';
  draftId?: string;
  confirmationStatus?: 'pending' | 'confirmed' | 'revised' | 'cancelled';
  researchPlan?: ResearchPlanPayload;
  model?: string;
  savedAt: number;
  resumeAttempts: number;
};

type ThinkingMarkupSplitState = {
  buffer: string;
  inThinking: boolean;
};

const isUserRole = (role: string) => role === 'human' || role === 'user';
const isAssistantRole = (role: string) => role === 'ai' || role === 'assistant';
const THINKING_TAG_CARRY_CHARS = 16;

const createThinkingMarkupSplitState = (): ThinkingMarkupSplitState => ({
  buffer: '',
  inThinking: false,
});

const appendUniqueText = (current: string, next: string): string => {
  const existing = String(current || '');
  const addition = String(next || '');
  if (!addition) return existing;
  if (!existing) return addition;
  if (existing.includes(addition)) return existing;
  if (addition.includes(existing)) return addition;
  return `${existing}${addition}`;
};

const splitThinkingMarkupChunk = (
  state: ThinkingMarkupSplitState,
  chunk: string,
  flush = false,
): { content: string; reasoning: string } => {
  state.buffer += String(chunk || '');
  let content = '';
  let reasoning = '';

  while (state.buffer) {
    if (state.inThinking) {
      const closeMatch = state.buffer.match(/<\/(?:think|thought)>/i);
      if (!closeMatch || closeMatch.index === undefined) {
        if (flush) {
          reasoning += state.buffer;
          state.buffer = '';
        } else {
          const emitLength = Math.max(0, state.buffer.length - THINKING_TAG_CARRY_CHARS);
          reasoning += state.buffer.slice(0, emitLength);
          state.buffer = state.buffer.slice(emitLength);
        }
        break;
      }

      reasoning += state.buffer.slice(0, closeMatch.index);
      state.buffer = state.buffer.slice(closeMatch.index + closeMatch[0].length);
      state.inThinking = false;
      continue;
    }

    const openMatch = state.buffer.match(/<(?:think|thought)>/i);
    if (!openMatch || openMatch.index === undefined) {
      if (flush) {
        content += state.buffer;
        state.buffer = '';
      } else {
        const emitLength = Math.max(0, state.buffer.length - THINKING_TAG_CARRY_CHARS);
        content += state.buffer.slice(0, emitLength);
        state.buffer = state.buffer.slice(emitLength);
      }
      break;
    }

    content += state.buffer.slice(0, openMatch.index);
    state.buffer = state.buffer.slice(openMatch.index + openMatch[0].length);
    state.inThinking = true;
  }

  return { content, reasoning };
};

const splitCompleteAssistantContent = (content: string): { content: string; reasoning: string } => {
  const state = createThinkingMarkupSplitState();
  return splitThinkingMarkupChunk(state, content, true);
};

const resolveModelSupportsThinking = (
  agentInfo: AgentInfo | null,
  modelName: string | null | undefined,
) => {
  const models = agentInfo?.available_models || [];
  if (!models.length) return true;
  const active = models.find((model) => model.name === modelName) || models[0];
  return active?.supports_thinking !== false;
};

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

const normalizeCompactionSummary = (content: string): string => {
  const text = String(content || '').trim();
  if (!text.startsWith('Another language model started to solve this problem')) {
    return text;
  }
  const newline = text.indexOf('\n');
  return newline >= 0 ? text.slice(newline + 1).trim() : text;
};

const compactionHistoryEvent = (msg: any): AgentProcessEvent => {
  const metadata = {
    ...((msg?.metadata?.context_compaction || {}) as Record<string, unknown>),
  };
  const windowId = String(metadata.window_id || msg?.id || 'context-window');
  const before = Number(metadata.tokens_before || 0);
  const after = Number(metadata.tokens_after || 0);
  const removed = Number(metadata.removed_messages || 0);
  const metrics = before && after ? `约 ${before} → ${after} tokens` : '历史已归档';
  return {
    id: `context-compaction:${windowId}`,
    kind: 'context_compaction',
    title: '上下文窗口已压缩',
    detail: `${metrics}${removed ? `，归档 ${removed} 条历史消息` : ''}`,
    level: 'success',
    tool: 'context_compression',
    summary: normalizeCompactionSummary(String(msg?.content || '')),
    metadata,
  };
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

const extractFallbackAnswerFromEvent = (event: AgentProcessEvent): string => {
  const detail = String(event.detail || '').trim();
  if (!detail) return '';

  const kind = String(event.kind || '').toLowerCase();
  if (kind === 'answer') return detail;

  const metadata = event.metadata || {};
  const runtimeType = String(metadata.runtime_type || '').toLowerCase();
  const role = String(metadata.role || '').toLowerCase();
  const finalRoles = new Set(['reviewer', 'report_writer', 'final_writer', 'coordinator']);

  if (runtimeType === 'coordination.unit.completed' && finalRoles.has(role)) {
    return detail;
  }

  return '';
};

const applyFallbackAnswerFromEvent = (event: AgentProcessEvent, assistantMsg: ChatMessage) => {
  if (String(assistantMsg.content || '').trim()) return;

  const fallbackAnswer = extractFallbackAnswerFromEvent(event);
  if (fallbackAnswer) {
    const split = splitCompleteAssistantContent(fallbackAnswer);
    assistantMsg.content = split.content;
    assistantMsg.reasoning = appendUniqueText(assistantMsg.reasoning || '', split.reasoning);
  }
};

const normalizeHistoryMessages = (history: any[]): ChatMessage[] => {
  const normalized: ChatMessage[] = [];
  let pendingAssistant: ChatMessage | null = null;
  let pendingStatuses: string[] = [];
  let pendingCompactionEvents: AgentProcessEvent[] = [];

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

    if (role === 'context_compaction') {
      pendingCompactionEvents.push(compactionHistoryEvent(msg));
      continue;
    }

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

      const splitAssistant = splitCompleteAssistantContent(content);
      if (!splitAssistant.content && !splitAssistant.reasoning) {
        // 纯工具调用轮的空 AI 消息：没有可展示内容，跳过。
        continue;
      }
      if (pendingAssistant) {
        // 同一轮的多段模型输出（工具轮预告 + 最终答案）直播时累积在同一个气泡，
        // 历史回放必须同样拼接；直接覆盖会让刷新后的正文比直播短一大截。
        pendingAssistant.content = pendingAssistant.content
          ? `${pendingAssistant.content}\n\n${splitAssistant.content}`
          : splitAssistant.content;
        pendingAssistant.reasoning = appendUniqueText(
          pendingAssistant.reasoning || '',
          splitAssistant.reasoning,
        );
        const followUps = mapFollowUpSuggestions(
          msg?.metadata?.follow_up_suggestions || msg?.metadata?.followUpSuggestions || [],
        );
        if (followUps.length > 0) {
          pendingAssistant.followUpSuggestions = followUps;
        }
        // 两段 AI 消息之间积累的压缩过程事件并入同一气泡，不能随清空丢弃
        // （首段分支带 processEvents，续段分支此前直接丢掉）。
        if (pendingCompactionEvents.length > 0) {
          pendingAssistant.processEvents = [
            ...(pendingAssistant.processEvents || []),
            ...pendingCompactionEvents,
          ];
        }
      } else {
        pendingAssistant = {
          id,
          role: 'assistant',
          content: splitAssistant.content,
          reasoning: appendUniqueText(String(msg?.metadata?.reasoning || ''), splitAssistant.reasoning),
          statusHistory: [...pendingStatuses],
          processEvents: [...pendingCompactionEvents],
          followUpSuggestions: mapFollowUpSuggestions(
            msg?.metadata?.follow_up_suggestions || msg?.metadata?.followUpSuggestions || [],
          ),
        };
      }
      pendingCompactionEvents = [];

      appendStatus('回答生成完成');
    }
  }

  flushPendingAssistant();

  if (pendingCompactionEvents.length > 0) {
    normalized.push({
      id: `context-compaction-tail-${Date.now()}`,
      role: 'assistant',
      content: '',
      processEvents: pendingCompactionEvents,
    });
  }

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

const readPendingStreamState = (storageKey: string): PendingStreamState | null => {
  if (typeof window === 'undefined') return null;
  try {
    const raw = sessionStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PendingStreamState;
    if (!parsed?.threadId || !parsed?.question || !parsed?.savedAt) {
      sessionStorage.removeItem(storageKey);
      return null;
    }
    if (Date.now() - parsed.savedAt > PENDING_STREAM_TTL_MS) {
      sessionStorage.removeItem(storageKey);
      return null;
    }
    return parsed;
  } catch {
    sessionStorage.removeItem(storageKey);
    return null;
  }
};

const writePendingStreamState = (storageKey: string, state: PendingStreamState) => {
  if (typeof window === 'undefined') return;
  sessionStorage.setItem(storageKey, JSON.stringify(state));
};

const clearPendingStreamState = (storageKey: string, threadId?: string) => {
  if (typeof window === 'undefined') return;
  const current = readPendingStreamState(storageKey);
  if (!current) return;
  if (threadId && current.threadId !== threadId) return;
  sessionStorage.removeItem(storageKey);
};

const normalizeSelectedSkills = (skills?: string[]): string[] => {
  if (!Array.isArray(skills)) return [];
  return Array.from(new Set(skills.map((skill) => String(skill || '').trim()).filter(Boolean)));
};

export function useAgentChat(config: { agentCode?: string } = {}) {
  const agentCode = config.agentCode || DEFAULT_AGENT_CODE;
  const pendingStreamStorageKey = `${PENDING_STREAM_STORAGE_KEY}:${agentCode}`;
  const readAgentPendingStreamState = (): PendingStreamState | null => {
    const scopedState = readPendingStreamState(pendingStreamStorageKey);
    if (scopedState || agentCode !== DEFAULT_AGENT_CODE || typeof window === 'undefined') {
      return scopedState;
    }

    // Migrate the pre-agent-namespace key once so an interrupted conversation
    // created before this feature remains recoverable by the default agent.
    const legacyState = readPendingStreamState(PENDING_STREAM_STORAGE_KEY);
    if (!legacyState) return null;
    writePendingStreamState(pendingStreamStorageKey, legacyState);
    sessionStorage.removeItem(PENDING_STREAM_STORAGE_KEY);
    return legacyState;
  };
  const clearAgentPendingStreamState = (threadId?: string) => {
    clearPendingStreamState(pendingStreamStorageKey, threadId);
    if (agentCode === DEFAULT_AGENT_CODE) {
      clearPendingStreamState(PENDING_STREAM_STORAGE_KEY, threadId);
    }
  };
  const messages = ref<ChatMessage[]>([]);
  const isBusy = ref(false);
  const status = ref('');
  const agentInfo = ref<AgentInfo | null>(null);
  const selectedModel = ref<string>('');
  const selectedKbs = ref<string[]>([]);
  const searchEnabled = ref(false);
  const deepRetrievalEnabled = ref(false);
  const selectedSkills = ref<string[]>([]);
  const normalizeWorkflowName = (value: unknown) =>
    String(value || '').trim().toLowerCase().replace(/-/g, '_');
  const deepResearchAvailable = computed(() =>
    (agentInfo.value?.allowed_workflows || [])
      .some((workflow) => normalizeWorkflowName(workflow) === 'deep_research')
  );
  const searchAvailable = computed(() => agentInfo.value?.search_enabled === true);
  // Explicit workflow mode and Skill selection are separate controls.  A
  // selected Skill's preferred workflow is resolved from Agent Info below;
  // no Skill key is allowed to impersonate an explicit workflow request.
  const researchMode = ref<'normal' | 'deep_research'>('normal');
  const artifacts = ref<any[]>([]);
  const forceGuestMode = import.meta.env.VITE_FORCE_GUEST_MODE === 'true';
  const isGuestMode = computed(() => !authState.value.token);
  const isGuestRestricted = computed(() => forceGuestMode || isGuestMode.value);
  const shouldPersistGuestLocally = computed(() => isGuestMode.value);
  const resolveQueryEndpoint = () => {
    return isGuestRestricted.value ? '/api/v2/guest/query' : '/api/v2/agent-runtime/query';
  };
  const activeThreadId = ref('');

  // ── Composer 偏好持久化（按智能体隔离）──────────────────
  // 恢复只发生在 loadAgentInfo 应用默认值之后；watcher 在恢复完成前不写入，
  // 避免把初始化中间态覆盖进用户已保存的偏好。访客不读不写。
  let composerPrefsRestored = false;

  const applyStoredComposerToggles = () => {
    if (isGuestRestricted.value) return;
    const prefs = readComposerPrefs(agentCode);
    if (!prefs) return;
    if (typeof prefs.searchEnabled === 'boolean') {
      searchEnabled.value = searchAvailable.value && prefs.searchEnabled;
    }
    if (typeof prefs.deepRetrieval === 'boolean') {
      deepRetrievalEnabled.value = prefs.deepRetrieval;
    }
  };

  const applyStoredComposerPrefs = () => {
    if (isGuestRestricted.value) return;
    const prefs = readComposerPrefs(agentCode);
    if (!prefs) return;
    const info = agentInfo.value;

    const modelIds = new Set((info?.available_models || []).map((m) => m.name));
    if (prefs.model && modelIds.has(prefs.model)) {
      selectedModel.value = prefs.model;
    }

    const allowedKbIds = info?.kb_ids || [];
    if (Array.isArray(prefs.kbIds)) {
      const validKbs = prefs.kbIds.filter((id) => allowedKbIds.includes(id));
      // 存量选择全部失效时回落默认；显式存过空数组则尊重“全不选”。
      if (validKbs.length > 0 || prefs.kbIds.length === 0) {
        selectedKbs.value = validKbs;
      }
    }

    applyStoredComposerToggles();

    if (Array.isArray(prefs.skills)) {
      const availableSkillNames = new Set(
        (info?.available_skills || [])
          .map((skill: any) => String(skill?.name || skill?.id || '').trim())
          .filter(Boolean)
      );
      const requiredSkills = normalizeSelectedSkills([
        ...(info?.required_skills || []),
        ...(info?.available_skills || [])
          .filter((skill: any) => skill.required)
          .map((skill: any) => skill.name),
      ]).filter((id) => availableSkillNames.has(id));
      const storedSkills = normalizeSelectedSkills(prefs.skills)
        .filter((id) => availableSkillNames.has(id));
      selectedSkills.value = Array.from(new Set([...requiredSkills, ...storedSkills]));
    }
  };

  watch(
    [selectedModel, selectedKbs, searchEnabled, deepRetrievalEnabled, selectedSkills],
    () => {
      if (!composerPrefsRestored || isGuestRestricted.value) return;
      // 智能体当前没有联网能力时，运行值一定是 false；此时保留已存的用户
      // 偏好，否则能力恢复后用户的「联网=开」已经被覆盖掉了。
      const storedSearch = readComposerPrefs(agentCode)?.searchEnabled;
      writeComposerPrefs(agentCode, {
        model: selectedModel.value || undefined,
        kbIds: [...selectedKbs.value],
        searchEnabled: searchAvailable.value
          ? searchEnabled.value
          : (typeof storedSearch === 'boolean' ? storedSearch : searchEnabled.value),
        deepRetrieval: deepRetrievalEnabled.value,
        skills: [...selectedSkills.value],
      });
    },
    { deep: true },
  );

  const resolveRequestSelectedSkills = (rawSkills?: string[]): string[] | undefined => {
    if (isGuestRestricted.value) return [];
    const normalized = normalizeSelectedSkills(rawSkills ?? selectedSkills.value);
    return normalized.length > 0 ? normalized : undefined;
  };
  let historyLoadSeq = 0;

  // ── 后台任务状态 ──────────────────────────────────────
  let backgroundRestoreTimer: ReturnType<typeof setTimeout> | null = null;
  let backgroundRestoreDisposed = false;
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
    const splitRecovered = splitCompleteAssistantContent(recoveredContent);

    if (splitRecovered.reasoning) {
      assistantMsg.reasoning = appendUniqueText(assistantMsg.reasoning || '', splitRecovered.reasoning);
    }

    if (options?.replaceContent || splitRecovered.content.length > (assistantMsg.content || '').length) {
      assistantMsg.content = splitRecovered.content;
    }

    if (options?.refreshArtifacts) {
      await refreshThreadArtifacts(normalizedThreadId);
    }

    return true;
  };

  const persistPendingStreamState = (question: string, options?: SendMessageOptions) => {
    const threadId = String(options?.threadId || '').trim();
    if (!threadId || isGuestMode.value) return;
    const existing = readAgentPendingStreamState();
    const resumeAttempts = (
      existing?.threadId === threadId &&
      existing?.question === question
    ) ? existing.resumeAttempts : 0;

    writePendingStreamState(pendingStreamStorageKey, {
      threadId,
      question,
      researchMode: options?.researchMode ?? researchMode.value,
      selectedSkills: normalizeSelectedSkills(options?.selectedSkills ?? selectedSkills.value),
      kbIds: [...(options?.kbIds ?? selectedKbs.value)],
      useSearch: options?.useSearch ?? searchEnabled.value,
      deepRetrieval: options?.deepRetrieval ?? deepRetrievalEnabled.value,
      intentMode: options?.intentMode,
      draftId: options?.draftId,
      confirmationStatus: options?.confirmationStatus,
      researchPlan: options?.researchPlan,
      model: options?.model || selectedModel.value || undefined,
      savedAt: Date.now(),
      resumeAttempts,
    });
  };

  const getPendingInterruptedThreadId = (): string | null => {
    if (isGuestMode.value) return null;
    return readAgentPendingStreamState()?.threadId || null;
  };

  /** 订阅后台任务事件流 */
  const subscribeToBackgroundTask = (taskId: string, threadId: string, assistantMsg: ChatMessage) => {
    // 先取消之前的订阅
    if (taskUnsubscribe) {
      taskUnsubscribe();
      taskUnsubscribe = null;
    }

    backgroundTaskStreaming.value = true;
    const thinkingSplitState = createThinkingMarkupSplitState();
    const flushBackgroundThinkingTail = () => {
      const split = splitThinkingMarkupChunk(thinkingSplitState, '', true);
      if (split.content) {
        assistantMsg.content = (assistantMsg.content || '') + split.content;
      }
      if (split.reasoning) {
        assistantMsg.reasoning = (assistantMsg.reasoning || '') + split.reasoning;
      }
    };

    taskUnsubscribe = AgentAPI.subscribeTaskStream(
      taskId,
      threadId,
      agentCode,
      (textChunk, mode) => {
        if (mode === 'replace') {
          thinkingSplitState.buffer = '';
          thinkingSplitState.inThinking = false;
        }
        const split = splitThinkingMarkupChunk(thinkingSplitState, textChunk, mode === 'replace');
        assistantMsg.reasoning = mode === 'replace'
          ? appendUniqueText(assistantMsg.reasoning || '', split.reasoning)
          : (assistantMsg.reasoning || '') + split.reasoning;
        assistantMsg.content = mode === 'replace'
          ? split.content
          : (assistantMsg.content || '') + split.content;
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
        const eventKey = processEventDedupeKey(event);
        if (eventKey && events.some(e => processEventDedupeKey(e) === eventKey)) return;
        applyFallbackAnswerFromEvent(event, assistantMsg);
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
        flushBackgroundThinkingTail();
        notifyTaskFinished('深度研究任务已完成', '后台任务已生成完整结果，点击查看。');
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
        flushBackgroundThinkingTail();
        console.error('[useAgentChat] Background task SSE error:', error);
        // Don't immediately mark as failed — the task may still be running on the server.
        // Show a transient warning and attempt auto-recovery after a short delay.
        const currentTaskId = taskId;
        const currentThreadId = threadId;

        assistantMsg.content += `\n\n⚠️ 连接暂时中断 (${error})，正在尝试自动恢复...`;
        status.value = '正在尝试恢复连接...';

        // Auto-recovery: wait 5s then check if the task is still active on the server
        if (currentTaskId && currentThreadId) {
          if (backgroundRestoreTimer) clearTimeout(backgroundRestoreTimer);
          const timerThreadId = currentThreadId;
          backgroundRestoreTimer = setTimeout(async () => {
            if (backgroundRestoreDisposed) return;
            // 会话已切换/清空时不得恢复订阅：isBusy 是组合式单例状态，
            // 旧会话的后台任务会把新会话的 composer 锁到任务结束。
            if (activeThreadId.value !== timerThreadId) return;
            try {
              const result = await AgentAPI.getThreadActiveTask(currentThreadId, agentCode);
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
                notifyTaskFinished('深度研究任务已完成', '后台任务已生成完整结果，点击查看。');
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
          notifyTaskFinished('后台任务出现异常', '任务连接中断且未能自动恢复，请回到会话查看。');
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
      await AgentAPI.cancelTask(task.task_id, task.thread_id, agentCode);
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
      const result = await AgentAPI.getThreadActiveTask(threadId, agentCode);
      if (result.has_active_task && result.task) {
        activeBackgroundTask.value = result.task;
        // Server history may still end at the user turn while the background
        // research task is running.  Always materialize a streaming assistant
        // target before subscribing; otherwise refresh reports success but no
        // subscription is actually created.
        const lastMsg = messages.value[messages.value.length - 1];
        let assistantMsg = lastMsg && lastMsg.role === 'assistant' ? lastMsg : null;
        if (!assistantMsg) {
          assistantMsg = reactive<ChatMessage>({
            id: `background-resume-${result.task.task_id}`,
            role: 'assistant',
            content: '',
            reasoning: '',
            statusHistory: [],
            processEvents: [],
            isStreaming: true,
          });
          messages.value.push(assistantMsg);
        }
        assistantMsg.isStreaming = true;
        isBusy.value = true;
        backgroundTaskStreaming.value = true;
        subscribeToBackgroundTask(result.task.task_id, threadId, assistantMsg);
        return true;
      }
    } catch (e) {
      console.error('[useAgentChat] Failed to check background task:', e);
    }
    return false;
  };

  const checkAndResumeInterruptedStream = async (threadId: string): Promise<boolean> => {
    if (isGuestMode.value) return false;

    const pending = readAgentPendingStreamState();
    if (!pending || pending.threadId !== threadId) return false;

    const lastMsg = messages.value[messages.value.length - 1];
    const normalizedQuestion = normalizeDisplayedUserQuestion(pending.question);

    if (!lastMsg || lastMsg.role !== 'user' || lastMsg.content !== normalizedQuestion) {
      clearAgentPendingStreamState(threadId);
      return false;
    }

    // Direct streams have no durable cursor. Re-POSTing here can execute tools
    // and persist the same user turn twice, so require an explicit resend.
    clearAgentPendingStreamState(threadId);
    status.value = '检测到上次回答中断。为避免重复执行，请手动重新发送问题。';
    return false;
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
      deepRetrievalEnabled.value = false; // 默认关闭深度检索
      researchMode.value = 'normal';
      if (isGuestRestricted.value) {
        selectedSkills.value = [];
      } else {
        const declaredSelectedSkills = [
          ...(agentInfo.value?.selected_skills || []),
          ...(agentInfo.value?.required_skills || []),
          ...(agentInfo.value?.available_skills || [])
            .filter((skill) => skill.required || skill.default_selected)
            .map((skill) => skill.name),
        ];
        const availableSkillNames = new Set(
          (agentInfo.value?.available_skills || [])
            .map((skill: any) => String(skill?.name || skill?.id || '').trim())
            .filter(Boolean)
        );
        selectedSkills.value = normalizeSelectedSkills(declaredSelectedSkills)
          .filter((id) => availableSkillNames.has(id));
      }
      const allowedKbIds = agentInfo.value?.kb_ids || [];
      // 保留用户已选中的有效 ID；无可访问知识库时必须清空历史默认值。
      const validSelected = selectedKbs.value.filter((id) => allowedKbIds.includes(id));
      selectedKbs.value = validSelected.length > 0 ? validSelected : [...allowedKbIds];

      // 默认值就绪后恢复该智能体上次保存的 composer 偏好。
      applyStoredComposerPrefs();
      composerPrefsRestored = true;
    } catch (e) {
      console.error('Failed to load agent info', e);
    }
  };

  const loadHistory = async (threadId: string) => {
    const normalizedThreadId = String(threadId || '').trim();
    const seq = ++historyLoadSeq;

    // 切换会话必须同时中止仍在进行的普通流：否则 fetch 继续读流并把 chunk 写进
    // 已从 messages 摘除的 assistant 对象（孤儿对象持续增长），连接也一直占用。
    cancelStream();

    // 取消之前的后台任务订阅（避免状态串扰）
    if (taskUnsubscribe) {
      taskUnsubscribe();
      taskUnsubscribe = null;
    }
    // 会话切换后，旧会话的自动恢复定时器不得再触发（否则旧后台任务
    // 重新拉起订阅并锁死新会话的 composer）。
    if (backgroundRestoreTimer) {
      clearTimeout(backgroundRestoreTimer);
      backgroundRestoreTimer = null;
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

    // 深研请求会转后台跑数十分钟：只在用户自己发起时申请通知授权，恢复既有
    // 任务的路径不申请，避免一打开会话就弹权限框。
    if (!isGuestRestricted.value
      && (options?.researchMode ?? researchMode.value) === 'deep_research') {
      ensureNotificationPermission();
    }

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
    const seenEventKeys = new Set<string>();
    const thinkingSplitState = createThinkingMarkupSplitState();

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
    if (shouldPersistGuestLocally.value && streamThreadId) {
      await saveGuestConversation(streamThreadId, messages.value, agentCode);
    }
    const requestedResearchMode = options?.researchMode ?? researchMode.value;
    const effectiveResearchMode = !isGuestRestricted.value
      && deepResearchAvailable.value
      && requestedResearchMode === 'deep_research'
      ? 'deep_research'
      : 'normal';

    // 访客仍不携带可选技能并关闭思考链；工作流由 Profile 能力解析。
    const requestSelectedSkills = resolveRequestSelectedSkills(options?.selectedSkills);
    const effectiveUseSearch = searchAvailable.value
      && Boolean(options?.useSearch ?? searchEnabled.value);
    const effectiveDeepRetrieval = Boolean(
      options?.deepRetrieval ?? deepRetrievalEnabled.value
    );
    const requestWorkflow = resolveAgentWorkflow(agentInfo.value, {
      researchMode: effectiveResearchMode,
      selectedSkills: requestSelectedSkills,
      useSearch: effectiveUseSearch,
      guest: isGuestRestricted.value,
    });
    const requestedModel = options?.model || selectedModel.value || undefined;
    const modelSupportsThinking = resolveModelSupportsThinking(agentInfo.value, requestedModel);
    const effectiveThinking = isGuestRestricted.value
      ? false
      : (modelSupportsThinking ? options?.thinking : false);

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
      persistPendingStreamState(question, {
        ...options,
        researchMode: effectiveResearchMode,
        selectedSkills: requestSelectedSkills,
        useSearch: effectiveUseSearch,
        deepRetrieval: effectiveDeepRetrieval,
      });
      console.log('[useAgentChat] Starting query:', question);
      const result = await AgentAPI.queryStream(
        question,
        (textChunk, mode) => {
          if (mode === 'replace') {
            thinkingSplitState.buffer = '';
            thinkingSplitState.inThinking = false;
            pendingChunk = '';
            const split = splitThinkingMarkupChunk(thinkingSplitState, textChunk, true);
            assistantMsg.content = split.content;
            assistantMsg.reasoning = appendUniqueText(assistantMsg.reasoning || '', split.reasoning);
          } else {
            const split = splitThinkingMarkupChunk(thinkingSplitState, textChunk);
            pendingChunk += split.content;
            pendingThinking += split.reasoning;
          }
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
          useSearch: effectiveUseSearch,
          deepRetrieval: effectiveDeepRetrieval,
          selectedSkills: requestSelectedSkills,
          workflow: requestWorkflow,
          intentMode: options?.intentMode,
          draftId: options?.draftId,
          confirmationStatus: options?.confirmationStatus,
          researchPlan: options?.researchPlan,
          model: requestedModel,
          token: authState.value.token || undefined,
          endpoint: resolveQueryEndpoint(),
          deviceId: isGuestMode.value ? getOrCreateGuestDeviceId() : undefined,
          signal: controller.signal,
          fileIds: options?.fileIds,
          agentCode,
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

          // 按“事件 + 工具调用 + 时间”去重，避免旧后端复用 id 时吞掉后续检索结果。
          const eventKey = processEventDedupeKey(event);
          if (eventKey && (
            seenEventKeys.has(eventKey)
            || events.some(e => processEventDedupeKey(e) === eventKey)
          )) {
            console.log('[useAgentChat] Ignoring duplicate event:', eventKey);
            return;
          }

          if (eventKey) {
            seenEventKeys.add(eventKey);
          }

          if (!String(assistantMsg.content || '').trim() && !pendingChunk.trim()) {
            const fallbackAnswer = extractFallbackAnswerFromEvent(event);
            if (fallbackAnswer) {
              const split = splitCompleteAssistantContent(fallbackAnswer);
              pendingChunk = split.content;
              pendingThinking = appendUniqueText(pendingThinking, split.reasoning);
            }
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
      const tailSplit = splitThinkingMarkupChunk(thinkingSplitState, '', true);
      pendingChunk += tailSplit.content;
      pendingThinking += tailSplit.reasoning;
      flushImmediately();

      if (
        FOLLOW_UP_SUGGESTIONS_ENABLED
        && assistantMsg.content?.trim()
        && !result.backgroundTaskId
        && effectiveResearchMode !== 'deep_research'
        && !assistantMsg.followUpSuggestions?.length
      ) {
        assistantMsg.suggestionsLoading = true;
        scheduleSuggestionsTimeout();
      }

      // 如果返回了 backgroundTaskId，说明深度研究已转为后台任务
      if (result.backgroundTaskId) {
        console.log('[useAgentChat] Background task started:', result.backgroundTaskId);
        clearAgentPendingStreamState(streamThreadId);
        
        // 立即订阅后台任务事件流（在获取任务信息之前），避免错过事件
        assistantMsg.isStreaming = true;
        isBusy.value = true;
        subscribeToBackgroundTask(result.backgroundTaskId, streamThreadId, assistantMsg);
        
        // 异步获取任务信息（不阻塞订阅）
        try {
          const taskStatus = await AgentAPI.getTaskStatus(result.backgroundTaskId, streamThreadId, agentCode);
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
        clearAgentPendingStreamState(streamThreadId);
        isBusy.value = false;
        status.value = '';
        assistantMsg.isStreaming = false;
        assistantMsg.suggestionsLoading = false;
        streamAbortController = null;
        if (shouldPersistGuestLocally.value && threadId) {
          await saveGuestConversation(threadId, messages.value, agentCode);
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
      // 用户切到其他标签页时用标题角标提示回答已生成。
      flashTitleWhileHidden();
      clearSuggestionsTimeout();
      clearAgentPendingStreamState(streamThreadId);
      isBusy.value = false;
      status.value = '';
      assistantMsg.isStreaming = false;
      if (assistantMsg.suggestionsLoading && !assistantMsg.followUpSuggestions?.length) {
        assistantMsg.suggestionsLoading = false;
      }
      streamAbortController = null;

      if (shouldPersistGuestLocally.value && threadId) {
        await saveGuestConversation(threadId, messages.value, agentCode);
      }
    } catch (e: any) {
      flushImmediately();
      clearSuggestionsTimeout();
      streamAbortController = null;

      // 用户主动取消（fetch 被 abort 时会抛 AbortError）
      if (e?.name === 'AbortError') {
        console.log('[useAgentChat] Fetch aborted by user');
        clearAgentPendingStreamState(streamThreadId);
        isBusy.value = false;
        status.value = '';
        assistantMsg.isStreaming = false;
        if (shouldPersistGuestLocally.value && threadId) {
          await saveGuestConversation(threadId, messages.value, agentCode);
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
      if (shouldPersistGuestLocally.value && threadId) {
        await saveGuestConversation(threadId, messages.value, agentCode);
      }
      isBusy.value = false;
      status.value = '';
      assistantMsg.isStreaming = false;
    }
  };

  const clearMessages = () => {
    // 新建对话同样要中止在飞的流（同 loadHistory 的理由）。
    cancelStream();
    if (taskUnsubscribe) {
      taskUnsubscribe();
      taskUnsubscribe = null;
    }
    if (backgroundRestoreTimer) {
      clearTimeout(backgroundRestoreTimer);
      backgroundRestoreTimer = null;
    }
    messages.value = [];
    status.value = '';
    artifacts.value = [];
    activeThreadId.value = '';
    clearAgentPendingStreamState();
    isBusy.value = false;
    backgroundTaskStreaming.value = false;
    activeBackgroundTask.value = null;
    
    // Resume to default modes for a fresh chat experience
    researchMode.value = 'normal';
    searchEnabled.value = false;
    deepRetrievalEnabled.value = false;
    // 新会话回到用户保存的默认开关（而不是一律关闭）。
    applyStoredComposerToggles();
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

    // 反馈键由问题摘要派生，重答会复用同一个键；被回滚掉的回答如果留着旧
    // 评价，新回答一进来就会显示上一轮的赞/踩。
    const keptMessages = trimMessagesByUserTurns(messages.value, keepUserTurns);
    const keptSet = new Set(keptMessages);
    const allKeys = feedbackKeysForMessages(messages.value);
    const droppedFeedbackKeys = messages.value
      .map((msg, index) => (keptSet.has(msg) ? null : allKeys[index]))
      .filter((key): key is string => Boolean(key));

    if (!isGuestMode.value) {
      await ConversationAPI.rollback(threadId, keepUserTurns);
      if (droppedFeedbackKeys.length > 0) {
        await FeedbackAPI.removeMany(threadId, droppedFeedbackKeys);
      }
    }
    messages.value = trimMessagesByUserTurns(messages.value, keepUserTurns);
    if (shouldPersistGuestLocally.value) {
      await saveGuestConversation(threadId, messages.value, agentCode);
    }
    artifacts.value = [];
  };

  // 离开页面/卸载组件时释放在飞的流与后台订阅：没有这层清理，路由离开后
  // fetch 仍在读流、订阅仍在回调，连接与孤儿对象一直留到流自然结束。
  // getCurrentInstance 守卫让该 composable 仍可在组件外（测试）调用。
  if (getCurrentInstance()) {
    onUnmounted(() => {
      cancelStream();
      backgroundRestoreDisposed = true;
      if (backgroundRestoreTimer) {
        clearTimeout(backgroundRestoreTimer);
        backgroundRestoreTimer = null;
      }
      if (taskUnsubscribe) {
        taskUnsubscribe();
        taskUnsubscribe = null;
      }
    });
  }

  return {
    messages,
    isBusy,
    status,
    agentInfo,
    selectedModel,
    selectedKbs,
    searchEnabled,
    deepRetrievalEnabled,
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
