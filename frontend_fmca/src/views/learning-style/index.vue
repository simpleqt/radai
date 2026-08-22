<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  ArrowLeft,
  ArrowRight,
  BrainCircuit,
  Check,
  Eye,
  Flame,
  Layers3,
  Lightbulb,
  Loader2,
  LogIn,
  MessageSquareText,
  RefreshCcw,
  Save,
  Sparkles,
  Target,
} from 'lucide-vue-next';
import AppShell from '@/components/layout/AppShell.vue';
import { authState } from '@/utils/request';
import {
  LearningStyleAPI,
  type LearningStyleAttempt,
  type LearningStyleDimension,
  type LearningStyleDimensionResult,
  type LearningStyleOptionKey,
  type LearningStyleProfile,
  type LearningStyleQuestionnaire,
  type LearningStyleResult,
} from '@/api/learning-style';

type ViewMode = 'intro' | 'test' | 'result';

interface GuestAttempt {
  status: 'in_progress' | 'completed';
  questionnaireVersion: string;
  answers: Record<string, LearningStyleOptionKey>;
  currentDimension: number;
  result?: LearningStyleResult;
  completedAt?: string;
}

const GUEST_STORAGE_KEY = 'learning_style_guest_attempt_v1';

const route = useRoute();
const router = useRouter();
const questionnaire = ref<LearningStyleQuestionnaire | null>(null);
const attempt = ref<LearningStyleAttempt | null>(null);
const profile = ref<LearningStyleProfile | null>(null);
const answers = ref<Record<string, LearningStyleOptionKey>>({});
const currentDimension = ref(0);
const view = ref<ViewMode>('intro');
const loading = ref(true);
const busy = ref(false);
const errorMessage = ref('');
const saveState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle');
const applyToAi = ref(false);
let saveTimer: number | null = null;
// 重新测评的两步内联确认（替代原生 window.confirm）。
const restartConfirm = ref(false);
let restartConfirmTimer: number | null = null;
// 登录后检测到本机游客测评结果时，提供导入入口。
const guestImportAvailable = ref(false);
const guestImporting = ref(false);

const isAuthenticated = computed(() => Boolean(authState.value.token));
const dimensions = computed(() => questionnaire.value?.dimensions || []);
const questions = computed(() => questionnaire.value?.questions || []);
const activeDimension = computed(() => dimensions.value[currentDimension.value] || null);
const activeQuestions = computed(() => {
  const dimensionId = activeDimension.value?.id;
  return questions.value.filter((question) => question.dimension === dimensionId);
});
const result = computed(() => profile.value?.result || attempt.value?.result || loadGuestAttempt()?.result || null);
const answeredCount = computed(() => Object.keys(answers.value).length);
const totalQuestions = computed(() => questionnaire.value?.question_count || 44);
const totalPercent = computed(() => Math.round((answeredCount.value / totalQuestions.value) * 100));
const activeAnsweredCount = computed(() => activeQuestions.value.filter((question) => answers.value[question.id]).length);
const activeComplete = computed(() => activeQuestions.value.length > 0 && activeAnsweredCount.value === activeQuestions.value.length);

function questionsForDimension(dimensionId: string) {
  return questions.value.filter((question) => question.dimension === dimensionId);
}

function dimensionAnsweredCount(dimensionId: string) {
  return questionsForDimension(dimensionId).filter((question) => answers.value[question.id]).length;
}

function dimensionTotal(dimensionId: string) {
  return questionsForDimension(dimensionId).length;
}

function dimensionPercent(dimensionId: string) {
  const total = dimensionTotal(dimensionId);
  return total ? (dimensionAnsweredCount(dimensionId) / total) * 100 : 0;
}

function dimensionComplete(index: number) {
  const dimension = dimensions.value[index];
  return Boolean(dimension) && dimensionAnsweredCount(dimension.id) === questionsForDimension(dimension.id).length;
}

function canOpenDimension(index: number) {
  if (index <= currentDimension.value) return true;
  for (let previous = 0; previous < index; previous += 1) {
    if (!dimensionComplete(previous)) return false;
  }
  return true;
}

function iconForDimension(dimensionId: string) {
  const icons: Record<string, any> = {
    processing: Flame,
    perception: Sparkles,
    input: Eye,
    understanding: Layers3,
  };
  return icons[dimensionId] || BrainCircuit;
}

function dimensionAccent(dimensionId: string) {
  const accents: Record<string, string> = {
    processing: 'text-orange-500 bg-orange-50 dark:bg-orange-500/10',
    perception: 'text-blue-500 bg-blue-50 dark:bg-blue-500/10',
    input: 'text-emerald-500 bg-emerald-50 dark:bg-emerald-500/10',
    understanding: 'text-accent bg-accent-soft',
  };
  return accents[dimensionId] || 'text-accent bg-accent-soft';
}

