<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ArrowLeft,
  AlertCircle,
  BrainCircuit,
  CalendarClock,
  Check,
  CheckCircle2,
  Clock3,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  LogOut,
  Monitor,
  Moon,
  Palette,
  ShieldCheck,
  Sun,
  UserRound,
} from 'lucide-vue-next';
import AppShell from '@/components/layout/AppShell.vue';
import { UserSettingsAPI, type CurrentUser } from '@/api/user-settings';
import { LearningStyleAPI } from '@/api/learning-style';
import { authState, logout, setAuth } from '@/utils/request';
import { useThemePreference, type ThemePreference } from '@/composables/useThemePreference';

const router = useRouter();
const { theme, setTheme } = useThemePreference();

const user = ref<CurrentUser | null>(authState.value.user || null);
const displayName = ref('');
const email = ref('');
const profileLoading = ref(false);
const profileSaving = ref(false);
const profileMessage = ref('');
const profileError = ref('');

const currentPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const showPasswords = ref(false);
const passwordSaving = ref(false);
const passwordError = ref('');
const passwordMessage = ref('');

const learningStyleEnabled = ref(false);
const learningStyleAvailable = ref(false);
const learningStyleLoading = ref(false);
const learningStyleSaving = ref(false);
const learningStyleError = ref('');

const isPasswordAccount = computed(() => (user.value?.auth_provider || 'password') === 'password');
const accountName = computed(() => user.value?.display_name || user.value?.username || '未命名用户');
const avatarInitial = computed(() => accountName.value.trim().charAt(0).toUpperCase() || 'U');
const roleLabel = computed(() => {
  const role = String(user.value?.role || 'user').toLowerCase();
  if (role === 'admin') return '管理员';
  if (role === 'user') return '普通用户';
  return user.value?.role || 'user';
});

const themeOptions: { value: ThemePreference; label: string; icon: typeof Monitor }[] = [
  { value: 'system', label: '跟随系统', icon: Monitor },
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: Moon },
];

const syncUserForm = (next: CurrentUser | null) => {
  user.value = next;
  displayName.value = next?.display_name || next?.username || '';
  email.value = next?.email || '';
};

const formatDate = (value?: string | null) => {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleString('zh-CN', { hour12: false });
};

const loadProfile = async () => {
  profileLoading.value = true;
  profileError.value = '';
  try {
    const response = await UserSettingsAPI.me();
    syncUserForm(response.user);
    setAuth(authState.value.token || '', response.user);
  } catch (err: any) {
    profileError.value = err.message || '用户资料加载失败';
  } finally {
    profileLoading.value = false;
  }
};

const saveProfile = async () => {
  profileSaving.value = true;
  profileError.value = '';
  profileMessage.value = '';
  const nextEmail = email.value.trim();
  if (nextEmail && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(nextEmail)) {
    profileError.value = '邮箱格式不正确';
    profileSaving.value = false;
    return;
  }

  try {
    const response = await UserSettingsAPI.updateMe({
      display_name: displayName.value.trim() || null,
      email: nextEmail || null,
    });
    syncUserForm(response.user);
    setAuth(authState.value.token || '', response.user);
    profileMessage.value = '资料已保存';
  } catch (err: any) {
    profileError.value = err.message || '资料保存失败';
  } finally {
    profileSaving.value = false;
  }
};

const changePassword = async () => {
  passwordError.value = '';
  passwordMessage.value = '';
  if (!currentPassword.value || !newPassword.value || !confirmPassword.value) {
    passwordError.value = '请完整填写密码信息';
    return;
  }
  if (newPassword.value.length < 6) {
    passwordError.value = '新密码至少6位';
    return;
  }
  if (newPassword.value !== confirmPassword.value) {
    passwordError.value = '两次输入的新密码不一致';
    return;
  }

  passwordSaving.value = true;
  try {
    await UserSettingsAPI.changePassword({
      current_password: currentPassword.value,
      new_password: newPassword.value,
    });
    passwordMessage.value = '密码已修改，请重新登录';
    window.setTimeout(() => logout({ redirectTo: '/login' }), 700);
  } catch (err: any) {
    passwordError.value = err.message || '密码修改失败';
  } finally {
    passwordSaving.value = false;
  }
};

