<template>
  <div>
    <header class="topbar">
      <div class="brand">双时间会员资格历史服务</div>
      <nav v-if="logged">
        <RouterLink to="/">总览</RouterLink>
        <RouterLink to="/events">接入账</RouterLink>
        <RouterLink to="/eligibility">资格查询</RouterLink>
        <RouterLink to="/identity">身份关系图</RouterLink>
        <RouterLink to="/rules">规则中心</RouterLink>
        <RouterLink to="/publications">发布与差异</RouterLink>
        <RouterLink to="/catalog">目录中心</RouterLink>
        <RouterLink to="/mapping">映射与校验</RouterLink>
        <RouterLink to="/datasets">数据集与可比</RouterLink>
      </nav>
      <div v-if="logged" class="user">
        <span>{{ username }}（{{ role }}）</span>
        <button class="link" @click="logout">退出</button>
      </div>
    </header>
    <main>
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
const logged = computed(() => !!localStorage.getItem('token'))
const username = computed(() => localStorage.getItem('username') || '')
const role = computed(() => localStorage.getItem('role') || '')
function logout() {
  localStorage.clear()
  location.href = '/login'
}
</script>