function loadGuestAttempt(): GuestAttempt | null {
  try {
    return JSON.parse(localStorage.getItem(GUEST_STORAGE_KEY) || 'null');
  } catch {
    return null;
  }
}

function persistGuestAttempt(payload: GuestAttempt) {
  localStorage.setItem(GUEST_STORAGE_KEY, JSON.stringify(payload));
}

function calculateGuestResult(): LearningStyleResult {
  if (!questionnaire.value) throw new Error('题库尚未加载');
  if (answeredCount.value !== questionnaire.value.question_count) throw new Error('请完成全部题目后再提交');

  const scores: Record<string, number> = {};
  const resultDimensions: LearningStyleDimensionResult[] = questionnaire.value.dimensions.map((dimension) => {
    const dimensionQuestions = questionsForDimension(dimension.id);
    const leftCount = dimensionQuestions.filter((question) => answers.value[question.id] === 'a').length;
    const rightCount = dimensionQuestions.filter((question) => answers.value[question.id] === 'b').length;
    const score = leftCount - rightCount;
    const strengthValue = Math.abs(score);
    const strength = strengthValue <= 3 ? 'balanced' : strengthValue <= 7 ? 'moderate' : 'strong';
    const preferredSide = score > 0 ? 'left' : 'right';
    scores[dimension.id] = score;
    return {
      dimension_id: dimension.id,
      title: dimension.title,
      title_en: dimension.title_en,
      left: dimension.left,
      right: dimension.right,
      score,
      left_count: leftCount,
      right_count: rightCount,
      preferred_side: preferredSide,
      preferred_label: dimension[preferredSide],
      strength,
      summary: dimension[`${preferredSide}_summary`],
      tips: dimension[`${preferredSide}_tips`],
    };
  });

  return {
    questionnaire_version: questionnaire.value.version,
    scores,
    dimensions: resultDimensions,
    disclaimer: '结果反映你当前自述的学习风格，不代表能力高低，也不意味着只能使用某一种学习方式。',
  };
}

async function initialize() {
  loading.value = true;
  errorMessage.value = '';
  try {
    questionnaire.value = await LearningStyleAPI.questionnaire();
    if (isAuthenticated.value) {
      const [currentResponse, profileResponse] = await Promise.all([
        LearningStyleAPI.currentAttempt(),
        LearningStyleAPI.profile(),
      ]);
      profile.value = profileResponse.profile;
      applyToAi.value = Boolean(profile.value?.apply_to_ai);
      if (currentResponse.attempt) {
        restoreAttempt(currentResponse.attempt);
        view.value = 'test';
      } else if (profile.value) {
        view.value = 'result';
      }
      // 登录前在本机做过的测评不再被静默丢弃：无进行中作答且账号无画像时提示导入。
      if (!currentResponse.attempt && !profile.value) {
        const guestAttempt = loadGuestAttempt();
        guestImportAvailable.value = Boolean(
          guestAttempt
          && guestAttempt.questionnaireVersion === questionnaire.value.version
          && Object.keys(guestAttempt.answers || {}).length > 0,
        );
      }
    } else {
      const guestAttempt = loadGuestAttempt();
      if (guestAttempt?.questionnaireVersion === questionnaire.value.version) {
        answers.value = guestAttempt.answers || {};
        currentDimension.value = Math.min(3, Math.max(0, guestAttempt.currentDimension || 0));
        if (guestAttempt.status === 'completed' && guestAttempt.result) {
          view.value = 'result';
        } else {
          view.value = 'test';
        }
      }
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '学习风格测评加载失败';
  } finally {
    loading.value = false;
  }
}

function restoreAttempt(nextAttempt: LearningStyleAttempt) {
  attempt.value = nextAttempt;
  answers.value = { ...(nextAttempt.answers || {}) };
  currentDimension.value = Math.min(3, Math.max(0, nextAttempt.current_dimension || 0));
}

function cancelRestartConfirm() {
  restartConfirm.value = false;
  if (restartConfirmTimer !== null) {
    window.clearTimeout(restartConfirmTimer);
    restartConfirmTimer = null;
  }
}

function requestRestart() {
  // 两步内联确认：第一次点击进入确认态，5 秒内再次点击才真正重新测评。
  if (!restartConfirm.value) {
    restartConfirm.value = true;
    if (restartConfirmTimer !== null) window.clearTimeout(restartConfirmTimer);
    restartConfirmTimer = window.setTimeout(() => {
      restartConfirm.value = false;
      restartConfirmTimer = null;
    }, 5000);
    return;
  }
  cancelRestartConfirm();
  void startTest(true);
}

async function importGuestAttempt() {
  const guestAttempt = loadGuestAttempt();
  if (!guestAttempt || !questionnaire.value || guestImporting.value) return;
  guestImporting.value = true;
  errorMessage.value = '';
  try {
    const created = await LearningStyleAPI.createAttempt();
    answers.value = { ...(guestAttempt.answers || {}) };
    const savedDimension = Math.min(3, Math.max(0, guestAttempt.currentDimension || 0));
    const saved = await LearningStyleAPI.saveAnswers(created.attempt.attempt_id, { ...answers.value }, savedDimension);
    attempt.value = saved.attempt;
    if (guestAttempt.status === 'completed' && Object.keys(answers.value).length === questionnaire.value.question_count) {
      const submitted = await LearningStyleAPI.submitAttempt(created.attempt.attempt_id);
      attempt.value = submitted.attempt;
      if (submitted.profile) {
        profile.value = submitted.profile;
        applyToAi.value = submitted.profile.apply_to_ai;
      }
      view.value = 'result';
    } else {
      currentDimension.value = savedDimension;
      view.value = 'test';
    }
    localStorage.removeItem(GUEST_STORAGE_KEY);
    guestImportAvailable.value = false;
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导入本地测评结果失败';
  } finally {
    guestImporting.value = false;
  }
}

function dismissGuestImport() {
  guestImportAvailable.value = false;
}

async function startTest(forceNew = false) {
  busy.value = true;
  errorMessage.value = '';
  try {
    answers.value = {};
    currentDimension.value = 0;
    profile.value = forceNew ? profile.value : null;
    if (isAuthenticated.value) {
      const response = await LearningStyleAPI.createAttempt();
      attempt.value = response.attempt;
    } else {
      attempt.value = null;
      persistGuestAttempt({
        status: 'in_progress',
        questionnaireVersion: questionnaire.value?.version || '',
        answers: {},
        currentDimension: 0,
      });
    }
    view.value = 'test';
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '无法开始测评';
  } finally {
    busy.value = false;
  }
}

function selectAnswer(questionId: string, option: LearningStyleOptionKey) {
  answers.value = { ...answers.value, [questionId]: option };
  scheduleSave();
}

function scheduleSave() {
  saveState.value = 'saving';
  if (saveTimer !== null) window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => {
    saveTimer = null;
    void saveProgress();
  }, 350);
}

