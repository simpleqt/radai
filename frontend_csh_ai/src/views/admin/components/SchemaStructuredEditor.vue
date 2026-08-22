<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import {
  AlertTriangle,
  Braces,
  Code2,
  Link2,
  Plus,
  SlidersHorizontal,
  Trash2,
} from 'lucide-vue-next';

type EntityDraft = {
  id: string;
  description: string;
  aliases: string;
};

type RelationDraft = {
  id: string;
  description: string;
  aliases: string;
  allowedSourceTypes: string;
  allowedTargetTypes: string;
  minConfidence: number;
};

type SchemaDraft = {
  schemaId: string;
  label: string;
  version: string;
  subject: string;
  minEntityConfidence: number;
  minRelationConfidence: number;
  candidateConfidence: number;
  entityTypes: EntityDraft[];
  relationTypes: RelationDraft[];
  forbiddenRelations: string;
};

export type SchemaValidationState = {
  valid: boolean;
  errors: string[];
};

const props = defineProps<{
  modelValue: string;
  schemaId: string;
  label: string;
  subject: string;
  version: string;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: string];
  validation: [state: SchemaValidationState];
}>();

const idPattern = /^[a-z][a-z0-9_]{1,63}$/;
const activeTab = ref<'structured' | 'yaml'>('structured');
const rawContent = ref('');
const isHydrating = ref(false);

const draft = reactive<SchemaDraft>({
  schemaId: '',
  label: '',
  version: '1.0',
  subject: '',
  minEntityConfidence: 55,
  minRelationConfidence: 55,
  candidateConfidence: 40,
  entityTypes: [],
  relationTypes: [],
  forbiddenRelations: '',
});

const splitList = (value: string | string[] | undefined): string[] => {
  if (Array.isArray(value)) return value.map(String).map((item) => item.trim()).filter(Boolean);
  return String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
};

const parseInlineList = (value: string): string[] => {
  const text = value.trim();
  if (text === '[]') return [];
  if (!text.startsWith('[') || !text.endsWith(']')) return text ? [stripQuotes(text)] : [];
  const body = text.slice(1, -1).trim();
  if (!body) return [];
  return body.split(',').map((item) => stripQuotes(item.trim())).filter(Boolean);
};

const stripQuotes = (value: string): string => {
  const text = value.trim();
  if ((text.startsWith('"') && text.endsWith('"')) || (text.startsWith("'") && text.endsWith("'"))) {
    return text.slice(1, -1);
  }
  return text;
};

const parseNumber = (value: unknown, fallback: number): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const emptyDraft = (): SchemaDraft => ({
  schemaId: props.schemaId || 'custom_hydrogeology',
  label: props.label || '自定义水文地质',
  version: props.version || '1.0',
  subject: props.subject || 'hydrogeology',
  minEntityConfidence: 55,
  minRelationConfidence: 55,
  candidateConfidence: 40,
  entityTypes: [
    { id: 'term', description: '领域术语、概念或对象', aliases: '' },
    { id: 'parameter', description: '带数值、单位或状态的参数', aliases: '' },
  ],
  relationTypes: [
    {
      id: 'co_occurs',
      description: '同一 chunk 中共同出现的弱关联',
      aliases: '',
      allowedSourceTypes: '',
      allowedTargetTypes: '',
      minConfidence: 50,
    },
  ],
  forbiddenRelations: '',
});

const readField = (line: string): [string, string] | null => {
  const match = line.match(/^([A-Za-z_][\w-]*):\s*(.*)$/);
  if (!match) return null;
  return [match[1], match[2] ?? ''];
};

