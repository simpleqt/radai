<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ArrowLeft, Bot, CheckCircle2, ImagePlus, X, XCircle } from 'lucide-vue-next';
import InlineMath from '@/components/InlineMath.vue';
import MarkdownRenderer from '@/views/ne-full/components/MarkdownRenderer.vue';
import { PracticeAPI, type CheckResult, type PracticeQuestion } from '@/api/practice';
import { difficultyLabel } from '@/api/question-bank';

const route = useRoute();
const router = useRouter();
const questionId = computed(() => String(route.params.id || ''));
const browseMode = computed(() => String(route.query.mode || '') === 'browse');

const loading = ref(true);
const error = ref('');
const question = ref<PracticeQuestion | null>(null);
const selectedOption = ref('');
const answerText = ref('');
const submitting = ref(false);
const result = ref<CheckResult | null>(null);

const isObjective = computed(() =>
  question.value ? ['选择题', '判断题', '填空题'].includes(question.value.question_type) : false,
);
const judgeOptions = [
  { key: '对', label: '正确（对）' },
  { key: '错', label: '错误（错）' },
];

const MAX_ANSWER_IMAGES = 3;
const MAX_IMAGE_MB = 5;
const answerImages = ref<File[]>([]);
const imagePreviews = ref<string[]>([]);
const imageInput = ref<HTMLInputElement | null>(null);

const verdictMeta = computed(() => {
  const grading = result.value?.visual_grading;
  if (!grading) return null;
  if (grading.verdict === 'correct') {
    return { label: '回答正确', cls: 'border-emerald-200 bg-emerald-50 text-emerald-800' };
  }
  if (grading.verdict === 'partial') {
    return { label: '部分正确', cls: 'border-amber-200 bg-amber-50 text-amber-800' };
  }
  return { label: '回答错误', cls: 'border-rose-200 bg-rose-50 text-rose-700' };
});

function onImageChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = '';
  for (const file of files) {
    if (answerImages.value.length >= MAX_ANSWER_IMAGES) {
      error.value = `最多上传 ${MAX_ANSWER_IMAGES} 张手写解答图片`;
      return;
    }
    if (!file.type.startsWith('image/')) {
      error.value = '仅支持图片文件';
      continue;
    }
    if (file.size > MAX_IMAGE_MB * 1024 * 1024) {
      error.value = `单张图片不能超过 ${MAX_IMAGE_MB}MB`;
      continue;
    }
    answerImages.value = [...answerImages.value, file];
    imagePreviews.value = [...imagePreviews.value, URL.createObjectURL(file)];
  }
}

function removeAnswerImage(index: number) {
  URL.revokeObjectURL(imagePreviews.value[index]);
  answerImages.value = answerImages.value.filter((_, i) => i !== index);
  imagePreviews.value = imagePreviews.value.filter((_, i) => i !== index);
}

function clearAnswerImages() {
  imagePreviews.value.forEach((url) => URL.revokeObjectURL(url));
  answerImages.value = [];
  imagePreviews.value = [];
}

onUnmounted(clearAnswerImages);

