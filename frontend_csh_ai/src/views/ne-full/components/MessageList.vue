<script setup lang="ts">
import { nextTick, ref, watch, onMounted, onUnmounted, computed, onBeforeUnmount } from 'vue';
import { type ChatMessage } from '@/api/agent-v2';
import type { ResearchPlanPayload } from '@/api/agent-v2';
import ProcessPanel from '@/features/process/ProcessPanel.vue';
import MarkdownRenderer from './MarkdownRenderer.vue';
import ResearchDraftCard from './ResearchDraftCard.vue';
import ReasoningBlock from './ReasoningBlock.vue';
import FollowUpSuggestions from './FollowUpSuggestions.vue';
import FeedbackControls from './FeedbackControls.vue';
import { feedbackKeysForMessages } from '@/api/feedback';
import { authState } from '@/utils/request';
import * as icons from 'lucide-vue-next';


interface Props {
  messages: ChatMessage[];
  isBusy?: boolean;
  threadId?: string;
  agentCode?: string;
  modelName?: string;
}

const props = withDefaults(defineProps<Props>(), {
  isBusy: false,
  threadId: '',
  agentCode: '',
  modelName: '',
});
const emit = defineEmits<{
  (e: 'edit-user-message', payload: { index: number; content: string }): void;
  (e: 'regenerate-answer', payload: { index: number }): void;
  (e: 'research-draft-submit', payload: {
    question: string;
    draftId: string;
    confirmationStatus: 'confirmed' | 'revised' | 'cancelled';
    researchPlan: ResearchPlanPayload;
  }): void;
  (e: 'select-suggestion', value: string): void;
  (e: 'process-action-send', value: string): void;
  (e: 'process-action-fill', value: string): void;
}>();

/** 该回答对应的用户问题（向前找最近一条 user 消息）。 */
const questionForIndex = (index: number): string => {
  for (let i = index - 1; i >= 0; i -= 1) {
    const msg = props.messages[i];
    if (msg?.role === 'user') return String(msg.content || '');
  }
  return '';
};

// 反馈键从问题摘要派生：消息 id 在直播态是客户端时间戳、刷新后是服务端
// uuid，用它当键会导致状态恢复不了并给同一条回答重复落行。
const feedbackKeys = computed(() => feedbackKeysForMessages(props.messages || []));

/** 该回答之后会被回滚一并删掉的用户轮次数（0 表示它就是最后一轮）。 */
const droppedTurnsAfter = (index: number): number => {
  let count = 0;
  for (let i = index + 1; i < props.messages.length; i += 1) {
    if (props.messages[i]?.role === 'user') count += 1;
  }
  return count;
};

const pendingRegenerateKey = ref<string | null>(null);
let pendingRegenerateTimer: ReturnType<typeof setTimeout> | null = null;

const resetPendingRegenerate = () => {
  pendingRegenerateKey.value = null;
  if (pendingRegenerateTimer) {
    clearTimeout(pendingRegenerateTimer);
    pendingRegenerateTimer = null;
  }
};

const regenerateHint = (index: number, messageId: string): string => {
  const dropped = droppedTurnsAfter(index);
  if (pendingRegenerateKey.value === messageId) {
    return `再次点击确认：这条之后的 ${dropped} 轮问答会被删除且无法恢复`;
  }
  return dropped > 0
    ? `重新生成（会删除之后的 ${dropped} 轮问答）`
    : '重新生成（可先切换模型/知识库后再点）';
};

/**
 * 重新生成会把该轮之后的问答一起回滚，服务端 checkpoint 同样截断且无法恢复。
 * 最后一轮直接执行；更早的回答需要两步确认，4 秒内不再点就自动取消。
 */
const requestRegenerate = (index: number, messageId: string) => {
  if (props.isBusy) return;
  if (droppedTurnsAfter(index) === 0 || pendingRegenerateKey.value === messageId) {
    resetPendingRegenerate();
    emit('regenerate-answer', { index });
    return;
  }
  pendingRegenerateKey.value = messageId;
  if (pendingRegenerateTimer) clearTimeout(pendingRegenerateTimer);
  pendingRegenerateTimer = setTimeout(resetPendingRegenerate, 4000);
};

onBeforeUnmount(resetPendingRegenerate);

const listRef = ref<HTMLElement | null>(null);
const copiedKey = ref<string | null>(null);

