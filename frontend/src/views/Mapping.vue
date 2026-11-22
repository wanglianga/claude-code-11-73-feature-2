<template>
  <div>
    <h2>映射解析 · 口径校验</h2>

    <div class="card">
      <h3>单行即时解释（路线 B：查询时按历史目录版本解释）</h3>
      <div class="form-row">
        <label>销售行</label>
        <select v-model="resolveSaleId">
          <option v-for="s in saleIds" :key="s" :value="s">{{ s }}</option>
        </select>
        <label>系统时点（留空=当前）</label>
        <input v-model="resolveSys" placeholder="2026-03-01T00:00:00Z" style="width:200px" />
        <button @click="doResolve">解释</button>
      </div>
      <template v-if="resolved">
        <table>
          <tbody>
            <tr><th>销售行</th><td class="mono">{{ resolved.sale_id }}（{{ resolved.store }}，{{ resolved.biz_time?.slice(0, 10) }}，sku {{ resolved.sku }}）</td></tr>
            <tr><th>结论</th><td>
              <span :class="['tag', resolved.flag === 'OK' ? 'OK' : 'OPEN']">{{ resolved.flag }}</span>
              <span v-if="resolved.catalog" class="mono">
                目录 v{{ resolved.catalog.version }}（event#{{ resolved.catalog.catalog_event_id }}）
                {{ resolved.catalog.category }}/{{ resolved.catalog.reg_class }}，
                生效 {{ resolved.catalog.effective_from.slice(0, 10) }} → {{ resolved.catalog.effective_to ? resolved.catalog.effective_to.slice(0, 10) : '∞' }}
              </span>
              <span v-else class="muted">{{ resolved.flag_detail }}</span>
            </td></tr>
            <tr v-if="resolved.allocations?.length"><th>分摊/积分</th><td>
              <span v-for="(a, i) in resolved.allocations" :key="i" class="cause">
                {{ a.category }}/{{ a.reg_class }} 净额¥{{ a.net_amount }} 数量{{ a.qty_base }} 积分{{ a.points }}{{ a.points_eligible ? '' : '（不计）' }}
              </span>
              <span class="muted">规则版本 v{{ resolved.rule_version }}</span>
            </td></tr>
          </tbody>
        </table>
        <p class="muted">示例：POS-0003 在系统时点 2026-03-01 解释为 RX（v2），当前解释为 OTC（v1 回落）——追溯撤回只重审 [02-01, 03-01) 区间的交易。</p>
      </template>
    </div>

    <div class="card">
      <h3>口径校验器 · 待核队列（无法自动归类，不平均分配、不猜测）</h3>
      <div class="form-row">
        <label>状态</label>
        <select v-model="flagStatus">
          <option value="OPEN">待核中</option>
          <option value="RESOLVED">已解决</option>
          <option value="">全部</option>
        </select>
      </div>
      <table>
        <thead><tr><th>状态</th><th>销售行</th><th>交易日</th><th>门店</th><th>商品码</th><th>标志</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="f in flags" :key="f.id">
            <td><span :class="['tag', f.status]">{{ f.status === 'OPEN' ? '待核' : '已解决' }}</span></td>
            <td class="mono">{{ f.sale_id }}</td>
            <td class="mono">{{ f.biz_time.slice(0, 10) }}</td>
            <td class="mono">{{ f.store }}</td>
            <td class="mono">{{ f.sku }}</td>
            <td><span class="tag bad-flag">{{ f.flag }}</span></td>
            <td class="muted">{{ f.detail }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3>物化映射切片（路线 A：目录修订时预物化，带有效区间与重审留痕）</h3>
      <div class="form-row">
        <label><input type="checkbox" v-model="currentOnly" /> 仅当前切片</label>
        <label>标志</label>
        <select v-model="sliceFlag">
          <option value="">全部</option>
          <option>OK</option><option>ONE_TO_MANY</option><option>STALE_CODE</option>
          <option>MISSING_UNIT</option><option>MISSING_DISCOUNT</option><option>NOT_SELLABLE</option>
        </select>
        <span class="muted">目录修订仅闭合并重开生效区间相交的切片（sys_to 留痕），区间外切片不动</span>
      </div>
      <table>
        <thead>
          <tr><th>销售行</th><th>交易日</th><th>门店</th><th>商品码</th><th>分类结论</th><th>目录版本</th><th>净额</th><th>基本数量</th><th>积分</th><th>规则</th><th>映射有效区间</th><th>切片系统区间</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in slices" :key="s.id" :style="s.sys_to ? 'opacity:.55' : ''">
            <td class="mono">{{ s.sale_id }}</td>
            <td class="mono">{{ s.biz_time.slice(0, 10) }}</td>
            <td class="mono">{{ s.store }}</td>
            <td class="mono">{{ s.sku }}</td>
            <td>
              <span v-if="s.flag === 'OK'">{{ s.category }}/<span :class="['tag', s.reg_class]">{{ s.reg_class }}</span></span>
              <span v-else class="tag bad-flag">{{ s.flag }}</span>
              <span v-if="s.alloc_weight !== 1" class="muted"> ×{{ s.alloc_weight }}</span>
            </td>
            <td class="mono">{{ s.catalog_version ? 'v' + s.catalog_version : '—' }}</td>
            <td class="mono">{{ s.net_amount ?? '—' }}</td>
            <td class="mono">{{ s.qty_base ?? '—' }}</td>
            <td class="mono">{{ s.points }}<span v-if="!s.points_eligible && s.flag === 'OK'" class="muted">（不计）</span></td>
            <td class="mono">{{ s.rule_version ? 'v' + s.rule_version : '—' }}</td>
            <td class="mono">{{ s.mapping_valid_from ? s.mapping_valid_from.slice(0, 10) : '—' }} → {{ s.mapping_valid_to ? s.mapping_valid_to.slice(0, 10) : '∞' }}</td>
            <td class="mono">[{{ s.sys_from.slice(0, 10) }}, {{ s.sys_to ? s.sys_to.slice(0, 10) : '今' }})</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'

const slices = ref([])
const flags = ref([])
const flagStatus = ref('OPEN')
const currentOnly = ref(true)
const sliceFlag = ref('')
const resolveSaleId = ref('POS-0003')
const resolveSys = ref('')
const resolved = ref(null)

const saleIds = computed(() => [...new Set(slices.value.map(s => s.sale_id))].sort())

async function loadSlices() {
  const q = []
  if (currentOnly.value) q.push('current=true')
  if (sliceFlag.value) q.push(`flag=${sliceFlag.value}`)
  slices.value = await api.get('/mapping/slices' + (q.length ? '?' + q.join('&') : ''))
}
async function loadFlags() {
  flags.value = await api.get('/validation/flags' + (flagStatus.value ? `?status=${flagStatus.value}` : ''))
}
async function doResolve() {
  const q = resolveSys.value ? `&sys_time=${encodeURIComponent(resolveSys.value)}` : ''
  resolved.value = await api.get(`/mapping/resolve?sale_id=${resolveSaleId.value}${q}`)
}
watch([currentOnly, sliceFlag], loadSlices)
watch(flagStatus, loadFlags)
onMounted(async () => { await loadSlices(); await loadFlags(); await doResolve() })
</script>