async function saveProgress() {
  if (!questionnaire.value || view.value !== 'test') return;
  saveState.value = 'saving';
  try {
    if (isAuthenticated.value && attempt.value) {
      const response = await LearningStyleAPI.saveAnswers(
        attempt.value.attempt_id,
        { ...answers.value },
        currentDimension.value,
      );
      attempt.value = response.attempt;
    } else {
      persistGuestAttempt({
        status: 'in_progress',
        questionnaireVersion: questionnaire.value.version,
        answers: { ...answers.value },
        currentDimension: currentDimension.value,
      });
    }
    saveState.value = 'saved';
  } catch (error) {
    saveState.value = 'error';
    errorMessage.value = error instanceof Error ? error.message : '保存失败';
  }
}

async function flushSave() {
  if (saveTimer !== null) {
    window.clearTimeout(saveTimer);
    saveTimer = null;
  }
  await saveProgress();
}

async function goToDimension(index: number) {
  if (!canOpenDimension(index) || index === currentDimension.value) return;
  await flushSave();
  currentDimension.value = index;
  window.setTimeout(() => document.querySelector('[data-question-scroll]')?.scrollTo({ top: 0, behavior: 'smooth' }), 0);
  scheduleSave();
}

async function nextStep() {
  if (!activeComplete.value) {
    errorMessage.value = `请先完成“${activeDimension.value?.title || ''}”的全部 ${activeQuestions.value.length} 道题`;
    return;
  }
  errorMessage.value = '';
  await flushSave();
  if (currentDimension.value < dimensions.value.length - 1) {
    currentDimension.value += 1;
    scheduleSave();
    window.setTimeout(() => document.querySelector('[data-question-scroll]')?.scrollTo({ top: 0, behavior: 'smooth' }), 0);
    return;
  }
  await submitTest();
}

async function submitTest() {
  if (answeredCount.value !== totalQuestions.value) {
    errorMessage.value = '请完成全部题目后再查看结果';
    return;
  }
  busy.value = true;
  try {
    if (isAuthenticated.value && attempt.value) {
      const response = await LearningStyleAPI.submitAttempt(attempt.value.attempt_id);
      attempt.value = response.attempt;
      if (response.profile) {
        profile.value = response.profile;
        applyToAi.value = response.profile.apply_to_ai;
      }
    } else {
      const guestResult = calculateGuestResult();
      persistGuestAttempt({
        status: 'completed',
        questionnaireVersion: questionnaire.value?.version || '',
        answers: { ...answers.value },
        currentDimension: 3,
        result: guestResult,
        completedAt: new Date().toISOString(),
      });
    }
    view.value = 'result';
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交测评失败';
  } finally {
    busy.value = false;
  }
}

