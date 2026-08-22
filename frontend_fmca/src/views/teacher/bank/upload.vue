<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { AlertTriangle, CheckCircle2, Download, FileUp, XCircle } from 'lucide-vue-next';
import { authState } from '@/utils/request';
import { QuestionBankAPI, type QBBatch } from '@/api/question-bank';

const router = useRouter();
const fileInput = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
const confirming = ref(false);
const error = ref('');
const batch = ref<QBBatch | null>(null);

async function downloadTemplate() {
  const response = await fetch('/api/v2/teacher/question-bank/template', {
    headers: { Authorization: `Bearer ${authState.value.token || ''}` },
  });
  if (!response.ok) {
    error.value = `模板下载失败（HTTP ${response.status}）`;
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'question-bank-template.docx';
  anchor.click();
  URL.revokeObjectURL(url);
}

async function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  uploading.value = true;
  error.value = '';
  batch.value = null;
  try {
    batch.value = await QuestionBankAPI.uploadBatch(file);
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '上传解析失败';
  } finally {
    uploading.value = false;
    input.value = '';
  }
}

async function confirmBatch() {
  if (!batch.value || confirming.value) return;
  confirming.value = true;
  try {
    const result = await QuestionBankAPI.confirmBatch(batch.value.id);
    batch.value = null;
    void router.push({
      path: '/teacher/bank',
      query: {
        inserted: String(result.inserted),
        skipped: String(result.skipped_duplicates ?? 0),
      },
    });
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '入库失败';
  } finally {
    confirming.value = false;
  }
}

async function discardBatch() {
  if (!batch.value) return;
  try {
    await QuestionBankAPI.deleteBatch(batch.value.id);
    batch.value = null;
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '废弃失败';
  }
}
</script>

<template>
  <div class="space-y-5">
    <header>
      <h1 class="text-xl font-extrabold text-text-primary">批量上传试题</h1>
      <p class="mt-1 text-sm text-text-tertiary">
        使用标准 Word 模板（固定标记 + ==== 分隔行），受力图直接粘贴在题干位置；答案与解析须自填，系统不生成
      </p>
    </header>

    <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

    <section class="rounded-2xl border border-border-subtle bg-surface p-6">
      <div class="flex flex-wrap items-center gap-3">
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-xl border border-border-subtle px-4 py-2 text-sm font-bold text-text-secondary transition hover:border-accent/40 hover:text-accent"
          @click="downloadTemplate"
        >
          <Download class="h-4 w-4" /> 下载模板 docx
        </button>
        <button
          type="button"
          :disabled="uploading"
          class="inline-flex items-center gap-2 rounded-xl bg-accent px-4 py-2 text-sm font-bold text-white transition hover:bg-accent-hover disabled:opacity-40"
          @click="fileInput?.click()"
        >
          <FileUp class="h-4 w-4" /> {{ uploading ? '解析中…' : '选择 docx 上传' }}
        </button>
        <input ref="fileInput" type="file" accept=".docx" class="hidden" @change="onFileChange" />
        <span class="text-xs text-text-tertiary">≤20MB，单批 ≤500 题，每题图 ≤6 张</span>
      </div>
      <pre class="mt-4 overflow-x-auto rounded-xl bg-surface-sunken p-4 text-xs leading-5 text-text-secondary">【题型】计算题
【题干】图示简支梁受均布载荷……（此处粘贴受力图）
【选项】A. …  B. …（仅选择题）
【答案】…
【解析】…
【难度】中等（简单/中等/较难）
【知识点】平面力系的平衡；受力图（分号分隔）
【章节】第 3 章 平面力系
【来源】2024-2025 期末卷 第 3 题
====</pre>
    </section>

    <section v-if="batch" class="space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border-subtle bg-surface p-5">
        <div>
          <p class="text-sm font-bold text-text-primary">{{ batch.file_name }}</p>
          <p class="mt-1 text-xs text-text-tertiary">
            解析 {{ batch.total_count }} 题 · 可入库 {{ batch.valid_count }} 题
            <span v-if="batch.parser"> · 解析器 {{ batch.parser }}</span>
          </p>
        </div>
        <div class="flex gap-2">
          <button
            type="button"
            :disabled="confirming"
            class="rounded-xl bg-accent px-5 py-2 text-sm font-bold text-white transition hover:bg-accent-hover disabled:opacity-40"
            @click="confirmBatch"
          >
            {{ confirming ? '入库中…' : `确认入库 ${batch.valid_count} 题` }}
          </button>
          <button
            type="button"
            class="rounded-xl border border-rose-200 px-4 py-2 text-sm font-medium text-rose-600 transition hover:bg-rose-50"
            @click="discardBatch"
          >
            废弃本批
          </button>
        </div>
      </div>

      <article
        v-for="item in batch.questions || []"
        :key="item.id"
        class="rounded-2xl border p-5"
        :class="item.has_error ? 'border-rose-200 bg-rose-50/40' : 'border-border-subtle bg-surface'"
      >
        <div class="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span class="rounded-lg bg-accent-soft px-2 py-0.5 font-bold text-accent">{{ item.question_type || '题型未知' }}</span>
          <span class="text-text-tertiary">第 {{ item.seq }} 题</span>
          <span v-if="item.has_error" class="inline-flex items-center gap-1 font-bold text-rose-600">
            <XCircle class="h-3.5 w-3.5" /> 不入库
          </span>
        </div>
        <p class="text-sm leading-6">{{ item.stem?.slice(0, 160) || '（题干缺失）' }}</p>
        <div v-if="item.images?.length" class="mt-2 flex gap-2 overflow-x-auto">
          <img
            v-for="image in item.images"
            :key="image.seq"
            :src="image.url"
            :alt="`题图${image.seq}`"
            class="h-20 w-20 shrink-0 rounded-lg border border-border-subtle object-contain"
            loading="lazy"
          />
        </div>
        <ul class="mt-2 space-y-1">
          <li
            v-for="(issue, index) in item.issues"
            :key="index"
            class="flex items-center gap-1.5 text-xs"
            :class="issue.level === 'error' ? 'text-rose-600' : 'text-amber-600'"
          >
            <AlertTriangle v-if="issue.level === 'warning'" class="h-3.5 w-3.5" />
            <XCircle v-else class="h-3.5 w-3.5" />
            {{ issue.message }}
          </li>
          <li v-if="!item.issues.length" class="flex items-center gap-1.5 text-xs text-emerald-600">
            <CheckCircle2 class="h-3.5 w-3.5" /> 校验通过
          </li>
        </ul>
      </article>
    </section>
  </div>
</template>