const parseSchemaYaml = (content: string): SchemaDraft => {
  const parsed = emptyDraft();
  parsed.entityTypes = [];
  parsed.relationTypes = [];

  let section: '' | 'entity_types' | 'relation_types' | 'forbidden_relations' = '';
  let currentEntity: EntityDraft | null = null;
  let currentRelation: RelationDraft | null = null;
  let pendingListField = '';

  const pushPendingValue = (value: string) => {
    if (!pendingListField) return;
    if (currentEntity && pendingListField === 'aliases') {
      currentEntity.aliases = splitList(currentEntity.aliases).concat(stripQuotes(value)).join(', ');
    }
    if (currentRelation) {
      if (pendingListField === 'aliases') {
        currentRelation.aliases = splitList(currentRelation.aliases).concat(stripQuotes(value)).join(', ');
      }
      if (pendingListField === 'allowed_source_types') {
        currentRelation.allowedSourceTypes = splitList(currentRelation.allowedSourceTypes).concat(stripQuotes(value)).join(', ');
      }
      if (pendingListField === 'allowed_target_types') {
        currentRelation.allowedTargetTypes = splitList(currentRelation.allowedTargetTypes).concat(stripQuotes(value)).join(', ');
      }
    }
  };

  for (const rawLine of content.split(/\r?\n/)) {
    if (!rawLine.trim() || rawLine.trim().startsWith('#')) continue;
    const indent = rawLine.match(/^ */)?.[0].length ?? 0;
    const trimmed = rawLine.trim();

    if (trimmed.startsWith('- ')) {
      const value = trimmed.slice(2).trim();
      if (section === 'forbidden_relations') {
        parsed.forbiddenRelations = splitList(parsed.forbiddenRelations).concat(stripQuotes(value)).join(', ');
      } else {
        pushPendingValue(value);
      }
      continue;
    }

    const field = readField(trimmed);
    if (!field) continue;
    const [key, value] = field;

    if (indent === 0) {
      pendingListField = '';
      currentEntity = null;
      currentRelation = null;
      if (key === 'entity_types' || key === 'relation_types' || key === 'forbidden_relations') {
        section = key;
        if (key === 'forbidden_relations') {
          parsed.forbiddenRelations = parseInlineList(value).join(', ');
        }
        continue;
      }
      section = '';
      if (key === 'schema_id') parsed.schemaId = stripQuotes(value);
      if (key === 'label') parsed.label = stripQuotes(value);
      if (key === 'version') parsed.version = stripQuotes(value);
      if (key === 'subject') parsed.subject = stripQuotes(value);
      if (key === 'min_entity_confidence') parsed.minEntityConfidence = parseNumber(value, 55);
      if (key === 'min_relation_confidence') parsed.minRelationConfidence = parseNumber(value, 55);
      if (key === 'candidate_confidence') parsed.candidateConfidence = parseNumber(value, 40);
      continue;
    }

    if (section === 'entity_types' && indent === 2) {
      currentEntity = { id: key, description: '', aliases: '' };
      parsed.entityTypes.push(currentEntity);
      pendingListField = '';
      continue;
    }
    if (section === 'relation_types' && indent === 2) {
      currentRelation = {
        id: key,
        description: '',
        aliases: '',
        allowedSourceTypes: '',
        allowedTargetTypes: '',
        minConfidence: parsed.minRelationConfidence,
      };
      parsed.relationTypes.push(currentRelation);
      pendingListField = '';
      continue;
    }

    if (section === 'entity_types' && currentEntity && indent >= 4) {
      pendingListField = value ? '' : key;
      if (key === 'description') currentEntity.description = stripQuotes(value);
      if (key === 'aliases') currentEntity.aliases = parseInlineList(value).join(', ');
    }
    if (section === 'relation_types' && currentRelation && indent >= 4) {
      pendingListField = value ? '' : key;
      if (key === 'description') currentRelation.description = stripQuotes(value);
      if (key === 'aliases') currentRelation.aliases = parseInlineList(value).join(', ');
      if (key === 'allowed_source_types') currentRelation.allowedSourceTypes = parseInlineList(value).join(', ');
      if (key === 'allowed_target_types') currentRelation.allowedTargetTypes = parseInlineList(value).join(', ');
      if (key === 'min_confidence') currentRelation.minConfidence = parseNumber(value, parsed.minRelationConfidence);
    }
  }

  if (!parsed.entityTypes.length) parsed.entityTypes = emptyDraft().entityTypes;
  if (!parsed.relationTypes.length) parsed.relationTypes = emptyDraft().relationTypes;
  return parsed;
};

const yamlScalar = (value: string): string => {
  const text = String(value || '').trim();
  return text || '""';
};

const yamlList = (value: string): string => {
  const items = splitList(value);
  return `[${items.join(', ')}]`;
};

