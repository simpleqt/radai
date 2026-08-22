import { ref, computed } from 'vue';
import { ExploreAPI, type ExploreNode } from '@/api/explore';
import { AgentAPI, type KbOption } from '@/api/agent-v2';
import { clearExploreNodeCaches } from './exploreNodeCache';

export interface MapNode {
  id: string;
  title: string;
  summary?: string;
  expandable: boolean;
  children: MapNode[];
  expanded: boolean;
  loading: boolean;
  depth: number;
  loaded: boolean; // 是否已加载过子节点
}

const DRAFT_KEY_PREFIX = 'explore_draft_';
const INVALID_NODE_TITLES = new Set(['子知识点', '子节点', '知识点', '节点', '主题名', '标题', 'title']);

function isValidNodeTitle(title: unknown): title is string {
  const normalized = String(title || '').trim().replace(/[\s:：,，.。;；、\-_/\\|]+/g, '').toLowerCase();
  return Boolean(normalized) && !INVALID_NODE_TITLES.has(normalized);
}

function usableExploreNodes(nodes: ExploreNode[] | undefined): ExploreNode[] {
  return (nodes || []).filter((node) => isValidNodeTitle(node?.title));
}

function toMapNode(n: ExploreNode, depth: number): MapNode {
  return {
    id: n.id || `n_${Math.random().toString(36).slice(2, 10)}`,
    title: n.title,
    summary: n.summary,
    expandable: n.expandable !== false,
    children: [],
    expanded: false,
    loading: false,
    depth,
    loaded: false,
  };
}

function normalizeDraftNode(raw: any, depth = 0): MapNode | null {
  if (!raw || !isValidNodeTitle(raw.title)) return null;
  const children = Array.isArray(raw.children)
    ? raw.children
        .map((child: any) => normalizeDraftNode(child, depth + 1))
        .filter((child: MapNode | null): child is MapNode => Boolean(child))
    : [];
  return {
    id: String(raw.id || `n_${Math.random().toString(36).slice(2, 10)}`),
    title: String(raw.title),
    summary: raw.summary,
    expandable: raw.expandable !== false,
    children,
    expanded: Boolean(raw.expanded),
    loading: false,
    depth,
    loaded: Boolean(raw.loaded || children.length > 0),
  };
}

