<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import FilePreviewChip from './FilePreviewChip.vue';
import { type FileAttachment, type UploadParseMode } from '../composables/useFileUpload';
import { useVoiceInput } from '../composables/useVoiceInput';

interface Props {
  isBusy: boolean;
  status: string;
  searchEnabled?: boolean;
  researchMode?: 'normal' | 'deep_research';
  selectedSkills?: string[];
  selectedKbs?: string[];
  guestMode?: boolean;
  modelValue?: string;
  agentInfo?: any;
  selectedModel?: string;
  attachments?: FileAttachment[];
  uploadParseMode?: UploadParseMode;
}

const props = withDefaults(defineProps<Props>(), {
  searchEnabled: false,
  researchMode: 'normal',
  selectedSkills: () => [],
  selectedKbs: () => ['c056bf59-6091-45ed-98cb-e2e860580a69', 'de4ccde7-b286-4e49-bd8c-9dd37bd61878'],
  guestMode: false,
  modelValue: '',
  selectedModel: '',
  attachments: () => [],
  uploadParseMode: 'auto',
});

const emit = defineEmits<{
  (e: 'send', text: string): void;
  (e: 'abort'): void;
  (e: 'update:search-enabled', value: boolean): void;
  (e: 'update:research-mode', value: 'normal' | 'deep_research'): void;
  (e: 'update:selected-skills', value: string[]): void;
  (e: 'update:selected-kbs', value: string[]): void;
  (e: 'update:model-value', value: string): void;
  (e: 'update:selected-model', value: string): void;
  (e: 'files-selected', files: File[]): void;
  (e: 'remove-attachment', id: string): void;
  (e: 'open-library-picker'): void;
  (e: 'update:upload-parse-mode', value: UploadParseMode): void;
}>();

const attachmentMenuOpen = ref(false);
const attachmentDropdownRef = ref<HTMLDivElement | null>(null);

const toggleAttachmentMenu = () => {
  if (props.isBusy) return;
  attachmentMenuOpen.value = !attachmentMenuOpen.value;
};

const fileInputRef = ref<HTMLInputElement | null>(null);

const parseModeLabel = computed(() =>
  props.uploadParseMode === 'mineru' ? 'MinerU' : '自动'
);

function setUploadParseMode(mode: UploadParseMode) {
  emit('update:upload-parse-mode', mode);
}

function openFilePicker() {
  if (props.isBusy) return;
  fileInputRef.value?.click();
}

function onFileInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  if (input.files && input.files.length > 0) {
    emit('files-selected', Array.from(input.files));
    input.value = '';
  }
}

function onPaste(e: ClipboardEvent) {
  if (!e.clipboardData) return;
  const files: File[] = [];
  for (const item of Array.from(e.clipboardData.items)) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const f = item.getAsFile();
      if (f) files.push(f);
    }
  }
  if (files.length > 0) {
    e.preventDefault();
    emit('files-selected', files);
  }
}

function onDrop(e: DragEvent) {
  e.preventDefault();
  if (!e.dataTransfer) return;
  const files = Array.from(e.dataTransfer.files);
  if (files.length > 0) emit('files-selected', files);
}

function onDragOver(e: DragEvent) {
  e.preventDefault();
}

const question = ref(props.modelValue);
const selectedModelLocal = ref(props.selectedModel);

// ── 语音输入 ──────────────────────────────────────────
const {
  phase: voicePhase,
  pendingText,
  error: voiceError,
  recordingSeconds,
  startRecording,
  stopRecording,
  cancelEdit,
} = useVoiceInput();

const editPanelRef = ref<HTMLTextAreaElement | null>(null);

// 进入编辑状态时自动聚焦
watch(voicePhase, async (p) => {
  if (p === 'editing') {
    await nextTick();
    editPanelRef.value?.focus();
    editPanelRef.value?.select();
  }
});

// 格式化录音秒数
const recordingTimeLabel = computed(() => {
  const s = recordingSeconds.value;
  return `${Math.floor(s / 60).toString().padStart(2, '0')}:${(s % 60).toString().padStart(2, '0')}`;
});

function confirmVoiceText() {
  const text = pendingText.value.trim();
  if (!text) { cancelEdit(); return; }
  const prefix = question.value.trim() ? `${question.value.trim()} ` : '';
  question.value = `${prefix}${text}`;
  adjustHeight();
  cancelEdit();
}

