<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleSlash2,
  Eye,
  Filter,
  Loader2,
  Play,
  RefreshCw,
  Search,
  Timer,
  Wrench,
  X,
} from 'lucide-vue-next';
import {
  AdminRuntimeAPI,
  type AgentRunSummary,
  type RegisteredToolRuntimePolicy,
  type ToolRuntimeObservation,
  type ToolRuntimeStateResponse,
} from '@/api/admin-runtime';

type DetailTab = 'overview' | 'events' | 'units' | 'tool_calls' | 'context_packs' | 'compactions' | 'handoffs';
type RuntimeView = 'runs' | 'tools';
type ToolRuntimeRow = RegisteredToolRuntimePolicy & ToolRuntimeObservation;

const pageSize = 20;
const activeView = ref<RuntimeView>('runs');
const page = ref(1);
const total = ref(0);
const items = ref<AgentRunSummary[]>([]);
const loading = ref(false);
const detailLoading = ref(false);
const actionKey = ref('');
const error = ref('');
const notice = ref('');
const statusFilter = ref('');
const agentFilter = ref('');
const userFilter = ref('');
const query = ref('');
const selected = ref<AgentRunSummary | null>(null);
const selectedTab = ref<DetailTab>('overview');
const detailRecords = ref<Partial<Record<Exclude<DetailTab, 'overview'>, Record<string, any>[]>>>({});
const replayResult = ref<Record<string, any> | null>(null);
const toolRuntime = ref<ToolRuntimeStateResponse | null>(null);
const toolLoading = ref(false);
const toolQuery = ref('');
const toolHealthFilter = ref('');

const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'running', label: '运行中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
];

const tabs: Array<{ key: DetailTab; label: string }> = [
  { key: 'overview', label: '运行摘要' },
  { key: 'events', label: '事件' },
  { key: 'units', label: '执行单元' },
  { key: 'tool_calls', label: '工具调用' },
  { key: 'context_packs', label: '上下文包' },
  { key: 'compactions', label: '压缩' },
  { key: 'handoffs', label: '交接' },
];

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));
const runningCount = computed(() => items.value.filter((item) => item.status === 'running').length);
const failedCount = computed(() => items.value.filter((item) => ['failed', 'error'].includes(String(item.status || '').toLowerCase())).length);
const toolCallCount = computed(() => items.value.reduce((sum, item) => sum + (item.counts?.tool_calls || 0), 0));
const emptyToolMetrics = {
  calls: 0,
  successes: 0,
  errors: 0,
  timeouts: 0,
  retries: 0,
  last_error_code: '',
  average_elapsed_ms: 0,
  average_queue_ms: 0,
};
const toolRows = computed<ToolRuntimeRow[]>(() => {
  const observations = new Map((toolRuntime.value?.tools || []).map((item) => [item.name, item]));
  const rows = (toolRuntime.value?.registered_tools || []).map((tool) => {
    const observed = observations.get(tool.name);
    observations.delete(tool.name);
    return {
      ...tool,
      health: observed?.health || { status: 'unknown', reason: 'not_observed', detail: '', observed_at: null },
      metrics: observed?.metrics || { ...emptyToolMetrics },
    };
  });
  for (const observed of observations.values()) {
    rows.push({
      provider: '',
      description: '当前 worker 动态注入的运行时工具',
      domains: [],
      workflows: [],
      required_skills: [],
      risk_level: 'safe',
      enabled_by_default: false,
      side_effect: 'unknown',
      timeout_seconds: null,
      idempotent: true,
      parallel_safe: true,
      max_concurrency: null,
      max_attempts: 1,
      retryable_errors: [],
      max_output_chars: null,
      cache_enabled: false,
      cache_ttl_seconds: null,
      ...observed,
    });
  }
  return rows.sort((left, right) => left.name.localeCompare(right.name));
});
const filteredToolRows = computed(() => {
  const queryValue = toolQuery.value.trim().toLowerCase();
  return toolRows.value.filter((tool) => {
    if (toolHealthFilter.value && tool.health.status !== toolHealthFilter.value) return false;
    if (!queryValue) return true;
    return [tool.name, tool.description, tool.provider, tool.side_effect, tool.risk_level]
      .some((value) => String(value || '').toLowerCase().includes(queryValue));
  });
});
const toolSummary = computed(() => toolRows.value.reduce((summary, tool) => {
  summary.calls += tool.metrics.calls || 0;
  summary.errors += tool.metrics.errors || 0;
  if (tool.health.status === 'ready') summary.ready += 1;
  if (['degraded', 'unavailable'].includes(tool.health.status)) summary.unhealthy += 1;
  return summary;
}, { calls: 0, errors: 0, ready: 0, unhealthy: 0 }));

