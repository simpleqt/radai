<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { request } from '@/utils/request';
import { KBDetailAPI } from '@/api/kb';
import { 
  Database, FileText, Network, Cpu, ArrowRight, Clock, Loader2, Activity 
} from 'lucide-vue-next';

const router = useRouter();
const stats = ref({
  total_kbs: 0,
  total_docs: 0,
  total_entities: 0,
  total_relations: 0
});
const recentKBs = ref<any[]>([]);
const recentDocs = ref<any[]>([]);
const loading = ref(true);

const formatDetailedDate = (dateStr: string) => {
  if (!dateStr) return { date: '-', time: '' };
  const date = new Date(dateStr);
  return {
    date: date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).replace(/\//g, '-'),
    time: date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  };
};

const formatFileSize = (value: unknown) => {
  const bytes = Number(value);
  return Number.isFinite(bytes) && bytes >= 0
    ? `${(bytes / 1024 / 1024).toFixed(2)} MB`
    : '-';
};

const kbStatusLabel = (status?: string) => {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'active') return '活跃';
  if (normalized === 'inactive' || normalized === 'disabled') return '停用';
  if (normalized === 'error' || normalized === 'failed') return '异常';
  return status || '未知';
};

const kbStatusDot = (status?: string) => {
  const normalized = String(status || '').toLowerCase();
  if (normalized === 'active') return 'bg-emerald-500 shadow-sm';
  if (normalized === 'error' || normalized === 'failed') return 'bg-red-500 shadow-sm shadow-red-500/50';
  return 'bg-slate-400';
};

const loadDashboardData = async () => {
  loading.value = true;
  try {
    const res = await request('/knowledge_bases');
    const items = res.items || [];
    
    // Stats calculation
    let docs = 0, entities = 0, relations = 0;
    items.forEach((kb: any) => {
      docs += kb.doc_count || 0;
      entities += kb.entity_count || 0;
      relations += kb.relation_count || 0;
    });
    
    stats.value = {
      total_kbs: items.length,
      total_docs: docs,
      total_entities: entities,
      total_relations: relations
    };

    recentKBs.value = [...items].sort((a, b) => 
      new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
    ).slice(0, 5);

    // Fetch real recent documents
    const docsRes = await KBDetailAPI.recentDocuments();
    recentDocs.value = docsRes || [];

  } catch (err) {
    console.error(err);
  } finally {
    loading.value = false;
  }
};

onMounted(() => {
  loadDashboardData();
});
</script>

