import { ref, type Ref } from 'vue';
import { request } from '@/utils/request';

export interface UploadFile {
  id: string;
  file: File | null;
  name: string;
  size: number;
  status: 'pending' | 'uploading' | 'paused' | 'completed' | 'filtered' | 'error';
  progress: number;
  upload_id?: string;
  file_id?: string;
  error?: string;
  last_modified?: number;
  content_type?: string;
  relative_path?: string;
  category_path?: string;
  archive_name?: string;
}

interface UploadedServerFile {
  file_id: string;
  file_name: string;
  file_size: number;
  relative_path?: string;
  category_path?: string;
  archive_name?: string;
}

interface RejectedServerFile {
  file_name: string;
  reason: string;
  relative_path?: string;
  archive_name?: string;
}

interface UploadResult {
  file_id?: string;
  files?: UploadedServerFile[];
  rejected_files?: RejectedServerFile[];
}

interface ChunkUploadStatus {
  upload_id: string;
  file_id: string;
  kb_id: string;
  filename: string;
  total_size: number;
  chunk_size: number;
  total_chunks: number;
  uploaded_chunks: number[];
  progress: number;
  state: string;
}

interface CompletedUploadLookup {
  found: boolean;
  upload_id?: string;
  total_chunks?: number;
  result?: UploadResult;
}

interface PersistedUpload {
  id: string;
  name: string;
  size: number;
  progress: number;
  upload_id: string;
  file_id?: string;
  last_modified?: number;
  content_type?: string;
}

interface UploadStore {
  queue: Ref<UploadFile[]>;
  isUploading: Ref<boolean>;
  processing: boolean;
  initialized: boolean;
  listenersReady: boolean;
}

const uploadStores = new Map<string, UploadStore>();
let generatedId = 0;

