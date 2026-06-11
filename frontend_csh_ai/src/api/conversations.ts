import { authState } from '@/utils/request';

export interface Conversation {
  id: number;
  thread_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ThreadArtifact {
  file_id: string;
  thread_id?: string | null;
  display_name: string;
  mime_type?: string | null;
  size_bytes?: number | null;
  source_type?: string;
  download_url: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export const ConversationAPI = {
  /**
   * List all conversations for the current user
   */
  async list(): Promise<Conversation[]> {
    const res = await fetch('/api/v2/conversations', {
      headers: {
        'Authorization': `Bearer ${authState.value.token}`
      }
    });
    if (!res.ok) throw new Error('ailed to fetch conversations');
    return res.json();
  },

  /**
   * Rename a conversation
   */
  async rename(threadId: string, title: string): Promise<Conversation> {
    const res = await fetch(`/api/v2/conversations/${threadId}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authState.value.token}`
      },
      body: JSON.stringify({ title })
    });
    if (!res.ok) throw new Error('Failed to rename conversation');
    return res.json();
  },

  /**
   * Delete a conversation
   */
  async delete(threadId: string): Promise<{ success: boolean }> {
    const res = await fetch(`/api/v2/conversations/${threadId}`, {
      method: 'DELETE',
      headers: {
        'Authorization': `Bearer ${authState.value.token}`
      }
    });
    if (!res.ok) throw new Error('Failed to delete conversation');
    return res.json();
  },

  /**
   * Get messages for a thread (history)
   * This endpoint might need to be implemented in agent-v2 or a dedicated one.
   * For now, we'll assume there's a messages endpoint or we list from the thread store.
   */
  async getMessages(threadId: string): Promise<any[]> {
     // implementation depends on how history is retrieved. 
     // Usually LangGraph has a get_state, but we need a specific history API.
     const res = await fetch(`/api/v2/conversations/${threadId}/messages`, {
       headers: {
         'Authorization': `Bearer ${authState.value.token}`
       }
     });
     if (!res.ok) return [];
     return res.json();
  },

  async getThreadArtifacts(threadId: string): Promise<ThreadArtifact[]> {
    const qs = new URLSearchParams({ thread_id: threadId });
    const res = await fetch(`/api/v2/files?${qs.toString()}`, {
      headers: {
        'Authorization': `Bearer ${authState.value.token}`
      }
    });
    if (!res.ok) return [];
    const payload = await res.json();
    return Array.isArray(payload?.items) ? payload.items : [];
  },

  /**
   * Rollback conversation to keep only the first N user turns.
   */
  async rollback(threadId: string, keepUserTurns: number): Promise<{ success: boolean }> {
    const res = await fetch(`/api/v2/conversations/${threadId}/rollback`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authState.value.token}`
      },
      body: JSON.stringify({ keep_user_turns: keepUserTurns })
    });
    if (!res.ok) throw new Error('Failed to rollback conversation');
    return res.json();
  }
};
