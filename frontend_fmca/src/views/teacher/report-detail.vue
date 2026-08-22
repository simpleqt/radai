<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ArrowLeft, RefreshCw } from 'lucide-vue-next';
import { getECharts, type EChartsInstance } from '@/views/ne-full/components/echartsLoader';
import { TeacherAnalyticsAPI, type ReportItem, type ReportResult } from '@/api/teacher-analytics';

const route = useRoute();
const router = useRouter();
const reportId = String(route.params.id || '');

const loading = ref(true);
const error = ref('');
const report = ref<(ReportItem & { result: ReportResult | null }) | null>(null);
const expandedClusters = ref(new Set<string>());

const trendHost = ref<HTMLElement | null>(null);
const topHost = ref<HTMLElement | null>(null);
const keywordHost = ref<HTMLElement | null>(null);
const trendChart = ref<EChartsInstance | null>(null);
const topChart = ref<EChartsInstance | null>(null);
const keywordChart = ref<EChartsInstance | null>(null);

const statusLabel: Record<string, string> = {
  running: '生成中',
  done: '已完成',
  failed: '失败',
};

const result = computed(() => report.value?.result || null);
const isRunning = computed(() => report.value?.status === 'running');

function fmt(value: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString('zh-CN', { hour12: false });
}

function crossCategoryLabel(category: string): string {
  if (category === 'asked_and_wrong') return '问得多且错得多';
  if (category === 'wrong_only') return '没人问但错得多';
  return '问得多但正确率高';
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    report.value = await TeacherAnalyticsAPI.report(reportId);
    if (report.value.status !== 'running') {
      await nextTick();
      renderCharts();
    }
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

function renderCharts() {
  void (async () => {
    const echarts = await getECharts();
    if (trendHost.value && result.value?.trend?.length) {
      trendChart.value?.dispose();
      trendChart.value = echarts.init(trendHost.value);
      trendChart.value.setOption({
        tooltip: { trigger: 'axis' },
        grid: { left: 40, right: 16, top: 24, bottom: 30 },
        xAxis: { type: 'category', data: result.value.trend.map((p) => p.date), axisLabel: { fontSize: 11 } },
        yAxis: { type: 'value', minInterval: 1 },
        series: [{ type: 'line', data: result.value.trend.map((p) => p.count), smooth: true, areaStyle: { opacity: 0.12 }, itemStyle: { color: '#3b82f6' } }],
      });
    }
    if (topHost.value && result.value?.top_questions?.length) {
      topChart.value?.dispose();
      topChart.value = echarts.init(topHost.value);
      const top = [...result.value.top_questions].reverse();
      topChart.value.setOption({
        tooltip: { trigger: 'axis' },
        grid: { left: 8, right: 40, top: 8, bottom: 8, containLabel: true },
        xAxis: { type: 'value', minInterval: 1 },
        yAxis: { type: 'category', data: top.map((q) => (q.question || '').slice(0, 18)), axisLabel: { fontSize: 11 } },
        series: [{ type: 'bar', data: top.map((q) => q.count), barMaxWidth: 18, itemStyle: { color: '#6366f1', borderRadius: [0, 4, 4, 0] } }],
      });
    }
    if (keywordHost.value && result.value?.top_keywords?.length) {
      keywordChart.value?.dispose();
      keywordChart.value = echarts.init(keywordHost.value);
      const keywords = [...result.value.top_keywords].slice(0, 20).reverse();
      keywordChart.value.setOption({
        tooltip: { trigger: 'axis' },
        grid: { left: 8, right: 40, top: 8, bottom: 8, containLabel: true },
        xAxis: { type: 'value', minInterval: 1 },
        yAxis: { type: 'category', data: keywords.map((k) => k.keyword || ''), axisLabel: { fontSize: 11 } },
        series: [{ type: 'bar', data: keywords.map((k) => k.count), barMaxWidth: 16, itemStyle: { color: '#14b8a6', borderRadius: [0, 4, 4, 0] } }],
      });
    }
  })().catch(() => {});
}

let pollTimer: number | null = null;

watch(isRunning, (running) => {
  if (running && !pollTimer) {
    pollTimer = window.setInterval(() => void load(), 3000);
  } else if (!running && pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
});

onMounted(() => void load());
onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer);
  trendChart.value?.dispose();
  topChart.value?.dispose();
  keywordChart.value?.dispose();
});
</script>

