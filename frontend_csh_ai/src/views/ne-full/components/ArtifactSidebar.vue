<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { authState } from '@/utils/request';
import MarkdownRenderer from './MarkdownRenderer.vue';

interface Artifact {
  id?: string;
  file_id?: string;
  type: 'code' | 'markdown' | 'file' | 'text' | 'html' | string;
  title: string;
  url?: string;
  content?: string;
  language?: string;
  mime_type?: string;
  extension?: string;
  previewable?: boolean;
  is_streaming?: boolean;
  metadata?: Record<string, any>;
}

const CODE_LANGUAGE_BY_EXTENSION: Record<string, string> = {
  py: 'python',
  js: 'javascript',
  jsx: 'jsx',
  ts: 'typescript',
  tsx: 'tsx',
  vue: 'vue',
  java: 'java',
  go: 'go',
  rs: 'rust',
  c: 'c',
  cc: 'cpp',
  cpp: 'cpp',
  h: 'c',
  hpp: 'cpp',
  cs: 'csharp',
  php: 'php',
  rb: 'ruby',
  swift: 'swift',
  kt: 'kotlin',
  scala: 'scala',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  ps1: 'powershell',
  sql: 'sql',
  css: 'css',
  scss: 'scss',
  less: 'less',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  xml: 'xml',
  html: 'html',
  htm: 'html',
  md: 'markdown',
  markdown: 'markdown',
};

const props = defineProps<{
  artifacts: Artifact[];
  show: boolean;
  resetKey?: number;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
}>();

const selectedId = ref<string | null>(null);
const currentArtifact = ref<Artifact | null>(null);
const downloading = ref(false);
const previewLoading = ref(false);
const previewError = ref('');
const previewCache = new Map<string, Artifact>();
let latestPreviewRequestId = 0;

const getArtifactId = (art: Artifact | null | undefined) => {
  return String(art?.id || art?.file_id || '').trim();
};

const getArtifactFilename = (art: Artifact | null) => {
  return String(art?.metadata?.filename || art?.title || '').trim();
};

const getArtifactExtension = (art: Artifact | null) => {
  const explicit = String(art?.extension || art?.metadata?.extension || '').trim().replace(/^\./, '').toLowerCase();
  if (explicit) return explicit;

  const filename = getArtifactFilename(art);
  const lastDot = filename.lastIndexOf('.');
  if (lastDot < 0) return '';
  return filename.slice(lastDot + 1).toLowerCase();
};

const getArtifactMimeType = (art: Artifact | null) => {
  return String(art?.mime_type || art?.metadata?.mime_type || '').trim().toLowerCase();
};

const resolveArtifactLanguage = (art: Artifact | null) => {
  const explicit = String(art?.language || art?.metadata?.language || '').trim();
  if (explicit) return explicit;
  return CODE_LANGUAGE_BY_EXTENSION[getArtifactExtension(art)] || '';
};

const resolveArtifactType = (art: Artifact | null): Artifact['type'] => {
  const explicit = String(art?.type || '').trim().toLowerCase();
  if (explicit && explicit !== 'file') return explicit;

  const extension = getArtifactExtension(art);
  const mimeType = getArtifactMimeType(art);

  if (extension === 'md' || extension === 'markdown' || mimeType.includes('markdown')) return 'markdown';
  if (extension === 'html' || extension === 'htm' || mimeType === 'text/html' || mimeType === 'application/xhtml+xml') return 'html';
  if (CODE_LANGUAGE_BY_EXTENSION[extension]) return 'code';
  if (mimeType.startsWith('text/')) return 'text';
  if (['application/json', 'application/xml', 'text/xml', 'application/yaml', 'application/x-yaml', 'text/yaml', 'application/toml', 'text/javascript', 'application/javascript'].includes(mimeType)) {
    return CODE_LANGUAGE_BY_EXTENSION[extension] ? 'code' : 'text';
  }

  return explicit || 'file';
};

const buildDisplayArtifact = (art: Artifact, overrides: Partial<Artifact> = {}): Artifact => {
  const mergedMetadata = {
    ...(art.metadata || {}),
    ...(overrides.metadata || {}),
  };

  const mergedArtifact: Artifact = {
    ...art,
    ...overrides,
    metadata: mergedMetadata,
  };

  const type = resolveArtifactType(mergedArtifact);
  const language = resolveArtifactLanguage(mergedArtifact);

  return {
    ...mergedArtifact,
    type,
    language: language || undefined,
    mime_type: getArtifactMimeType(mergedArtifact) || undefined,
    extension: getArtifactExtension(mergedArtifact) || undefined,
    previewable: type !== 'file',
  };
};

