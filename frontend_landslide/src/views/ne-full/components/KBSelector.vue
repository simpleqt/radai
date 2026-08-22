<script setup lang="ts">
interface KbOption {
  id: string;
  label: string;
}

const props = defineProps<{
  availableKbs: KbOption[];
  selectedKbs: string[];
}>();

const emit = defineEmits<{
  (e: 'update:selectedKbs', value: string[]): void;
}>();

const toggleKb = (kbId: string) => {
  const newSelection = [...props.selectedKbs];
  const index = newSelection.indexOf(kbId);
  if (index >= 0) {
    if (newSelection.length > 1) {
      newSelection.splice(index, 1);
    }
  } else {
    newSelection.push(kbId);
  }
  emit('update:selectedKbs', newSelection);
};

const isSelected = (kbId: string) => props.selectedKbs.includes(kbId);
</script>

<template>
  <div class="flex items-center space-x-2">
    <div 
      v-for="kb in availableKbs" 
      :key="kb.id"
      @click="toggleKb(kb.id)"
      class="cursor-pointer px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider transition-all duration-300 hover:-translate-y-0.5 flex items-center space-x-1.5 border"
      :class="[ isSelected(kb.id) 
          ? 'bg-emerald-50 border-emerald-200 text-emerald-600 shadow-sm' 
          : 'bg-surface border-border-subtle text-text-tertiary opacity-60 hover:opacity-100'
      ]"
    >
      <div 
        class="w-1.5 h-1.5 rounded-full transition-colors"
        :class="[isSelected(kb.id) ? 'bg-emerald-500' : 'bg-surface-sunken']"
      ></div>
      <span>{{ kb.label }}</span>
    </div>
  </div>
</template>
