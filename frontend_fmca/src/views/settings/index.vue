<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ArrowLeft,
  Check,
  Loader2,
  LogOut,
  MonitorSmartphone,
  Palette,
  ShieldCheck,
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
    brand-name="基础力学助手"
    brand-icon="Atom"
    @auth-action="logout()"
  >
    <main class="custom-scrollbar min-h-0 flex-1 overflow-y-auto px-4 py-5 @cozy/main:px-6 @wide/main:px-8">
      <div class="mx-auto flex max-w-5xl flex-col gap-5">
        <header class="flex flex-col gap-4 border-b border-border-subtle pb-5 sm:flex-row sm:items-center sm:justify-between">
          <div class="min-w-0">
            <button
              type="button"
              class="mb-4 inline-flex h-9 items-center gap-2 rounded-lg px-2 text-sm font-medium text-text-secondary transition-colors hover:bg-surface-hover hover:text-accent"
              @click="router.push('/app')"
            >
              <ArrowLeft class="h-4 w-4" />
              返回
            </button>
            <h1 class="text-2xl font-bold tracking-tight sm:text-3xl">用户设置</h1>
            <p class="mt-2 text-sm text-text-secondary">管理账号资料、安全信息和常用偏好。</p>
          </div>
          <div class="flex items-center gap-3 rounded-xl border border-border-subtle bg-surface px-4 py-3 shadow-sm">
            <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-accent-soft text-accent">
              <UserRound class="h-5 w-5" />
            </div>
            <div class="min-w-0">
              <p class="truncate text-sm font-bold">{{ accountName }}</p>
              <p class="truncate text-xs text-text-secondary">{{ user?.role || 'user' }}</p>
            </div>
          </div>
        </header>

        <section class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
          <div class="mb-5 flex items-center gap-3">
            <UserRound class="h-5 w-5 text-accent" />
            <h2 class="text-lg font-extrabold">账号信息</h2>
            <Loader2 v-if="profileLoading" class="h-4 w-4 animate-spin text-text-tertiary" />
          </div>

          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-1.5">
              <span class="text-xs font-bold text-text-secondary">用户名</span>
              <input :value="user?.username || ''" disabled class="h-11 w-full rounded-lg border border-border-subtle bg-surface-sunken px-3 text-sm text-text-secondary" />
            </label>
            <label class="space-y-1.5">
              <span class="text-xs font-bold text-text-secondary">显示名</span>
              <input v-model="displayName" maxlength="50" class="h-11 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/15" />
            </label>
            <label class="space-y-1.5">
              <span class="text-xs font-bold text-text-secondary">邮箱</span>
              <input v-model="email" class="h-11 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/15" placeholder="name@example.com" />
            </label>
            <div class="grid grid-cols-2 gap-3">
              <div class="rounded-lg bg-surface-sunken p-3">
                <p class="text-xs font-bold text-text-secondary">账号状态</p>
                <p class="mt-1 text-sm font-semibold">{{ user?.is_active ? '启用' : '停用' }}</p>
              </div>
              <div class="rounded-lg bg-surface-sunken p-3">
                <p class="text-xs font-bold text-text-secondary">登录来源</p>
                <p class="mt-1 text-sm font-semibold">{{ isPasswordAccount ? '密码登录' : '统一认证' }}</p>
              </div>
            </div>
            <div class="rounded-lg bg-surface-sunken p-3">
              <p class="text-xs font-bold text-text-secondary">创建时间</p>
              <p class="mt-1 text-sm font-semibold">{{ formatDate(user?.created_at) }}</p>
            </div>
            <div class="rounded-lg bg-surface-sunken p-3">
              <p class="text-xs font-bold text-text-secondary">最近登录</p>
              <p class="mt-1 text-sm font-semibold">{{ formatDate(user?.last_login) }}</p>
            </div>
          </div>

          <div class="mt-5 flex flex-wrap items-center gap-3">
            <button type="button" class="inline-flex h-10 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-bold text-white hover:bg-accent-hover disabled:opacity-60" :disabled="profileSaving" @click="saveProfile">
              <Loader2 v-if="profileSaving" class="h-4 w-4 animate-spin" />
              <Check v-else class="h-4 w-4" />
              保存资料
            </button>
            <span v-if="profileMessage" class="text-sm font-semibold text-emerald-600">{{ profileMessage }}</span>
            <span v-if="profileError" class="text-sm font-semibold text-rose-600">{{ profileError }}</span>
          </div>
        </section>

        <section class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
          <div class="mb-5 flex items-center gap-3">
            <ShieldCheck class="h-5 w-5 text-emerald-600" />
            <h2 class="text-lg font-extrabold">安全设置</h2>
          </div>

          <div v-if="isPasswordAccount" class="grid gap-4 md:grid-cols-3">
            <input v-model="currentPassword" type="password" class="h-11 rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/15" placeholder="当前密码" />
            <input v-model="newPassword" type="password" class="h-11 rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/15" placeholder="新密码" />
            <input v-model="confirmPassword" type="password" class="h-11 rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/15" placeholder="确认新密码" />
            <div class="md:col-span-3 flex flex-wrap items-center gap-3">
              <button type="button" class="inline-flex h-10 items-center gap-2 rounded-lg bg-emerald-600 px-4 text-sm font-bold text-white hover:bg-emerald-700 disabled:opacity-60" :disabled="passwordSaving" @click="changePassword">
                <Loader2 v-if="passwordSaving" class="h-4 w-4 animate-spin" />
                修改密码
              </button>
              <span v-if="passwordMessage" class="text-sm font-semibold text-emerald-600">{{ passwordMessage }}</span>
              <span v-if="passwordError" class="text-sm font-semibold text-rose-600">{{ passwordError }}</span>
            </div>
          </div>
          <p v-else class="rounded-lg bg-surface-sunken p-4 text-sm font-semibold text-text-secondary">当前账号由统一认证系统管理，请在统一认证系统中修改密码。</p>

          <button type="button" class="mt-5 inline-flex h-10 items-center gap-2 rounded-lg border border-border-subtle px-4 text-sm font-bold text-text-secondary hover:bg-surface-hover" @click="logout()">
            <LogOut class="h-4 w-4" />
            退出登录
          </button>
        </section>

        <section class="rounded-lg border border-border-subtle bg-surface p-5 shadow-sm">
          <div class="mb-5 flex items-center gap-3">
            <Palette class="h-5 w-5 text-amber-600" />
            <h2 class="text-lg font-extrabold">偏好设置</h2>
          </div>

          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-1.5">
              <span class="text-xs font-bold text-text-secondary">主题</span>
              <select :value="theme" class="h-11 w-full rounded-lg border border-border-subtle bg-surface px-3 text-sm outline-none" @change="setTheme(($event.target as HTMLSelectElement).value as ThemePreference)">
                <option value="system">跟随系统</option>
                <option value="light">浅色</option>
                <option value="dark">深色</option>
              </select>
            </label>
            <button
              type="button"
              role="switch"
              :aria-checked="learningStyleEnabled"
              class="flex w-full items-center justify-between gap-4 rounded-lg bg-surface-sunken p-3 text-left transition-all duration-200 hover:bg-surface-hover active:scale-[0.99] active:bg-surface-sunken disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100"
              :disabled="!learningStyleAvailable || learningStyleLoading || learningStyleSaving"
              @click="toggleLearningStyle"
            >
              <div class="min-w-0">
                <div class="flex items-center gap-2">
                  <MonitorSmartphone class="h-4 w-4 text-accent" />
                  <p class="text-sm font-bold">学习风格应用到 AI</p>
                </div>
                <p class="mt-1 text-xs text-text-secondary">{{ learningStyleAvailable ? '用于个性化回答与学习路径' : '完成学习风格测评后可启用' }}</p>
              </div>
              <span
                aria-hidden="true"
                class="relative h-6 w-11 shrink-0 rounded-full transition-colors duration-200 ease-in-out"
                :class="learningStyleEnabled ? 'bg-accent' : 'bg-surface-sunken'"
              >
                <span
                  class="absolute top-1 h-4 w-4 rounded-full bg-surface shadow-sm transition-transform duration-200 ease-in-out"
                  :class="learningStyleEnabled ? 'translate-x-6' : 'translate-x-1'"
                />
              </span>
            </button>
          </div>
          <p v-if="learningStyleError" class="mt-3 text-sm font-semibold text-rose-600">{{ learningStyleError }}</p>
        </section>
      </div>
    </main>
  </AppShell>
</template>
