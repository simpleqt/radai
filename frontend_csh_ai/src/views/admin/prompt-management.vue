<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { FileText, Layers, Loader2, Plus, RefreshCw, Save, Search, Wand2 } from 'lucide-vue-next';
import {
  PromptManagementAPI,
  type PromptCategory,
  type PromptCategoryInfo,
  type PromptTemplateDetail,
  type PromptTemplateSummary,
} from '@/api/prompt-management';
import SchemaStructuredEditor, {
  type SchemaValidationState,
} from '@/views/admin/components/SchemaStructuredEditor.vue';

const categories = ref<PromptCategoryInfo[]>([]);
const templates = ref<PromptTemplateSummary[]>([]);
const activeCategory = ref<PromptCategory | ''>('extraction_schema');
const activeTemplateId = ref('');
const query = ref('');
const loading = ref(false);
const detailLoading = ref(false);
const saving = ref(false);
const notice = ref('');
const error = ref('');
const mode = ref<'edit' | 'create'>('edit');
const schemaValidation = ref<SchemaValidationState>({ valid: true, errors: [] });

const form = ref({
  category: 'extraction_schema' as PromptCategory,
  template_id: '',
  label: '',
  subject: '',
  version: '1.0',
  content: '',
});

const activeCategoryInfo = computed(() =>
  categories.value.find((item) => item.category === activeCategory.value),
);
const filteredTemplates = computed(() => {
  const keyword = query.value.trim().toLowerCase();
  if (!keyword) return templates.value;
  return templates.value.filter((item) =>
    [item.label, item.template_id, item.subject, item.description]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(keyword)),
  );
});
const activeTemplate = computed(() =>
  templates.value.find((item) => item.template_id === activeTemplateId.value),
);

const showNotice = (message: string) => {
  notice.value = message;
  window.setTimeout(() => {
    notice.value = '';
  }, 2600);
};

const buildDefaultContent = () => {
  const templateId = form.value.template_id.trim() || 'custom_hydrogeology';
  const label = form.value.label.trim() || '自定义水文地质';
  const subject = form.value.subject.trim() || 'hydrogeology';
  const version = form.value.version.trim() || '1.0';
  return `schema_id: ${templateId}
label: ${label}
version: "${version}"
subject: ${subject}
min_entity_confidence: 55
min_relation_confidence: 55
candidate_confidence: 40

entity_types:
  term:
    description: 领域术语、概念或对象
  parameter:
    description: 带数值、单位或状态的参数

relation_types:
  co_occurs:
    description: 同一 chunk 中共同出现的弱关联
    min_confidence: 50

forbidden_relations: []
`;
};

const applyDetail = (detail: PromptTemplateDetail) => {
  mode.value = 'edit';
  activeCategory.value = detail.category;
  activeTemplateId.value = detail.template_id;
  form.value = {
    category: detail.category,
    template_id: detail.template_id,
    label: detail.label,
    subject: detail.subject || '',
    version: detail.version,
    content: detail.content,
  };
};

const selectTemplate = async (template: PromptTemplateSummary) => {
  detailLoading.value = true;
  error.value = '';
  try {
    applyDetail(await PromptManagementAPI.getTemplate(template.category, template.template_id));
  } catch (reason: any) {
    error.value = reason?.message || '加载模板详情失败';
  } finally {
    detailLoading.value = false;
  }
};

const loadTemplates = async (selectFirst = false) => {
  loading.value = true;
  error.value = '';
  try {
    templates.value = await PromptManagementAPI.templates(activeCategory.value);
    if (selectFirst || !activeTemplateId.value) {
      const preferred = templates.value.find((item) => item.template_id === 'hydrogeology') || templates.value[0];
      if (preferred) await selectTemplate(preferred);
      else startCreate();
    }
  } catch (reason: any) {
    error.value = reason?.message || '加载提示词模板失败';
  } finally {
    loading.value = false;
  }
};

const selectCategory = async (category: PromptCategory) => {
  activeCategory.value = category;
  activeTemplateId.value = '';
  mode.value = 'edit';
  await loadTemplates(true);
};

const startCreate = () => {
  mode.value = 'create';
  activeTemplateId.value = '';
  form.value = {
    category: (activeCategory.value || 'extraction_schema') as PromptCategory,
    template_id: 'custom_hydrogeology',
    label: '自定义水文地质',
    subject: 'hydrogeology',
    version: '1.0',
    content: '',
  };
  form.value.content = buildDefaultContent();
};

