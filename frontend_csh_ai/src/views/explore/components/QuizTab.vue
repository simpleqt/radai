<script setup lang="ts">
import { watch } from 'vue';
import { Loader2, Shuffle, ChevronLeft, ChevronRight, FileQuestion } from 'lucide-vue-next';
import type { QuizType } from '@/api/explore';
import { useQuiz } from '../composables/useQuiz';
import QuizCard from './QuizCard.vue';

const props = defineProps<{ nodeTitle: string; kbIds: string[] }>();

const {
  filtered, pageItems, loading, error, typeFilter, difficulty,
  page, totalPages, userAnswers, grades, grading,
  generate, gradeQuestion, setPage,
} = useQuiz();

const TYPE_TABS: { key: 'all' | QuizType; label: string }[] = [
  { key: 'all', label: '全部题型' },
  { key: 'choice', label: '选择题' },
  { key: 'judge', label: '判断题' },
  { key: 'short', label: '简答题' },
  { key: 'calc', label: '计算题' },
];

function regenerate(force = false) {
  generate(props.nodeTitle, props.kbIds, 6, force);
}

watch(
  () => props.nodeTitle,
  (t) => { if (t) regenerate(); },
  { immediate: true },
);

watch([typeFilter, difficulty], () => regenerate());
</script>

<template>
  <div class="h-full flex flex-col">
    <!-- 头部工具 -->
    <div class="shrink-0 px-4 pt-3 pb-2 border-b border-slate-100 dark:border-slate-800">
      <div class="flex items-center justify-between mb-2.5">
        <div>
          <h3 class="text-sm font-bold text-slate-800 dark:text-slate-100">题库练习</h3>
          <p class="text-[11px] text-slate-400">检验学习成果，巩固知识点</p>
        </div>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:opacity-50 transition-colors"
          :disabled="loading"
          @click="regenerate(true)"
        >
          <Loader2 v-if="loading" class="w-3.5 h-3.5 animate-spin" />
          <Shuffle v-else class="w-3.5 h-3.5" />
          重新出题
        </button>
      </div>

      <div class="flex items-center gap-1.5 flex-wrap">
        <button
          v-for="tab in TYPE_TABS"
          :key="tab.key"
          type="button"
          class="text-xs px-2.5 py-1 rounded-full border transition-all"
          :class="typeFilter === tab.key
            ? 'bg-teal-600 border-teal-600 text-white'
            : 'border-slate-200 dark:border-slate-700 text-slate-500 hover:border-teal-300'"
          @click="typeFilter = tab.key"
        >{{ tab.label }}</button>

        <select
          v-model="difficulty"
          class="ml-auto text-xs px-2 py-1 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-500 focus:outline-none"
        >
          <option value="all">难度：全部</option>
          <option value="easy">简单</option>
          <option value="medium">中等</option>
          <option value="hard">困难</option>
        </select>
      </div>
    </div>

    <!-- 题目列表 -->
    <div class="flex-1 overflow-y-auto custom-scrollbar px-4 py-3 space-y-3">
      <div v-if="loading" class="flex items-center gap-2 text-sm text-slate-400 py-10 justify-center">
        <Loader2 class="w-4 h-4 animate-spin" /> 正在生成习题…
      </div>

      <div v-else-if="error" class="text-sm text-rose-500 py-10 text-center">{{ error }}</div>

      <div v-else-if="filtered.length === 0" class="flex flex-col items-center gap-2 text-slate-400 py-12">
        <FileQuestion class="w-8 h-8" />
        <p class="text-sm">暂无题目</p>
      </div>

      <template v-else>
        <QuizCard
          v-for="(q, i) in pageItems"
          :key="q.id"
          :question="q"
          :index="(page - 1) * 3 + i + 1"
          :user-answer="userAnswers[q.id] || ''"
          :grade="grades[q.id]"
          :grading="grading[q.id]"
          @update:user-answer="userAnswers[q.id] = $event"
          @grade="gradeQuestion(q, nodeTitle, kbIds)"
        />
      </template>
    </div>

    <!-- 分页 -->
    <div v-if="totalPages > 1" class="shrink-0 px-4 py-2.5 border-t border-slate-100 dark:border-slate-800 flex items-center justify-center gap-2">
      <button
        type="button"
        class="w-7 h-7 rounded-lg flex items-center justify-center text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-40"
        :disabled="page <= 1"
        @click="setPage(page - 1)"
      ><ChevronLeft class="w-4 h-4" /></button>
      <span class="text-xs text-slate-400">{{ page }} / {{ totalPages }}</span>
      <button
        type="button"
        class="w-7 h-7 rounded-lg flex items-center justify-center text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-40"
        :disabled="page >= totalPages"
        @click="setPage(page + 1)"
      ><ChevronRight class="w-4 h-4" /></button>
    </div>
  </div>
</template>

<style scoped>
.custom-scrollbar::-webkit-scrollbar { width: 5px; }
.custom-scrollbar::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 10px; }
.dark .custom-scrollbar::-webkit-scrollbar-thumb { background: #334155; }
</style>
