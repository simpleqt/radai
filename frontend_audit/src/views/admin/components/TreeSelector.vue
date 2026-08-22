<script setup lang="ts">
import { PropType } from 'vue';
import { Folder as FolderIcon, Clock as ClockIcon } from 'lucide-vue-next';

defineProps({
  nodes: {
    type: Array as PropType<any[]>,
    required: true
  },
  currentPath: {
    type: String,
    required: true
  },
  depth: {
    type: Number,
    default: 0
  }
});

defineEmits(['select']);
</script>

<template>
  <div class="space-y-1">
    <div v-for="node in nodes" :key="node.path" class="space-y-1">
      <button
        @click="$emit('select', node.path)"
        :style="{ paddingLeft: (depth * 16 + 12) + 'px' }"
        :class="currentPath === node.path ? 'bg-accent-soft text-accent border-accent/50 font-semibold shadow-sm'
          : 'bg-transparent text-text-secondary border-transparent hover:bg-surface-hover'"
        class="w-full text-left py-2 rounded-xl text-sm flex items-center border transition-all duration-200"
      >
        <ClockIcon v-if="node.is_virtual" class="w-4 h-4 mr-2 flex-shrink-0" :class="currentPath === node.path ? 'text-accent' : 'text-text-tertiary'" />
        <FolderIcon v-else class="w-4 h-4 mr-2 flex-shrink-0" :class="currentPath === node.path ? 'text-accent' : 'text-text-tertiary'" />
        <span class="truncate">{{ node.name }}</span>
        <span v-if="node.is_virtual" class="text-[9px] ml-auto opacity-40 uppercase font-bold tracking-wider">日期</span>
      </button>
      <TreeSelector
        v-if="node.children && node.children.length > 0"
        :nodes="node.children"
        :current-path="currentPath"
        :depth="depth + 1"
        @select="p => $emit('select', p)"
      />
    </div>
  </div>
</template>
