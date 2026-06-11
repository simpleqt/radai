import { createApp } from 'vue';
import App from './App.vue';
import './style.css';
import { initializeGuestSessionLifecycle } from '@/services/storage/guestIndexedDb';
import { startAuthAutoLogoutMonitor } from '@/utils/request';

startAuthAutoLogoutMonitor();
initializeGuestSessionLifecycle();
createApp(App).mount('#app');

