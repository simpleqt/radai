/**
 * 过程事件 → 统一步骤模型。
 *
 * 两条路径：
 * 1. 事件带 `display`（后端 presenter 产出）→ 校验裁剪后直接使用；
 * 2. 事件没有 `display`（历史会话、未接入 presenter 的工具）→ 降级映射，
 *    行为与改造前的 mapEventToStep 保持一致，保证零回归。
 */

import { cleanProcessText, cleanStepDetail, isNoisyPayloadText, shortenText } from './humanize';
import type {
  ChipTone,
  DisplayAction,
  DisplayChip,
  DisplayView,
  IconKey,
  NormalizedStep,
  ProcessEvent,
  SourceEntry,
  StepTone,
  TableColumn,
  ToolDisplay,
} from './types';

const INTERNAL_TOOL_NAMES = new Set([
  'read_local_skill_file',
  'read_skill_reference',
  'list_available_skills',
]);

const MAX_CHIPS = 6;
const MAX_TABLE_ROWS = 20;
const MAX_ISSUE_ITEMS = 20;
const MAX_SOURCES = 6;
// An action option is a short instruction the user endorses with one click,
// not a channel for arbitrary model/tool text.
const MAX_ACTION_MESSAGE_CHARS = 200;

const CHIP_TONES = new Set<ChipTone>(['info', 'success', 'warning', 'error']);
const VIEWS = new Set<DisplayView>(['text', 'kv', 'table', 'issues', 'sources', 'artifact']);

/* ------------------------------------------------------------------ */
/* 通用取值工具                                                        */
/* ------------------------------------------------------------------ */

const asRecord = (value: unknown): Record<string, any> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, any>) : {};

const asArray = (value: unknown): any[] => (Array.isArray(value) ? value : []);

const text = (value: unknown): string => String(value ?? '').trim();

const uniqueLimited = (values: string[], limit = 5): string[] => {
  const deduped: string[] = [];
  for (const value of values) {
    const cleaned = value.trim();
    if (!cleaned) continue;
    if (!deduped.includes(cleaned)) deduped.push(cleaned);
    if (deduped.length >= limit) break;
  }
  return deduped;
};

const extractPath = (value?: string): string | undefined => {
  if (!value) return undefined;
  return value.match(/\/mnt\/[\w\-./]+/)?.[0];
};

const normalizeDomain = (urlOrDomain: string): string => {
  const value = text(urlOrDomain).toLowerCase();
  if (!value) return '';
  try {
    const withScheme = /^https?:\/\//.test(value) ? value : `https://${value}`;
    return new URL(withScheme).hostname.replace(/^www\./, '');
  } catch {
    return value.replace(/^www\./, '');
  }
};

// Process-event URLs come from backend presenters, which relay tool results
// (and therefore retrieved document content). Anything that is not plain
// http(s) — javascript:, data:, vbscript:, file: … — must never reach an
// <a href>, so it is dropped rather than "fixed" by prefixing a scheme.
const UNSAFE_URL_SCHEME = /^[a-z][a-z0-9+.-]*:/i;

