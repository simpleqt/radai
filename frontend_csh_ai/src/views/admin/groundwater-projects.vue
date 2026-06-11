<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { Check, Database, Edit3, Loader2, Plus, RefreshCw, Trash2, Upload, UserRoundCog, X } from 'lucide-vue-next';
import { GroundwaterAPI, type GroundwaterProject, type GroundwaterUser } from '@/api/groundwater';

const projects = ref<GroundwaterProject[]>([]);
const users = ref<GroundwaterUser[]>([]);
const loading = ref(false);
const actionLoading = ref(false);
const errorMessage = ref('');
const createOpen = ref(false);
const bindingProject = ref<GroundwaterProject | null>(null);
const editingProject = ref<GroundwaterProject | null>(null);
const selectedUserIds = ref<string[]>([]);
const uploadProjectId = ref('');
const fileInput = ref<HTMLInputElement | null>(null);
const newProject = ref({ name: '', project_id: '', region: '', coordinate_system: '' });
const editForm = ref({ name: '', region: '', coordinate_system: '' });
const assignableUsers = computed(() => users.value.filter((user) => user.role !== 'admin'));

const loadData = async () => {
  loading.value = true;
  errorMessage.value = '';
  try {
    [projects.value, users.value] = await Promise.all([
      GroundwaterAPI.listAdminProjects(),
      GroundwaterAPI.listUsers(),
    ]);
  } catch (error: any) {
    errorMessage.value = error?.message || '加载失败';
  } finally {
    loading.value = false;
  }
};

const createProject = async () => {
  if (!newProject.value.name.trim()) return;
  actionLoading.value = true;
  try {
    await GroundwaterAPI.createProject({
      name: newProject.value.name.trim(),
      project_id: newProject.value.project_id.trim() || undefined,
      region: newProject.value.region.trim(),
      coordinate_system: newProject.value.coordinate_system.trim(),
    });
    createOpen.value = false;
    newProject.value = { name: '', project_id: '', region: '', coordinate_system: '' };
    await loadData();
  } catch (error: any) {
    errorMessage.value = error?.message || '创建项目失败';
  } finally {
    actionLoading.value = false;
  }
};

const chooseUpload = (projectId: string) => {
  uploadProjectId.value = projectId;
  fileInput.value?.click();
};

const uploadPackage = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file || !uploadProjectId.value) return;
  actionLoading.value = true;
  try {
    await GroundwaterAPI.uploadPackage(uploadProjectId.value, file);
    await loadData();
  } catch (error: any) {
    errorMessage.value = error?.message || '上传项目包失败';
  } finally {
    input.value = '';
    uploadProjectId.value = '';
    actionLoading.value = false;
  }
};

const openBindings = async (project: GroundwaterProject) => {
  actionLoading.value = true;
  try {
    const bindings = await GroundwaterAPI.listBindings(project.project_id);
    selectedUserIds.value = bindings.map((item: { user_id: string }) => item.user_id);
    bindingProject.value = project;
  } catch (error: any) {
    errorMessage.value = error?.message || '读取授权失败';
  } finally {
    actionLoading.value = false;
  }
};

const toggleUser = (userId: string) => {
  selectedUserIds.value = selectedUserIds.value.includes(userId)
    ? selectedUserIds.value.filter((value) => value !== userId)
    : [...selectedUserIds.value, userId];
};

const saveBindings = async () => {
  if (!bindingProject.value) return;
  actionLoading.value = true;
  try {
    await GroundwaterAPI.replaceBindings(bindingProject.value.project_id, selectedUserIds.value);
    bindingProject.value = null;
    await loadData();
  } catch (error: any) {
    errorMessage.value = error?.message || '保存授权失败';
  } finally {
    actionLoading.value = false;
  }
};

const openEdit = (project: GroundwaterProject) => {
  editingProject.value = project;
  editForm.value = {
    name: project.name,
    region: project.region || '',
    coordinate_system: '',
  };
};

const saveProject = async () => {
  if (!editingProject.value || !editForm.value.name.trim()) return;
  actionLoading.value = true;
  try {
    await GroundwaterAPI.updateProject(editingProject.value.project_id, {
      name: editForm.value.name.trim(),
      region: editForm.value.region.trim(),
      coordinate_system: editForm.value.coordinate_system.trim(),
    });
    editingProject.value = null;
    await loadData();
  } catch (error: any) {
    errorMessage.value = error?.message || '保存项目失败';
  } finally {
    actionLoading.value = false;
  }
};

const deleteProject = async (project: GroundwaterProject) => {
  if (project.read_only) return;
  const ok = window.confirm(`确定删除地下水项目“${project.name}”？删除后普通用户将无法再选择该项目。`);
  if (!ok) return;
  actionLoading.value = true;
  try {
    await GroundwaterAPI.deleteProject(project.project_id);
    await loadData();
  } catch (error: any) {
    errorMessage.value = error?.message || '删除项目失败';
  } finally {
    actionLoading.value = false;
  }
};

onMounted(loadData);
</script>

