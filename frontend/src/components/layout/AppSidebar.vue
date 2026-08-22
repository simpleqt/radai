<script lang="ts">
import { ref as sharedRef } from 'vue';

type UiConversation = { threadId: string; title: string; updatedAt?: string | number | null; createdAt?: string | number | null; pinned?: boolean };

const conversations = sharedRef<UiConversation[]>([]);
const taskExpanded = sharedRef(false);
const loading = sharedRef(false);
const loadingMore = sharedRef(false);
const hasMore = sharedRef(true);
const nextOffset = sharedRef(0);
let conversationsLoadedFor = '';
</script>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import * as icons from 'lucide-vue-next';
import {
  ChevronDown,
  Folder,
  FolderOpen,
  LogIn,
  LogOut,
  MessageCirclePlus,
  PanelLeftClose,
  Pencil,
  Pin,
  PinOff,
  Search,
  Settings,
  Trash2,
} from 'lucide-vue-next';
import { NAV_ITEMS, isNavItemActive } from '@/app/navigation';
import { ConversationAPI, type Conversation } from '@/api/conversations';
import { authState } from '@/utils/request';
import {
  clearGuestConversations,
  deleteGuestConversation,
  listGuestConversations,
  renameGuestConversation,
} from '@/services/storage/guestIndexedDb';

const props = withDefaults(defineProps<{
  brandName?: string;
  brandIcon?: string;
  brandLogo?: string;
  showConversations?: boolean;
  basePath?: string;
  qaOnly?: boolean;
  conversationAgentCode?: string;
}>(), {
  brandName: '核电子学智能助手',
  brandIcon: 'Atom',
  brandLogo: '/log.png',
  showConversations: true,
  basePath: '',
  qaOnly: false,
  conversationAgentCode: '',
});

const emit = defineEmits<{
  (e: 'new-chat'): void;
  (e: 'auth-action'): void;
  (e: 'navigate'): void;
  (e: 'collapse'): void;
  (e: 'active-title-change', title: string): void;
}>();

const router = useRouter();
const route = useRoute();

const searchModalOpen = ref(false);
const searchQuery = ref('');
const historyListRef = ref<HTMLElement | null>(null);
let historyResizeObserver: ResizeObserver | null = null;
const HISTORY_PAGE_SIZE = 20;
const DEFAULT_TASK_LIMIT = 5;

const iconOf = (name: string) => (icons as any)[name] || icons.Circle;
const sidebarNavItems = computed(() => {
  if (props.qaOnly) return [];
  return NAV_ITEMS.filter((item) => item.id !== 'qa');
});

const isGuestMode = computed(() => !authState.value.token);
const conversationOwnerKey = () => {
  const owner = isGuestMode.value ? 'guest' : `user:${authState.value.token || ''}`;
  return `${owner}:agent:${props.conversationAgentCode || '*'}`;
};
const displayName = computed(() => {
  const user = authState.value.user || {};
  return String(user.display_name || user.name || user.username || '').trim();
});
const displayBadge = computed(() => {
  const text = displayName.value;
  return text ? text.slice(0, 2).toUpperCase() : 'U';
});
const isSettingsActive = computed(() => route.path === '/settings');
const filteredConversations = computed(() => {
  const keyword = searchQuery.value.trim().toLowerCase();
  if (!keyword) return conversations.value;
  return conversations.value.filter((item) => item.title.toLowerCase().includes(keyword));
});
const taskRemainingCount = computed(() => Math.max(filteredConversations.value.length - DEFAULT_TASK_LIMIT, 0));
const visibleConversations = computed(() =>
  taskExpanded.value || searchQuery.value.trim()
    ? filteredConversations.value
    : filteredConversations.value.slice(0, DEFAULT_TASK_LIMIT)
);

const basePath = () => {
  if (props.basePath) return props.basePath;
  return route.path === '/groundwater' || route.path.startsWith('/groundwater/')
    ? '/groundwater'
    : '/app';
};

const go = (path: string) => {
  if (route.path !== path) router.push(path);
  emit('navigate');
};

const openTaskSearch = () => {
  searchModalOpen.value = true;
};

const closeTaskSearch = () => {
  searchModalOpen.value = false;
  searchQuery.value = '';
};

const toTimestamp = (value?: string | number | null) => {
  if (typeof value === 'number') return value;
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
};

