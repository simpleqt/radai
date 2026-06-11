<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue';
import { useRouter } from 'vue-router';
import { authState } from '@/utils/request';
import HistorySidebar from './components/HistorySidebar.vue';

const router = useRouter();

const files = ref<any[]>([]);
const total = ref(0);
const loading = ref(false);
const showMobileSidebar = ref(false);

// 搜索与过滤条件
const keyword = ref('');
const fileType = ref('');
const source = ref('');
const limit = ref(15);
const offset = ref(0);

// 上传状态
const uploading = ref(false);
const fileInputRef = ref<HTMLInputElement | null>(null);

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

// 触发本地文件上传
function triggerUpload() {
  fileInputRef.value?.click();
}

async function handleFileUpload(e: Event) {
  const input = e.target as HTMLInputElement;
  if (!input.files || input.files.length === 0) return;

  const file = input.files[0];
  const token = authState.value.token;
  uploading.value = true;

  try {
    const formData = new FormData();
    formData.append('file', file);

    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const res = await fetch('/api/v1/files/upload', {
      method: 'POST',
      headers,
      body: formData,
    });

    if (res.ok) {
      input.value = '';
      offset.value = 0;
      await fetchFiles();
    } else {
      const errData = await res.json();
      alert(`上传失败: ${errData.detail || '未知错误'}`);
    }
  } catch (err) {
    console.error('上传文件失败:', err);
    alert('上传文件出现网络异常');
  } finally {
    uploading.value = false;
  }
}

// 重命名文件
async function handleRename(file: any) {
  const newName = prompt('请输入新的文件名:', file.display_name);
  if (!newName || newName === file.display_name) return;

  const token = authState.value.token;
  try {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const res = await fetch(`/api/v1/my-files/${file.file_id}`, {
      method: 'PUT',
      headers,
      body: JSON.stringify({ file_name: newName }),
    });

    if (res.ok) {
      await fetchFiles();
    } else {
      alert('重命名失败');
    }
  } catch (err) {
    console.error('重命名文件失败:', err);
  }
}

// 软删除文件
async function handleDelete(file: any) {
  if (!confirm(`确定要删除文件 "${file.display_name}" 吗？此操作将解绑关联的会话！`)) return;

  const token = authState.value.token;
  try {
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const res = await fetch(`/api/v1/my-files/${file.file_id}`, {
      method: 'DELETE',
      headers,
    });

    if (res.ok) {
      await fetchFiles();
    } else {
      alert('删除失败');
    }
  } catch (err) {
    console.error('删除文件失败:', err);
  }
}

// 触发下载
function handleDownload(file: any) {
  const token = authState.value.token;
  const url = `${file.download_url}${token ? `?token=${token}` : ''}`;
  window.open(url, '_blank');
}

function handleNewChat() {
  router.push('/app');
}