<template>
  <section class="max-w-7xl mx-auto">
    <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-8">
      <div>
        <h1 class="text-2xl font-bold text-slate-900 dark:text-white">地下水项目管理</h1>
        <p class="mt-1 text-sm text-slate-500">管理员上传标准项目包，并将项目授权给一个或多个用户。</p>
      </div>
      <div class="flex items-center gap-2">
        <button type="button" class="w-9 h-9 inline-flex items-center justify-center rounded-md border border-slate-300 bg-white text-slate-600 hover:bg-slate-100" title="刷新" @click="loadData">
          <RefreshCw class="w-4 h-4" />
        </button>
        <button type="button" class="h-9 px-4 inline-flex items-center gap-2 rounded-md bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700" @click="createOpen = true">
          <Plus class="w-4 h-4" />新建项目
        </button>
      </div>
    </div>

    <div v-if="errorMessage" class="mb-4 px-4 py-3 border border-rose-200 bg-rose-50 text-rose-700 text-sm rounded-md">{{ errorMessage }}</div>
    <div v-if="loading" class="h-56 flex items-center justify-center text-slate-500">
      <Loader2 class="w-6 h-6 animate-spin mr-2" />正在加载项目
    </div>
    <div v-else-if="projects.length === 0" class="h-56 border border-dashed border-slate-300 flex flex-col items-center justify-center text-slate-500">
      <Database class="w-10 h-10 mb-3 text-slate-300" />暂无地下水项目
    </div>
    <div v-else class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <article v-for="project in projects" :key="project.project_id" class="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-md p-5">
        <div class="flex items-start justify-between gap-4">
          <div class="min-w-0">
            <h2 class="font-bold text-slate-900 dark:text-white truncate">{{ project.name }}</h2>
            <p class="text-xs text-slate-400 mt-1 font-mono">{{ project.project_id }}</p>
          </div>
          <span class="text-xs px-2 py-1 rounded bg-emerald-50 text-emerald-700 shrink-0">v{{ project.data_version }}</span>
        </div>
        <div class="mt-5 grid grid-cols-2 gap-4 text-sm">
          <div><p class="text-xs text-slate-400">区域</p><p class="mt-1 text-slate-700 dark:text-slate-200">{{ project.region || '未填写' }}</p></div>
          <div><p class="text-xs text-slate-400">授权用户</p><p class="mt-1 text-slate-700 dark:text-slate-200">{{ project.authorized_user_count || 0 }} 人</p></div>
        </div>
        <div class="mt-5 pt-4 border-t border-slate-100 dark:border-slate-700 flex items-center gap-2">
          <button type="button" class="h-8 px-3 inline-flex items-center gap-2 rounded-md border border-slate-300 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50" :disabled="project.read_only || actionLoading" @click="chooseUpload(project.project_id)">
            <Upload class="w-4 h-4" />上传 ZIP
          </button>
          <button type="button" class="h-8 px-3 inline-flex items-center gap-2 rounded-md border border-slate-300 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50" :disabled="project.read_only || actionLoading" @click="openEdit(project)">
            <Edit3 class="w-4 h-4" />编辑
          </button>
          <button type="button" class="h-8 px-3 inline-flex items-center gap-2 rounded-md bg-slate-900 text-white text-sm hover:bg-slate-700 disabled:opacity-50" :disabled="actionLoading" @click="openBindings(project)">
            <UserRoundCog class="w-4 h-4" />用户授权
          </button>
          <button type="button" class="h-8 w-8 inline-flex items-center justify-center rounded-md border border-rose-200 text-rose-600 hover:bg-rose-50 disabled:opacity-50" title="删除项目" :disabled="project.read_only || actionLoading" @click="deleteProject(project)">
            <Trash2 class="w-4 h-4" />
          </button>
        </div>
      </article>
    </div>
    <input ref="fileInput" type="file" accept=".zip,application/zip" class="hidden" @change="uploadPackage" />

    <div v-if="createOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/50">
      <form class="w-full max-w-lg bg-white dark:bg-slate-800 rounded-md shadow-xl" @submit.prevent="createProject">
        <div class="h-14 px-5 border-b border-slate-200 flex items-center justify-between">
          <h2 class="font-bold">新建地下水项目</h2>
          <button type="button" class="p-1 text-slate-400" title="关闭" @click="createOpen = false"><X class="w-5 h-5" /></button>
        </div>
        <div class="p-5 grid grid-cols-1 md:grid-cols-2 gap-4">
          <label class="md:col-span-2 text-sm text-slate-600">项目名称<input v-model="newProject.name" required class="mt-1 w-full h-10 px-3 border border-slate-300 rounded-md outline-none focus:ring-2 focus:ring-indigo-500" /></label>
          <label class="text-sm text-slate-600">项目编号<input v-model="newProject.project_id" placeholder="可留空自动生成" class="mt-1 w-full h-10 px-3 border border-slate-300 rounded-md outline-none focus:ring-2 focus:ring-indigo-500" /></label>
          <label class="text-sm text-slate-600">项目区域<input v-model="newProject.region" class="mt-1 w-full h-10 px-3 border border-slate-300 rounded-md outline-none focus:ring-2 focus:ring-indigo-500" /></label>
          <label class="md:col-span-2 text-sm text-slate-600">坐标系<input v-model="newProject.coordinate_system" placeholder="例如 CGCS2000" class="mt-1 w-full h-10 px-3 border border-slate-300 rounded-md outline-none focus:ring-2 focus:ring-indigo-500" /></label>
        </div>
        <div class="px-5 py-4 border-t border-slate-200 flex justify-end gap-2">
          <button type="button" class="h-9 px-4 rounded-md border border-slate-300 text-sm" @click="createOpen = false">取消</button>
          <button type="submit" :disabled="actionLoading" class="h-9 px-4 rounded-md bg-indigo-600 text-white text-sm font-semibold disabled:opacity-50">创建</button>
        </div>
      </form>
    </div>

    <div v-if="editingProject" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/50">
      <form class="w-full max-w-lg bg-white dark:bg-slate-800 rounded-md shadow-xl" @submit.prevent="saveProject">
        <div class="h-14 px-5 border-b border-slate-200 flex items-center justify-between">
          <div><h2 class="font-bold">编辑地下水项目</h2><p class="text-xs text-slate-400">{{ editingProject.project_id }}</p></div>
          <button type="button" class="p-1 text-slate-400" title="关闭" @click="editingProject = null"><X class="w-5 h-5" /></button>
        </div>
        <div class="p-5 grid grid-cols-1 md:grid-cols-2 gap-4">
          <label class="md:col-span-2 text-sm text-slate-600">项目名称<input v-model="editForm.name" required class="mt-1 w-full h-10 px-3 border border-slate-300 rounded-md outline-none focus:ring-2 focus:ring-indigo-500" /></label>
          <label class="text-sm text-slate-600">项目区域<input v-model="editForm.region" class="mt-1 w-full h-10 px-3 border border-slate-300 rounded-md outline-none focus:ring-2 focus:ring-indigo-500" /></label>
          <label class="text-sm text-slate-600">坐标系<input v-model="editForm.coordinate_system" placeholder="留空表示不修改或待确认" class="mt-1 w-full h-10 px-3 border border-slate-300 rounded-md outline-none focus:ring-2 focus:ring-indigo-500" /></label>
        </div>
        <div class="px-5 py-4 border-t border-slate-200 flex justify-end gap-2">
          <button type="button" class="h-9 px-4 rounded-md border border-slate-300 text-sm" @click="editingProject = null">取消</button>
          <button type="submit" :disabled="actionLoading" class="h-9 px-4 rounded-md bg-indigo-600 text-white text-sm font-semibold disabled:opacity-50">保存</button>
        </div>
      </form>
    </div>

    <div v-if="bindingProject" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/50">
      <div class="w-full max-w-xl bg-white dark:bg-slate-800 rounded-md shadow-xl">
        <div class="h-14 px-5 border-b border-slate-200 flex items-center justify-between">
          <div><h2 class="font-bold">用户授权</h2><p class="text-xs text-slate-400">{{ bindingProject.name }}</p></div>
          <button type="button" class="p-1 text-slate-400" title="关闭" @click="bindingProject = null"><X class="w-5 h-5" /></button>
        </div>
        <div class="p-5 max-h-[55vh] overflow-y-auto space-y-2">
          <button v-for="user in assignableUsers" :key="user.user_id" type="button" class="w-full min-h-12 px-3 border rounded-md flex items-center gap-3 text-left" :class="selectedUserIds.includes(user.user_id) ? 'border-indigo-400 bg-indigo-50' : 'border-slate-200 hover:bg-slate-50'" @click="toggleUser(user.user_id)">
            <span class="w-5 h-5 rounded border flex items-center justify-center shrink-0" :class="selectedUserIds.includes(user.user_id) ? 'bg-indigo-600 border-indigo-600 text-white' : 'border-slate-300'">
              <Check v-if="selectedUserIds.includes(user.user_id)" class="w-3.5 h-3.5" />
            </span>
            <span class="min-w-0"><span class="block text-sm font-semibold text-slate-800">{{ user.username }}</span><span class="block text-xs text-slate-400 truncate">{{ user.email || user.user_id }}</span></span>
          </button>
          <p v-if="assignableUsers.length === 0" class="py-8 text-center text-sm text-slate-500">暂无可授权的普通用户</p>
        </div>
        <div class="px-5 py-4 border-t border-slate-200 flex justify-end gap-2">
          <button type="button" class="h-9 px-4 rounded-md border border-slate-300 text-sm" @click="bindingProject = null">取消</button>
          <button type="button" :disabled="actionLoading" class="h-9 px-4 rounded-md bg-indigo-600 text-white text-sm font-semibold disabled:opacity-50" @click="saveBindings">保存授权</button>
        </div>
      </div>
    </div>
  </section>
</template>
