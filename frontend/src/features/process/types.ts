/**
 * 智能体过程输出的前端信息模型。
 *
 * 后端通过 `phase` + `display` 给出结构化展示语义，前端只负责渲染；
 * 没有 `display` 的老事件由 normalizeEvent 降级映射成同一套模型。
 */

export type StepLevel = 'active' | 'info' | 'success' | 'warning' | 'error';
export type ChipTone = 'info' | 'success' | 'warning' | 'error';
export type DisplayView = 'text' | 'kv' | 'table' | 'issues' | 'sources' | 'artifact';
export type StepTone = 'default' | 'warning' | 'error';
export type GroupState = 'running' | 'completed' | 'failed' | 'idle';

/** 语义图标键。具体图标组件由 StepIcon 映射，模型层不引用组件。 */
export type IconKey =
  | 'search'
  | 'web'
  | 'tool'
  | 'task'
  | 'think'
  | 'note'
  | 'alert'
  | 'folder'
  | 'table'
  | 'check'
  | 'file';

export interface DisplayChip {
  text: string;
  tone?: ChipTone;
}

export interface DisplayActionOption {
  label: string;
  /** 点击后直接发送到对话的指令 */
  message: string;
  tone?: ChipTone;
}

export interface DisplayAction {
  kind: 'confirm' | 'choose' | 'edit';
  prompt: string;
  options?: DisplayActionOption[];
  /** 「我来修正」填入输入框的模板 */
  input_template?: string;
}

export interface TextViewData {
  text: string;
}

export interface KvViewRow {
  label: string;
  value: string;
  tone?: ChipTone;
}

export interface KvViewData {
  rows: KvViewRow[];
}

export interface TableColumn {
  key: string;
  label: string;
  align?: 'left' | 'center' | 'right';
}

export interface TableViewData {
  columns: TableColumn[];
  rows: Record<string, string | number | boolean | null>[];
  total_rows?: number;
  truncated?: boolean;
}

export interface IssueEntry {
  code?: string;
  level: 'error' | 'warning' | 'info';
  message: string;
  locator?: string;
}

export interface IssuesViewData {
  items: IssueEntry[];
  counts?: { error?: number; warning?: number };
}

export interface SourceEntry {
  title?: string;
  url: string;
  domain?: string;
}

export interface SourcesViewData {
  items: SourceEntry[];
}

export interface ArtifactViewData {
  title: string;
  kind?: string;
  url?: string;
  size?: string;
}

export interface ToolDisplay {
  label: string;
  status_text?: string;
  chips?: DisplayChip[];
  view?: DisplayView;
  data?: Record<string, unknown>;
  action?: DisplayAction;
}

/** SSE 过程事件（与 api/agent-v2 的 AgentProcessEvent 结构一致） */
export interface ProcessEvent {
  id?: string;
  kind: string;
  title?: string;
  detail?: string;
  level?: StepLevel;
  tool?: string;
  summary?: string;
  duration_ms?: number;
  timestamp?: number;
  phase?: string;
  display?: ToolDisplay;
  metadata?: Record<string, unknown>;
}

export interface NormalizedStep {
  id: string;
  index: number;
  kind: string;
  tool?: string;
  phase?: string;
  groupId: string;
  label: string;
  statusText?: string;
  chips: DisplayChip[];
  view: DisplayView;
  data: Record<string, unknown>;
  action?: DisplayAction;
  durationMs?: number;
  timestamp?: number;
  tone: StepTone;
  icon: IconKey;
  /** 技能/工件的本地路径，例如 /mnt/skills/... */
  path?: string;
}

export interface StepGroup {
  id: string;
  title: string;
  state: GroupState;
  stepCount: number;
  lastUpdated: number;
  steps: NormalizedStep[];
}

export interface IssueItem {
  level: 'error' | 'warning';
  message: string;
  code?: string;
  locator?: string;
  stepLabel: string;
}

export interface PendingAction {
  stepId: string;
  label: string;
  action: DisplayAction;
  view: DisplayView;
  data: Record<string, unknown>;
}

export interface PipelineSummary {
  state: 'running' | 'succeeded' | 'failed';
  stepCount: number;
  elapsedMs: number;
  currentLabel: string;
  errorCount: number;
  warningCount: number;
  pendingCount: number;
}

export interface PipelineModel {
  /** linear：后端给了 phase 的流水线；parallel：按 task_id 的并行子智能体；flat：单条时间线 */
  mode: 'linear' | 'parallel' | 'flat';
  groups: StepGroup[];
  steps: NormalizedStep[];
  summary: PipelineSummary;
  issues: IssueItem[];
  actions: PendingAction[];
}
