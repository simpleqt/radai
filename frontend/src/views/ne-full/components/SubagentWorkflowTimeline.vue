<script setup lang="ts">
import { computed } from 'vue';
import {
  BookOpenText,
  FolderOpen,
  Globe,
  Lightbulb,
  MessageCircleQuestionMark,
  NotebookPen,
  Search,
  SquareTerminal,
  Wrench,
} from 'lucide-vue-next';
import { type AgentProcessEvent } from '@/api/agent-v2';

interface Props {
  messageId: string;
  events: AgentProcessEvent[];
}

interface WorkflowStep {
  id: string;
  label: string;
  detail?: string;
  path?: string;
  tags?: string[];
  websites?: Array<{ domain: string; url: string }>;
  durationText?: string;
  icon: any;
}

const props = defineProps<Props>();

const INTERNAL_TOOL_NAMES = new Set([
  'read_local_skill_file',
  'read_skill_reference',
  'list_available_skills',
]);

const MAX_DETAIL_LENGTH = 260;

const parseQuery = (text?: string) => {
  if (!text) return '';
  const m = text.match(/"query"\s*:\s*"([^"]+)"/);
  return m?.[1] ?? '';
};

const extractPath = (text?: string) => {
  if (!text) return undefined;
  const match = text.match(/\/mnt\/[\w\-./]+/);
  return match?.[0];
};

const normalizeDomain = (urlOrDomain: string) => {
  const value = (urlOrDomain || '').trim().toLowerCase();
  if (!value) return '';
  try {
    const withScheme = /^https?:\/\//.test(value) ? value : `https://${value}`;
    const host = new URL(withScheme).hostname;
    return host.replace(/^www\./, '');
  } catch {
    return value.replace(/^www\./, '');
  }
};

const normalizeUrl = (rawUrl: string) => {
  const value = (rawUrl || '').trim();
  if (!value) return '';
  if (!/^https?:\/\//i.test(value)) {
    return `https://${value}`;
  }
  return value;
};

const isImageUrl = (url: string) => {
  const value = (url || '').trim().toLowerCase();
  if (!value) return false;
  return /\.(png|jpg|jpeg|gif|webp|bmp|svg)(\?|$)/.test(value);
};

const isKbFileUrl = (url: string) => {
  const value = (url || '').trim().toLowerCase();
  if (!value) return false;
  return /\/files\//.test(value);
};

const uniqueLimited = (values: string[], limit = 5) => {
  const deduped: string[] = [];
  for (const value of values) {
    const cleaned = value.trim();
    if (!cleaned) continue;
    if (!deduped.includes(cleaned)) deduped.push(cleaned);
    if (deduped.length >= limit) break;
  }
  return deduped;
};

const parsePossibleJson = (value: unknown): Record<string, any> | null => {
  if (!value) return null;
  if (typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, any>;
  }
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!text.startsWith('{')) return null;
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, any>;
    }
  } catch {
    return null;
  }
  return null;
};

