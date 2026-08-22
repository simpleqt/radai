<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import {
  Check,
  Database,
  Eye,
  Loader2,
  Pencil,
  RefreshCw,
  Search,
  Shield,
  TableProperties,
  Trash2,
  Upload,
  X,
} from 'lucide-vue-next';
import { DatasetAdminAPI, DatasetPreviewResponse, ExperimentDataset } from '@/api/datasets';

const AGENT_OPTIONS = [
  { code: 'ne', label: '核电子学智能体' },
  { code: 'csh', label: '水文地质智能体' },
  { code: 'groundwater', label: '地下水项目分析助手' },
  { code: 'fmca', label: '基础力学课程智能体' },
];

const datasets = ref<ExperimentDataset[]>([]);
const loading = ref(false);
const saving = ref(false);
const searchQuery = ref('');
const statusFilter = ref('all');
const agentFilter = ref('all');
const isUploadOpen = ref(false);
const editing = ref<ExperimentDataset | null>(null);
const previewDataset = ref<ExperimentDataset | null>(null);
const previewData = ref<DatasetPreviewResponse | null>(null);
const previewLoading = ref(false);

const editForm = ref({
  name: '',
  description: '',
  domain: '',
  tagsText: '',
  agent_codes: [] as string[],
  is_public: true,
  status: 'active',
});

const uploadForm = ref({
  file: null as File | null,
  code: '',
  name: '',
  description: '',
  domain: '',
  tagsText: '',
  agent_codes: [] as string[],
  is_public: true,
});

const parseTags = (value: string) => {
  const seen = new Set<string>();
  return value
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter((item) => {
      if (!item || seen.has(item)) return false;
      seen.add(item);
      return true;
    });
};

const totalRows = (dataset: ExperimentDataset) =>
  Object.values(dataset.row_counts || {}).reduce((sum, value) => sum + Number(value || 0), 0);

const formatSize = (size?: number | null) => {
  if (!size) return '-';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
};

const statusLabel = (status: string) => ({
  active: '启用',
  archived: '归档',
  processing: '处理中',
  error: '异常',
}[status] || status);

const agentLabel = (code: string) =>
  AGENT_OPTIONS.find((item) => item.code === code)?.label || code;

const bindingLabel = (codes: string[]) =>
  codes.length ? codes.map(agentLabel).join('、') : '全部智能体';

const filteredDatasets = computed(() => {
  const keyword = searchQuery.value.trim().toLowerCase();
  return datasets.value.filter((dataset) => {
    const matchesKeyword = !keyword || [
      dataset.name,
      dataset.code,
      dataset.description,
      dataset.domain,
      ...(dataset.tags || []),
      ...(dataset.agent_codes || []),
    ].some((value) => String(value || '').toLowerCase().includes(keyword));
    const matchesAgent = agentFilter.value === 'all'
      || (agentFilter.value === 'global' && (!dataset.agent_codes || dataset.agent_codes.length === 0))
      || (dataset.agent_codes || []).includes(agentFilter.value);
    return matchesKeyword && matchesAgent;
  });
});

const loadDatasets = async () => {
  loading.value = true;
  try {
    const res = await DatasetAdminAPI.list({ status: statusFilter.value });
    datasets.value = res.datasets || [];
  } catch (error: any) {
    alert(error?.message || '实验数据集加载失败');
  } finally {
    loading.value = false;
  }
};

const syncDataset = (next: ExperimentDataset) => {
  const index = datasets.value.findIndex((item) => item.id === next.id);
  if (index >= 0) datasets.value.splice(index, 1, next);
  else datasets.value.unshift(next);
};

const toggleAgent = (target: { agent_codes: string[] }, code: string) => {
  target.agent_codes = target.agent_codes.includes(code)
    ? target.agent_codes.filter((item) => item !== code)
    : [...target.agent_codes, code];
};

const openEdit = (dataset: ExperimentDataset) => {
  editing.value = dataset;
  editForm.value = {
    name: dataset.name,
    description: dataset.description || '',
    domain: dataset.domain || '',
    tagsText: (dataset.tags || []).join(', '),
    agent_codes: [...(dataset.agent_codes || [])],
    is_public: dataset.is_public,
    status: dataset.status || 'active',
  };
};

