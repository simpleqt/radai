<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  BarChart3,
  CalendarDays,
  ClipboardCheck,
  MessageSquare,
  Users,
} from 'lucide-vue-next';
import { getECharts, type EChartsInstance } from '@/views/ne-full/components/echartsLoader';
import {
  TeacherAnalyticsAPI,
  type Insights,
  type NamedCount,
  type TrendPoint,
} from '@/api/teacher-analytics';

const router = useRouter();
const loading = ref(true);
const error = ref('');
const insights = ref<Insights | null>(null);

const trendHost = ref<HTMLElement | null>(null);
const questionsHost = ref<HTMLElement | null>(null);
const keywordsHost = ref<HTMLElement | null>(null);
let trendChart: EChartsInstance | null = null;
let questionsChart: EChartsInstance | null = null;
let keywordsChart: EChartsInstance | null = null;

const cards = computed(() => {
  const o = insights.value?.overview;
  return [
    { label: '累计提问', value: o ? String(o.total) : '—', hint: '上线以来', icon: MessageSquare },
    { label: '今日提问', value: o ? String(o.today) : '—', hint: '今天', icon: CalendarDays },
    { label: `近 ${insights.value?.days ?? 30} 日`, value: o ? String(o.last_30_days) : '—', hint: '统计窗口内提问', icon: BarChart3 },
    { label: '活跃学生', value: o ? String(o.active_students_30_days) : '—', hint: '近 30 日提问人数', icon: Users },
  ];
});

function truncate(text: string, max: number): string {
  const value = String(text || '');
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

function renderTrend(trend: TrendPoint[]) {
  if (!trendHost.value || !trend.length) return;
  void getECharts().then((echarts) => {
    if (!trendHost.value) return;
    trendChart?.dispose();
    trendChart = echarts.init(trendHost.value);
    trendChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 44, right: 20, top: 30, bottom: 30 },
      xAxis: { type: 'category', data: trend.map((p) => p.date), axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value', minInterval: 1 },
      series: [
        {
          name: '提问量',
          type: 'line',
          data: trend.map((p) => p.count),
          smooth: true,
          symbolSize: 5,
          areaStyle: { opacity: 0.15 },
          lineStyle: { width: 2.5 },
          itemStyle: { color: '#3b82f6' },
        },
      ],
    });
  }).catch((err) => console.warn('[teacher-dashboard] chart render failed', err));
}

function renderQuestions(items: NamedCount[]) {
  if (!questionsHost.value || !items.length) return;
  void getECharts().then((echarts) => {
    if (!questionsHost.value) return;
    questionsChart?.dispose();
    questionsChart = echarts.init(questionsHost.value);
    const rows = [...items].reverse();
    questionsChart.setOption({
      tooltip: {
        trigger: 'axis',
        formatter: (params: unknown) => {
          const list = Array.isArray(params) ? params : [params];
          const item = list[0] as { dataIndex?: number };
          const index = item?.dataIndex ?? 0;
          const row = rows[index];
          return row ? `${escapeHtml(row.question ?? '')}<br/>出现 <b>${row.count}</b> 次` : '';
        },
      },
      grid: { left: 8, right: 44, top: 8, bottom: 8, containLabel: true },
      xAxis: { type: 'value', minInterval: 1 },
      yAxis: {
        type: 'category',
        data: rows.map((row) => truncate(row.question ?? '', 16)),
        axisLabel: { fontSize: 11 },
      },
      series: [
        {
          type: 'bar',
          data: rows.map((row) => row.count),
          barMaxWidth: 18,
          itemStyle: { color: '#6366f1', borderRadius: [0, 4, 4, 0] },
        },
      ],
    });
  }).catch((err) => console.warn('[teacher-dashboard] chart render failed', err));
}