const formatRelativeTime = (value?: string | number | null) => {
  const timestamp = toTimestamp(value);
  if (!timestamp) return '';
  const diff = Math.max(Date.now() - timestamp, 0);
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}小时前`;
  const days = Math.floor(diff / 86_400_000);
  if (days < 30) return `${days}天前`;
  return new Date(timestamp).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
};

const formatSearchTime = (value?: string | number | null) => {
  const timestamp = toTimestamp(value);
  if (!timestamp) return '';
  return new Date(timestamp).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
};

const updateActiveTitle = () => {
  const activeThreadId = String(route.params.threadId || '');
  if (!activeThreadId) {
    emit('active-title-change', '');
    return;
  }
  const active = conversations.value.find((item) => item.threadId === activeThreadId);
  emit('active-title-change', active?.title || '');
};

const loadConversations = async (append = false) => {
  if (!props.showConversations) return;
  if (append && (loading.value || loadingMore.value || !hasMore.value || isGuestMode.value)) return;
  if (!append && loading.value) return;
  const ownerKey = conversationOwnerKey();
  if (append) loadingMore.value = true;
  else loading.value = true;

  try {
    if (isGuestMode.value) {
      const guestConvs = (await listGuestConversations()).filter(
        (item) => !props.conversationAgentCode
          || item.agentCode === props.conversationAgentCode
          || (!item.agentCode && props.conversationAgentCode === 'ne'),
      );
      conversations.value = guestConvs.map((item) => ({
        threadId: item.threadId,
        title: item.title,
        updatedAt: item.updatedAt,
        createdAt: item.createdAt,
      }));
      nextOffset.value = 0;
      hasMore.value = false;
      conversationsLoadedFor = ownerKey;
    } else {
      const userConvs = await ConversationAPI.list({
        limit: HISTORY_PAGE_SIZE,
        offset: append ? nextOffset.value : 0,
        includeMetadata: Boolean(props.conversationAgentCode),
      });
      const scopedConvs = userConvs.filter((item) => {
        if (!props.conversationAgentCode) return true;
        // Conversations created before agent metadata was introduced belong to
        // the historical default `ne` experience. Keep them visible there,
        // while never exposing them in the audit workspace.
        return String(item.metadata?.agent_runtime?.agent_code || 'ne').trim().toLowerCase()
          === props.conversationAgentCode.trim().toLowerCase();
      });
      const nextPage = scopedConvs.map((item: Conversation) => ({
        threadId: item.thread_id,
        title: item.title,
        updatedAt: item.updated_at,
        createdAt: item.created_at,
        pinned: Boolean(item.pinned),
      }));
      if (append) {
        const existingIds = new Set(conversations.value.map((item) => item.threadId));
        conversations.value.push(...nextPage.filter((item) => !existingIds.has(item.threadId)));
      } else {
        conversations.value = nextPage;
      }
      nextOffset.value = (append ? nextOffset.value : 0) + userConvs.length;
      hasMore.value = userConvs.length === HISTORY_PAGE_SIZE;
      conversationsLoadedFor = ownerKey;
    }
  } catch (err) {
    console.error('Failed to load conversations', err);
  } finally {
    if (append) loadingMore.value = false;
    else loading.value = false;
    updateActiveTitle();
    const shouldScanNextScopedPage = Boolean(props.conversationAgentCode)
      && !isGuestMode.value
      && conversations.value.length < DEFAULT_TASK_LIMIT
      && hasMore.value;
    if (shouldScanNextScopedPage) void loadConversations(true);
    else void ensureHistoryOverflow();
  }
};

const ensureHistoryOverflow = async () => {
  await nextTick();
  const element = historyListRef.value;
  if (!element || loading.value || loadingMore.value || !hasMore.value || isGuestMode.value) return;
  if (!taskExpanded.value && !searchQuery.value.trim()) return;

  // 恰好填满面板的一页不会产生滚动条，也就无法通过滚动事件请求下一页。
  if (element.scrollHeight <= element.clientHeight + 8) {
    void loadConversations(true);
  }
};

const handleHistoryScroll = (event: Event) => {
  if (!taskExpanded.value && !searchQuery.value.trim()) return;
  const element = event.currentTarget as HTMLElement;
  const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
  if (remaining <= 80) void loadConversations(true);
};

const selectConversation = (threadId: string) => {
  closeTaskSearch();
  go(`${basePath()}/${threadId}`);
};

const newChat = () => {
  emit('new-chat');
  if (route.path !== basePath() || !!route.params.threadId) {
    router.push(basePath());
  }
  emit('navigate');
};

const handleDelete = async (e: Event, threadId: string) => {
  e.stopPropagation();
  if (!confirm('确定要删除这个会话吗？')) return;
  try {
    if (isGuestMode.value) await deleteGuestConversation(threadId);
    else await ConversationAPI.delete(threadId);
    await loadConversations();
    if (route.params.threadId === threadId) router.push(basePath());
  } catch {
    alert('删除失败');
  }
};

const handleRename = async (e: Event, conv: UiConversation) => {
  e.stopPropagation();
  const newTitle = prompt('请输入新的标题', conv.title);
  if (!newTitle || newTitle === conv.title) return;
  try {
    if (isGuestMode.value) await renameGuestConversation(conv.threadId, newTitle);
    else await ConversationAPI.rename(conv.threadId, newTitle);
    await loadConversations();
  } catch {
    alert('修改失败');
  }
};

// 置顶优先、其后按更新时间——与后端 list 的排序保持一致，便于就地重排而不
// 用重新拉第一页（翻过几页的列表不应该被一次置顶打回顶部）。
const sortConversationsInPlace = () => {
  const weight = (item: UiConversation) => (item.pinned ? 0 : 1);
  const time = (item: UiConversation) => new Date(item.updatedAt || item.createdAt || 0).getTime() || 0;
  conversations.value = [...conversations.value].sort(
    (a, b) => weight(a) - weight(b) || time(b) - time(a),
  );
};

const handleTogglePin = async (e: Event, conv: UiConversation) => {
  e.stopPropagation();
  if (isGuestMode.value) return;
  const next = !conv.pinned;
  conv.pinned = next;
  sortConversationsInPlace();
  try {
    await ConversationAPI.setPinned(conv.threadId, next);
  } catch {
    conv.pinned = !next;
    sortConversationsInPlace();
    alert(next ? '置顶失败' : '取消置顶失败');
  }
};

const handleClearAllGuest = async () => {
  if (!confirm('确定清空本地全部访客会话吗？')) return;
  try {
    await clearGuestConversations(props.conversationAgentCode || undefined);
    await loadConversations();
    if (route.params.threadId) router.push(basePath());
  } catch {
    alert('清空失败');
  }
};

onMounted(() => {
  if (typeof ResizeObserver !== 'undefined' && historyListRef.value) {
    historyResizeObserver = new ResizeObserver(() => void ensureHistoryOverflow());
    historyResizeObserver.observe(historyListRef.value);
  }
  if (conversationsLoadedFor !== conversationOwnerKey()) void loadConversations();
  else updateActiveTitle();
});

onBeforeUnmount(() => historyResizeObserver?.disconnect());

watch(() => authState.value.token, () => {
  conversationsLoadedFor = '';
  conversations.value = [];
  nextOffset.value = 0;
  hasMore.value = true;
  void loadConversations();
});
watch(() => route.params.threadId, updateActiveTitle);
watch(conversations, updateActiveTitle);
watch(taskExpanded, (expanded) => {
  if (expanded) void ensureHistoryOverflow();
});

defineExpose({ loadConversations });
</script>

<template>
  <aside
    class="@container/side flex h-full min-h-0 w-(--sidebar-w) flex-col border-r border-border-subtle bg-surface-sunken"
  >
    <div class="flex h-(--topbar-h) shrink-0 items-center gap-2.5 px-3">
      <span class="min-w-0 flex-1 truncate text-sm font-semibold text-text-primary">{{ props.brandName }}</span>
      <button
        type="button"
        class="inline-flex size-8 shrink-0 items-center justify-center rounded-lg transition-colors"
        :class="searchModalOpen ? 'bg-surface-hover text-text-primary' : 'text-text-tertiary hover:bg-surface-hover hover:text-text-primary'"
        title="查询任务"
        aria-label="查询任务"
        @click="openTaskSearch"
      >
        <Search class="size-4" />
      </button>
      <button
        type="button"
        class="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-primary"
        title="收起左侧菜单"
        aria-label="收起左侧菜单"
        @click="emit('collapse')"
      >
        <PanelLeftClose class="size-4" />
      </button>
    </div>

    <div class="px-3 pb-1">
      <button
        type="button"
        class="flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm font-medium text-text-primary transition-colors hover:bg-surface-hover"
        :title="props.qaOnly ? '新建问答' : '新建任务'"
        @click="newChat"
      >
        <MessageCirclePlus class="size-4 shrink-0 text-text-secondary" />
        <span class="min-w-0 truncate">{{ props.qaOnly ? '新建问答' : '新建任务' }}</span>
      </button>
    </div>

    <nav v-if="!props.qaOnly" class="flex flex-col gap-0.5 px-3 pb-2">
      <button
        v-for="item in sidebarNavItems"
        :key="item.id"
        type="button"
        :disabled="!item.enabled"
        :title="item.enabled ? item.label : `${item.label}（敬请期待）`"
        :aria-current="isNavItemActive(item, route.path) ? 'page' : undefined"
        class="flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-sm transition-colors"
        :class="[ !item.enabled && 'cursor-not-allowed opacity-40', isNavItemActive(item, route.path)
            ? 'bg-accent-soft font-medium text-accent'
            : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary',
        ]"
        @click="item.enabled && go(item.to)"
      >
        <component :is="iconOf(item.icon)" class="size-4 shrink-0" />
        <span class="min-w-0 truncate">{{ item.label }}</span>
      </button>

      <button
        v-if="!props.qaOnly && !isGuestMode"
        type="button"
        class="flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-sm transition-colors"
        :class="route.path === '/my-files' ? 'bg-accent-soft font-medium text-accent' : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary'"
        @click="go('/my-files')"
      >
        <FolderOpen class="size-4 shrink-0" />
        <span class="min-w-0 truncate">个人文件库</span>
      </button>
    </nav>

    <div
      v-if="props.showConversations"
      ref="historyListRef"
      class="custom-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain pb-2"
      @scroll.passive="handleHistoryScroll"
    >
      <div class="flex items-center justify-between px-5 pb-1 pt-3">
        <button
          type="button"
          class="flex min-w-0 items-center gap-1 text-sm font-semibold text-text-tertiary"
          @click="taskExpanded = !taskExpanded"
        >
          <span class="truncate">{{ props.qaOnly ? '问答记录' : '任务' }} ({{ filteredConversations.length }})</span>
          <ChevronDown
            class="size-3.5 shrink-0 transition-transform"
            :class="taskExpanded && 'rotate-180'"
          />
        </button>
        <button
          v-if="isGuestMode && conversations.length > 0"
          type="button"
          class="text-xs text-text-tertiary transition-colors hover:text-rose-500"
          @click="handleClearAllGuest"
        >
          清空
        </button>
      </div>

      <div class="space-y-0.5 px-3 pb-2">
        <div v-if="loading && conversations.length === 0" class="space-y-1.5 py-1">
          <div v-for="i in 5" :key="i" class="skeleton-shimmer h-9 animate-pulse rounded-lg bg-surface-hover" />
        </div>

        <TransitionGroup name="list-fade" tag="div" class="relative space-y-0.5">
        <div
          v-for="conv in visibleConversations"
          :key="conv.threadId"
          class="group relative flex h-9 cursor-pointer items-center rounded-lg px-2.5 text-sm transition-colors active:scale-[0.99]"
          :class="route.params.threadId === conv.threadId ? 'bg-accent-soft font-medium text-accent' : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary'"
          @click="selectConversation(conv.threadId)"
        >
          <Pin
            v-if="conv.pinned"
            class="mr-1.5 size-3 shrink-0 text-accent"
            aria-hidden="true"
          />
          <span v-if="conv.pinned" class="sr-only">已置顶</span>
          <span class="min-w-0 flex-1 truncate">{{ conv.title || '新任务' }}</span>
          <time
            class="ml-2 shrink-0 text-xs text-text-tertiary transition-opacity duration-150 group-hover:opacity-0"
            :datetime="String(conv.updatedAt || conv.createdAt || '')"
          >
            {{ formatRelativeTime(conv.updatedAt || conv.createdAt) }}
          </time>

          <span
            class="pointer-events-none absolute right-1.5 flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity duration-150 group-hover:pointer-events-auto group-hover:opacity-100"
          >
            <button
              v-if="!isGuestMode"
              type="button"
              class="inline-flex size-6 items-center justify-center rounded text-text-tertiary transition-colors hover:text-accent"
              :title="conv.pinned ? '取消置顶' : '置顶'"
              :aria-label="conv.pinned ? `取消置顶：${conv.title || '新任务'}` : `置顶：${conv.title || '新任务'}`"
              @click="handleTogglePin($event, conv)"
            >
              <PinOff v-if="conv.pinned" class="size-3.5" aria-hidden="true" />
              <Pin v-else class="size-3.5" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="inline-flex size-6 items-center justify-center rounded text-text-tertiary transition-colors hover:text-accent"
              title="重命名"
              @click="handleRename($event, conv)"
            >
              <Pencil class="size-3.5" />
            </button>
            <button
              type="button"
              class="inline-flex size-6 items-center justify-center rounded text-text-tertiary transition-colors hover:text-rose-500"
              title="删除"
              @click="handleDelete($event, conv.threadId)"
            >
              <Trash2 class="size-3.5" />
            </button>
          </span>
        </div>
        </TransitionGroup>

        <button
          v-if="!taskExpanded && !searchQuery.trim() && taskRemainingCount > 0"
          type="button"
          class="mt-2 flex h-9 w-full items-center rounded-lg px-2.5 text-sm font-semibold text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-primary"
          @click="taskExpanded = true"
        >
          查看更多 ({{ taskRemainingCount }})
        </button>

        <p v-if="loadingMore" class="py-3 text-center text-xs text-text-tertiary">正在加载更多记录…</p>
        <p v-else-if="!loading && filteredConversations.length === 0" class="py-8 text-center text-xs text-text-tertiary">
          {{ searchQuery.trim() ? '没有匹配任务' : '暂无任务' }}
        </p>
      </div>

    </div>
    <div v-else class="flex-1" />

    <div class="shrink-0 border-t border-border-subtle p-3">
      <div class="flex items-center gap-2.5">
        <span
          class="flex size-8 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-semibold text-accent-fg"
        >
          {{ isGuestMode ? '访' : displayBadge }}
        </span>
        <span class="min-w-0 flex-1">
          <span class="block truncate text-sm font-medium text-text-primary">
            {{ isGuestMode ? '访客模式' : displayName || '未命名用户' }}
          </span>
          <span class="block truncate text-xs text-text-tertiary">
            {{ isGuestMode ? '本地存储' : '在线' }}
          </span>
        </span>
        <button
          v-if="!props.qaOnly"
          type="button"
          class="inline-flex size-8 shrink-0 items-center justify-center rounded-lg transition-colors hover:bg-surface-hover"
          :class="isSettingsActive ? 'text-accent' : 'text-text-tertiary hover:text-text-primary'"
          title="设置"
          @click="go('/settings')"
        >
          <Settings class="size-4" />
        </button>
        <button
          type="button"
          class="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-primary"
          :title="isGuestMode ? '登录' : '退出登录'"
          @click="emit('auth-action')"
        >
          <LogIn v-if="isGuestMode" class="size-4" />
          <LogOut v-else class="size-4" />
        </button>
      </div>
    </div>

    <Teleport to="body">
      <Transition name="pop">
        <div
          v-if="searchModalOpen"
          class="fixed inset-0 z-80 flex items-center justify-center bg-slate-950/20 px-4 py-8 backdrop-blur-md dark:bg-black/45"
          role="dialog"
          aria-modal="true"
          aria-label="搜索任务"
          @click.self="closeTaskSearch"
        >
          <section class="pop-panel flex max-h-[78vh] w-full max-w-2xl flex-col rounded-2xl border border-white/55 bg-surface/90 p-5 shadow-2xl backdrop-blur-2xl dark:border-white/10 dark:bg-surface/95">
            <div class="flex items-center gap-2">
              <label class="relative min-w-0 flex-1">
                <Search class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-text-tertiary" />
                <input
                  v-model="searchQuery"
                  type="search"
                  autofocus
                  class="h-11 w-full rounded-xl bg-surface-sunken pl-9 pr-3 text-base text-text-primary outline-none placeholder:text-text-tertiary"
                  placeholder="搜索任务"
                />
              </label>
              <button
                type="button"
                class="inline-flex size-9 shrink-0 items-center justify-center rounded-lg text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-primary"
                title="关闭"
                aria-label="关闭"
                @click="closeTaskSearch"
              >
                ×
              </button>
            </div>

            <p class="px-1 pb-2 pt-5 text-sm font-semibold text-text-tertiary">
              {{ searchQuery.trim() ? '搜索结果' : '最近任务' }}
            </p>
            <div class="custom-scrollbar min-h-0 overflow-y-auto pr-1">
              <button
                v-for="conv in filteredConversations"
                :key="conv.threadId"
                type="button"
                class="group flex h-10 w-full items-center gap-3 rounded-lg px-1.5 text-left text-sm transition-colors hover:bg-surface-hover"
                @click="selectConversation(conv.threadId)"
              >
                <span class="min-w-0 flex-1 truncate text-text-primary">{{ conv.title || '新任务' }}</span>
                <span class="inline-flex max-w-56 shrink-0 items-center gap-1 text-text-tertiary">
                  <Folder class="size-4 shrink-0" />
                  <span class="truncate">{{ formatSearchTime(conv.updatedAt || conv.createdAt) || '未知时间' }}</span>
                </span>
              </button>
              <p v-if="!loading && filteredConversations.length === 0" class="py-10 text-center text-sm text-text-tertiary">
                {{ searchQuery.trim() ? '没有匹配任务' : '暂无任务' }}
              </p>
            </div>
          </section>
        </div>
      </Transition>
    </Teleport>
  </aside>
</template>
