<script setup lang="ts">
import { computed, toRefs } from 'vue';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import katex from 'katex';
import { useSmoothStreamText } from '@/composables/useSmoothStreamText';

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

const { content, isStreaming, smooth } = toRefs(props);
const { displayText } = useSmoothStreamText(
  () => content.value || '',
  () => Boolean(isStreaming.value),
  () => smooth.value !== false,
);

const renderMarkdown = (text: string) => {
  if (!text) return '';
  
  let processed = text;

  /**
   * 将单编码的 MinIO 路径段升级为双编码。
   * MinIO key 中若包含字面 % 字符（如 %E9%AB%98 目录名），HTTP URL 里的 %
   * 必须再编码为 %25，否则 MinIO HTTP 服务解码一次后找不到对象 key。
   *
   * 规则（幂等）：
   * - 含 %25XX（已双编码）→ 原样返回
   * - 含 %XX（且无 %25XX）→ 将每个 %XX 替换为 %25XX
   * - 无 %XX → 原样返回
   */
  const ensureMinioDoubleEncoding = (path: string): string => {
    if (/%25[0-9A-Fa-f]{2}/.test(path)) return path;   // 已双编码
    if (!/%[0-9A-Fa-f]{2}/.test(path)) return path;    // 无编码
    return path.replace(/%([0-9A-Fa-f]{2})/g, '%25$1'); // 单→双
  };

  /**
   * 知识库图片特殊情况：当 MinIO 对象键使用中文（UTF-8）字符命名时，
   * 正确的 HTTP URL 应为单重编码（%E6%B1%A1），而非双重编码（%25E6%25B1%25A1）。
   * 若 src 中含有 %25 + 高字节（≥0x80，即 [89A-Fa-f][0-9A-Fa-f]）序列，
   * 且去掉多余 %25 后能通过 decodeURIComponent 验证为合法 UTF-8，
   * 则将双重编码还原为单重编码，避免 MinIO 解码一次后找不到对象键（404）。
   *
   * 不影响的情况：
   *  - 不含 %25 高字节序列的路径（纯 ASCII，直接返回）
   *  - %2520/%252B 等双重编码的 ASCII 低字节（不匹配高字节范围，直接返回）
   *  - 当前已可正常访问的图片路径
   */
  const fixKbImageDoubleEncoding = (src: string): string => {
    if (!/%25[89A-Fa-f][0-9A-Fa-f]/i.test(src)) return src;
    const candidate = src.replace(/%25([89A-Fa-f][0-9A-Fa-f])/gi, '%$1');
    try {
      decodeURIComponent(candidate);
      return candidate;
    } catch {
      return src;
    }
  };

  const normalizeFileResourceUrl = (value: string): string => {
    const raw = String(value || '').trim().replace(/^`+|`+$/g, '');
    if (!raw) return raw;
    if (raw.startsWith('/files/')) return ensureMinioDoubleEncoding(raw);

    try {
      const parsed = new URL(raw);
      if (parsed.pathname.includes('/files/')) {
        const fixedPath = ensureMinioDoubleEncoding(parsed.pathname);
        return `${fixedPath}${parsed.search}${parsed.hash}`;
      }
    } catch {
      return raw;
    }

    return raw;
  };

  const normalizeRawImageHtml = (input: string): string => {
    return input.replace(/<img\b[^>]*\bsrc\s*=\s*(['"])([\s\S]*?)\1[^>]*>/gi, (tag, quote, src) => {
      // 仅标准化可直接访问的 /files/ 路径，不再对其他值做代理包装
      const normalized = normalizeFileResourceUrl(src);
      return tag.replace(`${quote}${src}${quote}`, `${quote}${normalized}${quote}`);
    });
  };

  const promoteStandaloneImageUrls = (input: string): string => {
    const imageTokenPattern = /`?(https?:\/\/[^\s`]+?\.(?:jpg|jpeg|png|gif|bmp|webp|tiff|tif)|\/files\/[^\s`]+?\.(?:jpg|jpeg|png|gif|bmp|webp|tiff|tif))`?/gi;
    const lines = input.split('\n');
    let inFence = false;

    return lines.map((line) => {
      const trimmed = line.trim();
      if (trimmed.startsWith('```')) {
        inFence = !inFence;
        return line;
      }
      if (inFence || !trimmed) return line;
      if (trimmed.includes('![') || trimmed.includes('<img')) return line;

      const matches = [...trimmed.matchAll(imageTokenPattern)];
      if (matches.length === 0) return line;

      const covered = matches.map((m) => m[0]).join(' ').trim();
      if (covered !== trimmed) return line;

      return matches
        .map((m) => `![](<${m[1]}>)`)
        .join('\n\n');
    }).join('\n');
  };

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

  processed = promoteStandaloneImageUrls(processed);
  processed = normalizeRawImageHtml(processed);
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
        strict: false
      });
    } catch (e) {
      console.warn('KaTeX render error:', e);
      return formula;
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
    ADD_ATTR: ['class', 'style', 'aria-hidden', 'viewBox', 'd', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin']
  });

  // 5. Restore Math
  for (const [placeholder, html] of mathMap.entries()) {
    renderedHtml = renderedHtml.split(placeholder).join(html);
  }

  renderedHtml = normalizeRawImageHtml(renderedHtml);

  // 对 marked 生成的 markdown 图片统一做 /files/ 路径标准化
  // （DOMPurify 可能修改 src，在最后一步统一处理）
  renderedHtml = renderedHtml.replace(
    /<img\b([^>]*)>/gi,
    (_tag, attrs) => {
      return _tag.replace(
        /(\bsrc\s*=\s*['"])([^'"]+)(['"])/i,
        (_m, prefix, src, suffix) => {
          const fixed = normalizeFileResourceUrl(src);
          return `${prefix}${fixed}${suffix}`;
        }
      );
    }
  );

  // 知识库图片特殊处理（独立步骤，不修改上面已有逻辑）：
  // 将中文目录名被双重编码（%25E6%25B1%25A1...）的 src 还原为单重编码（%E6%B1%A1...），
  // 使 MinIO HTTP 服务解码一次后能正确匹配以中文字符命名的对象键。
  renderedHtml = renderedHtml.replace(
    /<img\b([^>]*)>/gi,
    (_tag, _attrs) => {
      return _tag.replace(
        /(\bsrc\s*=\s*['"])([^'"]+)(['"])/i,
        (_m, prefix, src, suffix) => `${prefix}${fixKbImageDoubleEncoding(src)}${suffix}`
      );
    }
  );

  try {
    return renderedHtml;
  } catch (e) {
    console.error('Markdown processing error:', e);
    return text;
  }
};

