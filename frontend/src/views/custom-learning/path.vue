<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  ArrowLeft,
  BookOpenText,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Circle,
  ClipboardCheck,
  ExternalLink,
  FileText,
  FlaskConical,
  Library,
  ListChecks,
  Loader2,
  Lock,
  Map as MapIcon,
  Play,
} from 'lucide-vue-next';
import AppShell from '@/components/layout/AppShell.vue';
import { DEMO_PATH_PLANS, type NodeState, type PathPlan } from './templates';
import MarkdownRenderer from '@/views/ne-full/components/MarkdownRenderer.vue';
import { CustomLearningAPI, type CustomLearningAdjustment, type CustomLearningDashboard, type CustomLearningPlan, type LearningNodeStatus } from '@/api/custom-learning';
import { authState, downloadWithAuth } from '@/utils/request';



interface ExploreResourceCard {
  key: string;
  title: string;
  snippet: string;
  score: string;
  url: string;
}

type LearningTab = 'guide' | 'detail' | 'materials' | 'practice';

const route = useRoute();
const router = useRouter();


const plans: PathPlan[] = DEMO_PATH_PLANS;

const requestedPlanId = computed(() => String(route.params.planId || 'detector-principles'));
const activePlanId = ref(requestedPlanId.value);
const selectedNodeId = ref('');
const remotePlan = ref<CustomLearningPlan | null>(null);
const remoteLoading = ref(false);
const remoteError = ref('');
const completingNodeId = ref('');
const artifactText = ref('');
const artifactSubmitting = ref(false);
const quizLoading = ref(false);
const quizQuestions = ref<any[]>([]);
const quizAnswers = ref<Record<string, any>>({});
const quizResult = ref<any | null>(null);
const unansweredQuestionIds = ref<string[]>([]);
const adjustmentNotice = ref<CustomLearningAdjustment | null>(null);
const exploreLoading = ref(false);
const resourceDownloading = ref('');
const exploreResources = ref<any[]>([]);
const exploreAttempted = ref(false);
const activeLearningTab = ref<LearningTab>('guide');
const nodeDetailLoading = ref(false);
const kbEnrichLoading = ref(false);
const enrichingNodeId = ref('');
const reviewLoading = ref(false);
const reviewError = ref('');
const planReview = ref<{ review_nodes: any[]; weak_points: string[]; latest_attempts: any[]; adjustments: any[] } | null>(null);
const nextActionLoading = ref(false);
const latestRecommendation = ref<CustomLearningDashboard['next_recommendation'] | null>(null);
const dashboardStyle = ref<CustomLearningDashboard['style'] | null>(null);
const agentTrace = ref<any[]>([]);
const moduleEffects = ref<NonNullable<CustomLearningDashboard['module_effects']>>([]);
// 交卷后不再自动跳走：记录已解锁的下一节点，由用户显式点击跳转。
const pendingNextNodeId = ref('');
// 学习产物提交后的评分反馈。
const artifactResult = ref<any | null>(null);
// 手动完成的两步确认（替代原生 window.confirm）。
const confirmingComplete = ref(false);
let confirmCompleteTimer: number | null = null;
// 练习开始时间，交卷时上报用时。
const quizStartedAt = ref<number | null>(null);
// 移动端节点队列抽屉（xl 及以上始终展开）。
const nodeQueueOpen = ref(false);
// 后台知识库详解失败时的节点内联提示（不再污染页面级错误条）。
const enrichError = ref('');

function mapNodeState(status: LearningNodeStatus | string | undefined): NodeState {
  if (status === 'done') return 'done';
  if (status === 'locked') return 'locked';
  if (status === 'review') return 'review';
  if (status === 'learning') return 'learning';
  return 'open';
}

function mapRemotePlan(plan: CustomLearningPlan): PathPlan {
  const nodes = (plan.nodes || []).map((node, index) => ({
    id: node.node_id,
    title: node.title,
    subtitle: node.subtitle || node.objective || '',
    day: node.day || `Day ${index + 1}`,
    state: mapNodeState(node.status),
    isReviewChild: node.node_type === 'review',
    resource: node.resource || '',
    task: node.task || '',
    check: node.check || '',
  }));

  return {
    id: plan.plan_id,
    title: plan.title,
    category: plan.course_type || '核电子学',
    duration: `${plan.duration_days}天`,
    difficulty: plan.depth || '自定义',
    accent: plan.accent || '#2563eb',
    surface: plan.surface || '#eff6ff',
    description: plan.summary || plan.goal || '',
    outcomes: (plan.metadata?.objectives || []).slice(0, 3).map((item: any) => item.title || item.description).filter(Boolean),
    nodes,
  };
}

const activePlan = computed(() => {
  if (remotePlan.value) return mapRemotePlan(remotePlan.value);
  return plans.find((plan) => plan.id === activePlanId.value) || plans[0];
});
const selectedNode = computed(() => activePlan.value.nodes.find((node) => node.id === selectedNodeId.value) || activePlan.value.nodes[0]);
const selectedRemoteNode = computed(() => remotePlan.value?.nodes?.find((node) => node.node_id === selectedNode.value?.id) || null);
const openCount = computed(() => activePlan.value.nodes.filter((node) => node.state !== 'locked').length);
const reviewCount = computed(() => remotePlan.value?.stats?.review_nodes ?? planReview.value?.review_nodes?.length ?? 0);
const doneCount = computed(() => remotePlan.value?.stats?.done_nodes ?? activePlan.value.nodes.filter((node) => node.state === 'done').length);
const completionPercent = computed(() => activePlan.value.nodes.length ? Math.round((openCount.value / activePlan.value.nodes.length) * 100) : 0);
const displayProgress = computed(() => remotePlan.value?.progress ?? completionPercent.value);
const isReviewChildNode = computed(() => selectedRemoteNode.value?.node_type === 'review');
const quizPassed = computed(() => quizResult.value != null && Number(quizResult.value.score ?? 0) >= 70);
const reviewBanner = computed(() => {
  if (!isRemotePlan.value || reviewCount.value <= 0) return null;
  const weakPoints = planReview.value?.weak_points?.length ?? 0;
  const firstReview = planReview.value?.review_nodes?.[0];
  return {
    count: reviewCount.value,
    weakPoints,
    hint: firstReview
      ? `当前有 ${reviewCount.value} 个节点待复习，建议先完成「${firstReview.title}」。`
      : `当前有 ${reviewCount.value} 个节点待复习，请按推荐步骤完成补强。`,
  };
});
const isRemotePlan = computed(() => !!remotePlan.value);
const isDraftPlan = computed(() => remotePlan.value?.status === 'draft');
const isDemoPlan = computed(() => !isRemotePlan.value && plans.some((plan) => plan.id === requestedPlanId.value));
const needsLogin = computed(() => !authState.value.token && isLikelyRemotePlanId(requestedPlanId.value));
const isLikelyRemotePlanId = (planId: string) => /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(planId);
const normalizedExploreResources = computed<ExploreResourceCard[]>(() => {
  return (exploreResources.value || []).slice(0, 8).map((resource: any, index: number) => ({
    key: String(resource?.doc_id || resource?.doc_name || resource?.title || resource?.file_name || index),
    title: resourceTitle(resource, index),
    snippet: resourceSnippet(resource),
    score: resourceScore(resource),
    url: resourceUrl(resource),
  }));
});
const hasExploreResources = computed(() => normalizedExploreResources.value.length > 0);
const hasExploreMaterials = computed(() => hasExploreMap.value || hasExploreResources.value);
const canRetrieveKb = computed(() => (remotePlan.value?.kb_ids?.length ?? 0) > 0);
// content_status.detail_ready 兜底：瘦身响应下正文尚未补水时不误判为「缺详解」。
const hasKbDetail = computed(() => Boolean(
  selectedRemoteNode.value?.explore_cache?.detail_content
  || selectedRemoteNode.value?.content_status?.detail_ready,
));
const exploreMapChildren = computed(() => {
  const children = selectedRemoteNode.value?.explore_cache?.map?.children;
  return Array.isArray(children) ? children : [];
});
const hasExploreMap = computed(() => exploreMapChildren.value.length > 0);
const detailSources = computed(() => {
  const sources = selectedRemoteNode.value?.explore_cache?.detail_sources;
  return Array.isArray(sources) ? sources.slice(0, 6) : [];
});
const kbDetailError = computed(() => String(selectedRemoteNode.value?.explore_cache?.detail_error || ''));
const resourceErrors = computed(() => {
  const errors = selectedRemoteNode.value?.explore_cache?.resource_errors;
  return Array.isArray(errors) ? errors : [];
});
const resourceErrorKbLabels = computed(() => resourceErrors.value.map((item: any) => item?.kb_id).filter(Boolean).join('、') || '未知');
const detailLoadingHint = computed(() => {
  if (canRetrieveKb.value && !hasKbDetail.value) return '正在结合知识库生成详解，请稍候...';
  return '正在生成节点详解...';
});
const contentLoading = computed(() => nodeDetailLoading.value);
const needsKbDetail = computed(() => canRetrieveKb.value && !hasKbDetail.value);
const nodeMastery = computed(() => Number(selectedRemoteNode.value?.mastery ?? 0));
const questionFeedback = computed<Record<string, any>>(() => {
  const items = quizResult.value?.diagnosis?.question_results;
  if (!Array.isArray(items)) return {};
  const map: Record<string, any> = {};
  for (const item of items) {
    if (item && item.id != null) map[String(item.id)] = item;
  }
  return map;
});
const pendingNextNode = computed(() => {
  if (!pendingNextNodeId.value) return null;
  return remotePlan.value?.nodes?.find((node) => node.node_id === pendingNextNodeId.value) || null;
});

function feedbackVerdictLabel(verdict: string) {
  if (verdict === 'correct') return '答对了';
  if (verdict === 'partial') return '部分正确';
  return '未答对';
}

function feedbackToneClass(verdict: string) {
  if (verdict === 'correct') return 'border-emerald-100 bg-emerald-50/70 text-emerald-800 dark:border-emerald-900/50 dark:bg-emerald-950/20 dark:text-emerald-200';
  if (verdict === 'partial') return 'border-amber-100 bg-amber-50/70 text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200';
  return 'border-rose-100 bg-rose-50/70 text-rose-800 dark:border-rose-900/50 dark:bg-rose-950/20 dark:text-rose-200';
}

function quizDurationSeconds() {
  if (!quizStartedAt.value) return undefined;
  const seconds = Math.round((Date.now() - quizStartedAt.value) / 1000);
  return seconds > 0 && seconds < 6 * 3600 ? seconds : undefined;
}
const agentInsightCards = computed(() => {
  return (agentTrace.value || [])
    .filter((item) => item?.agent)
    .slice(0, 4)
    .map((item) => ({
      agent: String(item.agent || ''),
      decision: String(item.decision || item.policy || '学习策略'),
      reason: String(item.reason || item.impact || '根据当前计划状态给出学习建议'),
      confidence: typeof item.confidence === 'number' ? Math.round(item.confidence * 100) : null,
    }));
});

function moduleEffectIcon(id: string) {
  if (id === 'explore') return MapIcon;
  if (id === 'learning_style') return BookOpenText;
  if (id === 'practice') return ClipboardCheck;
  if (id === 'response_speed') return Loader2;
  return ListChecks;
}

