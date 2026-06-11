<script setup>
import { ref } from 'vue';
import { AuthAPI } from '@/api/auth';
import { setAuth } from '@/utils/request';

const emit = defineEmits(['login-success']);

const username = ref('');
const password = ref('');
const errorMsg = ref('');
const loading = ref(false);

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
      emit('login-success');
    } else {
      errorMsg.value = res.message || '登录失败，请检查用户名或密码';
    }
  } catch (err) {
    errorMsg.value = err.message || '网络错误，请稍后再试';
  } finally {
    loading.value = false;
  }
};
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-[#f8fafc] dark:bg-[#0b0f1a] relative overflow-hidden w-full">
    <!-- Background Accents -->
    <div class="absolute inset-0 z-0 overflow-hidden pointer-events-none">
      <div class="absolute -top-[10%] -left-[10%] w-[40%] h-[40%] bg-indigo-500/10 rounded-full blur-[120px]"></div>
      <div class="absolute bottom-[10%] right-[10%] w-[30%] h-[30%] bg-emerald-500/10 rounded-full blur-[100px]"></div>
    </div>

    <div class="z-10 w-full max-w-md px-6 py-12">
      <div class="text-center mb-10">
        <div class="inline-flex items-center justify-center w-20 h-20 rounded-[28px] bg-white dark:bg-slate-900 shadow-2xl shadow-indigo-200/50 dark:shadow-none mb-6 border border-slate-100 dark:border-slate-800">
          <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="text-indigo-600 dark:text-indigo-400">
            <path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><circle cx="9" cy="15" r="1"/>
          </svg>
        </div>
        <h1 class="text-3xl font-black text-slate-900 dark:text-white tracking-tight mb-3">核电子学智能助手</h1>
      </div>

      <div class="bg-white/80 dark:bg-slate-900/80 backdrop-blur-xl p-8 rounded-[32px] shadow-2xl shadow-slate-200/50 dark:shadow-none border border-white dark:border-slate-800">
        <!-- 账号密码登录 -->
        <form @submit.prevent="handleLogin" class="space-y-6">
          <div class="space-y-2">
            <label class="text-xs font-bold uppercase tracking-widest text-slate-400 pl-1">用户名</label>
            <div class="relative group">
              <input 
                type="text" 
                v-model="username" 
                placeholder="请输入用户名"
                class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
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
              class="w-full bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-800 rounded-2xl px-5 py-4 outline-none focus:ring-4 focus:ring-indigo-500/10 focus:border-indigo-500 transition-all duration-300 dark:text-white placeholder:text-slate-400 text-slate-800"
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
        </form>
      </div>
    </div>
  </div>
</template>