// 按住 / 松开处理（同时支持触屏）
function onMicPointerDown(e: Event) {
  if (props.isBusy || voicePhase.value === 'transcribing') return;
  e.preventDefault();
  void startRecording();
}
function onMicPointerUp() {
  if (voicePhase.value === 'recording') stopRecording();
}
function onMicPointerLeave() {
  if (voicePhase.value === 'recording') stopRecording();
}
const textareaRef = ref<HTMLTextAreaElement | null>(null);
const modelDropdownRef = ref<HTMLDivElement | null>(null);
const modelOpen = ref(false);

watch(() => props.modelValue, (newVal) => {
  question.value = newVal;
  adjustHeight();
});

watch(() => props.selectedModel, (newVal) => {
  selectedModelLocal.value = newVal;
});

watch(question, (newVal) => {
  emit('update:model-value', newVal);
});

watch(selectedModelLocal, (newVal) => {
  emit('update:selected-model', newVal || '');
});

const modelLabel = computed(() => {
  const models = props.agentInfo?.available_models || [];
  const active = models.find((model: any) => model.name === selectedModelLocal.value);
  if (active) return active.display_name || active.name;
  if (!models.length) return props.agentInfo?.name ? '无可用模型' : '模型加载中...';
  return models[0]?.display_name || models[0]?.name || '选择模型';
});

const currentModelSupportsVision = computed(() => {
  const models = props.agentInfo?.available_models || [];
  const active = models.find((model: any) => model.name === selectedModelLocal.value)
    || models[0];
  return active?.supports_vision ?? false;
});

const hasImageAttachment = computed(() =>
  props.attachments.some((a) => a.category === 'multimodal')
);

const visionWarning = computed(() =>
  hasImageAttachment.value && !currentModelSupportsVision.value
    ? '当前模型不支持图片理解，请切换至 qwen 系列模型'
    : ''
);

const toggleModelMenu = () => {
  if (props.isBusy || !props.agentInfo?.available_models?.length) return;
  modelOpen.value = !modelOpen.value;
};

const skillDropdownRef = ref<HTMLDivElement | null>(null);
const skillMenuOpen = ref(false);

const availableSkills = ref([
  { id: 'deep-research', name: '深度研究技能' },
  { id: 'html-ppt', name: 'PPT 创建（HTML）' },
  { id: 'academic-search', name: '学术文献搜索' },
  { id: 'excel-experiment-analysis', name: '实验数据分析' },
]);

const kbDropdownRef = ref<HTMLDivElement | null>(null);
const kbMenuOpen = ref(false);

const availableKbs = computed(() => {
  const kbs = props.agentInfo?.available_kbs;
  if (Array.isArray(kbs) && kbs.length > 0) {
    return kbs.map((kb: { id: string; label: string }) => ({ id: kb.id, name: kb.label }));
  }
  const ids = props.agentInfo?.kb_ids;
  if (Array.isArray(ids) && ids.length > 0) {
    return ids.map((id: string) => ({ id, name: id }));
  }
  return [];
});

const toggleKbMenu = () => {
  if (props.isBusy) return;
  kbMenuOpen.value = !kbMenuOpen.value;
};

const toggleKb = (kbId: string) => {
  const newKbs = [...props.selectedKbs];
  const idx = newKbs.indexOf(kbId);
  if (idx === -1) {
    newKbs.push(kbId);
  } else {
    newKbs.splice(idx, 1);
  }
  emit('update:selected-kbs', newKbs);
};

const toggleSkillMenu = () => {
  if (props.isBusy) return;
  skillMenuOpen.value = !skillMenuOpen.value;
};

const toggleSkill = (skillId: string) => {
  if (props.guestMode && skillId === 'deep-research') return;
  const newSkills = [...props.selectedSkills];
  const idx = newSkills.indexOf(skillId);
  if (idx === -1) {
    newSkills.push(skillId);
  } else {
    newSkills.splice(idx, 1);
  }
  emit('update:selected-skills', newSkills);
};

const selectModel = (name: string) => {
  selectedModelLocal.value = name;
  modelOpen.value = false;
};

