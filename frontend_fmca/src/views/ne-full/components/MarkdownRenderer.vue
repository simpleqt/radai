<script setup lang="ts">
import { nextTick, onUnmounted, ref, toRefs, watch } from 'vue';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import katex from 'katex';
import { useSmoothStreamText } from '@/composables/useSmoothStreamText';
import { downloadWithAuth } from '@/utils/request';
import { disposeChartHandles, hydrateCharts, type ChartHandle } from './chartHydrator';
import {
  focusCitationReference,
  hydrateCitations,
  parseCitationReferences,
  type CitationHandle,
  type CitationReference,
} from './citationHydrator';

type MermaidBlock = {
  placeholder: string;
  source: string;
};

let mermaidInstancePromise: Promise<(typeof import('mermaid'))['default']> | null = null;
let mermaidDiagramId = 0;

const getMermaidInstance = () => {
  if (!mermaidInstancePromise) {
    mermaidInstancePromise = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        suppressErrorRendering: true,
        theme: 'base',
        fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif',
        flowchart: {
          htmlLabels: true,
          useMaxWidth: true,
        },
        themeVariables: {
          background: '#ffffff',
          primaryColor: '#eef2ff',
          primaryTextColor: '#1e293b',
          primaryBorderColor: '#6366f1',
          secondaryColor: '#ecfeff',
          secondaryTextColor: '#164e63',
          secondaryBorderColor: '#06b6d4',
          tertiaryColor: '#f8fafc',
          tertiaryTextColor: '#334155',
          tertiaryBorderColor: '#94a3b8',
          lineColor: '#64748b',
          textColor: '#1e293b',
        },
      });
      return mermaid;
    });
  }
  return mermaidInstancePromise;
};

const props = withDefaults(
  defineProps<{
    content: string;
    /** 流式回答中（与是否开启思考模式无关，只要 SSE 未结束即为 true） */
    isStreaming?: boolean;
    /** 是否启用平滑放出，默认开启 */
    smooth?: boolean;
  }>(),
  { smooth: true },
);

// 聊天正文里的文件下载链接（/api/v2/files/<id>/download）需要带认证令牌请求：
// 浏览器直接跳转不带 Authorization 头，后端会返回「未提供认证令牌」。
// 这里委托点击，改走 downloadWithAuth（fetch + blob 另存）。
// 仅匹配相对路径：`https?://[^/]+` 放行任意主机会让回答里的外站链接
// （可经知识库内容提示注入）把 Bearer token 发到任意域。
const AUTH_FILE_DOWNLOAD_RE = /^\/api\/v[12]\/files\/[^/?#]+\/download(?:[?#].*)?$/;

/** 点击引用标记后展示的溯源卡片（fixed 定位，锚定标记位置并夹在视口内） */
const POPOVER_WIDTH = 288;
const POPOVER_HEIGHT = 160;
const activeCitation = ref<{ reference: CitationReference; left: number; top: number } | null>(null);
const citationRefs = ref<Map<number, CitationReference>>(new Map());

const closeCitation = () => {
  activeCitation.value = null;
};

const openCitation = (chip: HTMLElement) => {
  const n = Number(chip.getAttribute('data-citation'));
  const reference = citationRefs.value.get(n);
  if (!reference) return;
  const rect = chip.getBoundingClientRect();
  activeCitation.value = {
    reference,
    left: Math.max(12, Math.min(rect.left, window.innerWidth - POPOVER_WIDTH - 12)),
    top: Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - POPOVER_HEIGHT)),
  };
};

const onCitationKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') closeCitation();
};

watch(activeCitation, (current, previous) => {
  if (current && !previous) document.addEventListener('keydown', onCitationKeydown);
  else if (!current && previous) document.removeEventListener('keydown', onCitationKeydown);
});

const onContentClick = (event: MouseEvent) => {
  const target = event.target as HTMLElement | null;

  const chip = target?.closest?.('[data-citation]') as HTMLElement | null;
  if (chip) {
    event.preventDefault();
    openCitation(chip);
    return;
  }

  const anchor = target?.closest?.('a');
  if (!anchor) return;
  const href = anchor.getAttribute('href') || '';
  if (!AUTH_FILE_DOWNLOAD_RE.test(href)) return;
  // 同源断言兜底：即便未来放宽正则，也绝不把认证头发往其它源。
  if (new URL(href, window.location.origin).origin !== window.location.origin) return;
  event.preventDefault();
  const filename = (anchor.textContent || '').trim() || undefined;
  void downloadWithAuth(href, { filename }).catch((error: unknown) => {
    console.error('[MarkdownRenderer] 认证下载失败', error);
    const message = error instanceof Error ? error.message : '下载失败，请稍后重试';
    window.alert(message);
  });
};

