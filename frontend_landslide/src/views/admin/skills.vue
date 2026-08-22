<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import {
  Archive,
  Bot,
  Check,
  ChevronRight,
  Download,
  FileArchive,
  Loader2,
  PackageOpen,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Trash2,
  UploadCloud,
  Users,
  X,
  XCircle,
} from 'lucide-vue-next';
import {
  SkillAdminAPI,
  type AgentSkillBindingInput,
  type AgentSummary,
  type ManagedSkill,
  type SkillGrant,
  type SkillVersion,
} from '@/api/skills';

const skills = ref<ManagedSkill[]>([]);
const agents = ref<AgentSummary[]>([]);
const loading = ref(true);
const error = ref('');
const notice = ref('');
const query = ref('');
const statusFilter = ref('');
const selectedSkill = ref<ManagedSkill | null>(null);
const versions = ref<SkillVersion[]>([]);
const versionsLoading = ref(false);
const grants = ref<SkillGrant[]>([]);
const grantsLoading = ref(false);
const actionKey = ref('');

const showUpload = ref(false);
const uploadFile = ref<File | null>(null);
const visibility = ref<ManagedSkill['visibility']>('private');
const uploading = ref(false);

const selectedAgentCode = ref('');
const bindingDraft = ref<Record<string, AgentSkillBindingInput>>({});
const compatibleSkillIds = ref<Set<string>>(new Set());
const bindingsLoading = ref(false);
const savingBindings = ref(false);
const grantSubjectType = ref<SkillGrant['subject_type']>('role');
const grantSubjectId = ref('user');
const grantEffect = ref<SkillGrant['effect']>('allow');
const grantExpiresAt = ref('');
const grantPermissions = ref<SkillGrant['permission'][]>(['view', 'use']);
const savingGrant = ref(false);

const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'draft', label: '草稿' },
  { value: 'published', label: '已发布' },
  { value: 'disabled', label: '已停用' },
  { value: 'archived', label: '已归档' },
];

const filteredSkills = computed(() => {
  const keyword = query.value.trim().toLowerCase();
  return skills.value.filter((skill) => {
    const matchesStatus = !statusFilter.value || skill.status === statusFilter.value;
    const matchesKeyword =
      !keyword ||
      [skill.display_name, skill.skill_key, skill.domain, skill.description]
        .some((value) => String(value || '').toLowerCase().includes(keyword));
    return matchesStatus && matchesKeyword;
  });
});

const bindableSkills = computed(() =>
  skills.value.filter(
    (skill) =>
      (skill.status === 'published' && skill.active_version_id && compatibleSkillIds.value.has(skill.skill_id)) ||
      !!bindingDraft.value[skill.skill_id],
  )
);
const grantGroups = computed(() => {
  const groups = new Map<string, {
    subject_type: SkillGrant['subject_type'];
    subject_id: string;
    effect: SkillGrant['effect'];
    permissions: SkillGrant['permission'][];
    ids: number[];
  }>();
  grants.value.forEach((grant) => {
    const key = `${grant.subject_type}:${grant.subject_id}:${grant.effect}`;
    const group = groups.get(key) || {
      subject_type: grant.subject_type,
      subject_id: grant.subject_id,
      effect: grant.effect,
      permissions: [],
      ids: [],
    };
    group.permissions.push(grant.permission);
    group.ids.push(grant.id);
    groups.set(key, group);
  });
  return Array.from(groups.values());
});

const activeVersion = computed(() => {
  if (!selectedSkill.value) return null;
  return versions.value.find((version) => version.version_id === selectedSkill.value?.active_version_id)
    || selectedSkill.value.active_version
    || null;
});

const manifestEntries = computed(() => {
  const manifest = activeVersion.value?.manifest;
  if (!manifest || typeof manifest !== 'object') return [] as Array<[string, unknown]>;
  const preferred = ['tools', 'allowed_workflows', 'preferred_workflow', 'dependencies', 'conflicts', 'resource_budget', 'guardrails'];
  return preferred
    .filter((key) => manifest[key] !== undefined && manifest[key] !== null)
    .map((key) => [key, manifest[key]] as [string, unknown]);
});

