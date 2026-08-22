import { request } from '@/utils/request';

export type LearningStyleOptionKey = 'a' | 'b';

export interface LearningStyleDimension {
  id: string;
  title: string;
  title_en: string;
  left: string;
  right: string;
  description: string;
  left_summary: string;
  right_summary: string;
  left_tips: string[];
  right_tips: string[];
}

export interface LearningStyleQuestion {
  id: string;
  dimension: string;
  text: string;
  options: Array<{ key: LearningStyleOptionKey; text: string }>;
}

export interface LearningStyleQuestionnaire {
  version: string;
  question_count: number;
  dimensions: LearningStyleDimension[];
  questions: LearningStyleQuestion[];
}

export interface LearningStyleDimensionResult {
  dimension_id: string;
  title: string;
  title_en: string;
  left: string;
  right: string;
  score: number;
  left_count: number;
  right_count: number;
  preferred_side: 'left' | 'right';
  preferred_label: string;
  strength: 'balanced' | 'moderate' | 'strong';
  summary: string;
  tips: string[];
}

export interface LearningStyleResult {
  questionnaire_version: string;
  scores: Record<string, number>;
  dimensions: LearningStyleDimensionResult[];
  disclaimer: string;
}

export interface LearningStyleAttempt {
  attempt_id: string;
  questionnaire_version: string;
  status: 'in_progress' | 'completed';
  answers: Record<string, LearningStyleOptionKey>;
  scores?: Record<string, number> | null;
  result?: LearningStyleResult | null;
  current_dimension: number;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface LearningStyleProfile {
  user_id: string;
  latest_attempt_id: string;
  scores: Record<string, number>;
  result: LearningStyleResult;
  apply_to_ai: boolean;
  updated_at?: string | null;
}

export const LearningStyleAPI = {
  questionnaire(): Promise<LearningStyleQuestionnaire> {
    return request('/learning-style/questionnaire');
  },

  currentAttempt(): Promise<{ attempt: LearningStyleAttempt | null }> {
    return request('/learning-style/attempts/current');
  },

  createAttempt(): Promise<{ attempt: LearningStyleAttempt }> {
    return request('/learning-style/attempts', { method: 'POST' });
  },

  saveAnswers(
    attemptId: string,
    answers: Record<string, LearningStyleOptionKey>,
    currentDimension: number,
  ): Promise<{ attempt: LearningStyleAttempt }> {
    return request(`/learning-style/attempts/${attemptId}/answers`, {
      method: 'PUT',
      body: JSON.stringify({ answers, current_dimension: currentDimension }),
    });
  },

  submitAttempt(attemptId: string): Promise<{ attempt: LearningStyleAttempt; profile: LearningStyleProfile | null }> {
    return request(`/learning-style/attempts/${attemptId}/submit`, { method: 'POST' });
  },

  profile(): Promise<{ profile: LearningStyleProfile | null }> {
    return request('/learning-style/profile');
  },

  updatePreferences(applyToAi: boolean): Promise<{ profile: LearningStyleProfile }> {
    return request('/learning-style/profile/preferences', {
      method: 'PATCH',
      body: JSON.stringify({ apply_to_ai: applyToAi }),
    });
  },
};
