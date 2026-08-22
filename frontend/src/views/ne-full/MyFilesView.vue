<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue';
import { useRouter } from 'vue-router';
import { authState, downloadWithAuth } from '@/utils/request';
import AppShell from '@/components/layout/AppShell.vue';

const router = useRouter();

const files = ref<any[]>([]);
const total = ref(0);
const loading = ref(false);

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
async function handleDownload(file: any) {
  if (!file?.download_url) return;
  try {
    await downloadWithAuth(file.download_url, { filename: file.file_name || file.name || 'download' });
  } catch (err) {
    console.error('下载文件失败:', err);
    alert(err instanceof Error ? err.message : '下载失败');
  }
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
  <AppShell
    title="个人文件管理中心"
    brand-name="核电子学智能助手"
    brand-icon="Atom"
    @new-chat="handleNewChat"
    @auth-action="handleAuthAction"
  >
    <template #actions>
      <button
        type="button"
        class="inline-flex h-8 items-center gap-1.5 rounded-(--radius-control) border border-border-subtle px-2.5 text-xs font-medium text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary"
        @click="router.push('/app')"
      >
        <svg class="size-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
        </svg>
        <span class="hidden @compact/main:inline">回到对话</span>
      </button>
    </template>

    <input
      ref="fileInputRef"
      type="file"
      class="hidden"
      @change="handleFileUpload"
    />

    <div class="custom-scrollbar flex min-h-0 flex-1 flex-col overflow-y-auto p-4 @cozy/main:p-6">
        <!-- Advanced Search and Actions -->
        <div class="bg-surface border border-border-subtle rounded-2xl p-5 mb-6 shadow-sm flex flex-col md:flex-row gap-4 items-center justify-between">
          <div class="flex-1 w-full flex flex-col sm:flex-row gap-3">
            <!-- Search bar -->
            <div class="flex-1 relative">
              <input
                type="text"
                v-model="keyword"
                placeholder="按照文件名检索文件..."
                @keyup.enter="handleSearch"
                class="w-full h-10 pl-10 pr-4 rounded-xl border border-border-subtle bg-surface-sunken text-sm focus:outline-none focus:border-accent text-text-secondary"
              />
              <svg class="absolute left-3.5 top-3 w-4.5 h-4.5 text-text-tertiary" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
                <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
            </div>
            
            <div class="flex gap-2">
              <select
                v-model="fileType"
                @change="handleSearch"
                class="h-10 px-3 rounded-xl border border-border-subtle bg-surface-sunken text-sm focus:outline-none text-text-secondary"
              >
                <option v-for="opt in typeOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
              <select
                v-model="source"
                @change="handleSearch"
                class="h-10 px-3 rounded-xl border border-border-subtle bg-surface-sunken text-sm focus:outline-none text-text-secondary"
              >
                <option v-for="opt in sourceOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
              <button @click="handleSearch" class="px-5 bg-accent hover:bg-accent-hover text-white rounded-xl text-sm font-semibold transition-colors flex items-center justify-center shadow-sm dark:shadow-none">
                查询
              </button>
            </div>
          </div>

          <!-- Upload button -->
          <button
            @click="triggerUpload"
            :disabled="uploading"
            class="w-full md:w-auto h-10 px-5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-sm font-bold transition-colors flex items-center justify-center space-x-2 shadow-sm dark:shadow-none"
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
        <div class="flex-1 bg-surface border border-border-subtle rounded-2xl shadow-sm flex flex-col overflow-hidden">
          <div class="stack-table-wrap flex-1 overflow-x-auto">
            <table class="stack-table w-full text-left border-collapse min-w-[700px]">
              <thead>
                <tr class="border-b border-border-subtle text-xs text-text-tertiary font-bold uppercase tracking-wider bg-surface-sunken">
                  <th class="px-6 py-4">文件名</th>
                  <th class="px-6 py-4">大小</th>
                  <th class="px-6 py-4">类型</th>
                  <th class="px-6 py-4">上传时间</th>
                  <th class="px-6 py-4">来源</th>
                  <th class="px-6 py-4 text-right">操作</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-border-subtle">
                <tr v-if="loading" class="text-center">
                  <td colspan="6" class="py-16">
                    <svg class="animate-spin h-7 w-7 text-accent mx-auto" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                    </svg>
                    <span class="text-xs text-text-tertiary mt-2 block">加载个人文件数据中...</span>
                  </td>
                </tr>

                <tr v-else-if="files.length === 0" class="text-center">
                  <td colspan="6" class="py-16 text-text-tertiary text-sm">
                    <svg class="w-12 h-12 text-text-tertiary mx-auto mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" />
                    </svg>
                    <span>暂无文件记录，点击右上角上传首个文件</span>
                  </td>
                </tr>

                <tr
                  v-else
                  v-for="file in files"
                  :key="file.file_id"
                  class="hover:bg-surface-hover transition-colors group"
                >
                  <td class="px-6 py-4">
                    <div class="flex items-center space-x-3.5">
                      <div class="w-9 h-9 rounded-xl bg-surface-sunken flex items-center justify-center text-text-tertiary group-hover:text-accent transition-colors flex-shrink-0">
                        <svg class="w-4.5 h-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                          <path stroke-linecap="round" stroke-linejoin="round" :d="getFileIconPath(file.display_name)" />
                        </svg>
                      </div>
                      <span class="text-sm font-semibold text-text-secondary truncate max-w-[200px]" :title="file.display_name">
                        {{ file.display_name }}
                      </span>
                    </div>
                  </td>
                  <td class="px-6 py-4 text-xs font-semibold text-text-secondary" data-label="大小">
                    {{ formatBytes(file.size_bytes) }}
                  </td>
                  <td class="px-6 py-4 text-xs text-text-secondary" data-label="类型">
                    <span class="px-2 py-0.5 rounded-md bg-surface-sunken text-[10px] uppercase font-bold tracking-wide">
                      {{ file.mime_type?.split('/')?.[1] || 'DOC' }}
                    </span>
                  </td>
                  <td class="px-6 py-4 text-xs text-text-tertiary" data-label="上传时间">
                    {{ formatDate(file.created_at) }}
                  </td>
                  <td class="px-6 py-4" data-label="来源">
                    <span 
                      class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider"
                      :class="file.source_type === 'minio' ? 'bg-accent-soft text-accent' : 'bg-emerald-50 text-emerald-600 dark:bg-emerald-950/40 dark:text-emerald-400'"
                    >
                      {{ file.source_type === 'minio' ? '主动上传' : '系统生成' }}
                    </span>
                  </td>
                  <td class="px-6 py-4 text-right">
                    <div class="flex items-center justify-end space-x-1.5">
                      <button
                        @click="handleDownload(file)"
                        class="p-1.5 rounded-lg border border-border-subtle hover:bg-surface-hover text-text-secondary transition-colors"
                        title="下载"
                      >
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                        </svg>
                      </button>
                      <button
                        @click="handleRename(file)"
                        class="p-1.5 rounded-lg border border-border-subtle hover:bg-surface-hover text-text-secondary transition-colors"
                        title="重命名"
                      >
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                        </svg>
                      </button>
                      <button
                        @click="handleDelete(file)"
                        class="p-1.5 rounded-lg border border-border-subtle hover:bg-surface-hover text-rose-500 transition-colors"
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
          <div class="px-6 py-4 border-t border-border-subtle flex items-center justify-between bg-surface-sunken">
            <div class="text-xs text-text-tertiary font-medium">
              共 {{ total }} 个文件 / 当前显示 {{ files.length }} 个
            </div>
            <div class="flex items-center space-x-2">
              <button
                @click="prevPage"
                :disabled="offset === 0 || loading"
                class="px-3.5 py-1.5 rounded-lg border border-border-subtle disabled:opacity-50 text-text-secondary hover:bg-surface-hover transition-colors text-xs font-semibold"
              >
                上一页
              </button>
              <span class="text-xs text-text-tertiary font-medium px-2">
                第 {{ Math.floor(offset / limit) + 1 }} 页 / 共 {{ Math.ceil(total / limit) || 1 }} 页
              </span>
              <button
                @click="nextPage"
                :disabled="offset + limit >= total || loading"
                class="px-3.5 py-1.5 rounded-lg border border-border-subtle disabled:opacity-50 text-text-secondary hover:bg-surface-hover transition-colors text-xs font-semibold"
              >
                下一页
              </button>
            </div>
          </div>
        </div>
      </div>
  </AppShell>
</template>
