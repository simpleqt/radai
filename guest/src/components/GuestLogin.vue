<script setup>
import { onUnmounted, ref } from 'vue';
import { AuthAPI } from '@/api/auth';
import { setAuth } from '@/utils/request';

defineProps({ embedded: { type: Boolean, default: false } });
const emit = defineEmits(['login-success']);

const loginMode = ref('password');
const username = ref('');
const password = ref('');
const telephone = ref('');
const registerUsername = ref('');
const smsCode = ref('');
const registerPassword = ref('');
const confirmPassword = ref('');
const captchaId = ref('');
const captchaImage = ref('');
const captchaAnswer = ref('');
const errorMsg = ref('');
const loading = ref(false);
const captchaLoading = ref(false);
const sendingCode = ref(false);
const countdown = ref(0);
let countdownTimer = null;

const phonePattern = /^1[3-9]\d{9}$/;

const clearTimer = () => {
  if (countdownTimer) {
    clearInterval(countdownTimer);
    countdownTimer = null;
  }
};

const startCountdown = (seconds = 60) => {
  clearTimer();
  countdown.value = seconds;
  countdownTimer = setInterval(() => {
    countdown.value -= 1;
    if (countdown.value <= 0) {
      countdown.value = 0;
      clearTimer();
    }
  }, 1000);
};

const switchMode = async (mode) => {
  loginMode.value = mode;
  errorMsg.value = '';
  if (mode === 'register' && !captchaImage.value) {
    await loadSmsCaptcha();
  }
};

const loadSmsCaptcha = async () => {
  captchaLoading.value = true;
  try {
    const res = await AuthAPI.getGuestSmsCaptcha();
    captchaId.value = res.captcha_id || '';
    captchaImage.value = res.image_data || '';
    captchaAnswer.value = '';
  } catch (err) {
    captchaId.value = '';
    captchaImage.value = '';
    errorMsg.value = err.message || '人机验证加载失败，请稍后再试';
  } finally {
    captchaLoading.value = false;
  }
};

const handleSendSmsCode = async () => {
  if (!phonePattern.test(telephone.value)) {
    errorMsg.value = '请输入正确的11位手机号';
    return;
  }
  if (!captchaId.value || !captchaAnswer.value.trim()) {
    errorMsg.value = '请先完成人机验证';
    return;
  }

  sendingCode.value = true;
  errorMsg.value = '';

  try {
    const res = await AuthAPI.sendGuestSmsCode({
      telephone: telephone.value,
      captcha_id: captchaId.value,
      captcha_answer: captchaAnswer.value.trim(),
    });
    if (res.success) {
      startCountdown(res.retry_after || 60);
    } else {
      errorMsg.value = res.error || '验证码发送失败';
      if (res.refresh_captcha) {
        await loadSmsCaptcha();
      }
    }
  } catch (err) {
    errorMsg.value = err.message || '网络错误，请稍后再试';
    await loadSmsCaptcha();
  } finally {
    sendingCode.value = false;
  }
};

const finishLogin = (res, fallbackMessage) => {
  if (res.success && res.token) {
    setAuth(res.token, res.user);
    emit('login-success');
    return;
  }
  errorMsg.value = res.error || res.message || fallbackMessage;
};

const handleRegister = async () => {
  if (!/^[A-Za-z0-9_\u4e00-\u9fff]{3,20}$/.test(registerUsername.value.trim())) {
    errorMsg.value = '用户名需为3-20位中文、字母、数字或下划线';
    return;
  }
  if (!phonePattern.test(telephone.value)) {
    errorMsg.value = '请输入正确的11位手机号';
    return;
  }
  if (!smsCode.value.trim()) {
    errorMsg.value = '请输入短信验证码';
    return;
  }
  if (registerPassword.value.length < 6) {
    errorMsg.value = '密码至少6位';
    return;
  }
  if (registerPassword.value !== confirmPassword.value) {
    errorMsg.value = '两次输入的密码不一致';
    return;
  }

  loading.value = true;
  errorMsg.value = '';

  try {
    const res = await AuthAPI.guestSmsRegister({
      telephone: telephone.value,
      username: registerUsername.value.trim(),
      code: smsCode.value.trim(),
      password: registerPassword.value,
      confirm_password: confirmPassword.value,
    });
    finishLogin(res, '注册失败');
  } catch (err) {
    errorMsg.value = err.message || '网络错误，请稍后再试';
  } finally {
    loading.value = false;
  }
};

