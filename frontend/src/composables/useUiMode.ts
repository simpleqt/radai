import { computed, ref } from 'vue';
import { useDeviceProfile } from './useDeviceProfile';

export type UiMode = 'auto' | 'mobile' | 'desktop';

const UI_MODE_KEY = 'ui_mode';

const readUiMode = (): UiMode => {
  const val = (localStorage.getItem(UI_MODE_KEY) || 'auto').toLowerCase();
  if (val === 'mobile' || val === 'desktop' || val === 'auto') {
    return val;
  }
  return 'auto';
};

export const useUiMode = () => {
  const mode = ref<UiMode>(readUiMode());
  const { detectMode } = useDeviceProfile();

  const effectiveMode = computed<'mobile' | 'desktop'>(() => {
    if (mode.value === 'auto') return detectMode();
    return mode.value;
  });

  const setMode = (next: UiMode) => {
    mode.value = next;
    localStorage.setItem(UI_MODE_KEY, next);
  };

  return {
    mode,
    effectiveMode,
    setMode,
  };
};
