/**
 * 事件流 → PipelineModel。
 *
 * 归组是互斥两态，不再有「借用上一个分组」和「未归组事件」兜底：
 * - 任一事件带 phase        → linear，按阶段分组（课程评价这类线性流水线）
 * - 否则任一事件带 task_id  → parallel，按子智能体分组（深度研究）
 * - 都没有                  → flat，单条时间线
 */

import { cleanProcessText } from './humanize';
import { normalizeEvent } from './normalizeEvent';
import { phaseLabel, phaseOrder } from './phases';
import type {
  GroupState,
  IssueItem,
  IssuesViewData,
  NormalizedStep,
  PendingAction,
  PipelineModel,
  PipelineSummary,
  ProcessEvent,
  StepGroup,
} from './types';

const ORCHESTRATOR_ID = 'orchestrator';
const MAX_PENDING_ACTIONS = 2;

const KB_SEARCH_TOOLS = new Set(['multi_kb_search', 'kb_query_with_keywords']);

const asRecord = (value: unknown): Record<string, any> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, any>) : {};

/** 后台任务事件带的是父任务 id，不是被委派的子智能体 id，必须留在主控分组。 */
const eventTaskId = (event: ProcessEvent): string | null => {
  if (event.kind === 'background_task_started' || String(event.id || '').startsWith('background-task:')) {
    return null;
  }
  const rawTaskId = asRecord(event.metadata).task_id;
  if (typeof rawTaskId !== 'string') return null;
  return rawTaskId.trim() || null;
};

const eventTaskDescription = (event: ProcessEvent): string | null => {
  const metadata = asRecord(event.metadata);

  const description = String(metadata.description || '').trim();
  if (description) return description;

  const inputDescription = String(asRecord(metadata.input).description || '').trim();
  if (inputDescription) return inputDescription;

  return null;
};

const resolveBranchTitle = (groupId: string, events: ProcessEvent[]): string => {
  if (groupId === ORCHESTRATOR_ID) return '主控流程';

  for (const event of events) {
    const description = eventTaskDescription(event);
    if (description) return cleanProcessText(description);
  }
  for (const event of events) {
    const subagentType = String(asRecord(event.metadata).subagent_type || '').trim();
    if (subagentType) return `子智能体(${subagentType})`;
  }
  return `子智能体 ${groupId.slice(0, 8)}`;
};

const resolveGroupState = (steps: NormalizedStep[], events: ProcessEvent[], isStreaming: boolean): GroupState => {
  let state: GroupState = 'idle';
  let hasFailure = false;
  let hasCompletionSignal = false;
  let hasRunningSignal = false;

  for (const event of events) {
    const runtimeType = String(asRecord(event.metadata).runtime_type || '');

    const failed =
      event.level === 'error' ||
      runtimeType === 'task_failed' ||
      runtimeType === 'task_timed_out' ||
      (event.kind === 'task_end' && event.level === 'warning');
    if (failed) {
      state = 'failed';
      hasFailure = true;
      continue;
    }

    const completed =
      runtimeType === 'task_completed' ||
      (event.kind === 'task_end' && event.level === 'success') ||
      (event.tool === 'task' && (event.kind === 'tool_end' || event.kind === 'tool_result'));
    if (completed) {
      state = 'completed';
      hasCompletionSignal = true;
      continue;
    }

    const running =
      runtimeType === 'task_started' ||
      runtimeType === 'task_running' ||
      runtimeType === 'task_tool_start' ||
      event.level === 'active' ||
      event.kind === 'task_start' ||
      event.kind === 'task_progress';
    if (running) {
      state = 'running';
      hasRunningSignal = true;
    }
  }

  if (!hasFailure && steps.some((step) => step.tone === 'error')) {
    return 'failed';
  }

  // 部分分支不发显式完成事件。流已结束时，未失败的分支不应停在 running。
  if (!isStreaming && !hasFailure) {
    if (state === 'running') return 'completed';
    if (state === 'idle' && (hasCompletionSignal || hasRunningSignal || steps.length > 0)) return 'completed';
  }

  return state;
};

const lastTimestamp = (events: ProcessEvent[]): number =>
  events.reduce((acc, event, index) => Math.max(acc, typeof event.timestamp === 'number' ? event.timestamp : index), 0);

/** 同一轮里多次知识库检索只保留最后一条结果摘要，避免重复刷屏。 */
const dropSupersededKbResults = (events: ProcessEvent[]): ProcessEvent[] => {
  const lastIndex = events.reduce(
    (last, event, index) => (event.kind === 'tool_result' && KB_SEARCH_TOOLS.has(String(event.tool)) ? index : last),
    -1,
  );
  if (lastIndex < 0) return events;
  return events.filter(
    (event, index) =>
      !(event.kind === 'tool_result' && KB_SEARCH_TOOLS.has(String(event.tool)) && index !== lastIndex),
  );
};

const collectIssues = (steps: NormalizedStep[]): IssueItem[] => {
  const issues: IssueItem[] = [];
  for (const step of steps) {
    if (step.view === 'issues') {
      const data = step.data as unknown as IssuesViewData;
      for (const item of data.items || []) {
        if (item.level === 'info') continue;
        issues.push({
          level: item.level,
          message: item.message,
          code: item.code,
          locator: item.locator,
          stepLabel: step.label,
        });
      }
      continue;
    }
    if (step.tone === 'error') {
      issues.push({ level: 'error', message: step.statusText || step.label, stepLabel: step.label });
    }
  }
  return issues;
};