const saveTemplate = async () => {
  error.value = '';
  const templateId = form.value.template_id.trim();
  if (!templateId || !form.value.label.trim() || !form.value.content.trim()) {
    error.value = '请填写模板 ID、名称和内容';
    return;
  }
  if (!schemaValidation.value.valid) {
    error.value = schemaValidation.value.errors[0] || 'Schema 内容校验未通过';
    return;
  }
  saving.value = true;
  try {
    const detail = mode.value === 'create'
      ? await PromptManagementAPI.createTemplate({
          category: form.value.category,
          template_id: templateId,
          label: form.value.label.trim(),
          subject: form.value.subject.trim() || null,
          version: form.value.version.trim() || '1.0',
          content: form.value.content,
        })
      : await PromptManagementAPI.updateTemplate(form.value.category, templateId, form.value.content);
    await loadTemplates(false);
    applyDetail(detail);
    showNotice(mode.value === 'create' ? '提示词模板已创建' : '提示词模板已保存');
  } catch (reason: any) {
    error.value = reason?.message || '保存提示词模板失败';
  } finally {
    saving.value = false;
  }
};

onMounted(async () => {
  try {
    categories.value = await PromptManagementAPI.categories();
  } catch {
    categories.value = [{
      category: 'extraction_schema',
      label: '入库抽取 Schema',
      description: '管理自研入库的实体/关系抽取提示词模板。',
    }];
  }
  await loadTemplates(true);
});
</script>

