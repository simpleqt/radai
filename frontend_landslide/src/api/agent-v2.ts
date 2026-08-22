import { humanizeAgentStatus, workflowStartedStatus } from '@/features/process/humanize';
import type { ToolDisplay } from '@/features/process/types';

export { humanizeAgentStatus, workflowStartedStatus };
export type { ToolDisplay };

export interface ModelOption {
  name: string;
  display_name: string;
  supports_vision?: boolean;
  supports_thinking?: boolean;
}

export interface KbOption {
  id: string;
  label: string;
}

export interface SkillOption {
  skill_id?: string;
  name: string;
  label?: string;
  description?: string;
  display_description?: string;
  version?: string;
  required?: boolean;
  default_selected?: boolean;
  selectable?: boolean;
  allowed_workflows?: string[];
  preferred_workflow?: string | null;
  guest_selectable?: boolean;
}

export interface AgentInfo {
  agent_code?: string;
  ui_profile?: string | null;
  name: string;
  model: string | null;
  /** 与后端 llm_models 默认（is_default）一致，供下拉初始选中 */
  default_model?: string | null;
  available_models?: ModelOption[];
  kb_ids: string[];
  /** 知识库选项列表（含 ID 和前端展示名称），由后端从 MySQL 实时查询 */
  available_kbs?: KbOption[];
  subagent_enabled: boolean;
  search_enabled?: boolean;
  search_provider?: 'firecrawl' | null;
  max_concurrent_subagents?: number;
  selected_skills?: string[] | null;
  required_skills?: string[];
  available_skills?: SkillOption[];
  skill_catalog_status?: 'ok' | 'degraded';
  skill_catalog_error?: {
    code: string;
    message: string;
  } | null;
  /** Profile 声明的默认与可用工作流；task_type 为旧后端兼容别名。 */
  default_workflow?: string;
  allowed_workflows?: string[];
  task_type?: string;
  lightweight_mode?: boolean;
  lightweight_has_kb?: boolean;
  features: string[];
}

export function resolveAgentWorkflow(
  info: AgentInfo | null | undefined,
  options: {
    researchMode?: 'normal' | 'deep_research';
    selectedSkills?: string[];
    useSearch?: boolean;
    guest?: boolean;
  } = {},
): string | undefined {
  const normalize = (value: unknown) => String(value || '').trim().toLowerCase().replace(/-/g, '_');
  const defaultWorkflow = normalize(info?.default_workflow || info?.task_type);
  const allowed = (info?.allowed_workflows || []).map(normalize).filter(Boolean);

  if (options.guest) return defaultWorkflow || undefined;
  if (options.researchMode === 'deep_research') {
    return allowed.includes('deep_research') ? 'deep_research' : (defaultWorkflow || undefined);
  }

  const profileSelectedSkills = new Set([
    ...(info?.selected_skills || []),
    ...(info?.required_skills || []),
    ...(info?.available_skills || [])
      .filter((skill) => skill.required || skill.default_selected)
      .map((skill) => skill.name),
  ].map((skill) => String(skill || '').trim().toLowerCase()).filter(Boolean));
  const skillOptions = new Map(
    (info?.available_skills || []).map((skill) => [
      String(skill.name || '').trim().toLowerCase(),
      skill,
    ]),
  );
  const userSelectedSkills = (options.selectedSkills || [])
    .map((skill) => String(skill || '').trim().toLowerCase())
    .filter((skill) => skill && !profileSelectedSkills.has(skill));

  for (const skillName of userSelectedSkills) {
    const skill = skillOptions.get(skillName);
    const preferred = normalize(skill?.preferred_workflow);
    const skillAllowed = (skill?.allowed_workflows || []).map(normalize).filter(Boolean);
    if (
      preferred
      && allowed.includes(preferred)
      && (skillAllowed.length === 0 || skillAllowed.includes(preferred))
    ) {
      return preferred;
    }
  }

  if (options.useSearch && allowed.includes('full')) {
    return 'full';
  }
  // Leave an optional Skill without a declared/allowed preference to the
  // server-side resolver. Sending the Profile default here would turn a UI
  // fallback into an explicit workflow override.
  if (userSelectedSkills.length > 0) return undefined;
  return defaultWorkflow || undefined;
}

