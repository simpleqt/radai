<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { AuthAPI } from '@/api/admin';
import { setAuth } from '@/utils/request';
import { User, Lock, Bot, Loader2 } from 'lucide-vue-next';

const router = useRouter();
const username = ref('admin');
const password = ref('admin');
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
    if (res.token) {
      setAuth(res.token, res.user);
      router.push('/admin/dashboard');
    } else {
      errorMsg.value = res.message || '登录失败';
    }
  } catch (err: any) {
    errorMsg.value = err.message || '登录失败';
  } finally {
    loading.value = false;
  }
};
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-accent relative overflow-hidden p-4">
    <!-- Decorative Glowing Blobs -->
    <div class="absolute w-72 h-72 rounded-full bg-accent-soft blur-3xl -top-10 -left-10 animate-pulse-glow"></div>
    <div class="absolute w-80 h-80 rounded-full bg-accent-soft blur-3xl -bottom-10 -right-10 animate-pulse-glow" style="animation-delay: 1.5s"></div>

    <!-- Login Card -->
    <div class="bg-surface/85 backdrop-blur-xl border border-white/20 p-10 rounded-3xl shadow-sm w-full max-w-md relative overflow-hidden transition-all duration-300 hover:border-accent/20">
      
      <!-- Card Header -->
      <div class="flex flex-col items-center mb-8">
        <div class="w-12 h-12 bg-accent rounded-2xl flex items-center justify-center shadow-sm mb-4 hover:scale-105 transition-transform duration-300">
          <Bot class="w-6 h-6 text-white" />
        </div>
        <h2 class="text-2xl font-extrabold tracking-tight text-text-primary">
          智能体管理平台
        </h2>
        <p class="text-xs text-text-tertiary mt-1">水文地质与场地污染修复管控中心</p>
      </div>
      
      <!-- Form -->
      <form @submit.prevent="handleLogin" class="space-y-5">
        <div>
          <label class="block text-xs font-bold text-text-tertiary uppercase tracking-widest mb-1.5 ml-1">用户名</label>
          <div class="relative flex items-center focus-within:text-accent">
            <User class="w-4 h-4 absolute left-3.5 text-text-tertiary transition-colors" />
            <input 
              type="text" 
              v-model="username" 
              placeholder="请输入用户名"
              class="w-full pl-10 pr-4 py-3 bg-surface-sunken border border-border-subtle rounded-2xl outline-none text-sm transition-all focus:bg-surface focus:ring-2 focus:ring-accent/20 focus:border-accent/80 text-text-primary"
              required
            />
          </div>
        </div>
        
        <div>
          <label class="block text-xs font-bold text-text-tertiary uppercase tracking-widest mb-1.5 ml-1">密码</label>
          <div class="relative flex items-center focus-within:text-accent">
            <Lock class="w-4 h-4 absolute left-3.5 text-text-tertiary transition-colors" />
            <input 
              type="password" 
              v-model="password" 
              placeholder="请输入密码"
              class="w-full pl-10 pr-4 py-3 bg-surface-sunken border border-border-subtle rounded-2xl outline-none text-sm transition-all focus:bg-surface focus:ring-2 focus:ring-accent/20 focus:border-accent/80 text-text-primary"
              required
            />
          </div>
        </div>
        
        <!-- Error Alerts -->
        <transition name="fade">
          <div v-if="errorMsg" class="text-red-500 dark:text-red-400 text-xs font-semibold flex items-center space-x-1.5 mt-2 bg-red-50 dark:bg-red-950/20 p-2.5 rounded-xl border border-red-150 dark:border-red-900/30">
            <span class="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse"></span>
            <span>{{ errorMsg }}</span>
          </div>
        </transition>
        
        <!-- Submit Button -->
        <button 
          type="submit" 
          :disabled="loading"
          class="w-full mt-6 bg-accent text-white font-bold py-3 rounded-2xl shadow-sm transition-all focus:outline-none focus:ring-2 focus:ring-accent/50 active:scale-95 disabled:opacity-50 flex items-center justify-center space-x-2"
        >
          <Loader2 v-if="loading" class="w-4 h-4 animate-spin" />
          <span>{{ loading ? '正在验证身份...' : '立即登录' }}</span>
        </button>
      </form>
    </div>
  </div>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.25s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