const renderedHtml = computed(() => {
  return renderMarkdown(displayText.value || '');
});
</script>

<template>
  <div class="prose dark:prose-invert max-w-none academic-container font-sans px-3 md:px-6" v-html="renderedHtml"></div>
</template>

<style scoped>
@reference "../../../style.css";

.academic-container {
  @apply leading-[1.95] text-slate-800 dark:text-slate-200 text-[17px] font-sans antialiased max-w-[860px] mx-auto;
}

/* Math blocks styling - Centered Figure style */
.math-block {
  @apply my-6 md:my-10 py-6 md:py-8 px-2 md:px-6 overflow-x-auto overflow-y-hidden block text-[13px] md:text-base text-center bg-slate-50/50 dark:bg-slate-900/30 rounded-2xl md:rounded-3xl border border-slate-100 dark:border-slate-800/80 shadow-sm transition-shadow hover:shadow-md w-[calc(100%+2rem)] -ml-4 md:w-full md:ml-0;
}

:deep(.katex-display) {
  @apply overflow-x-auto overflow-y-hidden py-2;
}

:deep(.katex) {
  @apply text-[1.05em] md:text-[1.15em] whitespace-nowrap;
}

/* Premium Academic Tables */
:deep(table) {
  @apply mx-auto my-6 md:my-12 w-full rounded-none md:rounded-lg text-[12px] md:text-sm shadow-sm md:shadow-xl transition-all duration-500 hover:shadow-md md:hover:shadow-2xl md:hover:-translate-y-[2px];
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
  @apply bg-slate-100/90 dark:bg-slate-800/90 border-b-2 border-slate-200 dark:border-slate-700;
}

