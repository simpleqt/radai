<script setup lang="ts">
// 轻量行内公式渲染：选项、列表摘要等短文本里的 $...$ / $$...$$ 用 KaTeX 渲染，
// 其余按纯文本输出。与 MarkdownRenderer 的数学切分规则一致（行内不跨行），
// 但不套学术正文排版（缩进/居中/最大宽度），适合嵌在选项行、卡片摘要里。
import { computed } from 'vue';
import katex from 'katex';

const props = defineProps<{ text: string }>();

type Segment = { math: boolean; value: string; display?: boolean };

const segments = computed<Segment[]>(() => {
  const raw = String(props.text ?? '');
  const parts: Segment[] = [];
  const regex = /(\$\$[\s\S]+?\$\$|\$[^$\n]+?\$)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(raw)) !== null) {
    if (match.index > last) {
      parts.push({ math: false, value: raw.slice(last, match.index) });
    }
    const token = match[0];
    const display = token.startsWith('$$');
    parts.push({
      math: true,
      display,
      value: display ? token.slice(2, -2) : token.slice(1, -1),
    });
    last = match.index + token.length;
  }
  if (last < raw.length) {
    parts.push({ math: false, value: raw.slice(last) });
  }
  return parts;
});

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function render(formula: string, display: boolean): string {
  try {
    return katex.renderToString(formula, {
      displayMode: display,
      throwOnError: false,
      strict: false,
      trust: false,
    });
  } catch {
    // KaTeX 抛非 ParseError 时原始公式串必须转义：此输出直接进 v-html。
    return escapeHtml(formula);
  }
}
</script>

<template>
  <span class="inline-math">
    <template v-for="(part, index) in segments" :key="index">
      <!-- KaTeX 输出为受控 HTML（trust=false），v-html 仅用于公式片段 -->
      <span v-if="part.math" class="inline-math-formula" v-html="render(part.value, Boolean(part.display))" />
      <template v-else>{{ part.value }}</template>
    </template>
  </span>
</template>

<style scoped>
.inline-math-formula :deep(.katex) {
  font-size: 1.02em;
}
</style>