const questionSuggestions = [
  '什么是地下水污染物运移模拟？',
  '场地污染调查的主要流程和步骤是什么？',
  '重金属污染土壤的原位和异位修复技术有哪些？',
  '如何根据水文地质条件选择合适的地下水修复技术？',
  '地下水抽提-处理（Pump and Treat）系统的设计要点有哪些？',
  '什么是可渗透反应墙（PRB）技术？',
  '如何评估场地土壤和地下水的健康风险？',
  '建设用地土壤污染风险管控标准（GB36600-2018）的核心要求是什么？',
  '如何开展非水相液体（NAPL）污染场地的水文地质调查？',
  '地下水监测井的布设原则是什么？',
  '场地污染概念模型（CSM）包含哪些核心要素？',
];
const suggestionIcons = [
  'BadgeQuestion',
  'ClipboardList',
  'Wrench',
  'Map',
  'Droplets',
  'Blocks',
  'ShieldCheck',
  'BookOpenCheck',
  'Layers',
  'Gauge',
  'Network',
];
const iconOf = (index: number) => (icons as any)[suggestionIcons[index] || 'CircleHelp'] || icons.CircleHelp;

// 固定 4 条，列数交给容器查询决定，避免用 resize 监听驱动布局
const displayedSuggestions = computed(() => questionSuggestions.slice(0, 4));

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
          <title>导出回答 - 场地污染与地下水修复智能助理</title>
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
            /* 交互式图表：打印隐藏 ECharts 画布，显示 PNG 导出资产 */
            .chart-echarts {
              display: none !important;
            }
            img.chart-print-fallback {
              display: block !important;
              margin: 16px auto !important;
              max-width: 100% !important;
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

watch(() => props.messages, scrollToBottom, { deep: true });
</script>


<template>
  <div 
    ref="listRef" 
    class="custom-scrollbar flex min-h-0 w-full flex-1 flex-col overflow-y-auto overscroll-contain"
    :class="messages.length === 0 ? 'justify-center' : ''"
    @scroll="handleScroll"
  >
    <TransitionGroup 
      name="list" 
      tag="div"
      class="mx-auto flex w-full max-w-(--content-max) flex-col px-4 @cozy/main:px-6"
      :class="messages.length === 0 ? 'py-6'
        : 'space-y-6 py-6 @wide/main:space-y-8 @wide/main:py-8'"
    >
      <div 
        v-for="(msg, index) in messages" 
        :key="msg.id" 
        class="flex flex-col animate-in fade-in slide-in-from-bottom-2 duration-300 transform transition-all"
      :class="[msg.role === 'user' ? 'items-end' : 'items-start']"
    >
      <!-- User Message -->
      <div v-if="msg.role === 'user'" class="max-w-[85%]">
        <div class="rounded-(--radius-bubble) bg-surface-sunken px-4 py-2.5">
          <p class="whitespace-pre-wrap text-[15px] leading-relaxed text-text-primary">{{ msg.content }}</p>
        </div>
        <div class="mt-1.5 flex justify-end gap-1">
          <button
            type="button"
            class="inline-flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-secondary"
            title="复制问题"
            @click="copyText(msg.content, `${msg.id}-question`)"
          >
            <svg v-if="copiedKey === `${msg.id}-question`" class="animate-in fade-in zoom-in-50 duration-200" xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M20 6 9 17l-5-5"/>
            </svg>
            <svg v-else xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
          </button>
          <button
            type="button"
            class="inline-flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-secondary"
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

        <ProcessPanel
          :message-id="msg.id"
          :events="msg.processEvents"
          :status-history="msg.statusHistory"
          :is-streaming="msg.isStreaming"
          @action-send="(value) => emit('process-action-send', value)"
          @action-fill="(value) => emit('process-action-fill', value)"
        />

        <ReasoningBlock
          v-if="msg.reasoning"
          :reasoning="msg.reasoning"
          :is-streaming="msg.isStreaming"
          :has-answer="Boolean(msg.content)"
        />

        <div v-if="msg.content">
          <p
            v-if="msg.isStreaming"
            class="mb-2 inline-flex items-center gap-1.5 text-xs text-text-tertiary"
          >
            <span class="size-1.5 animate-pulse rounded-full bg-accent" />
            流式输出中
          </p>
          <div :id="`msg-content-${msg.id}`" class="overflow-x-hidden">
            <MarkdownRenderer :content="msg.content" :is-streaming="msg.isStreaming" />
          </div>
        </div>

        <!-- 操作按钮：流式结束后导出按钮柔和出现 -->
        <div v-if="msg.content" class="animate-in fade-in flex justify-end gap-1.5 duration-300">
          <!-- 复制回答 -->
          <button
            type="button"
            class="inline-flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-secondary"
            title="复制回答"
            @click="copyText(msg.content, `${msg.id}-answer`)"
          >
            <svg v-if="copiedKey === `${msg.id}-answer`" class="animate-in fade-in zoom-in-50 duration-200" xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
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
            class="animate-in fade-in inline-flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary transition-colors duration-300 hover:bg-surface-hover hover:text-text-secondary"
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
            class="animate-in fade-in inline-flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary transition-colors duration-300 hover:bg-surface-hover hover:text-text-secondary"
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

          <!-- 重新生成：回滚到该问题并按当前 composer 设置重答（可先换模型） -->
          <button
            v-if="!msg.isStreaming"
            type="button"
            class="animate-in fade-in inline-flex h-7 min-w-7 items-center justify-center gap-1 rounded-md px-1 transition-colors duration-300 hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50"
            :class="pendingRegenerateKey === msg.id
              ? 'text-amber-600 dark:text-amber-400'
              : 'text-text-tertiary hover:text-text-secondary'"
            :title="regenerateHint(index, msg.id)"
            :aria-label="regenerateHint(index, msg.id)"
            :disabled="isBusy"
            @click="requestRegenerate(index, msg.id)"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>
              <path d="M21 3v5h-5"/>
              <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>
              <path d="M8 16H3v5"/>
            </svg>
            <span v-if="pendingRegenerateKey === msg.id" class="text-xs font-medium">确认</span>
          </button>

          <!-- 赞/踩反馈（仅登录态渲染） -->
          <FeedbackControls
            v-if="!msg.isStreaming"
            :thread-id="threadId"
            :agent-code="agentCode"
            :message-key="feedbackKeys[index] || ''"
            :question="questionForIndex(index)"
            :answer="msg.content"
            :model-name="modelName"
            :disabled="isBusy"
          />
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
          <div class="h-1.5 w-1.5 bg-accent rounded-full animate-bounce [animation-delay:0ms]"></div>
          <div class="h-1.5 w-1.5 bg-accent rounded-full animate-bounce [animation-delay:200ms]"></div>
          <div class="h-1.5 w-1.5 bg-accent rounded-full animate-bounce [animation-delay:400ms]"></div>
        </div>
      </div>
    </div>

    <!-- Empty State：品牌 logo + 豆包式胶囊云布局，元素错峰入场 -->
    <div
      v-if="messages.length === 0"
      key="empty-state"
      class="flex w-full flex-col items-center gap-5 text-center"
    >
      <img
        src="/log.png?v=transparent-20260725"
        alt="成理场地污染水文地质智学助手"
        class="animate-in fade-in zoom-in-95 object-contain duration-500"
        style="width: clamp(10rem, 22cqw, 18rem); height: auto; max-height: min(26vh, 16rem)"
      />

      <div class="animate-in fade-in slide-in-from-bottom-2 space-y-1.5 duration-500" style="animation-delay: 80ms">
        <h2
          class="font-semibold tracking-tight text-text-primary"
          style="font-size: clamp(1.125rem, 2.4vh, 1.5rem)"
        >
          你好，我是成理场地污染水文地质智学助手
        </h2>
        <p class="text-sm text-text-secondary">
          我可以辅助场地调查、地下水修复、风险评估与项目资料分析
        </p>
      </div>

      <div class="grid w-full grid-cols-1 gap-2 @cozy/main:grid-cols-2">
        <button
          v-for="(suggestion, index) in displayedSuggestions"
          :key="suggestion"
          type="button"
          :disabled="isBusy"
          class="animate-in fade-in slide-in-from-bottom-2 group flex min-h-11 items-center gap-2 rounded-xl border border-border-subtle bg-surface px-3.5 py-2.5 text-left text-sm text-text-secondary transition-colors duration-400 hover:border-border-strong hover:bg-surface-hover hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
          :style="{ animationDelay: `${160 + index * 60}ms` }"
          @click="emit('select-suggestion', suggestion)"
        >
          <component :is="iconOf(index)" class="size-4 shrink-0 text-accent" />
          <span class="min-w-0 flex-1 line-clamp-2">{{ suggestion }}</span>
        </button>
      </div>
    </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.list-enter-active,
.list-leave-active {
  transition:
    opacity 0.3s ease,
    transform 0.3s ease;
}
.list-move {
  transition: transform 0.3s ease;
}
.list-enter-from {
  opacity: 0;
  transform: translateY(12px);
}
.list-leave-to {
  opacity: 0;
  transform: scale(0.97);
}
</style>