const updateCurrentArtifact = (art: Artifact | null) => {
  if (!art) {
    currentArtifact.value = null;
    return;
  }

  const artId = getArtifactId(art);
  const cached = artId ? previewCache.get(artId) : undefined;
  currentArtifact.value = buildDisplayArtifact(art, cached || {});
};

watch(() => props.artifacts, (newArts) => {
  if (newArts.length === 0) {
    selectedId.value = null;
    currentArtifact.value = null;
    previewLoading.value = false;
    previewError.value = '';
    return;
  }

  if (!selectedId.value) {
    selectArtifact(newArts[newArts.length - 1]);
    return;
  }

  const matched = newArts.find((art) => getArtifactId(art) === selectedId.value) || null;
  if (matched) {
    updateCurrentArtifact(matched);
    void ensureArtifactPreview(currentArtifact.value);
  }
}, { immediate: true, deep: true });

watch(() => props.resetKey, () => {
  selectedId.value = null;
  currentArtifact.value = null;
  previewLoading.value = false;
  previewError.value = '';
});

const selectArtifact = (art: Artifact) => {
  const id = getArtifactId(art) || 'unknown';
  selectedId.value = id;
  previewError.value = '';
  updateCurrentArtifact(art);
  void ensureArtifactPreview(currentArtifact.value);
};

const getArtifactHref = (art: Artifact | null) => {
  if (!art) return '';
  return String(art.url || art.metadata?.url || '').trim();
};

const getFilenameFromDisposition = (value: string | null): string | null => {
  if (!value) return null;

  const utf8Match = value.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }

  const plainMatch = value.match(/filename="?([^";]+)"?/i);
  return plainMatch?.[1] || null;
};