/** 同一工具只保留最新一次待办，最多展示 2 张，避免过期动作卡堆积。 */
const collectActions = (steps: NormalizedStep[]): PendingAction[] => {
  const latestByTool = new Map<string, PendingAction>();
  for (const step of steps) {
    if (!step.action) continue;
    latestByTool.set(step.tool || step.id, {
      stepId: step.id,
      label: step.label,
      action: step.action,
      view: step.view,
      data: step.data,
    });
  }
  return [...latestByTool.values()].slice(-MAX_PENDING_ACTIONS);
};

const buildSummary = (
  steps: NormalizedStep[],
  events: ProcessEvent[],
  issues: IssueItem[],
  actionCount: number,
  isStreaming: boolean,
): PipelineSummary => {
  const timestamps = events
    .map((event) => event.timestamp)
    .filter((value): value is number => typeof value === 'number' && value > 0);
  const spanMs = timestamps.length >= 2 ? Math.max(...timestamps) - Math.min(...timestamps) : 0;
  const durationSum = steps.reduce((sum, step) => sum + (step.durationMs || 0), 0);

  const errorCount = issues.filter((issue) => issue.level === 'error').length;
  const state: PipelineSummary['state'] = isStreaming ? 'running' : errorCount > 0 ? 'failed' : 'succeeded';

  const activeStep = [...steps].reverse().find((step) => step.tone !== 'error');
  const currentLabel = isStreaming ? activeStep?.label || '正在处理' : '';

  return {
    state,
    stepCount: steps.length,
    elapsedMs: Math.max(spanMs, durationSum),
    currentLabel,
    errorCount,
    warningCount: issues.filter((issue) => issue.level === 'warning').length,
    pendingCount: actionCount,
  };
};

export interface BuildPipelineOptions {
  messageId: string;
  isStreaming?: boolean;
}

export function buildPipelineModel(
  rawEvents: ProcessEvent[] | undefined,
  options: BuildPipelineOptions,
): PipelineModel {
  const isStreaming = Boolean(options.isStreaming);
  const events = dropSupersededKbResults(
    (rawEvents || []).filter((event) => event && event.kind !== 'context_compaction'),
  );

  const pairs: { event: ProcessEvent; step: NormalizedStep }[] = [];
  events.forEach((event, index) => {
    const step = normalizeEvent(event, index, options.messageId);
    if (step) pairs.push({ event, step });
  });

  const hasPhase = pairs.some(({ step }) => Boolean(step.phase));
  const hasTaskId = pairs.some(({ event }) => Boolean(eventTaskId(event)));
  const mode: PipelineModel['mode'] = hasPhase ? 'linear' : hasTaskId ? 'parallel' : 'flat';

  const buckets = new Map<string, { event: ProcessEvent; step: NormalizedStep }[]>();
  const firstSeen = new Map<string, number>();

  pairs.forEach((pair, index) => {
    const groupId =
      mode === 'linear'
        ? pair.step.phase || ORCHESTRATOR_ID
        : mode === 'parallel'
          ? eventTaskId(pair.event) || ORCHESTRATOR_ID
          : ORCHESTRATOR_ID;
    pair.step.groupId = groupId;
    if (!buckets.has(groupId)) {
      buckets.set(groupId, []);
      firstSeen.set(groupId, index);
    }
    buckets.get(groupId)?.push(pair);
  });

  const groups: StepGroup[] = [...buckets.entries()].map(([groupId, groupPairs]) => {
    const groupSteps = groupPairs.map((pair) => pair.step);
    const groupEvents = groupPairs.map((pair) => pair.event);
    return {
      id: groupId,
      title:
        mode === 'linear'
          ? groupId === ORCHESTRATOR_ID
            ? '其他步骤'
            : phaseLabel(groupId)
          : resolveBranchTitle(groupId, groupEvents),
      state: resolveGroupState(groupSteps, groupEvents, isStreaming),
      stepCount: groupSteps.length,
      lastUpdated: lastTimestamp(groupEvents),
      steps: groupSteps,
    };
  });

  if (mode === 'linear') {
    groups.sort(
      (a, b) => phaseOrder(a.id, firstSeen.get(a.id) || 0) - phaseOrder(b.id, firstSeen.get(b.id) || 0),
    );
  } else {
    const priority = (state: GroupState) =>
      state === 'running' ? 0 : state === 'failed' ? 1 : state === 'completed' ? 2 : 3;
    groups.sort((a, b) => {
      const byPriority = priority(a.state) - priority(b.state);
      if (byPriority !== 0) return byPriority;
      if (b.lastUpdated !== a.lastUpdated) return b.lastUpdated - a.lastUpdated;
      if (b.stepCount !== a.stepCount) return b.stepCount - a.stepCount;
      return (firstSeen.get(a.id) || 0) - (firstSeen.get(b.id) || 0);
    });
  }

  const steps = pairs.map((pair) => pair.step);
  const issues = collectIssues(steps);
  const actions = collectActions(steps);

  return {
    mode,
    groups,
    steps,
    summary: buildSummary(steps, events, issues, actions.length, isStreaming),
    issues,
    actions,
  };
}
