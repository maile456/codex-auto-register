import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      redirect: '/launch',
    },
    {
      path: '/launch',
      name: 'launch',
      component: () => import('@/views/LaunchView.vue'),
      meta: { title: '启动界面' },
    },
    {
      path: '/accounts',
      name: 'accounts',
      component: () => import('@/views/AccountsView.vue'),
      meta: { title: '账号池' },
    },
    {
      path: '/emails',
      name: 'emails',
      component: () => import('@/views/EmailsView.vue'),
      meta: { title: '邮箱池' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/SettingsView.vue'),
      meta: { title: '配置栏' },
    },
    {
      path: '/payment-tools',
      name: 'payment-tools',
      component: () => import('@/views/PaymentToolsView.vue'),
      meta: { title: '提炼' },
    },
    {
      path: '/pipeline',
      name: 'pipeline',
      component: () => import('@/views/PipelineView.vue'),
      meta: { title: '自动流水线' },
    },
    {
      path: '/hero-sms',
      name: 'hero-sms',
      component: () => import('@/views/HeroSmsSettingsView.vue'),
      meta: { title: 'HeroSMS 接码' },
    },
    {
      path: '/paid-accounts',
      name: 'paid-accounts',
      component: () => import('@/views/PaidAccountsView.vue'),
      meta: { title: '成品管理' },
    },
    {
      path: '/agreement-tools',
      name: 'agreement-tools',
      component: () => import('@/views/AgreementToolsView.vue'),
      meta: { title: '协议授权' },
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/launch',
    },
  ],
})

export default router
