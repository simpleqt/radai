<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  ArrowLeft,
  BadgeCheck,
  Beaker,
  BookOpenText,
  BrainCircuit,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  Circle,
  ClipboardCheck,
  Clock3,
  Compass,
  Database,
  FileText,
  FlaskConical,
  Layers3,
  Lightbulb,
  ListChecks,
  Loader2,
  Lock,
  Map,
  Pencil,
  Play,
  Plus,
  Route,
  Save,
  Target,
  Timer,
  Trash2,
  Video,
  WandSparkles,
  X,
} from 'lucide-vue-next';
import AppShell from '@/components/layout/AppShell.vue';
import { PLAN_TEMPLATES, type LearningPlan, type LearningResource, type NodeStatus } from './templates';
import { CustomLearningAPI, type CustomLearningKnowledgeBase, type CustomLearningModuleEffect, type CustomLearningPlan, type CustomLearningStyleSummary } from '@/api/custom-learning';
import { LearningStyleAPI, type LearningStyleProfile } from '@/api/learning-style';
import { authState } from '@/utils/request';

type ScreenMode = 'list' | 'detail';

const plans: LearningPlan[] = PLAN_TEMPLATES;

const route = useRoute();
const router = useRouter();
const mode = ref<ScreenMode>('list');
const selectedPlanId = ref('detector-principles');
const activeObjectiveIndex = ref(0);
const savedPlans = ref<CustomLearningPlan[]>([]);
const plansLoading = ref(false);
const plansError = ref('');
const savingPlanId = ref('');
const generatedGoal = ref('');
const generatedDays = ref(7);
const generatedMinutes = ref(90);
const generatedBaseline = ref('unknown');
const generatedDepth = ref('analysis');
const useLearningStyle = ref(true);
const generatingPlan = ref(false);
const goalInputRef = ref<HTMLTextAreaElement | null>(null);
const availableKbs = ref<CustomLearningKnowledgeBase[]>([]);
const selectedKbIds = ref<string[]>([]);
const kbLoading = ref(false);
const learningStyleProfile = ref<LearningStyleProfile | null>(null);
const workspaceStyleSummary = ref<CustomLearningStyleSummary | null>(null);
const learningStyleLoading = ref(false);
const planPreview = ref<CustomLearningPlan | null>(null);
const planPreviewRef = ref<HTMLElement | null>(null);
// 已有个人计划时模板区默认收起，避免与真实数据抢占首屏。
const templatesExpanded = ref(true);
const activatingPreview = ref(false);
// 归档的两步内联确认（替代原生 window.confirm）。
const archiveConfirmId = ref('');
let archiveConfirmTimer: number | null = null;
const workspaceModuleEffects = ref<CustomLearningModuleEffect[]>([]);
const exploreSnapshot = ref<Record<string, any> | null>(null);
const editingPlanId = ref('');
const editSavingPlanId = ref('');
const archivingPlanId = ref('');
const editForm = ref({
  title: '',
  goal: '',
  baseline: 'unknown',
  depth: 'analysis',
  duration_days: 7,
  daily_minutes: 90,
  kb_ids: [] as string[],
});

const learningAgentCode = 'ne';
const CUSTOM_LEARNING_EXPLORE_SNAPSHOT_KEY = 'custom_learning_explore_snapshot_v1';


const selectedPlan = computed(() => plans.find((plan) => plan.id === selectedPlanId.value) || plans[0]);
const activeObjective = computed(() => selectedPlan.value.objectives[activeObjectiveIndex.value] || selectedPlan.value.objectives[0]);
const isAuthenticated = computed(() => !!authState.value.token);

const totals = computed(() => ({
  plans: plans.length,
  // 只统计模板中真实存在的条目，不再使用虚构数字。
  resources: plans.reduce((sum, plan) => sum + plan.resources.length, 0),
  projects: plans.reduce((sum, plan) => sum + plan.projects.length, 0),
  openNodes: plans.reduce((sum, plan) => sum + plan.nodes.filter((node) => node.status === 'open').length, 0),
}));

const quickGoals = [
  '7 天掌握前置放大器原理，能画出电荷灵敏前放链路并做参数估算',
  '系统学习半导体探测器和低噪声读出，完成一份能谱系统方案',
  '从零补齐数字信号处理基础，能解释采样、FFT 和数字滤波',
  '围绕核衰变规律做一次计算训练，能完成活度、本底和半衰期分析',
];

const baselineOptions = [
  { value: 'unknown', label: '不确定', hint: '先从诊断和概念边界开始' },
  { value: 'beginner', label: '入门', hint: '多放案例和图示' },
  { value: 'intermediate', label: '有基础', hint: '保留推导和练习密度' },
  { value: 'advanced', label: '进阶', hint: '偏工程方案和薄弱点突破' },
];

const depthOptions = [
  { value: 'foundation', label: '打基础' },
  { value: 'analysis', label: '会分析' },
  { value: 'project', label: '做项目' },
];

const currentBaselineLabel = computed(() => baselineOptions.find((item) => item.value === generatedBaseline.value)?.label || '不确定');
const currentDepthLabel = computed(() => depthOptions.find((item) => item.value === generatedDepth.value)?.label || '会分析');
const selectedKbNames = computed(() => {
  const names = selectedKbIds.value
    .map((id) => availableKbs.value.find((kb) => kb.kb_id === id)?.kb_name)
    .filter(Boolean) as string[];
  return names.length ? names.join('、') : '未选择知识库';
});
const exploreSnapshotSummary = computed(() => {
  if (!exploreSnapshot.value) return '';
  const title = String(exploreSnapshot.value.node_title || '').trim();
  const detailReady = Boolean(exploreSnapshot.value.detail?.content);
  const resourceCount = Array.isArray(exploreSnapshot.value.resources) ? exploreSnapshot.value.resources.length : 0;
  const questionCount = Array.isArray(exploreSnapshot.value.questions) ? exploreSnapshot.value.questions.length : 0;
  const parts = [
    title ? `主题“${title}”` : '探索节点',
    detailReady ? '详解' : '',
    resourceCount ? `${resourceCount} 份资料` : '',
    questionCount ? `${questionCount} 道习题` : '',
  ].filter(Boolean);
  return `将继承探索式学习快照：${parts.join('、')}`;
});
const learningStyleState = computed(() => {
  if (!isAuthenticated.value) return { label: '登录后可启用', tone: 'slate', summary: '当前会按默认学习策略生成计划。' };
  if (learningStyleLoading.value) return { label: '读取中', tone: 'slate', summary: '正在读取你的学习风格画像。' };
  if (workspaceStyleSummary.value?.enabled) {
    const labels = (workspaceStyleSummary.value.labels || []).filter(Boolean).slice(0, 4);
    return {
      label: '已应用',
      tone: 'emerald',
      summary: labels.length ? `将参考：${labels.join('、')}` : workspaceStyleSummary.value.explanation_hint || '将参考你的学习偏好安排图表、练习和复盘。',
    };
  }
  if (!learningStyleProfile.value) return { label: '未测评', tone: 'amber', summary: '可以先生成计划，也可以测评后让任务更贴合偏好。' };
  if (!learningStyleProfile.value.apply_to_ai) return { label: '已测评未启用', tone: 'amber', summary: '结果已保存，开启后会影响任务形态和解释方式。' };
  const labels = (learningStyleProfile.value.result?.dimensions || [])
    .map((item) => item.preferred_label)
    .filter(Boolean)
    .slice(0, 4);
  return {
    label: '已应用',
    tone: 'emerald',
    summary: labels.length ? `将参考：${labels.join('、')}` : '将参考你的学习偏好安排图表、练习和复盘。',
  };
});
const currentPlanSuggestion = computed(() => {
  const active = savedPlans.value.filter((plan) => plan.status === 'active');
  const withReview = active.filter((plan) => (plan.stats?.review_nodes ?? 0) > 0);
  const pool = withReview.length ? withReview : active;
  return pool
    .slice()
    .sort((a, b) => Number(b.updated_at ? Date.parse(b.updated_at) : 0) - Number(a.updated_at ? Date.parse(a.updated_at) : 0))[0] || null;
});

const reviewWorkspaceSummary = computed(() => {
  const plansWithReview = savedPlans.value.filter((plan) => (plan.stats?.review_nodes ?? 0) > 0);
  const totalReviewNodes = plansWithReview.reduce((sum, plan) => sum + Number(plan.stats?.review_nodes ?? 0), 0);
  return {
    plansWithReview,
    totalReviewNodes,
    planCount: plansWithReview.length,
  };
});

function moduleEffectIcon(id: string) {
  if (id === 'explore') return Compass;
  if (id === 'learning_style') return BrainCircuit;
  if (id === 'practice') return ClipboardCheck;
  if (id === 'response_speed') return Timer;
  return Route;
}

