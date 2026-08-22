import { onUnmounted, ref } from 'vue';
import { transcribeAudio } from '@/api/asr';

export type VoicePhase = 'idle' | 'recording' | 'transcribing' | 'editing';

export function useVoiceInput() {
  const phase = ref<VoicePhase>('idle');
  const pendingText = ref('');
  const error = ref<string | null>(null);
  const recordingSeconds = ref(0);

  let mediaRecorder: MediaRecorder | null = null;
  let mediaStream: MediaStream | null = null;
  let chunks: Blob[] = [];
  let recordingTimer: ReturnType<typeof setInterval> | null = null;

  // 当 mouseup 在 getUserMedia 还未返回时就触发了，需要一个"提前停止"标志
  let stopRequestedBeforeStart = false;

  function cleanupStream() {
    mediaStream?.getTracks().forEach((t) => t.stop());
    mediaStream = null;
    mediaRecorder = null;
  }

  function clearTimer() {
    if (recordingTimer !== null) {
      clearInterval(recordingTimer);
      recordingTimer = null;
    }
  }

  async function startRecording() {
    if (phase.value !== 'idle') return;
    error.value = null;
    stopRequestedBeforeStart = false;

    if (!navigator.mediaDevices?.getUserMedia) {
      error.value = '当前浏览器不支持麦克风录音';
      return;
    }

    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });

      // getUserMedia 是异步的——如果用户在这段等待中已经松开按钮，直接放弃
      if (stopRequestedBeforeStart) {
        cleanupStream();
        phase.value = 'idle';
        return;
      }

      chunks = [];

      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
          ? 'audio/webm'
          : '';

      mediaRecorder = mimeType
        ? new MediaRecorder(mediaStream, { mimeType })
        : new MediaRecorder(mediaStream);

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunks.push(e.data);
      };

      mediaRecorder.onstop = async () => {
        clearTimer();
        const blobType = mediaRecorder?.mimeType || 'audio/webm';
        cleanupStream();
        const blob = new Blob(chunks, { type: blobType });
        chunks = [];

        if (blob.size < 500) {
          error.value = '录音时间过短，请重试';
          phase.value = 'idle';
          return;
        }

        phase.value = 'transcribing';
        try {
          const ext = blobType.includes('webm') ? 'webm' : 'wav';
          const result = await transcribeAudio(blob, {
            language: 'zh',
            filename: `recording.${ext}`,
          });
          const text = result.text?.trim();
          if (text) {
            pendingText.value = text;
            phase.value = 'editing';
          } else {
            error.value = '未识别到语音内容，请重试';
            phase.value = 'idle';
          }
        } catch (err: any) {
          error.value = err?.message || '语音识别失败';
          phase.value = 'idle';
        }
      };

      mediaRecorder.start(100);
      phase.value = 'recording';
      recordingSeconds.value = 0;
      recordingTimer = setInterval(() => {
        recordingSeconds.value++;
        if (recordingSeconds.value >= 60) stopRecording();
      }, 1000);
    } catch (err: any) {
      cleanupStream();
      clearTimer();
      error.value =
        err?.name === 'NotAllowedError'
          ? '麦克风权限被拒绝，请在浏览器设置中允许访问'
          : err?.message || '无法启动录音';
      phase.value = 'idle';
    }
  }

  function stopRecording() {
    // 无论此时 mediaRecorder 是否已就绪，先打上标志
    stopRequestedBeforeStart = true;

    if (phase.value !== 'recording' || !mediaRecorder) return;
    clearTimer();
    if (mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop();
    }
    // onstop 回调会把 phase 改为 transcribing
  }

  function cancelEdit() {
    pendingText.value = '';
    phase.value = 'idle';
    error.value = null;
  }

  onUnmounted(() => {
    if (phase.value === 'recording') stopRecording();
    cleanupStream();
    clearTimer();
  });

  return {
    phase,
    pendingText,
    error,
    recordingSeconds,
    startRecording,
    stopRecording,
    cancelEdit,
  };
}
