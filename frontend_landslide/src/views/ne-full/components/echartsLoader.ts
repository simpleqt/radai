/**
 * ECharts 懒加载器（与 mermaid 相同的动态 import 模式）：
 * 首个交互式图表出现时才拉取 echarts chunk，并按需注册本项目用到的
 * 图型与组件，避免把整个 echarts 打进首屏。
 */
import type * as EChartsCore from 'echarts/core';

export type EChartsModule = typeof EChartsCore;
export type EChartsInstance = ReturnType<EChartsModule['init']>;

let echartsPromise: Promise<EChartsModule> | null = null;

export const getECharts = (): Promise<EChartsModule> => {
  if (!echartsPromise) {
    echartsPromise = Promise.all([
      import('echarts/core'),
      import('echarts/charts'),
      import('echarts/components'),
      import('echarts/renderers'),
    ]).then(([core, charts, components, renderers]) => {
      core.use([
        charts.BarChart,
        charts.LineChart,
        charts.ScatterChart,
        charts.BoxplotChart,
        charts.HeatmapChart,
        charts.RadarChart,
        components.GridComponent,
        components.TitleComponent,
        components.TooltipComponent,
        components.LegendComponent,
        components.ToolboxComponent,
        components.DataZoomComponent,
        components.VisualMapComponent,
        components.GraphicComponent,
        renderers.CanvasRenderer,
      ]);
      return core;
    });
  }
  return echartsPromise;
};
