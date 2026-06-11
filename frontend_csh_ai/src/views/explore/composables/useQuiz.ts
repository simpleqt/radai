import { ref, computed } from 'vue';
import { ExploreAPI, type QuizQuestion, type QuizType, type GradeResult } from '@/api/explore';
import { buildQuizCacheKey, quizCache, setQuizCache } from './exploreNodeCache';

const ALL_TYPES: QuizType[] = ['choice', 'judge', 'short', 'calc'];

export function useQuiz() {
  const questions = ref<QuizQuestion[]>([]);
  const loading = ref(false);
  const error = ref('');
  const typeFilter = ref<'all' | QuizType>('all');
  const difficulty = ref<'all' | 'easy' | 'medium' | 'hard'>('all');
  const page = ref(1);
  const pageSize = 3;

  // 用户作答与评分缓存（按题 id）
  const userAnswers = ref<Record<string, string>>({});
  const grades = ref<Record<string, GradeResult>>({});
  const grading = ref<Record<string, boolean>>({});

  const filtered = computed(() => {
    if (typeFilter.value === 'all') return questions.value;
    return questions.value.filter((q) => q.type === typeFilter.value);
  });

  const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)));

  const pageItems = computed(() => {
    const start = (page.value - 1) * pageSize;
    return filtered.value.slice(start, start + pageSize);
  });

  function saveCache(nodeTitle: string, kbIds: string[]) {
    const key = buildQuizCacheKey(nodeTitle, kbIds, typeFilter.value, difficulty.value);
    setQuizCache(key, {
      questions: [...questions.value],
      userAnswers: { ...userAnswers.value },
      grades: { ...grades.value },
      page: page.value,
      error: error.value,
    });
  }

  function restoreFromCache(nodeTitle: string, kbIds: string[]) {
    const key = buildQuizCacheKey(nodeTitle, kbIds, typeFilter.value, difficulty.value);
    const cached = quizCache.get(key);
    if (!cached || cached.questions.length === 0) return false;
    questions.value = [...cached.questions];
    userAnswers.value = { ...cached.userAnswers };
    grades.value = { ...cached.grades };
    page.value = cached.page;
    error.value = cached.error;
    return true;
  }

  async function generate(nodeTitle: string, kbIds: string[], count = 6, force = false) {
    if (!force && restoreFromCache(nodeTitle, kbIds)) {
      loading.value = false;
      return;
    }

    loading.value = true;
    error.value = '';
    page.value = 1;
    try {
      const types: QuizType[] = typeFilter.value === 'all' ? ALL_TYPES : [typeFilter.value];
      const data = await ExploreAPI.quiz({
        nodeTitle,
        kbIds,
        types,
        difficulty: difficulty.value,
        count,
      });
      questions.value = data.questions || [];
      userAnswers.value = {};
      grades.value = {};
      saveCache(nodeTitle, kbIds);
    } catch (e: any) {
      error.value = e?.message || '生成习题失败';
      questions.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function gradeQuestion(q: QuizQuestion, nodeTitle?: string, kbIds?: string[]) {
    const answer = userAnswers.value[q.id] || '';
    if (!answer.trim()) return;
    grading.value = { ...grading.value, [q.id]: true };
    try {
      const result = await ExploreAPI.grade(q, answer);
      grades.value = { ...grades.value, [q.id]: result };
      if (nodeTitle && kbIds?.length) saveCache(nodeTitle, kbIds);
    } catch (e) {
      console.warn('[Explore] 评分失败', e);
    } finally {
      grading.value = { ...grading.value, [q.id]: false };
    }
  }

  function setPage(p: number) {
    page.value = Math.min(Math.max(1, p), totalPages.value);
  }

  return {
    questions, filtered, pageItems, loading, error, typeFilter, difficulty,
    page, totalPages, pageSize, userAnswers, grades, grading,
    generate, gradeQuestion, setPage, saveCache,
  };
}
