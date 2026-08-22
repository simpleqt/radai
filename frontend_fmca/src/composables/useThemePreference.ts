import { computed, ref } from 'vue';

export type ThemePreference = 'system' | 'light' | 'dark';

const THEME_KEY = 'theme';

const isThemePreference = (value: string | null): value is ThemePreference =>
  value === 'system' || value === 'light' || value === 'dark';

const readTheme = (): ThemePreference => {
  const stored = localStorage.getItem(THEME_KEY);
  return isThemePreference(stored) ? stored : 'system';
};

const prefersDark = () =>
  typeof window !== 'undefined'
  && window.matchMedia('(prefers-color-scheme: dark)').matches;

export const applyThemePreference = (preference: ThemePreference) => {
  const dark = preference === 'dark' || (preference === 'system' && prefersDark());
  const root = document.documentElement;
  if (root.classList.contains('dark') === dark) return;

  // 深浅色切换用 View Transition 整页交叉淡入；不支持或用户偏好减少动效时直接切换
  const startViewTransition = (document as Document & {
    startViewTransition?: (callback: () => void) => unknown;
  }).startViewTransition?.bind(document);
  const reducedMotion = typeof window !== 'undefined'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  if (!startViewTransition || reducedMotion) {
    root.classList.toggle('dark', dark);
    return;
  }
  startViewTransition(() => {
    root.classList.toggle('dark', dark);
  });
};

export const useThemePreference = () => {
  const theme = ref<ThemePreference>(readTheme());

  const effectiveTheme = computed<'light' | 'dark'>(() => {
    if (theme.value === 'system') return prefersDark() ? 'dark' : 'light';
    return theme.value;
  });

  const setTheme = (next: ThemePreference) => {
    theme.value = next;
    localStorage.setItem(THEME_KEY, next);
    applyThemePreference(next);
  };

  applyThemePreference(theme.value);

  return {
    theme,
    effectiveTheme,
    setTheme,
  };
};
