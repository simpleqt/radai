/**
 * 把正文里的引用编号 `[n]` 原位升级为可点击标记，并把末尾「参考文档」列表
 * 的对应条目标记为可定位锚点。
 *
 * - 编号与来源的映射直接来自回答自身的「参考文档」列表（所有提示词 profile
 *   与 writer stage 都强制 `- [n] 文献名[，第 x–y 页]` 这一行格式），因此
 *   历史回答无需任何后端改动即可获得溯源交互。
 * - 只有在「参考文档」里真实存在的编号才会被升级：正文里的 `[1]` 若没有对应
 *   条目就保持纯文本，避免把普通方括号数字误判为引用。
 * - 代码块、链接、公式与「参考文档」列表自身不参与替换。
 * - 与图表 hydration 一样只在流式结束后执行；v-html 重渲染会整体替换 DOM，
 *   调用方在重渲染前 dispose。
 */

export type CitationReference = {
  n: number;
  /** `[n] ` 之后的完整条目文本，例如「《地下水质量标准》(GB/T 14848-2017)，第 12 页」 */
  label: string;
};

export type CitationHandle = {
  dispose: () => void;
};

const REFERENCE_HEADING_RE = /^参考文档[：:]?\s*$/;
const REFERENCE_ENTRY_RE = /^[-*\s]*\[(\d{1,3})\]\s*(.+)$/;
const CITATION_IN_TEXT_RE = /\[(\d{1,3})\]/g;
const SKIP_TAGS = new Set(['A', 'CODE', 'PRE', 'SVG', 'BUTTON', 'SCRIPT', 'STYLE']);
const MAX_REFERENCES = 200;

/**
 * 从回答 Markdown 原文解析「参考文档」列表。
 *
 * 取最后一个「参考文档」标记之后的条目：模型偶尔会在正文里提到这个词，
 * 真正的列表总在末尾。
 */
