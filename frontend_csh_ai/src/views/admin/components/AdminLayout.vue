<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { authState, logout } from '@/utils/request';
import {
  BarChart3, Database, MessageSquare, LogOut, Droplets,
  Menu, X, User, ChevronRight, Settings
} from 'lucide-vue-next';

const router = useRouter();
const route = useRoute();
const isSidebarOpen = ref(true);
const displayName = computed(() =>
  String(
    authState.value.user?.display_name || authState.value.user?.name || authState.value.user?.username || ''
  ).trim()
);

const navItems = [
  { name: '控制台', path: '/admin/dashboard', icon: BarChart3 },
  { name: '知识库管理', path: '/admin/kbs', icon: Database },
  { name: '地下水项目', path: '/admin/groundwater-projects', icon: Droplets },
  { name: '智能对话', path: '/app', icon: MessageSquare },
];

const handleLogout = () => {
  if (confirm('确定要退出登录吗？')) {
    logout();
  }
};
</script>

<template>
  <div class="min-h-screen bg-slate-50 dark:bg-slate-900 flex text-slate-900 dark:text-slate-100 font-sans">
    
    <!-- Sidebar -->
    <aside 
      class="fixed inset-y-0 left-0 z-50 w-64 bg-white dark:bg-slate-800 border-r border-slate-200 dark:border-slate-700 transform transition-transform duration-300 lg:relative lg:translate-x-0"
      :class="isSidebarOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <div class="flex flex-col h-full">
        <!-- Logo -->
        <div class="px-6 py-8 flex items-center space-x-3">
          <div class="w-10 h-10 bg-indigo-600 rounded-xl flex items-center justify-center shadow-lg shadow-indigo-500/20">
            <Database class="w-6 h-6 text-white" />
          </div>
          <span class="text-xl font-bold tracking-tight">知识库管理 <span class="text-indigo-600">平台</span></span>
        </div>

        <!-- Nav -->
        <nav class="flex-1 px-4 space-y-2 mt-4">
          <router-link 
            v-for="item in navItems" 
            :key="item.path"
            :to="item.path"
            class="group flex items-center justify-between px-4 py-3 rounded-xl transition-all duration-200"
            :class="route.path === item.path ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300' : 'text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700/50'"
          >
            <div class="flex items-center space-x-3">
              <component :is="item.icon" class="w-5 h-5 transition-transform group-hover:scale-110" />
              <span class="font-medium text-sm">{{ item.name }}</span>
            </div>
            <ChevronRight v-if="route.path === item.path" class="w-4 h-4" />
          </router-link>
        </nav>

        <!-- User Profile Stub -->
        <div class="p-4 border-t border-slate-100 dark:border-slate-700">
          <div class="flex items-center p-2 rounded-xl border border-slate-100 dark:border-slate-700/50 bg-slate-50/50 dark:bg-slate-800/50">
            <div class="w-10 h-10 rounded-lg bg-slate-200 dark:bg-slate-700 flex items-center justify-center overflow-hidden">
               <User class="w-6 h-6 text-slate-400" />
            </div>
            <div class="ml-3 flex-1 min-w-0">
               <p class="text-xs font-bold truncate">{{ displayName || '管理员' }}</p>
               <p class="text-[10px] text-slate-400 truncate">{{ authState.user?.email || 'admin@example.com' }}</p>
            </div>
          </div>
          
          <button 
            @click="handleLogout"
            class="w-full mt-4 flex items-center justify-center space-x-2 py-2 text-sm font-medium text-red-500 hover:bg-red-50 dark:hover:bg-red-900/10 rounded-xl transition-colors"
          >
            <LogOut class="w-4 h-4" />
            <span>注销登录</span>
          </button>
        </div>
      </div>
    </aside>

    <!-- Main -->
    <div class="flex-1 flex flex-col min-w-0 overflow-hidden">
      <!-- Top Header Mobile -->
      <header class="lg:hidden flex items-center justify-between px-6 py-4 bg-white dark:bg-slate-800 border-b border-slate-200 dark:border-slate-700">
          <button @click="isSidebarOpen = !isSidebarOpen" class="p-2 -ml-2 rounded-lg hover:bg-slate-100">
            <Menu class="w-6 h-6" />
          </button>
          <span class="text-lg font-bold">知识库管理平台</span>
          <div class="w-10"></div>
      </header>

      <!-- View Wrapper -->
      <main class="flex-1 overflow-y-auto p-4 lg:p-8 scroll-smooth">
         <router-view v-slot="{ Component }">
            <transition name="fade" mode="out-in">
              <component :is="Component" />
            </transition>
         </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.fade-enter-from {
  opacity: 0;
  transform: translateY(10px);
}

.fade-leave-to {
  opacity: 0;
  transform: translateY(-10px);
}
</style>
