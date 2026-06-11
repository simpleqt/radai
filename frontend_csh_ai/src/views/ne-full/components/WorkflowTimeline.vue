<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import {
  BookOpenText,
  ChevronUp,
  FolderOpen,
  Globe,
  Lightbulb,
  MessageCircleQuestionMark,
  NotebookPen,
  Search,
  SquareTerminal,
  Wrench,
} from 'lucide-vue-next';
import { type AgentProcessEvent, type ChatMessage } from '@/api/agent-v2';
import SubagentWorkflowSwitcher, {
  type SubagentGroupState,
  type SubagentWorkflowGroupMeta,
} from './SubagentWorkflowSwitcher.vue';
import SubagentWorkflowTimeline from './SubagentWorkflowTimeline.vue';

interface Props {
  message: ChatMessage;
}

interface StatusStep {
  id: string;
  label: string;
  detail?: string;
  path?: string;
  icon: any;
}

const props = defineProps<Props>();

const ORCHESTRATOR_TASK_ID = 'orchestrator';
const UNKNOWN_TASK_ID = 'unknown';
const TASK_RELATED_KINDS = new Set(['task_start', 'task_progress', 'task_end']);
const TASK_RUNTIME_TYPES = new Set([
  'task_started',
  'task_running',
  'task_tool_start',
  'task_tool_end',
  'task_completed',
  'task_failed',
  'task_timed_out',
]);

const isOpen = ref(true);
const selectedTaskId = ref('');

const getEventTaskId = (event: AgentProcessEvent): string | null => {
  const metadata = (event.metadata || {}) as Record<string, unknown>;
  const rawTaskId = metadata.task_id;
  if (typeof rawTaskId !== 'string') return null;
  const value = rawTaskId.trim();
  return value || null;
};

const getEventTaskDescription = (event: AgentProcessEvent): string | null => {
  const metadata = (event.metadata || {}) as Record<string, unknown>;

  const rawDescription = metadata.description;
  if (typeof rawDescription === 'string' && rawDescription.trim()) {
    return rawDescription.trim();
  }

  const input = metadata.input;
  if (input && typeof input === 'object') {
    const inputDesc = (input as Record<string, unknown>).description;
    if (typeof inputDesc === 'string' && inputDesc.trim()) {
      return inputDesc.trim();
    }
  }

  const detail = String(event.detail || '');
  const delegated = detail.match(/委派子任务[:：]\s*(.+)$/);
  if (delegated && delegated[1]?.trim()) {
    return delegated[1].trim();
  }

  return null;
};

const isTaskRelatedEvent = (event: AgentProcessEvent): boolean => {
  if (TASK_RELATED_KINDS.has(event.kind)) return true;
  const metadata = (event.metadata || {}) as Record<string, unknown>;
  const runtimeType = typeof metadata.runtime_type === 'string' ? metadata.runtime_type.trim() : '';
  if (runtimeType && TASK_RUNTIME_TYPES.has(runtimeType)) return true;
  return event.tool === 'task';
};

const resolveEventGroupId = (event: AgentProcessEvent): string => {
  const taskId = getEventTaskId(event);
  if (taskId) return `task:${taskId}`;

  const description = getEventTaskDescription(event);
  if (description) return `desc:${description}`;

  if (isTaskRelatedEvent(event)) return UNKNOWN_TASK_ID;
  return ORCHESTRATOR_TASK_ID;
};

const resolveEventGroupIdWithContext = (
  event: AgentProcessEvent,
  lastDelegatedGroupId: string | null,
): { groupId: string; nextDelegatedGroupId: string | null } => {
  const ownGroupId = resolveEventGroupId(event);

  if (ownGroupId.startsWith('desc:')) {
    return { groupId: ownGroupId, nextDelegatedGroupId: ownGroupId };
  }

  // The generic task tool end/result events often don't carry task_id; bind them
  // to the most recent delegated subagent branch to avoid falling into unknown.
  const canBorrowLastGroup =
    event.tool === 'task' &&
    (event.kind === 'tool_end' || event.kind === 'tool_result' || event.kind === 'task_end' || event.kind === 'task_progress');

  if (ownGroupId === UNKNOWN_TASK_ID && canBorrowLastGroup && lastDelegatedGroupId) {
    return { groupId: lastDelegatedGroupId, nextDelegatedGroupId: lastDelegatedGroupId };
  }

  // Non-runtime task events without task_id belong to orchestrator instead of unknown.
  if (ownGroupId === UNKNOWN_TASK_ID) {
    const metadata = (event.metadata || {}) as Record<string, unknown>;
    const runtimeType = typeof metadata.runtime_type === 'string' ? metadata.runtime_type.trim() : '';
    if (!runtimeType) {
      return { groupId: ORCHESTRATOR_TASK_ID, nextDelegatedGroupId: lastDelegatedGroupId };
    }
  }

  return { groupId: ownGroupId, nextDelegatedGroupId: lastDelegatedGroupId };
};

