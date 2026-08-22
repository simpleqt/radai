import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import router from './router'
import { startAuthAutoLogoutMonitor } from '@/utils/request'
import { initializeGuestSessionLifecycle } from '@/services/storage/guestIndexedDb'

const app = createApp(App)
app.use(router)
startAuthAutoLogoutMonitor()
initializeGuestSessionLifecycle()
app.mount('#app')