const normalizeUrl = (rawUrl: string): string => {
  const value = text(rawUrl);
  if (!value) return '';
  if (/^https?:\/\//i.test(value)) return value;
  // A bare "example.com/x" has no scheme and is safe to upgrade; a value that
  // declares some other scheme is rejected outright.
  if (UNSAFE_URL_SCHEME.test(value)) return '';
  if (value.startsWith('//')) return `https:${value}`;
  return `https://${value}`;
};

const isImageUrl = (url: string) => /\.(png|jpg|jpeg|gif|webp|bmp|svg)(\?|$)/.test(text(url).toLowerCase());
const isKbFileUrl = (url: string) => /\/files\//.test(text(url).toLowerCase());

const dedupeSources = (candidates: SourceEntry[]): SourceEntry[] => {
  const deduped: SourceEntry[] = [];
  const seen = new Set<string>();
  for (const item of candidates) {
    if (!item.url || !item.domain) continue;
    if (isImageUrl(item.url) || isKbFileUrl(item.url)) continue;
    if (seen.has(item.url)) continue;
    seen.add(item.url);
    deduped.push(item);
    if (deduped.length >= MAX_SOURCES) break;
  }
  return deduped;
};

const parsePossibleJson = (value: unknown): Record<string, any> | null => {
  if (!value) return null;
  if (typeof value === 'object' && !Array.isArray(value)) return value as Record<string, any>;
  if (typeof value !== 'string') return null;
  const raw = value.trim();
  if (!raw.startsWith('{')) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
};

/* ------------------------------------------------------------------ */
/* display 路径：校验与裁剪                                            */
/* ------------------------------------------------------------------ */

const sanitizeChips = (chips: unknown): DisplayChip[] =>
  asArray(chips)
    .map((chip): DisplayChip | null => {
      const record = asRecord(chip);
      const chipText = cleanProcessText(record.text);
      if (!chipText) return null;
      const tone = CHIP_TONES.has(record.tone) ? (record.tone as ChipTone) : 'info';
      return { text: shortenText(chipText, 40), tone };
    })
    .filter((chip): chip is DisplayChip => chip !== null)
    .slice(0, MAX_CHIPS);

const sanitizeAction = (action: unknown): DisplayAction | undefined => {
  const record = asRecord(action);
  const prompt = cleanProcessText(record.prompt);
  const kind = record.kind === 'choose' || record.kind === 'edit' ? record.kind : 'confirm';
  if (!prompt) return undefined;

  const options = asArray(record.options)
    .map((option) => {
      const optionRecord = asRecord(option);
      const label = cleanProcessText(optionRecord.label);
      // Clicking an option sends `message` into the conversation as if the
      // user typed it. The string originates from a backend presenter that
      // relays tool output, so a poisoned tool result could smuggle a long
      // instruction behind an innocuous label ("确认继续"). Clean it the same
      // way as displayed text and bound its length so an option can only carry
      // a short, reviewable instruction.
      const message = shortenText(cleanProcessText(optionRecord.message), MAX_ACTION_MESSAGE_CHARS);
      if (!label || !message) return null;
      return {
        label,
        message,
        tone: CHIP_TONES.has(optionRecord.tone) ? (optionRecord.tone as ChipTone) : 'info',
      };
    })
    .filter((option): option is NonNullable<typeof option> => option !== null)
    .slice(0, 4);

  // input_template only pre-fills the composer (the user still reviews and
  // sends it), but bound it for the same reason as `message`.
  const inputTemplate = shortenText(cleanProcessText(record.input_template), MAX_ACTION_MESSAGE_CHARS);
  if (!options.length && !inputTemplate) return undefined;

  return { kind, prompt, options, input_template: inputTemplate };
};

const sanitizeTableData = (data: Record<string, any>): Record<string, unknown> | null => {
  const columns: TableColumn[] = asArray(data.columns)
    .map((column): TableColumn | null => {
      const record = asRecord(column);
      const key = text(record.key);
      if (!key) return null;
      const align = record.align === 'right' || record.align === 'center' ? record.align : 'left';
      return { key, label: cleanProcessText(record.label) || key, align };
    })
    .filter((column): column is TableColumn => column !== null)
    .slice(0, 8);
  if (!columns.length) return null;

  const allRows = asArray(data.rows);
  const rows = allRows.slice(0, MAX_TABLE_ROWS).map((row) => {
    const record = asRecord(row);
    const cell: Record<string, string | number | boolean | null> = {};
    for (const column of columns) {
      const value = record[column.key];
      cell[column.key] =
        value === null || value === undefined
          ? null
          : typeof value === 'number' || typeof value === 'boolean'
            ? value
            : shortenText(cleanProcessText(String(value)), 80);
    }
    return cell;
  });
  if (!rows.length) return null;

  const totalRows = Number.isFinite(data.total_rows) ? Number(data.total_rows) : allRows.length;
  return { columns, rows, total_rows: totalRows, truncated: totalRows > rows.length };
};

const sanitizeIssuesData = (data: Record<string, any>): Record<string, unknown> | null => {
  const items = asArray(data.items)
    .map((item) => {
      const record = asRecord(item);
      const message = cleanProcessText(record.message);
      if (!message) return null;
      const level = record.level === 'error' || record.level === 'warning' ? record.level : 'info';
      return {
        code: text(record.code) || undefined,
        level,
        message: shortenText(message, 200),
        locator: cleanProcessText(record.locator) || undefined,
      };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null);
  if (!items.length) return null;

  const ordered = [
    ...items.filter((item) => item.level === 'error'),
    ...items.filter((item) => item.level === 'warning'),
    ...items.filter((item) => item.level === 'info'),
  ].slice(0, MAX_ISSUE_ITEMS);

  return {
    items: ordered,
    counts: {
      error: items.filter((item) => item.level === 'error').length,
      warning: items.filter((item) => item.level === 'warning').length,
    },
  };
};

const sanitizeKvData = (data: Record<string, any>): Record<string, unknown> | null => {
  const rows = asArray(data.rows)
    .map((row) => {
      const record = asRecord(row);
      const label = cleanProcessText(record.label);
      const value = cleanProcessText(String(record.value ?? ''));
      if (!label && !value) return null;
      return {
        label: label || '-',
        value: shortenText(value, 120),
        tone: CHIP_TONES.has(record.tone) ? (record.tone as ChipTone) : undefined,
      };
    })
    .filter((row): row is NonNullable<typeof row> => row !== null)
    .slice(0, 12);
  return rows.length ? { rows } : null;
};

const sanitizeSourcesData = (data: Record<string, any>): Record<string, unknown> | null => {
  const items = dedupeSources(
    asArray(data.items).map((item) => {
      const record = asRecord(item);
      const url = normalizeUrl(text(record.url));
      return {
        title: cleanProcessText(record.title) || undefined,
        url,
        domain: text(record.domain) || normalizeDomain(url),
      };
    }),
  );
  return items.length ? { items } : null;
};

const sanitizeArtifactData = (data: Record<string, any>): Record<string, unknown> | null => {
  const title = cleanProcessText(data.title);
  if (!title) return null;
  return {
    title,
    kind: text(data.kind) || undefined,
    url: text(data.url) || undefined,
    size: text(data.size) || undefined,
  };
};

const sanitizeTextData = (data: Record<string, any>, fallback: string): Record<string, unknown> | null => {
  const value = cleanStepDetail(String(data.text ?? '')) || fallback;
  return value ? { text: value } : null;
};

/** 视图载荷校验：形状不合法时回落 text，绝不把原始对象透给渲染层。 */
const sanitizeViewData = (
  view: DisplayView,
  rawData: unknown,
  fallbackText: string,
): { view: DisplayView; data: Record<string, unknown> } => {
  const data = asRecord(rawData);

  const sanitized =
    view === 'table'
      ? sanitizeTableData(data)
      : view === 'issues'
        ? sanitizeIssuesData(data)
        : view === 'kv'
          ? sanitizeKvData(data)
          : view === 'sources'
            ? sanitizeSourcesData(data)
            : view === 'artifact'
              ? sanitizeArtifactData(data)
              : sanitizeTextData(data, fallbackText);

  if (sanitized) return { view, data: sanitized };
  return fallbackText ? { view: 'text', data: { text: fallbackText } } : { view: 'text', data: {} };
};

const toneFromLevel = (level?: string): StepTone => {
  if (level === 'error') return 'error';
  if (level === 'warning') return 'warning';
  return 'default';
};

const iconFromView = (view: DisplayView, tone: StepTone, tool?: string): IconKey => {
  if (tone === 'error') return 'alert';
  if (view === 'issues') return 'alert';
  if (view === 'table') return 'table';
  if (view === 'sources') return 'web';
  if (view === 'artifact') return 'file';
  if (tool?.includes('search')) return 'search';
  return 'tool';
};

/* ------------------------------------------------------------------ */
/* 降级路径：老事件语义还原                                            */
/* ------------------------------------------------------------------ */

const legacySources = (event: ProcessEvent): SourceEntry[] => {
  const metadata = asRecord(event.metadata);

  const fromResults = (items: any[]): SourceEntry[] =>
    items
      .map((item): SourceEntry | null => {
        const record = asRecord(item);
        const url = text(record.url || record.href || record.link);
        if (!url) return null;
        return {
          title: text(record.title) || undefined,
          domain: normalizeDomain(url),
          url: normalizeUrl(url),
        };
      })
      .filter((item): item is SourceEntry => item !== null);

  const fromPlain = (values: unknown): SourceEntry[] =>
    asArray(values)
      .map((value) => text(value))
      .filter(Boolean)
      .map((value) => ({ domain: normalizeDomain(value), url: normalizeUrl(value) }));

  const detailJson = parsePossibleJson(event.detail || event.summary);
  const rawText = `${event.detail || ''} ${event.summary || ''}`;
  const fromRegex = [...rawText.matchAll(/https?:\/\/[^\s"')]+/g)].map((match) => ({
    domain: normalizeDomain(match[0]),
    url: normalizeUrl(match[0]),
  }));

  return dedupeSources([
    ...fromResults(asArray(metadata.web_results)),
    ...fromPlain(metadata.urls),
    ...fromResults(asArray(detailJson?.results)),
    ...fromRegex,
    ...fromPlain(metadata.domains),
  ]);
};

const legacyQuery = (event: ProcessEvent): string => {
  const metadata = asRecord(event.metadata);
  const input = asRecord(metadata.input);
  const fromInput = text(input.query);
  if (fromInput) return fromInput;
  const fromMetadata = text(metadata.query);
  if (fromMetadata) return fromMetadata;

  const raw = event.detail || event.summary || '';
  const quoted = raw.match(/“([^”]+)”/)?.[1] || raw.match(/'([^']+)'/)?.[1];
  if (quoted) return quoted;
  return raw.match(/"query"\s*:\s*"([^"]+)"/)?.[1] ?? '';
};

