<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { Plus, Tags, Trash2 } from 'lucide-vue-next';
import { QuestionBankAPI, type QBKnowledgePoint } from '@/api/question-bank';

const loading = ref(true);
const error = ref('');
const items = ref<QBKnowledgePoint[]>([]);
const newName = ref('');
const newChapter = ref('');
const creating = ref(false);

async function load() {
  loading.value = true;
  try {
    items.value = (await QuestionBankAPI.knowledgePoints()).items;
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

async function create() {
  if (!newName.value.trim() || creating.value) return;
  creating.value = true;
  error.value = '';
  try {
    await QuestionBankAPI.createKnowledgePoint({
      name: newName.value.trim(),
      chapter: newChapter.value.trim() || null,
    });
    newName.value = '';
    newChapter.value = '';
    await load();
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '新增失败';
  } finally {
    creating.value = false;
  }
}

async function updateChapter(item: QBKnowledgePoint) {
  try {
    const next = window.prompt('修改知识点所属章节（留空清除）', item.chapter || '');
    if (next === null) return;
    await QuestionBankAPI.updateKnowledgePoint(item.id, { chapter: next.trim() || null });
    item.chapter = next.trim() || null;
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '修改失败';
  }
}

async function remove(item: QBKnowledgePoint) {
  if (!window.confirm(`确定删除知识点「${item.name}」？已标注题目的标签字符串不会被回收。`)) return;
  try {
    await QuestionBankAPI.deleteKnowledgePoint(item.id);
    items.value = items.value.filter((point) => point.id !== item.id);
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '删除失败';
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="space-y-4">
    <header>
      <h1 class="flex items-center gap-2 text-xl font-extrabold text-text-primary">
        <Tags class="h-5 w-5 text-accent" /> 知识点字典
      </h1>
      <p class="mt-1 text-sm text-text-tertiary">
        受控枚举：题目标注、学生筛选与教学分析聚合共用同一套命名，避免自由标签碎片化
      </p>
    </header>

    <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

    <section class="rounded-2xl border border-border-subtle bg-surface p-5">
      <div class="flex flex-wrap items-end gap-3">
        <label class="text-xs font-bold text-text-secondary">
          名称
          <input v-model="newName" type="text" placeholder="如 平面力系的平衡" class="mt-1 h-10 w-64 rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50" @keyup.enter="create" />
        </label>
        <label class="text-xs font-bold text-text-secondary">
          所属章节（可选）
          <input v-model="newChapter" type="text" placeholder="如 第 3 章 平面力系" class="mt-1 h-10 w-72 rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm outline-none focus:border-accent/50" @keyup.enter="create" />
        </label>
        <button
          type="button"
          :disabled="creating || !newName.trim()"
          class="inline-flex items-center gap-1.5 rounded-xl bg-accent px-4 py-2 text-sm font-bold text-white transition hover:bg-accent-hover disabled:opacity-40"
          @click="create"
        >
          <Plus class="h-4 w-4" /> 新增
        </button>
      </div>
    </section>

    <div v-if="loading" class="h-48 animate-pulse rounded-2xl bg-surface-hover" />

    <template v-else>
      <p v-if="!items.length" class="rounded-2xl border border-border-subtle bg-surface px-4 py-12 text-center text-sm text-text-tertiary">
        字典为空——录入知识点后，题目标注与练习筛选才能按知识点联动
      </p>
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <div
          v-for="item in items"
          :key="item.id"
          class="flex items-center justify-between gap-3 rounded-2xl border border-border-subtle bg-surface p-4"
        >
          <div class="min-w-0">
            <p class="truncate text-sm font-bold text-text-primary">{{ item.name }}</p>
            <button type="button" class="mt-0.5 text-xs text-text-tertiary hover:text-accent" @click="updateChapter(item)">
              {{ item.chapter || '未设章节（点击设置）' }}
            </button>
          </div>
          <button
            type="button"
            class="shrink-0 rounded-xl border border-rose-200 p-2 text-rose-500 transition hover:bg-rose-50"
            title="删除"
            @click="remove(item)"
          >
            <Trash2 class="h-4 w-4" />
          </button>
        </div>
      </div>
    </template>
  </div>
</template>
