<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { Bot, ChevronUp, Search, X } from 'lucide-vue-next';
import FilePreviewChip from './FilePreviewChip.vue';
import { type FileAttachment, type UploadParseMode } from '../composables/useFileUpload';
import { useVoiceInput } from '../composables/useVoiceInput';

interface Props {
  isBusy: boolean;
  status: string;
  searchEnabled?: boolean;
  deepRetrieval?: boolean;
  researchMode?: 'normal' | 'deep_research';
  selectedSkills?: string[];
  selectedKbs?: string[];
  showKbSelector?: boolean;
  showSkillSelector?: boolean;
  guestMode?: boolean;
  modelValue?: string;
  agentInfo?: any;
  selectedModel?: string;
  attachments?: FileAttachment[];
  uploadParseMode?: UploadParseMode;
}

const props = withDefaults(defineProps<Props>(), {
  searchEnabled: false,
  deepRetrieval: false,
  researchMode: 'normal',
  selectedSkills: () => [],
  selectedKbs: () => [],
  showKbSelector: true,
  showSkillSelector: true,
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
  (e: 'update:deep-retrieval', value: boolean): void;
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
const attachmentMenuPanelRef = ref<HTMLDivElement | null>(null);

function closeAllMenus() {
  modelOpen.value = false;
  skillMenuOpen.value = false;
  kbMenuOpen.value = false;
  attachmentMenuOpen.value = false;
}

const toggleAttachmentMenu = () => {
  if (props.isBusy) return;
  const opening = !attachmentMenuOpen.value;
  closeAllMenus();
  attachmentMenuOpen.value = opening;
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
const modelMenuPanelRef = ref<HTMLDivElement | null>(null);
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

const toggleModelMenu = () => {
  if (props.isBusy || !props.agentInfo?.available_models?.length) return;
  const opening = !modelOpen.value;
  closeAllMenus();
  modelOpen.value = opening;
};

const skillMenuPanelRef = ref<HTMLDivElement | null>(null);
const skillSearchInputRef = ref<HTMLInputElement | null>(null);
const skillMenuOpen = ref(false);
const skillSearchQuery = ref('');

interface SkillMenuOption {
  id: string;
  name: string;
  description?: string;
  required?: boolean;
  selectable?: boolean;
  guestSelectable: boolean;
}

const availableSkills = computed<SkillMenuOption[]>(() => {
  const skills = props.agentInfo?.available_skills;
  if (!Array.isArray(skills) || skills.length === 0) return [];
  return skills
    .map((skill: any) => {
      const id = String(skill.name || skill.id || '').trim();
      if (!id) return null;
      return {
        id,
        name: String(skill.label || skill.display_name || skill.name || id),
        description: skill.display_description || skill.description,
        required: Boolean(skill.required),
        selectable: skill.selectable !== false,
        guestSelectable: skill.guest_selectable === true,
      };
    })
    .filter(Boolean) as SkillMenuOption[];
});

const filteredSkills = computed(() => {
  const q = skillSearchQuery.value.trim().toLowerCase();
  if (!q) return availableSkills.value;
  return availableSkills.value.filter((skill) => {
    const name = skill.name.toLowerCase();
    const desc = (skill.description || '').toLowerCase();
    return name.includes(q) || desc.includes(q) || skill.id.toLowerCase().includes(q);
  });
});

const kbMenuPanelRef = ref<HTMLDivElement | null>(null);
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
  const opening = !kbMenuOpen.value;
  closeAllMenus();
  kbMenuOpen.value = opening;
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

const closeSkillMenu = () => {
  skillMenuOpen.value = false;
  skillSearchQuery.value = '';
};

const toggleSkillMenu = () => {
  if (props.isBusy) return;
  const opening = !skillMenuOpen.value;
  closeAllMenus();
  if (opening) {
    skillSearchQuery.value = '';
    skillMenuOpen.value = true;
    void nextTick(() => skillSearchInputRef.value?.focus());
  }
};

const isSkillDisabled = (skill: SkillMenuOption) =>
  (props.guestMode && !skill.guestSelectable) || skill.required || skill.selectable === false;

const toggleSkill = (skillId: string) => {
  const skill = availableSkills.value.find((item) => item.id === skillId);
  if (skill && isSkillDisabled(skill)) return;
  const newSkills = [...props.selectedSkills];
  const idx = newSkills.indexOf(skillId);
  if (idx === -1) {
    newSkills.push(skillId);
  } else {
    newSkills.splice(idx, 1);
  }
  emit('update:selected-skills', newSkills);
};

const clearSelectedSkills = () => {
  const requiredIds = availableSkills.value.filter((s) => s.required).map((s) => s.id);
  emit('update:selected-skills', requiredIds);
};

const selectModel = (name: string) => {
  selectedModelLocal.value = name;
  modelOpen.value = false;
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
  if (props.isBusy || !searchAvailable.value) return;
  emit('update:search-enabled', !props.searchEnabled);
};

const searchAvailable = computed(() => props.agentInfo?.search_enabled === true);

const toggleDeepRetrieval = () => {
  if (props.isBusy) return;
  emit('update:deep-retrieval', !props.deepRetrieval);
};

function handleGlobalKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return;
  if (modelOpen.value || skillMenuOpen.value || kbMenuOpen.value || attachmentMenuOpen.value) {
    closeAllMenus();
    skillSearchQuery.value = '';
    return;
  }
  if (voicePhase.value === 'editing') cancelEdit();
}

onMounted(() => {
  adjustHeight();
  document.addEventListener('keydown', handleGlobalKeydown);
});

onUnmounted(() => {
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
    accept="image/*,.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.txt,.md,.csv,.json,.py,.html,.xml,.kml,.yaml,.yml"
    @change="onFileInputChange"
  />

  <div
    class="relative w-full min-w-0"
    @dragover="onDragOver"
    @drop="onDrop"
  >
    <!-- Status Indicator -->
    <Transition name="fade">
      <div v-show="status" class="absolute -top-5 left-3 flex items-center gap-1.5 rounded-full border border-border-subtle bg-surface px-2.5 py-0.5 text-[11px] font-medium text-accent">
        <span class="relative flex size-1.5">
          <span class="absolute inline-flex size-full animate-ping rounded-full bg-accent opacity-75"></span>
          <span class="relative inline-flex size-1.5 rounded-full bg-accent"></span>
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
        class="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 rounded-xl md:rounded-2xl bg-surface/95 backdrop-blur-sm select-none"
      >
        <!-- 声波动画 -->
        <div class="flex items-end gap-1 h-10">
          <span v-for="i in 7" :key="i" class="w-1.5 rounded-full bg-accent wave-bar" :style="`animation-delay:${(i - 1) * 0.1}s`"></span>
        </div>
        <p class="text-sm font-semibold text-text-secondary">松开完成录音</p>
        <p class="text-xs text-text-tertiary tabular-nums">{{ recordingTimeLabel }}</p>
      </div>
    </Transition>

    <!-- ② 识别中遮罩 -->
    <Transition name="voice-overlay">
      <div
        v-if="voicePhase === 'transcribing'"
        class="absolute inset-0 z-20 flex flex-col items-center justify-center gap-2 rounded-xl md:rounded-2xl bg-surface/95 backdrop-blur-sm select-none"
      >
        <svg class="h-6 w-6 text-accent animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
        </svg>
        <p class="text-sm font-semibold text-text-secondary">正在识别语音…</p>
      </div>
    </Transition>

    <div class="group relative flex min-w-0 flex-col rounded-(--radius-composer) border border-border-strong bg-surface transition-[border-color,box-shadow] duration-200 focus-within:border-accent focus-within:shadow-panel">
      <p v-if="guestMode || researchMode === 'deep_research'" class="px-4 pb-0.5 pt-2 text-[11px] leading-snug text-text-tertiary">
        {{ guestMode
          ? '访客模式已禁用可选技能，请登录后使用。'
          : (researchMode === 'deep_research' ? '已启用深度研究：会自动切换到子智能体编排和研究报告流程。' : '') }}
      </p>

      <!-- 附件预览区 -->
      <FilePreviewChip
        :attachments="attachments"
        :disabled="isBusy"
        @remove="(id) => emit('remove-attachment', id)"
      />

      <textarea
        ref="textareaRef"
        v-model="question"
        @keydown="handleKeydown"
        @input="adjustHeight"
        @paste="onPaste"
        :disabled="isBusy"
        class="custom-scrollbar max-h-48 min-h-12 w-full resize-none border-0 bg-transparent px-4 pb-1.5 pt-3 text-[15px] leading-relaxed text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-0 disabled:opacity-50"
        placeholder="输入你的问题..."
        rows="1"
      ></textarea>

      <div class="flex min-w-0 items-center justify-between gap-2 px-3 pb-2.5">
        <div class="scrollbar-none flex min-w-0 flex-1 items-center gap-1 overflow-x-auto @cozy/main:gap-1.5">
          <!-- 文件添加下拉选项 -->
          <div class="relative flex-shrink-0">
            <button
              type="button"
              :disabled="isBusy"
              @click.stop="toggleAttachmentMenu"
              class="inline-flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border transition-colors disabled:opacity-50"
              :class="attachments.length > 0 ? 'border-accent/30 bg-accent-soft text-accent'
                : 'border-border-subtle bg-surface-sunken text-text-tertiary hover:border-accent/30 hover:bg-accent-soft hover:text-accent'"
              title="添加文件（上传本地文件或从文件库选择）"
            >
              <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
              </svg>
              <span v-if="attachments.length > 0" class="sr-only">{{ attachments.length }} 个附件</span>
            </button>

            <Teleport to="body">
              <Transition name="skill-modal">
                <div
                  v-if="attachmentMenuOpen"
                  class="fixed inset-0 z-[100] flex items-end justify-center p-0 sm:items-center sm:p-4"
                  role="dialog"
                  aria-modal="true"
                  aria-labelledby="attachment-picker-title"
                >
                  <div class="skill-modal-backdrop absolute inset-0 bg-slate-950/18 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="attachmentMenuOpen = false" />
                  <div
                    ref="attachmentMenuPanelRef"
                    class="skill-modal-panel relative flex w-full max-w-[34rem] flex-col overflow-hidden rounded-t-[1.75rem] border border-white/55 bg-surface/82 shadow-[0_24px_80px_rgba(15,23,42,0.20),0_3px_16px_rgba(15,23,42,0.08)] backdrop-blur-2xl dark:border-white/10 dark:bg-surface/88 sm:max-h-[min(80vh,520px)] sm:rounded-[1.75rem]"
                    @click.stop
                  >
                    <div class="flex items-center justify-between border-b border-border-subtle/80 bg-surface/38 px-5 py-4 sm:px-6">
                      <div>
                        <h3 id="attachment-picker-title" class="text-base font-bold tracking-tight text-text-primary">添加内容</h3>
                        <p class="mt-1 text-xs text-text-tertiary">上传本地文件，或从我的文件中选择</p>
                      </div>
                      <button type="button" class="rounded-xl p-2 text-text-tertiary transition-colors hover:bg-surface/75 hover:text-text-primary" aria-label="关闭添加内容窗口" @click="attachmentMenuOpen = false">
                        <X class="h-5 w-5" :stroke-width="2" />
                      </button>
                    </div>
                    <div class="grid gap-3 bg-surface/24 p-5 sm:grid-cols-2 sm:px-6">
                      <button type="button" @click="openFilePicker(); attachmentMenuOpen = false" class="flex min-h-24 flex-col items-start justify-center rounded-2xl border border-border-subtle/90 bg-surface/48 p-4 text-left transition-all hover:-translate-y-0.5 hover:border-accent/35 hover:bg-surface/72 hover:shadow-sm">
                        <span class="font-semibold text-text-primary">上传本地文件</span>
                        <span class="mt-1 text-xs leading-relaxed text-text-tertiary">支持图片、PDF、Office 文档及常用文本格式</span>
                      </button>
                      <button type="button" @click="emit('open-library-picker'); attachmentMenuOpen = false" class="flex min-h-24 flex-col items-start justify-center rounded-2xl border border-border-subtle/90 bg-surface/48 p-4 text-left transition-all hover:-translate-y-0.5 hover:border-accent/35 hover:bg-surface/72 hover:shadow-sm">
                        <span class="font-semibold text-text-primary">从我的文件添加</span>
                        <span class="mt-1 text-xs leading-relaxed text-text-tertiary">选择已上传到个人文件库中的内容</span>
                      </button>
                    </div>
                    <div class="border-t border-border-subtle/80 bg-surface/40 px-5 py-4 sm:px-6">
                      <p class="text-xs font-semibold text-text-secondary">文档解析方式</p>
                      <div class="mt-3 grid gap-2 sm:grid-cols-2">
                        <button type="button" @click="setUploadParseMode('auto')" class="flex items-center justify-between rounded-xl border px-3.5 py-3 text-left text-sm transition-colors" :class="uploadParseMode === 'auto' ? 'border-accent/40 bg-accent-soft text-accent' : 'border-border-subtle bg-surface/45 text-text-secondary hover:bg-surface/70'">
                          <span>自动解析</span><span v-if="uploadParseMode === 'auto'" class="text-[11px] font-semibold">当前</span>
                        </button>
                        <button type="button" @click="setUploadParseMode('mineru')" class="flex items-center justify-between rounded-xl border px-3.5 py-3 text-left text-sm transition-colors" :class="uploadParseMode === 'mineru' ? 'border-accent/40 bg-accent-soft text-accent' : 'border-border-subtle bg-surface/45 text-text-secondary hover:bg-surface/70'">
                          <span>使用 MinerU</span><span v-if="uploadParseMode === 'mineru'" class="text-[11px] font-semibold">当前</span>
                        </button>
                      </div>
                      <p class="mt-3 text-[11px] leading-5 text-text-tertiary">当前：{{ parseModeLabel }}。论文图片较多时建议使用 MinerU。</p>
                    </div>
                  </div>
                </div>
              </Transition>
            </Teleport>
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
            class="inline-flex h-8 w-8 flex-shrink-0 touch-none items-center justify-center rounded-full border transition-colors disabled:opacity-50"
            :class="voicePhase === 'recording' ? 'border-rose-400 bg-rose-500 text-white shadow-sm'
              : 'border-border-subtle bg-surface-sunken text-text-tertiary hover:border-accent/30 hover:bg-accent-soft hover:text-accent'"
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
            :disabled="isBusy || !searchAvailable"
            @click="toggleSearch"
            class="relative inline-flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border transition-colors disabled:opacity-50 sm:w-auto sm:gap-1.5 sm:px-3 sm:text-[12px]"
            :class="searchAvailable && searchEnabled ? 'border-accent/30 bg-accent-soft text-accent'
              : 'border-border-subtle bg-surface-sunken text-text-tertiary'"
            :title="!searchAvailable ? '当前智能体未开放联网搜索' : (searchEnabled ? '联网搜索：已开启' : '联网搜索：已关闭')"
          >
            <svg class="h-4 w-4 flex-shrink-0 sm:h-3.5 sm:w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <line x1="2" y1="12" x2="22" y2="12"/>
              <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
            </svg>
            <span class="hidden font-medium @cozy/main:inline">联网</span>
            <span class="hidden font-medium @cozy/main:inline">{{ searchAvailable && searchEnabled ? '开' : '关' }}</span>
            <span
              v-if="searchAvailable && searchEnabled"
              class="absolute right-0.5 top-0.5 h-2 w-2 rounded-full bg-accent ring-2 ring-surface @cozy/main:hidden"
            />
          </button>

          <button
            type="button"
            :disabled="isBusy"
            @click="toggleDeepRetrieval"
            class="relative inline-flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border transition-colors disabled:opacity-50 sm:w-auto sm:gap-1.5 sm:px-3 sm:text-[12px]"
            :class="deepRetrieval ? 'border-accent/30 bg-accent-soft text-accent'
              : 'border-border-subtle bg-surface-sunken text-text-tertiary'"
            :title="deepRetrieval ? '深度检索：已开启（知识库检索更准，响应稍慢）' : '深度检索：已关闭（开启后知识库检索更准，响应稍慢）'"
          >
            <svg class="h-4 w-4 flex-shrink-0 sm:h-3.5 sm:w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 7V5a2 2 0 0 1 2-2h2"/>
              <path d="M17 3h2a2 2 0 0 1 2 2v2"/>
              <path d="M21 17v2a2 2 0 0 1-2 2h-2"/>
              <path d="M7 21H5a2 2 0 0 1-2-2v-2"/>
              <circle cx="12" cy="12" r="3"/>
              <path d="m16 16-1.9-1.9"/>
            </svg>
            <span class="hidden font-medium @cozy/main:inline">深检</span>
            <span class="hidden font-medium @cozy/main:inline">{{ deepRetrieval ? '开' : '关' }}</span>
            <span
              v-if="deepRetrieval"
              class="absolute right-0.5 top-0.5 h-2 w-2 rounded-full bg-accent ring-2 ring-surface @cozy/main:hidden"
            />
          </button>

          <div v-if="showSkillSelector" class="relative flex-shrink-0">
            <button
              type="button"
              :disabled="isBusy"
              @click.stop="toggleSkillMenu"
              class="relative inline-flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border transition-colors disabled:opacity-50 sm:w-auto sm:gap-1.5 sm:px-3 sm:text-[12px]"
              :class="selectedSkills.length > 0 ? 'border-accent/30 bg-accent-soft text-accent'
                : 'border-border-subtle bg-surface-sunken text-text-tertiary'"
              title="技能"
            >
              <svg class="h-4 w-4 flex-shrink-0 sm:h-3.5 sm:w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                <path d="M2 17l10 5 10-5"/>
                <path d="M2 12l10 5 10-5"/>
              </svg>
              <span class="hidden font-medium @cozy/main:inline">技能</span>
              <span v-if="selectedSkills.length > 0" class="hidden font-medium @cozy/main:inline">{{ selectedSkills.length }}</span>
              <span
                v-if="selectedSkills.length > 0"
                class="pointer-events-none absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-0.5 text-[9px] font-bold text-white @cozy/main:hidden"
              >{{ selectedSkills.length }}</span>
            </button>
          </div>

          <Teleport to="body">
            <Transition name="skill-modal">
              <div
                v-if="skillMenuOpen"
                class="fixed inset-0 z-[100] flex items-end justify-center p-0 sm:items-center sm:p-4"
                role="dialog"
                aria-modal="true"
                aria-labelledby="skill-picker-title"
              >
                <div
                  class="skill-modal-backdrop absolute inset-0 bg-slate-950/18 backdrop-blur-md dark:bg-black/45"
                  aria-hidden="true"
                  @click="closeSkillMenu"
                />
                <div
                  ref="skillMenuPanelRef"
                  class="skill-modal-panel relative flex w-full max-w-[42rem] flex-col overflow-hidden rounded-t-[1.75rem] border border-white/55 bg-surface/82 shadow-[0_24px_80px_rgba(15,23,42,0.20),0_3px_16px_rgba(15,23,42,0.08)] backdrop-blur-2xl dark:border-white/10 dark:bg-surface/88 sm:max-h-[min(80vh,600px)] sm:rounded-[1.75rem]"
                  style="max-height: min(85vh, 600px)"
                  @click.stop
                >
                  <div class="flex items-center justify-between border-b border-border-subtle/80 bg-surface/38 px-5 py-4 sm:px-6">
                    <div>
                      <h3 id="skill-picker-title" class="text-base font-bold tracking-tight text-text-primary">选择技能</h3>
                      <p class="mt-1 text-xs text-text-tertiary">
                        共 {{ availableSkills.length }} 个，已选 {{ selectedSkills.length }} 个
                      </p>
                    </div>
                    <button
                      type="button"
                      class="rounded-xl p-2 text-text-tertiary transition-colors hover:bg-surface/75 hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
                      aria-label="关闭技能选择"
                      @click="closeSkillMenu"
                    >
                      <X class="h-5 w-5" :stroke-width="2" />
                    </button>
                  </div>

                  <div class="border-b border-border-subtle/80 bg-surface/28 px-5 py-3.5 sm:px-6">
                    <div
                      v-if="agentInfo?.skill_catalog_status === 'degraded'"
                      class="mb-3 rounded-xl border border-amber-300/60 bg-amber-50/85 px-3.5 py-2.5 text-xs leading-relaxed text-amber-800 dark:border-amber-700/60 dark:bg-amber-950/35 dark:text-amber-200"
                      role="status"
                    >
                      {{ agentInfo?.skill_catalog_error?.message || '托管技能目录暂时不可用，当前展示内置配置。' }}
                    </div>
                    <div class="relative">
                      <Search class="pointer-events-none absolute left-3.5 top-1/2 h-[1.125rem] w-[1.125rem] -translate-y-1/2 text-text-tertiary" :stroke-width="2" />
                      <input
                        ref="skillSearchInputRef"
                        v-model="skillSearchQuery"
                        type="search"
                        placeholder="搜索技能名称或描述..."
                        class="h-11 w-full rounded-xl border border-border-subtle/90 bg-surface/55 pl-10 pr-4 text-sm text-text-secondary outline-none transition-all placeholder:text-text-tertiary focus:border-accent/60 focus:bg-surface/78 focus:ring-4 focus:ring-accent/10"
                      />
                    </div>
                  </div>

                  <div class="custom-scrollbar min-h-0 flex-1 overflow-y-auto bg-surface/24 py-2">
                    <button
                      v-for="skill in filteredSkills"
                      :key="skill.id"
                      type="button"
                      :disabled="isSkillDisabled(skill)"
                      class="group flex w-full items-center justify-between gap-5 px-5 py-3 text-left transition-colors hover:bg-surface/58 focus-visible:bg-surface/58 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 sm:px-6"
                      @click="toggleSkill(skill.id)"
                    >
                      <span class="min-w-0">
                        <span class="block text-sm font-medium text-text-secondary">
                          {{ skill.name }}
                          <span v-if="skill.required" class="ml-1 text-[11px] font-normal text-accent">必选</span>
                        </span>
                        <span
                          v-if="skill.description"
                          class="mt-1 block text-xs leading-relaxed text-text-tertiary line-clamp-3"
                        >
                          {{ skill.description }}
                        </span>
                      </span>
                      <span
                        class="relative inline-flex h-6 w-11 shrink-0 rounded-full border border-transparent shadow-inner transition-colors duration-200 ease-out"
                        :class="selectedSkills.includes(skill.id) ? 'bg-accent' : 'bg-slate-300/80 dark:bg-slate-600/80'"
                        aria-hidden="true"
                      >
                        <span
                          class="pointer-events-none m-0.5 inline-block h-5 w-5 transform rounded-full bg-white shadow-sm ring-0 transition duration-200 ease-out"
                          :class="selectedSkills.includes(skill.id) ? 'translate-x-5' : 'translate-x-0'"
                        />
                      </span>
                    </button>
                    <div
                      v-if="filteredSkills.length === 0"
                      class="px-4 py-10 text-center text-sm text-text-tertiary"
                    >
                      没有匹配的技能
                    </div>
                  </div>

                  <div class="flex items-center justify-between border-t border-border-subtle/80 bg-surface/52 px-5 py-4 backdrop-blur-2xl sm:px-6">
                    <button
                      v-if="selectedSkills.length > 0"
                      type="button"
                      class="rounded-lg px-1 py-1 text-sm font-semibold text-text-secondary transition-colors hover:text-rose-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                      @click="clearSelectedSkills"
                    >
                      清空已选
                    </button>
                    <span v-else class="text-sm text-text-tertiary">可多选技能</span>
                    <button
                      type="button"
                      class="rounded-xl bg-accent px-5 py-2.5 text-sm font-semibold text-white shadow-[0_8px_20px_rgba(124,58,237,0.28)] transition-all hover:-translate-y-0.5 hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-accent/25 active:translate-y-0"
                      @click="closeSkillMenu"
                    >
                      完成
                    </button>
                  </div>
                </div>
              </div>
            </Transition>
          </Teleport>

          <div v-if="showKbSelector" class="relative flex-shrink-0">
            <button
              type="button"
              :disabled="isBusy"
              @click.stop="toggleKbMenu"
              class="relative inline-flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border transition-colors disabled:opacity-50 sm:w-auto sm:gap-1.5 sm:px-3 sm:text-[12px]"
              :class="selectedKbs.length > 0 ? 'border-accent/30 bg-accent-soft text-accent'
                : 'border-border-subtle bg-surface-sunken text-text-tertiary'"
              title="知识库"
            >
              <svg class="h-4 w-4 flex-shrink-0 sm:h-3.5 sm:w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                <path d="M20 17V5a2 2 0 0 0-2-2H6.5A2.5 2.5 0 0 0 4 5.5v13A2.5 2.5 0 0 0 6.5 21H20"/>
              </svg>
              <span class="hidden font-medium @cozy/main:inline">知识库</span>
              <span v-if="selectedKbs.length > 0" class="hidden font-medium @cozy/main:inline">{{ selectedKbs.length }}</span>
              <span
                v-if="selectedKbs.length > 0"
                class="pointer-events-none absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-0.5 text-[9px] font-bold text-white @cozy/main:hidden"
              >{{ selectedKbs.length }}</span>
            </button>
            <Teleport to="body">
              <Transition name="skill-modal">
                <div v-if="kbMenuOpen" class="fixed inset-0 z-[100] flex items-end justify-center p-0 sm:items-center sm:p-4" role="dialog" aria-modal="true" aria-labelledby="kb-picker-title">
                  <div class="skill-modal-backdrop absolute inset-0 bg-slate-950/18 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="kbMenuOpen = false" />
                  <div ref="kbMenuPanelRef" class="skill-modal-panel relative flex w-full max-w-[38rem] flex-col overflow-hidden rounded-t-[1.75rem] border border-white/55 bg-surface/82 shadow-[0_24px_80px_rgba(15,23,42,0.20),0_3px_16px_rgba(15,23,42,0.08)] backdrop-blur-2xl dark:border-white/10 dark:bg-surface/88 sm:max-h-[min(80vh,560px)] sm:rounded-[1.75rem]" @click.stop>
                    <div class="flex items-center justify-between border-b border-border-subtle/80 bg-surface/38 px-5 py-4 sm:px-6">
                      <div>
                        <h3 id="kb-picker-title" class="text-base font-bold tracking-tight text-text-primary">选择知识库</h3>
                        <p class="mt-1 text-xs text-text-tertiary">共 {{ availableKbs.length }} 个，已选 {{ selectedKbs.length }} 个</p>
                      </div>
                      <button type="button" class="rounded-xl p-2 text-text-tertiary transition-colors hover:bg-surface/75 hover:text-text-primary" aria-label="关闭知识库选择" @click="kbMenuOpen = false"><X class="h-5 w-5" :stroke-width="2" /></button>
                    </div>
                    <div class="custom-scrollbar min-h-0 flex-1 overflow-y-auto bg-surface/24 py-2">
                      <button v-for="kb in availableKbs" :key="kb.id" type="button" @click="toggleKb(kb.id)" class="group flex w-full items-center justify-between gap-4 px-5 py-3.5 text-left transition-colors hover:bg-surface/58 sm:px-6">
                        <span class="text-sm font-medium text-text-secondary">{{ kb.name }}</span>
                        <span class="relative inline-flex h-6 w-11 shrink-0 rounded-full border border-transparent shadow-inner transition-colors duration-200 ease-out" :class="selectedKbs.includes(kb.id) ? 'bg-accent' : 'bg-slate-300/80 dark:bg-slate-600/80'" aria-hidden="true">
                          <span class="pointer-events-none m-0.5 inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition duration-200 ease-out" :class="selectedKbs.includes(kb.id) ? 'translate-x-5' : 'translate-x-0'" />
                        </span>
                      </button>
                      <div v-if="availableKbs.length === 0" class="px-5 py-12 text-center text-sm text-text-tertiary">暂无可用知识库</div>
                    </div>
                    <div class="flex justify-end border-t border-border-subtle/80 bg-surface/52 px-5 py-4 backdrop-blur-2xl sm:px-6">
                      <button type="button" class="rounded-xl bg-accent px-5 py-2.5 text-sm font-semibold text-white shadow-[0_8px_20px_rgba(124,58,237,0.28)] transition-all hover:-translate-y-0.5 hover:bg-accent-hover" @click="kbMenuOpen = false">完成</button>
                    </div>
                  </div>
                </div>
              </Transition>
            </Teleport>
          </div>
        </div>

        <div class="flex flex-shrink-0 items-center gap-1 md:gap-2">
          <div class="relative flex-shrink-0">
            <button
              type="button"
              :disabled="isBusy || !agentInfo?.available_models?.length"
              @click.stop="toggleModelMenu"
              class="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-full border transition-colors disabled:opacity-50 sm:w-auto sm:gap-1.5 sm:px-3 sm:text-[12px]"
              :class="modelOpen ? 'border-accent/30 bg-accent-soft text-accent'
                : 'border-border-subtle bg-surface-sunken text-text-secondary hover:bg-surface'"
              :title="`模型：${modelLabel}`"
              :aria-label="`选择模型，当前：${modelLabel}`"
              :aria-expanded="modelOpen"
            >
              <Bot class="h-4 w-4 flex-shrink-0 sm:h-3.5 sm:w-3.5" />
              <span class="hidden max-w-40 truncate font-medium @cozy/main:inline">{{ modelLabel }}</span>
              <ChevronUp class="hidden size-3.5 @cozy/main:block" />
            </button>
            <Teleport to="body">
              <Transition name="skill-modal">
                <div v-if="modelOpen" class="fixed inset-0 z-[100] flex items-end justify-center p-0 sm:items-center sm:p-4" role="dialog" aria-modal="true" aria-labelledby="model-picker-title">
                  <div class="skill-modal-backdrop absolute inset-0 bg-slate-950/18 backdrop-blur-md dark:bg-black/45" aria-hidden="true" @click="modelOpen = false" />
                  <div ref="modelMenuPanelRef" class="skill-modal-panel relative flex w-full max-w-[36rem] flex-col overflow-hidden rounded-t-[1.75rem] border border-white/55 bg-surface/82 shadow-[0_24px_80px_rgba(15,23,42,0.20),0_3px_16px_rgba(15,23,42,0.08)] backdrop-blur-2xl dark:border-white/10 dark:bg-surface/88 sm:max-h-[min(75vh,520px)] sm:rounded-[1.75rem]" @click.stop>
                    <div class="flex items-center justify-between border-b border-border-subtle/80 bg-surface/38 px-5 py-4 sm:px-6">
                      <div>
                        <h3 id="model-picker-title" class="text-base font-bold tracking-tight text-text-primary">选择模型</h3>
                        <p class="mt-1 text-xs text-text-tertiary">当前：{{ modelLabel }}</p>
                      </div>
                      <button type="button" class="rounded-xl p-2 text-text-tertiary transition-colors hover:bg-surface/75 hover:text-text-primary" aria-label="关闭模型选择" @click="modelOpen = false"><X class="h-5 w-5" :stroke-width="2" /></button>
                    </div>
                    <div class="custom-scrollbar min-h-0 flex-1 overflow-y-auto bg-surface/24 p-2">
                      <button v-for="model in (agentInfo?.available_models || [])" :key="model.name" type="button" class="flex w-full items-center justify-between gap-4 rounded-xl px-4 py-3.5 text-left text-sm transition-colors" :class="(selectedModelLocal || agentInfo?.available_models?.[0]?.name) === model.name ? 'bg-accent-soft font-semibold text-accent' : 'text-text-secondary hover:bg-surface/58'" @click="selectModel(model.name)">
                        <span class="min-w-0 truncate">{{ model.display_name || model.name }}</span>
                        <span v-if="(selectedModelLocal || agentInfo?.available_models?.[0]?.name) === model.name" class="shrink-0 rounded-full bg-accent/10 px-2 py-1 text-[11px] font-semibold text-accent">当前</span>
                      </button>
                    </div>
                  </div>
                </div>
              </Transition>
            </Teleport>
          </div>

          <!-- 发送/停止按钮：同位叠放交叉切换，避免布局跳动 -->
          <div class="relative size-9 shrink-0">
            <Transition name="btn-swap">
              <button
                v-if="isBusy"
                key="stop"
                @click="emit('abort')"
                title="停止生成"
                class="absolute inset-0 flex items-center justify-center rounded-full bg-rose-500 text-white transition-colors hover:bg-rose-600 active:scale-95"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                  <rect x="5" y="5" width="14" height="14" rx="2"/>
                </svg>
              </button>
              <button
                v-else
                key="send"
                @click="submit"
                :disabled="!question.trim()"
                class="absolute inset-0 flex items-center justify-center rounded-full bg-accent text-accent-fg transition-colors hover:bg-accent-hover enabled:active:scale-95 disabled:bg-border-strong disabled:text-text-tertiary"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>
                </svg>
              </button>
            </Transition>
          </div>
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
      <div class="pointer-events-auto w-full max-w-2xl bg-surface rounded-2xl shadow-sm border border-border-subtle overflow-hidden">
        <!-- 面板头部 -->
        <div class="flex items-center justify-between px-4 py-2.5 bg-surface-sunken border-b border-border-subtle">
          <div class="flex items-center gap-2 text-[12px] font-semibold text-text-secondary">
            <svg class="h-3.5 w-3.5 text-accent" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              <line x1="12" y1="19" x2="12" y2="22"/>
            </svg>
            语音识别结果（可修改）
          </div>
          <button
            type="button"
            @click="cancelEdit"
            class="text-text-tertiary hover:text-text-secondary transition-colors p-1 rounded-lg hover:bg-surface-hover"
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
          class="w-full bg-transparent resize-none border-0 focus:outline-none focus:ring-0 px-4 py-3 text-sm text-text-primary placeholder:text-text-tertiary"
          placeholder="识别结果将显示在这里，可直接修改…"
          @keydown.ctrl.enter.prevent="confirmVoiceText"
        ></textarea>

        <!-- 操作栏 -->
        <div class="flex items-center justify-between px-4 py-2.5 border-t border-border-subtle bg-surface-sunken">
          <p class="text-[11px] text-text-tertiary">Ctrl+Enter 插入 · Esc 取消</p>
          <div class="flex items-center gap-2">
            <button
              type="button"
              @click="cancelEdit"
              class="px-3 h-7 rounded-lg text-[12px] font-medium text-text-secondary hover:bg-surface-hover transition-colors"
            >取消</button>
            <button
              type="button"
              @click="confirmVoiceText"
              :disabled="!pendingText.trim()"
              class="px-4 h-7 rounded-lg text-[12px] font-semibold bg-accent text-white hover:bg-accent-hover disabled:opacity-40 transition-colors shadow-sm"
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

/* 选择弹窗：遮罩保持透视，面板独立缩放，避免打开时出现整页空白。 */
.skill-modal-enter-active,
.skill-modal-leave-active {
  transition: opacity 0.22s ease;
}
.skill-modal-enter-active .skill-modal-backdrop,
.skill-modal-leave-active .skill-modal-backdrop {
  transition: opacity 0.22s ease, backdrop-filter 0.22s ease;
}
.skill-modal-enter-active .skill-modal-panel {
  transition: transform 0.28s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.2s ease;
}
.skill-modal-leave-active .skill-modal-panel {
  transition: transform 0.18s ease, opacity 0.15s ease;
}
.skill-modal-enter-from,
.skill-modal-leave-to,
.skill-modal-enter-from .skill-modal-backdrop,
.skill-modal-leave-to .skill-modal-backdrop {
  opacity: 0;
}
.skill-modal-enter-from .skill-modal-panel,
.skill-modal-leave-to .skill-modal-panel {
  opacity: 0;
  transform: translateY(16px) scale(0.97);
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

/* 发送↔停止按钮交叉切换（两者均绝对定位于同一格） */
.btn-swap-enter-active {
  transition: transform 0.22s cubic-bezier(0.34, 1.56, 0.64, 1), opacity 0.15s ease;
}
.btn-swap-leave-active {
  transition: transform 0.15s ease, opacity 0.12s ease;
}
.btn-swap-enter-from,
.btn-swap-leave-to {
  opacity: 0;
  transform: scale(0.5) rotate(-90deg);
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
