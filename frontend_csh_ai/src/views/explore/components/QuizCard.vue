<script setup lang="ts">
import { ref, computed } from 'vue';
import { ChevronDown, CheckCircle2, Loader2 } from 'lucide-vue-next';
import type { QuizQuestion, GradeResult } from '@/api/explore';

const props = defineProps<{
  question: QuizQuestion;
  index: number;
  userAnswer: string;
  grade?: GradeResult;
  grading?: boolean;
}>();

const emit = defineEmits<{
  (e: 'update:userAnswer', v: string): void;
  (e: 'grade'): void;
}>();

const showAnswer = ref(false);

const TYPE_LABELS: Record<string, string> = {
  choice: '选择题', multi: '多选题', judge: '判断题', short: '简答题', calc: '计算题',
};
const DIFF_LABELS: Record<string, string> = { easy: '简单', medium: '中等', hard: '困难' };
const DIFF_CLASS: Record<string, string> = {
  easy: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-900/20 dark:text-emerald-400',
  medium: 'bg-amber-50 text-amber-600 dark:bg-amber-900/20 dark:text-amber-400',
  hard: 'bg-rose-50 text-rose-600 dark:bg-rose-900/20 dark:text-rose-400',
};

const isObjective = computed(() => ['choice', 'multi', 'judge'].includes(props.question.type));
const isSubjective = computed(() => ['short', 'calc'].includes(props.question.type));

const judgeOptions = [{ key: '正确', text: '正确' }, { key: '错误', text: '错误' }];

function selectOption(key: string) {
  if (props.question.type === 'multi') {
    const set = new Set((props.userAnswer || '').split(''));
    if (set.has(key)) set.delete(key); else set.add(key);
    emit('update:userAnswer', [...set].sort().join(''));
  } else {
    emit('update:userAnswer', key);
  }
}

function isSelected(key: string): boolean {
  if (props.question.type === 'multi') return (props.userAnswer || '').includes(key);
  return props.userAnswer === key;
}

function optionState(key: string): 'correct' | 'wrong' | 'normal' {
  if (!showAnswer.value || !props.question.answer) return 'normal';
  const correct = props.question.answer.includes(key);
  if (correct) return 'correct';
  if (isSelected(key)) return 'wrong';
  return 'normal';
}
</script>

