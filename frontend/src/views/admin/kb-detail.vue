<script setup lang="ts">
import { ref, onMounted, computed, onUnmounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { KBDetailAPI, KnowledgeBase } from '@/api/kb';
import { useUpload } from '@/composables/useUpload';
import { 
  ArrowLeft, Database, FileText, Upload, Trash2, 
  ArrowRight, Search, Loader2, X, CheckCircle2, AlertCircle, Play, ChevronRight, ChevronLeft, Folder, FolderPlus, Home,
  MoreVertical, Edit2, Move, CheckSquare, Square, Clock
} from 'lucide-vue-next';

const route = useRoute();
const router = useRouter();
const kbId = route.params.id as string;

const kb = ref<KnowledgeBase | null>(null);
const documents = ref<any[]>([]);
const directoryTree = ref<any[]>([]);
const currentPath = ref('');
const loading = ref(true);
const searchQuery = ref('');
const isUploadingModalOpen = ref(false);
const ingestLoading = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

// Pagination State
const currentPage = ref(1);
const pageSize = ref(20);
const totalDocuments = ref(0);
const jumpPage = ref<string>("");

const isKeywordInitLoading = ref(false);
const keywordInitStatus = ref("");
const keywordInitTaskId = ref<string | null>(null);

const handleJump = () => {
  const page = parseInt(jumpPage.value);
  const maxPage = Math.ceil(totalDocuments.value / pageSize.value);
  if (!isNaN(page) && page > 0 && page <= maxPage) {
    currentPage.value = page;
    loadData();
    jumpPage.value = "";
  }
};

// Selection & Batch State
const selectedDocIds = ref(new Set<string>());
const documentDate = ref(new Date().toISOString().split('T')[0]);
const isMoveModalOpen = ref(false);
const isRenameModalOpen = ref(false);
const moveTargetPath = ref('');
const uploadTargetPath = ref(''); // Target for new uploads
const renameDirOldPath = ref('');
const renameDirNewName = ref('');

const { queue, isUploading, addFiles, removeItem, clearQueue } = useUpload(kbId);

const loadData = async () => {
  loading.value = true;
  try {
    const documentParams: Record<string, any> = {
      skip: (currentPage.value - 1) * pageSize.value,
      limit: pageSize.value,
      // Always send category_path to ensure exact filtering (empty string = root)
      category_path: currentPath.value || ''
    };

    const [kbRes, docsRes, dirRes] = await Promise.all([
      KBDetailAPI.get(kbId),
      KBDetailAPI.documents(kbId, documentParams),
      KBDetailAPI.getDirectories(kbId)
    ]);
    kb.value = kbRes;
    documents.value = docsRes.items || [];
    totalDocuments.value = docsRes.total || 0;
    directoryTree.value = dirRes.tree || [];
  } catch (e) {
    console.error(e);
  } finally {
    loading.value = false;
  }
};

const handleFileSelect = (e: Event) => {
  const files = (e.target as HTMLInputElement).files;
  if (files) {
    addFiles(files);
  }
};

const formatDetailedDate = (dateStr: string) => {
  const date = new Date(dateStr);
  return {
    date: date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).replace(/\//g, '-'),
    time: date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  };
};

const handleIngest = async () => {
  const completedFiles = queue.value.filter(f => f.status === 'completed' && f.file_id);
  if (completedFiles.length === 0) return;
  
  ingestLoading.value = true;
  try {
    // Logic: Form the final path by joining selected path + document date for segmentation
    const finalPath = uploadTargetPath.value.trim();

    const res = await KBDetailAPI.ingest({
      kb_id: kbId,
      file_ids: completedFiles.map(f => f.file_id!),
      document_date: documentDate.value,
      category_path: finalPath || null
    });
    alert(`入库任务已启动 (ID: ${res.task_id})`);
    clearQueue();
    isUploadingModalOpen.value = false;
    loadData();
  } catch (e: any) {
    alert('启动入库失败: ' + e.message);
  } finally {
    ingestLoading.value = false;
  }
};

const handleDeleteDoc = async (docId: string) => {
  if (!confirm('确定要删除此文档吗？此操作将同时删除其关联的向量数据。')) return;
  try {
    await KBDetailAPI.deleteDocument(kbId, docId);
    selectedDocIds.value.delete(docId);
    loadData();
  } catch (e) {
    alert('删除失败');
  }
};

const toggleSelect = (docId: string) => {
  if (selectedDocIds.value.has(docId)) {
    selectedDocIds.value.delete(docId);
  } else {
    selectedDocIds.value.add(docId);
  }
};

const toggleSelectAll = () => {
  if (selectedDocIds.value.size === filteredDocs.value.length) {
    selectedDocIds.value.clear();
  } else {
    filteredDocs.value.forEach(doc => selectedDocIds.value.add(doc.doc_id));
  }
};

const handleRenameDir = (dir: any) => {
  renameDirOldPath.value = dir.path || dir.category_path;
  renameDirNewName.value = dir.name;
  isRenameModalOpen.value = true;
};

const submitRenameDir = async () => {
  if (!renameDirNewName.value.trim()) return;
  try {
    await KBDetailAPI.renameDirectory(kbId, renameDirOldPath.value, renameDirNewName.value);
    isRenameModalOpen.value = false;
    loadData();
  } catch (e: any) {
    alert('重命名失败: ' + e.message);
  }
};

const handleDeleteDir = async (dir: any) => {
  const path = dir.path || dir.category_path;
  if (!confirm(`确定要删除目录 "${dir.name}" 吗？此操作不可恢复。`)) return;
  
  const deleteDocs = confirm('是否同时删除目录下的文档？');
  try {
    await KBDetailAPI.deleteDirectory(kbId, path, deleteDocs);
    loadData();
  } catch (e: any) {
    alert('删除失败: ' + e.message);
  }
};

const handleBatchMove = async () => {
  if (selectedDocIds.value.size === 0) return;
  moveTargetPath.value = currentPath.value || '';
  isMoveModalOpen.value = true;
};

const submitBatchMove = async () => {
  try {
    await KBDetailAPI.batchMoveDocuments(kbId, Array.from(selectedDocIds.value), moveTargetPath.value || null);
    selectedDocIds.value.clear();
    isMoveModalOpen.value = false;
    loadData();
  } catch (e: any) {
    alert('移动失败: ' + e.message);
  }
};

const handleBatchDelete = async () => {
  if (selectedDocIds.value.size === 0) return;
  if (!confirm(`确定批量删除已选中的 ${selectedDocIds.value.size} 个文档吗？此操作不可恢复。`)) return;

  try {
    const res = await KBDetailAPI.batchDeleteDocuments(kbId, Array.from(selectedDocIds.value), false);
    selectedDocIds.value.clear();
    loadData();

    if (res.failed && res.failed > 0) {
      alert(`批量删除完成，成功 ${res.deleted} 个，失败 ${res.failed} 个`);
    }
  } catch (e: any) {
    alert('批量删除失败: ' + e.message);
  }
};

const handleKeywordInit = async () => {
  if (!kbId) return;

  isKeywordInitLoading.value = true;
  keywordInitStatus.value = "任务提交中...";

  try {
    const resp = await KBDetailAPI.initKeywords(kbId, {
      force_rebuild: false,
      init_bm25: true,
      warmup_low_level: true,
      include_vector: false
    });

    keywordInitTaskId.value = resp.task_id;
    keywordInitStatus.value = `任务已提交: ${resp.task_id}`;
    pollKeywordInitStatus(resp.task_id);
  } catch (e: any) {
    isKeywordInitLoading.value = false;
    keywordInitStatus.value = `初始化提交失败: ${e.message}`;
    console.error(e);
  }
};

const pollKeywordInitStatus = async (taskId: string) => {
  const poll = async () => {
    try {
      const status = await KBDetailAPI.getKeywordInitStatus(taskId);
      const phase = status.phase || 'unknown';
      const msg = status.message || '';

      if (status.status === 'running') {
        keywordInitStatus.value = `阶段: ${phase} | ${msg}`;
        setTimeout(poll, 1500);
        return;
      }

      if (status.status === 'success') {
        const result = status.result || {};
        keywordInitStatus.value = `完成: 实体${result.entity_count || 0}，BM25文档${result.bm25_doc_count || 0}，BM25词项${result.bm25_term_count || 0}，图节点${result.graph_nodes || 0}，图边${result.graph_edges || 0}`;
        isKeywordInitLoading.value = false;
        return;
      }

      keywordInitStatus.value = `失败: ${status.error || status.message || '未知错误'}`;
      isKeywordInitLoading.value = false;
    } catch (e: any) {
      keywordInitStatus.value = `状态查询失败: ${e.message}`;
      isKeywordInitLoading.value = false;
    }
  };

  poll();
};

onMounted(loadData);

const currentLevelDirs = computed(() => {
  if (!currentPath.value) {
    return directoryTree.value.filter(d => !d.path.includes('/'));
  }
  const parts = currentPath.value.split('/');
  let node = null;
  let tree = directoryTree.value;
  for (const part of parts) {
    node = tree.find(n => n.name === part);
    if (!node) break;
    tree = node.children;
  }
  return node ? node.children : [];
});

const filteredDocs = computed(() => {
  return documents.value.filter(doc => doc.file_name.toLowerCase().includes(searchQuery.value.toLowerCase()));
});

const allPaths = computed(() => {
  const paths = new Set<string>();
  const traverse = (nodes: any[]) => {
    nodes.forEach(node => {
      paths.add(node.path);
      if (node.children) traverse(node.children);
    });
  };
  traverse(directoryTree.value);
  return paths;
});

const setPath = (path: string) => {
  currentPath.value = path;
  currentPage.value = 1; // Reset to page 1 on path change
  loadData();
};

const breadcrumbs = computed(() => {
  if (!currentPath.value) return [];
  const parts = currentPath.value.split('/');
  let current = '';
  return parts.map(p => {
    current = current ? `${current}/${p}` : p;
    return { name: p, path: current };
  });
});

const parentPath = computed(() => {
  if (!currentPath.value) return '';
  const parts = currentPath.value.split('/').filter(Boolean);
  if (parts.length <= 1) return '';
  return parts.slice(0, -1).join('/');
});

const uploadProgress = computed(() => {
  if (queue.value.length === 0) return 0;
  const totalCharge = queue.value.reduce((acc, f) => acc + f.progress, 0);
  return Math.round(totalCharge / queue.value.length);
});
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900 flex flex-col">
    <!-- Breadcrumbs & Actions -->
    <div class="flex flex-col md:flex-row md:items-center justify-between gap-6 mb-10">
      <div class="flex items-center space-x-4">
        <button @click="router.back()" class="p-2.5 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl hover:bg-slate-50 transition-colors shadow-sm">
          <ArrowLeft class="w-5 h-5 text-slate-500" />
        </button>
        <div>
          <div class="flex items-center space-x-2 text-xs font-bold text-slate-400 uppercase tracking-widest mb-1">
            <span>知识库</span>
            <ChevronRight class="w-3 h-3" />
            <span>管理详情</span>
          </div>
          <h2 class="text-3xl font-extrabold tracking-tight text-slate-800 dark:text-slate-100 flex items-center space-x-3">
             <Database class="w-7 h-7 text-indigo-500" />
             <span>{{ kb?.kb_name || '正在加载...' }}</span>
          </h2>
        </div>
      </div>
      
      <div class="flex items-center space-x-3">
        <div class="relative">
          <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input 
            v-model="searchQuery"
            type="text" 
            placeholder="搜索文档..." 
            class="pl-10 pr-4 py-2 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500 outline-none w-64 transition-all shadow-sm"
          />
        </div>
        <button 
          @click="isUploadingModalOpen = true; uploadTargetPath = currentPath"
          class="bg-indigo-600 hover:bg-indigo-700 text-white px-5 py-2.5 rounded-xl text-sm font-bold flex items-center space-x-2 transition-all shadow-lg shadow-indigo-500/20 active:scale-95"
        >
          <Upload class="w-4 h-4" />
          <span>上传文档</span>
        </button>
      </div>
    </div>

    <!-- Path Breadcrumbs -->
    <div class="flex items-center space-x-2 text-sm mb-6 bg-white dark:bg-slate-800 p-3 rounded-xl border border-slate-100 dark:border-slate-700 shadow-sm mx-6">
      <button 
        @click="setPath('')"
        class="flex items-center space-x-1.5 hover:text-indigo-600 transition-colors"
        :class="!currentPath ? 'text-indigo-600 font-bold' : 'text-slate-400'"
      >
        <Home class="w-4 h-4" />
        <span>根目录</span>
      </button>
      <template v-for="(crumb, index) in breadcrumbs" :key="crumb.path">
        <ChevronRight class="w-4 h-4 text-slate-300" />
        <button 
          @click="setPath(crumb.path)"
          class="hover:text-indigo-600 transition-colors"
          :class="index === breadcrumbs.length - 1 ? 'text-indigo-600 font-bold' : 'text-slate-400'"
        >
          {{ crumb.name }}
        </button>
      </template>
    </div>

    <!-- Main Content -->
    <main class="flex-1 p-6 pb-32 max-w-7xl mx-auto w-full grid grid-cols-1 lg:grid-cols-4 gap-6">
      
      <!-- Stats Sidebar -->
      <aside class="space-y-6 lg:col-span-1">
        <div class="bg-white dark:bg-slate-800 p-6 rounded-2xl border border-slate-200 dark:border-slate-700 shadow-sm">
          <h3 class="text-sm font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wider mb-6">数据库指标</h3>
          <div class="space-y-6">
            <div>
              <div class="text-xs text-slate-400 mb-1">文档数量</div>
              <div class="text-2xl font-bold text-slate-800 dark:text-slate-100">{{ kb?.doc_count || 0 }}</div>
            </div>
            <div class="h-px bg-slate-100 dark:bg-slate-700/50"></div>
            <div>
              <div class="text-xs text-slate-400 mb-1">活跃实体</div>
              <div class="text-2xl font-bold text-slate-800 dark:text-slate-100">{{ kb?.entity_count || 0 }}</div>
            </div>
            <div class="h-px bg-slate-100 dark:bg-slate-700/50"></div>
            <div>
              <div class="text-xs text-slate-400 mb-1">知识关系</div>
              <div class="text-2xl font-bold text-slate-800 dark:text-slate-100">{{ kb?.relation_count || 0 }}</div>
            </div>
          </div>
        </div>

        <div class="bg-indigo-50 dark:bg-indigo-900/20 p-6 rounded-2xl border border-indigo-100 dark:border-indigo-800/50">
          <h4 class="text-indigo-900 dark:text-indigo-300 font-bold text-sm mb-2">快速提示</h4>
          <p class="text-xs text-indigo-700/70 dark:text-indigo-400/70 leading-relaxed">
            上传 PDF 或 Markdown 文档后，请点击“开始解析”按钮将内容向量化至当前知识库。
          </p>
        </div>

        <!-- Search Optimization Card -->
        <div class="bg-white dark:bg-slate-800 p-6 rounded-2xl border border-slate-200 dark:border-slate-700 shadow-sm">
          <div class="flex items-center space-x-2 mb-6">
            <Search class="w-4 h-4 text-indigo-500" />
            <h3 class="text-sm font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wider">搜索优化</h3>
          </div>
          
          <div class="space-y-4">
            <button 
              @click="handleKeywordInit"
              :disabled="isKeywordInitLoading"
              class="w-full py-2.5 px-4 bg-slate-900 dark:bg-indigo-600 hover:bg-slate-800 dark:hover:bg-indigo-700 disabled:opacity-50 text-white rounded-xl text-xs font-bold transition-all flex items-center justify-center space-x-2"
            >
              <Loader2 v-if="isKeywordInitLoading" class="w-3.5 h-3.5 animate-spin" />
              <Play v-else class="w-3.5 h-3.5" />
              <span>{{ isKeywordInitLoading ? '初始化中...' : '初始化关键词提取' }}</span>
            </button>

            <div v-if="keywordInitStatus" class="mt-4 p-3 bg-slate-50 dark:bg-slate-900/50 rounded-lg border border-slate-100 dark:border-slate-700">
               <div class="flex items-start space-x-2">
                 <div class="w-1.5 h-1.5 rounded-full mt-1.5" :class="isKeywordInitLoading ? 'bg-indigo-500 animate-pulse' : 'bg-emerald-500'"></div>
                 <p class="text-[10px] leading-relaxed text-slate-500 dark:text-slate-400 font-medium whitespace-pre-wrap">{{ keywordInitStatus }}</p>
               </div>
            </div>
            
            <p class="text-[10px] text-slate-400 leading-relaxed italic">
              初始化 BM25 和低级关键词图，以提升混合检索的精确度。
            </p>
          </div>
        </div>
      </aside>

      <!-- Document List -->
      <div class="lg:col-span-3 space-y-4">
        <div v-if="loading" class="flex flex-col items-center justify-center py-20 text-slate-500">
          <Loader2 class="w-10 h-10 animate-spin mb-4 text-indigo-500" />
          <p>正在拉取文档...</p>
        </div>

        <div v-else-if="currentLevelDirs.length === 0 && filteredDocs.length === 0" class="flex flex-col items-center justify-center py-20 text-slate-500 border-2 border-dashed border-slate-200 rounded-2xl bg-white dark:bg-slate-800/50">
          <FileText class="w-12 h-12 mb-4 text-slate-300" />
          <p>当前目录没有符合条件的文档或子目录</p>
        </div>

        <div v-else class="bg-white dark:bg-slate-800 rounded-t-2xl border border-slate-200 dark:border-slate-700 overflow-x-auto shadow-sm">
          <table class="w-full text-sm text-left">
            <thead class="bg-slate-50 dark:bg-slate-900/50 text-slate-500 dark:text-slate-400 font-medium">
              <tr>
                <th class="px-6 py-4 w-10">
                  <button @click="toggleSelectAll" class="text-slate-400 hover:text-indigo-500 transition-colors">
                    <CheckSquare v-if="selectedDocIds.size === filteredDocs.length && filteredDocs.length > 0" class="w-5 h-5 text-indigo-500" />
                    <Square v-else class="w-5 h-5" />
                  </button>
                </th>
                <th class="px-6 py-4">文件名</th>
                <th class="px-6 py-4">状态</th>
                <th class="px-6 py-4">添加时间</th>
                <th class="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 dark:divide-slate-700/50">
              <!-- Folders First -->
              <tr v-for="dir in currentLevelDirs" :key="dir.path" class="hover:bg-slate-50 dark:hover:bg-slate-700/30 transition-colors group">
                <td class="px-6 py-4">
                  <div class="w-5 h-5"></div>
                </td>
                <td class="px-6 py-4 cursor-pointer" @click="setPath(dir.path)">
                  <div class="flex items-center space-x-3">
                    <Clock v-if="dir.is_virtual" class="w-5 h-5 text-indigo-400 fill-indigo-400/10" />
                    <FolderIcon v-else class="w-5 h-5 text-amber-500 fill-amber-500/20" />
                    <span class="font-bold text-slate-700 dark:text-slate-200">{{ dir.name }}</span>
                    <span v-if="dir.is_virtual" class="text-[10px] px-1.5 py-0.5 bg-slate-100 dark:bg-slate-800 text-slate-400 rounded uppercase tracking-tighter">虚拟</span>
                  </div>
                </td>
                <td class="px-6 py-4 text-xs font-medium text-slate-400">目录</td>
                <td class="px-6 py-4 text-slate-400">—</td>
                <td class="px-6 py-4 text-right">
                   <div class="flex items-center justify-end space-x-2">
                     <button v-if="!dir.is_virtual" @click.stop="handleRenameDir(dir)" class="p-1.5 text-slate-400 hover:text-indigo-500 transition-colors opacity-0 group-hover:opacity-100">
                       <Edit2 class="w-4 h-4" />
                     </button>
                     <button v-if="!dir.is_virtual" @click.stop="handleDeleteDir(dir)" class="p-1.5 text-slate-400 hover:text-red-500 transition-colors opacity-0 group-hover:opacity-100">
                       <Trash2 class="w-4 h-4" />
                     </button>
                     <ChevronRight class="w-4 h-4 text-slate-300" />
                   </div>
                </td>
              </tr>

              <!-- Documents -->
              <tr v-for="doc in filteredDocs" :key="doc.id" class="hover:bg-slate-50 dark:hover:bg-slate-700/30 transition-colors group">
                <td class="px-6 py-4">
                  <button @click="toggleSelect(doc.doc_id)" class="text-slate-300 hover:text-indigo-500 transition-colors">
                    <CheckSquare v-if="selectedDocIds.has(doc.doc_id)" class="w-5 h-5 text-indigo-500" />
                    <Square v-else class="w-5 h-5" />
                  </button>
                </td>
                <td class="px-6 py-4">
                  <div class="flex items-center space-x-3">
                    <FileText class="w-5 h-5 text-indigo-400" />
                    <span class="font-bold text-slate-700 dark:text-slate-200 truncate max-w-xs">{{ doc.file_name }}</span>
                  </div>
                </td>
                <td class="px-6 py-4 text-xs font-medium">
                  <span 
                    class="px-2 py-1 rounded-md"
                    :class="{
                      'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400': doc.parse_status === 1,
                      'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400': doc.parse_status === 2,
                      'bg-slate-100 text-slate-600': doc.parse_status === 0
                    }"
                  >
                    {{ doc.parse_status === 1 ? '解析成功' : doc.parse_status === 2 ? '解析失败' : '待处理' }}
                  </span>
                </td>
                <td class="px-6 py-4 text-slate-400 group-hover:text-slate-600 dark:group-hover:text-slate-300 transition-colors">
                  <div class="flex flex-col">
                    <span class="font-medium text-slate-500 dark:text-slate-400 text-xs">{{ formatDetailedDate(doc.created_at).date }}</span>
                    <span class="text-[10px] opacity-60 font-mono">{{ formatDetailedDate(doc.created_at).time }}</span>
                  </div>
                </td>
                <td class="px-6 py-4 text-right">
                  <div class="flex items-center justify-end space-x-2">
                    <button 
                      @click="handleDeleteDoc(doc.doc_id)"
                      class="p-2 text-slate-400 hover:text-red-500 transition-colors opacity-0 group-hover:opacity-100"
                    >
                      <Trash2 class="w-4 h-4" />
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Pagination Bar -->
        <div v-if="filteredDocs.length > 0 || currentPage > 1" class="flex items-center justify-between px-6 py-4 bg-white dark:bg-slate-800 border-x border-b border-slate-200 dark:border-slate-700 rounded-b-2xl shadow-sm">
          <div class="text-xs text-slate-500 font-medium">
            显示第 {{ (currentPage - 1) * pageSize + 1 }} - {{ Math.min(currentPage * pageSize, totalDocuments) }} 条，共 {{ totalDocuments }} 条
          </div>
          <div class="flex items-center space-x-2">
            <!-- Jump to Page Input -->
            <div class="flex items-center bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-2 mr-2">
              <span class="text-[10px] uppercase font-bold text-slate-400 mr-2">跳至</span>
              <input 
                v-model="jumpPage"
                type="text"
                placeholder="..."
                class="w-10 bg-transparent border-none text-xs font-bold text-center focus:outline-none focus:ring-0 p-1"
                @keyup.enter="handleJump"
              />
              <button 
                @click="handleJump"
                class="ml-1 p-1 text-slate-400 hover:text-indigo-600 transition-colors"
                title="跳转"
              >
                <ArrowRight class="w-3 h-3" />
              </button>
            </div>

            <button 
              @click="currentPage > 1 && (currentPage--, loadData())"
              :disabled="currentPage === 1"
              class="p-2 border border-slate-200 dark:border-slate-700 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-700 disabled:opacity-30 transition-colors"
            >
              <ChevronLeft class="w-4 h-4" />
            </button>
            <div class="px-4 py-1.5 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg text-xs font-bold text-indigo-600 min-w-[80px] text-center">
              第 {{ currentPage }} / {{ Math.ceil(totalDocuments / pageSize) }} 页
            </div>
            <button 
              @click="currentPage * pageSize < totalDocuments && (currentPage++, loadData())"
              :disabled="currentPage * pageSize >= totalDocuments"
              class="p-2 border border-slate-200 dark:border-slate-700 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-700 disabled:opacity-30 transition-colors"
            >
              <ChevronRight class="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    </main>

    <!-- Batch Actions Bar -->
    <div v-if="selectedDocIds.size > 0" class="fixed bottom-8 left-1/2 -translate-x-1/2 z-40 animate-in slide-in-from-bottom-10 fade-in duration-300">
      <div class="bg-slate-900 text-white px-6 py-4 rounded-2xl shadow-2xl flex items-center space-x-6 border border-slate-700/50 backdrop-blur-md bg-opacity-90">
        <div class="flex items-center space-x-2">
            <span class="bg-indigo-500 text-xs font-bold px-2 py-1 rounded-full">{{ selectedDocIds.size }}</span>
            <span class="text-sm font-medium text-slate-300">个文档已选择</span>
        </div>
        <div class="h-6 w-px bg-slate-700"></div>
        <div class="flex items-center space-x-3">
            <button @click="handleBatchMove" class="flex items-center space-x-2 text-sm font-bold hover:text-indigo-400 transition">
                <Move class="w-4 h-4" />
                <span>批量移动</span>
            </button>
          <button @click="handleBatchDelete" class="flex items-center space-x-2 text-sm font-bold hover:text-red-400 transition">
            <Trash2 class="w-4 h-4" />
            <span>批量删除</span>
          </button>
            <button @click="selectedDocIds.clear()" class="flex items-center space-x-2 text-sm font-bold hover:text-red-400 transition">
                <X class="w-4 h-4" />
                <span>取消选择</span>
            </button>
        </div>
      </div>
    </div>

    <!-- Upload Modal -->
    <div v-if="isUploadingModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-slate-900/60 backdrop-blur-sm" @click="!isUploading && (isUploadingModalOpen = false)"></div>
      <div class="relative bg-white dark:bg-slate-800 rounded-2xl shadow-2xl w-full max-w-2xl p-0 overflow-hidden flex flex-col max-h-[80vh] animate-in fade-in zoom-in duration-200">
        
        <!-- Modal Header -->
        <div class="p-6 border-b border-slate-100 dark:border-slate-700 flex justify-between items-center">
            <h2 class="text-xl font-bold text-slate-800 dark:text-slate-100">批量上传文档</h2>
            <button v-if="!isUploading" @click="isUploadingModalOpen = false" class="text-slate-400 hover:text-slate-600">
                <X class="w-5 h-5" />
            </button>
        </div>

        <!-- Upload Body -->
        <div class="flex-1 overflow-y-auto p-6 space-y-4">
            <!-- Directory Selection -->
            <div class="space-y-2 mb-4">
              <label class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block">目标存储目录</label>
              <div class="flex items-center space-x-2">
                <div class="relative flex-1 group">
                  <Folder class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400 group-focus-within:text-indigo-500 transition-colors" />
                  <input 
                    v-model="uploadTargetPath" 
                    type="text" 
                    class="w-full pl-10 pr-20 py-2.5 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl text-sm outline-none focus:ring-2 focus:ring-indigo-500 font-medium transition-all"
                    placeholder="解析到根目录"
                  />
                  <div class="absolute right-2 top-1/2 -translate-y-1/2 flex items-center space-x-1">
                    <button 
                      v-if="uploadTargetPath"
                      @click="uploadTargetPath = uploadTargetPath.endsWith('/') ? uploadTargetPath : uploadTargetPath + '/'"
                      class="p-1 text-slate-400 hover:text-indigo-500 hover:bg-white dark:hover:bg-slate-800 rounded-md transition-colors"
                      title="添加子层级"
                    >
                      <FolderPlus class="w-4 h-4" />
                    </button>
                    <button 
                      @click="uploadTargetPath = ''" 
                      class="p-1 text-slate-400 hover:text-red-500 hover:bg-white dark:hover:bg-slate-800 rounded-md transition-colors"
                      title="重置"
                    >
                      <X class="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </div>
              
              <!-- Indicator for New path -->
              <div v-if="uploadTargetPath && !allPaths.has(uploadTargetPath)" class="px-3 py-1 bg-amber-50 dark:bg-amber-900/20 rounded-lg flex items-center space-x-2 border border-amber-100 dark:border-amber-800/50">
                 <div class="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse"></div>
                 <span class="text-[10px] font-bold text-amber-600 dark:text-amber-400 uppercase tracking-tighter">新路径: 入库时将自动创建此目录结构</span>
              </div>
              
              <!-- Quick Tree Selector for Upload -->
              <div class="mt-2 max-h-40 overflow-y-auto border border-slate-100 dark:border-slate-700 rounded-xl p-2 bg-slate-50/50 dark:bg-slate-900/30">
                <TreeSelector :nodes="directoryTree" :current-path="uploadTargetPath" @select="p => uploadTargetPath = p" />
              </div>
            </div>

            <!-- Date Selection -->
            <div class="flex items-center space-x-4 mb-4 bg-slate-50 dark:bg-slate-900/30 p-4 rounded-xl border border-slate-100 dark:border-slate-800">
                <div class="flex-shrink-0 text-slate-400">
                    <Clock class="w-5 h-5" />
                </div>
                <div class="flex-1">
                    <label class="block text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-1">文档归属日期</label>
                    <input 
                      v-model="documentDate" 
                      type="date" 
                      class="bg-transparent border-none p-0 text-sm font-bold text-slate-700 dark:text-slate-200 focus:ring-0 w-full outline-none"
                    />
                </div>
                <div class="text-[10px] text-slate-400 max-w-[120px] leading-tight">上传后将自动关联此日期并作为目录层级</div>
            </div>

            <!-- Dropzone -->
            <div 
              @click="fileInput?.click()"
              class="border-2 border-dashed border-slate-200 dark:border-slate-700 rounded-2xl p-10 flex flex-col items-center justify-center hover:border-indigo-400 transition-colors cursor-pointer"
            >
                <div class="p-4 bg-indigo-50 dark:bg-indigo-900/20 rounded-full mb-4">
                    <Upload class="w-8 h-8 text-indigo-500" />
                </div>
                <p class="text-sm font-bold text-slate-700 dark:text-slate-200">点击或将文件拖拽至此处</p>
                <p class="text-xs text-slate-400 mt-2">支持 PDF, Markdown, DOCX (最大 500MB)</p>
                <input type="file" ref="fileInput" multiple class="hidden" @change="handleFileSelect" />
            </div>

            <!-- Queue -->
            <div v-if="queue.length > 0" class="space-y-2">
                <div v-for="item in queue" :key="item.id" class="flex items-center space-x-4 bg-slate-50 dark:bg-slate-900/30 p-3 rounded-xl border border-slate-100 dark:border-slate-800">
                    <FileText class="w-5 h-5 text-slate-400" />
                    <div class="flex-1 min-w-0">
                        <div class="flex justify-between items-center mb-1">
                            <span class="text-xs font-bold text-slate-700 dark:text-slate-200 truncate">{{ item.name }}</span>
                            <span class="text-[10px] text-slate-400">{{ (item.size / 1024 / 1024).toFixed(2) }} MB</span>
                        </div>
                        <!-- Progress Bar -->
                        <div class="w-full bg-slate-200 dark:bg-slate-700 h-1 rounded-full overflow-hidden">
                            <div 
                              class="bg-indigo-500 h-full transition-all duration-300"
                              :style="{ width: `${item.progress}%` }"
                            ></div>
                        </div>
                    </div>
                    
                    <div class="flex items-center">
                        <CheckCircle2 v-if="item.status === 'completed'" class="w-5 h-5 text-emerald-500" />
                        <AlertCircle v-else-if="item.status === 'error'" class="w-5 h-5 text-red-500" :title="item.error" />
                        <Loader2 v-else-if="item.status === 'uploading'" class="w-4 h-4 text-indigo-500 animate-spin" />
                        <button v-if="item.status !== 'uploading' && !isUploading" @click="removeItem(item.id)" class="p-1 hover:text-red-500">
                            <X class="w-4 h-4" />
                        </button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Modal Footer -->
        <div class="p-6 bg-slate-50 dark:bg-slate-900/50 border-t border-slate-100 dark:border-slate-700 flex justify-between items-center">
            <div class="text-xs text-slate-500">
                <span v-if="isUploading">正在同步数据 ({{ uploadProgress }}%)...</span>
                <span v-else>{{ queue.length }} 个文件就绪</span>
            </div>
            <div class="flex space-x-3">
                <button 
                  @click="clearQueue" 
                  v-if="queue.length > 0 && !isUploading"
                  class="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg transition"
                >
                  清空列表
                </button>
                <button 
                  @click="handleIngest"
                  :disabled="isUploading || ingestLoading || queue.filter(f => f.status === 'completed').length === 0"
                  class="bg-indigo-600 hover:bg-indigo-700 text-white px-6 py-2 rounded-lg text-sm font-bold flex items-center space-x-2 transition disabled:opacity-50"
                >
                    <Loader2 v-if="ingestLoading" class="w-4 h-4 animate-spin" />
                    <Play v-else class="w-4 h-4" />
                    <span>开始解析并存库</span>
                </button>
            </div>
        </div>
      </div>
    </div>

    <!-- Rename Modal -->
    <div v-if="isRenameModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-slate-900/60 backdrop-blur-sm" @click="isRenameModalOpen = false"></div>
      <div class="relative bg-white dark:bg-slate-800 rounded-2xl shadow-2xl w-full max-w-md p-6 animate-in zoom-in duration-200">
        <h3 class="text-lg font-bold text-slate-800 dark:text-slate-100 mb-4">重命名目录</h3>
        <input 
          v-model="renameDirNewName" 
          type="text" 
          class="w-full px-4 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl mb-6 outline-none focus:ring-2 focus:ring-indigo-500"
          placeholder="输入新目录名称"
          @keyup.enter="submitRenameDir"
        />
        <div class="flex justify-end space-x-3">
          <button @click="isRenameModalOpen = false" class="px-4 py-2 text-sm text-slate-500 font-medium">取消</button>
          <button @click="submitRenameDir" class="bg-indigo-600 text-white px-6 py-2 rounded-xl text-sm font-bold shadow-lg shadow-indigo-500/20">确认重命名</button>
        </div>
      </div>
    </div>

    <!-- Move Modal -->
    <div v-if="isMoveModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-slate-900/60 backdrop-blur-sm" @click="isMoveModalOpen = false"></div>
      <div class="relative bg-white dark:bg-slate-800 rounded-2xl shadow-2xl w-full max-w-lg flex flex-col max-h-[70vh] animate-in zoom-in duration-200">
        <div class="p-6 border-b border-slate-100 dark:border-slate-700 flex justify-between items-center">
          <h3 class="text-lg font-bold text-slate-800 dark:text-slate-100">批量移动到目录</h3>
          <button @click="isMoveModalOpen = false" class="text-slate-400 hover:text-slate-600"><X class="w-5 h-5" /></button>
        </div>
        
        <div class="flex-1 overflow-y-auto p-6">
          <div class="mb-4">
            <label class="text-xs font-bold text-slate-400 uppercase tracking-widest block mb-2">目标路径</label>
            <input 
              v-model="moveTargetPath" 
              type="text" 
              class="w-full px-4 py-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 text-sm font-mono"
              placeholder="例如: 财务/2024"
            />
          </div>
          
          <div class="space-y-1">
            <label class="text-xs font-bold text-slate-400 uppercase tracking-widest block mb-1">快速选择</label>
            <button @click="moveTargetPath = ''" :class="moveTargetPath === '' ? 'bg-indigo-50 text-indigo-600 border-indigo-200' : 'bg-transparent text-slate-600 border-transparent'" class="w-full text-left px-3 py-2 rounded-lg text-sm flex items-center border transition-all">
                <Home class="w-4 h-4 mr-2" />
                <span>根目录 (未分类)</span>
            </button>
            <button @click="moveTargetPath = currentPath || ''" :class="moveTargetPath === (currentPath || '') ? 'bg-indigo-50 text-indigo-600 border-indigo-200' : 'bg-transparent text-slate-600 border-transparent'" class="w-full text-left px-3 py-2 rounded-lg text-sm flex items-center border transition-all">
                <Folder class="w-4 h-4 mr-2" />
                <span>当前目录 ({{ currentPath || '根目录' }})</span>
            </button>
            <button @click="moveTargetPath = parentPath" :class="moveTargetPath === parentPath ? 'bg-indigo-50 text-indigo-600 border-indigo-200' : 'bg-transparent text-slate-600 border-transparent'" class="w-full text-left px-3 py-2 rounded-lg text-sm flex items-center border transition-all">
                <ArrowLeft class="w-4 h-4 mr-2" />
                <span>上级目录 ({{ parentPath || '根目录' }})</span>
            </button>
            
            <div class="pt-2">
              <TreeSelector :nodes="directoryTree" :current-path="moveTargetPath" @select="p => moveTargetPath = p" />
            </div>
          </div>
        </div>

        <div class="p-6 bg-slate-50 dark:bg-slate-900/50 border-t border-slate-100 dark:border-slate-700 flex justify-end space-x-3">
          <button @click="isMoveModalOpen = false" class="px-4 py-2 text-sm text-slate-500 font-medium">取消</button>
          <button @click="submitBatchMove" class="bg-indigo-600 text-white px-6 py-2 rounded-xl text-sm font-bold shadow-lg shadow-indigo-500/20">开始移动 ({{ selectedDocIds.size }}个文档)</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
