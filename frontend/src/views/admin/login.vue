<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { AuthAPI } from '@/api/admin';
import { setAuth } from '@/utils/request';

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
  <div class="min-h-screen flex items-center justify-center bg-slate-100 dark:bg-slate-900">
    <div class="bg-white dark:bg-slate-800 p-8 rounded-xl shadow-md w-full max-w-sm">
      <h2 class="text-2xl font-bold text-slate-800 dark:text-slate-100 text-center mb-6">知识库管理台</h2>
      
      <form @submit.prevent="handleLogin" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">用户名</label>
          <input 
            type="text" 
            v-model="username" 
            class="w-full px-3 py-2 border border-slate-300 rounded outline-none focus:ring-2 focus:ring-indigo-500"
            required
          />
        </div>
        
        <div>
          <label class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">密码</label>
          <input 
            type="password" 
            v-model="password" 
            class="w-full px-3 py-2 border border-slate-300 rounded outline-none focus:ring-2 focus:ring-indigo-500"
            required
          />
        </div>
        
        <div v-if="errorMsg" class="text-red-500 text-sm">
          {{ errorMsg }}
        </div>
        
        <button 
          type="submit" 
          :disabled="loading"
          class="w-full bg-indigo-600 text-white font-medium py-2 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500 hover:bg-indigo-700 disabled:opacity-50 mt-4"
        >
          {{ loading ? '登录中...' : '登录' }}
        </button>
      </form>
    </div>
  </div>
</template>
