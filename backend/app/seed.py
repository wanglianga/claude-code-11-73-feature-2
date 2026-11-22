"""演示种子场景（全部通过接入账写入，幂等键固定，可重复执行）。

同一会员 A(13800000001) 贯穿四类历史变更：
1. 加盟店离线补传：T4 业务时间 2026-01-28，平台 2026-03-15 才收到；
2. 误合并后拆分：2026-02-15 人工裁定把 B 并入 A，2026-04-05 追溯拆分 + 撤销；
3. 部分追溯规则撤回：规则 v2（2026-02-01 起追溯生效，GOLD 阈值 1000→1500）
   于 2026-04-10 仅对门店 S002 撤回；
4. 并发修订：v2 与 v3 在 2026-03-01 相隔 5 分钟先后修订 v1，
   由 (effective_from, version) 全序确定性解决，修订链完整保留。
另有更正链：T3 金额 300 → 380（2026-03-05 补录）。

商品主数据场景（CATALOG_SEED_EVENTS，交易分类历史映射验收样本）：
5. 换码：IBU-100 → IBU-200（03-01 起），旧码退役后仍有销售使用 → 失效换码待核；
6. 追溯撤回：COM-500 二月起转 RX（v2），04-01 撤回 v2 的 [02-01, 03-01) 段，
   仅重审该区间交易，2 月回落 v1 OTC；
7. 套装一对多：BUNDLE-1 跨品类且无分摊依据 → 待核（不平均分配）；
   BUNDLE-2 带目录价格权重 → 自动分摊；
8. 补传折扣：POS-0004 缺折扣事实 → 待核，03-10 补传后积分资格改变（交易更正归因）；
9. 高频目录修订：VIT-7 六周内四次修订（品类细化/包装规格/可售门店），
   另有缺单位换算（托盘）与门店不可售样本。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .services.ledger import ingest_event


def T(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


SEED_EVENTS = [
    # ---- 交易：会员 A ----
    dict(idem="seed-t1", source="pos", type="transaction", biz="2026-01-05T10:20:00", ing="2026-01-05T10:20:05",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "OTC", "amount": 500, "channel": "pos"}),
    dict(idem="seed-t2", source="online", type="transaction", biz="2026-01-20T21:00:00", ing="2026-01-20T21:00:03",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "ONLINE", "category": "保健", "amount": 400, "channel": "app"}),
    dict(idem="seed-t3", source="pos", type="transaction", biz="2026-02-10T12:00:00", ing="2026-02-10T12:00:04",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "RX", "amount": 300, "channel": "pos"}),
    # 更正：T3 金额 300 -> 380（修订链 supersedes）
    dict(idem="seed-t3-fix", source="pos", type="transaction", biz="2026-02-10T12:00:00", ing="2026-03-05T09:00:00",
         supersedes="seed-t3",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S001", "category": "RX", "amount": 380, "channel": "pos", "comment": "收银错单更正"}),
    # 加盟店离线补传：业务时间 2026-01-28，2026-03-15 才到达平台
    dict(idem="seed-t4", source="franchise_offline", type="transaction", biz="2026-01-28T15:30:00", ing="2026-03-15T08:00:00",
         payload={"member_key": "13800000001", "member_name": "张伟", "store": "S002", "category": "OTC", "amount": 350, "channel": "offline_batch", "batch": "S002-2026-03"}),
    # ---- 其他会员 ----
    dict(idem="seed-t5", source="pos", type="transaction", biz="2026-02-01T11:00:00", ing="2026-02-01T11:00:02",
         payload={"member_key": "13900000002", "member_name": "李芳", "store": "S001", "category": "OTC", "amount": 600, "channel": "pos"}),
    dict(idem="seed-t9", source="pos", type="transaction", biz="2026-02-15T16:00:00", ing="2026-02-15T16:00:02",
         payload={"member_key": "13900000003", "member_name": "王强", "store": "S001", "category": "OTC", "amount": 100, "channel": "pos"}),
    dict(idem="seed-t7", source="pos", type="transaction", biz="2026-02-05T10:00:00", ing="2026-02-05T10:00:02",
         payload={"member_key": "13900000004", "member_name": "赵敏", "store": "S002", "category": "OTC", "amount": 1100, "channel": "pos"}),
    dict(idem="seed-t8", source="pos", type="transaction", biz="2026-02-03T10:00:00", ing="2026-02-03T10:00:02",
         payload={"member_key": "13900000005", "member_name": "孙磊", "store": "S001", "category": "RX", "amount": 1200, "channel": "pos"}),
    dict(idem="seed-t10", source="pos", type="transaction", biz="2026-02-08T10:00:00", ing="2026-02-08T10:00:02",
         payload={"member_key": "13900000006", "member_name": "钱进", "store": "S002", "category": "OTC", "amount": 1400, "channel": "pos"}),
    # ---- 规则：v1 ----
    dict(idem="seed-r1", source="rule_admin", type="rule_upsert", biz="2026-01-01T00:00:00", ing="2026-01-01T00:00:00",
         payload={"rule_id": "tier-standard", "version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1000}, "effective_from": "2026-01-01T00:00:00+00:00"}),
    # ---- 身份：误合并（人工裁定）----
    dict(idem="seed-m1", source="identity_desk", type="identity_merge", biz="2026-02-15T09:00:00", ing="2026-02-15T09:00:00",
         payload={"primary_key": "13800000001", "secondary_key": "13900000002", "reason": "人工裁定：疑似同一人（事后证实为误合并）", "adjudicator": "ops-01"}),
    # ---- 并发规则修订：v2、v3 相隔 5 分钟先后修订 v1 ----
    dict(idem="seed-r2", source="rule_admin", type="rule_upsert", biz="2026-03-01T09:00:00", ing="2026-03-01T09:00:00",
         payload={"rule_id": "tier-standard", "version": 2, "supersedes_version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1500}, "effective_from": "2026-02-01T00:00:00+00:00",
                  "comment": "追溯修订：2 月起 GOLD 阈值上调"}),
    dict(idem="seed-r3", source="rule_admin", type="rule_upsert", biz="2026-03-01T09:05:00", ing="2026-03-01T09:05:00",
         payload={"rule_id": "tier-standard", "version": 3, "supersedes_version": 1, "store_scope": "ALL", "product_scope": "ALL",
                  "window_days": 90, "thresholds": {"SILVER": 300, "GOLD": 1300}, "effective_from": "2026-03-01T00:00:00+00:00",
                  "comment": "并发修订：3 月起 GOLD 阈值 1300"}),
    # ---- 身份：待核（不影响资格）----
    dict(idem="seed-p1", source="identity_desk", type="identity_pending", biz="2026-03-20T10:00:00", ing="2026-03-20T10:00:00",
         payload={"key_a": "13800000001", "key_b": "13900000003", "reason": "手机号相似，待核"}),
    # ---- 身份：误合并追溯拆分 + 撤销 ----
    dict(idem="seed-s1", source="identity_desk", type="identity_split", biz="2026-04-05T09:00:00", ing="2026-04-05T09:00:00",
         payload={"primary_key": "13800000001", "secondary_key": "13900000002", "merge_event_key": "seed-m1",
                  "retroactive": True, "reason": "误合并拆分：双方确认为不同自然人"}),
    dict(idem="seed-v1", source="identity_desk", type="identity_revoke", biz="2026-04-05T10:00:00", ing="2026-04-05T10:00:00",
         payload={"target_event_key": "seed-m1", "reason": "撤销申请：原人工裁定证据不足"}),
    # ---- 规则：部分追溯撤回（仅门店 S002）----
    dict(idem="seed-r4", source="rule_admin", type="rule_withdraw", biz="2026-04-10T10:00:00", ing="2026-04-10T10:00:00",
         payload={"rule_id": "tier-standard", "target_version": 2, "store_scope": ["S002"],
                  "withdraw_from": "2026-02-01T00:00:00+00:00",
                  "reason": "加盟店 S002 促销口径争议，v2 对该门店追溯撤回"}),
]


# ==================================================================
# 交易分类历史映射演示场景（商品主数据）：目录来源版本 + 销售行。
# 覆盖验收样本：换码、追溯撤回（部分生效期）、套装一对多、补传折扣、高频目录修订。
# ==================================================================
CATALOG_SEED_EVENTS = [
    # ---- 目录来源版本（目录接收器）----
    dict(idem="seed-cat-ibu100", source="catalog_admin", type="catalog_upsert", biz="2026-01-01T00:00:00", ing="2026-01-01T00:00:00",
         payload={"sku": "IBU-100", "name": "布洛芬缓释胶囊 0.3g", "base_unit": "粒",
                  "pack_hierarchy": [{"unit": "盒", "contains": 12}, {"unit": "箱", "contains": 20, "of": "盒"}],
                  "unit_conversions": {"盒": 12, "箱": 240}, "reg_class": "OTC", "category": "解热镇痛",
                  "store_scope": "ALL", "effective_from": "2026-01-01T00:00:00+00:00"}),
    dict(idem="seed-cat-com500-v1", source="catalog_admin", type="catalog_upsert", biz="2026-01-01T00:00:00", ing="2026-01-01T00:00:00",
         payload={"sku": "COM-500", "name": "复方氨酚烷胺片", "base_unit": "片",
                  "pack_hierarchy": [{"unit": "盒", "contains": 10}],
                  "unit_conversions": {"盒": 10}, "reg_class": "OTC", "category": "感冒用药",
                  "store_scope": "ALL", "effective_from": "2026-01-01T00:00:00+00:00"}),
    dict(idem="seed-cat-vit7-v1", source="catalog_admin", type="catalog_upsert", biz="2026-01-01T00:00:00", ing="2026-01-01T00:00:00",
         payload={"sku": "VIT-7", "name": "维生素C咀嚼片", "base_unit": "片",
                  "pack_hierarchy": [{"unit": "瓶", "contains": 60}],
                  "unit_conversions": {"瓶": 60}, "reg_class": "保健", "category": "保健",
                  "store_scope": "ALL", "effective_from": "2026-01-01T00:00:00+00:00"}),
    dict(idem="seed-cat-amox", source="catalog_admin", type="catalog_upsert", biz="2026-01-01T00:00:00", ing="2026-01-01T00:00:00",
         payload={"sku": "AMOX-750", "name": "阿莫西林胶囊", "base_unit": "粒",
                  "pack_hierarchy": [{"unit": "盒", "contains": 20}],
                  "unit_conversions": {"盒": 20}, "reg_class": "RX", "category": "抗感染",
                  "store_scope": "ALL", "effective_from": "2026-01-01T00:00:00+00:00"}),
    # 套装一对多（无可验证分摊依据 → 校验器待核，不平均分配）
    dict(idem="seed-cat-bundle1", source="catalog_admin", type="catalog_upsert", biz="2026-01-15T00:00:00", ing="2026-01-15T00:00:00",
         payload={"sku": "BUNDLE-1", "name": "家庭常备套装", "base_unit": "套", "unit_conversions": {},
                  "reg_class": "OTC", "category": "套装", "store_scope": "ALL",
                  "bundle_components": [
                      {"sku": "IBU-100", "category": "解热镇痛", "reg_class": "OTC", "qty": 1},
                      {"sku": "VIT-7", "category": "维矿补充", "reg_class": "保健", "qty": 1},
                  ],
                  "effective_from": "2026-01-15T00:00:00+00:00"}),
    # 高频目录修订样本：VIT-7 连续修订（品类细化 / 包装规格 / 可售门店）
    dict(idem="seed-cat-vit7-v2", source="catalog_admin", type="catalog_upsert", biz="2026-01-20T00:00:00", ing="2026-01-20T00:00:00",
         payload={"sku": "VIT-7", "name": "维生素C咀嚼片", "base_unit": "片",
                  "pack_hierarchy": [{"unit": "瓶", "contains": 60}],
                  "unit_conversions": {"瓶": 60}, "reg_class": "保健", "category": "维矿补充",
                  "store_scope": "ALL", "effective_from": "2026-01-20T00:00:00+00:00",
                  "comment": "品类细化：保健 → 维矿补充"}),
    # 套装一对多（带目录价格分摊依据 → 自动按权重分摊）
    dict(idem="seed-cat-bundle2", source="catalog_admin", type="catalog_upsert", biz="2026-02-01T00:00:00", ing="2026-02-01T00:00:00",
         payload={"sku": "BUNDLE-2", "name": "儿童护理套装", "base_unit": "套", "unit_conversions": {},
                  "reg_class": "OTC", "category": "套装", "store_scope": "ALL",
                  "bundle_components": [
                      {"sku": "COM-500", "category": "感冒用药", "reg_class": "OTC", "qty": 2},
                      {"sku": "SPR-1", "category": "皮肤护理", "reg_class": "OTC", "qty": 1},
                  ],
                  "allocation_basis": {"type": "catalog_price", "weights": {"感冒用药": 0.7, "皮肤护理": 0.3}},
                  "effective_from": "2026-02-01T00:00:00+00:00"}),
    # OTC 转处方：监管重分类追溯生效（2 月起 RX）
    dict(idem="seed-cat-com500-v2", source="catalog_admin", type="catalog_upsert", biz="2026-02-01T00:00:00", ing="2026-02-01T00:00:00",
         payload={"sku": "COM-500", "name": "复方氨酚烷胺片", "base_unit": "片",
                  "pack_hierarchy": [{"unit": "盒", "contains": 10}],
                  "unit_conversions": {"盒": 10}, "reg_class": "RX", "category": "感冒用药",
                  "store_scope": "ALL", "effective_from": "2026-02-01T00:00:00+00:00",
                  "comment": "含麻黄碱复方制剂转处方药管理，追溯自 2026-02-01"}),
    dict(idem="seed-cat-vit7-v3", source="catalog_admin", type="catalog_upsert", biz="2026-02-05T00:00:00", ing="2026-02-05T00:00:00",
         payload={"sku": "VIT-7", "name": "维生素C咀嚼片", "base_unit": "片",
                  "pack_hierarchy": [{"unit": "瓶", "contains": 90}],
                  "unit_conversions": {"瓶": 90}, "reg_class": "保健", "category": "维矿补充",
                  "store_scope": "ALL", "effective_from": "2026-02-05T00:00:00+00:00",
                  "comment": "包装规格变更：60 片/瓶 → 90 片/瓶"}),
    dict(idem="seed-cat-vit7-v4", source="catalog_admin", type="catalog_upsert", biz="2026-02-18T00:00:00", ing="2026-02-18T00:00:00",
         payload={"sku": "VIT-7", "name": "维生素C咀嚼片", "base_unit": "片",
                  "pack_hierarchy": [{"unit": "瓶", "contains": 90}],
                  "unit_conversions": {"瓶": 90}, "reg_class": "保健", "category": "维矿补充",
                  "store_scope": ["S001"], "effective_from": "2026-02-18T00:00:00+00:00",
                  "comment": "渠道调整：仅 S001 可售"}),
    # 换码：IBU-100 → IBU-200（旧码自新码生效起退役）
    dict(idem="seed-cat-ibu200", source="catalog_admin", type="catalog_upsert", biz="2026-02-25T00:00:00", ing="2026-02-25T00:00:00",
         payload={"sku": "IBU-200", "name": "布洛芬缓释胶囊 0.3g（新码）", "base_unit": "粒",
                  "pack_hierarchy": [{"unit": "盒", "contains": 12}, {"unit": "箱", "contains": 20, "of": "盒"}],
                  "unit_conversions": {"盒": 12, "箱": 240}, "reg_class": "OTC", "category": "解热镇痛",
                  "supersedes_code": "IBU-100", "store_scope": "ALL",
                  "effective_from": "2026-03-01T00:00:00+00:00"}),
    # 追溯撤回（只覆盖部分生效期）：监管决定部分撤销，2 月仍按 OTC，3 月起 RX
    dict(idem="seed-cat-com500-wd", source="catalog_admin", type="catalog_withdraw", biz="2026-04-01T10:00:00", ing="2026-04-01T10:00:00",
         payload={"sku": "COM-500", "target_event_key": "seed-cat-com500-v2",
                  "withdraw_from": "2026-02-01T00:00:00+00:00", "withdraw_to": "2026-03-01T00:00:00+00:00",
                  "reason": "监管决定部分撤销：2026-02 月仍按 OTC 管理，03-01 起 RX 生效"}),
    # ---- 销售行（映射解析器输入）----
    dict(idem="seed-sl1", source="pos", type="sale_line", biz="2026-01-10T10:00:00", ing="2026-01-10T10:00:05",
         payload={"sale_id": "POS-0001", "line_no": 1, "store": "S001", "sku": "IBU-100", "qty": 2, "unit": "盒",
                  "gross_amount": 60, "discount_amount": 5, "member_key": "13800000001"}),
    dict(idem="seed-sl2", source="pos", type="sale_line", biz="2026-01-20T11:00:00", ing="2026-01-20T11:00:03",
         payload={"sale_id": "POS-0002", "line_no": 1, "store": "S001", "sku": "COM-500", "qty": 1, "unit": "盒",
                  "gross_amount": 25, "discount_amount": 0, "member_key": "13800000001"}),
    # 追溯撤回样本：02-10 销售，先按 v2 RX，撤回后回落 v1 OTC
    dict(idem="seed-sl3", source="pos", type="sale_line", biz="2026-02-10T12:00:00", ing="2026-02-10T12:00:04",
         payload={"sale_id": "POS-0003", "line_no": 1, "store": "S001", "sku": "COM-500", "qty": 2, "unit": "盒",
                  "gross_amount": 50, "discount_amount": 0, "member_key": "13800000001"}),
    # 补传折扣样本：缺少折扣事实 → 待核；03-10 补传后积分资格改变
    dict(idem="seed-sl4", source="franchise_offline", type="sale_line", biz="2026-02-20T15:00:00", ing="2026-02-20T15:00:06",
         payload={"sale_id": "POS-0004", "line_no": 1, "store": "S002", "sku": "IBU-100", "qty": 1, "unit": "箱",
                  "gross_amount": 500, "member_key": "13900000002", "comment": "加盟店批量上传，折扣事实缺失"}),
    dict(idem="seed-sl4-fix", source="franchise_offline", type="sale_line", biz="2026-02-20T15:00:00", ing="2026-03-10T09:00:00",
         supersedes="seed-sl4",
         payload={"sale_id": "POS-0004", "line_no": 1, "store": "S002", "sku": "IBU-100", "qty": 1, "unit": "箱",
                  "gross_amount": 500, "discount_amount": 50, "member_key": "13900000002", "comment": "补传折扣事实"}),
    dict(idem="seed-sl5", source="pos", type="sale_line", biz="2026-03-05T10:00:00", ing="2026-03-05T10:00:02",
         payload={"sale_id": "POS-0005", "line_no": 1, "store": "S001", "sku": "IBU-200", "qty": 3, "unit": "盒",
                  "gross_amount": 95, "discount_amount": 5, "member_key": "13800000001"}),
    # 失效换码样本：旧码退役后仍被使用
    dict(idem="seed-sl6", source="pos", type="sale_line", biz="2026-03-15T10:00:00", ing="2026-03-15T10:00:02",
         payload={"sale_id": "POS-0006", "line_no": 1, "store": "S001", "sku": "IBU-100", "qty": 1, "unit": "盒",
                  "gross_amount": 30, "discount_amount": 0, "member_key": "13900000003"}),
    # 套装一对多样本：缺少分摊依据 → 待核（不平均分配）
    dict(idem="seed-sl7", source="pos", type="sale_line", biz="2026-02-25T16:00:00", ing="2026-02-25T16:00:02",
         payload={"sale_id": "POS-0007", "line_no": 1, "store": "S001", "sku": "BUNDLE-1", "qty": 1, "unit": "套",
                  "gross_amount": 128, "discount_amount": 0, "member_key": "13800000001"}),
    dict(idem="seed-sl8", source="pos", type="sale_line", biz="2026-02-26T16:00:00", ing="2026-02-26T16:00:02",
         payload={"sale_id": "POS-0008", "line_no": 1, "store": "S001", "sku": "BUNDLE-2", "qty": 1, "unit": "套",
                  "gross_amount": 99, "discount_amount": 9, "member_key": "13900000004"}),
    dict(idem="seed-sl9", source="pos", type="sale_line", biz="2026-01-15T10:00:00", ing="2026-01-15T10:00:02",
         payload={"sale_id": "POS-0009", "line_no": 1, "store": "S001", "sku": "VIT-7", "qty": 1, "unit": "瓶",
                  "gross_amount": 45, "discount_amount": 0, "member_key": "13900000003"}),
    dict(idem="seed-sl10", source="pos", type="sale_line", biz="2026-02-10T10:00:00", ing="2026-02-10T10:00:02",
         payload={"sale_id": "POS-0010", "line_no": 1, "store": "S002", "sku": "VIT-7", "qty": 1, "unit": "瓶",
                  "gross_amount": 48, "discount_amount": 0, "member_key": "13900000004"}),
    # 门店不可售样本：v4 仅 S001 可售，S002 销售 → 待核
    dict(idem="seed-sl11", source="pos", type="sale_line", biz="2026-03-01T10:00:00", ing="2026-03-01T10:00:02",
         payload={"sale_id": "POS-0011", "line_no": 1, "store": "S002", "sku": "VIT-7", "qty": 2, "unit": "瓶",
                  "gross_amount": 96, "discount_amount": 0, "member_key": "13900000003"}),
    dict(idem="seed-sl12", source="pos", type="sale_line", biz="2026-02-15T10:00:00", ing="2026-02-15T10:00:02",
         payload={"sale_id": "POS-0012", "line_no": 1, "store": "S001", "sku": "AMOX-750", "qty": 1, "unit": "盒",
                  "gross_amount": 80, "discount_amount": 0, "member_key": "13900000002"}),
    # 缺少单位样本：单位「托盘」无换算定义 → 待核
    dict(idem="seed-sl13", source="pos", type="sale_line", biz="2026-02-18T10:00:00", ing="2026-02-18T10:00:02",
         payload={"sale_id": "POS-0013", "line_no": 1, "store": "S001", "sku": "VIT-7", "qty": 1, "unit": "托盘",
                  "gross_amount": 900, "discount_amount": 0, "member_key": "13800000001"}),
]

SEED_EVENTS = SEED_EVENTS + CATALOG_SEED_EVENTS


def seed(db: Session) -> dict:
    from sqlalchemy import select

    from . import models

    created, skipped = 0, 0
    key_to_id: dict[str, int] = {}
    for spec in SEED_EVENTS:
        supersedes_id = None
        if spec.get("supersedes"):
            supersedes_id = key_to_id.get(spec["supersedes"])
        payload = dict(spec["payload"])
        # 把业务键引用解析为事件 id（拆分/撤销指向合并事件）
        for ref_in, ref_out in (("merge_event_key", "merge_event_id"), ("target_event_key", "target_event_id")):
            if ref_in in payload:
                payload[ref_out] = key_to_id.get(payload.pop(ref_in))
        ev, was_created = ingest_event(
            db,
            source=spec["source"],
            event_type=spec["type"],
            biz_time=T(spec["biz"]),
            ingested_at=T(spec["ing"]),
            payload=payload,
            actor="seed",
            supersedes_id=supersedes_id,
            idempotency_key=spec["idem"],
        )
        key_to_id[spec["idem"]] = ev.id
        created += 1 if was_created else 0
        skipped += 0 if was_created else 1

    # 时间语义目录
    semantics = [
        ("AS_OF", "当时可见", "query", "query",
         "以查询时点同时作为业务截止与系统截止：只纳入当时已到达平台的证据，重现当时可见的资格结论。"),
        ("AS_KNOWN_NOW", "事后补全", "query", "now",
         "业务时点不变、系统时点为当前：纳入其后到达的补传、更正、身份与规则变更，给出事后补全口径。"),
        ("DELTA", "二者差异", "query", "both",
         "对同一业务时点分别按两种口径计算并归因差异（迟到证据、更正、身份变更、规则修订/撤回）。"),
    ]
    for code, name, biz_p, sys_p, desc in semantics:
        if db.get(models.Semantics, code) is None:
            db.add(models.Semantics(code=code, name=name, biz_policy=biz_p, sys_policy=sys_p, description=desc))
    # 演示账号
    for username, password, role in (("admin", "admin123", "管理员"), ("analyst", "analyst123", "分析师")):
        if db.get(models.User, username) is None:
            db.add(models.User(username=username, password=password, role=role, token=f"token-{username}-2026"))
    db.commit()
    return {"events_created": created, "events_skipped": skipped, "total_events": db.query(models.Event).count()}
