<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { Bot, Check, Eye, Loader2, MessageSquare, Pencil, Plus, RefreshCw, Search, ServerCog, Trash2, X } from 'lucide-vue-next';
import { AdminPlatformAPI, type AdminAgent, type AdminConversation, type AdminModel } from '@/api/admin-platform';
import { KBDetailAPI, type KnowledgeBase } from '@/api/kb';
import {
  SkillAdminAPI,
  type AgentSkillBindingInput,
  type ManagedSkill,
} from '@/api/skills';

type Tab = 'models' | 'agents' | 'conversations';
const activeTab = ref<Tab>('models');
const loading = ref(false);
const error = ref('');
const notice = ref('');
const models = ref<AdminModel[]>([]);
const agents = ref<AdminAgent[]>([]);
const knowledgeBases = ref<KnowledgeBase[]>([]);
const conversations = ref<AdminConversation[]>([]);
const conversationTotal = ref(0);
const conversationPage = ref(1);
const conversationQuery = ref('');
const includeDeleted = ref(false);
const conversationStats = ref<Record<string, any>>({});
const conversationDetail = ref<any | null>(null);
const pageSize = 20;

const modelEditor = ref<AdminModel | null>(null);
const modelForm = ref<Record<string, any>>({});
const agentEditor = ref<AdminAgent | null>(null);
const isNewAgent = ref(false);
const agentKbIds = ref<string[]>([]);
const availableSkills = ref<ManagedSkill[]>([]);
const agentSkillBindings = ref<AgentSkillBindingInput[]>([]);

const tabs = [
  { key: 'models' as const, label: '模型配置', icon: ServerCog },
  { key: 'agents' as const, label: '智能体', icon: Bot },
  { key: 'conversations' as const, label: '会话管理', icon: MessageSquare },
];

const modelUsageScopes = [
  { value: 'chat', label: '前端对话', description: '允许用户在对话页选择' },
  { value: 'image_analysis', label: '图片分析', description: '仅供后端自动解析图片' },
  { value: 'ingestion', label: '知识入库', description: '仅供文档抽取与入库' },
  { value: 'asr', label: '语音识别', description: '仅供语音转写' },
  { value: 'embedding', label: '向量化', description: '仅供向量生成' },
  { value: 'rerank', label: '重排序', description: '仅供检索重排' },
] as const;
const modelUsageLabel = (scope: string) =>
  modelUsageScopes.find((item) => item.value === scope)?.label || scope;

const emptyModel = () => ({
  id: 0,
  usage_scope: 'chat',
  name: '',
  display_name: '',
  description: '',
  provider: 'custom',
  use: 'langchain_openai:ChatOpenAI',
  model: '',
  api_key: '',
  base_url: '',
  api_key_env_var: '',
  base_url_env_var: '',
  request_timeout: 600,
  max_retries: 2,
  max_tokens: 16384,
  max_input_tokens: 131072,
  temperature: 0.1,
  supports_vision: false,
  supports_thinking: false,
  supports_reasoning_effort: false,
  supports_streaming: true,
  is_active: true,
  is_default: false,
  sort_order: 0,
});

const notify = (message: string) => {
  notice.value = message;
  window.setTimeout(() => { notice.value = ''; }, 2500);
};

const run = async (fn: () => Promise<void>) => {
  loading.value = true;
  error.value = '';
  try { await fn(); } catch (reason: any) { error.value = reason?.message || '操作失败'; } finally { loading.value = false; }
};

const loadModels = () => run(async () => { models.value = await AdminPlatformAPI.models(); });
const loadAgents = () => run(async () => {
  const [agentRows, kbRows, skillRows] = await Promise.all([
    AdminPlatformAPI.agents(),
    KBDetailAPI.list({ limit: 100 }),
    SkillAdminAPI.list('published'),
  ]);
  agents.value = agentRows;
  knowledgeBases.value = kbRows.items || [];
  availableSkills.value = skillRows.skills || [];
});
const loadConversations = () => run(async () => {
  const [rows, stats] = await Promise.all([
    AdminPlatformAPI.conversations({
      username: conversationQuery.value.trim(),
      include_deleted: includeDeleted.value,
      limit: pageSize,
      offset: (conversationPage.value - 1) * pageSize,
    }),
    AdminPlatformAPI.conversationStats(),
  ]);
  conversations.value = rows.items || [];
  conversationTotal.value = rows.total || 0;
  conversationStats.value = stats;
});

const refresh = () => {
  if (activeTab.value === 'models') return loadModels();
  if (activeTab.value === 'agents') return loadAgents();
  return loadConversations();
};
const switchTab = (tab: Tab) => { activeTab.value = tab; void refresh(); };

