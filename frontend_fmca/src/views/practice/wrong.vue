<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ArrowLeft, ClipboardList, RefreshCw } from 'lucide-vue-next';
import InlineMath from '@/components/InlineMath.vue';
import { PracticeAPI, type PracticeQuestion } from '@/api/practice';
import { difficultyLabel } from '@/api/question-bank';

const router = useRouter();
const loading = ref(true);
const error = ref('');
const items = ref<PracticeQuestion[]>([]);
const total = ref(0);
const pageSize = 100;

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const result = await PracticeAPI.wrongQuestions(pageSize);
    items.value = result.items;
    total.value = result.total ?? result.items.length;
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

function excerpt(stem: string): string {
  const text = String(stem || '').replace(/\s+/g, ' ').trim();
  let cut = text.length > 100 ? text.slice(0, 100) : text;
  // 截断不能落在 $...$ 定界符中间：奇数个 $ 时退回到最后一个 $ 之前
  if ((cut.match(/\$/g) || []).length % 2 === 1) {
    cut = cut.slice(0, cut.lastIndexOf('$'));
  }
  return cut.length < text.length ? `${cut}…` : cut;
}

onMounted(() => void load());
</script>

<template>
  <!-- 全局 html/body overflow:hidden，练习独立页自带滚动容器 -->
  <div class="flex h-screen flex-col overflow-hidden bg-surface-sunken text-text-primary">
    <header class="flex-none border-b border-border-subtle bg-surface/80 px-6 py-3 backdrop-blur-lg">
      <div class="mx-auto flex max-w-3xl items-center justify-between gap-3">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-sm font-medium text-text-secondary transition hover:bg-surface-hover hover:text-text-primary"
          @click="router.push('/practice')"
        >
          <ArrowLeft class="h-4 w-4" /> 返回题库
        </button>
        <div class="flex items-center gap-2 text-sm font-bold">
          <ClipboardList class="h-4 w-4 text-accent" /> 我的错题本
        </div>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-sm text-text-tertiary transition hover:text-accent"
          @click="load"
        >
          <RefreshCw class="h-4 w-4" /> 刷新
        </button>
      </div>
    </header>

    <main class="mx-auto w-full max-w-3xl flex-1 space-y-4 overflow-y-auto px-6 py-6">
      <p class="text-xs text-text-tertiary">
        收录最近一次作答仍为错的客观题；重做正确后自动移出。主观题按你的自评「仍不会」收录。
        <span v-if="total > items.length">共 {{ total }} 题，当前显示最近 {{ items.length }} 题。</span>
        <span v-else-if="total > 0">共 {{ total }} 题。</span>
      </p>
      <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

      <div v-if="loading" class="space-y-3">
        <div v-for="i in 4" :key="i" class="h-24 animate-pulse rounded-2xl bg-surface-hover" />
      </div>

      <template v-else>
        <p v-if="!items.length" class="rounded-2xl border border-border-subtle bg-surface px-4 py-14 text-center text-sm text-text-tertiary">
          错题本是空的——继续保持！
        </p>
        <article
          v-for="question in items"
          :key="question.id"
          class="rounded-2xl border border-border-subtle bg-surface p-5"
        >
          <div class="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <span class="rounded-lg bg-accent-soft px-2 py-0.5 font-bold text-accent">{{ question.question_type }}</span>
            <span class="rounded-lg bg-surface-hover px-2 py-0.5 text-text-secondary">{{ difficultyLabel(question.difficulty) }}</span>
            <span v-for="kp in question.knowledge_points.slice(0, 3)" :key="kp" class="rounded-lg bg-surface-hover px-2 py-0.5 text-text-secondary">{{ kp }}</span>
          </div>
          <p class="text-sm leading-6"><InlineMath :text="excerpt(question.stem)" /></p>
          <button
            type="button"
            class="mt-3 rounded-xl bg-accent px-4 py-1.5 text-sm font-bold text-white transition hover:bg-accent-hover"
            @click="router.push(`/practice/${question.id}`)"
          >
            重做
          </button>
        </article>
      </template>
    </main>
  </div>
</template>