const resolveGroupTitle = (groupId: string, events: AgentProcessEvent[]): string => {
  if (groupId === ORCHESTRATOR_TASK_ID) return '主控流程';
  if (groupId === UNKNOWN_TASK_ID) return '未归组事件';
  if (groupId.startsWith('desc:')) {
    return groupId.slice('desc:'.length);
  }

  if (groupId.startsWith('task:')) {
    for (const event of events) {
      const desc = getEventTaskDescription(event);
      if (desc) return desc;
    }
    const rawTaskId = groupId.slice('task:'.length);
    return `子智能体 ${rawTaskId.slice(0, 8)}`;
  }

  const firstEvent = events[0];
  const metadata = (firstEvent?.metadata || {}) as Record<string, unknown>;
  const description = typeof metadata.description === 'string' ? metadata.description.trim() : '';
  if (description) return description;

  const subagentType = typeof metadata.subagent_type === 'string' ? metadata.subagent_type.trim() : '';
  if (subagentType) return `子智能体(${subagentType})`;

  return `子智能体 ${groupId.slice(0, 8)}`;
};

const resolveGroupState = (events: AgentProcessEvent[]): SubagentGroupState => {
  let state: SubagentGroupState = 'idle';
  let hasFailure = false;
  let hasCompletionSignal = false;
  let hasRunningSignal = false;

  for (const event of events) {
    const metadata = (event.metadata || {}) as Record<string, unknown>;
    const runtimeType = String(metadata.runtime_type || '');

    const isFailed =
      event.level === 'error' ||
      runtimeType === 'task_failed' ||
      runtimeType === 'task_timed_out' ||
      (event.kind === 'task_end' && event.level === 'warning');
    if (isFailed) {
      state = 'failed';
      hasFailure = true;
      continue;
    }

    const isCompleted =
      runtimeType === 'task_completed' ||
      (event.kind === 'task_end' && event.level === 'success') ||
      (event.tool === 'task' && (event.kind === 'tool_end' || event.kind === 'tool_result'));
    if (isCompleted) {
      state = 'completed';
      hasCompletionSignal = true;
      continue;
    }

    const isRunning =
      runtimeType === 'task_started' ||
      runtimeType === 'task_running' ||
      runtimeType === 'task_tool_start' ||
      event.level === 'active' ||
      event.kind === 'task_start' ||
      event.kind === 'task_progress';
    if (isRunning) {
      state = 'running';
      hasRunningSignal = true;
    }
  }

  // Some orchestrator branches don't emit an explicit completion event.
  // Once the assistant stream has ended, keep non-failed branches from being
  // stuck in `running` due to earlier active events.
  if (!props.message.isStreaming && !hasFailure && state === 'running') {
    return 'completed';
  }

  if (!props.message.isStreaming && !hasFailure && state === 'idle' && (hasCompletionSignal || hasRunningSignal)) {
    return 'completed';
  }

  return state;
};

const groupedWorkflow = computed(() => {
  const allEvents = props.message.processEvents || [];
  const events = allEvents.filter(
    (event) => !(event.tool === 'task' && (event.kind === 'tool_start' || event.kind === 'tool_end' || event.kind === 'tool_result')),
  );
  const eventsByGroup = new Map<string, AgentProcessEvent[]>();
  const createOrder = new Map<string, number>();
  const descriptionTaskAlias = new Map<string, string>();
  let unknownTaskEvents = 0;
  let lastDelegatedGroupId: string | null = null;

  // Pass 1: learn stable mapping from delegated description -> task_id.
  for (const event of events) {
    const taskId = getEventTaskId(event);
    const description = getEventTaskDescription(event);
    if (!taskId || !description) continue;
    descriptionTaskAlias.set(`desc:${description}`, `task:${taskId}`);
  }

  events.forEach((event, index) => {
    const resolved = resolveEventGroupIdWithContext(event, lastDelegatedGroupId);
    lastDelegatedGroupId = resolved.nextDelegatedGroupId;
    let groupId = resolved.groupId;

    // If the same delegated branch later exposes task_id, collapse earlier
    // description-only events into that task group to avoid split timelines.
    if (groupId.startsWith('desc:')) {
      const aliased = descriptionTaskAlias.get(groupId);
      if (aliased) {
        groupId = aliased;
      }
    }

    if (!eventsByGroup.has(groupId)) {
      eventsByGroup.set(groupId, []);
      createOrder.set(groupId, index);
    }
    if (groupId === UNKNOWN_TASK_ID) {
      unknownTaskEvents += 1;
    }
    eventsByGroup.get(groupId)?.push(event);
  });

  const groups: SubagentWorkflowGroupMeta[] = [...eventsByGroup.entries()].map(([groupId, groupEvents]) => {
    const lastUpdated = groupEvents.reduce((acc, event, index) => {
      const current = typeof event.timestamp === 'number' ? event.timestamp : index;
      return Math.max(acc, current);
    }, 0);

    return {
      id: groupId,
      title: resolveGroupTitle(groupId, groupEvents),
      state: resolveGroupState(groupEvents),
      eventCount: groupEvents.length,
      lastUpdated,
    };
  });

  const priority = (state: SubagentGroupState) => {
    if (state === 'running') return 0;
    if (state === 'failed') return 1;
    if (state === 'completed') return 2;
    return 3;
  };

  groups.sort((a, b) => {
    const p = priority(a.state) - priority(b.state);
    if (p !== 0) return p;
    if (b.lastUpdated !== a.lastUpdated) return b.lastUpdated - a.lastUpdated;
    if (b.eventCount !== a.eventCount) return b.eventCount - a.eventCount;
    return (createOrder.get(a.id) || 0) - (createOrder.get(b.id) || 0);
  });

  return {
    groups,
    eventsByGroup,
    unknownTaskEvents,
  };
});

