/**
 * 把 Markdown 渲染出的图表 <img>（/api/v1/charts/ 签名代理 URL）原位升级为
 * ECharts 交互式图表（缩放 / tooltip / 图例 / 下钻）。
 *
 * - 后端对每张新图表同 chart_id 存储了 ECharts option payload，经同一 URL 的
 *   `format=option` 获取；option 是纯 JSON，需要函数的行为在这里注入。
 * - 原 <img>（PNG 导出资产）加 `chart-print-fallback` 类后保留在 DOM 中：
 *   屏幕上隐藏，PDF 打印 / 复制导出时恢复显示。
 * - payload 拉取失败（网络异常、签名过期等）时保留静态 PNG，仅作错误兜底。
 */
import { getECharts, type EChartsInstance, type EChartsModule } from './echartsLoader';

export type ChartHandle = {
  dispose: () => void;
};

type ChartDrilldown = {
  x_col: string;
  y_col: string;
  group_by?: string | null;
  columns: string[];
  rows: unknown[][];
};

type ChartPayload = {
  version?: number;
  chart_type?: string;
  option?: Record<string, unknown>;
  meta?: {
    x_col?: string | null;
    y_cols?: string[];
    group_by?: string | null;
    rows?: number;
    height?: number;
    rendered_as?: string;
  };
  drilldown?: ChartDrilldown | null;
};

const CHART_URL_MARK = '/api/v1/charts/';

const buildOptionUrl = (src: string): string | null => {
  try {
    const url = new URL(src, window.location.origin);
    if (!url.pathname.includes(CHART_URL_MARK)) return null;
    url.searchParams.set('format', 'option');
    return url.pathname + url.search;
  } catch {
    return null;
  }
};

const fetchPayload = async (src: string): Promise<ChartPayload | null> => {
  const target = buildOptionUrl(src);
  if (!target) return null;
  try {
    const resp = await fetch(target);
    if (!resp.ok) return null;
    const payload = (await resp.json()) as ChartPayload;
    if (!payload || typeof payload !== 'object' || !payload.option) return null;
    return payload;
  } catch {
    return null;
  }
};

const fmtNumber = (value: unknown): string => {
  if (value == null) return '-';
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (Number.isInteger(n)) return String(n);
  if (Math.abs(n) >= 1e5 || (Math.abs(n) < 1e-3 && n !== 0)) return n.toExponential(3);
  return String(Number(n.toFixed(4)));
};

/** tooltip formatter 返回值按 innerHTML 插入：用户可控的类目/系列名必须转义。 */
const escapeHtml = (text: string) =>
  text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

/** 全部图表（含下钻视图）共用的转义 tooltip formatter。 */
const escapedTooltipFormatter = (params: unknown) => {
  const list = Array.isArray(params) ? params : [params];
  const head = escapeHtml(String((list[0] as { name?: unknown })?.name ?? ''));
  const lines = list.map((p) => {
    const item = p as { marker?: string; seriesName?: string; data?: unknown; value?: unknown };
    const data = item.data as { value?: unknown; std?: unknown } | number | null;
    const value = data && typeof data === 'object' ? data.value : (data ?? item.value);
    const std = data && typeof data === 'object' ? data.std : null;
    const suffix = std != null ? ` ± ${fmtNumber(std)}` : '';
    return `${item.marker || ''}${escapeHtml(String(item.seriesName || ''))}: ${fmtNumber(value)}${suffix}`;
  });
  return [head, ...lines].filter(Boolean).join('<br/>');
};

/** 注入纯 JSON option 无法表达的行为（目前只有 bar 系列的 mean±std tooltip）。 */
const decorateOption = (payload: ChartPayload): Record<string, unknown> => {
  const option = { ...(payload.option as Record<string, unknown>) };
  if (payload.chart_type === 'bar' || payload.chart_type === 'barh') {
    option.tooltip = {
      ...((option.tooltip as Record<string, unknown>) || {}),
      formatter: escapedTooltipFormatter,
    };
  }
  return option;
};

/**
 * 聚合柱状图的类目下钻：点击柱子 → 用 payload 附带的原始明细行渲染该类目
 * 的逐行数值，「◀ 返回」还原总览。
 */
