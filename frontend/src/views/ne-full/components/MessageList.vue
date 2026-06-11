<script setup lang="ts">
import { nextTick, ref, watch, onMounted, onUnmounted, computed } from 'vue';
import { type ChatMessage } from '@/api/agent-v2';
import type { ResearchPlanPayload } from '@/api/agent-v2';
import WorkflowTimeline from './WorkflowTimeline.vue';
import MarkdownRenderer from './MarkdownRenderer.vue';
import ResearchDraftCard from './ResearchDraftCard.vue';
import ReasoningBlock from './ReasoningBlock.vue';
import FollowUpSuggestions from './FollowUpSuggestions.vue';
import { authState } from '@/utils/request';

interface Props {
  messages: ChatMessage[];
  isBusy?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  isBusy: false,
});
const emit = defineEmits<{
  (e: 'edit-user-message', payload: { index: number; content: string }): void;
  (e: 'research-draft-submit', payload: {
    question: string;
    draftId: string;
    confirmationStatus: 'confirmed' | 'revised' | 'cancelled';
    researchPlan: ResearchPlanPayload;
  }): void;
  (e: 'select-suggestion', value: string): void;
}>();
const listRef = ref<HTMLElement | null>(null);
const copiedKey = ref<string | null>(null);

const questionSuggestions = [
  '高纯锗探测器是什么？',
  '中子探测器有哪些类型？',
  '电制冷高纯锗探测器选型要看哪些参数？',
  '核电子学系统的前端电路怎么设计？',
  '闪烁体探测器的工作原理是什么？',
  'γ射线谱仪的能量分辨率如何衡量？',
  '核脉冲信号的幅度甄别怎么做？',
  '多道分析器（MCA）怎么选型？',
  '核仪表中的死时间如何修正？',
  '半导体探测器与气体探测器如何选型？',
  '液体闪烁计数器适用于哪些场景？',
];

const isMobile = ref(false);

const updateScreenWidth = () => {
  if (typeof window !== 'undefined') {
    isMobile.value = window.innerWidth < 768;
  }
};

onMounted(() => {
  updateScreenWidth();
  window.addEventListener('resize', updateScreenWidth);
});

onUnmounted(() => {
  window.removeEventListener('resize', updateScreenWidth);
});

const displayedSuggestions = computed(() => {
  if (isMobile.value) {
    // Show only the first 4 suggestions on mobile screens to keep it clean
    return questionSuggestions.slice(0, 4);
  }
  return questionSuggestions;
});

const followUpEnabled = import.meta.env.VITE_FOLLOW_UP_SUGGESTIONS !== 'false';

const hasPendingResearchDraft = (msg: ChatMessage) => {
  const events = msg.processEvents || [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.kind !== 'research_draft_created' && event.kind !== 'research_draft_updated') continue;
    const status = String(event.metadata?.confirmation_status || 'pending').trim();
    return status === 'pending';
  }
  return false;
};

const hasClarificationPending = (msg: ChatMessage) => {
  const events = msg.processEvents || [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.kind === 'tool_end' && event.tool === 'ask_clarification') {
      return true;
    }
  }
  return false;
};

const shouldShowFollowUp = (msg: ChatMessage) => {
  if (!followUpEnabled) return false;
  if (msg.role !== 'assistant') return false;
  if (!msg.content?.trim()) return false;
  if (msg.isStreaming) return false;
  if (msg.suggestionsHidden) return false;
  if (hasPendingResearchDraft(msg)) return false;
  if (hasClarificationPending(msg)) return false;
  return Boolean(msg.followUpSuggestions?.length || msg.suggestionsLoading);
};

const copyText = async (content: string, key: string) => {
  const text = (content || '').trim();
  if (!text) return;

  try {
    await navigator.clipboard.writeText(text);
    copiedKey.value = key;
    setTimeout(() => {
      if (copiedKey.value === key) copiedKey.value = null;
    }, 1200);
  } catch (e) {
    console.error('Copy failed', e);
  }
};

