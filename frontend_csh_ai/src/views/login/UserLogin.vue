<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { AuthAPI } from '@/api/auth';
import { setAuth } from '@/utils/request';

const router = useRouter();
const route = useRoute();
const username = ref('');
const password = ref('');
const confirmPassword = ref('');
const errorMsg = ref('');
const loading = ref(false);

const mode = ref<'password' | 'sms_login' | 'sms' | 'forgot'>('password');
const telephone = ref('');
const code = ref('');
const countdown = ref(0);
const captchaId = ref('');
const captchaImage = ref('');
const captchaAnswer = ref('');
const captchaLoading = ref(false);
let timer: any = null;

const handleOpenSmsLogin = () => {
  mode.value = 'sms_login';
  password.value = '';
  confirmPassword.value = '';
  code.value = '';
  errorMsg.value = '';
  void loadSmsCaptcha();
};

const handleOpenRegister = () => {
  mode.value = 'sms';
  password.value = '';
  confirmPassword.value = '';
  code.value = '';
  errorMsg.value = '';
  void loadSmsCaptcha();
};

const handleForgotPassword = () => {
  mode.value = 'forgot';
  password.value = '';
  confirmPassword.value = '';
  code.value = '';
  errorMsg.value = '';
  void loadSmsCaptcha();
};

const handleBackToLogin = () => {
  mode.value = 'password';
  password.value = '';
  confirmPassword.value = '';
  code.value = '';
  errorMsg.value = '';
};

const loadSmsCaptcha = async () => {
  captchaLoading.value = true;
  try {
    const response = await fetch('/api/sms-captcha', { cache: 'no-store' });
    const res = await response.json();
    captchaId.value = res.captcha_id || '';
    captchaImage.value = res.image_data || '';
    captchaAnswer.value = '';
  } catch (err: any) {
    captchaId.value = '';
    captchaImage.value = '';
    errorMsg.value = '人机验证加载失败，请稍后再试';
  } finally {
    captchaLoading.value = false;
  }
};

const handleSendCode = async (type: 'register' | 'forgot' | 'login' = 'register') => {
  if (!/^1[3-9]\d{9}$/.test(telephone.value)) {
    errorMsg.value = '请输入正确的11位手机号';
    return;
  }
  if (!captchaId.value || !captchaAnswer.value.trim()) {
    errorMsg.value = '请先完成人机验证';
    return;
  }
  
  errorMsg.value = '';
  try {
    const response = await fetch('/api/send-sms-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        telephone: telephone.value,
        type,
        captcha_id: captchaId.value,
        captcha_answer: captchaAnswer.value.trim(),
      })
    });
    const res = await response.json();
    if (res.success) {
      // 人机验证已在服务端一次性消费；发送成功后不立刻刷新，避免用户感觉“还要再输一次”
      // 倒计时结束后再换新图，供下次重发使用
      countdown.value = res.retry_after || 60;
      if (timer) clearInterval(timer);
      timer = setInterval(() => {
        countdown.value--;
        if (countdown.value <= 0) {
          clearInterval(timer);
          void loadSmsCaptcha();
        }
      }, 1000);
    } else {
      errorMsg.value = res.error || '验证码发送失败';
      if (res.refresh_captcha) {
        void loadSmsCaptcha();
      }
    }
  } catch (err: any) {
    errorMsg.value = '网络错误，请稍后再试';
    void loadSmsCaptcha();
  }
};

const smsCodeType = () => {
  if (mode.value === 'sms_login') return 'login' as const;
  if (mode.value === 'forgot') return 'forgot' as const;
  return 'register' as const;
};

