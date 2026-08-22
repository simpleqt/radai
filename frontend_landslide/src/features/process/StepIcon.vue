<script setup lang="ts">
import { computed } from 'vue';
import {
  CircleAlert,
  FileText,
  FolderOpen,
  Globe,
  Lightbulb,
  MessageCircleQuestionMark,
  NotebookPen,
  Search,
  SquareCheckBig,
  Table,
  Wrench,
} from 'lucide-vue-next';
import type { IconKey } from './types';

const props = defineProps<{ name: IconKey }>();

const ICONS: Record<IconKey, any> = {
  search: Search,
  web: Globe,
  tool: Wrench,
  task: MessageCircleQuestionMark,
  think: Lightbulb,
  note: NotebookPen,
  alert: CircleAlert,
  folder: FolderOpen,
  table: Table,
  check: SquareCheckBig,
  file: FileText,
};

const icon = computed(() => ICONS[props.name] || Wrench);
</script>

<template>
  <component :is="icon" />
</template>
