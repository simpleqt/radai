import { authState } from '@/utils/request';

export interface TranscribeResult {
  text: string;
  model: string;
  language: string;
}

export async function transcribeAudio(
  blob: Blob,
  options?: { language?: string; modelName?: string; filename?: string },
): Promise<TranscribeResult> {
  const token = authState.value.token;
  if (!token) {
    throw new Error('请先登录后再使用语音输入');
  }

  const formData = new FormData();
  const filename = options?.filename || 'recording.webm';
  formData.append('file', blob, filename);
  if (options?.language) {
    formData.append('language', options.language);
  }
  if (options?.modelName) {
    formData.append('model_name', options.modelName);
  }

  const res = await fetch('/api/v1/asr/transcribe', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
    },
    body: formData,
  });

  if (!res.ok) {
    let detail = '语音识别失败';
    try {
      const err = await res.json();
      detail = err.detail || err.message || detail;
    } catch {
      detail = await res.text() || detail;
    }
    throw new Error(detail);
  }

  return res.json();
}