function handleAuthAction() {
  if (!authState.value.token) {
    router.push('/login');
  } else {
    // logout
    authState.value.token = '';
    authState.value.user = null;
    router.push('/login');
  }
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
    year: 'numeric',
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

onMounted(() => {
  fetchFiles();
});
</script>

<template>
  <div class="flex h-screen bg-white dark:bg-[#0b0f1a] relative overflow-hidden">
    <!-- Premium Backdrop Accent -->
    <div class="absolute inset-0 bg-[radial-gradient(circle_at_50%_-10%,#eff6ff,transparent_50%)] dark:bg-[radial-gradient(circle_at_50%_-10%,#1e1b4b,transparent_60%)] pointer-events-none opacity-30"></div>

    <input
      ref="fileInputRef"
      type="file"
      class="hidden"
      @change="handleFileUpload"
    />

    <!-- Desktop Sidebar -->
    <HistorySidebar
      class="hidden md:flex flex-shrink-0"
      @new-chat="handleNewChat"
      @auth-action="handleAuthAction"
    />

    <!-- Mobile Toggled Sidebar -->
    <Transition name="mobile-drawer">
      <div
        v-if="showMobileSidebar"
        class="fixed inset-0 z-[55] bg-black/35 md:hidden"
        @click.self="showMobileSidebar = false"
      >
        <div class="absolute left-0 top-0 h-full w-[84%] max-w-[320px] bg-white dark:bg-[#0f172a] shadow-2xl">
          <HistorySidebar
            @new-chat="handleNewChat"
            @auth-action="handleAuthAction"
          />
        </div>
      </div>
    </Transition>

    <!-- Main Folder Container -->
    <div class="flex-1 flex flex-col min-w-0 relative z-10">
      <!-- Header -->
      <header class="h-14 border-b border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-900/85 backdrop-blur-md px-6 flex items-center justify-between">
        <div class="flex items-center space-x-3">
          <button
            @click="showMobileSidebar = !showMobileSidebar"
            class="md:hidden inline-flex items-center justify-center w-8 h-8 rounded-lg border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-300"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/>
            </svg>
          </button>
          <div class="flex items-center space-x-2">
            <svg class="w-5 h-5 text-indigo-600 dark:text-indigo-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
            </svg>
            <h1 class="text-base font-bold text-slate-800 dark:text-slate-100 tracking-tight">个人文件管理中心</h1>
          </div>
        </div>

        <button
          @click="router.push('/app')"
          class="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl border border-slate-200 dark:border-slate-800 text-sm font-semibold text-slate-500 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-800 transition-colors"
        >
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
          </svg>
          <span>回到对话</span>
        </button>
      </header>

      <!-- Center content panel -->
      <div class="flex-1 min-h-0 overflow-y-auto p-6 flex flex-col">
        <!-- Advanced Search and Actions -->
        <div class="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800/80 rounded-2xl p-5 mb-6 shadow-sm flex flex-col md:flex-row gap-4 items-center justify-between">
          <div class="flex-1 w-full flex flex-col sm:flex-row gap-3">
            <!-- Search bar -->
            <div class="flex-1 relative">
              <input
                type="text"
                v-model="keyword"
                placeholder="按照文件名检索文件..."
                @keyup.enter="handleSearch"
                class="w-full h-10 pl-10 pr-4 rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50/50 dark:bg-slate-800/50 text-sm focus:outline-none focus:border-indigo-500 text-slate-700 dark:text-slate-200"
              />
              <svg class="absolute left-3.5 top-3 w-4.5 h-4.5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
            </div>
            
            <div class="flex gap-2">
              <select
                v-model="fileType"
                @change="handleSearch"
                class="h-10 px-3 rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50/50 dark:bg-slate-800/50 text-sm focus:outline-none text-slate-700 dark:text-slate-200"
              >
                <option v-for="opt in typeOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
              <select
                v-model="source"
                @change="handleSearch"
                class="h-10 px-3 rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50/50 dark:bg-slate-800/50 text-sm focus:outline-none text-slate-700 dark:text-slate-200"
              >
                <option v-for="opt in sourceOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
              <button @click="handleSearch" class="px-5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-sm font-semibold transition-colors flex items-center justify-center shadow-lg shadow-indigo-100 dark:shadow-none">
                查询
              </button>
            </div>
          </div>

          <!-- Upload button -->
          <button
            @click="triggerUpload"
            :disabled="uploading"
            class="w-full md:w-auto h-10 px-5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-sm font-bold transition-colors flex items-center justify-center space-x-2 shadow-lg shadow-emerald-100 dark:shadow-none"
          >
            <svg v-if="uploading" class="animate-spin h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
            </svg>
            <svg v-else class="w-4.5 h-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
              <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
            </svg>
            <span>{{ uploading ? '上传中...' : '上传新文件' }}</span>
          </button>
        </div>

        <!-- Files Data Grid -->
        <div class="flex-1 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800/80 rounded-2xl shadow-sm flex flex-col overflow-hidden">
          <div class="flex-1 overflow-x-auto">
            <table class="w-full text-left border-collapse min-w-[700px]">
              <thead>
                <tr class="border-b border-slate-100 dark:border-slate-800/85 text-xs text-slate-400 font-bold uppercase tracking-wider bg-slate-50/50 dark:bg-slate-800/20">
                  <th class="px-6 py-4">文件名</th>
                  <th class="px-6 py-4">大小</th>
                  <th class="px-6 py-4">类型</th>
                  <th class="px-6 py-4">上传时间</th>
                  <th class="px-6 py-4">来源</th>
                  <th class="px-6 py-4 text-right">操作</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100 dark:divide-slate-850">
                <tr v-if="loading" class="text-center">
                  <td colspan="6" class="py-16">
                    <svg class="animate-spin h-7 w-7 text-indigo-500 mx-auto" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                    </svg>
                    <span class="text-xs text-slate-400 mt-2 block">加载个人文件数据中...</span>
                  </td>
                </tr>

                <tr v-else-if="files.length === 0" class="text-center">
                  <td colspan="6" class="py-16 text-slate-400 text-sm">
                    <svg class="w-12 h-12 text-slate-200 dark:text-slate-700 mx-auto mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
                    </svg>
                    <span>暂无文件记录，点击右上角上传首个文件</span>
                  </td>
                </tr>

                <tr
                  v-else
                  v-for="file in files"
                  :key="file.file_id"
                  class="hover:bg-slate-50/40 dark:hover:bg-slate-850/20 transition-colors group"
                >
                  <td class="px-6 py-4 min-w-[280px]">
                    <div class="flex items-center space-x-3.5">
                      <div class="w-9 h-9 rounded-xl bg-slate-50 dark:bg-slate-800 flex items-center justify-center text-slate-400 group-hover:text-indigo-500 transition-colors flex-shrink-0">
                        <svg class="w-4.5 h-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                          <path stroke-linecap="round" stroke-linejoin="round" :d="getFileIconPath(file.display_name)" />
                        </svg>
                      </div>
                      <span class="text-sm font-semibold text-slate-700 dark:text-slate-200 truncate max-w-[200px]" :title="file.display_name">
                        {{ file.display_name }}
                      </span>
                    </div>
                  </td>
                  <td class="px-6 py-4 text-xs font-semibold text-slate-500 dark:text-slate-400">
                    {{ formatBytes(file.size_bytes) }}
                  </td>
                  <td class="px-6 py-4 text-xs text-slate-500">
                    <span class="px-2 py-0.5 rounded-md bg-slate-100 dark:bg-slate-800 text-[10px] uppercase font-bold tracking-wide">
                      {{ file.mime_type?.split('/')?.[1] || 'DOC' }}
                    </span>
                  </td>
                  <td class="px-6 py-4 text-xs text-slate-400">
                    {{ formatDate(file.created_at) }}
                  </td>
                  <td class="px-6 py-4">
                    <span 
                      class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider"
                      :class="file.source_type === 'minio'
                        ? 'bg-indigo-50 text-indigo-600 dark:bg-indigo-950/40 dark:text-indigo-400'
                        : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-950/40 dark:text-emerald-400'"
                    >
                      {{ file.source_type === 'minio' ? '主动上传' : '系统生成' }}
                    </span>
                  </td>
                  <td class="px-6 py-4 text-right">
                    <div class="flex items-center justify-end space-x-1.5">
                      <button
                        @click="handleDownload(file)"
                        class="p-1.5 rounded-lg border border-slate-200 dark:border-slate-850 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-300 transition-colors"
                        title="下载"
                      >
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                        </svg>
                      </button>
                      <button
                        @click="handleRename(file)"
                        class="p-1.5 rounded-lg border border-slate-200 dark:border-slate-850 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-300 transition-colors"
                        title="重命名"
                      >
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                        </svg>
                      </button>
                      <button
                        @click="handleDelete(file)"
                        class="p-1.5 rounded-lg border border-slate-200 dark:border-slate-850 hover:bg-slate-100 dark:hover:bg-slate-800 text-rose-500 transition-colors"
                        title="删除"
                      >
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Pagination Footer -->
          <div class="px-6 py-4 border-t border-slate-100 dark:border-slate-800 flex items-center justify-between bg-slate-50/30 dark:bg-slate-900/10">
            <div class="text-xs text-slate-400 font-medium">
              共 {{ total }} 个文件 / 当前显示 {{ files.length }} 个
            </div>
            <div class="flex items-center space-x-2">
              <button
                @click="prevPage"
                :disabled="offset === 0 || loading"
                class="px-3.5 py-1.5 rounded-lg border border-slate-200 dark:border-slate-800 disabled:opacity-50 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800/80 transition-colors text-xs font-semibold"
              >
                上一页
              </button>
              <span class="text-xs text-slate-400 dark:text-slate-500 font-medium px-2">
                第 {{ Math.floor(offset / limit) + 1 }} 页 / 共 {{ Math.ceil(total / limit) || 1 }} 页
              </span>
              <button
                @click="nextPage"
                :disabled="offset + limit >= total || loading"
                class="px-3.5 py-1.5 rounded-lg border border-slate-200 dark:border-slate-800 disabled:opacity-50 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800/80 transition-colors text-xs font-semibold"
              >
                下一页
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mobile-drawer-enter-active,
.mobile-drawer-leave-active {
  transition: opacity 0.2s ease;
}

.mobile-drawer-enter-from,
.mobile-drawer-leave-to {
  opacity: 0;
}
</style>