const jumpToReference = () => {
  const current = activeCitation.value;
  if (!current || !containerRef.value) return;
  focusCitationReference(containerRef.value, current.reference.n);
  closeCitation();
};

const { content, isStreaming, smooth } = toRefs(props);
const { displayText } = useSmoothStreamText(
  () => content.value || '',
  () => Boolean(isStreaming.value),
  () => smooth.value !== false,
);

const MERMAID_PH = (id: number) => `\uE002MERMAID${id}\uE003`;

const FENCE_ONLY_LINE = /^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([^`~]*)$/;
const GLUED_FENCE_LINE = /^(.*\S)[ \t]*(`{3,}|~{3,})[ \t]*([^`~]*)$/;
const FENCE_INFO_STRING = /^[\w+#.-]*$/;
// 开围栏粘正文且代码内容跟在同一行：「…大纲：```json { "course": … }」
const GLUED_FENCE_WITH_BODY = /^(.*?)(`{3,}|~{3,})[ \t]*([\w+#.-]*)[ \t]+(\S.*)$/;

const closesFence = (marker: string, openFence: string | null, info: string) =>
  Boolean(openFence) &&
  marker[0] === openFence![0] &&
  marker.length >= openFence!.length &&
  !info.trim();

const hasLaterClosingFence = (lines: string[], fromIndex: number, marker: string) =>
  lines.slice(fromIndex).some((line) => {
    const fenceOnly = line.match(FENCE_ONLY_LINE);
    return Boolean(fenceOnly) && closesFence(fenceOnly![1], marker, fenceOnly![2]);
  });

/**
 * 剥除粘在正文里的 JSON 倾倒块（模型违规输出的兜底）。
 *
 * 课程评价等对话中模型偶发把工具数据以「…正文：```json { … }```」的形式粘进
 * 句子里（受同一会话历史消息的示例影响，提示词禁令拦不住）。这类内容对
 * 教师毫无意义且破坏排版：围栏不在行首时 CommonMark 不认，JSON 散在段落里。
 * 规则（只处理**非行首**的围栏 + json/裸 { 开头，行首的合法代码块不受影响）：
 * - JSON 已流完（花括号配平）→ 精确剥除该块与紧随的孤立闭栏；
 * - 还在流式传输（未配平）→ 剥到当前文本末尾，正文保持干净。
 */
const stripGluedJsonDumps = (input: string): string => {
  if (!input.includes('```') && !input.includes('~~~')) return input;
  let text = input;
  for (let guard = 0; guard < 8; guard += 1) {
    const match = text.match(/(\S[ \t]*)(`{3,}|~{3,})[ \t]*(?:json|JSON)?[ \t]*\{/);
    if (!match || match.index === undefined) break;
    const fenceStart = match.index + match[1].length;
    // 行首围栏（前缀只是行内空白）是合法代码块，不剥
    const lineStart = text.lastIndexOf('\n', fenceStart) + 1;
    if (!text.slice(lineStart, fenceStart).trim()) break;
    const braceStart = text.indexOf('{', fenceStart);
    let depth = 0;
    let inString = false;
    let escaped = false;
    let end = -1;
    for (let i = braceStart; i < text.length; i += 1) {
      const ch = text[i];
      if (escaped) { escaped = false; continue; }
      if (ch === '\\') { escaped = true; continue; }
      if (ch === '"') { inString = !inString; continue; }
      if (inString) continue;
      if (ch === '{') depth += 1;
      else if (ch === '}') {
        depth -= 1;
        if (depth === 0) { end = i + 1; break; }
      }
    }
    if (end === -1) {
      // JSON 未配平：还在流式输出，剥到末尾
      text = text.slice(0, fenceStart);
      break;
    }
    // 吃掉紧随其后的闭栏（同行或独立行）
    const tail = text.slice(end).match(/^[ \t]*\r?\n?[ \t]*(?:`{3,}|~{3,})[ \t]*(?=\r?\n|$)/);
    const cutEnd = end + (tail ? tail[0].length : 0);
    text = text.slice(0, fenceStart) + text.slice(cutEnd);
  }
  return text;
};

/**
 * 把与正文粘在同一行的代码围栏拆到独立行。
 *
 * CommonMark 只承认独占一行的围栏，所以「…映射建议。```json」里的开围栏会退化成
 * 普通文字，随后那个本应闭合的围栏反而开启一个代码块，把后面的标题与表格全部吞掉。
 * 只有在后文存在可配对的闭围栏时才提升开围栏，避免把原本正常渲染的内容
 * （模型只写了开围栏、流式尚未输出闭围栏）改成代码块。
 */
const normalizeGluedCodeFences = (input: string): string => {
  if (!input.includes('```') && !input.includes('~~~')) return input;

  const lines = input.split('\n');
  const normalized: string[] = [];
  let openFence: string | null = null;

  lines.forEach((line, index) => {
    const fenceOnly = line.match(FENCE_ONLY_LINE);
    if (fenceOnly) {
      const marker = fenceOnly[1];
      const info = fenceOnly[2];
      if (openFence) {
        if (closesFence(marker, openFence, info)) openFence = null;
        normalized.push(line);
        return;
      }
      // 无信息串的孤立围栏、且后文没有能与之配对的闭栏：这是粘连开栏
      // 留下的「孤儿闭栏」，按原样会把后面的正常 Markdown 全部吞进代码块。
      // 前置零宽空格使其退化为普通文本行（渲染上不可见）。
      if (!info.trim() && !hasLaterClosingFence(lines, index + 1, marker)) {
        normalized.push(`\u200B${line}`);
        return;
      }
      openFence = marker;
      normalized.push(line);
      return;
    }

    if (!openFence) {
      // 形态一：围栏在行尾（「…映射建议。```json」），代码在后续行
      const glued = line.match(GLUED_FENCE_LINE);
      if (glued && FENCE_INFO_STRING.test(glued[3].trim())) {
        const [, prefix, marker, info] = glued;
        if (hasLaterClosingFence(lines, index + 1, marker)) {
          normalized.push(prefix);
          normalized.push(`${marker}${info.trim()}`);
          openFence = marker;
          return;
        }
      }
      // 形态二：围栏与代码内容都粘在同一行（「…大纲：```json { … }」）
      const gluedBody = line.match(GLUED_FENCE_WITH_BODY);
      if (
        gluedBody &&
        !gluedBody[1].includes('`') &&
        !gluedBody[1].includes('~') &&
        hasLaterClosingFence(lines, index + 1, gluedBody[2])
      ) {
        const [, prefix, marker, info, body] = gluedBody;
        if (prefix.trim()) normalized.push(prefix.trimEnd());
        normalized.push(`${marker}${info}`);
        normalized.push(body);
        openFence = marker;
        return;
      }
    } else {
      // 围栏已打开时只识别行尾的粘连闭栏（「…} ] }```」）
      const glued = line.match(GLUED_FENCE_LINE);
      if (glued && closesFence(glued[2], openFence, glued[3])) {
        normalized.push(glued[1]);
        normalized.push(glued[2]);
        openFence = null;
        return;
      }
    }

    normalized.push(line);
  });

  return normalized.join('\n');
};

const extractMermaidBlocks = (input: string): { content: string; blocks: MermaidBlock[] } => {
  const blocks: MermaidBlock[] = [];
  const content = input.replace(
    /(^|\r?\n)[ \t]{0,3}(`{3,}|~{3,})[ \t]*mermaid(?:[ \t][^\r\n]*)?\r?\n([\s\S]*?)\r?\n[ \t]{0,3}\2[ \t]*(?=\r?\n|$)/gi,
    (_match, leading: string, _fence: string, source: string) => {
      const placeholder = MERMAID_PH(blocks.length);
      blocks.push({ placeholder, source: source.trim() });
      return `${leading}${placeholder}`;
    },
  );
  return { content, blocks };
};

const escapeHtml = (value: string) =>
  value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');

const replaceMermaidPlaceholder = (html: string, placeholder: string, replacement: string) => {
  return html
    .split(`<p>${placeholder}</p>`)
    .join(replacement)
    .split(placeholder)
    .join(replacement);
};

const mermaidSourceFallback = (source: string, hasError = false) => `
  <div class="mermaid-fallback${hasError ? ' mermaid-fallback-error' : ''}">
    ${hasError ? '<div class="mermaid-error-title">图表渲染失败，已显示 Mermaid 源码</div>' : '<div class="mermaid-loading-title">正在生成图表…</div>'}
    <pre><code class="language-mermaid">${escapeHtml(source)}</code></pre>
  </div>
`;

const renderMarkdown = (text: string, shouldRenderMermaid: boolean) => {
  if (!text) return { html: '', mermaidBlocks: [] as MermaidBlock[] };
  
  let processed = normalizeGluedCodeFences(stripGluedJsonDumps(text));
  let mermaidBlocks: MermaidBlock[] = [];

  // Mermaid 源码必须先于 KaTeX/Markdown 处理提取，否则图中的 $、<br/>
  // 等语法会被正文渲染器改写。流式阶段保留为代码块，待消息完成后再绘图。
  if (shouldRenderMermaid) {
    const extracted = extractMermaidBlocks(processed);
    processed = extracted.content;
    mermaidBlocks = extracted.blocks;
  }

  // NOTE: All loose HTML <sup>/<sub> normalization has been removed.
  // The markdown parser (marked) and DOMPurify already handle well-formed
  // <sup> / <sub> tags correctly. Previous regex-based normalization was
  // over-aggressive and caused false replacements (e.g. turning "<" into
  // tag openers inside normal text). If malformed OCR-like tags appear,
  // fix them at the data-source (backend) instead of the frontend.

  // Promote standalone Chinese section title lines to markdown headings.
  // This prevents title style regressions when upstream output omits # prefixes.
  const normalizeSectionHeadings = (input: string): string => {
    return input.replace(
      /(^|\n)([ \t]{0,3})(?!#{1,6}\s)([一二三四五六七八九十百千万]+、[^\n]{2,48})(?=\n|$)/g,
      (_m, leading, indent, title) => `${leading}${indent}### ${title.trim()}`
    );
  };

  // Avoid GFM strikethrough parsing when numeric ranges are emitted as
  // "2.96~~2.965" or "2.96~2.965" by normalizing to a range dash.
  const normalizeNumericRangeTilde = (input: string): string => {
    return input.replace(
      /(\d(?:\.\d+)?)\s*~{1,2}\s*(\d(?:\.\d+)?)/g,
      (_m, left, right) => `${left}～${right}`
    );
  };

  const stripUnresolvedImageTokens = (input: string): string => {
    if (
      !input.includes('IMAGE:') &&
      !/%5BIMAGE:/i.test(input) &&
      !/!\[[^\]]*\]\(\s*\)/.test(input)
    ) {
      return input;
    }
    return input
      .replace(/!\[[^\]]*\]\(\s*(?:\[?IMAGE:[^\]\)\n]+\]?|%5BIMAGE:[^)]+?%5D)\s*\)/gi, '')
      .replace(/!\[[^\]]*\]\(\s*\)/g, '')
      .replace(/\[IMAGE:[^\]\n)]+\]/gi, '')
      .replace(/%5BIMAGE:[^)]+?%5D/gi, '');
  };

  processed = stripUnresolvedImageTokens(processed);
  processed = normalizeSectionHeadings(processed);
  processed = normalizeNumericRangeTilde(processed);

  // NOTE: Plain-text superscript normalization (e.g. 10^7 -> <sup>7</sup>)
  // has been removed because it conflicts with legitimate KaTeX markup and
  // well-formed HTML <sup> tags. If the backend needs superscripts, it should
  // emit proper HTML or KaTeX delimiters ($...$ / $$...$$).
  
  // 1. Handle Think blocks
  if (processed.includes('<think>')) {
    processed = processed.replace(/<think>([\s\S]*?)<\/think>/g, 
      '<div class="think-block"><div class="think-header">思索过程</div><div class="think-content">$1</div></div>'
    );
  }

  // 2. Extract and Render Math (Manual Injection Strategy)
  const mathMap = new Map<string, string>();
  let mathCounter = 0;

  const renderMath = (formula: string, displayMode: boolean) => {
    try {
      return katex.renderToString(formula.trim(), {
        displayMode,
        throwOnError: false,
        strict: false,
        trust: false
      });
    } catch (e) {
      console.warn('KaTeX render error:', e);
      // KaTeX 对深层嵌套等输入可能抛非 ParseError；原始公式串必须转义，
      // 否则经 mathMap 在 DOMPurify 之后回填会构成存储型 XSS。
      return escapeHtml(formula);
    }
  };

  // 数学占位符使用 Unicode 私用区字符（\uE000 / \uE001）作为边界，
  // 避免含 [ ] 的占位符破坏 markdown 图片语法 ![alt](url)。
  const MATH_PH = (id: number) => `\uE000MATH${id}\uE001`;

  // Replace Display Math: $$...$$ and \[...\]
  processed = processed.replace(/\$\$([\s\S]*?)\$\$/g, (_m, formula) => {
    const rendered = `<div class="math-block">${renderMath(formula, true)}</div>`;
    const placeholder = MATH_PH(mathCounter++);
    mathMap.set(placeholder, rendered);
    return placeholder;
  });
  processed = processed.replace(/\\\[([\s\S]*?)\\\]/g, (_m, formula) => {
    const rendered = `<div class="math-block">${renderMath(formula, true)}</div>`;
    const placeholder = MATH_PH(mathCounter++);
    mathMap.set(placeholder, rendered);
    return placeholder;
  });

  // Replace Inline Math: $...$ and \(...\)
  processed = processed.replace(/\$([^$\n]+?)\$/g, (_m, formula) => {
    const rendered = renderMath(formula, false);
    const placeholder = MATH_PH(mathCounter++);
    mathMap.set(placeholder, rendered);
    return placeholder;
  });
  processed = processed.replace(/\\\(([\s\S]*?)\\\)/g, (_m, formula) => {
    const rendered = renderMath(formula, false);
    const placeholder = MATH_PH(mathCounter++);
    mathMap.set(placeholder, rendered);
    return placeholder;
  });

  // 3. Render Markdown
  let renderedHtml = marked.parse(processed) as string;
  
  // Tag image/table captions
  renderedHtml = renderedHtml.replace(
    /<p>(<em>|<strong>|<i>|<b>)?((?:图|表)\s*\d+[^<]*?)(<\/em>|<\/strong>|<\/i>|<\/b>)?<\/p>/g,
    '<p class="image-caption">$1$2$3</p>'
  );

  // 4. Sanitize
  // NOTE: 'sup' and 'sub' are explicitly kept in ADD_TAGS so that
  // well-formed HTML superscripts / subscripts survive DOMPurify and are
  // rendered natively by the browser (no extra CSS needed).
  renderedHtml = DOMPurify.sanitize(renderedHtml, {
    ADD_TAGS: [
      'math', 'annotation', 'semantics', 'mrow', 'msub', 'msup', 'mi', 'mn', 'mo', 'mtext', 'mspace', 'msqrt', 'mroot', 'mfrac', 'mfenced', 'mtable', 'mtr', 'mtd', 'maligngroup', 'malignmark', 'ms', 'mglyph', 'mspace', 'svg', 'path', 'sup', 'sub'
    ],
    ADD_ATTR: ['class', 'aria-hidden', 'viewBox', 'd', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin']
  });

  renderedHtml = renderedHtml.replace(/<p>(?:\s|&nbsp;)*<\/p>/g, '');

  // 5. Restore Math
  for (const [placeholder, html] of mathMap.entries()) {
    renderedHtml = renderedHtml.split(placeholder).join(html);
  }

  try {
    return { html: renderedHtml, mermaidBlocks };
  } catch (e) {
    console.error('Markdown processing error:', e);
    return { html: escapeHtml(text), mermaidBlocks: [] as MermaidBlock[] };
  }
};

