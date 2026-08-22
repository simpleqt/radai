import { onBeforeUnmount, readonly, ref } from 'vue';

/**
 * 响应式媒体查询。用 matchMedia 而非 resize 监听，
 * 只在断点跨越时触发一次，不会逐帧刷新。
 */
export const useMediaQuery = (query: string) => {
  const supported = typeof window !== 'undefined' && typeof window.matchMedia === 'function';
  const matches = ref(supported ? window.matchMedia(query).matches : false);

  if (supported) {
    const mql = window.matchMedia(query);
    const onChange = (event: MediaQueryListEvent) => {
      matches.value = event.matches;
    };
    mql.addEventListener('change', onChange);
    onBeforeUnmount(() => mql.removeEventListener('change', onChange));
  }

  return readonly(matches);
};

/** 与 Tailwind lg 断点一致 */
export const useIsDesktop = () => useMediaQuery('(min-width: 64rem)');
