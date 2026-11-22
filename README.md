# 双时间会员资格历史服务 + 交易分类历史映射与可比数据服务（连锁药房）

面向会员分析与商品主数据团队：
1. **基础服务（冻结）**：在任意历史时点解释会员为何获得/失去等级资格，为营销、门店预算、
   模型训练提供「当时可见」「事后补全」「二者差异」三种口径的数据集。
2. **本迭代新增**：让营销与财务按**交易发生时的货品语义**比较积分与品类贡献——
   药品换码、套装拆分、OTC 转处方、单位换算不再让旧月报无法解释。

技术栈：Vue 3 + FastAPI + PostgreSQL。

## 架构选型

基础服务与本服务共用同一 **追加式双时间事件账 + 可重建投影**：

- `events` 接入账是唯一事实来源：追加-only（DB 触发器禁止 UPDATE/DELETE），每行保存
  **来源哈希**、**链式哈希**、**两类时间**（`biz_time` / `ingested_at`）、**修订链**、**幂等键**。
- 身份关系图、规则版本、会员名录、**目录版本、映射切片、校验标志**均为可重建投影，
  `POST /api/admin/rebuild` 可全量重建并验证结论一致。

### 交易分类历史映射：两条路线都实现，发布时按场景选择

题目给出的两条路线本实现**全部落地**，共享同一解析核心（`resolve_line`），
因此同一期间必然输出**相同的可比范围**（验证脚本逐行断言两路线行级一致）：

| | 路线 A：MATERIALIZED（物化切片） | 路线 B：INTERPRETED（查询时解释） |
| --- | --- | --- |
| 机制 | 目录/销售事件到达时预物化 `mapping_slices`（带有效区间），目录修订仅闭合并重开**生效区间相交**的切片 | 不预计算，查询/发布时按历史目录版本逐行解释每笔销售行 |
| 优势 | 月度/门店分析直接读切片，读取快 | 无预计算与修订写放大；账本天然归档全部目录版本 |
| 代价 | 换码链/监管分类追溯修订时要重建更多切片（写放大） | 大范围历史查询逐行解析，成本高；要求长期归档全部目录版本 |
| 适用 | 目录稳定、历史分析并发高 | 目录修订频繁、存储预算紧 |

- **选择依据**：发布数据集时 `route=AUTO` 按可观测指标启发式选择——目录修订频率
  （次/月，阈值 2）与销售行量（阈值 50000）任一超限即选 INTERPRETED（避免切片写放大），
  否则选 MATERIALIZED（读快）；也可人工指定。选择理由随数据集保存。
- **每份已发布数据集都显示所用路线与目录版本**（`route` + `catalog_version` = 冻结的最大目录事件 id），
  另冻结映射引用（路线 A=切片水位 / 路线 B=销售行水位）、规则集合版本与证据水位。

## 核心组件

| 组件 | 实现 |
| --- | --- |
| 接入账 | `events` 表：追加-only、来源哈希、链式哈希、双时间、修订链、幂等键 |
| 身份关系图 | `identity_edges` 投影：合并/拆分/待核/撤销的双时间有效区间 |
| 时间语义目录 | `semantics` 表：`AS_OF` / `AS_KNOWN_NOW` / `DELTA` |
| 资格计算器 | 按业务时点、系统时点、规则版本、身份簇即时重放计算等级 |
| 发布库 | `publications`：冻结证据水位、身份图版本、规则集版本 |
| 差异归因器 | 比较两个发布，把变化归因到水位之间的具体事件 |
| 影响索引器 | 把等级变化映射到营销名单、门店预算、训练样本 |
| **目录接收器** | `catalog_items`：商品码、包装层级、单位换算、监管分类、品类、换码链、门店可售、生效区间的来源版本；`catalog_withdrawals`：追溯撤回（可只覆盖部分生效期） |
| **映射解析器** | 按交易日、门店可售状态、规则版本把销售行关联到历史分类；路线 A 物化 `mapping_slices`（带有效区间），路线 B 查询时解释；同一解析核心 |
| **口径校验器** | `validation_flags`：一对多映射（缺分摊依据，**不平均分配**）、失效换码、缺单位/折扣事实、门店不可售等无法自动归类的记录列入待核 |
| **可比性计算器** | 旧新目录口径间生成可比较集合、待排除范围、分类迁移量；分类变化与交易更正**分别归因**（积分影响不重复计数） |
| **分析数据集发布器** | `analytics_datasets`：冻结目录/映射/规则引用，向积分、品类分析、财务对账交付；目录被追溯修订时登记**待复核期间**且**原月报依据保留** |