function moduleEffectToneClass(status: string) {
  if (status === 'risk') return 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200';
  if (status === 'active') return 'border-blue-100 bg-blue-50 text-blue-800 dark:border-blue-900/50 dark:bg-blue-950/20 dark:text-blue-200';
  if (status === 'ready') return 'border-emerald-100 bg-emerald-50 text-emerald-800 dark:border-emerald-900/50 dark:bg-emerald-950/20 dark:text-emerald-200';
  return 'border-border-subtle bg-surface text-text-secondary';
}

const savedPlanStats = computed(() => {
  const active = savedPlans.value.filter((plan) => plan.status === 'active').length;
  const completed = savedPlans.value.filter((plan) => plan.status === 'completed').length;
  const averageProgress = savedPlans.value.length
    ? Math.round(savedPlans.value.reduce((sum, plan) => sum + (plan.progress || 0), 0) / savedPlans.value.length)
    : 0;
  return { active, completed, averageProgress };
});

function featureRoute(path: 'explore' | 'learning-style') {
  const prefix = '';
  return `${prefix}/${path}`;
}

function openExplore() {
  router.push(featureRoute('explore'));
}

function openLearningStyle() {
  router.push(featureRoute('learning-style'));
}

function applyQuickGoal(goal: string) {
  generatedGoal.value = goal;
  focusGoalInput();
}

function openPlan(planId: string) {
  selectedPlanId.value = planId;
  activeObjectiveIndex.value = 0;
  mode.value = 'detail';
}

function openSavedPlan(planId: string) {
  router.push(`/custom-learning/path/${planId}`);
}

function statusLabel(status: CustomLearningPlan['status']) {
  if (status === 'completed') return '已完成';
  if (status === 'draft') return '草稿';
  if (status === 'archived') return '已归档';
  return '进行中';
}

function openSavedPlanCard(plan: CustomLearningPlan) {
  // 草稿与进行中一致：进入路径页查看；路径页对 draft 已支持「确认并开始计划」
  openSavedPlan(plan.plan_id);
}

function readExploreSnapshot() {
  if (String(route.query.snapshot || '') !== '1' && String(route.query.from || '') !== 'explore') return;
  try {
    const raw = sessionStorage.getItem(CUSTOM_LEARNING_EXPLORE_SNAPSHOT_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!parsed || parsed.source !== 'explore') return;
    exploreSnapshot.value = parsed;
  } catch {
    exploreSnapshot.value = null;
  }
}

function clearExploreSnapshot() {
  exploreSnapshot.value = null;
  try {
    sessionStorage.removeItem(CUSTOM_LEARNING_EXPLORE_SNAPSHOT_KEY);
  } catch {
    // ignore
  }
}

function hydrateGeneratorFromPlan(plan: CustomLearningPlan) {
  generatedGoal.value = String(plan.goal || plan.summary || plan.title || '').trim();
  generatedBaseline.value = plan.baseline || 'unknown';
  generatedDepth.value = plan.depth || 'analysis';
  generatedDays.value = Number(plan.duration_days || 7);
  generatedMinutes.value = Number(plan.daily_minutes || 90);
  selectedKbIds.value = Array.isArray(plan.kb_ids) ? [...plan.kb_ids] : [];
}

function startEditPlan(plan: CustomLearningPlan) {
  editingPlanId.value = plan.plan_id;
  editForm.value = {
    title: plan.title || '',
    goal: plan.goal || plan.summary || '',
    baseline: plan.baseline || 'unknown',
    depth: plan.depth || 'analysis',
    duration_days: Number(plan.duration_days || 7),
    daily_minutes: Number(plan.daily_minutes || 90),
    kb_ids: Array.isArray(plan.kb_ids) ? [...plan.kb_ids] : [],
  };
}

function cancelEditPlan() {
  editingPlanId.value = '';
}

function toggleEditKnowledgeBase(kbId: string) {
  editForm.value.kb_ids = editForm.value.kb_ids.includes(kbId)
    ? editForm.value.kb_ids.filter((id) => id !== kbId)
    : [...editForm.value.kb_ids, kbId];
}

async function savePlanEdits(plan: CustomLearningPlan) {
  const title = editForm.value.title.trim();
  if (!title) {
    plansError.value = '计划标题不能为空';
    return;
  }
  editSavingPlanId.value = plan.plan_id;
  plansError.value = '';
  try {
    const response = await CustomLearningAPI.updatePlan(plan.plan_id, {
      title,
      goal: editForm.value.goal.trim(),
      baseline: editForm.value.baseline,
      depth: editForm.value.depth,
      duration_days: Number(editForm.value.duration_days || 7),
      daily_minutes: Number(editForm.value.daily_minutes || 90),
      kb_ids: editForm.value.kb_ids,
    });
    savedPlans.value = savedPlans.value.map((item) => (item.plan_id === response.plan.plan_id ? response.plan : item));
    if (planPreview.value?.plan_id === response.plan.plan_id) planPreview.value = response.plan;
    editingPlanId.value = '';
  } catch (error) {
    plansError.value = error instanceof Error ? error.message : '计划编辑失败';
  } finally {
    editSavingPlanId.value = '';
  }
}

function cancelArchiveConfirm() {
  archiveConfirmId.value = '';
  if (archiveConfirmTimer !== null) {
    window.clearTimeout(archiveConfirmTimer);
    archiveConfirmTimer = null;
  }
}

async function archiveSavedPlan(plan: CustomLearningPlan) {
  // 两步内联确认：第一次点击进入确认态，5 秒内再次点击才执行归档。
  if (archiveConfirmId.value !== plan.plan_id) {
    archiveConfirmId.value = plan.plan_id;
    if (archiveConfirmTimer !== null) window.clearTimeout(archiveConfirmTimer);
    archiveConfirmTimer = window.setTimeout(() => {
      archiveConfirmId.value = '';
      archiveConfirmTimer = null;
    }, 5000);
    return;
  }
  cancelArchiveConfirm();
  archivingPlanId.value = plan.plan_id;
  plansError.value = '';
  try {
    await CustomLearningAPI.archivePlan(plan.plan_id);
    savedPlans.value = savedPlans.value.filter((item) => item.plan_id !== plan.plan_id);
    if (planPreview.value?.plan_id === plan.plan_id) planPreview.value = null;
  } catch (error) {
    plansError.value = error instanceof Error ? error.message : '计划归档失败';
  } finally {
    archivingPlanId.value = '';
  }
}

