<script setup lang="ts">
/**
 * 智能体过程面板。
 *
 * 默认折叠成一行摘要：做了什么、成没成、要我做什么。展开后是阶段化时间线。
 * 需要用户回应的动作卡不受折叠影响，始终可见。
 */
import { computed, ref, watch } from 'vue';
import { ChevronDown, CircleAlert, CircleCheck, LoaderCircle } from 'lucide-vue-next';
import ActionCard from './ActionCard.vue';
import BranchSwitcher from './BranchSwitcher.vue';
import StepItem from './StepItem.vue';
import { formatDuration } from './humanize';
import { statusHistoryToSteps } from './statusHistory';
import type { ProcessEvent, StepGroup } from './types';
import { buildPipelineModel } from './usePipelineModel';

const props = defineProps<{
  messageId: string;
  events?: ProcessEvent[];
  statusHistory?: string[];
  isStreaming?: boolean;
}>();

const emit = defineEmits<{
  (e: 'action-send', message: string): void;
  (e: 'action-fill', template: string): void;
}>();

const isOpen = ref(false);
const selectedGroupId = ref('');

const model = computed(() =>
  buildPipelineModel(props.events, { messageId: props.messageId, isStreaming: props.isStreaming }),
);

const legacyStatusSteps = computed(() =>
  model.value.steps.length ? [] : statusHistoryToSteps(props.statusHistory, props.messageId),
);

const hasContent = computed(() => model.value.steps.length > 0 || legacyStatusSteps.value.length > 0);

const summary = computed(() => model.value.summary);

const elapsedText = computed(() => formatDuration(summary.value.elapsedMs));

const summaryText = computed(() => {
  const state = summary.value;
  if (legacyStatusSteps.value.length) {
    return props.isStreaming ? '正在处理' : `已完成 ${legacyStatusSteps.value.length} 步`;
  }
  if (state.state === 'running') {
    return state.currentLabel || '正在处理';
  }
  if (state.state === 'failed') {
    return `执行中断 · ${state.errorCount} 项错误`;
  }
  return `已完成 ${state.stepCount} 步`;
});

const branchGroups = computed<StepGroup[]>(() => (model.value.mode === 'parallel' ? model.value.groups : []));

const selectedBranch = computed<StepGroup | null>(
  () => branchGroups.value.find((group) => group.id === selectedGroupId.value) || branchGroups.value[0] || null,
);

watch(
  branchGroups,
  (groups) => {
    const ids = new Set(groups.map((group) => group.id));
    if (!selectedGroupId.value || !ids.has(selectedGroupId.value)) {
      selectedGroupId.value = groups.find((group) => group.state === 'running')?.id || groups[0]?.id || '';
    }
  },
  { immediate: true },
);
</script>

<template>
  <div v-if="hasContent" class="w-full space-y-2">
    <section class="w-full rounded-xl border border-border-subtle bg-surface-sunken px-3 py-2">
      <button
        type="button"
        class="flex w-full items-center gap-2 rounded-md px-1 py-1 text-left text-sm text-text-secondary transition-colors hover:text-text-primary"
        @click="isOpen = !isOpen"
      >
        <!-- 运行/失败/完成图标叠放同一格，交叉淡入切换 -->
        <span class="grid size-4 shrink-0 place-items-center">
          <Transition name="state-icon">
            <LoaderCircle v-if="summary.state === 'running'" class="col-start-1 row-start-1 size-4 animate-spin text-accent" />
            <CircleAlert v-else-if="summary.state === 'failed'" class="col-start-1 row-start-1 size-4 text-rose-500" />
            <CircleCheck v-else class="col-start-1 row-start-1 size-4 text-emerald-500" />
          </Transition>
        </span>

        <span class="min-w-0 flex-1 truncate font-medium" :title="summaryText">{{ summaryText }}</span>

        <span v-if="elapsedText" class="hidden shrink-0 text-xs text-text-tertiary sm:inline">{{ elapsedText }}</span>

        <span
          v-if="summary.pendingCount > 0"
          class="shrink-0 rounded-full border border-accent/40 bg-accent-soft px-2 py-0.5 text-[11px] font-medium text-accent"
        >
          {{ summary.pendingCount }} 项待确认
        </span>
        <span
          v-if="summary.warningCount > 0"
          class="shrink-0 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200"
        >
          {{ summary.warningCount }} 项提醒
        </span>

        <!-- 收起时向下（提示可展开），展开后旋转为向上，与其余折叠面板方向一致 -->
        <ChevronDown class="size-4 shrink-0 transition-transform" :class="isOpen ? 'rotate-180' : ''" />
      </button>

      <div class="collapse-rows" :data-open="isOpen" :inert="!isOpen">
        <div class="space-y-3 pl-1 pt-2">
        <template v-if="legacyStatusSteps.length">
          <StepItem v-for="step in legacyStatusSteps" :key="step.id" :step="step" />
        </template>

        <template v-else-if="model.mode === 'parallel'">
          <BranchSwitcher
            :groups="branchGroups"
            :selected-id="selectedBranch?.id || ''"
            @select="(id) => { selectedGroupId = id; }"
          />
          <div class="space-y-2 rounded-lg border border-border-subtle bg-surface/70 p-2">
            <p v-if="selectedBranch" class="text-[11px] font-medium text-text-secondary">
              当前分组: {{ selectedBranch.title }} · {{ selectedBranch.stepCount }} 个步骤
            </p>
            <StepItem v-for="step in selectedBranch?.steps || []" :key="step.id" :step="step" />
          </div>
        </template>

        <template v-else-if="model.mode === 'linear'">
          <section v-for="group in model.groups" :key="group.id" class="space-y-2">
            <div class="flex items-center gap-2">
              <span
                class="size-1.5 shrink-0 rounded-full"
                :class="group.state === 'failed' ? 'bg-rose-500'
                  : group.state === 'running' ? 'bg-amber-500'
                  : group.state === 'completed' ? 'bg-emerald-500' : 'bg-slate-400'"
              />
              <p class="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-tertiary">
                {{ group.title }}
              </p>
              <span class="text-[11px] text-text-tertiary">{{ group.stepCount }} 步</span>
            </div>
            <div class="space-y-2 border-l border-border-subtle pl-3">
              <StepItem v-for="step in group.steps" :key="step.id" :step="step" />
            </div>
          </section>
        </template>

        <template v-else>
          <StepItem v-for="step in model.steps" :key="step.id" :step="step" />
        </template>
        </div>
      </div>
    </section>

    <ActionCard
      v-for="action in model.actions"
      :key="action.stepId"
      :action="action"
      @send="(message) => emit('action-send', message)"
      @fill="(template) => emit('action-fill', template)"
    />
  </div>
</template>

<style scoped>
.state-icon-enter-active,
.state-icon-leave-active {
  transition:
    opacity 200ms ease,
    transform 200ms ease;
}
.state-icon-enter-from,
.state-icon-leave-to {
  opacity: 0;
  transform: scale(0.6);
}
</style>
