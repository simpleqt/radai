<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ArrowLeft, Plus, Save, Trash2 } from 'lucide-vue-next';
import MarkdownRenderer from '@/views/ne-full/components/MarkdownRenderer.vue';
import {
  DIFFICULTIES,
  QuestionBankAPI,
  QUESTION_TYPES,
  type QBKnowledgePoint,
  type QBQuestion,
} from '@/api/question-bank';

const route = useRoute();
const router = useRouter();
const questionId = computed(() => String(route.params.id || ''));
const isCreate = computed(() => questionId.value === 'new');

const loading = ref(true);
const saving = ref(false);
const error = ref('');
const savedHint = ref('');

const questionType = ref('计算题');
const stem = ref('');
const optionsText = ref('');
const answer = ref('');
const analysis = ref('');
const difficulty = ref('');
const chapter = ref('');
const sourceName = ref('');
const knowledgePoints = ref<string[]>([]);
const status = ref('published');
const images = ref<QBQuestion['images']>([]);
const knowledgePointOptions = ref<QBKnowledgePoint[]>([]);
const newKnowledgePoint = ref('');
const addingKnowledgePoint = ref(false);

async function addKnowledgePoint() {
  const name = newKnowledgePoint.value.trim();
  if (!name || addingKnowledgePoint.value) return;
  if (knowledgePointOptions.value.some((item) => item.name === name)) {
    knowledgePoints.value = Array.from(new Set([...knowledgePoints.value, name]));
    newKnowledgePoint.value = '';
    return;
  }
  addingKnowledgePoint.value = true;
  error.value = '';
  try {
    const created = await QuestionBankAPI.createKnowledgePoint({ name });
    knowledgePointOptions.value = [...knowledgePointOptions.value, created];
    knowledgePoints.value = Array.from(new Set([...knowledgePoints.value, created.name]));
    newKnowledgePoint.value = '';
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '知识点新增失败';
  } finally {
    addingKnowledgePoint.value = false;
  }
}

const optionItems = computed(() =>
  optionsText.value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^([A-H])\s*[.、．:：]?\s*(.*)$/);
      return match ? { key: match[1], text: match[2] } : { key: '', text: line };
    }),
);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    knowledgePointOptions.value = (await QuestionBankAPI.knowledgePoints()).items;
    if (!isCreate.value) {
      const detail = await QuestionBankAPI.question(questionId.value);
      questionType.value = detail.question_type;
      stem.value = detail.stem;
      optionsText.value = (detail.options || []).map((option) => `${option.key}. ${option.text}`).join('\n');
      answer.value = detail.answer || '';
      analysis.value = detail.analysis || '';
      difficulty.value = detail.difficulty || '';
      chapter.value = detail.chapter || '';
      sourceName.value = detail.source_name || '';
      knowledgePoints.value = detail.knowledge_points || [];
      status.value = detail.status;
      images.value = detail.images || [];
    }
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (saving.value) return;
  if (!stem.value.trim()) {
    error.value = '题干不能为空';
    return;
  }
  saving.value = true;
  error.value = '';
  savedHint.value = '';
  try {
    const payload: Record<string, unknown> = {
      question_type: questionType.value,
      stem: stem.value,
      options: questionType.value === '选择题' ? optionItems.value.filter((option) => option.text) : [],
      answer: answer.value,
      analysis: analysis.value,
      difficulty: difficulty.value || null,
      chapter: chapter.value,
      source_name: sourceName.value,
      knowledge_points: knowledgePoints.value,
    };
    if (isCreate.value) {
      const created = await QuestionBankAPI.createQuestion(payload);
      void router.replace(`/teacher/bank/${created.id}`);
      savedHint.value = '题目已创建并发布';
    } else {
      payload.status = status.value;
      await QuestionBankAPI.updateQuestion(questionId.value, payload);
      savedHint.value = '已保存';
    }
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '保存失败';
  } finally {
    saving.value = false;
  }
}

async function disableQuestion() {
  if (isCreate.value) return;
  try {
    await QuestionBankAPI.deleteQuestion(questionId.value);
    void router.push('/teacher/bank');
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '下线失败';
  }
}

