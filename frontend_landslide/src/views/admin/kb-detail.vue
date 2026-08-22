<script setup lang="ts">
import { ref, onMounted, computed, onUnmounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { KBDetailAPI, KnowledgeBase, type DirectoryTreeNode, type DocumentChunkItem, type IngestionBackend, type KbDocument, type RetrievalBackend } from '@/api/kb';
import { useUpload } from '@/composables/useUpload';
import KnowledgeGraphNebula from './components/KnowledgeGraphNebula.vue';
import TreeSelector from './components/TreeSelector.vue';
import { 
  ArrowLeft, Database, FileText, Upload, Trash2, 
  ArrowRight, Search, Loader2, X, CheckCircle2, AlertCircle, Play, ChevronRight, ChevronLeft, Folder, FolderPlus, Home,
  MoreVertical, Edit2, Move, CheckSquare, Square, Clock, Share2, Settings, GripVertical, Layers
} from 'lucide-vue-next';

const route = useRoute();
const router = useRouter();
const kbId = route.params.id as string;

const kb = ref<KnowledgeBase | null>(null);
const documents = ref<KbDocument[]>([]);
const directoryTree = ref<DirectoryTreeNode[]>([]);
const currentPath = ref('');
const loading = ref(true);
const loadError = ref('');
const documentsError = ref('');
const directoriesError = ref('');
const searchQuery = ref('');
const activePanel = ref<'documents' | 'graph'>('documents');
const isUploadingModalOpen = ref(false);
const ingestLoading = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

type IngestFileStatus = {
  file_id: string;
  file_name: string;
  status: 'pending' | 'processing' | 'completed' | 'failed' | string;
  error?: string | null;
};

type IngestTaskStatus = {
  task_id: string;
  kb_id: string;
  status: 'processing' | 'completed' | 'partial' | string;
  total: number;
  completed: number;
  failed: number;
  progress: number;
  files: IngestFileStatus[];
};

const latestIngestStatus = ref<IngestTaskStatus | null>(null);
const ingestStatusError = ref('');
let ingestPollTimer: number | null = null;

const isIngestPolling = computed(() => latestIngestStatus.value?.status === 'processing');
const failedIngestFiles = computed(() =>
  (latestIngestStatus.value?.files || []).filter(file => file.status === 'failed')
);

// Pagination State
const currentPage = ref(1);
const pageSize = ref(20);
const totalDocuments = ref(0);
const jumpPage = ref<string>("");

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
const isRenameSaving = ref(false);
const renameDirIsVirtual = ref(false);
const isDirectoryMoveSaving = ref(false);
const isKbEditModalOpen = ref(false);
const kbEditForm = ref({
  kb_name: '',
  subject: '',
  description: '',
  ingestion_backend: 'legacy_lightrag' as IngestionBackend,
  retrieval_backend: 'legacy_lightrag' as RetrievalBackend,
});

const INGESTION_OPTIONS: { value: IngestionBackend; label: string }[] = [
  { value: 'legacy_lightrag', label: '传统 LightRAG' },
  { value: 'self_ingestion', label: '自研入库' },
];
const RETRIEVAL_OPTIONS: { value: RetrievalBackend; label: string }[] = [
  { value: 'legacy_lightrag', label: '传统 LightRAG' },
  { value: 'self_ingestion', label: '自研检索 v2' },
];

const openKbEditModal = () => {
  if (!kb.value) return;
  kbEditForm.value = {
    kb_name: kb.value.kb_name,
    subject: kb.value.subject || '',
    description: kb.value.description || '',
    ingestion_backend: kb.value.ingestion_backend || 'legacy_lightrag',
    retrieval_backend: kb.value.retrieval_backend || 'legacy_lightrag',
  };
  isKbEditModalOpen.value = true;
};

const saveKbEdit = async () => {
  if (!kb.value || !kbEditForm.value.kb_name.trim()) {
    alert('请填写知识库名称');
    return;
  }
  const useSelfIngestion = kbEditForm.value.ingestion_backend === 'self_ingestion';
  const useSelfRetriever = kbEditForm.value.retrieval_backend !== 'legacy_lightrag';
  try {
    const updated = await KBDetailAPI.update(kbId, {
      kb_name: kbEditForm.value.kb_name.trim(),
      subject: kbEditForm.value.subject || undefined,
      description: kbEditForm.value.description || undefined,
      ingestion_backend: kbEditForm.value.ingestion_backend,
      retrieval_backend: kbEditForm.value.retrieval_backend,
      self_ingestion_enabled: useSelfIngestion,
      self_retriever_enabled: useSelfRetriever,
    });
    kb.value = updated as KnowledgeBase;
    isKbEditModalOpen.value = false;
  } catch {
    alert('更新失败，请确认后端配置合法');
  }
};
const moveTargetPath = ref('');
const uploadTargetPath = ref(''); // Target for new uploads
const renameDirOldPath = ref('');
const renameDirNewName = ref('');

const { queue, isUploading, addFiles, resumeItem, removeItem, clearQueue } = useUpload(kbId);
const acceptedUploadCount = computed(() => queue.value.filter(file => file.status === 'completed' && file.file_id).length);
const filteredUploadCount = computed(() => queue.value.filter(file => file.status === 'filtered').length);
const failedUploadCount = computed(() => queue.value.filter(file => file.status === 'error').length);
const pausedUploadCount = computed(() => queue.value.filter(file => file.status === 'paused').length);
const acceptedUploadTypes = [
  '.pdf', '.doc', '.docx', '.txt', '.md',
  '.xlsx', '.xls', '.ppt', '.pptx',
  '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff',
  '.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz',
].join(',');

const getErrorMessage = (error: unknown, fallback: string) => {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === 'object' && error && 'message' in error) {
    return String((error as { message?: unknown }).message || fallback);
  }
  return fallback;
};

const loadData = async () => {
  loading.value = true;
  loadError.value = '';
  documentsError.value = '';
  directoriesError.value = '';

  const documentParams: Record<string, any> = {
    skip: (currentPage.value - 1) * pageSize.value,
    limit: pageSize.value,
    // Always send category_path to ensure exact filtering (empty string = root)
    category_path: currentPath.value || ''
  };

  try {
    const [kbRes, docsRes, dirRes] = await Promise.allSettled([
      KBDetailAPI.get(kbId),
      KBDetailAPI.documents(kbId, documentParams),
      KBDetailAPI.getDirectories(kbId)
    ]);

    if (kbRes.status === 'fulfilled') {
      kb.value = kbRes.value as KnowledgeBase;
    } else {
      loadError.value = getErrorMessage(kbRes.reason, '知识库详情加载失败');
      console.error(kbRes.reason);
    }

    if (docsRes.status === 'fulfilled') {
      documents.value = docsRes.value.items || [];
      totalDocuments.value = docsRes.value.total || 0;
    } else {
      documents.value = [];
      totalDocuments.value = 0;
      documentsError.value = getErrorMessage(docsRes.reason, '文档列表加载失败');
      console.error(docsRes.reason);
    }

    if (dirRes.status === 'fulfilled') {
      directoryTree.value = dirRes.value.tree || [];
    } else {
      directoryTree.value = [];
      directoriesError.value = getErrorMessage(dirRes.reason, '目录树加载失败');
      console.error(dirRes.reason);
    }
  } finally {
    loading.value = false;
  }
};

const handleFileSelect = (e: Event) => {
  const input = e.target as HTMLInputElement;
  const files = input.files;
  if (files) {
    addFiles(files);
  }
  input.value = '';
};

const handleFileDrop = (e: DragEvent) => {
  if (e.dataTransfer?.files?.length) {
    addFiles(e.dataTransfer.files);
  }
};

const formatDetailedDate = (dateStr: string) => {
  const date = new Date(dateStr);
  return {
    date: date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).replace(/\//g, '-'),
    time: date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  };
};

