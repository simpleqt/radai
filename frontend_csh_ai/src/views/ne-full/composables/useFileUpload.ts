import { ref } from 'vue';
import { authState } from '@/utils/request';

export type UploadParseMode = 'auto' | 'mineru';

export interface FileAttachment {
  id: string;
  file: File;
  name: string;
  size: number;
  mimeType: string;
  // multimodal / short_doc / long_doc / failed / pending
  category: 'multimodal' | 'short_doc' | 'long_doc' | 'failed' | 'pending';
  // pending / uploading / ready / error
  status: 'pending' | 'uploading' | 'ready' | 'error';
  strategy: 'vision' | 'context' | 'indexed' | 'none';
  progress: number;
  previewUrl?: string;
  errorMessage?: string;
  contentLength?: number;
  tableAnalysisEnabled?: boolean;
}

const ACCEPTED_TYPES = [
  // 图片
  'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp', 'image/svg+xml',
  // 纯文本
  'text/plain', 'text/markdown', 'text/csv', 'application/json',
  'text/html', 'text/xml', 'text/x-python', 'application/vnd.google-earth.kml+xml', '.kml',
  // 富文档
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/msword',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.ms-powerpoint',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel',
].join(',');

function isImageType(mimeType: string): boolean {
  return mimeType.startsWith('image/');
}

function generateId(): string {
  return `f_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export function useFileUpload() {
  const attachments = ref<FileAttachment[]>([]);
  const isUploading = ref(false);
  const uploadParseMode = ref<UploadParseMode>('auto');

  /** 添加文件到待上传列表（本地预览，尚未上传） */
  function addFiles(files: FileList | File[]) {
    const fileArray = Array.from(files);
    for (const file of fileArray) {
      if (attachments.value.length >= 10) break;

      const attachment: FileAttachment = {
        id: generateId(),
        file,
        name: file.name,
        size: file.size,
        mimeType: file.type || 'application/octet-stream',
        category: 'pending',
        status: 'pending',
        strategy: 'none',
        progress: 0,
      };

      if (isImageType(file.type)) {
        attachment.previewUrl = URL.createObjectURL(file);
      }

      attachments.value.push(attachment);
    }
  }

  /** 上传所有 pending 状态文件到服务器 */
  async function uploadPendingFiles(threadId: string): Promise<boolean> {
    const pending = attachments.value.filter((a) => a.status === 'pending');
    if (pending.length === 0) return true;

    isUploading.value = true;
    const token = authState.value.token;

    try {
      // 标记为上传中
      for (const att of pending) {
        att.status = 'uploading';
        att.progress = 10;
      }

      const formData = new FormData();
      formData.append('thread_id', threadId);
      formData.append('parse_mode', uploadParseMode.value);
      for (const att of pending) {
        formData.append('files', att.file);
      }

      const headers: Record<string, string> = {};
      if (token) headers['Authorization'] = `Bearer ${token}`;

      const res = await fetch('/api/v2/chat-files/upload', {
        method: 'POST',
        headers,
        body: formData,
      });

      if (!res.ok) {
        throw new Error(`上传失败: ${res.statusText}`);
      }

      const data = await res.json();
      const serverFiles: any[] = data.files || [];

      // 按文件名匹配结果（同名文件按顺序对应）
      const serverMap: Record<string, any[]> = {};
      for (const sf of serverFiles) {
        if (!serverMap[sf.file_name]) serverMap[sf.file_name] = [];
        serverMap[sf.file_name].push(sf);
      }

      for (const att of pending) {
        const matches = serverMap[att.name];
        const sf = matches?.shift();
        if (sf) {
          att.id = sf.file_id;
          att.category = sf.file_category;
          att.strategy = sf.strategy;
          att.contentLength = sf.content_length;
          att.tableAnalysisEnabled = Boolean(sf.table_analysis_enabled);
          att.status = sf.parse_status === 'completed' ? 'ready' : 'error';
          att.errorMessage = sf.error || undefined;
          att.progress = 100;
        } else {
          att.status = 'error';
          att.errorMessage = '服务器未返回对应文件结果';
          att.progress = 0;
        }
      }

      return true;
    } catch (err: any) {
      for (const att of pending) {
        att.status = 'error';
        att.errorMessage = err.message || '上传失败';
        att.progress = 0;
      }
      return false;
    } finally {
      isUploading.value = false;
    }
  }

  /** 移除附件 */
  function removeAttachment(id: string) {
    const att = attachments.value.find((a) => a.id === id);
    if (att?.previewUrl) {
      URL.revokeObjectURL(att.previewUrl);
    }
    attachments.value = attachments.value.filter((a) => a.id !== id);
  }

  /** 清空所有附件 */
  function clearAttachments() {
    for (const att of attachments.value) {
      if (att.previewUrl) URL.revokeObjectURL(att.previewUrl);
    }
    attachments.value = [];
  }

  /** 获取已 ready 的文件 ID 列表（发送时用） */
  function getReadyFileIds(): string[] {
    return attachments.value
      .filter((a) => a.status === 'ready')
      .map((a) => a.id);
  }

  /** 从个人文件库直接添加已就绪的文件 */
  function addReadyAttachment(file: {
    file_id: string;
    display_name: string;
    size_bytes: number;
    mime_type: string;
    file_category: 'multimodal' | 'short_doc' | 'long_doc' | 'failed' | 'pending';
    strategy: 'vision' | 'context' | 'indexed' | 'none';
    table_analysis_enabled?: boolean;
  }) {
    if (attachments.value.some((a) => a.id === file.file_id)) return; // 避免重复添加

    const attachment: FileAttachment = {
      id: file.file_id,
      file: new File([], file.display_name),
      name: file.display_name,
      size: file.size_bytes,
      mimeType: file.mime_type || 'application/octet-stream',
      category: file.file_category,
      status: 'ready',
      strategy: file.strategy,
      tableAnalysisEnabled: Boolean(file.table_analysis_enabled),
      progress: 100,
    };

    if (file.mime_type?.startsWith('image/')) {
      attachment.previewUrl = `/api/v2/files/${file.file_id}/download`;
    }

    attachments.value.push(attachment);
  }

  function setUploadParseMode(mode: UploadParseMode) {
    uploadParseMode.value = mode;
  }

  return {
    attachments,
    isUploading,
    uploadParseMode,
    acceptedTypes: ACCEPTED_TYPES,
    addFiles,
    uploadPendingFiles,
    removeAttachment,
    clearAttachments,
    getReadyFileIds,
    addReadyAttachment,
    setUploadParseMode,
  };
}
