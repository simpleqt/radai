<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { KBDetailAPI, KnowledgeBase, type IngestionBackend, type RetrievalBackend, type ExtractionSchemaOption } from '@/api/kb';
import { Database, Plus, Search, Trash2, ArrowRight, Loader2, X, Pencil } from 'lucide-vue-next';

const router = useRouter();
const kbs = ref<KnowledgeBase[]>([]);
const loading = ref(true);
const searchQuery = ref('');
const currentPage = ref(1);
const total = ref(0);
const pageSize = 20;
const isCreateModalOpen = ref(false);
const isEditModalOpen = ref(false);
const editingKbId = ref('');
const extractionSchemas = ref<ExtractionSchemaOption[]>([]);
const schemasLoading = ref(false);

const DEFAULT_EXTRACTION_SCHEMA = 'basic_mechanics';

const INGESTION_OPTIONS: { value: IngestionBackend; label: string; desc: string }[] = [
  { value: 'legacy_lightrag', label: '传统 LightRAG', desc: '沿用既有 LightRAG 入库链路（旧知识库默认）' },
  { value: 'self_ingestion', label: '自研入库', desc: '本项目自研 Parser/Chunker/CommitWriter，支持版本化与证据溯源' },
];
const RETRIEVAL_OPTIONS: { value: RetrievalBackend; label: string; desc: string }[] = [
  { value: 'legacy_lightrag', label: '传统 LightRAG', desc: '沿用既有 DirectRetriever + LightRAG fallback' },
  { value: 'self_ingestion', label: '自研检索 v2', desc: '四路并行召回 self_ingestion 索引' },
];

const newKB = ref({
  kb_name: '',
  kb_code: '',
  subject: '',
  description: '',
  ingestion_backend: 'legacy_lightrag' as IngestionBackend,
  retrieval_backend: 'legacy_lightrag' as RetrievalBackend,
});

const ingestionDesc = computed(
  () => INGESTION_OPTIONS.find(o => o.value === newKB.value.ingestion_backend)?.desc ?? '',
);
const isSelfIngestion = computed(() => newKB.value.ingestion_backend === 'self_ingestion');
const selectedSchema = computed(
  () => extractionSchemas.value.find(s => s.schema_id === newKB.value.subject),
);
const retrievalDesc = computed(
  () => RETRIEVAL_OPTIONS.find(o => o.value === newKB.value.retrieval_backend)?.desc ?? '',
);

const onIngestionChange = (value: IngestionBackend) => {
  newKB.value.ingestion_backend = value;
  if (value === 'self_ingestion') {
    if (newKB.value.retrieval_backend === 'legacy_lightrag') {
      newKB.value.retrieval_backend = 'self_ingestion';
    }
    if (!extractionSchemas.value.some(s => s.schema_id === newKB.value.subject)) {
      newKB.value.subject = DEFAULT_EXTRACTION_SCHEMA;
    }
    void loadExtractionSchemas();
  }
};

const loadExtractionSchemas = async () => {
  if (extractionSchemas.value.length) return;
  schemasLoading.value = true;
  try {
    extractionSchemas.value = await KBDetailAPI.listExtractionSchemas();
    if (
      isSelfIngestion.value
      && !extractionSchemas.value.some(s => s.schema_id === newKB.value.subject)
    ) {
      newKB.value.subject = extractionSchemas.value[0]?.schema_id || DEFAULT_EXTRACTION_SCHEMA;
    }
  } catch (e) {
    console.error(e);
  } finally {
    schemasLoading.value = false;
  }
};

const openCreateModal = () => {
  isCreateModalOpen.value = true;
  void loadExtractionSchemas();
};

const openEditModal = (kb: KnowledgeBase) => {
  editingKbId.value = kb.kb_id;
  newKB.value = {
    kb_name: kb.kb_name,
    kb_code: kb.kb_code,
    subject: kb.subject || '',
    description: kb.description || '',
    ingestion_backend: kb.ingestion_backend || 'legacy_lightrag',
    retrieval_backend: kb.retrieval_backend || 'legacy_lightrag',
  };
  isEditModalOpen.value = true;
  if (newKB.value.ingestion_backend === 'self_ingestion') {
    void loadExtractionSchemas();
  }
};

const resetNewKB = () => {
  newKB.value = {
    kb_name: '',
    kb_code: '',
    subject: '',
    description: '',
    ingestion_backend: 'legacy_lightrag',
    retrieval_backend: 'legacy_lightrag',
  };
};

