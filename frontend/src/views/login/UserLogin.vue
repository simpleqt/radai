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

const mode = ref<'password' | 'sms' | 'forgot'>('password');
const telephone = ref('');
const code = ref('');
const countdown = ref(0);
let timer: any = null;

const handleSmsLogin = () => {
  mode.value = 'sms';
  password.value = '';
  confirmPassword.value = '';
  errorMsg.value = '';
};

const handleForgotPassword = () => {
  mode.value = 'forgot';
  password.value = '';
  confirmPassword.value = '';
  errorMsg.value = '';
};

const handleBackToLogin = () => {
  mode.value = 'password';
  password.value = '';
  confirmPassword.value = '';
  errorMsg.value = '';
};

const handleSendCode = async (type: 'register' | 'forgot' = 'register') => {
  if (!/^1[3-9]\d{9}$/.test(telephone.value)) {
    errorMsg.value = '请输入正确的11位手机号';
    return;
  }
  
  errorMsg.value = '';
  try {
    const response = await fetch('/api/send-sms-code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ telephone: telephone.value, type })
    });
    const res = await response.json();
    if (res.success) {
      countdown.value = 60;
      timer = setInterval(() => {
        countdown.value--;
        if (countdown.value <= 0) clearInterval(timer);
      }, 1000);
    } else {
      errorMsg.value = res.error || '验证码发送失败';
    }
  } catch (err: any) {
    errorMsg.value = '网络错误，请稍后再试';
  }
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
        router.push('/app');
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

const handleCasEntry = () => {
  const target = '/app';
  window.location.href = AuthAPI.getCasLoginUrl(target);
};