const formatDate = (value?: string | null) =>
  value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-';

const statusLabel = (value?: string | null) => ({
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  error: '错误',
  cancelled: '已取消',
}[String(value || '').toLowerCase()] || value || '未知');

const statusClass = (value?: string | null) => {
  const normalized = String(value || '').toLowerCase();
  if (normalized === 'running') return 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300';
  if (normalized === 'completed') return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300';
  if (['failed', 'error', 'cancelled'].includes(normalized)) return 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300';
  return 'bg-surface-sunken text-text-secondary';
};

const healthLabel = (value?: string | null) => ({
  ready: '正常',
  degraded: '降级',
  unavailable: '不可用',
  unknown: '未观测',
}[String(value || '').toLowerCase()] || value || '未观测');

const healthClass = (value?: string | null) => {
  const normalized = String(value || '').toLowerCase();
  if (normalized === 'ready') return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300';
  if (normalized === 'degraded') return 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300';
  if (normalized === 'unavailable') return 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300';
  return 'bg-surface-sunken text-text-secondary';
};

const formatDuration = (value?: number | null) => {
  const milliseconds = Number(value || 0);
  if (milliseconds >= 1000) return `${(milliseconds / 1000).toFixed(2)} s`;
  return `${milliseconds.toFixed(milliseconds >= 10 ? 0 : 1)} ms`;
};

const successRate = (tool: ToolRuntimeRow) => {
  if (!tool.metrics.calls) return '-';
  return `${((tool.metrics.successes / tool.metrics.calls) * 100).toFixed(1)}%`;
};

const jsonPreview = (value: unknown) => {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? '');
  }
};

const countFor = (key: string) => {
  if (!selected.value?.counts) return 0;
  return Number((selected.value.counts as Record<string, number>)[key] || 0);
};

const showNotice = (message: string) => {
  notice.value = message;
  window.setTimeout(() => {
    notice.value = '';
  }, 2600);
};

const loadRuns = async () => {
  loading.value = true;
  error.value = '';
  try {
    const result = await AdminRuntimeAPI.list({
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
      status: statusFilter.value,
      agent_code: agentFilter.value.trim(),
      user_id: userFilter.value.trim(),
      q: query.value.trim(),
    });
    items.value = result.items || [];
    total.value = result.total || 0;
    if (page.value > totalPages.value) page.value = totalPages.value;
  } catch (reason: any) {
    error.value = reason?.message || '运行记录加载失败';
  } finally {
    loading.value = false;
  }
};

const loadToolRuntime = async () => {
  toolLoading.value = true;
  error.value = '';
  try {
    toolRuntime.value = await AdminRuntimeAPI.toolRuntime();
  } catch (reason: any) {
    error.value = reason?.message || '工具运行状态加载失败';
  } finally {
    toolLoading.value = false;
  }
};

const selectRuntimeView = (view: RuntimeView) => {
  activeView.value = view;
  if (view === 'tools' && !toolRuntime.value) void loadToolRuntime();
};

const refreshCurrent = () => {
  if (activeView.value === 'tools') void loadToolRuntime();
  else void loadRuns();
};

const applyFilters = () => {
  page.value = 1;
  void loadRuns();
};

const setPage = (next: number) => {
  page.value = Math.min(Math.max(1, next), totalPages.value);
  void loadRuns();
};

const closeDetail = () => {
  selected.value = null;
  detailRecords.value = {};
  replayResult.value = null;
};

const openDetail = async (run: AgentRunSummary) => {
  selected.value = run;
  selectedTab.value = 'overview';
  detailRecords.value = {};
  replayResult.value = null;
  detailLoading.value = true;
  try {
    const result = await AdminRuntimeAPI.detail(run.run_id);
    if (result.run) selected.value = { ...run, ...result.run, counts: result.counts || run.counts };
  } catch (reason: any) {
    error.value = reason?.message || '运行详情加载失败';
  } finally {
    detailLoading.value = false;
  }
};

