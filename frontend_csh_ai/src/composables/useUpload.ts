import { ref } from 'vue';
import { request, authState } from '@/utils/request';

export interface UploadFile {
  id: string;
  file: File;
  name: string;
  size: number;
  status: 'pending' | 'uploading' | 'completed' | 'error';
  progress: number;
  upload_id?: string;
  file_id?: string;
  error?: string;
}

export function useUpload(kbId: string) {
  const queue = ref<UploadFile[]>([]);
  const isUploading = ref(false);
  const chunkSize = 10 * 1024 * 1024; // 10MB
  const maxConcurrent = 3;

  const addFiles = (files: FileList | File[]) => {
    const newFiles = Array.from(files).map(file => ({
      id: Math.random().toString(36).substring(7),
      file,
      name: file.name,
      size: file.size,
      status: 'pending' as const,
      progress: 0,
    }));
    queue.value.push(...newFiles);
    processQueue();
  };

  const processQueue = async () => {
    if (isUploading.value) return;
    isUploading.value = true;

    while (queue.value.some(f => f.status === 'pending')) {
      const pending = queue.value.filter(f => f.status === 'pending').slice(0, maxConcurrent);
      await Promise.all(pending.map(f => uploadFile(f)));
    }

    isUploading.value = false;
  };

  const uploadFile = async (item: UploadFile) => {
    try {
      item.status = 'uploading';
      if (item.size > chunkSize) {
        await uploadChunked(item);
      } else {
        await uploadDirect(item);
      }
      item.status = 'completed';
      item.progress = 100;
    } catch (e: any) {
      item.status = 'error';
      item.error = e.message;
    }
  };

  const uploadDirect = async (item: UploadFile) => {
    const formData = new FormData();
    formData.append('kb_id', kbId);
    formData.append('files', item.file);

    const res = await request('/uploads/batch', {
      method: 'POST',
      body: formData,
    });

    if (res.files && res.files.length > 0) {
      item.file_id = res.files[0].file_id;
    }
  };

  const uploadChunked = async (item: UploadFile) => {
    const totalChunks = Math.ceil(item.size / chunkSize);

    // Init
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

    for (let i = 0; i < totalChunks; i++) {
        const start = i * chunkSize;
        const end = Math.min(start + chunkSize, item.size);
        const chunk = item.file.slice(start, end);

        const formData = new FormData();
        formData.append('chunk', chunk);

        await request(`/uploads/chunks/${item.upload_id}/${i}`, {
          method: 'POST',
          body: formData,
        });

        item.progress = Math.round(((i + 1) / totalChunks) * 100);
    }

    // Complete
    await request(`/uploads/complete/${item.upload_id}?total_chunks=${totalChunks}`, {
      method: 'POST'
    });
  };

  const removeItem = (id: string) => {
    queue.value = queue.value.filter(f => f.id !== id);
  };

  const clearQueue = () => {
    queue.value = [];
  };

  return {
    queue,
    isUploading,
    addFiles,
    removeItem,
    clearQueue
  };
}
