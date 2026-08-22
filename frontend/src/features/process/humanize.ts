/**
 * 过程输出的唯一文案清洗入口。
 *
 * 过程输出的文案清洗只能写在这里。历史上时间线、子智能体面板各有一份近似的正则，
 * 规则彼此漂移；渲染层新增同类正则会让这个问题重新出现。
 */

const WORKFLOW_STATUS_LABELS: Record<string, string> = {
  lightweight: '轻量问答',
  full: '完整流程',
  deep_research: '深度研究',
  plan_solve: '规划求解',
  multi_agent_parallel: '多智能体并行',
  multi_agent_alternating: '多智能体接力',
};

export const MAX_DETAIL_LENGTH = 260;

export function workflowStartedStatus(workflow: unknown): string {
  const code = String(workflow || '').trim().toLowerCase().replace(/-/g, '_');
  if (!code) return '已启动工作流';
  return `已启动工作流：${WORKFLOW_STATUS_LABELS[code] || code}`;
}

/**
 * 把技术状态串转成用户可读文案。
 * 工作流代码与事件类型是内部标识，`workflow.started:full` 这类原文对使用者没有意义；
 * 历史会话里已存下的旧状态串也从这里统一收敛。
 */
export function humanizeAgentStatus(raw: string): string {
  const text = String(raw || '').trim();
  if (!text) return '';

  const workflow = text.match(/^workflow\.started:(.*)$/i);
  if (workflow) return workflowStartedStatus(workflow[1]);

  const failure = text.match(/^error:\s*(.*)$/i);
  if (failure) return `执行出错：${failure[1].trim() || '未知错误'}`;

  return text;
}

/** 把畸形的上下标标记（`< sup >9< / sup >`、`&lt;sub&gt;`）折算成纯文本 `^9` / `_9`。 */
function normalizeSuperSubNoise(input: string): string {
  let output = input
    .replace(/\u200b|\u200c|\u200d|\ufeff/gi, '')
    .replace(/&amp;lt;/gi, '&lt;')
    .replace(/&amp;gt;/gi, '&gt;');

  const normalizeTag = (tagName: 'sup' | 'sub') => {
    const letters = tagName.split('').join('[\\s\\u00A0\\u200B\\u200C\\u200D]*');
    const open = new RegExp(`(?:<|&lt;)\\s*${letters}\\s*(?:>|&gt;)`, 'gi');
    const close = new RegExp(`(?:<|&lt;)\\s*\\/\\s*${letters}\\s*(?:>|&gt;)`, 'gi');

    output = output
      .replace(open, `<${tagName}>`)
      .replace(close, `</${tagName}>`)
      .replace(new RegExp(`</${tagName}>\\s*(?:>|&gt;)`, 'gi'), `</${tagName}>`);
  };

  normalizeTag('sup');
  normalizeTag('sub');

  return output
    .replace(/<sup>\s*([^<\n]{1,16})\s*<\/sup>/gi, '^$1')
    .replace(/<sub>\s*([^<\n]{1,16})\s*<\/sub>/gi, '_$1');
}

/** 过程文本清洗：剥运行时包装、思考段、注入的时间策略块，输出可直接展示的纯文本。 */
export function cleanProcessText(value?: string | null): string {
  if (!value) return '';
  let text = normalizeSuperSubNoise(String(value).trim());

  text = text.replace(/\\n/g, '\n').replace(/\\t/g, '\t');

  const contentWrapper = text.match(/^content=(['"])([\s\S]*)\1$/);
  if (contentWrapper && contentWrapper[2]) {
    text = contentWrapper[2].trim();
  }

  text = text.replace(/<(think|thought)>[\s\S]*?<\/\1>/gi, '');
  text = text.replace(/<\/?(think|thought)>/gi, '');

  text = text
    .replace(/^Task\s+Succeeded\.\s*Result:\s*/i, '')
    .replace(/^Task\s+failed\.\s*Error:\s*/i, '执行失败：')
    .replace(/^Task\s+timed\s+out\.\s*Error:\s*/i, '执行超时：')
    .replace(/^Task\s+polling\s+timed\s+out\.?\s*/i, '任务轮询超时：');

  text = text.replace(/<runtime_current_time>[\s\S]*?<\/runtime_current_time>/gi, '').trim();
  text = text.replace(/^\s*时间范围严格限定为：.*$/gim, '').trim();

  if ((text.startsWith('"') && text.endsWith('"')) || (text.startsWith("'") && text.endsWith("'"))) {
    text = text.slice(1, -1).trim();
  }

  return humanizeAgentStatus(text);
}

/** 判断文本是不是模型/运行时的原始负载，这类内容不应出现在过程面板里。 */
export function isNoisyPayloadText(text: string): boolean {
  if (!text) return false;
  const trimmed = text.trim();
  const noisyMarkers = [
    '"response_metadata"',
    '"token_usage"',
    '"completion_tokens"',
    '"prompt_tokens"',
    '"system_fingerprint"',
    '"additional_kwargs"',
    '"tool_calls"',
  ];
  const markerHits = noisyMarkers.reduce((sum, marker) => sum + (trimmed.includes(marker) ? 1 : 0), 0);
  const looksLikeRawObject = trimmed.startsWith('{') && trimmed.includes('"type"');
  return markerHits >= 2 || looksLikeRawObject;
}

export function shortenText(text: string, limit = MAX_DETAIL_LENGTH): string {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}...`;
}

/** 步骤详情文本：清洗 + 丢弃原始负载 + 截断。 */
export function cleanStepDetail(value?: string | null, limit = MAX_DETAIL_LENGTH): string {
  const cleaned = cleanProcessText(value);
  if (!cleaned) return '';
  if (isNoisyPayloadText(cleaned)) return '';
  return shortenText(cleaned, limit);
}

export function formatDuration(durationMs?: number): string | undefined {
  if (typeof durationMs !== 'number' || !Number.isFinite(durationMs) || durationMs < 0) return undefined;
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(1)}s`;
  const minutes = Math.floor(durationMs / 60_000);
  const seconds = Math.round((durationMs % 60_000) / 1000);
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
}
