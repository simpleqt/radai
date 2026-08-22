import { request } from '@/utils/request';

export type PromptCategory = 'extraction_schema';
export type PromptContentFormat = 'yaml' | 'text';

export interface PromptCategoryInfo {
  category: PromptCategory;
  label: string;
  description: string;
}

export interface PromptTemplateSummary {
  category: PromptCategory;
  template_id: string;
  label: string;
  subject?: string | null;
  version: string;
  template_version: string;
  content_format: PromptContentFormat;
  description?: string | null;
  metadata: Record<string, any>;
}

export interface PromptTemplateDetail extends PromptTemplateSummary {
  content: string;
}

export interface PromptTemplateCreateInput {
  category: PromptCategory;
  template_id: string;
  label: string;
  subject?: string | null;
  version?: string;
  content?: string | null;
}

export const PromptManagementAPI = {
  categories: () => request('/prompts/categories') as Promise<PromptCategoryInfo[]>,
  templates: (category?: PromptCategory | '') => {
    const qs = category ? `?category=${encodeURIComponent(category)}` : '';
    return request(`/prompts/templates${qs}`) as Promise<PromptTemplateSummary[]>;
  },
  getTemplate: (category: PromptCategory, templateId: string) =>
    request(`/prompts/templates/${encodeURIComponent(category)}/${encodeURIComponent(templateId)}`) as Promise<PromptTemplateDetail>,
  createTemplate: (data: PromptTemplateCreateInput) =>
    request('/prompts/templates', {
      method: 'POST',
      body: JSON.stringify(data),
    }) as Promise<PromptTemplateDetail>,
  updateTemplate: (category: PromptCategory, templateId: string, content: string) =>
    request(`/prompts/templates/${encodeURIComponent(category)}/${encodeURIComponent(templateId)}`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }) as Promise<PromptTemplateDetail>,
};