const attachDrilldown = (
  inst: EChartsInstance,
  payload: ChartPayload,
  baseOption: Record<string, unknown>,
) => {
  const drill = payload.drilldown;
  if (!drill || !Array.isArray(drill.rows) || !drill.rows.length) return;
  const xIdx = drill.columns.indexOf(drill.x_col);
  const yIdx = drill.columns.indexOf(drill.y_col);
  const gIdx = drill.group_by ? drill.columns.indexOf(drill.group_by) : -1;
  if (xIdx < 0 || yIdx < 0) return;

  let drilled = false;
  const restore = () => {
    if (!drilled) return;
    drilled = false;
    inst.setOption(baseOption as never, { notMerge: true });
  };
  inst.on('restore', () => {
    drilled = false;
  });

  inst.on('click', (params) => {
    const event = params as { componentType?: string; seriesType?: string; name?: unknown };
    if (drilled || event.componentType !== 'series' || event.seriesType !== 'bar') return;
    const category = String(event.name ?? '');
    if (!category) return;
    const rows = drill.rows.filter((row) => String(row[xIdx]) === category);
    if (!rows.length) return;

    const labels = rows.map((row, i) =>
      gIdx >= 0 && row[gIdx] != null ? String(row[gIdx]) : `样本${i + 1}`,
    );
    const values = rows.map((row) => row[yIdx] as number | null);
    drilled = true;
    inst.setOption(
      {
        color: (baseOption as { color?: unknown }).color,
        title: {
          text: `${category} · 明细（n=${rows.length}）`,
          left: 'center',
          textStyle: { fontSize: 13, fontWeight: 'bold' },
        },
        tooltip: {
          trigger: 'axis',
          confine: true,
          axisPointer: { type: 'shadow' },
          formatter: escapedTooltipFormatter,
        },
        grid: { left: 48, right: 32, top: 56, bottom: 56, containLabel: true },
        dataZoom: [{ type: 'inside' }],
        xAxis: {
          type: 'category',
          data: labels,
          axisLabel: { rotate: labels.length > 6 ? 30 : 0, hideOverlap: true },
        },
        yAxis: { type: 'value', scale: true },
        series: [
          {
            name: drill.y_col,
            type: 'bar',
            data: values,
            label: { show: rows.length <= 20, position: 'top', fontSize: 10 },
          },
        ],
        graphic: [
          {
            type: 'text',
            left: 12,
            top: 10,
            style: { text: '◀ 返回', fill: '#6366f1', fontSize: 13, fontWeight: 'bold' },
            cursor: 'pointer',
            onclick: restore,
          },
        ],
      } as never,
      { notMerge: true },
    );
  });
};

/**
 * 扫描容器内的图表 <img> 并升级为 ECharts 实例。
 * 返回句柄列表，调用方负责在重渲染 / 卸载前 dispose。
 */
export const hydrateCharts = async (root: HTMLElement): Promise<ChartHandle[]> => {
  const images = Array.from(root.querySelectorAll<HTMLImageElement>('img')).filter((img) =>
    (img.getAttribute('src') || '').includes(CHART_URL_MARK),
  );
  if (!images.length) return [];

  const payloads = await Promise.all(
    images.map((img) => fetchPayload(img.getAttribute('src') || '')),
  );
  if (!payloads.some(Boolean)) return [];

  let echarts: EChartsModule;
  try {
    echarts = await getECharts();
  } catch (error) {
    console.warn('[Charts] ECharts 加载失败，保留静态图片', error);
    return [];
  }

  const handles: ChartHandle[] = [];
  images.forEach((img, index) => {
    const payload = payloads[index];
    // isConnected：期间发生过重渲染的话旧节点已脱离文档，直接跳过
    if (!payload || !img.isConnected) return;
    try {
      const host = document.createElement('div');
      host.className = 'chart-echarts';
      host.style.height = `${payload.meta?.height || 380}px`;
      img.classList.add('chart-print-fallback');
      img.insertAdjacentElement('afterend', host);

      const inst = echarts.init(host);
      const option = decorateOption(payload);
      inst.setOption(option as never);
      attachDrilldown(inst, payload, option);

      const observer = new ResizeObserver(() => inst.resize());
      observer.observe(host);

      handles.push({
        dispose: () => {
          observer.disconnect();
          inst.dispose();
          host.remove();
          img.classList.remove('chart-print-fallback');
        },
      });
    } catch (error) {
      console.warn('[Charts] 图表初始化失败，保留静态图片', error);
    }
  });
  return handles;
};

export const disposeChartHandles = (handles: ChartHandle[]) => {
  handles.forEach((handle) => {
    try {
      handle.dispose();
    } catch {
      /* 容器可能已随 v-html 重渲染脱离文档 */
    }
  });
};
