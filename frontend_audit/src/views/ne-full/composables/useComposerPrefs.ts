/**
 * Composer 偏好持久化（按用户 + 智能体隔离）。
 *
 * 记住用户上次选择的模型 / 知识库 / 联网 / 深检 / 技能，下次进入同一
 * 智能体时自动恢复。仅登录态使用；存储值必须经过 agentInfo 校验后再
 * 应用（模型下线、知识库回收、技能变更都不能把非法值带回请求）。
 * 存储键带用户维度：同一浏览器换账号后不应看到上一个人的预选。
 */
import { authState } from '@/utils/request';

export interface ComposerPrefs {
  model?: string;
  kbIds?: string[];
  searchEnabled?: boolean;
  deepRetrieval?: boolean;
  skills?: string[];
}

const STORAGE_PREFIX = 'ne.composer.prefs.v1';

const ownerScope = (): string => {
  const user = authState.value.user as Record<string, unknown> | null;
  const id = String(user?.user_id || user?.id || user?.username || '').trim();
  return id || 'anon';
};

const storageKey = (agentCode: string) => `${STORAGE_PREFIX}:${ownerScope()}:${agentCode || 'ne'}`;

export const readComposerPrefs = (agentCode: string): ComposerPrefs | null => {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(storageKey(agentCode));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed as ComposerPrefs;
  } catch {
    return null;
  }
};

export const writeComposerPrefs = (agentCode: string, prefs: ComposerPrefs) => {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(storageKey(agentCode), JSON.stringify(prefs));
  } catch {
    // 存储满/隐私模式下静默失败，不影响对话主流程。
  }
};

export const clearComposerPrefs = (agentCode: string) => {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(storageKey(agentCode));
  } catch {
    /* noop */
  }
};
