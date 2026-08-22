import { createRouter, createWebHistory, type RouteLocationGeneric, type RouteRecordRaw } from 'vue-router'
import { authState } from '@/utils/request'
import { APP_NAME } from '@/app/branding'
import AdminLayout from '@/views/admin/components/AdminLayout.vue'
import TeacherLayout from '@/views/teacher/TeacherLayout.vue'

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
  // 各功能页需先于 /app/:threadId? 定义，避免被通配的 threadId 抢占匹配。
  {
    path: '/practice',
    component: () => import('@/views/practice/index.vue'),
    meta: { title: `习题练习 - ${APP_NAME}`, requiresAuth: true }
  },
  {
    // 需先于 /practice/:id 定义，避免被动态 id 抢占。
    path: '/practice/wrong',
    component: () => import('@/views/practice/wrong.vue'),
    meta: { title: `错题本 - ${APP_NAME}`, requiresAuth: true }
  },
  {
    path: '/practice/:id',
    component: () => import('@/views/practice/question.vue'),
    meta: { title: `做题 - ${APP_NAME}`, requiresAuth: true }
  },
  {
    path: '/explore',
    component: () => import('@/views/explore/index.vue'),
    meta: { title: `探索式学习 - ${APP_NAME}` }
  },
  {
    path: '/custom-learning/path/:planId?',
    component: () => import('@/views/custom-learning/path.vue'),
    meta: { title: `学习路径 - ${APP_NAME}` }
  },
  {
    path: '/custom-learning',
    component: () => import('@/views/custom-learning/index.vue'),
    meta: { title: `自定义学习 - ${APP_NAME}` }
  },
  {
    path: '/learning-style',
    component: () => import('@/views/learning-style/index.vue'),
    meta: { title: `学习风格测评 - ${APP_NAME}` }
  },
  {
    path: '/settings',
    component: () => import('@/views/settings/index.vue'),
    meta: { title: `用户设置 - ${APP_NAME}`, requiresAuth: true }
  },
  {
    path: '/eval/chat/:threadId?',
    component: () => import('@/views/course-eval/Chat.vue'),
    meta: { title: `课程评价智能对话 - ${APP_NAME}`, requiresAuth: true },
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
  { path: '/m/custom-learning/path/:planId?', redirect: (to) => ({ path: `/custom-learning/path/${to.params.planId || ''}`.replace(/\/$/, '') }) },
  { path: '/m/custom-learning', redirect: '/custom-learning' },
  { path: '/m/learning-style', redirect: '/learning-style' },
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
    meta: { requiresAuth: true, requiresAdmin: true },
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
        path: 'kb/:id',
        component: () => import('@/views/admin/kb-detail.vue'),
        meta: { title: '知识库详情' }
      }
    ]
  },
  {
    path: '/teacher',
    component: TeacherLayout,
    redirect: '/teacher/dashboard',
    meta: { requiresAuth: true, requiresTeacher: true },
    children: [
      {
        path: 'dashboard',
        component: () => import('@/views/teacher/dashboard.vue'),
        meta: { title: `教学分析 · 学情总览 - ${APP_NAME}` }
      },
      {
        path: 'questions',
        component: () => import('@/views/teacher/questions.vue'),
        meta: { title: `教学分析 · 提问列表 - ${APP_NAME}` }
      },
      {
        path: 'reports',
        component: () => import('@/views/teacher/reports.vue'),
        meta: { title: `教学分析 · 分析报告 - ${APP_NAME}` }
      },
      {
        path: 'reports/:id',
        component: () => import('@/views/teacher/report-detail.vue'),
        meta: { title: `教学分析 · 报告详情 - ${APP_NAME}` }
      },
      {
        path: 'bank',
        component: () => import('@/views/teacher/bank/index.vue'),
        meta: { title: `教学分析 · 题库管理 - ${APP_NAME}` }
      },
      {
        // 需先于 bank/:id 定义。
        path: 'bank/upload',
        component: () => import('@/views/teacher/bank/upload.vue'),
        meta: { title: `教学分析 · 批量上传试题 - ${APP_NAME}` }
      },
      {
        path: 'bank/knowledge-points',
        component: () => import('@/views/teacher/bank/knowledge-points.vue'),
        meta: { title: `教学分析 · 知识点字典 - ${APP_NAME}` }
      },
      {
        path: 'bank/new',
        component: () => import('@/views/teacher/bank/detail.vue'),
        meta: { title: `教学分析 · 手工录题 - ${APP_NAME}` }
      },
      {
        path: 'bank/:id',
        component: () => import('@/views/teacher/bank/detail.vue'),
        meta: { title: `教学分析 · 题目编辑 - ${APP_NAME}` }
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
  const role = String(authState.value.user?.role || '').toLowerCase();
  const isTeacher = role === 'teacher' || role === 'admin';

  // 管理员登录页始终可访问，避免已登录普通用户无法切换到管理员账号。
  if (to.path === '/admin/login') {
    next();
    return;
  }

  const requiresAdmin = to.matched.some((record) => record.meta.requiresAdmin) || to.path.startsWith('/admin');
  const requiresTeacher = to.matched.some((record) => record.meta.requiresTeacher) || to.path.startsWith('/teacher');

  if (requiresAdmin) {
    if (!isAuth) {
      next('/admin/login');
      return;
    }
    if (!isAdmin) {
      next('/app');
      return;
    }
  }

  if (requiresTeacher) {
    if (!isAuth) {
      next({ path: '/login', query: { redirect: to.fullPath } });
      return;
    }
    if (!isTeacher) {
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
