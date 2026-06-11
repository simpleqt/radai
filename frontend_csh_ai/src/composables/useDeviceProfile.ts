import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

const MOBILE_WIDTH = 900;

const testMobileUa = (ua: string) => /android|iphone|ipad|ipod|mobile|miui|harmonyos/i.test(ua);

export const useDeviceProfile = () => {
  const getWidth = () => (typeof window !== 'undefined' ? window.innerWidth : 1280);
  const getUa = () => (typeof navigator !== 'undefined' ? navigator.userAgent : '');
  const width = ref(getWidth());
  const uaMobile = testMobileUa(getUa());

  const updateWidth = () => {
    width.value = getWidth();
  };

  onMounted(() => {
    if (typeof window === 'undefined') return;
    window.addEventListener('resize', updateWidth);
  });

  onBeforeUnmount(() => {
    if (typeof window === 'undefined') return;
    window.removeEventListener('resize', updateWidth);
  });

  const isMobile = computed(() => {
    const widthMobile = width.value <= MOBILE_WIDTH;
    return uaMobile || widthMobile;
  });

  return {
    isMobile,
    detectMode: () => (isMobile.value ? 'mobile' : 'desktop') as 'mobile' | 'desktop',
  };
};