function moduleEffectToneClass(status: string) {
  if (status === 'risk') return 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200';
  if (status === 'active') return 'border-blue-100 bg-blue-50 text-blue-800 dark:border-blue-900/50 dark:bg-blue-950/20 dark:text-blue-200';
  if (status === 'ready') return 'border-emerald-100 bg-emerald-50 text-emerald-800 dark:border-emerald-900/50 dark:bg-emerald-950/20 dark:text-emerald-200';
  return 'border-border-subtle bg-surface-sunken text-text-secondary';
}

const nodeTaskItems = computed(() => {
  const fromList = (selectedRemoteNode.value?.tasks || []).map((item) => String(item).trim()).filter(Boolean);
  if (fromList.length) return fromList;
  return String(selectedNode.value?.task || '')
    .split('；')
    .map((item) => item.trim())
    .filter(Boolean);
});
const nodeCheckItems = computed(() => {
  const fromList = (selectedRemoteNode.value?.checks || []).map((item) => String(item).trim()).filter(Boolean);
  if (fromList.length) return fromList;
  const check = String(selectedNode.value?.check || '').trim();
  return check ? [check] : [];
});
const learningTabs = [
  { id: 'guide' as const, label: '学习指引', hint: '明确任务与验收', icon: ListChecks },
  { id: 'detail' as const, label: '节点详解', hint: '核心概念与步骤', icon: BookOpenText },
  { id: 'materials' as const, label: '知识库', hint: '补充阅读资料', icon: Library },
  { id: 'practice' as const, label: '练习检测', hint: '检验掌握程度', icon: ClipboardCheck },
];
const learningStepDone = computed(() => ({
  guide: selectedNode.value?.state !== 'locked',
  detail: Boolean(selectedRemoteNode.value?.explanation || selectedRemoteNode.value?.content_status?.explanation_ready)
    && (!canRetrieveKb.value || hasKbDetail.value),
  materials: hasExploreMaterials.value,
  practice:
    selectedNode.value?.state === 'done'
    || (quizResult.value != null && Number(quizResult.value.score ?? 0) >= 70),
}));
const pendingReviewChild = computed(() => {
  const current = selectedRemoteNode.value;
  if (!remotePlan.value || !current || current.status !== 'review') return null;
  return (remotePlan.value.nodes || []).find(
    (node) => node.parent_node_id === current.node_id
      && node.node_type === 'review'
      && (node.status === 'open' || node.status === 'learning'),
  ) || null;
});
const nodeTitleById = computed<Record<string, string>>(() => {
  const titles: Record<string, string> = {};
  for (const node of remotePlan.value?.nodes || []) {
    titles[node.node_id] = node.title;
  }
  for (const node of planReview.value?.review_nodes || []) {
    titles[node.node_id] = node.title;
  }
  return titles;
});
const primaryAction = computed(() => {
  if (latestRecommendation.value?.label) {
    const action = latestRecommendation.value.action;
    const tab = action === 'goto_review_child'
      ? 'guide' as LearningTab
      : (action === 'enrich_content' || action === 'start_node'
        ? 'detail' as LearningTab
        : (action === 'retry_quiz' || action === 'await_quiz' || action === 'generate_quiz' ? 'practice' as LearningTab : defaultLearningTab()));
    return {
      label: latestRecommendation.value.label,
      tab,
      nodeId: latestRecommendation.value.node_id,
      reason: latestRecommendation.value.reason,
    };
  }
  if (!isRemotePlan.value) return { label: '开始学习', tab: 'guide' as LearningTab };
  if (isDraftPlan.value) return { label: '确认并开始计划', tab: 'guide' as LearningTab };
  if (selectedNode.value?.state === 'locked') return { label: '前置节点未完成', tab: 'guide' as LearningTab };
  if (pendingReviewChild.value) {
    return {
      label: '先完成复习节点',
      tab: 'guide' as LearningTab,
      nodeId: pendingReviewChild.value.node_id,
      reason: `请先完成「${pendingReviewChild.value.title}」`,
    };
  }
  if (isReviewChildNode.value && !selectedRemoteNode.value?.explanation) {
    return { label: '开始复习', tab: 'detail' as LearningTab, reason: '生成复习详解并进入补强学习' };
  }
  if (!selectedRemoteNode.value?.explanation) return { label: '开始学习', tab: 'detail' as LearningTab };
  if (canRetrieveKb.value && !hasKbDetail.value) {
    return { label: '加载知识库详解', tab: 'detail' as LearningTab, reason: '结合绑定知识库生成更完整的讲解' };
  }
  if (canRetrieveKb.value && !hasExploreMap.value && !hasExploreResources.value && !isReviewChildNode.value) {
    return { label: '检索资料', tab: 'materials' as LearningTab };
  }
  if (!quizQuestions.value.length && !quizPassed.value && selectedNode.value?.state !== 'done') {
    return {
      label: isReviewChildNode.value ? '生成复习练习' : (selectedNode.value?.state === 'review' ? '重新生成练习' : '生成练习'),
      tab: 'practice' as LearningTab,
    };
  }
  if (quizQuestions.value.length && !quizPassed.value) {
    return {
      label: isReviewChildNode.value ? '提交复习练习' : (selectedNode.value?.state === 'review' ? '重新提交练习' : '提交练习'),
      tab: 'practice' as LearningTab,
    };
  }
  if (selectedNode.value?.state === 'done') return { label: '已完成，查看下一节点', tab: 'guide' as LearningTab };
  return { label: '继续学习', tab: defaultLearningTab() };
});

function defaultLearningTab(): LearningTab {
  if (!isRemotePlan.value || selectedNode.value?.state === 'locked') return 'guide';
  if (quizQuestions.value.length || quizResult.value) return 'practice';
  if (selectedRemoteNode.value?.explanation) return 'detail';
  if (hasExploreResources.value) return 'materials';
  return 'guide';
}

function openLearningTab(tab: LearningTab) {
  if (isRemotePlan.value && selectedNode.value?.state === 'locked') return;
  activeLearningTab.value = tab;
}

async function runPrimaryAction() {
  if (!isRemotePlan.value) return;
  if (!isDraftPlan.value && selectedNode.value?.state === 'locked') return;
  const action = primaryAction.value;
  const targetNodeId = action.nodeId;
  if (targetNodeId && targetNodeId !== selectedNode.value?.id) {
    await focusLearningNode(targetNodeId, action.tab);
    return;
  }
  if (action.tab) openLearningTab(action.tab);
  if (action.label === '加载知识库详解') {
    await retryKbDetail();
    return;
  }
  if (action.tab === 'practice' && quizQuestions.value.length && !quizPassed.value) {
    if (!validateQuizAnswers()) return;
    await runNextAction({ answers: quizAnswers.value, duration_seconds: quizDurationSeconds(), prefer_materials: false });
    return;
  }
  const preferMaterials = action.label === '检索资料';
  await runNextAction({ prefer_materials: preferMaterials });
}

async function retryKbDetail() {
  if (!isRemotePlan.value || !selectedNode.value || selectedNode.value.state === 'locked') return;
  remoteError.value = '';
  openLearningTab('detail');
  await enrichNodeInBackground(true);
}

function nodeStateLabel(state: NodeState, isReviewChild = false) {
  if (state === 'locked') return '未解锁';
  if (state === 'done') return '已完成';
  if (isReviewChild) return '复习补强';
  if (state === 'review') return '需复习';
  if (state === 'learning') return '学习中';
  return '待学习';
}

function resolveFocusNodeId(
  refreshed: CustomLearningPlan,
  response: {
    unlocked_node_ids?: string[];
    adjustment?: CustomLearningAdjustment | null;
    plan?: CustomLearningPlan;
  },
) {
  return response.unlocked_node_ids?.[0]
    || response.adjustment?.after_snapshot?.created_node_id
    || refreshed.current_node_id
    || response.plan?.current_node_id
    || '';
}

function jumpToReviewNode(nodeId: string, tab: LearningTab = 'guide') {
  void focusLearningNode(nodeId, tab);
}

function goToLogin() {
  const redirect = route.fullPath;
  router.push({ path: '/login', query: { redirect } });
}

function syncSelectedNodeContext() {
  hydrateExploreFromNode();
  const remote = selectedRemoteNode.value;
  exploreAttempted.value = hasExploreMaterials.value || Boolean(remote?.content_status?.explore_ready);
}

function mergePlanNode(node: NonNullable<CustomLearningPlan['nodes']>[number], planPatch?: Partial<CustomLearningPlan>) {
  if (!remotePlan.value?.nodes) return;
  remotePlan.value = {
    ...remotePlan.value,
    ...planPatch,
    nodes: remotePlan.value.nodes.map((item) => (item.node_id === node.node_id ? node : item)),
  };
  syncSelectedNodeContext();
}

async function enrichNodeInBackground(force = false) {
  const nodeId = selectedNode.value?.id;
  if (!isRemotePlan.value || !nodeId || selectedNode.value?.state === 'locked') return;
  if (!canRetrieveKb.value) return;
  const remote = selectedRemoteNode.value;
  if (!force && remote?.explore_cache?.detail_content) return;
  if (enrichingNodeId.value === nodeId) return;

  enrichingNodeId.value = nodeId;
  kbEnrichLoading.value = true;
  enrichError.value = '';
  try {
    const response = await CustomLearningAPI.enrichNode(nodeId, force);
    if (selectedNodeId.value !== nodeId) return;
    mergePlanNode(response.node, response.plan);
  } catch (error) {
    // 后台任务失败只在对应节点的详解页内联提示，避免用户切换节点后错误条突兀出现。
    if (selectedNodeId.value === nodeId) {
      enrichError.value = friendlyError(error, '知识库详解加载失败，已保留通用学习导引');
    }
  } finally {
    if (enrichingNodeId.value === nodeId) enrichingNodeId.value = '';
    if (selectedNodeId.value === nodeId) kbEnrichLoading.value = false;
  }
}

async function restoreNodeSession(nodeId: string) {
  if (!isRemotePlan.value) return;
  const remote = remotePlan.value?.nodes?.find((node) => node.node_id === nodeId);
  if (!remote) return;
  const status = (remote.content_status || {}) as NonNullable<typeof remote.content_status>;
  const cache = remote.explore_cache || {};
  // 列表响应对非选中节点做了瘦身：正文按 content_status 标志在选中时补水。
  const needsBody = Boolean(
    (status.explanation_ready && !remote.explanation)
    || (status.detail_ready && !cache.detail_content)
    || (status.map_ready && !cache.map)
    || (status.explore_ready && !(remote.resources || []).length),
  );
  const needsQuiz = Boolean(status.quiz_ready);
  if (!needsBody && !needsQuiz) return;
  try {
    const response = await CustomLearningAPI.node(nodeId, { include_quiz: needsQuiz });
    if (selectedNodeId.value !== nodeId) return;
    if (response.node) mergePlanNode(response.node);
    // 补水后延续原有行为：已有导引但缺知识库详解时后台生成。
    if (
      canRetrieveKb.value
      && response.node?.explanation
      && !response.node?.explore_cache?.detail_content
      && response.node?.status !== 'locked'
    ) {
      void enrichNodeInBackground();
    }
    if (needsQuiz) {
      quizQuestions.value = response.questions || [];
      quizAnswers.value = {};
      if (quizQuestions.value.length) quizStartedAt.value = Date.now();
      const attempt = planReview.value?.latest_attempts?.find(
        (item) => item.node_id === nodeId && item.action_type === 'quiz',
      );
      quizResult.value = attempt?.score != null
        ? { score: attempt.score, diagnosis: attempt.diagnosis || {} }
        : null;
    }
  } catch {
    // 恢复失败不阻断浏览。
  }
}

