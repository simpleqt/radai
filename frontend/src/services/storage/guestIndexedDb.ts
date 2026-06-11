import type { ChatMessage } from '@/api/agent-v2';

const DB_NAME = 'guest-chat-db';
const DB_VERSION = 1;
const CONVERSATIONS_STORE = 'conversations';
const MESSAGES_STORE = 'messages';
const DEVICE_ID_KEY = 'guest_device_id';
const TABS_REGISTRY_KEY = 'guest_active_tabs_registry';
const CLEAR_ON_BOOT_KEY = 'guest_clear_on_boot';
const SESSION_TAB_ID_KEY = 'guest_session_tab_id';
const TAB_STALE_MS = 60_000;
const TAB_HEARTBEAT_MS = 20_000;

export interface GuestConversationMeta {
  threadId: string;
  title: string;
  updatedAt: number;
  createdAt: number;
}

export interface GuestExportPayload {
  version: 1;
  exportedAt: number;
  conversations: GuestConversationMeta[];
  messages: Array<{
    id: string;
    threadId: string;
    role: 'user' | 'assistant';
    content: string;
    reasoning?: string;
    statusHistory?: string[];
    processEvents?: any[];
    createdAt?: number;
  }>;
}

const openDb = (): Promise<IDBDatabase> =>
  new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(CONVERSATIONS_STORE)) {
        db.createObjectStore(CONVERSATIONS_STORE, { keyPath: 'threadId' });
      }
      if (!db.objectStoreNames.contains(MESSAGES_STORE)) {
        const msgStore = db.createObjectStore(MESSAGES_STORE, { keyPath: 'id' });
        msgStore.createIndex('threadId', 'threadId', { unique: false });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('open indexeddb failed'));
  });

const runTxn = <T>(
  storeNames: string | string[],
  mode: IDBTransactionMode,
  runner: (tx: IDBTransaction, done: (value: T) => void, fail: (err: Error) => void) => void,
): Promise<T> =>
  new Promise(async (resolve, reject) => {
    try {
      const db = await openDb();
      const tx = db.transaction(storeNames, mode);
      let finished = false;

      const done = (value: T) => {
        if (finished) return;
        finished = true;
        resolve(value);
      };

      const fail = (err: Error) => {
        if (finished) return;
        finished = true;
        reject(err);
      };

      tx.onerror = () => fail(tx.error || new Error('indexeddb transaction failed'));
      tx.onabort = () => fail(tx.error || new Error('indexeddb transaction aborted'));
      runner(tx, done, fail);
    } catch (e: any) {
      reject(e);
    }
  });

const safeJsonClone = <T>(value: T, fallback: T): T => {
  try {
    return JSON.parse(JSON.stringify(value)) as T;
  } catch {
    return fallback;
  }
};

const normalizeMessageForStorage = (msg: ChatMessage): ChatMessage => {
  const normalizedRole = msg.role === 'assistant' ? 'assistant' : 'user';
  const messageId = String(msg.id || `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`);

  return {
    id: messageId,
    role: normalizedRole,
    content: String(msg.content || ''),
    reasoning: String(msg.reasoning || ''),
    statusHistory: safeJsonClone<string[]>(msg.statusHistory || [], []),
    processEvents: safeJsonClone<any[]>(msg.processEvents || [], []),
    followUpSuggestions: safeJsonClone(msg.followUpSuggestions || [], []),
    isStreaming: false,
  };
};

const toStorageMessage = (threadId: string, msg: ChatMessage) => ({
  id: msg.id,
  threadId,
  role: msg.role,
  content: msg.content,
  reasoning: msg.reasoning || '',
  statusHistory: msg.statusHistory || [],
  processEvents: msg.processEvents || [],
  followUpSuggestions: msg.followUpSuggestions || [],
  createdAt: Number(msg.id) || Date.now(),
});

const buildTitle = (messages: ChatMessage[]) => {
  const firstUser = messages.find((m) => m.role === 'user');
  const raw = (firstUser?.content || '新对话').trim();
  if (!raw) return '新对话';
  return raw.length > 30 ? `${raw.slice(0, 30)}...` : raw;
};

