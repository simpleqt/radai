import { request } from '@/utils/request';

export type LearningPlanStatus = 'draft' | 'active' | 'completed' | 'archived';
export type LearningNodeStatus = 'locked' | 'open' | 'learning' | 'done' | 'review';

export interface CustomLearningNode {
  node_id: string;
  plan_id: string;
  parent_node_id?: string | null;
  order_index: number;
  title: string;
  subtitle?: string | null;
  objective?: string | null;
  explanation?: string | null;
  resource?: string | null;
  task?: string | null;
  check?: string | null;
  day?: string | null;
  node_type?: string;
  status: LearningNodeStatus;
  state: LearningNodeStatus;
  mastery: number;
  estimated_minutes: number;
  resources?: unknown[];
  tasks?: string[];
  checks?: string[];
  explore_cache?: Record<string, any>;
  content_status?: {
    explanation_ready: boolean;
    quiz_ready: boolean;
    explore_ready?: boolean;
    detail_ready?: boolean;
    map_ready?: boolean;
  };
  completed_at?: string | null;
}

export interface CustomLearningPlan {
  plan_id: string;
  template_id?: string | null;
  title: string;
  summary?: string | null;
  domain: string;
  goal?: string | null;
  baseline?: string | null;
  depth?: string | null;
  duration_days: number;
  daily_minutes: number;
  kb_ids?: string[];
  status: LearningPlanStatus;
  progress: number;
  mastery: number;
  accent?: string | null;
  surface?: string | null;
  course_type?: string | null;
  current_node_id?: string | null;
  style_applied?: boolean;
  metadata?: Record<string, any>;
  stats?: {
    total_nodes: number;
    done_nodes: number;
    open_nodes: number;
    review_nodes?: number;
    locked_nodes?: number;
  };
  nodes?: CustomLearningNode[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CustomLearningAdjustment {
  adjustment_id: string;
  plan_id: string;
  trigger_type: string;
  reason?: string | null;
  accepted: boolean;
  before_snapshot?: Record<string, any>;
  after_snapshot?: Record<string, any>;
  created_at?: string | null;
}

export interface CustomLearningReview {
  review_nodes: CustomLearningNode[];
  weak_points: string[];
  latest_attempts: any[];
  adjustments: CustomLearningAdjustment[];
}

export interface CustomLearningStyleSummary {
  enabled: boolean;
  dimensions?: Record<string, string>;
  labels?: string[];
  task_hints?: string[];
  explanation_hint?: string;
  profile_updated_at?: string | null;
}

export interface CustomLearningModuleEffect {
  id: 'custom_path' | 'explore' | 'learning_style' | 'practice' | 'response_speed' | string;
  title: string;
  status: 'active' | 'ready' | 'waiting' | 'risk' | 'idle' | string;
  metric: string;
  description: string;
  impact?: string;
}

export interface CustomLearningDashboard {
  plan: CustomLearningPlan;
  current_node?: CustomLearningNode | null;
  review: CustomLearningReview;
  style: CustomLearningStyleSummary;
  agent_trace?: any[];
  module_effects?: CustomLearningModuleEffect[];
  next_recommendation?: {
    action: string;
    label: string;
    reason?: string;
    node_id?: string;
  };
  action?: string;
  result?: Record<string, any>;
}

export interface CustomLearningWorkspace {
  plans: CustomLearningPlan[];
  recommended_plan?: CustomLearningPlan | null;
  draft_plans: CustomLearningPlan[];
  active_plans: CustomLearningPlan[];
  completed_plans: CustomLearningPlan[];
  stats: {
    total: number;
    draft: number;
    active: number;
    completed: number;
    average_progress: number;
  };
  style: CustomLearningStyleSummary;
  module_effects?: CustomLearningModuleEffect[];
  knowledge_bases: CustomLearningKnowledgeBase[];
  agent_code: string;
}

export interface CreateCustomLearningPlanPayload {
  template_id?: string;
  title: string;
  subtitle?: string;
  summary?: string;
  domain?: string;
  goal?: string;
  baseline?: string;
  duration?: string;
  duration_days?: number;
  daily_minutes?: number;
  depth?: string;
  course_type?: string;
  accent?: string;
  surface?: string;
  soft?: string;
  kb_ids?: string[];
  objectives?: unknown[];
  resources?: unknown[];
  projects?: unknown[];
  nodes: unknown[];
  weekly_plan?: string[];
}

export interface GenerateCustomLearningPlanPayload {
  goal: string;
  domain?: string;
  baseline?: string;
  depth?: string;
  duration_days?: number;
  daily_minutes?: number;
  kb_ids?: string[];
  use_learning_style?: boolean;
  explore_snapshot?: Record<string, any> | null;
}

export interface UpdateCustomLearningPlanPayload {
  title?: string;
  goal?: string;
  baseline?: string;
  depth?: string;
  duration_days?: number;
  daily_minutes?: number;
  kb_ids?: string[];
}

export interface AdjustCustomLearningPlanPayload {
  trigger_type?: string;
  reason?: string;
  accept?: boolean;
}

export interface NextCustomLearningActionPayload {
  node_id?: string;
  answers?: Record<string, any>;
  duration_seconds?: number;
  prefer_materials?: boolean;
}

export interface CustomLearningKnowledgeBase {
  kb_id: string;
  kb_name: string;
}

export const CustomLearningAPI = {
  knowledgeBases(params: { agent_code?: string; domain?: string } = {}): Promise<{ items: CustomLearningKnowledgeBase[]; agent_code: string }> {
    const qs = new URLSearchParams();
    if (params.agent_code) qs.set('agent_code', params.agent_code);
    if (params.domain) qs.set('domain', params.domain);
    const query = qs.toString();
    return request(`/custom-learning/knowledge-bases${query ? `?${query}` : ''}`);
  },

  plans(): Promise<{ plans: CustomLearningPlan[] }> {
    return request('/custom-learning/plans');
  },

  workspace(params: { domain?: string; agent_code?: string } = {}): Promise<CustomLearningWorkspace> {
    const qs = new URLSearchParams();
    if (params.domain) qs.set('domain', params.domain);
    if (params.agent_code) qs.set('agent_code', params.agent_code);
    const query = qs.toString();
    return request(`/custom-learning/workspace${query ? `?${query}` : ''}`);
  },

  createPlan(payload: CreateCustomLearningPlanPayload): Promise<{ plan: CustomLearningPlan }> {
    return request('/custom-learning/plans', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  generatePlan(payload: GenerateCustomLearningPlanPayload): Promise<{ plan: CustomLearningPlan }> {
    return request('/custom-learning/plans/generate', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  plan(planId: string): Promise<{ plan: CustomLearningPlan }> {
    return request(`/custom-learning/plans/${planId}`);
  },

  dashboard(planId: string, nodeId?: string): Promise<CustomLearningDashboard> {
    const qs = new URLSearchParams();
    if (nodeId) qs.set('node_id', nodeId);
    const query = qs.toString();
    return request(`/custom-learning/plans/${planId}/dashboard${query ? `?${query}` : ''}`);
  },

  updatePlan(planId: string, payload: UpdateCustomLearningPlanPayload): Promise<{ plan: CustomLearningPlan }> {
    return request(`/custom-learning/plans/${planId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
  },

  activatePlan(planId: string): Promise<{ plan: CustomLearningPlan }> {
    return request(`/custom-learning/plans/${planId}/activate`, { method: 'POST' });
  },

  nextAction(planId: string, payload: NextCustomLearningActionPayload = {}): Promise<CustomLearningDashboard> {
    return request(`/custom-learning/plans/${planId}/next-action`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  nodes(planId: string): Promise<{ nodes: CustomLearningNode[] }> {
    return request(`/custom-learning/plans/${planId}/nodes`);
  },

  node(nodeId: string, params: { include_quiz?: boolean } = {}): Promise<{ node: CustomLearningNode; content_status: CustomLearningNode['content_status']; questions?: any[] }> {
    const qs = new URLSearchParams();
    if (params.include_quiz) qs.set('include_quiz', 'true');
    const query = qs.toString();
    return request(`/custom-learning/nodes/${nodeId}${query ? `?${query}` : ''}`);
  },

  startNode(nodeId: string): Promise<{ plan: CustomLearningPlan; node: CustomLearningNode }> {
    return request(`/custom-learning/nodes/${nodeId}/start`, { method: 'POST' });
  },

  completeNode(nodeId: string, score?: number | null, payload: Record<string, any> = {}): Promise<{ plan: CustomLearningPlan; node: CustomLearningNode; unlocked_node_ids: string[]; adjustment?: CustomLearningAdjustment | null; attempt?: { score: number; diagnosis: Record<string, any>; mastery_before: number; mastery_after: number } | null }> {
    return request(`/custom-learning/nodes/${nodeId}/complete`, {
      method: 'POST',
      body: JSON.stringify({ score, payload }),
    });
  },

  quiz(nodeId: string, options: { force?: boolean } = {}): Promise<{ questions: any[] }> {
    return request(`/custom-learning/nodes/${nodeId}/quiz`, {
      method: 'POST',
      body: JSON.stringify(options),
    });
  },

  gradeQuiz(
    nodeId: string,
    answers: Record<string, any>,
    durationSeconds?: number,
  ): Promise<{ attempt: any; plan: CustomLearningPlan; node: CustomLearningNode; unlocked_node_ids: string[]; adjustment?: CustomLearningAdjustment | null }> {
    return request(`/custom-learning/nodes/${nodeId}/quiz/grade`, {
      method: 'POST',
      body: JSON.stringify({ answers, duration_seconds: durationSeconds }),
    });
  },

  exploreNode(nodeId: string): Promise<{ map: any; resources: any[]; node: CustomLearningNode }> {
    return request(`/custom-learning/nodes/${nodeId}/explore`, { method: 'POST' });
  },

  enrichNode(nodeId: string, force = false): Promise<{ plan: CustomLearningPlan; node: CustomLearningNode }> {
    return request(`/custom-learning/nodes/${nodeId}/enrich`, {
      method: 'POST',
      body: JSON.stringify({ force }),
    });
  },

  adjustPlan(planId: string, payload: AdjustCustomLearningPlanPayload = {}): Promise<{ plan: CustomLearningPlan; adjustment: CustomLearningAdjustment }> {
    return request(`/custom-learning/plans/${planId}/adjust`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  reviewPlan(planId: string): Promise<{ plan: CustomLearningPlan; review_nodes: CustomLearningNode[]; weak_points: string[]; latest_attempts: any[]; adjustments: CustomLearningAdjustment[] }> {
    return request(`/custom-learning/plans/${planId}/review`);
  },

  archivePlan(planId: string): Promise<{ plan: CustomLearningPlan }> {
    return request(`/custom-learning/plans/${planId}/archive`, { method: 'POST' });
  },
};