const handleSmsAuth = async () => {
  if (!/^1[3-9]\d{9}$/.test(telephone.value)) {
    errorMsg.value = '请输入正确的11位手机号';
    return;
  }
  if (!code.value) {
    errorMsg.value = '请输入验证码';
    return;
  }

  if (mode.value === 'sms_login') {
    loading.value = true;
    errorMsg.value = '';
    try {
      const response = await fetch('/api/sms-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          telephone: telephone.value,
          code: code.value,
        }),
      });
      const res = await response.json();
      if (res.success && res.token) {
        setAuth(res.token, res.user);
        router.push(getRedirectTarget());
      } else {
        errorMsg.value = res.error || '登录失败';
      }
    } catch (err: any) {
      errorMsg.value = '网络错误，请稍后再试';
    } finally {
      loading.value = false;
    }
    return;
  }

  if (mode.value === 'sms' && (!username.value || !password.value || !confirmPassword.value)) {
    errorMsg.value = '请输入用于注册的用户名和密码';
    return;
  }
  if (mode.value === 'forgot' && (!password.value || !confirmPassword.value)) {
    errorMsg.value = '请输入新密码并确认';
    return;
  }
  if (password.value !== confirmPassword.value) {
    errorMsg.value = '两次输入的密码不一致';
    return;
  }
  if (password.value.length < 6) {
    errorMsg.value = '密码至少6位';
    return;
  }

  loading.value = true;
  errorMsg.value = '';
  
  try {
    const endpoint = mode.value === 'sms' ? '/api/register' : '/api/forgot-password';
    const body = mode.value === 'sms' 
      ? {
          telephone: telephone.value,
          code: code.value,
          username: username.value,
          password: password.value,
          confirmPassword: confirmPassword.value,
        }
      : {
          telephone: telephone.value,
          code: code.value,
          newPassword: password.value,
          confirmPassword: confirmPassword.value,
        };
      
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const res = await response.json();
    
    if (res.success) {
      if (mode.value === 'sms' && res.token) {
        setAuth(res.token, res.user);
        router.push(getRedirectTarget());
      } else {
        // forgot password success, return to login
        mode.value = 'password';
        errorMsg.value = '密码重置成功，请重新登录';
      }
    } else {
      errorMsg.value = res.error || '操作失败';
    }
  } catch (err: any) {
    errorMsg.value = '网络错误，请稍后再试';
  } finally {
    loading.value = false;
  }
};

const normalizeRedirect = (target: string | undefined): string => {
  if (!target) return '/app';
  if (target.startsWith('/') && !target.startsWith('//')) return target;
  return '/app';
};

const getRedirectTarget = () => normalizeRedirect(
  typeof route.query.redirect === 'string' ? route.query.redirect : undefined,
);

const handleCasEntry = () => {
  const target = getRedirectTarget();
  window.location.href = AuthAPI.getCasLoginUrl(target);
};

onMounted(async () => {
  const code = String(route.query.code || '').trim();
  if (!code) {
    return;
  }

  loading.value = true;
  errorMsg.value = '';
  try {
    const result = await AuthAPI.exchangeCasCode(code);
    if (!result.success || !result.token) {
      errorMsg.value = result.message || '统一认证登录失败，请重试';
      return;
    }
    setAuth(result.token, result.user);
    const targetUrl = normalizeRedirect(
      typeof route.query.targetUrl === 'string' ? route.query.targetUrl : '/app'
    );
    router.replace(targetUrl);
  } catch {
    errorMsg.value = '统一认证登录已过期，请重新登录';
  } finally {
    loading.value = false;
  }
});