onMounted(() => {
  const token = String(route.query.token || '').trim();
  if (!token) {
    return;
  }

  const user = {
    user_id: String(route.query.user_id || ''),
    username: String(route.query.username || ''),
    display_name: String(route.query.display_name || route.query.name || route.query.username || ''),
    role: String(route.query.role || 'user'),
  };

  setAuth(token, user);
  const targetUrl = normalizeRedirect(
    typeof route.query.targetUrl === 'string' ? route.query.targetUrl : '/app'
  );
  router.replace(targetUrl);
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
      router.push('/app');
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
      <div class="absolute -top-[10%] -left-[10%] w-[40%] h-[40%] bg-indigo-500/10 rounded-full blur-[120px]"></div>
      <div class="absolute bottom-[10%] right-[10%] w-[30%] h-[30%] bg-emerald-500/10 rounded-full blur-[100px]"></div>
    </div>

    <div class="z-10 w-full max-w-md px-6">
      <div class="text-center mb-10">
        <div class="inline-flex items-center justify-center w-20 h-20 rounded-[28px] bg-white dark:bg-slate-900 shadow-2xl shadow-indigo-200/50 dark:shadow-none mb-6 border border-slate-100 dark:border-slate-800">
          <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="text-indigo-600 dark:text-indigo-400">
            <path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><circle cx="9" cy="15" r="1"/>
          </svg>
        </div>
        <h1 class="text-3xl font-black text-slate-900 dark:text-white tracking-tight mb-3">核电子学智能助手</h1>
        <p class="text-slate-500 dark:text-slate-400 font-medium">请登录以开启您的学术研究之旅</p>
      </div>

      <div class="bg-white/80 dark:bg-slate-900/80 backdrop-blur-xl p-8 rounded-[32px] shadow-2xl shadow-slate-200/50 dark:shadow-none border border-white dark:border-slate-800">
        <!-- 账号密码登录 -->
        <form v-if="mode === 'password'" @submit.prevent="handleLogin" class="space-y-6">
          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">用户名</label>
            <div class="relative group">
              <input 
                type="text" 
                v-model="username" 
                placeholder="请输入用户名"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
                required
              />
            </div>
          </div>
          
          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">密码</label>
            <input 
              type="password" 
              v-model="password" 
              placeholder="请输入密码"
              class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
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
            class="w-full bg-indigo-600 dark:bg-indigo-500 text-white font-bold py-5 rounded-[20px] shadow-xl shadow-indigo-200 dark:shadow-none hover:bg-indigo-700 dark:hover:bg-indigo-600 transition-all duration-300 active:scale-[0.98] disabled:opacity-50 disabled:active:scale-100 flex items-center justify-center gap-3 mt-4"
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
            class="w-full border border-indigo-200 dark:border-indigo-700 text-indigo-700 dark:text-indigo-300 font-semibold py-4 rounded-[16px] hover:bg-indigo-50 dark:hover:bg-indigo-900/20 transition-all duration-300"
          >
            统一认证 CAS 登录
          </button>

          <div class="flex items-center justify-between pt-2">
            <button type="button" @click="handleSmsLogin" class="text-sm font-semibold text-slate-500 hover:text-indigo-600 transition-colors">用户注册</button>
            <button type="button" @click="handleForgotPassword" class="text-sm font-semibold text-slate-500 hover:text-indigo-600 transition-colors">忘记密码?</button>
          </div>
        </form>

        <!-- 用户注册 / 忘记密码 -->
        <form v-else @submit.prevent="handleSmsAuth" class="space-y-6">
          <div class="text-center mb-6">
            <h2 class="text-lg font-bold text-slate-800 dark:text-slate-200">{{ mode === 'sms' ? '用户注册' : '重置密码' }}</h2>
          </div>

          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">手机号</label>
            <input 
              type="text" 
              v-model="telephone" 
              placeholder="请输入11位手机号"
              class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
              required
            />
          </div>

          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">验证码</label>
            <div class="relative flex gap-2">
              <input 
                type="text" 
                v-model="code" 
                placeholder="短信验证码"
                class="flex-1 bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
                required
              />
              <button 
                type="button" 
                @click="handleSendCode(mode === 'sms' ? 'register' : 'forgot')"
                :disabled="countdown > 0"
                class="bg-indigo-50 dark:bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 font-semibold px-4 rounded-2xl border border-indigo-100 dark:border-indigo-500/20 hover:bg-indigo-100 dark:hover:bg-indigo-500/20 transition-colors whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {{ countdown > 0 ? `${countdown}s 后重试` : '获取验证码' }}
              </button>
            </div>
          </div>

          <div v-if="mode === 'sms'" class="space-y-4">
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">用户名</label>
              <input 
                type="text" 
                v-model="username" 
                placeholder="设置用户名"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
                required
              />
            </div>
            
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">设置密码</label>
              <input 
                type="password" 
                v-model="password" 
                placeholder="至少6位"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
                required
              />
            </div>

            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">确认密码</label>
              <input 
                type="password" 
                v-model="confirmPassword" 
                placeholder="请再次输入密码"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
                required
              />
            </div>
          </div>

          <div v-if="mode === 'forgot'" class="space-y-4">
            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">新密码</label>
              <input 
                type="password" 
                v-model="password" 
                placeholder="至少6位"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
                required
              />
            </div>

            <div class="space-y-2">
              <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">确认新密码</label>
              <input 
                type="password" 
                v-model="confirmPassword" 
                placeholder="请再次输入新密码"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400"
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
            class="w-full bg-indigo-600 dark:bg-indigo-500 text-white font-bold py-4 rounded-[20px] shadow-xl shadow-indigo-200 dark:shadow-none hover:bg-indigo-700 dark:hover:bg-indigo-600 transition-all duration-300 flex items-center justify-center gap-2"
          >
            <svg v-if="loading" class="animate-spin h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            <span class="tracking-widest">{{ loading ? '处理中...' : (mode === 'sms' ? '登录 / 注册' : '重置密码') }}</span>
          </button>

          <div class="text-center mt-4">
            <button type="button" @click="handleBackToLogin" class="text-sm font-semibold text-slate-500 hover:text-indigo-600 transition-colors">返回账号密码登录</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>
