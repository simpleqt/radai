<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

const props = defineProps({
  isBusy: { type: Boolean, required: true },
  status: { type: String, default: '' },
  selectedKbs: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
  searchEnabled: { type: Boolean, default: false },
});

const emit = defineEmits([
  'send',
  'abort',
  'update:selectedKbs',
  'update:modelValue',
  'update:searchEnabled',
]);

const question = ref(props.modelValue);
const textareaRef = ref(null);
const kbDropdownRef = ref(null);
const kbMenuOpen = ref(false);

const availableKbs = ref([]);

watch(
  () => props.modelValue,
  (newVal) => {
    question.value = newVal;
    adjustHeight();
  },
);

watch(question, (newVal) => {
  emit('update:modelValue', newVal);
});

const toggleKbMenu = () => {
  if (props.isBusy) return;
  kbMenuOpen.value = !kbMenuOpen.value;
};

const toggleSearch = () => {
  if (props.isBusy) return;
  emit('update:searchEnabled', !props.searchEnabled);
};

const toggleKb = (kbId) => {
  const current = Array.isArray(props.selectedKbs) ? props.selectedKbs : [];
  const next = [...current];
  const idx = next.indexOf(kbId);
  if (idx === -1) {
    next.push(kbId);
  } else if (next.length > 1) {
    next.splice(idx, 1);
  }
  emit('update:selectedKbs', next);
};

const handleClickOutside = (event) => {
  const target = event.target;
  if (!target) return;
  if (kbMenuOpen.value && kbDropdownRef.value && !kbDropdownRef.value.contains(target)) {
    kbMenuOpen.value = false;
  }
};

const adjustHeight = () => {
  if (textareaRef.value) {
    textareaRef.value.style.height = 'auto';
    textareaRef.value.style.height = textareaRef.value.scrollHeight + 'px';
  }
};

const submit = () => {
  if (!question.value.trim() || props.isBusy) return;
  emit('send', question.value);
  question.value = '';
  if (textareaRef.value) textareaRef.value.style.height = 'auto';
};

const handleKeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
};

onMounted(() => {
  adjustHeight();
  document.addEventListener('click', handleClickOutside);
});

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside);
});

watch(question, () => {
  adjustHeight();
});
</script>