const renderedHtml = ref('');
let renderVersion = 0;

// ── 交互式图表（ECharts）hydration ──────────────────────────────────
// v-html 每次重渲染都会替换整个 DOM，因此实例句柄按渲染版本管理：
// 渲染前 dispose 旧实例，流式结束后把图表 <img> 原位升级为 ECharts。
const containerRef = ref<HTMLElement | null>(null);
let chartHandles: ChartHandle[] = [];
let chartHydrationVersion = 0;

watch(
  [renderedHtml, () => Boolean(isStreaming.value)],
  async ([, streaming]) => {
    const version = ++chartHydrationVersion;
    disposeChartHandles(chartHandles);
    chartHandles = [];
    if (streaming) return; // 流式期间 <img> 的 PNG 即加载态
    await nextTick();
    if (version !== chartHydrationVersion || !containerRef.value) return;
    const handles = await hydrateCharts(containerRef.value);
    if (version !== chartHydrationVersion) {
      disposeChartHandles(handles);
      return;
    }
    chartHandles = handles;
  },
  { flush: 'post' },
);

// ── 引用溯源（[n] → 参考文档条目）hydration ──────────────────────────
// 编号映射来自回答自身的「参考文档」列表，因此对历史回答同样生效。
let citationHandle: CitationHandle | null = null;
let citationHydrationVersion = 0;