const imageFileInput = ref<HTMLInputElement | null>(null);

async function onImageChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file || isCreate.value) return;
  try {
    const image = await QuestionBankAPI.addImage(questionId.value, file, '');
    images.value = [...images.value, image];
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '图片上传失败';
  } finally {
    input.value = '';
  }
}

async function removeImage(seq: number) {
  if (isCreate.value) return;
  try {
    await QuestionBankAPI.removeImage(questionId.value, seq);
    images.value = images.value.filter((image) => image.seq !== seq);
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '图片删除失败';
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="space-y-4">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <button
        type="button"
        class="inline-flex items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-sm font-medium text-text-secondary transition hover:bg-surface-hover hover:text-text-primary"
        @click="router.push('/teacher/bank')"
      >
        <ArrowLeft class="h-4 w-4" /> 返回题库
      </button>
      <div class="flex items-center gap-2">
        <span v-if="savedHint" class="text-xs font-bold text-emerald-600">{{ savedHint }}</span>
        <button
          v-if="!isCreate"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-xl border border-rose-200 px-4 py-2 text-sm font-medium text-rose-600 transition hover:bg-rose-50"
          @click="disableQuestion"
        >
          <Trash2 class="h-4 w-4" /> 下线题目
        </button>
        <button
          type="button"
          :disabled="saving"
          class="inline-flex items-center gap-2 rounded-xl bg-accent px-5 py-2 text-sm font-bold text-white transition hover:bg-accent-hover disabled:opacity-40"
          @click="save"
        >
          <Save class="h-4 w-4" /> {{ saving ? '保存中…' : isCreate ? '创建并发布' : '保存' }}
        </button>
      </div>
    </header>

    <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

    <div v-if="loading" class="h-96 animate-pulse rounded-2xl bg-surface-hover" />

    <template v-else>
      <section class="rounded-2xl border border-border-subtle bg-surface p-6">
        <div class="mb-4 grid gap-3 md:grid-cols-4">
          <label class="text-xs font-bold text-text-secondary">
            题型
            <select v-model="questionType" class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50">
              <option v-for="item in QUESTION_TYPES" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>
          <label class="text-xs font-bold text-text-secondary">
            难度
            <select v-model="difficulty" class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50">
              <option value="">未标注</option>
              <option v-for="item in DIFFICULTIES" :key="item.value" :value="item.value">{{ item.label }}</option>
            </select>
          </label>
          <label class="text-xs font-bold text-text-secondary">
            章节
            <input v-model="chapter" type="text" class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50" />
          </label>
          <label class="text-xs font-bold text-text-secondary">
            来源
            <input v-model="sourceName" type="text" placeholder="如 2024-2025 期末卷 第 3 题" class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50" />
          </label>
        </div>

        <label class="block text-xs font-bold text-text-secondary">
          适用知识点（多选，来自字典；可直接新增）
          <div class="mt-1.5 flex max-h-40 flex-wrap gap-1.5 overflow-y-auto rounded-xl border border-border-subtle bg-surface-sunken p-2.5">
            <label
              v-for="item in knowledgePointOptions"
              :key="item.id"
              class="flex cursor-pointer items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs transition"
              :class="knowledgePoints.includes(item.name) ? 'border-accent bg-accent-soft text-accent' : 'border-border-subtle text-text-secondary hover:border-accent/40'"
            >
              <input
                :checked="knowledgePoints.includes(item.name)"
                type="checkbox"
                class="accent-[var(--color-accent,#3b82f6)]"
                @change="knowledgePoints = knowledgePoints.includes(item.name)
                  ? knowledgePoints.filter((name) => name !== item.name)
                  : [...knowledgePoints, item.name]"
              />
              {{ item.name }}
            </label>
            <span v-if="!knowledgePointOptions.length" class="px-1 py-0.5 text-xs text-text-tertiary">字典为空，可直接在下方新增</span>
          </div>
          <div class="mt-2 flex gap-2">
            <input
              v-model="newKnowledgePoint"
              type="text"
              placeholder="新知识点名称，如 平面力系的平衡"
              class="h-9 flex-1 rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50"
              @keyup.enter="addKnowledgePoint"
            />
            <button
              type="button"
              :disabled="addingKnowledgePoint || !newKnowledgePoint.trim()"
              class="inline-flex items-center gap-1.5 rounded-xl border border-border-subtle px-3.5 py-1.5 text-xs font-bold text-text-secondary transition hover:border-accent/40 hover:text-accent disabled:opacity-40"
              @click="addKnowledgePoint"
            >
              <Plus class="h-3.5 w-3.5" /> {{ addingKnowledgePoint ? '新增中…' : '新增并选中' }}
            </button>
          </div>
        </label>

        <label class="mt-4 block text-xs font-bold text-text-secondary">
          题干（支持 LaTeX：行内 $...$，独立 $$...$$）
          <textarea v-model="stem" rows="6" class="mt-1 w-full rounded-xl border border-border-subtle bg-surface-sunken p-3 text-sm leading-6 outline-none focus:border-accent/50" />
        </label>
        <details class="mt-2">
          <summary class="cursor-pointer text-xs text-text-tertiary">题干预览</summary>
          <div class="mt-2 rounded-xl border border-border-subtle p-3"><MarkdownRenderer :content="stem || '（空）'" /></div>
        </details>

        <label v-if="questionType === '选择题'" class="mt-4 block text-xs font-bold text-text-secondary">
          选项（每行一个，A. 开头）
          <textarea v-model="optionsText" rows="4" placeholder="A. 10kN·m&#10;B. 20kN·m" class="mt-1 w-full rounded-xl border border-border-subtle bg-surface-sunken p-3 text-sm leading-6 outline-none focus:border-accent/50" />
        </label>

        <label class="mt-4 block text-xs font-bold text-text-secondary">
          参考答案
          <textarea v-model="answer" rows="2" class="mt-1 w-full rounded-xl border border-border-subtle bg-surface-sunken p-3 text-sm leading-6 outline-none focus:border-accent/50" />
        </label>
        <label class="mt-4 block text-xs font-bold text-text-secondary">
          解析
          <textarea v-model="analysis" rows="5" class="mt-1 w-full rounded-xl border border-border-subtle bg-surface-sunken p-3 text-sm leading-6 outline-none focus:border-accent/50" />
        </label>

        <label v-if="!isCreate" class="mt-4 flex items-center gap-2 text-xs font-bold text-text-secondary">
          状态
          <select v-model="status" class="h-9 rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50">
            <option value="published">已发布</option>
            <option value="disabled">已停用</option>
          </select>
        </label>
      </section>

      <section class="rounded-2xl border border-border-subtle bg-surface p-6">
        <div class="flex items-center justify-between">
          <h2 class="text-base font-bold text-text-primary">题图（入库即与本题强绑定）</h2>
          <button
            v-if="!isCreate"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-xl border border-border-subtle px-3 py-1.5 text-xs font-bold text-text-secondary transition hover:border-accent/40 hover:text-accent"
            @click="imageFileInput?.click()"
          >
            <Plus class="h-3.5 w-3.5" /> 添加图片
          </button>
          <span v-else class="text-xs text-text-tertiary">保存题目后可添加图片</span>
          <input ref="imageFileInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp" class="hidden" @change="onImageChange" />
        </div>
        <div v-if="images.length" class="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <figure v-for="image in images" :key="image.seq" class="rounded-xl border border-border-subtle bg-surface-sunken p-3">
            <img :src="image.url" :alt="image.caption || `题图${image.seq}`" class="mx-auto max-h-56 object-contain" loading="lazy" />
            <figcaption class="mt-2 flex items-center justify-between gap-2 text-xs text-text-tertiary">
              <span>图 {{ image.seq }}{{ image.caption ? ` · ${image.caption}` : '' }}</span>
              <button type="button" class="text-rose-500 hover:text-rose-600" @click="removeImage(image.seq)">删除</button>
            </figcaption>
          </figure>
        </div>
        <p v-else class="mt-4 text-sm text-text-tertiary">暂无题图。受力图/结构简图是力学题的核心，建议补齐。</p>
      </section>
    </template>
  </div>
</template>