const hasWorkflowGroupingSignal = computed(() => {
  const events = props.message.processEvents || [];
  if (events.length === 0) return false;

  return events.some((event) => resolveEventGroupId(event) !== ORCHESTRATOR_TASK_ID);
});

const pickDefaultTaskId = (groups: SubagentWorkflowGroupMeta[]): string => {
  if (groups.length === 0) return '';
  const running = groups.filter((group) => group.state === 'running');
  if (running.length > 0) return running[0].id;
  return groups[0].id;
};

watch(
  () => groupedWorkflow.value.groups,
  (groups) => {
    const ids = new Set(groups.map((group) => group.id));
    if (!selectedTaskId.value || !ids.has(selectedTaskId.value)) {
      selectedTaskId.value = pickDefaultTaskId(groups);
    }
  },
  { immediate: true, deep: true },
);

const lastUnknownWarningCount = ref(0);
watch(
  () => groupedWorkflow.value.unknownTaskEvents,
  (count) => {
    if (count > lastUnknownWarningCount.value) {
      console.warn('[WorkflowTimeline] task-related events missing task_id', {
        messageId: props.message.id,
        unknownCount: count,
      });
    }
    lastUnknownWarningCount.value = count;
  },
  { immediate: true },
);

const selectedGroupEvents = computed<AgentProcessEvent[]>(() => {
  const key = selectedTaskId.value;
  if (!key) return [];
  return groupedWorkflow.value.eventsByGroup.get(key) || [];
});

const timelineEvents = computed<AgentProcessEvent[]>(() => {
  if (!hasWorkflowGroupingSignal.value) {
    return props.message.processEvents || [];
  }
  return selectedGroupEvents.value;
});

const selectedGroupMeta = computed<SubagentWorkflowGroupMeta | null>(() => {
  return groupedWorkflow.value.groups.find((group) => group.id === selectedTaskId.value) || null;
});

const extractPath = (text?: string) => {
  if (!text) return undefined;
  const match = text.match(/\/mnt\/[\w\-./]+/);
  return match?.[0];
};

const cleanTimelinePlainText = (value: string): string => {
  if (!value) return '';
  let text = String(value);

  text = text
    .replace(/\u200b|\u200c|\u200d|\ufeff/gi, '')
    .replace(/&amp;lt;/gi, '&lt;')
    .replace(/&amp;gt;/gi, '&gt;');

  // NOTE: <sup>/<sub> tag normalization and stripping has been removed.
  // Timeline text is plain-text status labels; HTML tags should be left
  // untouched or handled by the caller if rendering HTML. Stripping them
  // caused display issues when legitimate tags were present.

  return text;
};