const serializeDraft = (): string => {
  const schemaId = props.schemaId.trim() || draft.schemaId.trim() || 'custom_hydrogeology';
  const label = props.label.trim() || draft.label.trim() || schemaId;
  const version = props.version.trim() || draft.version.trim() || '1.0';
  const subject = props.subject.trim() || draft.subject.trim() || schemaId;
  const lines: string[] = [
    `schema_id: ${schemaId}`,
    `label: ${yamlScalar(label)}`,
    `version: "${version}"`,
    `subject: ${yamlScalar(subject)}`,
    `min_entity_confidence: ${draft.minEntityConfidence || 0}`,
    `min_relation_confidence: ${draft.minRelationConfidence || 0}`,
    `candidate_confidence: ${draft.candidateConfidence || 0}`,
    '',
    'entity_types:',
  ];

  draft.entityTypes.forEach((entity) => {
    lines.push(`  ${entity.id.trim() || 'unnamed_entity'}:`);
    lines.push(`    description: ${yamlScalar(entity.description)}`);
    if (splitList(entity.aliases).length) lines.push(`    aliases: ${yamlList(entity.aliases)}`);
  });

  lines.push('', 'relation_types:');
  draft.relationTypes.forEach((relation) => {
    lines.push(`  ${relation.id.trim() || 'unnamed_relation'}:`);
    lines.push(`    description: ${yamlScalar(relation.description)}`);
    if (splitList(relation.aliases).length) lines.push(`    aliases: ${yamlList(relation.aliases)}`);
    if (splitList(relation.allowedSourceTypes).length) lines.push(`    allowed_source_types: ${yamlList(relation.allowedSourceTypes)}`);
    if (splitList(relation.allowedTargetTypes).length) lines.push(`    allowed_target_types: ${yamlList(relation.allowedTargetTypes)}`);
    lines.push(`    min_confidence: ${relation.minConfidence || 0}`);
  });

  lines.push('', `forbidden_relations: ${yamlList(draft.forbiddenRelations)}`, '');
  return lines.join('\n');
};

const hydrate = (content: string) => {
  isHydrating.value = true;
  const parsed = parseSchemaYaml(content);
  Object.assign(draft, parsed);
  rawContent.value = content || serializeDraft();
  isHydrating.value = false;
};

const validation = computed<SchemaValidationState>(() => {
  const errors: string[] = [];
  const schemaId = props.schemaId.trim() || draft.schemaId.trim();
  const entityIds = draft.entityTypes.map((item) => item.id.trim()).filter(Boolean);
  const relationIds = draft.relationTypes.map((item) => item.id.trim()).filter(Boolean);
  const duplicateEntity = entityIds.find((id, index) => entityIds.indexOf(id) !== index);
  const duplicateRelation = relationIds.find((id, index) => relationIds.indexOf(id) !== index);

  if (!idPattern.test(schemaId)) errors.push('schema_id 只能包含小写字母、数字、下划线，且必须以字母开头');
  if (!draft.entityTypes.length) errors.push('至少需要一个实体类型');
  if (!draft.relationTypes.length) errors.push('至少需要一个关系类型');
  if (duplicateEntity) errors.push(`实体类型重复：${duplicateEntity}`);
  if (duplicateRelation) errors.push(`关系类型重复：${duplicateRelation}`);

  draft.entityTypes.forEach((entity) => {
    if (!idPattern.test(entity.id.trim())) errors.push(`实体类型 ID 不合法：${entity.id || '空'}`);
    if (!entity.description.trim()) errors.push(`实体类型缺少描述：${entity.id || '空'}`);
  });
  draft.relationTypes.forEach((relation) => {
    if (!idPattern.test(relation.id.trim())) errors.push(`关系类型 ID 不合法：${relation.id || '空'}`);
    if (!relation.description.trim()) errors.push(`关系类型缺少描述：${relation.id || '空'}`);
    if (relation.minConfidence < 0 || relation.minConfidence > 100) errors.push(`关系置信度需在 0-100：${relation.id}`);
    const unknownSources = splitList(relation.allowedSourceTypes).filter((type) => type !== 'any' && !entityIds.includes(type));
    const unknownTargets = splitList(relation.allowedTargetTypes).filter((type) => type !== 'any' && !entityIds.includes(type));
    if (unknownSources.length) errors.push(`${relation.id} 的 source_types 未定义：${unknownSources.join(', ')}`);
    if (unknownTargets.length) errors.push(`${relation.id} 的 target_types 未定义：${unknownTargets.join(', ')}`);
  });
  [
    ['实体阈值', draft.minEntityConfidence],
    ['关系阈值', draft.minRelationConfidence],
    ['候选阈值', draft.candidateConfidence],
  ].forEach(([name, value]) => {
    const num = Number(value);
    if (!Number.isFinite(num) || num < 0 || num > 100) errors.push(`${name}需在 0-100`);
  });

  return { valid: errors.length === 0, errors };
});

