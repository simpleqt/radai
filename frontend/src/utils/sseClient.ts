import { authState } from '@/utils/request';
import { workflowStartedStatus } from '@/api/agent-v2';

export interface SSEHandlers {
  onChunk?: (text: string, mode?: 'append' | 'replace') => void;
  onStatus?: (status: string) => void;
  onEvent?: (event: any) => void;
  onThinking?: (text: string) => void;
  onDone?: () => void;
  onError?: (message: string) => void;
}

function normalizeRuntimeEvent(data: any): { type: string | undefined; payload: any } {
  if (data && typeof data === 'object' && data.payload && typeof data.type === 'string') {
    return { type: data.type, payload: data.payload };
  }
  return { type: data?.type, payload: data };
}

/**
 * 通用 SSE 读取器：基于 fetch + ReadableStream 解析 `data: {json}` 行协议，
 * 支持标准 AgentEvent wire payload 和后台任务旧事件格式。
 * 自动注入 Authorization。返回 Promise，在流结束（[DONE] 或连接关闭）时 resolve。
 */
export async function streamSSE(
  url: string,
  body: unknown,
  handlers: SSEHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authState.value.token) {
    headers['Authorization'] = `Bearer ${authState.value.token}`;
  }

  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal,
    });
  } catch (err: any) {
    if (err?.name === 'AbortError') return;
    handlers.onError?.(err?.message || '网络请求失败');
    return;
  }

  if (!res.ok) {
    handlers.onError?.(`请求失败: ${res.status} ${res.statusText}`);
    return;
  }

  const reader = res.body?.getReader();
  if (!reader) {
    handlers.onError?.('无法读取响应流');
    return;
  }

  const decoder = new TextDecoder();
  let buffer = '';
  let doneReceived = false;

  try {
    while (true) {
      if (signal?.aborted) {
        try { await reader.cancel(); } catch { /* ignore */ }
        return;
      }

      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('data: ')) continue;
        const dataStr = trimmed.slice(6).trim();
        if (dataStr === '[DONE]') {
          doneReceived = true;
          continue;
        }
        try {
          const data = JSON.parse(dataStr);
          const { type, payload } = normalizeRuntimeEvent(data);
          if (type === 'done') {
            doneReceived = true;
          } else if (type === 'reasoning.delta' || type === 'thinking') {
            handlers.onThinking?.(payload.content || '');
          } else if (type === 'workflow.started') {
            handlers.onStatus?.(workflowStartedStatus(payload.workflow));
          } else if (type === 'workflow.step' && payload.event !== undefined) {
            handlers.onEvent?.(payload.event);
          } else if (type === 'workflow.step' || type === 'status' || payload.status !== undefined) {
            handlers.onStatus?.(payload.content || payload.status || '');
          } else if (type === 'event' || payload.event !== undefined) {
            if (payload.event !== undefined) handlers.onEvent?.(payload.event);
          } else if (type === 'message.delta' || type === 'chunk') {
            handlers.onChunk?.(payload.content || '', 'append');
          } else if (type === 'message.replace') {
            handlers.onChunk?.(payload.content || '', 'replace');
          } else if (type === 'error' || payload.error !== undefined) {
            handlers.onError?.(String(payload.message || payload.error));
          } else if (typeof payload.content === 'string') {
            handlers.onChunk?.(payload.content);
          }
        } catch (e) {
          console.error('[SSE] parse error', e, dataStr);
        }
      }
    }
  } catch (err: any) {
    if (err?.name !== 'AbortError') {
      handlers.onError?.(err?.message || '流读取异常');
    }
    return;
  }

  if (!doneReceived) {
    // 流在未收到 [DONE]/done 事件时关闭（代理掐断/后端崩溃）：按中断
    // 处理而不是当作正常完成，避免消费者把截断内容缓存成完整结果
    // （与 agent 聊天路径的 interrupted 纪律一致）。
    handlers.onError?.('连接中断，内容可能不完整');
    return;
  }
  handlers.onDone?.();
}
