import { request } from '@/utils/request';

export type IngestionBackend = 'legacy_lightrag' | 'self_ingestion';
export type RetrievalBackend = 'legacy_lightrag' | 'self_ingestion';

export interface KnowledgeBase {
  kb_id: string;
  kb_code: string;
  kb_name: string;
  subject?: string | null;
  description?: string | null;
  status: string;
  doc_count: number;
  entity_count: number;
  relation_count: number;
  ingestion_backend?: IngestionBackend;
  retrieval_backend?: RetrievalBackend;
  self_ingestion_enabled?: boolean;
  self_retriever_enabled?: boolean;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBaseCreateInput {
  kb_name: string;
  kb_code: string;
  subject?: string | null;
  description?: string | null;
  ingestion_backend: IngestionBackend;
  retrieval_backend: RetrievalBackend;
  self_ingestion_enabled: boolean;
  self_retriever_enabled: boolean;
}

export interface ExtractionSchemaOption {
  schema_id: string;
  subject: string;
  version: string;
  schema_version: string;
  label: string;
  entity_type_count: number;
  relation_type_count: number;
}

export interface KnowledgeGraphNode {
  id: string;
  label: string;
  entity_type: string;
  description: string;
  source_id: string;
  file_path: string;
  degree: number;
}

export interface KnowledgeGraphEdge {
  source: string;
  target: string;
  relation_type: string;
  description: string;
  source_id: string;
  weight: number;
}

export interface KnowledgeGraphResponse {
  kb_id: string;
  kb_name: string;
  total_nodes: number;
  total_edges: number;
  sampled_nodes: number;
  sampled_edges: number;
  truncated: boolean;
  cache_hit: boolean;
  cache_ttl_seconds: number;
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
}

export interface KbDocument {
  id: number;
  kb_id: string;
  doc_id: string;
  file_name: string;
  file_path?: string | null;
  category_path?: string | null;
  parse_status: number;
  storage_status?: number;
  error_message?: string | null;
  chunk_count?: number;
  entity_count?: number;
  relation_count?: number;
  ingestion_backend?: IngestionBackend;
  doc_status?: string | null;
  commit_version?: number;
  sort_order?: number;
  created_at: string;
  updated_at?: string | null;
}

export interface DocumentChunkItem {
  chunk_id: string;
  chunk_index: number;
  block_type: string;
  content: string;
  token_count: number;
  char_start?: number | null;
  char_end?: number | null;
  block_id?: string | null;
  commit_version: number;
}

export interface DocumentChunkListResponse {
  kb_id: string;
  doc_id: string;
  file_name: string;
  total: number;
  items: DocumentChunkItem[];
}

export interface DirectoryTreeNode {
  id?: number | string;
  path: string;
  name: string;
  description?: string | null;
  icon?: string | null;
  color?: string | null;
  sort_order?: number;
  doc_count?: number;
  total_doc_count?: number;
  is_virtual?: boolean;
  children: DirectoryTreeNode[];
}

export interface DirectoryTreeResponse {
  tree: DirectoryTreeNode[];
  total_directories: number;
  total_docs: number;
}

export const KBDetailAPI = {
  list: (params: Record<string, any> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/knowledge_bases${qs ? '?' + qs : ''}`);
  },
  create: (data: Partial<KnowledgeBaseCreateInput>) => request('/knowledge_bases', {
    method: 'POST',
    body: JSON.stringify(data)
  }),
  listExtractionSchemas: () =>
    request('/knowledge_bases/extraction_schemas') as Promise<ExtractionSchemaOption[]>,
  get: (id: string) => request(`/knowledge_bases/${id}`),
  update: (id: string, data: Partial<KnowledgeBase>) => request(`/knowledge_bases/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data)
  }),
  delete: (id: string) => request(`/knowledge_bases/${id}`, {
    method: 'DELETE'
  }),
  documents: (id: string, params: Record<string, any> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/knowledge_bases/${id}/documents${qs ? '?' + qs : ''}`);
  },
  graph: (id: string, params: Record<string, any> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/knowledge_bases/${id}/graph${qs ? '?' + qs : ''}`) as Promise<KnowledgeGraphResponse>;
  },
  deleteDocument: (kbId: string, docId: string) => request(`/knowledge_bases/${kbId}/documents/${docId}`, {
    method: 'DELETE'
  }),
  ingest: (data: {
    kb_id: string,
    file_ids: string[],
    document_date?: string | null,
    category_path?: string | null,
    options?: any
  }) => request('/uploads/ingest', {
    method: 'POST',
    body: JSON.stringify(data)
  }),
  ingestStatus: (taskId: string) => request(`/uploads/ingest/status/${taskId}`),
  recentDocuments: () => request('/documents/recent'),
  getDirectories: (id: string) => request(`/knowledge_bases/${id}/directories`) as Promise<DirectoryTreeResponse>,
  batchMoveDocuments: (kbId: string, docIds: string[], targetPath: string | null) => request(`/knowledge_bases/${kbId}/documents/batch-move`, {
    method: 'POST',
    body: JSON.stringify({ doc_ids: docIds, category_path: targetPath })
  }),
  batchDeleteDocuments: (kbId: string, docIds: string[], deleteLlmCache: boolean = false) => request(`/knowledge_bases/${kbId}/documents/batch-delete`, {
    method: 'POST',
    body: JSON.stringify({ doc_ids: docIds, delete_llm_cache: deleteLlmCache })
  }),
  batchMovePending: (fileIds: string[], targetPath: string | null) => request('/uploads/pending/batch-move', {
    method: 'PATCH',
    body: JSON.stringify({ file_ids: fileIds, category_path: targetPath })
  }),
  renameDirectory: (kbId: string, oldPath: string, newName: string) => request(`/knowledge_bases/${kbId}/directories/${encodeURIComponent(oldPath)}/rename`, {
    method: 'POST',
    body: JSON.stringify({ new_name: newName })
  }) as Promise<{ success: boolean; old_path: string; new_path: string; affected_directories: number }>,
  moveDirectory: (kbId: string, sourcePath: string, targetParentPath: string | null) => request(`/knowledge_bases/${kbId}/directories/${encodeURIComponent(sourcePath)}/move`, {
    method: 'POST',
    body: JSON.stringify({ target_parent_path: targetParentPath })
  }) as Promise<{ success: boolean; old_path: string; new_path: string; affected_directories: number }>,
  deleteDirectory: (kbId: string, path: string, deleteDocs: boolean = false) => request(`/knowledge_bases/${kbId}/directories/${encodeURIComponent(path)}?delete_documents=${deleteDocs}`, {
    method: 'DELETE'
  }),
  reorderDirectories: (kbId: string, payload: { parent_path?: string | null; paths: string[] }) =>
    request(`/knowledge_bases/${kbId}/directories/reorder`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }) as Promise<{ success: boolean; updated: number }>,
  reorderDocuments: (kbId: string, payload: { category_path?: string | null; doc_ids: string[] }) =>
    request(`/knowledge_bases/${kbId}/documents/reorder`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }) as Promise<{ success: boolean; updated: number }>,
  documentChunks: (kbId: string, docId: string, params: Record<string, any> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/knowledge_bases/${kbId}/documents/${encodeURIComponent(docId)}/chunks${qs ? '?' + qs : ''}`) as Promise<DocumentChunkListResponse>;
  },
  updateDocument: (kbId: string, docId: string, data: any) => request(`/knowledge_bases/${kbId}/documents/${docId}`, {
    method: 'PATCH',
    body: JSON.stringify(data)
  }),
  initKeywords: (kbId: string, data: any) => request(`/keywords/init/${kbId}`, {
    method: 'POST',
    body: JSON.stringify(data)
  }),
  getKeywordInitStatus: (taskId: string) => request(`/keywords/init/status/${taskId}`)
};
