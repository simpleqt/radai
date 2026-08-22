<script setup lang="ts">
import { computed, ref, onMounted } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { authState, logout } from '@/utils/request';
import {
  BarChart3, Bot, Database, MessageSquare, LogOut, Droplets,
  Menu, User, ChevronRight, Sparkles, TableProperties, FileText,
  Sun, Moon
} from 'lucide-vue-next';

const router = useRouter();
const route = useRoute();
const isSidebarOpen = ref(true);
const isDark = ref(false);

const displayName = computed(() =>
  String(
    authState.value.user?.display_name || authState.value.user?.name || authState.value.user?.username || ''
  ).trim()
);

const navItems = [
  { name: '控制台', path: '/admin/dashboard', icon: BarChart3 },
  { name: '知识库管理', path: '/admin/kbs', icon: Database },
  { name: '提示词管理', path: '/admin/prompts', icon: FileText },
  { name: 'Skills 管理', path: '/admin/skills', icon: Sparkles },
  { name: '实验数据', path: '/admin/datasets', icon: TableProperties },
  { name: '地下水项目', path: '/admin/groundwater-projects', icon: Droplets },
  { name: '智能对话', path: '/app', icon: MessageSquare },
];

const toggleDarkMode = () => {
  isDark.value = !isDark.value;
  if (isDark.value) {
    document.documentElement.classList.add('dark');
    localStorage.setItem('theme', 'dark');
  } else {
    document.documentElement.classList.remove('dark');
    localStorage.setItem('theme', 'light');
  }
};

const handleLogout = () => {
  if (confirm('确定要退出登录吗？')) {
    logout();
  }
};

onMounted(() => {
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme === 'dark' || (!savedTheme && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    isDark.value = true;
    document.documentElement.classList.add('dark');
  } else {
    isDark.value = false;
    document.documentElement.classList.remove('dark');
  }
});
</script>

<template>
  <div class="h-screen overflow-hidden bg-surface-sunken flex text-text-primary font-sans">
    
    <!-- Sidebar -->
    <aside 
      class="fixed inset-y-0 left-0 z-50 w-64 bg-surface/80 backdrop-blur-lg border-r border-border-subtle transform transition-all duration-300 lg:sticky lg:top-0 lg:bottom-auto lg:h-screen lg:translate-x-0"
      :class="isSidebarOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <div class="flex h-full min-h-0 flex-col">
        <!-- Logo & Theme Toggle -->
        <div class="px-6 py-6 flex shrink-0 items-center justify-between border-b border-border-subtle">
          <div class="flex items-center space-x-2.5">
            <div class="w-9 h-9 bg-accent rounded-xl flex items-center justify-center shadow-sm hover:scale-105 transition-transform duration-300">
              <Bot class="w-5 h-5 text-white" />
            </div>
            <span class="text-lg font-extrabold tracking-tight text-text-primary">
              智能体管理<span class="text-accent font-medium">平台</span>
            </span>
          </div>
          <button 
            @click="toggleDarkMode" 
            class="p-2 rounded-xl bg-surface-sunken text-text-secondary hover:text-accent hover-lift transition-colors duration-200 border border-border-subtle"
            :title="isDark ? '切换至浅色模式' : '切换至暗色模式'"
          >
            <Sun v-if="isDark" class="w-4 h-4 text-amber-500" />
            <Moon v-else class="w-4 h-4 text-text-secondary" />
          </button>
        </div>

        <!-- Nav -->
        <nav class="min-h-0 flex-1 overflow-y-auto px-3.5 space-y-1.5 mt-5 pb-4 custom-scrollbar">
          <router-link 
            v-for="item in navItems" 
            :key="item.path"
            :to="item.path"
            class="group relative flex items-center justify-between px-3.5 py-2.5 rounded-xl transition-all duration-200 hover-lift border-l-2 border-transparent"
            :class="route.path === item.path ? 'bg-accent-soft text-accent font-bold border-accent shadow-sm pl-4' 
              : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary dark:hover:text-white'"
          >
            <div class="flex items-center space-x-3">
              <component 
                :is="item.icon" 
                class="w-4.5 h-4.5 transition-transform duration-300 group-hover:scale-110"
                :class="route.path === item.path ? 'text-accent' : 'text-text-tertiary group-hover:text-accent'"
              />
              <span class="text-sm font-medium tracking-wide">{{ item.name }}</span>
            </div>
            <ChevronRight 
              v-if="route.path === item.path" 
              class="w-3.5 h-3.5 text-accent" 
            />
          </router-link>
        </nav>

        <!-- User Profile Stub -->
        <div class="shrink-0 p-4 border-t border-border-subtle bg-white/30">
          <div class="flex items-center justify-between p-3 rounded-2xl border border-border-subtle bg-surface-sunken backdrop-blur-sm relative group/profile">
            <div class="flex items-center min-w-0 flex-1">
              <div class="w-9 h-9 rounded-xl bg-accent-soft flex items-center justify-center overflow-hidden border border-accent/25">
                 <User class="w-5 h-5 text-accent/80" />
              </div>
              <div class="ml-2.5 flex-1 min-w-0">
                 <p class="text-xs font-bold text-text-primary truncate">{{ displayName || '管理员' }}</p>
                 <p class="text-[10px] text-text-tertiary truncate mt-0.5">{{ authState.user?.email || 'admin@example.com' }}</p>
              </div>
            </div>
            <button 
              @click="handleLogout" 
              class="w-8 h-8 rounded-xl flex items-center justify-center text-text-tertiary hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/30 border border-transparent hover:border-red-100 dark:hover:border-red-900/30 transition-all cursor-pointer"
              title="注销登录"
            >
              <LogOut class="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    </aside>

    <!-- Main -->
    <div class="flex h-full flex-1 flex-col min-w-0 overflow-hidden">
      <!-- Top Header Mobile -->
      <header class="lg:hidden flex shrink-0 items-center justify-between px-6 py-4 bg-surface/80 backdrop-blur-md border-b border-border-subtle">
          <button @click="isSidebarOpen = !isSidebarOpen" class="p-2 -ml-2 rounded-lg hover:bg-surface-hover">
            <Menu class="w-6 h-6" />
          </button>
          <span class="text-lg font-bold">智能体管理平台</span>
          <div class="w-10"></div>
      </header>

      <!-- View Wrapper -->
      <main class="min-h-0 flex-1 overflow-y-auto p-4 lg:p-8 scroll-smooth custom-scrollbar">
         <router-view />
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