const loadTab = async (tab: DetailTab) => {
  if (!selected.value || tab === 'overview' || detailRecords.value[tab]) return;
  detailLoading.value = true;
  try {
    const runId = selected.value.run_id;
    let result: Record<string, any[]>;
    if (tab === 'events') result = await AdminRuntimeAPI.events(runId);
    else if (tab === 'units') result = await AdminRuntimeAPI.units(runId);
    else if (tab === 'tool_calls') result = await AdminRuntimeAPI.toolCalls(runId);
    else if (tab === 'context_packs') result = await AdminRuntimeAPI.contextPacks(runId);
    else if (tab === 'compactions') result = await AdminRuntimeAPI.compactions(runId);
    else result = await AdminRuntimeAPI.handoffs(runId);
    const key = Object.keys(result).find((item) => item !== 'run_id') as Exclude<DetailTab, 'overview'> | undefined;
    detailRecords.value = { ...detailRecords.value, [tab]: key ? result[key] : [] };
  } catch (reason: any) {
    error.value = reason?.message || '运行明细加载失败';
  } finally {
    detailLoading.value = false;
  }
};

const selectTab = (tab: DetailTab) => {
  selectedTab.value = tab;
  void loadTab(tab);
};

const replay = async () => {
  if (!selected.value || !confirm('将以 dry-run 模式重放此运行记录，是否继续？')) return;
  actionKey.value = `replay:${selected.value.run_id}`;
  error.value = '';
  try {
    replayResult.value = await AdminRuntimeAPI.replay(selected.value.run_id, 'dry-run');
    showNotice('重放检查已完成');
  } catch (reason: any) {
    error.value = reason?.message || '重放失败';
  } finally {
    actionKey.value = '';
  }
};

onMounted(loadRuns);
</script>

