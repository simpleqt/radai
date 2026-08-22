<script setup lang="ts">
import { computed, ref } from 'vue';
import AppSidebar from './AppSidebar.vue';
import TopBar from './TopBar.vue';
import { useSidebar } from '@/composables/useSidebar';

const props = withDefaults(defineProps<{
  title?: string;
  brandName?: string;
  brandIcon?: string;
  brandLogo?: string;
  showConversations?: boolean;
  /** 是否存在打开的右侧面板；≥2xl 时占据栅格第三列，否则由面板自身覆盖显示 */
  panelOpen?: boolean;
}>(), {
  showConversations: true,
  panelOpen: false,
});

const emit = defineEmits<{
  (e: 'new-chat'): void;
  (e: 'auth-action'): void;
}>();

const { state, isOverlay, toggle, close } = useSidebar();
const sidebarRef = ref<InstanceType<typeof AppSidebar> | null>(null);
const activeConversationTitle = ref('');
const topBarTitle = computed(() => {
  const explicitTitle = props.title?.trim();
  if (explicitTitle && explicitTitle !== props.brandName) return explicitTitle;
  return activeConversationTitle.value || undefined;
});

const handleNavigate = () => {
  if (isOverlay.value) close();
};

const handleActiveTitleChange = (title: string) => {
  activeConversationTitle.value = title.trim();
};

defineExpose({
  reloadConversations: () => sidebarRef.value?.loadConversations(),
});
</script>

<template>
  <div
    class="app-shell grid h-[100dvh] overflow-hidden bg-surface text-text-primary"
    :data-sidebar="state"
    :data-panel="panelOpen ? 'docked' : 'hidden'"
  >
    <AppSidebar
      ref="sidebarRef"
      class="app-shell__sidebar"
      :brand-name="brandName"
      :brand-icon="brandIcon"
      :brand-logo="brandLogo"
      :show-conversations="showConversations"
      @new-chat="emit('new-chat')"
      @auth-action="emit('auth-action')"
      @navigate="handleNavigate"
      @collapse="close"
      @active-title-change="handleActiveTitleChange"
    />

    <Transition name="fade">
      <div
        v-if="isOverlay && state === 'expanded'"
        class="fixed inset-0 z-40 bg-black/40"
        aria-hidden="true"
        @click="close"
      />
    </Transition>

    <main class="app-shell__main @container/main flex min-h-0 min-w-0 flex-col overflow-hidden">
      <TopBar
        :title="topBarTitle"
        :sidebar-state="state"
        :is-overlay="isOverlay"
        @toggle-sidebar="toggle"
      >
        <template #actions>
          <slot name="actions" />
        </template>
      </TopBar>

      <slot name="subbar" />
      <slot />
    </main>

    <slot name="panel" />
  </div>
</template>

<style scoped>
.app-shell {
  grid-template-columns: var(--col-sidebar, 0px) minmax(0, 1fr) var(--col-panel, 0px);
  grid-template-rows: 100%;
  transition: grid-template-columns 200ms ease;
}

/* 侧栏与面板在窄视口下 position:fixed 会脱离栅格流，
   若不锁定列号，剩余的 in-flow 子元素会被自动放进第一列（宽 0）。 */
.app-shell__sidebar {
  grid-column: 1;
  grid-row: 1;
  transition: transform 200ms ease;
}

.app-shell[data-sidebar='collapsed'] .app-shell__sidebar {
  pointer-events: none;
  transform: translateX(-100%);
}

.app-shell__main {
  grid-column: 2;
  grid-row: 1;
}

/* 媒体查询不提升特异性，因此列宽只在对应断点内赋值，
   而不是先赋值再在窄屏归零——后者会被 [data-*] 选择器压过。 */
@media (width >= 64rem) {
  .app-shell[data-sidebar='expanded'] {
    --col-sidebar: var(--sidebar-w);
  }
}

@media (width >= 96rem) {
  .app-shell[data-panel='docked'] {
    --col-panel: var(--panel-w);
  }
}

/* <lg：侧栏脱离栅格改为抽屉 */
@media (width < 64rem) {
  .app-shell__sidebar {
    position: fixed;
    inset-block: 0;
    inset-inline-start: 0;
    z-index: 50;
    transform: translateX(-100%);
  }

  .app-shell[data-sidebar='expanded'] .app-shell__sidebar {
    transform: translateX(0);
  }
}
</style>