function getUnansweredQuestionIds() {
  return quizQuestions.value
    .filter((question) => {
      const answer = quizAnswers.value[question.id];
      return answer === undefined || answer === null || String(answer).trim() === '';
    })
    .map((question) => String(question.id));
}

function isQuestionUnanswered(questionId: string) {
  return unansweredQuestionIds.value.includes(questionId);
}

function validateQuizAnswers(): boolean {
  if (!quizQuestions.value.length) {
    unansweredQuestionIds.value = [];
    remoteError.value = '请先生成练习';
    activeLearningTab.value = 'practice';
    return false;
  }
  const missingIds = getUnansweredQuestionIds();
  if (missingIds.length) {
    unansweredQuestionIds.value = missingIds;
    remoteError.value = `还有 ${missingIds.length} 道题未作答，请补全后再提交`;
    activeLearningTab.value = 'practice';
    void nextTick(() => {
      document.getElementById(`quiz-question-${missingIds[0]}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    return false;
  }
  unansweredQuestionIds.value = [];
  remoteError.value = '';
  return true;
}

async function refreshDashboard(nodeId = selectedNodeId.value) {
  if (!remotePlan.value) return null;
  const response = await CustomLearningAPI.dashboard(remotePlan.value.plan_id, nodeId || undefined);
  applyDashboard(response);
  return response;
}

async function focusLearningNode(nodeId: string, tab?: LearningTab) {
  selectedNodeId.value = nodeId;
  nodeQueueOpen.value = false;
  if (tab) activeLearningTab.value = tab;
  if (!isRemotePlan.value || !remotePlan.value) return;
  try {
    const response = await CustomLearningAPI.dashboard(remotePlan.value.plan_id, nodeId);
    latestRecommendation.value = response.next_recommendation || null;
    planReview.value = {
      review_nodes: response.review?.review_nodes || [],
      weak_points: response.review?.weak_points || [],
      latest_attempts: response.review?.latest_attempts || [],
      adjustments: response.review?.adjustments || [],
    };
  } catch {
    // 节点聚焦失败时保留本地视图，不影响浏览。
  }
}

function stripNoisyText(value: unknown) {
  let text = String(value || '');
  text = text
    .replace(/<!doctype[^>]*>/gi, ' ')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/&nbsp;|&#160;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;|&gt;|&quot;|&#39;/gi, ' ')
    .replace(/\b(charset|viewport|initial-scale|device-width|base href|content=|meta name)\b[^，。；\n]*/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return text;
}

function looksLikeMachineId(value: string) {
  const text = value.trim();
  return /^doc[-_][a-z0-9-]{12,}$/i.test(text)
    || /^[a-f0-9]{20,}$/i.test(text)
    || /^chunk[-_][a-z0-9-]{10,}$/i.test(text);
}

function firstMeaningfulText(...values: unknown[]) {
  for (const value of values) {
    const text = stripNoisyText(value);
    if (text && !looksLikeMachineId(text)) return text;
  }
  return '';
}

function shortText(value: unknown, max = 120) {
  const text = stripNoisyText(value);
  if (!text) return '';
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function resourceTitle(resource: any, index: number) {
  return firstMeaningfulText(resource?.doc_name, resource?.title, resource?.file_name, resource?.name, resource?.source)
    || `关联资料 ${index + 1}`;
}

function resourceSnippet(resource: any) {
  const snippet = firstMeaningfulText(resource?.snippet, resource?.summary, resource?.description, resource?.content);
  if (!snippet || /doctype html|charset|viewport|initial-scale/i.test(snippet)) {
    return '该资源与当前节点相关，可作为补充阅读材料。';
  }
  return shortText(snippet, 150);
}

function resourceScore(resource: any) {
  const score = Number(resource?.score || resource?._score || 0);
  if (!Number.isFinite(score) || score <= 0) return '';
  const percent = score <= 1 ? score * 100 : score;
  return `${Math.min(99, Math.max(1, Math.round(percent)))}%`;
}

function resourceUrl(resource: any) {
  return resource?.download_url || resource?.url || '';
}

async function downloadResource(resource: { key: string; url: string; title: string }) {
  if (!resource.url || resourceDownloading.value) return;
  resourceDownloading.value = resource.key;
  remoteError.value = '';
  try {
    await downloadWithAuth(resource.url, { filename: resource.title });
  } catch (error) {
    remoteError.value = friendlyError(error, '资源下载失败');
  } finally {
    resourceDownloading.value = '';
  }
}

function friendlyError(error: unknown, fallback: string) {
  const message = error instanceof Error ? error.message : String(error || '');
  if (/504|timeout|timed out|gateway/i.test(message)) return '内容生成响应超时，当前内容已保留，请稍后重试。';
  if (/401|unauthorized|登录/i.test(message)) return '登录状态已过期，请重新登录后继续学习。';
  if (/409|前置节点/i.test(message)) return '前置节点尚未完成，请先完成当前路径要求。';
  return fallback;
}

function selectPlan(planId: string) {
  remotePlan.value = null;
  remoteError.value = '';
  activePlanId.value = planId;
  selectedNodeId.value = '';
  router.replace(`/custom-learning/path/${planId}`);
}

function backToCustomLearning() {
  router.push('/custom-learning');
}

function previousPlan() {
  const index = plans.findIndex((plan) => plan.id === activePlan.value.id);
  const next = plans[(index - 1 + plans.length) % plans.length];
  selectPlan(next.id);
}

function nextPlan() {
  const index = plans.findIndex((plan) => plan.id === activePlan.value.id);
  const next = plans[(index + 1) % plans.length];
  selectPlan(next.id);
}

function applyDashboard(dashboard: CustomLearningDashboard) {
  remotePlan.value = dashboard.plan;
  activePlanId.value = dashboard.plan.plan_id;
  latestRecommendation.value = dashboard.next_recommendation || null;
  dashboardStyle.value = dashboard.style || null;
  agentTrace.value = dashboard.agent_trace || [];
  moduleEffects.value = dashboard.module_effects || [];
  planReview.value = {
    review_nodes: dashboard.review?.review_nodes || [],
    weak_points: dashboard.review?.weak_points || [],
    latest_attempts: dashboard.review?.latest_attempts || [],
    adjustments: dashboard.review?.adjustments || [],
  };
  // 交卷（grade_quiz）后停留在被评分的节点上展示结果；
  // 已解锁的下一节点记录到 pendingNextNodeId，由用户显式跳转。
  const isGradeAction = dashboard.action === 'grade_quiz';
  const explicitFocusNodeId = isGradeAction
    ? ''
    : (dashboard.result?.unlocked_node_ids?.[0]
      || dashboard.result?.adjustment?.after_snapshot?.created_node_id
      || '');
  if (isGradeAction) {
    pendingNextNodeId.value = dashboard.result?.unlocked_node_ids?.[0] || '';
  }
  const currentSelectionStillExists = Boolean(
    selectedNodeId.value
    && dashboard.plan.nodes?.some((node) => node.node_id === selectedNodeId.value),
  );
  const preferredNodeId = explicitFocusNodeId
    || (currentSelectionStillExists ? selectedNodeId.value : '')
    || dashboard.next_recommendation?.node_id
    || dashboard.current_node?.node_id
    || dashboard.plan.current_node_id
    || dashboard.plan.nodes?.[0]?.node_id
    || '';
  selectedNodeId.value = preferredNodeId;
  const currentNode = dashboard.plan.nodes?.find((node) => node.node_id === preferredNodeId) || dashboard.current_node || null;
  exploreResources.value = Array.isArray(currentNode?.resources) ? currentNode.resources : [];
  exploreAttempted.value = Boolean(
    currentNode?.content_status?.explore_ready
    || currentNode?.content_status?.map_ready
    || exploreResources.value.length
    || currentNode?.explore_cache?.map?.children?.length,
  );
  if (dashboard.result?.questions) {
    quizQuestions.value = dashboard.result.questions || [];
    quizResult.value = null;
    quizStartedAt.value = Date.now();
  }
  if (dashboard.result?.attempt) {
    quizResult.value = dashboard.result.attempt;
    adjustmentNotice.value = dashboard.result.adjustment || null;
  }
  if (dashboard.action === 'start_node' || dashboard.action === 'enrich_content') activeLearningTab.value = 'detail';
  else if (dashboard.action === 'explore_node') activeLearningTab.value = 'materials';
  else if (['generate_quiz', 'await_quiz', 'grade_quiz', 'retry_quiz'].includes(dashboard.action || '')) activeLearningTab.value = 'practice';
  else if (dashboard.next_recommendation?.action === 'goto_review_child') activeLearningTab.value = 'guide';
  else if (['retry_quiz', 'await_quiz', 'generate_quiz'].includes(dashboard.next_recommendation?.action || '')) activeLearningTab.value = 'practice';
  else activeLearningTab.value = defaultLearningTab();

  const focusedNode = dashboard.plan.nodes?.find((node) => node.node_id === selectedNodeId.value);
  // 用 content_status 标志判断（瘦身响应下正文可能未随列表返回）。
  const shouldBackgroundEnrich = (dashboard.plan.kb_ids?.length ?? 0) > 0
    && Boolean(focusedNode?.explanation || focusedNode?.content_status?.explanation_ready)
    && !focusedNode?.content_status?.detail_ready
    && focusedNode?.status !== 'locked';
  if (shouldBackgroundEnrich) void enrichNodeInBackground();
}

async function loadRemotePlan() {
  const planId = requestedPlanId.value;
  if (!authState.value.token) {
    remotePlan.value = null;
    remoteLoading.value = false;
    remoteError.value = isLikelyRemotePlanId(planId) ? '请先登录后访问你的学习计划' : '';
    planReview.value = null;
    latestRecommendation.value = null;
    return;
  }
  if (!isLikelyRemotePlanId(planId)) {
    remotePlan.value = null;
    remoteLoading.value = false;
    remoteError.value = '';
    planReview.value = null;
    latestRecommendation.value = null;
    return;
  }
  remoteLoading.value = true;
  remoteError.value = '';
  try {
    const response = await CustomLearningAPI.dashboard(planId);
    applyDashboard(response);
  } catch (error) {
    remotePlan.value = null;
    if (!plans.some((plan) => plan.id === planId)) {
      remoteError.value = friendlyError(error, '学习计划加载失败');
    }
  } finally {
    remoteLoading.value = false;
  }
}

async function runNextAction(payload: { answers?: Record<string, any>; duration_seconds?: number; prefer_materials?: boolean } = {}) {
  if (!remotePlan.value) return;
  nextActionLoading.value = true;
  remoteError.value = '';
  try {
    const response = await CustomLearningAPI.nextAction(remotePlan.value.plan_id, {
      node_id: selectedNode.value?.id,
      answers: payload.answers,
      duration_seconds: payload.duration_seconds,
      prefer_materials: payload.prefer_materials ?? false,
    });
    if (response.action === 'generate_quiz' || response.action === 'await_quiz') {
      quizAnswers.value = {};
    }
    applyDashboard(response);
    if (response.action === 'start_node') {
      void enrichNodeInBackground();
    }
  } catch (error) {
    remoteError.value = friendlyError(error, '下一步执行失败');
  } finally {
    nextActionLoading.value = false;
  }
}

async function loadPlanReview(planId = remotePlan.value?.plan_id) {
  if (!planId || !isRemotePlan.value) return;
  reviewLoading.value = true;
  reviewError.value = '';
  try {
    const response = await CustomLearningAPI.reviewPlan(planId);
    planReview.value = {
      review_nodes: response.review_nodes || [],
      weak_points: response.weak_points || [],
      latest_attempts: response.latest_attempts || [],
      adjustments: response.adjustments || [],
    };
    if (response.plan?.nodes?.length) {
      remotePlan.value = response.plan;
    }
  } catch (error) {
    reviewError.value = friendlyError(error, '复盘数据加载失败');
  } finally {
    reviewLoading.value = false;
  }
}

function attemptLabel(actionType: string) {
  if (actionType === 'quiz') return '练习';
  if (actionType === 'complete') return '手动完成';
  if (actionType === 'study') return '学习';
  return '记录';
}

function formatDuration(seconds: unknown) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return '';
  if (value >= 90) return `${Math.round(value / 60)} 分钟`;
  return `${Math.round(value)} 秒`;
}

function openExplorePage() {
  const kbIds = remotePlan.value?.kb_ids?.join(',') || '';
  const topic = selectedRemoteNode.value?.title || '';
  const domain = remotePlan.value?.domain || '';
  router.push({
    path: '/explore',
    query: {
      ...(topic ? { topic } : {}),
      ...(kbIds ? { kb_ids: kbIds } : {}),
      ...(domain ? { domain } : {}),
    },
  });
}

async function startSelectedNode() {
  if (!isRemotePlan.value || !selectedNode.value || selectedNode.value.state === 'locked') return;
  nodeDetailLoading.value = true;
  try {
    const response = await CustomLearningAPI.startNode(selectedNode.value.id);
    mergePlanNode(response.node, response.plan);
    activeLearningTab.value = 'detail';
  } catch (error) {
    remoteError.value = friendlyError(error, '节点开始失败');
  } finally {
    nodeDetailLoading.value = false;
  }
  if (needsKbDetail.value) void enrichNodeInBackground();
}

async function beginNodeLearning() {
  if (selectedRemoteNode.value?.explanation && !needsKbDetail.value) {
    openLearningTab('detail');
    if (selectedNode.value?.state === 'open' || selectedNode.value?.state === 'learning') {
      await startSelectedNode();
    }
    return;
  }
  openLearningTab('detail');
  await startSelectedNode();
}

function cancelCompleteConfirm() {
  confirmingComplete.value = false;
  if (confirmCompleteTimer !== null) {
    window.clearTimeout(confirmCompleteTimer);
    confirmCompleteTimer = null;
  }
}

async function completeSelectedNode() {
  if (!isRemotePlan.value || !selectedNode.value || selectedNode.value.state === 'locked') return;
  // 两步内联确认，替代原生 window.confirm。
  if (!confirmingComplete.value) {
    confirmingComplete.value = true;
    if (confirmCompleteTimer !== null) window.clearTimeout(confirmCompleteTimer);
    confirmCompleteTimer = window.setTimeout(() => {
      confirmingComplete.value = false;
      confirmCompleteTimer = null;
    }, 5000);
    return;
  }
  cancelCompleteConfirm();
  completingNodeId.value = selectedNode.value.id;
  remoteError.value = '';
  try {
    const response = await CustomLearningAPI.completeNode(selectedNode.value.id, 100);
    adjustmentNotice.value = response.adjustment || null;
    await refreshDashboard(resolveFocusNodeId(response.plan, response) || selectedNode.value.id);
    void loadPlanReview(response.plan.plan_id);
  } catch (error) {
    remoteError.value = friendlyError(error, '节点完成失败');
  } finally {
    completingNodeId.value = '';
  }
}

function jumpToPendingNextNode() {
  const nodeId = pendingNextNodeId.value;
  pendingNextNodeId.value = '';
  if (nodeId) void focusLearningNode(nodeId, 'guide');
}

async function submitLearningArtifact() {
  if (!isRemotePlan.value || !selectedNode.value || selectedNode.value.state === 'locked') return;
  const text = artifactText.value.trim();
  if (!text) {
    remoteError.value = '请先填写学习产物或复盘说明';
    activeLearningTab.value = 'guide';
    return;
  }
  const submittedNodeId = selectedNode.value.id;
  artifactSubmitting.value = true;
  remoteError.value = '';
  try {
    const response = await CustomLearningAPI.completeNode(submittedNodeId, null, {
      artifact_text: text,
      artifact_type: 'learning_note',
      submitted_at: new Date().toISOString(),
    });
    adjustmentNotice.value = response.adjustment || null;
    artifactText.value = '';
    // 停留在当前节点展示产物评分反馈，下一节点由用户显式跳转。
    await refreshDashboard(submittedNodeId);
    artifactResult.value = response.attempt || null;
    pendingNextNodeId.value = response.unlocked_node_ids?.[0] || '';
    void loadPlanReview(response.plan.plan_id);
  } catch (error) {
    remoteError.value = friendlyError(error, '学习产物提交失败');
  } finally {
    artifactSubmitting.value = false;
  }
}

async function loadQuiz() {
  if (!isRemotePlan.value || !selectedNode.value || selectedNode.value.state === 'locked') return;
  const force = quizQuestions.value.length > 0;
  activeLearningTab.value = 'practice';
  quizLoading.value = true;
  remoteError.value = '';
  quizResult.value = null;
  unansweredQuestionIds.value = [];
  try {
    const response = await CustomLearningAPI.quiz(selectedNode.value.id, { force });
    quizQuestions.value = response.questions || [];
    quizAnswers.value = {};
    quizStartedAt.value = Date.now();
  } catch (error) {
    remoteError.value = friendlyError(error, force ? '练习重生成失败' : '练习生成失败');
  } finally {
    quizLoading.value = false;
  }
}

async function submitQuiz() {
  if (!isRemotePlan.value || !selectedNode.value) return;
  if (!validateQuizAnswers()) return;
  const gradedNodeId = selectedNode.value.id;
  quizLoading.value = true;
  remoteError.value = '';
  try {
    const response = await CustomLearningAPI.gradeQuiz(gradedNodeId, quizAnswers.value, quizDurationSeconds());
    const attempt = response.attempt;
    const adjustment = response.adjustment || null;
    // 停留在被评分的节点上展示结果；下一节点由用户显式跳转。
    await refreshDashboard(gradedNodeId);
    quizResult.value = attempt;
    adjustmentNotice.value = adjustment;
    pendingNextNodeId.value = response.unlocked_node_ids?.[0] || '';
    void loadPlanReview(response.plan.plan_id);
  } catch (error) {
    remoteError.value = friendlyError(error, '练习提交失败');
  } finally {
    quizLoading.value = false;
  }
}

function hydrateExploreFromNode() {
  const node = selectedRemoteNode.value;
  if (!node) {
    exploreResources.value = [];
    return;
  }
  exploreResources.value = Array.isArray(node.resources) ? node.resources : [];
}

async function exploreSelectedNode() {
  if (!isRemotePlan.value || !selectedNode.value || selectedNode.value.state === 'locked') return;
  exploreLoading.value = true;
  exploreAttempted.value = true;
  remoteError.value = '';
  activeLearningTab.value = 'materials';
  try {
    const response = await CustomLearningAPI.exploreNode(selectedNode.value.id);
    exploreResources.value = response.resources || [];
    if (response.node) mergePlanNode(response.node);
  } catch (error) {
    remoteError.value = friendlyError(error, '知识库资料检索失败');
  } finally {
    exploreLoading.value = false;
  }
}

onMounted(() => {
  void loadRemotePlan();
});

watch(
  () => route.params.planId,
  () => {
    activePlanId.value = requestedPlanId.value;
    selectedNodeId.value = '';
    void loadRemotePlan();
  },
);

watch(
  quizAnswers,
  () => {
    if (!unansweredQuestionIds.value.length) return;
    unansweredQuestionIds.value = getUnansweredQuestionIds();
  },
  { deep: true },
);

watch(
  () => selectedNode.value?.id,
  (nodeId) => {
    quizQuestions.value = [];
	    quizAnswers.value = {};
	    quizResult.value = null;
	    artifactText.value = '';
	    unansweredQuestionIds.value = [];
    adjustmentNotice.value = null;
    artifactResult.value = null;
    pendingNextNodeId.value = '';
    quizStartedAt.value = null;
    enrichError.value = '';
    cancelCompleteConfirm();
    syncSelectedNodeContext();
    void (async () => {
      if (nodeId) await restoreNodeSession(nodeId);
      if (selectedNodeId.value === nodeId) {
        activeLearningTab.value = defaultLearningTab();
      }
    })();
  },
);
</script>

<template>
  <AppShell title="学习工作台" brand-name="核电子学智能助手" brand-icon="Atom">
    <template #actions>
      <button
        v-if="!isRemotePlan"
        type="button"
        class="inline-flex size-8 items-center justify-center rounded-lg border border-border-subtle text-text-secondary transition-colors hover:border-accent/30 hover:text-accent"
        title="上一个计划"
        aria-label="上一个计划"
        @click="previousPlan"
      >
        <ChevronLeft class="size-4" />
      </button>
      <button
        v-if="!isRemotePlan"
        type="button"
        class="inline-flex size-8 items-center justify-center rounded-lg border border-border-subtle text-text-secondary transition-colors hover:border-accent/30 hover:text-accent"
        title="下一个计划"
        aria-label="下一个计划"
        @click="nextPlan"
      >
        <ChevronRight class="size-4" />
      </button>
    </template>

    <main class="custom-scrollbar min-h-0 flex-1 overflow-y-auto bg-surface-sunken">
        <section class="mx-auto max-w-[1500px] px-4 py-4 pb-24 md:px-6 xl:pb-4">
          <div class="mb-4 rounded-lg border border-border-subtle bg-surface p-4 shadow-sm">
            <div class="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
              <div class="min-w-0">
                <div class="mb-2 flex flex-wrap items-center gap-2">
                  <span class="rounded-md px-2.5 py-1 text-xs font-semibold" :style="{ color: activePlan.accent, backgroundColor: activePlan.surface }">{{ activePlan.category }}</span>
                  <span class="text-xs font-semibold text-text-tertiary">{{ activePlan.duration }}</span>
                  <span class="text-xs font-semibold text-text-tertiary">{{ activePlan.difficulty }}</span>
                </div>
                <h2 class="truncate text-2xl font-bold text-text-primary dark:text-white">{{ activePlan.title }}</h2>
                <p class="mt-1 max-w-3xl text-sm leading-6 text-text-secondary">{{ activePlan.description }}</p>
                <p v-if="remoteLoading" class="mt-2 text-sm text-text-tertiary">正在加载学习计划...</p>
                <p v-else-if="remoteError" class="mt-2 text-sm text-rose-500">{{ remoteError }}</p>
                <p v-else-if="needsLogin" class="mt-2 text-sm text-amber-600 dark:text-amber-300">该学习计划需要登录后访问，登录后可继续学习、练习和复盘。</p>
                <p v-else-if="isDemoPlan" class="mt-2 text-sm text-amber-600 dark:text-amber-300">当前为内置演示路径，不含个人学习记录。请从「自定义学习」首页进入你的计划。</p>
              </div>
              <div class="grid min-w-[280px] grid-cols-3 gap-2">
                <div class="rounded-lg bg-surface-sunken p-3 text-center">
                  <div class="text-xl font-bold" :style="{ color: activePlan.accent }">{{ displayProgress }}%</div>
                  <div class="mt-1 text-xs text-text-secondary">进度</div>
                </div>
                <div class="rounded-lg bg-surface-sunken p-3 text-center">
                  <div class="text-xl font-bold" :class="reviewCount > 0 ? 'text-amber-600' : 'text-emerald-600'">{{ reviewCount > 0 ? reviewCount : doneCount }}</div>
                  <div class="mt-1 text-xs text-text-secondary">{{ reviewCount > 0 ? '待复习' : '已完成' }}</div>
                </div>
                <div class="rounded-lg bg-surface-sunken p-3 text-center">
                  <div class="text-xl font-bold text-text-secondary">{{ activePlan.nodes.length }}</div>
                  <div class="mt-1 text-xs text-text-secondary">节点</div>
                </div>
              </div>
            </div>
            <div v-if="needsLogin" class="mt-4 flex flex-col gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-4 text-sm text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-100 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <p class="font-semibold">需要登录才能使用学习工作台</p>
                <p class="mt-1 text-xs leading-5 opacity-90">登录后可加载你的计划、记录练习结果，并自动插入复习节点。</p>
              </div>
              <button
                type="button"
                class="shrink-0 rounded-lg bg-amber-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-amber-700"
                @click="goToLogin"
              >
                去登录
              </button>
            </div>
            <div v-else-if="reviewBanner" class="mt-4 flex flex-col gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-100 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <p class="font-semibold">复习闭环提醒</p>
                <p class="mt-1 text-xs leading-5 opacity-90">{{ reviewBanner.hint }}</p>
              </div>
              <button
                v-if="planReview?.review_nodes?.[0]"
                type="button"
                class="shrink-0 rounded-lg bg-amber-600 px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-amber-700"
                @click="jumpToReviewNode(planReview.review_nodes[0].node_id)"
              >
                前往复习节点
              </button>
            </div>
            <div class="mt-4 h-2 overflow-hidden rounded-full bg-surface-sunken">
              <div class="h-full rounded-full transition-all" :style="{ width: `${displayProgress}%`, backgroundColor: activePlan.accent }" />
            </div>
          </div>

          <div v-if="!isRemotePlan" class="mb-4 flex gap-2 overflow-x-auto rounded-lg border border-border-subtle bg-surface p-2 shadow-sm">
            <button
              v-for="plan in plans"
              :key="plan.id"
              type="button"
              class="min-w-40 rounded-md px-3 py-2 text-sm font-semibold transition-colors"
              :class="plan.id === activePlan.id ? 'text-white' : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary dark:hover:text-white'"
              :style="plan.id === activePlan.id ? { backgroundColor: activePlan.accent } : undefined"
              @click="selectPlan(plan.id)"
            >
              {{ plan.title }}
            </button>
          </div>

          <div class="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)_360px]">
            <aside class="rounded-lg border border-border-subtle bg-surface shadow-sm">
              <div class="border-b border-border-subtle p-4">
                <button
                  type="button"
                  class="flex w-full items-center justify-between gap-2 text-left xl:pointer-events-none"
                  :aria-expanded="nodeQueueOpen"
                  aria-label="展开或收起节点队列"
                  @click="nodeQueueOpen = !nodeQueueOpen"
                >
                  <span class="min-w-0">
                    <span class="block text-sm font-bold">节点队列</span>
                    <span class="mt-1 hidden text-xs text-text-secondary xl:block">从开放节点进入学习，诊断不过会回流到复习节点。</span>
                    <span class="mt-1 block truncate text-xs text-text-secondary xl:hidden">
                      {{ nodeQueueOpen ? '从开放节点进入学习，诊断不过会回流到复习节点。' : `当前：${selectedNode?.title || '未选择节点'}` }}
                    </span>
                  </span>
                  <ChevronRight class="h-4 w-4 shrink-0 text-text-tertiary transition-transform xl:hidden" :class="nodeQueueOpen ? 'rotate-90' : ''" />
                </button>
              </div>
              <div class="max-h-[calc(100vh-250px)] overflow-y-auto p-2" :class="nodeQueueOpen ? '' : 'hidden xl:block'">
                <button
                  v-for="(node, index) in activePlan.nodes"
                  :key="node.id"
                  type="button"
                  class="mb-2 flex w-full gap-3 rounded-lg border p-3 text-left transition-colors"
                  :class="selectedNode.id === node.id ? 'border-blue-200 bg-blue-50 dark:border-blue-900/70 dark:bg-blue-950/30' : 'border-transparent hover:border-border-subtle hover:bg-surface-hover'"
                  @click="focusLearningNode(node.id)"
                >
                  <span class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-xs font-bold" :style="{ backgroundColor: node.state === 'locked' ? '#f1f5f9' : activePlan.surface, color: node.state === 'locked' ? '#94a3b8' : activePlan.accent }">
                    <Lock v-if="node.state === 'locked'" class="h-3.5 w-3.5" />
                    <span v-else>{{ index + 1 }}</span>
                  </span>
                  <span class="min-w-0 flex-1">
                    <span class="block truncate text-sm font-bold">{{ node.title }}</span>
                    <span class="mt-1 line-clamp-2 text-xs leading-5 text-text-secondary">{{ node.subtitle }}</span>
                    <span class="mt-2 inline-flex rounded px-2 py-0.5 text-[11px] font-semibold" :class="node.state === 'locked' ? 'bg-surface-sunken text-text-tertiary' : node.state === 'done' ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300' : node.state === 'review' ? 'bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300' : node.isReviewChild ? 'bg-accent-soft text-accent' : 'bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300'">
                      {{ nodeStateLabel(node.state, node.isReviewChild) }}
                    </span>
                  </span>
                </button>
              </div>
            </aside>

            <section class="space-y-4">
              <article class="overflow-hidden rounded-xl border border-border-subtle bg-surface shadow-sm">
                <div class="border-b border-border-subtle px-5 py-5" :style="{ background: `linear-gradient(135deg, ${activePlan.surface} 0%, transparent 72%)` }">
                  <div class="flex flex-col justify-between gap-4 lg:flex-row lg:items-start">
                    <div class="min-w-0 flex-1">
                      <div class="flex flex-wrap items-center gap-2">
                        <span class="inline-flex rounded-md px-2.5 py-1 text-xs font-semibold" :style="{ color: activePlan.accent, backgroundColor: 'rgba(255,255,255,0.72)' }">{{ selectedNode.day }}</span>
                        <span
                          class="inline-flex rounded-md px-2.5 py-1 text-xs font-semibold"
                          :class="selectedNode.state === 'done' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300' : selectedNode.state === 'review' ? 'bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300' : selectedNode.state === 'locked' ? 'bg-surface-sunken text-text-tertiary' : 'bg-blue-100 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300'"
                        >
                          {{ nodeStateLabel(selectedNode.state, selectedNode.isReviewChild) }}
                        </span>
                        <span v-if="isRemotePlan" class="text-xs font-semibold text-text-secondary">掌握度 {{ nodeMastery }}%</span>
                      </div>
                      <h3 class="mt-3 text-2xl font-bold tracking-tight">{{ selectedNode.title }}</h3>
                      <p class="mt-2 max-w-3xl text-sm leading-6 text-text-secondary">{{ selectedNode.subtitle }}</p>
                      <p v-if="primaryAction.reason" class="mt-2 text-xs leading-5 text-text-secondary">{{ primaryAction.reason }}</p>
                    </div>
                    <div v-if="isRemotePlan" class="flex flex-wrap gap-2">
                      <button
                        type="button"
                        class="inline-flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
                        :style="{ backgroundColor: activePlan.accent }"
                        :disabled="(!isDraftPlan && selectedNode.state === 'locked') || nextActionLoading || nodeDetailLoading || kbEnrichLoading || quizLoading || exploreLoading"
                        @click="runPrimaryAction"
                      >
                        <Loader2 v-if="nextActionLoading || nodeDetailLoading || kbEnrichLoading || quizLoading || exploreLoading" class="h-4 w-4 animate-spin" />
                        <Play v-else class="h-4 w-4" />
                        {{ nextActionLoading || nodeDetailLoading || kbEnrichLoading || quizLoading || exploreLoading ? '处理中' : primaryAction.label }}
                      </button>
                      <button
                        type="button"
                        class="inline-flex items-center gap-2 rounded-lg border px-4 py-2.5 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50"
                        :class="confirmingComplete ? 'border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200' : 'border-border-subtle bg-surface/80 text-text-secondary hover:border-amber-200 hover:text-amber-700'"
                        :disabled="selectedNode.state === 'locked' || selectedNode.state === 'done' || completingNodeId === selectedNode.id"
                        @click="completeSelectedNode"
                      >
                        <CheckCircle2 class="h-4 w-4" />
                        {{ completingNodeId === selectedNode.id ? '提交中' : confirmingComplete ? '再点一次确认完成' : '手动完成' }}
                      </button>
                      <button
                        v-if="confirmingComplete"
                        type="button"
                        class="inline-flex items-center rounded-lg border border-border-subtle bg-surface/80 px-3 py-2.5 text-sm font-semibold text-text-secondary transition-colors hover:text-text-primary"
                        @click="cancelCompleteConfirm"
                      >
                        取消
                      </button>
                    </div>
                  </div>
                  <div v-if="isDraftPlan" class="mt-4 rounded-lg border border-blue-100 bg-blue-50 px-4 py-3 text-sm leading-6 text-blue-700 dark:border-blue-900/60 dark:bg-blue-950/30 dark:text-blue-200">
                    这是待确认的草稿计划。确认开始后会解锁第一个节点，并开始记录学习进度。
                  </div>
                  <div v-if="isRemotePlan" class="mt-4">
                    <div class="mb-2 flex items-center justify-between text-xs font-semibold text-text-secondary">
                      <span>节点掌握度</span>
                      <span>{{ nodeMastery }}%</span>
                    </div>
                    <div class="h-2 overflow-hidden rounded-full bg-surface/70">
                      <div class="h-full rounded-full transition-all" :style="{ width: `${nodeMastery}%`, backgroundColor: activePlan.accent }" />
                    </div>
                  </div>
                </div>

                <div v-if="isRemotePlan" class="border-b border-border-subtle px-3 py-3">
                  <div class="grid grid-cols-2 gap-2 md:grid-cols-4">
                    <button
                      v-for="tab in learningTabs"
                      :key="tab.id"
                      type="button"
                      class="rounded-lg border px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50"
                      :class="activeLearningTab === tab.id ? 'border-blue-200 bg-blue-50 dark:border-blue-900/60 dark:bg-blue-950/30' : 'border-transparent hover:border-border-subtle hover:bg-surface-hover'"
                      :disabled="selectedNode.state === 'locked'"
                      @click="openLearningTab(tab.id)"
                    >
                      <div class="flex items-center gap-2">
                        <component :is="learningStepDone[tab.id] ? CheckCircle2 : tab.icon" class="h-4 w-4 shrink-0" :class="learningStepDone[tab.id] ? 'text-emerald-500' : 'text-text-tertiary'" />
                        <span class="text-sm font-bold">{{ tab.label }}</span>
                      </div>
                      <span class="mt-1 block text-[11px] leading-4 text-text-secondary">{{ tab.hint }}</span>
                    </button>
                  </div>
                </div>

                <div v-if="isRemotePlan" class="p-5">
                  <div v-if="activeLearningTab === 'guide'" class="space-y-4">
                    <div class="rounded-xl border border-border-subtle bg-surface-sunken p-4">
                      <div class="mb-3 flex items-center gap-2 text-sm font-bold">
                        <BookOpenText class="h-4 w-4" :style="{ color: activePlan.accent }" />
                        推荐学习材料
                      </div>
                      <p class="text-sm leading-7 text-text-secondary">{{ selectedNode.resource || '结合课程讲义与知识库资料完成本节点。' }}</p>
                    </div>

                    <div class="rounded-xl border border-border-subtle p-4">
                      <div class="mb-3 flex items-center gap-2 text-sm font-bold">
                        <ListChecks class="h-4 w-4" :style="{ color: activePlan.accent }" />
                        本节点要做的事
                      </div>
                      <ul v-if="nodeTaskItems.length" class="space-y-2">
                        <li v-for="(task, index) in nodeTaskItems" :key="`${task}-${index}`" class="flex gap-3 text-sm leading-6 text-text-secondary">
                          <span class="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold" :style="{ backgroundColor: activePlan.surface, color: activePlan.accent }">{{ index + 1 }}</span>
                          <span>{{ task }}</span>
                        </li>
                      </ul>
                      <p v-else class="text-sm text-text-secondary">暂无任务说明。</p>
                    </div>

	                    <div class="rounded-xl border border-amber-100 bg-amber-50/60 p-4 dark:border-amber-900/40 dark:bg-amber-950/20">
	                      <div class="mb-3 flex items-center gap-2 text-sm font-bold text-amber-800 dark:text-amber-200">
	                        <ClipboardCheck class="h-4 w-4" />
                        通过标准
                      </div>
                      <ul v-if="nodeCheckItems.length" class="space-y-2">
                        <li v-for="(check, index) in nodeCheckItems" :key="`${check}-${index}`" class="flex gap-2 text-sm leading-6 text-amber-900/90 dark:text-amber-100/90">
                          <Circle class="mt-1.5 h-2 w-2 shrink-0 fill-current" />
                          <span>{{ check }}</span>
                        </li>
                      </ul>
	                      <p v-else class="text-sm text-amber-800/80 dark:text-amber-100/80">完成练习并达到 70 分即可通过。</p>
	                    </div>

	                    <div class="rounded-xl border border-border-subtle p-4">
	                      <div class="mb-3 flex items-center gap-2 text-sm font-bold">
	                        <FileText class="h-4 w-4" :style="{ color: activePlan.accent }" />
	                        提交学习产物
	                      </div>
	                      <textarea
	                        v-model="artifactText"
	                        class="min-h-28 w-full rounded-lg border border-border-subtle bg-surface p-3 text-sm leading-6 outline-none transition-colors focus:border-blue-300"
	                        placeholder="写下本节点的学习小结、方案说明、计算过程或复盘结论。系统会按完整度给出基础评价，并更新掌握度。"
	                      />
	                      <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
	                        <p class="text-xs leading-5 text-text-secondary">建议包含：关键概念、判断依据、一个例子和容易混淆点。</p>
	                        <button
	                          type="button"
	                          class="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
	                          :style="{ backgroundColor: activePlan.accent }"
	                          :disabled="artifactSubmitting || selectedNode.state === 'locked' || selectedNode.state === 'done'"
	                          @click="submitLearningArtifact"
	                        >
	                          <Loader2 v-if="artifactSubmitting" class="h-4 w-4 animate-spin" />
	                          <ClipboardCheck v-else class="h-4 w-4" />
	                          {{ artifactSubmitting ? '评价中' : '提交产物' }}
	                        </button>
	                      </div>
	                      <div
	                        v-if="artifactResult"
	                        class="mt-3 rounded-lg border p-3 text-sm leading-6"
	                        :class="Number(artifactResult.score ?? 0) >= 70 ? 'border-emerald-100 bg-emerald-50 text-emerald-800 dark:border-emerald-900/50 dark:bg-emerald-950/20 dark:text-emerald-200' : 'border-amber-100 bg-amber-50 text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200'"
	                      >
	                        <p class="font-semibold">
	                          产物评分 {{ artifactResult.score }} 分，掌握度 {{ artifactResult.mastery_before }}% → {{ artifactResult.mastery_after }}%
	                        </p>
	                        <p v-if="artifactResult.diagnosis?.weak_points?.length" class="mt-1">
	                          待补强：{{ artifactResult.diagnosis.weak_points.join('；') }}
	                        </p>
	                        <p v-if="artifactResult.diagnosis?.suggestions?.length" class="mt-1">
	                          建议：{{ artifactResult.diagnosis.suggestions.join('；') }}
	                        </p>
	                        <button
	                          v-if="pendingNextNode"
	                          type="button"
	                          class="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-emerald-700"
	                          @click="jumpToPendingNextNode"
	                        >
	                          前往下一节点「{{ pendingNextNode.title }}」
	                        </button>
	                      </div>
	                    </div>

	                    <div v-if="isRemotePlan && latestRecommendation?.reason" class="rounded-lg border border-blue-100 bg-blue-50/70 px-4 py-3 text-xs leading-5 text-blue-800 dark:border-blue-900/50 dark:bg-blue-950/20 dark:text-blue-200">
	                      推荐下一步：{{ latestRecommendation.reason }}
                    </div>

                    <div class="flex flex-wrap gap-2">
                      <button
                        type="button"
                        class="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white"
                        :style="{ backgroundColor: activePlan.accent }"
                        @click="beginNodeLearning"
                      >
                        进入详解
                      </button>
                      <button
                        v-if="canRetrieveKb"
                        type="button"
                        class="inline-flex items-center gap-2 rounded-lg border border-border-subtle px-4 py-2 text-sm font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
                        :disabled="exploreLoading"
                        @click="exploreSelectedNode"
                      >
                        <Library class="h-4 w-4" />
                        {{ exploreLoading ? '检索中' : '查找知识库资料' }}
                      </button>
                    </div>
                  </div>

                  <div v-else-if="activeLearningTab === 'detail'">
                    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
                      <div class="flex flex-wrap items-center gap-2">
                        <span
                          v-if="hasKbDetail"
                          class="rounded-full bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300"
                        >
                          知识库详解
                        </span>
                        <span
                          v-else-if="selectedRemoteNode?.explanation"
                          class="rounded-full bg-surface-sunken px-2.5 py-1 text-[11px] font-semibold text-text-secondary"
                        >
                          学习导引
                        </span>
                      </div>
                      <button
                        type="button"
                        class="inline-flex items-center gap-1.5 rounded-lg border border-border-subtle px-3 py-1.5 text-xs font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
                        @click="openExplorePage"
                      >
                        <ExternalLink class="h-3.5 w-3.5" />
                        打开探索式学习
                      </button>
                    </div>
                    <div
                      v-if="kbEnrichLoading"
                      class="mb-4 flex items-center gap-2 rounded-lg border border-blue-100 bg-blue-50/80 px-3 py-2 text-xs text-blue-800 dark:border-blue-900/50 dark:bg-blue-950/20 dark:text-blue-200"
                    >
                      <Loader2 class="h-3.5 w-3.5 animate-spin" />
                      正在后台生成知识库详解，可先阅读下方学习导引…
                    </div>
                    <div
                      v-else-if="enrichError"
                      class="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200"
                    >
                      <span>{{ enrichError }}</span>
                      <button
                        type="button"
                        class="rounded-md border border-amber-300 bg-surface px-2.5 py-1 text-xs font-semibold text-amber-700 transition-colors hover:bg-amber-100/60 dark:border-amber-800 dark:text-amber-200"
                        @click="retryKbDetail"
                      >
                        重试
                      </button>
                    </div>
                    <div v-if="contentLoading" class="flex items-center gap-2 text-sm text-text-secondary">
                      <Loader2 class="h-4 w-4 animate-spin" />
                      {{ detailLoadingHint }}
                    </div>
                    <div v-else-if="selectedRemoteNode?.explanation" class="node-explanation-md">
                      <MarkdownRenderer :content="selectedRemoteNode.explanation" :smooth="false" />
                      <div v-if="detailSources.length" class="mt-6 rounded-xl border border-border-subtle bg-surface-sunken p-4">
                        <div class="mb-2 text-xs font-semibold uppercase tracking-wide text-text-secondary">参考来源</div>
                        <ul class="space-y-2">
                          <li v-for="(source, index) in detailSources" :key="String(source.doc_id || index)" class="text-xs leading-5 text-text-secondary">
                            {{ source.doc_name || source.file_path || `资料 ${index + 1}` }}
                          </li>
                        </ul>
                      </div>
                      <div v-else-if="needsKbDetail" class="mt-4 flex flex-wrap items-center gap-3">
                        <p class="text-xs text-text-secondary">当前为学习导引内容。可加载知识库详解获取更深入的讲解。</p>
                        <button
                          type="button"
                          class="inline-flex items-center gap-1.5 rounded-lg border border-border-subtle px-3 py-1.5 text-xs font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
                          @click="retryKbDetail"
                        >
                          <Loader2 v-if="kbEnrichLoading" class="h-3.5 w-3.5 animate-spin" />
                          <Library v-else class="h-3.5 w-3.5" />
                          加载知识库详解
                        </button>
                      </div>
                    </div>
                    <div
                      v-else-if="selectedRemoteNode?.content_status?.explanation_ready"
                      class="flex items-center justify-center gap-2 rounded-xl border border-dashed border-border-subtle bg-surface-sunken py-10 text-sm text-text-secondary"
                    >
                      <Loader2 class="h-4 w-4 animate-spin" />
                      正在加载节点内容...
                    </div>
                    <div v-else class="rounded-xl border border-dashed border-border-subtle bg-surface-sunken p-6 text-center">
                      <p class="text-sm text-text-secondary">详解尚未生成，点击「开始学习」后自动加载。</p>
                      <button type="button" class="mt-4 inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white" :style="{ backgroundColor: activePlan.accent }" @click="beginNodeLearning">
                        <Play class="h-4 w-4" />
                        开始学习
                      </button>
                    </div>
                  </div>

                  <div v-else-if="activeLearningTab === 'materials'" class="space-y-4">
                    <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
                      <div>
                        <h4 class="text-base font-bold">知识库补充阅读</h4>
                        <p class="mt-1 text-sm text-text-secondary">展示知识图谱子节点与相关文档片段，可继续进入探索式学习深入展开。</p>
                      </div>
                      <div class="flex flex-wrap gap-2">
                        <button
                          type="button"
                          class="inline-flex items-center gap-2 rounded-lg border border-border-subtle px-3 py-2 text-sm font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600"
                          @click="openExplorePage"
                        >
                          <ExternalLink class="h-4 w-4" />
                          探索式学习
                        </button>
                        <button
                          v-if="canRetrieveKb"
                          type="button"
                          class="inline-flex items-center gap-2 rounded-lg border border-border-subtle px-3 py-2 text-sm font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-50"
                          :disabled="exploreLoading || selectedNode.state === 'locked'"
                          @click="exploreSelectedNode"
                        >
                          <Loader2 v-if="exploreLoading" class="h-4 w-4 animate-spin" />
                          <Library v-else class="h-4 w-4" />
                          {{ exploreLoading ? '检索中' : hasExploreResources || hasExploreMap ? '重新检索' : '检索知识库' }}
                        </button>
                      </div>
                    </div>

                    <div v-if="kbDetailError" class="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200">
                      {{ kbDetailError }}
                    </div>
                    <div v-if="resourceErrors.length" class="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200">
                      部分知识库检索失败，已展示可用资料。失败知识库：{{ resourceErrorKbLabels }}
                    </div>

                    <div v-if="hasExploreMap" class="rounded-xl border border-border-subtle bg-surface p-4">
                      <div class="mb-3 flex items-center gap-2 text-sm font-bold text-text-primary">
                        <MapIcon class="h-4 w-4" />
                        知识图谱
                        <span class="text-xs font-normal text-text-tertiary">{{ selectedRemoteNode?.explore_cache?.map?.root?.title || selectedRemoteNode?.title }}</span>
                      </div>
                      <div class="grid gap-2 sm:grid-cols-2">
                        <div
                          v-for="child in exploreMapChildren"
                          :key="String(child.id || child.title)"
                          class="rounded-lg border border-border-subtle bg-surface-sunken px-3 py-2"
                        >
                          <div class="text-sm font-semibold text-text-primary">{{ child.title }}</div>
                          <p v-if="child.summary" class="mt-1 text-xs leading-5 text-text-secondary">{{ child.summary }}</p>
                        </div>
                      </div>
                    </div>

                    <div v-if="hasExploreResources" class="space-y-3">
                      <button
                        v-for="resource in normalizedExploreResources"
                        :key="resource.key"
                        type="button"
                        class="group flex w-full gap-3 rounded-xl border border-border-subtle p-4 text-left transition-colors hover:border-blue-200 hover:bg-blue-50/40 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:bg-blue-950/20"
                        :disabled="!resource.url || resourceDownloading === resource.key"
                        @click="downloadResource(resource)"
                      >
                        <span class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-surface-sunken text-text-secondary group-hover:bg-blue-100 group-hover:text-blue-600">
                          <FileText class="h-4 w-4" />
                        </span>
                        <span class="min-w-0 flex-1">
                          <span class="flex items-start justify-between gap-3">
                            <strong class="line-clamp-2 text-sm text-text-primary">{{ resource.title }}</strong>
                            <span class="flex shrink-0 items-center gap-2">
                              <span v-if="resource.score" class="rounded bg-surface-sunken px-2 py-0.5 text-[11px] font-semibold text-text-secondary">{{ resource.score }}</span>
                              <Loader2 v-if="resourceDownloading === resource.key" class="h-3.5 w-3.5 animate-spin text-blue-500" />
                              <ExternalLink v-else-if="resource.url" class="h-3.5 w-3.5 text-text-tertiary group-hover:text-blue-500" />
                            </span>
                          </span>
                          <span class="mt-1 line-clamp-2 text-xs leading-5 text-text-secondary">{{ resource.snippet }}</span>
                        </span>
                      </button>
                    </div>

                    <template v-if="!hasExploreMaterials">
                      <div v-if="exploreLoading" class="flex items-center justify-center gap-2 rounded-xl border border-dashed border-border-subtle py-10 text-sm text-text-secondary">
                        <Loader2 class="h-4 w-4 animate-spin" />
                        正在从知识库检索相关资料...
                      </div>

                      <div v-else-if="!canRetrieveKb" class="rounded-xl border border-dashed border-border-subtle bg-surface-sunken p-6 text-center text-sm text-text-secondary">
                        当前学习计划未绑定知识库。可先阅读节点详解，或进入探索式学习自由拓展。
                        <button type="button" class="mt-4 inline-flex items-center gap-2 rounded-lg border border-border-subtle px-4 py-2 text-sm font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600" @click="openExplorePage">
                          <ExternalLink class="h-4 w-4" />
                          打开探索式学习
                        </button>
                      </div>

                      <div v-else class="rounded-xl border border-dashed border-border-subtle bg-surface-sunken p-6 text-center">
                        <p class="text-sm text-text-secondary">{{ exploreAttempted ? '未召回到足够相关的资料，建议先完成节点详解与练习。' : '需要更多背景材料时，可一键从知识库检索。' }}</p>
                        <button type="button" class="mt-4 inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white" :style="{ backgroundColor: activePlan.accent }" @click="exploreSelectedNode">
                          <Library class="h-4 w-4" />
                          检索知识库
                        </button>
                      </div>
                    </template>
                  </div>

                  <div v-else class="space-y-4">
                    <div class="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
                      <div>
                        <h4 class="text-base font-bold">节点练习</h4>
                        <p class="mt-1 text-sm text-text-secondary">提交后更新掌握度，低于 70 分会自动插入复习节点。</p>
                      </div>
                      <button
                        type="button"
                        class="inline-flex items-center gap-2 rounded-lg border border-border-subtle px-3 py-2 text-sm font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-50"
                        :disabled="quizLoading || selectedNode.state === 'locked'"
                        @click="loadQuiz"
                      >
                        <FileText class="h-4 w-4" />
                        {{ quizQuestions.length ? '换一批题' : '生成练习' }}
                      </button>
                    </div>

                    <div v-if="quizQuestions.length" class="space-y-4">
                      <p v-if="unansweredQuestionIds.length" class="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200">
                        还有 {{ unansweredQuestionIds.length }} 道题未作答，已高亮提示。
                      </p>
                      <div
                        v-for="question in quizQuestions"
                        :id="`quiz-question-${question.id}`"
                        :key="question.id"
                        class="rounded-xl border p-4 transition-colors"
                        :class="isQuestionUnanswered(question.id) ? 'border-amber-300 bg-amber-50/70 ring-1 ring-amber-200 dark:border-amber-800 dark:bg-amber-950/20 dark:ring-amber-900/50' : 'border-border-subtle'"
                      >
                        <p class="font-semibold">
                          {{ question.question }}
                          <span v-if="isQuestionUnanswered(question.id)" class="ml-2 text-xs font-semibold text-amber-600 dark:text-amber-300">待作答</span>
                        </p>
                        <div v-if="question.options?.length" class="mt-3 grid gap-2">
                          <label
                            v-for="option in question.options"
                            :key="option.key"
                            class="flex cursor-pointer items-center gap-2 rounded-lg border border-border-subtle px-3 py-2 text-sm hover:border-blue-200"
                          >
                            <input v-model="quizAnswers[question.id]" type="radio" :name="question.id" :value="option.key" />
                            <span>{{ option.key }}. {{ option.text }}</span>
                          </label>
                        </div>
                        <textarea
                          v-else
                          v-model="quizAnswers[question.id]"
                          class="mt-3 min-h-24 w-full rounded-lg border border-border-subtle bg-surface p-3 text-sm outline-none transition-colors focus:border-blue-300"
                          placeholder="写下你的判断依据"
                        />
                        <div
                          v-if="questionFeedback[String(question.id)]"
                          class="mt-3 rounded-lg border px-3 py-2.5 text-sm leading-6"
                          :class="feedbackToneClass(questionFeedback[String(question.id)].verdict)"
                        >
                          <p class="font-semibold">
                            {{ feedbackVerdictLabel(questionFeedback[String(question.id)].verdict) }}
                            <span v-if="questionFeedback[String(question.id)].expected_answer && questionFeedback[String(question.id)].verdict !== 'correct'">
                              · 正确答案：{{ questionFeedback[String(question.id)].expected_answer }}
                            </span>
                          </p>
                          <p v-if="questionFeedback[String(question.id)].comment" class="mt-1 opacity-90">
                            点评：{{ questionFeedback[String(question.id)].comment }}
                          </p>
                          <p v-if="questionFeedback[String(question.id)].analysis" class="mt-1 opacity-90">
                            解析：{{ questionFeedback[String(question.id)].analysis }}
                          </p>
                          <p
                            v-else-if="questionFeedback[String(question.id)].reference && questionFeedback[String(question.id)].verdict !== 'correct'"
                            class="mt-1 opacity-90"
                          >
                            参考要点：{{ questionFeedback[String(question.id)].reference }}
                          </p>
                          <p v-if="questionFeedback[String(question.id)].missing_points?.length" class="mt-1 opacity-90">
                            建议补充：{{ questionFeedback[String(question.id)].missing_points.join('、') }}
                          </p>
                        </div>
                      </div>

                      <button
                        type="button"
                        class="inline-flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
                        :style="{ backgroundColor: activePlan.accent }"
                        :disabled="quizLoading || !quizQuestions.length"
                        @click="submitQuiz"
                      >
                        <ClipboardCheck class="h-4 w-4" />
                        {{ quizLoading ? '提交中' : '提交练习' }}
                      </button>
                    </div>

                    <div v-else class="rounded-xl border border-dashed border-border-subtle bg-surface-sunken p-6 text-center text-sm text-text-secondary">
                      建议先阅读详解，再生成练习检验掌握情况。
                    </div>

                    <div
                      v-if="quizResult"
                      class="rounded-xl border p-4 text-sm leading-6"
                      :class="quizPassed ? 'border-emerald-100 bg-emerald-50 text-emerald-700 dark:border-emerald-900/50 dark:bg-emerald-950/20 dark:text-emerald-300' : 'border-amber-100 bg-amber-50 text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-200'"
                    >
                      <p class="font-semibold">
                        得分 {{ quizResult.score }} 分，掌握度 {{ quizResult.mastery_before }}% → {{ quizResult.mastery_after }}%
                        <span
                          v-if="quizResult.diagnosis?.grading === 'llm_assisted'"
                          class="ml-2 rounded bg-white/70 px-1.5 py-0.5 align-middle text-[11px] font-semibold dark:bg-white/10"
                        >
                          AI 辅助判分
                        </span>
                      </p>
                      <p v-if="!quizPassed" class="mt-1">未达到 70 分，请对照上方逐题反馈复习后再重考。</p>
                      <p v-else-if="isReviewChildNode" class="mt-1">复习练习已通过，请回到原节点重新检测。</p>
                      <p v-if="quizResult.diagnosis?.weak_points?.length" class="mt-1">
                        薄弱点：{{ quizResult.diagnosis.weak_points.join('、') }}
                      </p>
                      <button
                        v-if="pendingNextNode"
                        type="button"
                        class="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-emerald-700"
                        @click="jumpToPendingNextNode"
                      >
                        前往下一节点「{{ pendingNextNode.title }}」
                      </button>
                    </div>

                    <div v-if="adjustmentNotice" class="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200">
                      {{ adjustmentNotice.reason || '系统已根据本次学习结果调整路径。' }}
                      <div class="mt-3 flex flex-wrap gap-2">
                        <button
                          v-if="adjustmentNotice.after_snapshot?.created_node_id"
                          type="button"
                          class="rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-amber-700"
                          @click="jumpToReviewNode(String(adjustmentNotice.after_snapshot.created_node_id), 'guide')"
                        >
                          前往复习节点
                        </button>
                        <span v-else-if="adjustmentNotice.after_snapshot?.strategy === 'mark_current_node_review_only'" class="text-xs opacity-90">
                          当前计划节点较多，系统已标记需复习，请在本节点完成补强练习。
                        </span>
                      </div>
                    </div>
                  </div>
                </div>

                <div v-else class="grid gap-3 p-5 md:grid-cols-3">
                  <div class="rounded-lg bg-surface-sunken p-4">
                    <div class="mb-2 flex items-center gap-2 text-sm font-semibold">
                      <BookOpenText class="h-4 w-4" :style="{ color: activePlan.accent }" />
                      学习资源
                    </div>
                    <p class="text-sm leading-6 text-text-secondary">{{ selectedNode.resource || '暂无资源' }}</p>
                  </div>
                  <div class="rounded-lg bg-surface-sunken p-4">
                    <div class="mb-2 flex items-center gap-2 text-sm font-semibold">
                      <FlaskConical class="h-4 w-4" :style="{ color: activePlan.accent }" />
                      节点任务
                    </div>
                    <p class="text-sm leading-6 text-text-secondary">{{ selectedNode.task || '暂无任务' }}</p>
                  </div>
                  <div class="rounded-lg bg-surface-sunken p-4">
                    <div class="mb-2 flex items-center gap-2 text-sm font-semibold">
                      <FileText class="h-4 w-4" :style="{ color: activePlan.accent }" />
                      验收标准
                    </div>
                    <p class="text-sm leading-6 text-text-secondary">{{ selectedNode.check || '暂无标准' }}</p>
                  </div>
                </div>
              </article>
            </section>

            <aside class="space-y-4">
              <article class="rounded-lg border border-blue-100 bg-surface p-4 shadow-sm dark:border-blue-900/50">
                <div class="flex items-start justify-between gap-3">
                  <div>
                    <h3 class="text-sm font-bold">智能学习助手</h3>
                    <p class="mt-1 text-xs leading-5 text-text-secondary">{{ latestRecommendation?.reason || '根据当前节点状态推荐下一步。' }}</p>
                  </div>
                  <span class="rounded-md bg-blue-50 px-2 py-1 text-[11px] font-semibold text-blue-700 dark:bg-blue-950/40 dark:text-blue-300">
                    {{ latestRecommendation?.label || primaryAction.label }}
                  </span>
                </div>

                <div class="mt-4 rounded-lg bg-surface-sunken p-3">
                  <div class="text-xs font-bold text-text-secondary">学习风格策略</div>
                  <p class="mt-2 text-sm leading-6 text-text-secondary">
                    {{ dashboardStyle?.enabled ? (dashboardStyle.explanation_hint || '已应用你的学习偏好。') : '当前使用默认学习策略。' }}
                  </p>
                  <div v-if="dashboardStyle?.task_hints?.length" class="mt-2 flex flex-wrap gap-1.5">
                    <span v-for="hint in dashboardStyle.task_hints.slice(0, 3)" :key="hint" class="rounded bg-surface px-2 py-1 text-[11px] font-semibold text-text-secondary">{{ hint }}</span>
                  </div>
                </div>

                <div v-if="moduleEffects.length" class="mt-4 grid gap-2">
                  <div
                    v-for="effect in moduleEffects"
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

                <div v-if="agentInsightCards.length" class="mt-4 space-y-2">
                  <div v-for="item in agentInsightCards" :key="`${item.agent}-${item.decision}`" class="rounded-lg border border-border-subtle p-3">
                    <div class="flex items-center justify-between gap-2">
                      <span class="truncate text-xs font-bold text-text-secondary">{{ item.agent }}</span>
                      <span v-if="item.confidence !== null" class="shrink-0 rounded bg-surface-sunken px-2 py-0.5 text-[11px] font-semibold text-text-secondary">{{ item.confidence }}%</span>
                    </div>
                    <p class="mt-1 text-xs font-semibold text-blue-600 dark:text-blue-300">{{ item.decision }}</p>
                    <p class="mt-1 line-clamp-2 text-xs leading-5 text-text-secondary">{{ item.reason }}</p>
                  </div>
                </div>
              </article>

              <article class="rounded-lg border border-border-subtle bg-surface p-4 shadow-sm">
                <div class="mb-4 flex items-center justify-between gap-3">
                  <div>
                    <h3 class="text-sm font-bold">计划复盘</h3>
                    <p class="mt-1 text-xs text-text-secondary">薄弱点、复习节点和调整记录。</p>
                  </div>
                  <button
                    v-if="isRemotePlan"
                    type="button"
                    class="rounded-lg border border-border-subtle px-3 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-blue-200 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-50"
                    :disabled="reviewLoading"
                    @click="loadPlanReview()"
                  >
                    {{ reviewLoading ? '刷新中' : '刷新' }}
                  </button>
                </div>

                <div class="space-y-4">
                  <p v-if="reviewError" class="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:border-rose-900/60 dark:bg-rose-950/20 dark:text-rose-200">
                    {{ reviewError }}
                  </p>
                  <div>
                    <div class="text-xs font-bold text-text-secondary">薄弱点</div>
                    <div v-if="planReview?.weak_points?.length" class="mt-2 flex flex-wrap gap-2">
                      <span v-for="point in planReview.weak_points" :key="point" class="rounded-md bg-surface-sunken px-2.5 py-1 text-xs font-semibold text-text-secondary">{{ point }}</span>
                    </div>
                    <p v-else class="mt-2 text-sm text-text-secondary">暂无薄弱点记录。</p>
                  </div>
                  <div>
                    <div class="text-xs font-bold text-text-secondary">复习节点</div>
                    <div v-if="planReview?.review_nodes?.length" class="mt-2 space-y-2">
                      <button
                        v-for="node in planReview.review_nodes"
                        :key="node.node_id"
                        type="button"
                        class="block w-full rounded-md bg-surface-sunken px-3 py-2 text-left text-sm font-semibold text-text-secondary transition-colors hover:text-blue-600"
                        @click="jumpToReviewNode(node.node_id)"
                      >
                        {{ node.title }}
                      </button>
                    </div>
                    <p v-else class="mt-2 text-sm text-text-secondary">暂无待复习节点。</p>
                  </div>
                  <div>
                    <div class="text-xs font-bold text-text-secondary">最近练习</div>
                    <div v-if="planReview?.latest_attempts?.length" class="mt-2 space-y-2">
                      <div
                        v-for="attempt in planReview.latest_attempts.slice(0, 4)"
                        :key="attempt.attempt_id"
                        class="rounded-md bg-surface-sunken px-3 py-2 text-sm text-text-secondary"
                      >
                        <div class="flex items-center justify-between gap-2">
                          <span class="truncate font-semibold">{{ nodeTitleById[attempt.node_id] || '学习节点' }}</span>
                          <span class="shrink-0 text-xs" :class="Number(attempt.score ?? 0) >= 70 ? 'text-emerald-600' : 'text-amber-600'">
                            {{ attempt.score ?? 0 }} 分
                          </span>
                        </div>
                        <p class="mt-1 text-xs text-text-secondary">
                          {{ attemptLabel(attempt.action_type) }}<template v-if="formatDuration(attempt.duration_seconds)"> · 用时 {{ formatDuration(attempt.duration_seconds) }}</template>
                        </p>
                      </div>
                    </div>
                    <p v-else class="mt-2 text-sm text-text-secondary">暂无练习记录。</p>
                  </div>
                  <div>
                    <div class="text-xs font-bold text-text-secondary">调整记录</div>
                    <div v-if="planReview?.adjustments?.length" class="mt-2 space-y-2">
                      <p v-for="item in planReview.adjustments.slice(0, 3)" :key="item.adjustment_id" class="text-sm leading-6 text-text-secondary">
                        {{ item.reason || item.trigger_type }}
                      </p>
                    </div>
                    <p v-else class="mt-2 text-sm text-text-secondary">暂无调整记录。</p>
                  </div>
                </div>
              </article>

              <article class="rounded-lg border border-border-subtle bg-surface p-4 shadow-sm">
                <h3 class="text-sm font-bold">学习产出</h3>
                <div class="mt-3 space-y-2">
                  <div v-for="outcome in activePlan.outcomes" :key="outcome" class="rounded-lg bg-surface-sunken p-3 text-sm leading-6 text-text-secondary">
                    {{ outcome }}
                  </div>
                  <p v-if="!activePlan.outcomes.length" class="text-sm leading-6 text-text-secondary">完成当前路径后生成阶段性学习总结。</p>
                </div>
              </article>
            </aside>
          </div>
        </section>

        <!-- 移动/中屏的主操作固定底栏：三栏布局(xl)下主按钮始终可见，无需底栏。 -->
        <footer
          v-if="isRemotePlan && !needsLogin"
          class="fixed inset-x-0 bottom-0 z-40 border-t border-border-subtle bg-surface/95 px-4 py-3 backdrop-blur xl:hidden"
        >
          <div class="mx-auto flex max-w-[1500px] items-center gap-3">
            <div class="min-w-0 flex-1">
              <p class="truncate text-xs font-semibold text-text-secondary">{{ selectedNode?.title }}</p>
              <p v-if="primaryAction.reason" class="truncate text-[11px] text-text-tertiary">{{ primaryAction.reason }}</p>
            </div>
            <button
              type="button"
              class="inline-flex shrink-0 items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
              :style="{ backgroundColor: activePlan.accent }"
              :disabled="(!isDraftPlan && selectedNode?.state === 'locked') || nextActionLoading || nodeDetailLoading || kbEnrichLoading || quizLoading || exploreLoading"
              @click="runPrimaryAction"
            >
              <Loader2 v-if="nextActionLoading || nodeDetailLoading || kbEnrichLoading || quizLoading || exploreLoading" class="h-4 w-4 animate-spin" />
              <Play v-else class="h-4 w-4" />
              {{ nextActionLoading || nodeDetailLoading || kbEnrichLoading || quizLoading || exploreLoading ? '处理中' : primaryAction.label }}
            </button>
          </div>
        </footer>
    </main>
  </AppShell>
</template>

<style scoped>
@reference "../../style.css";

.node-explanation-md :deep(.academic-container) {
  @apply max-w-none px-0 text-[15px] leading-7;
}

.node-explanation-md :deep(h2) {
  @apply mt-2 mb-3 text-lg font-bold;
}

.node-explanation-md :deep(h2::before) {
  content: none;
}

.node-explanation-md :deep(h3) {
  @apply mt-4 mb-2 text-base font-bold text-text-primary;
}

.node-explanation-md :deep(p) {
  @apply mb-3 text-[14px] leading-7 text-text-secondary;
  text-indent: 0 !important;
  text-align: left;
}

.node-explanation-md :deep(ul),
.node-explanation-md :deep(ol) {
  @apply mb-3 pl-5 text-[14px];
}

.node-explanation-md :deep(li) {
  @apply leading-6;
}

.node-explanation-md :deep(strong) {
  @apply font-semibold text-text-primary;
}
</style>
