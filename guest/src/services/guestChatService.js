const DEVICE_ID_KEY = 'guest_frontend_device_id';

const FALLBACK_KBS = (
  import.meta.env.VITE_DEFAULT_KB_IDS || ''
).split(',').filter(Boolean);

export const KB_LABELS = (() => {
  const labels = {};
  const entries = (import.meta.env.VITE_KB_LABELS || '').split(',').filter(Boolean);
  for (const entry of entries) {
    const [id, name] = entry.split('=');
    if (id && name) labels[id.trim()] = name.trim();
  }
  return labels;
})();

export function getOrCreateGuestDeviceId() {
  const existing = sessionStorage.getItem(DEVICE_ID_KEY);
  if (existing) return existing;

  const nextId =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `guest_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  sessionStorage.setItem(DEVICE_ID_KEY, nextId);
  return nextId;
}

export async function getAvailableKbs() {
  try {
    const res = await fetch('/api/v2/agent/info');
    if (!res.ok) throw new Error('agent info request failed');
    const data = await res.json();
    if (Array.isArray(data?.kb_ids) && data.kb_ids.length > 0) {
      return data.kb_ids;
    }
  } catch (_) {
    // fallback below
  }
  return FALLBACK_KBS;
}

export async function queryGuestStream({ question, kbIds, threadId, onChunk, onStatus }) {
  const headers = {
    'Content-Type': 'application/json',
    'X-Device-ID': getOrCreateGuestDeviceId(),
  };

  const payload = {
    question,
    thread_id: threadId,
    kb_ids: kbIds,
    use_search: false,
    selected_skills: [],
    subagent_enabled: false,
    thinking: false,
    mode: 'lightweight',
  };

  const res = await fetch('/api/v2/guest/query', {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  });

  if (!res.ok) throw new Error(`请求失败: ${res.status} ${res.statusText}`);
  if (!res.body) throw new Error('响应流不可用');

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const raw = line.slice(6).trim();
      if (raw === '[DONE]') return;

      try {
        const data = JSON.parse(raw);
        if (data.type === 'chunk') {
          onChunk(data.content || '');
          continue;
        }
        if (data.type === 'status' || data.status !== undefined) {
          onStatus(data.content || data.status || '');
        }
      } catch (_) {
        // ignore malformed chunk
      }
    }
  }
}