### 目录版本解析语义（确定性）

- 同一 sku 的多个来源版本按 `(effective_from, version)` 全序排列；
- 版本 v 的**派生生效区间** = `[v.effective_from, min(v.effective_to, 未在 T_b 被撤回的后序版本的最小 effective_from, 可见后继新码的最早生效起点))`；
  —— 撤回后序版本时前序版本的派生区间自然延伸覆盖缺口（**回落语义**）；
  换码退役点由可见后继版本派生，不改写旧版本行（不把未来事实泄漏到过去的系统时点口径）；
- 撤回 `(sku, 目标版本, [withdraw_from, withdraw_to))` 只在该业务区间内剔除目标版本，
  **解析器仅重审该区间的交易**（切片闭合留痕，区间外不动）；
- 候选中取 `(effective_from, version)` 最大者；门店须在可售范围内。

### 积分资格口径（确定性）

销售行解析结论为 OK（无待核标志）、监管分类 ∈ {OTC, 双跨, 保健}（处方药 RX 不计积分）、
且交易日适用规则版本的商品范围覆盖该品类时计积分；积分 = 净额（总额 − 折扣）取整。
单位或折扣事实补传改变积分资格时，可比性计算器将其归入**交易更正**桶，
与目录修订驱动的**分类变化**桶分别归因。

## 快速启动

### 方式一：Docker Compose（一键）

```bash
docker compose up --build
```

- 前端：http://localhost:8080
- 后端 API：http://localhost:8000 （Swagger UI: http://localhost:8000/docs）
- 数据库：localhost:15432（ccdev/ccdev，库 member_bitemp）

compose 启动时 `AUTO_SEED=1` 会自动灌入演示场景（仅当账本为空）。

### 方式二：本地开发

前置：容器内 PostgreSQL（127.0.0.1:5432，ccdev/ccdev）已运行。

```bash
# 后端（首次会自动创建 member_bitemp 库与表结构）
cd backend
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 前端（另开终端）
cd frontend
/opt/node-22/bin/npm install
/opt/node-22/bin/npm run dev        # http://localhost:5173，已配置 /api 代理

# 灌入演示数据（或在前端「总览」页点击"灌入演示场景"）
curl -X POST http://127.0.0.1:8000/api/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}'
curl -X POST http://127.0.0.1:8000/api/seed -H 'Authorization: Bearer token-admin-2026'
```

## 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| admin | admin123 | 管理员 |
| analyst | analyst123 | 分析师 |

## 演示场景（种子数据）

### 基础服务场景（会员资格）

同一会员 A（13800000001）贯穿四类历史变更，另有多名对照会员：

1. **加盟店离线补传**：交易 T4 业务时间 2026-01-28，2026-03-15 才到达平台；
2. **误合并后拆分**：2026-02-15 人工裁定把 B 并入 A，2026-04-05 追溯拆分并撤销裁定；
3. **部分追溯规则撤回**：规则 v2（2026-02-01 起追溯生效，GOLD 阈值 1000→1500）于
   2026-04-10 仅对门店 S002 撤回；
4. **并发修订**：v2、v3 相隔 5 分钟先后修订 v1（另有交易 T3 的更正链 300→380）。

关键结论（验证脚本逐项断言）：

| 查询 | 当时可见 (AS_OF) | 事后补全 (AS_KNOWN_NOW) |
| --- | --- | --- |
| A @ 2026-01-31 | SILVER（¥900） | **GOLD**（¥1250，补传改判） |
| A @ 2026-02-28 | GOLD（¥1800，簇 {A,B}） | **SILVER**（v2 范围 ¥1280 + S002 撤回后 v1 范围 ¥350） |
| A @ 2026-03-31 | GOLD（¥2230） | GOLD（¥1630，构成不同） |
| D @ 2026-02-28 | GOLD | GOLD（**被 S002 撤回保护**，否则落入 v2 的 1500 线） |
| F @ 2026-02-28 | GOLD | **SILVER**（被 v2 追溯阈值击中） |

