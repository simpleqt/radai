import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { authState } from '@/utils/request'

const APP_NAME = '高校审计智能助手'

const normalizeSafeRedirect = (value: unknown): string | null => {
  if (typeof value !== 'string') return null
  if (!value.startsWith('/') || value.startsWith('//')) return null
  return value.startsWith('/audit') ? value : null
}

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/audit' },
  {
    path: '/audit/:threadId?',
    component: () => import('@/views/audit/index.vue'),
    meta: { title: APP_NAME },
  },
  {
    path: '/login',
    component: () => import('@/views/login/UserLogin.vue'),
    meta: { title: '用户登录', guest: true },
  },
  { path: '/:pathMatch(.*)*', redirect: '/audit' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition || { top: 0 }
  },
})

router.beforeEach((to, _from, next) => {
  document.title = (to.meta.title as string | undefined) || APP_NAME

  if (to.meta.guest && authState.value.token) {
    next(normalizeSafeRedirect(to.query.redirect) || '/audit')
    return
  }

  next()
})

export default router