const ensureArtifactPreview = async (art: Artifact | null) => {
  if (!art) return;

  const artId = getArtifactId(art);
  if (!artId) return;

  const displayArtifact = buildDisplayArtifact(art);
  if (displayArtifact.content || displayArtifact.type === 'file') return;

  const url = getArtifactHref(displayArtifact);
  if (!url) return;

  const requestId = ++latestPreviewRequestId;
  previewLoading.value = true;
  previewError.value = '';

  try {
    const headers: Record<string, string> = {};
    if (authState.value.token) {
      headers.Authorization = `Bearer ${authState.value.token}`;
    }

    const response = await fetch(url, {
      method: 'GET',
      headers,
    });

    if (!response.ok) {
      throw new Error(`预览加载失败 (${response.status})`);
    }

    const responseMimeType = String(response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
    const textContent = await response.text();
    const resolvedArtifact = buildDisplayArtifact(displayArtifact, {
      content: textContent,
      mime_type: responseMimeType || displayArtifact.mime_type,
      metadata: {
        ...(displayArtifact.metadata || {}),
        mime_type: responseMimeType || displayArtifact.mime_type,
      },
    });

    previewCache.set(artId, resolvedArtifact);

    if (requestId === latestPreviewRequestId && selectedId.value === artId) {
      currentArtifact.value = resolvedArtifact;
    }
  } catch (error: any) {
    if (requestId === latestPreviewRequestId && selectedId.value === artId) {
      previewError.value = error?.message || '预览加载失败，请稍后重试';
    }
  } finally {
    if (requestId === latestPreviewRequestId) {
      previewLoading.value = false;
    }
  }
};

const downloadArtifact = async (art: Artifact | null) => {
  const url = getArtifactHref(art);
  if (!url || downloading.value) return;

  // External links may not require bearer auth; open directly.
  if (!url.startsWith('/api/')) {
    window.open(url, '_blank', 'noopener,noreferrer');
    return;
  }

  try {
    downloading.value = true;

    const headers: Record<string, string> = {};
    if (authState.value.token) {
      headers.Authorization = `Bearer ${authState.value.token}`;
    }

    const response = await fetch(url, {
      method: 'GET',
      headers,
    });

    if (!response.ok) {
      const fallback = `下载失败 (${response.status})`;
      let detail = fallback;
      try {
        const body = await response.json();
        detail = body?.detail || body?.message || fallback;
      } catch {
        detail = fallback;
      }
      throw new Error(detail);
    }

    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition');
    const filenameFromHeader = getFilenameFromDisposition(disposition);
    const fallbackName = (art?.title || 'download').trim();
    const filename = filenameFromHeader || fallbackName;

    const objectUrl = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(objectUrl);
  } catch (error: any) {
    alert(error?.message || '下载失败，请稍后重试');
  } finally {
    downloading.value = false;
  }
};

const close = () => emit('close');

const currentArtifactKind = computed(() => resolveArtifactType(currentArtifact.value));
const currentArtifactLanguage = computed(() => resolveArtifactLanguage(currentArtifact.value));
const currentArtifactMime = computed(() => getArtifactMimeType(currentArtifact.value));
const currentArtifactIsStreaming = computed(() => {
  return Boolean(currentArtifact.value?.is_streaming || currentArtifact.value?.metadata?.is_streaming);
});

const htmlPreviewSrcdoc = computed(() => {
  if (currentArtifactKind.value !== 'html') return '';

  const rawHtml = String(currentArtifact.value?.content || '');
  if (!rawHtml.trim()) return '';

  if (/<html[\s>]/i.test(rawHtml)) {
    return rawHtml;
  }

  return `<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      body { margin: 0; padding: 16px; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #0f172a; }
      img, video, canvas, svg, table, pre { max-width: 100%; }
      pre { white-space: pre-wrap; word-break: break-word; }
    </style>
  </head>
  <body>${rawHtml}</body>
</html>`;
});
</script>

<template>
  <div 
    v-if="show"
    class="w-[min(48rem,56vw)] min-w-[36rem] max-w-[60vw] border-l border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 flex flex-col h-full shadow-2xl relative z-40 transition-all duration-300 transform"
  >
    <!-- Header -->
    <div class="h-14 px-4 border-b border-slate-100 dark:border-slate-800 flex items-center justify-between bg-slate-50/50 dark:bg-slate-900/50">
      <div class="flex items-center space-x-2">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-indigo-500">
          <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/>
        </svg>
        <span class="text-xs font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200">工件预览 ({{ artifacts.length }})</span>
      </div>
      <button @click="close" class="p-1.5 hover:bg-slate-200 dark:hover:bg-slate-800 rounded-lg transition-colors text-slate-400">
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>

    <div class="flex-1 flex flex-col overflow-hidden">
      <!-- Artifact List (Vertical) -->
      <div
        v-if="artifacts.length > 1"
        class="border-b border-slate-100 dark:border-slate-800 bg-white dark:bg-slate-900 max-h-52 overflow-y-auto p-2 space-y-2"
      >
        <button
          v-for="(art, idx) in artifacts"
          :key="art.id || art.file_id"
          @click="selectArtifact(art)"
          class="w-full text-left px-3 py-2 rounded-lg text-xs font-medium transition-all"
          :class="[
            (art.id || art.file_id) === selectedId
              ? 'bg-indigo-600 text-white shadow-md'
              : 'bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700'
          ]"
        >
          <div class="flex items-start justify-between gap-2">
            <span class="truncate">{{ art.title || '未命名' }}</span>
            <span class="shrink-0 opacity-70">{{ idx + 1 }}</span>
          </div>
        </button>
      </div>

      <!-- Preview Content -->
      <div v-if="currentArtifact" class="flex-1 overflow-y-auto p-4 custom-scrollbar bg-slate-50/30 dark:bg-slate-950/20">
        <div class="mb-4">
          <div class="flex items-start justify-between gap-3">
            <div>
              <h2 class="text-lg font-bold text-slate-800 dark:text-slate-100 mb-1">{{ currentArtifact.title }}</h2>
              <div class="flex items-center space-x-2 flex-wrap">
                <span class="text-[10px] px-2 py-0.5 rounded bg-slate-200 dark:bg-slate-800 text-slate-500 font-mono">{{ currentArtifactKind.toUpperCase() }}</span>
                <span v-if="currentArtifactLanguage" class="text-[10px] px-2 py-0.5 rounded bg-emerald-50 dark:bg-emerald-900/20 text-emerald-600 dark:text-emerald-300 font-mono">{{ currentArtifactLanguage }}</span>
                <span v-if="currentArtifactMime" class="text-[10px] px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800/80 text-slate-500 font-mono">{{ currentArtifactMime }}</span>
              </div>
            </div>
            <button
              v-if="getArtifactHref(currentArtifact)"
              type="button"
              :disabled="downloading"
              @click="downloadArtifact(currentArtifact)"
              class="shrink-0 px-3 py-1.5 rounded-lg border border-slate-200 dark:border-slate-700 text-xs font-medium text-slate-600 dark:text-slate-300 hover:border-indigo-300 hover:text-indigo-600 dark:hover:border-indigo-700 dark:hover:text-indigo-300 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {{ downloading ? '下载中...' : '下载文件' }}
            </button>
          </div>
        </div>

        <div class="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-sm overflow-hidden">
          <div v-if="previewLoading" class="p-12 flex flex-col items-center justify-center text-center text-slate-500 dark:text-slate-400">
            <div class="h-8 w-8 rounded-full border-2 border-indigo-200 border-t-indigo-600 animate-spin mb-4"></div>
            <p class="text-sm font-medium">正在加载文档预览...</p>
          </div>

          <div v-else-if="previewError" class="p-12 flex flex-col items-center justify-center text-center">
            <p class="text-sm font-medium text-slate-700 dark:text-slate-200 mb-2">暂时无法渲染该工件</p>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-6">{{ previewError }}</p>
            <button
              v-if="getArtifactHref(currentArtifact)"
              type="button"
              :disabled="downloading"
              @click="downloadArtifact(currentArtifact)"
              class="px-6 py-2.5 bg-indigo-600 text-white text-sm font-bold rounded-xl shadow-lg shadow-indigo-200 hover:bg-indigo-700 transition-all dark:shadow-none disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {{ downloading ? '下载中...' : '下载查看' }}
            </button>
          </div>

          <!-- Code View -->
          <div v-else-if="currentArtifactKind === 'code'" class="relative group">
            <div class="absolute right-3 top-3 opacity-0 group-hover:opacity-100 transition-opacity">
              <button class="p-2 bg-white/80 dark:bg-slate-800/80 rounded-md shadow-sm text-slate-500 hover:text-indigo-500 transition-colors">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              </button>
            </div>
            <pre class="p-4 text-xs font-mono text-slate-700 dark:text-slate-300 overflow-x-auto bg-[#fafafa] dark:bg-[#0d1117]"><code>{{ currentArtifact.content }}</code></pre>
          </div>

          <!-- Markdown View -->
          <div v-else-if="currentArtifactKind === 'markdown'" class="p-6">
            <MarkdownRenderer :content="currentArtifact.content || ''" :is-streaming="currentArtifactIsStreaming" />
          </div>

          <!-- HTML View -->
          <div v-else-if="currentArtifactKind === 'html'" class="bg-white">
            <iframe
              title="HTML 文档预览"
              class="w-full h-[70vh] border-0 bg-white"
              sandbox="allow-scripts allow-same-origin allow-popups allow-modals"
              :srcdoc="htmlPreviewSrcdoc"
            />
          </div>

          <!-- File View -->
          <div v-else-if="currentArtifactKind === 'file'" class="p-12 flex flex-col items-center justify-center text-center">
            <div class="w-16 h-16 bg-blue-50 dark:bg-blue-900/20 rounded-2xl flex items-center justify-center mb-4">
               <svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="text-blue-500"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/></svg>
            </div>
            <p class="text-sm font-medium text-slate-700 dark:text-slate-200 mb-6">该工件为二进制文件或外部资源</p>
            <button
              v-if="getArtifactHref(currentArtifact)"
              type="button"
              :disabled="downloading"
              @click="downloadArtifact(currentArtifact)"
              class="px-6 py-2.5 bg-indigo-600 text-white text-sm font-bold rounded-xl shadow-lg shadow-indigo-200 hover:bg-indigo-700 transition-all dark:shadow-none disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {{ downloading ? '下载中...' : '点击下载 / 预览' }}
            </button>
          </div>

          <!-- Text/Default View -->
          <div v-else class="p-6">
            <p class="text-sm text-slate-600 dark:text-slate-400 whitespace-pre-wrap font-sans leading-relaxed">{{ currentArtifact.content }}</p>
          </div>
        </div>
      </div>

      <!-- Empty Preview -->
      <div v-else class="flex-1 flex flex-col items-center justify-center text-slate-400 p-8 text-center">
        <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" class="opacity-20 mb-4"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
        <p class="text-sm">选择一个工件以预览内容</p>
      </div>
    </div>
  </div>
</template>
