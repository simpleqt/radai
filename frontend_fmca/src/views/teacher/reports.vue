<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { Plus, Trash2 } from 'lucide-vue-next';
import { TeacherAnalyticsAPI, type ReportItem } from '@/api/teacher-analytics';

const router = useRouter();
const loading = ref(true);
const error = ref('');
const items = ref<ReportItem[]>([]);
const showForm = ref(false);
const rangeStart = ref('');
const rangeEnd = ref('');
const submitting = ref(false);

const statusLabel: Record<string, string> = {
  running: '生成中',
  done: '已完成',
  failed: '失败',
};

function defaultRange() {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - 29);
  const toISO = (d: Date) => d.toISOString().slice(0, 10);
  rangeStart.value = toISO(start);
  rangeEnd.value = toISO(end);
}

function fmt(value: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN', { hour12: false });
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const result = await TeacherAnalyticsAPI.reports();
    items.value = result.items;
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

async function create() {
  submitting.value = true;
  error.value = '';
  try {
    await TeacherAnalyticsAPI.createReport({ from: rangeStart.value, to: rangeEnd.value });
    showForm.value = false;
    await load();
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '创建失败';
  } finally {
    submitting.value = false;
  }
}

async function remove(id: string) {
  if (!confirm('确定删除该报告吗？')) return;
  try {
    await TeacherAnalyticsAPI.deleteReport(id);
    await load();
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '删除失败';
  }
}

const anyRunning = computed(() => items.value.some((item) => item.status === 'running'));

onMounted(() => {
  defaultRange();
  void load();
});
</script>

<template>
  <div class="space-y-5">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-xl font-extrabold text-text-primary">分析报告</h1>
        <p class="mt-1 text-sm text-text-tertiary">对基础力学智能体的学生提问生成汇总分析</p>
      </div>
      <button type="button" class="inline-flex items-center gap-2 rounded-xl bg-accent px-4 py-2 text-sm font-bold text-white hover:bg-accent-hover transition" @click="showForm = !showForm">
        <Plus class="w-4 h-4" /> 新建分析
      </button>
    </header>

    <div v-if="showForm" class="rounded-2xl border border-border-subtle bg-surface p-5">
      <div class="grid gap-3 md:grid-cols-3">
        <label>
          <span class="text-xs font-semibold text-text-tertiary">开始日期</span>
          <input v-model="rangeStart" type="date" class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm text-text-primary outline-none focus:border-accent" />
        </label>
        <label>
          <span class="text-xs font-semibold text-text-tertiary">结束日期</span>
          <input v-model="rangeEnd" type="date" class="mt-1 h-10 w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 text-sm text-text-primary outline-none focus:border-accent" />
        </label>
        <div class="flex items-end gap-2">
          <button type="button" class="h-10 flex-1 rounded-xl bg-accent px-4 text-sm font-bold text-white hover:bg-accent-hover transition disabled:opacity-50" :disabled="submitting" @click="create">
            {{ submitting ? '创建中…' : '开始生成' }}
          </button>
          <button type="button" class="h-10 rounded-xl border border-border-subtle px-4 text-sm text-text-secondary hover:bg-surface-hover transition" @click="showForm = false">取消</button>
        </div>
      </div>
      <p v-if="anyRunning" class="mt-3 text-xs text-amber-600">已有分析任务正在生成，请等待完成后再新建。</p>
    </div>

    <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

    <div v-if="loading" class="space-y-2">
      <div v-for="i in 4" :key="i" class="skeleton-shimmer h-16 animate-pulse rounded-xl bg-surface-hover" />
    </div>

    <div v-else-if="items.length === 0" class="rounded-2xl border border-border-subtle bg-surface p-10 text-center text-sm text-text-tertiary">
      还没有分析报告，点击「新建分析」开始。
    </div>

    <div v-else class="space-y-2">
      <div
        v-for="item in items"
        :key="item.id"
        class="flex cursor-pointer items-center gap-4 rounded-xl border border-border-subtle bg-surface px-4 py-3 transition hover:border-accent/40"
        @click="router.push(`/teacher/reports/${item.id}`)"
      >
        <div class="min-w-0 flex-1">
          <p class="text-sm font-semibold text-text-primary">
            {{ fmt(item.range_start) }} ~ {{ fmt(item.range_end) }}
          </p>
          <p class="mt-1 text-xs text-text-tertiary">
            提问 {{ item.question_count }} 条 · 创建于 {{ fmt(item.created_at) }}
          </p>
        </div>
        <span
          class="rounded-full px-2.5 py-1 text-xs font-semibold"
          :class="{
            'bg-amber-50 text-amber-600': item.status === 'running',
            'bg-emerald-50 text-emerald-600': item.status === 'done',
            'bg-rose-50 text-rose-600': item.status === 'failed',
          }"
        >
          {{ statusLabel[item.status] || item.status }}
        </span>
        <button
          type="button"
          class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-text-tertiary transition hover:bg-rose-50 hover:text-rose-500"
          title="删除"
          aria-label="删除报告"
          @click.stop="remove(item.id)"
        >
          <Trash2 class="w-4 h-4" />
        </button>
      </div>
    </div>
  </div>
</template>