export const getOrCreateGuestDeviceId = (): string => {
  const existing = sessionStorage.getItem(DEVICE_ID_KEY);
  if (existing) return existing;

  const deviceId =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? `gdev_${crypto.randomUUID()}`
      : `gdev_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;

  sessionStorage.setItem(DEVICE_ID_KEY, deviceId);
  return deviceId;
};

export const saveGuestConversation = async (threadId: string, messages: ChatMessage[]) => {
  const normalizedMessages = (messages || []).map(normalizeMessageForStorage);
  const now = Date.now();
  await runTxn<void>([CONVERSATIONS_STORE, MESSAGES_STORE], 'readwrite', (tx, done, fail) => {
    const convStore = tx.objectStore(CONVERSATIONS_STORE);
    const msgStore = tx.objectStore(MESSAGES_STORE);

    const convReq = convStore.get(threadId);
    convReq.onsuccess = () => {
      const existing = convReq.result as GuestConversationMeta | undefined;
      convStore.put({
        threadId,
        title: buildTitle(normalizedMessages),
        updatedAt: now,
        createdAt: existing?.createdAt || now,
      });

      const index = msgStore.index('threadId');
      const cursorReq = index.openCursor(IDBKeyRange.only(threadId));
      cursorReq.onsuccess = () => {
        const cursor = cursorReq.result;
        if (cursor) {
          msgStore.delete(cursor.primaryKey);
          cursor.continue();
          return;
        }

        try {
          normalizedMessages.forEach((msg) => {
            msgStore.put(toStorageMessage(threadId, msg));
          });
        } catch (e: any) {
          fail(e instanceof Error ? e : new Error(String(e)));
          return;
        }
        done(undefined);
      };
      cursorReq.onerror = () => fail(cursorReq.error || new Error('load old guest messages failed'));
    };
    convReq.onerror = () => fail(convReq.error || new Error('load guest conversation failed'));
  });
};

export const loadGuestMessages = async (threadId: string): Promise<ChatMessage[]> =>
  runTxn<ChatMessage[]>(MESSAGES_STORE, 'readonly', (tx, done) => {
    const store = tx.objectStore(MESSAGES_STORE);
    const index = store.index('threadId');
    const req = index.getAll(IDBKeyRange.only(threadId));
    req.onsuccess = () => {
      const rows = (req.result || []) as any[];
      rows.sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0));
      done(
        rows.map((row) => ({
          id: row.id,
          role: row.role,
          content: row.content,
          reasoning: row.reasoning || '',
          statusHistory: row.statusHistory || [],
          processEvents: row.processEvents || [],
          followUpSuggestions: row.followUpSuggestions || [],
          isStreaming: false,
        })),
      );
    };
  });

export const listGuestConversations = async (): Promise<GuestConversationMeta[]> =>
  runTxn<GuestConversationMeta[]>(CONVERSATIONS_STORE, 'readonly', (tx, done) => {
    const store = tx.objectStore(CONVERSATIONS_STORE);
    const req = store.getAll();
    req.onsuccess = () => {
      const list = (req.result || []) as GuestConversationMeta[];
      list.sort((a, b) => b.updatedAt - a.updatedAt);
      done(list);
    };
  });

export const deleteGuestConversation = async (threadId: string) => {
  await runTxn<void>([CONVERSATIONS_STORE, MESSAGES_STORE], 'readwrite', (tx, done) => {
    tx.objectStore(CONVERSATIONS_STORE).delete(threadId);

    const msgStore = tx.objectStore(MESSAGES_STORE);
    const index = msgStore.index('threadId');
    const cursorReq = index.openCursor(IDBKeyRange.only(threadId));
    cursorReq.onsuccess = () => {
      const cursor = cursorReq.result;
      if (cursor) {
        msgStore.delete(cursor.primaryKey);
        cursor.continue();
        return;
      }
      done(undefined);
    };
  });
};

export const renameGuestConversation = async (threadId: string, title: string) => {
  const nextTitle = title.trim();
  if (!nextTitle) return;

  await runTxn<void>(CONVERSATIONS_STORE, 'readwrite', (tx, done, fail) => {
    const store = tx.objectStore(CONVERSATIONS_STORE);
    const req = store.get(threadId);
    req.onsuccess = () => {
      const existing = req.result as GuestConversationMeta | undefined;
      if (!existing) {
        done(undefined);
        return;
      }
      store.put({
        ...existing,
        title: nextTitle,
        updatedAt: Date.now(),
      });
      done(undefined);
    };
    req.onerror = () => fail(req.error || new Error('load conversation failed'));
  });
};

export const clearGuestConversations = async () => {
  await runTxn<void>([CONVERSATIONS_STORE, MESSAGES_STORE], 'readwrite', (tx, done) => {
    tx.objectStore(CONVERSATIONS_STORE).clear();
    tx.objectStore(MESSAGES_STORE).clear();
    done(undefined);
  });
};

export const exportGuestConversations = async (): Promise<GuestExportPayload> => {
  const [conversations, messages] = await Promise.all([
    listGuestConversations(),
    runTxn<any[]>(MESSAGES_STORE, 'readonly', (tx, done) => {
      const req = tx.objectStore(MESSAGES_STORE).getAll();
      req.onsuccess = () => done((req.result || []) as any[]);
    }),
  ]);

  return {
    version: 1,
    exportedAt: Date.now(),
    conversations,
    messages,
  };
};

export const importGuestConversations = async (payload: GuestExportPayload) => {
  if (!payload || payload.version !== 1) {
    throw new Error('不支持的导入文件版本');
  }

  await runTxn<void>([CONVERSATIONS_STORE, MESSAGES_STORE], 'readwrite', (tx, done) => {
    const convStore = tx.objectStore(CONVERSATIONS_STORE);
    const msgStore = tx.objectStore(MESSAGES_STORE);

    for (const conv of payload.conversations || []) {
      convStore.put(conv);
    }
    for (const msg of payload.messages || []) {
      msgStore.put(msg);
    }
    done(undefined);
  });
};

const generateTabId = () =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? `gtab_${crypto.randomUUID()}`
    : `gtab_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;

const readTabsRegistry = (): Record<string, number> => {
  try {
    const raw = localStorage.getItem(TABS_REGISTRY_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== 'object') {
      return {};
    }
    return parsed as Record<string, number>;
  } catch {
    return {};
  }
};

const writeTabsRegistry = (registry: Record<string, number>) => {
  localStorage.setItem(TABS_REGISTRY_KEY, JSON.stringify(registry));
};

const pruneStaleTabs = (registry: Record<string, number>) => {
  const now = Date.now();
  const next: Record<string, number> = {};
  Object.entries(registry).forEach(([tabId, ts]) => {
    if (typeof ts === 'number' && now - ts <= TAB_STALE_MS) {
      next[tabId] = ts;
    }
  });
  return next;
};

const markClearOnBoot = () => {
  localStorage.setItem(CLEAR_ON_BOOT_KEY, '1');
};

const ensureSessionTabId = () => {
  let tabId = sessionStorage.getItem(SESSION_TAB_ID_KEY);
  if (tabId) return tabId;
  tabId = generateTabId();
  sessionStorage.setItem(SESSION_TAB_ID_KEY, tabId);
  return tabId;
};

const upsertCurrentTab = (tabId: string) => {
  const registry = pruneStaleTabs(readTabsRegistry());
  registry[tabId] = Date.now();
  writeTabsRegistry(registry);
};

const removeCurrentTab = (tabId: string) => {
  const registry = pruneStaleTabs(readTabsRegistry());
  delete registry[tabId];
  writeTabsRegistry(registry);
  return Object.keys(registry).length;
};

export const initializeGuestSessionLifecycle = () => {
  if (typeof window === 'undefined') return;

  const tabId = ensureSessionTabId();
  const shouldClear = localStorage.getItem(CLEAR_ON_BOOT_KEY) === '1';
  const activeRegistry = pruneStaleTabs(readTabsRegistry());

  // Last browser session ended: clear persisted guest data before registering this tab.
  if (shouldClear || Object.keys(activeRegistry).length === 0) {
    void clearGuestConversations()
      .catch(() => undefined)
      .finally(() => {
        localStorage.removeItem(CLEAR_ON_BOOT_KEY);
      });
  }

  upsertCurrentTab(tabId);

  const heartbeatTimer = window.setInterval(() => {
    upsertCurrentTab(tabId);
  }, TAB_HEARTBEAT_MS);

  const onLeavingPage = () => {
    const remainingTabs = removeCurrentTab(tabId);
    if (remainingTabs === 0) {
      // page unload may interrupt async cleanup; keep a boot flag as a reliable fallback.
      markClearOnBoot();
      void clearGuestConversations().catch(() => undefined);
    }
  };

  window.addEventListener('pagehide', onLeavingPage);
  window.addEventListener('beforeunload', onLeavingPage);

  window.addEventListener('storage', (event) => {
    if (event.key !== TABS_REGISTRY_KEY) return;
    const latest = pruneStaleTabs(readTabsRegistry());
    if (!latest[tabId]) {
      latest[tabId] = Date.now();
      writeTabsRegistry(latest);
    }
  });

  const cleanup = () => {
    window.clearInterval(heartbeatTimer);
    window.removeEventListener('pagehide', onLeavingPage);
    window.removeEventListener('beforeunload', onLeavingPage);
  };

  return cleanup;
};