### 商品主数据场景（交易分类历史映射）

覆盖题目要求的五类验收样本：

| 样本 | 场景 |
| --- | --- |
| **换码** | IBU-100 → IBU-200（03-01 起）；旧码退役后 POS-0006 仍用旧码 → 失效换码待核 |
| **追溯撤回** | COM-500 二月起转 RX（v2），04-01 撤回 v2 的 [02-01, 03-01) 段：仅重审该区间交易，POS-0003（02-10）回落 v1 OTC、积分资格恢复；1 月的 POS-0002 不受影响 |
| **套装一对多** | BUNDLE-1 跨品类且**无分摊依据** → 待核（不平均分配）；BUNDLE-2 带目录价格权重（0.7/0.3）→ 自动分摊 |
| **补传折扣** | POS-0004 缺折扣事实 → 待核；03-10 补传后净额 450、积分资格恢复（可比性归入交易更正） |
| **高频目录修订** | VIT-7 六周内四次修订（品类细化/包装规格 60→90 片/可售门店收缩）；验证中再追加第五次追溯修订（维矿补充→膳食营养，生效自 02-10）触发已发布月报的待复核 |

另有对照样本：AMOX-750（RX 不计积分）、POS-0013（单位「托盘」无换算 → 待核）、
POS-0011（v4 仅 S001 可售，S002 销售 → 待核）。

## 验证方式

### 一键端到端验证（82 项断言）

```bash
# 后端运行中执行：
python3 scripts/verify.py            # 默认 http://127.0.0.1:8000
python3 scripts/verify.py http://localhost:8000
```

脚本会重置业务数据、灌入种子，然后断言：

- **基础服务**：任意时点资格（上表全部结论）、更正链前后口径、DELTA 语义、发布冻结水位、
  差异归因（迟到交易/更正/身份拆分/规则修订/规则撤回）、下游影响、链式哈希完整性、
  幂等重放、追加-only 触发器、并发修订确定性解决；
- **目录版本**：高频修订的四个来源版本、单位换算与可售门店随版本变化、换码链、
  部分生效期撤回；
- **映射结论**：同一销售行在不同系统时点的历史分类（03-01 水位 = RX，当前 = OTC 回落）、
  撤回区间外交易不被重审、换码后新码正常解析；
- **口径校验**：失效换码/套装一对多/缺单位/不可售列入待核，一对多**不平均分配**
  （净额留空），折扣补传后标志解决、积分资格恢复；
- **可比范围**：可比较集合、待排除范围、分类迁移量（感冒用药 RX→OTC 50 元；
  维矿补充→膳食营养 48 元）、分类变化（+50 分）与交易更正（+450 分）**分别归因**；
- **数据集**：两路线同一期间行级一致、每份数据集显示路线与目录版本、
  待核行不参与聚合、下游积分/品类贡献/财务对账口径、AUTO 路线选择；
- **下游影响可追查**：目录追溯修订 → 三份已发布数据集登记待复核期间（2026-02），
  **原月报行不被修改**，复核后新数据集反映新口径；
- **投影重建一致性**：45 事件重放后映射切片签名逐条一致、解析结论一致、待复核期间仍在。

**本仓库交付前已实际执行：`82 通过, 0 失败`。**

### 手工验证示例

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 任意双时点资格：A 在 2026-02-28 的两种口径
curl -s "http://127.0.0.1:8000/api/members/13800000001/eligibility?biz_time=2026-02-28T23:59:00Z&semantics=DELTA&sys_time=2026-02-28T23:59:00Z" \
  -H "Authorization: Bearer $TOKEN"

# 同一销售行在两个系统时点的历史分类（追溯撤回前 RX / 撤回后 OTC 回落）
curl -s "http://127.0.0.1:8000/api/mapping/resolve?sale_id=POS-0003&sys_time=2026-03-01T00:00:00Z" -H "Authorization: Bearer $TOKEN"
curl -s "http://127.0.0.1:8000/api/mapping/resolve?sale_id=POS-0003" -H "Authorization: Bearer $TOKEN"