<template>
  <div class="space-y-5">
    <header class="flex items-center justify-between">
      <button type="button" class="inline-flex items-center gap-2 text-sm font-semibold text-text-secondary hover:text-accent transition" @click="router.push('/teacher/reports')">
        <ArrowLeft class="w-4 h-4" /> 返回报告列表
      </button>
      <button v-if="isRunning" type="button" class="inline-flex items-center gap-2 text-sm text-text-tertiary">
        <RefreshCw class="w-4 h-4 animate-spin" /> 生成中，自动刷新…
      </button>
    </header>

    <div v-if="loading" class="rounded-2xl border border-border-subtle bg-surface p-10">
      <div class="skeleton-shimmer h-6 w-64 animate-pulse rounded bg-surface-hover" />
      <div class="skeleton-shimmer mt-4 h-40 animate-pulse rounded-xl bg-surface-hover" />
    </div>

    <div v-else-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</div>

    <template v-else-if="report">
      <div class="rounded-2xl border border-border-subtle bg-surface p-5">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 class="text-lg font-extrabold text-text-primary">
              {{ fmt(report.range_start) }} ~ {{ fmt(report.range_end) }}
            </h1>
            <p class="mt-1 text-sm text-text-tertiary">
              提问 {{ report.question_count }} 条
              <span v-if="report.model_name" class="ml-2">· 模型 {{ report.model_name }}</span>
            </p>
          </div>
          <span
            class="rounded-full px-2.5 py-1 text-xs font-semibold"
            :class="{
              'bg-amber-50 text-amber-600': report.status === 'running',
              'bg-emerald-50 text-emerald-600': report.status === 'done',
              'bg-rose-50 text-rose-600': report.status === 'failed',
            }"
          >
            {{ statusLabel[report.status] || report.status }}
          </span>
        </div>
      </div>

      <div v-if="report.status === 'failed'" class="rounded-2xl border border-rose-200 bg-rose-50 p-5 text-sm text-rose-700">
        报告生成失败：{{ report.error || '未知错误' }}
      </div>

      <div v-else-if="report.status === 'running'" class="rounded-2xl border border-border-subtle bg-surface p-10 text-center text-sm text-text-tertiary">
        报告正在生成，页面会自动刷新…
      </div>

      <template v-else-if="result">
        <div v-if="result.llm_status === 'degraded'" class="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-700">
          本次分析的语义聚类/教学总结因模型不可用而降级，仅展示确定性统计结果。
        </div>
        <div v-if="result.sampled" class="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-700">
          窗口内提问共 {{ result.sampled_total ?? report.question_count }} 条，超出单次分析上限，语义聚类基于均匀抽样的部分数据；趋势与高频统计仍覆盖全量。
        </div>

        <section v-if="result.summary" class="rounded-2xl border border-border-subtle bg-surface p-5">
          <h2 class="text-base font-bold text-text-primary">教学总结</h2>
          <p class="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-text-secondary">{{ result.summary }}</p>
        </section>

        <section class="rounded-2xl border border-border-subtle bg-surface p-5">
          <h2 class="text-base font-bold text-text-primary">提问趋势</h2>
          <div ref="trendHost" class="mt-3 h-64 w-full"></div>
        </section>

        <section class="grid gap-4 lg:grid-cols-2">
          <div class="rounded-2xl border border-border-subtle bg-surface p-5">
            <h2 class="text-base font-bold text-text-primary">高频问题 TOP</h2>
            <div ref="topHost" class="mt-3 h-72 w-full"></div>
          </div>
          <div class="rounded-2xl border border-border-subtle bg-surface p-5">
            <h2 class="text-base font-bold text-text-primary">高频关键词</h2>
            <div ref="keywordHost" class="mt-3 h-72 w-full"></div>
          </div>
        </section>

        <section v-if="result.clusters?.length" class="space-y-3">
          <h2 class="text-base font-bold text-text-primary">语义聚类（{{ result.clusters.length }} 个簇）</h2>
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <div v-for="cluster in result.clusters" :key="cluster.label" class="rounded-2xl border border-border-subtle bg-surface p-4">
              <button
                type="button"
                class="flex w-full items-center justify-between gap-2 text-left"
                @click="expandedClusters.has(cluster.label) ? expandedClusters.delete(cluster.label) : expandedClusters.add(cluster.label)"
              >
                <span class="font-bold text-text-primary">{{ cluster.label }}</span>
                <span class="shrink-0 text-xs text-text-tertiary">
                  {{ cluster.count }} 次
                  <span v-if="cluster.samples?.length" class="ml-1 text-accent">{{ expandedClusters.has(cluster.label) ? '收起' : '展开' }}</span>
                </span>
              </button>
              <p v-if="cluster.keywords?.length" class="mt-2 flex flex-wrap gap-1.5">
                <span v-for="kw in cluster.keywords.slice(0, 6)" :key="kw" class="rounded bg-surface-sunken px-1.5 py-0.5 text-xs text-text-secondary">{{ kw }}</span>
              </p>
              <ul v-if="expandedClusters.has(cluster.label) && cluster.samples?.length" class="mt-3 space-y-1.5 border-t border-border-subtle pt-2">
                <li v-for="(sample, index) in cluster.samples" :key="index" class="text-xs leading-relaxed text-text-secondary">
                  {{ sample }}
                </li>
              </ul>
            </div>
          </div>
        </section>

        <!-- 练习表现（题库导入且有做题记录后出现） -->
        <section v-if="result.practice_summary && !result.practice_summary.empty" class="rounded-2xl border border-border-subtle bg-surface p-5">
          <div class="flex items-baseline justify-between">
            <h2 class="text-base font-bold text-text-primary">练习表现</h2>
            <span class="text-xs text-text-tertiary">
              做题 {{ result.practice_summary.attempt_count }} 次 · 判分 {{ result.practice_summary.graded_count }} 次 · 正确率
              {{ result.practice_summary.correct_rate === null ? '—' : `${Math.round(result.practice_summary.correct_rate * 100)}%` }}
              · 活跃 {{ result.practice_summary.active_students }} 人
            </span>
          </div>
          <div class="mt-4 grid gap-4 lg:grid-cols-2">
            <div>
              <h3 class="mb-2 text-sm font-bold text-text-secondary">知识点错误率</h3>
              <ul class="space-y-1.5">
                <li
                  v-for="item in result.practice_summary.by_knowledge_point.slice(0, 10)"
                  :key="item.knowledge_point"
                  class="flex items-center justify-between gap-2 rounded-xl bg-surface-sunken px-3 py-2 text-sm"
                >
                  <span class="min-w-0 truncate">{{ item.knowledge_point }}</span>
                  <span class="shrink-0 text-xs" :class="item.error_rate >= 0.4 ? 'font-bold text-rose-600' : 'text-text-tertiary'">
                    {{ Math.round(item.error_rate * 100) }}% · {{ item.wrong }}/{{ item.attempts }}
                  </span>
                </li>
              </ul>
            </div>
            <div>
              <h3 class="mb-2 text-sm font-bold text-text-secondary">高频错题</h3>
              <ul class="space-y-1.5">
                <li
                  v-for="item in result.practice_summary.top_wrong_questions.slice(0, 10)"
                  :key="item.question_id"
                  class="rounded-xl bg-surface-sunken px-3 py-2 text-sm"
                >
                  <p class="truncate">{{ item.stem || '（题干缺失）' }}</p>
                  <p class="mt-0.5 text-xs text-text-tertiary">错 {{ item.wrong_count }} 次 · {{ item.question_type }}</p>
                </li>
              </ul>
            </div>
          </div>
        </section>

        <!-- 交叉洞察：问的 × 错的 -->
        <section v-if="result.cross_insights?.length" class="rounded-2xl border border-border-subtle bg-surface p-5">
          <h2 class="text-base font-bold text-text-primary">交叉洞察（问了什么 × 错在哪里）</h2>
          <ul class="mt-3 space-y-2">
            <li
              v-for="item in result.cross_insights"
              :key="item.knowledge_point"
              class="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-surface-sunken px-3 py-2.5 text-sm"
            >
              <div class="flex min-w-0 items-center gap-2">
                <span
                  class="shrink-0 rounded-lg px-2 py-0.5 text-xs font-bold"
                  :class="{
                    'bg-rose-100 text-rose-700': item.category === 'asked_and_wrong',
                    'bg-amber-100 text-amber-700': item.category === 'wrong_only',
                    'bg-sky-100 text-sky-700': item.category === 'asked_only',
                  }"
                >
                  {{ crossCategoryLabel(item.category) }}
                </span>
                <span class="min-w-0 truncate font-bold">{{ item.knowledge_point }}</span>
              </div>
              <span class="shrink-0 text-xs text-text-tertiary">
                错误率 {{ Math.round(item.error_rate * 100) }}% · 相关提问 {{ item.question_mentions }} 条
              </span>
            </li>
          </ul>
          <p class="mt-3 text-xs leading-relaxed text-text-tertiary">
            「问得多且错得多」建议优先课堂讲解；「没人问但错得多」是沉默的薄弱点，主动覆盖；「问得多但正确率高」多为概念澄清型提问，讲概念即可。
          </p>
        </section>

        <!-- 按薄弱点抽题 -->
        <section v-if="result.recommended_exercises?.length" class="rounded-2xl border border-border-subtle bg-surface p-5">
          <h2 class="text-base font-bold text-text-primary">建议讲解 + 配套习题</h2>
          <p class="mt-1 text-xs text-text-tertiary">按薄弱知识点从已发布题库抽取，难度递进；点击题号跳转题库管理。</p>
          <div class="mt-3 space-y-3">
            <div v-for="group in result.recommended_exercises" :key="group.knowledge_point">
              <p class="text-sm font-bold text-text-primary">{{ group.knowledge_point }}</p>
              <ul class="mt-1.5 space-y-1">
                <li v-for="question in group.questions" :key="question.id" class="flex items-start gap-2 text-sm text-text-secondary">
                  <button
                    type="button"
                    class="shrink-0 font-mono text-xs text-accent hover:underline"
                    @click="router.push(`/teacher/bank/${question.id}`)"
                  >
                    QB-{{ question.id.slice(0, 8) }}
                  </button>
                  <span class="min-w-0">
                    <span class="mr-1.5 rounded bg-surface-sunken px-1.5 py-0.5 text-xs">{{ question.question_type }}</span>{{ question.stem }}
                  </span>
                </li>
              </ul>
            </div>
          </div>
        </section>
      </template>
    </template>
  </div>
</template>
