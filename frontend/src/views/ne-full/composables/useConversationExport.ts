/**
 * 整会话导出：把当前会话的问答拼成 Markdown，下载为 .md 或经后端渲染为 .docx。
 *
 * - Markdown 纯前端生成并下载，不经过服务端；
 * - Word 复用既有的 `POST /api/v2/files/export-docx`（已鉴权，正文上限 20 万
 *   字符），因此不新增任何后端接口；超限时丢弃最早的轮次并在文首写明截断。
 * - 只导出用户问题与可见回答：思考链、过程事件、追问建议都是过程态，不进文档。
 * - 回答里的图片是签名代理 URL，导出后随签名过期失效；这是既有约束，导出侧
 *   不做物化（与单条导出的行为一致）。
 */
import { authState } from '@/utils/request';

/** 与后端 ExportDocxRequest.content 的 max_length 对齐 */
export const MAX_DOCX_CONTENT_CHARS = 200_000;

export interface ExportMessage {
  role: string;
  content?: string;
  isStreaming?: boolean;
}

export interface ConversationExportMeta {
  /** 智能体名称，写入文档抬头 */
  agentName?: string;
  /** 会话标题；缺省时用第一条用户问题派生 */
  title?: string;
}

type Turn = { question: string; answer: string };

const TURN_SEPARATOR = '\n\n---\n\n';

/** 成对收敛为「问 → 答」；连续多条回答合并，无问题的回答挂到上一轮。 */
const collectTurns = (messages: ExportMessage[]): Turn[] => {
  const turns: Turn[] = [];
  for (const message of messages || []) {
    const role = String(message?.role || '').toLowerCase();
    const content = String(message?.content || '').trim();
    if (role === 'user') {
      turns.push({ question: content, answer: '' });
      continue;
    }
    if (role !== 'assistant' || !content) continue;
    if (!turns.length) {
      turns.push({ question: '', answer: content });
      continue;
    }
    const current = turns[turns.length - 1];
    current.answer = current.answer ? `${current.answer}\n\n${content}` : content;
  }
  // 末轮可能只有问题没有回答（正在生成或已中断），不导出空轮
  return turns.filter((turn) => turn.question || turn.answer);
};

const formatTimestamp = (date = new Date()): string => {
  const pad = (value: number) => String(value).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
};

const firstLine = (text: string): string => {
  const line = String(text || '')
    .split('\n')
    .map((item) => item.trim())
    .find(Boolean);
  return line || '';
};