const legacyKbChips = (event: ProcessEvent): DisplayChip[] => {
  const metadata = asRecord(event.metadata);
  const input = asRecord(metadata.input);
  const kbIds = asArray(metadata.kb_ids).length ? asArray(metadata.kb_ids) : asArray(input.kb_ids);
  return uniqueLimited(kbIds.map((id) => `库: ${text(id)}`), 3).map((chipText) => ({ text: chipText, tone: 'info' as ChipTone }));
};

const legacyTaskChips = (event: ProcessEvent): DisplayChip[] => {
  const metadata = asRecord(event.metadata);
  const toolCalls = asArray(metadata.message_tool_calls).length
    ? asArray(metadata.message_tool_calls)
    : asArray(metadata.tool_calls);

  const chips = [
    ...uniqueLimited(asArray(metadata.search_queries).map((query) => `检索: ${text(query)}`), 4),
    ...uniqueLimited(
      toolCalls
        .map((name) => text(name))
        .filter((name) => name && !INTERNAL_TOOL_NAMES.has(name))
        .map((name) => `工具: ${name}`),
      4,
    ),
    ...uniqueLimited(asArray(metadata.result_sections).map((section) => `要点: ${text(section)}`), 4),
  ];
  return uniqueLimited(chips, MAX_CHIPS).map((chipText) => ({ text: chipText, tone: 'info' as ChipTone }));
};

