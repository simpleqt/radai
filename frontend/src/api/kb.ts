import { request } from '@/utils/request';

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
  created_at: string;
  updated_at: string;
}

export const KBDetailAPI = {
  list: (params: Record<string, any> = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/knowledge_bases${qs ? '?' + qs : ''}`);
  },
  create: (data: Partial<KnowledgeBase>) => request('/knowledge_bases', {
    method: 'POST',
    body: JSON.stringify(data)
  }),
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
  getDirectories: (id: string) => request(`/knowledge_bases/${id}/directories`),
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
  }),
  deleteDirectory: (kbId: string, path: string, deleteDocs: boolean = false) => request(`/knowledge_bases/${kbId}/directories/${encodeURIComponent(path)}?delete_documents=${deleteDocs}`, {
    method: 'DELETE'
  }),
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
