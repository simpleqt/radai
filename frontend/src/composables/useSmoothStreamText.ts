import { onBeforeUnmount, ref, watch } from 'vue';

/** 基准展示速率（约 42 字/秒） */
const BASE_CHARS_PER_MS = 0.042;
/** 积压超过该字数时加速追赶 */
const CATCHUP_CHAR_THRESHOLD = 72;
/** 流式结束后的收尾速率（约 180 字/秒） */
const FLUSH_CHARS_PER_MS = 0.18;
const MIN_CHARS_PER_MS = 0.01;
const MAX_CHARS_PER_MS = 0.2;

/**
 * 将上游可能「突发」的流式文本，以稳定速率渲染到 UI。
 * - 流式中：从空串开始匀速放出（避免组件晚挂载时一次性展示已积压正文）
 * - 流式结束：快速追平剩余内容
 * - 非流式（历史消息）：直接展示全文
 */
export function useSmoothStreamText(
  getTarget: () => string,
  getIsStreaming: () => boolean,
  getEnabled: () => boolean = () => true,
) {
  const initialTarget = getTarget() || '';
  const shouldSmooth = getEnabled() && getIsStreaming();
  const displayText = ref(shouldSmooth ? '' : initialTarget);
  let displayLength = displayText.value.length;
  let animationFrameId: number | null = null;
  let lastFrameAt = 0;

  const stopAnimation = () => {
    if (animationFrameId !== null) {
      cancelAnimationFrame(animationFrameId);
      animationFrameId = null;
    }
    lastFrameAt = 0;
  };

  const syncToTarget = (target: string) => {
    stopAnimation();
    displayLength = target.length;
    displayText.value = target;
  };

  const animate = (now: number) => {
    const target = getTarget() || '';
    const targetLength = target.length;
    const streaming = getIsStreaming();
    const remain = targetLength - displayLength;

    if (remain <= 0) {
      displayLength = targetLength;
      displayText.value = target;
      animationFrameId = null;
      lastFrameAt = 0;
      return;
    }

    const dtMs = lastFrameAt ? Math.min(Math.max(now - lastFrameAt, 8), 48) : 16;
    lastFrameAt = now;

    let charsPerMs = BASE_CHARS_PER_MS;
    if (!streaming) {
      charsPerMs = FLUSH_CHARS_PER_MS;
    } else if (remain > CATCHUP_CHAR_THRESHOLD) {
      const boost = 1 + Math.min((remain - CATCHUP_CHAR_THRESHOLD) / CATCHUP_CHAR_THRESHOLD, 2.2);
      charsPerMs = Math.min(MAX_CHARS_PER_MS, BASE_CHARS_PER_MS * boost);
    }

    charsPerMs = Math.max(charsPerMs, MIN_CHARS_PER_MS);

    let step = charsPerMs * dtMs;
    step = Math.max(step, MIN_CHARS_PER_MS * dtMs);
    step = Math.min(step, remain);

    displayLength += step;
    if (displayLength > targetLength) {
      displayLength = targetLength;
    }

    displayText.value = target.slice(0, Math.floor(displayLength));

    animationFrameId = requestAnimationFrame(animate);
  };

  const scheduleAnimation = () => {
    if (animationFrameId === null) {
      lastFrameAt = 0;
      animationFrameId = requestAnimationFrame(animate);
    }
  };

  watch(
    () => [getTarget(), getIsStreaming(), getEnabled()] as const,
    ([target, streaming, enabled], prev) => {
      target = target || '';
      const smoothActive = Boolean(enabled && streaming);
      const wasSmoothActive = Boolean(prev && prev[2] && prev[1]);

      if (!smoothActive) {
        syncToTarget(target);
        return;
      }

      // 进入流式平滑：若此前未在平滑（含组件晚挂载、首段正文已到达），从空串开始放出
      if (!wasSmoothActive && displayLength > 0 && displayLength >= target.length) {
        displayLength = 0;
        displayText.value = '';
      }

      if (displayLength > target.length) {
        displayLength = target.length;
        displayText.value = target;
      }

      if (target.length > displayLength) {
        scheduleAnimation();
      }
    },
    { immediate: true },
  );

  onBeforeUnmount(stopAnimation);

  return { displayText };
}
