/**
 * 任务完成提醒：浏览器系统通知 + 页签标题角标。
 *
 * 深度研究等后台任务可运行数十分钟，用户切走标签页后此前毫无感知。
 * 页面可见时不打扰（两条路径都自动跳过）；系统通知仅在用户已授权且
 * 页面隐藏时发出。
 */

let originalTitle: string | null = null;
let badgedTitle: string | null = null;
let titleRestoreListener: (() => void) | null = null;

/**
 * 只在用户自己发起长任务时调用：无用户手势的授权请求会被 Chrome 降级成
 * 静默 UI、被 Firefox 直接 reject，所以恢复已有任务的路径不走这里。
 * default 状态下请求授权，已授权/已拒绝都不再打扰。
 */
export const ensureNotificationPermission = () => {
  if (typeof window === 'undefined' || !('Notification' in window)) return;
  if (Notification.permission !== 'default') return;
  try {
    // 无手势时 Firefox 返回 rejected promise；不接住会变成 unhandled rejection。
    const pending = Notification.requestPermission();
    if (pending && typeof pending.catch === 'function') {
      void pending.catch(() => undefined);
    }
  } catch {
    /* 旧浏览器回调式签名抛错时忽略 */
  }
};

/** 页面隐藏时给标题加角标，回到页面自动恢复。 */
export const flashTitleWhileHidden = (badge = '【已完成】') => {
  if (typeof document === 'undefined' || !document.hidden) return;
  if (originalTitle === null) originalTitle = document.title;
  badgedTitle = `${badge}${originalTitle}`;
  document.title = badgedTitle;
  if (!titleRestoreListener) {
    titleRestoreListener = () => {
      if (document.hidden) return;
      // 期间路由改过标题就不要用旧标题盖回去。
      if (originalTitle !== null && document.title === badgedTitle) {
        document.title = originalTitle;
      }
      originalTitle = null;
      badgedTitle = null;
      if (titleRestoreListener) {
        document.removeEventListener('visibilitychange', titleRestoreListener);
        titleRestoreListener = null;
      }
    };
    document.addEventListener('visibilitychange', titleRestoreListener);
  }
};

/** 后台任务结束（成功或失败）时调用；页面可见时静默。 */
export const notifyTaskFinished = (title: string, body?: string) => {
  flashTitleWhileHidden();
  if (typeof document === 'undefined' || !document.hidden) return;
  if (typeof window === 'undefined' || !('Notification' in window)) return;
  if (Notification.permission !== 'granted') return;
  try {
    const notification = new Notification(title, {
      body: body || '',
      tag: 'agent-background-task',
    });
    notification.onclick = () => {
      try {
        window.focus();
      } catch {
        /* noop */
      }
      notification.close();
    };
  } catch {
    /* 通知失败不影响主流程 */
  }
};
