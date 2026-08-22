<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { Plus, Search, Tags, Upload } from 'lucide-vue-next';
import InlineMath from '@/components/InlineMath.vue';
import {
  DIFFICULTIES,
  difficultyLabel,
  QuestionBankAPI,
  QUESTION_TYPES,
  type QBKnowledgePoint,
  type QBQuestion,
} from '@/api/question-bank';

const router = useRouter();
const route = useRoute();
const loading = ref(true);
// 入库结果横幅：inserted=实际入库数；skipped=跨批题干查重跳过数——
// 不展示会让教师把「少入库」当丢题缺陷反复报障。
const confirmBanner = ref('');
const error = ref('');
const items = ref<QBQuestion[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;

const keyword = ref('');
const questionType = ref('');
const difficulty = ref('');
const knowledgePoint = ref('');
const status = ref('');
const knowledgePoints = ref<QBKnowledgePoint[]>([]);

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const result = await QuestionBankAPI.questions({
      keyword: keyword.value.trim(),
      question_type: questionType.value,
      difficulty: difficulty.value,
      knowledge_point: knowledgePoint.value,
      status: status.value,
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
    knowledgePoints.value = (await QuestionBankAPI.knowledgePoints()).items;
  } catch {
    knowledgePoints.value = [];
  }
}

function search() {
  page.value = 1;
  void load();
}

async function toggleStatus(question: QBQuestion) {
  const next = question.status === 'published' ? 'disabled' : 'published';
  try {
    await QuestionBankAPI.updateQuestion(question.id, { status: next });
    question.status = next;
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '操作失败';
  }
}

function excerpt(stem: string): string {
  const text = String(stem || '').replace(/\s+/g, ' ').trim();
  const limit = 100;
  let cut = text.length > limit ? text.slice(0, limit) : text;
  // 截断不能落在 $...$ 定界符中间：奇数个 $ 时退回到最后一个 $ 之前
  if ((cut.match(/\$/g) || []).length % 2 === 1) {
    cut = cut.slice(0, cut.lastIndexOf('$'));
  }
  return cut.length < text.length ? `${cut}…` : cut;
}

onMounted(() => {
  const inserted = Number(route.query.inserted ?? NaN);
  const skipped = Number(route.query.skipped ?? 0);
  if (Number.isFinite(inserted)) {
    confirmBanner.value = skipped > 0
      ? `已入库 ${inserted} 题；另有 ${skipped} 题因与已有题目题干重复而跳过（可在批次详情查看重复提示，直接复用已有题目即可）`
      : `已入库 ${inserted} 题`;
  }
  void loadKnowledgePoints();
  void load();
});
</script>

<template>
  <div class="space-y-4">
    <div v-if="confirmBanner" class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800 flex items-start justify-between gap-3">
      <span>{{ confirmBanner }}</span>
      <button type="button" class="text-text-tertiary hover:text-text-primary" @click="confirmBanner = ''">✕</button>
    </div>
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-xl font-extrabold text-text-primary">题库管理</h1>
        <p class="mt-1 text-sm text-text-tertiary">题目为课程共享资产，全部教师可协同维护</p>
      </div>
      <div class="flex gap-2">
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-xl border border-border-subtle bg-surface px-4 py-2 text-sm font-bold text-text-secondary transition hover:border-accent/40 hover:text-accent"
          @click="router.push('/teacher/bank/knowledge-points')"
        >
          <Tags class="h-4 w-4" /> 知识点字典
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-xl border border-border-subtle bg-surface px-4 py-2 text-sm font-bold text-text-secondary transition hover:border-accent/40 hover:text-accent"
          @click="router.push('/teacher/bank/upload')"
        >
          <Upload class="h-4 w-4" /> 批量上传
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-xl bg-accent px-4 py-2 text-sm font-bold text-white transition hover:bg-accent-hover"
          @click="router.push('/teacher/bank/new')"
        >
          <Plus class="h-4 w-4" /> 手工录题
        </button>
      </div>
    </header>

    <section class="rounded-2xl border border-border-subtle bg-surface p-4">
      <div class="grid gap-3 md:grid-cols-5">
        <div class="relative md:col-span-2">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
          <input
            v-model="keyword"
            type="text"
            placeholder="搜索题干/解析"
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
      <div class="mt-3 flex items-center gap-3 text-xs text-text-tertiary">
        <label class="flex cursor-pointer items-center gap-1.5">
          <input v-model="status" type="radio" value="" class="accent-[var(--color-accent,#3b82f6)]" @change="search" /> 全部状态
        </label>
        <label class="flex cursor-pointer items-center gap-1.5">
          <input v-model="status" type="radio" value="published" class="accent-[var(--color-accent,#3b82f6)]" @change="search" /> 已发布
        </label>
        <label class="flex cursor-pointer items-center gap-1.5">
          <input v-model="status" type="radio" value="disabled" class="accent-[var(--color-accent,#3b82f6)]" @change="search" /> 已停用
        </label>
        <span class="ml-auto">共 {{ total }} 题</span>
      </div>
    </section>

    <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

    <div v-if="loading" class="space-y-3">
      <div v-for="i in 5" :key="i" class="h-24 animate-pulse rounded-2xl bg-surface-hover" />
    </div>

    <template v-else>
      <p v-if="!items.length" class="rounded-2xl border border-border-subtle bg-surface px-4 py-14 text-center text-sm text-text-tertiary">
        题库为空——先到「批量上传」导入模板试题
      </p>

      <article
        v-for="question in items"
        :key="question.id"
        class="rounded-2xl border border-border-subtle bg-surface p-5 transition hover:border-accent/30"
      >
        <div class="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span class="rounded-lg bg-accent-soft px-2 py-0.5 font-bold text-accent">{{ question.question_type }}</span>
          <span class="rounded-lg bg-surface-hover px-2 py-0.5 text-text-secondary">{{ difficultyLabel(question.difficulty) }}</span>
          <span v-for="kp in (question.knowledge_points || []).slice(0, 3)" :key="kp" class="rounded-lg bg-surface-hover px-2 py-0.5 text-text-secondary">{{ kp }}</span>
          <span v-if="question.source_name" class="text-text-tertiary">{{ question.source_name }}</span>
          <span
            class="ml-auto rounded-lg px-2 py-0.5 font-bold"
            :class="question.status === 'published' ? 'bg-emerald-50 text-emerald-700' : 'bg-surface-hover text-text-tertiary'"
          >
            {{ question.status === 'published' ? '已发布' : '已停用' }}
          </span>
        </div>
        <p class="cursor-pointer text-sm leading-6" @click="router.push(`/teacher/bank/${question.id}`)"><InlineMath :text="excerpt(question.stem)" /></p>
        <div class="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            class="rounded-xl border border-border-subtle px-4 py-1.5 text-sm font-medium text-text-secondary transition hover:border-accent/40 hover:text-accent"
            @click="router.push(`/teacher/bank/${question.id}`)"
          >
            编辑 / 标注
          </button>
          <button
            type="button"
            class="rounded-xl border px-4 py-1.5 text-sm font-medium transition"
            :class="question.status === 'published'
              ? 'border-rose-200 text-rose-600 hover:bg-rose-50'
              : 'border-emerald-200 text-emerald-700 hover:bg-emerald-50'"
            @click="toggleStatus(question)"
          >
            {{ question.status === 'published' ? '停用' : '重新发布' }}
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
        <span class="text-sm text-text-tertiary">第 {{ page }} / {{ totalPages }} 页</span>
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
  </div>
</template>
