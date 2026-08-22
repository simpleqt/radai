/** 侧栏一级导航配置：集中管理多学习模式入口。 */

export interface NavItem {
  id: string;
  label: string;
  /** lucide-vue-next 图标名 */
  icon: string;
  to: string;
  /** 命中高亮的路径前缀 */
  match: string[];
  /** 是否已上线，未上线显示「敬请期待」并禁用 */
  enabled: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  {
    id: 'qa',
    label: '知识问答',
    icon: 'MessagesSquare',
    to: '/app',
    match: ['/app'],
    enabled: true,
  },
  {
    id: 'explore',
    label: '探索式学习',
    icon: 'Compass',
    to: '/explore',
    match: ['/explore'],
    enabled: true,
  },
  {
    id: 'skills-market',
    label: '专家·技能·连接器',
    icon: 'PlugZap',
    to: '/skills',
    match: ['/skills'],
    enabled: false,
  },
  {
    id: 'course-eval',
    label: '课程评价',
    icon: 'ClipboardCheck',
    to: '/eval/chat',
    match: ['/eval/chat'],
    enabled: true,
  },
];

export function isNavItemActive(item: NavItem, currentPath: string): boolean {
  return item.match.some((m) => currentPath === m || currentPath.startsWith(`${m}/`));
}
