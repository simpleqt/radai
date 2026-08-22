import { request } from '@/utils/request';

export type SkillStatus = 'draft' | 'published' | 'disabled' | 'archived';

export interface SkillVersion {
  version_id: string;
  skill_id: string;
  version: string;
  status: string;
  package_sha256: string;
  package_size: number;
  manifest: Record<string, any>;
  file_index: Array<Record<string, any>>;
  review_comment?: string | null;
  created_by: string;
  reviewed_by?: string | null;
  created_at: string;
  published_at?: string | null;
}

export interface ManagedSkill {
  skill_id: string;
  skill_key: string;
  display_name: string;
  description: string;
  display_description: string;
  domain: string;
  source_type: string;
  visibility: 'public' | 'private' | 'restricted';
  owner_user_id?: string | null;
  status: SkillStatus;
  active_version_id?: string | null;
  active_version?: SkillVersion;
  created_at: string;
  updated_at: string;
}

export interface AgentSummary {
  agent_code: string;
  agent_name: string;
  domain: string;
  description?: string | null;
  is_active: boolean;
  sort_order: number;
}

interface AgentProfileListResponse {
  profiles: Array<{
    code: string;
    name: string;
    domain: string;
    description?: string;
  }>;
}

export interface AgentSkillBinding {
  skill_id: string;
  skill_key: string;
  display_name: string;
  domain: string;
  status: string;
  is_enabled: boolean;
  is_required: boolean;
  default_selected: boolean;
  is_selectable: boolean;
  sort_order: number;
}

export interface SkillGrant {
  id: number;
  subject_type: 'user' | 'role';
  subject_id: string;
  permission: 'view' | 'use' | 'edit' | 'manage';
  effect: 'allow' | 'deny';
  expires_at?: string | null;
}

export type AgentSkillBindingInput = Pick<
  AgentSkillBinding,
  'skill_id' | 'is_enabled' | 'is_required' | 'default_selected' | 'is_selectable' | 'sort_order'
>;

const skillPath = (skillId: string) => `/skills/${encodeURIComponent(skillId)}`;
const versionPath = (skillId: string, versionId: string) =>
  `${skillPath(skillId)}/versions/${encodeURIComponent(versionId)}`;

export const SkillAdminAPI = {
  list: (status?: string) =>
    request(`/skills${status ? `?status=${encodeURIComponent(status)}` : ''}`) as Promise<{
      total: number;
      skills: ManagedSkill[];
    }>,

  upload: (file: File, visibility: ManagedSkill['visibility']) => {
    const body = new FormData();
    body.append('file', file);
    body.append('visibility', visibility);
    return request('/skills/uploads', { method: 'POST', body });
  },

  versions: (skillId: string) =>
    request(`${skillPath(skillId)}/versions`) as Promise<{
      total: number;
      versions: SkillVersion[];
    }>,

  submit: (skillId: string, versionId: string) =>
    request(`${versionPath(skillId, versionId)}/submit`, { method: 'POST' }),

  publish: (skillId: string, versionId: string) =>
    request(`${versionPath(skillId, versionId)}/publish`, { method: 'POST' }),

  reject: (skillId: string, versionId: string, comment: string) =>
    request(`${versionPath(skillId, versionId)}/reject`, {
      method: 'POST',
      body: JSON.stringify({ comment }),
    }),

  setEnabled: (skillId: string, enabled: boolean) =>
    request(`${skillPath(skillId)}/${enabled ? 'enable' : 'disable'}`, { method: 'POST' }),

  archive: (skillId: string) => request(skillPath(skillId), { method: 'DELETE' }),

  download: (skillId: string, versionId: string) =>
    request(`${versionPath(skillId, versionId)}/download`) as Promise<{
      url: string;
      expires_in: number;
    }>,

  listGrants: (skillId: string) =>
    request(`${skillPath(skillId)}/grants`) as Promise<{ grants: SkillGrant[] }>,

  replaceGrants: (
    skillId: string,
    payload: {
      subject_type: SkillGrant['subject_type'];
      subject_id: string;
      permissions: SkillGrant['permission'][];
      effect: SkillGrant['effect'];
      expires_at: string | null;
    },
  ) =>
    request(`${skillPath(skillId)}/grants`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  deleteGrant: (skillId: string, grantId: number) =>
    request(`${skillPath(skillId)}/grants/${grantId}`, { method: 'DELETE' }),

  listAgents: async (): Promise<AgentSummary[]> => {
    const result = await request('/api/v2/agent-runtime/profiles') as AgentProfileListResponse;
    return (result.profiles || []).map((profile, index) => ({
      agent_code: profile.code,
      agent_name: profile.name,
      domain: profile.domain,
      description: profile.description,
      is_active: true,
      sort_order: index,
    }));
  },

  listAgentSkills: (agentCode: string) =>
    request(`/agents/${encodeURIComponent(agentCode)}/skills`) as Promise<{
      agent_code: string;
      skills: AgentSkillBinding[];
      compatible_skill_ids: string[];
    }>,

  setAgentSkills: (agentCode: string, skills: AgentSkillBindingInput[]) =>
    request(`/agents/${encodeURIComponent(agentCode)}/skills`, {
      method: 'PUT',
      body: JSON.stringify({ skills }),
    }),
};
