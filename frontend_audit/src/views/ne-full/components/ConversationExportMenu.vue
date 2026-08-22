<script setup lang="ts">
/**
 * 整会话导出入口（顶栏）：Markdown / Word 两种格式。
 *
 * 只在会话里已经有可导出内容时显示；导出中禁用重复点击，失败在菜单内就地提示
 * 而不是弹 alert。截断（Word 超 20 万字符）也在菜单内说明。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { Download } from 'lucide-vue-next';
import {
  exportConversationDocx,
  exportConversationMarkdown,
  type ExportMessage,
} from '../composables/useConversationExport';

const props = withDefaults(defineProps<{
  messages: ExportMessage[];
  agentName?: string;
  title?: string;
  disabled?: boolean;
}>(), {
  agentName: '',
  title: '',
  disabled: false,
});

const open = ref(false);
const busy = ref<'md' | 'docx' | null>(null);
const message = ref('');
let messageTimer: ReturnType<typeof setTimeout> | null = null;

const hasContent = computed(() =>
  (props.messages || []).some(
    (item) => String(item?.role || '').toLowerCase() === 'assistant'
      && String(item?.content || '').trim(),
  ),
);

const clearMessageTimer = () => {
  if (messageTimer) {
    clearTimeout(messageTimer);
    messageTimer = null;
  }
};

const flashMessage = (text: string) => {
  message.value = text;
  clearMessageTimer();
  messageTimer = setTimeout(() => {
    message.value = '';
    messageTimer = null;
  }, 6000);
};

const meta = () => ({ agentName: props.agentName, title: props.title });

const runMarkdown = () => {
  if (busy.value) return;
  busy.value = 'md';
  try {
    exportConversationMarkdown(props.messages || [], meta());
    open.value = false;
  } catch (error) {
    console.error('[ConversationExportMenu] Markdown 导出失败', error);
    flashMessage(error instanceof Error ? error.message : 'Markdown 导出失败');
  } finally {
    busy.value = null;
  }
};

const runDocx = async () => {
  if (busy.value) return;
  busy.value = 'docx';
  try {
    const built = await exportConversationDocx(props.messages || [], meta());
    if (built.droppedTurns > 0) {
      flashMessage(`已导出，因篇幅限制省略了最早 ${built.droppedTurns} 轮问答`);
    } else {
      open.value = false;
    }
  } catch (error) {
    console.error('[ConversationExportMenu] Word 导出失败', error);
    flashMessage(error instanceof Error ? error.message : 'Word 导出失败');
  } finally {
    busy.value = null;
  }
};

const onKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') open.value = false;
};

watch(open, (isOpen, wasOpen) => {
  if (isOpen && !wasOpen) document.addEventListener('keydown', onKeydown);
  else if (!isOpen && wasOpen) {
    document.removeEventListener('keydown', onKeydown);
    message.value = '';
    clearMessageTimer();
  }
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
  clearMessageTimer();
});
</script>

<template>
  <div v-if="hasContent" class="relative">
    <button
      type="button"
      class="inline-flex size-8 items-center justify-center rounded-lg transition-colors hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50"
      :class="open ? 'text-accent' : 'text-text-tertiary hover:text-text-primary'"
      title="导出整个会话"
      aria-label="导出整个会话"
      :aria-expanded="open"
      :disabled="disabled"
      @click="open = !open"
    >
      <Download class="size-4.5" />
    </button>

    <Teleport to="body">
      <div v-if="open" class="fixed inset-0 z-70" @click="open = false">
        <div
          class="absolute right-4 top-14 w-60 rounded-xl border border-border-subtle bg-surface p-1.5 shadow-lg"
          role="menu"
          aria-label="导出整个会话"
          @click.stop
        >
          <button
            type="button"
            role="menuitem"
            class="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-sm text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="Boolean(busy)"
            @click="runMarkdown"
          >
            <span class="min-w-0 flex-1 truncate">导出为 Markdown</span>
            <span class="shrink-0 text-xs text-text-tertiary">.md</span>
          </button>
          <button
            type="button"
            role="menuitem"
            class="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-sm text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="Boolean(busy)"
            @click="runDocx"
          >
            <span class="min-w-0 flex-1 truncate">
              {{ busy === 'docx' ? '正在生成 Word…' : '导出为 Word' }}
            </span>
            <span class="shrink-0 text-xs text-text-tertiary">.docx</span>
          </button>
          <p v-if="message" class="px-2.5 py-1.5 text-xs leading-relaxed text-text-tertiary">
            {{ message }}
          </p>
        </div>
      </div>
    </Teleport>
  </div>
</template>