const loadLearningStyle = async () => {
  learningStyleLoading.value = true;
  learningStyleError.value = '';
  try {
    const response = await LearningStyleAPI.profile();
    learningStyleAvailable.value = Boolean(response.profile);
    learningStyleEnabled.value = Boolean(response.profile?.apply_to_ai);
  } catch (err: any) {
    learningStyleError.value = err.message || '学习风格偏好加载失败';
  } finally {
    learningStyleLoading.value = false;
  }
};

const toggleLearningStyle = async () => {
  if (!learningStyleAvailable.value || learningStyleSaving.value) return;
  const previous = learningStyleEnabled.value;
  const next = !previous;
  learningStyleEnabled.value = next;
  learningStyleSaving.value = true;
  learningStyleError.value = '';
  try {
    const response = await LearningStyleAPI.updatePreferences(next);
    learningStyleEnabled.value = Boolean(response.profile.apply_to_ai);
  } catch (err: any) {
    learningStyleEnabled.value = previous;
    learningStyleError.value = err.message || '学习风格偏好保存失败';
  } finally {
    learningStyleSaving.value = false;
  }
};

onMounted(() => {
  void loadProfile();
  void loadLearningStyle();
});
</script>

<template>
  <AppShell
    title="用户设置"
    brand-name="核电子学智能助手"
    brand-icon="Atom"
    @auth-action="logout()"
  >
    <main class="custom-scrollbar min-h-0 flex-1 overflow-y-auto px-4 py-6 @cozy/main:px-6 @wide/main:px-8">
      <div class="mx-auto flex max-w-4xl flex-col gap-6 pb-10">
        <!-- 返回 + 标题 -->
        <div>
          <button
            type="button"
            class="inline-flex h-9 items-center gap-2 rounded-lg px-2 text-sm font-medium text-text-secondary transition-colors hover:bg-surface-hover hover:text-accent"
            @click="router.push('/app')"
          >
            <ArrowLeft class="h-4 w-4" />
            返回
          </button>
          <h1 class="mt-3 text-2xl font-bold tracking-tight sm:text-[28px]">用户设置</h1>
          <p class="mt-1.5 text-sm text-text-secondary">管理账号资料、安全信息和常用偏好。</p>
        </div>

        <!-- 个人信息 Hero 卡 -->
        <section class="relative overflow-hidden rounded-2xl border border-border-subtle bg-surface shadow-sm">
          <div class="pointer-events-none absolute inset-x-0 top-0 h-24 bg-gradient-to-r from-accent/15 via-fuchsia-400/10 to-sky-400/15"></div>
          <div class="pointer-events-none absolute -right-10 -top-14 h-40 w-40 rounded-full bg-accent/10 blur-3xl"></div>

          <div class="relative flex flex-col gap-5 p-6 sm:flex-row sm:items-end sm:justify-between">
            <div class="flex items-end gap-4">
              <div class="flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500 via-purple-500 to-indigo-500 text-2xl font-extrabold text-white shadow-lg shadow-violet-500/25 ring-4 ring-surface">
                {{ avatarInitial }}
              </div>
              <div class="min-w-0 pb-0.5">
                <div class="flex flex-wrap items-center gap-2">
                  <h2 class="truncate text-lg font-extrabold">{{ accountName }}</h2>
                  <span class="rounded-full bg-accent-soft px-2.5 py-0.5 text-[11px] font-bold text-accent">{{ roleLabel }}</span>
                  <span
                    class="rounded-full px-2.5 py-0.5 text-[11px] font-bold"
                    :class="user?.is_active
                      ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-950/40 dark:text-emerald-400'
                      : 'bg-rose-50 text-rose-600 dark:bg-rose-950/40 dark:text-rose-400'"
                  >{{ user?.is_active ? '已启用' : '已停用' }}</span>
                </div>
                <p class="mt-1 truncate text-xs text-text-secondary">
                  @{{ user?.username || '-' }} · {{ isPasswordAccount ? '密码登录' : '统一认证登录' }}
                </p>
              </div>
            </div>

            <div class="flex shrink-0 flex-wrap gap-4 text-xs text-text-secondary sm:flex-col sm:gap-1.5 sm:text-right">
              <span class="inline-flex items-center gap-1.5 sm:justify-end">
                <CalendarClock class="h-3.5 w-3.5 text-text-tertiary" />
                创建于 {{ formatDate(user?.created_at) }}
              </span>
              <span class="inline-flex items-center gap-1.5 sm:justify-end">
                <Clock3 class="h-3.5 w-3.5 text-text-tertiary" />
                最近登录 {{ formatDate(user?.last_login) }}
              </span>
            </div>
          </div>
        </section>

        <!-- 账号信息 -->
        <section class="overflow-hidden rounded-2xl border border-border-subtle bg-surface shadow-sm">
          <header class="flex items-center gap-3 border-b border-border-subtle bg-surface-sunken/60 px-5 py-4">
            <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-accent-soft text-accent">
              <UserRound class="h-4.5 w-4.5" />
            </div>
            <div class="min-w-0">
              <h2 class="text-sm font-extrabold">账号信息</h2>
              <p class="mt-0.5 text-xs text-text-secondary">更新显示名与联系方式</p>
            </div>
            <Loader2 v-if="profileLoading" class="ml-auto h-4 w-4 animate-spin text-text-tertiary" />
          </header>

          <div class="p-5">
            <div class="grid gap-4 md:grid-cols-2">
              <label class="space-y-1.5">
                <span class="text-xs font-bold text-text-secondary">用户名</span>
                <input
                  :value="user?.username || ''"
                  disabled
                  class="h-11 w-full cursor-not-allowed rounded-xl border border-border-subtle bg-surface-sunken px-3.5 text-sm text-text-tertiary"
                />
              </label>
              <label class="space-y-1.5">
                <span class="text-xs font-bold text-text-secondary">显示名</span>
                <input
                  v-model="displayName"
                  maxlength="50"
                  placeholder="输入展示给他人的名称"
                  class="h-11 w-full rounded-xl border border-border-subtle bg-surface px-3.5 text-sm outline-none transition-all focus:border-accent focus:ring-2 focus:ring-accent/15"
                />
              </label>
              <label class="space-y-1.5 md:col-span-2">
                <span class="text-xs font-bold text-text-secondary">邮箱</span>
                <input
                  v-model="email"
                  placeholder="name@example.com"
                  class="h-11 w-full rounded-xl border border-border-subtle bg-surface px-3.5 text-sm outline-none transition-all focus:border-accent focus:ring-2 focus:ring-accent/15"
                />
              </label>
            </div>

            <div class="mt-5 flex flex-wrap items-center gap-3">
              <button
                type="button"
                class="inline-flex h-10 items-center gap-2 rounded-xl bg-accent px-5 text-sm font-bold text-white shadow-sm shadow-accent/25 transition-all hover:bg-accent-hover hover:shadow-md hover:shadow-accent/30 active:scale-[0.98] disabled:opacity-60"
                :disabled="profileSaving"
                @click="saveProfile"
              >
                <Loader2 v-if="profileSaving" class="h-4 w-4 animate-spin" />
                <Check v-else class="h-4 w-4" />
                保存资料
              </button>
              <Transition name="fade">
                <span v-if="profileMessage" class="inline-flex items-center gap-1.5 text-sm font-semibold text-emerald-600 dark:text-emerald-400">
                  <CheckCircle2 class="h-4 w-4" />{{ profileMessage }}
                </span>
              </Transition>
              <Transition name="fade">
                <span v-if="profileError" class="inline-flex items-center gap-1.5 text-sm font-semibold text-rose-600 dark:text-rose-400">
                  <AlertCircle class="h-4 w-4" />{{ profileError }}
                </span>
              </Transition>
            </div>
          </div>
        </section>

        <!-- 安全设置 -->
        <section class="overflow-hidden rounded-2xl border border-border-subtle bg-surface shadow-sm">
          <header class="flex items-center gap-3 border-b border-border-subtle bg-surface-sunken/60 px-5 py-4">
            <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600 dark:bg-emerald-950/40 dark:text-emerald-400">
              <ShieldCheck class="h-4.5 w-4.5" />
            </div>
            <div class="min-w-0">
              <h2 class="text-sm font-extrabold">安全设置</h2>
              <p class="mt-0.5 text-xs text-text-secondary">定期更换密码以保护账号安全</p>
            </div>
          </header>

          <div class="p-5">
            <div v-if="isPasswordAccount">
              <div class="grid gap-4 md:grid-cols-3">
                <label class="space-y-1.5">
                  <span class="text-xs font-bold text-text-secondary">当前密码</span>
                  <div class="relative">
                    <KeyRound class="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
                    <input
                      v-model="currentPassword"
                      :type="showPasswords ? 'text' : 'password'"
                      autocomplete="current-password"
                      placeholder="当前密码"
                      class="h-11 w-full rounded-xl border border-border-subtle bg-surface pl-10 pr-3.5 text-sm outline-none transition-all focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/15"
                    />
                  </div>
                </label>
                <label class="space-y-1.5">
                  <span class="text-xs font-bold text-text-secondary">新密码</span>
                  <div class="relative">
                    <KeyRound class="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
                    <input
                      v-model="newPassword"
                      :type="showPasswords ? 'text' : 'password'"
                      autocomplete="new-password"
                      placeholder="至少 6 位"
                      class="h-11 w-full rounded-xl border border-border-subtle bg-surface pl-10 pr-3.5 text-sm outline-none transition-all focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/15"
                    />
                  </div>
                </label>
                <label class="space-y-1.5">
                  <span class="text-xs font-bold text-text-secondary">确认新密码</span>
                  <div class="relative">
                    <KeyRound class="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
                    <input
                      v-model="confirmPassword"
                      :type="showPasswords ? 'text' : 'password'"
                      autocomplete="new-password"
                      placeholder="再次输入新密码"
                      class="h-11 w-full rounded-xl border border-border-subtle bg-surface pl-10 pr-3.5 text-sm outline-none transition-all focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/15"
                    />
                  </div>
                </label>
              </div>

              <div class="mt-5 flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  class="inline-flex h-10 items-center gap-2 rounded-xl bg-emerald-600 px-5 text-sm font-bold text-white shadow-sm shadow-emerald-600/25 transition-all hover:bg-emerald-700 hover:shadow-md active:scale-[0.98] disabled:opacity-60"
                  :disabled="passwordSaving"
                  @click="changePassword"
                >
                  <Loader2 v-if="passwordSaving" class="h-4 w-4 animate-spin" />
                  <ShieldCheck v-else class="h-4 w-4" />
                  修改密码
                </button>
                <button
                  type="button"
                  class="inline-flex h-10 items-center gap-1.5 rounded-xl px-3 text-xs font-semibold text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary"
                  @click="showPasswords = !showPasswords"
                >
                  <EyeOff v-if="showPasswords" class="h-4 w-4" />
                  <Eye v-else class="h-4 w-4" />
                  {{ showPasswords ? '隐藏密码' : '显示密码' }}
                </button>
                <Transition name="fade">
                  <span v-if="passwordMessage" class="inline-flex items-center gap-1.5 text-sm font-semibold text-emerald-600 dark:text-emerald-400">
                    <CheckCircle2 class="h-4 w-4" />{{ passwordMessage }}
                  </span>
                </Transition>
                <Transition name="fade">
                  <span v-if="passwordError" class="inline-flex items-center gap-1.5 text-sm font-semibold text-rose-600 dark:text-rose-400">
                    <AlertCircle class="h-4 w-4" />{{ passwordError }}
                  </span>
                </Transition>
              </div>
            </div>
            <p v-else class="rounded-xl bg-surface-sunken p-4 text-sm font-semibold text-text-secondary">
              当前账号由统一认证系统管理，请在统一认证系统中修改密码。
            </p>

            <div class="mt-5 border-t border-border-subtle pt-5">
              <button
                type="button"
                class="inline-flex h-10 items-center gap-2 rounded-xl border border-border-subtle px-4 text-sm font-bold text-text-secondary transition-all hover:border-rose-200 hover:bg-rose-50 hover:text-rose-600 dark:hover:border-rose-900/40 dark:hover:bg-rose-950/30 dark:hover:text-rose-400"
                @click="logout()"
              >
                <LogOut class="h-4 w-4" />
                退出登录
              </button>
            </div>
          </div>
        </section>

        <!-- 偏好设置 -->
        <section class="overflow-hidden rounded-2xl border border-border-subtle bg-surface shadow-sm">
          <header class="flex items-center gap-3 border-b border-border-subtle bg-surface-sunken/60 px-5 py-4">
            <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-amber-50 text-amber-600 dark:bg-amber-950/40 dark:text-amber-400">
              <Palette class="h-4.5 w-4.5" />
            </div>
            <div class="min-w-0">
              <h2 class="text-sm font-extrabold">偏好设置</h2>
              <p class="mt-0.5 text-xs text-text-secondary">外观主题与 AI 个性化</p>
            </div>
          </header>

          <div class="grid gap-5 p-5 md:grid-cols-2">
            <div class="space-y-2">
              <span class="text-xs font-bold text-text-secondary">界面主题</span>
              <div class="grid grid-cols-3 gap-1 rounded-xl border border-border-subtle bg-surface-sunken p-1">
                <button
                  v-for="option in themeOptions"
                  :key="option.value"
                  type="button"
                  class="flex items-center justify-center gap-1.5 rounded-lg px-2 py-2 text-xs transition-all"
                  :class="theme === option.value
                    ? 'bg-surface font-bold text-accent shadow-sm ring-1 ring-border-strong/60'
                    : 'font-medium text-text-secondary hover:text-text-primary'"
                  @click="setTheme(option.value)"
                >
                  <component :is="option.icon" class="h-3.5 w-3.5" />
                  {{ option.label }}
                </button>
              </div>
              <p class="text-xs text-text-tertiary">跟随系统会依据设备外观自动切换。</p>
            </div>

            <div class="space-y-2">
              <span class="text-xs font-bold text-text-secondary">AI 个性化</span>
              <button
                type="button"
                role="switch"
                :aria-checked="learningStyleEnabled"
                class="flex w-full items-center justify-between gap-4 rounded-xl border border-border-subtle bg-surface-sunken p-3.5 text-left transition-all duration-200 hover:border-accent/30 hover:bg-surface-hover active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100"
                :disabled="!learningStyleAvailable || learningStyleLoading || learningStyleSaving"
                @click="toggleLearningStyle"
              >
                <div class="flex min-w-0 items-start gap-2.5">
                  <div class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent">
                    <BrainCircuit class="h-4 w-4" />
                  </div>
                  <div class="min-w-0">
                    <p class="text-sm font-bold">学习风格应用到 AI</p>
                    <p class="mt-0.5 text-xs text-text-secondary">
                      {{ learningStyleAvailable ? '用于个性化回答与学习路径' : '完成学习风格测评后可启用' }}
                    </p>
                  </div>
                </div>
                <span
                  aria-hidden="true"
                  class="relative h-6 w-11 shrink-0 rounded-full transition-colors duration-200 ease-in-out"
                  :class="learningStyleEnabled ? 'bg-accent' : 'bg-border-strong'"
                >
                  <span
                    class="absolute top-1 h-4 w-4 rounded-full bg-white shadow-sm transition-transform duration-200 ease-in-out"
                    :class="learningStyleEnabled ? 'translate-x-6' : 'translate-x-1'"
                  />
                </span>
              </button>
              <p v-if="learningStyleError" class="inline-flex items-center gap-1.5 text-xs font-semibold text-rose-600 dark:text-rose-400">
                <AlertCircle class="h-3.5 w-3.5" />{{ learningStyleError }}
              </p>
            </div>
          </div>
        </section>
      </div>
    </main>
  </AppShell>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