const clearIngestPollTimer = () => {
  if (ingestPollTimer !== null) {
    window.clearTimeout(ingestPollTimer);
    ingestPollTimer = null;
  }
};

const ingestStatusLabel = (status: string) => {
  if (status === 'completed') return '已完成';
  if (status === 'partial') return '部分失败';
  if (status === 'failed') return '失败';
  return '处理中';
};

const ingestStatusClass = (status: string) => {
  if (status === 'completed') return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300';
  if (status === 'partial' || status === 'failed') return 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300';
  return 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300';
};

const pollIngestStatus = async (taskId: string) => {
  clearIngestPollTimer();
  ingestStatusError.value = '';

  const poll = async () => {
    try {
      const status = await KBDetailAPI.ingestStatus(taskId) as IngestTaskStatus;
      latestIngestStatus.value = status;

      if (status.status === 'processing') {
        ingestPollTimer = window.setTimeout(poll, 2000);
        return;
      }

      clearIngestPollTimer();
      await loadData();
    } catch (e: any) {
      ingestStatusError.value = e?.message || '入库状态查询失败';
      ingestPollTimer = window.setTimeout(poll, 4000);
    }
  };

  await poll();
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
      category_path: finalPath || null,
      options: {
        preserve_archive_paths: true,
      },
    });
    latestIngestStatus.value = {
      task_id: res.task_id,
      kb_id: kbId,
      status: 'processing',
      total: res.files_count || completedFiles.length,
      completed: 0,
      failed: 0,
      progress: 0,
      files: completedFiles.map(file => ({
        file_id: file.file_id!,
        file_name: file.name,
        status: 'processing',
      })),
    };
    void pollIngestStatus(res.task_id);
    clearQueue();
    isUploadingModalOpen.value = false;
    loadData();
  } catch (e: any) {
    alert('启动入库失败: ' + e.message);
  } finally {
    ingestLoading.value = false;
  }
};

onUnmounted(clearIngestPollTimer);

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
  renameDirOldPath.value = dir.path || dir.category_path || '';
  if (!renameDirOldPath.value) return;
  renameDirIsVirtual.value = Boolean(dir.is_virtual);
  renameDirNewName.value = dir.name;
  isRenameModalOpen.value = true;
};

const submitRenameDir = async () => {
  const oldPath = renameDirOldPath.value.trim();
  const newName = renameDirNewName.value.trim();
  if (!oldPath || !newName || isRenameSaving.value) return;
  if (renameDirIsVirtual.value && !/^\d{4}-\d{2}-\d{2}$/.test(newName)) {
    alert('虚拟日期目录名称必须是 YYYY-MM-DD');
    return;
  }

  isRenameSaving.value = true;
  try {
    const response = await KBDetailAPI.renameDirectory(kbId, oldPath, newName);
    const newPath = response.new_path || oldPath;
    updateCurrentPathAfterDirectoryChange(oldPath, newPath);
    isRenameModalOpen.value = false;
    await loadData();
  } catch (e: any) {
    alert('重命名失败: ' + getErrorMessage(e, '请求失败'));
  } finally {
    isRenameSaving.value = false;
  }
};

