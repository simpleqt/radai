/**
 * 兼容路径：老会话只有 statusHistory 字符串，没有结构化过程事件。
 * 这里把状态串收敛成同一套步骤模型，避免面板出现第二种渲染分支。
 */

import { cleanProcessText } from './humanize';
import type { IconKey, NormalizedStep } from './types';

interface StatusRule {
  match: (text: string, lower: string) => boolean;
  label?: (text: string) => string;
  icon: IconKey;
}

const RULES: StatusRule[] = [
  { match: (_t, lower) => lower.includes('web_search'), label: () => '在网络上搜索', icon: 'search' },
  { match: (t) => t.includes('在网络上搜索'), icon: 'search' },
  { match: (t, lower) => lower.includes('multi_kb_search') || t.includes('知识库'), label: () => '在知识库中搜索', icon: 'search' },
  { match: (t) => t.includes('委派') || t.includes('子智能体'), icon: 'task' },
  { match: (t) => t.includes('思考') || t.includes('分析'), icon: 'think' },
  { match: (t) => t.includes('/mnt/skills/'), label: () => '加载深度研究技能', icon: 'note' },
  { match: (t) => t.includes('save_deep_research_report') || t.includes('研究报告'), label: () => '创建深度研究报告', icon: 'note' },
  { match: (t, lower) => t.includes('读取') || lower.includes('read_file'), icon: 'file' },
  { match: (t) => t.includes('列出目录') || t.includes('查看目录'), icon: 'folder' },
  { match: (t, lower) => lower.includes('bash') || t.includes('终端'), icon: 'tool' },
  { match: (t, lower) => t.includes('网页') || lower.includes('web_fetch'), icon: 'web' },
];

const extractPath = (value: string): string | undefined => value.match(/\/mnt\/[\w\-./]+/)?.[0];

export function statusHistoryToSteps(history: string[] | undefined, messageId: string): NormalizedStep[] {
  return (history || [])
    .map((raw, index): NormalizedStep | null => {
      const text = cleanProcessText(raw);
      if (!text) return null;

      const lower = text.toLowerCase();
      const rule = RULES.find((candidate) => candidate.match(text, lower));
      const label = rule?.label ? rule.label(text) : text;

      return {
        id: `${messageId}-status-${index}`,
        index,
        kind: 'status',
        groupId: 'orchestrator',
        label,
        statusText: label === text ? undefined : text,
        chips: [],
        view: 'text' as const,
        data: {},
        tone: 'default' as const,
        icon: rule?.icon || 'tool',
        path: extractPath(text),
      };
    })
    .filter((step): step is NormalizedStep => step !== null);
}
