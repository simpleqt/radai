import { request } from '@/utils/request';

export interface LearningStyleProfile {
  user_id: string;
  latest_attempt_id: string;
  scores: Record<string, number>;
  result: Record<string, any>;
  apply_to_ai: boolean;
  updated_at?: string | null;
}

export const LearningStyleAPI = {
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