export interface AgentProcessEvent {
  id?: string;
  kind: 'tool_start' | 'tool_end' | 'tool_result' | 'task_start' | 'task_end' | 'status' | 'answer' | 'thinking' | 'error' | 'metrics' | string;
  title: string;
  detail: string;
  level?: 'active' | 'info' | 'success' | 'warning' | 'error';
  tool?: string;
  summary?: string;
  duration_ms?: number;
  timestamp?: number;
  /** 流水线阶段码，由后端 presenter 给出（可选） */
  phase?: string;
  /** 结构化展示载荷，由后端 presenter 给出（可选） */
  display?: ToolDisplay;
  metadata?: Record<string, unknown>;
}

export interface ResearchPlanPayload {
  topic?: string;
  time_range?: string;
  dimensions?: string[];
  evidence_sources?: string[];
  deliverable_format?: string;
  uncertainties?: string[];
  [key: string]: unknown;
}

export interface FollowUpSuggestion {
  id: string;
  text: string;
  category?: 'deep_dive' | 'related' | 'compare' | 'action' | 'source';
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  reasoning?: string; // For COT/Thinking
  statusHistory?: string[]; // To track tool usage steps
  processEvents?: AgentProcessEvent[];
  isStreaming?: boolean;
  followUpSuggestions?: FollowUpSuggestion[];
  suggestionsLoading?: boolean;
  suggestionsHidden?: boolean;
}

export interface BackgroundTaskInfo {
  task_id: string;
  thread_id: string;
  user_id: string;
  question: string;
  draft_id: string | null;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  error_message: string | null;
}

export const DEFAULT_AGENT_CODE = import.meta.env.VITE_AGENT_CODE || 'landslide';

function normalizeRuntimeEvent(data: any): { type: string | undefined; payload: any } {
  if (data && typeof data === 'object' && data.payload && typeof data.type === 'string') {
    return { type: data.type, payload: data.payload };
  }
  return { type: data?.type, payload: data };
}

function coordinationEventToProcessEvent(
  type: string | undefined,
  payload: Record<string, any>,
): AgentProcessEvent | null {
  if (!type) return null;
  if (type === 'plan.created') {
    const stages = Array.isArray(payload.stages) ? payload.stages : [];
    return {
      id: `plan.created:${payload.workflow || ''}:${stages.map((stage: any) => stage?.key || stage?.title || '').join(',')}`,
      kind: 'status',
      title: '研究流程已启动',
      detail: payload.summary || `已建立 ${stages.length} 个执行阶段。`,
      level: 'active',
      metadata: { runtime_type: 'plan_created', stages },
    };
  }
  if (type === 'plan.updated') {
    const tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    return {
      id: `plan.updated:${payload.workflow || ''}:${tasks.map((task: any) => task?.key || task?.title || '').join(',')}`,
      kind: 'status',
      title: '并行研究计划已生成',
      detail: tasks.map((task: any) => task?.title || task?.question || '').filter(Boolean).join('；'),
      level: 'success',
      metadata: { runtime_type: 'plan_updated', tasks },
    };
  }
  if (type === 'coordination.unit.completed' || type === 'coordination.unit.failed') {
    const failed = type.endsWith('.failed');
    return {
      id: `${type}:${payload.workflow || ''}:${payload.task_id || payload.task_key || payload.role || ''}`,
      kind: failed ? 'error' : 'status',
      title: failed ? `${payload.role || '协作单元'}执行失败` : `${payload.role || '协作单元'}已完成`,
      detail: payload.error || payload.answer_preview || '',
      level: failed ? 'error' : 'success',
      metadata: { runtime_type: type, role: payload.role, task_id: payload.task_id, task_key: payload.task_key, task_title: payload.task_title },
    };
  }
  if (type === 'coordination.partial_handoff') {
    const handoff = payload.handoff && typeof payload.handoff === 'object' ? payload.handoff : {};
    const claims = Array.isArray(handoff.claims) ? handoff.claims : [];
    const summary = String(handoff.summary || '').trim();
    const metadata = payload.metadata && typeof payload.metadata === 'object' ? payload.metadata : {};
    const taskStatus = String(metadata.task_status || '').trim();
    const error = String(payload.error || metadata.error || '').trim();
    const missing = Array.isArray(handoff.missing) ? handoff.missing.map((item: unknown) => String(item)) : [];
    const abnormalText = [error, summary, ...missing].join(' ');
    const degraded = Boolean(
      payload.degraded
      || payload.valid === false
      || metadata.degraded
      || taskStatus === 'task_failed'
      || taskStatus === 'task_timed_out'
      || /(?:execution timed out|timed out after|task timed out|执行超时|执行失败|必需工具未完成|不可采信|无有效结果|子任务未返回内容)/i.test(abnormalText)
    );
    return {
      id: `${type}:${payload.workflow || ''}:${payload.task_id || payload.role || ''}`,
      kind: 'status',
      title: degraded ? `${payload.role || '研究分支'}阶段性结果异常` : `${payload.role || '研究分支'}已提交阶段性结论`,
      detail: error || summary || (claims.length ? `已形成 ${claims.length} 条可供汇总的结论。` : '阶段性研究结果已交给汇总智能体。'),
      level: degraded ? 'warning' : 'success',
      metadata: { ...metadata, runtime_type: type, role: payload.role, task_id: payload.task_id, handoff, degraded, error: error || undefined },
    };
  }
  if (type === 'coordination.completed') {
    return {
      id: `coordination.completed:${payload.workflow || ''}`,
      kind: 'status',
      title: '深度研究流程已完成',
      detail: `完成 ${payload.successful_units ?? 0}/${payload.units ?? 0} 个协作单元。`,
      level: 'success',
      metadata: { ...payload, runtime_type: type },
    };
  }
  return null;
}