const openModel = (model?: AdminModel) => {
  modelEditor.value = model || ({ ...emptyModel() } as AdminModel);
  modelForm.value = { ...(model || emptyModel()), api_key: model ? '' : '' };
};
const saveModel = () => run(async () => {
  if (!modelForm.value.name || !modelForm.value.display_name || !modelForm.value.model || !modelForm.value.base_url) {
    throw new Error('请填写标识、名称、模型和 Base URL');
  }
  if (!modelEditor.value?.id && !modelForm.value.api_key) throw new Error('新模型必须填写 API Key');
  const payload = { ...modelForm.value };
  delete payload.id;
  if (!payload.api_key) delete payload.api_key;
  if (modelEditor.value?.id) await AdminPlatformAPI.updateModel(modelEditor.value.id, payload);
  else await AdminPlatformAPI.createModel(payload);
  modelEditor.value = null;
  await loadModels();
  notify('模型配置已保存');
});
const toggleModel = (model: AdminModel) => run(async () => {
  await AdminPlatformAPI.toggleModel(model.id, !model.is_active);
  model.is_active = !model.is_active;
});
const removeModel = (model: AdminModel) => {
  if (!confirm(`确定删除模型「${model.display_name}」吗？`)) return;
  void run(async () => { await AdminPlatformAPI.deleteModel(model.id); await loadModels(); });
};

const openAgent = (agent: AdminAgent) => run(async () => {
  const result = await SkillAdminAPI.listAgentSkills(agent.agent_code);
  isNewAgent.value = false;
  agentEditor.value = { ...agent };
  agentKbIds.value = agent.kb_bindings.map((item) => item.kb_id);
  agentSkillBindings.value = result.skills.map((item) => ({
    skill_id: item.skill_id,
    is_enabled: item.is_enabled,
    is_required: item.is_required,
    default_selected: item.default_selected,
    is_selectable: item.is_selectable,
    sort_order: item.sort_order,
  }));
});

const openNewAgent = () => run(async () => {
  const [kbRows, skillRows] = await Promise.all([
    KBDetailAPI.list({ limit: 100 }),
    SkillAdminAPI.list('published'),
  ]);
  knowledgeBases.value = kbRows.items || [];
  availableSkills.value = skillRows.skills || [];
  isNewAgent.value = true;
  agentEditor.value = {
    agent_code: '',
    agent_name: '',
    description: '',
    is_active: true,
    sort_order: 0,
    kb_bindings: [],
  };
  agentKbIds.value = [];
  agentSkillBindings.value = [];
});
const toggleAgentKb = (kbId: string) => {
  agentKbIds.value = agentKbIds.value.includes(kbId)
    ? agentKbIds.value.filter((id) => id !== kbId)
    : [...agentKbIds.value, kbId];
};
const agentHasSkill = (skillId: string) =>
  agentSkillBindings.value.some((item) => item.skill_id === skillId);
const toggleAgentSkill = (skillId: string) => {
  if (agentHasSkill(skillId)) {
    agentSkillBindings.value = agentSkillBindings.value.filter((item) => item.skill_id !== skillId);
    return;
  }
  agentSkillBindings.value.push({
    skill_id: skillId,
    is_enabled: true,
    is_required: false,
    default_selected: true,
    is_selectable: true,
    sort_order: agentSkillBindings.value.length,
  });
};
const skillBinding = (skillId: string) =>
  agentSkillBindings.value.find((item) => item.skill_id === skillId) as AgentSkillBindingInput;
const saveAgent = () => run(async () => {
  if (!agentEditor.value) return;
  const agent = agentEditor.value;
  const code = agent.agent_code.trim();
  if (!code) throw new Error('请填写智能体编码');
  if (!agent.agent_name.trim()) throw new Error('请填写智能体名称');
  const wasNew = isNewAgent.value || agent.has_display_record === false;
  await AdminPlatformAPI.upsertAgent(code, {
    agent_name: agent.agent_name.trim(),
    description: agent.description,
    is_active: agent.is_active,
    sort_order: agent.sort_order,
  });
  await AdminPlatformAPI.setAgentKbs(code, agentKbIds.value);
  await SkillAdminAPI.setAgentSkills(code, agentSkillBindings.value);
  agentEditor.value = null;
  isNewAgent.value = false;
  await loadAgents();
  notify(wasNew ? '展示记录已注册并保存' : '展示记录配置已保存');
});

const searchConversations = () => { conversationPage.value = 1; void loadConversations(); };
const conversationPages = computed(() => Math.max(1, Math.ceil(conversationTotal.value / pageSize)));
const changeConversationPage = (delta: number) => {
  conversationPage.value = Math.min(conversationPages.value, Math.max(1, conversationPage.value + delta));
  void loadConversations();
};
const openConversation = (item: AdminConversation) => run(async () => {
  conversationDetail.value = await AdminPlatformAPI.conversation(item.user_id, item.thread_id);
});
const removeConversation = (item: AdminConversation) => {
  if (!confirm(`确定删除会话「${item.title || item.thread_id}」吗？`)) return;
  void run(async () => { await AdminPlatformAPI.deleteConversation(item.user_id, item.thread_id); await loadConversations(); });
};
const formatDate = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-';

