<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, watch } from 'vue';
import { useRoute } from 'vue-router';
import { authState, logout } from '@/utils/request';
import {
  Activity, BarChart3, Bot, Database, MessageSquare, LogOut,
  Menu, ChevronRight, Sparkles, TableProperties, Settings, ServerCog, FileText,
  Sun, Moon, ArrowUpRight,
} from 'lucide-vue-next';

const route = useRoute();
const isSidebarOpen = ref(typeof window === 'undefined' ? true : window.innerWidth >= 1024);
const isDark = ref(false);

const displayName = computed(() =>
  String(
    authState.value.user?.display_name || authState.value.user?.name || authState.value.user?.username || ''
  ).trim()
);
const avatarInitial = computed(() => (displayName.value || '管').charAt(0).toUpperCase());

const navGroups = [
  {
    label: '总览',
    items: [
      { name: '控制台', path: '/admin/dashboard', icon: BarChart3 },
    ],
  },
  {
    label: '知识资产',
    items: [
      { name: '知识库管理', path: '/admin/kbs', icon: Database },
      { name: '提示词管理', path: '/admin/prompts', icon: FileText },
      { name: 'Skills 管理', path: '/admin/skills', icon: Sparkles },
      { name: '实验数据', path: '/admin/datasets', icon: TableProperties },
    ],
  },
  {
    label: '运行维护',
    items: [
      { name: '平台运维', path: '/admin/platform', icon: ServerCog },
      { name: '运行监控', path: '/admin/runtime', icon: Activity },
      { name: '系统管理', path: '/admin/system', icon: Settings },
    ],
  },
];

const isNavItemActive = (path: string) => {
  if (path === '/app') return route.path === '/app' || route.path.startsWith('/app/');
  return route.path === path || route.path.startsWith(`${path}/`);
};

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

const closeSidebarOnEscape = (event: KeyboardEvent) => {
  if (event.key === 'Escape' && window.innerWidth < 1024) {
    isSidebarOpen.value = false;
  }
};

watch(
  () => route.fullPath,
  () => {
    if (typeof window !== 'undefined' && window.innerWidth < 1024) {
      isSidebarOpen.value = false;
    }
  },
);

onMounted(() => {
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme === 'dark' || (!savedTheme && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    isDark.value = true;
    document.documentElement.classList.add('dark');
  } else {
    isDark.value = false;
    document.documentElement.classList.remove('dark');
  }
  document.addEventListener('keydown', closeSidebarOnEscape);
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', closeSidebarOnEscape);
});
</script>