export const parseCitationReferences = (markdown: string): Map<number, CitationReference> => {
  const refs = new Map<number, CitationReference>();
  const text = String(markdown || '');
  if (!text.includes('参考文档')) return refs;

  const lines = text.split('\n');
  let start = -1;
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    // 允许「## 参考文档」「**参考文档：**」等包装
    const bare = lines[i].replace(/[#*>\s]/g, '');
    if (REFERENCE_HEADING_RE.test(bare) || bare === '参考文档') {
      start = i + 1;
      break;
    }
  }
  if (start < 0) return refs;

  for (let i = start; i < lines.length && refs.size < MAX_REFERENCES; i += 1) {
    const line = lines[i].trim();
    if (!line) continue;
    const match = line.match(REFERENCE_ENTRY_RE);
    if (!match) {
      // 列表结束（出现非条目正文）后不再继续吸收
      if (refs.size > 0) break;
      continue;
    }
    const n = Number(match[1]);
    const label = match[2].trim().replace(/\s*[，,、;；]\s*$/, '');
    if (!Number.isFinite(n) || n <= 0 || !label) continue;
    if (!refs.has(n)) refs.set(n, { n, label });
  }

  return refs;
};

/** 该文本节点是否位于不应替换的容器内。 */
const isSkippable = (node: Text, root: HTMLElement): boolean => {
  let current = node.parentElement;
  while (current && current !== root) {
    if (SKIP_TAGS.has(current.tagName)) return true;
    if (current.classList.contains('katex')) return true;
    if (current.classList.contains('math-block')) return true;
    if (current.hasAttribute('data-citation-list')) return true;
    current = current.parentElement;
  }
  return false;
};

/**
 * 标记「参考文档」列表：给标题与列表项加锚点属性，供正文标记定位。
 * 返回被标记的列表元素，供 `isSkippable` 排除。
 */
const markReferenceList = (root: HTMLElement, refs: Map<number, CitationReference>) => {
  const headings = Array.from(
    root.querySelectorAll<HTMLElement>('p, h1, h2, h3, h4, h5, h6, strong'),
  ).filter((el) => {
    const bare = (el.textContent || '').replace(/[#*>\s]/g, '');
    return bare === '参考文档' || REFERENCE_HEADING_RE.test(bare);
  });
  const heading = headings[headings.length - 1];
  if (!heading) return;

  // 标题可能被包在 <p><strong>…</strong></p> 里，列表挂在最外层块的后面
  let block: HTMLElement = heading;
  while (block.parentElement && block.parentElement !== root && block.parentElement.children.length === 1) {
    block = block.parentElement;
  }
  const list = block.nextElementSibling;
  if (!list || !(list instanceof HTMLElement)) return;
  if (list.tagName !== 'UL' && list.tagName !== 'OL') return;

  list.setAttribute('data-citation-list', '1');
  Array.from(list.children).forEach((item) => {
    if (!(item instanceof HTMLElement)) return;
    const match = (item.textContent || '').trim().match(REFERENCE_ENTRY_RE);
    if (!match) return;
    const n = Number(match[1]);
    if (refs.has(n)) item.setAttribute('data-citation-ref', String(n));
  });
};

/** 收集需要替换的文本节点（先收集后改，避免遍历中改动 DOM）。 */
const collectTextNodes = (root: HTMLElement): Text[] => {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  let current = walker.nextNode();
  while (current) {
    const node = current as Text;
    if (node.nodeValue && node.nodeValue.includes('[') && !isSkippable(node, root)) {
      nodes.push(node);
    }
    current = walker.nextNode();
  }
  return nodes;
};

/**
 * 扫描容器内正文的 `[n]` 并升级为可点击标记。
 * 无「参考文档」列表时不做任何改动。
 */
export const hydrateCitations = (
  root: HTMLElement,
  refs: Map<number, CitationReference>,
): CitationHandle | null => {
  if (!refs.size) return null;

  markReferenceList(root, refs);

  const inserted: HTMLElement[] = [];
  for (const node of collectTextNodes(root)) {
    const source = node.nodeValue || '';
    CITATION_IN_TEXT_RE.lastIndex = 0;
    let match = CITATION_IN_TEXT_RE.exec(source);
    if (!match) continue;

    const fragment = document.createDocumentFragment();
    let cursor = 0;
    let replaced = false;
    while (match) {
      const n = Number(match[1]);
      const reference = refs.get(n);
      // 紧跟标识符字符的 `[n]` 是数组下标（`arr[1]`）而不是引用；引用标记前面
      // 总是中文、空格或标点。
      const previous = match.index > 0 ? source[match.index - 1] : '';
      const looksLikeIndexAccess = /[A-Za-z0-9_\])]/.test(previous);
      if (reference && !looksLikeIndexAccess) {
        if (match.index > cursor) {
          fragment.appendChild(document.createTextNode(source.slice(cursor, match.index)));
        }
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'citation-chip';
        chip.setAttribute('data-citation', String(n));
        chip.setAttribute('title', reference.label);
        chip.setAttribute('aria-label', `引用 ${n}：${reference.label}`);
        // 保留方括号：用户手动选中复制正文时引用格式不能丢
        chip.textContent = `[${n}]`;
        fragment.appendChild(chip);
        inserted.push(chip);
        cursor = match.index + match[0].length;
        replaced = true;
      }
      match = CITATION_IN_TEXT_RE.exec(source);
    }

    if (!replaced) continue;
    if (cursor < source.length) {
      fragment.appendChild(document.createTextNode(source.slice(cursor)));
    }
    node.parentNode?.replaceChild(fragment, node);
  }

  if (!inserted.length) return null;

  return {
    dispose: () => {
      // 正常路径下 v-html 已整体替换 DOM，这里只在同一 DOM 上重复 hydration
      // 时把标记还原成纯文本，避免嵌套包裹。
      inserted.forEach((chip) => {
        if (!chip.isConnected) return;
        chip.replaceWith(document.createTextNode(chip.textContent || ''));
      });
    },
  };
};

/** 滚动到「参考文档」中的对应条目并短暂高亮。 */
export const focusCitationReference = (root: HTMLElement, n: number): boolean => {
  const target = root.querySelector<HTMLElement>(`[data-citation-ref="${n}"]`);
  if (!target) return false;
  target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  target.classList.add('citation-ref-flash');
  window.setTimeout(() => target.classList.remove('citation-ref-flash'), 1600);
  return true;
};
