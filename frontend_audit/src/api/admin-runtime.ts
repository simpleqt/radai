import { request } from '@/utils/request';

export interface AgentRunCounts {
  events: number;
  units: number;
  tool_calls: number;
  context_packs: number;
  compactions: number;
  compaction_attempts: number;
  compaction_skipped: number;
  handoffs: number;
}

export interface AgentRunSummary {
  run_id: string;
  request_id?: string | null;
  user_id?: string | null;
  thread_id?: string | null;
  agent_code?: string | null;
  workflow?: string | null;
  model_name?: string | null;
  selected_skills?: string[];
  tool_policy?: string | null;
  status?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  total_tokens_estimated?: number;
  total_tool_calls?: number;
  error_code?: string | null;
  record_count: number;
  counts: AgentRunCounts;
}

export interface AgentRunListResponse {
  items: AgentRunSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface RegisteredToolRuntimePolicy {
  name: string;
  provider: string;
  description: string;
  domains: string[];
  workflows: string[];
  required_skills: string[];
  risk_level: 'safe' | 'network' | 'write';
  enabled_by_default: boolean;
  side_effect: string;
  timeout_seconds: number | null;
  idempotent: boolean;
  parallel_safe: boolean;
  max_concurrency: number | null;
  max_attempts: number;
  retryable_errors: string[];
  max_output_chars: number | null;
  cache_enabled: boolean;
  cache_ttl_seconds: number | null;
}

export interface ToolRuntimeHealth {
  status: 'ready' | 'degraded' | 'unavailable' | 'unknown' | string;
  reason: string;
  detail: string;
  observed_at: string | null;
}

export interface ToolRuntimeMetrics {
  calls: number;
  successes: number;
  errors: number;
  timeouts: number;
  retries: number;
  last_error_code: string;
  average_elapsed_ms: number;
  average_queue_ms: number;
}

export interface ToolRuntimeObservation {
  name: string;
  health: ToolRuntimeHealth;
  metrics: ToolRuntimeMetrics;
}

export interface ToolRuntimeStateResponse {
  scope: 'process' | string;
  generated_at: string;
  tools: ToolRuntimeObservation[];
  registered_tools: RegisteredToolRuntimePolicy[];
}

const qs = (params: Record<string, string | number | undefined>) => {
  const values = Object.entries(params).filter(([, value]) => value !== undefined && value !== '');
  const query = new URLSearchParams(values as Array<[string, string]>).toString();
  return query ? `?${query}` : '';
};

const base = '/api/v2/agent-runtime';

export const AdminRuntimeAPI = {
  list: (params: {
    limit?: number;
    offset?: number;
    status?: string;
    agent_code?: string;
    user_id?: string;
    q?: string;
  } = {}) => request(`${base}/admin/runs${qs(params)}`) as Promise<AgentRunListResponse>,

  toolRuntime: () =>
    request(`${base}/admin/tool-runtime`) as Promise<ToolRuntimeStateResponse>,

  detail: (runId: string) => request(`${base}/runs/${encodeURIComponent(runId)}`) as Promise<{
    run_id: string;
    run: AgentRunSummary | null;
    record_count: number;
    counts: AgentRunCounts;
  }>,

  events: (runId: string) => request(`${base}/runs/${encodeURIComponent(runId)}/events`) as Promise<{ events: Record<string, any>[] }>,
  units: (runId: string) => request(`${base}/runs/${encodeURIComponent(runId)}/units`) as Promise<{ units: Record<string, any>[] }>,
  toolCalls: (runId: string) => request(`${base}/runs/${encodeURIComponent(runId)}/tool-calls`) as Promise<{ tool_calls: Record<string, any>[] }>,
  contextPacks: (runId: string) => request(`${base}/runs/${encodeURIComponent(runId)}/context-packs`) as Promise<{ context_packs: Record<string, any>[] }>,
  compactions: (runId: string) => request(`${base}/runs/${encodeURIComponent(runId)}/compactions`) as Promise<{ compactions: Record<string, any>[] }>,
  handoffs: (runId: string) => request(`${base}/runs/${encodeURIComponent(runId)}/handoffs`) as Promise<{ handoffs: Record<string, any>[] }>,

  replay: (runId: string, mode: 'dry-run' | 'tool-cache' | 'full' = 'dry-run') =>
    request(`${base}/runs/${encodeURIComponent(runId)}/replay?mode=${encodeURIComponent(mode)}`, { method: 'POST' }) as Promise<Record<string, any>>,
};