<template>
  <div class="max-w-7xl mx-auto space-y-8">
    <!-- Premium Header Banner -->
    <div class="relative overflow-hidden rounded-3xl bg-accent p-8 text-white shadow-sm border border-accent/10">
      <!-- Glow background bubbles -->
      <div class="absolute w-64 h-64 rounded-full bg-white/5 blur-3xl -top-10 -right-10 animate-pulse-glow"></div>
      <div class="absolute w-48 h-48 rounded-full bg-accent-soft blur-2xl bottom-0 left-10"></div>
      
      <div class="relative z-10 flex flex-col md:flex-row md:items-center justify-between gap-6">
        <div>
          <span class="inline-flex items-center space-x-1.5 px-3 py-1 rounded-full bg-white/10 text-[10px] font-bold tracking-widest uppercase backdrop-blur-md mb-4 border border-white/10">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
            <span>系统正常运行中</span>
          </span>
          <h2 class="text-3xl font-extrabold tracking-tight">仪表盘概述</h2>
          <p class="mt-2 max-w-xl text-sm leading-relaxed text-white/80">
            欢迎回来！这是成理场地污染水文地质智学系统的后台运行控制台。您可以在此管理场地污染修复的知识要素、文档和图谱关联。
          </p>
        </div>
        <button 
          @click="router.push('/admin/kbs')"
          class="shrink-0 bg-surface text-accent hover:bg-surface-hover px-6 py-3 rounded-2xl font-bold transition-all hover-lift shadow-sm hover:shadow-sm active:scale-95 text-xs"
        >
          新建库任务
        </button>
      </div>
    </div>

    <!-- Stats Grid -->
    <div v-if="loading" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
      <div v-for="i in 4" :key="i" class="h-32 bg-surface border border-border-subtle rounded-2xl animate-pulse"></div>
    </div>
    <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
      <!-- Card 1 -->
      <div class="bg-surface backdrop-blur-md p-6 rounded-2xl border border-border-subtle shadow-sm hover-lift glow-shadow-indigo transition-all duration-300 relative group overflow-hidden">
        <div class="absolute top-0 left-0 right-0 h-1 bg-accent transform scale-x-0 group-hover:scale-x-100 transition-transform duration-300"></div>
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-text-tertiary font-bold text-xs uppercase tracking-widest">知识资源库</h3>
          <div class="p-2.5 bg-accent-soft rounded-xl group-hover:scale-110 group-hover:rotate-6 transition-all duration-300">
            <Database class="w-5 h-5 text-accent" />
          </div>
        </div>
        <div class="text-3xl font-extrabold text-text-primary tracking-tight flex items-baseline">
          {{ stats.total_kbs }}
          <span class="text-xs text-text-tertiary font-normal ml-1.5">UNIT</span>
        </div>
        <p class="text-[10px] text-text-tertiary mt-2 font-medium">包含的所有专业领域知识库</p>
      </div>

      <!-- Card 2 -->
      <div class="bg-surface backdrop-blur-md p-6 rounded-2xl border border-border-subtle shadow-sm hover-lift glow-shadow-blue transition-all duration-300 relative group overflow-hidden">
        <div class="absolute top-0 left-0 right-0 h-1 bg-accent transform scale-x-0 group-hover:scale-x-100 transition-transform duration-300"></div>
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-text-tertiary font-bold text-xs uppercase tracking-widest">解析文档</h3>
          <div class="p-2.5 bg-blue-50 dark:bg-blue-950/40 rounded-xl group-hover:scale-110 group-hover:rotate-6 transition-all duration-300">
            <FileText class="w-5 h-5 text-blue-600 dark:text-blue-400" />
          </div>
        </div>
        <div class="text-3xl font-extrabold text-text-primary tracking-tight flex items-baseline">
          {{ stats.total_docs }}
          <span class="text-xs text-text-tertiary font-normal ml-1.5">DOC</span>
        </div>
        <p class="text-[10px] text-text-tertiary mt-2 font-medium">已向量化索引的学术文献总数</p>
      </div>

      <!-- Card 3 -->
      <div class="bg-surface backdrop-blur-md p-6 rounded-2xl border border-border-subtle shadow-sm hover-lift glow-shadow-emerald transition-all duration-300 relative group overflow-hidden">
        <div class="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-emerald-500 to-emerald-600 transform scale-x-0 group-hover:scale-x-100 transition-transform duration-300"></div>
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-text-tertiary font-bold text-xs uppercase tracking-widest">抽取实体</h3>
          <div class="p-2.5 bg-emerald-50 dark:bg-emerald-950/40 rounded-xl group-hover:scale-110 group-hover:rotate-6 transition-all duration-300">
            <Cpu class="w-5 h-5 text-emerald-600 dark:text-emerald-400" />
          </div>
        </div>
        <div class="text-3xl font-extrabold text-text-primary tracking-tight flex items-baseline">
          {{ stats.total_entities }}
          <span class="text-xs text-text-tertiary font-normal ml-1.5">ENT</span>
        </div>
        <p class="text-[10px] text-text-tertiary mt-2 font-medium">图谱中已分析定位的核心学术实体</p>
      </div>

      <!-- Card 4 -->
      <div class="bg-surface backdrop-blur-md p-6 rounded-2xl border border-border-subtle shadow-sm hover-lift glow-shadow-amber transition-all duration-300 relative group overflow-hidden">
        <div class="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-amber-500 to-amber-600 transform scale-x-0 group-hover:scale-x-100 transition-transform duration-300"></div>
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-text-tertiary font-bold text-xs uppercase tracking-widest">推断关系</h3>
          <div class="p-2.5 bg-amber-50 dark:bg-amber-955/40 rounded-xl group-hover:scale-110 group-hover:rotate-6 transition-all duration-300">
            <Network class="w-5 h-5 text-amber-600 dark:text-amber-450" />
          </div>
        </div>
        <div class="text-3xl font-extrabold text-text-primary tracking-tight flex items-baseline">
          {{ stats.total_relations }}
          <span class="text-xs text-text-tertiary font-normal ml-1.5">REL</span>
        </div>
        <p class="text-[10px] text-text-tertiary mt-2 font-medium">实体间自动演绎关联的知识弧线数</p>
      </div>
    </div>

    <!-- Secondary Grids -->
    <div class="grid grid-cols-1 xl:grid-cols-2 gap-8">
      
      <!-- Recent KBs Card -->
      <div class="bg-surface backdrop-blur-md rounded-3xl border border-border-subtle shadow-sm overflow-hidden min-h-[400px] flex flex-col">
        <div class="px-6 py-5 border-b border-border-subtle flex justify-between items-center bg-white/40 backdrop-blur-sm">
          <div class="flex items-center space-x-2.5">
            <Clock class="w-4.5 h-4.5 text-accent" />
            <h3 class="font-bold text-text-primary text-sm">最近更新的知识库</h3>
          </div>
          <button @click="router.push('/admin/kbs')" class="text-xs text-accent font-bold hover:underline flex items-center space-x-1 group">
            <span>浏览全部</span>
            <ArrowRight class="w-3.5 h-3.5 group-hover:translate-x-0.5 transition-transform" />
          </button>
        </div>
        
        <div class="flex-1 overflow-x-auto">
          <table class="w-full text-sm text-left">
             <thead class="bg-surface-sunken text-text-tertiary text-[10px] uppercase font-bold tracking-widest border-b border-border-subtle">
                <tr>
                   <th class="px-6 py-3.5">库名称</th>
                   <th class="px-6 py-3.5">文档数</th>
                   <th class="px-6 py-3.5">活跃度</th>
                   <th class="px-6 py-3.5 text-right">最后编辑</th>
                </tr>
             </thead>
             <tbody class="divide-y divide-border-subtle">
                <tr 
                  v-for="kb in recentKBs" 
                  :key="kb.kb_id" 
                  @click="router.push(`/admin/kb/${kb.kb_id}`)" 
                  class="hover:bg-surface-hover cursor-pointer transition-colors group"
                >
                   <td class="px-6 py-4">
                     <p class="font-bold text-text-secondary truncate max-w-[180px] group-hover:text-accent transition-colors">
                       {{ kb.kb_name || kb.name || '未命名知识库' }}
                     </p>
                   </td>
                   <td class="px-6 py-4 text-text-secondary font-medium">{{ kb.doc_count }}</td>
                   <td class="px-6 py-4">
                      <div class="flex items-center space-x-1.5">
                         <span class="h-2 w-2 rounded-full" :class="kbStatusDot(kb.status)"></span>
                         <span class="text-xs text-text-secondary">{{ kbStatusLabel(kb.status) }}</span>
                      </div>
                   </td>
                   <td class="px-6 py-4 text-text-tertiary text-xs text-right">
                      <div class="flex flex-col items-end">
                        <span class="font-bold text-text-secondary">{{ formatDetailedDate(kb.updated_at).date }}</span>
                        <span class="text-[10px] opacity-75 font-mono text-text-tertiary">{{ formatDetailedDate(kb.updated_at).time }}</span>
                      </div>
                    </td>
                </tr>
             </tbody>
          </table>
          <div v-if="!loading && recentKBs.length === 0" class="p-20 text-center text-text-tertiary italic text-sm">暂无活动记录</div>
        </div>
      </div>

      <!-- Recent Docs Card -->
      <div class="bg-surface backdrop-blur-md rounded-3xl border border-border-subtle shadow-sm overflow-hidden min-h-[400px] flex flex-col">
        <div class="px-6 py-5 border-b border-border-subtle flex justify-between items-center bg-white/40 backdrop-blur-sm">
          <div class="flex items-center space-x-2.5">
            <FileText class="w-4.5 h-4.5 text-blue-500" />
            <h3 class="font-bold text-text-primary text-sm">最新入库动态</h3>
          </div>
        </div>
        <div class="flex-1 p-6 space-y-5 custom-scrollbar overflow-y-auto max-h-[360px]">
           <div 
             v-for="doc in recentDocs" 
             :key="doc.doc_id" 
             class="flex items-start space-x-4 p-3 bg-surface-sunken border border-border-subtle rounded-2xl hover:border-accent/30 transition-all duration-200"
           >
              <div class="w-10 h-10 rounded-xl bg-surface-sunken flex-shrink-0 flex items-center justify-center border border-border-subtle">
                 <FileText class="w-5 h-5 text-text-tertiary" />
              </div>
              <div class="flex-1 min-w-0">
                 <div class="flex justify-between items-center mb-1.5 gap-2">
                    <p class="text-sm font-bold text-text-secondary truncate">{{ doc.file_name }}</p>
                    <span 
                      class="text-[10px] font-bold px-2 py-0.5 rounded-lg shrink-0"
                      :class="{ 'text-emerald-600 bg-emerald-50 dark:bg-emerald-955/20 border border-emerald-100 dark:border-emerald-900/30': doc.parse_status === 1,
                        'text-amber-600 bg-amber-50 dark:bg-amber-955/20 border border-amber-100 dark:border-amber-900/30': doc.parse_status === 0,
                        'text-rose-650 bg-rose-50 dark:bg-rose-955/20 border border-rose-100 dark:border-rose-900/30': doc.parse_status === 2
                      }"
                    >
                      {{ doc.parse_status === 1 ? '解析完成' : doc.parse_status === 0 ? '待解析' : '解析失败' }}
                    </span>
                 </div>
                 <p class="text-xs text-text-tertiary font-medium">所属知识库: {{ doc.kb_name }}</p>
                 <div class="flex items-center mt-2.5 text-[10px] text-text-tertiary space-x-3">
                    <span class="bg-surface-sunken px-2 py-0.5 rounded text-text-secondary font-medium">{{ formatFileSize(doc.file_size) }}</span>
                    <span class="flex items-center"><Clock class="w-3 h-3 mr-1 text-text-tertiary" />{{ formatDetailedDate(doc.created_at).date }} {{ formatDetailedDate(doc.created_at).time }}</span>
                 </div>
              </div>
           </div>
           <div v-if="!loading && recentDocs.length === 0" class="p-10 text-center text-text-tertiary italic text-sm">暂无入库动态</div>
           <!-- Skeleton for more docs -->
           <div v-if="loading" class="space-y-6">
              <div v-for="i in 2" :key="i" class="flex space-x-4 animate-pulse">
                 <div class="w-10 h-10 bg-surface-sunken rounded-xl"></div>
                 <div class="flex-1 space-y-2">
                    <div class="h-4 bg-surface-sunken rounded w-1/2"></div>
                    <div class="h-3 bg-surface-sunken rounded w-1/4"></div>
                 </div>
              </div>
           </div>
        </div>
      </div>

    </div>
  </div>
</template>
