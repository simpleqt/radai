<script setup lang="ts">
/**
 * 待用户回应的动作卡。
 *
 * 不新增接口：选项按钮直接把规范化指令发进对话，「我来修正」把模板填入输入框，
 * 与课程评价「全流程在对话里完成」的形态一致。
 */
import { computed } from 'vue';
import { CircleHelp } from 'lucide-vue-next';
import type { DisplayActionOption, PendingAction } from './types';
import { resolveViewComponent } from './views';

const props = defineProps<{ action: PendingAction }>();

const emit = defineEmits<{
  (e: 'send', message: string): void;
  (e: 'fill', template: string): void;
}>();

const viewComponent = computed(() => resolveViewComponent(props.action.view));
const hasViewContent = computed(() => Object.keys(props.action.data || {}).length > 0);
const options = computed<DisplayActionOption[]>(() => props.action.action.options || []);
const inputTemplate = computed(() => props.action.action.input_template || '');

const optionClass = (option: DisplayActionOption) => {
  if (option.tone === 'warning') {
    return 'border-amber-300 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200';
  }
  if (option.tone === 'error') {
    return 'border-rose-300 bg-rose-50 text-rose-800 hover:bg-rose-100 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-200';
  }
  return 'border-accent/40 bg-accent-soft text-accent hover:border-accent';
};
</script>

<template>
  <section class="animate-in fade-in slide-in-from-bottom-2 w-full space-y-2 rounded-xl border border-accent/30 bg-accent-soft/40 px-3 py-2.5 duration-300">
    <div class="flex items-start gap-2">
      <CircleHelp class="mt-0.5 size-4 shrink-0 text-accent" />
      <div class="min-w-0 flex-1">
        <p class="text-[11px] font-semibold uppercase tracking-[0.24em] text-accent">需要你确认</p>
        <p class="mt-0.5 text-sm leading-6 text-text-primary">{{ action.action.prompt }}</p>
      </div>
    </div>

    <component :is="viewComponent" v-if="hasViewContent" :data="action.data" />

    <div class="flex flex-wrap gap-2 pt-0.5">
      <button
        v-for="option in options"
        :key="option.label"
        type="button"
        class="inline-flex items-center rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors"
        :class="optionClass(option)"
        @click="emit('send', option.message)"
      >
        {{ option.label }}
      </button>
      <button
        v-if="inputTemplate"
        type="button"
        class="inline-flex items-center rounded-lg border border-border-subtle bg-surface px-3 py-1.5 text-xs font-medium text-text-secondary transition-colors hover:border-border-strong hover:text-text-primary"
        @click="emit('fill', inputTemplate)"
      >
        我来修正
      </button>
    </div>
  </section>
</template>