const handleDeleteDir = async (dir: any) => {
  const path = dir.path || dir.category_path;
  if (!confirm(`确定要删除目录 "${dir.name}" 吗？此操作不可恢复。`)) return;
  
  const deleteDocs = confirm('是否同时删除目录下的文档？');
  try {
    await KBDetailAPI.deleteDirectory(kbId, path, deleteDocs);
    await loadData();
  } catch (e: any) {
    alert('删除失败: ' + getErrorMessage(e, '请求失败'));
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

onMounted(loadData);

const findDirectoryNode = (nodes: DirectoryTreeNode[], path: string): DirectoryTreeNode | null => {
  for (const node of nodes) {
    if (node.path === path) return node;
    const child = findDirectoryNode(node.children || [], path);
    if (child) return child;
  }
  return null;
};

const currentLevelDirs = computed<DirectoryTreeNode[]>(() => {
  if (!currentPath.value) return directoryTree.value;
  return findDirectoryNode(directoryTree.value, currentPath.value)?.children || [];
});

const filteredDocs = computed(() => displayDocs.value);

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

const isSelfIngestionKb = computed(() => kb.value?.ingestion_backend === 'self_ingestion');
const isSelfIngestionDoc = (doc: KbDocument) => (
  doc.ingestion_backend === 'self_ingestion' || isSelfIngestionKb.value
);
const hasSelfIngestionResult = (doc: KbDocument) => (
  isSelfIngestionDoc(doc)
  && (
    (doc.chunk_count || 0) > 0
    || (doc.entity_count || 0) > 0
    || (doc.relation_count || 0) > 0
    || (doc.storage_status === 1 && doc.doc_status === 'visible')
  )
);

const canPreviewChunks = (doc: KbDocument) => (
  hasSelfIngestionResult(doc)
);

const documentStatusMeta = (doc: KbDocument) => {
  if (doc.parse_status === 2 || doc.doc_status === 'failed' || doc.storage_status === 2) {
    return {
      label: '入库失败',
      classes: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
    };
  }

  if (isSelfIngestionDoc(doc)) {
    if (hasSelfIngestionResult(doc)) {
      return {
        label: '入库成功',
        classes: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
      };
    }
    if (doc.doc_status === 'staged' || doc.doc_status === 'processing' || doc.parse_status === 1 || doc.storage_status === 0) {
      return {
        label: '入库中',
        classes: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
      };
    }
  }

  if (doc.storage_status === 1 || doc.parse_status === 1) {
    return {
      label: '入库成功',
      classes: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
    };
  }

  if (doc.doc_status === 'processing') {
    return {
      label: '入库中',
      classes: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
    };
  }

  return {
    label: '待入库',
    classes: 'bg-surface-sunken text-text-secondary',
  };
};

const dragSortEnabled = computed(() => !searchQuery.value.trim());
const reorderSaving = ref(false);
const directoryDropTargetPath = ref<string | null>(null);
const dragItem = ref<{ kind: 'dir' | 'doc'; index: number; path?: string; isVirtual?: boolean } | null>(null);
const dragOverIndex = ref<{ kind: 'dir' | 'doc'; index: number } | null>(null);

const sortedLevelDirs = computed(() => {
  return [...currentLevelDirs.value].sort((a, b) => {
    const orderDiff = (a.sort_order ?? 0) - (b.sort_order ?? 0);
    if (orderDiff !== 0) return orderDiff;
    return String(a.name).localeCompare(String(b.name), 'zh-CN');
  });
});

const draggableDirs = computed(() => sortedLevelDirs.value.filter((dir) => !dir.is_virtual));

const displayDocs = computed(() => {
  const query = searchQuery.value.trim().toLowerCase();
  const rows = query
    ? documents.value.filter((doc) => doc.file_name.toLowerCase().includes(query))
    : documents.value;
  return rows;
});

const onDragStart = (
  kind: 'dir' | 'doc',
  index: number,
  event: DragEvent,
  directory?: DirectoryTreeNode,
) => {
  if (!dragSortEnabled.value) return;
  if (kind === 'doc' && !isSelfIngestionKb.value) return;
  const path = kind === 'dir' ? directory?.path || draggableDirs.value[index]?.path : undefined;
  if (kind === 'dir' && !path) return;
  dragItem.value = {
    kind,
    index,
    path,
    isVirtual: kind === 'dir' ? Boolean(directory?.is_virtual) : false,
  };
  directoryDropTargetPath.value = null;
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', path || `${kind}:${index}`);
  }
};

const onDragOverRow = (kind: 'dir' | 'doc', index: number, event: DragEvent) => {
  if (!dragItem.value || dragItem.value.kind !== kind) return;
  event.preventDefault();
  event.dataTransfer && (event.dataTransfer.dropEffect = 'move');
  directoryDropTargetPath.value = null;
  dragOverIndex.value = { kind, index };
};

const onDragLeaveRow = () => {
  dragOverIndex.value = null;
};

const onDragEnd = () => {
  dragItem.value = null;
  dragOverIndex.value = null;
  directoryDropTargetPath.value = null;
};

const reorderList = <T,>(items: T[], from: number, to: number) => {
  const next = [...items];
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
};

const onDropRow = async (kind: 'dir' | 'doc', targetIndex: number, event: DragEvent) => {
  event.preventDefault();
  dragOverIndex.value = null;
  const source = dragItem.value;
  if (!source || source.kind !== kind || source.index === targetIndex || (kind === 'dir' && source.isVirtual)) {
    onDragEnd();
    return;
  }

  reorderSaving.value = true;
  try {
    if (kind === 'dir') {
      const dirs = draggableDirs.value;
      const reordered = reorderList(dirs, source.index, targetIndex);
      await KBDetailAPI.reorderDirectories(kbId, {
        parent_path: currentPath.value || null,
        paths: reordered.map((dir) => dir.path),
      });
    } else {
      const reordered = reorderList(displayDocs.value, source.index, targetIndex);
      documents.value = reordered;
      await KBDetailAPI.reorderDocuments(kbId, {
        category_path: currentPath.value || '',
        doc_ids: reordered.map((doc) => doc.doc_id),
      });
    }
    await loadData();
  } catch (error: any) {
    alert(getErrorMessage(error, '排序保存失败'));
    await loadData();
  } finally {
    reorderSaving.value = false;
    onDragEnd();
  }
};

const onDragOverDirTarget = (path: string, event: DragEvent) => {
  const sourcePath = dragItem.value?.kind === 'dir' ? dragItem.value.path : null;
  if (!sourcePath || sourcePath === path) return;
  event.preventDefault();
  event.stopPropagation();
  event.dataTransfer && (event.dataTransfer.dropEffect = 'move');
  dragOverIndex.value = null;
  directoryDropTargetPath.value = path;
};

const replaceDirectoryPathPrefix = (path: string, oldPath: string, newPath: string) => {
  if (path === oldPath) return newPath;
  if (path.startsWith(`${oldPath}/`)) return `${newPath}${path.slice(oldPath.length)}`;
  return path;
};

const updateCurrentPathAfterDirectoryChange = (oldPath: string, newPath: string) => {
  if (currentPath.value === oldPath || currentPath.value.startsWith(`${oldPath}/`)) {
    currentPath.value = replaceDirectoryPathPrefix(currentPath.value, oldPath, newPath);
    currentPage.value = 1;
  }
};

const onDropDirInto = async (targetPath: string, event: DragEvent) => {
  event.preventDefault();
  event.stopPropagation();
  const source = dragItem.value;
  const sourcePath = source?.kind === 'dir' ? source.path : null;
  if (!sourcePath || sourcePath === targetPath || isDirectoryMoveSaving.value) {
    onDragEnd();
    return;
  }

  isDirectoryMoveSaving.value = true;
  directoryDropTargetPath.value = null;
  try {
    const response = await KBDetailAPI.moveDirectory(kbId, sourcePath, targetPath);
    updateCurrentPathAfterDirectoryChange(sourcePath, response.new_path);
    await loadData();
  } catch (error: any) {
    alert(getErrorMessage(error, '目录移动失败'));
    await loadData();
  } finally {
    isDirectoryMoveSaving.value = false;
    onDragEnd();
  }
};

const chunkPanelOpen = ref(false);
const chunkDoc = ref<KbDocument | null>(null);
const chunkItems = ref<DocumentChunkItem[]>([]);
const chunkTotal = ref(0);
const chunkLoading = ref(false);
const chunkPage = ref(1);
const chunkPageSize = 20;

const loadDocumentChunks = async () => {
  if (!chunkDoc.value) return;
  chunkLoading.value = true;
  try {
    const res = await KBDetailAPI.documentChunks(kbId, chunkDoc.value.doc_id, {
      skip: (chunkPage.value - 1) * chunkPageSize,
      limit: chunkPageSize,
    });
    chunkItems.value = res.items || [];
    chunkTotal.value = res.total || 0;
  } catch (error: any) {
    alert(error?.message || '加载分块失败');
    chunkItems.value = [];
    chunkTotal.value = 0;
  } finally {
    chunkLoading.value = false;
  }
};

const openChunkPanel = async (doc: KbDocument) => {
  if (!canPreviewChunks(doc)) return;
  chunkDoc.value = doc;
  chunkPage.value = 1;
  chunkPanelOpen.value = true;
  await loadDocumentChunks();
};

const closeChunkPanel = () => {
  chunkPanelOpen.value = false;
  chunkDoc.value = null;
  chunkItems.value = [];
};

const changeChunkPage = async (delta: number) => {
  const pages = Math.max(1, Math.ceil(chunkTotal.value / chunkPageSize));
  chunkPage.value = Math.min(pages, Math.max(1, chunkPage.value + delta));
  await loadDocumentChunks();
};

const onDragStartDir = (path: string, event: DragEvent) => {
  const directory = sortedLevelDirs.value.find((dir) => dir.path === path);
  if (!directory) return;
  const index = draggableDirs.value.findIndex((dir) => dir.path === path);
  onDragStart('dir', index, event, directory);
};

const onDropDir = (path: string, event: DragEvent) => {
  const directory = sortedLevelDirs.value.find((dir) => dir.path === path);
  if (!directory || directory.is_virtual || dragItem.value?.isVirtual) {
    event.preventDefault();
    onDragEnd();
    return;
  }
  const index = draggableDirs.value.findIndex((dir) => dir.path === path);
  if (index < 0) return;
  void onDropRow('dir', index, event);
};

const onDragOverDir = (path: string, event: DragEvent) => {
  const directory = sortedLevelDirs.value.find((dir) => dir.path === path);
  if (!directory || directory.is_virtual || dragItem.value?.isVirtual) return;
  const index = draggableDirs.value.findIndex((dir) => dir.path === path);
  if (index < 0) return;
  onDragOverRow('dir', index, event);
};

const rowDragClass = (kind: 'dir' | 'doc', index: number) => {
  if (!dragOverIndex.value || dragOverIndex.value.kind !== kind || dragOverIndex.value.index !== index) {
    return '';
  }
  return 'ring-2 ring-inset ring-accent/30 bg-accent-soft';
};

const dirDragIndex = (path: string) => draggableDirs.value.findIndex((dir) => dir.path === path);
</script>

<template>
  <div class="min-h-screen bg-surface-sunken flex flex-col">
    <!-- Breadcrumbs & Actions -->
    <div class="flex flex-col md:flex-row md:items-center justify-between gap-6 mb-8 pb-6 border-b border-border-subtle">
      <div class="flex items-center space-x-4">
        <button @click="router.back()" class="p-2.5 bg-surface/70 border border-border-subtle rounded-2xl hover:bg-surface-hover hover:text-accent transition-colors shadow-sm hover-lift">
          <ArrowLeft class="w-4.5 h-4.5 text-text-secondary" />
        </button>
        <div>
          <div class="flex items-center space-x-1.5 text-[10px] font-bold text-text-tertiary uppercase tracking-widest mb-1.5">
            <span>知识库</span>
            <ChevronRight class="w-3 h-3" />
            <span>管理详情</span>
          </div>
          <h2 class="text-3xl font-extrabold tracking-tight text-text-primary flex items-center space-x-3">
             <Database class="w-7 h-7 text-accent" />
             <span>{{ kb?.kb_name || (loading ? '正在加载...' : '知识库加载失败') }}</span>
          </h2>
          <p v-if="loadError" class="mt-2 text-xs font-semibold text-red-500 dark:text-red-400">
            {{ loadError }}
          </p>
        </div>
      </div>
      
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex items-center bg-surface/80 border border-border-subtle rounded-2xl p-1 shadow-sm backdrop-blur-sm">
          <button
            @click="activePanel = 'documents'"
            class="px-4 py-2 rounded-xl text-xs font-bold flex items-center space-x-1.5 transition-colors hover-lift"
            :class="activePanel === 'documents' ? 'bg-accent text-white shadow-sm' : 'text-text-secondary hover:text-text-primary'"
          >
            <FileText class="w-3.5 h-3.5" />
            <span>文档</span>
          </button>
          <button
            @click="activePanel = 'graph'"
            class="px-4 py-2 rounded-xl text-xs font-bold flex items-center space-x-1.5 transition-colors hover-lift"
            :class="activePanel === 'graph' ? 'bg-accent text-white shadow-sm' : 'text-text-secondary hover:text-text-primary'"
          >
            <Share2 class="w-3.5 h-3.5" />
            <span>知识图谱</span>
          </button>
        </div>

        <div v-if="activePanel === 'documents'" class="relative">
          <Search class="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-text-tertiary" />
          <input 
            v-model="searchQuery"
            type="text" 
            placeholder="搜索文档..." 
            class="pl-10 pr-4 py-2.5 bg-surface/70 border border-border-subtle rounded-2xl text-xs focus:ring-2 focus:ring-accent/20 focus:border-accent outline-none w-64 transition-all shadow-sm text-text-primary"
          />
        </div>

        <button
          @click="openKbEditModal"
          class="bg-surface/80 border border-border-subtle text-text-secondary px-4 py-2.5 rounded-2xl text-xs font-bold flex items-center space-x-2 transition-all hover:bg-surface-hover hover-lift"
          title="编辑知识库设置"
        >
          <Settings class="w-3.5 h-3.5" />
          <span>设置</span>
        </button>

        <button 
          @click="isUploadingModalOpen = true; uploadTargetPath = currentPath"
          class="bg-accent hover:bg-accent-hover text-white px-5 py-2.5 rounded-2xl text-xs font-bold flex items-center space-x-2 transition-all shadow-sm hover-lift active:scale-95"
        >
          <Upload class="w-4 h-4" />
          <span>上传文档</span>
        </button>
      </div>
    </div>

    <!-- Path Breadcrumbs -->
    <div v-if="activePanel === 'documents'" class="flex items-center space-x-2 text-xs font-semibold mb-6 bg-surface/60 backdrop-blur-md p-3 rounded-2xl border border-border-subtle shadow-sm mx-6">
      <button 
        @click="setPath('')"
        class="flex items-center space-x-1.5 hover:text-accent transition-colors"
        :class="!currentPath ? 'text-accent font-bold' : 'text-text-tertiary'"
      >
        <Home class="w-4 h-4" />
        <span>根目录</span>
      </button>
      <template v-for="(crumb, index) in breadcrumbs" :key="crumb.path">
        <ChevronRight class="w-3.5 h-3.5 text-text-tertiary" />
        <button 
          @click="setPath(crumb.path)"
          class="hover:text-accent transition-colors"
          :class="index === breadcrumbs.length - 1 ? 'text-accent font-bold' : 'text-text-tertiary'"
        >
          {{ crumb.name }}
        </button>
      </template>
    </div>

    <!-- Main Content -->
    <main
      class="flex-1 w-full"
      :class="activePanel === 'graph' ? 'p-6 pt-2 pb-6 max-w-none' : 'p-6 pb-32 max-w-7xl mx-auto grid grid-cols-1 lg:grid-cols-4 gap-6'"
    >
      
      <!-- Stats Sidebar -->
      <aside v-if="activePanel === 'documents'" class="space-y-6 lg:col-span-1">
        <div class="bg-surface/80 backdrop-blur-md p-6 rounded-3xl border border-border-subtle shadow-sm relative group overflow-hidden">
          <div class="absolute top-0 left-0 right-0 h-1 bg-accent transform scale-x-0 group-hover:scale-x-100 transition-transform duration-300"></div>
          <h3 class="text-xs font-bold text-text-tertiary uppercase tracking-widest mb-6">数据库指标</h3>
          
          <div class="space-y-5">
            <div>
              <div class="text-[10px] text-text-tertiary uppercase tracking-widest font-bold mb-1">文档数量</div>
              <div class="text-2xl font-extrabold text-text-primary">{{ kb?.doc_count || 0 }}</div>
            </div>
            <div class="h-px bg-surface-sunken"></div>
            <div>
              <div class="text-[10px] text-text-tertiary uppercase tracking-widest font-bold mb-1">活跃实体</div>
              <div class="text-2xl font-extrabold text-text-primary">{{ kb?.entity_count || 0 }}</div>
            </div>
            <div class="h-px bg-surface-sunken"></div>
            <div>
              <div class="text-[10px] text-text-tertiary uppercase tracking-widest font-bold mb-1">知识关系</div>
              <div class="text-2xl font-extrabold text-text-primary">{{ kb?.relation_count || 0 }}</div>
            </div>
          </div>
        </div>

        <div class="bg-accent-soft p-6 rounded-3xl border border-accent/30">
          <h4 class="text-accent font-bold text-xs mb-2">快速提示</h4>
          <p class="text-[11px] text-accent/80 leading-relaxed font-medium">
            上传 PDF 或 Markdown 文档后，请点击“开始解析”按钮将内容向量化至当前知识库。
          </p>
        </div>

      </aside>

      <!-- Document List / Knowledge Graph -->
      <div :class="activePanel === 'graph' ? 'w-full min-h-0' : 'lg:col-span-3 space-y-4'">
        <KnowledgeGraphNebula
          v-if="activePanel === 'graph'"
          :kb-id="kbId"
          :kb-name="kb?.kb_name"
          full-page
        />

        <template v-else>
	        <div
	          v-if="latestIngestStatus"
	          class="rounded-2xl border border-border-subtle bg-surface/85 p-4 shadow-sm"
	        >
	          <div class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
	            <div class="min-w-0">
	              <div class="flex flex-wrap items-center gap-2">
	                <span
	                  class="rounded-full px-2.5 py-1 text-[11px] font-bold"
	                  :class="ingestStatusClass(latestIngestStatus.status)"
	                >
	                  {{ ingestStatusLabel(latestIngestStatus.status) }}
	                </span>
	                <span class="truncate text-xs font-semibold text-text-secondary">
	                  入库任务 {{ latestIngestStatus.task_id }}
	                </span>
	              </div>
	              <p class="mt-2 text-xs text-text-secondary">
	                共 {{ latestIngestStatus.total }} 个文件，已完成 {{ latestIngestStatus.completed }} 个，失败 {{ latestIngestStatus.failed }} 个
	              </p>
	            </div>
	            <button
	              v-if="isIngestPolling"
	              class="inline-flex items-center justify-center gap-2 rounded-xl border border-border-subtle px-3 py-2 text-xs font-bold text-text-secondary"
	              disabled
	            >
	              <Loader2 class="h-3.5 w-3.5 animate-spin" />
	              <span>同步状态</span>
	            </button>
	          </div>
	          <div class="mt-3 h-2 overflow-hidden rounded-full bg-surface-sunken">
	            <div
	              class="h-full rounded-full transition-all duration-500"
	              :class="latestIngestStatus.failed > 0 ? 'bg-red-500' : 'bg-emerald-500'"
	              :style="{ width: `${Math.max(3, Math.min(100, latestIngestStatus.progress || 0))}%` }"
	            ></div>
	          </div>
	          <p v-if="ingestStatusError" class="mt-2 text-xs font-medium text-amber-600 dark:text-amber-400">
	            {{ ingestStatusError }}
	          </p>
	          <div v-if="failedIngestFiles.length > 0" class="mt-3 space-y-1.5">
	            <div
	              v-for="file in failedIngestFiles"
	              :key="file.file_id"
	              class="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/30 dark:text-red-300"
	            >
	              <span class="font-bold">{{ file.file_name }}</span>
	              <span v-if="file.error">：{{ file.error }}</span>
	            </div>
	          </div>
	        </div>

	        <div v-if="loading" class="flex flex-col items-center justify-center py-20 text-text-secondary">
          <Loader2 class="w-9 h-9 animate-spin mb-4 text-accent" />
          <p class="text-sm font-semibold tracking-wide">正在拉取文档列表...</p>
        </div>

        <div
          v-else-if="documentsError || directoriesError"
          class="rounded-3xl border border-red-100 bg-red-50/70 p-5 text-sm font-semibold text-red-700 dark:border-red-900/40 dark:bg-red-950/25 dark:text-red-300"
        >
          <p v-if="documentsError">{{ documentsError }}</p>
          <p v-if="directoriesError" class="mt-1">{{ directoriesError }}</p>
        </div>

        <div v-else-if="currentLevelDirs.length === 0 && filteredDocs.length === 0" class="flex flex-col items-center justify-center py-20 text-text-secondary border-2 border-dashed border-border-subtle rounded-3xl bg-white/35">
          <FileText class="w-12 h-12 mb-4 text-text-tertiary" />
          <p class="text-sm font-semibold">当前目录没有符合条件的文档或子目录</p>
        </div>

        <div v-else class="bg-surface/80 backdrop-blur-md rounded-t-3xl border border-border-subtle overflow-x-auto shadow-sm">
          <div v-if="dragSortEnabled && (sortedLevelDirs.length > 0 || (isSelfIngestionKb && displayDocs.length > 0))" class="px-6 py-2 border-b border-border-subtle text-[10px] text-text-tertiary flex items-center gap-2">
            <GripVertical class="w-3.5 h-3.5" />
            <span>拖拽左侧手柄可调整{{ isSelfIngestionKb ? '目录与文档' : '目录' }}顺序；拖到文件夹名称上可移入该目录</span>
            <Loader2 v-if="reorderSaving || isDirectoryMoveSaving" class="w-3.5 h-3.5 animate-spin text-accent" />
          </div>
          <table class="w-full text-sm text-left">
            <thead class="bg-surface-sunken text-text-secondary font-bold border-b border-border-subtle">
              <tr>
                <th v-if="dragSortEnabled" class="px-3 py-4 w-8"></th>
                <th class="px-6 py-4 w-10">
                  <button @click="toggleSelectAll" class="text-text-tertiary hover:text-accent transition-colors">
                    <CheckSquare v-if="selectedDocIds.size === filteredDocs.length && filteredDocs.length > 0" class="w-5 h-5 text-accent" />
                    <Square v-else class="w-5 h-5" />
                  </button>
                </th>
                <th class="px-6 py-4">文件名</th>
                <th class="px-6 py-4">状态</th>
                <th class="px-6 py-4">添加时间</th>
                <th class="px-6 py-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border-subtle">
              <!-- Folders First -->
              <tr
                v-for="dir in sortedLevelDirs"
                :key="dir.path"
                class="hover:bg-surface-hover transition-colors group"
                :class="[ !dir.is_virtual ? rowDragClass('dir', dirDragIndex(dir.path)) : '',
                  directoryDropTargetPath === dir.path ? 'ring-2 ring-inset ring-emerald-400 bg-emerald-50/60 dark:bg-emerald-950/20' : '',
                ]"
                :draggable="dragSortEnabled"
                @dragstart="onDragStartDir(dir.path, $event)"
                @dragover="onDragOverDir(dir.path, $event)"
                @dragleave="onDragLeaveRow"
                @dragend="onDragEnd"
                @drop="onDropDir(dir.path, $event)"
              >
                <td v-if="dragSortEnabled" class="px-3 py-4">
                  <GripVertical
                    v-if="dragSortEnabled"
                    class="w-4 h-4 text-text-tertiary group-hover:text-text-secondary cursor-grab active:cursor-grabbing"
                  />
                </td>
                <td class="px-6 py-4">
                  <div class="w-5 h-5"></div>
                </td>
                <td
                  class="px-6 py-4 cursor-pointer"
                  @click="setPath(dir.path)"
                  @dragover.stop.prevent="!dir.is_virtual && onDragOverDirTarget(dir.path, $event)"
                  @drop.stop.prevent="!dir.is_virtual && onDropDirInto(dir.path, $event)"
                >
                  <div class="flex items-center space-x-3">
                    <Clock v-if="dir.is_virtual" class="w-5 h-5 text-accent fill-indigo-400/10" />
                    <Folder v-else class="w-5 h-5 text-amber-500 fill-amber-500/20" />
                    <span class="font-bold text-text-secondary">{{ dir.name }}</span>
                    <span v-if="dir.is_virtual" class="text-[10px] px-1.5 py-0.5 bg-surface-sunken text-text-tertiary rounded uppercase tracking-tighter">虚拟</span>
                  </div>
                </td>
                <td class="px-6 py-4 text-xs font-medium text-text-tertiary">目录</td>
                <td class="px-6 py-4 text-text-tertiary">—</td>
                <td class="px-6 py-4 text-right">
                   <div class="flex items-center justify-end space-x-2">
                     <button @click.stop="handleRenameDir(dir)" class="p-1.5 text-text-tertiary hover:text-accent transition-colors opacity-0 group-hover:opacity-100">
                       <Edit2 class="w-4 h-4" />
                     </button>
                     <button v-if="!dir.is_virtual" @click.stop="handleDeleteDir(dir)" class="p-1.5 text-text-tertiary hover:text-red-500 transition-colors opacity-0 group-hover:opacity-100">
                       <Trash2 class="w-4 h-4" />
                     </button>
                     <ChevronRight class="w-4 h-4 text-text-tertiary" />
                   </div>
                </td>
              </tr>

              <!-- Documents -->
              <tr
                v-for="(doc, docIndex) in displayDocs"
                :key="doc.id"
                class="hover:bg-surface-hover transition-colors group"
                :class="isSelfIngestionKb && dragSortEnabled ? rowDragClass('doc', docIndex) : ''"
                :draggable="isSelfIngestionKb && dragSortEnabled"
                @dragstart="onDragStart('doc', docIndex, $event)"
                @dragover="onDragOverRow('doc', docIndex, $event)"
                @dragleave="onDragLeaveRow"
                @dragend="onDragEnd"
                @drop="onDropRow('doc', docIndex, $event)"
              >
                <td v-if="dragSortEnabled" class="px-3 py-4">
                  <GripVertical
                    v-if="isSelfIngestionKb"
                    class="w-4 h-4 text-text-tertiary group-hover:text-text-secondary cursor-grab active:cursor-grabbing"
                  />
                </td>
                <td class="px-6 py-4">
                  <button @click="toggleSelect(doc.doc_id)" class="text-text-tertiary hover:text-accent transition-colors">
                    <CheckSquare v-if="selectedDocIds.has(doc.doc_id)" class="w-5 h-5 text-accent" />
                    <Square v-else class="w-5 h-5" />
                  </button>
                </td>
                <td class="px-6 py-4">
                  <button
                    v-if="canPreviewChunks(doc)"
                    type="button"
                    class="flex items-center space-x-3 text-left w-full"
                    @click="openChunkPanel(doc)"
                  >
                    <FileText class="w-5 h-5 text-accent shrink-0" />
                    <span class="font-bold text-text-secondary truncate max-w-xs hover:text-accent">{{ doc.file_name }}</span>
                    <span v-if="doc.chunk_count" class="text-[10px] px-1.5 py-0.5 rounded bg-surface-sunken text-text-secondary">{{ doc.chunk_count }} 块</span>
                  </button>
                  <div v-else class="flex items-center space-x-3">
                    <FileText class="w-5 h-5 text-accent" />
                    <span class="font-bold text-text-secondary truncate max-w-xs">{{ doc.file_name }}</span>
                  </div>
                </td>
                <td class="px-6 py-4 text-xs font-medium">
                  <span 
                    class="px-2 py-1 rounded-md"
                    :class="documentStatusMeta(doc).classes"
                  >
                    {{ documentStatusMeta(doc).label }}
                  </span>
                </td>
                <td class="px-6 py-4 text-text-tertiary group-hover:text-text-secondary transition-colors">
                  <div class="flex flex-col">
                    <span class="font-medium text-text-secondary text-xs">{{ formatDetailedDate(doc.created_at).date }}</span>
                    <span class="text-[10px] opacity-60 font-mono">{{ formatDetailedDate(doc.created_at).time }}</span>
                  </div>
                </td>
                <td class="px-6 py-4 text-right">
                  <div class="flex items-center justify-end space-x-2">
                    <button
                      v-if="canPreviewChunks(doc)"
                      @click="openChunkPanel(doc)"
                      class="p-2 text-text-tertiary hover:text-accent transition-colors opacity-0 group-hover:opacity-100"
                      title="查看分块"
                    >
                      <Layers class="w-4 h-4" />
                    </button>
                    <button 
                      @click="handleDeleteDoc(doc.doc_id)"
                      class="p-2 text-text-tertiary hover:text-red-500 transition-colors opacity-0 group-hover:opacity-100"
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
        <div v-if="filteredDocs.length > 0 || currentPage > 1" class="flex items-center justify-between px-6 py-4 bg-surface border-x border-b border-border-subtle rounded-b-2xl shadow-sm">
          <div class="text-xs text-text-secondary font-medium">
            显示第 {{ (currentPage - 1) * pageSize + 1 }} - {{ Math.min(currentPage * pageSize, totalDocuments) }} 条，共 {{ totalDocuments }} 条
          </div>
          <div class="flex items-center space-x-2">
            <!-- Jump to Page Input -->
            <div class="flex items-center bg-surface-sunken border border-border-subtle rounded-lg px-2 mr-2">
              <span class="text-[10px] uppercase font-bold text-text-tertiary mr-2">跳至</span>
              <input 
                v-model="jumpPage"
                type="text"
                placeholder="..."
                class="w-10 bg-transparent border-none text-xs font-bold text-center focus:outline-none focus:ring-0 p-1"
                @keyup.enter="handleJump"
              />
              <button 
                @click="handleJump"
                class="ml-1 p-1 text-text-tertiary hover:text-accent transition-colors"
                title="跳转"
              >
                <ArrowRight class="w-3 h-3" />
              </button>
            </div>

            <button 
              @click="currentPage > 1 && (currentPage--, loadData())"
              :disabled="currentPage === 1"
              class="p-2 border border-border-subtle rounded-lg hover:bg-surface-hover disabled:opacity-30 transition-colors"
            >
              <ChevronLeft class="w-4 h-4" />
            </button>
            <div class="px-4 py-1.5 bg-surface-sunken border border-border-subtle rounded-lg text-xs font-bold text-accent min-w-[80px] text-center">
              第 {{ currentPage }} / {{ Math.ceil(totalDocuments / pageSize) }} 页
            </div>
            <button 
              @click="currentPage * pageSize < totalDocuments && (currentPage++, loadData())"
              :disabled="currentPage * pageSize >= totalDocuments"
              class="p-2 border border-border-subtle rounded-lg hover:bg-surface-hover disabled:opacity-30 transition-colors"
            >
              <ChevronRight class="w-4 h-4" />
            </button>
          </div>
        </div>
        </template>
      </div>
    </main>

    <!-- Batch Actions Bar -->
    <div v-if="selectedDocIds.size > 0" class="fixed bottom-8 left-1/2 -translate-x-1/2 z-40 animate-in slide-in-from-bottom-10 fade-in duration-300">
      <div class="bg-accent text-accent-fg px-6 py-4 rounded-2xl shadow-sm flex items-center space-x-6 border border-border-strong backdrop-blur-md bg-opacity-90">
        <div class="flex items-center space-x-2">
            <span class="bg-accent text-xs font-bold px-2 py-1 rounded-full">{{ selectedDocIds.size }}</span>
            <span class="text-sm font-medium text-text-tertiary">个文档已选择</span>
        </div>
        <div class="h-6 w-px bg-surface"></div>
        <div class="flex items-center space-x-3">
            <button @click="handleBatchMove" class="flex items-center space-x-2 text-sm font-bold hover:text-accent transition">
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
    <Transition name="pop">
    <div v-if="isUploadingModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="批量上传文档">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="!isUploading && (isUploadingModalOpen = false)"></div>
      <div class="relative flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-white/55 bg-surface/90 p-0 shadow-2xl backdrop-blur-2xl pop-panel dark:border-white/10 dark:bg-surface/95">
        
        <!-- Modal Header -->
        <div class="p-6 border-b border-border-subtle flex justify-between items-center">
            <h2 class="text-xl font-bold text-text-primary">批量上传文档</h2>
            <button v-if="!isUploading" @click="isUploadingModalOpen = false" class="text-text-tertiary hover:text-text-secondary">
                <X class="w-5 h-5" />
            </button>
        </div>

        <!-- Upload Body -->
        <div class="flex-1 overflow-y-auto p-6 space-y-4">
            <!-- Directory Selection -->
            <div class="space-y-2 mb-4">
              <label class="text-[10px] font-bold text-text-tertiary uppercase tracking-widest block">目标存储目录</label>
              <div class="flex items-center space-x-2">
                <div class="relative flex-1 group">
                  <Folder class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-tertiary group-focus-within:text-accent transition-colors" />
                  <input 
                    v-model="uploadTargetPath" 
                    type="text" 
                    class="w-full pl-10 pr-20 py-2.5 bg-surface-sunken border border-border-subtle rounded-xl text-sm outline-none focus:ring-2 focus:ring-accent/30 font-medium transition-all"
                    placeholder="解析到根目录"
                  />
                  <div class="absolute right-2 top-1/2 -translate-y-1/2 flex items-center space-x-1">
                    <button 
                      v-if="uploadTargetPath"
                      @click="uploadTargetPath = uploadTargetPath.endsWith('/') ? uploadTargetPath : uploadTargetPath + '/'"
                      class="p-1 text-text-tertiary hover:text-accent hover:bg-surface rounded-md transition-colors"
                      title="添加子层级"
                    >
                      <FolderPlus class="w-4 h-4" />
                    </button>
                    <button 
                      @click="uploadTargetPath = ''" 
                      class="p-1 text-text-tertiary hover:text-red-500 hover:bg-surface rounded-md transition-colors"
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
              <div class="mt-2 max-h-40 overflow-y-auto border border-border-subtle rounded-xl p-2 bg-surface-sunken">
                <TreeSelector :nodes="directoryTree" :current-path="uploadTargetPath" @select="p => uploadTargetPath = p" />
              </div>
            </div>

            <!-- Date Selection -->
            <div class="flex items-center space-x-4 mb-4 bg-surface-sunken p-4 rounded-xl border border-border-subtle">
                <div class="flex-shrink-0 text-text-tertiary">
                    <Clock class="w-5 h-5" />
                </div>
                <div class="flex-1">
                    <label class="block text-[10px] font-bold text-text-tertiary uppercase tracking-widest mb-1">文档归属日期</label>
                    <input 
                      v-model="documentDate" 
                      type="date" 
                      class="bg-transparent border-none p-0 text-sm font-bold text-text-secondary focus:ring-0 w-full outline-none"
                    />
                </div>
                <div class="text-[10px] text-text-tertiary max-w-[120px] leading-tight">上传后将自动关联此日期并作为目录层级</div>
            </div>

            <!-- Dropzone -->
            <div 
              @click="fileInput?.click()"
              @dragover.prevent
              @drop.prevent.stop="handleFileDrop"
              class="border-2 border-dashed border-border-subtle rounded-2xl p-10 flex flex-col items-center justify-center hover:border-accent transition-colors cursor-pointer"
            >
                <div class="p-4 bg-accent-soft rounded-full mb-4">
                    <Upload class="w-8 h-8 text-accent" />
                </div>
                <p class="text-sm font-bold text-text-secondary">点击或将文件拖拽至此处</p>
                <p class="text-xs text-text-tertiary mt-2">支持文档、图片及 ZIP/TAR 压缩文件夹（单个最大 5GB）</p>
                <p class="text-[10px] text-text-tertiary mt-1">压缩包会自动展开，并过滤不支持或不安全的文件</p>
                <input type="file" ref="fileInput" multiple class="hidden" :accept="acceptedUploadTypes" @change="handleFileSelect" />
            </div>

            <!-- Queue -->
            <div v-if="queue.length > 0" class="space-y-2">
                <div v-for="item in queue" :key="item.id" class="flex items-center space-x-4 bg-surface-sunken p-3 rounded-xl border border-border-subtle">
                    <FileText class="w-5 h-5 text-text-tertiary" />
                    <div class="flex-1 min-w-0">
                        <div class="flex justify-between items-center mb-1">
                            <div class="flex min-w-0 items-center gap-2">
                              <span class="text-xs font-bold text-text-secondary truncate" :title="item.name">{{ item.name }}</span>
                              <span v-if="item.archive_name" class="shrink-0 rounded bg-accent-soft px-1.5 py-0.5 text-[9px] text-accent" :title="`来自 ${item.archive_name}`">压缩包</span>
                            </div>
                            <span v-if="item.status !== 'filtered'" class="text-[10px] text-text-tertiary">{{ (item.size / 1024 / 1024).toFixed(2) }} MB</span>
                        </div>
                        <p v-if="item.error" class="mb-1 truncate text-[10px]" :class="item.status === 'filtered' || item.status === 'paused' ? 'text-amber-600' : 'text-red-500'" :title="item.error">{{ item.error }}</p>
                        <!-- Progress Bar -->
                        <div class="w-full bg-surface-sunken h-1 rounded-full overflow-hidden">
                            <div 
                              class="h-full transition-all duration-300"
                              :class="item.status === 'filtered' || item.status === 'paused' ? 'bg-amber-400' : item.status === 'error' ? 'bg-red-500' : item.status === 'completed' ? 'bg-emerald-500' : 'bg-accent'"
                              :style="{ width: `${item.progress}%` }"
                            ></div>
                        </div>
                    </div>
                    
                    <div class="flex items-center">
                        <CheckCircle2 v-if="item.status === 'completed'" class="w-5 h-5 text-emerald-500" title="可入库" />
                        <AlertCircle v-else-if="item.status === 'filtered'" class="w-5 h-5 text-amber-500" :title="item.error || '已过滤'" />
                        <AlertCircle v-else-if="item.status === 'error'" class="w-5 h-5 text-red-500" :title="item.error" />
                        <Loader2 v-else-if="item.status === 'uploading'" class="w-4 h-4 text-accent animate-spin" />
                        <button v-else-if="item.status === 'paused'" @click.stop="item.file ? resumeItem(item.id) : fileInput?.click()" class="p-1 text-amber-500 hover:text-amber-600" :title="item.file ? '从断点继续上传' : '重新选择同一原文件后续传'">
                            <Play class="w-4 h-4" />
                        </button>
                        <button v-if="item.status !== 'uploading' && !isUploading" @click="removeItem(item.id)" class="p-1 hover:text-red-500">
                            <X class="w-4 h-4" />
                        </button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Modal Footer -->
        <div class="p-6 bg-surface-sunken border-t border-border-subtle flex justify-between items-center">
            <div class="text-xs text-text-secondary">
                <span v-if="isUploading">正在同步数据 ({{ uploadProgress }}%)...</span>
                <span v-else>
                  <span class="text-emerald-600">{{ acceptedUploadCount }} 个文件可入库</span>
                  <span v-if="filteredUploadCount" class="text-amber-600"> · {{ filteredUploadCount }} 个已过滤</span>
                  <span v-if="pausedUploadCount" class="text-amber-600"> · {{ pausedUploadCount }} 个等待续传</span>
                  <span v-if="failedUploadCount" class="text-red-500"> · {{ failedUploadCount }} 个上传失败</span>
                </span>
            </div>
            <div class="flex space-x-3">
                <button 
                  @click="clearQueue" 
                  v-if="queue.length > 0 && !isUploading"
                  class="px-4 py-2 text-sm font-medium text-text-secondary hover:bg-surface-hover rounded-lg transition"
                >
                  清空列表
                </button>
                <button 
                  @click="handleIngest"
                  :disabled="isUploading || ingestLoading || acceptedUploadCount === 0"
                  class="bg-accent hover:bg-accent-hover text-white px-6 py-2 rounded-lg text-sm font-bold flex items-center space-x-2 transition disabled:opacity-50"
                >
                    <Loader2 v-if="ingestLoading" class="w-4 h-4 animate-spin" />
                    <Play v-else class="w-4 h-4" />
                    <span>开始解析并存库 ({{ acceptedUploadCount }})</span>
                </button>
            </div>
        </div>
      </div>
    </div>
    </Transition>

    <!-- Rename Modal -->
    <Transition name="pop">
    <div v-if="isRenameModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="重命名目录">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="isRenameModalOpen = false"></div>
      <div class="relative w-full max-w-md rounded-2xl border border-white/55 bg-surface/90 p-6 shadow-2xl backdrop-blur-2xl pop-panel dark:border-white/10 dark:bg-surface/95">
        <h3 class="text-lg font-bold text-text-primary mb-4">{{ renameDirIsVirtual ? '修改日期目录' : '重命名目录' }}</h3>
        <input 
          v-model="renameDirNewName" 
          type="text" 
          class="w-full px-4 py-2 bg-surface-sunken border border-border-subtle rounded-xl mb-6 outline-none focus:ring-2 focus:ring-accent/30"
          :placeholder="renameDirIsVirtual ? '输入日期，例如 2026-07-12' : '输入新目录名称'"
          @keyup.enter="submitRenameDir"
        />
        <div class="flex justify-end space-x-3">
          <button @click="isRenameModalOpen = false" class="px-4 py-2 text-sm text-text-secondary font-medium">取消</button>
          <button @click="submitRenameDir" :disabled="isRenameSaving" class="bg-accent text-white px-6 py-2 rounded-xl text-sm font-bold shadow-sm disabled:opacity-50">{{ renameDirIsVirtual ? '确认修改' : '确认重命名' }}</button>
        </div>
      </div>
    </div>
    </Transition>

    <!-- Move Modal -->
    <Transition name="pop">
    <div v-if="isMoveModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="移动文档">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="isMoveModalOpen = false"></div>
      <div class="relative flex max-h-[70vh] w-full max-w-lg flex-col rounded-2xl border border-white/55 bg-surface/90 shadow-2xl backdrop-blur-2xl pop-panel dark:border-white/10 dark:bg-surface/95">
        <div class="p-6 border-b border-border-subtle flex justify-between items-center">
          <h3 class="text-lg font-bold text-text-primary">批量移动到目录</h3>
          <button @click="isMoveModalOpen = false" class="text-text-tertiary hover:text-text-secondary"><X class="w-5 h-5" /></button>
        </div>
        
        <div class="flex-1 overflow-y-auto p-6">
          <div class="mb-4">
            <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest block mb-2">目标路径</label>
            <input 
              v-model="moveTargetPath" 
              type="text" 
              class="w-full px-4 py-2 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/30 text-sm font-mono"
              placeholder="例如: 财务/2024"
            />
          </div>
          
          <div class="space-y-1">
            <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest block mb-1">快速选择</label>
            <button @click="moveTargetPath = ''" :class="moveTargetPath === '' ? 'bg-accent-soft text-accent border-accent/30' : 'bg-transparent text-text-secondary border-transparent'" class="w-full text-left px-3 py-2 rounded-lg text-sm flex items-center border transition-all">
                <Home class="w-4 h-4 mr-2" />
                <span>根目录 (未分类)</span>
            </button>
            <button @click="moveTargetPath = currentPath || ''" :class="moveTargetPath === (currentPath || '') ? 'bg-accent-soft text-accent border-accent/30' : 'bg-transparent text-text-secondary border-transparent'" class="w-full text-left px-3 py-2 rounded-lg text-sm flex items-center border transition-all">
                <Folder class="w-4 h-4 mr-2" />
                <span>当前目录 ({{ currentPath || '根目录' }})</span>
            </button>
            <button @click="moveTargetPath = parentPath" :class="moveTargetPath === parentPath ? 'bg-accent-soft text-accent border-accent/30' : 'bg-transparent text-text-secondary border-transparent'" class="w-full text-left px-3 py-2 rounded-lg text-sm flex items-center border transition-all">
                <ArrowLeft class="w-4 h-4 mr-2" />
                <span>上级目录 ({{ parentPath || '根目录' }})</span>
            </button>
            
            <div class="pt-2">
              <TreeSelector :nodes="directoryTree" :current-path="moveTargetPath" @select="p => moveTargetPath = p" />
            </div>
          </div>
        </div>

        <div class="p-6 bg-surface-sunken border-t border-border-subtle flex justify-end space-x-3">
          <button @click="isMoveModalOpen = false" class="px-4 py-2 text-sm text-text-secondary font-medium">取消</button>
          <button @click="submitBatchMove" class="bg-accent text-white px-6 py-2 rounded-xl text-sm font-bold shadow-sm">开始移动 ({{ selectedDocIds.size }}个文档)</button>
        </div>
      </div>
    </div>
    </Transition>

    <!-- KB Settings Modal -->
    <Transition name="pop">
    <div v-if="isKbEditModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="知识库设置">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="isKbEditModalOpen = false"></div>
      <div class="relative w-full max-w-md rounded-2xl border border-white/55 bg-surface/90 p-6 shadow-2xl backdrop-blur-2xl pop-panel dark:border-white/10 dark:bg-surface/95">
        <h3 class="text-lg font-bold text-text-primary mb-4 flex items-center gap-2">
          <Settings class="w-5 h-5 text-accent" />
          知识库设置
        </h3>
        <div class="space-y-4">
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1">名称</label>
            <input v-model="kbEditForm.kb_name" class="w-full px-4 py-2 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/30 text-sm" />
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1">学科 / Schema</label>
            <input v-model="kbEditForm.subject" class="w-full px-4 py-2 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/30 text-sm" />
          </div>
          <div>
            <label class="block text-xs font-bold text-text-tertiary mb-1">描述</label>
            <textarea v-model="kbEditForm.description" class="w-full px-4 py-2 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/30 text-sm h-20"></textarea>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs font-bold text-text-tertiary mb-1">入库后端</label>
              <select v-model="kbEditForm.ingestion_backend" class="w-full px-3 py-2 bg-surface-sunken border border-border-subtle rounded-xl text-xs">
                <option v-for="opt in INGESTION_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </div>
            <div>
              <label class="block text-xs font-bold text-text-tertiary mb-1">查询后端</label>
              <select v-model="kbEditForm.retrieval_backend" class="w-full px-3 py-2 bg-surface-sunken border border-border-subtle rounded-xl text-xs">
                <option v-for="opt in RETRIEVAL_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </div>
          </div>
        </div>
        <div class="flex justify-end space-x-3 mt-6">
          <button @click="isKbEditModalOpen = false" class="px-4 py-2 text-sm text-text-secondary font-medium">取消</button>
          <button @click="saveKbEdit" class="bg-accent text-white px-6 py-2 rounded-xl text-sm font-bold">保存</button>
        </div>
      </div>
    </div>
    </Transition>

    <!-- Chunk Preview Panel (self-ingestion only) -->
    <Transition name="pop">
    <div
      v-if="chunkPanelOpen"
      class="fixed inset-0 z-50 flex items-end justify-center p-0 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="chunk-panel-title"
    >
      <div class="absolute inset-0 bg-slate-950/18 backdrop-blur-md dark:bg-black/45" @click="closeChunkPanel"></div>
      <aside class="relative flex h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-t-[1.75rem] border border-white/55 bg-surface/88 shadow-[0_24px_80px_rgba(15,23,42,0.24),0_3px_16px_rgba(15,23,42,0.10)] backdrop-blur-2xl pop-panel dark:border-white/10 dark:bg-surface/92 sm:h-[min(88vh,900px)] sm:rounded-[1.75rem]">
        <div class="flex items-start justify-between gap-4 border-b border-border-subtle/80 bg-surface/45 px-5 py-4 sm:px-7 sm:py-5">
          <div class="min-w-0">
            <p class="text-[10px] font-bold uppercase tracking-widest text-accent">文章分块</p>
            <h3 id="chunk-panel-title" class="mt-1 truncate text-base font-extrabold text-text-primary sm:text-lg">{{ chunkDoc?.file_name }}</h3>
            <p class="mt-1 text-xs text-text-secondary">共 {{ chunkTotal }} 个分块 · 自研入库</p>
          </div>
          <button @click="closeChunkPanel" class="rounded-xl p-2 text-text-tertiary transition-colors hover:bg-surface/75 hover:text-text-primary" aria-label="关闭分块预览">
            <X class="w-5 h-5" />
          </button>
        </div>

        <div class="custom-scrollbar min-h-0 flex-1 space-y-4 overflow-y-auto bg-surface/24 px-4 py-4 sm:px-7 sm:py-5">
          <div v-if="chunkLoading" class="flex flex-col items-center justify-center py-16 text-text-secondary">
            <Loader2 class="w-8 h-8 animate-spin mb-3 text-accent" />
            <p class="text-sm font-semibold">正在加载分块...</p>
          </div>
          <div v-else-if="chunkItems.length === 0" class="py-16 text-center text-sm text-text-secondary italic">
            暂无可预览的可见分块，请确认文档已完成自研入库。
          </div>
          <article
            v-for="chunk in chunkItems"
            :key="chunk.chunk_id"
            class="mx-auto w-full max-w-4xl rounded-2xl border border-border-subtle/90 bg-surface/60 p-4 shadow-sm sm:p-5"
          >
            <div class="flex flex-wrap items-center gap-2 mb-3">
              <span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-accent-soft text-accent">
                #{{ chunk.chunk_index + 1 }}
              </span>
              <span class="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-surface-sunken text-text-secondary">
                {{ chunk.block_type }}
              </span>
              <span class="text-[10px] text-text-tertiary">{{ chunk.token_count }} tokens</span>
              <span v-if="chunk.block_id" class="max-w-[min(100%,22rem)] truncate font-mono text-[10px] text-text-tertiary">{{ chunk.block_id }}</span>
            </div>
            <pre class="whitespace-pre-wrap break-words font-sans text-[13px] leading-6 text-text-secondary [overflow-wrap:anywhere] sm:text-sm">{{ chunk.content }}</pre>
          </article>
        </div>

        <div
          v-if="chunkTotal > chunkPageSize"
          class="flex items-center justify-between border-t border-border-subtle/80 bg-surface/55 px-5 py-3.5 text-xs font-semibold text-text-secondary backdrop-blur-2xl sm:px-7 sm:py-4"
        >
          <span>第 {{ chunkPage }} / {{ Math.max(1, Math.ceil(chunkTotal / chunkPageSize)) }} 页</span>
          <div class="flex gap-2">
            <button
              @click="changeChunkPage(-1)"
              :disabled="chunkPage <= 1 || chunkLoading"
              class="px-3 py-1.5 rounded-lg border border-border-subtle disabled:opacity-40"
            >
              上一页
            </button>
            <button
              @click="changeChunkPage(1)"
              :disabled="chunkPage >= Math.ceil(chunkTotal / chunkPageSize) || chunkLoading"
              class="px-3 py-1.5 rounded-lg border border-border-subtle disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      </aside>
    </div>
    </Transition>
  </div>
</template>
