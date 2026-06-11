<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { KBDetailAPI, KnowledgeBase } from '@/api/kb';
import { Database, Plus, Search, Trash2, ArrowRight, Loader2, ChevronRight } from 'lucide-vue-next';

const router = useRouter();
const kbs = ref<KnowledgeBase[]>([]);
const loading = ref(true);
const searchQuery = ref('');
const isCreateModalOpen = ref(false);
const newKB = ref({ kb_name: '', kb_code: '', subject: '', description: '' });

const loadKBs = async () => {
  loading.value = true;
  try {
    const res = await KBDetailAPI.list();
    kbs.value = res.items || [];
  } catch (e) {
    console.error(e);
  } finally {
    loading.value = false;
  }
};

const handleCreate = async () => {
  if (!newKB.value.kb_name || !newKB.value.kb_code) {
    alert('需填写知识库名称和编码');
    return;
  }
  try {
    await KBDetailAPI.create(newKB.value);
    isCreateModalOpen.value = false;
    newKB.value = { kb_name: '', kb_code: '', subject: '', description: '' };
    loadKBs();
  } catch (e) {
    alert('创建失败');
  }
};

const handleDelete = async (id: string) => {
  if (!confirm('确定要删除这个知识库吗？此操作不可逆。')) return;
  try {
    await KBDetailAPI.delete(id);
    loadKBs();
  } catch (e) {
    alert('删除失败');
  }
};