function scrollPreviewIntoView() {
  void nextTick(() => {
    planPreviewRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function durationDays(duration: string) {
  const match = String(duration || '').match(/\d+/);
  return match ? Number(match[0]) : 7;
}

function toCreatePayload(plan: LearningPlan) {
  return {
    template_id: plan.id,
    title: plan.title,
    subtitle: plan.subtitle,
    summary: plan.summary,
    domain: 'ne',
    goal: plan.subtitle,
    duration: plan.duration,
    duration_days: durationDays(plan.duration),
    daily_minutes: 90,
    depth: plan.depth,
    course_type: plan.courseType,
    accent: plan.accent,
    surface: plan.soft,
    soft: plan.soft,
    objectives: plan.objectives,
    resources: plan.resources,
    projects: plan.projects,
    nodes: plan.nodes.map((node, index) => ({
      id: `${plan.id}-${index + 1}`,
      title: node.title,
      label: node.label,
      state: index === 0 ? 'open' : node.status,
      subtitle: plan.objectives[index]?.description || node.title,
      resource: plan.resources[index % Math.max(plan.resources.length, 1)]?.title || '',
      task: plan.projects[index % Math.max(plan.projects.length, 1)]?.title || '',
      check: plan.objectives[index]?.checkpoints?.[0] || '',
      day: `Day ${index + 1}`,
    })),
    weekly_plan: plan.weeklyPlan,
  };
}

async function loadSavedPlans() {
  if (!isAuthenticated.value) return;
  plansLoading.value = true;
  plansError.value = '';
  try {
    const response = await CustomLearningAPI.plans();
    savedPlans.value = response.plans;
  } catch (error) {
    plansError.value = error instanceof Error ? error.message : '学习计划加载失败';
  } finally {
    plansLoading.value = false;
  }
}

async function loadLearningWorkspace() {
  if (!isAuthenticated.value) return false;
  plansLoading.value = true;
  kbLoading.value = true;
  learningStyleLoading.value = true;
  plansError.value = '';
  try {
    const response = await CustomLearningAPI.workspace({ agent_code: learningAgentCode, domain: 'ne' });
    savedPlans.value = response.plans || [];
    availableKbs.value = response.knowledge_bases || [];
    workspaceStyleSummary.value = response.style || null;
    workspaceModuleEffects.value = response.module_effects || [];
    useLearningStyle.value = Boolean(response.style?.enabled);
    templatesExpanded.value = savedPlans.value.length === 0;
    const allowedIds = new Set(availableKbs.value.map((kb) => kb.kb_id));
    selectedKbIds.value = selectedKbIds.value.filter((id) => allowedIds.has(id));
    if (!selectedKbIds.value.length) {
      selectedKbIds.value = availableKbs.value.slice(0, 1).map((kb) => kb.kb_id);
    }
    return true;
  } catch (error) {
    plansError.value = error instanceof Error ? error.message : '学习工作台加载失败';
    return false;
  } finally {
    plansLoading.value = false;
    kbLoading.value = false;
    learningStyleLoading.value = false;
  }
}

async function loadKnowledgeBases() {
  if (!isAuthenticated.value) return;
  kbLoading.value = true;
  try {
    const response = await CustomLearningAPI.knowledgeBases({ agent_code: learningAgentCode, domain: 'ne' });
    availableKbs.value = response.items || [];
    const allowedIds = new Set(availableKbs.value.map((kb) => kb.kb_id));
    const seeded = selectedKbIds.value.filter((id) => allowedIds.has(id));
    selectedKbIds.value = seeded.length
      ? seeded
      : availableKbs.value.slice(0, 1).map((kb) => kb.kb_id);
  } catch {
    availableKbs.value = [];
    selectedKbIds.value = [];
  } finally {
    kbLoading.value = false;
  }
}

async function loadLearningStyleProfile() {
  if (!isAuthenticated.value) return;
  learningStyleLoading.value = true;
  try {
    const response = await LearningStyleAPI.profile();
    learningStyleProfile.value = response.profile;
    useLearningStyle.value = Boolean(response.profile?.apply_to_ai);
  } catch {
    learningStyleProfile.value = null;
  } finally {
    learningStyleLoading.value = false;
  }
}

function toggleKnowledgeBase(kbId: string) {
  selectedKbIds.value = selectedKbIds.value.includes(kbId)
    ? selectedKbIds.value.filter((id) => id !== kbId)
    : [...selectedKbIds.value, kbId];
}

async function createPlanFromTemplate(plan: LearningPlan) {
  if (!isAuthenticated.value) {
    router.push({ path: '/login', query: { redirect: route.fullPath } });
    return;
  }
  savingPlanId.value = plan.id;
  plansError.value = '';
  try {
    const response = await CustomLearningAPI.createPlan(toCreatePayload(plan));
    savedPlans.value = [response.plan, ...savedPlans.value.filter((item) => item.plan_id !== response.plan.plan_id)];
    planPreview.value = response.plan;
    mode.value = 'list';
    scrollPreviewIntoView();
  } catch (error) {
    plansError.value = error instanceof Error ? error.message : '计划创建失败';
  } finally {
    savingPlanId.value = '';
  }
}

async function generatePlanFromGoal() {
  if (!isAuthenticated.value) {
    router.push({ path: '/login', query: { redirect: route.fullPath } });
    return;
  }
  const goal = generatedGoal.value.trim();
  if (!goal) {
    plansError.value = '请输入学习目标';
    return;
  }
  generatingPlan.value = true;
  plansError.value = '';
  try {
    const response = await CustomLearningAPI.generatePlan({
      goal,
      domain: 'ne',
      baseline: generatedBaseline.value,
      depth: generatedDepth.value,
      duration_days: generatedDays.value,
      daily_minutes: generatedMinutes.value,
      kb_ids: selectedKbIds.value,
      use_learning_style: useLearningStyle.value,
      explore_snapshot: exploreSnapshot.value,
    });
    savedPlans.value = [response.plan, ...savedPlans.value.filter((item) => item.plan_id !== response.plan.plan_id)];
    generatedGoal.value = '';
    planPreview.value = response.plan;
    scrollPreviewIntoView();
    if (exploreSnapshot.value) clearExploreSnapshot();
  } catch (error) {
    plansError.value = error instanceof Error ? error.message : '计划生成失败';
  } finally {
    generatingPlan.value = false;
  }
}

async function confirmPreviewPlan() {
  if (!planPreview.value) return;
  activatingPreview.value = true;
  plansError.value = '';
  try {
    const response = await CustomLearningAPI.activatePlan(planPreview.value.plan_id);
    savedPlans.value = [response.plan, ...savedPlans.value.filter((item) => item.plan_id !== response.plan.plan_id)];
    planPreview.value = null;
    openSavedPlan(response.plan.plan_id);
  } catch (error) {
    plansError.value = error instanceof Error ? error.message : '计划启用失败';
  } finally {
    activatingPreview.value = false;
  }
}

function closePlanPreview() {
  if (planPreview.value) {
    hydrateGeneratorFromPlan(planPreview.value);
    focusGoalInput();
  }
  planPreview.value = null;
}

function switchPlan(planId: string) {
  selectedPlanId.value = planId;
  activeObjectiveIndex.value = 0;
}

function backToList() {
  mode.value = 'list';
}

function backToHome() {
  router.push('/app');
}

function focusGoalInput() {
  mode.value = 'list';
  window.setTimeout(() => goalInputRef.value?.focus(), 0);
}

function openPath(planId = selectedPlan.value.id) {
  router.push(`/custom-learning/path/${planId}`);
}

function nodeIcon(status: NodeStatus) {
  if (status === 'done') return CheckCircle2;
  if (status === 'locked') return Lock;
  return Circle;
}

function resourceIcon(type: LearningResource['type']) {
  if (type === '视频') return Video;
  if (type === '数据') return ClipboardCheck;
  if (type === '讲义') return FileText;
  return BookOpenText;
}

onMounted(async () => {
  readExploreSnapshot();
  const seedGoal = String(route.query.goal || route.query.topic || '').trim();
  if (seedGoal && !generatedGoal.value) {
    generatedGoal.value = seedGoal;
  }
  const seedIntent = String(route.query.intent || '').trim();
  if (seedIntent === 'prerequisite') {
    generatedDepth.value = 'foundation';
  } else if (seedIntent === 'review') {
    generatedBaseline.value = 'intermediate';
    generatedDepth.value = 'analysis';
  }
  const seedKbIds = String(route.query.kb_ids || '').split(',').map((item) => item.trim()).filter(Boolean);
  if (seedKbIds.length) {
    selectedKbIds.value = Array.from(new Set(seedKbIds));
  }
  const loaded = await loadLearningWorkspace();
  if (!loaded) {
    void loadSavedPlans();
    void loadLearningStyleProfile();
    await loadKnowledgeBases();
  }
  if (seedKbIds.length) {
    const allowed = new Set(availableKbs.value.map((kb) => kb.kb_id));
    selectedKbIds.value = Array.from(new Set(seedKbIds)).filter((id) => allowed.has(id));
  }
});
</script>

<template>
  <AppShell title="自定义学习" brand-name="遥感滑坡分析智能体" brand-icon="Mountain">
    <template #actions>
      <button
        v-if="mode === 'detail'"
        type="button"
        class="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border-subtle px-2.5 text-xs font-medium text-text-secondary transition-colors hover:border-accent/30 hover:text-accent"
        @click="backToList"
      >
        <ArrowLeft class="size-3.5" />
        计划列表
      </button>
      <button
        type="button"
        class="inline-flex h-8 items-center gap-1.5 rounded-lg bg-accent px-2.5 text-xs font-medium text-accent-fg transition-colors hover:bg-accent-hover"
        @click="focusGoalInput"
      >
        <Plus class="size-3.5" />
        新建计划
      </button>
    </template>

    <main class="custom-scrollbar min-h-0 flex-1 overflow-y-auto bg-surface-sunken">
        <section class="px-5 pb-5 pt-6 md:px-8">
          <div class="mx-auto max-w-7xl">
            <div class="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
              <div>
                <p class="text-xs font-semibold text-blue-500">探索式学习 · 自定义学习 · 学习风格</p>
                <h2 class="mt-2 text-2xl font-bold text-text-primary dark:text-white md:text-3xl">按目标组织你的学习路径</h2>
                <p class="mt-3 max-w-3xl text-sm leading-6 text-text-secondary">
                  先探索知识图谱找方向，再用自定义学习生成可执行路径，最后用学习风格把解释、练习和复盘节奏调到更适合你。
                </p>
              </div>
              <div class="flex flex-wrap gap-2">
                <button
                  type="button"
                  class="inline-flex items-center gap-2 rounded-lg border border-border-subtle bg-surface/76 px-4 py-2.5 text-sm font-semibold text-text-secondary shadow-sm transition-colors hover:border-accent/30 hover:text-accent"
                  @click="openExplore"
                >
                  <Compass class="h-4 w-4" />
                  先探索
                </button>
                <button
                  type="button"
                  class="inline-flex items-center gap-2 rounded-lg border border-border-subtle bg-surface/76 px-4 py-2.5 text-sm font-semibold text-text-secondary shadow-sm transition-colors hover:border-accent/30 hover:text-accent"
                  @click="openLearningStyle"
                >
                  <BrainCircuit class="h-4 w-4" />
                  学习风格
                </button>
                <button
                  type="button"
                  class="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-blue-700"
                  @click="focusGoalInput"
                >
                  <WandSparkles class="h-4 w-4" />
                  生成计划
                </button>
              </div>
            </div>
          </div>
        </section>

        <div v-if="mode === 'list'" class="mx-auto max-w-7xl px-5 pb-8 pt-5 md:px-8">
          <section
            v-if="isAuthenticated && reviewWorkspaceSummary.planCount > 0"
            class="mb-5 rounded-lg border border-amber-200/70 bg-surface/82 p-5 shadow-sm dark:border-amber-900/50 dark:bg-amber-950/20"
          >
            <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
              <div>
                <div class="mb-2 inline-flex items-center gap-2 rounded-md bg-amber-100 px-2.5 py-1 text-xs font-semibold text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
                  <ClipboardCheck class="h-3.5 w-3.5" />
                  复习闭环提醒
                </div>
                <h3 class="text-xl font-bold text-amber-950 dark:text-amber-50">
                  {{ reviewWorkspaceSummary.planCount }} 个计划、{{ reviewWorkspaceSummary.totalReviewNodes }} 个节点待复习
                </h3>
                <p class="mt-1 max-w-3xl text-sm leading-6 text-amber-900/80 dark:text-amber-100/80">
                  建议优先完成复习节点，再回到原节点重新检测。完成练习并达到 70 分后，系统会自动解锁后续路径。
                </p>
              </div>
            </div>
            <div class="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              <button
                v-for="plan in reviewWorkspaceSummary.plansWithReview.slice(0, 3)"
                :key="plan.plan_id"
                type="button"
                class="rounded-lg border border-amber-100 bg-amber-50/35 p-4 text-left transition-colors hover:border-amber-200 hover:bg-surface dark:border-amber-900/40"
                @click="openSavedPlan(plan.plan_id)"
              >
                <div class="flex items-center justify-between gap-2">
                  <span class="truncate font-bold text-text-primary dark:text-white">{{ plan.title }}</span>
                  <span class="shrink-0 rounded bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                    {{ plan.stats?.review_nodes }} 待复习
                  </span>
                </div>
                <p class="mt-2 line-clamp-2 text-sm text-text-secondary">{{ plan.summary || plan.goal }}</p>
              </button>
            </div>
          </section>

          <section
            v-if="currentPlanSuggestion"
            class="mb-5 rounded-lg border border-emerald-100/80 bg-surface/86 p-5 shadow-sm dark:border-emerald-900/40"
          >
            <div class="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
              <div class="min-w-0">
                <div class="mb-2 inline-flex items-center gap-2 rounded-md bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
                  <Play class="h-3.5 w-3.5" />
                  {{ (currentPlanSuggestion.stats?.review_nodes ?? 0) > 0 ? '优先复习' : '推荐继续学习' }}
                </div>
                <h3 class="truncate text-xl font-bold">{{ currentPlanSuggestion.title }}</h3>
                <p class="mt-1 line-clamp-2 max-w-3xl text-sm leading-6 text-text-secondary">{{ currentPlanSuggestion.summary || currentPlanSuggestion.goal }}</p>
              </div>
              <div class="flex flex-col gap-3 sm:flex-row sm:items-center">
                <div class="min-w-40 rounded-lg border border-border-subtle bg-surface-sunken px-4 py-3">
                  <div class="text-xs font-semibold text-text-secondary">当前进度</div>
                  <div class="mt-1 text-2xl font-bold text-emerald-600">{{ currentPlanSuggestion.progress }}%</div>
                </div>
                <button
                  type="button"
                  class="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-emerald-600 px-5 text-sm font-semibold text-white transition-colors hover:bg-emerald-700"
                  @click="openSavedPlan(currentPlanSuggestion.plan_id)"
                >
                  <Route class="h-4 w-4" />
                  {{ (currentPlanSuggestion.stats?.review_nodes ?? 0) > 0 ? '进入复习' : '进入学习工作台' }}
                </button>
              </div>
            </div>
          </section>

          <section class="mb-5 rounded-lg border border-blue-100/80 bg-surface/88 p-5 shadow-sm">
            <div class="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
              <div>
                <div class="mb-4 flex items-start justify-between gap-3">
                  <div>
                    <div class="inline-flex items-center gap-2 rounded-md bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-700 dark:bg-blue-950/40 dark:text-blue-300">
                      <WandSparkles class="h-3.5 w-3.5" />
                      自定义学习生成器
                    </div>
                    <h3 class="mt-3 text-xl font-bold">把一句学习目标变成可执行路径</h3>
                    <p class="mt-1 text-sm leading-6 text-text-secondary">生成后会先预览计划草稿，确认开始后进入学习工作台，支持节点详解、探索资源、练习诊断和薄弱点复盘。</p>
                  </div>
                </div>

	                <textarea
	                  ref="goalInputRef"
	                  v-model="generatedGoal"
                  class="min-h-28 w-full rounded-lg border border-border-subtle bg-surface-sunken p-4 text-sm leading-6 outline-none transition-colors focus:border-blue-300 focus:bg-surface"
                  placeholder="例如：7 天掌握前置放大器原理，能画出电荷灵敏前放链路并做参数估算"
	                  @keyup.ctrl.enter="generatePlanFromGoal"
	                />
	                <div
	                  v-if="exploreSnapshotSummary"
	                  class="mt-3 flex items-start justify-between gap-3 rounded-lg border border-accent/30 bg-accent-soft px-3 py-2.5 text-sm text-accent"
	                >
	                  <p class="leading-6">{{ exploreSnapshotSummary }}</p>
	                  <button
	                    type="button"
	                    class="shrink-0 rounded-md border border-accent/30 bg-surface px-2.5 py-1 text-xs font-semibold text-accent transition-colors hover:bg-accent-soft"
	                    @click="clearExploreSnapshot"
	                  >
	                    不继承
	                  </button>
	                </div>

                <div class="mt-4 grid gap-3 lg:grid-cols-3">
                  <label class="block">
                    <span class="mb-1.5 block text-xs font-semibold text-text-secondary">当前基础</span>
                    <select v-model="generatedBaseline" class="min-h-11 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-blue-300">
                      <option v-for="option in baselineOptions" :key="option.value" :value="option.value">{{ option.label }} · {{ option.hint }}</option>
                    </select>
                  </label>
                  <label class="block">
                    <span class="mb-1.5 block text-xs font-semibold text-text-secondary">学习深度</span>
                    <select v-model="generatedDepth" class="min-h-11 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-blue-300">
                      <option v-for="option in depthOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                    </select>
                  </label>
                  <div class="grid grid-cols-2 gap-3">
                    <label class="block">
                      <span class="mb-1.5 block text-xs font-semibold text-text-secondary">天数</span>
                      <input v-model.number="generatedDays" type="number" min="1" max="365" class="min-h-11 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-blue-300" />
                    </label>
                    <label class="block">
                      <span class="mb-1.5 block text-xs font-semibold text-text-secondary">分钟/天</span>
                      <input v-model.number="generatedMinutes" type="number" min="5" max="1440" class="min-h-11 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-blue-300" />
                    </label>
                  </div>
                </div>

                <div class="mt-4 rounded-lg border border-border-subtle bg-surface-sunken p-3">
                  <div class="mb-3 flex items-center justify-between gap-3">
                    <div class="flex items-center gap-2 text-sm font-bold">
                      <Database class="h-4 w-4 text-blue-600" />
                      知识来源
                    </div>
                    <span class="text-xs text-text-tertiary">{{ selectedKbNames }}</span>
                  </div>
                  <div v-if="kbLoading" class="flex items-center gap-2 text-sm text-text-secondary">
                    <Loader2 class="h-4 w-4 animate-spin" />
                    正在读取知识库...
                  </div>
                  <div v-else-if="availableKbs.length" class="flex flex-wrap gap-2">
                    <button
                      v-for="kb in availableKbs"
                      :key="kb.kb_id"
                      type="button"
                      class="inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold transition-colors"
                      :class="selectedKbIds.includes(kb.kb_id) ? 'border-blue-300 bg-blue-50 text-blue-700 dark:border-blue-900/60 dark:bg-blue-950/30 dark:text-blue-300' : 'border-border-subtle bg-surface text-text-secondary hover:border-blue-200 hover:text-blue-600'"
                      :aria-pressed="selectedKbIds.includes(kb.kb_id)"
                      @click="toggleKnowledgeBase(kb.kb_id)"
                    >
                      <CheckCircle2 v-if="selectedKbIds.includes(kb.kb_id)" class="h-3.5 w-3.5" />
                      <Circle v-else class="h-3.5 w-3.5" />
                      {{ kb.kb_name }}
                    </button>
                  </div>
                  <p v-else class="text-sm text-text-secondary">暂无可选知识库，计划仍会使用通用课程结构生成。</p>
                </div>

                <div class="mt-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                  <label class="inline-flex cursor-pointer items-start gap-3 rounded-lg border border-border-subtle bg-surface-sunken px-3 py-2.5 text-sm text-text-secondary">
                    <input v-model="useLearningStyle" type="checkbox" class="mt-0.5 h-4 w-4 rounded border-border-subtle text-blue-600" />
                    <span>
                      <span class="block font-semibold">应用学习风格偏好</span>
                      <span class="mt-0.5 block text-xs text-text-tertiary">{{ learningStyleState.summary }}</span>
                    </span>
                  </label>
                  <button
                    type="button"
                    class="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-blue-600 px-5 text-sm font-semibold text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
                    :disabled="generatingPlan"
                    @click="generatePlanFromGoal"
                  >
                    <WandSparkles class="h-4 w-4" />
                    {{ generatingPlan ? '正在生成路径' : '生成并预览计划' }}
                  </button>
                </div>

                <div class="mt-4 flex flex-wrap gap-2">
                  <button
                    v-for="goal in quickGoals"
                    :key="goal"
                    type="button"
                    class="rounded-lg border border-border-subtle bg-surface/80 px-3 py-2 text-left text-xs leading-5 text-text-secondary transition-colors hover:border-blue-200 hover:bg-surface hover:text-blue-600"
                    @click="applyQuickGoal(goal)"
                  >
                    {{ goal }}
                  </button>
                </div>
              </div>

              <aside class="rounded-lg border border-border-subtle bg-surface-sunken p-4">
                <h4 class="flex items-center gap-2 text-sm font-bold">
                  <ListChecks class="h-4 w-4 text-blue-600" />
                  本次计划会这样生成
                </h4>
                <div class="mt-4 space-y-3 text-sm text-text-secondary">
                  <div class="flex items-start gap-3">
                    <span class="mt-1 h-2 w-2 rounded-full bg-blue-500" />
                    <p>按“{{ currentBaselineLabel }} · {{ currentDepthLabel }}”拆成 {{ generatedDays }} 天节点。</p>
                  </div>
                  <div class="flex items-start gap-3">
                    <span class="mt-1 h-2 w-2 rounded-full bg-teal-500" />
                    <p>每个节点包含目标、资源、任务、验收标准和练习。</p>
                  </div>
                  <div class="flex items-start gap-3">
                    <span class="mt-1 h-2 w-2 rounded-full bg-amber-500" />
                    <p>练习低分会进入复习节点，路径会自动补强薄弱点。</p>
                  </div>
                  <div class="flex items-start gap-3">
                    <span class="mt-1 h-2 w-2 rounded-full bg-accent" />
                    <p>{{ useLearningStyle ? learningStyleState.summary : '本次不应用学习风格偏好。' }}</p>
                  </div>
	                  <div class="flex items-start gap-3">
	                    <span class="mt-1 h-2 w-2 rounded-full bg-sky-500" />
	                    <p>知识来源：{{ selectedKbNames }}</p>
	                  </div>
	                  <div v-if="exploreSnapshotSummary" class="flex items-start gap-3">
	                    <span class="mt-1 h-2 w-2 rounded-full bg-accent" />
	                    <p>{{ exploreSnapshotSummary }}</p>
	                  </div>
	                </div>

                <div v-if="workspaceModuleEffects.length" class="mt-5 space-y-2">
                  <div class="text-xs font-bold text-text-secondary">模块使用效果</div>
                  <div
                    v-for="effect in workspaceModuleEffects"
                    :key="effect.id"
                    class="rounded-lg border p-3"
                    :class="moduleEffectToneClass(effect.status)"
                  >
                    <div class="flex items-center justify-between gap-2">
                      <div class="flex min-w-0 items-center gap-2">
                        <component :is="moduleEffectIcon(effect.id)" class="h-4 w-4 shrink-0" />
                        <span class="truncate text-xs font-bold">{{ effect.title }}</span>
                      </div>
                      <span class="shrink-0 text-[11px] font-bold">{{ effect.metric }}</span>
                    </div>
                    <p class="mt-1 line-clamp-2 text-xs leading-5 opacity-80">{{ effect.description }}</p>
                  </div>
                </div>

                <button
                  type="button"
                  class="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-lg border border-accent/30 bg-surface px-3 py-2.5 text-sm font-semibold text-accent transition-colors hover:bg-accent-soft"
                  @click="openLearningStyle"
                >
                  <BrainCircuit class="h-4 w-4" />
                  去完善学习风格
                </button>
              </aside>
            </div>
          </section>

          <section
            v-if="planPreview"
            ref="planPreviewRef"
            class="mb-5 rounded-lg border border-blue-200/80 bg-surface/88 p-5 shadow-sm dark:border-blue-900/60"
          >
            <div class="flex flex-col justify-between gap-4 lg:flex-row lg:items-start">
              <div class="min-w-0">
                <div class="mb-2 inline-flex items-center gap-2 rounded-md bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-700 dark:bg-blue-950/40 dark:text-blue-300">
                  <WandSparkles class="h-3.5 w-3.5" />
                  计划预览
                </div>
                <h3 class="truncate text-xl font-bold">{{ planPreview.title }}</h3>
                <p class="mt-1 max-w-3xl text-sm leading-6 text-text-secondary">{{ planPreview.summary || planPreview.goal }}</p>
                <div class="mt-3 flex flex-wrap gap-2 text-xs font-semibold">
                  <span class="rounded-md bg-surface-sunken px-2.5 py-1 text-text-secondary">{{ planPreview.duration_days }} 天</span>
                  <span class="rounded-md bg-surface-sunken px-2.5 py-1 text-text-secondary">{{ planPreview.daily_minutes }} 分钟/天</span>
                  <span class="rounded-md bg-surface-sunken px-2.5 py-1 text-text-secondary">{{ planPreview.nodes?.length || 0 }} 个节点</span>
                  <span class="rounded-md bg-surface-sunken px-2.5 py-1 text-text-secondary">{{ planPreview.style_applied ? '已应用学习风格' : '默认学习策略' }}</span>
                </div>
              </div>
              <div class="flex shrink-0 gap-2">
                <button
                  type="button"
                  class="inline-flex min-h-10 items-center justify-center rounded-lg border border-border-subtle px-4 text-sm font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
                  @click="closePlanPreview"
	                >
	                  返回修改并重新生成
	                </button>
                <button
                  type="button"
                  class="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 text-sm font-semibold text-white transition-colors hover:bg-blue-700"
                  :disabled="activatingPreview"
                  @click="confirmPreviewPlan"
                >
                  <Loader2 v-if="activatingPreview" class="h-4 w-4 animate-spin" />
                  <Play v-else class="h-4 w-4" />
                  {{ activatingPreview ? '启用中' : '确认并开始' }}
                </button>
              </div>
            </div>
            <div class="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              <article
                v-for="node in (planPreview.nodes || []).slice(0, 6)"
                :key="node.node_id"
                class="rounded-lg border border-border-subtle bg-surface-sunken p-4"
              >
                <div class="flex items-center justify-between gap-3">
                  <h4 class="truncate text-sm font-bold">{{ node.title }}</h4>
                  <span class="shrink-0 rounded bg-surface px-2 py-0.5 text-[11px] font-semibold text-text-secondary">{{ node.day }}</span>
                </div>
                <p class="mt-2 line-clamp-2 text-xs leading-5 text-text-secondary">{{ node.objective || node.subtitle }}</p>
                <p class="mt-3 line-clamp-2 text-xs leading-5 text-text-secondary">{{ node.task }}</p>
              </article>
            </div>
          </section>

          <section class="mb-5 grid gap-4 md:grid-cols-4">
            <article class="rounded-lg border border-border-subtle bg-surface/82 p-4 shadow-sm">
              <div class="flex items-center justify-between">
                <span class="text-sm text-text-secondary">我的计划</span>
                <Map class="h-4 w-4 text-blue-500" />
              </div>
              <p class="mt-3 text-2xl font-bold">{{ savedPlans.length }}</p>
              <p class="mt-1 text-xs text-text-tertiary">{{ savedPlanStats.active }} 个进行中</p>
            </article>
            <article class="rounded-lg border border-border-subtle bg-surface/82 p-4 shadow-sm">
              <div class="flex items-center justify-between">
                <span class="text-sm text-text-secondary">平均进度</span>
                <BadgeCheck class="h-4 w-4 text-teal-600" />
              </div>
              <p class="mt-3 text-2xl font-bold">{{ savedPlanStats.averageProgress }}%</p>
              <p class="mt-1 text-xs text-text-tertiary">{{ savedPlanStats.completed }} 个已完成</p>
            </article>
            <article class="rounded-lg border border-border-subtle bg-surface/82 p-4 shadow-sm">
              <div class="flex items-center justify-between">
                <span class="text-sm text-text-secondary">模板资源</span>
                <BookOpenText class="h-4 w-4 text-amber-600" />
              </div>
              <p class="mt-3 text-2xl font-bold">{{ totals.resources }}</p>
              <p class="mt-1 text-xs text-text-tertiary">{{ totals.projects }} 个项目练习</p>
            </article>
            <article class="rounded-lg border border-border-subtle bg-surface/82 p-4 shadow-sm">
              <div class="flex items-center justify-between">
                <span class="text-sm text-text-secondary">{{ isAuthenticated ? '待复习节点' : '可学节点' }}</span>
                <Lightbulb class="h-4 w-4" :class="isAuthenticated && reviewWorkspaceSummary.totalReviewNodes > 0 ? 'text-amber-600' : 'text-accent'" />
              </div>
              <p class="mt-3 text-2xl font-bold" :class="isAuthenticated && reviewWorkspaceSummary.totalReviewNodes > 0 ? 'text-amber-600' : ''">
                {{ isAuthenticated ? reviewWorkspaceSummary.totalReviewNodes : totals.openNodes }}
              </p>
              <p class="mt-1 text-xs text-text-tertiary">
                {{ isAuthenticated ? `${reviewWorkspaceSummary.planCount} 个计划需复盘` : `${totals.plans} 个预制专项` }}
              </p>
            </article>
          </section>

          <section class="mb-8 rounded-lg border border-border-subtle bg-surface/86 p-5 shadow-sm">
            <div class="mb-4 flex flex-col justify-between gap-3 md:flex-row md:items-center">
              <div>
                <h3 class="text-xl font-bold">我的学习计划</h3>
                <p class="mt-1 text-sm text-text-secondary">生成或保存后的计划会在这里继续学习和追踪进度。</p>
              </div>
              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
                @click="loadLearningWorkspace"
              >
                <Timer class="h-3.5 w-3.5" />
                刷新
              </button>
            </div>

            <div v-if="!isAuthenticated" class="rounded-lg border border-dashed border-border-subtle bg-surface-sunken p-5 text-sm text-text-secondary">
              登录后可以保存个人学习计划、记录节点进度并跨设备继续。
            </div>
            <div v-else-if="plansLoading" class="rounded-lg border border-dashed border-border-subtle bg-surface-sunken p-5 text-sm text-text-secondary">
              正在加载学习计划...
            </div>
            <div v-else-if="plansError" class="rounded-lg border border-rose-100 bg-rose-50 p-5 text-sm text-rose-600 dark:border-rose-900/50 dark:bg-rose-950/20">
              {{ plansError }}
            </div>
            <div v-else-if="savedPlans.length === 0" class="rounded-lg border border-dashed border-border-subtle bg-surface-sunken p-5 text-sm text-text-secondary">
              还没有保存的计划。可以先用上方目标生成器创建，也可以从下方模板保存一个开始。
            </div>
            <div v-else class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
	              <article
	                v-for="plan in savedPlans"
	                :key="plan.plan_id"
	                class="rounded-lg border border-border-subtle bg-surface/72 p-4 transition-colors hover:border-blue-200 hover:bg-surface"
	              >
                <div class="mb-3 h-1 rounded-full" :style="{ backgroundColor: plan.accent || '#2563eb' }" />
                <div class="flex items-start justify-between gap-3">
                  <div class="min-w-0">
                    <h4 class="truncate font-bold">{{ plan.title }}</h4>
                    <p class="mt-1 line-clamp-2 text-sm leading-5 text-text-secondary">{{ plan.summary || plan.goal }}</p>
                  </div>
                  <div class="flex shrink-0 flex-col items-end gap-1">
                    <span class="rounded bg-surface-sunken px-2 py-1 text-xs font-semibold text-text-secondary">{{ statusLabel(plan.status) }}</span>
                    <span
                      v-if="(plan.stats?.review_nodes ?? 0) > 0"
                      class="rounded bg-amber-50 px-2 py-1 text-xs font-semibold text-amber-700 dark:bg-amber-950/30 dark:text-amber-300"
                    >
                      {{ plan.stats?.review_nodes }} 待复习
                    </span>
                  </div>
                </div>
                <div class="mt-4">
                  <div class="flex items-center justify-between text-xs">
                    <span class="text-text-secondary">进度</span>
                    <strong :style="{ color: plan.accent || '#2563eb' }">{{ plan.progress }}%</strong>
                  </div>
                  <div class="mt-2 h-2 overflow-hidden rounded-full bg-surface-sunken">
                    <div class="h-full rounded-full" :style="{ width: `${plan.progress}%`, backgroundColor: plan.accent || '#2563eb' }" />
                  </div>
                </div>
	                <div v-if="editingPlanId === plan.plan_id" class="mt-4 space-y-3 rounded-lg border border-blue-100 bg-blue-50/50 p-3 dark:border-blue-900/50 dark:bg-blue-950/20">
	                  <input
	                    v-model="editForm.title"
	                    class="min-h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm font-semibold outline-none focus:border-blue-300"
	                    placeholder="计划标题"
	                  />
	                  <textarea
	                    v-model="editForm.goal"
	                    class="min-h-20 w-full rounded-lg border border-border-subtle bg-surface p-3 text-sm outline-none focus:border-blue-300"
	                    placeholder="学习目标"
	                  />
	                  <div class="grid gap-2 sm:grid-cols-2">
	                    <label class="block">
	                      <span class="mb-1 block text-[11px] font-semibold text-text-tertiary">当前基础</span>
	                      <select v-model="editForm.baseline" class="min-h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-blue-300">
	                        <option v-for="option in baselineOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
	                      </select>
	                    </label>
	                    <label class="block">
	                      <span class="mb-1 block text-[11px] font-semibold text-text-tertiary">学习深度</span>
	                      <select v-model="editForm.depth" class="min-h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-blue-300">
	                        <option v-for="option in depthOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
	                      </select>
	                    </label>
	                    <label class="block">
	                      <span class="mb-1 block text-[11px] font-semibold text-text-tertiary">天数</span>
	                      <input v-model.number="editForm.duration_days" type="number" min="1" max="365" class="min-h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-blue-300" />
	                    </label>
	                    <label class="block">
	                      <span class="mb-1 block text-[11px] font-semibold text-text-tertiary">分钟/天</span>
	                      <input v-model.number="editForm.daily_minutes" type="number" min="5" max="1440" class="min-h-10 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-blue-300" />
	                    </label>
	                  </div>
	                  <div v-if="availableKbs.length" class="flex flex-wrap gap-2">
	                    <button
	                      v-for="kb in availableKbs"
	                      :key="kb.kb_id"
	                      type="button"
	                      class="rounded-md border px-2.5 py-1 text-xs font-semibold"
	                      :class="editForm.kb_ids.includes(kb.kb_id) ? 'border-blue-300 bg-surface text-blue-700 dark:border-blue-800 dark:text-blue-300' : 'border-border-subtle bg-surface/70 text-text-secondary'"
	                      @click="toggleEditKnowledgeBase(kb.kb_id)"
	                    >
	                      {{ kb.kb_name }}
	                    </button>
	                  </div>
	                  <div class="flex flex-wrap gap-2">
	                    <button
	                      type="button"
	                      class="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-3 py-2 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
	                      :disabled="editSavingPlanId === plan.plan_id"
	                      @click="savePlanEdits(plan)"
	                    >
	                      <Loader2 v-if="editSavingPlanId === plan.plan_id" class="h-3.5 w-3.5 animate-spin" />
	                      <Save v-else class="h-3.5 w-3.5" />
	                      保存
	                    </button>
	                    <button
	                      type="button"
	                      class="inline-flex items-center gap-2 rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs font-semibold text-text-secondary"
	                      @click="cancelEditPlan"
	                    >
	                      <X class="h-3.5 w-3.5" />
	                      取消
	                    </button>
	                  </div>
	                </div>
	                <div v-else class="mt-4 grid grid-cols-[1fr_auto_auto] gap-2">
	                  <button
	                    type="button"
	                    class="inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-semibold text-white"
	                    :style="{ backgroundColor: plan.accent || '#2563eb' }"
	                    @click="openSavedPlanCard(plan)"
	                  >
	                    <Route class="h-4 w-4" />
	                    {{ plan.status === 'draft' ? '查看草稿' : ((plan.stats?.review_nodes ?? 0) > 0 ? '继续复习' : '继续学习') }}
	                  </button>
	                  <button
	                    type="button"
	                    class="rounded-lg border border-border-subtle p-2 text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
	                    title="编辑计划"
	                    aria-label="编辑计划"
	                    @click="startEditPlan(plan)"
	                  >
	                    <Pencil class="h-4 w-4" />
	                  </button>
	                  <button
	                    type="button"
	                    class="inline-flex items-center gap-1.5 rounded-lg border p-2 transition-colors disabled:cursor-not-allowed disabled:opacity-50"
	                    :class="archiveConfirmId === plan.plan_id ? 'border-rose-300 bg-rose-50 text-rose-700 dark:border-rose-800 dark:bg-rose-950/30 dark:text-rose-300' : 'border-border-subtle text-text-secondary hover:border-rose-200 hover:text-rose-600'"
	                    :title="archiveConfirmId === plan.plan_id ? '再点一次确认归档' : '归档计划'"
	                    :aria-label="archiveConfirmId === plan.plan_id ? '再点一次确认归档' : '归档计划'"
	                    :disabled="archivingPlanId === plan.plan_id"
	                    @click="archiveSavedPlan(plan)"
	                  >
	                    <Loader2 v-if="archivingPlanId === plan.plan_id" class="h-4 w-4 animate-spin" />
	                    <Trash2 v-else class="h-4 w-4" />
	                    <span v-if="archiveConfirmId === plan.plan_id" class="text-xs font-semibold">确认归档</span>
	                  </button>
	                </div>
	              </article>
            </div>
          </section>

          <div class="mb-4 flex items-end justify-between gap-4">
            <div>
              <h3 class="text-xl font-bold">学习计划模板</h3>
              <p class="mt-1 text-sm text-text-secondary">点击卡片查看模板详情，或保存为你的个人学习计划。</p>
            </div>
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg border border-border-subtle bg-surface px-3 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
              :aria-expanded="templatesExpanded"
              @click="templatesExpanded = !templatesExpanded"
            >
              <ChevronRight class="h-3.5 w-3.5 transition-transform" :class="templatesExpanded ? 'rotate-90' : ''" />
              {{ templatesExpanded ? '收起模板' : `展开 ${plans.length} 个模板` }}
            </button>
          </div>

          <section v-show="templatesExpanded" class="grid gap-5 lg:grid-cols-2 2xl:grid-cols-3">
            <article
              v-for="plan in plans"
              :key="plan.id"
              class="group rounded-lg border border-border-subtle bg-surface/82 shadow-sm transition-all hover:-translate-y-0.5 hover:border-blue-200 hover:bg-surface hover:shadow-sm dark:hover:border-blue-900 dark:hover:shadow-none"
            >
              <button type="button" class="block w-full p-5 text-left" @click="openPlan(plan.id)">
                <div class="mb-4 h-1 rounded-full" :style="{ backgroundColor: plan.accent }" />
                <div class="flex items-start justify-between gap-4">
                  <div class="min-w-0">
                    <h4 class="truncate text-lg font-bold text-text-primary dark:text-white">{{ plan.title }}</h4>
                    <p class="mt-2 line-clamp-2 min-h-10 text-sm leading-5 text-text-secondary">{{ plan.subtitle }}</p>
                  </div>
                  <ChevronRight class="mt-1 h-5 w-5 shrink-0 text-text-tertiary transition-transform group-hover:translate-x-1 group-hover:text-blue-500" />
                </div>

                <div class="mt-5 space-y-3 border-t border-border-subtle pt-4">
                  <div class="flex items-center justify-between text-sm">
                    <span class="inline-flex items-center gap-2 text-text-secondary"><CalendarDays class="h-4 w-4" :style="{ color: plan.accent }" /> 计划时长</span>
                    <strong>{{ plan.duration }}</strong>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="inline-flex items-center gap-2 text-text-secondary"><Beaker class="h-4 w-4" :style="{ color: plan.accent }" /> 学习深度</span>
                    <strong>{{ plan.depth }}</strong>
                  </div>
                  <div class="flex items-center justify-between text-sm">
                    <span class="inline-flex items-center gap-2 text-text-secondary"><Layers3 class="h-4 w-4" :style="{ color: plan.accent }" /> 课程类型</span>
                    <strong>{{ plan.courseType }}</strong>
                  </div>
                </div>

                <div class="mt-5">
                  <div class="flex items-center justify-between text-xs">
                    <span class="text-text-secondary">学习进度</span>
                    <strong :style="{ color: plan.accent }">{{ plan.progress }}%</strong>
                  </div>
                  <div class="mt-2 h-2 overflow-hidden rounded-full bg-surface-sunken">
                    <div class="h-full rounded-full transition-all" :style="{ width: `${plan.progress}%`, backgroundColor: plan.accent }" />
                  </div>
                </div>
              </button>

              <div class="flex items-center justify-end gap-2 border-t border-border-subtle px-5 py-3">
                <button type="button" class="rounded-lg p-2 text-text-secondary transition-colors hover:bg-blue-50 hover:text-blue-600 dark:hover:bg-blue-500/10" title="查看模板" @click="openPlan(plan.id)">
                  <Play class="h-4 w-4" />
                </button>
                <button
                  type="button"
                  class="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
                  :disabled="savingPlanId === plan.id"
                  @click="createPlanFromTemplate(plan)"
                >
                  <Plus class="h-3.5 w-3.5" />
                  {{ savingPlanId === plan.id ? '保存中' : '保存计划' }}
                </button>
              </div>
            </article>
          </section>
        </div>

        <div v-else class="mx-auto grid max-w-7xl gap-6 px-5 py-6 md:px-8 xl:grid-cols-[260px_minmax(0,1fr)]">
          <aside class="rounded-lg border border-border-subtle bg-surface p-4 shadow-sm">
            <p class="mb-3 text-xs font-semibold text-text-tertiary">课程目录</p>
            <div class="space-y-2">
              <button
                v-for="plan in plans"
                :key="plan.id"
                type="button"
                class="flex w-full items-center justify-between rounded-lg border px-3 py-3 text-left text-sm transition-colors"
                :class="plan.id === selectedPlan.id ? 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-500/10 dark:text-blue-300' : 'border-transparent text-text-secondary hover:bg-surface-hover'"
                @click="switchPlan(plan.id)"
              >
                <span class="min-w-0 truncate font-medium">{{ plan.title }}</span>
                <span class="shrink-0 rounded bg-surface/70 px-2 py-0.5 text-[11px] text-text-tertiary">{{ plan.progress }}%</span>
              </button>
            </div>

            <div class="mt-6 border-t border-border-subtle pt-4">
              <p class="mb-3 text-xs font-semibold text-text-tertiary">辅助资源</p>
              <div class="space-y-2 text-sm text-text-secondary">
                <div class="flex items-center gap-2"><BookOpenText class="h-4 w-4 text-blue-500" /> 推荐教材与文献</div>
                <div class="flex items-center gap-2"><FileText class="h-4 w-4 text-teal-600" /> 课程讲义下载</div>
                <div class="flex items-center gap-2"><Video class="h-4 w-4 text-amber-600" /> 实验演示视频</div>
              </div>
            </div>
          </aside>

          <section class="min-w-0 space-y-6">
            <article class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm md:p-6">
              <div class="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
                <div class="min-w-0">
                  <div class="mb-3 inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium" :style="{ color: selectedPlan.accent, borderColor: selectedPlan.accent, backgroundColor: selectedPlan.soft }">
                    <Map class="h-3.5 w-3.5" />
                    学习计划详情
                  </div>
                  <h3 class="text-2xl font-bold md:text-3xl">{{ selectedPlan.title }}</h3>
                  <p class="mt-3 max-w-3xl text-sm leading-6 text-text-secondary">{{ selectedPlan.summary }}</p>
                </div>
                <button
                  type="button"
                  class="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg px-4 py-3 text-sm font-semibold text-white transition-colors disabled:cursor-not-allowed disabled:opacity-60"
                  :style="{ backgroundColor: selectedPlan.accent }"
                  :disabled="savingPlanId === selectedPlan.id"
                  @click="createPlanFromTemplate(selectedPlan)"
                >
                  <Play class="h-4 w-4" />
                  {{ savingPlanId === selectedPlan.id ? '保存中' : '保存并预览' }}
                </button>
                <button
                  type="button"
                  class="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg border border-border-subtle bg-surface px-4 py-3 text-sm font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
                  @click="openPath()"
                >
                  <Route class="h-4 w-4" />
                  查看完整路径
                </button>
              </div>

              <div class="mt-6 grid gap-4 md:grid-cols-4">
                <div class="rounded-lg border border-border-subtle p-4">
                  <Target class="h-4 w-4" :style="{ color: selectedPlan.accent }" />
                  <p class="mt-3 text-2xl font-bold">{{ selectedPlan.objectives.length }}</p>
                  <p class="mt-1 text-xs text-text-secondary">个核心学习目标</p>
                </div>
                <div class="rounded-lg border border-border-subtle p-4">
                  <BookOpenText class="h-4 w-4" :style="{ color: selectedPlan.accent }" />
                  <p class="mt-3 text-2xl font-bold">{{ selectedPlan.resources.length }}</p>
                  <p class="mt-1 text-xs text-text-secondary">份精选学习材料</p>
                </div>
                <div class="rounded-lg border border-border-subtle p-4">
                  <FlaskConical class="h-4 w-4" :style="{ color: selectedPlan.accent }" />
                  <p class="mt-3 text-2xl font-bold">{{ selectedPlan.projects.length }}</p>
                  <p class="mt-1 text-xs text-text-secondary">个实操训练项目</p>
                </div>
                <div class="rounded-lg border border-border-subtle p-4">
                  <Clock3 class="h-4 w-4" :style="{ color: selectedPlan.accent }" />
                  <p class="mt-3 text-2xl font-bold">{{ selectedPlan.duration }}</p>
                  <p class="mt-1 text-xs text-text-secondary">建议每日约 90 分钟投入</p>
                </div>
              </div>
            </article>

            <section class="grid gap-6 xl:grid-cols-[360px_minmax(0,1fr)]">
              <article class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
                <div class="mb-4 flex items-center gap-2">
                  <Target class="h-5 w-5" :style="{ color: selectedPlan.accent }" />
                  <h4 class="text-lg font-bold">学习目标</h4>
                </div>
                <div class="space-y-3">
                  <button
                    v-for="(objective, index) in selectedPlan.objectives"
                    :key="objective.title"
                    type="button"
                    class="w-full rounded-lg border p-4 text-left transition-all"
                    :class="index === activeObjectiveIndex ? 'border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-500/10' : 'border-border-subtle hover:border-border-subtle hover:bg-surface-hover'"
                    @click="activeObjectiveIndex = index"
                  >
                    <div class="flex gap-3">
                      <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white" :style="{ backgroundColor: selectedPlan.accent }">{{ index + 1 }}</span>
                      <div class="min-w-0">
                        <h5 class="font-semibold">{{ objective.title }}</h5>
                        <p class="mt-2 text-sm leading-6 text-text-secondary">{{ objective.description }}</p>
                      </div>
                    </div>
                  </button>
                </div>
              </article>

              <article class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
                <p class="mb-2 text-sm text-text-secondary">为什么设置这个目标</p>
                <h4 class="text-xl font-bold">{{ activeObjective.title }}</h4>
                <p class="mt-3 rounded-lg border border-border-subtle bg-surface-sunken p-4 text-sm leading-6 text-text-secondary">
                  {{ activeObjective.rationale }}
                </p>
                <div class="mt-5">
                  <p class="mb-3 text-sm font-semibold">完成后你应该能够：</p>
                  <div class="grid gap-3 md:grid-cols-3">
                    <div
                      v-for="checkpoint in activeObjective.checkpoints"
                      :key="checkpoint"
                      class="rounded-lg border border-border-subtle p-4 text-sm leading-6 text-text-secondary"
                    >
                      <CheckCircle2 class="mb-2 h-4 w-4" :style="{ color: selectedPlan.accent }" />
                      {{ checkpoint }}
                    </div>
                  </div>
                </div>
              </article>
            </section>

            <article class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
              <div class="mb-5 flex flex-col justify-between gap-3 md:flex-row md:items-center">
                <div>
                  <div class="flex items-center gap-2">
                    <Route class="h-5 w-5" :style="{ color: selectedPlan.accent }" />
                    <h4 class="text-lg font-bold">课程路径图</h4>
                  </div>
                  <p class="mt-1 text-sm text-text-secondary">开放节点可直接进入，锁定节点会随前置目标完成后解锁。</p>
                </div>
                <div class="flex items-center gap-2">
                  <div class="hidden items-center gap-2 rounded-lg bg-surface-sunken px-3 py-2 text-xs text-text-secondary md:inline-flex">
                    <Timer class="h-3.5 w-3.5" />
                    {{ selectedPlan.duration }} · {{ selectedPlan.depth }}
                  </div>
                  <button
                    type="button"
                    class="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-semibold text-white transition-colors"
                    :style="{ backgroundColor: selectedPlan.accent }"
                    @click="openPath()"
                  >
                    <Map class="h-3.5 w-3.5" />
                    专门路径页
                  </button>
                </div>
              </div>

              <div class="@container pb-2">
                <div class="relative grid grid-cols-2 gap-4 @cozy:grid-cols-3 @wide:grid-cols-6">
                  <div class="absolute left-10 right-10 top-10 hidden h-1 rounded-full bg-surface-sunken @wide:block" />
                  <button
                    v-for="node in selectedPlan.nodes"
                    :key="node.title"
                    type="button"
                    class="relative flex min-h-32 flex-col items-center rounded-lg border bg-surface p-4 text-center transition-colors"
                    :class="node.status === 'locked' ? 'border-border-subtle text-text-tertiary' : 'border-border-subtle hover:border-blue-200'"
                  >
                    <span
                      class="z-10 flex h-12 w-12 items-center justify-center rounded-full border-2 bg-surface text-sm font-bold"
                      :style="{ borderColor: node.status === 'locked' ? '#e2e8f0' : selectedPlan.accent, color: node.status === 'locked' ? '#94a3b8' : selectedPlan.accent }"
                    >
                      <component :is="nodeIcon(node.status)" v-if="node.status !== 'open'" class="h-4 w-4" />
                      <span v-else>{{ node.label }}</span>
                    </span>
                    <strong class="mt-3 text-sm">{{ node.title }}</strong>
                    <span class="mt-2 text-xs">{{ node.status === 'locked' ? '待解锁' : node.status === 'done' ? '已完成' : '可学习' }}</span>
                  </button>
                </div>
              </div>
            </article>

            <section class="grid gap-6 xl:grid-cols-2">
              <article class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
                <div class="mb-4 flex items-center gap-2">
                  <BookOpenText class="h-5 w-5" :style="{ color: selectedPlan.accent }" />
                  <h4 class="text-lg font-bold">精选学习资源</h4>
                </div>
                <div class="space-y-3">
                  <div
                    v-for="resource in selectedPlan.resources"
                    :key="resource.title"
                    class="flex gap-3 rounded-lg border border-border-subtle p-4"
                  >
                    <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg" :style="{ backgroundColor: selectedPlan.soft, color: selectedPlan.accent }">
                      <component :is="resourceIcon(resource.type)" class="h-5 w-5" />
                    </div>
                    <div class="min-w-0 flex-1">
                      <div class="flex items-center justify-between gap-3">
                        <h5 class="font-semibold">{{ resource.title }}</h5>
                        <span class="shrink-0 text-xs text-text-tertiary">{{ resource.minutes }} 分钟</span>
                      </div>
                      <p class="mt-1 text-sm leading-6 text-text-secondary">{{ resource.description }}</p>
                    </div>
                  </div>
                </div>
              </article>

              <article class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
                <div class="mb-4 flex items-center gap-2">
                  <FlaskConical class="h-5 w-5" :style="{ color: selectedPlan.accent }" />
                  <h4 class="text-lg font-bold">实验项目</h4>
                </div>
                <div class="space-y-3">
                  <div
                    v-for="project in selectedPlan.projects"
                    :key="project.title"
                    class="rounded-lg border border-border-subtle p-4"
                  >
                    <div class="flex items-start justify-between gap-4">
                      <div>
                        <h5 class="font-semibold">{{ project.title }}</h5>
                        <p class="mt-2 text-sm leading-6 text-text-secondary">{{ project.description }}</p>
                      </div>
                      <span class="shrink-0 rounded-lg px-2.5 py-1 text-xs font-medium" :style="{ backgroundColor: selectedPlan.soft, color: selectedPlan.accent }">{{ project.output }}</span>
                    </div>
                  </div>
                </div>
              </article>
            </section>

            <article class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
              <div class="mb-4 flex items-center gap-2">
                <CalendarDays class="h-5 w-5" :style="{ color: selectedPlan.accent }" />
                <h4 class="text-lg font-bold">建议学习节奏</h4>
              </div>
              <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                <div
                  v-for="(item, index) in selectedPlan.weeklyPlan"
                  :key="item"
                  class="rounded-lg border border-border-subtle p-4 text-sm leading-6 text-text-secondary"
                >
                  <span class="mb-2 inline-flex h-6 w-6 items-center justify-center rounded-full text-xs font-bold text-white" :style="{ backgroundColor: selectedPlan.accent }">{{ index + 1 }}</span>
                  <p>{{ item }}</p>
                </div>
              </div>
            </article>
          </section>
        </div>
    </main>
  </AppShell>
</template>