# 可比性：2026-02 期间，2 月末口径 vs 4 月口径
curl -s -X POST http://127.0.0.1:8000/api/comparability -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"period_from":"2026-02-01T00:00:00Z","period_to":"2026-03-01T00:00:00Z","old_sys_time":"2026-02-28T23:59:00Z","new_sys_time":"2026-04-10T00:00:00Z"}'

# 账本完整性
curl -s http://127.0.0.1:8000/api/admin/ledger/verify -H "Authorization: Bearer $TOKEN"

# 追加-only：直接改账应被拒绝
psql postgresql://ccdev:ccdev@127.0.0.1:5432/member_bitemp -c "UPDATE events SET actor='x' WHERE id=1"
# => ERROR: events 账为追加式：禁止 UPDATE，请以新事件修订
```

前端页面同样可完成全部验证：总览（语义目录/重建投影）、接入账（写入与查看哈希链）、
资格查询（三种口径 + 时间线）、身份关系图、规则中心、发布与差异、
**目录中心**（来源版本/换码链/撤回）、**映射与校验**（单行即时解释 + 待核队列 + 物化切片）、
**数据集与可比**（发布数据集选路线 → 查看下游口径与待复核期间 → 可比性计算）。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/login | 登录取令牌 |
| POST | /api/events | 写入事件（幂等键去重） |
| GET | /api/events | 账本流水查询 |
| GET | /api/semantics | 时间语义目录 |
| GET | /api/members | 会员名录 |
| GET | /api/members/{key}/eligibility | 任意双时点资格（semantics=AS_OF/AS_KNOWN_NOW/DELTA） |
| GET | /api/members/{key}/timeline | 等级时间线 |
| GET | /api/members/{key}/cluster | 任意时点身份簇 |
| GET | /api/identity/edges | 身份关系边（双时间区间） |
| GET | /api/rules/versions · /api/rules/withdrawals | 规则版本与撤回 |
| POST/GET | /api/publications | 冻结发布（证据水位/身份图/规则集版本） |
| POST/GET | /api/diffs | 差异归因 |
| GET | /api/impacts | 下游影响（可按 dimension/entity 反查） |
| GET | /api/catalog/items · /api/catalog/withdrawals | 目录来源版本与撤回 |
| GET | /api/catalog/chain/{sku} | 换码链（前驱/后继/版本） |
| GET | /api/mapping/slices | 物化映射切片（含重审留痕；current=true 仅当前） |
| GET | /api/mapping/resolve?sale_id=&sys_time= | 单行即时解释（路线 B，任意系统时点） |
| GET | /api/validation/flags?status= | 口径校验待核队列 |
| POST | /api/comparability | 可比性计算（可比集合/排除范围/迁移量/分别归因） |
| POST/GET | /api/datasets | 发布分析数据集（route=AUTO/MATERIALIZED/INTERPRETED） |
| GET | /api/datasets/{id} | 数据集行 + 下游口径 + 待复核期间 |
| GET | /api/reviews?dataset_id= | 待复核期间列表 |
| POST | /api/admin/rebuild | 从事件账重建全部投影 |
| GET | /api/admin/ledger/verify | 链式哈希校验 |
| POST | /api/admin/reset | 清空业务数据（演示用） |

完整交互式文档见 `/docs`（Swagger UI，FastAPI 自动生成）。

## 设计红线

- **不得以当前余额或当前主档覆盖历史**：系统不保存"当前等级/当前余额/当前目录"字段；
  任何时点的资格与分类都由事件账按双时间口径重放得出。误合并拆分、规则撤回、
  目录追溯修订等更正只追加新事件并闭合旧区间，历史行永不修改、永不删除。
- **待核不猜测**：套装一对多缺少可验证分摊依据时列为待核，绝不平均分配；
  缺单位/折扣事实的记录不参与任何聚合，待补传后自动重归类。
- **已发布即冻结**：分析数据集发布后其行永不修改（原月报依据）；
  目录被追溯修订时登记待复核期间，复核 = 按新目录版本发布新数据集。