export interface TaskStatusResponse {
  task_id: string;
  thread_id: string;
  status: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  error_message: string | null;
  recent_events: Array<{
    id: string;
    kind: string;
    payload: Record<string, unknown>;
    timestamp: number;
  }>;
}

async function buildAgentQueryError(res: Response): Promise<Error> {
  let detail = res.statusText || '';

  try {
    const text = await res.text();
    if (text) {
      try {
        const json = JSON.parse(text) as { detail?: unknown; message?: unknown; error?: unknown };
        const payloadDetail = json.detail ?? json.message ?? json.error;
        detail = typeof payloadDetail === 'string'
          ? payloadDetail
          : payloadDetail !== undefined
            ? JSON.stringify(payloadDetail)
            : text;
      } catch {
        detail = text;
      }
    }
  } catch {
    // Keep the HTTP status visible even when the body cannot be read.
  }

  return new Error(`Failed to query agent: HTTP ${res.status}${detail ? ` ${detail}` : ''}`);
}

// ── 对话文件 API ──────────────────────────────────────

export interface ChatFileUploadResult {
  file_id: string;
  file_name: string;
  file_size: number;
  file_category: 'multimodal' | 'short_doc' | 'long_doc' | 'failed';
  parse_status: 'completed' | 'failed';
  content_length: number;
  strategy: 'vision' | 'context' | 'indexed' | 'none';
  message: string;
  error?: string;
  mime_type?: string;
}

export type ChatFileParseMode = 'auto' | 'mineru';

