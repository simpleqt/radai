<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { authState, logout } from '@/utils/request';
import {
  BarChart3,
  Bot,
  ChevronRight,
  FileText,
  GraduationCap,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  User,
} from 'lucide-vue-next';

const router = useRouter();
const route = useRoute();
const isSidebarOpen = ref(typeof window === 'undefined' ? true : window.innerWidth >= 1024);

const displayName = computed(() =>
  String(
    authState.value.user?.display_name || authState.value.user?.name || authState.value.user?.username || ''
  ).trim(),
);

const navItems = [
  { name: '学情总览', path: '/teacher/dashboard', icon: LayoutDashboard },
  { name: '提问列表', path: '/teacher/questions', icon: MessageSquare },
  { name: '分析报告', path: '/teacher/reports', icon: BarChart3 },
  { name: '题库管理', path: '/teacher/bank', icon: FileText },
  { name: '返回智能体', path: '/app', icon: Bot },
];

const isNavItemActive = (path: string) => {
  if (path === '/app') return route.path === '/app' || route.path.startsWith('/app/');
  return route.path === path || route.path.startsWith(`${path}/`);
};

const handleLogout = () => {
  if (confirm('确定要退出登录吗？')) logout();
};

const closeSidebarOnEscape = (event: KeyboardEvent) => {
  if (event.key === 'Escape' && window.innerWidth < 1024) {
    isSidebarOpen.value = false;
  }
};

onMounted(() => document.addEventListener('keydown', closeSidebarOnEscape));
onBeforeUnmount(() => document.removeEventListener('keydown', closeSidebarOnEscape));
</script>

<template>
  <div class="h-screen overflow-hidden bg-surface-sunken flex text-text-primary font-sans">
    <aside
      class="fixed inset-y-0 left-0 z-50 w-64 bg-surface/80 backdrop-blur-lg border-r border-border-subtle transform transition-all duration-300 lg:sticky lg:top-0 lg:bottom-auto lg:h-screen lg:translate-x-0"
      :class="isSidebarOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <div class="flex h-full min-h-0 flex-col">
        <div class="px-6 py-6 flex shrink-0 items-center gap-2.5 border-b border-border-subtle">
          <div class="w-9 h-9 bg-accent rounded-xl flex items-center justify-center shadow-sm">
            <GraduationCap class="w-5 h-5 text-white" />
          </div>
          <span class="text-lg font-extrabold tracking-tight text-text-primary">
            教学<span class="text-accent font-medium">分析</span>
          </span>
        </div>

        <nav class="min-h-0 flex-1 overflow-y-auto px-3.5 space-y-1.5 mt-5 pb-4 custom-scrollbar">
          <router-link
            v-for="item in navItems"
            :key="item.path"
            :to="item.path"
            @click="isSidebarOpen = false"
            class="group relative flex items-center justify-between px-3.5 py-2.5 rounded-xl transition-all duration-200 hover-lift border-l-2 border-transparent"
            :class="isNavItemActive(item.path) ? 'bg-accent-soft text-accent font-bold border-accent shadow-sm pl-4'
              : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary dark:hover:text-white'"
          >
            <div class="flex items-center space-x-3">
              <component
                :is="item.icon"
                class="w-4.5 h-4.5"
                :class="isNavItemActive(item.path) ? 'text-accent' : 'text-text-tertiary group-hover:text-accent'"
              />
              <span class="text-sm font-medium tracking-wide">{{ item.name }}</span>
            </div>
            <ChevronRight v-if="isNavItemActive(item.path)" class="w-3.5 h-3.5 text-accent" />
          </router-link>
        </nav>

        <div class="shrink-0 p-4 border-t border-border-subtle bg-white/30">
          <div class="flex items-center justify-between p-3 rounded-2xl border border-border-subtle bg-surface-sunken backdrop-blur-sm">
            <div class="flex items-center min-w-0 flex-1">
              <div class="w-9 h-9 rounded-xl bg-accent-soft flex items-center justify-center overflow-hidden border border-accent/25">
                <User class="w-5 h-5 text-accent/80" />
              </div>
              <div class="ml-2.5 flex-1 min-w-0">
                <p class="text-xs font-bold text-text-primary truncate">{{ displayName || '教师' }}</p>
                <p class="text-[10px] text-text-tertiary truncate mt-0.5">基础力学课程</p>
              </div>
            </div>
            <button
              @click="handleLogout"
              class="w-8 h-8 rounded-xl flex items-center justify-center text-text-tertiary hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/30 border border-transparent transition-all cursor-pointer"
              title="注销登录"
              aria-label="注销登录"
            >
              <LogOut class="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    </aside>

    <div class="flex h-full flex-1 flex-col min-w-0 overflow-hidden">
      <header class="lg:hidden flex shrink-0 items-center justify-between px-6 py-4 bg-surface/80 backdrop-blur-md border-b border-border-subtle">
        <button
          @click="isSidebarOpen = !isSidebarOpen"
          class="p-2 -ml-2 rounded-lg hover:bg-surface-hover focus:outline-none"
          aria-label="打开或关闭教学分析导航"
          :aria-expanded="isSidebarOpen"
        >
          <Menu class="w-6 h-6" />
        </button>
        <span class="text-lg font-bold">教学分析</span>
        <div class="w-10"></div>
      </header>

      <Transition name="fade">
        <button
          v-if="isSidebarOpen"
          type="button"
          class="fixed inset-0 z-40 bg-slate-950/20 backdrop-blur-md dark:bg-black/45 lg:hidden"
          aria-label="关闭教学分析导航"
          @click="isSidebarOpen = false"
        ></button>
      </Transition>

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