const cleanDetailText = (value?: string) => {
  if (!value) return '';
  let text = String(value).trim();

  // Normalize malformed superscript/subscript tag fragments in plain text
  // (e.g. "< sup >9< / sup >", "&lt; sup &gt;9&lt; / sup &gt;").
  const normalizePlainSuperSubNoise = (input: string): string => {
    let output = input;

    output = output
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

    // Convert normalized tags to plain-text exponent/index for timeline display.
    output = output
      .replace(/<sup>\s*([^<\n]{1,16})\s*<\/sup>/gi, '^$1')
      .replace(/<sub>\s*([^<\n]{1,16})\s*<\/sub>/gi, '_$1');

    return output;
  };

  text = normalizePlainSuperSubNoise(text);

  // Convert escaped newlines from serialized message payloads into readable text.
  text = text.replace(/\\n/g, '\n').replace(/\\t/g, '\t');

  // Remove wrappers like content='...' or content="...".
  const contentWrapper = text.match(/^content=(['"])([\s\S]*)\1$/);
  if (contentWrapper && contentWrapper[2]) {
    text = contentWrapper[2].trim();
  }

  // Strip model thinking segments and any dangling think tags.
  text = text.replace(/<(think|thought)>[\s\S]*?<\/\1>/gi, '');
  text = text.replace(/<\/?(think|thought)>/gi, '');

  text = text
    .replace(/^Task\s+Succeeded\.\s*Result:\s*/i, '')
    .replace(/^Task\s+failed\.\s*Error:\s*/i, '执行失败：')
    .replace(/^Task\s+timed\s+out\.\s*Error:\s*/i, '执行超时：')
    .replace(/^Task\s+polling\s+timed\s+out\.?\s*/i, '任务轮询超时：');

  // Remove injected runtime time policy block from delegated subtask text.
  text = text.replace(/<runtime_current_time>[\s\S]*?<\/runtime_current_time>/gi, '').trim();
  text = text.replace(/^\s*时间范围严格限定为：.*$/gim, '').trim();

  // Strip one pair of wrapping quotes left after prefix cleanup.
  if ((text.startsWith('"') && text.endsWith('"')) || (text.startsWith("'") && text.endsWith("'"))) {
    text = text.slice(1, -1).trim();
  }

  return text;
};

const isNoisyPayloadText = (text: string) => {
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
};

const shortenText = (text: string, limit = MAX_DETAIL_LENGTH) => {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}...`;
};

const cleanStepDetail = (value?: string) => {
  const cleaned = cleanDetailText(value);
  if (!cleaned) return '';
  if (isNoisyPayloadText(cleaned)) return '';
  return shortenText(cleaned);
};

const parseQueryFromText = (text?: string) => {
  if (!text) return '';
  const direct = text.match(/“([^”]+)”/)?.[1] || text.match(/'([^']+)'/)?.[1];
  if (direct) return direct;
  return parseQuery(text);
};

const getEventQuery = (event: AgentProcessEvent) => {
  const metadata = (event.metadata || {}) as Record<string, any>;
  const input = metadata.input;
  if (input && typeof input === 'object' && !Array.isArray(input)) {
    const query = String((input as Record<string, any>).query || '').trim();
    if (query) return query;
  }
  const metadataQuery = String(metadata.query || '').trim();
  if (metadataQuery) return metadataQuery;
  return parseQueryFromText(event.detail || event.summary || '');
};

const getKbTags = (event: AgentProcessEvent) => {
  const metadata = (event.metadata || {}) as Record<string, any>;
  const input = metadata.input;
  const inputData = input && typeof input === 'object' && !Array.isArray(input)
    ? input as Record<string, any>
    : {};
  const kbIds = Array.isArray(metadata.kb_ids)
    ? metadata.kb_ids
    : (Array.isArray(inputData.kb_ids) ? inputData.kb_ids : []);
  return uniqueLimited(kbIds.map((id) => `库: ${String(id)}`), 3);
};

const getWebsites = (event: AgentProcessEvent) => {
  const metadata = (event.metadata || {}) as Record<string, any>;
  const fromWebResults = Array.isArray(metadata.web_results)
    ? (metadata.web_results as any[])
        .map((item) => {
          const url = String(item?.url || item?.href || item?.link || '').trim();
          if (!url) return null;
          return { domain: normalizeDomain(url), url: normalizeUrl(url) };
        })
        .filter((item): item is { domain: string; url: string } => !!item)
    : [];
  const fromDomains = Array.isArray(metadata.domains)
    ? metadata.domains
        .map((d) => String(d || '').trim())
        .filter(Boolean)
        .map((domain) => ({ domain: normalizeDomain(domain), url: normalizeUrl(domain) }))
    : [];
  const fromUrls = Array.isArray(metadata.urls)
    ? metadata.urls
        .map((u) => String(u || '').trim())
        .filter(Boolean)
        .map((url) => ({ domain: normalizeDomain(url), url: normalizeUrl(url) }))
    : [];

  const jsonInDetail = parsePossibleJson(event.detail || event.summary || cleanDetailText(event.detail || event.summary));
  const detailUrls = Array.isArray(jsonInDetail?.results)
    ? (jsonInDetail?.results as any[])
        .map((item) => String(item?.url || item?.href || item?.link || '').trim())
        .filter(Boolean)
        .map((url) => ({ domain: normalizeDomain(url), url: normalizeUrl(url) }))
    : [];

  const text = `${event.detail || ''} ${event.summary || ''}`;
  const regexMatches = [...text.matchAll(/https?:\/\/[^\s"')]+/g)].map((m) => ({
    domain: normalizeDomain(m[0]),
    url: normalizeUrl(m[0]),
  }));

  const deduped: Array<{ domain: string; url: string }> = [];
  const seen = new Set<string>();
  for (const item of [...fromWebResults, ...fromUrls, ...detailUrls, ...regexMatches, ...fromDomains]) {
    if (!item.domain || !item.url) continue;
    if (isImageUrl(item.url)) continue;
    if (isKbFileUrl(item.url)) continue;
    if (seen.has(item.url)) continue;
    seen.add(item.url);
    deduped.push(item);
    if (deduped.length >= 6) break;
  }
  return deduped;
};

const durationText = (event: AgentProcessEvent) => {
  if (typeof event.duration_ms !== 'number') return undefined;
  if (event.duration_ms < 1000) return `${event.duration_ms}ms`;
  return `${(event.duration_ms / 1000).toFixed(1)}s`;
};

const getTaskProgressTag = (event: AgentProcessEvent) => {
  const metadata = (event.metadata || {}) as Record<string, any>;
  const index = metadata.message_index;
  const total = metadata.total_messages;
  if (typeof index === 'number' && typeof total === 'number' && total > 0) {
    return `进度 ${index}/${total}`;
  }
  return undefined;
};

const getResultSectionTags = (event: AgentProcessEvent) => {
  const metadata = (event.metadata || {}) as Record<string, any>;
  const sections = Array.isArray(metadata.result_sections) ? metadata.result_sections : [];
  return uniqueLimited(sections.map((section) => `要点: ${String(section)}`), 4);
};

const getToolCallTags = (event: AgentProcessEvent) => {
  const metadata = (event.metadata || {}) as Record<string, any>;
  const toolCalls = Array.isArray(metadata.message_tool_calls)
    ? metadata.message_tool_calls
    : (Array.isArray(metadata.tool_calls) ? metadata.tool_calls : []);
  return uniqueLimited(
    toolCalls
      .map((name) => String(name || '').trim())
      .filter((name) => name && !INTERNAL_TOOL_NAMES.has(name))
      .map((name) => `工具: ${name}`),
    4,
  );
};

const getSearchQueryTags = (event: AgentProcessEvent) => {
  const metadata = (event.metadata || {}) as Record<string, any>;
  const searchQueries = Array.isArray(metadata.search_queries) ? metadata.search_queries : [];
  return uniqueLimited(searchQueries.map((query) => `检索: ${String(query)}`), 4);
};

const mapEventToStep = (event: AgentProcessEvent, index: number): WorkflowStep | null => {
  if (event.kind === 'tool_start') {
    if (event.tool === 'task') return null;
    if (event.tool === 'web_search' || event.tool === 'duckduckgo_search') {
      const query = getEventQuery(event);
      return {
        id: `${props.messageId}-event-${index}`,
        label: query ? `联网检索关键词: ${query}` : '联网检索',
        detail: cleanStepDetail(event.detail || event.summary),
        tags: query ? [`关键词: ${query}`] : [],
        icon: Search,
      };
    }
    if (event.tool === 'multi_kb_search' || event.tool === 'kb_query_with_keywords') {
      const query = getEventQuery(event);
      const kbTags = getKbTags(event);
      return {
        id: `${props.messageId}-event-${index}`,
        label: query ? `知识库检索关键词: ${query}` : '知识库检索',
        detail: cleanStepDetail(event.detail || event.summary),
        tags: uniqueLimited([query ? `关键词: ${query}` : '', ...kbTags], 5),
        icon: Search,
      };
    }
    return {
      id: `${props.messageId}-event-${index}`,
      label: `调用工具 ${event.tool || ''}`.trim(),
      detail: cleanStepDetail(event.detail || event.summary),
      path: extractPath(event.detail),
      durationText: durationText(event),
      icon: Wrench,
    };
  }

  if (event.kind === 'tool_result') {
    if (event.tool === 'task') return null;
    if (event.tool === 'web_search' || event.tool === 'duckduckgo_search') {
      const query = getEventQuery(event);
      const sites = getWebsites(event);
      return {
        id: `${props.messageId}-event-${index}`,
        label: query ? `网络检索完成: ${query}` : '网络检索完成',
        detail: cleanStepDetail(event.summary || event.detail),
        websites: sites,
        durationText: durationText(event),
        icon: Search,
      };
    }
    if (event.tool === 'multi_kb_search' || event.tool === 'kb_query_with_keywords') {
      const kbTags = getKbTags(event);
      return {
        id: `${props.messageId}-event-${index}`,
        label: '知识库检索完成',
        detail: cleanStepDetail(event.summary || event.detail),
        tags: kbTags,
        durationText: durationText(event),
        icon: Search,
      };
    }
  }

  if (event.kind === 'tool_end') {
    if (event.tool === 'task' || event.tool === 'web_search' || event.tool === 'multi_kb_search' || event.tool === 'kb_query_with_keywords') {
      return null;
    }
    return {
      id: `${props.messageId}-event-${index}`,
      label: event.title || `${event.tool || '工具'} 调用完成`,
      detail: cleanStepDetail(event.detail),
      durationText: durationText(event),
      icon: Wrench,
    };
  }

  if (event.kind === 'task_start') {
    return {
      id: `${props.messageId}-event-${index}`,
      label: event.title || '子智能体已启动',
      detail: cleanStepDetail(event.detail),
      icon: MessageCircleQuestionMark,
    };
  }

  if (event.kind === 'task_end') {
    const sites = getWebsites(event);
    const sectionTags = getResultSectionTags(event);
    const toolTags = getToolCallTags(event);
    const queryTags = getSearchQueryTags(event);
    return {
      id: `${props.messageId}-event-${index}`,
      label: '子任务已完成',
      detail: cleanStepDetail(event.detail),
      websites: sites,
      tags: uniqueLimited([...queryTags, ...toolTags, ...sectionTags], 6),
      durationText: durationText(event),
      icon: MessageCircleQuestionMark,
    };
  }

  if (event.kind === 'task_progress') {
    const progressTag = getTaskProgressTag(event);
    const sites = getWebsites(event);
    const toolTags = getToolCallTags(event);
    return {
      id: `${props.messageId}-event-${index}`,
      label: progressTag ? `子智能体输出（${progressTag}）` : '子智能体输出',
      detail: cleanStepDetail(event.detail),
      tags: uniqueLimited([progressTag || '', ...toolTags].filter(Boolean), 4),
      websites: sites,
      durationText: durationText(event),
      icon: MessageCircleQuestionMark,
    };
  }

  if (event.kind === 'status') {
    return {
      id: `${props.messageId}-event-${index}`,
      label: event.title || '状态更新',
      detail: cleanStepDetail(event.detail),
      durationText: durationText(event),
      icon: Lightbulb,
    };
  }

  if (event.kind === 'answer') {
    return {
      id: `${props.messageId}-event-${index}`,
      label: '回答生成完成',
      detail: cleanStepDetail(event.detail),
      durationText: durationText(event),
      icon: NotebookPen,
    };
  }

  if (event.kind === 'thinking') {
    return {
      id: `${props.messageId}-event-${index}`,
      label: '推理中',
      detail: cleanStepDetail(event.detail),
      durationText: durationText(event),
      icon: Lightbulb,
    };
  }

  if (event.kind === 'error') {
    return {
      id: `${props.messageId}-event-${index}`,
      label: event.title || '执行异常',
      detail: cleanStepDetail(event.detail),
      durationText: durationText(event),
      icon: SquareTerminal,
    };
  }

  if (event.kind === 'artifact') {
    return {
      id: `${props.messageId}-event-${index}`,
      label: event.title || '生成工件',
      detail: cleanStepDetail(event.detail),
      durationText: durationText(event),
      icon: FolderOpen,
    };
  }

  return {
    id: `${props.messageId}-event-${index}`,
    label: event.title || event.kind || '流程事件',
    detail: cleanStepDetail(event.detail || event.summary),
    durationText: durationText(event),
    icon: event.tool?.includes('web') ? Globe : Wrench,
  };
};

const workflowSteps = computed<WorkflowStep[]>(() => {
  const events = props.events || [];
  const lastKbResultIndex = events.reduce((last, event, index) => {
    if (event.kind === 'tool_result' && (event.tool === 'multi_kb_search' || event.tool === 'kb_query_with_keywords')) {
      return index;
    }
    return last;
  }, -1);

  return events
    .map((event, index) => {
      if (
        event.kind === 'tool_result' &&
        (event.tool === 'multi_kb_search' || event.tool === 'kb_query_with_keywords') &&
        index !== lastKbResultIndex
      ) {
        return null;
      }
      return mapEventToStep(event, index);
    })
    .filter((step): step is WorkflowStep => !!step);
});
</script>

<template>
  <div class="space-y-2 pl-1 pt-2">
    <div v-for="step in workflowSteps" :key="step.id" class="flex items-start gap-2 text-[15px] text-slate-700 dark:text-slate-200">
      <component :is="step.icon" class="mt-1 size-[15px] shrink-0 text-slate-500 dark:text-slate-400" />
      <div class="min-w-0 flex-1">
        <p class="leading-6">{{ step.label }}</p>
        <div v-if="step.tags && step.tags.length" class="mt-1 flex flex-wrap gap-1.5">
          <span
            v-for="tag in step.tags"
            :key="`${step.id}-${tag}`"
            class="inline-flex items-center rounded-full border border-stone-300 bg-white/80 px-2 py-0.5 text-[11px] text-stone-700 dark:border-stone-700 dark:bg-stone-800/70 dark:text-stone-200"
          >
            {{ tag }}
          </span>
        </div>
        <div v-if="step.websites && step.websites.length" class="mt-1 space-y-1">
          <p class="text-[11px] text-emerald-700 dark:text-emerald-300">网页来源</p>
          <a
            v-for="site in step.websites"
            :key="`${step.id}-${site.url}`"
            :href="site.url"
            target="_blank"
            rel="noopener noreferrer"
            class="flex items-center gap-1.5 rounded-md border border-emerald-200 bg-emerald-50/70 px-2 py-1 text-[11px] text-emerald-700 transition-colors hover:bg-emerald-100 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300 dark:hover:bg-emerald-900/50"
          >
            <Globe class="size-3 shrink-0" />
            <span class="truncate">{{ site.domain }}</span>
            <span class="truncate text-emerald-600/90 dark:text-emerald-400/90">{{ site.url }}</span>
          </a>
        </div>
        <p v-if="step.durationText" class="mt-1 text-[11px] text-slate-400 dark:text-slate-500">耗时 {{ step.durationText }}</p>
        <p v-if="step.detail" class="mt-0.5 whitespace-pre-wrap break-words text-xs leading-5 text-slate-500 dark:text-slate-400">{{ step.detail }}</p>
        <div v-if="step.path" class="mt-1 border-l border-stone-300 pl-3 text-xs dark:border-stone-700">
          <span class="inline-block rounded bg-stone-200/70 px-2 py-0.5 text-stone-700 dark:bg-stone-800 dark:text-stone-300">
            {{ step.path }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
