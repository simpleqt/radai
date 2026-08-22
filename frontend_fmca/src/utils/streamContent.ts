/**
 * 流式正文展示工具：剥离模型思考标签，判断是否有可见回答。
 */

const THINK_BLOCK_RE = /<(think|thought|redacted_thinking)>[\s\S]*?<\/\1>/gi;
const THINK_TAG_RE = /<\/?(think|thought|redacted_thinking)>/gi;

export function stripThinkTags(input: string): string {
  return String(input || '')
    .replace(THINK_BLOCK_RE, '')
    .replace(THINK_TAG_RE, '')
    .trim();
}

/** 是否有可展示的正文（排除仅占位的流式文案） */
export function hasVisibleAnswerContent(content: string): boolean {
  const visible = stripThinkTags(content);
  if (!visible) return false;
  if (visible === '正在生成分析结果，请稍候...') return false;
  return true;
}

export function resolveAnswerDisplayContent(
  content: string,
  isStreaming: boolean,
): string {
  const visible = stripThinkTags(content);
  if (visible) return visible;
  if (isStreaming) return '正在生成分析结果，请稍候...';
  return '';
}