function renderKeywords(items: NamedCount[]) {
  if (!keywordsHost.value || !items.length) return;
  void getECharts().then((echarts) => {
    if (!keywordsHost.value) return;
    keywordsChart?.dispose();
    keywordsChart = echarts.init(keywordsHost.value);
    const rows = [...items].reverse();
    keywordsChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 8, right: 44, top: 8, bottom: 8, containLabel: true },
      xAxis: { type: 'value', minInterval: 1 },
      yAxis: { type: 'category', data: rows.map((row) => row.keyword ?? ''), axisLabel: { fontSize: 11 } },
      series: [
        {
          type: 'bar',
          data: rows.map((row) => row.count),
          barMaxWidth: 14,
          itemStyle: { color: '#14b8a6', borderRadius: [0, 4, 4, 0] },
        },
      ],
    });
  }).catch((err) => console.warn('[teacher-dashboard] chart render failed', err));
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    insights.value = await TeacherAnalyticsAPI.insights();
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '加载失败';
  } finally {
    // 先结束 loading，让图表容器（v-else 分支）进入 DOM，再初始化 ECharts；
    // 否则骨架屏期间容器不存在，渲染函数会静默跳过。
    loading.value = false;
  }
  if (insights.value && !error.value) {
    await nextTick();
    renderTrend(insights.value.trend || []);
    renderQuestions(insights.value.top_questions || []);
    renderKeywords(insights.value.top_keywords || []);
  }
}

onMounted(() => void load());
onBeforeUnmount(() => {
  trendChart?.dispose();
  questionsChart?.dispose();
  keywordsChart?.dispose();
});
</script>