import { defineComponent, PropType } from 'vue';
import { Folder as FolderIcon, Home as HomeIcon, X as CloseIcon, Clock as ClockIcon } from 'lucide-vue-next';

// Helper component for recursive tree selection
const TreeSelector = defineComponent({
  name: 'TreeSelector',
  props: {
    nodes: { type: Array as PropType<any[]>, required: true },
    currentPath: { type: String, required: true },
    depth: { type: Number, default: 0 }
  },
  components: { FolderIcon, ClockIcon },
  emits: ['select'],
  template: `
    <div class="space-y-1">
      <div v-for="node in nodes" :key="node.path" class="space-y-1">
        <button 
          @click="$emit('select', node.path)"
          :style="{ paddingLeft: (depth * 16 + 12) + 'px' }"
          :class="currentPath === node.path ? 'bg-indigo-50 text-indigo-600 border-indigo-200' : 'bg-transparent text-slate-600 border-transparent hover:bg-slate-50'"
          class="w-full text-left py-2 rounded-lg text-sm flex items-center border transition-all"
        >
          <ClockIcon v-if="node.is_virtual" class="w-4 h-4 mr-2" :class="currentPath === node.path ? 'text-indigo-500' : 'text-slate-400'" />
          <FolderIcon v-else class="w-4 h-4 mr-2" :class="currentPath === node.path ? 'text-indigo-500' : 'text-slate-400'" />
          <span class="truncate">{{ node.name }}</span>
          <span v-if="node.is_virtual" class="text-[9px] ml-auto opacity-40 uppercase">日期</span>
        </button>
        <TreeSelector v-if="node.children && node.children.length > 0" :nodes="node.children" :current-path="currentPath" :depth="depth + 1" @select="p => $emit('select', p)" />
      </div>
    </div>
  `
});

export default {
  components: { TreeSelector, CloseIcon, HomeIcon }
}
</script>
