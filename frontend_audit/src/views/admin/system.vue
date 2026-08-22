<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import {
  Activity,
  ClipboardList,
  Copy,
  Database,
  Eye,
  KeyRound,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Server,
  ShieldCheck,
  Trash2,
  Users,
  XCircle,
} from 'lucide-vue-next';
import {
  AdminManagementAPI,
  type AdminApiKey,
  type AdminAuditLog,
  type AdminTask,
  type AdminUser,
  type SelfIngestionHealth,
  type SelfIngestionJob,
  type SelfIngestionJobStats,
  type SelfIngestionQualityFact,
  type SelfIngestionQualityStats,
  type SystemComponent,
} from '@/api/admin-management';

type TabKey = 'overview' | 'users' | 'apiKeys' | 'audits' | 'tasks' | 'selfIngestion' | 'system';

const tabs: Array<{ key: TabKey; label: string; icon: any }> = [
  { key: 'overview', label: '总览', icon: Activity },
  { key: 'users', label: '用户', icon: Users },
  { key: 'apiKeys', label: 'API Key', icon: KeyRound },
  { key: 'audits', label: '审计', icon: ClipboardList },
  { key: 'tasks', label: '任务', icon: Database },
  { key: 'selfIngestion', label: '自研入库', icon: ShieldCheck },
  { key: 'system', label: '系统', icon: Server },
];

const activeTab = ref<TabKey>('overview');
const loading = ref(false);
const notice = ref('');
const error = ref('');

const overview = ref<Record<string, any> | null>(null);
const users = ref<AdminUser[]>([]);
const usersTotal = ref(0);
const userSearch = ref('');
const userRole = ref('');
const userActive = ref('');
const userFormOpen = ref(false);
const userPage = ref(1);
const userForm = ref({
  username: '',
  password: '',
  email: '',
  role: 'user',
  is_active: true,
  create_api_key: true,
});

const apiKeys = ref<AdminApiKey[]>([]);
const apiKeysTotal = ref(0);
const apiKeyUsers = ref<AdminUser[]>([]);
const apiKeyUserId = ref('');
const apiKeyCreated = ref('');
const apiKeyPage = ref(1);
const apiKeyFormOpen = ref(false);
const apiKeyForm = ref({
  user_id: '',
  key_name: '',
  access_scope: 'mcp_only' as AdminApiKey['access_scope'],
  rate_limit: 100,
  expires_at: '',
});
const apiKeyEditor = ref<AdminApiKey | null>(null);
const apiKeyEditForm = ref({
  key_name: '',
  access_scope: 'full' as AdminApiKey['access_scope'],
  rate_limit: 100,
  expires_at: '',
});

const audits = ref<AdminAuditLog[]>([]);
const auditsTotal = ref(0);
const auditPage = ref(1);
const auditAction = ref('');
const auditTargetType = ref('');
const auditDetail = ref<AdminAuditLog | null>(null);

const tasks = ref<AdminTask[]>([]);
const tasksTotal = ref(0);
const taskPage = ref(1);
const taskStatus = ref('');
const taskDetail = ref<AdminTask | null>(null);

const health = ref<Record<string, SystemComponent>>({});
const selfIngestionHealth = ref<SelfIngestionHealth | null>(null);
const selfIngestionStats = ref<SelfIngestionJobStats | null>(null);
const selfIngestionQualityStats = ref<SelfIngestionQualityStats | null>(null);
const selfIngestionQualityFacts = ref<SelfIngestionQualityFact[]>([]);
const selfIngestionQualityFactsTotal = ref(0);
const selfIngestionQualityPage = ref(1);
const selfIngestionQualityKind = ref('');
const selfIngestionQualityItemKind = ref('');
const selfIngestionQualityReason = ref('');
const selfIngestionQualityJobId = ref('');
const selfIngestionQualityDocId = ref('');
const selfIngestionQualityDetail = ref<SelfIngestionQualityFact | null>(null);
const selfIngestionJobs = ref<SelfIngestionJob[]>([]);
const selfIngestionJobsTotal = ref(0);
const selfIngestionPage = ref(1);
const selfIngestionStatus = ref('');
const selfIngestionKbId = ref('');
const selfIngestionLastResult = ref<Record<string, any> | null>(null);
const switchBackendForm = ref({
  kb_id: '',
  ingestion_backend: 'legacy_lightrag' as 'legacy_lightrag' | 'self_ingestion',
  retrieval_backend: 'legacy_lightrag' as 'legacy_lightrag' | 'self_ingestion',
  reason: '',
});
const shadowRebuildForm = ref({
  kb_id: '',
  kb_code: '',
  limit: 20,
  dry_run: true,
  reason: 'admin_ui_shadow_rebuild',
});
const pageSize = 20;
const credentialResult = ref<{ title: string; password?: string; apiKey?: string } | null>(null);

const totalPages = (total: number) => Math.max(1, Math.ceil(total / pageSize));

const overviewCards = computed(() => {
  const value = overview.value || {};
  return [
    { label: '用户', value: value.users?.total ?? 0, sub: `活跃 ${value.users?.active ?? 0}` },
    { label: '知识库', value: value.knowledge_bases?.total ?? 0, sub: `文档 ${value.knowledge_bases?.documents ?? 0}` },
    { label: '会话', value: value.conversations?.total ?? 0, sub: `活跃 ${value.conversations?.active ?? 0}` },
    { label: '任务', value: value.tasks?.total ?? 0, sub: `失败 ${value.tasks?.failed ?? 0}` },
  ];
});

