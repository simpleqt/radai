<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue';
import { ChevronLeft, ChevronRight, MessageSquareText, Search, X } from 'lucide-vue-next';
import MarkdownRenderer from '@/views/ne-full/components/MarkdownRenderer.vue';
import {
  TeacherAnalyticsAPI,
  type ConversationTurn,
  type QuestionRecord,
} from '@/api/teacher-analytics';

const loading = ref(true);
const error = ref('');
const items = ref<QuestionRecord[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;

const keyword = ref('');
const userId = ref('');
const from = ref('');
const to = ref('');

// 查看对话弹窗
const conversationOpen = ref(false);
const conversationLoading = ref(false);
const conversationError = ref('');
const conversationTurns = ref<ConversationTurn[]>([]);
const conversationTitle = ref('');
// 展开了过程/异常消息的轮次下标集合
const expandedTurns = ref<Set<number>>(new Set());
// 从提问列表点进来时定位高亮的轮次下标（-1 为无）
const highlightTurnIndex = ref(-1);

const turnProcessMessages = (turn: ConversationTurn) =>
  turn.answers.filter((answer) => answer.kind !== 'answer');

function normalizeText(value: string): string {
  return (value || '').split(/\s+/).join('');
}

function toggleTurnProcess(index: number) {
  const next = new Set(expandedTurns.value);
  if (next.has(index)) next.delete(index);
  else next.add(index);
  expandedTurns.value = next;
}

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

function fmt(value: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN', { hour12: false });
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const result = await TeacherAnalyticsAPI.questions({
      agent_code: 'fmca',
      from: from.value || undefined,
      to: to.value || undefined,
      keyword: keyword.value || undefined,
      user_id: userId.value || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    });
    items.value = result.items;
    total.value = result.total;
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

async function openConversation(item: QuestionRecord) {
  conversationOpen.value = true;
  conversationLoading.value = true;
  conversationError.value = '';
  conversationTurns.value = [];
  conversationTitle.value = '';
  expandedTurns.value = new Set();
  highlightTurnIndex.value = -1;
  // 提问采集可能截断到 2000 字，定位时允许「轮次问题以采集文本开头」的匹配。
  const anchor = normalizeText(item.question);
  try {
    const detail = await TeacherAnalyticsAPI.conversation(item.thread_id, item.user_id);
    conversationTurns.value = detail.turns || [];
    conversationTitle.value =
      detail.title || `${detail.username || '学生'} 的基础力学对话`;
    if (anchor) {
      const index = conversationTurns.value.findIndex(
        (turn) =>
          normalizeText(turn.question) === anchor ||
          normalizeText(turn.question).startsWith(anchor),
      );
      if (index >= 0) {
        highlightTurnIndex.value = index;
        void nextTick(() => {
          document
            .querySelector(`[data-turn-index="${index}"]`)
            ?.scrollIntoView({ block: 'center' });
        });
      }
    }
  } catch (err: unknown) {
    conversationError.value = err instanceof Error ? err.message : '加载对话失败';
  } finally {
    conversationLoading.value = false;
  }
}

function closeConversation() {
  conversationOpen.value = false;
  highlightTurnIndex.value = -1;
}

function apply() {
  page.value = 1;
  void load();
}

function go(p: number) {
  if (p < 1 || p > totalPages.value) return;
  page.value = p;
  void load();
}

onMounted(() => void load());
</script>

<template>
  <div class="space-y-5">
    <header>
      <h1 class="text-xl font-extrabold text-text-primary">提问列表</h1>
      <p class="mt-1 text-sm text-text-tertiary">共 {{ total }} 条学生提问（仅基础力学智能体）</p>
    </header>

    <div class="grid gap-3 rounded-2xl border border-border-subtle bg-surface p-4 md:grid-cols-5">
      <label class="md:col-span-2">
        <span class="text-xs font-semibold text-text-tertiary">关键词</span>
        <input
          v-model="keyword"
          type="text"
          class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm text-text-primary outline-none focus:border-accent"
          placeholder="搜索问题内容"
          @keyup.enter="apply"
        />
      </label>
      <label>
        <span class="text-xs font-semibold text-text-tertiary">学生 ID</span>
        <input
          v-model="userId"
          type="text"
          class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm text-text-primary outline-none focus:border-accent"
          placeholder="可选"
          @keyup.enter="apply"
        />
      </label>
      <label>
        <span class="text-xs font-semibold text-text-tertiary">开始日期</span>
        <input v-model="from" type="date" class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm text-text-primary outline-none focus:border-accent" />
      </label>
      <label>
        <span class="text-xs font-semibold text-text-tertiary">结束日期</span>
        <input v-model="to" type="date" class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm text-text-primary outline-none focus:border-accent" />
      </label>
      <div class="md:col-span-5 flex justify-end">
        <button type="button" class="inline-flex items-center gap-2 rounded-xl bg-accent px-4 py-2 text-sm font-bold text-white hover:bg-accent-hover transition" @click="apply">
          <Search class="w-4 h-4" /> 筛选
        </button>
      </div>
    </div>

    <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

    <div v-if="loading" class="space-y-2">
      <div v-for="i in 8" :key="i" class="skeleton-shimmer h-14 animate-pulse rounded-xl bg-surface-hover" />
    </div>

    <div v-else-if="items.length === 0" class="rounded-2xl border border-border-subtle bg-surface p-10 text-center text-sm text-text-tertiary">
      暂无提问记录
    </div>

    <div v-else class="space-y-2">
      <div v-for="item in items" :key="item.id" class="rounded-xl border border-border-subtle bg-surface px-4 py-3">
        <p class="text-sm text-text-primary">{{ item.question }}</p>
        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-tertiary">
          <span>{{ item.username || (item.is_guest ? '访客' : '学生') }}</span>
          <span v-if="item.is_guest" class="rounded bg-surface-sunken px-1.5 py-0.5">访客</span>
          <span>{{ fmt(item.created_at) }}</span>
          <span v-if="item.workflow">工作流 {{ item.workflow }}</span>
          <span v-if="item.model_name">{{ item.model_name }}</span>
          <button
            v-if="!item.is_guest"
            type="button"
            class="inline-flex items-center gap-1 rounded-lg px-1.5 py-0.5 font-semibold text-accent transition hover:bg-accent-soft"
            title="查看该学生在基础力学智能体下的完整对话"
            @click="openConversation(item)"
          >
            <MessageSquareText class="size-3.5" />
            查看对话
          </button>
        </div>
      </div>
    </div>

    <Teleport to="body">
      <Transition name="pop">
        <div
          v-if="conversationOpen"
          class="fixed inset-0 z-80 flex items-center justify-center bg-slate-950/20 px-4 py-8 backdrop-blur-md dark:bg-black/45"
          role="dialog"
          aria-modal="true"
          aria-label="查看完整对话"
          @click.self="closeConversation"
        >
          <section class="pop-panel flex max-h-[85vh] w-full max-w-4xl flex-col rounded-2xl border border-border-subtle bg-surface/95 p-5 shadow-2xl backdrop-blur-2xl">
            <header class="flex items-center justify-between gap-3 border-b border-border-subtle pb-3">
              <div class="min-w-0">
                <h2 class="truncate text-base font-bold text-text-primary">{{ conversationTitle || '完整对话' }}</h2>
                <p class="mt-0.5 text-xs text-text-tertiary">仅展示基础力学智能体下该学生的对话</p>
              </div>
              <button
                type="button"
                class="inline-flex size-9 shrink-0 items-center justify-center rounded-lg text-text-tertiary transition hover:bg-surface-hover hover:text-text-primary"
                title="关闭"
                aria-label="关闭"
                @click="closeConversation"
              >
                <X class="size-4" />
              </button>
            </header>

            <div class="custom-scrollbar min-h-0 flex-1 space-y-4 overflow-y-auto py-4">
              <p v-if="conversationLoading" class="py-8 text-center text-sm text-text-tertiary">正在加载对话…</p>
              <p v-else-if="conversationError" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ conversationError }}</p>
              <p v-else-if="conversationTurns.length === 0" class="py-8 text-center text-sm text-text-tertiary">该会话暂无可展示的消息</p>
              <template v-else>
                <p class="text-xs text-text-tertiary">共 {{ conversationTurns.length }} 轮提问（重复发送已合并；点开「过程消息」可看检索/作图等中间播报）</p>
                <section
                  v-for="(turn, index) in conversationTurns"
                  :key="index"
                  :data-turn-index="index"
                  class="space-y-2 rounded-2xl border p-3 transition"
                  :class="index === highlightTurnIndex
                    ? 'border-accent bg-accent-soft/40 ring-2 ring-accent'
                    : 'border-transparent'"
                >
                  <!-- 学生提问：右侧纯文本气泡，重复发送合并计数 -->
                  <div class="flex justify-end">
                    <div class="max-w-[80%] rounded-2xl bg-accent-soft px-4 py-2.5 text-sm leading-relaxed text-text-primary">
                      <p class="mb-1 flex items-center gap-2 text-[10px] font-bold text-accent">
                        <span>学生提问</span>
                        <span
                          v-if="turn.repeat_count > 1"
                          class="rounded bg-accent/15 px-1.5 py-0.5 font-semibold"
                          :title="`该问题连续发送了 ${turn.repeat_count} 次（通常为回答失败后的重试）`"
                        >重复发送 {{ turn.repeat_count }} 次</span>
                      </p>
                      <p class="whitespace-pre-wrap break-words">{{ turn.question || '（无提问原文）' }}</p>
                    </div>
                  </div>
                  <!-- 智能体回答：只渲染最终回答；过程/报错默认折叠 -->
                  <div class="w-full max-w-[92%] rounded-2xl border border-border-subtle bg-surface-sunken px-4 py-2.5">
                    <p class="mb-1 text-[10px] font-bold text-text-tertiary">智能体</p>
                    <template v-if="turn.has_answer">
                      <MarkdownRenderer
                        v-for="(answer, answerIndex) in turn.answers.filter((entry) => entry.kind === 'answer')"
                        :key="answerIndex"
                        :content="answer.content"
                        :is-streaming="false"
                        :smooth="false"
                      />
                    </template>
                    <p v-else class="text-sm italic text-text-tertiary">该轮提问未生成有效回答</p>
                    <button
                      v-if="turnProcessMessages(turn).length > 0"
                      type="button"
                      class="mt-1 inline-flex items-center gap-1 rounded-lg px-1.5 py-0.5 text-xs font-semibold text-text-tertiary transition hover:bg-surface-hover hover:text-text-secondary"
                      @click="toggleTurnProcess(index)"
                    >
                      {{ expandedTurns.has(index) ? '收起' : '展开' }}过程消息（{{ turnProcessMessages(turn).length }} 条）
                    </button>
                    <div
                      v-if="expandedTurns.has(index)"
                      class="mt-2 space-y-1.5 border-t border-border-subtle pt-2"
                    >
                      <p
                        v-for="(entry, entryIndex) in turnProcessMessages(turn)"
                        :key="entryIndex"
                        class="whitespace-pre-wrap break-words rounded-lg px-2 py-1 text-xs leading-relaxed"
                        :class="entry.kind === 'error'
                          ? 'border border-rose-200 bg-rose-50 text-rose-700'
                          : 'bg-surface-hover/60 text-text-tertiary'"
                      >{{ entry.kind === 'error' ? '［异常］' : '［过程］' }}{{ entry.content }}</p>
                    </div>
                  </div>
                </section>
              </template>
            </div>
          </section>
        </div>
      </Transition>
    </Teleport>

    <div v-if="totalPages > 1" class="flex items-center justify-end gap-2">
      <button type="button" class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-border-subtle text-text-secondary hover:bg-surface-hover disabled:opacity-40" :disabled="page <= 1" @click="go(page - 1)">
        <ChevronLeft class="w-4 h-4" />
      </button>
      <span class="text-sm text-text-secondary">第 {{ page }} / {{ totalPages }} 页</span>
      <button type="button" class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-border-subtle text-text-secondary hover:bg-surface-hover disabled:opacity-40" :disabled="page >= totalPages" @click="go(page + 1)">
        <ChevronRight class="w-4 h-4" />
      </button>
    </div>
  </div>
</template>