watch(
  [renderedHtml, () => Boolean(isStreaming.value)],
  async ([, streaming]) => {
    const version = ++citationHydrationVersion;
    citationHandle?.dispose();
    citationHandle = null;
    closeCitation();
    if (streaming) return; // 流式期间列表还没写完，编号映射不可靠
    await nextTick();
    if (version !== citationHydrationVersion || !containerRef.value) return;
    const refs = parseCitationReferences(content.value || '');
    citationRefs.value = refs;
    citationHandle = hydrateCitations(containerRef.value, refs);
  },
  { flush: 'post' },
);

onUnmounted(() => {
  disposeChartHandles(chartHandles);
  chartHandles = [];
  citationHandle?.dispose();
  citationHandle = null;
  document.removeEventListener('keydown', onCitationKeydown);
});

watch(
  [displayText, () => Boolean(isStreaming.value)],
  async ([text, streaming]) => {
    const currentVersion = ++renderVersion;
    const { html, mermaidBlocks } = renderMarkdown(text || '', !streaming);

    if (!mermaidBlocks.length) {
      renderedHtml.value = html;
      return;
    }

    let nextHtml = html;
    for (const block of mermaidBlocks) {
      nextHtml = replaceMermaidPlaceholder(nextHtml, block.placeholder, mermaidSourceFallback(block.source));
    }
    renderedHtml.value = nextHtml;

    try {
      const mermaid = await getMermaidInstance();
      const renderedDiagrams = await Promise.all(
        mermaidBlocks.map(async (block) => {
          try {
            const diagramId = `mermaid-diagram-${Date.now()}-${mermaidDiagramId++}`;
            const { svg } = await mermaid.render(diagramId, block.source);
            const safeSvg = DOMPurify.sanitize(svg, {
              ADD_TAGS: ['foreignObject'],
              ADD_ATTR: ['xmlns', 'role', 'aria-roledescription'],
              HTML_INTEGRATION_POINTS: { foreignobject: true },
            });
            return `<div class="mermaid-diagram">${safeSvg}</div>`;
          } catch (error) {
            console.warn('Mermaid render error:', error);
            return mermaidSourceFallback(block.source, true);
          }
        }),
      );

      if (currentVersion !== renderVersion) return;
      nextHtml = html;
      mermaidBlocks.forEach((block, index) => {
        nextHtml = replaceMermaidPlaceholder(nextHtml, block.placeholder, renderedDiagrams[index]);
      });
      renderedHtml.value = nextHtml;
    } catch (error) {
      console.warn('Mermaid load error:', error);
      if (currentVersion !== renderVersion) return;
      nextHtml = html;
      for (const block of mermaidBlocks) {
        nextHtml = replaceMermaidPlaceholder(nextHtml, block.placeholder, mermaidSourceFallback(block.source, true));
      }
      renderedHtml.value = nextHtml;
    }
  },
  { immediate: true },
);
</script>

