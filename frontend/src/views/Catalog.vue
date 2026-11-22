<template>
  <div>
    <h2>目录中心 · 来源版本 / 换码链 / 撤回</h2>

    <div class="card">
      <h3>目录来源版本（目录接收器）</h3>
      <div class="form-row">
        <label>按商品码过滤</label>
        <select v-model="skuFilter">
          <option value="">全部</option>
          <option v-for="s in skus" :key="s" :value="s">{{ s }}</option>
        </select>
        <span class="muted">每行一个来源版本：商品码、包装层级、单位换算、监管分类、品类、门店可售与生效区间</span>
      </div>
      <table>
        <thead>
          <tr><th>商品码</th><th>版本</th><th>名称</th><th>品类</th><th>监管分类</th><th>基本单位</th><th>单位换算</th><th>可售门店</th><th>生效区间</th><th>换码/套装</th><th>入账</th></tr>
        </thead>
        <tbody>
          <tr v-for="it in filteredItems" :key="it.id">
            <td class="mono">{{ it.sku }}</td>
            <td class="mono">v{{ it.version }} <span class="muted">#{{ it.catalog_event_id }}</span></td>
            <td>{{ it.name }}</td>
            <td>{{ it.category }}</td>
            <td><span :class="['tag', it.reg_class]">{{ it.reg_class }}</span></td>
            <td>{{ it.base_unit }}</td>
            <td class="mono">{{ fmtConv(it) }}</td>
            <td class="mono">{{ it.store_scope === 'ALL' ? 'ALL' : it.store_scope.join(',') }}</td>
            <td class="mono">{{ it.effective_from.slice(0, 10) }} → {{ it.effective_to ? it.effective_to.slice(0, 10) : '∞' }}</td>
            <td>
              <span v-if="it.supersedes_code" class="cause">换码←{{ it.supersedes_code }}</span>
              <span v-if="it.bundle_components" class="cause">套装×{{ it.bundle_components.length }}</span>
              <span v-if="it.allocation_basis" class="cause">分摊{{ JSON.stringify(it.allocation_basis.weights) }}</span>
            </td>
            <td class="mono">{{ it.sys_from.slice(0, 10) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h3>换码链</h3>
        <div class="form-row">
          <input v-model="chainSku" placeholder="商品码，如 IBU-100" style="width:160px" />
          <button @click="loadChain">查询</button>
        </div>
        <template v-if="chain">
          <p>
            <span v-for="p in chain.predecessors" :key="p" class="cause">{{ p }}</span>
            <span class="arrow">→</span>
            <b class="mono">{{ chain.sku }}</b>
            <span class="arrow">→</span>
            <span v-for="s in chain.successors" :key="s" class="cause">{{ s }}</span>
          </p>
          <table>
            <thead><tr><th>版本</th><th>品类/监管</th><th>生效区间</th></tr></thead>
            <tbody>
              <tr v-for="v in chain.versions" :key="v.id">
                <td class="mono">v{{ v.version }}</td>
                <td>{{ v.category }} / <span :class="['tag', v.reg_class]">{{ v.reg_class }}</span></td>
                <td class="mono">{{ v.effective_from.slice(0, 10) }} → {{ v.effective_to ? v.effective_to.slice(0, 10) : '∞' }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </div>

      <div class="card">
        <h3>目录撤回（追溯撤回，可只覆盖部分生效期）</h3>
        <table>
          <thead><tr><th>商品码</th><th>目标版本事件</th><th>撤回区间</th><th>入账</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="w in withdrawals" :key="w.id">
              <td class="mono">{{ w.sku }}</td>
              <td class="mono">event#{{ w.target_event_id }}</td>
              <td class="mono">[{{ w.withdraw_from.slice(0, 10) }}, {{ w.withdraw_to ? w.withdraw_to.slice(0, 10) : '∞' }})</td>
              <td class="mono">{{ w.sys_from.slice(0, 10) }}</td>
              <td class="muted">{{ w.reason }}</td>
            </tr>
          </tbody>
        </table>
        <p class="muted">撤回只剔除目标版本在撤回区间内的效力；前序版本的派生生效区间自然延伸覆盖缺口（回落语义），解析器仅重审该区间的交易。</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const items = ref([])
const withdrawals = ref([])
const skuFilter = ref('')
const chainSku = ref('IBU-100')
const chain = ref(null)

const skus = computed(() => [...new Set(items.value.map(i => i.sku))].sort())
const filteredItems = computed(() => skuFilter.value ? items.value.filter(i => i.sku === skuFilter.value) : items.value)

function fmtConv(it) {
  const conv = it.unit_conversions || {}
  const parts = Object.entries(conv).map(([u, n]) => `1${u}=${n}${it.base_unit || ''}`)
  return parts.length ? parts.join(' ') : '—'
}

async function load() {
  items.value = await api.get('/catalog/items')
  withdrawals.value = await api.get('/catalog/withdrawals')
}
async function loadChain() {
  chain.value = null
  try {
    chain.value = await api.get(`/catalog/chain/${chainSku.value}`)
  } catch (e) {
    alert(e.message)
  }
}
onMounted(async () => { await load(); await loadChain() })
</script>