const handleLogin = async () => {
  if (!username.value || !password.value) {
    errorMsg.value = '请输入手机号/用户名和密码';
    return;
  }

  loading.value = true;
  errorMsg.value = '';

  try {
    const res = await AuthAPI.login(username.value.trim(), password.value);
    finishLogin(res, '登录失败，请检查账号或密码');
  } catch (err) {
    errorMsg.value = err.message || '网络错误，请稍后再试';
  } finally {
    loading.value = false;
  }
};

onUnmounted(() => {
  clearTimer();
});
</script>

<template>
  <div :class="embedded ? 'w-full bg-transparent' : 'min-h-screen w-full bg-[#f8fafc] dark:bg-[#0b0f1a]'" class="flex items-center justify-center relative overflow-hidden">
    <div class="absolute inset-0 z-0 overflow-hidden pointer-events-none">
      <div class="absolute -top-[10%] -left-[10%] w-[40%] h-[40%] bg-indigo-500/10 rounded-full blur-[120px]"></div>
      <div class="absolute bottom-[10%] right-[10%] w-[30%] h-[30%] bg-emerald-500/10 rounded-full blur-[100px]"></div>
    </div>

    <div class="z-10 w-full max-w-md px-6" :class="embedded ? 'pb-8 pt-14' : 'py-10'">
      <div class="text-center mb-8">
        <div class="inline-flex items-center justify-center w-20 h-20 rounded-[28px] bg-white dark:bg-slate-900 shadow-2xl shadow-indigo-200/50 dark:shadow-none mb-6 border border-slate-100 dark:border-slate-800">
          <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="text-indigo-600 dark:text-indigo-400">
            <path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><circle cx="9" cy="15" r="1"/>
          </svg>
        </div>
        <h1 class="text-3xl font-black text-slate-900 dark:text-white tracking-tight mb-3">核仪AI</h1>
      </div>

      <div class="bg-white/80 dark:bg-slate-900/80 backdrop-blur-xl p-8 rounded-[32px] shadow-2xl shadow-slate-200/50 dark:shadow-none border border-white dark:border-slate-800">
        <div class="mb-6 grid grid-cols-2 rounded-2xl bg-slate-100 p-1 dark:bg-slate-800/70">
          <button
            type="button"
            @click="switchMode('password')"
            :class="[
              'rounded-xl px-2 py-3 text-sm font-bold transition-all',
              loginMode === 'password'
                ? 'bg-white text-indigo-600 shadow-sm dark:bg-slate-950 dark:text-indigo-300'
                : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-100'
            ]"
          >
            账号登录
          </button>
          <button
            type="button"
            @click="switchMode('register')"
            :class="[
              'rounded-xl px-2 py-3 text-sm font-bold transition-all',
              loginMode === 'register'
                ? 'bg-white text-indigo-600 shadow-sm dark:bg-slate-950 dark:text-indigo-300'
                : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-100'
            ]"
          >
            注册
          </button>
        </div>

        <form v-if="loginMode === 'password'" @submit.prevent="handleLogin" class="space-y-6">
          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">手机号/用户名</label>
            <input
              type="text"
              v-model="username"
              placeholder="请输入手机号或用户名"
              class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
              required
            />
          </div>

          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">密码</label>
            <input
              type="password"
              v-model="password"
              placeholder="请输入密码"
              class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
              required
            />
          </div>

          <button
            type="submit"
            :disabled="loading"
            class="w-full bg-indigo-600 dark:bg-indigo-500 text-white font-bold py-5 rounded-[20px] shadow-xl shadow-indigo-200 dark:shadow-none hover:bg-indigo-700 dark:hover:bg-indigo-600 transition-all duration-300 active:scale-[0.98] disabled:opacity-50 disabled:active:scale-100 flex items-center justify-center gap-3 mt-4"
          >
            <svg v-if="loading" class="animate-spin h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span class="tracking-widest">{{ loading ? '正在认证' : '登录' }}</span>
          </button>
        </form>

        <form v-else-if="loginMode === 'register'" @submit.prevent="handleRegister" class="space-y-5">
          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">用户名</label>
            <input type="text" v-model.trim="registerUsername" maxlength="20" autocomplete="username" placeholder="请输入用户名（注册后可用于登录）" class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800" required />
          </div>
          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">手机号</label>
            <input
              type="tel"
              inputmode="numeric"
              v-model="telephone"
              placeholder="请输入11位手机号"
              class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
              required
            />
          </div>

          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">人机验证</label>
            <div class="flex gap-2">
              <button
                type="button"
                @click="loadSmsCaptcha"
                class="flex h-14 flex-1 items-center justify-center overflow-hidden rounded-2xl border border-slate-200 bg-slate-50 transition-colors hover:border-indigo-200 dark:border-slate-800 dark:bg-slate-800/50"
              >
                <img v-if="captchaImage" :src="captchaImage" alt="人机验证" class="h-full w-full object-cover" />
                <span v-else class="text-xs font-semibold text-slate-400">{{ captchaLoading ? '加载中' : '点击刷新' }}</span>
              </button>
              <input
                type="text"
                v-model="captchaAnswer"
                placeholder="结果"
                class="w-24 bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-4 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
                autocomplete="off"
              />
            </div>
          </div>

          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">短信验证码</label>
            <div class="flex gap-2">
              <input
                type="text"
                inputmode="numeric"
                v-model="smsCode"
                placeholder="请输入验证码"
                class="min-w-0 flex-1 bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
                required
              />
              <button
                type="button"
                @click="handleSendSmsCode"
                :disabled="countdown > 0 || sendingCode || captchaLoading"
                class="shrink-0 rounded-2xl border border-indigo-100 bg-indigo-50 px-4 text-sm font-bold text-indigo-600 transition-colors hover:bg-indigo-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-indigo-500/20 dark:bg-indigo-500/10 dark:text-indigo-300"
              >
                {{ countdown > 0 ? `${countdown}s` : (sendingCode ? '发送中' : '获取') }}
              </button>
            </div>
          </div>

          <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">密码</label>
              <input
                type="password"
                v-model="registerPassword"
                placeholder="至少6位"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
                required
              />
            </div>
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">确认密码</label>
              <input
                type="password"
                v-model="confirmPassword"
                placeholder="再次输入"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
                required
              />
            </div>
          </div>

          <button
            type="submit"
            :disabled="loading"
            class="w-full bg-indigo-600 dark:bg-indigo-500 text-white font-bold py-5 rounded-[20px] shadow-xl shadow-indigo-200 dark:shadow-none hover:bg-indigo-700 dark:hover:bg-indigo-600 transition-all duration-300 active:scale-[0.98] disabled:opacity-50 disabled:active:scale-100 flex items-center justify-center gap-3 mt-4"
          >
            <svg v-if="loading" class="animate-spin h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span class="tracking-widest">{{ loading ? '正在注册' : '注册并登录' }}</span>
          </button>
        </form>

        <div v-if="errorMsg" class="mt-6 flex items-center gap-2 text-rose-500 text-sm font-medium bg-rose-50 dark:bg-rose-500/10 px-4 py-3 rounded-xl border border-rose-100 dark:border-rose-500/20 animate-in fade-in slide-in-from-top-2">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>
          {{ errorMsg }}
        </div>
      </div>
    </div>
  </div>
</template>