const mapStatusToStep = (status: string, index: number): StatusStep => {
  const cleanedStatus = cleanTimelinePlainText(status);
  const normalized = cleanedStatus.toLowerCase();
  const path = extractPath(cleanedStatus);

  if (normalized.includes('web_search')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '在网络上搜索',
      detail: cleanedStatus,
      icon: Search,
    };
  }

  if (cleanedStatus.includes('在网络上搜索')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: cleanedStatus,
      detail: cleanedStatus,
      icon: Search,
    };
  }

  if (normalized.includes('multi_kb_search') || cleanedStatus.includes('知识库')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '在知识库中搜索',
      detail: cleanedStatus,
      icon: Search,
    };
  }

  if (cleanedStatus.includes('委派') || cleanedStatus.includes('子智能体')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '委派子任务',
      detail: cleanedStatus,
      icon: MessageCircleQuestionMark,
    };
  }

  if (cleanedStatus.includes('思考') || cleanedStatus.includes('分析')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '思考中',
      detail: cleanedStatus,
      icon: Lightbulb,
    };
  }

  if (cleanedStatus.includes('/mnt/skills/')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '加载深度研究技能',
      detail: cleanedStatus,
      path,
      icon: BookOpenText,
    };
  }

  if (cleanedStatus.includes('save_deep_research_report') || cleanedStatus.includes('研究报告')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '创建深度研究报告',
      detail: cleanedStatus,
      path,
      icon: NotebookPen,
    };
  }

  if (cleanedStatus.includes('读取') || cleanedStatus.includes('read_file')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '读取文件',
      detail: cleanedStatus,
      path,
      icon: BookOpenText,
    };
  }

  if (cleanedStatus.includes('列出') || cleanedStatus.includes('ls')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '查看目录',
      detail: cleanedStatus,
      icon: FolderOpen,
    };
  }

  if (cleanedStatus.includes('bash') || cleanedStatus.includes('终端')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '执行命令',
      detail: cleanedStatus,
      icon: SquareTerminal,
    };
  }

  if (cleanedStatus.includes('网页') || cleanedStatus.includes('web_fetch')) {
    return {
      id: `${props.message.id}-status-${index}`,
      label: '浏览网页',
      detail: cleanedStatus,
      icon: Globe,
    };
  }

  return {
    id: `${props.message.id}-status-${index}`,
    label: cleanedStatus,
    detail: cleanedStatus,
    path,
    icon: Wrench,
  };
};

const statusSteps = computed<StatusStep[]>(() => {
  if ((props.message.processEvents || []).length > 0) return [];
  const history = props.message.statusHistory || [];
  return history.map((status, index) => mapStatusToStep(status, index));
});

const hasContent = computed(() => {
  if ((props.message.processEvents || []).length > 0) return true;
  return statusSteps.value.length > 0;
});

const toggleOpen = () => {
  isOpen.value = !isOpen.value;
};
</script>

<template>
  <section
    v-if="hasContent"
    class="w-full rounded-xl border border-stone-300 bg-stone-50/80 px-3 py-2 dark:border-stone-700 dark:bg-stone-900/35"
  >
    <button
      type="button"
      class="w-full rounded-md px-1 py-1 text-left text-sm text-stone-600 transition-colors hover:text-stone-800 dark:text-stone-300 dark:hover:text-stone-100"
      @click="toggleOpen"
    >
      <div class="flex items-center gap-2">
        <ChevronUp class="size-4 transition-transform" :class="isOpen ? 'rotate-180' : ''" />
        <span class="font-medium">{{ isOpen ? '隐藏步骤' : '展开步骤' }}</span>
      </div>
    </button>

    <div v-show="isOpen" class="space-y-2 pl-1 pt-2">
      <template v-if="(props.message.processEvents || []).length > 0">
        <SubagentWorkflowSwitcher
          v-if="hasWorkflowGroupingSignal"
          :groups="groupedWorkflow.groups"
          :selected-task-id="selectedTaskId"
          @select="(taskId) => { selectedTaskId = taskId; }"
        />

        <div class="rounded-lg border border-stone-300 bg-white/70 p-2 dark:border-stone-700 dark:bg-stone-900/45">
          <p v-if="hasWorkflowGroupingSignal && selectedGroupMeta" class="mb-1 text-[11px] font-medium text-stone-500 dark:text-stone-400">
            当前分组: {{ selectedGroupMeta.title }} · {{ selectedGroupMeta.eventCount }} 条事件
          </p>
          <SubagentWorkflowTimeline :message-id="props.message.id" :events="timelineEvents" />
        </div>
      </template>

      <template v-else>
        <div v-for="step in statusSteps" :key="step.id" class="flex items-start gap-2 text-[15px] text-slate-700 dark:text-slate-200">
          <component :is="step.icon" class="mt-1 size-[15px] shrink-0 text-slate-500 dark:text-slate-400" />
          <div class="min-w-0 flex-1">
            <p class="leading-6">{{ step.label }}</p>
            <p v-if="step.detail" class="mt-0.5 whitespace-pre-wrap break-words text-xs leading-5 text-slate-500 dark:text-slate-400">{{ step.detail }}</p>
            <div v-if="step.path" class="mt-1 border-l border-stone-300 pl-3 text-xs dark:border-stone-700">
              <span class="inline-block rounded bg-stone-200/70 px-2 py-0.5 text-stone-700 dark:bg-stone-800 dark:text-stone-300">
                {{ step.path }}
              </span>
            </div>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>