async function load() {
  loading.value = true;
  error.value = '';
  result.value = null;
  selectedOption.value = '';
  answerText.value = '';
  clearAnswerImages();
  try {
    question.value = await PracticeAPI.question(questionId.value, browseMode.value);
    if (browseMode.value && question.value) {
      result.value = {
        question_id: question.value.id,
        question_type: question.value.question_type,
        is_self_review: false,
        is_correct: null,
        answer: question.value.answer ?? null,
        analysis: question.value.analysis ?? null,
        checked_at: '',
      };
    }
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

async function submit() {
  if (!question.value || submitting.value) return;
  const answer = question.value.question_type === '选择题' || question.value.question_type === '判断题'
    ? selectedOption.value
    : answerText.value.trim();
  const hasImages = answerImages.value.length > 0;
  if (!answer && !hasImages) return;
  submitting.value = true;
  error.value = '';
  try {
    result.value = hasImages
      ? await PracticeAPI.checkVisual(question.value.id, answer, answerImages.value)
      : await PracticeAPI.check(question.value.id, answer);
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '提交失败';
  } finally {
    submitting.value = false;
  }
}

async function selfReview(correct: boolean) {
  if (!question.value || submitting.value) return;
  submitting.value = true;
  try {
    result.value = await PracticeAPI.check(
      question.value.id,
      answerText.value.trim() || '（自评）',
      correct,
    );
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '自评提交失败';
  } finally {
    submitting.value = false;
  }
}

function askAgent() {
  void router.push({
    path: '/app',
    query: { q: `请讲解试题 QB-${questionId.value}：请先取回完整题目（含题图）再逐步讲解。` },
  });
}

onMounted(() => void load());
</script>

<template>
  <!-- 全局 html/body 是 overflow:hidden（应用壳页面自管滚动），练习独立页必须自带滚动容器 -->
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
        <div class="flex items-center gap-2 text-xs text-text-tertiary">
          <span v-if="question">QB-{{ question.id.slice(0, 10) }} · {{ question.question_type }} · {{ difficultyLabel(question.difficulty) }}</span>
        </div>
      </div>
    </header>

    <main class="mx-auto w-full max-w-3xl flex-1 space-y-4 overflow-y-auto px-6 py-6">
      <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

      <div v-if="loading" class="h-96 animate-pulse rounded-2xl bg-surface-hover" />

      <template v-else-if="question">
        <section class="rounded-2xl border border-border-subtle bg-surface p-6">
          <div class="mb-3 flex flex-wrap gap-2 text-xs">
            <span class="rounded-lg bg-accent-soft px-2 py-0.5 font-bold text-accent">{{ question.question_type }}</span>
            <span class="rounded-lg bg-surface-hover px-2 py-0.5 text-text-secondary">{{ difficultyLabel(question.difficulty) }}</span>
            <span v-for="kp in question.knowledge_points" :key="kp" class="rounded-lg bg-surface-hover px-2 py-0.5 text-text-secondary">{{ kp }}</span>
          </div>
          <MarkdownRenderer :content="question.stem" />
          <div v-if="question.images.length" class="mt-4 space-y-3">
            <figure v-for="image in question.images" :key="image.seq" class="rounded-xl border border-border-subtle bg-surface-sunken p-3">
              <img :src="image.url" :alt="image.caption || `题图${image.seq}`" class="mx-auto max-h-96 object-contain" loading="lazy" />
              <figcaption v-if="image.caption" class="mt-2 text-center text-xs text-text-tertiary">{{ image.caption }}</figcaption>
            </figure>
          </div>
        </section>

        <!-- 做题模式：作答区（提交前不渲染任何答案/解析内容） -->
        <section v-if="!result" class="rounded-2xl border border-border-subtle bg-surface p-6">
          <h2 class="mb-4 text-base font-bold text-text-primary">
            {{ browseMode ? '解答模式' : '作答' }}
          </h2>

          <template v-if="browseMode">
            <p class="text-sm text-text-tertiary">解答模式直接展示参考答案与解析，供复习使用。</p>
          </template>

          <template v-else-if="question.question_type === '选择题'">
            <label
              v-for="option in question.options"
              :key="option.key"
              class="mb-2 flex cursor-pointer items-start gap-3 rounded-xl border px-4 py-3 text-sm transition"
              :class="selectedOption === option.key ? 'border-accent bg-accent-soft' : 'border-border-subtle hover:border-accent/40'"
            >
              <input v-model="selectedOption" type="radio" :value="option.key" class="mt-0.5 accent-[var(--color-accent,#3b82f6)]" />
              <span class="leading-7"><b class="mr-1.5">{{ option.key }}.</b><InlineMath :text="option.text" /></span>
            </label>
          </template>

          <template v-else-if="question.question_type === '判断题'">
            <div class="flex gap-3">
              <button
                v-for="option in judgeOptions"
                :key="option.key"
                type="button"
                class="flex-1 rounded-xl border px-4 py-3 text-sm font-bold transition"
                :class="selectedOption === option.key ? 'border-accent bg-accent-soft' : 'border-border-subtle text-text-secondary hover:border-accent/40'"
                @click="selectedOption = option.key"
              >
                {{ option.label }}
              </button>
            </div>
          </template>

          <template v-else>
            <textarea
              v-model="answerText"
              rows="6"
              placeholder="写下你的解答过程或答案；也可以拍照上传手写解答，由 AI 批改"
              class="w-full rounded-xl border border-border-subtle bg-surface-sunken p-4 text-sm leading-6 outline-none transition focus:border-accent/50"
            />
          </template>

          <!-- 手写解答图片（视觉判分） -->
          <div v-if="!browseMode" class="mt-4">
            <div class="flex flex-wrap items-center gap-2">
              <button
                type="button"
                class="inline-flex items-center gap-1.5 rounded-xl border border-border-subtle px-3.5 py-2 text-xs font-bold text-text-secondary transition hover:border-accent/40 hover:text-accent"
                @click="imageInput?.click()"
              >
                <ImagePlus class="h-4 w-4" /> 上传手写解答
              </button>
              <span class="text-xs text-text-tertiary">拍照上传手写过程，AI 对照参考答案批改并讲解（最多 3 张）</span>
              <input
                ref="imageInput"
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                multiple
                class="hidden"
                @change="onImageChange"
              />
            </div>
            <div v-if="imagePreviews.length" class="mt-3 flex flex-wrap gap-3">
              <div
                v-for="(preview, index) in imagePreviews"
                :key="preview"
                class="relative rounded-xl border border-border-subtle bg-surface-sunken p-2"
              >
                <img :src="preview" alt="手写解答" class="h-24 w-24 rounded-lg object-contain" />
                <button
                  type="button"
                  class="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-rose-500 text-white shadow"
                  @click="removeAnswerImage(index)"
                >
                  <X class="h-3 w-3" />
                </button>
              </div>
            </div>
          </div>

          <div class="mt-4 flex flex-wrap items-center gap-2">
            <button
              v-if="!browseMode"
              type="button"
              :disabled="submitting || (!selectedOption && !answerText.trim() && !answerImages.length)"
              class="rounded-xl bg-accent px-5 py-2 text-sm font-bold text-white transition hover:bg-accent-hover disabled:opacity-40"
              @click="submit"
            >
              {{ submitting ? '判分中…' : answerImages.length ? '提交 AI 批改' : '提交判分' }}
            </button>
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-xl border border-border-subtle px-4 py-2 text-sm font-medium text-text-secondary transition hover:border-accent/40 hover:text-accent"
              @click="askAgent"
            >
              <Bot class="h-4 w-4" /> 问智能体
            </button>
          </div>
        </section>

        <!-- 结果与解析（判分后 / 解答模式） -->
        <section v-if="result" class="space-y-4">
          <div
            v-if="result.visual_grading && verdictMeta"
            class="rounded-2xl border px-5 py-4"
            :class="verdictMeta.cls"
          >
            <div class="flex flex-wrap items-center gap-3">
              <CheckCircle2 v-if="result.visual_grading.verdict === 'correct'" class="h-6 w-6" />
              <XCircle v-else class="h-6 w-6" />
              <p class="text-base font-bold">{{ verdictMeta.label }}</p>
              <span
                v-if="result.visual_grading.score !== null"
                class="rounded-lg bg-white/60 px-2.5 py-0.5 text-sm font-bold"
              >
                得分 {{ result.visual_grading.score }}/100
              </span>
              <span class="text-xs opacity-70">AI 批改（对照教师参考答案）</span>
            </div>
          </div>

          <div
            v-else-if="result.is_correct !== null"
            class="flex items-center gap-3 rounded-2xl border px-5 py-4"
            :class="result.is_correct
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
              : 'border-rose-200 bg-rose-50 text-rose-700'"
          >
            <CheckCircle2 v-if="result.is_correct" class="h-6 w-6" />
            <XCircle v-else class="h-6 w-6" />
            <p class="text-base font-bold">{{ result.is_correct ? '回答正确' : '回答错误，看看解析' }}</p>
          </div>

          <div v-if="result.is_correct === null && !result.visual_grading" class="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-4">
            <p class="text-sm font-bold text-amber-800">主观题 · 请对照参考答案自评</p>
            <div class="mt-3 flex gap-2">
              <button
                type="button"
                :disabled="submitting"
                class="rounded-xl bg-emerald-600 px-4 py-1.5 text-sm font-bold text-white transition hover:bg-emerald-700 disabled:opacity-40"
                @click="selfReview(true)"
              >
                我已掌握
              </button>
              <button
                type="button"
                :disabled="submitting"
                class="rounded-xl border border-rose-300 px-4 py-1.5 text-sm font-bold text-rose-700 transition hover:bg-rose-100 disabled:opacity-40"
                @click="selfReview(false)"
              >
                仍不会（进错题本）
              </button>
            </div>
          </div>

          <section v-if="result.visual_grading" class="rounded-2xl border border-border-subtle bg-surface p-6">
            <h2 class="mb-3 text-base font-bold text-text-primary">AI 批改意见</h2>
            <p v-if="result.visual_grading.comment" class="mb-4 text-sm leading-6 text-text-secondary">{{ result.visual_grading.comment }}</p>
            <h3 class="mb-2 text-sm font-bold text-text-primary">讲解</h3>
            <MarkdownRenderer :content="result.visual_grading.explanation || '（无）'" />
          </section>

          <section class="rounded-2xl border border-border-subtle bg-surface p-6">
            <h2 class="mb-3 text-base font-bold text-text-primary">参考答案</h2>
            <MarkdownRenderer :content="result.answer || '（教师未填写答案）'" />
            <h2 class="mb-3 mt-5 text-base font-bold text-text-primary">解析</h2>
            <MarkdownRenderer :content="result.analysis || '（教师未填写解析）'" />
            <div class="mt-5 flex flex-wrap gap-2">
              <button
                type="button"
                class="rounded-xl bg-accent px-4 py-1.5 text-sm font-bold text-white transition hover:bg-accent-hover"
                @click="router.push('/practice')"
              >
                继续做题
              </button>
              <button
                type="button"
                class="inline-flex items-center gap-1.5 rounded-xl border border-border-subtle px-4 py-1.5 text-sm font-medium text-text-secondary transition hover:border-accent/40 hover:text-accent"
                @click="askAgent"
              >
                <Bot class="h-4 w-4" /> 问智能体讲解
              </button>
            </div>
          </section>
        </section>
      </template>
    </main>
  </div>
</template>