const syncStructuredToRaw = () => {
  const next = serializeDraft();
  rawContent.value = next;
  emit('update:modelValue', next);
};

const switchTab = (tab: 'structured' | 'yaml') => {
  if (tab === 'structured' && activeTab.value === 'yaml') hydrate(rawContent.value);
  if (tab === 'yaml' && activeTab.value === 'structured') syncStructuredToRaw();
  activeTab.value = tab;
};

const onRawInput = () => {
  isHydrating.value = true;
  Object.assign(draft, parseSchemaYaml(rawContent.value));
  isHydrating.value = false;
  emit('update:modelValue', rawContent.value);
  emit('validation', validation.value);
};

const addEntity = () => {
  draft.entityTypes.push({ id: 'new_entity', description: '', aliases: '' });
};

const removeEntity = (index: number) => {
  if (draft.entityTypes.length <= 1) return;
  draft.entityTypes.splice(index, 1);
};

const addRelation = () => {
  draft.relationTypes.push({
    id: 'new_relation',
    description: '',
    aliases: '',
    allowedSourceTypes: '',
    allowedTargetTypes: '',
    minConfidence: draft.minRelationConfidence,
  });
};

const removeRelation = (index: number) => {
  if (draft.relationTypes.length <= 1) return;
  draft.relationTypes.splice(index, 1);
};

watch(
  () => props.modelValue,
  (value) => {
    if (value !== rawContent.value) hydrate(value || '');
  },
  { immediate: true },
);

watch(
  () => [props.schemaId, props.label, props.subject, props.version],
  () => {
    if (activeTab.value === 'structured') syncStructuredToRaw();
  },
);

watch(
  draft,
  () => {
    if (isHydrating.value || activeTab.value !== 'structured') return;
    syncStructuredToRaw();
  },
  { deep: true },
);

watch(
  validation,
  (value) => emit('validation', value),
  { immediate: true },
);