const saveEdit = async () => {
  if (!editing.value) return;
  if (!editForm.value.name.trim()) {
    alert('请填写数据集名称');
    return;
  }

  saving.value = true;
  try {
    const next = await DatasetAdminAPI.update(editing.value.id, {
      name: editForm.value.name.trim(),
      description: editForm.value.description.trim(),
      domain: editForm.value.domain.trim(),
      tags: parseTags(editForm.value.tagsText),
      agent_codes: editForm.value.agent_codes,
      is_public: editForm.value.is_public,
      status: editForm.value.status,
    });
    syncDataset(next);
    editing.value = null;
  } catch (error: any) {
    alert(error?.message || '保存失败');
  } finally {
    saving.value = false;
  }
};

const onUploadFile = (event: Event) => {
  const input = event.target as HTMLInputElement;
  uploadForm.value.file = input.files?.[0] || null;
  if (uploadForm.value.file && !uploadForm.value.name) {
    uploadForm.value.name = uploadForm.value.file.name.replace(/\.[^.]+$/, '');
  }
};

const uploadDataset = async () => {
  if (!uploadForm.value.file || !uploadForm.value.code.trim() || !uploadForm.value.name.trim()) {
    alert('请完整填写编码、名称并选择文件');
    return;
  }

  saving.value = true;
  try {
    const created = await DatasetAdminAPI.upload({
      file: uploadForm.value.file,
      code: uploadForm.value.code.trim(),
      name: uploadForm.value.name.trim(),
      description: uploadForm.value.description.trim(),
      domain: uploadForm.value.domain.trim(),
      tags: parseTags(uploadForm.value.tagsText),
      agent_codes: uploadForm.value.agent_codes,
      is_public: uploadForm.value.is_public,
    });
    syncDataset(created);
    uploadForm.value = {
      file: null,
      code: '',
      name: '',
      description: '',
      domain: '',
      tagsText: '',
      agent_codes: [],
      is_public: true,
    };
    isUploadOpen.value = false;
  } catch (error: any) {
    alert(error?.message || '上传失败');
  } finally {
    saving.value = false;
  }
};

const refreshSchema = async (dataset: ExperimentDataset) => {
  try {
    const res = await DatasetAdminAPI.refresh(dataset.id);
    syncDataset(res.dataset);
  } catch (error: any) {
    alert(error?.message || '刷新失败');
  }
};

const openPreview = async (dataset: ExperimentDataset) => {
  previewDataset.value = dataset;
  previewData.value = null;
  previewLoading.value = true;
  try {
    previewData.value = await DatasetAdminAPI.preview(dataset.id, { rows: 30 });
  } catch (error: any) {
    alert(error?.message || '预览失败');
    previewDataset.value = null;
  } finally {
    previewLoading.value = false;
  }
};

const deleteDataset = async (dataset: ExperimentDataset) => {
  if (!confirm(`确定删除实验数据集「${dataset.name}」吗？`)) return;
  const deleteFile = confirm('是否同时删除服务器上的数据文件？');
  try {
    await DatasetAdminAPI.delete(dataset.id, deleteFile);
    datasets.value = datasets.value.filter((item) => item.id !== dataset.id);
  } catch (error: any) {
    alert(error?.message || '删除失败');
  }
};

const tableColumns = (rows: Record<string, any>[]) => Object.keys(rows[0] || {}).slice(0, 12);

onMounted(loadDatasets);
</script>