const handleClickOutside = (event: MouseEvent) => {
  const target = event.target as Node | null;
  if (!target) return;
  if (modelOpen.value && modelDropdownRef.value && !modelDropdownRef.value.contains(target)) {
    modelOpen.value = false;
  }
  if (skillMenuOpen.value && skillDropdownRef.value && !skillDropdownRef.value.contains(target)) {
    skillMenuOpen.value = false;
  }
  if (kbMenuOpen.value && kbDropdownRef.value && !kbDropdownRef.value.contains(target)) {
    kbMenuOpen.value = false;
  }
  if (attachmentMenuOpen.value && attachmentDropdownRef.value && !attachmentDropdownRef.value.contains(target)) {
    attachmentMenuOpen.value = false;
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
  // Reset height
  if (textareaRef.value) textareaRef.value.style.height = 'auto';
};

const handleKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
};

const toggleSearch = () => {
  if (props.isBusy) return;
  emit('update:search-enabled', !props.searchEnabled);
};

function handleGlobalKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && voicePhase.value === 'editing') cancelEdit();
}

onMounted(() => {
  adjustHeight();
  document.addEventListener('click', handleClickOutside);
  document.addEventListener('keydown', handleGlobalKeydown);
});

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside);
  document.removeEventListener('keydown', handleGlobalKeydown);
});

watch(question, () => {
  adjustHeight();
});
</script>