<template>
  <div class="h-screen overflow-hidden bg-surface-sunken flex text-text-primary font-sans">

    <!-- Sidebar -->
    <aside
      class="fixed inset-y-0 left-0 z-50 w-64 bg-surface/85 backdrop-blur-xl border-r border-border-subtle transform transition-all duration-300 lg:sticky lg:top-0 lg:bottom-auto lg:h-screen lg:translate-x-0"
      :class="isSidebarOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <div class="flex h-full min-h-0 flex-col">
        <!-- Logo & Theme Toggle -->
        <div class="px-5 py-6 flex shrink-0 items-center justify-between border-b border-border-subtle">
          <div class="flex items-center gap-2.5 min-w-0">
            <div class="w-9 h-9 shrink-0 bg-gradient-to-br from-violet-500 via-purple-500 to-indigo-600 rounded-xl flex items-center justify-center shadow-md shadow-violet-500/25 hover:scale-105 transition-transform duration-300">
              <Bot class="w-5 h-5 text-white" />
            </div>
            <div class="min-w-0">
              <p class="text-[15px] font-extrabold tracking-tight text-text-primary leading-tight truncate">
                智能体管理<span class="text-accent">平台</span>
              </p>
              <p class="text-[10px] text-text-tertiary tracking-wide truncate">Agent Ops Console</p>
            </div>
          </div>
          <button
            @click="toggleDarkMode"
            class="p-2 shrink-0 rounded-xl bg-surface-sunken text-text-secondary hover:text-accent hover-lift transition-colors duration-200 border border-border-subtle"
            :title="isDark ? '切换至浅色模式' : '切换至暗色模式'"
            :aria-label="isDark ? '切换至浅色模式' : '切换至暗色模式'"
          >
            <Sun v-if="isDark" class="w-4 h-4 text-amber-500" />
            <Moon v-else class="w-4 h-4 text-text-secondary" />
          </button>
        </div>

        <!-- Nav -->
        <nav class="min-h-0 flex-1 overflow-y-auto px-3.5 py-4 custom-scrollbar">
          <div v-for="(group, groupIndex) in navGroups" :key="group.label" :class="groupIndex > 0 ? 'mt-5' : ''">
            <p class="px-3 mb-1.5 text-[10px] font-bold uppercase tracking-[0.18em] text-text-tertiary/80">{{ group.label }}</p>
            <div class="space-y-1">
              <router-link
                v-for="item in group.items"
                :key="item.path"
                :to="item.path"
                @click="isSidebarOpen = false"
                class="group relative flex items-center justify-between pl-3 pr-2.5 py-2.5 rounded-xl transition-all duration-200"
                :class="isNavItemActive(item.path)
                  ? 'bg-accent-soft text-accent font-bold shadow-sm'
                  : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary'"
              >
                <span
                  class="absolute left-0 top-1/2 -translate-y-1/2 h-5 w-1 rounded-r-full bg-accent transition-all duration-200"
                  :class="isNavItemActive(item.path) ? 'opacity-100 scale-y-100' : 'opacity-0 scale-y-50'"
                ></span>
                <div class="flex items-center gap-3 min-w-0">
                  <component
                    :is="item.icon"
                    class="w-4.5 h-4.5 shrink-0 transition-transform duration-300 group-hover:scale-110"
                    :class="isNavItemActive(item.path) ? 'text-accent' : 'text-text-tertiary group-hover:text-accent'"
                  />
                  <span class="text-sm font-medium tracking-wide truncate">{{ item.name }}</span>
                </div>
                <ChevronRight
                  v-if="isNavItemActive(item.path)"
                  class="w-3.5 h-3.5 text-accent shrink-0"
                />
              </router-link>
            </div>
          </div>

          <!-- 快捷入口:回到对话前台 -->
          <div class="mt-6 border-t border-border-subtle pt-4">
            <router-link
              to="/app"
              class="group flex items-center justify-between rounded-xl border border-dashed border-border-strong/70 px-3 py-2.5 text-text-secondary transition-all duration-200 hover:border-accent/50 hover:bg-accent-soft/50 hover:text-accent"
            >
              <div class="flex items-center gap-3">
                <MessageSquare class="w-4.5 h-4.5 text-text-tertiary group-hover:text-accent transition-colors" />
                <span class="text-sm font-medium">智能对话</span>
              </div>
              <ArrowUpRight class="w-3.5 h-3.5 text-text-tertiary group-hover:text-accent transition-all group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
            </router-link>
          </div>
        </nav>

        <!-- User Profile -->
        <div class="shrink-0 p-4 border-t border-border-subtle">
          <div class="flex items-center justify-between p-2.5 rounded-2xl border border-border-subtle bg-surface-sunken/80 backdrop-blur-sm">
            <div class="flex items-center min-w-0 flex-1">
              <div class="w-9 h-9 shrink-0 rounded-xl bg-gradient-to-br from-violet-500 via-purple-500 to-indigo-500 flex items-center justify-center text-sm font-extrabold text-white shadow-sm shadow-violet-500/25">
                {{ avatarInitial }}
              </div>
              <div class="ml-2.5 flex-1 min-w-0">
                <p class="text-xs font-bold text-text-primary truncate">{{ displayName || '管理员' }}</p>
                <p class="text-[10px] text-text-tertiary truncate mt-0.5">{{ authState.user?.email || 'admin@example.com' }}</p>
              </div>
            </div>
            <button
              @click="handleLogout"
              class="w-8 h-8 shrink-0 rounded-xl flex items-center justify-center text-text-tertiary hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/30 border border-transparent hover:border-red-100 dark:hover:border-red-900/30 transition-all cursor-pointer"
              title="注销登录"
              aria-label="注销登录"
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
          <button
            @click="isSidebarOpen = !isSidebarOpen"
            class="p-2 -ml-2 rounded-lg hover:bg-surface-hover focus:outline-none focus:ring-2 focus:ring-accent/40"
            aria-label="打开或关闭管理导航"
            :aria-expanded="isSidebarOpen"
          >
            <Menu class="w-6 h-6" />
          </button>
          <span class="text-lg font-bold">智能体管理平台</span>
          <div class="w-10"></div>
      </header>

      <Transition name="fade">
        <button
          v-if="isSidebarOpen"
          type="button"
          class="fixed inset-0 z-40 bg-slate-950/20 backdrop-blur-md dark:bg-black/45 lg:hidden"
          aria-label="关闭管理导航"
          @click="isSidebarOpen = false"
        ></button>
      </Transition>

      <!-- View Wrapper -->
      <main class="relative min-h-0 flex-1 overflow-y-auto scroll-smooth custom-scrollbar">
        <!-- 氛围背景:顶部极光,仅装饰 -->
        <div class="pointer-events-none absolute inset-x-0 top-0 h-72 overflow-hidden" aria-hidden="true">
          <div class="absolute -top-24 left-[12%] h-64 w-[38rem] rounded-full bg-accent/[0.07] blur-3xl dark:bg-accent/[0.13]"></div>
          <div class="absolute -top-32 right-[8%] h-72 w-96 rounded-full bg-sky-400/[0.06] blur-3xl dark:bg-sky-400/[0.1]"></div>
          <div class="absolute -top-16 left-[55%] h-48 w-72 rounded-full bg-fuchsia-400/[0.05] blur-3xl dark:bg-fuchsia-400/[0.09]"></div>
        </div>
        <div class="relative p-4 lg:p-8">
          <router-view />
        </div>
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