const exportToPdf = (msg: ChatMessage) => {
  console.log('exportToPdf clicked for message ID:', msg.id);
  try {
    const elementId = `msg-content-${msg.id}`;
    const element = document.getElementById(elementId);
    if (!element) {
      console.error('Cannot find element to export:', elementId);
      alert('抱歉，未能定位到回答内容，请重试！');
      return;
    }

    // 1. 创建用于打印的隐式 iframe
    const iframe = document.createElement('iframe');
    iframe.style.position = 'fixed';
    iframe.style.right = '0';
    iframe.style.bottom = '0';
    iframe.style.width = '0';
    iframe.style.height = '0';
    iframe.style.border = '0';
    document.body.appendChild(iframe);

    const iframeDoc = iframe.contentWindow?.document;
    if (!iframeDoc) {
      throw new Error('无法初始化打印框架');
    }

    // 2. 完美复制当前页面的所有样式标签与外部样式链接，包含 KaTeX 和 Tailwind 样式
    let stylesHtml = '';
    document.querySelectorAll('link[rel="stylesheet"], style').forEach(el => {
      stylesHtml += el.outerHTML + '\n';
    });
    // 注入公网 KaTeX 样式作为兜底双保险
    stylesHtml += `<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.css">\n`;

    // 3. 构建高保真打印 HTML 结构
    iframeDoc.write(`
      <!DOCTYPE html>
      <html>
        <head>
          <meta charset="utf-8">
          <title>导出回答 - 核电子学智能助手</title>
          ${stylesHtml}
          <style>
            body {
              font-family: 'Microsoft YaHei', SimSun, sans-serif !important;
              background-color: white !important;
              color: black !important;
              padding: 2.5cm 2cm !important;
              line-height: 1.8 !important;
            }
            .academic-container {
              max-width: 100% !important;
              margin: 0 !important;
              padding: 0 !important;
            }
            /* 完美分页防截断优化 */
            table, tr, img, .math-block, blockquote, pre {
              page-break-inside: avoid !important;
              break-inside: avoid !important;
            }
            @media print {
              body {
                padding: 1.5cm 1.2cm !important;
              }
              /* 隐藏打印时的页眉页脚（部分浏览器支持） */
              @page {
                size: A4 portrait;
                margin: 1.5cm 1.5cm;
              }
            }
          </style>
        </head>
        <body>
          <div class="academic-container prose dark:prose-invert">
            ${element.innerHTML}
          </div>
          <script>
            window.onload = function() {
              setTimeout(function() {
                window.print();
                // 打印完毕后自动销毁临时框架
                setTimeout(function() {
                  window.frameElement.remove();
                }, 1000);
              }, 500);
            };
          <\/script>
        </body>
      </html>
    `);
    iframeDoc.close();
    console.log('Vector PDF print window triggered successfully.');
  } catch (error) {
    console.error('PDF export failed:', error);
    alert(`PDF 导出失败，详情: ${error}`);
  }
};

const exportToDocx = async (msg: ChatMessage) => {
  console.log('exportToDocx clicked for message ID:', msg.id);
  try {
    const rawMarkdown = msg.content || '';
    const title = '智能助手回答报告';

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (authState.value?.token) {
      headers['Authorization'] = `Bearer ${authState.value.token}`;
    }

    const response = await fetch('/api/v2/files/export-docx', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        title,
        content: rawMarkdown
      })
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    
    // 自动从 Content-Disposition 响应头里提取后端生成的文件名（保持中文不乱码）
    const disposition = response.headers.get('Content-Disposition');
    let filename = `export-answer-${msg.id.slice(0, 8)}.docx`;
    if (disposition && disposition.includes('filename*=')) {
      const match = disposition.match(/filename\*=UTF-8''(.+)/);
      if (match && match[1]) {
        filename = decodeURIComponent(match[1]);
      }
    }
    
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    console.error('Word export failed:', error);
    alert(`Word 导出失败，请重试！错误详情: ${error}`);
  }
};


const isAtBottom = ref(true);

const handleScroll = () => {
  if (listRef.value) {
    const { scrollHeight, scrollTop, clientHeight } = listRef.value;
    // 允许 100px 的误差范围，判定为“在底部”
    isAtBottom.value = scrollHeight - scrollTop - clientHeight < 100;
  }
};

