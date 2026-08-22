<script setup lang="ts">
import type { FileAttachment } from '../composables/useFileUpload';

interface Props {
  attachments: FileAttachment[];
  disabled?: boolean;
}

const props = withDefaults(defineProps<Props>(), { disabled: false });
const emit = defineEmits<{ (e: 'remove', id: string): void }>();

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

function categoryIcon(att: FileAttachment): string {
  if (att.category === 'multimodal') return '🖼️';
  if (att.category === 'short_doc') return '📄';
  if (att.category === 'long_doc') return '📚';
  if (att.category === 'failed') return '❌';
  return '📎';
}

function strategyLabel(att: FileAttachment): string {
  if (att.status === 'uploading') return `上传中 ${att.progress}%`;
  if (att.status === 'error') return att.errorMessage || '处理失败';
  if (att.tableAnalysisEnabled) return '可分析表格';
  const map: Record<string, string> = {
    vision: '图片分析',
    context: `上下文注入${att.contentLength ? ' · ' + att.contentLength + '字' : ''}`,
    indexed: `已建索引${att.contentLength ? ' · ' + att.contentLength + '字' : ''} · 多轮可用`,
    none: '就绪',
  };
  return map[att.strategy] || '就绪';
}

function chipColor(att: FileAttachment): string {
  if (att.status === 'error') return 'border-red-200 bg-red-50 dark:border-red-800 dark:bg-red-900/20';
  if (att.status === 'uploading') return 'border-amber-200 bg-amber-50 dark:border-amber-700 dark:bg-amber-900/20';
  if (att.status === 'ready') {
    if (att.strategy === 'vision') return 'border-accent/30 bg-accent-soft';
    if (att.strategy === 'indexed') return 'border-blue-200 bg-blue-50 dark:border-blue-700 dark:bg-blue-900/20';
    return 'border-emerald-200 bg-emerald-50 dark:border-emerald-700 dark:bg-emerald-900/20';
  }
  return 'border-border-subtle bg-surface-sunken';
}
</script>

<template>
  <TransitionGroup
    v-if="attachments.length > 0"
    name="chip"
    tag="div"
    class="relative flex flex-wrap gap-2 px-4 pt-2 pb-1"
  >
    <div
      v-for="att in attachments"
      :key="att.id"
      class="group relative flex items-center gap-2 rounded-xl border px-2.5 py-1.5 text-xs transition-all max-w-[200px]"
      :class="chipColor(att)"
    >
      <!-- 图片缩略图 -->
      <img
        v-if="att.previewUrl && att.category === 'multimodal'"
        :src="att.previewUrl"
        class="h-7 w-7 rounded-lg object-cover flex-shrink-0"
        :alt="att.name"
      />
      <!-- 文件图标 -->
      <span v-else class="text-base flex-shrink-0">{{ categoryIcon(att) }}</span>

      <!-- 文件信息 -->
      <div class="min-w-0 flex-1">
        <div class="font-medium text-text-secondary truncate leading-tight">
          {{ att.name }}
        </div>
        <div class="text-[10px] leading-tight mt-0.5"
          :class="att.status === 'error' ? 'text-red-500'
            : att.status === 'uploading'
            ? 'text-amber-600 dark:text-amber-400'
            : 'text-text-tertiary'"
        >
          {{ strategyLabel(att) }}
          <span v-if="att.status !== 'uploading' && att.status !== 'error'" class="opacity-60"> · {{ formatSize(att.size) }}</span>
        </div>
        <!-- 上传进度条 -->
        <div v-if="att.status === 'uploading'" class="mt-1 h-0.5 w-full rounded-full bg-amber-200 dark:bg-amber-800">
          <div
            class="h-full rounded-full bg-amber-500 transition-all duration-300"
            :style="{ width: att.progress + '%' }"
          ></div>
        </div>
      </div>

      <!-- 删除按钮 -->
      <button
        v-if="!disabled && att.status !== 'uploading'"
        @click="emit('remove', att.id)"
        class="flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity p-0.5 rounded-full hover:bg-surface-hover text-text-tertiary hover:text-text-secondary"
        title="移除附件"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>
  </TransitionGroup>
</template>

<style scoped>
.chip-enter-active {
  transition: opacity 200ms ease, transform 200ms cubic-bezier(0.34, 1.3, 0.64, 1);
}
.chip-leave-active {
  /* 离场项原地脱离布局，兄弟 chip 由 move 过渡平滑补位 */
  position: absolute;
  transition: opacity 150ms ease, transform 150ms ease;
}
.chip-move {
  transition: transform 250ms ease;
}
.chip-enter-from {
  opacity: 0;
  transform: translateY(6px) scale(0.9);
}
.chip-leave-to {
  opacity: 0;
  transform: scale(0.85);
}
</style>
