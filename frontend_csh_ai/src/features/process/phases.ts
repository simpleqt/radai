/**
 * 流水线阶段码 → 展示名与排序。
 *
 * 阶段码由后端 presenter 给出。未登记的阶段码不会被丢弃：
 * 它按首次出现顺序排在已登记阶段之后，展示名回落为阶段码本身。
 */

interface PhaseMeta {
  label: string;
  order: number;
}

const PHASE_REGISTRY: Record<string, PhaseMeta> = {
  // 课程达成度评价流水线
  'task.create': { label: '创建任务', order: 10 },
  'material.bind': { label: '绑定材料', order: 20 },
  'material.extract': { label: '材料抽取', order: 30 },
  'data.validate': { label: '数据校验', order: 40 },
  'score.review': { label: '分数核对', order: 50 },
  mapping: { label: '目标映射', order: 60 },
  stats: { label: '统计计算', order: 70 },
  report: { label: '报告生成', order: 80 },
  meta: { label: '任务管理', order: 90 },
};

const UNREGISTERED_BASE_ORDER = 1000;

export function phaseLabel(phase: string): string {
  return PHASE_REGISTRY[phase]?.label || phase;
}

export function phaseOrder(phase: string, firstSeenIndex: number): number {
  const meta = PHASE_REGISTRY[phase];
  if (meta) return meta.order;
  return UNREGISTERED_BASE_ORDER + firstSeenIndex;
}