export const ChatFileAPI = {
  async upload(
    threadId: string,
    files: File[],
    parseMode: ChatFileParseMode = 'auto',
    token?: string,
  ): Promise<{ success: boolean; files: ChatFileUploadResult[]; thread_id: string }> {
    const formData = new FormData();
    formData.append('thread_id', threadId);
    formData.append('parse_mode', parseMode);
    for (const f of files) formData.append('files', f);

    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const res = await fetch('/api/v2/chat-files/upload', {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!res.ok) throw new Error(`文件上传失败: ${res.statusText}`);
    return res.json();
  },

  async listFiles(threadId: string, token?: string): Promise<ChatFileUploadResult[]> {
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(`/api/v2/chat-files/${encodeURIComponent(threadId)}`, { headers });
    if (!res.ok) throw new Error(`文件列表加载失败: ${res.status} ${res.statusText}`);
    const data = await res.json();
    return data.items || [];
  },

  async deleteFile(fileId: string, token?: string): Promise<void> {
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(`/api/v2/chat-files/${encodeURIComponent(fileId)}`, {
      method: 'DELETE',
      headers,
    });
    if (!res.ok) throw new Error(`文件删除失败: ${res.status} ${res.statusText}`);
  },
};

export const AgentAPI = {
  async getInfo(agentCode = DEFAULT_AGENT_CODE): Promise<AgentInfo> {
    const headers: Record<string, string> = {};
    const token = localStorage.getItem('auth_token');
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(`/api/v2/agent-runtime/profiles/${encodeURIComponent(agentCode)}/info`, { headers });
    if (!res.ok) throw new Error('Failed to fetch agent info');
    return res.json();
  },

  async queryStream(
    question: string,
    onChunk: (text: string, mode?: 'append' | 'replace') => void,
    onStatus: (status: string) => void,
    options?: {
      threadId?: string;
      researchMode?: 'normal' | 'deep_research';
      thinking?: boolean;
      kbIds?: string[];
      useSearch?: boolean;
      deepRetrieval?: boolean;
      intentMode?: 'auto' | 'manual';
      draftId?: string;
      confirmationStatus?: 'pending' | 'confirmed' | 'revised' | 'cancelled';
      researchPlan?: ResearchPlanPayload;
      model?: string;
      token?: string;
      selectedSkills?: string[];
      endpoint?: '/api/v2/agent-runtime/query' | '/api/v2/guest/query';
      deviceId?: string;
      signal?: AbortSignal;
      fileIds?: string[];
      agentCode?: string;
      workflow?: string;
    },
    onEvent?: (event: AgentProcessEvent) => void,
    onThinking?: (text: string) => void,
    onDraftChunk?: (text: string) => void,
    onFollowUpSuggestions?: (
      suggestions: string[],
      metadata?: Record<string, unknown>,
    ) => void,
  ): Promise<{ backgroundTaskId?: string; interrupted?: boolean; aborted?: boolean }> {
    const agentCode = options?.agentCode || DEFAULT_AGENT_CODE;
    const payload: any = { question, agent_code: agentCode };
    if (options?.threadId) payload.thread_id = options.threadId;
    const researchMode = options?.researchMode ?? 'normal';
    const endpoint = options?.endpoint || '/api/v2/agent-runtime/query';
    const isGuestEndpoint = endpoint === '/api/v2/guest/query';
    const requestedWorkflow = String(options?.workflow || '').trim()
      || (researchMode === 'deep_research' ? 'deep_research' : '');
    if (!isGuestEndpoint && researchMode === 'deep_research') {
      payload.subagent_enabled = true;
    } else {
      // 默认普通问题只使用主智能体
      payload.subagent_enabled = false;
    }
    if (!isGuestEndpoint && requestedWorkflow) payload.workflow = requestedWorkflow;
    if (isGuestEndpoint) {
      // 访客仍禁用技能、子智能体和思考链；工作流来自 Profile 信息。
      payload.selected_skills = [];
      payload.thinking = false;
      if (requestedWorkflow) {
        payload.mode = requestedWorkflow.replace(/_/g, '-');
        payload.task_type = requestedWorkflow;
      }
    } else if (options?.selectedSkills) {
      payload.selected_skills = options.selectedSkills;
    }
    if (!isGuestEndpoint) {
      // 支持按需或默认开启思考模式，以便模型流式输出思维链。
      payload.thinking = options?.thinking !== false;
    }
    if (options?.kbIds !== undefined) payload.kb_ids = options.kbIds;
    if (options?.useSearch !== undefined) payload.use_search = options.useSearch;
    if (!isGuestEndpoint && options?.deepRetrieval !== undefined) {
      payload.deep_retrieval = options.deepRetrieval;
    }
    if (options?.model) payload.model = options.model;
    const metadata: Record<string, unknown> = {};
    if (options?.intentMode) metadata.intent_mode = options.intentMode;
    if (options?.draftId) metadata.draft_id = options.draftId;
    if (options?.confirmationStatus) metadata.confirmation_status = options.confirmationStatus;
    if (options?.researchPlan) metadata.research_plan = options.researchPlan;
    if (options?.fileIds && options.fileIds.length > 0) metadata.file_ids = options.fileIds;
    if (Object.keys(metadata).length > 0) payload.metadata = metadata;

    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const authToken = options?.token;
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }

    if (options?.deviceId) {
      headers['X-Device-ID'] = options.deviceId;
    }

    const res = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal: options?.signal,
    });

    if (!res.ok) {
      throw await buildAgentQueryError(res);
    }

    const reader = res.body?.getReader();
    if (!reader) throw new Error('No reader available');

    const decoder = new TextDecoder();
    let buffer = '';
    let backgroundTaskId: string | undefined;
    let doneReceived = false;
    let userAborted = false;

    while (true) {
      // 检查 AbortSignal
      if (options?.signal?.aborted) {
        userAborted = true;
        try { reader.cancel(); } catch { /* ignore */ }
        break;
      }

      let readResult: ReadableStreamReadResult<Uint8Array>;
      try {
        readResult = await reader.read();
      } catch (readErr: any) {
        if (readErr?.name === 'AbortError') {
          userAborted = true;
          break;
        }
        throw readErr;
      }

      const { done, value } = readResult;
      if (done) {
        // 连接关闭：如果没有收到显式的 [DONE]，标记为中断
        if (!doneReceived) {
          console.warn('[SSE] Stream closed without [DONE] marker — connection may have been interrupted');
        }
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.trim() && line.startsWith('data: ')) {
          const dataStr = line.slice(6).trim();
          if (dataStr === '[DONE]') {
            doneReceived = true;
            console.log('SSE Stream Finished');
            continue;
          }

          try {
            const data = JSON.parse(dataStr);
            const { type, payload } = normalizeRuntimeEvent(data);
            // console.log('SSE Received:', data);

            if (type === 'done') {
              doneReceived = true;
              continue;
            }

            if (type === 'reasoning.delta' || type === 'thinking') {
              if (onThinking) onThinking(payload.content || '');
              continue;
            }

            if (
              type === 'workflow.step' &&
              payload.event !== undefined &&
              onEvent
            ) {
              const evt = payload.event as AgentProcessEvent;
              onEvent(evt);
              if (evt.kind === 'background_task_started' && evt.metadata?.task_id) {
                backgroundTaskId = evt.metadata.task_id as string;
              }
              continue;
            }

            if (type === 'workflow.started') {
              onStatus(workflowStartedStatus(payload.workflow));
              continue;
            }

            const coordinationEvent = coordinationEventToProcessEvent(type, payload);
            if (coordinationEvent && onEvent) {
              onEvent(coordinationEvent);
              continue;
            }

            if (type === 'workflow.step' || type === 'status' || payload.status !== undefined) {
              onStatus(payload.content || payload.status || '');
              continue;
            }

            if (type === 'event' || payload.event !== undefined) {
              if (payload.event !== undefined && onEvent) {
                const evt = payload.event as AgentProcessEvent;
                onEvent(evt);
                // 检测后台任务启动事件
                if (evt.kind === 'background_task_started' && evt.metadata?.task_id) {
                  backgroundTaskId = evt.metadata.task_id as string;
                }
              }
              continue;
            }

            if (type === 'message.delta' || type === 'chunk') {
              onChunk(payload.content || '', 'append');
              continue;
            }

            if (type === 'message.replace') {
              onChunk(payload.content || '', 'replace');
              continue;
            }

            if (type === 'research.draft.delta' || type === 'draft_chunk') {
              if (onDraftChunk) onDraftChunk(payload.content || '');
              continue;
            }

            if (type === 'follow_up.created' || type === 'follow_up_suggestions') {
              const suggestions = Array.isArray(payload.suggestions)
                ? payload.suggestions.map((item: unknown) => String(item || '').trim()).filter(Boolean)
                : [];
              onFollowUpSuggestions?.(suggestions, payload.metadata);
              continue;
            }

            // Fallback: only treat as chunk when payload is content-only and has no other semantic fields.
            if (
              type === undefined &&
              typeof payload.content === 'string' &&
              payload.status === undefined &&
              payload.event === undefined &&
              payload.error === undefined
            ) {
              onChunk(payload.content);
              continue;
            }

            if (type === 'error' || payload.error !== undefined) {
              const message = payload.message || payload.error || 'Unknown error';
              console.error('Agent error:', message);
              onStatus(humanizeAgentStatus(`Error: ${message}`));
            }
          } catch (e) {
            console.error('Error parsing SSE data:', e, 'Raw:', dataStr);
          }
        }
      }
    }

    return { backgroundTaskId, interrupted: !doneReceived && !userAborted, aborted: userAborted };
  },

  // ── 后台任务 API ──────────────────────────────────────

  async getTaskStatus(taskId: string, threadId: string, agentCode = DEFAULT_AGENT_CODE): Promise<TaskStatusResponse> {
    const headers: Record<string, string> = {};
    const token = localStorage.getItem('auth_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const url = `/api/v2/agent-runtime/${encodeURIComponent(agentCode)}/task/${taskId}/status?thread_id=${encodeURIComponent(threadId)}`;
    const res = await fetch(url, { headers });
    if (!res.ok) throw new Error('Failed to get task status');
    return res.json();
  },

  async cancelTask(taskId: string, threadId: string, agentCode = DEFAULT_AGENT_CODE): Promise<{ status: string; task_status?: string }> {
    const headers: Record<string, string> = {};
    const token = localStorage.getItem('auth_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const url = `/api/v2/agent-runtime/${encodeURIComponent(agentCode)}/task/${taskId}/cancel?thread_id=${encodeURIComponent(threadId)}`;
    const res = await fetch(url, { method: 'POST', headers });
    if (!res.ok) throw new Error('Failed to cancel task');
    return res.json();
  },

  async getThreadActiveTask(threadId: string, agentCode = DEFAULT_AGENT_CODE): Promise<{ has_active_task: boolean; task: BackgroundTaskInfo | null }> {
    const headers: Record<string, string> = {};
    const token = localStorage.getItem('auth_token');
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(`/api/v2/agent-runtime/${encodeURIComponent(agentCode)}/thread/${encodeURIComponent(threadId)}/task`, { headers });
    if (!res.ok) throw new Error('Failed to get thread task');
    return res.json();
  },

  /**
   * 订阅后台任务事件流（SSE），支持自动重连。
   * 当连接中断（未收到 [DONE]）时，会自动重试最多 MAX_RETRIES 次，
   * 每次重试间隔递增（1s, 2s, 4s）。
   * 返回一个 abort 函数用于取消订阅。
   */
  subscribeTaskStream(
    taskId: string,
    threadId: string,
    agentCode: string,
    onChunk: (text: string, mode?: 'append' | 'replace') => void,
    onStatus: (status: string) => void,
    onEvent: (event: AgentProcessEvent) => void,
    onDone: () => void,
    onError: (error: string) => void,
    onThinking?: (text: string) => void,
    onFollowUpSuggestions?: (
      suggestions: string[],
      metadata?: Record<string, unknown>,
    ) => void,
  ): () => void {
    const MAX_RETRIES = 3;
    const BASE_RETRY_DELAY_MS = 1000;
    let aborted = false;
    let retryCount = 0;
    const abortController = new AbortController();
    let activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    const waitForRetry = (delay: number) => new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, delay);
      abortController.signal.addEventListener('abort', () => {
        window.clearTimeout(timer);
        reject(new DOMException('Aborted', 'AbortError'));
      }, { once: true });
    });
    const cursorKey = `agent_task_cursor:${agentCode}:${taskId}`;
    let lastEventId = Number(sessionStorage.getItem(cursorKey) ?? -1);
    if (!Number.isFinite(lastEventId)) lastEventId = -1;

    const attemptConnect = async (): Promise<void> => {
      const headers: Record<string, string> = {};
      const token = localStorage.getItem('auth_token');
      if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }

      const cursor = lastEventId >= 0 ? `&after_event_id=${lastEventId}` : '';
      const url = `/api/v2/agent-runtime/${encodeURIComponent(agentCode)}/task/${taskId}/stream?thread_id=${encodeURIComponent(threadId)}${cursor}`;
      const res = await fetch(url, { headers, signal: abortController.signal });

      if (!res.ok) {
        // 对于可重试的 HTTP 状态码（5xx, 429），尝试重连
        if (res.status >= 500 || res.status === 429) {
          if (retryCount < MAX_RETRIES && !aborted) {
            retryCount++;
            const delay = BASE_RETRY_DELAY_MS * Math.pow(2, retryCount - 1);
            console.warn(`[TaskSSE] HTTP ${res.status}, retrying in ${delay}ms (attempt ${retryCount}/${MAX_RETRIES})`);
            onStatus(`连接中断，正在重连... (${retryCount}/${MAX_RETRIES})`);
            await waitForRetry(delay);
            return attemptConnect();
          }
        }
        onError(`订阅失败: ${res.status} ${res.statusText}`);
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) {
        onError('No reader available');
        return;
      }
      activeReader = reader;

      const decoder = new TextDecoder();
      let buffer = '';
      let doneReceived = false;

      while (!aborted) {
        const { done, value } = await reader.read();
        if (done) {
          if (!doneReceived && !aborted) {
            // 连接中断但没有收到 [DONE]：尝试重连
            if (retryCount < MAX_RETRIES) {
              retryCount++;
              const delay = BASE_RETRY_DELAY_MS * Math.pow(2, retryCount - 1);
              console.warn(`[TaskSSE] Connection lost without [DONE], retrying in ${delay}ms (attempt ${retryCount}/${MAX_RETRIES})`);
              onStatus(`连接中断，正在重连... (${retryCount}/${MAX_RETRIES})`);
              await waitForRetry(delay);
              return attemptConnect();
            }
            // 重连次数耗尽，通知上层连接中断（上层会尝试自动恢复）
            console.warn('[TaskSSE] Max retries exhausted, notifying upper layer for auto-recovery');
            onError('连接中断，自动重连失败');
          }
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.trim() && line.startsWith('data: ')) {
            const dataStr = line.slice(6).trim();
            if (dataStr === '[DONE]') {
              if (!doneReceived) {
                doneReceived = true;
                onDone();
              }
              continue;
            }

            try {
              const data = JSON.parse(dataStr);
              const { type, payload } = normalizeRuntimeEvent(data);
              const eventCursor = Number(payload?._task_event_id);
              if (Number.isInteger(eventCursor) && eventCursor > lastEventId) {
                lastEventId = eventCursor;
                sessionStorage.setItem(cursorKey, String(lastEventId));
              }
              if (type === 'done') {
                if (!doneReceived) {
                  doneReceived = true;
                  sessionStorage.removeItem(cursorKey);
                  onDone();
                }
              } else if (type === 'message.delta' || type === 'chunk') {
                onChunk(payload.content || '', 'append');
              } else if (type === 'message.replace') {
                onChunk(payload.content || '', 'replace');
              } else if (type === 'reasoning.delta' || type === 'thinking') {
                if (onThinking) onThinking(payload.content || '');
              } else if (type === 'workflow.started') {
                onStatus(workflowStartedStatus(payload.workflow));
              } else if (type === 'workflow.step' && payload.event !== undefined) {
                onEvent(payload.event as AgentProcessEvent);
              } else if (coordinationEventToProcessEvent(type, payload)) {
                onEvent(coordinationEventToProcessEvent(type, payload) as AgentProcessEvent);
              } else if (type === 'workflow.step' || type === 'status') {
                onStatus(payload.status || payload.content || '');
              } else if (type === 'event') {
                onEvent(payload.event as AgentProcessEvent);
              } else if (type === 'follow_up.created' || type === 'follow_up_suggestions') {
                const suggestions = Array.isArray(payload.suggestions)
                  ? payload.suggestions.map((item: unknown) => String(item || '').trim()).filter(Boolean)
                  : [];
                onFollowUpSuggestions?.(suggestions, payload.metadata);
              } else if (type === 'error') {
                onError(payload.message || payload.error || 'Unknown error');
              }
            } catch (e) {
              console.error('Error parsing task SSE:', e);
            }
          }
        }
      }
    };

    (async () => {
      try {
        await attemptConnect();
      } catch (e: any) {
        if (!aborted) {
          onError(e.message || 'Stream error');
        }
      }
    })();

    return () => {
      aborted = true;
      abortController.abort();
      if (activeReader) void activeReader.cancel().catch(() => undefined);
    };
  },
};