const entityCount = computed(() => draft.entityTypes.length);
const relationCount = computed(() => draft.relationTypes.length);
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex rounded-lg border border-border-subtle bg-surface-sunken p-1">
        <button
          type="button"
          @click="switchTab('structured')"
          class="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-bold transition"
          :class="activeTab === 'structured' ? 'bg-surface text-accent shadow-sm' : 'text-text-secondary hover:text-text-primary'"
        >
          <Braces class="h-3.5 w-3.5" />
          <span>结构化</span>
        </button>
        <button
          type="button"
          @click="switchTab('yaml')"
          class="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-bold transition"
          :class="activeTab === 'yaml' ? 'bg-surface text-accent shadow-sm' : 'text-text-secondary hover:text-text-primary'"
        >
          <Code2 class="h-3.5 w-3.5" />
          <span>YAML</span>
        </button>
      </div>
      <div class="flex flex-wrap gap-2 text-[11px] font-bold text-text-secondary">
        <span class="rounded-md bg-surface-sunken px-2 py-1">实体 {{ entityCount }}</span>
        <span class="rounded-md bg-surface-sunken px-2 py-1">关系 {{ relationCount }}</span>
      </div>
    </div>

    <div v-if="validation.errors.length" class="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-200">
      <div class="mb-1 flex items-center gap-1.5">
        <AlertTriangle class="h-3.5 w-3.5" />
        <span>保存前需处理</span>
      </div>
      <div class="space-y-1">
        <p v-for="item in validation.errors" :key="item">{{ item }}</p>
      </div>
    </div>

    <div v-if="activeTab === 'structured'" class="space-y-5">
      <section class="rounded-lg border border-border-subtle bg-surface-sunken p-4">
        <div class="mb-3 flex items-center gap-2 text-sm font-extrabold text-text-primary">
          <SlidersHorizontal class="h-4 w-4 text-accent" />
          <span>阈值</span>
        </div>
        <div class="grid grid-cols-1 gap-3 md:grid-cols-3">
          <label class="block">
            <span class="mb-1.5 ml-1 block text-xs font-bold text-text-tertiary">实体</span>
            <input v-model.number="draft.minEntityConfidence" type="number" min="0" max="100" class="w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
          </label>
          <label class="block">
            <span class="mb-1.5 ml-1 block text-xs font-bold text-text-tertiary">关系</span>
            <input v-model.number="draft.minRelationConfidence" type="number" min="0" max="100" class="w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
          </label>
          <label class="block">
            <span class="mb-1.5 ml-1 block text-xs font-bold text-text-tertiary">候选</span>
            <input v-model.number="draft.candidateConfidence" type="number" min="0" max="100" class="w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
          </label>
        </div>
      </section>

      <section class="rounded-lg border border-border-subtle bg-surface p-4">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div class="text-sm font-extrabold text-text-primary">实体类型</div>
          <button type="button" @click="addEntity" class="flex items-center gap-1.5 rounded-lg bg-surface-sunken px-3 py-1.5 text-xs font-bold text-text-secondary hover:bg-surface-hover">
            <Plus class="h-3.5 w-3.5" />
            <span>新增</span>
          </button>
        </div>
        <div class="space-y-3">
          <div v-for="(entity, index) in draft.entityTypes" :key="index" class="grid grid-cols-1 gap-2 rounded-lg border border-border-subtle bg-surface-sunken p-3 lg:grid-cols-[180px_minmax(0,1fr)_minmax(180px,260px)_36px]">
            <input v-model.trim="entity.id" placeholder="entity_id" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs font-bold text-text-primary outline-none focus:border-accent" />
            <input v-model="entity.description" placeholder="描述" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs text-text-primary outline-none focus:border-accent" />
            <input v-model="entity.aliases" placeholder="别名，逗号分隔" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs text-text-primary outline-none focus:border-accent" />
            <button type="button" :disabled="draft.entityTypes.length <= 1" @click="removeEntity(index)" class="flex h-9 items-center justify-center rounded-lg text-text-tertiary hover:bg-red-50 hover:text-red-600 disabled:opacity-30 dark:hover:bg-red-950/30">
              <Trash2 class="h-4 w-4" />
            </button>
          </div>
        </div>
      </section>

      <section class="rounded-lg border border-border-subtle bg-surface p-4">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div class="flex items-center gap-2 text-sm font-extrabold text-text-primary">
            <Link2 class="h-4 w-4 text-accent" />
            <span>关系类型</span>
          </div>
          <button type="button" @click="addRelation" class="flex items-center gap-1.5 rounded-lg bg-surface-sunken px-3 py-1.5 text-xs font-bold text-text-secondary hover:bg-surface-hover">
            <Plus class="h-3.5 w-3.5" />
            <span>新增</span>
          </button>
        </div>
        <div class="space-y-3">
          <div v-for="(relation, index) in draft.relationTypes" :key="index" class="space-y-2 rounded-lg border border-border-subtle bg-surface-sunken p-3">
            <div class="grid grid-cols-1 gap-2 lg:grid-cols-[180px_minmax(0,1fr)_120px_36px]">
              <input v-model.trim="relation.id" placeholder="relation_id" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs font-bold text-text-primary outline-none focus:border-accent" />
              <input v-model="relation.description" placeholder="描述" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs text-text-primary outline-none focus:border-accent" />
              <input v-model.number="relation.minConfidence" type="number" min="0" max="100" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs text-text-primary outline-none focus:border-accent" />
              <button type="button" :disabled="draft.relationTypes.length <= 1" @click="removeRelation(index)" class="flex h-9 items-center justify-center rounded-lg text-text-tertiary hover:bg-red-50 hover:text-red-600 disabled:opacity-30 dark:hover:bg-red-950/30">
                <Trash2 class="h-4 w-4" />
              </button>
            </div>
            <div class="grid grid-cols-1 gap-2 lg:grid-cols-3">
              <input v-model="relation.allowedSourceTypes" placeholder="source_types" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs text-text-primary outline-none focus:border-accent" />
              <input v-model="relation.allowedTargetTypes" placeholder="target_types" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs text-text-primary outline-none focus:border-accent" />
              <input v-model="relation.aliases" placeholder="别名，逗号分隔" class="rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs text-text-primary outline-none focus:border-accent" />
            </div>
          </div>
        </div>
      </section>

      <section class="rounded-lg border border-border-subtle bg-surface-sunken p-4">
        <label class="block">
          <span class="mb-1.5 ml-1 block text-xs font-bold text-text-tertiary">禁用关系</span>
          <input v-model="draft.forbiddenRelations" placeholder="causality_without_evidence" class="w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm text-text-primary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20" />
        </label>
      </section>
    </div>

    <textarea
      v-else
      v-model="rawContent"
      @input="onRawInput"
      class="min-h-[560px] w-full resize-y rounded-lg border border-border-subtle bg-surface px-4 py-3 font-mono text-xs leading-5 text-text-tertiary outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
      spellcheck="false"
    ></textarea>
  </div>
</template>
