import { createRouter, createWebHistory } from 'vue-router'
import { authState } from '@/utils/request'
import AdminLayout from '@/views/admin/components/AdminLayout.vue'

const detectDefaultMode = (): 'mobile' | 'desktop' => {
  const fromStorage = (localStorage.getItem('ui_mode') || 'auto').toLowerCase();
  if (fromStorage === 'mobile') return 'mobile';
  if (fromStorage === 'desktop') return 'desktop';

  // Client-side detection only since we need window
  if (typeof window === 'undefined') return 'desktop';

  const ua = typeof navigator !== 'undefined' ? navigator.userAgent : '';
  const width = window.innerWidth;
  const uaMobile = /android|webos|iphone|ipad|ipod|blackberry|iemobile|opera mini/i.test(ua.toLowerCase());
  return uaMobile || width <= 900 ? 'mobile' : 'desktop';
};

const remapChatPathByMode = (toPath: string, mode: 'mobile' | 'desktop', threadId?: string) => {
  const base = mode === 'mobile' ? '/m' : '/app';
  if (threadId) return `${base}/${threadId}`;
  return base;
};

const routes = [
  { path: '/', redirect: () => (detectDefaultMode() === 'mobile' ? '/m' : '/app') },
  // 探索式学习路由需先于 /app/:threadId? 与 /m/:threadId? 定义，避免被通配的 threadId 抢占匹配。
  {
    path: '/explore',
    component: () => import('@/views/explore/index.vue'),
    meta: { title: '探索式学习 - 成理场地污染水文地质智学助手' }
  },
  {
    path: '/m/explore',
    component: () => import('@/views/explore/index.vue'),
    meta: { title: '探索式学习 - 成理场地污染水文地质智学助手' }
  },
  {
    path: '/groundwater/:threadId?',
    component: () => import('@/views/groundwater/index.vue'),
    meta: {
      title: '地下水项目分析 - 成理场地污染水文地质智学助手',
      requiresAuth: true,
    }
  },
  {
    path: '/app/:threadId?',
    component: () => import('@/views/ne-full/index.vue'),
    meta: { title: '成理场地污染水文地质智学助手' }
  },
  {
    path: '/m/:threadId?',
    component: () => import('@/views/ne-full/index.vue'),
    meta: { title: '成理场地污染水文地质智学助手' }
  },
  {
    path: '/login',
    component: () => import('@/views/login/UserLogin.vue'),
    meta: { title: '用户登录', guest: true }
  },
  {
    path: '/my-files',
    component: () => import('@/views/ne-full/MyFilesView.vue'),
    meta: { title: '个人文件库 - 成理场地污染水文地质智学助手', requiresAuth: true }
  },
  {
    path: '/chat/:threadId?',
    redirect: (to: any) => {
      const mode = detectDefaultMode();
      return { path: remapChatPathByMode(to.path, mode, to.params.threadId as string | undefined) };
    },
  },
  { 
    path: '/ne-full/:threadId?', 
    redirect: (to: any) => {
      const mode = detectDefaultMode();
      return { path: remapChatPathByMode(to.path, mode, to.params.threadId as string | undefined) };
    },
  },
  {
    path: '/admin/login',
    component: () => import('@/views/admin/login.vue'),
    meta: { title: '管理员登录', guest: true }
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
  routes
})

router.beforeEach((to, from, next) => {
  if (to.meta.title) document.title = to.meta.title as string;

  const isChatPath = (path: string) => {
    if (path === '/explore' || path === '/m/explore') return false;
    return path === '/app' || path.startsWith('/app/') || path === '/m' || path.startsWith('/m/');
  };

  if (isChatPath(to.path)) {
    const expectedMode = to.query.mode === 'desktop' ? 'desktop' : to.query.mode === 'mobile' ? 'mobile' : detectDefaultMode();
    const targetPath = remapChatPathByMode(to.path, expectedMode, to.params.threadId as string | undefined);
    
    if (expectedMode === 'mobile' && (to.path === '/app' || to.path.startsWith('/app/'))) {
      return next({ path: targetPath, query: to.query, hash: to.hash, replace: true });
    }
    if (expectedMode === 'desktop' && (to.path === '/m' || to.path.startsWith('/m/'))) {
      return next({ path: targetPath, query: to.query, hash: to.hash, replace: true });
    }
  }

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

    if (!isAdmin && to.path !== '/admin/login') {
      next('/app');
      return;
    }
  }
  
  if (to.meta.requiresAuth && !isAuth) {
    if (to.path.startsWith('/admin')) {
      next('/admin/login');
    } else {
      next('/login');
    }
  } else if (to.meta.guest && isAuth) {
    if (to.path.startsWith('/admin')) {
      next(isAdmin ? '/admin/dashboard' : '/app');
    } else {
      next('/app');
    }
  } else {
    next();
  }
})

export default router
