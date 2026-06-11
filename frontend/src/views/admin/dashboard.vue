<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { request } from '@/utils/request';
import { KBDetailAPI } from '@/api/kb';
import { Database, FileText, Share2, ArrowRight, Clock, Loader2 } from 'lucide-vue-next';

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

    // Sort by updated_at or similar for "recent"
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
  <div class="max-w-7xl mx-auto space-y-10">
    <!-- Header -->
    <div class="flex flex-col md:flex-row md:items-end justify-between gap-4">
      <div>
        <h2 class="text-3xl font-extrabold tracking-tight text-slate-800 dark:text-slate-100">仪表盘概述</h2>
        <p class="text-slate-500 dark:text-slate-400 mt-2">欢迎回来，这是核电子学知识图谱系统的实时运行监控。</p>
      </div>
      <button 
        @click="router.push('/admin/kbs')"
        class="bg-indigo-600 hover:bg-indigo-700 text-white px-6 py-2.5 rounded-xl font-bold shadow-lg shadow-indigo-500/20 transition-all active:scale-95"
      >
        新建库任务
      </button>
    </div>

    <!-- Stats Grid -->
    <div v-if="loading" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
      <div v-for="i in 4" :key="i" class="h-32 bg-white dark:bg-slate-800 rounded-2xl animate-pulse"></div>
    </div>
    <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
      <div class="bg-white dark:bg-slate-800 p-6 rounded-2xl border border-slate-200 dark:border-slate-700/50 shadow-sm group hover:shadow-md transition-all">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-slate-400 dark:text-slate-500 font-bold text-xs uppercase tracking-widest">知识资源库</h3>
          <div class="p-2 bg-indigo-50 dark:bg-indigo-900/30 rounded-lg group-hover:scale-110 transition-transform">
            <Database class="w-5 h-5 text-indigo-600 dark:text-indigo-400" />
          </div>
        </div>
        <div class="text-3xl font-bold text-slate-800 dark:text-slate-100">{{ stats.total_kbs }} <span class="text-xs text-slate-400 font-normal">UNIT</span></div>
      </div>

      <div class="bg-white dark:bg-slate-800 p-6 rounded-2xl border border-slate-200 dark:border-slate-700/50 shadow-sm group hover:shadow-md transition-all">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-slate-400 dark:text-slate-500 font-bold text-xs uppercase tracking-widest">解析文档</h3>
          <div class="p-2 bg-blue-50 dark:bg-blue-900/30 rounded-lg group-hover:scale-110 transition-transform">
            <FileText class="w-5 h-5 text-blue-600 dark:text-blue-400" />
          </div>
        </div>
        <div class="text-3xl font-bold text-slate-800 dark:text-slate-100">{{ stats.total_docs }} <span class="text-xs text-slate-400 font-normal">DOC</span></div>
      </div>

      <div class="bg-white dark:bg-slate-800 p-6 rounded-2xl border border-slate-200 dark:border-slate-700/50 shadow-sm group hover:shadow-md transition-all">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-slate-400 dark:text-slate-500 font-bold text-xs uppercase tracking-widest">抽取实体</h3>
          <div class="p-2 bg-emerald-50 dark:bg-emerald-900/30 rounded-lg group-hover:scale-110 transition-transform">
            <Share2 class="w-5 h-5 text-emerald-600 dark:text-emerald-400" />
          </div>
        </div>
        <div class="text-3xl font-bold text-slate-800 dark:text-slate-100">{{ stats.total_entities }} <span class="text-xs text-slate-400 font-normal">ENT</span></div>
      </div>

      <div class="bg-white dark:bg-slate-800 p-6 rounded-2xl border border-slate-200 dark:border-slate-700/50 shadow-sm group hover:shadow-md transition-all">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-slate-400 dark:text-slate-500 font-bold text-xs uppercase tracking-widest">推断关系</h3>
          <div class="p-2 bg-amber-50 dark:bg-amber-900/30 rounded-lg group-hover:scale-110 transition-transform">
            <Share2 class="w-5 h-5 text-amber-600 dark:text-amber-400" />
          </div>
        </div>
        <div class="text-3xl font-bold text-slate-800 dark:text-slate-100">{{ stats.total_relations }} <span class="text-xs text-slate-400 font-normal">REL</span></div>
      </div>
    </div>

    <!-- Secondary Grids -->
    <div class="grid grid-cols-1 xl:grid-cols-2 gap-8">
      
      <!-- Recent KBs Card -->
      <div class="bg-white dark:bg-slate-800 rounded-3xl border border-slate-200 dark:border-slate-700/50 shadow-sm overflow-hidden min-h-[400px] flex flex-col">
        <div class="px-6 py-5 border-b border-slate-100 dark:border-slate-700/50 flex justify-between items-center">
          <div class="flex items-center space-x-2">
            <Clock class="w-4 h-4 text-indigo-500" />
            <h3 class="font-bold text-slate-800 dark:text-slate-100">最近更新的知识库</h3>
          </div>
          <button @click="router.push('/admin/kbs')" class="text-xs text-indigo-600 font-bold hover:underline flex items-center space-x-1">
            <span>浏览全部</span>
            <ArrowRight class="w-3 h-3" />
          </button>
        </div>
        
        <div class="flex-1 overflow-x-auto">
          <table class="w-full text-sm text-left">
             <thead class="bg-slate-50/50 dark:bg-slate-900/40 text-slate-400 text-[10px] uppercase font-bold tracking-widest">
                <tr>
                   <th class="px-6 py-3">库名称</th>
                   <th class="px-6 py-3">文档数</th>
                   <th class="px-6 py-3">活跃度</th>
                   <th class="px-6 py-3">最后编辑</th>
                </tr>
             </thead>
             <tbody class="divide-y divide-slate-100 dark:divide-slate-700/50">
                <tr v-for="kb in recentKBs" :key="kb.kb_id" @click="router.push(`/admin/kb/${kb.kb_id}`)" class="hover:bg-slate-50 dark:hover:bg-slate-700/30 cursor-pointer transition-colors group">
                   <td class="px-6 py-4">
                     <p class="font-bold text-slate-700 dark:text-slate-200 truncate max-w-[150px] group-hover:text-indigo-600 transition-colors">{{ kb.kb_name || kb.name || '未命名知识库' }}</p>
                   </td>
                   <td class="px-6 py-4 text-slate-500">{{ kb.doc_count }}</td>
                   <td class="px-6 py-4">
                      <div class="flex items-center space-x-1">
                         <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
                         <span class="text-xs">活跃</span>
                      </div>
                   </td>
                   <td class="px-6 py-4 text-slate-400 text-xs text-right">
                      <div class="flex flex-col items-end">
                        <span class="font-medium text-slate-500 dark:text-slate-400">{{ formatDetailedDate(kb.updated_at).date }}</span>
                        <span class="text-[10px] opacity-60 font-mono">{{ formatDetailedDate(kb.updated_at).time }}</span>
                      </div>
                    </td>
                </tr>
             </tbody>
          </table>
          <div v-if="!loading && recentKBs.length === 0" class="p-20 text-center text-slate-400 italic">暂无活动记录</div>
        </div>
      </div>

      <!-- Recent Docs Card -->
      <div class="bg-white dark:bg-slate-800 rounded-3xl border border-slate-200 dark:border-slate-700/50 shadow-sm overflow-hidden min-h-[400px] flex flex-col">
        <div class="px-6 py-5 border-b border-slate-100 dark:border-slate-700/50 flex justify-between items-center">
          <div class="flex items-center space-x-2">
            <FileText class="w-4 h-4 text-blue-500" />
            <h3 class="font-bold text-slate-800 dark:text-slate-100">最新入库动态</h3>
          </div>
        </div>
        <div class="flex-1 p-6 space-y-6">
           <div v-for="doc in recentDocs" :key="doc.doc_id" class="flex items-start space-x-4">
              <div class="w-10 h-10 rounded-xl bg-slate-100 dark:bg-slate-700 flex-shrink-0 flex items-center justify-center">
                 <FileText class="w-5 h-5 text-slate-400" />
              </div>
              <div class="flex-1 min-w-0">
                 <div class="flex justify-between items-center mb-1">
                    <p class="text-sm font-bold text-slate-700 dark:text-slate-200 truncate">{{ doc.file_name }}</p>
                    <span 
                      class="text-[10px] font-bold px-2 py-0.5 rounded"
                      :class="{
                        'text-emerald-500 bg-emerald-50 dark:bg-emerald-900/20': doc.parse_status === 1,
                        'text-amber-500 bg-amber-50 dark:bg-amber-900/20': doc.parse_status === 0,
                        'text-red-500 bg-red-50 dark:bg-red-900/20': doc.parse_status === 2
                      }"
                    >
                      {{ doc.parse_status === 1 ? '解析完成' : doc.parse_status === 0 ? '待解析' : '解析失败' }}
                    </span>
                 </div>
                 <p class="text-xs text-slate-400">所属知识库: {{ doc.kb_name }}</p>
                 <div class="flex items-center mt-2 text-[10px] text-slate-400 space-x-3">
                    <span>{{ (doc.file_size / 1024 / 1024).toFixed(2) }} MB</span>
                    <span>{{ formatDetailedDate(doc.created_at).date }} {{ formatDetailedDate(doc.created_at).time }}</span>
                 </div>
              </div>
           </div>
           <div v-if="!loading && recentDocs.length === 0" class="p-10 text-center text-slate-400 italic text-sm">暂无入库动态</div>
           <!-- Skeleton for more docs -->
           <div v-if="loading" class="space-y-6">
              <div v-for="i in 2" :key="i" class="flex space-x-4 animate-pulse">
                 <div class="w-10 h-10 bg-slate-100 dark:bg-slate-700 rounded-xl"></div>
                 <div class="flex-1 space-y-2">
                    <div class="h-4 bg-slate-100 dark:bg-slate-700 rounded w-1/2"></div>
                    <div class="h-3 bg-slate-100 dark:bg-slate-700 rounded w-1/4"></div>
                 </div>
              </div>
           </div>
        </div>
      </div>

    </div>
  </div>
</template>
