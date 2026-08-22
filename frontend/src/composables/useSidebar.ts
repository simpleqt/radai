import { computed, onBeforeUnmount, ref } from 'vue';

export type SidebarState = 'expanded' | 'collapsed';

const SIDEBAR_KEY = 'sidebar_state';
/** 与 Tailwind lg 断点一致：≥1024px 视为可容纳并排侧栏 */
const DOCKED_QUERY = '(min-width: 64rem)';

const isSidebarState = (value: string | null): value is SidebarState =>
  value === 'expanded' || value === 'collapsed';

const readStored = (): SidebarState | null => {
  try {
    const raw = localStorage.getItem(SIDEBAR_KEY);
    return isSidebarState(raw) ? raw : null;
  } catch {
    return null;
  }
};

const matchDocked = () =>
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(DOCKED_QUERY).matches
    : true;

/** 模块级单例，保证 AppShell 与各视图看到同一份状态 */
const isDocked = ref(matchDocked());
const userChoice = ref<SidebarState | null>(readStored());
const autoState = ref<SidebarState>(isDocked.value ? 'expanded' : 'collapsed');

let mediaQuery: MediaQueryList | null = null;
let listenerCount = 0;

const handleChange = (event: MediaQueryListEvent) => {
  isDocked.value = event.matches;
  autoState.value = event.matches ? 'expanded' : 'collapsed';
};

export const useSidebar = () => {
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    if (!mediaQuery) {
      mediaQuery = window.matchMedia(DOCKED_QUERY);
      mediaQuery.addEventListener('change', handleChange);
    }
    listenerCount += 1;
    onBeforeUnmount(() => {
      listenerCount -= 1;
      if (listenerCount <= 0 && mediaQuery) {
        mediaQuery.removeEventListener('change', handleChange);
        mediaQuery = null;
        listenerCount = 0;
      }
    });
  }

  /** overlay 态下不沿用用户在桌面宽度选择的展开偏好，避免抽屉默认盖住内容 */
  const state = computed<SidebarState>(() => {
    if (!isDocked.value) return autoState.value;
    return userChoice.value ?? autoState.value;
  });

  const isOverlay = computed(() => !isDocked.value);

  const setState = (next: SidebarState) => {
    autoState.value = next;
    if (isDocked.value) {
      userChoice.value = next;
      try {
        localStorage.setItem(SIDEBAR_KEY, next);
      } catch {
        /* 隐私模式下忽略持久化失败 */
      }
    }
  };

  const toggle = () => setState(state.value === 'expanded' ? 'collapsed' : 'expanded');
  const open = () => setState('expanded');
  const close = () => setState('collapsed');

  return { state, isDocked, isOverlay, toggle, open, close };
};
