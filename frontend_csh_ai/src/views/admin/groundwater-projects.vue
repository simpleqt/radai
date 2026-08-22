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
    coordinate_system: project.coordinate_system || '',
  };
};

const saveProject = async () => {
  if (!editingProject.value) return;
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
    errorMessage.value = error?.message || '更新项目失败';
  } finally {
    actionLoading.value = false;
  }
};

const deleteProject = async (project: GroundwaterProject) => {
  if (!confirm(`确定删除项目 "${project.name}" 吗？此操作不可恢复。`)) return;
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
  <section class="max-w-7xl mx-auto space-y-8">
    <!-- Header Banner -->
    <div class="flex flex-col md:flex-row md:items-center justify-between gap-6 pb-6 border-b border-border-subtle">
      <div>
        <h1 class="text-3xl font-extrabold tracking-tight text-text-primary dark:text-white">地下水项目管理</h1>
        <p class="mt-1.5 text-sm text-text-secondary">管理员上传标准项目包，并将项目授权给一个或多个用户进行分析与查询。</p>
      </div>
      
      <div class="flex items-center gap-2.5">
        <button 
          type="button" 
          class="w-10 h-10 inline-flex items-center justify-center rounded-2xl border border-border-subtle bg-surface/70 text-text-secondary hover:text-accent hover:bg-surface-hover transition-all hover-lift shadow-sm" 
          title="刷新" 
          @click="loadData"
        >
          <RefreshCw class="w-4.5 h-4.5" :class="loading ? 'animate-spin' : ''" />
        </button>
        <button 
          type="button" 
          class="h-10 px-5 inline-flex items-center gap-2 rounded-2xl bg-accent text-white text-xs font-bold hover:bg-accent-hover transition-all hover-lift shadow-sm" 
          @click="createOpen = true"
        >
          <Plus class="w-4.5 h-4.5" />
          <span>新建项目</span>
        </button>
      </div>
    </div>

    <!-- Alerts -->
    <transition name="fade">
      <div v-if="errorMessage" class="px-4 py-3 bg-red-50 dark:bg-red-950/20 text-red-650 dark:text-red-400 text-xs font-semibold flex items-center space-x-1.5 rounded-xl border border-red-150 dark:border-red-900/30">
        <span class="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse"></span>
        <span>{{ errorMessage }}</span>
      </div>
    </transition>

    <!-- Loading state -->
    <div v-if="loading" class="py-24 flex flex-col items-center justify-center text-text-secondary">
      <Loader2 class="w-9 h-9 animate-spin text-accent mb-3" />
      <p class="text-sm font-semibold tracking-wide">正在加载地下水项目数据...</p>
    </div>

    <!-- Empty state -->
    <div v-else-if="projects.length === 0" class="py-20 border-2 border-dashed border-border-subtle rounded-3xl bg-white/30 flex flex-col items-center justify-center text-text-tertiary">
      <div class="w-14 h-14 bg-surface-sunken rounded-2xl flex items-center justify-center mb-4">
        <Database class="w-7 h-7 text-text-tertiary" />
      </div>
      <h3 class="text-base font-bold text-text-primary mb-1">暂无地下水项目</h3>
      <p class="text-xs max-w-xs text-center mb-6">目前未创建任何项目。新建一个地下水项目并授权对应科研人员查看数据。</p>
      <button 
        type="button" 
        class="bg-accent-soft text-accent px-5 py-2.5 rounded-xl text-xs font-bold hover:bg-accent-soft transition-all" 
        @click="createOpen = true"
      >
        新建项目
      </button>
    </div>

    <!-- Projects Grid -->
    <div v-else class="grid grid-cols-1 xl:grid-cols-2 gap-6">
      <article 
        v-for="project in projects" 
        :key="project.project_id" 
        class="group bg-surface/80 backdrop-blur-md border border-border-subtle rounded-3xl p-6 shadow-sm hover-lift glow-shadow-indigo transition-all duration-300 relative flex flex-col justify-between"
      >
        <div class="absolute top-0 left-0 right-0 h-1 bg-accent opacity-0 group-hover:opacity-100 transition-opacity rounded-t-3xl"></div>
        
        <div>
          <div class="flex items-start justify-between gap-4 mb-4">
            <div class="min-w-0">
              <h2 class="text-base font-extrabold text-text-primary dark:text-white truncate group-hover:text-accent transition-colors">
                {{ project.name }}
              </h2>
              <p class="text-[10px] text-text-tertiary font-mono mt-1 bg-surface-sunken px-2 py-0.5 rounded border border-border-subtle inline-block">
                {{ project.project_id }}
              </p>
            </div>
            <span class="text-[10px] font-bold px-2 py-1 rounded-lg bg-emerald-50 dark:bg-emerald-950/20 text-emerald-700 dark:text-emerald-400 border border-emerald-100 dark:border-emerald-900/30 shrink-0">
              数据版本: v{{ project.data_version }}
            </span>
          </div>

          <!-- Metas -->
          <div class="grid grid-cols-2 gap-4 py-4 border-t border-b border-border-subtle text-xs text-text-secondary mb-6">
            <div>
              <p class="text-[10px] text-text-tertiary uppercase tracking-widest font-bold">覆盖区域</p>
              <p class="mt-1 font-semibold text-text-secondary">{{ project.region || '未填写' }}</p>
            </div>
            <div>
              <p class="text-[10px] text-text-tertiary uppercase tracking-widest font-bold">授权科研人员</p>
              <p class="mt-1 font-semibold text-text-secondary">{{ project.authorized_user_count || 0 }} 人</p>
            </div>
          </div>
        </div>

        <!-- Action tools -->
        <div class="flex flex-wrap items-center gap-2 pt-2">
          <button 
            type="button" 
            class="h-8 px-3 inline-flex items-center gap-1.5 rounded-xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover hover:text-accent disabled:opacity-50 transition-colors shadow-sm" 
            :disabled="project.read_only || actionLoading" 
            @click="chooseUpload(project.project_id)"
          >
            <Upload class="w-3.5 h-3.5" />
            <span>上传 ZIP 包</span>
          </button>
          
          <button 
            type="button" 
            class="h-8 px-3 inline-flex items-center gap-1.5 rounded-xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover hover:text-accent disabled:opacity-50 transition-colors shadow-sm" 
            :disabled="project.read_only || actionLoading" 
            @click="openEdit(project)"
          >
            <Edit3 class="w-3.5 h-3.5" />
            <span>编辑项目</span>
          </button>
          
          <button 
            type="button" 
            class="h-8 px-3 inline-flex items-center gap-1.5 rounded-xl bg-accent hover:bg-accent-hover text-accent-fg text-xs font-bold disabled:opacity-50 transition-all hover-lift shadow-sm" 
            :disabled="actionLoading" 
            @click="openBindings(project)"
          >
            <UserRoundCog class="w-3.5 h-3.5" />
            <span>授权范围</span>
          </button>
          
          <button 
            type="button" 
            class="h-8 w-8 inline-flex items-center justify-center rounded-xl border border-rose-100 hover:border-rose-200 text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-950/20 disabled:opacity-50 transition-colors shrink-0 shadow-sm ml-auto" 
            title="删除项目" 
            :disabled="project.read_only || actionLoading" 
            @click="deleteProject(project)"
          >
            <Trash2 class="w-3.5 h-3.5" />
          </button>
        </div>
      </article>
    </div>
    <input ref="fileInput" type="file" accept=".zip,application/zip" class="hidden" @change="uploadPackage" />

    <!-- Create project modal dialog -->
    <div v-if="createOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="createOpen = false"></div>
      <form class="relative w-full max-w-lg overflow-hidden rounded-3xl border border-white/55 bg-surface/90 shadow-2xl backdrop-blur-2xl animate-in fade-in zoom-in duration-200 dark:border-white/10 dark:bg-surface/95" role="dialog" aria-modal="true" aria-label="新建地下水项目" @submit.prevent="createProject">
        <div class="h-14 px-6 border-b border-border-subtle flex items-center justify-between">
          <h2 class="font-extrabold text-text-primary">新建地下水项目</h2>
          <button type="button" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition" title="关闭" @click="createOpen = false"><X class="w-4.5 h-4.5" /></button>
        </div>
        <div class="p-6 grid grid-cols-1 md:grid-cols-2 gap-4.5">
          <label class="md:col-span-2 text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            项目名称
            <input v-model="newProject.name" required class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            项目编号 (ID)
            <input v-model="newProject.project_id" placeholder="可留空自动生成" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium font-mono" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            项目区域 (Region)
            <input v-model="newProject.region" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
          <label class="md:col-span-2 text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            地理坐标系
            <input v-model="newProject.coordinate_system" placeholder="例如 CGCS2000" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
        </div>
        <div class="px-6 py-4.5 border-t border-border-subtle flex justify-end gap-2.5">
          <button type="button" class="h-9 px-4 rounded-xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover" @click="createOpen = false">取消</button>
          <button type="submit" :disabled="actionLoading" class="h-9 px-5 rounded-xl bg-accent text-white text-xs font-bold hover:bg-accent-hover shadow-sm transition disabled:opacity-50">立即创建</button>
        </div>
      </form>
    </div>

    <!-- Edit project modal dialog -->
    <div v-if="editingProject" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="editingProject = null"></div>
      <form class="relative w-full max-w-lg overflow-hidden rounded-3xl border border-white/55 bg-surface/90 shadow-2xl backdrop-blur-2xl animate-in fade-in zoom-in duration-200 dark:border-white/10 dark:bg-surface/95" role="dialog" aria-modal="true" aria-label="编辑地下水项目" @submit.prevent="saveProject">
        <div class="h-14 px-6 border-b border-border-subtle flex items-center justify-between">
          <div>
            <h2 class="font-extrabold text-text-primary">编辑项目基本信息</h2>
            <p class="text-[10px] text-text-tertiary font-mono mt-0.5">{{ editingProject.project_id }}</p>
          </div>
          <button type="button" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition" title="关闭" @click="editingProject = null"><X class="w-4.5 h-4.5" /></button>
        </div>
        <div class="p-6 grid grid-cols-1 md:grid-cols-2 gap-4.5">
          <label class="md:col-span-2 text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            项目名称
            <input v-model="editForm.name" required class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            项目区域 (Region)
            <input v-model="editForm.region" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
          <label class="text-xs font-bold text-text-tertiary uppercase tracking-widest ml-1">
            地理坐标系
            <input v-model="editForm.coordinate_system" placeholder="留空表示不修改或待确认" class="mt-1.5 w-full h-10 px-3 bg-surface-sunken border border-border-subtle rounded-xl outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent text-sm text-text-primary font-medium" />
          </label>
        </div>
        <div class="px-6 py-4.5 border-t border-border-subtle flex justify-end gap-2.5">
          <button type="button" class="h-9 px-4 rounded-xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover" @click="editingProject = null">取消</button>
          <button type="submit" :disabled="actionLoading" class="h-9 px-5 rounded-xl bg-accent text-white text-xs font-bold hover:bg-accent-hover shadow-sm transition disabled:opacity-50">保存修改</button>
        </div>
      </form>
    </div>

    <!-- Bindings modal dialog -->
    <div v-if="bindingProject" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-slate-950/20 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="bindingProject = null"></div>
      <div class="relative w-full max-w-xl overflow-hidden rounded-3xl border border-white/55 bg-surface/90 shadow-2xl backdrop-blur-2xl animate-in fade-in zoom-in duration-200 dark:border-white/10 dark:bg-surface/95" role="dialog" aria-modal="true" aria-label="项目授权范围">
        <div class="h-14 px-6 border-b border-border-subtle flex items-center justify-between">
          <div>
            <h2 class="font-extrabold text-text-primary">项目授权范围管辖</h2>
            <p class="text-[10px] text-text-tertiary font-mono mt-0.5">{{ bindingProject.name }} ({{ bindingProject.project_id }})</p>
          </div>
          <button type="button" class="p-1.5 rounded-lg text-text-tertiary hover:bg-surface-hover transition" title="关闭" @click="bindingProject = null"><X class="w-4.5 h-4.5" /></button>
        </div>
        <div class="p-6 max-h-[350px] overflow-y-auto custom-scrollbar space-y-2">
          <p class="text-xs text-text-tertiary mb-4 font-medium">请勾选允许访问此地下水项目的用户，管理员默认具备全局读写权限且不可取消授权。</p>
          
          <div 
            v-for="user in assignableUsers" 
            :key="user.user_id" 
            @click="toggleUser(user.user_id)"
            class="flex items-center justify-between p-3 bg-surface-sunken border rounded-2xl cursor-pointer hover:bg-accent-soft hover:border-accent/30 transition-all"
            :class="selectedUserIds.includes(user.user_id) ? 'border-accent/50 bg-accent-soft' : 'border-border-subtle'"
          >
            <div>
              <p class="text-sm font-bold text-text-secondary">{{ user.username }}</p>
              <p class="text-[10px] text-text-tertiary mt-0.5 font-mono">{{ user.email || '暂无邮箱' }}</p>
            </div>
            
            <div 
              class="w-5 h-5 rounded-full flex items-center justify-center border transition-all"
              :class="selectedUserIds.includes(user.user_id) ? 'bg-accent border-accent text-white shadow-sm' : 'border-border-subtle'"
            >
              <Check v-if="selectedUserIds.includes(user.user_id)" class="w-3 h-3 stroke-[3]" />
            </div>
          </div>
          <div v-if="assignableUsers.length === 0" class="p-10 text-center text-text-tertiary italic text-xs">无可授权的用户角色</div>
        </div>
        <div class="px-6 py-4.5 border-t border-border-subtle flex justify-end gap-2.5">
          <button type="button" class="h-9 px-4 rounded-xl border border-border-subtle text-xs font-bold text-text-secondary hover:bg-surface-hover" @click="bindingProject = null">取消</button>
          <button type="button" :disabled="actionLoading" class="h-9 px-5 rounded-xl bg-accent text-white text-xs font-bold hover:bg-accent-hover shadow-sm transition" @click="saveBindings">保存权限配置</button>
        </div>
      </div>
    </div>
  </section>
</template>
