<script setup lang="ts">
/**
 * 单条回答的赞/踩反馈控件（仅登录态渲染）。
 *
 * 点赞立即提交；点踩立即提交并展开可选的原因补充面板（再次提交覆盖）。
 * 再次点击同一评价撤回。`messageKey` 由 `feedbackKeysForMessages` 派生，
 * 跨刷新稳定，所以状态经 FeedbackAPI 的会话级缓存恢复后仍可见。
 * 提交失败会就地提示（请求层已统一处理会话过期跳登录）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { FeedbackAPI, type FeedbackRating } from '@/api/feedback';
import { authState } from '@/utils/request';

const props = withDefaults(defineProps<{
  threadId?: string;
  agentCode?: string;
  messageKey?: string;
  question?: string;
  answer?: string;
  modelName?: string;
  disabled?: boolean;
}>(), {
  threadId: '',
  agentCode: '',
  messageKey: '',
  question: '',
  answer: '',
  modelName: '',
  disabled: false,
});

const DOWN_REASONS = ['答非所问', '内容不准确', '引用有误', '过于冗长', '响应太慢', '格式混乱'];

const rating = ref<FeedbackRating | ''>('');
const showReasonPanel = ref(false);
const selectedReasons = ref<string[]>([]);
const comment = ref('');
const submitting = ref(false);
const failed = ref(false);
const rootRef = ref<HTMLElement | null>(null);

const visible = computed(() => Boolean(authState.value.token && props.threadId && props.messageKey));

const restoreState = async () => {
  if (!visible.value) return;
  try {
    const map = await FeedbackAPI.loadThreadFeedback(props.threadId);
    const existing = map.get(props.messageKey);
    rating.value = existing?.rating || '';
    selectedReasons.value = Array.isArray(existing?.reasons) ? [...existing.reasons] : [];
    comment.value = existing?.comment || '';
  } catch {
    /* 反馈状态恢复失败不影响阅读 */
  }
};

onMounted(restoreState);
watch(() => [props.threadId, props.messageKey], restoreState);

const closePanel = () => {
  showReasonPanel.value = false;
};

// 面板是消息流内的浮层：Esc 与点击外部都要能关掉，否则滚动时会一直挡住正文。
const handleDocumentPointerDown = (event: MouseEvent) => {
  if (!showReasonPanel.value) return;
  const root = rootRef.value;
  if (root && !root.contains(event.target as Node)) closePanel();
};

const handleDocumentKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') closePanel();
};

watch(showReasonPanel, (open) => {
  if (typeof document === 'undefined') return;
  if (open) {
    document.addEventListener('mousedown', handleDocumentPointerDown);
    document.addEventListener('keydown', handleDocumentKeydown);
  } else {
    document.removeEventListener('mousedown', handleDocumentPointerDown);
    document.removeEventListener('keydown', handleDocumentKeydown);
  }
});

onBeforeUnmount(() => {
  if (typeof document === 'undefined') return;
  document.removeEventListener('mousedown', handleDocumentPointerDown);
  document.removeEventListener('keydown', handleDocumentKeydown);
});

const submit = async (nextRating: FeedbackRating, reasons?: string[], note?: string) => {
  submitting.value = true;
  failed.value = false;
  try {
    await FeedbackAPI.submit(props.threadId, {
      messageKey: props.messageKey,
      rating: nextRating,
      reasons,
      comment: note,
      agentCode: props.agentCode || undefined,
      modelName: props.modelName || undefined,
      questionExcerpt: props.question || undefined,
      answerExcerpt: props.answer || undefined,
    });
    rating.value = nextRating;
    return true;
  } catch (e) {
    failed.value = true;
    console.error('[FeedbackControls] submit failed', e);
    return false;
  } finally {
    submitting.value = false;
  }
};

const removeFeedback = async () => {
  submitting.value = true;
  failed.value = false;
  try {
    await FeedbackAPI.remove(props.threadId, props.messageKey);
    rating.value = '';
    selectedReasons.value = [];
    comment.value = '';
  } catch (e) {
    failed.value = true;
    console.error('[FeedbackControls] remove failed', e);
  } finally {
    submitting.value = false;
  }
};