<template>
  <div
    ref="containerRef"
    class="prose dark:prose-invert max-w-none academic-container font-sans px-3 md:px-6"
    v-html="renderedHtml"
    @click="onContentClick"
  ></div>

  <!-- 引用溯源卡片：点击正文 [n] 后显示该编号对应的来源 -->
  <Teleport to="body">
    <div
      v-if="activeCitation"
      class="fixed inset-0 z-70"
      @click="closeCitation"
      @wheel="closeCitation"
    >
      <div
        class="citation-popover absolute w-72 max-w-[min(18rem,calc(100vw-1.5rem))] rounded-xl border border-border-subtle bg-surface p-3 shadow-lg"
        :style="{ left: `${activeCitation.left}px`, top: `${activeCitation.top}px` }"
        role="dialog"
        :aria-label="`引用 ${activeCitation.reference.n} 来源`"
        @click.stop
      >
        <p class="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-text-tertiary">
          引用 [{{ activeCitation.reference.n }}] 来源
        </p>
        <p class="mb-2.5 text-sm leading-relaxed text-text-primary">
          {{ activeCitation.reference.label }}
        </p>
        <div class="flex items-center justify-end gap-2">
          <button
            type="button"
            class="rounded-md px-2.5 py-1 text-xs text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-secondary"
            @click="closeCitation"
          >
            关闭
          </button>
          <button
            type="button"
            class="rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-accent-fg transition-opacity hover:opacity-90"
            @click="jumpToReference"
          >
            定位到参考文档
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
@reference "../../../style.css";

