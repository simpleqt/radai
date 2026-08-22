import { authState, request } from '@/utils/request';

export type FeedbackRating = 'up' | 'down';

export interface FeedbackItem {
  message_key: string;
  rating: FeedbackRating;
  reasons?: string[] | null;
  comment?: string | null;
}

export interface FeedbackSubmitPayload {
  messageKey: string;
  rating: FeedbackRating;
  reasons?: string[];
  comment?: string;
  agentCode?: string;
  modelName?: string;
  questionExcerpt?: string;
  answerExcerpt?: string;
}

export interface FeedbackKeySourceMessage {
  role: string;
  content?: string;
}

// 反馈键不能用消息 id：直播态的 assistant 消息 id 是客户端时间戳，刷新后
// 同一条回答换成服务端分配的 uuid，(user, thread, message_key) 唯一键下会
// 变成「状态恢复不了 + 同一条回答两行」。这里改用「该回答对应的用户问题」
// 的摘要——问题文本在直播与历史两条路径上都经过同一次归一化，所以刷新、
// 换标签页、历史窗口截断都能对上。同一问题在窗口内重复出现时用出现序号消歧。
const FEEDBACK_KEY_VERSION = 'q1';

const digestText = (text: string): string => {
  // 两条不同常数的 FNV-1a 变体拼接，纯前端可复算且无需 crypto。
  let h1 = 0x811c9dc5;
  let h2 = 0xc2b2ae35;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ code, 0x01000193);
    h2 = Math.imul(h2 ^ code, 0x85ebca6b);
  }
  return `${(h1 >>> 0).toString(36)}${(h2 >>> 0).toString(36)}`;
};

/**
 * 为消息列表逐条算出反馈键：user 消息与无法归属问题的 assistant 消息返回
 * null（此时不渲染反馈控件）。返回数组与入参下标一一对应。
 */
export const feedbackKeysForMessages = (
  messages: FeedbackKeySourceMessage[],
): (string | null)[] => {
  const keys: (string | null)[] = [];
  const occurrences = new Map<string, number>();
  let currentDigest: string | null = null;

  for (const message of messages || []) {
    const role = String(message?.role || '').toLowerCase();
    if (role === 'user') {
      const question = String(message?.content || '').trim();
      currentDigest = question ? digestText(question) : null;
      keys.push(null);
      continue;
    }
    if (role !== 'assistant' || !currentDigest) {
      keys.push(null);
      continue;
    }
    const seen = occurrences.get(currentDigest) ?? 0;
    occurrences.set(currentDigest, seen + 1);
    keys.push(`${FEEDBACK_KEY_VERSION}:${currentDigest}:${seen}`);
  }

  return keys;
};

// 每个会话一次拉取 + 内存缓存：多条消息的反馈按钮共享同一份状态，
// 避免逐消息 N+1 请求。提交/撤回后原地更新缓存。
const threadFeedbackCache = new Map<string, Promise<Map<string, FeedbackItem>>>();

const normalizeThreadId = (threadId: string): string => String(threadId || '').trim();

const feedbackPath = (threadId: string, messageKey?: string): string => {
  const base = `/api/v2/conversations/${encodeURIComponent(threadId)}/feedback`;
  return messageKey ? `${base}/${encodeURIComponent(messageKey)}` : base;
};

export const FeedbackAPI = {
  /** 当前会话的反馈状态（按 message_key 索引，进程内缓存）。 */
  loadThreadFeedback(threadId: string): Promise<Map<string, FeedbackItem>> {
    const key = normalizeThreadId(threadId);
    if (!key || !authState.value.token) {
      return Promise.resolve(new Map());
    }
    const cached = threadFeedbackCache.get(key);
    if (cached) return cached;

    const loading = (async () => {
      const payload = await request(feedbackPath(key));
      const map = new Map<string, FeedbackItem>();
      for (const item of payload?.items || []) {
        if (item?.message_key) map.set(String(item.message_key), item as FeedbackItem);
      }
      return map;
    })().catch((error) => {
      threadFeedbackCache.delete(key);
      throw error;
    });
    threadFeedbackCache.set(key, loading);
    return loading;
  },

  invalidateThread(threadId: string) {
    threadFeedbackCache.delete(normalizeThreadId(threadId));
  },

  async submit(threadId: string, payload: FeedbackSubmitPayload): Promise<void> {
    const key = normalizeThreadId(threadId);
    await request(feedbackPath(key), {
      method: 'POST',
      body: JSON.stringify({
        message_key: payload.messageKey,
        rating: payload.rating,
        reasons: payload.reasons?.length ? payload.reasons : undefined,
        comment: payload.comment || undefined,
        agent_code: payload.agentCode || undefined,
        model_name: payload.modelName || undefined,
        question_excerpt: payload.questionExcerpt?.slice(0, 500) || undefined,
        answer_excerpt: payload.answerExcerpt?.slice(0, 1000) || undefined,
      }),
    });

    const cached = threadFeedbackCache.get(key);
    if (cached) {
      const map = await cached.catch(() => null);
      map?.set(payload.messageKey, {
        message_key: payload.messageKey,
        rating: payload.rating,
        reasons: payload.reasons?.length ? payload.reasons : null,
        comment: payload.comment || null,
      });
    }
  },

  async remove(threadId: string, messageKey: string): Promise<void> {
    const key = normalizeThreadId(threadId);
    await request(feedbackPath(key, messageKey), { method: 'DELETE' });

    const cached = threadFeedbackCache.get(key);
    if (cached) {
      const map = await cached.catch(() => null);
      map?.delete(messageKey);
    }
  },

  /**
   * 回滚（重新生成 / 编辑重发）删掉的那些轮次，其反馈行必须一起撤回：
   * 新回答复用同一个问题摘要，否则旧评价会顶到新回答上。只删确实存在的
   * 键，所以正常情况是 0-1 个 DELETE。
   */
  async removeMany(threadId: string, messageKeys: string[]): Promise<void> {
    const key = normalizeThreadId(threadId);
    const candidates = Array.from(new Set(messageKeys.filter(Boolean)));
    if (!key || !candidates.length || !authState.value.token) return;

    let existing: Map<string, FeedbackItem>;
    try {
      existing = await this.loadThreadFeedback(key);
    } catch {
      return;
    }

    for (const messageKey of candidates) {
      if (!existing.has(messageKey)) continue;
      try {
        await this.remove(key, messageKey);
      } catch (error) {
        console.error('[FeedbackAPI] failed to drop stale feedback', messageKey, error);
      }
    }
  },
};
