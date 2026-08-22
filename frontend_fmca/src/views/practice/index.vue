<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BookOpen, Bot, ClipboardList, Search } from 'lucide-vue-next';
import InlineMath from '@/components/InlineMath.vue';
import { PracticeAPI, type PracticeQuestion } from '@/api/practice';
import { DIFFICULTIES, difficultyLabel, QUESTION_TYPES } from '@/api/question-bank';

const router = useRouter();
const loading = ref(true);
const error = ref('');
const items = ref<PracticeQuestion[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;

const keyword = ref('');
const questionType = ref('');
const difficulty = ref('');
const chapter = ref('');
const knowledgePoints = ref<{ id: string; name: string }[]>([]);
const knowledgePoint = ref('');

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const result = await PracticeAPI.questions({
      keyword: keyword.value.trim(),
      question_type: questionType.value,
      difficulty: difficulty.value,
      chapter: chapter.value.trim(),
      knowledge_point: knowledgePoint.value,
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

async function loadKnowledgePoints() {
  try {
    const { QuestionBankAPI } = await import('@/api/question-bank');
    const result = await QuestionBankAPI.knowledgePoints();
    knowledgePoints.value = result.items;
  } catch {
    knowledgePoints.value = [];
  }
}

function search() {
  page.value = 1;
  void load();
}

function goQuestion(id: string, mode: 'practice' | 'browse') {
  void router.push({ path: `/practice/${id}`, query: mode === 'browse' ? { mode: 'browse' } : {} });
}

function askAgent(id: string) {
  void router.push({ path: '/app', query: { q: `请讲解试题 QB-${id}：请先取回完整题目（含题图）再逐步讲解。` } });
}

function excerpt(stem: string): string {
  const text = String(stem || '').replace(/\s+/g, ' ').trim();
  let cut = text.length > 120 ? text.slice(0, 120) : text;
  // 截断不能落在 $...$ 定界符中间：奇数个 $ 时退回到最后一个 $ 之前，避免摘要露出半个公式
  if ((cut.match(/\$/g) || []).length % 2 === 1) {
    cut = cut.slice(0, cut.lastIndexOf('$'));
  }
  return cut.length < text.length ? `${cut}…` : cut;
}

onMounted(() => {
  void loadKnowledgePoints();
  void load();
});
</script>

<template>
  <!-- 全局 html/body overflow:hidden，练习独立页自带滚动容器 -->
  <div class="flex h-screen flex-col overflow-hidden bg-surface-sunken text-text-primary">
    <header class="flex-none border-b border-border-subtle bg-surface/80 px-6 py-4 backdrop-blur-lg">
      <div class="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-2.5">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-accent shadow-sm">
            <BookOpen class="h-5 w-5 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-extrabold tracking-tight">习题练习</h1>
            <p class="text-xs text-text-tertiary">题目原样呈现、图必随题，作答后解锁解析</p>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <button
            type="button"
            class="inline-flex items-center gap-2 rounded-xl border border-border-subtle bg-surface px-3.5 py-2 text-sm font-medium text-text-secondary transition hover:border-accent/40 hover:text-accent"
            @click="router.push('/practice/wrong')"
          >
            <ClipboardList class="h-4 w-4" /> 错题本
          </button>
          <button
            type="button"
            class="inline-flex items-center gap-2 rounded-xl border border-border-subtle bg-surface px-3.5 py-2 text-sm font-medium text-text-secondary transition hover:border-accent/40 hover:text-accent"
            @click="router.push('/app')"
          >
            <Bot class="h-4 w-4" /> 返回对话
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto w-full max-w-5xl flex-1 space-y-4 overflow-y-auto px-6 py-6">
      <section class="rounded-2xl border border-border-subtle bg-surface p-4">
        <div class="grid gap-3 md:grid-cols-5">
          <div class="relative md:col-span-2">
            <Search class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
            <input
              v-model="keyword"
              type="text"
              placeholder="搜索题干关键词"
              class="h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken pl-9 pr-3 text-sm outline-none transition focus:border-accent/50"
              @keyup.enter="search"
            />
          </div>
          <select v-model="questionType" class="h-10 rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50" @change="search">
            <option value="">全部题型</option>
            <option v-for="item in QUESTION_TYPES" :key="item" :value="item">{{ item }}</option>
          </select>
          <select v-model="difficulty" class="h-10 rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50" @change="search">
            <option value="">全部难度</option>
            <option v-for="item in DIFFICULTIES" :key="item.value" :value="item.value">{{ item.label }}</option>
          </select>
          <select v-model="knowledgePoint" class="h-10 rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50" @change="search">
            <option value="">全部知识点</option>
            <option v-for="item in knowledgePoints" :key="item.id" :value="item.name">{{ item.name }}</option>
          </select>
        </div>
      </section>

      <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

      <div v-if="loading" class="space-y-3">
        <div v-for="i in 5" :key="i" class="h-28 animate-pulse rounded-2xl bg-surface-hover" />
      </div>

      <template v-else>
        <p v-if="!items.length" class="rounded-2xl border border-border-subtle bg-surface px-4 py-16 text-center text-sm text-text-tertiary">
          没有匹配的题目——教师尚未导入题库或筛选条件过窄
        </p>

        <article
          v-for="question in items"
          :key="question.id"
          class="rounded-2xl border border-border-subtle bg-surface p-5 transition hover:border-accent/30"
        >
          <div class="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <span class="rounded-lg bg-accent-soft px-2 py-0.5 font-bold text-accent">{{ question.question_type }}</span>
            <span class="rounded-lg bg-surface-hover px-2 py-0.5 text-text-secondary">{{ difficultyLabel(question.difficulty) }}</span>
            <span v-for="kp in question.knowledge_points.slice(0, 3)" :key="kp" class="rounded-lg bg-surface-hover px-2 py-0.5 text-text-secondary">{{ kp }}</span>
            <span v-if="question.source_name" class="ml-auto text-text-tertiary">{{ question.source_name }}</span>
          </div>
          <p class="text-sm leading-6 text-text-primary"><InlineMath :text="excerpt(question.stem)" /></p>
          <div v-if="question.images.length" class="mt-2 flex gap-2 overflow-x-auto">
            <img
              v-for="image in question.images"
              :key="image.seq"
              :src="image.url"
              :alt="image.caption || `题图${image.seq}`"
              class="h-20 w-20 shrink-0 rounded-lg border border-border-subtle object-contain"
              loading="lazy"
            />
          </div>
          <div class="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-xl bg-accent px-4 py-1.5 text-sm font-bold text-white transition hover:bg-accent-hover"
              @click="goQuestion(question.id, 'practice')"
            >
              做题
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-xl border border-border-subtle px-4 py-1.5 text-sm font-medium text-text-secondary transition hover:border-accent/40 hover:text-accent"
              @click="goQuestion(question.id, 'browse')"
            >
              解答模式
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-xl border border-border-subtle px-4 py-1.5 text-sm font-medium text-text-secondary transition hover:border-accent/40 hover:text-accent"
              @click="askAgent(question.id)"
            >
              <Bot class="h-4 w-4" /> 问智能体
            </button>
          </div>
        </article>

        <div v-if="totalPages > 1" class="flex items-center justify-center gap-3 pt-2">
          <button
            type="button"
            :disabled="page <= 1"
            class="rounded-xl border border-border-subtle px-3 py-1.5 text-sm text-text-secondary disabled:opacity-40"
            @click="page -= 1; load()"
          >
            上一页
          </button>
          <span class="text-sm text-text-tertiary">第 {{ page }} / {{ totalPages }} 页 · 共 {{ total }} 题</span>
          <button
            type="button"
            :disabled="page >= totalPages"
            class="rounded-xl border border-border-subtle px-3 py-1.5 text-sm text-text-secondary disabled:opacity-40"
            @click="page += 1; load()"
          >
            下一页
          </button>
        </div>
      </template>
    </main>
  </div>
</template>