async function toggleApplyToAi() {
  if (!isAuthenticated.value) {
    router.push({ path: '/login', query: { redirect: route.fullPath } });
    return;
  }
  const nextValue = !applyToAi.value;
  busy.value = true;
  try {
    const response = await LearningStyleAPI.updatePreferences(nextValue);
    profile.value = response.profile;
    applyToAi.value = response.profile.apply_to_ai;
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '偏好设置更新失败';
  } finally {
    busy.value = false;
  }
}

function strengthText(strength: LearningStyleDimensionResult['strength']) {
  return strength === 'strong' ? '明显偏好' : strength === 'moderate' ? '中等偏好' : '相对均衡';
}

function scorePosition(score: number) {
  return `${((11 - score) / 22) * 100}%`;
}

function backToHome() {
  router.push('/app');
}

function firstUnansweredQuestion() {
  return activeQuestions.value.find((question) => !answers.value[question.id]) || null;
}

// 键盘快捷作答：1/2 给当前维度第一道未答题选左/右选项，Enter 进入下一维度。
function handleTestKeydown(event: KeyboardEvent) {
  if (view.value !== 'test' || busy.value) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target as HTMLElement | null;
  if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return;
  if (event.key === '1' || event.key === '2') {
    const question = firstUnansweredQuestion();
    if (!question) return;
    const option = question.options[event.key === '1' ? 0 : 1];
    if (!option) return;
    event.preventDefault();
    selectAnswer(question.id, option.key);
    window.setTimeout(() => {
      const next = firstUnansweredQuestion();
      if (next) {
        document.getElementById(`ls-question-${next.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }, 0);
  } else if (event.key === 'Enter' && activeComplete.value) {
    event.preventDefault();
    void nextStep();
  }
}

// 错误提示 8 秒后自动消失，仍可手动关闭。
let errorDismissTimer: number | null = null;
watch(errorMessage, (value) => {
  if (errorDismissTimer !== null) {
    window.clearTimeout(errorDismissTimer);
    errorDismissTimer = null;
  }
  if (!value) return;
  errorDismissTimer = window.setTimeout(() => {
    errorMessage.value = '';
    errorDismissTimer = null;
  }, 8000);
});

onMounted(() => {
  void initialize();
  window.addEventListener('keydown', handleTestKeydown);
});
onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleTestKeydown);
  if (saveTimer !== null) window.clearTimeout(saveTimer);
  if (restartConfirmTimer !== null) window.clearTimeout(restartConfirmTimer);
  if (errorDismissTimer !== null) window.clearTimeout(errorDismissTimer);
  if (view.value === 'test') void saveProgress();
});
</script>

<template>
  <AppShell title="学习风格测评" brand-name="基础力学助手" brand-icon="Atom">
    <template #actions>
      <div
        v-if="view === 'test'"
        class="flex items-center gap-1.5 text-xs"
        :class="saveState === 'error' ? 'text-rose-500' : 'text-text-tertiary'"
        :title="saveState === 'saving' ? '正在保存' : saveState === 'error' ? '保存失败' : '已自动保存'"
      >
        <Save v-if="saveState !== 'saving'" class="size-3.5" />
        <Loader2 v-else class="size-3.5 animate-spin" />
        <span class="hidden @cozy/main:inline">{{ saveState === 'saving' ? '正在保存' : saveState === 'error' ? '保存失败' : '已自动保存' }}</span>
      </div>
      <button
        v-if="view === 'result'"
        type="button"
        class="inline-flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors disabled:opacity-60"
        :class="restartConfirm ? 'border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200' : 'border-border-subtle text-text-secondary hover:border-accent/30 hover:text-accent'"
        :disabled="busy"
        @click="requestRestart"
      >
        <RefreshCcw class="size-3.5" />
        {{ restartConfirm ? '再点一次开始新测评' : '重新测评' }}
      </button>
      <button
        v-if="view === 'result' && restartConfirm"
        type="button"
        class="inline-flex h-8 items-center rounded-lg border border-border-subtle px-2.5 text-xs font-medium text-text-secondary transition-colors hover:text-text-primary"
        @click="cancelRestartConfirm"
      >
        取消
      </button>
    </template>

    <main class="relative min-h-0 min-w-0 flex-1 overflow-hidden">
        <div v-if="loading" class="absolute inset-0 flex flex-col items-center justify-center gap-3 text-text-tertiary">
          <Loader2 class="w-8 h-8 animate-spin text-blue-500" />
          <p class="text-sm">正在准备测评</p>
        </div>

        <div v-else-if="!questionnaire" class="h-full flex items-center justify-center px-6">
          <div class="max-w-md text-center">
            <BrainCircuit class="w-10 h-10 mx-auto text-text-tertiary mb-4" />
            <h2 class="text-lg font-semibold">测评暂时无法加载</h2>
            <p class="text-sm text-text-secondary mt-2">{{ errorMessage }}</p>
            <button class="mt-5 px-4 py-2 rounded-xl bg-blue-600 text-white text-sm" @click="initialize">重新加载</button>
          </div>
        </div>

        <div v-else-if="view === 'intro'" class="h-full overflow-y-auto">
          <div class="max-w-5xl mx-auto px-5 py-10 md:py-16">
            <div
              v-if="guestImportAvailable"
              class="mb-5 flex flex-col gap-3 rounded-2xl border border-blue-200 bg-blue-50 px-5 py-4 text-sm text-blue-900 dark:border-blue-900/60 dark:bg-blue-950/20 dark:text-blue-100 sm:flex-row sm:items-center sm:justify-between"
            >
              <p class="leading-6">检测到你登录前在本机完成的测评作答，可以导入到当前账号继续使用。</p>
              <div class="flex shrink-0 gap-2">
                <button
                  type="button"
                  class="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-blue-700 disabled:opacity-60"
                  :disabled="guestImporting"
                  @click="importGuestAttempt"
                >
                  <Loader2 v-if="guestImporting" class="size-3.5 animate-spin" />
                  {{ guestImporting ? '导入中' : '导入结果' }}
                </button>
                <button
                  type="button"
                  class="rounded-lg border border-blue-200 px-3 py-2 text-xs font-semibold text-blue-700 transition-colors hover:bg-blue-100/60 dark:border-blue-900/60 dark:text-blue-200"
                  @click="dismissGuestImport"
                >
                  暂不导入
                </button>
              </div>
            </div>
            <section class="relative overflow-hidden rounded-[32px] border border-blue-100 dark:border-blue-900/60 bg-surface px-6 py-9 md:px-12 md:py-12 shadow-sm dark:shadow-none">
              <div class="absolute -right-16 -top-20 w-72 h-72 rounded-full bg-blue-100/60 dark:bg-blue-500/10 blur-3xl" />
              <div class="relative max-w-2xl">
                <span class="inline-flex items-center gap-1.5 rounded-full bg-blue-50 dark:bg-blue-500/10 px-3 py-1.5 text-xs font-medium text-blue-600 dark:text-blue-300">
                  <Sparkles class="w-3.5 h-3.5" /> 约 6 分钟 · 44 道题
                </span>
                <h2 class="mt-5 text-3xl md:text-5xl font-bold tracking-tight text-text-primary dark:text-white">认识你偏好的学习方式</h2>
                <p class="mt-5 text-base md:text-lg leading-8 text-text-secondary">从信息加工、感知、输入和理解四个角度，获得一份连续维度结果和可以马上尝试的学习策略。</p>
                <button
                  type="button"
                  class="mt-8 inline-flex items-center gap-2 rounded-2xl bg-blue-600 hover:bg-blue-700 px-6 py-3.5 text-sm font-semibold text-white shadow-sm dark:shadow-none disabled:opacity-60"
                  :disabled="busy"
                  @click="startTest(false)"
                >
                  <Loader2 v-if="busy" class="w-4 h-4 animate-spin" />
                  <Target v-else class="w-4 h-4" />
                  开始测评
                  <ArrowRight class="w-4 h-4" />
                </button>
              </div>
            </section>

            <section class="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-6">
              <article v-for="dimension in dimensions" :key="dimension.id" class="rounded-2xl border border-border-subtle bg-surface p-5">
                <div class="w-10 h-10 rounded-xl flex items-center justify-center" :class="dimensionAccent(dimension.id)">
                  <component :is="iconForDimension(dimension.id)" class="w-5 h-5" />
                </div>
                <h3 class="mt-4 font-semibold">{{ dimension.title }}</h3>
                <p class="mt-1 text-xs text-text-tertiary">{{ dimension.left }} vs {{ dimension.right }}</p>
                <p class="mt-3 text-sm leading-6 text-text-secondary">{{ dimension.description }}</p>
              </article>
            </section>

            <div class="mt-6 flex items-start gap-3 rounded-2xl bg-amber-50 dark:bg-amber-500/10 border border-amber-100 dark:border-amber-900/60 px-5 py-4 text-sm text-amber-800 dark:text-amber-200">
              <Lightbulb class="w-5 h-5 shrink-0 mt-0.5" />
              <p>这里没有“更好”的类型。结果反映你当前的自述偏好，并不代表能力高低，也不会限制你使用其他学习方式。</p>
            </div>
          </div>
        </div>

        <div v-else-if="view === 'test'" class="h-full flex flex-col md:flex-row bg-surface dark:bg-[#0b0f1a]">
          <aside class="hidden md:flex w-72 shrink-0 flex-col border-r border-border-subtle bg-surface-sunken p-5 overflow-y-auto">
            <div class="mb-5">
              <p class="text-[11px] uppercase tracking-[0.18em] text-text-tertiary font-semibold">四个维度</p>
              <p class="text-sm text-text-secondary mt-2">请选择更符合大多数学习情境的选项。</p>
              <p class="mt-2 text-xs text-text-tertiary">快捷键：<kbd class="rounded border border-border-subtle px-1">1</kbd> / <kbd class="rounded border border-border-subtle px-1">2</kbd> 选择，<kbd class="rounded border border-border-subtle px-1">Enter</kbd> 下一维度</p>
            </div>
            <button
              v-for="(dimension, index) in dimensions"
              :key="dimension.id"
              type="button"
              class="text-left rounded-2xl border p-4 mb-3 transition-all disabled:cursor-not-allowed"
              :disabled="!canOpenDimension(index)"
              :class="index === currentDimension ? 'bg-surface border-blue-200 dark:border-blue-800 shadow-sm dark:shadow-none'
                : 'border-transparent hover:border-border-subtle disabled:opacity-45'"
              @click="goToDimension(index)"
            >
              <div class="flex items-center gap-2">
                <component :is="iconForDimension(dimension.id)" class="w-4 h-4" :class="index === currentDimension ? 'text-blue-500' : 'text-text-tertiary'" />
                <span class="font-semibold text-sm" :class="index === currentDimension ? 'text-blue-600 dark:text-blue-300' : 'text-text-secondary'">{{ dimension.title }}</span>
                <Check v-if="dimensionComplete(index)" class="w-4 h-4 text-emerald-500 ml-auto" />
              </div>
              <p class="text-xs text-text-tertiary mt-2">{{ dimension.left }} vs {{ dimension.right }}</p>
              <div class="mt-3 h-1.5 rounded-full bg-surface-sunken overflow-hidden">
                <div class="h-full bg-blue-500 rounded-full transition-all" :style="{ width: `${dimensionPercent(dimension.id)}%` }" />
              </div>
              <p class="mt-2 text-right text-[11px] text-text-tertiary">{{ dimensionAnsweredCount(dimension.id) }} / {{ dimensionTotal(dimension.id) }}</p>
            </button>
            <button type="button" class="mt-auto pt-5 inline-flex items-center justify-center gap-2 text-sm text-text-secondary hover:text-text-primary" @click="backToHome">
              <ArrowLeft class="w-4 h-4" /> 暂时离开
            </button>
          </aside>

          <section class="flex-1 min-w-0 min-h-0 flex flex-col">
            <div class="shrink-0 px-5 md:px-9 py-5 border-b border-border-subtle bg-surface/95 dark:bg-[#0b0f1a]/95">
              <div class="max-w-5xl mx-auto flex items-center justify-between gap-4">
                <div>
                  <p class="text-xs font-semibold text-blue-500">维度 {{ currentDimension + 1 }} / 4</p>
                  <h2 class="mt-1 text-xl md:text-2xl font-bold">{{ activeDimension?.title }} <span class="text-text-tertiary">/ {{ activeDimension?.title_en }}</span></h2>
                  <p class="mt-1 text-sm text-text-tertiary">{{ activeDimension?.left }} vs {{ activeDimension?.right }}</p>
                </div>
                <div class="md:hidden text-right">
                  <p class="text-lg font-bold text-blue-600">{{ activeAnsweredCount }}/{{ activeQuestions.length }}</p>
                  <p class="text-[10px] text-text-tertiary">当前维度</p>
                </div>
              </div>
            </div>

            <div data-question-scroll class="flex-1 min-h-0 overflow-y-auto px-4 md:px-9 py-5 md:py-7 pb-32">
              <div class="max-w-5xl mx-auto space-y-7">
                <article v-for="(question, index) in activeQuestions" :id="`ls-question-${question.id}`" :key="question.id">
                  <div class="flex items-start gap-3">
                    <span class="text-sm font-bold text-text-tertiary pt-0.5">Q{{ String(index + 1).padStart(2, '0') }}</span>
                    <div class="flex-1 min-w-0">
                      <h3 class="text-base md:text-lg font-semibold leading-7">{{ question.text }}</h3>
                      <div class="grid md:grid-cols-2 gap-3 mt-3" role="radiogroup" :aria-label="question.text">
                        <button
                          v-for="option in question.options"
                          :key="option.key"
                          type="button"
                          role="radio"
                          :aria-checked="answers[question.id] === option.key"
                          class="min-h-16 rounded-2xl border px-5 py-4 text-left text-sm md:text-base flex items-center justify-between gap-3 transition-all"
                          :class="answers[question.id] === option.key ? 'border-blue-500 bg-accent text-white shadow-sm dark:shadow-none'
                            : 'border-border-subtle bg-surface hover:border-blue-300 hover:bg-blue-50/40 dark:hover:bg-blue-500/5'"
                          @click="selectAnswer(question.id, option.key)"
                        >
                          <span>{{ option.text }}</span>
                          <span v-if="answers[question.id] === option.key" class="w-6 h-6 rounded-full bg-white/15 flex items-center justify-center shrink-0"><Check class="w-4 h-4" /></span>
                        </button>
                      </div>
                    </div>
                  </div>
                </article>
              </div>
            </div>

            <footer class="absolute md:relative bottom-0 left-0 right-0 shrink-0 border-t border-border-subtle bg-surface/95 backdrop-blur-xl px-4 md:px-8 py-4">
              <div class="max-w-5xl mx-auto flex items-center gap-4 md:gap-6">
                <div class="hidden sm:flex items-center gap-3 flex-1">
                  <span class="text-xs text-text-tertiary">总进度</span>
                  <strong class="text-blue-600">{{ answeredCount }} / {{ totalQuestions }}</strong>
                  <div class="h-2 flex-1 max-w-52 rounded-full bg-surface-sunken overflow-hidden"><div class="h-full bg-blue-500 rounded-full transition-all" :style="{ width: `${totalPercent}%` }" /></div>
                  <span class="text-sm font-semibold text-blue-500">{{ totalPercent }}%</span>
                </div>
                <div class="flex items-center gap-1.5">
                  <button
                    v-for="(_, index) in dimensions"
                    :key="index"
                    type="button"
                    class="rounded-full transition-all"
                    :class="index === currentDimension ? 'w-5 h-2.5 bg-blue-500' : dimensionComplete(index) ? 'w-2.5 h-2.5 bg-emerald-400' : 'w-2.5 h-2.5 bg-surface-sunken'"
                    :disabled="!canOpenDimension(index)"
                    @click="goToDimension(index)"
                  />
                </div>
                <button
                  type="button"
                  class="ml-auto min-w-32 inline-flex items-center justify-center gap-2 rounded-2xl px-5 py-3 text-sm font-semibold transition-colors disabled:bg-surface-sunken disabled:text-text-tertiary bg-blue-600 hover:bg-blue-700 text-white"
                  :disabled="!activeComplete || busy"
                  @click="nextStep"
                >
                  <Loader2 v-if="busy" class="w-4 h-4 animate-spin" />
                  <span>{{ currentDimension === 3 ? '查看结果' : '下一维度' }}</span>
                  <ArrowRight v-if="!busy" class="w-4 h-4" />
                </button>
              </div>
            </footer>
          </section>
        </div>

        <div v-else-if="view === 'result' && result" class="h-full overflow-y-auto px-4 py-7 md:px-8 md:py-10">
          <div class="max-w-5xl mx-auto">
            <div
              v-if="guestImportAvailable"
              class="mb-5 flex flex-col gap-3 rounded-2xl border border-blue-200 bg-blue-50 px-5 py-4 text-sm text-blue-900 dark:border-blue-900/60 dark:bg-blue-950/20 dark:text-blue-100 sm:flex-row sm:items-center sm:justify-between"
            >
              <p class="leading-6">检测到你登录前在本机完成的测评作答，可以导入到当前账号继续使用。</p>
              <div class="flex shrink-0 gap-2">
                <button
                  type="button"
                  class="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-blue-700 disabled:opacity-60"
                  :disabled="guestImporting"
                  @click="importGuestAttempt"
                >
                  <Loader2 v-if="guestImporting" class="size-3.5 animate-spin" />
                  {{ guestImporting ? '导入中' : '导入结果' }}
                </button>
                <button
                  type="button"
                  class="rounded-lg border border-blue-200 px-3 py-2 text-xs font-semibold text-blue-700 transition-colors hover:bg-blue-100/60 dark:border-blue-900/60 dark:text-blue-200"
                  @click="dismissGuestImport"
                >
                  暂不导入
                </button>
              </div>
            </div>
            <section class="rounded-[28px] bg-accent text-white p-7 md:p-10 overflow-hidden relative">
              <div class="absolute right-0 top-0 w-80 h-80 rounded-full bg-blue-500/20 blur-3xl" />
              <div class="relative">
                <span class="inline-flex items-center gap-1.5 text-xs font-medium text-blue-200"><Sparkles class="w-4 h-4" /> 测评完成</span>
                <h2 class="mt-3 text-3xl md:text-4xl font-bold">你的学习风格图谱</h2>
                <p class="mt-3 max-w-2xl text-sm md:text-base leading-7 text-blue-100/70">四个维度彼此独立，也会随任务和经验变化。把它当作选择学习策略的起点，而不是固定标签。</p>
              </div>
            </section>

            <section class="grid md:grid-cols-2 gap-5 mt-6">
              <article v-for="dimensionResult in result.dimensions" :key="dimensionResult.dimension_id" class="rounded-3xl border border-border-subtle bg-surface p-5 md:p-6 shadow-sm">
                <div class="flex items-start justify-between gap-3">
                  <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-xl flex items-center justify-center" :class="dimensionAccent(dimensionResult.dimension_id)">
                      <component :is="iconForDimension(dimensionResult.dimension_id)" class="w-5 h-5" />
                    </div>
                    <div>
                      <h3 class="font-bold">{{ dimensionResult.title }}</h3>
                      <p class="text-[11px] text-text-tertiary uppercase tracking-widest">{{ dimensionResult.title_en }}</p>
                    </div>
                  </div>
                  <span class="rounded-full bg-blue-50 dark:bg-blue-500/10 px-3 py-1 text-xs font-semibold text-blue-600 dark:text-blue-300">{{ strengthText(dimensionResult.strength) }}</span>
                </div>

                <div class="mt-6">
                  <div class="flex justify-between text-sm font-semibold"><span>{{ dimensionResult.left }}</span><span>{{ dimensionResult.right }}</span></div>
                  <div class="relative h-2 mt-3 rounded-full bg-accent">
                    <span class="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 w-5 h-5 rounded-full bg-surface border-4 border-border-strong dark:border-white shadow-sm" :style="{ left: scorePosition(dimensionResult.score) }" />
                  </div>
                  <div class="flex justify-between mt-2 text-[11px] text-text-tertiary"><span>{{ dimensionResult.left_count }} 票</span><span>{{ dimensionResult.right_count }} 票</span></div>
                </div>

                <div class="mt-5 rounded-2xl bg-surface-sunken p-4">
                  <p class="text-sm leading-6 text-text-secondary"><strong class="text-text-primary dark:text-white">偏向 {{ dimensionResult.preferred_label }}：</strong>{{ dimensionResult.summary }}</p>
                  <ul class="mt-3 space-y-2">
                    <li v-for="tip in dimensionResult.tips" :key="tip" class="flex items-start gap-2 text-sm text-text-secondary">
                      <Check class="w-4 h-4 text-emerald-500 shrink-0 mt-0.5" />{{ tip }}
                    </li>
                  </ul>
                </div>
              </article>
            </section>

            <section class="mt-6 rounded-3xl border border-border-subtle bg-surface p-6 flex flex-col md:flex-row md:items-center gap-5">
              <div class="w-12 h-12 rounded-2xl bg-accent-soft text-accent flex items-center justify-center shrink-0"><MessageSquareText class="w-6 h-6" /></div>
              <div class="flex-1">
                <h3 class="font-bold">应用于自定义学习</h3>
                <p class="text-sm leading-6 text-text-secondary mt-1">开启后，「自定义学习」会按你的偏好调整讲解方式、练习形式和复盘提示；暂不影响知识问答的回答内容，也不会改变检索范围。</p>
              </div>
              <div class="flex flex-col gap-2 sm:flex-row">
                <button
                  type="button"
                  class="inline-flex items-center justify-center gap-2 min-w-36 rounded-2xl px-4 py-3 text-sm font-semibold border transition-colors"
                  :class="applyToAi ? 'bg-accent border-accent text-white' : 'border-border-subtle text-text-secondary'"
                  :disabled="busy"
                  @click="toggleApplyToAi"
                >
                  <LogIn v-if="!isAuthenticated" class="w-4 h-4" />
                  <Check v-else-if="applyToAi" class="w-4 h-4" />
                  {{ !isAuthenticated ? '登录后启用' : applyToAi ? '已启用' : '暂不启用' }}
                </button>
                <button
                  type="button"
                  class="inline-flex items-center justify-center gap-2 rounded-2xl border border-accent/30 px-4 py-3 text-sm font-semibold text-accent transition-colors hover:bg-accent-soft"
                  @click="router.push('/custom-learning')"
                >
                  去自定义学习
                  <ArrowRight class="w-4 h-4" />
                </button>
              </div>
            </section>

            <div class="mt-6 mb-10 flex items-start gap-3 rounded-2xl bg-surface-sunken p-5 text-sm leading-6 text-text-secondary">
              <Lightbulb class="w-5 h-5 shrink-0 text-amber-500 mt-0.5" />
              <p>{{ result.disclaimer }} 当前研究也不足以证明按单一“学习类型”匹配教学媒介必然提高成绩，建议在优势方式之外主动练习多种有效策略。</p>
            </div>
          </div>
        </div>

        <div v-if="errorMessage && questionnaire" class="fixed z-[70] bottom-24 md:bottom-7 left-1/2 -translate-x-1/2 max-w-[90vw] rounded-xl bg-rose-600 text-white px-4 py-2.5 text-sm shadow-sm">
          {{ errorMessage }}
          <button class="ml-3 text-rose-100 hover:text-white" @click="errorMessage = ''">关闭</button>
        </div>
    </main>
  </AppShell>
</template>