const legacyProgressChip = (event: ProcessEvent): string | undefined => {
  const metadata = asRecord(event.metadata);
  const index = metadata.message_index;
  const total = metadata.total_messages;
  if (typeof index === 'number' && typeof total === 'number' && total > 0) {
    return `进度 ${index}/${total}`;
  }
  return undefined;
};

const ABNORMAL_DETAIL_PATTERN =
  /(?:execution timed out|timed out after|task timed out|执行超时|执行失败|必需工具未完成|不可采信|无有效结果|子任务未返回内容|produced no events)/i;

export const isAbnormalEvent = (event: ProcessEvent): boolean => {
  const metadata = asRecord(event.metadata);
  const runtimeType = text(metadata.runtime_type || metadata.task_status);
  if (event.kind === 'error' || event.level === 'error') return true;
  if (['task_failed', 'task_timed_out', 'coordination.unit.failed'].includes(runtimeType)) return true;
  if (event.kind === 'task_end' && event.level === 'warning') return true;
  if (event.kind === 'status' && event.level === 'warning') return true;
  const raw = `${event.title || ''} ${event.detail || ''} ${event.summary || ''}`;
  return (event.kind === 'task_end' || event.kind === 'status') && ABNORMAL_DETAIL_PATTERN.test(raw);
};