.academic-container {
  @apply leading-[1.95] text-text-primary text-[17px] font-sans antialiased max-w-[860px] mx-auto;
}

/* Math blocks styling - Centered Figure style */
.math-block {
  @apply my-6 md:my-10 py-6 md:py-8 px-2 md:px-6 overflow-x-auto overflow-y-hidden block text-[13px] md:text-base text-center bg-surface-sunken rounded-2xl md:rounded-3xl border border-border-subtle shadow-sm transition-shadow hover:shadow-sm w-[calc(100%+2rem)] -ml-4 md:w-full md:ml-0;
}

:deep(.katex-display) {
  @apply overflow-x-auto overflow-y-hidden py-2;
}

:deep(.katex) {
  @apply text-[1.05em] md:text-[1.15em] whitespace-nowrap;
}

/* 交互式图表（ECharts）容器：由 chartHydrator 插入到图表 <img> 之后 */
:deep(.chart-echarts) {
  @apply my-6 md:my-8 w-full rounded-2xl border border-border-subtle bg-surface px-2 py-3 shadow-sm;
  text-indent: 0 !important;
}

/* 被交互图表替换的 PNG：屏幕上隐藏，仅作打印/导出资产 */
:deep(img.chart-print-fallback) {
  display: none !important;
}

/* 引用标记：由 citationHydrator 把正文 [n] 替换为可点击标记 */
:deep(.citation-chip) {
  @apply mx-0.5 inline-flex cursor-pointer items-center justify-center rounded border border-accent/30 bg-accent-soft px-1 align-baseline text-[0.78em] font-semibold leading-tight text-accent transition-colors;
  text-indent: 0;
}

