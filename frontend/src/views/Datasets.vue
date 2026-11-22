<template>
  <div>
    <h2>分析数据集 · 可比性 · 待复核</h2>

    <div class="card">
      <h3>发布分析数据集（冻结目录 / 映射 / 规则引用）</h3>
      <div class="form-row">
        <input v-model="dsForm.name" placeholder="名称，如 2026-02月报" style="width:180px" />
        <label>期间</label>
        <input v-model="dsForm.period_from" style="width:190px" placeholder="2026-02-01T00:00:00Z" />
        <span class="arrow">→</span>
        <input v-model="dsForm.period_to" style="width:190px" placeholder="2026-03-01T00:00:00Z" />
        <select v-model="dsForm.route">
          <option>AUTO</option><option>MATERIALIZED</option><option>INTERPRETED</option>
        </select>
        <button @click="createDataset">冻结发布</button>
      </div>
      <p class="muted">
        路线：MATERIALIZED=读物化切片（月度/门店分析读快，目录稳定期适用）；
        INTERPRETED=查询时逐行解释（高频修订避免切片写放大）；AUTO 按目录修订频率与行量启发式选择。
        两路线共享同一解析核心，同一期间输出相同可比范围。每份数据集显示所用路线与目录版本。
      </p>
      <table>
        <thead>
          <tr><th>id</th><th>名称</th><th>期间</th><th>路线</th><th>目录版本</th><th>映射引用</th><th>规则集</th><th>证据水位</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="d in datasets" :key="d.id">
            <td class="mono">#{{ d.id }}</td>
            <td>{{ d.name }}</td>
            <td class="mono">{{ d.period_from.slice(0, 10) }} → {{ d.period_to.slice(0, 10) }}</td>
            <td><span :class="['tag', d.route]">{{ d.route }}</span></td>
            <td class="mono">event#{{ d.catalog_version }}</td>
            <td class="mono">#{{ d.mapping_version }}</td>
            <td class="mono">event#{{ d.rule_set_version }}</td>
            <td class="mono">event#{{ d.watermark_event_id }}</td>
            <td><button class="ghost" @click="viewDataset(d.id)">明细</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="dsDetail" class="card">
      <h3>数据集 #{{ dsDetail.id }}「{{ dsDetail.name }}」
        <span :class="['tag', dsDetail.route]">{{ dsDetail.route }}</span>
        <span class="tag">目录 event#{{ dsDetail.catalog_version }}</span>
      </h3>
      <p class="muted">{{ dsDetail.route_reason }}</p>

      <div v-if="dsDetail.reviews.length" class="review-box">
        <b>待复核期间（目录被追溯修订，原月报依据保留）：</b>
        <div v-for="rv in dsDetail.reviews" :key="rv.id" class="review-item">
          <span class="tag PENDING">{{ rv.status }}</span>
          <span class="mono">{{ rv.periods.join(', ') }}</span>
          <span class="muted">{{ rv.reason }}</span>
        </div>
      </div>

      <h4>可比口径行（按 期间/门店/品类 聚合；待核行不参与任何平均与分摊）</h4>
      <table>
        <thead><tr><th>期间</th><th>门店</th><th>品类</th><th>监管</th><th>净额</th><th>基本数量</th><th>积分</th><th>行数</th></tr></thead>
        <tbody>
          <tr v-for="(r, i) in dsDetail.rows" :key="i">
            <td class="mono">{{ r.period }}</td><td class="mono">{{ r.store }}</td>
            <td>{{ r.category }}</td><td><span :class="['tag', r.reg_class]">{{ r.reg_class }}</span></td>
            <td class="mono">¥{{ r.net_amount }}</td><td class="mono">{{ r.qty_base }}</td>
            <td class="mono">{{ r.points }}</td><td class="mono">{{ r.line_count }}</td>
          </tr>
        </tbody>
      </table>

      <div class="grid cols-2" style="margin-top:12px">
        <div>
          <h4>下游 · 积分（按会员/期间）</h4>
          <table>
            <thead><tr><th>会员</th><th>期间</th><th>积分</th></tr></thead>
            <tbody>
              <tr v-for="(p, i) in dsDetail.detail.downstream.points" :key="i">
                <td class="mono">{{ p.member_key }}</td><td class="mono">{{ p.period }}</td><td class="mono">{{ p.points }}</td>
              </tr>
            </tbody>
          </table>
          <h4 style="margin-top:12px">下游 · 财务对账（按门店/期间）</h4>
          <table>
            <thead><tr><th>门店</th><th>期间</th><th>净额</th><th>行数</th></tr></thead>
            <tbody>
              <tr v-for="(f, i) in dsDetail.detail.downstream.finance_reconciliation" :key="i">
                <td class="mono">{{ f.store }}</td><td class="mono">{{ f.period }}</td>
                <td class="mono">¥{{ f.net_amount }}</td><td class="mono">{{ f.line_count }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4>下游 · 品类贡献</h4>
          <table>
            <thead><tr><th>品类</th><th>净额</th><th>贡献占比</th></tr></thead>
            <tbody>
              <tr v-for="(c, i) in dsDetail.detail.downstream.category_analysis" :key="i">
                <td>{{ c.category }}</td><td class="mono">¥{{ c.net_amount }}</td>
                <td>
                  <div class="contrib-bar"><div class="contrib-fill" :style="{width: c.contribution_pct + '%'}"></div></div>
                  <span class="mono">{{ c.contribution_pct }}%</span>
                </td>
              </tr>
            </tbody>
          </table>
          <h4 style="margin-top:12px">待核行（被排除，不参与聚合）</h4>
          <table>
            <thead><tr><th>销售行</th><th>商品码</th><th>标志</th></tr></thead>
            <tbody>
              <tr v-for="(p, i) in dsDetail.detail.pending_lines" :key="i">
                <td class="mono">{{ p.sale_id }}</td><td class="mono">{{ p.sku }}</td>
                <td><span class="tag bad-flag">{{ p.flag }}</span></td>
              </tr>
              <tr v-if="!dsDetail.detail.pending_lines.length"><td colspan="3" class="muted">无</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>可比性计算器（旧新目录口径：可比较集合 / 待排除范围 / 分类迁移量 / 分别归因）</h3>
      <div class="form-row">
        <label>期间</label>
        <input v-model="cmpForm.period_from" style="width:185px" placeholder="2026-02-01T00:00:00Z" />
        <span class="arrow">→</span>
        <input v-model="cmpForm.period_to" style="width:185px" placeholder="2026-03-01T00:00:00Z" />
        <label>旧口径水位</label>
        <input v-model="cmpForm.old_sys_time" style="width:185px" placeholder="2026-02-28T23:59:00Z" />
        <label>新口径水位</label>
        <input v-model="cmpForm.new_sys_time" style="width:185px" placeholder="2026-04-10T00:00:00Z" />
        <button @click="runCompare">计算</button>
      </div>
      <template v-if="cmp">
        <p>
          目录版本 <span class="tag">event#{{ cmp.old.catalog_version }}</span>
          <span class="arrow">→</span>
          <span class="tag">event#{{ cmp.new.catalog_version }}</span>
          积分影响：
          <span class="cause">分类变化 {{ cmp.points_impact.classification_change }}</span>
          <span class="cause">交易更正 {{ cmp.points_impact.transaction_correction }}</span>
          <span class="muted">（分别归因，互不重复计数）</span>
        </p>
        <div class="grid cols-2">
          <div>
            <h4>可比较集合（两口径结论一致，可直接对比）</h4>
            <table>
              <thead><tr><th>品类</th><th>监管</th><th>净额</th><th>积分</th><th>行数</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in cmp.comparable_set" :key="i">
                  <td>{{ c.category }}</td><td><span :class="['tag', c.reg_class]">{{ c.reg_class }}</span></td>
                  <td class="mono">¥{{ c.net_amount }}</td><td class="mono">{{ c.points }}</td><td class="mono">{{ c.line_count }}</td>
                </tr>
              </tbody>
            </table>
            <h4 style="margin-top:12px">待排除范围</h4>
            <table>
              <thead><tr><th>销售行</th><th>商品码</th><th>标志</th><th>说明</th></tr></thead>
              <tbody>
                <tr v-for="(e, i) in cmp.exclusions" :key="i">
                  <td class="mono">{{ e.sale_id }}</td><td class="mono">{{ e.sku }}</td>
                  <td><span class="tag bad-flag">{{ e.flag }}</span></td><td class="muted">{{ e.detail }}</td>
                </tr>
              </tbody>
            </table>
            <h4 style="margin-top:12px">分类迁移量（旧分类 → 新分类）</h4>
            <table>
              <thead><tr><th>从</th><th></th><th>到</th><th>净额</th><th>数量</th><th>商品码</th></tr></thead>
              <tbody>
                <tr v-for="(m, i) in cmp.migration" :key="i">
                  <td>{{ m.from_category }}/{{ m.from_reg_class }}</td><td class="arrow">→</td>
                  <td>{{ m.to_category }}/{{ m.to_reg_class }}</td>
                  <td class="mono">¥{{ m.net_amount }}</td><td class="mono">{{ m.qty_base }}</td>
                  <td class="mono">{{ m.skus.join(', ') }}</td>
                </tr>
                <tr v-if="!cmp.migration.length"><td colspan="6" class="muted">无迁移</td></tr>
              </tbody>
            </table>
          </div>
          <div>
            <h4>分类变化（目录修订/撤回驱动）</h4>
            <table>
              <thead><tr><th>销售行</th><th>前</th><th>后</th><th>积分Δ</th><th>原因事件</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in cmp.classification_changes" :key="i">
                  <td class="mono">{{ c.sale_id }}</td>
                  <td class="mono">{{ JSON.stringify(c.before.signature) }}</td>
                  <td class="mono">{{ JSON.stringify(c.after.signature) }}</td>
                  <td class="mono">{{ c.points_delta }}</td>
                  <td class="mono">{{ c.cause_event_ids.map(e => '#' + e).join(' ') }}</td>
                </tr>
                <tr v-if="!cmp.classification_changes.length"><td colspan="5" class="muted">无</td></tr>
              </tbody>
            </table>
            <h4 style="margin-top:12px">交易更正（单位/折扣事实补传驱动）</h4>
            <table>
              <thead><tr><th>销售行</th><th>前</th><th>后</th><th>积分Δ</th><th>原因事件</th></tr></thead>
              <tbody>
                <tr v-for="(c, i) in cmp.transaction_corrections" :key="i">
                  <td class="mono">{{ c.sale_id }}</td>
                  <td class="mono">净{{ c.before?.net_amount ?? '—' }} 积分{{ c.before?.points ?? '—' }}</td>
                  <td class="mono">净{{ c.after?.net_amount ?? '—' }} 积分{{ c.after?.points ?? '—' }}</td>
                  <td class="mono">{{ c.points_delta }}</td>
                  <td class="mono">{{ c.cause_event_ids.map(e => '#' + e).join(' ') }}</td>
                </tr>
                <tr v-if="!cmp.transaction_corrections.length"><td colspan="5" class="muted">无</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const datasets = ref([])
const dsDetail = ref(null)
const dsForm = ref({
  name: '', period_from: '2026-02-01T00:00:00Z', period_to: '2026-03-01T00:00:00Z', route: 'AUTO',
})
const cmpForm = ref({
  period_from: '2026-02-01T00:00:00Z', period_to: '2026-03-01T00:00:00Z',
  old_sys_time: '2026-02-28T23:59:00Z', new_sys_time: '2026-04-10T00:00:00Z',
})
const cmp = ref(null)

async function load() {
  datasets.value = await api.get('/datasets')
}
async function createDataset() {
  const ds = await api.post('/datasets', dsForm.value)
  dsForm.value.name = ''
  await load()
  await viewDataset(ds.id)
}
async function viewDataset(id) {
  dsDetail.value = await api.get(`/datasets/${id}`)
}
async function runCompare() {
  cmp.value = await api.post('/comparability', cmpForm.value)
}
onMounted(load)
</script>

<style scoped>
.review-box { background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.review-item { display: flex; gap: 8px; align-items: center; margin-top: 6px; font-size: 13px; }
.contrib-bar { display: inline-block; width: 120px; height: 8px; background: #e2e8f0; border-radius: 4px; margin-right: 8px; vertical-align: middle; overflow: hidden; }
.contrib-fill { height: 100%; background: #2563eb; }
h4 { font-size: 14px; margin: 8px 0; }
</style>