const handleLogin = async () => {
  if (!username.value || !password.value) {
    errorMsg.value = '请输入用户名和密码';
    return;
  }
  
  loading.value = true;
  errorMsg.value = '';
  
  try {
    const res = await AuthAPI.login(username.value, password.value);
    if (res.success && res.token) {
      setAuth(res.token, res.user);
      router.push(getRedirectTarget());
    } else {
      errorMsg.value = res.message || '登录失败，请检查用户名或密码';
    }
  } catch (err: any) {
    errorMsg.value = err.message || '网络错误，请稍后再试';
  } finally {
    loading.value = false;
  }
};
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-[#f8fafc] dark:bg-[#0b0f1a] relative overflow-hidden">
    <!-- Background Accents -->
    <div class="absolute inset-0 z-0 overflow-hidden pointer-events-none">
      <div class="absolute -top-[10%] -left-[10%] w-[40%] h-[40%] bg-accent-soft rounded-full blur-[120px]"></div>
      <div class="absolute bottom-[10%] right-[10%] w-[30%] h-[30%] bg-emerald-500/10 rounded-full blur-[100px]"></div>
    </div>

    <div class="z-10 w-full max-w-md px-6">
      <div class="text-center mb-10">
        <div class="inline-flex items-center justify-center w-20 h-20 rounded-[28px] bg-surface shadow-sm dark:shadow-none mb-6 border border-border-subtle">
          <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="text-accent">
            <path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><circle cx="9" cy="15" r="1"/>
          </svg>
        </div>
        <h1 class="text-3xl font-black text-text-primary dark:text-white tracking-tight mb-3">成理场地污染水文地质智学助手</h1>
        <p class="text-text-secondary font-medium">请登录以开启您的学术研究之旅</p>
      </div>

      <div class="bg-surface/80 backdrop-blur-xl p-8 rounded-[32px] shadow-sm dark:shadow-none border border-border-subtle">
        <!-- 账号密码登录 -->
        <form v-if="mode === 'password'" @submit.prevent="handleLogin" class="space-y-6">
          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">用户名</label>
            <div class="relative group">
              <input 
                type="text" 
                v-model="username" 
                placeholder="请输入用户名"
                class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
                required
              />
            </div>
          </div>
          
          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">密码</label>
            <input 
              type="password" 
              v-model="password" 
              placeholder="请输入密码"
              class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
              required
            />
          </div>
          
          <div v-if="errorMsg" class="flex items-center gap-2 text-rose-500 text-sm font-medium bg-rose-50 dark:bg-rose-500/10 px-4 py-3 rounded-xl border border-rose-100 dark:border-rose-500/20 animate-in fade-in slide-in-from-top-2">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>
            {{ errorMsg }}
          </div>
          
          <button 
            type="submit" 
            :disabled="loading"
            class="w-full bg-accent text-white font-bold py-5 rounded-[20px] shadow-sm dark:shadow-none hover:bg-accent-hover transition-all duration-300 active:scale-[0.98] disabled:opacity-50 disabled:active:scale-100 flex items-center justify-center gap-3 mt-4"
          >
            <svg v-if="loading" class="animate-spin h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span class="tracking-widest">{{ loading ? '正在认证' : '登录' }}</span>
          </button>

          <button
            type="button"
            @click="handleCasEntry"
            class="w-full border border-accent/30 text-accent font-semibold py-4 rounded-[16px] hover:bg-accent-soft transition-all duration-300"
          >
            统一认证 CAS 登录
          </button>

          <div class="flex items-center justify-between pt-2 gap-3 flex-wrap">
            <button type="button" @click="handleOpenSmsLogin" class="text-sm font-semibold text-text-secondary hover:text-accent transition-colors">短信登录</button>
            <button type="button" @click="handleOpenRegister" class="text-sm font-semibold text-text-secondary hover:text-accent transition-colors">用户注册</button>
            <button type="button" @click="handleForgotPassword" class="text-sm font-semibold text-text-secondary hover:text-accent transition-colors">忘记密码?</button>
          </div>
        </form>

        <!-- 短信登录 / 用户注册 / 忘记密码 -->
        <form v-else @submit.prevent="handleSmsAuth" class="space-y-6">
          <div class="text-center mb-6">
            <h2 class="text-lg font-bold text-text-primary">
              {{ mode === 'sms_login' ? '短信登录' : (mode === 'sms' ? '用户注册' : '重置密码') }}
            </h2>
          </div>

          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">手机号</label>
            <input 
              type="text" 
              v-model="telephone" 
              placeholder="请输入11位手机号"
              class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
              required
            />
          </div>

          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">人机验证</label>
            <div class="flex gap-2">
              <div class="flex h-14 w-44 items-center justify-center overflow-hidden rounded-2xl border border-border-subtle bg-surface-sunken">
                <img v-if="captchaImage" :src="captchaImage" alt="人机验证" class="h-full w-full object-cover" />
                <span v-else class="text-xs font-semibold text-text-tertiary">{{ captchaLoading ? '加载中' : '点击刷新' }}</span>
              </div>
              <button
                type="button"
                @click="loadSmsCaptcha"
                :disabled="captchaLoading"
                class="shrink-0 rounded-2xl border border-border-subtle bg-surface px-4 text-sm font-semibold text-text-secondary transition-colors hover:border-accent/30 hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
              >
                刷新
              </button>
            </div>
            <input
              type="text"
              v-model="captchaAnswer"
              placeholder="请输入图片中的计算结果"
              class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
              autocomplete="off"
              required
            />
          </div>

          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">验证码</label>
            <div class="relative flex gap-2">
              <input 
                type="text" 
                v-model="code" 
                placeholder="短信验证码"
                class="flex-1 bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
                required
              />
              <button 
                type="button" 
                @click="handleSendCode(smsCodeType())"
                :disabled="countdown > 0 || captchaLoading"
                class="bg-accent-soft text-accent font-semibold px-4 rounded-2xl border border-accent/30 hover:bg-accent-soft transition-colors whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {{ countdown > 0 ? `${countdown}s 后重试` : '获取验证码' }}
              </button>
            </div>
          </div>

          <div v-if="mode === 'sms'" class="space-y-4">
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">用户名</label>
              <input 
                type="text" 
                v-model="username" 
                placeholder="设置用户名"
                class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
                required
              />
            </div>
            
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">设置密码</label>
              <input 
                type="password" 
                v-model="password" 
                placeholder="至少6位"
                class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
                required
              />
            </div>

            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">确认密码</label>
              <input 
                type="password" 
                v-model="confirmPassword" 
                placeholder="请再次输入密码"
                class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
                required
              />
            </div>
          </div>

          <div v-if="mode === 'forgot'" class="space-y-4">
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">新密码</label>
              <input 
                type="password" 
                v-model="password" 
                placeholder="至少6位"
                class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
                required
              />
            </div>

            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-text-tertiary pl-1">确认新密码</label>
              <input 
                type="password" 
                v-model="confirmPassword" 
                placeholder="请再次输入新密码"
                class="w-full bg-surface-sunken border border-border-subtle rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-accent/10 focus:border-accent transition-all duration-300 dark:text-white placeholder:text-text-tertiary"
                required
              />
            </div>
          </div>

          <div v-if="errorMsg" class="flex items-center gap-2 text-rose-500 text-sm font-medium bg-rose-50 dark:bg-rose-500/10 px-4 py-3 rounded-xl border border-rose-100 dark:border-rose-500/20 animate-in fade-in slide-in-from-top-2">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>
            {{ errorMsg }}
          </div>

          <button 
            type="submit" 
            :disabled="loading"
            class="w-full bg-accent text-white font-bold py-4 rounded-[20px] shadow-sm dark:shadow-none hover:bg-accent-hover transition-all duration-300 flex items-center justify-center gap-2"
          >
            <svg v-if="loading" class="animate-spin h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span class="tracking-widest">{{ loading ? '处理中...' : (mode === 'sms_login' ? '登录' : (mode === 'sms' ? '注册' : '重置密码')) }}</span>
          </button>

          <div class="text-center mt-4">
            <button type="button" @click="handleBackToLogin" class="text-sm font-semibold text-text-secondary hover:text-accent transition-colors">返回账号密码登录</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>