/** 会话标题：优先用调用方给的标题，否则取第一条问题的首行。 */
export const deriveConversationTitle = (
  messages: ExportMessage[],
  meta: ConversationExportMeta = {},
): string => {
  const explicit = String(meta.title || '').trim();
  if (explicit && explicit !== '新对话' && explicit !== '新任务') return explicit;
  const firstQuestion = collectTurns(messages)[0]?.question || '';
  const line = firstLine(firstQuestion).replace(/[#*`>]/g, '').trim();
  return line.slice(0, 60) || '会话记录';
};

/** 用于文件名：去掉路径与不可用字符。 */
export const safeFileStem = (title: string): string => {
  let stem = String(title || '会话记录').replace(/[\\/:*?"<>|\n\r\t]/g, '_').trim();
  stem = stem.replace(/\s+/g, ' ').slice(0, 60).trim();
  return stem || '会话记录';
};

export interface BuiltConversationMarkdown {
  markdown: string;
  turnCount: number;
  /** 因超出上限而被丢弃的最早轮次数 */
  droppedTurns: number;
}

/**
 * 拼装整会话 Markdown。
 *
 * 轮次之间用 `---` 分隔、问题用加粗行而不是标题：回答自身常包含 `##`/`###`
 * 标题，若给轮次也套标题会把两级结构混在一起，Word 目录会乱。
 */
export const buildConversationMarkdown = (
  messages: ExportMessage[],
  meta: ConversationExportMeta = {},
  options: { maxChars?: number } = {},
): BuiltConversationMarkdown => {
  const turns = collectTurns(messages);
  const title = deriveConversationTitle(messages, meta);
  const agentName = String(meta.agentName || '').trim();

  const renderTurn = (turn: Turn, index: number): string => {
    const parts: string[] = [];
    if (turn.question) parts.push(`**问 ${index + 1}：** ${turn.question}`);
    if (turn.answer) parts.push(turn.answer);
    return parts.join('\n\n');
  };

  const buildFrom = (startIndex: number): string => {
    const kept = turns.slice(startIndex);
    const header = [
      `# ${title}`,
      '',
      `> 导出时间：${formatTimestamp()}` +
        (agentName ? ` · 智能体：${agentName}` : '') +
        ` · 共 ${kept.length} 轮问答` +
        (startIndex > 0 ? `（因篇幅限制已省略最早 ${startIndex} 轮）` : ''),
    ].join('\n');
    const body = kept.map(renderTurn).filter(Boolean).join(TURN_SEPARATOR);
    return body ? `${header}${TURN_SEPARATOR}${body}\n` : `${header}\n`;
  };

  const maxChars = Math.max(1000, Number(options.maxChars) || Number.MAX_SAFE_INTEGER);
  let startIndex = 0;
  let markdown = buildFrom(startIndex);
  // 从最早的轮次开始丢，直到落到上限内；全丢完仍超限时只能截断正文。
  while (markdown.length > maxChars && startIndex < turns.length - 1) {
    startIndex += 1;
    markdown = buildFrom(startIndex);
  }
  if (markdown.length > maxChars) {
    markdown = `${markdown.slice(0, maxChars - 20).trimEnd()}\n\n…（已截断）\n`;
  }

  return {
    markdown,
    turnCount: turns.length - startIndex,
    droppedTurns: startIndex,
  };
};

const triggerBlobDownload = (blob: Blob, filename: string) => {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
};

const filenameFromDisposition = (header: string | null): string | null => {
  if (!header) return null;
  const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }
  const basicMatch = header.match(/filename="?([^";]+)"?/i);
  return basicMatch?.[1]?.trim() || null;
};

/** 导出为 Markdown 文件（纯前端）。 */
export const exportConversationMarkdown = (
  messages: ExportMessage[],
  meta: ConversationExportMeta = {},
): BuiltConversationMarkdown => {
  const built = buildConversationMarkdown(messages, meta);
  const stem = safeFileStem(deriveConversationTitle(messages, meta));
  triggerBlobDownload(
    new Blob([built.markdown], { type: 'text/markdown;charset=utf-8' }),
    `${stem}.md`,
  );
  return built;
};

/** 导出为 Word 文档（复用后端 export-docx 渲染）。 */
export const exportConversationDocx = async (
  messages: ExportMessage[],
  meta: ConversationExportMeta = {},
): Promise<BuiltConversationMarkdown> => {
  const token = authState.value.token;
  if (!token) throw new Error('请先登录后再导出 Word');

  const built = buildConversationMarkdown(messages, meta, {
    maxChars: MAX_DOCX_CONTENT_CHARS,
  });
  if (!built.markdown.trim()) throw new Error('当前会话没有可导出的内容');

  const title = safeFileStem(deriveConversationTitle(messages, meta));
  const response = await fetch('/api/v2/files/export-docx', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ title, content: built.markdown }),
  });

  if (response.status === 401) throw new Error('登录状态已过期，请重新登录');
  if (!response.ok) {
    let message = `导出 Word 失败 (${response.status})`;
    try {
      const payload = await response.json();
      if (typeof payload?.detail === 'string') message = payload.detail;
    } catch {
      /* 非 JSON 错误体忽略 */
    }
    throw new Error(message);
  }

  const blob = await response.blob();
  const filename =
    filenameFromDisposition(response.headers.get('Content-Disposition')) || `${title}.docx`;
  triggerBlobDownload(blob, filename);
  return built;
};
