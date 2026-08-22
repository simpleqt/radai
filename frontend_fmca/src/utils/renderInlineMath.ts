import katex from 'katex';
import DOMPurify from 'dompurify';

const INLINE_MATH_RE = /\$([^$\n]+?)\$|\\\(([\s\S]*?)\\\)/g;

const KATEX_SANITIZE_OPTIONS = {
  ADD_TAGS: [
    'math', 'annotation', 'semantics', 'mrow', 'msub', 'msup', 'mi', 'mn', 'mo',
    'mtext', 'mspace', 'msqrt', 'mroot', 'mfrac', 'mfenced', 'mtable', 'mtr', 'mtd',
    'maligngroup', 'malignmark', 'ms', 'mglyph', 'svg', 'path', 'sup', 'sub', 'span',
  ],
  ADD_ATTR: [
    'class', 'style', 'aria-hidden', 'viewBox', 'd', 'fill', 'stroke',
    'stroke-width', 'stroke-linecap', 'stroke-linejoin',
  ],
};

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatSuperscript(html: string): string {
  // 1. Isotope pattern: ^3He -> <sup>3</sup>He
  let result = html.replace(/\^(\d+)([a-zA-Z]+)/g, '<sup>$1</sup>$2');
  // 2. Standard power/superscript pattern: x^2 or 10^6 -> x<sup>2</sup> or 10<sup>6</sup>
  result = result.replace(/\^([0-9a-zA-Z+-]+)/g, '<sup>$1</sup>');
  return result;
}

function renderFormula(formula: string): string {
  try {
    return katex.renderToString(formula.trim(), {
      displayMode: false,
      throwOnError: false,
      strict: false,
    });
  } catch {
    return escapeHtml(`$${formula}$`);
  }
}

/** Render inline LaTeX delimiters ($...$ / \\(...\\)) in plain text as KaTeX HTML. */
export function renderTextWithInlineMath(text: string): string {
  if (!text) return '';

  const parts: string[] = [];
  let lastIndex = 0;

  for (const match of text.matchAll(INLINE_MATH_RE)) {
    const index = match.index ?? 0;
    if (index > lastIndex) {
      parts.push(formatSuperscript(escapeHtml(text.slice(lastIndex, index))));
    }
    const formula = match[1] ?? match[2] ?? '';
    parts.push(renderFormula(formula));
    lastIndex = index + match[0].length;
  }

  if (lastIndex < text.length) {
    parts.push(formatSuperscript(escapeHtml(text.slice(lastIndex))));
  }

  return DOMPurify.sanitize(parts.join(''), KATEX_SANITIZE_OPTIONS);
}