onMounted(loadModels);
</script>

<template>
  <div class="mx-auto max-w-7xl space-y-6">
    <!-- Header -->
    <header class="flex flex-col gap-4 md:flex-row md:items-end md:justify-between border-b border-border-subtle pb-5">
      <div>
        <h2 class="text-3xl font-extrabold tracking-tight text-text-primary">平台运维</h2>
        <p class="mt-2 text-sm text-text-secondary">配置底层模型与智能体展示记录，实时监察并管理全站会话运行情况。</p>
      </div>
      <button
        @click="refresh"
        class="inline-flex items-center justify-center gap-2 rounded-2xl bg-accent px-5 py-2.5 text-xs font-bold text-accent-fg transition-all hover:bg-accent-hover hover-lift shadow-sm cursor-pointer"
      >
        <Loader2 v-if="loading" class="h-4 w-4 animate-spin" />
        <RefreshCw v-else class="h-4 w-4" />
        <span>刷新数据</span>
      </button>
    </header>

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

    <!-- Alert Notices -->
    <p v-if="notice" class="rounded-2xl border border-emerald-250/60 bg-emerald-50/50 dark:border-emerald-800/40 dark:bg-emerald-950/20 px-4 py-3 text-xs font-bold text-emerald-700 dark:text-emerald-350">
      {{ notice }}
    </p>
    <p v-if="error" class="rounded-2xl border border-red-200/60 bg-red-50/50 dark:border-red-800/40 dark:bg-red-950/20 px-4 py-3 text-xs font-bold text-red-700 dark:text-red-350">
      {{ error }}
    </p>

    <!-- TAB 1: Models -->
    <section v-if="activeTab === 'models'" class="space-y-4">
      <div class="flex justify-end">
        <button
          @click="openModel()"
          class="inline-flex items-center gap-2 rounded-2xl bg-accent px-5 py-2.5 text-xs font-bold text-white transition hover:bg-accent-hover hover-lift shadow-sm cursor-pointer"
        >
          <Plus class="h-4 w-4" />
          <span>新增模型</span>
        </button>
      </div>

      <!-- Table Container -->
      <div class="overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm">
            <thead class="bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary border-b border-border-subtle">
              <tr>
                <th class="px-6 py-4">模型名称</th>
                <th class="px-6 py-4">用途隔离</th>
                <th class="px-6 py-4">提供商</th>
                <th class="px-6 py-4">支持能力</th>
                <th class="px-6 py-4">运行状态</th>
                <th class="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="model in models" :key="model.id" class="hover:bg-surface-hover transition-colors">
                <td class="px-6 py-4">
                  <p class="font-bold text-text-primary text-sm">{{ model.display_name }}</p>
                  <p class="font-mono text-[10px] text-text-tertiary mt-0.5">{{ model.name }} / {{ model.model }}</p>
                </td>
                <td class="px-6 py-4">
                  <span
                    class="inline-flex items-center rounded-lg bg-indigo-50 px-2 py-1 text-[10px] font-bold text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300"
                  >
                    {{ modelUsageLabel(model.usage_scope) }}
                  </span>
                  <p class="mt-1 max-w-[180px] text-[10px] text-text-tertiary">
                    {{ modelUsageScopes.find((item) => item.value === model.usage_scope)?.description }}
                  </p>
                </td>
                <td class="px-6 py-4">
                  <span class="inline-flex items-center px-2 py-0.5 rounded-lg text-[10px] font-bold bg-surface-sunken text-text-secondary">
                    {{ model.provider }}
                  </span>
                  <p class="max-w-[200px] truncate text-xs text-text-tertiary mt-1" :title="model.base_url">
                    {{ model.base_url }}
                  </p>
                </td>
                <td class="px-6 py-4">
                  <div class="flex flex-wrap gap-1">
                    <span v-for="cap in ([model.supports_vision && '视觉', model.supports_thinking && '思考', model.supports_streaming && '流式'].filter(Boolean) as string[])" :key="cap" class="px-2 py-0.5 rounded-md text-[10px] font-bold bg-accent-soft text-accent">
                      {{ cap }}
                    </span>
                    <span v-if="![model.supports_vision && '视觉', model.supports_thinking && '思考', model.supports_streaming && '流式'].filter(Boolean).length" class="text-xs text-text-tertiary">-</span>
                  </div>
                </td>
                <td class="px-6 py-4">
                  <span
                    class="inline-flex items-center px-2.5 py-1 rounded-full text-[11px] font-bold"
                    :class="model.is_active ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-400' : 'bg-surface-sunken text-text-secondary'"
                  >
                    <span v-if="model.is_default" class="text-[10px] mr-1 opacity-70">默认 ·</span>
                    <span>{{ model.is_active ? '已启用' : '停用' }}</span>
                  </span>
                </td>
                <td class="px-6 py-4">
                  <div class="flex justify-end gap-1.5">
                    <button
                      @click="toggleModel(model)"
                      class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer"
                      :title="model.is_active ? '停用' : '启用'"
                    >
                      <Check class="h-4 w-4" />
                    </button>
                    <button
                      @click="openModel(model)"
                      class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer"
                      title="编辑"
                    >
                      <Pencil class="h-4 w-4" />
                    </button>
                    <button
                      @click="removeModel(model)"
                      class="p-2 rounded-xl border border-red-200 dark:border-red-900/30 hover:bg-red-50 dark:hover:bg-red-950/20 text-red-600 dark:text-red-400 transition cursor-pointer"
                      title="删除"
                    >
                      <Trash2 class="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
              <tr v-if="!models.length">
                <td colspan="6" class="px-6 py-12 text-center text-text-tertiary italic text-xs">
                  暂无配置模型，请点击右上方按钮添加。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- TAB 2: Agents -->
    <section v-if="activeTab === 'agents'" class="space-y-4">
      <div class="flex justify-end">
        <button
          @click="openNewAgent"
          class="inline-flex items-center gap-2 rounded-2xl bg-accent px-5 py-2.5 text-xs font-bold text-white transition hover:bg-accent-hover hover-lift shadow-sm cursor-pointer"
        >
          <Plus class="h-4 w-4" />
          <span>注册展示记录</span>
        </button>
      </div>
      <div class="grid gap-5 lg:grid-cols-2">
      <article
        v-for="agent in agents"
        :key="agent.agent_code"
        class="relative group bg-surface/80 backdrop-blur-md border border-border-subtle rounded-3xl p-6 transition-all duration-300 hover:shadow-sm hover:border-accent/30"
      >
        <div class="flex justify-between items-start gap-4">
          <div class="flex items-start gap-3.5">
            <div class="p-3 rounded-2xl bg-accent-soft text-accent">
              <Bot class="h-6 w-6" />
            </div>
            <div>
              <h3 class="font-extrabold text-text-primary text-base">{{ agent.agent_name }}</h3>
              <p class="font-mono text-[10px] text-text-tertiary mt-0.5 tracking-wider">{{ agent.agent_code }}</p>
              <div class="mt-1.5 flex flex-wrap gap-1.5">
                <span
                  v-if="agent.has_display_record === false"
                  class="rounded-lg bg-amber-50 dark:bg-amber-950/30 border border-amber-200/70 dark:border-amber-900/40 px-2 py-0.5 text-[10px] font-bold text-amber-700 dark:text-amber-300"
                >待注册展示记录</span>
                <span
                  v-if="agent.legacy_agent_code"
                  class="rounded-lg bg-surface-sunken border border-border-subtle px-2 py-0.5 text-[10px] font-bold text-text-secondary"
                >旧编码 {{ agent.legacy_agent_code }}</span>
              </div>
            </div>
          </div>
          <button
            @click="openAgent(agent)"
            class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition-all duration-200 cursor-pointer"
            title="配置"
          >
            <Pencil class="h-4 w-4" />
          </button>
        </div>

        <p class="mt-4 text-xs leading-relaxed text-text-secondary min-h-8">
          {{ agent.description || '暂无描述信息' }}
        </p>

        <div class="mt-4 pt-4 border-t border-border-subtle">
          <p class="text-[10px] font-bold text-text-tertiary uppercase tracking-widest mb-2.5 ml-0.5">绑定知识库</p>
          <div class="flex flex-wrap gap-1.5">
            <span
              v-for="kb in agent.kb_bindings"
              :key="kb.kb_id"
              class="rounded-xl bg-surface-sunken border border-border-subtle px-2.5 py-1 text-xs font-semibold text-text-secondary"
            >
              {{ kb.kb_name || kb.kb_id }}
            </span>
            <span v-if="!agent.kb_bindings.length" class="text-xs text-text-tertiary italic mt-0.5 ml-0.5">未绑定任何知识库</span>
          </div>
        </div>
      </article>
      <div v-if="!agents.length" class="col-span-2 py-16 text-center text-text-tertiary italic text-xs border border-dashed border-border-subtle rounded-3xl">
        暂无智能体展示记录，请点击上方按钮注册。
      </div>
      </div>
    </section>

    <!-- TAB 3: Conversations -->
    <section v-if="activeTab === 'conversations'" class="space-y-5">
      <!-- Stats Cards -->
      <div class="grid gap-4 sm:grid-cols-2 md:grid-cols-4">
        <div class="relative overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">全部会话</p>
          <p class="mt-2 text-2xl font-extrabold text-text-primary dark:text-white">{{ conversationStats.total_conversations || 0 }}</p>
        </div>
        <div class="relative overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">活跃会话</p>
          <p class="mt-2 text-2xl font-extrabold text-accent">{{ conversationStats.active_conversations || 0 }}</p>
        </div>
        <div class="relative overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">会话用户数</p>
          <p class="mt-2 text-2xl font-extrabold text-text-primary dark:text-white">{{ conversationStats.unique_users || 0 }}</p>
        </div>
        <div class="relative overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 p-5 shadow-sm">
          <p class="text-[10px] font-bold uppercase tracking-widest text-text-tertiary">已删除会话</p>
          <p class="mt-2 text-2xl font-extrabold text-text-tertiary">{{ conversationStats.deleted_conversations || 0 }}</p>
        </div>
      </div>

      <!-- Filter Bar -->
      <div class="flex flex-col gap-3 rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md p-4 md:flex-row md:items-center">
        <div class="relative flex-1">
          <Search class="absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
          <input
            v-model="conversationQuery"
            @keyup.enter="searchConversations"
            class="h-10 w-full rounded-2xl border border-border-subtle bg-surface/70 pl-10 pr-3.5 text-xs outline-none transition focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-primary"
            placeholder="输入用户名搜索会话"
          />
        </div>
        <label class="flex items-center gap-2 text-xs font-bold text-text-secondary cursor-pointer select-none px-2">
          <input
            v-model="includeDeleted"
            type="checkbox"
            @change="searchConversations"
            class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30"
          />
          <span>包含已删除的会话</span>
        </label>
        <button
          @click="searchConversations"
          class="h-10 px-5 rounded-2xl bg-accent text-xs font-bold text-white transition hover:bg-accent-hover hover-lift shadow-sm cursor-pointer"
        >
          <span>搜索</span>
        </button>
      </div>

      <!-- Table Container -->
      <div class="overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md shadow-sm">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm">
            <thead class="bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary border-b border-border-subtle">
              <tr>
                <th class="px-6 py-4">会话信息</th>
                <th class="px-6 py-4">所有者</th>
                <th class="px-6 py-4">更新时间</th>
                <th class="px-6 py-4">状态</th>
                <th class="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="item in conversations" :key="item.id" class="hover:bg-surface-hover transition-colors">
                <td class="px-6 py-4">
                  <p class="max-w-md truncate font-bold text-text-primary text-sm">{{ item.title || '未命名会话' }}</p>
                  <p class="font-mono text-[10px] text-text-tertiary mt-0.5">{{ item.thread_id }}</p>
                </td>
                <td class="px-6 py-4">
                  <p class="font-semibold text-text-secondary">{{ item.username }}</p>
                  <p class="font-mono text-[10px] text-text-tertiary mt-0.5">{{ item.user_id }}</p>
                </td>
                <td class="px-6 py-4 text-xs text-text-secondary">
                  {{ formatDate(item.updated_at) }}
                </td>
                <td class="px-6 py-4">
                  <span
                    class="inline-flex px-2 py-0.5 rounded-full text-[10px] font-bold"
                    :class="item.is_deleted ? 'bg-red-50 text-red-700 dark:bg-red-950/20 dark:text-red-400' : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/20 dark:text-emerald-400'"
                  >
                    {{ item.is_deleted ? '已删除' : '活跃' }}
                  </span>
                </td>
                <td class="px-6 py-4">
                  <div class="flex justify-end gap-1.5">
                    <button
                      @click="openConversation(item)"
                      class="p-2 rounded-xl border border-border-subtle hover:bg-surface-hover text-text-secondary transition cursor-pointer"
                      title="查看消息"
                    >
                      <Eye class="h-4 w-4" />
                    </button>
                    <button
                      v-if="!item.is_deleted"
                      @click="removeConversation(item)"
                      class="p-2 rounded-xl border border-red-200 dark:border-red-900/30 hover:bg-red-50 dark:hover:bg-red-950/20 text-red-600 dark:text-red-400 transition cursor-pointer"
                      title="删除"
                    >
                      <Trash2 class="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
              <tr v-if="!conversations.length">
                <td colspan="5" class="px-6 py-12 text-center text-text-tertiary italic text-xs">
                  暂无匹配会话记录
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="conversationTotal > 0" class="flex flex-col sm:flex-row items-center justify-between gap-4 py-2 text-xs font-semibold text-text-secondary">
        <span>共 {{ conversationTotal }} 条，第 {{ conversationPage }} / {{ conversationPages }} 页</span>
        <div class="flex gap-2">
          <button
            @click="changeConversationPage(-1)"
            :disabled="conversationPage <= 1"
            class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer"
          >
            上一页
          </button>
          <button
            @click="changeConversationPage(1)"
            :disabled="conversationPage >= conversationPages"
            class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm cursor-pointer"
          >
            下一页
          </button>
        </div>
      </div>
    </section>

    <!-- Modal: Model Editor -->
    <div v-if="modelEditor" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="模型配置">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="modelEditor = null"></div>
      <div class="relative max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-3xl border border-white/55 bg-surface/90 p-8 shadow-2xl backdrop-blur-2xl animate-in fade-in zoom-in duration-200 custom-scrollbar dark:border-white/10 dark:bg-surface/95">
        <div class="mb-6 flex justify-between items-center border-b border-border-subtle pb-4">
          <h3 class="text-lg font-bold text-text-primary flex items-center gap-2">
            <ServerCog class="w-5 h-5 text-accent" />
            <span>{{ modelEditor.id ? '编辑模型配置' : '新增模型配置' }}</span>
          </h3>
          <button @click="modelEditor = null" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition cursor-pointer">
            <X class="h-4.5 w-4.5" />
          </button>
        </div>

        <div class="grid gap-4.5 md:grid-cols-2">
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            唯一标识
            <input v-model="modelForm.name" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" placeholder="如: openai-gpt-4o" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            显示名称
            <input v-model="modelForm.display_name" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" placeholder="如: GPT-4o (OpenAI)" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1 md:col-span-2">
            模型用途
            <select v-model="modelForm.usage_scope" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium">
              <option v-for="scope in modelUsageScopes" :key="scope.value" :value="scope.value">
                {{ scope.label }} — {{ scope.description }}
              </option>
            </select>
            <span class="mt-1.5 block normal-case tracking-normal text-[11px] font-medium text-text-tertiary">
              只有“前端对话”用途会出现在用户模型选择器；其他用途只能由后端对应流程调用。
            </span>
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            模型名称
            <input v-model="modelForm.model" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" placeholder="如: gpt-4o" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            服务商
            <input v-model="modelForm.provider" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" placeholder="如: openai 或 custom" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1 md:col-span-2">
            Base URL
            <input v-model="modelForm.base_url" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" placeholder="如: https://api.openai.com/v1" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1 md:col-span-2">
            API Key
            <input v-model="modelForm.api_key" type="password" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" :placeholder="modelEditor.id ? '留空则保持不变' : '必填，请输入授权密钥'" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            API Key 环境变量
            <input v-model="modelForm.api_key_env_var" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-mono" placeholder="例如 OPENAI_API_KEY" autocomplete="off" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            Base URL 环境变量
            <input v-model="modelForm.base_url_env_var" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-mono" placeholder="可选，例如 OPENAI_BASE_URL" autocomplete="off" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1 md:col-span-2">
            LangChain 实现
            <input v-model="modelForm.use" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-mono" placeholder="如 langchain_openai:ChatOpenAI" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            最大输出 Token
            <input v-model.number="modelForm.max_tokens" type="number" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            上下文窗口 Token
            <input v-model.number="modelForm.max_input_tokens" type="number" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            请求超时（秒）
            <input v-model.number="modelForm.request_timeout" type="number" min="1" step="1" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            最大重试次数
            <input v-model.number="modelForm.max_retries" type="number" min="0" max="20" step="1" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            温度
            <input v-model.number="modelForm.temperature" type="number" min="0" max="2" step="0.01" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>

          <div class="md:col-span-2 grid grid-cols-2 gap-3 mt-2 bg-surface-sunken border border-border-subtle p-4 rounded-2xl">
            <label class="flex items-center gap-2.5 text-xs font-bold text-text-secondary cursor-pointer">
              <input v-model="modelForm.supports_vision" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
              <span>支持视觉分析 (Vision)</span>
            </label>
            <label class="flex items-center gap-2.5 text-xs font-bold text-text-secondary cursor-pointer">
              <input v-model="modelForm.supports_thinking" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
              <span>支持推理思考 (Thinking)</span>
            </label>
            <label class="flex items-center gap-2.5 text-xs font-bold text-text-secondary cursor-pointer">
              <input v-model="modelForm.supports_reasoning_effort" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
              <span>支持 reasoning_effort</span>
            </label>
            <label class="flex items-center gap-2.5 text-xs font-bold text-text-secondary cursor-pointer">
              <input v-model="modelForm.supports_streaming" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
              <span>支持流式输出</span>
            </label>
            <label class="flex items-center gap-2.5 text-xs font-bold text-text-secondary cursor-pointer">
              <input v-model="modelForm.is_active" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
              <span>启用此模型</span>
            </label>
            <label class="flex items-center gap-2.5 text-xs font-bold text-text-secondary cursor-pointer">
              <input v-model="modelForm.is_default" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
              <span>设为系统默认模型</span>
            </label>
          </div>
        </div>

        <div class="mt-8 flex justify-end gap-3 border-t border-border-subtle pt-4">
          <button @click="modelEditor = null" class="h-10 px-5 rounded-xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover cursor-pointer">取消</button>
          <button @click="saveModel" class="h-10 px-6 rounded-xl bg-accent text-xs font-bold text-white hover:bg-accent-hover shadow-sm transition-all cursor-pointer">保存配置</button>
        </div>
      </div>
    </div>

    <!-- Modal: Agent Editor -->
    <div v-if="agentEditor" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="智能体配置">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="agentEditor = null"></div>
      <div class="relative max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-3xl border border-white/55 bg-surface/90 p-8 shadow-2xl backdrop-blur-2xl animate-in fade-in zoom-in duration-200 custom-scrollbar dark:border-white/10 dark:bg-surface/95">
        <div class="mb-6 flex justify-between items-center border-b border-border-subtle pb-4">
          <h3 class="text-lg font-bold text-text-primary flex items-center gap-2">
            <Bot class="w-5 h-5 text-accent" />
            <span>智能体展示记录与绑定配置</span>
          </h3>
          <button @click="agentEditor = null" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition cursor-pointer">
            <X class="h-4.5 w-4.5" />
          </button>
        </div>

        <div class="space-y-5">
          <p class="rounded-2xl border border-amber-200/70 bg-amber-50/70 px-4 py-3 text-xs leading-relaxed text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-300">
            此处仅注册管理后台展示记录及知识库、技能绑定；可运行智能体仍须部署同编码的 Agent Profile。
            旧编码（如 ne_full_feature）保存时会自动写入对应运行时编码（ne）。
          </p>
          <label v-if="isNewAgent" class="block text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            智能体编码 (唯一标识，须与 Profile 一致，如 ne / csh / groundwater / fmca)
            <input v-model="agentEditor.agent_code" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-mono" placeholder="例如：ne" />
          </label>
          <div v-else class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            智能体编码
            <p class="mt-1 font-mono text-sm text-text-secondary normal-case">{{ agentEditor.agent_code }}</p>
            <p v-if="agentEditor.legacy_agent_code" class="mt-1 text-[11px] font-medium normal-case text-text-secondary">
              当前知识库仍来自旧编码 {{ agentEditor.legacy_agent_code }}；保存后将写入 {{ agentEditor.agent_code }}。
            </p>
          </div>

          <label class="block text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            智能体名称
            <input v-model="agentEditor.agent_name" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>

          <label class="block text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            职能描述与工作流备注
            <textarea v-model="agentEditor.description" class="mt-1.5 w-full h-24 p-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium h-24 resize-none" placeholder="简要说明此智能体的职责..." />
          </label>

          <div class="flex flex-wrap items-center gap-6 bg-surface-sunken border border-border-subtle p-4 rounded-2xl">
            <label class="flex items-center gap-2 text-xs font-bold text-text-secondary cursor-pointer">
              <input v-model="agentEditor.is_active" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
              <span>启用此展示记录</span>
            </label>
            <label class="flex items-center gap-2 text-xs font-bold text-text-secondary ml-auto">
              排列顺序 (权值越大排越前)
              <input v-model.number="agentEditor.sort_order" type="number" class="ml-2 w-20 h-8 px-2 bg-surface border border-border-subtle rounded-lg outline-none text-xs text-center text-text-primary" />
            </label>
          </div>

          <!-- Bind Knowledge Base -->
          <div>
            <p class="text-xs font-bold text-text-tertiary uppercase tracking-widest mb-2.5 ml-1">知识库关联</p>
            <div class="grid gap-2 sm:grid-cols-2 max-h-[160px] overflow-y-auto pr-1">
              <label
                v-for="kb in knowledgeBases"
                :key="kb.kb_id"
                @click.prevent="toggleAgentKb(kb.kb_id)"
                class="flex items-center gap-2.5 border p-3 rounded-2xl cursor-pointer hover:bg-surface-hover transition"
                :class="agentKbIds.includes(kb.kb_id) ? 'border-accent/60 bg-accent-soft' : 'border-border-subtle'"
              >
                <input type="checkbox" :checked="agentKbIds.includes(kb.kb_id)" class="h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" />
                <span class="text-xs font-bold text-text-secondary truncate">{{ kb.kb_name }}</span>
              </label>
              <p v-if="!knowledgeBases.length" class="text-xs text-text-tertiary italic p-2 col-span-2">系统暂无可用知识库</p>
            </div>
          </div>

          <!-- Bind Skills -->
          <div>
            <p class="text-xs font-bold text-text-tertiary uppercase tracking-widest mb-2.5 ml-1">能力包 (Skills) 绑定</p>
            <div class="space-y-3.5 max-h-[260px] overflow-y-auto pr-1 custom-scrollbar">
              <div
                v-for="skill in availableSkills"
                :key="skill.skill_id"
                class="border border-border-subtle rounded-2xl p-4 transition-all duration-200"
                :class="agentHasSkill(skill.skill_id) ? 'border-accent/60 bg-accent-soft' : ''"
              >
                <label class="flex items-start gap-3 cursor-pointer select-none" @click.prevent="toggleAgentSkill(skill.skill_id)">
                  <input class="mt-0.5 h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30" type="checkbox" :checked="agentHasSkill(skill.skill_id)" />
                  <span class="min-w-0 flex-1">
                    <span class="text-xs font-extrabold text-text-primary">{{ skill.display_name }}</span>
                    <span class="ml-2 font-mono text-[9px] font-bold text-accent">{{ skill.skill_key }}</span>
                    <span class="mt-1 block text-xs text-text-secondary truncate">{{ skill.display_description || skill.description || '暂无说明' }}</span>
                  </span>
                </label>

                <!-- Sub-skills Settings -->
                <div v-if="agentHasSkill(skill.skill_id) && skillBinding(skill.skill_id)" class="mt-3.5 pt-3.5 border-t border-border-subtle grid grid-cols-2 sm:grid-cols-4 gap-3 text-[11px] font-bold text-text-secondary">
                  <label class="flex items-center gap-1.5 cursor-pointer">
                    <input v-model="skillBinding(skill.skill_id)!.is_enabled" type="checkbox" class="h-3.5 w-3.5 rounded border-border-subtle text-accent focus:ring-accent/30" />
                    <span>启用</span>
                  </label>
                  <label class="flex items-center gap-1.5 cursor-pointer">
                    <input v-model="skillBinding(skill.skill_id)!.is_required" type="checkbox" class="h-3.5 w-3.5 rounded border-border-subtle text-accent focus:ring-accent/30" />
                    <span>系统必需</span>
                  </label>
                  <label class="flex items-center gap-1.5 cursor-pointer">
                    <input v-model="skillBinding(skill.skill_id)!.default_selected" type="checkbox" class="h-3.5 w-3.5 rounded border-border-subtle text-accent focus:ring-accent/30" />
                    <span>默认选中</span>
                  </label>
                  <label class="flex items-center gap-1.5 cursor-pointer">
                    <input v-model="skillBinding(skill.skill_id)!.is_selectable" type="checkbox" class="h-3.5 w-3.5 rounded border-border-subtle text-accent focus:ring-accent/30" />
                    <span>允许用户选择</span>
                  </label>
                </div>
              </div>
              <p v-if="!availableSkills.length" class="text-xs text-text-tertiary italic p-2">系统无发布的能力包 (Skills)</p>
            </div>
          </div>
        </div>

        <div class="mt-8 flex justify-end gap-3 border-t border-border-subtle pt-4">
          <button @click="agentEditor = null" class="h-10 px-5 rounded-xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover cursor-pointer">取消</button>
          <button @click="saveAgent" class="h-10 px-6 rounded-xl bg-accent text-xs font-bold text-white hover:bg-accent-hover shadow-sm transition-all cursor-pointer">{{ isNewAgent || agentEditor?.has_display_record === false ? '注册并保存' : '保存配置' }}</button>
        </div>
      </div>
    </div>

    <!-- Modal: Conversation Detail Messages -->
    <div v-if="conversationDetail" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="会话详情">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="conversationDetail = null"></div>
      <div class="relative flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-3xl border border-white/55 bg-surface/90 shadow-2xl backdrop-blur-2xl animate-in fade-in zoom-in duration-200 dark:border-white/10 dark:bg-surface/95">
        <div class="flex items-center justify-between border-b border-border-subtle p-5">
          <div class="min-w-0">
            <h3 class="font-extrabold text-text-primary text-base truncate">{{ conversationDetail.conversation.title || '会话详情' }}</h3>
            <p class="font-mono text-[10px] text-text-tertiary mt-0.5">{{ conversationDetail.conversation.thread_id }}</p>
          </div>
          <button @click="conversationDetail = null" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition cursor-pointer">
            <X class="h-5 w-5" />
          </button>
        </div>

        <div class="flex-1 space-y-4 overflow-y-auto p-6 custom-scrollbar bg-surface-sunken">
          <div
            v-for="(message, index) in conversationDetail.messages"
            :key="message.id || index"
            class="rounded-2xl border p-4 shadow-sm"
            :class="message.role === 'human' || message.role === 'user' ? 'border-accent/30 bg-accent-soft' : 'border-emerald-150 bg-emerald-50/30 dark:border-emerald-950/30 dark:bg-emerald-950/15'"
          >
            <p class="mb-2 text-[10px] font-bold uppercase tracking-wider text-text-tertiary">{{ message.role }}</p>
            <pre class="whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-text-secondary">{{ message.content }}</pre>
          </div>
          <p v-if="!conversationDetail.messages.length" class="py-16 text-center text-text-tertiary italic text-xs">
            该会话暂无可供显示的消息记录。
          </p>
        </div>
      </div>
    </div>
  </div>
</template>