export function useUpload(kbId: string) {
  const chunkSize = 10 * 1024 * 1024; // 10MB
  const maxFileSize = 5 * 1024 * 1024 * 1024; // 5GB
  const maxConcurrent = 3;
  const storageKey = `kb-resumable-uploads:${kbId}`;
  let store = uploadStores.get(kbId);
  if (!store) {
    store = {
      queue: ref<UploadFile[]>([]),
      isUploading: ref(false),
      processing: false,
      initialized: false,
      listenersReady: false,
    };
    uploadStores.set(kbId, store);
  }
  const queue = store.queue;
  const isUploading = store.isUploading;

  const nextId = () => `${Date.now().toString(36)}-${(generatedId++).toString(36)}`;

  const persistResumableUploads = () => {
    if (typeof window === 'undefined') return;
    const resumable: PersistedUpload[] = queue.value
      .filter(item => item.upload_id && ['pending', 'uploading', 'paused'].includes(item.status))
      .map(item => ({
        id: item.id,
        name: item.name,
        size: item.size,
        progress: item.progress,
        upload_id: item.upload_id!,
        file_id: item.file_id,
        last_modified: item.last_modified,
        content_type: item.content_type,
      }));
    if (resumable.length > 0) {
      localStorage.setItem(storageKey, JSON.stringify(resumable));
    } else {
      localStorage.removeItem(storageKey);
    }
  };

  const sameLocalFile = (item: UploadFile, file: File) => (
    item.name === file.name
    && item.size === file.size
    && (!item.last_modified || item.last_modified === file.lastModified)
  );

  const addFiles = (files: FileList | File[]) => {
    const newFiles: UploadFile[] = [];
    Array.from(files).forEach(file => {
      const resumable = queue.value.find(item => (
        ['paused', 'error'].includes(item.status) && sameLocalFile(item, file)
      ));
      if (resumable) {
        resumable.file = file;
        resumable.last_modified = file.lastModified;
        resumable.content_type = file.type;
        resumable.status = 'pending';
        resumable.error = '已匹配原文件，将从已上传分片继续';
        return;
      }
      if (queue.value.some(item => (
        ['pending', 'uploading'].includes(item.status) && sameLocalFile(item, file)
      ))) {
        return;
      }
      const isOversized = file.size > maxFileSize;
      const isEmpty = file.size <= 0;
      newFiles.push({
        id: nextId(),
        file,
        name: file.name,
        size: file.size,
        status: isOversized || isEmpty ? 'error' : 'pending',
        progress: isOversized || isEmpty ? 100 : 0,
        error: isOversized
          ? '文件大小超过 5GB 限制'
          : isEmpty ? '不支持上传空文件' : undefined,
        last_modified: file.lastModified,
        content_type: file.type,
      });
    });
    queue.value.push(...newFiles);
    persistResumableUploads();
    void processQueue();
  };

  const applyUploadResult = (
    item: UploadFile,
    result: UploadResult,
  ) => {
    const accepted = result.files || [];
    const rejected = result.rejected_files || [];

    if (accepted.length === 0 && rejected.length === 0 && result.file_id) {
      accepted.push({
        file_id: result.file_id,
        file_name: item.name,
        file_size: item.size,
      });
    }

    const rows: UploadFile[] = [
      ...accepted.map((serverFile, index) => ({
        id: index === 0 ? item.id : nextId(),
        file: item.file,
        name: serverFile.relative_path || serverFile.file_name,
        size: serverFile.file_size,
        status: 'completed' as const,
        progress: 100,
        upload_id: item.upload_id,
        file_id: serverFile.file_id,
        relative_path: serverFile.relative_path,
        category_path: serverFile.category_path,
        archive_name: serverFile.archive_name,
      })),
      ...rejected.map(serverFile => ({
        id: nextId(),
        file: item.file,
        name: serverFile.relative_path || serverFile.file_name,
        size: 0,
        status: 'filtered' as const,
        progress: 100,
        upload_id: item.upload_id,
        error: serverFile.reason,
        relative_path: serverFile.relative_path,
        archive_name: serverFile.archive_name,
      })),
    ];

    if (rows.length === 0) {
      rows.push({
        ...item,
        status: 'error',
        progress: 100,
        error: '服务端未返回可入库文件',
      });
    }

    const itemIndex = queue.value.findIndex(file => file.id === item.id);
    if (itemIndex >= 0) {
      queue.value.splice(itemIndex, 1, ...rows);
    }
    persistResumableUploads();
  };

  const processQueue = async () => {
    if (store!.processing) return;
    store!.processing = true;
    isUploading.value = true;

    try {
      while (queue.value.some(f => f.status === 'pending' && f.file)) {
        const pending = queue.value
          .filter(f => f.status === 'pending' && f.file)
          .slice(0, maxConcurrent);
        await Promise.all(pending.map(f => uploadFile(f)));
      }
    } finally {
      store!.processing = false;
      isUploading.value = false;
      persistResumableUploads();
    }
  };

  const uploadFile = async (item: UploadFile) => {
    if (!item.file) {
      item.status = 'paused';
      item.error = '请重新选择同一原文件以继续上传';
      persistResumableUploads();
      return;
    }
    try {
      item.status = 'uploading';
      item.error = undefined;
      persistResumableUploads();
      const result = await uploadChunked(item);
      applyUploadResult(item, result);
    } catch (e: any) {
      const current = queue.value.find(file => file.id === item.id);
      if (current) {
        current.status = 'paused';
        current.error = `${e?.message || '上传中断'}；可点击继续或重新选择同一文件`;
        persistResumableUploads();
      }
    }
  };

  const getChunkStatus = async (item: UploadFile): Promise<ChunkUploadStatus | null> => {
    if (!item.upload_id) return null;
    try {
      const status = await request(`/uploads/status/${item.upload_id}`) as ChunkUploadStatus;
      if (
        status.kb_id !== kbId
        || (
          status.state !== 'completed'
          && (
            status.filename !== item.name
            || status.total_size !== item.size
            || status.chunk_size !== chunkSize
          )
        )
      ) {
        throw new Error('续传会话与本地文件不匹配');
      }
      return status;
    } catch (error: any) {
      if (error?.status === 404) {
        item.upload_id = undefined;
        item.file_id = undefined;
        item.progress = 0;
        persistResumableUploads();
        return null;
      }
      throw error;
    }
  };

  const fetchCompletionResult = (
    item: UploadFile,
    totalChunks: number,
  ): Promise<UploadResult> => request(
    `/uploads/complete/${item.upload_id}?total_chunks=${totalChunks}`,
    { method: 'POST' },
  );

  const lookupCompletedUpload = async (item: UploadFile): Promise<UploadResult | null> => {
    const query = new URLSearchParams({
      kb_id: kbId,
      filename: item.name,
      total_size: String(item.size),
    });
    const recovered = await request(
      `/uploads/completed/lookup?${query.toString()}`,
    ) as CompletedUploadLookup;
    if (!recovered.found || !recovered.upload_id || !recovered.result) {
      return null;
    }
    item.upload_id = recovered.upload_id;
    item.file_id = recovered.result.file_id;
    item.progress = 100;
    persistResumableUploads();
    return recovered.result;
  };

  const waitForServerCompletion = async (
    item: UploadFile,
    totalChunks: number,
  ): Promise<UploadResult> => {
    item.progress = 100;
    item.error = '文件已上传，正在服务器端解压和提取，请勿重新上传';
    persistResumableUploads();
    for (let attempt = 0; attempt < 180; attempt++) {
      const status = await getChunkStatus(item);
      if (!status) throw new Error('服务器端完成状态丢失');
      if (status.state === 'completed') {
        return fetchCompletionResult(item, totalChunks);
      }
      if (status.state === 'open') {
        return fetchCompletionResult(item, totalChunks);
      }
      await new Promise(resolve => window.setTimeout(resolve, 2000));
    }
    throw new Error('服务器仍在解压提取，可稍后点击继续查询结果');
  };

  const uploadChunked = async (item: UploadFile): Promise<UploadResult> => {
    if (!item.file) throw new Error('缺少本地原文件');
    const totalChunks = Math.ceil(item.size / chunkSize);
    const recovered = await lookupCompletedUpload(item);
    if (recovered) return recovered;

    let status = await getChunkStatus(item);

    if (!status) {
      const init = await request('/uploads/init', {
        method: 'POST',
        body: JSON.stringify({
          kb_id: kbId,
          filename: item.name,
          file_size: item.size,
          content_type: item.file.type || 'application/octet-stream',
        })
      });
      item.upload_id = init.upload_id;
      item.file_id = init.file_id;
      item.progress = 0;
      persistResumableUploads();
      status = {
        upload_id: init.upload_id,
        file_id: init.file_id,
        kb_id: kbId,
        filename: item.name,
        total_size: item.size,
        chunk_size: chunkSize,
        total_chunks: totalChunks,
        uploaded_chunks: [],
        progress: 0,
        state: 'open',
      };
    }

    if (status.state === 'completed') {
      return fetchCompletionResult(item, totalChunks);
    }
    if (status.state !== 'open') {
      return waitForServerCompletion(item, totalChunks);
    }
    const uploadedChunks = new Set(status.uploaded_chunks);
    item.progress = Math.round(status.progress);
    for (let i = 0; i < totalChunks; i++) {
      if (uploadedChunks.has(i)) continue;
      const start = i * chunkSize;
      const end = Math.min(start + chunkSize, item.size);
      const chunk = item.file.slice(start, end);

      const formData = new FormData();
      formData.append('chunk', chunk);

      await request(`/uploads/chunks/${item.upload_id}/${i}`, {
        method: 'POST',
        body: formData,
      });

      uploadedChunks.add(i);
      item.progress = Math.round((uploadedChunks.size / totalChunks) * 100);
      persistResumableUploads();
    }

    try {
      return await fetchCompletionResult(item, totalChunks);
    } catch (error: any) {
      const retryableStatus = error?.status;
      if (
        retryableStatus === undefined
        || [408, 409, 425, 429, 500, 502, 503, 504].includes(retryableStatus)
      ) {
        return waitForServerCompletion(item, totalChunks);
      }
      throw error;
    }
  };

  const resumeItem = (id: string) => {
    const item = queue.value.find(file => file.id === id);
    if (!item) return;
    if (!item.file) {
      item.status = 'paused';
      item.error = '页面刷新后浏览器不能自动读取本地文件，请重新选择同一原文件';
      return;
    }
    item.status = 'pending';
    item.error = undefined;
    void processQueue();
  };

  const removeItem = (id: string) => {
    queue.value = queue.value.filter(f => f.id !== id);
    persistResumableUploads();
  };

  const clearQueue = () => {
    queue.value = [];
    persistResumableUploads();
  };

  const restoreResumableUploads = async () => {
    if (store!.initialized || typeof window === 'undefined') return;
    store!.initialized = true;
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || '[]') as PersistedUpload[];
      queue.value.push(...saved.map(item => ({
        ...item,
        file: null,
        status: 'paused' as const,
        error: '页面已恢复：请重新选择同一原文件，即可从断点继续',
      })));
      for (const item of queue.value.filter(file => file.upload_id && !file.file)) {
        try {
          const status = await getChunkStatus(item);
          if (status) {
            item.file_id = status.file_id;
            item.progress = Math.round(status.progress);
          } else {
            item.error = '服务端续传会话已过期；重新选择原文件后将重新上传';
          }
        } catch (error: any) {
          item.error = error?.message || '暂时无法查询续传进度';
        }
      }
      persistResumableUploads();
    } catch {
      localStorage.removeItem(storageKey);
    }
  };

  if (!store.listenersReady && typeof window !== 'undefined') {
    store.listenersReady = true;
    window.addEventListener('online', () => {
      queue.value
        .filter(item => item.status === 'paused' && item.file)
        .forEach(item => {
          item.status = 'pending';
          item.error = undefined;
        });
      void processQueue();
    });
    window.addEventListener('beforeunload', (event) => {
      if (!isUploading.value) return;
      event.preventDefault();
      event.returnValue = '';
    });
  }
  void restoreResumableUploads();

  return {
    queue,
    isUploading,
    addFiles,
    resumeItem,
    removeItem,
    clearQueue
  };
}