const statusMeta = (status: string) => {
  const values: Record<string, { label: string; classes: string }> = {
    draft: { label: '草稿', classes: 'bg-surface-sunken text-text-secondary' },
    validated: { label: '已校验', classes: 'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300' },
    pending_review: { label: '待审核', classes: 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300' },
    published: { label: '已发布', classes: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300' },
    disabled: { label: '已停用', classes: 'bg-orange-50 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300' },
    rejected: { label: '已驳回', classes: 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300' },
    archived: { label: '已归档', classes: 'bg-surface-sunken text-text-secondary' },
  };
  return values[status] || { label: status || '未知', classes: 'bg-surface-sunken text-text-secondary' };
};

const visibilityLabel = (value: string) =>
  ({ public: '公开', private: '私有', restricted: '受限' }[value] || value);

const formatDate = (value?: string | null) =>
  value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-';

const formatSize = (bytes = 0) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

let noticeTimer: number | undefined;
const notify = (message: string) => {
  notice.value = message;
  window.clearTimeout(noticeTimer);
  noticeTimer = window.setTimeout(() => {
    notice.value = '';
  }, 2600);
};

const messageOf = (reason: unknown) =>
  reason instanceof Error ? reason.message : '操作失败，请稍后重试';

const loadSkills = async () => {
  const selectedId = selectedSkill.value?.skill_id;
  try {
    error.value = '';
    const result = await SkillAdminAPI.list();
    skills.value = result.skills || [];
    if (selectedId) {
      selectedSkill.value = skills.value.find((item) => item.skill_id === selectedId) || null;
    }
  } catch (reason) {
    error.value = messageOf(reason);
  }
};

const loadAgents = async () => {
  try {
    agents.value = (await SkillAdminAPI.listAgents()).filter((agent) => agent.is_active);
    if (!selectedAgentCode.value && agents.value.length) {
      selectedAgentCode.value = agents.value[0].agent_code;
    }
  } catch (reason) {
    error.value = messageOf(reason);
  }
};

const refreshAll = async () => {
  loading.value = true;
  await Promise.all([loadSkills(), loadAgents()]);
  await loadAgentBindings();
  loading.value = false;
};

const openSkill = async (skill: ManagedSkill) => {
  selectedSkill.value = skill;
  versions.value = [];
  grants.value = [];
  versionsLoading.value = true;
  grantsLoading.value = true;
  try {
    const [versionResult, grantResult] = await Promise.all([
      SkillAdminAPI.versions(skill.skill_id),
      SkillAdminAPI.listGrants(skill.skill_id),
    ]);
    versions.value = versionResult.versions || [];
    grants.value = grantResult.grants || [];
  } catch (reason) {
    error.value = messageOf(reason);
  } finally {
    versionsLoading.value = false;
    grantsLoading.value = false;
  }
};

const refreshSelected = async () => {
  await loadSkills();
  if (selectedSkill.value) {
    const [versionResult, grantResult] = await Promise.all([
      SkillAdminAPI.versions(selectedSkill.value.skill_id),
      SkillAdminAPI.listGrants(selectedSkill.value.skill_id),
    ]);
    versions.value = versionResult.versions || [];
    grants.value = grantResult.grants || [];
  }
};

const runAction = async (key: string, task: () => Promise<unknown>, success: string) => {
  actionKey.value = key;
  try {
    error.value = '';
    await task();
    await refreshSelected();
    notify(success);
  } catch (reason) {
    error.value = messageOf(reason);
  } finally {
    actionKey.value = '';
  }
};

const publishVersion = (version: SkillVersion) => {
  if (!selectedSkill.value || !confirm(`确定发布版本 ${version.version} 吗？发布后将成为活动版本。`)) return;
  void runAction(
    `publish:${version.version_id}`,
    () => SkillAdminAPI.publish(selectedSkill.value!.skill_id, version.version_id),
    '版本已发布',
  );
};

const submitVersion = (version: SkillVersion) => {
  if (!selectedSkill.value) return;
  void runAction(
    `submit:${version.version_id}`,
    () => SkillAdminAPI.submit(selectedSkill.value!.skill_id, version.version_id),
    '版本已提交审核',
  );
};

const rejectVersion = (version: SkillVersion) => {
  if (!selectedSkill.value) return;
  const comment = window.prompt('请输入驳回原因：', version.review_comment || '');
  if (comment === null) return;
  void runAction(
    `reject:${version.version_id}`,
    () => SkillAdminAPI.reject(selectedSkill.value!.skill_id, version.version_id, comment.trim()),
    '版本已驳回',
  );
};

const downloadVersion = async (version: SkillVersion) => {
  if (!selectedSkill.value) return;
  actionKey.value = `download:${version.version_id}`;
  try {
    const result = await SkillAdminAPI.download(selectedSkill.value.skill_id, version.version_id);
    window.open(result.url, '_blank', 'noopener,noreferrer');
  } catch (reason) {
    error.value = messageOf(reason);
  } finally {
    actionKey.value = '';
  }
};

const setSkillEnabled = (enabled: boolean) => {
  if (!selectedSkill.value) return;
  const verb = enabled ? '启用' : '停用';
  if (!confirm(`确定${verb} Skill「${selectedSkill.value.display_name}」吗？`)) return;
  void runAction(
    `${enabled ? 'enable' : 'disable'}:${selectedSkill.value.skill_id}`,
    () => SkillAdminAPI.setEnabled(selectedSkill.value!.skill_id, enabled),
    `Skill 已${verb}`,
  );
};

const archiveSkill = () => {
  if (!selectedSkill.value || !confirm('确定归档此 Skill 吗？归档后不会再参与运行时选择。')) return;
  void runAction(
    `archive:${selectedSkill.value.skill_id}`,
    () => SkillAdminAPI.archive(selectedSkill.value!.skill_id),
    'Skill 已归档',
  );
};

const handleFileChange = (event: Event) => {
  const input = event.target as HTMLInputElement;
  uploadFile.value = input.files?.[0] || null;
};

const uploadSkill = async () => {
  if (!uploadFile.value) {
    error.value = '请选择一个 ZIP 格式的 Skill 包';
    return;
  }
  if (!uploadFile.value.name.toLowerCase().endsWith('.zip')) {
    error.value = '仅支持 ZIP 格式的 Skill 包';
    return;
  }
  uploading.value = true;
  try {
    error.value = '';
    const result = await SkillAdminAPI.upload(uploadFile.value, visibility.value) as ManagedSkill;
    showUpload.value = false;
    uploadFile.value = null;
    await loadSkills();
    const uploaded = skills.value.find((item) => item.skill_id === result.skill_id);
    if (uploaded) await openSkill(uploaded);
    notify('Skill 包校验并上传成功');
  } catch (reason) {
    error.value = messageOf(reason);
  } finally {
    uploading.value = false;
  }
};

const loadAgentBindings = async () => {
  if (!selectedAgentCode.value) {
    bindingDraft.value = {};
    compatibleSkillIds.value = new Set();
    return;
  }
  bindingsLoading.value = true;
  try {
    const result = await SkillAdminAPI.listAgentSkills(selectedAgentCode.value);
    compatibleSkillIds.value = new Set(result.compatible_skill_ids || []);
    bindingDraft.value = Object.fromEntries(
      (result.skills || []).map((binding) => [
        binding.skill_id,
        {
          skill_id: binding.skill_id,
          is_enabled: binding.is_enabled,
          is_required: binding.is_required,
          default_selected: binding.default_selected,
          is_selectable: binding.is_selectable,
          sort_order: binding.sort_order,
        },
      ]),
    );
  } catch (reason) {
    bindingDraft.value = {};
    compatibleSkillIds.value = new Set();
    error.value = messageOf(reason);
  } finally {
    bindingsLoading.value = false;
  }
};

const toggleBinding = (skill: ManagedSkill) => {
  const next = { ...bindingDraft.value };
  if (next[skill.skill_id]) {
    delete next[skill.skill_id];
  } else {
    next[skill.skill_id] = {
      skill_id: skill.skill_id,
      is_enabled: true,
      is_required: false,
      default_selected: false,
      is_selectable: true,
      sort_order: Object.keys(next).length,
    };
  }
  bindingDraft.value = next;
};

const updateBinding = (
  skillId: string,
  field: keyof Omit<AgentSkillBindingInput, 'skill_id' | 'sort_order'>,
  checked: boolean,
) => {
  const current = bindingDraft.value[skillId];
  if (!current) return;
  const next = { ...current, [field]: checked };
  if (field === 'is_required' && checked) next.is_enabled = true;
  bindingDraft.value = { ...bindingDraft.value, [skillId]: next };
};

const saveBindings = async () => {
  if (!selectedAgentCode.value) return;
  savingBindings.value = true;
  try {
    const values = Object.values(bindingDraft.value)
      .sort((a, b) => a.sort_order - b.sort_order)
      .map((item, index) => ({ ...item, sort_order: index }));
    await SkillAdminAPI.setAgentSkills(selectedAgentCode.value, values);
    await loadAgentBindings();
    notify('智能体 Skill 绑定已保存');
  } catch (reason) {
    error.value = messageOf(reason);
  } finally {
    savingBindings.value = false;
  }
};

const toggleGrantPermission = (permission: SkillGrant['permission']) => {
  grantPermissions.value = grantPermissions.value.includes(permission)
    ? grantPermissions.value.filter((item) => item !== permission)
    : [...grantPermissions.value, permission];
};

const saveGrant = async () => {
  if (!selectedSkill.value || !grantSubjectId.value.trim() || grantPermissions.value.length === 0) {
    error.value = '请填写授权主体并至少选择一项权限';
    return;
  }
  savingGrant.value = true;
  try {
    await SkillAdminAPI.replaceGrants(selectedSkill.value.skill_id, {
      subject_type: grantSubjectType.value,
      subject_id: grantSubjectId.value.trim(),
      permissions: grantPermissions.value,
      effect: grantEffect.value,
      expires_at: grantExpiresAt.value || null,
    });
    const result = await SkillAdminAPI.listGrants(selectedSkill.value.skill_id);
    grants.value = result.grants || [];
    grantExpiresAt.value = '';
    notify('Skill 授权已保存');
  } catch (reason) {
    error.value = messageOf(reason);
  } finally {
    savingGrant.value = false;
  }
};

const deleteGrantGroup = async (ids: number[]) => {
  if (!selectedSkill.value || !confirm('确定删除该主体的这组授权吗？')) return;
  actionKey.value = `grant:${ids.join(',')}`;
  try {
    await Promise.all(ids.map((id) => SkillAdminAPI.deleteGrant(selectedSkill.value!.skill_id, id)));
    const result = await SkillAdminAPI.listGrants(selectedSkill.value.skill_id);
    grants.value = result.grants || [];
    notify('授权已删除');
  } catch (reason) {
    error.value = messageOf(reason);
  } finally {
    actionKey.value = '';
  }
};

onMounted(refreshAll);
</script>

<template>
  <div class="max-w-7xl mx-auto space-y-8">
    <div class="flex flex-col xl:flex-row xl:items-end justify-between gap-5 border-b border-border-subtle pb-5">
      <div>
        <div class="flex items-center gap-2 text-accent text-xs font-bold mb-2 ml-0.5">
          <Sparkles class="w-4 h-4 animate-pulse" />
          <span>能力扩展中心</span>
        </div>
        <h2 class="text-3xl font-extrabold tracking-tight text-text-primary">Skills 管理</h2>
        <p class="text-text-secondary mt-2 text-sm">上传、审核、发布 Skill 托管能力包，并动态调整对应智能体与用户的调用和关联权限。</p>
      </div>
      <div class="flex flex-wrap items-center gap-3">
        <button
          class="h-10 px-4 py-2.5 rounded-2xl border border-border-subtle bg-surface/70 text-xs font-bold flex items-center gap-2 hover:bg-surface-hover transition cursor-pointer"
          @click="refreshAll"
        >
          <RefreshCw class="w-4 h-4" />
          <span>刷新</span>
        </button>
        <button
          class="h-10 px-5 py-2.5 rounded-2xl bg-accent hover:bg-accent-hover text-white text-xs font-bold flex items-center gap-2 shadow-sm transition hover-lift cursor-pointer"
          @click="showUpload = true"
        >
          <Plus class="w-4 h-4" />
          <span>上传 Skill</span>
        </button>
      </div>
    </div>

    <div v-if="notice" class="fixed top-6 right-6 z-[70] px-5 py-3 bg-emerald-600 text-white rounded-xl shadow-sm flex items-center gap-2">
      <Check class="w-4 h-4" />
      {{ notice }}
    </div>

    <div v-if="error" class="rounded-2xl border border-red-200 bg-red-50 dark:bg-red-900/20 dark:border-red-800 px-5 py-4 flex items-start justify-between gap-4 text-red-700 dark:text-red-300">
      <div class="flex gap-2">
        <XCircle class="w-5 h-5 mt-0.5 shrink-0" />
        <span class="text-sm">{{ error }}</span>
      </div>
      <button @click="error = ''"><X class="w-4 h-4" /></button>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_360px] gap-6">
      <section class="space-y-5">
        <div class="bg-surface/80 border border-border-subtle rounded-3xl p-4 flex flex-col md:flex-row gap-3">
          <div class="relative flex-1">
            <Search class="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-text-tertiary" />
            <input
              v-model="query"
              class="w-full pl-10 pr-4 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs text-text-primary"
              placeholder="搜索名称、Key、Domain..."
            />
          </div>
          <select
            v-model="statusFilter"
            class="px-4 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle outline-none text-xs text-text-secondary font-bold"
          >
            <option v-for="option in statusOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </div>

        <div v-if="loading" class="py-24 flex flex-col items-center text-text-tertiary font-semibold text-xs">
          <Loader2 class="w-9 h-9 animate-spin text-accent mb-3" />
          <span>正在加载 Skills...</span>
        </div>
        <div v-else-if="filteredSkills.length === 0" class="py-24 border-2 border-dashed border-border-subtle rounded-3xl text-center text-text-tertiary">
          <PackageOpen class="w-14 h-14 mx-auto mb-4 text-text-tertiary" />
          <p class="font-bold text-text-secondary text-sm">暂无匹配的 Skill</p>
          <p class="text-xs mt-1">可以上传 ZIP 包创建第一个托管 Skill。</p>
        </div>
        <div v-else class="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <button
            v-for="skill in filteredSkills"
            :key="skill.skill_id"
            class="text-left bg-surface/85 border rounded-3xl p-5 hover:shadow-sm hover:border-accent/30 transition-all duration-300 group cursor-pointer"
            :class="selectedSkill?.skill_id === skill.skill_id ? 'border-accent ring-4 ring-accent/10' : 'border-border-subtle hover:border-accent/30'"
            @click="openSkill(skill)"
          >
            <div class="flex items-start justify-between gap-3">
              <div class="p-3 rounded-2xl bg-accent-soft text-accent">
                <Sparkles class="w-5 h-5" />
              </div>
              <span class="px-2.5 py-0.5 rounded-full text-[10px] font-bold" :class="statusMeta(skill.status).classes">
                {{ statusMeta(skill.status).label }}
              </span>
            </div>
            <h3 class="mt-4 font-extrabold text-text-primary text-sm truncate">{{ skill.display_name }}</h3>
            <p class="text-[10px] font-mono font-bold text-accent mt-1 truncate">{{ skill.skill_key }}</p>
            <p class="text-xs text-text-secondary mt-3 line-clamp-2 h-9 leading-relaxed">{{ skill.display_description || skill.description || '暂无说明' }}</p>
            <div class="mt-4 pt-4 border-t border-border-subtle flex items-center justify-between text-[11px] font-bold text-text-tertiary">
              <span class="font-mono">{{ skill.domain }}</span>
              <span class="flex items-center gap-1 group-hover:text-accent transition-colors">详情 <ChevronRight class="w-3.5 h-3.5" /></span>
            </div>
          </button>
        </div>
      </section>

      <aside class="bg-surface/80 border border-border-subtle rounded-3xl p-5 h-fit xl:sticky xl:top-6 shadow-sm">
        <div class="flex items-center gap-2 mb-1">
          <Bot class="w-5 h-5 text-accent" />
          <h3 class="font-extrabold text-text-primary text-sm">智能体绑定</h3>
        </div>
        <p class="text-xs text-text-tertiary mb-4 font-medium">指定并配置当前智能体所能够调用的已发布 Skill。</p>
        <select
          v-model="selectedAgentCode"
          class="w-full px-3 py-2.5 rounded-2xl bg-surface-sunken border border-border-subtle text-xs font-bold outline-none text-text-secondary"
          @change="loadAgentBindings"
        >
          <option value="" disabled>选择需要配置的智能体</option>
          <option v-for="agent in agents" :key="agent.agent_code" :value="agent.agent_code">
            {{ agent.agent_name }} ({{ agent.agent_code }})
          </option>
        </select>

        <div v-if="bindingsLoading" class="py-12 flex justify-center"><Loader2 class="w-6 h-6 animate-spin text-accent" /></div>
        <div v-else-if="!selectedAgentCode" class="py-12 text-center text-xs text-text-tertiary italic">请在上方下拉菜单选择智能体进行关联。</div>
        <div v-else class="mt-4 space-y-3 max-h-[460px] overflow-y-auto pr-1 custom-scrollbar">
          <div v-for="skill in bindableSkills" :key="skill.skill_id" class="rounded-2xl border border-border-subtle p-3.5 hover:border-accent/30 transition-all duration-200">
            <label class="flex items-start gap-3 cursor-pointer select-none">
              <input
                type="checkbox"
                class="mt-0.5 h-4 w-4 rounded border-border-subtle text-accent focus:ring-accent/30 cursor-pointer"
                :checked="!!bindingDraft[skill.skill_id]"
                @change="toggleBinding(skill)"
              />
              <span class="min-w-0 flex-1">
                <span class="block text-xs font-bold text-text-secondary truncate">{{ skill.display_name }}</span>
                <span class="block text-[10px] text-text-tertiary truncate font-bold font-mono mt-0.5">
                  {{ skill.domain }}
                  <span v-if="skill.status !== 'published'" class="text-orange-500"> · 已失效</span>
                </span>
              </span>
            </label>
            
            <div v-if="bindingDraft[skill.skill_id]" class="mt-3 pt-3 border-t border-border-subtle grid grid-cols-2 gap-2 text-[10px] font-bold text-text-secondary">
              <label class="flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" :checked="bindingDraft[skill.skill_id].is_enabled" @change="updateBinding(skill.skill_id, 'is_enabled', ($event.target as HTMLInputElement).checked)" class="h-3.5 w-3.5 rounded border-border-subtle text-accent" />
                <span>启用</span>
              </label>
              <label class="flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" :checked="bindingDraft[skill.skill_id].is_required" @change="updateBinding(skill.skill_id, 'is_required', ($event.target as HTMLInputElement).checked)" class="h-3.5 w-3.5 rounded border-border-subtle text-accent" />
                <span>必选</span>
              </label>
              <label class="flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" :checked="bindingDraft[skill.skill_id].default_selected" @change="updateBinding(skill.skill_id, 'default_selected', ($event.target as HTMLInputElement).checked)" class="h-3.5 w-3.5 rounded border-border-subtle text-accent" />
                <span>默认选择</span>
              </label>
              <label class="flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" :checked="bindingDraft[skill.skill_id].is_selectable" @change="updateBinding(skill.skill_id, 'is_selectable', ($event.target as HTMLInputElement).checked)" class="h-3.5 w-3.5 rounded border-border-subtle text-accent" />
                <span>允许自选</span>
              </label>
            </div>
          </div>
          <div v-if="bindableSkills.length === 0" class="py-8 text-center text-xs text-text-tertiary italic">当前智能体暂无兼容的已发布 Skill。</div>
        </div>
        
        <button
          v-if="selectedAgentCode"
          class="w-full mt-4 h-10 px-5 rounded-2xl bg-accent text-accent-fg text-xs font-bold disabled:opacity-50 flex items-center justify-center gap-2 cursor-pointer transition shadow-sm hover:bg-accent-hover"
          :disabled="savingBindings"
          @click="saveBindings"
        >
          <Loader2 v-if="savingBindings" class="w-4 h-4 animate-spin" />
          <ShieldCheck v-else class="w-4 h-4" />
          <span>保存绑定配置</span>
        </button>
      </aside>
    </div>

    <Transition name="pop">
    <div v-if="selectedSkill" class="fixed inset-0 z-50 flex items-end justify-center p-0 sm:items-center sm:p-5">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" @click="selectedSkill = null"></div>
      <div
        class="pop-panel relative flex h-[94dvh] w-full max-w-5xl flex-col overflow-hidden rounded-t-[1.75rem] border border-white/60 bg-surface/88 shadow-2xl backdrop-blur-2xl dark:border-white/10 dark:bg-surface/92 sm:h-[min(88vh,900px)] sm:rounded-[1.75rem]"
        role="dialog"
        aria-modal="true"
        aria-labelledby="skill-detail-title"
      >
        <!-- Sticky Header -->
        <div class="z-10 flex flex-none items-start justify-between border-b border-border-subtle bg-surface/75 px-6 py-5 backdrop-blur-xl">
          <div class="min-w-0">
            <h3 id="skill-detail-title" class="text-xl font-extrabold text-text-primary truncate">{{ selectedSkill.display_name }}</h3>
            <p class="text-[10px] font-mono font-bold text-accent mt-1">{{ selectedSkill.skill_key }}</p>
          </div>
          <button class="p-2 rounded-xl hover:bg-surface-hover cursor-pointer" aria-label="关闭技能详情" @click="selectedSkill = null">
            <X class="w-4.5 h-4.5" />
          </button>
        </div>

        <div class="min-h-0 flex-1 space-y-7 overflow-y-auto p-6 custom-scrollbar sm:p-7">
          <!-- Metadata Cards -->
          <div class="grid grid-cols-2 gap-3 text-sm">
            <div class="rounded-2xl bg-surface-sunken border border-border-subtle p-4">
              <p class="text-[10px] font-bold text-text-tertiary uppercase tracking-widest mb-1.5">状态</p>
              <span class="inline-flex px-2.5 py-0.5 rounded-full text-[10px] font-bold" :class="statusMeta(selectedSkill.status).classes">{{ statusMeta(selectedSkill.status).label }}</span>
            </div>
            <div class="rounded-2xl bg-surface-sunken border border-border-subtle p-4">
              <p class="text-[10px] font-bold text-text-tertiary uppercase tracking-widest mb-1.5">可见范围</p>
              <p class="font-bold text-text-primary text-xs">{{ visibilityLabel(selectedSkill.visibility) }}</p>
            </div>
            <div class="rounded-2xl bg-surface-sunken border border-border-subtle p-4">
              <p class="text-[10px] font-bold text-text-tertiary uppercase tracking-widest mb-1.5">应用域 Domain</p>
              <p class="font-mono text-[10px] font-bold break-all text-text-primary">{{ selectedSkill.domain }}</p>
            </div>
            <div class="rounded-2xl bg-surface-sunken border border-border-subtle p-4">
              <p class="text-[10px] font-bold text-text-tertiary uppercase tracking-widest mb-1.5">来源类型</p>
              <p class="font-bold text-text-primary text-xs">{{ selectedSkill.source_type === 'builtin' ? '系统内置' : 'ZIP上传托管' }}</p>
            </div>
          </div>

          <div>
            <p class="text-xs font-bold text-text-tertiary uppercase tracking-widest mb-2 ml-1">详细说明</p>
            <p class="text-xs leading-relaxed text-text-secondary bg-surface-sunken border border-border-subtle rounded-2xl p-4">{{ selectedSkill.display_description || selectedSkill.description || '暂无说明文件' }}</p>
          </div>

          <div class="border-t border-border-subtle pt-5">
            <div class="flex items-center justify-between mb-2">
              <p class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">运行时 Manifest</p>
              <span v-if="activeVersion" class="text-[10px] font-mono text-text-tertiary">v{{ activeVersion.version }}</span>
            </div>
            <div v-if="manifestEntries.length" class="space-y-2">
              <div v-for="entry in manifestEntries" :key="entry[0]" class="rounded-xl bg-surface-sunken border border-border-subtle px-3 py-2">
                <p class="text-[10px] font-bold text-text-secondary">{{ entry[0] }}</p>
                <pre class="mt-1 whitespace-pre-wrap break-words text-[11px] text-text-secondary">{{ typeof entry[1] === 'string' ? entry[1] : JSON.stringify(entry[1], null, 2) }}</pre>
              </div>
            </div>
            <p v-else class="text-xs text-text-tertiary italic rounded-xl bg-surface-sunken p-3">当前没有可读取的活动版本 Manifest。</p>
          </div>

          <div class="flex flex-wrap gap-2 border-t border-border-subtle pt-5">
            <button v-if="selectedSkill.status === 'disabled'" class="h-9 px-4 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold shadow-sm cursor-pointer transition" @click="setSkillEnabled(true)">重新启用</button>
            <button v-if="selectedSkill.status === 'published'" class="h-9 px-4 rounded-xl bg-orange-500 hover:bg-orange-650 text-white text-xs font-bold shadow-sm cursor-pointer transition" @click="setSkillEnabled(false)">强行停用</button>
            <button v-if="selectedSkill.status !== 'archived'" class="h-9 px-4 rounded-xl border border-red-200 dark:border-red-900/30 text-red-600 dark:text-red-400 text-xs font-bold flex items-center gap-1.5 hover:bg-red-50 dark:hover:bg-red-950/15 cursor-pointer transition" @click="archiveSkill">
              <Archive class="w-4 h-4" />
              <span>归档</span>
            </button>
          </div>

          <!-- Version histories -->
          <div class="space-y-4 border-t border-border-subtle pt-5">
            <div class="flex items-center justify-between">
              <h4 class="font-extrabold text-text-primary text-sm">版本更迭记录</h4>
              <span class="text-xs text-text-tertiary font-semibold">{{ versions.length }} 个迭代版本</span>
            </div>
            
            <div v-if="versionsLoading" class="py-12 flex justify-center"><Loader2 class="w-7 h-7 animate-spin text-accent" /></div>
            <div v-else class="space-y-3">
              <div v-for="version in versions" :key="version.version_id" class="border border-border-subtle rounded-2xl p-4">
                <div class="flex items-start justify-between gap-3">
                  <div>
                    <div class="flex items-center gap-2">
                      <span class="font-extrabold text-sm text-text-primary">v{{ version.version }}</span>
                      <span v-if="selectedSkill.active_version_id === version.version_id" class="text-[9px] px-2 py-0.5 rounded-full bg-accent-soft text-accent font-bold border border-accent/20">线上生效版本</span>
                    </div>
                    <p class="text-[10px] text-text-tertiary mt-1 font-semibold">{{ formatDate(version.created_at) }} · 包体积: {{ formatSize(version.package_size) }}</p>
                  </div>
                  <span class="px-2.5 py-0.5 rounded-full text-[10px] font-bold" :class="statusMeta(version.status).classes">{{ statusMeta(version.status).label }}</span>
                </div>
                <p v-if="version.review_comment" class="mt-3 text-xs text-red-650 bg-red-50/50 dark:bg-red-950/20 border border-red-200/10 rounded-xl px-3 py-2 font-semibold">审核退回意见：{{ version.review_comment }}</p>
                
                <div class="mt-4 flex flex-wrap gap-2">
                  <button v-if="['validated', 'rejected'].includes(version.status)" class="h-8 px-3 rounded-lg bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-400 text-xs font-bold cursor-pointer hover:bg-amber-100/50 transition" @click="submitVersion(version)">提交审核</button>
                  <button v-if="['validated', 'pending_review'].includes(version.status)" class="h-8 px-3 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold cursor-pointer transition" @click="publishVersion(version)">发布上线</button>
                  <button v-if="version.status === 'pending_review'" class="h-8 px-3 rounded-lg bg-red-50 dark:bg-red-950/30 text-red-655 text-xs font-bold cursor-pointer hover:bg-red-100/50 transition" @click="rejectVersion(version)">驳回</button>
                  
                  <button class="h-8 px-3 rounded-lg bg-surface-sunken text-text-secondary text-xs font-bold flex items-center gap-1.5 cursor-pointer hover:bg-surface-hover transition" @click="downloadVersion(version)">
                    <Loader2 v-if="actionKey === `download:${version.version_id}`" class="w-3.5 h-3.5 animate-spin" />
                    <Download v-else class="w-3.5 h-3.5" />
                    <span>下载 ZIP</span>
                  </button>
                </div>
              </div>
              <div v-if="versions.length === 0" class="py-10 text-center text-xs text-text-tertiary italic">暂无任何能力版本提交记录。</div>
            </div>
          </div>

          <!-- ACL Access Control -->
          <div class="space-y-4 border-t border-border-subtle pt-5">
            <div class="flex items-center gap-2">
              <Users class="w-4.5 h-4.5 text-accent" />
              <h4 class="font-extrabold text-text-primary text-sm">用户/角色调用授权</h4>
            </div>
            
            <div class="rounded-2xl bg-surface-sunken border border-border-subtle p-4 space-y-3.5">
              <div class="grid grid-cols-2 gap-3">
                <select v-model="grantSubjectType" class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none text-xs font-bold text-text-secondary">
                  <option value="role">按账号角色授权</option>
                  <option value="user">按特定用户 ID 授权</option>
                </select>
                <input
                  v-model="grantSubjectId"
                  class="h-10 px-3 bg-surface border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-xs font-semibold text-text-primary"
                  :placeholder="grantSubjectType === 'role' ? 'user 或 admin' : '请输入精确用户 ID'"
                />
              </div>
              <div class="flex flex-wrap gap-4 text-xs font-bold text-text-secondary items-center">
                <span class="text-[10px] uppercase tracking-widest text-text-tertiary mr-1">授予权限:</span>
                <label v-for="permission in (['view', 'use', 'edit', 'manage'] as const)" :key="permission" class="flex items-center gap-1.5 cursor-pointer">
                  <input type="checkbox" :checked="grantPermissions.includes(permission)" @change="toggleGrantPermission(permission)" class="h-3.5 w-3.5 rounded border-border-subtle text-accent focus:ring-accent/30" />
                  <span>{{ permission }}</span>
                </label>
                <select v-model="grantEffect" class="ml-auto h-8 px-2 rounded-lg bg-surface border border-border-subtle text-xs font-bold text-text-secondary">
                  <option value="allow">允许调用</option>
                  <option value="deny">拒绝调用</option>
                </select>
              </div>
              <label class="block text-[10px] font-bold uppercase tracking-widest text-text-tertiary">
                授权到期时间（可选）
                <input v-model="grantExpiresAt" type="datetime-local" class="mt-1.5 h-9 w-full px-3 rounded-xl bg-surface border border-border-subtle text-xs font-semibold text-text-secondary" />
              </label>
              <button class="w-full h-10 px-5 rounded-xl bg-accent text-white text-xs font-bold hover:bg-accent-hover shadow-sm transition disabled:opacity-50 cursor-pointer" :disabled="savingGrant" @click="saveGrant">
                <span>{{ savingGrant ? '保存授权中...' : '增加并发布此授权' }}</span>
              </button>
            </div>
            
            <div v-if="grantsLoading" class="py-8 flex justify-center"><Loader2 class="w-6 h-6 animate-spin text-accent" /></div>
            <div v-else class="space-y-2">
              <div v-for="group in grantGroups" :key="`${group.subject_type}:${group.subject_id}:${group.effect}`" class="border border-border-subtle rounded-2xl px-4 py-3 flex items-center justify-between gap-3 bg-surface/50">
                <div class="min-w-0">
                  <p class="text-xs font-extrabold text-text-primary truncate">针对: {{ group.subject_type === 'role' ? '系统角色' : '独立用户' }} · <span class="font-mono text-xs text-accent">{{ group.subject_id }}</span></p>
                  <p class="text-[10px] font-bold mt-1" :class="group.effect === 'allow' ? 'text-emerald-655' : 'text-red-500'">
                    策略: [{{ group.effect === 'allow' ? '放行' : '阻断' }}] -> {{ group.permissions.join(', ') }}
                  </p>
                </div>
                <button class="p-2 rounded-lg text-text-tertiary hover:text-red-650 hover:bg-red-55/20 dark:hover:bg-red-950/20 transition cursor-pointer" @click="deleteGrantGroup(group.ids)"><Trash2 class="w-4 h-4" /></button>
              </div>
              <p v-if="grantGroups.length === 0" class="py-6 text-center text-xs text-text-tertiary italic">未发现额外关联的 ACL 白名单/黑名单。</p>
            </div>
          </div>
        </div>
      </div>
    </div>
    </Transition>

    <Transition name="pop">
    <div v-if="showUpload" class="fixed inset-0 z-[60] flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="上传 Skill 插件">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="showUpload = false"></div>
      <div class="pop-panel relative w-full max-w-lg rounded-3xl border border-white/55 bg-surface/90 p-7 shadow-2xl backdrop-blur-2xl dark:border-white/10 dark:bg-surface/95">
        <div class="flex items-start justify-between mb-6">
          <div>
            <h3 class="text-lg font-bold text-text-primary">上传 Skill 插件压缩包</h3>
            <p class="text-xs text-text-tertiary mt-1">压缩包根目录下必须包含一个标准规范的 SKILL.md 文件。</p>
          </div>
          <button @click="showUpload = false" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition cursor-pointer"><X class="w-4.5 h-4.5" /></button>
        </div>
        
        <label class="block border-2 border-dashed border-border-subtle hover:border-accent rounded-3xl p-8 text-center cursor-pointer transition-all duration-250 bg-surface-sunken">
          <input class="hidden" type="file" accept=".zip,application/zip" @change="handleFileChange" />
          <UploadCloud class="w-10 h-10 mx-auto text-accent mb-3" />
          <p class="font-bold text-xs text-text-secondary truncate px-4">{{ uploadFile?.name || '点击此处选择 ZIP 文件' }}</p>
          <p class="text-[10px] text-text-tertiary mt-1.5 font-medium">如果 skill_key 重复，将会被作为该 Skill 的全新版本进行追加更迭。</p>
        </label>
        
        <div class="mt-5">
          <label class="block text-xs font-bold text-text-tertiary uppercase tracking-widest mb-2 ml-1">默认可见可见范围</label>
          <select v-model="visibility" class="w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none text-xs font-semibold text-text-secondary">
            <option value="private">私有 (仅所有者与获授权的 ACL 用户可见)</option>
            <option value="restricted">受限 (必须经过 ACL 明确授权才可调用)</option>
            <option value="public">公开 (平台所有用户均可见并可调用)</option>
          </select>
        </div>
        
        <button
          class="w-full mt-6 h-11 rounded-2xl bg-accent hover:bg-accent-hover text-white font-bold flex items-center justify-center gap-2 disabled:opacity-50 cursor-pointer shadow-sm transition hover-lift"
          :disabled="uploading"
          @click="uploadSkill"
        >
          <Loader2 v-if="uploading" class="w-4.5 h-4.5 animate-spin" />
          <FileArchive v-else class="w-4.5 h-4.5" />
          <span>{{ uploading ? '正在进行安全合规校验并上传...' : '校验并开始上传' }}</span>
        </button>
      </div>
    </div>
    </Transition>
  </div>
</template>