const scrollToBottom = async () => {
  await nextTick();
  if (listRef.value && isAtBottom.value) {
    listRef.value.scrollTop = listRef.value.scrollHeight;
  }
};

watch(() => props.messages, scrollToBottom, { deep: true });
</script>

<template>
  <div 
    ref="listRef" 
    class="flex-1 min-h-0 overflow-y-auto w-full custom-scrollbar"
    :class="messages.length === 0 ? 'flex flex-col' : ''"
    @scroll="handleScroll"
  >
    <TransitionGroup 
      name="list" 
      tag="div"
      class="w-full max-w-4xl mx-auto px-3 md:px-4 flex flex-col"
      :class="messages.length === 0
        ? 'flex-1 min-h-0'
        : 'py-4 md:py-12 space-y-4 md:space-y-12'"
    >
      <div 
        v-for="(msg, index) in messages" 
        :key="msg.id" 
        class="flex flex-col animate-in fade-in slide-in-from-bottom-2 duration-300 transform transition-all"
      :class="[msg.role === 'user' ? 'items-end' : 'items-start']"
    >
      <!-- User Message -->
      <div v-if="msg.role === 'user'" class="max-w-[85%]">
        <div class="bg-indigo-600 text-white px-6 py-3.5 rounded-2xl rounded-tr-none shadow-lg shadow-indigo-100/50 dark:shadow-none">
          <p class="text-[15px] font-medium leading-relaxed whitespace-pre-wrap">{{ msg.content }}</p>
        </div>
        <div class="mt-2 flex justify-end gap-1">
          <button
            type="button"
            class="inline-flex h-7 w-7 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-500 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            title="复制问题"
            @click="copyText(msg.content, `${msg.id}-question`)"
          >
            <svg v-if="copiedKey === `${msg.id}-question`" xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6 9 17l-5-5"/>
            </svg>
            <svg v-else xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
          </button>
          <button
            type="button"
            class="inline-flex h-7 w-7 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-500 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            title="编辑后重发"
            @click="emit('edit-user-message', { index, content: msg.content })"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 1 1 3 3L7 19l-4 1 1-4Z"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- Assistant Message -->
      <div v-else class="w-full space-y-5">
        <ResearchDraftCard
          :message="msg"
          :disabled="msg.isStreaming"
          @submit="(payload) => emit('research-draft-submit', payload)"
        />

        <WorkflowTimeline
          v-if="(msg.processEvents && msg.processEvents.length > 0) || (msg.statusHistory && msg.statusHistory.length > 0)"
          :message="msg"
        />

        <ReasoningBlock
          v-if="msg.reasoning"
          :reasoning="msg.reasoning"
          :is-streaming="msg.isStreaming"
          :has-answer="Boolean(msg.content)"
        />

        <div 
          v-if="msg.content" 
          class="overflow-hidden rounded-[32px] border border-slate-200/80 bg-white/90 shadow-[0_18px_60px_-32px_rgba(15,23,42,0.28)] backdrop-blur-sm transition-all hover:shadow-[0_22px_72px_-28px_rgba(15,23,42,0.34)] dark:border-slate-800/80 dark:bg-slate-900/60 dark:hover:shadow-none"
        >
          <div class="flex items-center justify-between border-b border-slate-200/70 px-6 py-4 dark:border-slate-800/70">
            <div class="space-y-1">
              <p class="text-[11px] font-semibold uppercase tracking-[0.32em] text-slate-400 dark:text-slate-500">最终输出</p>
              <h3 class="text-sm font-semibold text-slate-900 dark:text-slate-100">回答内容</h3>
            </div>
            <div 
              v-if="msg.isStreaming"
              class="inline-flex items-center gap-2 rounded-full border border-indigo-200/70 bg-indigo-50 px-2.5 py-1 text-[11px] font-medium text-indigo-600 dark:border-indigo-800/60 dark:bg-indigo-950/30 dark:text-indigo-300"
            >
              <div class="h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-500"></div>
              流式输出中
            </div>
          </div>
          <div :id="`msg-content-${msg.id}`" class="px-4 py-5 md:px-8 md:py-8 overflow-x-hidden bg-white dark:bg-slate-900">
            <MarkdownRenderer :content="msg.content" :is-streaming="msg.isStreaming" />
          </div>
        </div>

        <div v-if="msg.content" class="-mt-2 flex justify-end gap-1.5">
          <!-- 复制按钮 -->
          <button
            type="button"
            class="inline-flex h-7 w-7 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-500 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            title="复制回答"
            @click="copyText(msg.content, `${msg.id}-answer`)"
          >
            <svg v-if="copiedKey === `${msg.id}-answer`" xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6 9 17l-5-5"/>
            </svg>
            <svg v-else xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
          </button>

          <!-- 导出 PDF -->
          <button
            v-if="!msg.isStreaming"
            type="button"
            class="inline-flex h-7 w-7 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-500 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            title="导出为 PDF"
            @click="exportToPdf(msg)"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
              <polyline points="10 9 9 9 8 9"/>
            </svg>
          </button>

          <!-- 导出 Word -->
          <button
            v-if="!msg.isStreaming"
            type="button"
            class="inline-flex h-7 w-7 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-500 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            title="导出为 Word"
            @click="exportToDocx(msg)"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/>
              <polyline points="14 2 14 8 20 8"/>
              <path d="M8 13h2v4H8z"/>
              <path d="M12 13h4"/>
              <path d="M12 17h4"/>
            </svg>
          </button>
        </div>

        <FollowUpSuggestions
          v-if="shouldShowFollowUp(msg)"
          :suggestions="msg.followUpSuggestions || []"
          :loading="Boolean(msg.suggestionsLoading)"
          :disabled="isBusy"
          @select="(text) => emit('select-suggestion', text)"
        />

        <!-- Streaming Indicator -->
        <div v-if="msg.isStreaming && !msg.content" class="flex items-center space-x-2 pl-8 opacity-40">
          <div class="h-1.5 w-1.5 bg-indigo-400 rounded-full animate-bounce [animation-delay:0ms]"></div>
          <div class="h-1.5 w-1.5 bg-indigo-400 rounded-full animate-bounce [animation-delay:200ms]"></div>
          <div class="h-1.5 w-1.5 bg-indigo-400 rounded-full animate-bounce [animation-delay:400ms]"></div>
        </div>
      </div>
    </div>

    <!-- Empty State：品牌 logo + 豆包式胶囊云布局 -->
    <div
      v-if="messages.length === 0"
      class="flex w-full flex-1 flex-col items-center justify-center gap-4 pb-6 text-center md:gap-6 md:pb-0"
    >
      <div class="flex w-full max-w-md justify-center px-4 pt-1 md:max-w-2xl">
        <img
          src="/log.png"
          alt="核电子学智能体"
          class="mx-auto max-h-[18vh] w-auto max-w-[min(200px,50vw)] object-contain md:max-h-none md:w-full md:max-w-xl mix-blend-multiply contrast-[1.05] brightness-[1.05] dark:invert dark:mix-blend-screen"
        />
      </div>

      <h2 class="px-4 text-lg font-semibold tracking-tight text-slate-900 md:text-2xl dark:text-slate-100">
        你好，我是核电子学智能助手
      </h2>

      <div class="w-full max-w-3xl px-4 md:max-w-4xl md:px-6">
        <div class="flex flex-wrap justify-center gap-2.5 md:gap-3">
          <button
            v-for="suggestion in displayedSuggestions"
            :key="suggestion"
            type="button"
            :disabled="isBusy"
            class="rounded-xl bg-slate-100 px-3.5 py-2 text-[12px] leading-snug text-slate-800 transition-colors hover:bg-slate-200 disabled:cursor-not-allowed disabled:opacity-50 md:rounded-2xl md:px-5 md:py-3 md:text-[14px] dark:bg-slate-800/80 dark:text-slate-200 dark:hover:bg-slate-700"
            @click="emit('select-suggestion', suggestion)"
          >
            {{ suggestion }}
          </button>
        </div>
      </div>
    </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.list-enter-active,
.list-leave-active {
  transition: all 0.4s ease;
}
.list-enter-from {
  opacity: 0;
  transform: translateY(20px);
}
.list-leave-to {
  opacity: 0;
  transform: scale(0.95);
}
</style>