<template>
  <div class="min-h-full space-y-6">
    <div class="flex flex-col gap-5 border-b border-border-subtle pb-6 lg:flex-row lg:items-center lg:justify-between">
      <div>
        <h2 class="text-3xl font-extrabold tracking-tight text-text-primary">提示词管理</h2>
        <p class="mt-1 text-sm text-text-secondary">统一管理入库抽取、智能体系统提示词等可维护模板。</p>
      </div>
      <div class="flex flex-wrap items-center gap-3">
        <div class="relative">
          <Search class="absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
          <input v-model="query" type="text" placeholder="搜索模板..." class="w-64 rounded-2xl border border-border-subtle bg-surface/80 py-2.5 pl-10 pr-4 text-xs text-text-primary shadow-sm outline-none transition-all focus:border-accent focus:ring-2 focus:ring-accent/20" />
        </div>
        <button @click="loadTemplates(true)" class="flex items-center gap-2 rounded-2xl border border-border-subtle bg-surface/80 px-4 py-2.5 text-xs font-bold text-text-secondary transition hover:bg-surface-hover">
          <RefreshCw class="h-4 w-4" />
          <span>刷新</span>
        </button>
        <button @click="startCreate" class="flex items-center gap-2 rounded-2xl bg-accent px-5 py-2.5 text-xs font-bold text-white shadow-sm transition hover:bg-accent-hover">
          <Plus class="h-4 w-4" />
          <span>新建模板</span>
        </button>
      </div>
    </div>

    <div v-if="notice" class="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs font-semibold text-emerald-700 dark:border-emerald-900/50 dark:bg-emerald-950/30 dark:text-emerald-300">{{ notice }}</div>
    <div v-if="error" class="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs font-semibold text-red-650 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">{{ error }}</div>

    <div class="grid min-h-[680px] grid-cols-1 gap-5 xl:grid-cols-[300px_360px_minmax(0,1fr)]">
      <section class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
        <div class="mb-3 flex items-center gap-2 text-sm font-extrabold text-text-primary">
          <Layers class="h-4 w-4 text-accent" />
          <span>模板分类</span>
        </div>
        <div class="space-y-2">
          <button v-for="category in categories" :key="category.category" @click="selectCategory(category.category)" class="w-full rounded-xl border px-3 py-3 text-left transition" :class="activeCategory === category.category ? 'border-accent/30 bg-accent-soft text-accent' : 'border-border-subtle bg-surface-sunken text-text-secondary hover:bg-surface-hover'">
            <div class="text-xs font-bold">{{ category.label }}</div>
            <p class="mt-1 text-[11px] leading-relaxed text-text-tertiary">{{ category.description }}</p>
          </button>
        </div>
      </section>

      <section class="rounded-2xl border border-border-subtle bg-surface/80 p-4">
        <div class="mb-3 flex items-center justify-between">
          <div class="flex items-center gap-2 text-sm font-extrabold text-text-primary">
            <FileText class="h-4 w-4 text-accent" />
            <span>模板列表</span>
          </div>
          <span class="text-[11px] font-semibold text-text-tertiary">{{ filteredTemplates.length }}</span>
        </div>
        <div v-if="loading" class="flex items-center justify-center py-16 text-xs text-text-tertiary">
          <Loader2 class="mr-2 h-4 w-4 animate-spin" />
          <span>加载中...</span>
        </div>
        <div v-else class="max-h-[610px] space-y-2 overflow-y-auto pr-1">
          <button v-for="template in filteredTemplates" :key="`${template.category}:${template.template_id}`" @click="selectTemplate(template)" class="w-full rounded-xl border px-3 py-3 text-left transition" :class="mode === 'edit' && activeTemplateId === template.template_id ? 'border-accent/30 bg-accent-soft text-accent' : 'border-border-subtle bg-surface-sunken text-text-secondary hover:bg-surface-hover'">
            <div class="flex items-center justify-between gap-2">
              <span class="truncate text-xs font-bold">{{ template.label }}</span>
              <span class="shrink-0 rounded-md bg-surface/70 px-1.5 py-0.5 text-[10px] text-text-tertiary">{{ template.version }}</span>
            </div>
            <p class="mt-1 truncate text-[10px] text-accent">{{ template.template_id }}</p>
            <p class="mt-1 text-[10px] text-text-tertiary">实体 {{ template.metadata?.entity_type_count ?? 0 }} · 关系 {{ template.metadata?.relation_type_count ?? 0 }}</p>
          </button>
        </div>
      </section>

      <section class="relative min-w-0 rounded-2xl border border-border-subtle bg-surface/80 p-5">
        <div v-if="detailLoading" class="absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-surface/70">
          <Loader2 class="h-6 w-6 animate-spin text-accent" />
        </div>
        <div class="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h3 class="text-lg font-extrabold text-text-primary">{{ mode === 'create' ? '新建提示词模板' : (activeTemplate?.label || form.label || '提示词模板') }}</h3>
            <p class="mt-1 text-[11px] text-text-tertiary">{{ activeCategoryInfo?.label || '提示词模板' }} · {{ form.template_id || 'new_template' }}</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <button v-if="mode === 'create'" @click="form.content = buildDefaultContent()" class="flex items-center gap-1.5 rounded-xl bg-surface-sunken px-3 py-2 text-xs font-bold text-text-secondary transition hover:bg-surface-hover">
              <Wand2 class="h-3.5 w-3.5" />
              <span>生成模板</span>
            </button>
            <button @click="saveTemplate" :disabled="saving || detailLoading" class="flex items-center gap-1.5 rounded-xl bg-accent px-4 py-2 text-xs font-bold text-white transition hover:bg-accent-hover disabled:opacity-60">
              <Loader2 v-if="saving" class="h-3.5 w-3.5 animate-spin" />
              <Save v-else class="h-3.5 w-3.5" />
              <span>保存</span>
            </button>
          </div>
        </div>
        <div class="mb-4 grid grid-cols-1 gap-3 lg:grid-cols-4">
          <div>
            <label class="mb-1.5 ml-1 block text-xs font-bold text-text-tertiary">模板 ID</label>
            <input v-model="form.template_id" :disabled="mode === 'edit'" class="w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 py-2.5 text-sm text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-70" />
          </div>
          <div>
            <label class="mb-1.5 ml-1 block text-xs font-bold text-text-tertiary">名称</label>
            <input v-model="form.label" class="w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 py-2.5 text-sm text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
          </div>
          <div>
            <label class="mb-1.5 ml-1 block text-xs font-bold text-text-tertiary">领域</label>
            <input v-model="form.subject" class="w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 py-2.5 text-sm text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
          </div>
          <div>
            <label class="mb-1.5 ml-1 block text-xs font-bold text-text-tertiary">版本</label>
            <input v-model="form.version" class="w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 py-2.5 text-sm text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
          </div>
        </div>
        <SchemaStructuredEditor
          v-model="form.content"
          :schema-id="form.template_id"
          :label="form.label"
          :subject="form.subject"
          :version="form.version"
          @validation="schemaValidation = $event"
        />
      </section>
    </div>
  </div>
</template>