onMounted(loadKBs);
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900 flex flex-col">
    <!-- View Title & Actions -->
    <div class="flex flex-col md:flex-row md:items-center justify-between gap-6 mb-10">
      <div>
        <h2 class="text-3xl font-extrabold tracking-tight text-slate-800 dark:text-slate-100">知识库列表</h2>
        <p class="text-slate-500 dark:text-slate-400 mt-1">管理、查询与配置您的专业领域知识资产。</p>
      </div>
      <div class="flex items-center space-x-3">
        <div class="relative">
          <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input 
            v-model="searchQuery"
            type="text" 
            placeholder="搜索知识库..." 
            class="pl-10 pr-4 py-2 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl text-sm focus:ring-2 focus:ring-indigo-500 outline-none w-64 transition-all shadow-sm"
          />
        </div>
        <button 
          @click="isCreateModalOpen = true"
          class="bg-indigo-600 hover:bg-indigo-700 text-white px-5 py-2.5 rounded-xl text-sm font-bold flex items-center space-x-2 transition-all shadow-lg shadow-indigo-500/20 active:scale-95"
        >
          <Plus class="w-4 h-4" />
          <span>新建知识库</span>
        </button>
      </div>
    </div>
      <div v-if="loading" class="flex flex-col items-center justify-center py-20 text-slate-500">
        <Loader2 class="w-10 h-10 animate-spin mb-4 text-indigo-500" />
        <p>正在加载列表...</p>
      </div>

      <div v-else-if="kbs.length === 0" class="flex flex-col items-center justify-center py-20 text-slate-500 border-2 border-dashed border-slate-200 rounded-2xl">
        <Database class="w-16 h-16 mb-4 text-slate-300" />
        <h3 class="text-lg font-medium text-slate-700 dark:text-slate-200">暂无知识库</h3>
        <p class="mb-6">开始创建您的第一个核电子学知识库吧</p>
        <button @click="isCreateModalOpen = true" class="text-indigo-600 font-medium hover:underline">点击新建</button>
      </div>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <TransitionGroup 
          enter-active-class="transition duration-300 ease-out"
          enter-from-class="opacity-0 transform translate-y-4"
          enter-to-class="opacity-100 transform translate-y-0"
        >
          <div 
            v-for="kb in kbs.filter(k => k.kb_name.toLowerCase().includes(searchQuery.toLowerCase()))" 
            :key="kb.kb_id"
            class="group bg-white dark:bg-slate-800 rounded-2xl border border-slate-200 dark:border-slate-700 overflow-hidden hover:shadow-xl hover:border-indigo-300/50 transition-all duration-300"
          >
            <div class="p-6">
              <div class="flex justify-between items-start mb-4">
                <div class="p-3 bg-indigo-50 dark:bg-indigo-900/30 rounded-xl">
                  <Database class="w-6 h-6 text-indigo-600 dark:text-indigo-400" />
                </div>
                <button 
                  @click.stop="handleDelete(kb.kb_id)"
                  class="p-2 text-slate-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors opacity-0 group-hover:opacity-100"
                >
                  <Trash2 class="w-4 h-4" />
                </button>
              </div>
              
              <h3 class="text-lg font-bold text-slate-800 dark:text-slate-100 mb-2 truncate">{{ kb.kb_name }}</h3>
              <p class="text-sm font-medium text-indigo-600 mb-2">{{ kb.kb_code }} <span v-if="kb.subject" class="text-slate-400">· {{ kb.subject }}</span></p>
              <p class="text-sm text-slate-500 dark:text-slate-400 line-clamp-2 h-10 mb-4">{{ kb.description || '暂无描述' }}</p>
              
              <div class="flex items-center space-x-4 text-xs font-medium text-slate-400 dark:text-slate-500 mb-6">
                <div class="flex items-center">
                  <span class="text-slate-700 dark:text-slate-300 mr-1">{{ kb.doc_count }}</span> 文档
                </div>
                <div class="flex items-center border-l border-slate-200 dark:border-slate-700 pl-4">
                  <span class="text-slate-700 dark:text-slate-300 mr-1">{{ kb.entity_count }}</span> 实体
                </div>
              </div>

              <button 
                @click="router.push(`/admin/kb/${kb.kb_id}`)"
                class="w-full py-2.5 bg-slate-50 dark:bg-slate-700/50 hover:bg-indigo-50 dark:hover:bg-indigo-900/40 text-slate-700 dark:text-slate-200 hover:text-indigo-600 dark:hover:text-indigo-300 text-sm font-bold rounded-xl transition-all flex items-center justify-center space-x-2"
              >
                <span>管理详情</span>
                <ArrowRight class="w-4 h-4" />
              </button>
            </div>
          </div>
        </TransitionGroup>
      </div>

    <!-- Create Modal Stub -->
    <div v-if="isCreateModalOpen" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-slate-900/60 backdrop-blur-sm" @click="isCreateModalOpen = false"></div>
      <div class="relative bg-white dark:bg-slate-800 rounded-2xl shadow-2xl w-full max-w-md p-8 animate-in fade-in zoom-in duration-200">
        <h2 class="text-xl font-bold text-slate-800 dark:text-slate-100 mb-6">新建知识库</h2>
        <div class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">名称</label>
            <input v-model="newKB.kb_name" type="text" class="w-full px-4 py-2.5 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl focus:ring-2 focus:ring-indigo-500 outline-none" placeholder="例如：电子学论文集" />
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">编码</label>
            <input v-model="newKB.kb_code" type="text" class="w-full px-4 py-2.5 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl focus:ring-2 focus:ring-indigo-500 outline-none" placeholder="例如：electronics_papers" />
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">学科</label>
            <input v-model="newKB.subject" type="text" class="w-full px-4 py-2.5 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl focus:ring-2 focus:ring-indigo-500 outline-none" placeholder="例如：核电子学" />
          </div>
          <div>
            <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">描述</label>
            <textarea v-model="newKB.description" class="w-full px-4 py-2.5 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-xl focus:ring-2 focus:ring-indigo-500 outline-none h-24" placeholder="简要说明知识库用途..."></textarea>
          </div>
        </div>
        <div class="flex space-x-3 mt-8">
          <button @click="isCreateModalOpen = false" class="flex-1 py-2.5 bg-slate-100 text-slate-600 font-bold rounded-xl hover:bg-slate-200 transition">取消</button>
          <button @click="handleCreate" class="flex-1 py-2.5 bg-indigo-600 text-white font-bold rounded-xl hover:bg-indigo-700 transition">立即创建</button>
        </div>
      </div>
    </div>
  </div>
</template>