const loadKBs = async () => {
  loading.value = true;
  try {
    const res = await KBDetailAPI.list({
      q: searchQuery.value.trim(),
      skip: (currentPage.value - 1) * pageSize,
      limit: pageSize,
    });
    kbs.value = res.items || [];
    total.value = res.total || 0;
  } catch (e) {
    console.error(e);
  } finally {
    loading.value = false;
  }
};

const search = () => {
  currentPage.value = 1;
  void loadKBs();
};

const changePage = (delta: number) => {
  const pages = Math.max(1, Math.ceil(total.value / pageSize));
  currentPage.value = Math.min(pages, Math.max(1, currentPage.value + delta));
  void loadKBs();
};

const handleCreate = async () => {
  if (!newKB.value.kb_name || !newKB.value.kb_code) {
    alert('需填写知识库名称和编码');
    return;
  }
  const useSelfIngestion = newKB.value.ingestion_backend === 'self_ingestion';
  const useSelfRetriever = newKB.value.retrieval_backend !== 'legacy_lightrag';
  const subject = useSelfIngestion
    ? (newKB.value.subject || DEFAULT_EXTRACTION_SCHEMA)
    : (newKB.value.subject || undefined);
  const payload = {
    kb_name: newKB.value.kb_name,
    kb_code: newKB.value.kb_code,
    subject,
    description: newKB.value.description || undefined,
    ingestion_backend: newKB.value.ingestion_backend,
    retrieval_backend: newKB.value.retrieval_backend,
    self_ingestion_enabled: useSelfIngestion,
    self_retriever_enabled: useSelfRetriever,
  };
  try {
    await KBDetailAPI.create(payload);
    isCreateModalOpen.value = false;
    resetNewKB();
    loadKBs();
  } catch (e) {
    alert('创建失败，请确认全局自研开关已启用且知识库编码未被占用');
  }
};

const handleUpdate = async () => {
  if (!editingKbId.value || !newKB.value.kb_name) {
    alert('需填写知识库名称');
    return;
  }
  const useSelfIngestion = newKB.value.ingestion_backend === 'self_ingestion';
  const useSelfRetriever = newKB.value.retrieval_backend !== 'legacy_lightrag';
  const subject = useSelfIngestion
    ? (newKB.value.subject || DEFAULT_EXTRACTION_SCHEMA)
    : (newKB.value.subject || undefined);
  try {
    await KBDetailAPI.update(editingKbId.value, {
      kb_name: newKB.value.kb_name,
      subject,
      description: newKB.value.description || undefined,
      ingestion_backend: newKB.value.ingestion_backend,
      retrieval_backend: newKB.value.retrieval_backend,
      self_ingestion_enabled: useSelfIngestion,
      self_retriever_enabled: useSelfRetriever,
    });
    isEditModalOpen.value = false;
    editingKbId.value = '';
    resetNewKB();
    loadKBs();
  } catch {
    alert('更新失败，请确认后端配置合法且全局开关已启用');
  }
};

const handleDelete = async (id: string) => {
  if (!confirm('确定要删除这个知识库吗？此操作不可逆。')) return;
  try {
    await KBDetailAPI.delete(id);
    loadKBs();
  } catch (e) {
    alert('删除失败');
  }
};

onMounted(loadKBs);
</script>