const selfIngestionQualityReasons = computed(() => {
  const seen = new Set<string>();
  return (selfIngestionQualityStats.value?.by_reason || [])
    .filter((item) => !selfIngestionQualityKind.value || item.kind === selfIngestionQualityKind.value)
    .filter((item) => !selfIngestionQualityItemKind.value || item.item_kind === selfIngestionQualityItemKind.value)
    .filter((item) => {
      const key = item.reason || '-';
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
});

const formatDate = (value?: string | null) =>
  value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-';

const statusClasses = (status: string) => {
  const normalized = String(status || '').toLowerCase();
  if (['ok', 'ready', 'active', 'completed', 'success'].includes(normalized)) {
    return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300';
  }
  if (['pending', 'processing', 'committing', 'repairing', 'starting'].includes(normalized)) {
    return 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300';
  }
  if (['failed', 'partial_success', 'error', 'disabled', 'cancelled'].includes(normalized)) {
    return 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300';
  }
  return 'bg-surface-sunken text-text-secondary';
};

const qualityKindClasses = (kind: string) =>
  kind === 'dropped'
    ? 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300'
    : 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300';

const payloadObject = (fact?: SelfIngestionQualityFact | null) => {
  const payload = fact?.payload;
  return payload && typeof payload === 'object' && !Array.isArray(payload) ? payload as Record<string, any> : {};
};

const qualityFactTitle = (fact: SelfIngestionQualityFact) => {
  const payload = payloadObject(fact);
  if (fact.item_kind === 'relation') {
    return `${payload.source_entity_id || payload.source_name || '-'} -> ${payload.target_entity_id || payload.target_name || '-'}`;
  }
  return String(payload.name || fact.item_id || '-');
};

const qualityFactEvidence = (fact: SelfIngestionQualityFact) => {
  const payload = payloadObject(fact);
  const evidence = payload.evidence;
  if (evidence && typeof evidence === 'object' && !Array.isArray(evidence)) {
    const first = Object.entries(evidence).find(([, value]) => String(value || '').trim());
    if (first) return `${first[0]}: ${String(first[1]).slice(0, 120)}`;
  }
  return '-';
};

const qualityFactChunks = (fact: SelfIngestionQualityFact) => {
  const chunkIds = payloadObject(fact).chunk_ids;
  return Array.isArray(chunkIds) && chunkIds.length ? chunkIds.join(', ') : '-';
};

const showNotice = (message: string) => {
  notice.value = message;
  window.setTimeout(() => {
    notice.value = '';
  }, 2800);
};

const run = async (task: () => Promise<void>) => {
  loading.value = true;
  error.value = '';
  try {
    await task();
  } catch (reason: any) {
    error.value = reason?.message || '操作失败';
  } finally {
    loading.value = false;
  }
};

const loadOverview = async () => {
  overview.value = await AdminManagementAPI.overview();
};

const loadUsers = async () => {
  const res = await AdminManagementAPI.users({
    q: userSearch.value.trim(),
    role: userRole.value,
    is_active: userActive.value,
    limit: pageSize,
    offset: (userPage.value - 1) * pageSize,
  });
  users.value = res.items || [];
  usersTotal.value = res.total || 0;
};

const loadApiKeys = async () => {
  if (apiKeyUsers.value.length === 0) {
    const userResult = await AdminManagementAPI.users({ limit: 200, offset: 0 });
    apiKeyUsers.value = userResult.items || [];
  }
  const res = await AdminManagementAPI.apiKeys({
    user_id: apiKeyUserId.value.trim(),
    limit: pageSize,
    offset: (apiKeyPage.value - 1) * pageSize,
  });
  apiKeys.value = res.items || [];
  apiKeysTotal.value = res.total || 0;
};

const loadAudits = async () => {
  const res = await AdminManagementAPI.auditLogs({
    action: auditAction.value.trim(),
    target_type: auditTargetType.value.trim(),
    limit: pageSize,
    offset: (auditPage.value - 1) * pageSize,
  });
  audits.value = res.items || [];
  auditsTotal.value = res.total || 0;
};

const loadTasks = async () => {
  const res = await AdminManagementAPI.tasks({
    status: taskStatus.value,
    limit: pageSize,
    offset: (taskPage.value - 1) * pageSize,
  });
  tasks.value = res.items || [];
  tasksTotal.value = res.total || 0;
};

const loadHealth = async () => {
  health.value = await AdminManagementAPI.systemHealth();
};

const loadSelfIngestion = async () => {
  const params = { kb_id: selfIngestionKbId.value.trim() };
  const [healthRes, statsRes, jobsRes, qualityStatsRes, qualityFactsRes] = await Promise.all([
    AdminManagementAPI.selfIngestionHealth(),
    AdminManagementAPI.selfIngestionJobStats(params),
    AdminManagementAPI.selfIngestionJobs({
      ...params,
      status: selfIngestionStatus.value,
      limit: pageSize,
      offset: (selfIngestionPage.value - 1) * pageSize,
    }),
    AdminManagementAPI.selfIngestionQualityStats(params),
    AdminManagementAPI.selfIngestionQualityFacts({
      ...params,
      kind: selfIngestionQualityKind.value || 'all',
      item_kind: selfIngestionQualityItemKind.value,
      reason: selfIngestionQualityReason.value,
      job_id: selfIngestionQualityJobId.value.trim(),
      doc_id: selfIngestionQualityDocId.value.trim(),
      limit: pageSize,
      offset: (selfIngestionQualityPage.value - 1) * pageSize,
    }),
  ]);
  selfIngestionHealth.value = healthRes;
  selfIngestionStats.value = statsRes;
  selfIngestionJobs.value = jobsRes.items || [];
  selfIngestionJobsTotal.value = jobsRes.total || 0;
  selfIngestionQualityStats.value = qualityStatsRes;
  selfIngestionQualityFacts.value = qualityFactsRes.items || [];
  selfIngestionQualityFactsTotal.value = qualityFactsRes.total || 0;
};

const refreshActive = () => run(async () => {
  if (activeTab.value === 'overview') await loadOverview();
  if (activeTab.value === 'users') await loadUsers();
  if (activeTab.value === 'apiKeys') await loadApiKeys();
  if (activeTab.value === 'audits') await loadAudits();
  if (activeTab.value === 'tasks') await loadTasks();
  if (activeTab.value === 'selfIngestion') await loadSelfIngestion();
  if (activeTab.value === 'system') await loadHealth();
});

const switchTab = (key: TabKey) => {
  activeTab.value = key;
  void refreshActive();
};

const createUser = () => run(async () => {
  if (!userForm.value.username.trim()) {
    error.value = '请填写用户名';
    return;
  }
  const submittedPassword = userForm.value.password || undefined;
  const created = await AdminManagementAPI.createUser({
    username: userForm.value.username.trim(),
    password: userForm.value.password || null,
    email: userForm.value.email || null,
    role: userForm.value.role,
    is_active: userForm.value.is_active,
    create_api_key: userForm.value.create_api_key,
  });
  userForm.value = {
    username: '',
    password: '',
    email: '',
    role: 'user',
    is_active: true,
    create_api_key: true,
  };
  userFormOpen.value = false;
  credentialResult.value = {
    title: `用户 ${created.username} 的一次性凭据`,
    password: created.generated_password || submittedPassword,
    apiKey: created.raw_api_key || undefined,
  };
  userPage.value = 1;
  await loadUsers();
  showNotice('用户已创建');
});

const updateUserRole = (user: AdminUser, role: string) => run(async () => {
  const next = await AdminManagementAPI.updateUser(user.user_id, { role });
  Object.assign(user, next);
  showNotice('角色已更新');
});

const updateUserEmail = (user: AdminUser, email: string) => run(async () => {
  const normalized = email.trim();
  if ((user.email || '') === normalized) return;
  const next = await AdminManagementAPI.updateUser(user.user_id, { email: normalized || null });
  Object.assign(user, next);
  showNotice('邮箱已更新');
});

const selectValue = (event: Event) => (event.target as HTMLSelectElement).value;

const toggleUser = (user: AdminUser) => run(async () => {
  const next = await AdminManagementAPI.setUserActive(user.user_id, !user.is_active);
  Object.assign(user, next);
  showNotice(next.is_active ? '用户已启用' : '用户已禁用');
});

const resetPassword = (user: AdminUser) => run(async () => {
  if (!confirm(`确定重置用户「${user.username}」的密码吗？`)) return;
  const res = await AdminManagementAPI.resetPassword(user.user_id);
  credentialResult.value = {
    title: `用户 ${user.username} 的新密码`,
    password: res.generated_password || undefined,
  };
  showNotice('密码已重置');
});

const createApiKey = () => run(async () => {
  if (!apiKeyForm.value.user_id.trim()) {
    error.value = '请选择绑定用户';
    return;
  }
  const created = await AdminManagementAPI.createApiKey(apiKeyForm.value.user_id.trim(), {
    key_name: apiKeyForm.value.key_name || undefined,
    access_scope: apiKeyForm.value.access_scope,
    rate_limit: Number(apiKeyForm.value.rate_limit || 100),
    expires_at: apiKeyForm.value.expires_at || null,
  });
  apiKeyCreated.value = created.raw_api_key;
  apiKeyForm.value = { user_id: '', key_name: '', access_scope: 'mcp_only', rate_limit: 100, expires_at: '' };
  apiKeyFormOpen.value = false;
  await loadApiKeys();
});

const toggleApiKey = (key: AdminApiKey) => run(async () => {
  const next = await AdminManagementAPI.updateApiKey(key.id, { is_active: !key.is_active });
  Object.assign(key, next);
  showNotice(next.is_active ? 'API Key 已启用' : 'API Key 已停用');
});

const rotateApiKey = (key: AdminApiKey) => run(async () => {
  if (!confirm('轮换后旧 Key 将立即失效，确定继续吗？')) return;
  const next = await AdminManagementAPI.rotateApiKey(key.id);
  Object.assign(key, next);
  apiKeyCreated.value = next.raw_api_key;
});

const deleteApiKey = (key: AdminApiKey) => run(async () => {
  if (!confirm('确定删除这个 API Key 吗？')) return;
  await AdminManagementAPI.deleteApiKey(key.id);
  apiKeys.value = apiKeys.value.filter((item) => item.id !== key.id);
  showNotice('API Key 已删除');
});

const openApiKeyEditor = (key: AdminApiKey) => {
  apiKeyEditor.value = key;
  apiKeyEditForm.value = {
    key_name: key.key_name || '',
    access_scope: key.access_scope,
    rate_limit: key.rate_limit,
    expires_at: key.expires_at ? key.expires_at.slice(0, 16) : '',
  };
};

const saveApiKeyEdit = () => run(async () => {
  if (!apiKeyEditor.value) return;
  const next = await AdminManagementAPI.updateApiKey(apiKeyEditor.value.id, {
    key_name: apiKeyEditForm.value.key_name || undefined,
    access_scope: apiKeyEditForm.value.access_scope,
    rate_limit: Number(apiKeyEditForm.value.rate_limit || 100),
    expires_at: apiKeyEditForm.value.expires_at || null,
  });
  const idx = apiKeys.value.findIndex((item) => item.id === next.id);
  if (idx >= 0) apiKeys.value[idx] = next;
  apiKeyEditor.value = null;
  showNotice('API Key 已更新');
});

const switchKbBackend = () => run(async () => {
  const kbId = switchBackendForm.value.kb_id.trim();
  if (!kbId) {
    error.value = '请填写知识库 ID';
    return;
  }
  if (!confirm('确定切换该知识库的入库/检索后端吗？')) return;
  selfIngestionLastResult.value = await AdminManagementAPI.switchKbBackend(kbId, {
    ingestion_backend: switchBackendForm.value.ingestion_backend,
    retrieval_backend: switchBackendForm.value.retrieval_backend,
    reason: switchBackendForm.value.reason || undefined,
  });
  showNotice('后端切换成功');
  await loadSelfIngestion();
});

const retryTask = (task: AdminTask) => run(async () => {
  const next = await AdminManagementAPI.retryTask(task.task_id);
  Object.assign(task, next);
  showNotice('任务已重置为待处理');
});

const cancelTask = (task: AdminTask) => run(async () => {
  if (!confirm('确定取消这个任务吗？')) return;
  const next = await AdminManagementAPI.cancelTask(task.task_id);
  Object.assign(task, next);
  showNotice('任务已取消');
});

const openAuditDetail = (auditId: number) => run(async () => {
  auditDetail.value = await AdminManagementAPI.auditLog(auditId);
});

const openTaskDetail = (taskId: string) => run(async () => {
  taskDetail.value = await AdminManagementAPI.task(taskId);
});

const openSelfIngestionQualityDetail = (fact: SelfIngestionQualityFact) => run(async () => {
  selfIngestionQualityDetail.value = await AdminManagementAPI.selfIngestionQualityFact(fact.kind, fact.id);
});

const filterQualityFactsByJob = (job: SelfIngestionJob) => run(async () => {
  selfIngestionQualityJobId.value = job.job_id;
  selfIngestionQualityDocId.value = '';
  selfIngestionQualityPage.value = 1;
  await loadSelfIngestion();
});

const repairSelfIngestionJob = (job: SelfIngestionJob) => run(async () => {
  selfIngestionLastResult.value = await AdminManagementAPI.repairSelfIngestionJob(job.job_id);
  await loadSelfIngestion();
  showNotice('自研入库任务已提交修复');
});

const scanSelfIngestionRepair = () => run(async () => {
  selfIngestionLastResult.value = await AdminManagementAPI.scanSelfIngestionRepair(100);
  await loadSelfIngestion();
  showNotice('自研入库修复扫描已触发');
});

const shadowRebuildSelfIngestion = () => run(async () => {
  if (!shadowRebuildForm.value.kb_id.trim() && !shadowRebuildForm.value.kb_code.trim()) {
    error.value = '请填写知识库 ID 或知识库编码';
    return;
  }
  if (!shadowRebuildForm.value.dry_run && !confirm('将执行真实影子重建，确定继续吗？')) return;
  selfIngestionLastResult.value = await AdminManagementAPI.shadowRebuildSelfIngestion({
    kb_id: shadowRebuildForm.value.kb_id.trim() || null,
    kb_code: shadowRebuildForm.value.kb_code.trim() || null,
    limit: Number(shadowRebuildForm.value.limit || 20),
    dry_run: shadowRebuildForm.value.dry_run,
    reason: shadowRebuildForm.value.reason.trim() || 'admin_ui_shadow_rebuild',
  });
  await loadSelfIngestion();
  showNotice('影子重建请求已提交');
});

const formatJson = (value: any) => JSON.stringify(value || {}, null, 2);
const copyText = async (value: string) => {
  await navigator.clipboard.writeText(value);
  showNotice('已复制');
};

const changePage = async (target: 'users' | 'keys' | 'audits' | 'tasks' | 'selfIngestion' | 'selfIngestionQuality', delta: number) => {
  const refs = { users: userPage, keys: apiKeyPage, audits: auditPage, tasks: taskPage, selfIngestion: selfIngestionPage, selfIngestionQuality: selfIngestionQualityPage };
  const totals = { users: usersTotal.value, keys: apiKeysTotal.value, audits: auditsTotal.value, tasks: tasksTotal.value, selfIngestion: selfIngestionJobsTotal.value, selfIngestionQuality: selfIngestionQualityFactsTotal.value };
  const page = refs[target];
  page.value = Math.min(totalPages(totals[target]), Math.max(1, page.value + delta));
  await refreshActive();
};

const applyFilter = (target: 'users' | 'keys' | 'audits' | 'tasks' | 'selfIngestion' | 'selfIngestionQuality') => {
  const refs = { users: userPage, keys: apiKeyPage, audits: auditPage, tasks: taskPage, selfIngestion: selfIngestionPage, selfIngestionQuality: selfIngestionQualityPage };
  refs[target].value = 1;
  void refreshActive();
};

onMounted(() => {
  void refreshActive();
});
</script>

<template>
  <div class="mx-auto max-w-7xl space-y-6">
    <!-- Header -->
    <div class="flex flex-col gap-4 md:flex-row md:items-end md:justify-between border-b border-border-subtle pb-5">
      <div>
        <h2 class="text-3xl font-extrabold tracking-tight text-text-primary">系统管理</h2>
        <p class="mt-2 text-sm text-text-secondary">统一管辖用户权限、API 密钥、全站审计日志、后台异步任务与底层容器健康度监视。</p>
      </div>
      <button
        @click="refreshActive"
        class="inline-flex items-center justify-center gap-2 rounded-2xl bg-accent px-5 py-2.5 text-xs font-bold text-accent-fg transition-all hover:bg-accent-hover hover-lift shadow-sm cursor-pointer"
      >
        <Loader2 v-if="loading" class="h-4 w-4 animate-spin" />
        <RefreshCw v-else class="h-4 w-4" />
        <span>刷新</span>
      </button>
    </div>

    <!-- Tab Bar -->
    <div class="flex flex-wrap gap-2 rounded-2xl border border-border-subtle bg-surface/70 p-2 backdrop-blur-md shadow-sm">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        @click="switchTab(tab.key)"
        class="inline-flex items-center gap-2 rounded-xl px-4 py-2.5 text-xs font-bold transition-all duration-200 cursor-pointer"
        :class="activeTab === tab.key ? 'bg-accent text-white shadow-sm' : 'text-text-secondary hover:bg-surface-hover'"
      >
        <component :is="tab.icon" class="h-4 w-4" />
        <span>{{ tab.label }}</span>
      </button>
    </div>

    <!-- Notices & Credentials -->
    <p v-if="notice" class="rounded-2xl border border-emerald-250/60 bg-emerald-50/50 dark:border-emerald-800/40 dark:bg-emerald-950/20 px-4 py-3 text-xs font-bold text-emerald-700 dark:text-emerald-350">
      {{ notice }}
    </p>
    <p v-if="error" class="rounded-2xl border border-red-200/60 bg-red-50/50 dark:border-red-800/40 dark:bg-red-950/20 px-4 py-3 text-xs font-bold text-red-700 dark:text-red-350">
      {{ error }}
    </p>

    <div v-if="credentialResult" class="rounded-3xl border border-amber-200/60 bg-amber-50/50 dark:border-amber-800/40 dark:bg-amber-950/20 p-5 text-xs text-amber-900 dark:text-amber-350 font-medium shadow-sm">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0 space-y-2.5">
          <p class="font-bold text-sm flex items-center gap-1.5">
            <ShieldCheck class="w-4 h-4 text-amber-550" />
            <span>{{ credentialResult.title }}</span>
          </p>
          <p v-if="credentialResult.password" class="break-all font-mono bg-surface/70 p-2.5 rounded-xl border border-amber-200/30">密码：{{ credentialResult.password }}</p>
          <p v-if="credentialResult.apiKey" class="break-all font-mono bg-surface/70 p-2.5 rounded-xl border border-amber-200/30">API Key：{{ credentialResult.apiKey }}</p>
          <p class="text-[10px] text-amber-700 dark:text-amber-450 italic font-semibold">此凭证仅在创建时展示一次，离开本页或关闭该提示后将永远无法恢复，请立即复制并交付给对应人员。</p>
        </div>
        <div class="flex shrink-0 gap-2">
          <button v-if="credentialResult.password" @click="copyText(credentialResult.password)" class="p-2 rounded-xl border border-amber-300/60 bg-surface/90 hover:bg-amber-100/50 text-text-secondary cursor-pointer" title="复制密码"><Copy class="h-4 w-4" /></button>
          <button v-if="credentialResult.apiKey" @click="copyText(credentialResult.apiKey)" class="p-2 rounded-xl border border-amber-300/60 bg-surface/90 hover:bg-amber-100/50 text-text-secondary cursor-pointer" title="复制 API Key"><KeyRound class="h-4 w-4" /></button>
          <button @click="credentialResult = null" class="p-2 rounded-xl border border-amber-300/60 bg-surface/90 hover:bg-amber-100/50 text-text-secondary cursor-pointer" title="关闭"><XCircle class="h-4 w-4" /></button>
        </div>
      </div>
    </div>

    <!-- TAB 1: System Overview -->
    <section v-if="activeTab === 'overview'" class="space-y-6">
      <div class="grid gap-4 sm:grid-cols-2 md:grid-cols-4">
        <div v-for="card in overviewCards" :key="card.label" class="relative overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">{{ card.label }}</p>
          <p class="mt-2.5 text-3xl font-black text-text-primary dark:text-white">{{ card.value }}</p>
          <p class="mt-1 text-[11px] font-medium text-text-secondary">{{ card.sub }}</p>
        </div>
      </div>

      <!-- Recent Audits Table -->
      <div class="rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm backdrop-blur-md">
        <h3 class="mb-4 font-bold text-text-primary text-sm flex items-center gap-2">
          <ClipboardList class="w-4.5 h-4.5 text-accent" />
          <span>最新后台安全审计</span>
        </h3>

        <div class="overflow-hidden rounded-2xl border border-border-subtle">
          <div class="overflow-x-auto">
            <table class="w-full text-left text-sm">
              <thead class="bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary border-b border-border-subtle">
                <tr>
                  <th class="px-6 py-4">动作指令</th>
                  <th class="px-6 py-4">审计对象</th>
                  <th class="px-6 py-4">执行人</th>
                  <th class="px-6 py-4 text-right">时间</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-border-subtle">
                <tr v-for="item in overview?.recent_audits || []" :key="item.audit_id" class="hover:bg-surface-hover transition-colors">
                  <td class="px-6 py-4 font-mono text-xs font-bold text-text-secondary">{{ item.action }}</td>
                  <td class="px-6 py-4 text-xs text-text-secondary">{{ item.target_type }} / {{ item.target_id || '-' }}</td>
                  <td class="px-6 py-4 text-xs font-semibold">{{ item.actor_username || item.actor_user_id || '-' }}</td>
                  <td class="px-6 py-4 text-right text-xs text-text-tertiary">{{ formatDate(item.created_at) }}</td>
                </tr>
                <tr v-if="!(overview?.recent_audits || []).length">
                  <td colspan="4" class="px-6 py-10 text-center text-text-tertiary italic text-xs">
                    系统最近未产生额外审计记录。
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

    <!-- TAB 2: Users Management -->
    <section v-if="activeTab === 'users'" class="space-y-4">
      <!-- Filter Bar -->
      <div class="flex flex-col gap-3 rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md p-4 lg:flex-row lg:items-center">
        <div class="relative flex-1">
          <Search class="absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
          <input
            v-model="userSearch"
            @keyup.enter="refreshActive"
            class="h-10 w-full rounded-2xl border border-border-subtle bg-surface/70 pl-10 pr-3.5 text-xs outline-none transition focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-primary"
            placeholder="搜索用户名或邮箱..."
          />
        </div>
        <select v-model="userRole" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-secondary">
          <option value="">全部角色</option>
          <option value="admin">管理员 (admin)</option>
          <option value="user">普通用户 (user)</option>
        </select>
        <select v-model="userActive" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-secondary">
          <option value="">全部状态</option>
          <option value="true">正常启用</option>
          <option value="false">锁定停用</option>
        </select>
        <button @click="applyFilter('users')" class="h-10 px-5 rounded-2xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover cursor-pointer">筛选</button>
        <button @click="userFormOpen = !userFormOpen" class="inline-flex h-10 items-center justify-center gap-2 rounded-2xl bg-accent px-5 text-xs font-bold text-white transition hover:bg-accent-hover hover-lift shadow-sm cursor-pointer">
          <Plus class="h-4 w-4" />
          <span>新建用户</span>
        </button>
      </div>

      <!-- New User Form Grid -->
      <div v-if="userFormOpen" class="grid gap-3.5 rounded-3xl border border-border-subtle bg-surface-sunken p-4 md:grid-cols-6 items-center animate-in fade-in duration-200">
        <input v-model="userForm.username" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary font-semibold" placeholder="用户名" />
        <input v-model="userForm.password" type="password" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary font-semibold" placeholder="登录密码，可空" />
        <input v-model="userForm.email" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary font-semibold" placeholder="联系邮箱，可空" />
        <select v-model="userForm.role" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-secondary font-semibold">
          <option value="user">普通用户 (user)</option>
          <option value="admin">超级管理员 (admin)</option>
        </select>
        <label class="flex items-center gap-2 text-xs font-bold text-text-secondary cursor-pointer px-2">
          <input v-model="userForm.create_api_key" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
          <span>初始化 API Key</span>
        </label>
        <button @click="createUser" class="h-10 rounded-2xl bg-accent text-xs font-bold text-accent-fg transition hover:bg-accent-hover hover-lift cursor-pointer">立即创建</button>
      </div>

      <!-- Users Table Container -->
      <div class="overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm">
            <thead class="bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary border-b border-border-subtle">
              <tr>
                <th class="px-6 py-4">登录账号</th>
                <th class="px-6 py-4">邮箱</th>
                <th class="px-6 py-4">系统权限角色</th>
                <th class="px-6 py-4">账号状态</th>
                <th class="px-6 py-4">最近登录时间</th>
                <th class="px-6 py-4 text-right">管理操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="user in users" :key="user.user_id" class="hover:bg-surface-hover transition-colors">
                <td class="px-6 py-4">
                  <p class="font-bold text-text-primary text-sm">{{ user.username }}</p>
                  <p class="font-mono text-[10px] text-text-tertiary mt-0.5">{{ user.user_id }}</p>
                </td>
                <td class="px-6 py-4">
                  <input
                    :value="user.email || ''"
                    @change="updateUserEmail(user, selectValue($event))"
                    class="h-8 w-full max-w-[200px] px-2 bg-surface-sunken border border-border-subtle rounded-lg outline-none text-xs font-semibold text-text-secondary focus:ring-2 focus:ring-accent/20 focus:border-accent"
                    placeholder="填写邮箱"
                  />
                </td>
                <td class="px-6 py-4">
                  <select
                    :value="user.role"
                    @change="updateUserRole(user, selectValue($event))"
                    class="h-8 px-2 bg-surface-sunken border border-border-subtle rounded-lg outline-none text-xs font-semibold text-text-secondary"
                  >
                    <option value="user">普通用户 (user)</option>
                    <option value="admin">管理员 (admin)</option>
                  </select>
                </td>
                <td class="px-6 py-4">
                  <span class="inline-flex px-2 py-0.5 rounded-full text-[10px] font-bold" :class="statusClasses(user.is_active ? 'active' : 'disabled')">
                    {{ user.is_active ? '活动启用' : '锁定停用' }}
                  </span>
                </td>
                <td class="px-6 py-4 text-xs text-text-tertiary">{{ formatDate(user.last_login) }}</td>
                <td class="px-6 py-4">
                  <div class="flex justify-end gap-1.5">
                    <button @click="toggleUser(user)" class="h-8 px-3 rounded-lg border border-border-subtle hover:bg-surface-hover text-xs font-bold text-text-secondary cursor-pointer">
                      {{ user.is_active ? '禁用' : '启用' }}
                    </button>
                    <button @click="resetPassword(user)" class="h-8 px-3 rounded-lg border border-border-subtle hover:bg-surface-hover text-xs font-bold text-text-secondary cursor-pointer">
                      重置密码
                    </button>
                  </div>
                </td>
              </tr>
              <tr v-if="users.length === 0">
                <td colspan="6" class="px-6 py-12 text-center text-text-tertiary italic text-xs">
                  暂无相关用户，请点击上方的“新建用户”添加账户。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="usersTotal > 0" class="flex flex-col sm:flex-row items-center justify-between gap-4 py-2 text-xs font-semibold text-text-secondary">
        <span>共 {{ usersTotal }} 个用户，当前第 {{ userPage }} / {{ totalPages(usersTotal) }} 页</span>
        <div class="flex gap-2">
          <button @click="changePage('users', -1)" :disabled="userPage <= 1" class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer">上一页</button>
          <button @click="changePage('users', 1)" :disabled="userPage >= totalPages(usersTotal)" class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer">下一页</button>
        </div>
      </div>
    </section>

    <!-- TAB 3: API Keys Management -->
    <section v-if="activeTab === 'apiKeys'" class="space-y-4">
      <!-- Filter Bar -->
      <div class="flex flex-col gap-3 rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md p-4 md:flex-row md:items-center">
        <div class="relative flex-1">
          <Search class="absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
          <input
            v-model="apiKeyUserId"
            class="h-10 w-full rounded-2xl border border-border-subtle bg-surface/70 pl-10 pr-3.5 text-xs outline-none transition focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-primary"
            placeholder="输入用户 ID 进行精确筛选"
          />
        </div>
        <button @click="applyFilter('keys')" class="h-10 px-5 rounded-2xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover cursor-pointer">筛选</button>
        <button @click="apiKeyFormOpen = !apiKeyFormOpen" class="inline-flex h-10 items-center justify-center gap-2 rounded-2xl bg-accent px-5 text-xs font-bold text-white transition hover:bg-accent-hover hover-lift shadow-sm cursor-pointer">
          <Plus class="h-4 w-4" />
          <span>创建新 Key</span>
        </button>
      </div>

      <!-- New Key Info Alert Box -->
      <div v-if="apiKeyCreated" role="status" aria-live="polite" class="rounded-3xl border border-amber-250/60 bg-amber-50/50 dark:border-amber-800/40 dark:bg-amber-950/20 p-5 text-xs text-amber-900 dark:text-amber-350 font-medium shadow-sm flex items-center justify-between gap-4 animate-in fade-in duration-200">
        <div>
          <p class="font-bold mb-1">新生成的 API Key 已经就绪：</p>
          <span class="font-mono text-sm select-all bg-surface/70 px-2 py-1 rounded border border-amber-200/30">{{ apiKeyCreated }}</span>
          <p class="text-[10px] text-amber-700 dark:text-amber-450 mt-2 font-semibold">此秘钥仅在此展示一次，关闭提示后无法重新查看，请妥善保存。</p>
        </div>
        <button @click="copyText(apiKeyCreated)" class="p-2.5 rounded-xl border border-amber-300/60 bg-surface/90 hover:bg-amber-100/50 text-text-secondary shrink-0 cursor-pointer" title="复制 API Key" aria-label="复制 API Key">
          <Copy class="h-4 w-4" />
        </button>
      </div>

      <!-- New Key Form Grid -->
      <div v-if="apiKeyFormOpen" class="grid gap-3.5 rounded-3xl border border-border-subtle bg-surface-sunken p-4 md:grid-cols-6 items-center animate-in fade-in duration-200">
        <select v-model="apiKeyForm.user_id" required aria-label="绑定用户" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary font-semibold">
          <option value="" disabled>选择绑定用户（必填）</option>
          <option v-for="user in apiKeyUsers" :key="user.user_id" :value="user.user_id">{{ user.username }} · {{ user.user_id }}</option>
        </select>
        <input v-model="apiKeyForm.key_name" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary font-semibold" placeholder="密钥描述名称" />
        <select v-model="apiKeyForm.access_scope" aria-label="访问范围" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary font-semibold">
          <option value="mcp_only">仅 MCP 服务</option>
          <option value="full">全部 API</option>
        </select>
        <input v-model.number="apiKeyForm.rate_limit" type="number" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary font-semibold" placeholder="频次限流 (请求次数/分钟)" />
        <input v-model="apiKeyForm.expires_at" type="datetime-local" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-secondary font-semibold" />
        <button @click="createApiKey" class="h-10 rounded-2xl bg-accent text-xs font-bold text-accent-fg transition hover:bg-accent-hover hover-lift cursor-pointer">立即创建</button>
      </div>

      <!-- API Keys Table Container -->
      <div class="overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm">
            <thead class="bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary border-b border-border-subtle">
              <tr>
                <th class="px-6 py-4">授权密钥 (Masked Key)</th>
                <th class="px-6 py-4">绑定用户账号</th>
                <th class="px-6 py-4">限流限制</th>
                <th class="px-6 py-4">运行状态</th>
                <th class="px-6 py-4">调用量统计</th>
                <th class="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="key in apiKeys" :key="key.id" class="hover:bg-surface-hover transition-colors">
                <td class="px-6 py-4">
                  <p class="font-mono text-xs font-bold text-text-primary">{{ key.api_key }}</p>
                  <p class="text-[10px] text-text-tertiary mt-0.5">{{ key.key_name || '未命名密钥' }}</p>
                  <span class="mt-1 inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold" :class="key.access_scope === 'mcp_only' ? 'bg-cyan-50 text-cyan-700 dark:bg-cyan-950/30 dark:text-cyan-300' : 'bg-surface-sunken text-text-secondary'">
                    {{ key.access_scope === 'mcp_only' ? '仅 MCP' : '全部 API' }}
                  </span>
                </td>
                <td class="px-6 py-4">
                  <p class="font-semibold">{{ key.username || '-' }}</p>
                  <p class="font-mono text-[10px] text-text-tertiary mt-0.5">{{ key.user_id || '-' }}</p>
                </td>
                <td class="px-6 py-4 text-xs font-semibold">{{ key.rate_limit }} 次/分钟</td>
                <td class="px-6 py-4">
                  <span class="inline-flex px-2 py-0.5 rounded-full text-[10px] font-bold" :class="statusClasses(key.is_active ? 'active' : 'disabled')">
                    {{ key.is_active ? '活动启用' : '停用锁定' }}
                  </span>
                </td>
                <td class="px-6 py-4 text-xs text-text-secondary font-semibold">{{ key.usage_count }} 次</td>
                <td class="px-6 py-4">
                  <div class="flex justify-end gap-1.5">
                    <button @click="openApiKeyEditor(key)" class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer" title="编辑"><Pencil class="h-4 w-4" /></button>
                    <button @click="toggleApiKey(key)" class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer" :title="key.is_active ? '停用' : '启用'"><ShieldCheck class="h-4 w-4" /></button>
                    <button @click="rotateApiKey(key)" class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer" title="轮换 (废弃旧密钥并生成新密钥)"><RotateCcw class="h-4 w-4" /></button>
                    <button @click="deleteApiKey(key)" class="p-2 rounded-xl border border-red-200 dark:border-red-900/30 hover:bg-red-50 dark:hover:bg-red-950/20 text-red-600 dark:text-red-400 transition cursor-pointer" title="删除"><Trash2 class="h-4 w-4" /></button>
                  </div>
                </td>
              </tr>
              <tr v-if="apiKeys.length === 0">
                <td colspan="6" class="px-6 py-12 text-center text-text-tertiary italic text-xs">
                  暂无匹配 API 密钥记录。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="apiKeysTotal > 0" class="flex flex-col sm:flex-row items-center justify-between gap-4 py-2 text-xs font-semibold text-text-secondary">
        <span>共 {{ apiKeysTotal }} 个密钥，当前第 {{ apiKeyPage }} / {{ totalPages(apiKeysTotal) }} 页</span>
        <div class="flex gap-2">
          <button @click="changePage('keys', -1)" :disabled="apiKeyPage <= 1" class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer">上一页</button>
          <button @click="changePage('keys', 1)" :disabled="apiKeyPage >= totalPages(apiKeysTotal)" class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer">下一页</button>
        </div>
      </div>
    </section>

    <!-- TAB 4: Audits Logs -->
    <section v-if="activeTab === 'audits'" class="space-y-4">
      <!-- Filter Bar -->
      <div class="flex flex-col gap-3 rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md p-4 md:flex-row md:items-center">
        <input
          v-model="auditAction"
          class="h-10 px-3 bg-surface/70 border border-border-subtle rounded-2xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary"
          placeholder="过滤操作动作 (例如：user.create)"
        />
        <input
          v-model="auditTargetType"
          class="h-10 px-3 bg-surface/70 border border-border-subtle rounded-2xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary animate-in"
          placeholder="过滤目标类型 (例如：knowledge_base)"
        />
        <button @click="applyFilter('audits')" class="h-10 px-5 rounded-2xl bg-accent text-xs font-bold text-white transition hover:bg-accent-hover hover-lift shadow-sm ml-auto cursor-pointer">筛选日志</button>
      </div>

      <!-- Audits Table Container -->
      <div class="overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm">
            <thead class="bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary border-b border-border-subtle">
              <tr>
                <th class="px-6 py-4">审计指令动作</th>
                <th class="px-6 py-4">动作对象类型与标识</th>
                <th class="px-6 py-4">操作者</th>
                <th class="px-6 py-4">HTTP 请求</th>
                <th class="px-6 py-4">执行时间</th>
                <th class="px-6 py-4 text-right">详情</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="item in audits" :key="item.audit_id" class="hover:bg-surface-hover transition-colors">
                <td class="px-6 py-4 font-mono text-xs font-bold text-text-primary">{{ item.action }}</td>
                <td class="px-6 py-4 text-xs font-semibold text-text-secondary">{{ item.target_type }} / {{ item.target_id || '-' }}</td>
                <td class="px-6 py-4 text-xs font-bold text-text-secondary">{{ item.actor_username || item.actor_user_id || '-' }}</td>
                <td class="px-6 py-4">
                  <p class="text-xs font-black uppercase text-accent">{{ item.method || '-' }}</p>
                  <p class="max-w-[220px] truncate text-[10px] text-text-tertiary font-mono mt-0.5" :title="item.path || undefined">{{ item.path || '-' }}</p>
                </td>
                <td class="px-6 py-4 text-xs text-text-tertiary">{{ formatDate(item.created_at) }}</td>
                <td class="px-6 py-4">
                  <div class="flex justify-end">
                    <button @click="openAuditDetail(item.audit_id)" class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer" title="查看完整 JSON 审计详情"><Eye class="h-4 w-4" /></button>
                  </div>
                </td>
              </tr>
              <tr v-if="audits.length === 0">
                <td colspan="6" class="px-6 py-12 text-center text-text-tertiary italic text-xs">
                  未检索到任何符合条件的系统审计日志。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="auditsTotal > 0" class="flex flex-col sm:flex-row items-center justify-between gap-4 py-2 text-xs font-semibold text-text-secondary">
        <span>共 {{ auditsTotal }} 条日志记录，当前第 {{ auditPage }} / {{ totalPages(auditsTotal) }} 页</span>
        <div class="flex gap-2">
          <button @click="changePage('audits', -1)" :disabled="auditPage <= 1" class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer">上一页</button>
          <button @click="changePage('audits', 1)" :disabled="auditPage >= totalPages(auditsTotal)" class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer">下一页</button>
        </div>
      </div>
    </section>

    <!-- TAB 5: Backend Tasks Queue -->
    <section v-if="activeTab === 'tasks'" class="space-y-4">
      <!-- Filter Bar -->
      <div class="flex gap-3 rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md p-4">
        <select v-model="taskStatus" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-secondary">
          <option value="">全部运行状态</option>
          <option value="pending">待调度 (pending)</option>
          <option value="processing">处理中 (processing)</option>
          <option value="completed">已完成 (completed)</option>
          <option value="failed">执行失败 (failed)</option>
          <option value="cancelled">已取消 (cancelled)</option>
        </select>
        <button @click="applyFilter('tasks')" class="h-10 px-5 rounded-2xl bg-accent text-xs font-bold text-white transition hover:bg-accent-hover hover-lift shadow-sm cursor-pointer">过滤队列</button>
      </div>

      <!-- Tasks Table Container -->
      <div class="overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm">
            <thead class="bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary border-b border-border-subtle">
              <tr>
                <th class="px-6 py-4">任务编号</th>
                <th class="px-6 py-4">关联解析文件/文档</th>
                <th class="px-6 py-4">投递用户</th>
                <th class="px-6 py-4">状态</th>
                <th class="px-6 py-4">处理进度</th>
                <th class="px-6 py-4">异常简述</th>
                <th class="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="task in tasks" :key="task.task_id" class="hover:bg-surface-hover transition-colors">
                <td class="px-6 py-4">
                  <p class="font-mono text-xs font-bold text-text-primary">{{ task.task_id }}</p>
                  <p class="text-[10px] text-text-tertiary mt-0.5">{{ formatDate(task.created_at) }}</p>
                </td>
                <td class="px-6 py-4 text-xs font-bold text-text-secondary max-w-[200px] truncate" :title="task.filename || task.doc_id">{{ task.filename || task.doc_id }}</td>
                <td class="px-6 py-4 text-xs font-semibold">{{ task.username || task.user_id || '-' }}</td>
                <td class="px-6 py-4">
                  <span class="inline-flex px-2.5 py-1 rounded-full text-[10px] font-bold" :class="statusClasses(task.status)">
                    {{ task.status }}
                  </span>
                </td>
                <td class="px-6 py-4">
                  <div class="flex items-center gap-2">
                    <div class="w-16 bg-surface-sunken rounded-full h-1.5 overflow-hidden border border-border-subtle">
                      <div class="bg-accent h-full transition-all duration-300" :style="{ width: `${task.progress}%` }"></div>
                    </div>
                    <span class="text-xs font-bold text-text-secondary font-mono">{{ task.progress }}%</span>
                  </div>
                </td>
                <td class="px-6 py-4">
                  <p class="max-w-[200px] truncate text-[11px] text-red-500 font-semibold" :title="task.error_detail || task.error || ''">{{ task.error || '-' }}</p>
                </td>
                <td class="px-6 py-4">
                  <div class="flex justify-end gap-1.5">
                    <button @click="openTaskDetail(task.task_id)" class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer" title="查看完整错误堆栈及日志"><Eye class="h-4 w-4" /></button>
                    <button v-if="['failed', 'cancelled'].includes(task.status)" @click="retryTask(task)" class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer" title="重新投递此解析任务"><RotateCcw class="h-4 w-4" /></button>
                    <button v-if="['pending', 'processing'].includes(task.status)" @click="cancelTask(task)" class="p-2 rounded-xl border border-red-205 dark:border-red-900/30 hover:bg-red-50 dark:hover:bg-red-950/20 text-red-600 dark:text-red-400 transition cursor-pointer" title="强行终止该任务"><XCircle class="h-4 w-4" /></button>
                  </div>
                </td>
              </tr>
              <tr v-if="tasks.length === 0">
                <td colspan="7" class="px-6 py-12 text-center text-text-tertiary italic text-xs">
                  暂无正在执行的后台解析任务。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="tasksTotal > 0" class="flex flex-col sm:flex-row items-center justify-between gap-4 py-2 text-xs font-semibold text-text-secondary">
        <span>共 {{ tasksTotal }} 个任务，当前第 {{ taskPage }} / {{ totalPages(tasksTotal) }} 页</span>
        <div class="flex gap-2">
          <button @click="changePage('tasks', -1)" :disabled="taskPage <= 1" class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer">上一页</button>
          <button @click="changePage('tasks', 1)" :disabled="taskPage >= totalPages(tasksTotal)" class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer">下一页</button>
        </div>
      </div>
    </section>

    <!-- TAB 6: Self Ingestion Operations -->
    <section v-if="activeTab === 'selfIngestion'" class="space-y-5">
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">链路状态</p>
          <p class="mt-2 text-2xl font-black text-text-primary dark:text-white">{{ selfIngestionHealth?.status || '-' }}</p>
          <p class="mt-1 text-[11px] font-semibold text-text-secondary">队列深度 {{ selfIngestionHealth?.queue_depth ?? 0 }}</p>
        </div>
        <div class="rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">入库任务</p>
          <p class="mt-2 text-2xl font-black text-text-primary dark:text-white">{{ selfIngestionStats?.total ?? 0 }}</p>
          <p class="mt-1 text-[11px] font-semibold text-text-secondary">运行 {{ selfIngestionStats?.processing_count ?? 0 }} / 待处理 {{ selfIngestionStats?.by_status?.pending ?? 0 }}</p>
        </div>
        <div class="rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">异常任务</p>
          <p class="mt-2 text-2xl font-black text-red-600 dark:text-red-350">{{ (selfIngestionStats?.failed_count ?? 0) + (selfIngestionStats?.partial_count ?? 0) }}</p>
          <p class="mt-1 text-[11px] font-semibold text-text-secondary">失败 {{ selfIngestionStats?.failed_count ?? 0 }} / 部分完成 {{ selfIngestionStats?.partial_count ?? 0 }}</p>
        </div>
        <div class="rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">质检事实</p>
          <p class="mt-2 text-2xl font-black text-text-primary dark:text-white">{{ (selfIngestionQualityStats?.total_candidates ?? 0) + (selfIngestionQualityStats?.total_dropped ?? 0) }}</p>
          <p class="mt-1 text-[11px] font-semibold text-text-secondary">候选 {{ selfIngestionQualityStats?.total_candidates ?? 0 }} / 丢弃 {{ selfIngestionQualityStats?.total_dropped ?? 0 }}</p>
        </div>
      </div>

      <div class="flex flex-col gap-3 rounded-3xl border border-border-subtle bg-surface/80 p-4 shadow-sm backdrop-blur-md lg:flex-row lg:items-center">
        <div class="relative flex-1">
          <Search class="absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
          <input
            v-model="selfIngestionKbId"
            @keyup.enter="applyFilter('selfIngestion')"
            class="h-10 w-full rounded-2xl border border-border-subtle bg-surface/70 pl-10 pr-3.5 text-xs font-semibold text-text-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
            placeholder="按知识库 ID 筛选"
          />
        </div>
        <select v-model="selfIngestionStatus" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs font-semibold text-text-secondary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20">
          <option value="">全部状态</option>
          <option value="pending">待处理</option>
          <option value="processing">处理中</option>
          <option value="committing">提交中</option>
          <option value="completed">已完成</option>
          <option value="failed">失败</option>
          <option value="partial_success">部分完成</option>
          <option value="repairing">修复中</option>
        </select>
        <button @click="applyFilter('selfIngestion')" class="h-10 rounded-2xl bg-accent px-5 text-xs font-bold text-white shadow-sm transition hover:bg-accent-hover hover-lift cursor-pointer">筛选任务</button>
        <button @click="scanSelfIngestionRepair" class="inline-flex h-10 items-center justify-center gap-2 rounded-2xl border border-border-subtle px-5 text-xs font-bold text-text-secondary transition hover:bg-surface-hover cursor-pointer">
          <RotateCcw class="h-4 w-4" />
          <span>扫描修复</span>
        </button>
      </div>

      <div class="rounded-3xl border border-amber-200/60 bg-amber-50/30 p-5 shadow-sm dark:border-amber-800/30 dark:bg-amber-950/10">
        <p class="text-xs font-bold text-amber-900 dark:text-amber-300 mb-3">切换知识库入库/检索后端</p>
        <div class="grid gap-3 md:grid-cols-5 items-center">
          <input v-model="switchBackendForm.kb_id" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl text-xs font-semibold" placeholder="知识库 ID (必填)" />
          <select v-model="switchBackendForm.ingestion_backend" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl text-xs font-semibold">
            <option value="legacy_lightrag">传统入库</option>
            <option value="self_ingestion">自研入库</option>
          </select>
          <select v-model="switchBackendForm.retrieval_backend" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl text-xs font-semibold">
            <option value="legacy_lightrag">传统检索</option>
            <option value="self_ingestion">自研检索</option>
          </select>
          <input v-model="switchBackendForm.reason" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl text-xs font-semibold" placeholder="切换原因（可选）" />
          <button @click="switchKbBackend" class="h-10 rounded-2xl bg-amber-600 px-4 text-xs font-bold text-white hover:bg-amber-700 cursor-pointer">执行切换</button>
        </div>
      </div>

      <div class="overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 shadow-sm backdrop-blur-md">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm">
            <thead class="border-b border-border-subtle bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary">
              <tr>
                <th class="px-6 py-4">任务</th>
                <th class="px-6 py-4">知识库 / 文档</th>
                <th class="px-6 py-4">状态</th>
                <th class="px-6 py-4">产物</th>
                <th class="px-6 py-4">异常</th>
                <th class="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="job in selfIngestionJobs" :key="job.job_id" class="transition-colors hover:bg-surface-hover">
                <td class="px-6 py-4">
                  <p class="font-mono text-xs font-bold text-text-primary">{{ job.job_id }}</p>
                  <p class="mt-0.5 text-[10px] text-text-tertiary">{{ formatDate(job.created_at) }}</p>
                </td>
                <td class="px-6 py-4">
                  <p class="font-mono text-xs font-bold text-text-secondary">{{ job.kb_id || '-' }}</p>
                  <p class="mt-0.5 max-w-[220px] truncate font-mono text-[10px] text-text-tertiary" :title="job.doc_id || job.file_id || undefined">{{ job.doc_id || job.file_id || '-' }}</p>
                </td>
                <td class="px-6 py-4">
                  <span class="inline-flex rounded-full px-2.5 py-1 text-[10px] font-bold" :class="statusClasses(job.status)">
                    {{ job.status }}
                  </span>
                  <p class="mt-1 text-[10px] font-semibold text-text-tertiary">{{ job.stage || '-' }} · retry {{ job.retry_count }}</p>
                </td>
                <td class="px-6 py-4 text-xs font-semibold text-text-secondary">
                  <p>chunks {{ job.chunk_count }}</p>
                  <p>vectors {{ job.vector_count }}</p>
                </td>
                <td class="px-6 py-4">
                  <p class="max-w-[260px] truncate text-[11px] font-semibold text-red-500" :title="job.error_message || ''">{{ job.error_message || '-' }}</p>
                </td>
                <td class="px-6 py-4">
                  <div class="flex justify-end gap-1.5">
                    <button @click="filterQualityFactsByJob(job)" class="p-2 rounded-xl border border-border-subtle text-text-secondary transition hover:bg-surface-hover cursor-pointer" title="查看该任务质检事实">
                      <Eye class="h-4 w-4" />
                    </button>
                    <button @click="repairSelfIngestionJob(job)" class="p-2 rounded-xl border border-border-subtle text-text-secondary transition hover:bg-surface-hover cursor-pointer" title="提交单任务修复">
                      <RotateCcw class="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
              <tr v-if="selfIngestionJobs.length === 0">
                <td colspan="6" class="px-6 py-12 text-center text-xs italic text-text-tertiary">
                  暂无符合条件的自研入库任务。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="selfIngestionJobsTotal > 0" class="flex flex-col items-center justify-between gap-4 py-2 text-xs font-semibold text-text-secondary sm:flex-row">
        <span>共 {{ selfIngestionJobsTotal }} 个任务，当前第 {{ selfIngestionPage }} / {{ totalPages(selfIngestionJobsTotal) }} 页</span>
        <div class="flex gap-2">
          <button @click="changePage('selfIngestion', -1)" :disabled="selfIngestionPage <= 1" class="rounded-xl border border-border-subtle px-4 py-2 shadow-sm transition-colors hover:bg-surface-hover disabled:opacity-40 cursor-pointer">上一页</button>
          <button @click="changePage('selfIngestion', 1)" :disabled="selfIngestionPage >= totalPages(selfIngestionJobsTotal)" class="rounded-xl border border-border-subtle px-4 py-2 shadow-sm transition-colors hover:bg-surface-hover disabled:opacity-40 cursor-pointer">下一页</button>
        </div>
      </div>

      <div class="rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm backdrop-blur-md">
        <div class="mb-4 flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h3 class="flex items-center gap-2 text-sm font-bold text-text-primary">
              <ShieldCheck class="h-4.5 w-4.5 text-accent" />
              <span>质量审计事实</span>
            </h3>
            <p class="mt-1 text-[11px] font-semibold text-text-tertiary">
              共 {{ selfIngestionQualityFactsTotal }} 条，候选 {{ selfIngestionQualityStats?.total_candidates ?? 0 }} / 丢弃 {{ selfIngestionQualityStats?.total_dropped ?? 0 }}
            </p>
          </div>
          <div class="flex flex-wrap gap-2">
            <span
              v-for="item in selfIngestionQualityStats?.by_reason || []"
              :key="`${item.kind}-${item.item_kind}-${item.reason}`"
              class="inline-flex items-center gap-1.5 rounded-full border border-border-subtle bg-surface-sunken px-2.5 py-1 text-[10px] font-bold text-text-secondary"
            >
              <span>{{ item.kind }}/{{ item.item_kind }}</span>
              <span class="text-text-tertiary">{{ item.reason || '-' }}</span>
              <span class="text-accent">{{ item.count }}</span>
            </span>
          </div>
        </div>

        <div class="mb-4 grid gap-3 lg:grid-cols-6">
          <select v-model="selfIngestionQualityKind" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs font-semibold text-text-secondary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20">
            <option value="">全部类型</option>
            <option value="candidate">候选</option>
            <option value="dropped">丢弃</option>
          </select>
          <select v-model="selfIngestionQualityItemKind" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs font-semibold text-text-secondary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20">
            <option value="">实体/关系</option>
            <option value="entity">实体</option>
            <option value="relation">关系</option>
          </select>
          <select v-model="selfIngestionQualityReason" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs font-semibold text-text-secondary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20">
            <option value="">全部原因</option>
            <option v-for="item in selfIngestionQualityReasons" :key="item.reason" :value="item.reason">{{ item.reason || '-' }}</option>
          </select>
          <input v-model="selfIngestionQualityJobId" @keyup.enter="applyFilter('selfIngestionQuality')" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs font-semibold text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" placeholder="job_id" />
          <input v-model="selfIngestionQualityDocId" @keyup.enter="applyFilter('selfIngestionQuality')" class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs font-semibold text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" placeholder="doc_id" />
          <button @click="applyFilter('selfIngestionQuality')" class="h-10 rounded-2xl bg-accent px-5 text-xs font-bold text-white shadow-sm transition hover:bg-accent-hover hover-lift cursor-pointer">筛选质检</button>
        </div>

        <div class="overflow-hidden rounded-2xl border border-border-subtle">
          <div class="overflow-x-auto">
            <table class="w-full text-left text-sm">
              <thead class="border-b border-border-subtle bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary">
                <tr>
                  <th class="px-5 py-3.5">事实</th>
                  <th class="px-5 py-3.5">类型 / 原因</th>
                  <th class="px-5 py-3.5">证据</th>
                  <th class="px-5 py-3.5">上下文</th>
                  <th class="px-5 py-3.5 text-right">操作</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-border-subtle">
                <tr v-for="fact in selfIngestionQualityFacts" :key="`${fact.kind}-${fact.id}`" class="transition-colors hover:bg-surface-hover">
                  <td class="px-5 py-4">
                    <p class="max-w-[240px] truncate text-xs font-bold text-text-primary" :title="qualityFactTitle(fact)">{{ qualityFactTitle(fact) }}</p>
                    <p class="mt-0.5 font-mono text-[10px] text-text-tertiary">{{ fact.item_id }}</p>
                  </td>
                  <td class="px-5 py-4">
                    <div class="flex flex-wrap items-center gap-1.5">
                      <span class="inline-flex rounded-full px-2.5 py-1 text-[10px] font-bold" :class="qualityKindClasses(fact.kind)">{{ fact.kind }}</span>
                      <span class="inline-flex rounded-full bg-surface-sunken px-2.5 py-1 text-[10px] font-bold text-text-secondary">{{ fact.item_kind }}</span>
                    </div>
                    <p class="mt-1 text-[11px] font-semibold text-text-secondary">{{ fact.item_type || '-' }} · {{ fact.reason || '-' }}</p>
                    <p class="mt-0.5 text-[10px] text-text-tertiary">confidence {{ fact.confidence }}</p>
                  </td>
                  <td class="px-5 py-4">
                    <p class="max-w-[300px] truncate text-[11px] font-semibold text-text-secondary" :title="qualityFactEvidence(fact)">{{ qualityFactEvidence(fact) }}</p>
                    <p class="mt-0.5 max-w-[300px] truncate font-mono text-[10px] text-text-tertiary" :title="qualityFactChunks(fact)">chunks {{ qualityFactChunks(fact) }}</p>
                  </td>
                  <td class="px-5 py-4">
                    <p class="font-mono text-[10px] font-bold text-text-secondary">{{ fact.kb_id || '-' }}</p>
                    <p class="mt-0.5 max-w-[220px] truncate font-mono text-[10px] text-text-tertiary" :title="fact.job_id || undefined">{{ fact.job_id || '-' }}</p>
                    <p class="mt-0.5 max-w-[220px] truncate font-mono text-[10px] text-text-tertiary" :title="fact.doc_id || undefined">{{ fact.doc_id || '-' }}</p>
                  </td>
                  <td class="px-5 py-4 text-right">
                    <button @click="openSelfIngestionQualityDetail(fact)" class="inline-flex items-center justify-center rounded-xl border border-border-subtle p-2 text-text-secondary transition hover:bg-surface-hover cursor-pointer" title="查看原始 payload">
                      <Eye class="h-4 w-4" />
                    </button>
                  </td>
                </tr>
                <tr v-if="selfIngestionQualityFacts.length === 0">
                  <td colspan="5" class="px-6 py-10 text-center text-xs italic text-text-tertiary">
                    暂无符合条件的质量审计事实。
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-if="selfIngestionQualityFactsTotal > 0" class="mt-4 flex flex-col items-center justify-between gap-4 text-xs font-semibold text-text-secondary sm:flex-row">
          <span>共 {{ selfIngestionQualityFactsTotal }} 条事实，当前第 {{ selfIngestionQualityPage }} / {{ totalPages(selfIngestionQualityFactsTotal) }} 页</span>
          <div class="flex gap-2">
            <button @click="changePage('selfIngestionQuality', -1)" :disabled="selfIngestionQualityPage <= 1" class="rounded-xl border border-border-subtle px-4 py-2 shadow-sm transition-colors hover:bg-surface-hover disabled:opacity-40 cursor-pointer">上一页</button>
            <button @click="changePage('selfIngestionQuality', 1)" :disabled="selfIngestionQualityPage >= totalPages(selfIngestionQualityFactsTotal)" class="rounded-xl border border-border-subtle px-4 py-2 shadow-sm transition-colors hover:bg-surface-hover disabled:opacity-40 cursor-pointer">下一页</button>
          </div>
        </div>
      </div>

      <div class="grid gap-5 lg:grid-cols-2">
        <div class="rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <h3 class="mb-4 flex items-center gap-2 text-sm font-bold text-text-primary">
            <Database class="h-4.5 w-4.5 text-accent" />
            <span>影子重建</span>
          </h3>
          <div class="grid gap-3">
            <input v-model="shadowRebuildForm.kb_id" class="h-10 rounded-xl border border-border-subtle bg-surface px-3 text-xs font-semibold text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" placeholder="知识库 ID" />
            <input v-model="shadowRebuildForm.kb_code" class="h-10 rounded-xl border border-border-subtle bg-surface px-3 text-xs font-semibold text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" placeholder="知识库编码，可替代 ID" />
            <div class="grid gap-3 sm:grid-cols-2">
              <input v-model.number="shadowRebuildForm.limit" type="number" min="1" max="500" class="h-10 rounded-xl border border-border-subtle bg-surface px-3 text-xs font-semibold text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" placeholder="重建上限" />
              <input v-model="shadowRebuildForm.reason" class="h-10 rounded-xl border border-border-subtle bg-surface px-3 text-xs font-semibold text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" placeholder="操作原因" />
            </div>
            <label class="flex items-center gap-2 px-1 text-xs font-bold text-text-secondary cursor-pointer">
              <input v-model="shadowRebuildForm.dry_run" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
              <span>仅预演，不写入真实任务</span>
            </label>
            <button @click="shadowRebuildSelfIngestion" class="h-10 rounded-2xl bg-accent text-xs font-bold text-accent-fg transition hover:bg-accent-hover hover-lift cursor-pointer">提交重建</button>
          </div>
        </div>

        <div class="rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <h3 class="mb-4 flex items-center gap-2 text-sm font-bold text-text-primary">
            <Server class="h-4.5 w-4.5 text-accent" />
            <span>链路组件</span>
          </h3>
          <pre class="max-h-72 overflow-auto rounded-2xl border border-border-subtle bg-surface-sunken p-4 text-[10px] font-mono text-text-secondary shadow-inner custom-scrollbar">{{ formatJson(selfIngestionHealth?.components) }}</pre>
        </div>
      </div>

      <div v-if="selfIngestionLastResult" class="rounded-3xl border border-emerald-200/60 bg-emerald-50/50 p-5 text-xs text-emerald-900 shadow-sm dark:border-emerald-800/40 dark:bg-emerald-950/20 dark:text-emerald-350">
        <div class="mb-3 flex items-center justify-between gap-3">
          <p class="font-bold">最近一次运维操作结果</p>
          <button @click="selfIngestionLastResult = null" class="rounded-xl border border-emerald-300/60 bg-surface/90 px-3 py-1.5 text-[10px] font-bold text-emerald-800 hover:bg-emerald-100/50 dark:border-emerald-800 dark:text-emerald-300 cursor-pointer">关闭</button>
        </div>
        <pre class="max-h-80 overflow-auto rounded-2xl border border-emerald-200/30 bg-surface/80 p-4 text-[10px] font-mono text-text-secondary shadow-inner dark:border-emerald-900/20 custom-scrollbar">{{ formatJson(selfIngestionLastResult) }}</pre>
      </div>
    </section>

    <!-- TAB 6: System Components Status -->
    <section v-if="activeTab === 'system'" class="grid gap-5 lg:grid-cols-2">
      <div v-for="(component, name) in health" :key="name" class="rounded-3xl border border-border-subtle bg-surface/80 p-6 shadow-sm">
        <div class="flex items-start justify-between gap-3">
          <div class="flex items-start gap-3">
            <div class="p-2.5 rounded-xl bg-surface-sunken text-text-secondary">
              <Server class="w-5.5 h-5.5" />
            </div>
            <div>
              <p class="font-bold text-text-primary text-sm tracking-wide">{{ name }}</p>
              <p class="mt-1 text-xs text-text-secondary leading-relaxed font-semibold">{{ component.detail || '外部网络连接与底层响应正常' }}</p>
            </div>
          </div>
          <span class="inline-flex px-2.5 py-1 rounded-full text-[10px] font-bold" :class="statusClasses(component.status)">
            {{ component.status }}
          </span>
        </div>

        <!-- Metadata Json Block -->
        <pre v-if="Object.keys(component.metadata || {}).length" class="mt-4 max-h-52 overflow-auto rounded-2xl bg-surface-sunken border border-border-subtle p-4 text-[10px] font-mono text-text-secondary custom-scrollbar shadow-inner">{{ formatJson(component.metadata) }}</pre>
      </div>
      <div v-if="Object.keys(health).length === 0" class="col-span-2 py-16 text-center text-text-tertiary italic text-xs border border-dashed border-border-subtle rounded-3xl">
        未监控到任何后台运行组件的状态。
      </div>
    </section>

    <!-- Modal: Self Ingestion Quality Fact Detail -->
    <div v-if="selfIngestionQualityDetail" class="fixed inset-0 z-50 flex items-center justify-center p-4 animate-in fade-in duration-200" role="dialog" aria-modal="true" aria-label="质量审计事实详情">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="selfIngestionQualityDetail = null"></div>
      <div class="relative max-h-[90vh] w-full max-w-4xl overflow-y-auto rounded-3xl border border-white/55 bg-surface/90 p-8 shadow-2xl backdrop-blur-2xl custom-scrollbar dark:border-white/10 dark:bg-surface/95">
        <div class="mb-5 flex items-start justify-between gap-4 border-b border-border-subtle pb-4">
          <div>
            <h3 class="font-extrabold text-text-primary">质量审计事实详情</h3>
            <p class="mt-1 break-all font-mono text-[10px] text-text-tertiary">
              #{{ selfIngestionQualityDetail.id }} · {{ selfIngestionQualityDetail.item_id }}
            </p>
          </div>
          <button @click="selfIngestionQualityDetail = null" class="rounded-lg p-1.5 text-text-tertiary transition hover:bg-surface-hover cursor-pointer" title="关闭">
            <XCircle class="h-5 w-5" />
          </button>
        </div>

        <dl class="grid gap-4.5 rounded-2xl border border-border-subtle bg-surface-sunken p-5 text-xs md:grid-cols-2">
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">审计类型</dt><dd class="mt-1"><span class="rounded-full px-2.5 py-0.5 text-[10px] font-bold" :class="qualityKindClasses(selfIngestionQualityDetail.kind)">{{ selfIngestionQualityDetail.kind }}</span></dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">事实类型</dt><dd class="mt-1 font-semibold text-text-primary">{{ selfIngestionQualityDetail.item_kind }} / {{ selfIngestionQualityDetail.item_type || '-' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">原因</dt><dd class="mt-1 font-semibold text-text-primary">{{ selfIngestionQualityDetail.reason || '-' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">自动处理</dt><dd class="mt-1 font-semibold text-text-primary">{{ selfIngestionQualityDetail.auto_action || '-' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">置信度</dt><dd class="mt-1 font-mono font-bold text-text-primary">{{ selfIngestionQualityDetail.confidence }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">人工复核</dt><dd class="mt-1 font-semibold text-text-primary">{{ selfIngestionQualityDetail.requires_manual_review ? '需要' : '不需要' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">知识库 / 文档</dt><dd class="mt-1 break-all font-mono text-text-primary">{{ selfIngestionQualityDetail.kb_id }} / {{ selfIngestionQualityDetail.doc_id || '-' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">任务 / 时间</dt><dd class="mt-1 break-all font-mono text-text-primary">{{ selfIngestionQualityDetail.job_id || '-' }} / {{ formatDate(selfIngestionQualityDetail.created_at) }}</dd></div>
        </dl>

        <div class="mt-5 grid gap-4 lg:grid-cols-2">
          <div>
            <p class="mb-2 ml-1 text-xs font-bold uppercase tracking-widest text-text-tertiary">Evidence</p>
            <pre class="max-h-72 overflow-auto rounded-2xl border border-border-subtle bg-surface-sunken p-4 text-[10px] font-mono text-text-secondary shadow-inner custom-scrollbar">{{ formatJson(payloadObject(selfIngestionQualityDetail).evidence) }}</pre>
          </div>
          <div>
            <p class="mb-2 ml-1 text-xs font-bold uppercase tracking-widest text-text-tertiary">Payload</p>
            <pre class="max-h-72 overflow-auto rounded-2xl border border-border-subtle bg-surface-sunken p-4 text-[10px] font-mono text-text-secondary shadow-inner custom-scrollbar">{{ formatJson(selfIngestionQualityDetail.payload) }}</pre>
          </div>
        </div>
      </div>
    </div>

    <!-- Modal: API Key Editor -->
    <div v-if="apiKeyEditor" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="编辑 API Key">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="apiKeyEditor = null"></div>
      <div class="relative w-full max-w-md rounded-3xl border border-white/55 bg-surface/90 p-8 shadow-2xl backdrop-blur-2xl dark:border-white/10 dark:bg-surface/95">
        <h3 class="text-lg font-bold text-text-primary mb-5">编辑 API Key</h3>
        <div class="space-y-4">
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1">密钥名称</label>
            <input v-model="apiKeyEditForm.key_name" class="w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl text-xs" />
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1">限流 (次/分钟)</label>
            <input v-model.number="apiKeyEditForm.rate_limit" type="number" class="w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl text-xs" />
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1">访问范围</label>
            <select v-model="apiKeyEditForm.access_scope" class="w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl text-xs">
              <option value="mcp_only">仅 MCP 服务</option>
              <option value="full">全部 API</option>
            </select>
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1">过期时间</label>
            <input v-model="apiKeyEditForm.expires_at" type="datetime-local" class="w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl text-xs" />
          </div>
        </div>
        <div class="flex justify-end gap-3 mt-6">
          <button @click="apiKeyEditor = null" class="px-4 py-2 text-xs font-bold text-text-secondary">取消</button>
          <button @click="saveApiKeyEdit" class="px-5 py-2 bg-accent text-white text-xs font-bold rounded-xl">保存</button>
        </div>
      </div>
    </div>

    <!-- Modal: Audit Detail Messages -->
    <div v-if="auditDetail" class="fixed inset-0 z-50 flex items-center justify-center p-4 animate-in fade-in duration-200" role="dialog" aria-modal="true" aria-label="安全审计快照">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="auditDetail = null"></div>
      <div class="relative max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-3xl border border-white/55 bg-surface/90 p-8 shadow-2xl backdrop-blur-2xl custom-scrollbar dark:border-white/10 dark:bg-surface/95">
        <div class="mb-5 flex items-start justify-between gap-4 border-b border-border-subtle pb-4">
          <div>
            <h3 class="font-extrabold text-text-primary">底层安全审计快照</h3>
            <p class="mt-1 font-mono text-[10px] text-text-tertiary">#{{ auditDetail.audit_id }} · 指令: {{ auditDetail.action }}</p>
          </div>
          <button @click="auditDetail = null" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition cursor-pointer" title="关闭"><XCircle class="h-5 w-5" /></button>
        </div>

        <dl class="grid gap-4.5 text-xs md:grid-cols-2 bg-surface-sunken p-5 rounded-2xl border border-border-subtle">
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">安全管理员</dt><dd class="mt-1 font-semibold text-text-primary">{{ auditDetail.actor_username || auditDetail.actor_user_id || '-' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">执行响应状态</dt><dd class="mt-1 font-semibold text-text-primary">{{ auditDetail.status }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">被操纵对象 (Type / ID)</dt><dd class="mt-1 font-mono text-text-primary">{{ auditDetail.target_type }} / {{ auditDetail.target_id || '-' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">审计捕获时间</dt><dd class="mt-1 font-semibold text-text-primary">{{ formatDate(auditDetail.created_at) }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">请求方法与端点</dt><dd class="mt-1 font-mono text-text-primary">{{ auditDetail.method || '-' }} {{ auditDetail.path || '-' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">操作端 IP 来源</dt><dd class="mt-1 font-mono break-all text-text-primary">{{ auditDetail.ip_address || '-' }}</dd></div>
        </dl>

        <div class="mt-5">
          <p class="mb-2 text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">操作详细数据 (Detail Json)</p>
          <pre class="max-h-80 overflow-auto rounded-2xl bg-surface-sunken border border-border-subtle p-4 text-[10px] font-mono text-text-secondary shadow-inner custom-scrollbar">{{ formatJson(auditDetail.detail_json) }}</pre>
        </div>
      </div>
    </div>

    <!-- Modal: Task Detail & logs -->
    <div v-if="taskDetail" class="fixed inset-0 z-50 flex items-center justify-center p-4 animate-in fade-in duration-200" role="dialog" aria-modal="true" aria-label="后台任务日志">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="taskDetail = null"></div>
      <div class="relative max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-3xl border border-white/55 bg-surface/90 p-8 shadow-2xl backdrop-blur-2xl custom-scrollbar dark:border-white/10 dark:bg-surface/95">
        <div class="mb-5 flex items-start justify-between gap-4 border-b border-border-subtle pb-4">
          <div>
            <h3 class="font-extrabold text-text-primary">后台队列任务日志</h3>
            <p class="mt-1 break-all font-mono text-[10px] text-text-tertiary">UUID: {{ taskDetail.task_id }}</p>
          </div>
          <button @click="taskDetail = null" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition cursor-pointer" title="关闭"><XCircle class="h-5 w-5" /></button>
        </div>

        <dl class="grid gap-4.5 text-xs md:grid-cols-2 bg-surface-sunken p-5 rounded-2xl border border-border-subtle">
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">投递关联文件名</dt><dd class="mt-1 font-bold text-text-primary truncate" :title="taskDetail.filename || taskDetail.doc_id">{{ taskDetail.filename || taskDetail.doc_id }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">所有者账户</dt><dd class="mt-1 font-semibold text-text-primary">{{ taskDetail.username || taskDetail.user_id || '-' }}</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">调度运行状态</dt><dd class="mt-1"><span class="rounded-full px-2.5 py-0.5 text-[10px] font-bold" :class="statusClasses(taskDetail.status)">{{ taskDetail.status }}</span></dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">当前任务进度</dt><dd class="mt-1 font-mono font-bold text-text-primary">{{ taskDetail.progress }}%</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">故障自愈重试量</dt><dd class="mt-1 font-semibold text-text-primary">{{ taskDetail.retry_count }} / {{ taskDetail.max_retries }} 次</dd></div>
          <div><dt class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">生命周期 (创建/调度完成)</dt><dd class="mt-1 font-semibold text-text-primary">{{ formatDate(taskDetail.started_at) }} / {{ formatDate(taskDetail.completed_at) }}</dd></div>
        </dl>

        <div v-if="taskDetail.error || taskDetail.error_detail" class="mt-5 rounded-2xl border border-red-200/60 bg-red-50/50 dark:border-red-900/30 dark:bg-red-950/20 p-5 text-xs text-red-700 dark:text-red-300">
          <p class="font-bold text-sm flex items-center gap-1.5 mb-2">
            <XCircle class="w-4.5 h-4.5" />
            <span>任务故障堆栈详情: {{ taskDetail.error || '任务处理中断异常' }}</span>
          </p>
          <pre class="max-h-72 overflow-auto whitespace-pre-wrap text-[10px] font-mono bg-surface/70 p-4 rounded-xl border border-red-200/10 custom-scrollbar">{{ taskDetail.error_detail || '-' }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>