const handleThumbUp = async () => {
  if (submitting.value || props.disabled) return;
  closePanel();
  if (rating.value === 'up') {
    await removeFeedback();
    return;
  }
  await submit('up');
};

const handleThumbDown = async () => {
  if (submitting.value || props.disabled) return;
  if (rating.value === 'down') {
    closePanel();
    await removeFeedback();
    return;
  }
  const ok = await submit('down', selectedReasons.value, comment.value);
  showReasonPanel.value = ok;
};

const toggleReason = (reason: string) => {
  const index = selectedReasons.value.indexOf(reason);
  if (index >= 0) selectedReasons.value.splice(index, 1);
  else selectedReasons.value.push(reason);
};

const submitReasons = async () => {
  const ok = await submit('down', selectedReasons.value, comment.value.trim());
  if (ok) closePanel();
};
</script>

<template>
  <div v-if="visible" ref="rootRef" class="relative inline-flex items-center gap-1.5">
    <button
      type="button"
      class="inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors hover:bg-surface-hover"
      :class="rating === 'up' ? 'text-accent' : 'text-text-tertiary hover:text-text-secondary'"
      :disabled="submitting || disabled"
      :title="rating === 'up' ? '取消点赞' : '答得好'"
      :aria-label="rating === 'up' ? '取消点赞' : '这条回答答得好'"
      :aria-pressed="rating === 'up'"
      @click="handleThumbUp"
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" :fill="rating === 'up' ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M7 10v12"/>
        <path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>
      </svg>
    </button>
    <button
      type="button"
      class="inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors hover:bg-surface-hover"
      :class="rating === 'down' ? 'text-rose-500' : 'text-text-tertiary hover:text-text-secondary'"
      :disabled="submitting || disabled"
      :title="rating === 'down' ? '取消点踩' : '有问题'"
      :aria-label="rating === 'down' ? '取消点踩' : '这条回答有问题'"
      :aria-pressed="rating === 'down'"
      @click="handleThumbDown"
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" :fill="rating === 'down' ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M17 14V2"/>
        <path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>
      </svg>
    </button>

    <span v-if="failed" class="text-xs text-rose-500" role="status">未保存，请重试</span>

    <Transition name="feedback-panel">
      <div
        v-if="showReasonPanel"
        class="absolute right-0 top-full z-20 mt-2 w-72 rounded-xl border border-border-subtle bg-surface p-3 shadow-lg"
        role="dialog"
        aria-label="反馈原因"
      >
        <p class="mb-2 text-xs font-medium text-text-secondary">哪里有问题？（可选，帮助我们改进）</p>
        <div class="mb-2 flex flex-wrap gap-1.5">
          <button
            v-for="reason in DOWN_REASONS"
            :key="reason"
            type="button"
            class="rounded-full border px-2.5 py-1 text-xs transition-colors"
            :class="selectedReasons.includes(reason)
              ? 'border-accent/60 bg-accent-soft text-accent'
              : 'border-border-subtle text-text-tertiary hover:border-border-strong hover:text-text-secondary'"
            :aria-pressed="selectedReasons.includes(reason)"
            @click="toggleReason(reason)"
          >
            {{ reason }}
          </button>
        </div>
        <textarea
          v-model="comment"
          rows="2"
          maxlength="2000"
          placeholder="补充说明（可选）"
          aria-label="补充说明"
          class="mb-2 w-full resize-none rounded-lg border border-border-subtle bg-transparent px-2.5 py-1.5 text-xs text-text-primary outline-none placeholder:text-text-tertiary focus:border-accent/60"
        />
        <div class="flex items-center justify-end gap-2">
          <button
            type="button"
            class="rounded-md px-2.5 py-1 text-xs text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-secondary"
            @click="closePanel"
          >
            跳过
          </button>
          <button
            type="button"
            class="rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-50"
            :disabled="submitting"
            @click="submitReasons"
          >
            提交
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.feedback-panel-enter-active,
.feedback-panel-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.feedback-panel-enter-from,
.feedback-panel-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