<template>
  <div class="relative w-full max-w-4xl mx-auto px-2 pb-2 pt-1 md:px-4 md:pb-8 md:pt-4">
    <Transition name="fade">
      <div v-show="status" class="absolute -top-4 left-6 flex items-center space-x-2 text-[10px] font-bold uppercase tracking-widest text-indigo-500 bg-indigo-50 dark:bg-indigo-900/40 px-2 py-0.5 rounded-sm shadow-sm border border-indigo-100 dark:border-indigo-800">
        <span class="relative flex h-1.5 w-1.5">
          <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-75"></span>
          <span class="relative inline-flex rounded-full h-1.5 w-1.5 bg-indigo-500"></span>
        </span>
        <span>{{ status }}</span>
      </div>
    </Transition>

    <div class="relative group bg-white dark:bg-slate-800 rounded-xl md:rounded-2xl shadow-lg shadow-indigo-100/20 dark:shadow-none border border-slate-200 dark:border-slate-800 focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-100/50 md:focus-within:ring-4 dark:focus-within:ring-indigo-900/30 transition-all duration-300 flex flex-col">
      <textarea
        ref="textareaRef"
        v-model="question"
        @keydown="handleKeydown"
        @input="adjustHeight"
        :disabled="isBusy"
        class="w-full bg-transparent resize-none border-0 focus:outline-none focus:ring-0 px-3 pt-2 pb-2 min-h-[40px] max-h-[140px] text-sm placeholder:text-slate-400 disabled:opacity-50 transition-all md:px-4 md:pt-3 md:pb-3 md:min-h-[56px] md:max-h-[300px] md:text-base text-slate-800 dark:text-slate-100"
        placeholder="输入您的问题... (Enter 发送, Shift+Enter 换行)"
        rows="1"
      ></textarea>

      <div class="flex flex-nowrap items-center justify-between gap-1.5 px-2 pb-2 min-w-0 md:px-4 md:pb-3 md:gap-2">
        <div class="scrollbar-none flex min-w-0 flex-1 flex-nowrap items-center gap-1.5 overflow-x-auto">
          <!-- 联网搜索按钮 -->
          <button
            type="button"
            :disabled="isBusy"
            @click="toggleSearch"
            class="inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 h-7 text-[11px] font-semibold transition-colors disabled:opacity-50 md:gap-2 md:px-3 md:h-8 md:text-[12px]"
            :class="searchEnabled
              ? 'border-indigo-200 bg-indigo-50 text-indigo-700 dark:border-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300'
              : 'border-slate-200 bg-slate-50 text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400'"
          >
            <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <line x1="2" y1="12" x2="22" y2="12"/>
              <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
            </svg>
            联网搜索
            <span>{{ searchEnabled ? '开' : '关' }}</span>
          </button>

          <!-- 知识库选择按钮 -->
          <div ref="kbDropdownRef" class="relative">
            <button
              type="button"
              :disabled="isBusy"
              @click="toggleKbMenu"
              class="inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 h-7 text-[11px] font-semibold transition-colors disabled:opacity-50 md:gap-2 md:px-3 md:h-8 md:text-[12px]"
              :class="selectedKbs.length > 0
                ? 'border-indigo-200 bg-indigo-50 text-indigo-700 dark:border-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300'
                : 'border-slate-200 bg-slate-50 text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400'"
            >
              <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                <path d="M20 17V5a2 2 0 0 0-2-2H6.5A2.5 2.5 0 0 0 4 5.5v13A2.5 2.5 0 0 0 6.5 21H20"/>
              </svg>
              知识库
              <span v-if="selectedKbs.length > 0">{{ selectedKbs.length }} 项</span>
            </button>
            <div
              v-if="kbMenuOpen"
              class="absolute left-0 bottom-full mb-2 w-64 rounded-xl border border-slate-200 bg-white shadow-lg shadow-slate-200/50 dark:border-slate-700 dark:bg-slate-900 dark:shadow-slate-900/30 overflow-hidden z-50"
            >
              <div class="px-4 py-2 bg-slate-50 text-xs font-semibold text-slate-500 border-b border-slate-200 dark:bg-slate-800/60 dark:border-slate-700 dark:text-slate-400">
                知识库
              </div>
              <div class="py-1">
                <button
                  v-for="kb in availableKbs"
                  :key="kb.id"
                  @click="toggleKb(kb.id)"
                  class="w-full flex items-center justify-between px-4 py-2 hover:bg-slate-50 dark:hover:bg-slate-800/60 transition-colors"
                >
                  <span class="text-[13px] text-slate-700 dark:text-slate-200">{{ kb.name }}</span>
                  <div class="relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out cursor-pointer"
                    :class="selectedKbs.includes(kb.id) ? 'bg-indigo-600' : 'bg-slate-200 dark:bg-slate-700'">
                    <span class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out"
                      :class="selectedKbs.includes(kb.id) ? 'translate-x-4' : 'translate-x-0'"></span>
                  </div>
                </button>
              </div>
            </div>
          </div>
        </div>

        <div class="flex shrink-0 items-center">
          <button
            v-if="isBusy"
            type="button"
            title="停止生成"
            @click="emit('abort')"
            class="p-1.5 md:p-2 rounded-lg md:rounded-xl bg-rose-500 text-white hover:bg-rose-600 transition-all duration-200 flex items-center justify-center shadow-md shadow-rose-200/50 dark:shadow-none active:scale-95 flex-shrink-0"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
              <rect x="5" y="5" width="14" height="14" rx="2"/>
            </svg>
          </button>
          <button
            v-else
            @click="submit"
            :disabled="!question.trim()"
            class="p-1.5 md:p-2 rounded-lg md:rounded-xl bg-indigo-600 text-white hover:bg-indigo-700 disabled:bg-slate-200 dark:disabled:bg-slate-700 disabled:text-slate-400 transition-all duration-300 flex items-center justify-center shadow-md shadow-indigo-200/50 dark:shadow-none active:scale-95 flex-shrink-0"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>
            </svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fade-enter-active, .fade-leave-active {
  transition: all 0.3s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

.scrollbar-none {
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.scrollbar-none::-webkit-scrollbar {
  display: none;
}
</style>
