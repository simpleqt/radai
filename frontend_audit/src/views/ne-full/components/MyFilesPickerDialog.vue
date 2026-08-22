<script setup lang="ts">
import { ref, onMounted, watch } from 'vue';
import { authState } from '@/utils/request';

interface Props {
  show: boolean;
}

const props = defineProps<Props>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'confirm', selectedFiles: any[]): void;
}>();

const files = ref<any[]>([]);
const total = ref(0);
const loading = ref(false);

// 搜索与过滤条件
const keyword = ref('');
const fileType = ref('');
const source = ref('');
const limit = ref(10);
const offset = ref(0);

// 已选中的文件
const selectedFileIds = ref<string[]>([]);

// 过滤选项
const typeOptions = [
  { label: '全部类型', value: '' },
  { label: '图片', value: 'image' },
  { label: '文本文档', value: 'document' },
  { label: '表格数据', value: 'spreadsheet' },
  { label: '幻灯片', value: 'presentation' },
];

const sourceOptions = [
  { label: '全部来源', value: '' },
  { label: '主动上传', value: 'minio' },
  { label: '系统生成', value: 'local' },
];

async function fetchFiles() {
  loading.value = true;
  const token = authState.value.token;
  try {
    const params = new URLSearchParams({
      limit: String(limit.value),
      offset: String(offset.value),
    });
    if (keyword.value.trim()) params.append('keyword', keyword.value.trim());
    if (fileType.value) params.append('file_type', fileType.value);
    if (source.value) params.append('source', source.value);

    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const res = await fetch(`/api/v1/my-files?${params.toString()}`, {
      headers,
    });
    if (res.ok) {
      const data = await res.json();
      files.value = data.items || [];
      total.value = data.total || 0;
    }
  } catch (err) {
    console.error('加载个人文件失败:', err);
  } finally {
    loading.value = false;
  }
}

function handleSearch() {
  offset.value = 0;
  fetchFiles();
}

function toggleSelectFile(file: any) {
  const index = selectedFileIds.value.indexOf(file.file_id);
  if (index === -1) {
    selectedFileIds.value.push(file.file_id);
  } else {
    selectedFileIds.value.splice(index, 1);
  }
}

function isSelected(fileId: string): boolean {
  return selectedFileIds.value.includes(fileId);
}

function confirmSelection() {
  const selectedList = files.value.filter(f => selectedFileIds.value.includes(f.file_id));
  emit('confirm', selectedList);
  emit('close');
}