const isPartialHandoffEvent = (event: ProcessEvent): boolean => {
  const metadata = asRecord(event.metadata);
  return (
    metadata.runtime_type === 'coordination.partial_handoff' ||
    /已提交阶段性结论|阶段性结果异常/.test(event.title || '')
  );
};

const partialHandoffRole = (event: ProcessEvent): string => {
  const role = text(asRecord(event.metadata).role);
  if (role) return role;
  return text(event.title).replace(/(?:已提交阶段性结论|阶段性结果异常)$/, '') || '研究分支';
};

interface LegacyStep {
  label: string;
  statusText?: string;
  chips?: DisplayChip[];
  sources?: SourceEntry[];
  icon: IconKey;
  tone?: StepTone;
  path?: string;
}

/** 老事件 → 展示语义。规则与改造前的 mapEventToStep 一一对应。 */
const legacyStep = (event: ProcessEvent): LegacyStep | null => {
  const tool = event.tool;
  const detail = cleanStepDetail(event.detail);
  const summaryDetail = cleanStepDetail(event.detail || event.summary);

  if (event.kind === 'tool_start') {
    if (tool === 'task') return null;
    if (tool === 'large_result_summarization') {
      return {
        label: '正在压缩检索结果，请稍候…',
        statusText: detail || '查询结果正文较长，正在提取结论和关键证据；对应页面标题和链接会继续保留。',
        icon: 'note',
      };
    }
    if (tool === 'context_compression') {
      return {
        label: '正在压缩历史上下文，请稍候…',
        statusText: detail || '正在精简历史消息以继续执行当前任务。',
        icon: 'note',
      };
    }
    if (tool === 'web_search') {
      const query = legacyQuery(event);
      return {
        label: query ? `联网检索关键词: ${query}` : '联网检索',
        statusText: summaryDetail,
        chips: query ? [{ text: `关键词: ${query}`, tone: 'info' }] : [],
        icon: 'search',
      };
    }
    if (tool === 'multi_kb_search' || tool === 'kb_query_with_keywords') {
      const query = legacyQuery(event);
      return {
        label: query ? `知识库检索关键词: ${query}` : '知识库检索',
        statusText: summaryDetail,
        chips: [...(query ? [{ text: `关键词: ${query}`, tone: 'info' as ChipTone }] : []), ...legacyKbChips(event)].slice(0, MAX_CHIPS),
        icon: 'search',
      };
    }
    return {
      label: `调用工具 ${tool || ''}`.trim(),
      statusText: summaryDetail,
      path: extractPath(event.detail),
      icon: 'tool',
    };
  }

  if (event.kind === 'tool_result') {
    if (tool === 'task') return null;
    if (tool === 'web_search') {
      const query = legacyQuery(event);
      const engine = text(asRecord(event.metadata).engine);
      return {
        label: query ? `网络检索完成: ${query}` : '网络检索完成',
        statusText: summaryDetail,
        sources: legacySources(event),
        chips: engine ? [{ text: `引擎: ${engine}`, tone: 'info' }] : [],
        icon: 'search',
      };
    }
    if (tool === 'multi_kb_search' || tool === 'kb_query_with_keywords') {
      return {
        label: '知识库检索完成',
        statusText: cleanStepDetail(event.summary || event.detail),
        chips: legacyKbChips(event),
        icon: 'search',
      };
    }
    return null;
  }

  if (event.kind === 'tool_end') {
    if (tool === 'task' || tool === 'web_search' || tool === 'multi_kb_search' || tool === 'kb_query_with_keywords') {
      return null;
    }
    if (tool === 'large_result_summarization') {
      return {
        label: '检索结果压缩完成',
        statusText: detail || '已提取关键证据，页面标题和链接保持可查看。',
        icon: 'note',
      };
    }
    if (tool === 'context_compression') {
      return { label: '历史上下文压缩完成', statusText: detail, icon: 'note' };
    }
    return {
      label: cleanProcessText(event.title) || `${tool || '工具'} 调用完成`,
      statusText: detail,
      icon: 'tool',
    };
  }

  if (event.kind === 'task_start') {
    return { label: cleanProcessText(event.title) || '子智能体已启动', statusText: detail, icon: 'task' };
  }

  if (event.kind === 'task_end') {
    const abnormal = isAbnormalEvent(event);
    return {
      label: abnormal ? '子任务异常' : '子任务已完成',
      statusText: detail,
      sources: legacySources(event),
      chips: legacyTaskChips(event),
      icon: abnormal ? 'alert' : 'task',
      tone: abnormal ? 'error' : 'default',
    };
  }

  if (event.kind === 'task_progress') {
    const progress = legacyProgressChip(event);
    return {
      label: progress ? `子智能体输出（${progress}）` : '子智能体输出',
      statusText: detail,
      sources: legacySources(event),
      chips: uniqueLimited([progress || '', ...legacyTaskChips(event).map((chip) => chip.text)], 4).map((chipText) => ({
        text: chipText,
        tone: 'info' as ChipTone,
      })),
      icon: 'task',
    };
  }

  if (event.kind === 'status') {
    const abnormal = isAbnormalEvent(event);
    return {
      label:
        abnormal && isPartialHandoffEvent(event)
          ? `${partialHandoffRole(event)}阶段性结果异常`
          : cleanProcessText(event.title) || '状态更新',
      statusText: detail,
      icon: abnormal ? 'alert' : 'think',
      tone: abnormal ? 'error' : 'default',
    };
  }

  if (event.kind === 'answer') {
    return { label: '回答生成完成', statusText: detail, icon: 'note' };
  }

  if (event.kind === 'thinking') {
    return { label: '推理中', statusText: detail, icon: 'think' };
  }

  if (event.kind === 'error') {
    return { label: cleanProcessText(event.title) || '执行异常', statusText: detail, icon: 'alert', tone: 'error' };
  }

  if (event.kind === 'artifact') {
    return { label: cleanProcessText(event.title) || '生成工件', statusText: detail, icon: 'folder' };
  }

  return {
    label: cleanProcessText(event.title) || event.kind || '流程事件',
    statusText: summaryDetail,
    icon: tool?.includes('web') ? 'web' : 'tool',
  };
};