:deep(.citation-chip:hover) {
  @apply border-accent/60 bg-accent text-accent-fg;
}

/* 被定位的「参考文档」条目短暂高亮 */
:deep([data-citation-ref].citation-ref-flash) {
  @apply -mx-1 rounded bg-accent-soft px-1;
  transition: background-color 0.4s ease;
}

@media print {
  :deep(.chart-echarts) {
    display: none !important;
  }
  :deep(img.chart-print-fallback) {
    display: block !important;
    margin: 16px auto;
    max-width: 100%;
  }
  /* 导出的 PDF 里引用回落为纯文本 [n]，不带按钮外观 */
  :deep(.citation-chip) {
    border: 0 !important;
    background: none !important;
    color: inherit !important;
    padding: 0 !important;
  }
}

/* Mermaid diagrams */
:deep(.mermaid-diagram) {
  @apply my-6 md:my-10 overflow-x-auto rounded-2xl border border-border-subtle bg-surface px-3 py-5 shadow-sm md:px-6 md:py-8;
  text-indent: 0 !important;
}

:deep(.mermaid-diagram svg) {
  display: block;
  width: auto;
  max-width: 100%;
  height: auto;
  margin: 0 auto;
}

/* Mermaid 在 body 下测量节点尺寸，插入 Markdown 容器后不能继承正文的
   段落行高、缩进和颜色，否则 foreignObject 内的多行标签会溢出节点。 */
:deep(.mermaid-diagram foreignObject),
:deep(.mermaid-diagram foreignObject div),
:deep(.mermaid-diagram foreignObject span),
:deep(.mermaid-diagram foreignObject p) {
  line-height: 1.5 !important;
}

:deep(.mermaid-diagram foreignObject p) {
  margin: 0 !important;
  padding: 0 !important;
  color: inherit !important;
  text-align: center !important;
  text-indent: 0 !important;
  text-justify: auto !important;
}

:deep(.mermaid-fallback) {
  @apply my-6 md:my-10 overflow-hidden rounded-2xl border border-border-subtle;
  text-indent: 0 !important;
}

:deep(.mermaid-loading-title),
:deep(.mermaid-error-title) {
  @apply bg-surface-sunken px-4 py-2 text-xs font-semibold text-text-secondary;
}

:deep(.mermaid-error-title) {
  @apply bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300;
}

:deep(.mermaid-fallback pre) {
  @apply m-0 rounded-none;
}

