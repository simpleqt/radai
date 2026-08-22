import { createRouter, createWebHistory, type RouteLocationGeneric, type RouteRecordRaw } from 'vue-router'
import { authState } from '@/utils/request'
import AdminLayout from '@/views/admin/components/AdminLayout.vue'

const APP_NAME = '成理场地污染水文地质智学助手'

const normalizeSafeRedirect = (value: unknown): string | null => {
  if (typeof value !== 'string') return null;
  if (!value.startsWith('/') || value.startsWith('//')) return null;
  return value;
};

/** 旧的移动端专用路径统一并入 /app，布局改由容器宽度决定 */
const toAppPath = (to: RouteLocationGeneric) => {
  const threadId = to.params.threadId as string | undefined;
  return { path: threadId ? `/app/${threadId}` : '/app', query: to.query, hash: to.hash };
};

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/app' },
  // 探索式学习路由需先于 /app/:threadId? 定义，避免被通配的 threadId 抢占匹配。
  {
    path: '/explore',
    component: () => import('@/views/explore/index.vue'),
    meta: { title: `探索式学习 - ${APP_NAME}` }
  },
  {
    path: '/groundwater/:threadId?',
    component: () => import('@/views/groundwater/index.vue'),
    meta: {
      title: `地下水项目分析 - ${APP_NAME}`,
      requiresAuth: true,
    }
  },
  {
    path: '/settings',
    component: () => import('@/views/settings/index.vue'),
    meta: { title: `用户设置 - ${APP_NAME}`, requiresAuth: true }
  },
  {
    path: '/app/:threadId?',
    component: () => import('@/views/ne-full/index.vue'),
    meta: { title: APP_NAME }
  },
  {
    path: '/login',
    component: () => import('@/views/login/UserLogin.vue'),
    meta: { title: '用户登录', guest: true }
  },
  {
    path: '/my-files',
    component: () => import('@/views/ne-full/MyFilesView.vue'),
    meta: { title: `个人文件库 - ${APP_NAME}`, requiresAuth: true }
  },

  // 兼容旧链接与用户收藏
  { path: '/m/explore', redirect: '/explore' },
  { path: '/m/settings', redirect: '/settings' },
  { path: '/m/:threadId?', redirect: toAppPath },
  { path: '/chat/:threadId?', redirect: toAppPath },
  { path: '/ne-full/:threadId?', redirect: toAppPath },

  {
    path: '/admin/login',
    component: () => import('@/views/admin/login.vue'),
    meta: { title: '智能体管理平台 - 管理员登录', guest: true }
  },
  {
    path: '/admin',
    component: AdminLayout,
    redirect: '/admin/dashboard',
    meta: { requiresAuth: true },
    children: [
      {
        path: 'dashboard',
        component: () => import('@/views/admin/dashboard.vue'),
        meta: { title: '仪表盘' }
      },
      {
        path: 'kbs',
        component: () => import('@/views/admin/knowledge-bases.vue'),
        meta: { title: '知识库管理' }
      },
      {
        path: 'prompts',
        component: () => import('@/views/admin/prompt-management.vue'),
        meta: { title: '提示词管理' }
      },
      {
        path: 'skills',
        component: () => import('@/views/admin/skills.vue'),
        meta: { title: 'Skills 管理' }
      },
      {
        path: 'datasets',
        component: () => import('@/views/admin/datasets.vue'),
        meta: { title: '实验数据管理' }
      },
      {
        path: 'groundwater-projects',
        component: () => import('@/views/admin/groundwater-projects.vue'),
        meta: { title: '地下水项目管理' }
      },
      {
        path: 'kb/:id',
        component: () => import('@/views/admin/kb-detail.vue'),
        meta: { title: '知识库详情' }
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition || { top: 0 };
  }
});

router.beforeEach((to, _from, next) => {
  if (to.meta.title) document.title = to.meta.title as string;

  const isAuth = !!authState.value.token;
  const isAdmin = String(authState.value.user?.role || '').toLowerCase() === 'admin';
  const isAdminLoginPage = to.path === '/admin/login';

  // 管理员登录页始终可访问，避免已登录普通用户无法切换到管理员账号。
  if (isAdminLoginPage) {
    next();
    return;
  }

  if (to.path.startsWith('/admin')) {
    if (!isAuth) {
      next('/admin/login');
      return;
    }

    if (!isAdmin) {
      next('/app');
      return;
    }
  }

  if (to.meta.requiresAuth && !isAuth) {
    if (to.path.startsWith('/admin')) {
      next('/admin/login');
    } else {
      next({ path: '/login', query: { redirect: to.fullPath } });
    }
  } else if (to.meta.guest && isAuth) {
    if (to.path.startsWith('/admin')) {
      next(isAdmin ? '/admin/dashboard' : '/app');
    } else {
      next(normalizeSafeRedirect(to.query.redirect) || '/app');
    }
  } else {
    next();
  }
})

export default router