<template>
  <div class="min-h-screen bg-surface-sunken text-text-primary space-y-8 flex flex-col">
    <!-- Header Banner -->
    <div class="flex flex-col gap-6 xl:flex-row xl:items-center xl:justify-between pb-6 border-b border-border-subtle">
      <div>
        <div class="flex items-center gap-3">
          <div class="flex h-10 w-10 items-center justify-center rounded-2xl bg-accent text-white shadow-sm">
            <TableProperties class="h-5 w-5" />
          </div>
          <div>
            <h2 class="text-3xl font-extrabold tracking-tight text-text-primary dark:text-white">实验数据管理</h2>
            <p class="mt-1 text-sm text-text-secondary">管辖实验数据可见性、智能体授权绑定、物理表结构重构与数据集预览。</p>
          </div>
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-3">
        <div class="relative min-w-0 md:w-64">
          <Search class="absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
          <input
            v-model="searchQuery"
            class="h-10 w-full rounded-2xl border border-border-subtle bg-surface/70 pl-10 pr-3.5 text-xs outline-none transition focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-primary shadow-sm"
            placeholder="搜索名称、编码、领域、标签..."
          />
        </div>
        
        <select
          v-model="statusFilter"
          @change="loadDatasets"
          class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-secondary shadow-sm"
        >
          <option value="all">全部状态</option>
          <option value="active">启用</option>
          <option value="archived">归档</option>
          <option value="processing">处理中</option>
          <option value="error">异常</option>
        </select>
        
        <select
          v-model="agentFilter"
          class="h-10 rounded-2xl border border-border-subtle bg-surface/70 px-3.5 text-xs outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-text-secondary shadow-sm"
        >
          <option value="all">全部绑定</option>
          <option value="global">全部智能体</option>
          <option v-for="agent in AGENT_OPTIONS" :key="agent.code" :value="agent.code">{{ agent.label }}</option>
        </select>
        
        <button
          class="inline-flex h-10 items-center justify-center gap-2 rounded-2xl bg-accent px-5 text-xs font-bold text-white transition hover:bg-accent-hover hover-lift shadow-sm"
          @click="isUploadOpen = true"
        >
          <Upload class="h-4 w-4" />
          <span>上传数据</span>
        </button>
      </div>
    </div>

    <!-- Table Container -->
    <div class="overflow-hidden rounded-3xl border border-border-subtle bg-surface/80 backdrop-blur-md shadow-sm">
      <div v-if="loading" class="flex flex-col items-center justify-center gap-3 py-20 text-text-secondary">
        <Loader2 class="h-8 w-8 animate-spin text-accent mb-2" />
        <span class="text-sm font-semibold tracking-wide">正在加载实验数据集...</span>
      </div>

      <div v-else-if="filteredDatasets.length === 0" class="flex flex-col items-center justify-center py-20 text-center text-text-secondary">
        <Database class="mb-4 h-12 w-12 text-text-tertiary" />
        <p class="font-bold text-text-primary mb-1">暂无匹配的实验数据集</p>
        <p class="text-xs text-text-tertiary mb-6">没有找到对应的实验记录。上传一个新的实验文件以激活监控。</p>
        <button 
          class="bg-accent-soft text-accent px-5 py-2.5 rounded-xl text-xs font-bold hover:bg-accent-soft transition-all" 
          @click="isUploadOpen = true"
        >
          上传新的实验数据
        </button>
      </div>

      <div v-else class="overflow-x-auto">
        <table class="min-w-full divide-y divide-border-subtle text-sm text-left">
          <thead class="bg-surface-sunken text-xs font-bold uppercase tracking-wider text-text-tertiary border-b border-border-subtle">
            <tr>
              <th class="px-6 py-4 font-extrabold">数据集</th>
              <th class="px-6 py-4 font-extrabold">领域 / 标签</th>
              <th class="px-6 py-4 font-extrabold">规模</th>
              <th class="px-6 py-4 font-extrabold">可见范围与授权</th>
              <th class="px-6 py-4 font-extrabold">状态</th>
              <th class="px-6 py-4 text-right font-extrabold">操作</th>
            </tr>
          </thead>
            <tbody class="divide-y divide-border-subtle">
              <tr v-for="dataset in filteredDatasets" :key="dataset.id" class="align-top hover:bg-surface-hover">
                <td class="px-4 py-4">
                  <p class="font-bold text-text-primary">{{ dataset.name }}</p>
                  <p class="mt-1 font-mono text-xs text-text-secondary">{{ dataset.code }}</p>
                  <p class="mt-2 line-clamp-2 max-w-md text-xs text-text-secondary">{{ dataset.description || '暂无描述' }}</p>
                </td>
                <td class="px-4 py-4">
                  <p class="text-sm font-medium">{{ dataset.domain || '-' }}</p>
                  <div class="mt-2 flex max-w-xs flex-wrap gap-1.5">
                    <span v-for="tag in dataset.tags" :key="tag" class="rounded-md bg-surface-sunken px-2 py-1 text-xs text-text-secondary">{{ tag }}</span>
                    <span v-if="!dataset.tags?.length" class="text-xs text-text-tertiary">无标签</span>
                  </div>
                </td>
                <td class="px-4 py-4 text-text-secondary">
                  <p>{{ totalRows(dataset) }} 行</p>
                  <p class="mt-1 text-xs text-text-secondary">{{ dataset.sheet_names?.length || 0 }} 个表 / {{ dataset.file_type }} / {{ formatSize(dataset.file_size) }}</p>
                  <p class="mt-1 text-xs text-text-tertiary">{{ dataset.storage_type }}</p>
                </td>
                <td class="px-4 py-4">
                  <div class="flex items-center gap-2 text-sm">
                    <Shield class="h-4 w-4" :class="dataset.is_public ? 'text-emerald-500' : 'text-amber-500'" />
                    <span>{{ dataset.is_public ? '用户可见' : '仅管理员可见' }}</span>
                  </div>
                  <p class="mt-2 max-w-xs text-xs text-text-secondary">{{ bindingLabel(dataset.agent_codes || []) }}</p>
                </td>
                <td class="px-4 py-4">
                  <span class="inline-flex rounded-md px-2 py-1 text-xs font-bold" :class="dataset.status === 'active' ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300' : 'bg-surface-sunken text-text-secondary'">
                    {{ statusLabel(dataset.status) }}
                  </span>
                </td>
                <td class="px-4 py-4">
                  <div class="flex justify-end gap-1.5">
                    <button class="rounded-lg p-2 text-text-secondary hover:bg-surface-hover hover:text-accent" title="预览数据" @click="openPreview(dataset)">
                      <Eye class="h-4 w-4" />
                    </button>
                    <button class="rounded-lg p-2 text-text-secondary hover:bg-surface-hover hover:text-accent" title="刷新表结构" @click="refreshSchema(dataset)">
                      <RefreshCw class="h-4 w-4" />
                    </button>
                    <button class="rounded-lg p-2 text-text-secondary hover:bg-surface-hover hover:text-accent" title="编辑权限与绑定" @click="openEdit(dataset)">
                      <Pencil class="h-4 w-4" />
                    </button>
                    <button class="rounded-lg p-2 text-text-secondary hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-900/20" title="删除数据集" @click="deleteDataset(dataset)">
                      <Trash2 class="h-4 w-4" />
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <Transition name="pop">
    <div v-if="editing" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="编辑实验数据">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="editing = null"></div>
      <div class="pop-panel relative max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-white/55 bg-surface/90 p-6 shadow-2xl backdrop-blur-2xl dark:border-white/10 dark:bg-surface/95">
        <div class="mb-5 flex items-center justify-between">
          <h3 class="text-lg font-extrabold">编辑实验数据</h3>
          <button class="rounded-lg p-2 hover:bg-surface-hover" @click="editing = null"><X class="h-5 w-5" /></button>
        </div>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="block">
            <span class="mb-1 block text-sm font-medium">名称</span>
            <input v-model="editForm.name" class="h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-medium">状态</span>
            <select v-model="editForm.status" class="h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent">
              <option value="active">启用</option>
              <option value="archived">归档</option>
              <option value="processing">处理中</option>
              <option value="error">异常</option>
            </select>
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-medium">领域</span>
            <input v-model="editForm.domain" class="h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-medium">标签</span>
            <input v-model="editForm.tagsText" class="h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent" placeholder="用逗号分隔" />
          </label>
          <label class="block md:col-span-2">
            <span class="mb-1 block text-sm font-medium">描述</span>
            <textarea v-model="editForm.description" class="h-24 w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm outline-none focus:border-accent"></textarea>
          </label>
        </div>

        <div class="mt-5 border-t border-border-subtle pt-5">
          <label class="flex items-center gap-3">
            <input v-model="editForm.is_public" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent" />
            <span class="text-sm font-medium">普通用户可见</span>
          </label>
          <div class="mt-4 grid gap-2 md:grid-cols-2">
            <label v-for="agent in AGENT_OPTIONS" :key="agent.code" class="flex items-center gap-3 rounded-lg border border-border-subtle px-3 py-2 text-sm">
              <input :checked="editForm.agent_codes.includes(agent.code)" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent" @change="toggleAgent(editForm, agent.code)" />
              <span>{{ agent.label }}</span>
            </label>
          </div>
          <p class="mt-2 text-xs text-text-secondary">不选择智能体时，数据集对全部智能体可用。</p>
        </div>

        <div class="mt-6 flex justify-end gap-3">
          <button class="rounded-lg bg-surface-sunken px-4 py-2 text-sm font-bold text-text-secondary hover:bg-surface-hover" @click="editing = null">取消</button>
          <button class="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-bold text-white hover:bg-accent-hover disabled:opacity-60" :disabled="saving" @click="saveEdit">
            <Loader2 v-if="saving" class="h-4 w-4 animate-spin" />
            <Check v-else class="h-4 w-4" />
            <span>保存</span>
          </button>
        </div>
      </div>
    </div>
    </Transition>

    <Transition name="pop">
    <div v-if="isUploadOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="上传实验数据">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="isUploadOpen = false"></div>
      <div class="pop-panel relative max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-white/55 bg-surface/90 p-6 shadow-2xl backdrop-blur-2xl dark:border-white/10 dark:bg-surface/95">
        <div class="mb-5 flex items-center justify-between">
          <h3 class="text-lg font-extrabold">上传实验数据</h3>
          <button class="rounded-lg p-2 hover:bg-surface-hover" @click="isUploadOpen = false"><X class="h-5 w-5" /></button>
        </div>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="block md:col-span-2">
            <span class="mb-1 block text-sm font-medium">文件</span>
            <input type="file" accept=".csv,.xlsx,.xls" class="w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm" @change="onUploadFile" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-medium">编码</span>
            <input v-model="uploadForm.code" class="h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent" placeholder="experiment_dataset_code" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-medium">名称</span>
            <input v-model="uploadForm.name" class="h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-medium">领域</span>
            <input v-model="uploadForm.domain" class="h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent" />
          </label>
          <label class="block">
            <span class="mb-1 block text-sm font-medium">标签</span>
            <input v-model="uploadForm.tagsText" class="h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent" placeholder="用逗号分隔" />
          </label>
          <label class="block md:col-span-2">
            <span class="mb-1 block text-sm font-medium">描述</span>
            <textarea v-model="uploadForm.description" class="h-24 w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm outline-none focus:border-accent"></textarea>
          </label>
        </div>
        <div class="mt-5 border-t border-border-subtle pt-5">
          <label class="flex items-center gap-3">
            <input v-model="uploadForm.is_public" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent" />
            <span class="text-sm font-medium">普通用户可见</span>
          </label>
          <div class="mt-4 grid gap-2 md:grid-cols-2">
            <label v-for="agent in AGENT_OPTIONS" :key="agent.code" class="flex items-center gap-3 rounded-lg border border-border-subtle px-3 py-2 text-sm">
              <input :checked="uploadForm.agent_codes.includes(agent.code)" type="checkbox" class="h-4 w-4 rounded border-border-subtle text-accent" @change="toggleAgent(uploadForm, agent.code)" />
              <span>{{ agent.label }}</span>
            </label>
          </div>
        </div>
        <div class="mt-6 flex justify-end gap-3">
          <button class="rounded-lg bg-surface-sunken px-4 py-2 text-sm font-bold text-text-secondary hover:bg-surface-hover" @click="isUploadOpen = false">取消</button>
          <button class="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-bold text-white hover:bg-accent-hover disabled:opacity-60" :disabled="saving" @click="uploadDataset">
            <Loader2 v-if="saving" class="h-4 w-4 animate-spin" />
            <Upload v-else class="h-4 w-4" />
            <span>上传并注册</span>
          </button>
        </div>
      </div>
    </div>
    </Transition>

    <Transition name="pop">
    <div v-if="previewDataset" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="实验数据预览">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="previewDataset = null"></div>
      <div class="pop-panel relative flex max-h-[90vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-white/55 bg-surface/90 shadow-2xl backdrop-blur-2xl dark:border-white/10 dark:bg-surface/95">
        <div class="flex items-center justify-between border-b border-border-subtle px-5 py-4">
          <div>
            <h3 class="font-extrabold">{{ previewDataset.name }}</h3>
            <p class="mt-1 font-mono text-xs text-text-secondary">{{ previewDataset.code }}</p>
          </div>
          <button class="rounded-lg p-2 hover:bg-surface-hover" @click="previewDataset = null"><X class="h-5 w-5" /></button>
        </div>
        <div class="overflow-auto p-5">
          <div v-if="previewLoading" class="flex items-center justify-center gap-3 py-16 text-text-secondary">
            <Loader2 class="h-5 w-5 animate-spin text-accent" />
            <span>正在读取数据预览...</span>
          </div>
          <div v-else-if="previewData" class="space-y-6">
            <section v-for="(rows, sheetName) in previewData.sheets" :key="sheetName">
              <div class="mb-2 flex items-center justify-between">
                <h4 class="font-bold">{{ sheetName }}</h4>
                <span class="text-xs text-text-secondary">{{ rows.length }} 行预览</span>
              </div>
              <div class="overflow-x-auto rounded-lg border border-border-subtle">
                <table class="min-w-full divide-y divide-border-subtle text-xs">
                  <thead class="bg-surface-sunken text-left">
                    <tr>
                      <th v-for="column in tableColumns(rows)" :key="column" class="px-3 py-2 font-bold text-text-secondary">{{ column }}</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-border-subtle">
                    <tr v-for="(row, index) in rows" :key="index">
                      <td v-for="column in tableColumns(rows)" :key="column" class="max-w-[220px] truncate px-3 py-2">{{ row[column] ?? '' }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>
          </div>
      </div>
    </div>
  </div>
    </Transition>
</template>