/* Premium Academic Tables */
:deep(table) {
  @apply mx-auto my-6 md:my-12 w-full rounded-none md:rounded-lg text-[12px] md:text-sm shadow-sm md:shadow-sm transition-all duration-500 hover:shadow-sm md:hover:shadow-sm md:hover:-translate-y-[2px];
  display: table !important;
  table-layout: fixed;
  border-collapse: collapse !important;
  border: 1px solid var(--color-slate-200, #e2e8f0) !important;
  border-left: none !important;
  border-right: none !important;
}

html.dark :deep(table),
.dark :deep(table) {
  border-color: var(--color-slate-800, #1e293b) !important;
}

:deep(thead) {
  @apply bg-surface-sunken border-b-2 border-border-subtle;
}

:deep(th) {
  @apply px-3 py-3 md:px-6 md:py-5 font-black text-text-primary text-center uppercase tracking-wider bg-surface-sunken;
  border: 1px solid var(--color-slate-200, #e2e8f0) !important;
}

html.dark :deep(th),
.dark :deep(th) {
  border-color: var(--color-slate-700, #334155) !important;
}

:deep(td) {
  @apply px-3 py-3 md:px-6 md:py-5 text-text-secondary text-center align-middle;
  white-space: normal;
  word-break: break-word;
  border: 1px solid var(--color-slate-200, #e2e8f0) !important;
}

html.dark :deep(td),
.dark :deep(td) {
  border-color: var(--color-slate-800, #1e293b) !important;
}

:deep(tr:nth-child(even)) {
  @apply bg-surface-sunken;
}

:deep(tr:hover) {
  @apply bg-accent-soft transition-colors duration-200;
}

/* Paragraph spacing */
:deep(p) {
  @apply mb-4 mt-2 text-[14px] md:text-[16px] leading-[1.8] md:leading-8 text-text-secondary;
  text-indent: 1.5em !important;
  text-align: justify;
  text-justify: inter-ideograph;
}

/* Increase specificity so typography defaults do not override indentation */
.academic-container :deep(p) {
  text-indent: 2em !important;
}

/* Avoid extra indentation for content that should remain flush */
:deep(li p),
:deep(blockquote p),
:deep(table p) {
  text-indent: 0 !important;
}

/* Images and Image Captions */
:deep(img) {
  @apply mx-auto block my-6 md:my-8 rounded-lg shadow-sm border border-border-subtle;
}

:deep(p:has(img)), :deep(p.image-caption) {
  text-indent: 0 !important;
  text-align: center !important;
}

:deep(p.image-caption) {
  @apply text-text-secondary text-sm md:text-base mb-8;
}

/* Headings with premium markers */
:deep(h1) {
  @apply font-black text-3xl text-text-primary dark:text-white mt-20 mb-10 tracking-tight pb-4 border-b-2 border-accent/20 relative;
}
:deep(h1::after) {
  content: '';
  @apply absolute bottom-[-2px] left-0 w-24 h-0.5 bg-accent;
}

:deep(h2) {
  @apply font-extrabold text-2xl text-text-primary dark:text-white mt-16 mb-8 tracking-tight flex items-center gap-3;
}
:deep(h2::before) {
  content: '';
  @apply w-1.5 h-6 bg-accent rounded-full;
}

:deep(h3) {
  @apply font-bold text-xl text-accent mt-12 mb-6 tracking-tight;
}

/* Lists with more breathing room */
:deep(ul), :deep(ol) {
  @apply mb-4 space-y-2 pl-5 text-[14px] md:text-[16px];
}
:deep(li) {
  @apply pl-1 leading-[1.8] md:leading-relaxed;
}

/* Blockquotes as specialized callouts */
:deep(blockquote) {
  @apply border-l-4 border-accent bg-accent-soft px-6 md:px-8 py-5 md:py-6 italic text-text-secondary mb-8 md:mb-10 rounded-r-2xl font-serif text-[16px] md:text-[18px];
}

/* Think Blocks refinement */
:deep(.think-block) {
  @apply bg-surface-sunken border-l-4 border-border-subtle my-8 rounded-r-xl overflow-hidden shadow-sm;
}
:deep(.think-header) {
  @apply px-4 py-1.5 bg-surface-sunken text-text-tertiary text-[10px] font-bold uppercase tracking-widest;
}
:deep(.think-content) {
  @apply px-6 py-4 text-xs italic text-text-secondary font-mono leading-7;
}
</style>