<template>
  <div class="min-h-screen space-y-8 flex flex-col">
    <!-- View Title & Actions Banner -->
    <div class="flex flex-col md:flex-row md:items-center justify-between gap-6 pb-6 border-b border-border-subtle">
      <div>
        <h2 class="text-3xl font-extrabold tracking-tight text-text-primary">知识库管理</h2>
        <p class="text-text-secondary mt-1 text-sm">组织、上传与配给您的专业领域知识资产。</p>
      </div>
      
      <div class="flex flex-wrap items-center gap-3.5">
        <div class="relative">
          <Search class="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-text-tertiary" />
          <input 
            v-model="searchQuery"
            @keyup.enter="search"
            type="text" 
            placeholder="搜索知识库..." 
            class="pl-10 pr-4 py-2.5 bg-surface/70 border border-border-subtle rounded-2xl text-xs focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none w-64 transition-all shadow-sm text-text-primary"
          />
        </div>
        <button 
          @click="openCreateModal"
          class="bg-accent hover:bg-accent-hover text-white px-5 py-2.5 rounded-2xl text-xs font-bold flex items-center space-x-2 transition-all hover-lift shadow-sm active:scale-95"
        >
          <Plus class="w-4 h-4" />
          <span>新建知识库</span>
        </button>
      </div>
    </div>

    <!-- Loading State -->
    <div v-if="loading" class="flex flex-col items-center justify-center py-24 text-text-secondary">
      <Loader2 class="w-9 h-9 animate-spin mb-4 text-accent" />
      <p class="text-sm font-semibold tracking-wide">正在载入知识库列表...</p>
    </div>

    <!-- Empty State -->
    <div v-else-if="kbs.length === 0" class="flex flex-col items-center justify-center py-20 text-text-secondary border-2 border-dashed border-border-subtle rounded-3xl bg-white/30">
      <div class="w-14 h-14 bg-surface-sunken rounded-2xl flex items-center justify-center mb-4">
        <Database class="w-7 h-7 text-text-tertiary" />
      </div>
      <h3 class="text-base font-bold text-text-primary mb-1">暂无知识库</h3>
      <p class="text-xs text-text-tertiary mb-6 max-w-xs text-center">系统内暂无可用知识存储。立即创建一个，开始索引核电子学学术文献。</p>
      <button 
        @click="openCreateModal" 
        class="bg-accent-soft text-accent px-5 py-2.5 rounded-xl text-xs font-bold hover:bg-accent-soft transition-all active:scale-95"
      >
        立即新建
      </button>
    </div>

    <!-- Cards Grid -->
    <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
      <TransitionGroup 
        enter-active-class="transition duration-300 ease-out"
        enter-from-class="opacity-0 transform translate-y-4"
        enter-to-class="opacity-100 transform translate-y-0"
      >
        <div 
          v-for="kb in kbs"
          :key="kb.kb_id"
          class="group bg-surface/80 backdrop-blur-md rounded-3xl border border-border-subtle overflow-hidden hover-lift glow-shadow-indigo transition-all duration-300 relative flex flex-col justify-between min-h-[250px]"
        >
          <!-- Accent top border -->
          <div class="absolute top-0 left-0 right-0 h-1 bg-accent opacity-0 group-hover:opacity-100 transition-opacity"></div>
          
          <div class="p-6 flex-1 flex flex-col">
            <div class="flex justify-between items-start mb-4">
              <div class="p-2.5 bg-accent-soft rounded-2xl group-hover:scale-105 group-hover:rotate-3 transition-transform duration-300">
                <Database class="w-5 h-5 text-accent" />
              </div>
              <div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <button 
                  @click.stop="openEditModal(kb)"
                  class="p-2 text-text-tertiary hover:text-accent hover:bg-accent-soft rounded-xl transition-all duration-200 border border-transparent hover:border-accent/50"
                  title="编辑"
                >
                  <Pencil class="w-4 h-4" />
                </button>
                <button 
                  @click.stop="handleDelete(kb.kb_id)"
                  class="p-2 text-text-tertiary hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/30 rounded-xl transition-all duration-200 border border-transparent hover:border-red-100/50"
                  title="删除"
                >
                  <Trash2 class="w-4 h-4" />
                </button>
              </div>
            </div>
            
            <h3 class="text-base font-extrabold text-text-primary mb-1.5 truncate group-hover:text-accent transition-colors">
              {{ kb.kb_name }}
            </h3>
            
            <div class="flex items-center space-x-2 text-[10px] font-bold text-accent tracking-wider mb-3">
              <span class="bg-accent-soft px-2 py-0.5 rounded-md border border-accent/30">{{ kb.kb_code }}</span>
              <span v-if="kb.subject" class="text-text-tertiary font-normal">·</span>
              <span v-if="kb.subject" class="text-text-tertiary font-normal">{{ kb.subject }}</span>
            </div>
            
            <p class="text-xs text-text-secondary line-clamp-2 h-9 mb-4 leading-relaxed">
              {{ kb.description || '无描述' }}
            </p>
          </div>
          
          <div class="px-6 pb-6 pt-2 border-t border-border-subtle flex items-center justify-between">
            <div class="flex items-center space-x-3 text-[10px] font-bold text-text-tertiary">
              <div class="flex items-center">
                <span class="text-text-secondary mr-1 text-sm font-extrabold">{{ kb.doc_count }}</span> 文档
              </div>
              <div class="w-1.5 h-1.5 rounded-full bg-surface-sunken"></div>
              <div class="flex items-center">
                <span class="text-text-secondary mr-1 text-sm font-extrabold">{{ kb.entity_count }}</span> 实体
              </div>
            </div>

            <button 
              @click="router.push(`/admin/kb/${kb.kb_id}`)"
              class="px-4 py-2 bg-surface-sunken hover:bg-accent-hover text-text-secondary hover:text-white dark:hover:text-white text-xs font-bold rounded-xl transition-all duration-200 flex items-center space-x-1.5 border border-border-subtle shadow-sm"
            >
              <span>详情</span>
              <ArrowRight class="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </TransitionGroup>
    </div>

    <!-- Pagination -->
    <div v-if="!loading && total > 0" class="flex flex-col sm:flex-row items-center justify-between gap-4 py-4 text-xs font-medium text-text-secondary border-t border-border-subtle">
      <span>共 {{ total }} 个知识库，当前第 {{ currentPage }} / {{ Math.max(1, Math.ceil(total / pageSize)) }} 页</span>
      <div class="flex gap-2">
        <button 
          @click="changePage(-1)" 
          :disabled="currentPage <= 1" 
          class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm"
        >
          上一页
        </button>
        <button 
          @click="changePage(1)" 
          :disabled="currentPage >= Math.ceil(total / pageSize)" 
          class="rounded-xl border border-border-subtle px-4 py-2 hover:bg-surface-hover disabled:opacity-40 transition-colors shadow-sm"
        >
          下一页
        </button>
      </div>
    </div>

    <!-- Edit Modal dialog -->
    <div v-if="isEditModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="编辑知识库">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="isEditModalOpen = false"></div>
      <div class="relative max-h-[90vh] w-full max-w-md overflow-y-auto rounded-3xl border border-white/55 bg-surface/90 p-8 shadow-2xl backdrop-blur-2xl animate-in fade-in zoom-in duration-200 dark:border-white/10 dark:bg-surface/95">
        <button @click="isEditModalOpen = false" class="absolute top-5 right-5 p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition">
          <X class="w-4.5 h-4.5" />
        </button>

        <h2 class="text-xl font-bold text-text-primary mb-6 flex items-center space-x-2">
          <Pencil class="w-5 h-5 text-accent" />
          <span>编辑知识库</span>
        </h2>

        <div class="space-y-4.5">
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">识别编码 (不可修改)</label>
            <input :value="newKB.kb_code" type="text" disabled class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl text-sm text-text-secondary cursor-not-allowed" />
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">知识库名称</label>
            <input v-model="newKB.kb_name" type="text" class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary" />
          </div>
          <div v-if="isSelfIngestion">
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">入库抽取 Schema</label>
            <select
              v-model="newKB.subject"
              :disabled="schemasLoading"
              class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary disabled:opacity-60"
            >
              <option v-for="schema in extractionSchemas" :key="schema.schema_id" :value="schema.schema_id">
                {{ schema.label }}（{{ schema.schema_version }}）
              </option>
            </select>
          </div>
          <div v-else>
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">涉及学科 / 专业领域</label>
            <input v-model="newKB.subject" type="text" class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary" />
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">描述与备注</label>
            <textarea v-model="newKB.description" class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary h-24"></textarea>
          </div>
          <div class="pt-3 mt-2 border-t border-border-subtle">
            <p class="text-xs font-bold text-text-secondary mb-3 ml-1">入库与检索后端</p>
            <div class="space-y-3">
              <div>
                <label class="block text-[11px] font-semibold text-text-tertiary mb-1 ml-1">入库后端</label>
                <select
                  :value="newKB.ingestion_backend"
                  @change="onIngestionChange(($event.target as HTMLSelectElement).value as IngestionBackend)"
                  class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary"
                >
                  <option v-for="opt in INGESTION_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                </select>
              </div>
              <div>
                <label class="block text-[11px] font-semibold text-text-tertiary mb-1 ml-1">查询后端</label>
                <select
                  v-model="newKB.retrieval_backend"
                  class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary"
                >
                  <option v-for="opt in RETRIEVAL_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <div class="flex space-x-3 mt-8">
          <button @click="isEditModalOpen = false" class="flex-1 py-2.5 bg-surface-sunken text-text-secondary text-xs font-bold rounded-xl hover:bg-surface-hover transition-colors">取消</button>
          <button @click="handleUpdate" class="flex-1 py-2.5 bg-accent hover:bg-accent-hover text-white text-xs font-bold rounded-xl hover-lift shadow-sm transition-all">保存修改</button>
        </div>
      </div>
    </div>

    <!-- Create Modal dialog -->
    <div v-if="isCreateModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="创建知识库">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="isCreateModalOpen = false"></div>
      <div class="relative w-full max-w-md rounded-3xl border border-white/55 bg-surface/90 p-8 shadow-2xl backdrop-blur-2xl animate-in fade-in zoom-in duration-200 dark:border-white/10 dark:bg-surface/95">
        <!-- Close button -->
        <button @click="isCreateModalOpen = false" class="absolute top-5 right-5 p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition">
          <X class="w-4.5 h-4.5" />
        </button>

        <h2 class="text-xl font-bold text-text-primary mb-6 flex items-center space-x-2">
          <Database class="w-5 h-5 text-accent" />
          <span>新建知识库</span>
        </h2>
        
        <div class="space-y-4.5">
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">知识库名称</label>
            <input v-model="newKB.kb_name" type="text" class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary" placeholder="例如：电子学论文集" />
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">识别编码 (Code)</label>
            <input v-model="newKB.kb_code" type="text" class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary" placeholder="例如：electronics_papers" />
          </div>
          <div v-if="isSelfIngestion">
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">入库抽取 Schema（提示词模板）</label>
            <select
              v-model="newKB.subject"
              :disabled="schemasLoading"
              class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary disabled:opacity-60"
            >
              <option v-if="schemasLoading" value="">加载中...</option>
              <option
                v-for="schema in extractionSchemas"
                :key="schema.schema_id"
                :value="schema.schema_id"
              >
                {{ schema.label }}（{{ schema.schema_version }}）
              </option>
            </select>
            <p v-if="selectedSchema" class="text-[10px] text-text-tertiary mt-1 ml-1 leading-relaxed">
              实体类型 {{ selectedSchema.entity_type_count }} 种 · 关系类型 {{ selectedSchema.relation_type_count }} 种；
              入库时 LLM 按该 Schema 生成抽取提示词（非 LightRAG merge）。
            </p>
          </div>
          <div v-else>
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">涉及学科 / 专业领域</label>
            <input v-model="newKB.subject" type="text" class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary" placeholder="例如：核电子学（展示用，可选）" />
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1.5 ml-1">描述与备注</label>
            <textarea v-model="newKB.description" class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary h-24" placeholder="简要说明知识库用途与文献收集范围..."></textarea>
          </div>

          <!-- Backend selection -->
          <div class="pt-3 mt-2 border-t border-border-subtle">
            <p class="text-xs font-bold text-text-secondary mb-3 ml-1">入库与检索后端</p>
            <div class="space-y-3">
              <div>
                <label class="block text-[11px] font-semibold text-text-tertiary mb-1 ml-1">入库后端</label>
                <select
                  :value="newKB.ingestion_backend"
                  @change="onIngestionChange(($event.target as HTMLSelectElement).value as IngestionBackend)"
                  class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary"
                >
                  <option v-for="opt in INGESTION_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                </select>
                <p class="text-[10px] text-text-tertiary mt-1 ml-1 leading-relaxed">{{ ingestionDesc }}</p>
              </div>
              <div>
                <label class="block text-[11px] font-semibold text-text-tertiary mb-1 ml-1">查询后端</label>
                <select
                  v-model="newKB.retrieval_backend"
                  class="w-full px-4 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none text-sm text-text-primary"
                >
                  <option v-for="opt in RETRIEVAL_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                </select>
                <p class="text-[10px] text-text-tertiary mt-1 ml-1 leading-relaxed">{{ retrievalDesc }}</p>
              </div>
              <p class="text-[10px] text-amber-600 dark:text-amber-400 mt-1 ml-1 leading-relaxed">
                注：选择「自研」后端需服务端全局开关 SELF_INGESTION_ENABLED / SELF_RETRIEVER_ENABLED 已启用；未启用时任务会被阻塞，不会回退写入传统链路。
              </p>
            </div>
          </div>
        </div>
        
        <div class="flex space-x-3 mt-8">
          <button @click="isCreateModalOpen = false" class="flex-1 py-2.5 bg-surface-sunken text-text-secondary text-xs font-bold rounded-xl hover:bg-surface-hover transition-colors">取消</button>
          <button @click="handleCreate" class="flex-1 py-2.5 bg-accent hover:bg-accent-hover text-white text-xs font-bold rounded-xl hover-lift shadow-sm transition-all">立即创建</button>
        </div>
      </div>
    </div>
  </div>
</template>