<template>
  <div class="space-y-5">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-xl font-extrabold text-text-primary">基础力学 · 学情大屏</h1>
        <p class="mt-1 text-sm text-text-tertiary">
          近 {{ insights?.days ?? 30 }} 天学生提问概览，数据自动统计，无需配置
        </p>
      </div>
      <div class="flex gap-2">
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-xl border border-border-subtle bg-surface px-4 py-2 text-sm font-bold text-text-secondary transition hover:border-accent/40 hover:text-accent"
          @click="router.push('/teacher/questions')"
        >
          <MessageSquare class="w-4 h-4" /> 查看全部提问
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-xl bg-accent px-4 py-2 text-sm font-bold text-white transition hover:bg-accent-hover"
          @click="router.push('/teacher/reports')"
        >
          <ClipboardCheck class="w-4 h-4" /> 历史分析报告
        </button>
      </div>
    </header>

    <p v-if="error" class="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</p>

    <div v-if="loading" class="space-y-4">
      <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <div v-for="i in 4" :key="i" class="skeleton-shimmer h-28 animate-pulse rounded-2xl bg-surface-hover" />
      </div>
      <div class="skeleton-shimmer h-72 animate-pulse rounded-2xl bg-surface-hover" />
    </div>

    <template v-else>
      <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <div
          v-for="card in cards"
          :key="card.label"
          class="rounded-2xl border border-border-subtle bg-surface p-5"
        >
          <div class="flex items-center justify-between">
            <p class="text-sm font-semibold text-text-secondary">{{ card.label }}</p>
            <component :is="card.icon" class="w-4 h-4 text-accent/70" />
          </div>
          <p class="mt-3 text-3xl font-extrabold text-text-primary">{{ card.value }}</p>
          <p class="mt-1 text-xs text-text-tertiary">{{ card.hint }}</p>
        </div>
      </div>

      <section class="rounded-2xl border border-border-subtle bg-surface p-5">
        <div class="flex items-baseline justify-between">
          <h2 class="text-base font-bold text-text-primary">提问趋势</h2>
          <span class="text-xs text-text-tertiary">按天统计</span>
        </div>
        <div v-if="insights?.trend?.length" ref="trendHost" class="mt-3 h-72 w-full"></div>
        <p v-else class="py-16 text-center text-sm text-text-tertiary">暂无提问数据</p>
      </section>

      <section class="grid gap-4 lg:grid-cols-2">
        <div class="rounded-2xl border border-border-subtle bg-surface p-5">
          <div class="flex items-baseline justify-between">
            <h2 class="text-base font-bold text-text-primary">高频问题 TOP 10</h2>
            <span class="text-xs text-text-tertiary">完全相同的提问归并计数</span>
          </div>
          <div v-if="insights?.top_questions?.length" ref="questionsHost" class="mt-3 h-96 w-full"></div>
          <p v-else class="py-20 text-center text-sm text-text-tertiary">暂无提问数据</p>
        </div>
        <div class="rounded-2xl border border-border-subtle bg-surface p-5">
          <div class="flex items-baseline justify-between">
            <h2 class="text-base font-bold text-text-primary">高频关键词 TOP 20</h2>
            <span class="text-xs text-text-tertiary">自动分词统计</span>
          </div>
          <div v-if="insights?.top_keywords?.length" ref="keywordsHost" class="mt-3 h-96 w-full"></div>
          <p v-else class="py-20 text-center text-sm text-text-tertiary">暂无提问数据</p>
        </div>
      </section>

      <!-- 练习维度（题库导入后自动出现；未导入时不占版面） -->
      <section v-if="insights?.practice && !insights.practice.empty" class="rounded-2xl border border-border-subtle bg-surface p-5">
        <div class="flex items-baseline justify-between">
          <h2 class="text-base font-bold text-text-primary">练习表现（做题 × 提问交叉验证）</h2>
          <span class="text-xs text-text-tertiary">
            做题 {{ insights.practice.attempt_count }} 次 · 正确率
            {{ insights.practice.correct_rate === null ? '—' : `${Math.round(insights.practice.correct_rate * 100)}%` }}
            · 活跃 {{ insights.practice.active_students }} 人
            <span v-if="insights.practice.browse_count"> · 查看答案 {{ insights.practice.browse_count }} 次</span>
          </span>
        </div>
        <div class="mt-4 grid gap-4 lg:grid-cols-2">
          <div>
            <h3 class="mb-2 text-sm font-bold text-text-secondary">错题知识点 TOP</h3>
            <ul class="space-y-1.5">
              <li
                v-for="item in insights.practice.by_knowledge_point.slice(0, 8)"
                :key="item.knowledge_point"
                class="flex items-center justify-between gap-2 rounded-xl bg-surface-sunken px-3 py-2 text-sm"
              >
                <span class="min-w-0 truncate text-text-primary">{{ item.knowledge_point }}</span>
                <span class="shrink-0 text-xs" :class="item.error_rate >= 0.4 ? 'font-bold text-rose-600' : 'text-text-tertiary'">
                  错误率 {{ Math.round(item.error_rate * 100) }}% · {{ item.wrong }}/{{ item.attempts }}
                </span>
              </li>
              <li v-if="!insights.practice.by_knowledge_point.length" class="px-3 py-2 text-sm text-text-tertiary">暂无练习数据</li>
            </ul>
          </div>
          <div>
            <h3 class="mb-2 text-sm font-bold text-text-secondary">高频错题 TOP</h3>
            <ul class="space-y-1.5">
              <li
                v-for="item in insights.practice.top_wrong_questions.slice(0, 8)"
                :key="item.question_id"
                class="rounded-xl bg-surface-sunken px-3 py-2 text-sm"
              >
                <p class="truncate text-text-primary">{{ item.stem || '（题干缺失）' }}</p>
                <p class="mt-0.5 text-xs text-text-tertiary">
                  错 {{ item.wrong_count }} 次 · {{ item.question_type }}
                  <span v-if="item.knowledge_points?.length"> · {{ item.knowledge_points.join('、') }}</span>
                </p>
              </li>
              <li v-if="!insights.practice.top_wrong_questions.length" class="px-3 py-2 text-sm text-text-tertiary">暂无错题</li>
            </ul>
          </div>
        </div>
      </section>
      <section v-else class="rounded-2xl border border-dashed border-border-subtle bg-surface/50 px-5 py-4 text-sm text-text-tertiary">
        练习维度尚未启用：教师在「题库管理」导入试题、学生开始做题后，这里会展示做题表现与错题分析。
      </section>
    </template>
  </div>
</template>