<template>
  <!-- 隐藏文件输入 -->
  <input
    ref="fileInputRef"
    type="file"
    multiple
    class="hidden"
    accept="image/*,.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.txt,.md,.csv,.json,.py,.html,.xml,.yaml,.yml"
    @change="onFileInputChange"
  />

  <div
    class="relative w-full max-w-4xl mx-auto px-2 pb-1 pt-1 md:px-4 md:pb-4 md:pt-4"
    @dragover="onDragOver"
    @drop="onDrop"
  >
    <!-- Status Indicator -->
    <Transition name="fade">
      <div v-show="status" class="absolute -top-4 left-6 flex items-center space-x-2 text-[10px] font-bold uppercase tracking-widest text-indigo-500 bg-indigo-50 dark:bg-indigo-900/40 px-2 py-0.5 rounded-sm shadow-sm border border-indigo-100 dark:border-indigo-800">
        <span class="relative flex h-1.5 w-1.5">
          <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-75"></span>
          <span class="relative inline-flex rounded-full h-1.5 w-1.5 bg-indigo-500"></span>
        </span>
        <span>{{ status }}</span>
      </div>
    </Transition>

    <!-- 语音识别错误提示 -->
    <Transition name="fade">
      <p v-if="voiceError" class="absolute -top-5 left-1/2 -translate-x-1/2 whitespace-nowrap text-[11px] text-rose-500 dark:text-rose-400 bg-rose-50 dark:bg-rose-900/30 border border-rose-200 dark:border-rose-800 px-2.5 py-0.5 rounded-full shadow-sm">
        {{ voiceError }}
      </p>
    </Transition>

    <!-- ① 录音中遮罩 -->
    <Transition name="voice-overlay">
      <div
        v-if="voicePhase === 'recording'"
        class="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 rounded-xl md:rounded-2xl bg-white/95 dark:bg-slate-800/95 backdrop-blur-sm select-none"
      >
        <!-- 声波动画 -->
        <div class="flex items-end gap-1 h-10">
          <span v-for="i in 7" :key="i" class="w-1.5 rounded-full bg-indigo-500 dark:bg-indigo-400 wave-bar" :style="`animation-delay:${(i - 1) * 0.1}s`"></span>
        </div>
        <p class="text-sm font-semibold text-slate-700 dark:text-slate-200">松开完成录音</p>
        <p class="text-xs text-slate-400 tabular-nums">{{ recordingTimeLabel }}</p>
      </div>
    </Transition>

    <!-- ② 识别中遮罩 -->
    <Transition name="voice-overlay">
      <div
        v-if="voicePhase === 'transcribing'"
        class="absolute inset-0 z-20 flex flex-col items-center justify-center gap-2 rounded-xl md:rounded-2xl bg-white/95 dark:bg-slate-800/95 backdrop-blur-sm select-none"
      >
        <svg class="h-6 w-6 text-indigo-500 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
        </svg>
        <p class="text-sm font-semibold text-slate-600 dark:text-slate-300">正在识别语音…</p>
      </div>
    </Transition>

    <div class="relative group bg-white dark:bg-slate-800 rounded-xl md:rounded-2xl shadow-lg shadow-indigo-100/20 dark:shadow-none border border-slate-200 dark:border-slate-800 focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-100/50 md:focus-within:ring-4 dark:focus-within:ring-indigo-900/30 transition-all duration-300 flex flex-col">
      <p v-if="guestMode || researchMode === 'deep_research'" class="px-3 pt-2 pb-0.5 text-[10px] leading-snug text-slate-500 md:px-4 md:pt-3 md:pb-1 md:text-[11px] md:leading-5 dark:text-slate-400">
        {{ guestMode
          ? '访客模式已禁用深度研究，请登录后使用。'
          : (researchMode === 'deep_research' ? '已启用深度研究：会自动切换到子智能体编排和研究报告流程。' : '') }}
      </p>

      <!-- 附件预览区 -->
      <FilePreviewChip
        :attachments="attachments"
        :disabled="isBusy"
        @remove="(id) => emit('remove-attachment', id)"
      />

      <!-- Vision 不兼容警告 -->
      <p v-if="visionWarning" class="px-4 pt-1 pb-0 text-[11px] leading-5 text-amber-600 dark:text-amber-400 flex items-center gap-1">
        <svg class="h-3 w-3 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
        {{ visionWarning }}
      </p>

      <textarea
        ref="textareaRef"
        v-model="question"
        @keydown="handleKeydown"
        @input="adjustHeight"
        @paste="onPaste"
        :disabled="isBusy"
        class="w-full bg-transparent resize-none border-0 focus:outline-none focus:ring-0 px-3 pt-1.5 pb-1 md:px-4 md:pt-3 md:pb-3 min-h-[40px] md:min-h-[56px] max-h-[120px] md:max-h-[300px] text-slate-800 dark:text-slate-100 text-sm md:text-base placeholder:text-slate-400 disabled:opacity-50 transition-all"
        placeholder="输入您的问题... (Enter 发送, Shift+Enter 换行, 可粘贴图片或使用语音输入)"
        rows="1"
      ></textarea>
      
      <div class="flex flex-nowrap items-center justify-between gap-1.5 px-2 pb-2 md:px-4 md:pb-3 md:gap-2 min-w-0">
        <div class="flex flex-wrap flex-1 items-center gap-1 min-w-0 pb-0.5">
          <!-- 文件添加下拉选项 -->
          <div ref="attachmentDropdownRef" class="relative flex-shrink-0">
            <button
              type="button"
              :disabled="isBusy"
              @click="toggleAttachmentMenu"
              class="inline-flex items-center justify-center w-7 h-7 md:w-8 md:h-8 rounded-full border transition-colors disabled:opacity-50 flex-shrink-0"
              :class="attachments.length > 0
                ? 'border-indigo-200 bg-indigo-50 text-indigo-600 dark:border-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400'
                : 'border-slate-200 bg-slate-50 text-slate-500 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400'"
              title="添加文件（上传本地文件或从文件库选择）"
            >
              <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
              </svg>
              <span v-if="attachments.length > 0" class="sr-only">{{ attachments.length }} 个附件</span>
            </button>

            <!-- 下拉菜单 -->
            <div
              v-if="attachmentMenuOpen"
              class="absolute left-0 bottom-full mb-2 w-56 rounded-xl border border-slate-200 bg-white shadow-lg shadow-slate-200/50 dark:border-slate-700 dark:bg-slate-900 dark:shadow-slate-900/30 overflow-hidden z-50 animate-fade-in"
            >
              <div class="py-1">
                <button
                  type="button"
                  @click="openFilePicker(); attachmentMenuOpen = false"
                  class="w-full flex items-center space-x-2 px-3 py-2.5 text-left text-[12px] text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-800/60 transition-colors"
                >
                  <svg class="w-3.5 h-3.5 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                  </svg>
                  <span>上传本地文件</span>
                </button>
                <button
                  type="button"
                  @click="emit('open-library-picker'); attachmentMenuOpen = false"
                  class="w-full flex items-center space-x-2 px-3 py-2.5 text-left text-[12px] text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-800/60 transition-colors"
                >
                  <svg class="w-3.5 h-3.5 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z" />
                  </svg>
                  <span>从我的文件添加</span>
                </button>
              </div>
              <div class="border-t border-slate-200 px-3 py-2 dark:border-slate-700">
                <p class="text-[11px] font-semibold text-slate-500 dark:text-slate-400">文档解析方式</p>
                <div class="mt-2 grid gap-1">
                  <button
                    type="button"
                    @click="setUploadParseMode('auto')"
                    class="flex items-start justify-between rounded-lg px-2 py-2 text-left text-[12px] transition-colors"
                    :class="uploadParseMode === 'auto'
                      ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300'
                      : 'text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-800/60'"
                  >
                    <span>自动解析</span>
                    <span v-if="uploadParseMode === 'auto'" class="text-[10px] font-semibold">当前</span>
                  </button>
                  <button
                    type="button"
                    @click="setUploadParseMode('mineru')"
                    class="flex items-start justify-between rounded-lg px-2 py-2 text-left text-[12px] transition-colors"
                    :class="uploadParseMode === 'mineru'
                      ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300'
                      : 'text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-800/60'"
                  >
                    <span>使用 MinerU</span>
                    <span v-if="uploadParseMode === 'mineru'" class="text-[10px] font-semibold">当前</span>
                  </button>
                </div>
                <p class="mt-2 text-[10px] leading-4 text-slate-400 dark:text-slate-500">
                  当前：{{ parseModeLabel }}。论文图片较多时建议使用 MinerU。
                </p>
              </div>
            </div>
          </div>

          <!-- 麦克风按住说话按钮 -->
          <button
            type="button"
            :disabled="isBusy || voicePhase === 'transcribing'"
            @mousedown.prevent="onMicPointerDown"
            @mouseup="onMicPointerUp"
            @mouseleave="onMicPointerLeave"
            @touchstart.prevent="onMicPointerDown"
            @touchend.prevent="onMicPointerUp"
            class="inline-flex items-center justify-center w-7 h-7 md:w-8 md:h-8 rounded-full border transition-colors disabled:opacity-50 flex-shrink-0 touch-none"
            :class="voicePhase === 'recording'
              ? 'border-rose-400 bg-rose-500 text-white shadow-md shadow-rose-200 dark:shadow-rose-900/40'
              : 'border-slate-200 bg-slate-50 text-slate-500 hover:bg-indigo-50 hover:border-indigo-200 hover:text-indigo-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400'"
            title="按住说话"
          >
            <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              <line x1="12" y1="19" x2="12" y2="22"/>
            </svg>
          </button>

          <button
            type="button"
            :disabled="isBusy"
            @click="toggleSearch"
            class="inline-flex items-center gap-1.5 rounded-full border px-2 md:px-3 h-7 md:h-8 text-[11px] md:text-[12px] font-semibold transition-colors disabled:opacity-50 flex-shrink-0 whitespace-nowrap"
            :class="searchEnabled
              ? 'border-indigo-200 bg-indigo-50 text-indigo-700 dark:border-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300'
              : 'border-slate-200 bg-slate-50 text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400'"
          >
            <svg class="h-3 w-3 md:h-3.5 md:w-3.5 flex-shrink-0" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <line x1="2" y1="12" x2="22" y2="12"/>
              <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
            </svg>
            <span class="hidden sm:inline">联网</span>
            <span>{{ searchEnabled ? '开' : '关' }}</span>
          </button>

          <div ref="skillDropdownRef" class="relative flex-shrink-0">
            <button
              type="button"
              :disabled="isBusy"
              @click="toggleSkillMenu"
              class="inline-flex items-center gap-1.5 rounded-full border px-2 md:px-3 h-7 md:h-8 text-[11px] md:text-[12px] font-semibold transition-colors disabled:opacity-50 whitespace-nowrap"
              :class="selectedSkills.length > 0
                ? 'border-indigo-200 bg-indigo-50 text-indigo-700 dark:border-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300'
                : 'border-slate-200 bg-slate-50 text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400'"
            >
              <svg class="h-3 w-3 md:h-3.5 md:w-3.5 flex-shrink-0" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                <path d="M2 17l10 5 10-5"/>
                <path d="M2 12l10 5 10-5"/>
              </svg>
              技能
              <span v-if="selectedSkills.length > 0">{{ selectedSkills.length }}</span>
            </button>
            <div
              v-if="skillMenuOpen"
              class="absolute left-0 bottom-full mb-2 w-64 rounded-xl border border-slate-200 bg-white shadow-lg shadow-slate-200/50 dark:border-slate-700 dark:bg-slate-900 dark:shadow-slate-900/30 overflow-hidden z-50"
            >
              <div class="px-4 py-2 bg-slate-50 text-xs font-semibold text-slate-500 border-b border-slate-200 dark:bg-slate-800/60 dark:border-slate-700 dark:text-slate-400">
                内置
              </div>
              <div class="py-1">
                <button
                  v-for="skill in availableSkills"
                  :key="skill.id"
                  @click="toggleSkill(skill.id)"
                  :disabled="guestMode && skill.id === 'deep-research'"
                  class="w-full flex items-center justify-between px-4 py-2 hover:bg-slate-50 dark:hover:bg-slate-800/60 transition-colors disabled:opacity-50 disabled:cursor-not-allowed group"
                >
                  <span class="text-[13px] text-slate-700 dark:text-slate-200">{{ skill.name }}</span>
                  <div 
                    class="relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none"
                    :class="selectedSkills.includes(skill.id) ? 'bg-indigo-600' : 'bg-slate-200 dark:bg-slate-700'"
                  >
                    <span 
                      class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out"
                      :class="selectedSkills.includes(skill.id) ? 'translate-x-4' : 'translate-x-0'"
                    ></span>
                  </div>
                </button>
              </div>
            </div>
          </div>

          <div ref="kbDropdownRef" class="relative flex-shrink-0">
            <button
              type="button"
              :disabled="isBusy"
              @click="toggleKbMenu"
              class="inline-flex items-center gap-1.5 rounded-full border px-2 md:px-3 h-7 md:h-8 text-[11px] md:text-[12px] font-semibold transition-colors disabled:opacity-50 whitespace-nowrap"
              :class="selectedKbs.length > 0
                ? 'border-indigo-200 bg-indigo-50 text-indigo-700 dark:border-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300'
                : 'border-slate-200 bg-slate-50 text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400'"
            >
              <svg class="h-3 w-3 md:h-3.5 md:w-3.5 flex-shrink-0" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                <path d="M20 17V5a2 2 0 0 0-2-2H6.5A2.5 2.5 0 0 0 4 5.5v13A2.5 2.5 0 0 0 6.5 21H20"/>
              </svg>
              知识库
              <span v-if="selectedKbs.length > 0">{{ selectedKbs.length }}</span>
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
                  class="w-full flex items-center justify-between px-4 py-2 hover:bg-slate-50 dark:hover:bg-slate-800/60 transition-colors disabled:opacity-50 disabled:cursor-not-allowed group"
                >
                  <span class="text-[13px] text-slate-700 dark:text-slate-200">{{ kb.name }}</span>
                  <div 
                    class="relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none"
                    :class="selectedKbs.includes(kb.id) ? 'bg-indigo-600' : 'bg-slate-200 dark:bg-slate-700'"
                  >
                    <span 
                      class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out"
                      :class="selectedKbs.includes(kb.id) ? 'translate-x-4' : 'translate-x-0'"
                    ></span>
                  </div>
                </button>
              </div>
            </div>
          </div>
        </div>

        <div class="flex items-center gap-1.5 md:gap-2 flex-shrink-0">
          <div ref="modelDropdownRef" class="relative">
            <button
              type="button"
              :disabled="isBusy || !agentInfo?.available_models?.length"
              @click="toggleModelMenu"
              class="h-7 md:h-8 cursor-pointer px-2 md:px-3 rounded-full border transition-colors border-slate-200 bg-slate-50 text-[11px] md:text-[12px] font-semibold text-slate-500 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 inline-flex items-center gap-1.5 whitespace-nowrap"
              title="选择模型"
            >
              <span class="max-w-[80px] sm:max-w-[120px] md:max-w-[160px] truncate">{{ modelLabel }}</span>
              <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="m18 15-6-6-6 6" />
              </svg>
            </button>
            <div
              v-if="modelOpen"
              class="absolute right-0 bottom-full mb-2 w-56 rounded-xl border border-slate-200 bg-white shadow-lg shadow-slate-200/50 dark:border-slate-700 dark:bg-slate-900 dark:shadow-slate-900/30"
            >
              <button
                v-for="model in (agentInfo?.available_models || [])"
                :key="model.name"
                type="button"
                class="w-full px-3 py-2 text-left text-[12px] text-slate-600 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-800/60 first:rounded-t-xl last:rounded-b-xl"
                @click="selectModel(model.name)"
              >
                {{ model.display_name }}
              </button>
            </div>
          </div>

          <!-- 停止按钮（思考中时显示） -->
          <button
            v-if="isBusy"
            @click="emit('abort')"
            title="停止生成"
            class="p-1.5 md:p-2 rounded-xl bg-rose-500 text-white hover:bg-rose-600 transition-all duration-200 flex items-center justify-center shadow-lg shadow-rose-200/50 dark:shadow-none active:scale-95 flex-shrink-0"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
              <rect x="5" y="5" width="14" height="14" rx="2"/>
            </svg>
          </button>
          <!-- 发送按钮（空闲时显示） -->
          <button
            v-else
            @click="submit"
            :disabled="!question.trim()"
            class="p-1.5 md:p-2 rounded-xl bg-indigo-600 text-white hover:bg-indigo-700 disabled:bg-slate-200 dark:disabled:bg-slate-700 disabled:text-slate-400 transition-all duration-300 flex items-center justify-center shadow-lg shadow-indigo-200/50 dark:shadow-none active:scale-95 flex-shrink-0"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>
            </svg>
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- ③ 识别结果编辑面板（浮层） -->
  <Transition name="edit-panel">
    <div
      v-if="voicePhase === 'editing'"
      class="fixed inset-x-0 bottom-0 z-50 flex justify-center pb-4 px-3 pointer-events-none"
    >
      <div class="pointer-events-auto w-full max-w-2xl bg-white dark:bg-slate-800 rounded-2xl shadow-2xl shadow-slate-300/40 dark:shadow-slate-900/80 border border-slate-200 dark:border-slate-700 overflow-hidden">
        <!-- 面板头部 -->
        <div class="flex items-center justify-between px-4 py-2.5 bg-slate-50 dark:bg-slate-900/60 border-b border-slate-100 dark:border-slate-700">
          <div class="flex items-center gap-2 text-[12px] font-semibold text-slate-500 dark:text-slate-400">
            <svg class="h-3.5 w-3.5 text-indigo-500" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              <line x1="12" y1="19" x2="12" y2="22"/>
            </svg>
            语音识别结果（可修改）
          </div>
          <button
            type="button"
            @click="cancelEdit"
            class="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 transition-colors p-1 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700"
            title="取消"
          >
            <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        <!-- 可编辑文本区 -->
        <textarea
          ref="editPanelRef"
          v-model="pendingText"
          rows="3"
          class="w-full bg-transparent resize-none border-0 focus:outline-none focus:ring-0 px-4 py-3 text-sm text-slate-800 dark:text-slate-100 placeholder:text-slate-400"
          placeholder="识别结果将显示在这里，可直接修改…"
          @keydown.ctrl.enter.prevent="confirmVoiceText"
        ></textarea>

        <!-- 操作栏 -->
        <div class="flex items-center justify-between px-4 py-2.5 border-t border-slate-100 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/60">
          <p class="text-[11px] text-slate-400 dark:text-slate-500">Ctrl+Enter 插入 · Esc 取消</p>
          <div class="flex items-center gap-2">
            <button
              type="button"
              @click="cancelEdit"
              class="px-3 h-7 rounded-lg text-[12px] font-medium text-slate-500 hover:bg-slate-200 dark:text-slate-400 dark:hover:bg-slate-700 transition-colors"
            >取消</button>
            <button
              type="button"
              @click="confirmVoiceText"
              :disabled="!pendingText.trim()"
              class="px-4 h-7 rounded-lg text-[12px] font-semibold bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40 transition-colors shadow-sm"
            >插入输入框</button>
          </div>
        </div>
      </div>
    </div>
  </Transition>
</template>


<style scoped>
.fade-enter-active, .fade-leave-active {
  transition: all 0.3s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

/* 录音/识别中遮罩 */
.voice-overlay-enter-active, .voice-overlay-leave-active {
  transition: opacity 0.2s ease;
}
.voice-overlay-enter-from, .voice-overlay-leave-to {
  opacity: 0;
}

/* 编辑面板从底部滑入 */
.edit-panel-enter-active {
  transition: transform 0.28s cubic-bezier(0.34, 1.56, 0.64, 1), opacity 0.2s ease;
}
.edit-panel-leave-active {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.edit-panel-enter-from, .edit-panel-leave-to {
  transform: translateY(100%);
  opacity: 0;
}

/* 声波动画 */
@keyframes wave {
  0%, 100% { height: 8px;  }
  50%       { height: 32px; }
}
.wave-bar {
  animation: wave 0.9s ease-in-out infinite;
}

/* 隐藏横向滚动条 */
.scrollbar-none {
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.scrollbar-none::-webkit-scrollbar {
  display: none;
}
</style>