export function useExploreSession() {
  const view = ref<'galaxy' | 'map'>('galaxy');
  const currentTopic = ref('');
  const kbId = ref<string>('');
  const boundKbIds = ref<string[]>([]);
  const availableKbs = ref<KbOption[]>([]);
  const root = ref<MapNode | null>(null);
  const selectedNodeId = ref<string | null>(null);
  const loadingMap = ref(false);
  const errorMsg = ref('');

  const kbIds = computed(() => (kbId.value ? [kbId.value] : [...boundKbIds.value]));

  const selectedNode = computed<MapNode | null>(() => {
    if (!selectedNodeId.value || !root.value) return null;
    return findNode(root.value, selectedNodeId.value);
  });

  function findNode(node: MapNode, id: string): MapNode | null {
    if (node.id === id) return node;
    for (const child of node.children) {
      const found = findNode(child, id);
      if (found) return found;
    }
    return null;
  }

  function getPath(id: string): string[] {
    if (!root.value) return [];
    const path: string[] = [];
    const walk = (node: MapNode): boolean => {
      path.push(node.title);
      if (node.id === id) return true;
      for (const child of node.children) {
        if (walk(child)) return true;
      }
      path.pop();
      return false;
    };
    walk(root.value);
    return path;
  }

  async function loadAgentInfo() {
    try {
      const info = await AgentAPI.getInfo();
      availableKbs.value = info.available_kbs || [];
      const availableIds = new Set(availableKbs.value.map((kb) => kb.id));
      boundKbIds.value = (info.kb_ids || []).filter((id) => availableIds.has(id));
      const preferredKbId = boundKbIds.value[0] || availableKbs.value[0]?.id || '';
      if (!kbId.value || !availableIds.has(kbId.value)) {
        kbId.value = preferredKbId;
      }
    } catch (e) {
      console.warn('[Explore] 加载智能体信息失败', e);
    }
  }

  async function startTopic(topic: string) {
    const t = topic.trim();
    if (!t || loadingMap.value) return;
    currentTopic.value = t;
    loadingMap.value = true;
    errorMsg.value = '';
    selectedNodeId.value = null;
    clearExploreNodeCaches();
    view.value = 'map';
    try {
      const data = await ExploreAPI.map(t, kbIds.value);
      const rootNode: MapNode = {
        id: data.root.id || 'n_root',
        title: data.root.title || t,
        summary: data.root.summary,
        expandable: true,
        children: usableExploreNodes(data.children).map((c) => toMapNode(c, 1)),
        expanded: true,
        loading: false,
        depth: 0,
        loaded: true,
      };
      root.value = rootNode;
      saveDraft();
    } catch (e: any) {
      errorMsg.value = e?.message || '生成思维导图失败';
      view.value = 'galaxy';
    } finally {
      loadingMap.value = false;
    }
  }

  async function toggleExpand(node: MapNode) {
    if (node.loaded && node.children.length > 0) {
      node.expanded = !node.expanded;
      return;
    }
    if (!node.expandable || node.loading) return;
    node.loading = true;
    errorMsg.value = '';
    try {
      const path = getPath(node.id);
      const data = await ExploreAPI.expand(node.title, path, kbIds.value);
      node.children = usableExploreNodes(data.children).map((c) => toMapNode(c, node.depth + 1));
      node.loaded = true;
      node.expanded = true;
      if (node.children.length === 0) {
        node.expandable = false;
        errorMsg.value = '该节点暂未生成可用子知识点';
      } else {
        saveDraft();
      }
    } catch (e: any) {
      console.warn('[Explore] 展开节点失败', e);
      errorMsg.value = e?.message || '展开节点失败，请稍后重试';
    } finally {
      node.loading = false;
    }
  }

  function selectNode(node: MapNode) {
    selectedNodeId.value = node.id;
  }

  function closePanel() {
    selectedNodeId.value = null;
  }

  function backToGalaxy() {
    view.value = 'galaxy';
    selectedNodeId.value = null;
  }

  function reset() {
    root.value = null;
    currentTopic.value = '';
    selectedNodeId.value = null;
    view.value = 'galaxy';
    clearExploreNodeCaches();
    clearDraft();
  }

  // ── 草稿留存 ─────────────────────────────────
  function draftKey() {
    return `${DRAFT_KEY_PREFIX}${kbId.value || 'default'}`;
  }
  function saveDraft() {
    try {
      if (!root.value) return;
      localStorage.setItem(draftKey(), JSON.stringify({ topic: currentTopic.value, root: root.value }));
    } catch { /* ignore */ }
  }
  function clearDraft() {
    try { localStorage.removeItem(draftKey()); } catch { /* ignore */ }
  }
  function loadDraft() {
    try {
      const raw = localStorage.getItem(draftKey());
      if (!raw) return false;
      const data = JSON.parse(raw);
      if (data?.root) {
        const restored = normalizeDraftNode(data.root);
        if (!restored) {
          clearDraft();
          return false;
        }
        root.value = restored;
        currentTopic.value = data.topic || '';
        view.value = 'map';
        return true;
      }
    } catch { /* ignore */ }
    return false;
  }

  return {
    view, currentTopic, kbId, kbIds, availableKbs, root, selectedNodeId, selectedNode,
    loadingMap, errorMsg,
    loadAgentInfo, startTopic, toggleExpand, selectNode, closePanel, backToGalaxy, reset,
    getPath, loadDraft, saveDraft, clearDraft,
  };
}

export type ExploreSession = ReturnType<typeof useExploreSession>;
