<script setup lang="ts">
import { useThemePreference } from '@/composables/useThemePreference';

useThemePreference();
</script>

<template>
  <router-view v-slot="{ Component }">
    <!-- 页面级轻量过渡；多根组件会跳过动画，因此路由视图须保持单根 -->
    <Transition name="page" mode="out-in">
      <component :is="Component" />
    </Transition>
  </router-view>
</template>

<style>
#app {
  min-height: 100vh;
  width: 100%;
  margin: 0;
  padding: 0;
  background-color: var(--color-background);
}
</style>
