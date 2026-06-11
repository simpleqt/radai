<script setup lang="ts">
import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import * as icons from 'lucide-vue-next';
import { NAV_ITEMS, isNavItemActive, type NavItem } from '@/app/navigation';

const route = useRoute();
const router = useRouter();

const isMobile = computed(() => route.path === '/m' || route.path.startsWith('/m/'));

const resolveTarget = (item: NavItem): string => {
  if (!isMobile.value) return item.to;
  // 移动端布局下保持 /m 前缀
  if (item.id === 'qa') return '/m';
  if (item.id === 'explore') return '/m/explore';
  return item.to;
};

const handleClick = (item: NavItem) => {
  if (!item.enabled) return;
  const target = resolveTarget(item);
  if (route.path !== target) router.push(target);
};

const iconOf = (name: string) => (icons as any)[name] || icons.Circle;
</script>

<template>
  <nav
    class="hidden md:flex flex-col items-center w-[76px] shrink-0 h-full bg-white dark:bg-[#0b0f1a] border-r border-slate-200 dark:border-slate-800 py-4 gap-1 z-40"
  >
    <!-- Brand -->
    <div class="mb-3 flex flex-col items-center gap-1">
      <div class="w-10 h-10 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shadow-lg shadow-indigo-200/60 dark:shadow-none">
        <component :is="iconOf('Atom')" class="w-5 h-5 text-white" />
      </div>
    </div>

    <button
      v-for="item in NAV_ITEMS"
      :key="item.id"
      type="button"
      :disabled="!item.enabled"
      @click="handleClick(item)"
      class="group relative w-full flex flex-col items-center gap-1 py-2.5 transition-all"
      :class="[
        !item.enabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer',
      ]"
      :title="item.enabled ? item.label : item.label + '（敬请期待）'"
    >
      <span
        class="absolute left-0 top-1/2 -translate-y-1/2 h-7 w-1 rounded-r-full bg-indigo-500 transition-all"
        :class="isNavItemActive(item, route.path) ? 'opacity-100' : 'opacity-0'"
      />
      <span
        class="w-10 h-10 rounded-xl flex items-center justify-center transition-all"
        :class="isNavItemActive(item, route.path)
          ? 'bg-indigo-50 dark:bg-indigo-500/15 text-indigo-600 dark:text-indigo-400'
          : 'text-slate-400 group-hover:text-slate-600 dark:group-hover:text-slate-200 group-hover:bg-slate-100 dark:group-hover:bg-slate-800/60'"
      >
        <component :is="iconOf(item.icon)" class="w-5 h-5" />
      </span>
      <span
        class="text-[10px] font-medium leading-tight text-center px-0.5"
        :class="isNavItemActive(item, route.path) ? 'text-indigo-600 dark:text-indigo-400' : 'text-slate-400'"
      >{{ item.label }}</span>
    </button>

    <div class="flex-1" />
  </nav>
</template>