/* ------------------------------------------------------------------ */
/* 对外入口                                                            */
/* ------------------------------------------------------------------ */

const readDisplay = (event: ProcessEvent): ToolDisplay | null => {
  const display = event.display;
  if (!display || typeof display !== 'object') return null;
  return cleanProcessText(display.label) ? display : null;
};

export function normalizeEvent(event: ProcessEvent, index: number, messageId: string): NormalizedStep | null {
  const id = `${messageId}-step-${index}`;
  const phase = text(event.phase) || undefined;
  const display = readDisplay(event);

  if (display) {
    const tone = toneFromLevel(event.level);
    const statusText = cleanStepDetail(display.status_text);
    const requestedView = VIEWS.has(display.view as DisplayView) ? (display.view as DisplayView) : 'text';
    const { view, data } = sanitizeViewData(requestedView, display.data, requestedView === 'text' ? statusText : '');

    return {
      id,
      index,
      kind: event.kind,
      tool: event.tool,
      phase,
      groupId: '',
      label: shortenText(cleanProcessText(display.label), 120),
      statusText: view === 'text' ? undefined : statusText,
      chips: sanitizeChips(display.chips),
      view,
      data,
      action: sanitizeAction(display.action),
      durationMs: event.duration_ms,
      timestamp: event.timestamp,
      tone,
      icon: iconFromView(view, tone, event.tool),
    };
  }

  const legacy = legacyStep(event);
  if (!legacy) return null;

  const label = shortenText(legacy.label, 120);
  if (!label) return null;

  const sources = legacy.sources || [];
  const statusText = legacy.statusText && !isNoisyPayloadText(legacy.statusText) ? legacy.statusText : '';
  const view: DisplayView = sources.length ? 'sources' : 'text';
  const data = sources.length ? { items: sources } : statusText ? { text: statusText } : {};

  return {
    id,
    index,
    kind: event.kind,
    tool: event.tool,
    phase,
    groupId: '',
    label,
    statusText: sources.length ? statusText : undefined,
    chips: legacy.chips || [],
    view,
    data,
    durationMs: event.duration_ms,
    timestamp: event.timestamp,
    tone: legacy.tone || toneFromLevel(event.level),
    icon: legacy.icon,
    path: legacy.path,
  };
}