function formatBytes(bytes: number, decimals = 2) {
  if (!bytes) return '0 Bytes';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['Bytes', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

function formatDate(dateStr: string) {
  if (!dateStr) return '';
  const date = new Date(dateStr);
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}

function getFileIconPath(name: string) {
  const ext = name.split('.').pop()?.toLowerCase();
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'].includes(ext || '')) {
    return 'M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z';
  }
  if (['xlsx', 'xls', 'csv'].includes(ext || '')) {
    return 'M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z';
  }
  if (['ppt', 'pptx'].includes(ext || '')) {
    return 'M7 12l3-3 3 3 4-4M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z';
  }
  return 'M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253';
}

// 翻页操作
function prevPage() {
  if (offset.value > 0) {
    offset.value -= limit.value;
    fetchFiles();
  }
}

function nextPage() {
  if (offset.value + limit.value < total.value) {
    offset.value += limit.value;
    fetchFiles();
  }
}

watch(() => props.show, (newVal) => {
  if (newVal) {
    selectedFileIds.value = [];
    keyword.value = '';
    fileType.value = '';
    source.value = '';
    offset.value = 0;
    fetchFiles();
  }
});
</script>

<template>
  <Transition name="fade">
    <div v-if="show" class="fixed inset-0 z-[100] flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="选择我的文件">
      <!-- Backdrop with premium blur effect -->
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="emit('close')"></div>

      <!-- Modal window -->
      <div class="relative flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-white/55 bg-surface/90 shadow-2xl backdrop-blur-2xl animate-scale-up dark:border-white/10 dark:bg-surface/95">
        <!-- Header -->
        <div class="px-6 py-4 border-b border-border-subtle flex items-center justify-between">
          <div class="flex items-center space-x-2.5">
            <div class="w-8 h-8 rounded-lg bg-accent-soft flex items-center justify-center text-accent">
              <svg class="w-4.5 h-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z" />
              </svg>
            </div>
            <div>
              <h3 class="text-base font-bold text-text-primary">选择个人文件库文件</h3>
              <p class="text-xs text-text-tertiary mt-0.5">可复用历史已上传及系统生成的文件，避免重复解析</p>
            </div>
          </div>
          <button @click="emit('close')" class="p-1.5 rounded-lg text-text-tertiary hover:text-text-secondary transition-colors">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <!-- Filter bar -->
        <div class="p-4 bg-surface-sunken border-b border-border-subtle flex flex-col sm:flex-row gap-3">
          <div class="flex-1 relative">
            <input
              type="text"
              v-model="keyword"
              placeholder="搜索文件名..."
              @keyup.enter="handleSearch"
              class="w-full h-9 pl-9 pr-4 rounded-lg border border-border-subtle bg-surface text-sm focus:outline-none focus:border-accent text-text-secondary"
            />
            <svg class="absolute left-3 top-2.5 w-4 h-4 text-text-tertiary" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          </div>
          <div class="flex gap-2">
            <select
              v-model="fileType"
              @change="handleSearch"
              class="h-9 px-3 rounded-lg border border-border-subtle bg-surface text-sm focus:outline-none text-text-secondary"
            >
              <option v-for="opt in typeOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
            </select>
            <select
              v-model="source"
              @change="handleSearch"
              class="h-9 px-3 rounded-lg border border-border-subtle bg-surface text-sm focus:outline-none text-text-secondary"
            >
              <option v-for="opt in sourceOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
            </select>
            <button @click="handleSearch" class="px-4 bg-accent hover:bg-accent-hover text-white rounded-lg text-sm font-semibold transition-colors flex items-center justify-center">
              筛选
            </button>
          </div>
        </div>

        <!-- File List -->
        <div class="flex-1 min-h-[300px] overflow-y-auto p-4">
          <div v-if="loading" class="flex flex-col items-center justify-center h-64">
            <svg class="animate-spin h-8 w-8 text-accent" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span class="text-sm text-text-tertiary mt-2">加载文件列表中...</span>
          </div>

          <div v-else-if="files.length === 0" class="flex flex-col items-center justify-center h-64 text-center">
            <svg class="w-12 h-12 text-text-tertiary" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M9 13h6m-3-3v6m-9 1V4a2 2 0 012-2h6l2 2h6a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
            </svg>
            <p class="text-text-tertiary text-sm mt-3">没有找到任何匹配的文件</p>
          </div>

          <div v-else class="space-y-1.5">
            <div
              v-for="file in files"
              :key="file.file_id"
              @click="toggleSelectFile(file)"
              class="group flex items-center justify-between p-3 rounded-xl border transition-all cursor-pointer"
              :class="isSelected(file.file_id) ? 'border-accent bg-accent-soft'
                : 'border-border-subtle hover:border-border-subtle bg-surface'"
            >
              <div class="flex items-center space-x-3 min-w-0">
                <!-- Checkbox -->
                <div
                  class="w-5 h-5 rounded border flex items-center justify-center transition-colors flex-shrink-0"
                  :class="isSelected(file.file_id) ? 'bg-accent border-accent text-white'
                    : 'border-border-subtle text-transparent'"
                >
                  <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                </div>

                <!-- File Icon -->
                <div class="w-9 h-9 rounded-lg bg-surface-sunken flex items-center justify-center text-text-tertiary group-hover:text-accent transition-colors flex-shrink-0">
                  <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" :d="getFileIconPath(file.display_name)" />
                  </svg>
                </div>

                <!-- File info -->
                <div class="min-w-0">
                  <p class="text-sm font-semibold text-text-secondary truncate" :title="file.display_name">
                    {{ file.display_name }}
                  </p>
                  <div class="flex items-center space-x-3 text-xs text-text-tertiary mt-0.5">
                    <span>{{ formatBytes(file.size_bytes) }}</span>
                    <span>•</span>
                    <span>{{ formatDate(file.created_at) }}</span>
                  </div>
                </div>
              </div>

              <!-- Badges -->
              <div class="flex items-center space-x-2 flex-shrink-0">
                <span
                  class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider"
                  :class="file.source_type === 'minio' ? 'bg-accent-soft text-accent'
                    : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-950/40 dark:text-emerald-400'"
                >
                  {{ file.source_type === 'minio' ? '上传' : '自动生成' }}
                </span>
              </div>
            </div>
          </div>
        </div>

        <!-- Footer / Action bar -->
        <div class="px-6 py-4 border-t border-border-subtle flex items-center justify-between bg-surface-sunken">
          <!-- Pagination -->
          <div class="flex items-center space-x-2">
            <button
              @click="prevPage"
              :disabled="offset === 0 || loading"
              class="p-1.5 rounded-lg border border-border-subtle disabled:opacity-50 text-text-secondary hover:bg-surface-hover transition-colors"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
              </svg>
            </button>
            <span class="text-xs text-text-tertiary font-medium">
              第 {{ Math.floor(offset / limit) + 1 }} 页 / 共 {{ Math.ceil(total / limit) || 1 }} 页
            </span>
            <button
              @click="nextPage"
              :disabled="offset + limit >= total || loading"
              class="p-1.5 rounded-lg border border-border-subtle disabled:opacity-50 text-text-secondary hover:bg-surface-hover transition-colors"
            >
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
              </svg>
            </button>
          </div>

          <div class="flex items-center space-x-2">
            <button @click="emit('close')" class="px-4 py-2 border border-border-subtle rounded-xl text-sm font-semibold text-text-secondary hover:bg-surface-hover transition-colors">
              取消
            </button>
            <button
              @click="confirmSelection"
              :disabled="selectedFileIds.length === 0"
              class="px-5 py-2 bg-accent hover:bg-accent-hover disabled:bg-surface-sunken disabled:text-text-tertiary text-white rounded-xl text-sm font-semibold transition-colors flex items-center space-x-2 shadow-sm dark:shadow-none"
            >
              <span>确认添加 (已选 {{ selectedFileIds.length }} 个)</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.25s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}

@keyframes scaleUp {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
}

.animate-scale-up {
  animation: scaleUp 0.2s cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
</style>