:deep(th) {
  @apply px-3 py-3 md:px-6 md:py-5 font-black text-slate-800 dark:text-slate-100 text-center uppercase tracking-wider bg-slate-50/50 dark:bg-slate-900/50;
  border: 1px solid var(--color-slate-200, #e2e8f0) !important;
}

html.dark :deep(th),
.dark :deep(th) {
  border-color: var(--color-slate-700, #334155) !important;
}

:deep(td) {
  @apply px-3 py-3 md:px-6 md:py-5 text-slate-600 dark:text-slate-300 text-center align-middle;
  white-space: normal;
  word-break: break-word;
  border: 1px solid var(--color-slate-200, #e2e8f0) !important;
}

html.dark :deep(td),
.dark :deep(td) {
  border-color: var(--color-slate-800, #1e293b) !important;
}

:deep(tr:nth-child(even)) {
  @apply bg-slate-50/30 dark:bg-slate-800/20;
}

:deep(tr:hover) {
  @apply bg-indigo-50/30 dark:bg-indigo-900/10 transition-colors duration-200;
}

/* Paragraph spacing */
:deep(p) {
  @apply mb-4 mt-2 text-[14px] md:text-[16px] leading-[1.8] md:leading-8 text-slate-700 dark:text-slate-300;
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
  @apply mx-auto block my-6 md:my-8 rounded-lg shadow-sm border border-slate-200 dark:border-slate-800;
}

:deep(p:has(img)), :deep(p.image-caption) {
  text-indent: 0 !important;
  text-align: center !important;
}

:deep(p.image-caption) {
  @apply text-slate-500 dark:text-slate-400 text-sm md:text-base mb-8;
}

/* Headings with premium markers */
:deep(h1) {
  @apply font-black text-3xl text-slate-900 dark:text-white mt-20 mb-10 tracking-tight pb-4 border-b-2 border-indigo-500/20 relative;
}
:deep(h1::after) {
  content: '';
  @apply absolute bottom-[-2px] left-0 w-24 h-0.5 bg-indigo-500;
}

:deep(h2) {
  @apply font-extrabold text-2xl text-slate-900 dark:text-white mt-16 mb-8 tracking-tight flex items-center gap-3;
}
:deep(h2::before) {
  content: '';
  @apply w-1.5 h-6 bg-indigo-400 rounded-full;
}

:deep(h3) {
  @apply font-bold text-xl text-indigo-700 dark:text-indigo-400 mt-12 mb-6 tracking-tight;
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
  @apply border-l-4 border-indigo-500 bg-indigo-50/20 dark:bg-indigo-900/10 px-6 md:px-8 py-5 md:py-6 italic text-slate-600 dark:text-slate-400 mb-8 md:mb-10 rounded-r-2xl font-serif text-[16px] md:text-[18px];
}

/* Think Blocks refinement */
:deep(.think-block) {
  @apply bg-slate-50/50 dark:bg-slate-900/20 border-l-4 border-slate-300 dark:border-slate-700 my-8 rounded-r-xl overflow-hidden shadow-sm;
}
:deep(.think-header) {
  @apply px-4 py-1.5 bg-slate-100/50 dark:bg-slate-800/50 text-slate-400 text-[10px] font-bold uppercase tracking-widest;
}
:deep(.think-content) {
  @apply px-6 py-4 text-xs italic text-slate-500 font-mono leading-7;
}
</style>
