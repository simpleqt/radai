/** 一级导航栏（Nav Rail）配置：集中管理多学习模式入口。 */

export interface NavItem {
  id: string;
  label: string;
  /** lucide-vue-next 图标名 */
  icon: string;
  /** 目标路由（桌面）；移动端由各页自行处理 */
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
    match: ['/app', '/m'],
    enabled: true,
  },
  {
    id: 'explore',
    label: '探索式学习',
    icon: 'Compass',
    to: '/explore',
    match: ['/explore', '/m/explore'],
    enabled: true,
  },
  {
    id: 'groundwater',
    label: '地下水项目',
    icon: 'Droplets',
    to: '/groundwater',
    match: ['/groundwater'],
    enabled: true,
  },
];

export function isNavItemActive(item: NavItem, currentPath: string): boolean {
  return item.match.some((m) => {
    if (m === '/app') return currentPath === '/app' || currentPath.startsWith('/app/');
    if (m === '/m') return currentPath === '/m' || (currentPath.startsWith('/m/') && !currentPath.startsWith('/m/explore'));
    return currentPath === m || currentPath.startsWith(`${m}/`);
  });
}