<template>
  <div class="rounded-2xl border border-slate-200 dark:border-slate-700 p-4 bg-white dark:bg-slate-800/50">
    <!-- 头部 -->
    <div class="flex items-center gap-2 mb-3">
      <span class="w-6 h-6 rounded-lg bg-teal-600 text-white text-xs font-bold flex items-center justify-center shrink-0">{{ index }}</span>
      <span class="text-[11px] px-2 py-0.5 rounded-md bg-teal-50 dark:bg-teal-500/10 text-teal-600 dark:text-teal-400 font-medium">{{ TYPE_LABELS[question.type] }}</span>
      <span class="text-[11px] px-2 py-0.5 rounded-md font-medium" :class="DIFF_CLASS[question.difficulty]">{{ DIFF_LABELS[question.difficulty] }}</span>
    </div>

    <!-- 题干 -->
    <p class="text-[14px] font-medium text-slate-800 dark:text-slate-100 leading-relaxed mb-3">{{ question.stem }}</p>

    <!-- 选项（选择/多选） -->
    <div v-if="question.type === 'choice' || question.type === 'multi'" class="space-y-2">
      <button
        v-for="opt in question.options"
        :key="opt.key"
        type="button"
        class="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl border text-left text-sm transition-all"
        :class="[
          optionState(opt.key) === 'correct' ? 'border-emerald-400 bg-emerald-50 dark:bg-emerald-900/20'
          : optionState(opt.key) === 'wrong' ? 'border-rose-400 bg-rose-50 dark:bg-rose-900/20'
          : isSelected(opt.key) ? 'border-teal-400 bg-teal-50 dark:bg-teal-500/10'
          : 'border-slate-200 dark:border-slate-700 hover:border-teal-300 dark:hover:border-teal-500/60',
        ]"
        @click="selectOption(opt.key)"
      >
        <span
          class="w-5 h-5 rounded-full border flex items-center justify-center text-[11px] font-bold shrink-0"
          :class="isSelected(opt.key) ? 'border-teal-500 bg-teal-500 text-white' : 'border-slate-300 dark:border-slate-600 text-slate-400'"
        >{{ opt.key }}</span>
        <span class="text-slate-700 dark:text-slate-200">{{ opt.text }}</span>
      </button>
    </div>

    <!-- 判断题 -->
    <div v-else-if="question.type === 'judge'" class="space-y-2">
      <button
        v-for="opt in judgeOptions"
        :key="opt.key"
        type="button"
        class="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl border text-left text-sm transition-all"
        :class="[
          showAnswer && question.answer === opt.key ? 'border-emerald-400 bg-emerald-50 dark:bg-emerald-900/20'
          : showAnswer && isSelected(opt.key) ? 'border-rose-400 bg-rose-50 dark:bg-rose-900/20'
          : isSelected(opt.key) ? 'border-teal-400 bg-teal-50 dark:bg-teal-500/10'
          : 'border-slate-200 dark:border-slate-700 hover:border-teal-300',
        ]"
        @click="selectOption(opt.key)"
      >
        <span class="w-5 h-5 rounded-full border flex items-center justify-center shrink-0"
          :class="isSelected(opt.key) ? 'border-teal-500 bg-teal-500' : 'border-slate-300 dark:border-slate-600'">
          <CheckCircle2 v-if="isSelected(opt.key)" class="w-3 h-3 text-white" />
        </span>
        <span class="text-slate-700 dark:text-slate-200">{{ opt.text }}</span>
      </button>
    </div>

    <!-- 简答/计算 -->
    <div v-else-if="isSubjective" class="space-y-2">
      <textarea
        :value="userAnswer"
        @input="emit('update:userAnswer', ($event.target as HTMLTextAreaElement).value)"
        rows="3"
        placeholder="在此处输入答案…"
        class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-sm text-slate-700 dark:text-slate-200 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-teal-400/50 resize-y"
      />
      <div class="flex justify-end">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-teal-600 text-white hover:bg-teal-700 disabled:opacity-50 transition-colors"
          :disabled="grading || !userAnswer.trim()"
          @click="emit('grade')"
        >
          <Loader2 v-if="grading" class="w-3.5 h-3.5 animate-spin" />
          提交评分
        </button>
      </div>

      <!-- 评分结果 -->
      <div v-if="grade" class="rounded-xl bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 p-3">
        <div class="flex items-center gap-2 mb-1.5">
          <span class="text-lg font-black text-teal-600 dark:text-teal-400">{{ grade.score }}</span>
          <span class="text-xs text-slate-400">/ 100</span>
          <span class="text-xs px-2 py-0.5 rounded-md bg-teal-50 dark:bg-teal-500/10 text-teal-600 dark:text-teal-400">{{ grade.verdict }}</span>
        </div>
        <p class="text-xs text-slate-600 dark:text-slate-300 leading-relaxed">{{ grade.feedback }}</p>
        <div v-if="grade.missing_points && grade.missing_points.length" class="mt-1.5">
          <p class="text-[11px] text-slate-400">遗漏要点：</p>
          <ul class="text-[11px] text-rose-500 list-disc pl-4">
            <li v-for="(m, i) in grade.missing_points" :key="i">{{ m }}</li>
          </ul>
        </div>
      </div>
    </div>

    <!-- 查看答案 -->
    <div class="mt-3">
      <button
        type="button"
        class="inline-flex items-center gap-1 text-xs text-teal-600 dark:text-teal-400 hover:underline"
        @click="showAnswer = !showAnswer"
      >
        {{ isSubjective ? '查看参考答案' : '查看答案' }}
        <ChevronDown class="w-3.5 h-3.5 transition-transform" :class="showAnswer ? 'rotate-180' : ''" />
      </button>
      <div v-if="showAnswer" class="mt-2 rounded-xl bg-teal-50/50 dark:bg-teal-900/10 border border-teal-100 dark:border-teal-500/20 p-3 text-xs leading-relaxed">
        <p v-if="isObjective" class="text-slate-700 dark:text-slate-200">
          <span class="font-semibold text-emerald-600 dark:text-emerald-400">正确答案：</span>{{ question.answer }}
        </p>
        <p v-if="isSubjective && question.reference_answer" class="text-slate-700 dark:text-slate-200">
          <span class="font-semibold text-emerald-600 dark:text-emerald-400">参考答案：</span>{{ question.reference_answer }}
        </p>
        <p v-if="question.explanation" class="text-slate-500 dark:text-slate-400 mt-1.5">
          <span class="font-semibold">解析：</span>{{ question.explanation }}
        </p>
      </div>
    </div>
  </div>
</template>