<template>
  <div class="max-w-[1600px] mx-auto space-y-6">
    <div class="flex flex-col lg:flex-row lg:items-end justify-between gap-4 border-b border-border-subtle pb-5">
      <div>
        <div class="flex items-center gap-2 text-accent text-xs font-bold mb-2">
          <Activity class="w-4 h-4" />
          <span>Agent Runtime</span>
        </div>
        <h2 class="text-3xl font-extrabold tracking-tight text-text-primary">运行监控</h2>
        <p class="text-text-secondary mt-2 text-sm">查看智能体运行状态、工具调用、上下文压缩和可回放记录。</p>
      </div>
      <button
        type="button"
        class="h-10 px-4 rounded-2xl border border-border-subtle bg-surface/70 text-xs font-bold flex items-center gap-2 hover:bg-surface-hover transition"
        :disabled="loading || toolLoading"
        @click="refreshCurrent"
      >
        <RefreshCw class="w-4 h-4" :class="loading || toolLoading ? 'animate-spin' : ''" />
        刷新
      </button>
    </div>

    <div class="inline-flex rounded-2xl border border-border-subtle bg-surface/80 p-1" role="tablist" aria-label="运行监控视图">
      <button
        type="button"
        role="tab"
        class="px-4 py-2 rounded-xl text-xs font-bold inline-flex items-center gap-2 transition"
        :class="activeView === 'runs' ? 'bg-accent text-white shadow-sm' : 'text-text-secondary hover:bg-surface-hover'"
        :aria-selected="activeView === 'runs'"
        @click="selectRuntimeView('runs')"
      >
        <Activity class="w-4 h-4" />运行记录
      </button>
      <button
        type="button"
        role="tab"
        class="px-4 py-2 rounded-xl text-xs font-bold inline-flex items-center gap-2 transition"
        :class="activeView === 'tools' ? 'bg-accent text-white shadow-sm' : 'text-text-secondary hover:bg-surface-hover'"
        :aria-selected="activeView === 'tools'"
        @click="selectRuntimeView('tools')"
      >
        <Wrench class="w-4 h-4" />工具运行状态
      </button>
    </div>

    <div v-if="notice" class="fixed top-6 right-6 z-[80] px-5 py-3 bg-emerald-600 text-white rounded-xl shadow-sm text-sm font-semibold" role="status" aria-live="polite">{{ notice }}</div>
    <div v-if="error" class="rounded-2xl border border-red-200 bg-red-50 dark:bg-red-950/25 dark:border-red-800 px-5 py-4 flex items-start gap-3 text-red-700 dark:text-red-300" role="alert">
      <AlertTriangle class="w-5 h-5 mt-0.5 shrink-0" />
      <span class="text-sm flex-1">{{ error }}</span>
      <button type="button" class="p-1" aria-label="关闭错误提示" @click="error = ''"><X class="w-4 h-4" /></button>
    </div>

    <template v-if="activeView === 'runs'">
    <div class="grid grid-cols-2 xl:grid-cols-4 gap-4">
      <div class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
        <p class="text-xs text-text-secondary">筛选结果</p>
        <p class="text-2xl font-extrabold mt-1">{{ total }}</p>
      </div>
      <div class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
        <p class="text-xs text-text-secondary">本页运行中</p>
        <p class="text-2xl font-extrabold mt-1 text-amber-600 dark:text-amber-300">{{ runningCount }}</p>
      </div>
      <div class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
        <p class="text-xs text-text-secondary">本页失败</p>
        <p class="text-2xl font-extrabold mt-1 text-red-600 dark:text-red-300">{{ failedCount }}</p>
      </div>
      <div class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
        <p class="text-xs text-text-secondary">本页工具调用</p>
        <p class="text-2xl font-extrabold mt-1 text-accent">{{ toolCallCount }}</p>
      </div>
    </div>

    <section class="rounded-3xl border border-border-subtle bg-surface/80 p-4">
      <div class="flex items-center gap-2 text-xs font-bold text-text-secondary mb-3"><Filter class="w-4 h-4" />筛选</div>
      <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-[1.5fr_1fr_1fr_1fr_auto] gap-3">
        <label class="relative">
          <span class="sr-only">搜索运行记录</span>
          <Search class="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-text-tertiary" />
          <input v-model="query" class="w-full pl-10 pr-4 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle outline-none text-xs focus:ring-2 focus:ring-accent/20" placeholder="运行 ID、用户、线程、模型..." @keyup.enter="applyFilters" />
        </label>
        <select v-model="statusFilter" class="px-4 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle outline-none text-xs">
          <option v-for="option in statusOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
        <input v-model="agentFilter" class="px-4 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle outline-none text-xs" placeholder="Agent code" @keyup.enter="applyFilters" />
        <input v-model="userFilter" class="px-4 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle outline-none text-xs" placeholder="用户 ID" @keyup.enter="applyFilters" />
        <button type="button" class="px-5 py-2.5 rounded-2xl bg-accent hover:bg-accent-hover text-white text-xs font-bold transition" @click="applyFilters">应用</button>
      </div>
    </section>

    <section class="rounded-3xl border border-border-subtle bg-surface/80 overflow-hidden">
      <div v-if="loading" class="py-16 flex items-center justify-center text-text-tertiary"><Loader2 class="w-5 h-5 animate-spin mr-2" />加载运行记录...</div>
      <div v-else-if="items.length === 0" class="py-16 text-center text-sm text-text-secondary">暂无匹配的运行记录</div>
      <div v-else class="overflow-x-auto">
        <table class="w-full min-w-[980px] text-left text-xs">
          <thead class="bg-surface-sunken text-text-secondary">
            <tr>
              <th class="px-5 py-3 font-bold">运行 / 时间</th>
              <th class="px-5 py-3 font-bold">用户 / Agent</th>
              <th class="px-5 py-3 font-bold">工作流 / 模型</th>
              <th class="px-5 py-3 font-bold">状态</th>
              <th class="px-5 py-3 font-bold">运行指标</th>
              <th class="px-5 py-3 font-bold text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-border-subtle">
            <tr v-for="run in items" :key="run.run_id" class="hover:bg-accent-soft transition-colors">
              <td class="px-5 py-4">
                <button type="button" class="text-left group" @click="openDetail(run)">
                  <span class="font-bold text-accent group-hover:underline break-all">{{ run.run_id }}</span>
                  <span class="block mt-1 text-text-secondary">{{ formatDate(run.started_at) }}</span>
                </button>
              </td>
              <td class="px-5 py-4">
                <span class="block font-semibold text-text-secondary">{{ run.user_id || 'guest' }}</span>
                <span class="block mt-1 text-text-secondary">{{ run.agent_code || '-' }}</span>
              </td>
              <td class="px-5 py-4">
                <span class="block text-text-secondary">{{ run.workflow || '-' }}</span>
                <span class="block mt-1 text-text-secondary truncate max-w-[220px]" :title="run.model_name || ''">{{ run.model_name || '-' }}</span>
              </td>
              <td class="px-5 py-4"><span class="px-2.5 py-1 rounded-full font-bold" :class="statusClass(run.status)">{{ statusLabel(run.status) }}</span></td>
              <td class="px-5 py-4 text-text-secondary">
                <span class="block">事件 {{ run.counts?.events || 0 }} · 工具 {{ run.counts?.tool_calls || 0 }}</span>
                <span class="block mt-1">上下文包 {{ run.counts?.context_packs || 0 }} · 压缩 {{ run.counts?.compactions || 0 }}</span>
              </td>
              <td class="px-5 py-4 text-right">
                <button type="button" class="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl border border-border-subtle hover:bg-surface-hover font-bold" @click="openDetail(run)"><Eye class="w-3.5 h-3.5" />详情</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="items.length" class="px-5 py-4 border-t border-border-subtle flex items-center justify-between text-xs text-text-secondary">
        <span>第 {{ page }} / {{ totalPages }} 页，共 {{ total }} 条</span>
        <div class="flex items-center gap-2">
          <button type="button" class="p-2 rounded-lg border border-border-subtle disabled:opacity-40" :disabled="page <= 1" aria-label="上一页" @click="setPage(page - 1)"><ChevronLeft class="w-4 h-4" /></button>
          <button type="button" class="p-2 rounded-lg border border-border-subtle disabled:opacity-40" :disabled="page >= totalPages" aria-label="下一页" @click="setPage(page + 1)"><ChevronRight class="w-4 h-4" /></button>
        </div>
      </div>
    </section>

    <Transition name="sheet">
    <div v-if="selected" class="fixed inset-0 z-[70] flex items-end justify-center p-0 sm:items-center sm:p-5">
      <button type="button" class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-label="关闭运行详情" @click="closeDetail"></button>
      <aside class="sheet-panel relative h-[94dvh] w-full max-w-5xl overflow-y-auto rounded-t-[1.75rem] border border-white/55 bg-surface/90 p-5 shadow-2xl backdrop-blur-2xl dark:border-white/10 dark:bg-surface/95 sm:h-[min(88vh,900px)] sm:rounded-[1.75rem] sm:p-7" role="dialog" aria-modal="true" aria-label="运行详情">
        <div class="flex items-start justify-between gap-4 border-b border-border-subtle pb-4">
          <div class="min-w-0">
            <p class="text-xs text-accent font-bold">运行详情</p>
            <h3 class="text-lg font-extrabold mt-1 break-all">{{ selected.run_id }}</h3>
            <p class="text-xs text-text-secondary mt-1">{{ selected.agent_code || '-' }} · {{ selected.workflow || '-' }}</p>
          </div>
          <button type="button" class="p-2 rounded-xl hover:bg-surface-hover" aria-label="关闭运行详情" @click="closeDetail"><X class="w-5 h-5" /></button>
        </div>

        <div class="flex flex-wrap gap-2 py-4 border-b border-border-subtle">
          <button v-for="tab in tabs" :key="tab.key" type="button" class="px-3 py-2 rounded-xl text-xs font-bold transition" :class="selectedTab === tab.key ? 'bg-accent text-white' : 'bg-surface-sunken text-text-secondary'" @click="selectTab(tab.key)">{{ tab.label }}</button>
        </div>

        <div v-if="detailLoading" class="py-10 flex justify-center text-text-tertiary"><Loader2 class="w-5 h-5 animate-spin" /></div>
        <div v-else-if="selectedTab === 'overview'" class="space-y-5 pt-5">
          <div class="grid grid-cols-2 gap-3">
            <div class="rounded-2xl bg-surface-sunken p-3"><p class="text-[11px] text-text-secondary">状态</p><p class="mt-1 font-bold" :class="statusClass(selected.status).replace(/bg-[^ ]+\s?/, '')">{{ statusLabel(selected.status) }}</p></div>
            <div class="rounded-2xl bg-surface-sunken p-3"><p class="text-[11px] text-text-secondary">用户</p><p class="mt-1 font-bold break-all">{{ selected.user_id || 'guest' }}</p></div>
            <div class="rounded-2xl bg-surface-sunken p-3"><p class="text-[11px] text-text-secondary">开始时间</p><p class="mt-1 font-bold">{{ formatDate(selected.started_at) }}</p></div>
            <div class="rounded-2xl bg-surface-sunken p-3"><p class="text-[11px] text-text-secondary">结束时间</p><p class="mt-1 font-bold">{{ formatDate(selected.ended_at) }}</p></div>
          </div>
          <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-3 text-xs">
            <div><dt class="text-text-secondary">请求 ID</dt><dd class="mt-1 font-mono break-all">{{ selected.request_id || '-' }}</dd></div>
            <div><dt class="text-text-secondary">线程 ID</dt><dd class="mt-1 font-mono break-all">{{ selected.thread_id || '-' }}</dd></div>
            <div><dt class="text-text-secondary">模型</dt><dd class="mt-1">{{ selected.model_name || '-' }}</dd></div>
            <div><dt class="text-text-secondary">错误码</dt><dd class="mt-1 text-red-600 dark:text-red-300">{{ selected.error_code || '-' }}</dd></div>
          </dl>
          <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div v-for="key in ['events', 'units', 'tool_calls', 'context_packs']" :key="key" class="rounded-2xl border border-border-subtle p-3"><p class="text-[11px] text-text-secondary">{{ key }}</p><p class="mt-1 text-xl font-extrabold">{{ countFor(key) }}</p></div>
          </div>
          <div class="flex items-center justify-between gap-3 pt-2">
            <button type="button" class="px-4 py-2.5 rounded-xl bg-accent hover:bg-accent-hover text-white text-xs font-bold inline-flex items-center gap-2 disabled:opacity-50" :disabled="!!actionKey" @click="replay"><Loader2 v-if="actionKey" class="w-4 h-4 animate-spin" /><Play v-else class="w-4 h-4" />dry-run 重放</button>
            <span class="text-[11px] text-text-secondary">只读取已持久化记录，不会再次调用生产工具。</span>
          </div>
          <pre v-if="replayResult" class="max-h-64 overflow-auto rounded-2xl bg-surface text-text-tertiary p-4 text-[11px]">{{ jsonPreview(replayResult) }}</pre>
        </div>
        <div v-else class="pt-5">
          <pre class="max-h-[65vh] overflow-auto rounded-2xl bg-surface text-text-tertiary p-4 text-[11px]">{{ jsonPreview(detailRecords[selectedTab] || []) }}</pre>
        </div>
      </aside>
    </div>
    </Transition>
    </template>

    <template v-else>
      <div class="grid grid-cols-2 xl:grid-cols-4 gap-4">
        <div class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
          <p class="text-xs text-text-secondary">已注册工具</p>
          <p class="text-2xl font-extrabold mt-1">{{ toolRows.length }}</p>
        </div>
        <div class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
          <p class="text-xs text-text-secondary">当前正常</p>
          <p class="text-2xl font-extrabold mt-1 text-emerald-600 dark:text-emerald-300">{{ toolSummary.ready }}</p>
        </div>
        <div class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
          <p class="text-xs text-text-secondary">降级 / 不可用</p>
          <p class="text-2xl font-extrabold mt-1" :class="toolSummary.unhealthy ? 'text-red-600 dark:text-red-300' : 'text-text-secondary'">{{ toolSummary.unhealthy }}</p>
        </div>
        <div class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
          <p class="text-xs text-text-secondary">调用 / 错误</p>
          <p class="text-2xl font-extrabold mt-1 text-accent">{{ toolSummary.calls }} <span class="text-sm text-text-tertiary">/ {{ toolSummary.errors }}</span></p>
        </div>
      </div>

      <section class="rounded-3xl border border-border-subtle bg-surface/80 p-4">
        <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-3">
          <div class="flex-1 grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_180px] gap-3">
            <label class="relative">
              <span class="sr-only">搜索工具</span>
              <Search class="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-text-tertiary" />
              <input v-model="toolQuery" class="w-full pl-10 pr-4 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle outline-none text-xs focus:ring-2 focus:ring-accent/20" placeholder="工具名、Provider、副作用..." />
            </label>
            <select v-model="toolHealthFilter" class="px-4 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle outline-none text-xs">
              <option value="">全部健康状态</option>
              <option value="ready">正常</option>
              <option value="degraded">降级</option>
              <option value="unavailable">不可用</option>
              <option value="unknown">未观测</option>
            </select>
          </div>
          <p class="text-[11px] text-text-secondary">
            作用域：当前 worker · 快照 {{ formatDate(toolRuntime?.generated_at) }}
          </p>
        </div>
      </section>

      <section class="rounded-3xl border border-border-subtle bg-surface/80 overflow-hidden">
        <div v-if="toolLoading" class="py-16 flex items-center justify-center text-text-tertiary"><Loader2 class="w-5 h-5 animate-spin mr-2" />加载工具运行状态...</div>
        <div v-else-if="filteredToolRows.length === 0" class="py-16 text-center text-sm text-text-secondary">暂无匹配的工具状态</div>
        <div v-else class="overflow-x-auto">
          <table class="w-full min-w-[1180px] text-left text-xs">
            <thead class="bg-surface-sunken text-text-secondary">
              <tr>
                <th class="px-5 py-3 font-bold">工具</th>
                <th class="px-5 py-3 font-bold">健康状态</th>
                <th class="px-5 py-3 font-bold">运行策略</th>
                <th class="px-5 py-3 font-bold">调用质量</th>
                <th class="px-5 py-3 font-bold">延迟</th>
                <th class="px-5 py-3 font-bold">缓存 / 输出</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="tool in filteredToolRows" :key="tool.name" class="hover:bg-accent-soft transition-colors align-top">
                <td class="px-5 py-4 max-w-[320px]">
                  <span class="font-bold text-text-primary break-all">{{ tool.name }}</span>
                  <span class="block mt-1 text-text-secondary line-clamp-2" :title="tool.description">{{ tool.description || '-' }}</span>
                  <span class="block mt-1 text-[10px] text-text-tertiary break-all" :title="tool.provider">{{ tool.provider || '动态工具' }}</span>
                </td>
                <td class="px-5 py-4">
                  <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full font-bold" :class="healthClass(tool.health.status)">
                    <CheckCircle2 v-if="tool.health.status === 'ready'" class="w-3.5 h-3.5" />
                    <AlertTriangle v-else-if="['degraded', 'unavailable'].includes(tool.health.status)" class="w-3.5 h-3.5" />
                    <CircleSlash2 v-else class="w-3.5 h-3.5" />
                    {{ healthLabel(tool.health.status) }}
                  </span>
                  <span v-if="tool.health.reason && tool.health.reason !== 'not_observed'" class="block mt-2 text-[11px] text-text-secondary">{{ tool.health.reason }}<template v-if="tool.health.detail"> · {{ tool.health.detail }}</template></span>
                  <span class="block mt-1 text-[10px] text-text-tertiary">{{ formatDate(tool.health.observed_at) }}</span>
                </td>
                <td class="px-5 py-4 text-text-secondary">
                  <span class="block"><b>{{ tool.risk_level }}</b> · {{ tool.side_effect || 'none' }}</span>
                  <span class="block mt-1">超时 {{ tool.timeout_seconds ? `${tool.timeout_seconds}s` : '默认' }} · 并发 {{ tool.parallel_safe ? (tool.max_concurrency || '默认') : 1 }}</span>
                  <span class="block mt-1">{{ tool.idempotent ? `幂等 · 最多 ${tool.max_attempts} 次` : '非幂等 · 不重试' }}</span>
                </td>
                <td class="px-5 py-4 text-text-secondary">
                  <span class="block font-semibold">调用 {{ tool.metrics.calls }} · 成功率 {{ successRate(tool) }}</span>
                  <span class="block mt-1">错误 {{ tool.metrics.errors }} · 超时 {{ tool.metrics.timeouts }}</span>
                  <span class="block mt-1">重试 {{ tool.metrics.retries }}<template v-if="tool.metrics.last_error_code"> · 最近 {{ tool.metrics.last_error_code }}</template></span>
                </td>
                <td class="px-5 py-4 text-text-secondary">
                  <span class="inline-flex items-center gap-1.5"><Timer class="w-3.5 h-3.5 text-accent" />执行 {{ formatDuration(tool.metrics.average_elapsed_ms) }}</span>
                  <span class="block mt-1">排队 {{ formatDuration(tool.metrics.average_queue_ms) }}</span>
                </td>
                <td class="px-5 py-4 text-text-secondary">
                  <span class="block">{{ tool.cache_enabled ? `缓存 ${tool.cache_ttl_seconds || '-'}s` : '未启用缓存' }}</span>
                  <span class="block mt-1">输出上限 {{ tool.max_output_chars ? tool.max_output_chars.toLocaleString() : '默认' }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </div>
</template>
