#!/usr/bin/env python3
"""端到端验证：任意时点资格、两种口径差异、下游影响、交易分类历史映射与可比数据服务。

用法: python3 scripts/verify.py [base_url]
前置: 后端已启动（默认 http://127.0.0.1:8000）。脚本会重置业务数据并灌入种子场景。
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
A, B, C, D, F, G = ("13800000001", "13900000002", "13900000003",
                    "13900000004", "13900000005", "13900000006")
passed, failed = 0, 0


def call(method, path, body=None, token=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} | ({"Authorization": f"Bearer {token}"} if token else {}),
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def check(name, cond, detail=""):
    global passed, failed
    passed, failed = passed + bool(cond), failed + (not cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {detail}"))


def elig(token, member, biz, semantics="AS_OF", sys_time=None):
    q = f"/api/members/{member}/eligibility?biz_time={biz}&semantics={semantics}"
    if sys_time:
        q += f"&sys_time={sys_time}"
    return call("GET", q, token=token)


def main():
    token = call("POST", "/api/login", {"username": "admin", "password": "admin123"})["token"]
    call("POST", "/api/admin/reset", token=token)
    seed = call("POST", "/api/seed", token=token)
    check("种子灌入 44 个事件（18 基础 + 26 目录/销售行）", seed["events_created"] == 44, seed)

    # ---------- 1. 任意时点资格 ----------
    # Case1: 2026-02-28 当时可见：误合并仍有效 -> 簇{A,B}，v1 阈值 1000，1800 -> GOLD
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-02-28T23:59:00Z")
    check("Case1 A@02-28 当时可见 = GOLD", r["tier"] == "GOLD", r)
    check("Case1 簇含 B（合并生效中）", set(r["cluster"]) == {A, B}, r["cluster"])
    check("Case1 消费 1800（含 B 的 600）", r["total_spend"] == 1800.0, r["total_spend"])

    # Case2: 同一业务时点、事后补全：拆分追溯生效 -> 簇{A}；v2 追溯(1500)适用 S001/ONLINE；
    #        v2 对 S002 被部分撤回 -> S002 用 v1。1280 / 350 -> SILVER
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("Case2 A@02-28 事后补全 = SILVER", r["tier"] == "SILVER", r)
    check("Case2 簇仅 A（误合并已追溯拆分）", r["cluster"] == [A], r["cluster"])
    vers = {s["rule_version"]: s["spend"] for s in r["scopes"]}
    check("Case2 v2 范围 1280 / v1(S002 撤回后) 350", vers.get(2) == 1280.0 and vers.get(1) == 350.0, vers)

    # Case0: 2026-01-31 当时可见 = SILVER(900)；事后补全因离线补传 T4(01-28,350) 变 GOLD(1250)
    r = elig(token, A, "2026-01-31T23:59:00Z", "AS_OF", "2026-01-31T23:59:00Z")
    check("Case0 A@01-31 当时可见 = SILVER", r["tier"] == "SILVER" and r["total_spend"] == 900.0, r)
    r = elig(token, A, "2026-01-31T23:59:00Z", "AS_KNOWN_NOW")
    check("Case0b A@01-31 事后补全 = GOLD（离线补传改判）", r["tier"] == "GOLD" and r["total_spend"] == 1250.0, r)

    # Case3/4: 2026-03-31 两个口径均 GOLD，但消费构成不同（2230 vs 1630）
    r = elig(token, A, "2026-03-31T23:59:00Z", "AS_OF", "2026-03-31T23:59:00Z")
    check("Case3 A@03-31 当时可见 = GOLD 2230（合并中+更正后+补传）", r["tier"] == "GOLD" and r["total_spend"] == 2230.0, r)
    r = elig(token, A, "2026-03-31T23:59:00Z", "AS_KNOWN_NOW")
    check("Case4 A@03-31 事后补全 = GOLD 1630（拆分后）", r["tier"] == "GOLD" and r["total_spend"] == 1630.0, r)

    # 更正链：T3 在 03-05 前为 300，之后为 380
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-03-04T23:59:00Z")
    check("更正前口径含 T3=300（总 1800）", r["total_spend"] == 1800.0, r["total_spend"])
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_OF", "2026-03-06T00:00:00Z")
    check("更正后口径含 T3=380（总 1880）", r["total_spend"] == 1880.0, r["total_spend"])
    check("更正留痕 replaced_events", len(r["replaced_events"]) == 1, r["replaced_events"])

    # 其他会员：B 拆分后回落；D 被撤回保护；F 被规则修订击中；G 被撤回保护
    r = elig(token, B, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("B@02-28 事后补全 = SILVER 600（拆出后独立）", r["tier"] == "SILVER" and r["total_spend"] == 600.0, r)
    r = elig(token, D, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("D@02-28 事后补全 = GOLD 1100（S002 撤回 v2 保护）", r["tier"] == "GOLD" and r["total_spend"] == 1100.0, r)
    r = elig(token, F, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("F@02-28 事后补全 = SILVER（v2 阈值 1500 击中）", r["tier"] == "SILVER", r)
    r = elig(token, G, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("G@02-28 事后补全 = GOLD 1400（撤回保护，否则 SILVER）", r["tier"] == "GOLD", r)

    # DELTA 语义
    r = elig(token, A, "2026-02-28T23:59:00Z", "DELTA", "2026-02-28T23:59:00Z")
    check("DELTA: A@02-28 两种口径 GOLD->SILVER", r["as_of"]["tier"] == "GOLD" and r["as_known_now"]["tier"] == "SILVER" and r["tier_changed"], r)

    # ---------- 2. 发布库 + 差异归因 + 影响索引 ----------
    p1 = call("POST", "/api/publications", {"name": "2026-02月末·当时可见", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-02-28T23:59:00Z", "semantics_code": "AS_OF"}, token)
    pmid = call("POST", "/api/publications", {"name": "2026-02月末·3月末水位", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-03-31T23:59:00Z", "semantics_code": "AS_OF"}, token)
    p2 = call("POST", "/api/publications", {"name": "2026-02月末·事后补全", "biz_as_of": "2026-02-28T23:59:00Z", "sys_as_of": "2026-11-22T00:00:00Z", "semantics_code": "AS_KNOWN_NOW"}, token)
    check("发布 P1 冻结证据水位=44（2月末可见最大事件id）", p1["watermark_event_id"] == 44, p1)
    check("发布 P2 冻结规则集合版本>0 且身份图版本>0", p2["rule_set_version"] > 0 and p2["identity_graph_version"] > 0, p2)

    d_full = call("POST", "/api/diffs", {"pub_a": p1["id"], "pub_b": p2["id"]}, token)
    d_mid = call("POST", "/api/diffs", {"pub_a": pmid["id"], "pub_b": p2["id"]}, token)
    diff = call("GET", f"/api/diffs/{d_full['id']}", token=token)
    items = {i["member_key"]: i for i in diff["items"]}
    check("全量差异含 A 的降级记录", A in items and items[A]["before"]["tier"] == "GOLD" and items[A]["after"]["tier"] == "SILVER", items.get(A))
    causes_a = {c["cause"] for c in items[A]["causes"]} if A in items else set()
    check("A 的归因覆盖：迟到交易/更正/身份拆分/规则修订",
          {"LATE_TRANSACTION", "CORRECTION", "IDENTITY_SPLIT", "RULE_REVISION"} <= causes_a, causes_a)
    check("B 的归因含身份拆分", any(c["cause"] == "IDENTITY_SPLIT" for c in items.get(B, {}).get("causes", [])), items.get(B))
    check("F 的归因含规则修订", any(c["cause"] == "RULE_REVISION" for c in items.get(F, {}).get("causes", [])), items.get(F))

    diff_mid = call("GET", f"/api/diffs/{d_mid['id']}", token=token)
    items_mid = {i["member_key"]: i for i in diff_mid["items"]}
    check("中段差异：D 因撤回由 SILVER 升回 GOLD",
          D in items_mid and items_mid[D]["before"]["tier"] == "SILVER" and items_mid[D]["after"]["tier"] == "GOLD", items_mid.get(D))
    check("D 的归因含规则撤回", any(c["cause"] == "RULE_WITHDRAWAL" for c in items_mid.get(D, {}).get("causes", [])), items_mid.get(D))
    check("中段差异：A 因拆分降级", A in items_mid and items_mid[A]["after"]["tier"] == "SILVER", items_mid.get(A))

    # 影响索引
    impacts = diff["impacts"]
    mk = [i for i in impacts if i["dimension"] == "marketing" and i["member_key"] == A]
    bg = [i for i in impacts if i["dimension"] == "budget" and i["member_key"] == A]
    tr = [i for i in impacts if i["dimension"] == "training" and i["member_key"] == A]
    check("影响：A 退出 GOLD 营销名单、进入 SILVER 名单",
          any(i["entity"] == "marketing:list:GOLD" for i in mk) and any(i["entity"] == "marketing:list:SILVER" for i in mk), mk)
    check("影响：A 的门店预算条目（S001/S002/ONLINE）", len(bg) >= 2, bg)
    check("影响：A 的 2026-02 训练样本标签重述 GOLD->SILVER",
          any(i["change_type"] == "LABEL_RESTATED" and i["entity"] == "training:samples:2026-02" for i in tr), tr)
    check("影响条目携带归因事件 id", all(i["attributed_event_ids"] for i in mk + tr), mk + tr)
    ent = call("GET", "/api/impacts?entity=marketing:list:GOLD", token=token)
    check("影响索引可按下游实体反查", len(ent) >= 1, ent)

    # ---------- 3. 目录版本 / 映射解析 / 口径校验 ----------
    vit = call("GET", "/api/catalog/items?sku=VIT-7", token=token)
    check("高频目录修订：VIT-7 四个来源版本", len(vit) == 4, vit)
    check("单位换算随版本变化（60→90 片/瓶）",
          vit[0]["unit_conversions"] == {"瓶": 60} and vit[2]["unit_conversions"] == {"瓶": 90}, vit)
    check("门店可售状态随版本变化（v4 仅 S001）", vit[3]["store_scope"] == ["S001"], vit[3])
    chain = call("GET", "/api/catalog/chain/IBU-100", token=token)
    check("换码链 IBU-100 → IBU-200", chain["successors"] == ["IBU-200"] and chain["predecessors"] == [], chain)
    wds = call("GET", "/api/catalog/withdrawals", token=token)
    check("追溯撤回仅覆盖部分生效期 [2026-02-01, 2026-03-01)",
          len(wds) == 1 and wds[0]["withdraw_from"].startswith("2026-02-01") and wds[0]["withdraw_to"].startswith("2026-03-01"), wds)

    # 映射解析器：同一销售行在不同系统时点的历史分类（路线 B 即时解释）
    r = call("GET", "/api/mapping/resolve?sale_id=POS-0003", token=token)
    check("追溯撤回后 POS-0003 回落 v1 OTC（积分资格恢复 50 分）",
          r["catalog"]["version"] == 1 and r["catalog"]["reg_class"] == "OTC" and r["allocations"][0]["points"] == 50, r)
    r = call("GET", "/api/mapping/resolve?sale_id=POS-0003&sys_time=2026-03-01T00:00:00Z", token=token)
    check("同一行按 2026-03-01 水位解释为 v2 RX（当时不计积分）",
          r["catalog"]["version"] == 2 and r["catalog"]["reg_class"] == "RX" and r["allocations"][0]["points"] == 0, r)
    r = call("GET", "/api/mapping/resolve?sale_id=POS-0002", token=token)
    check("撤回区间外的 1 月交易保持 v1 OTC（仅重审区间内交易）",
          r["catalog"]["version"] == 1 and r["catalog"]["reg_class"] == "OTC" and r["allocations"][0]["points"] == 25, r)
    r = call("GET", "/api/mapping/resolve?sale_id=POS-0005", token=token)
    check("换码后新码 IBU-200 正常解析（解热镇痛/OTC）",
          r["flag"] == "OK" and r["catalog"]["category"] == "解热镇痛", r)

    # 口径校验器：无法自动归类的记录列入待核
    flags = call("GET", "/api/validation/flags?status=OPEN", token=token)
    fmap = {f["sale_id"]: f["flag"] for f in flags}
    check("待核：失效换码（POS-0006 旧码退役后使用）", fmap.get("POS-0006") == "STALE_CODE", fmap)
    check("待核：套装一对多（POS-0007 缺分摊依据）", fmap.get("POS-0007") == "ONE_TO_MANY", fmap)
    check("待核：缺少单位换算（POS-0013 托盘）", fmap.get("POS-0013") == "MISSING_UNIT", fmap)
    check("待核：门店不可售（POS-0011 S002 不在 v4 范围）", fmap.get("POS-0011") == "NOT_SELLABLE", fmap)
    slices = call("GET", "/api/mapping/slices?current=true", token=token)
    smap = {}
    for s in slices:
        smap.setdefault(s["sale_id"], []).append(s)
    b1 = smap.get("POS-0007", [])
    check("套装一对多不平均分配（净额/数量留空，仅一条待核切片）",
          len(b1) == 1 and b1[0]["net_amount"] is None and b1[0]["qty_base"] is None, b1)
    all_flags = call("GET", "/api/validation/flags", token=token)
    check("折扣补传后 MISSING_DISCOUNT 标志已解决",
          any(f["sale_id"] == "POS-0004" and f["flag"] == "MISSING_DISCOUNT" and f["status"] == "RESOLVED" for f in all_flags), all_flags)
    sl4 = smap.get("POS-0004", [])
    check("补传折扣后 POS-0004 净额 450、积分资格恢复",
          len(sl4) == 1 and sl4[0]["flag"] == "OK" and sl4[0]["net_amount"] == 450.0 and sl4[0]["points"] == 450.0, sl4)

    # ---------- 4. 可比性计算器 / 分析数据集发布器 ----------
    cmp1 = call("POST", "/api/comparability", {
        "period_from": "2026-02-01T00:00:00Z", "period_to": "2026-03-01T00:00:00Z",
        "old_sys_time": "2026-02-28T23:59:00Z", "new_sys_time": "2026-04-10T00:00:00Z"}, token)
    check("可比性：目录版本 29 → 30（撤回事件）",
          cmp1["old"]["catalog_version"] == 29 and cmp1["new"]["catalog_version"] == 30, (cmp1["old"], cmp1["new"]))
    cc = {c["sale_id"]: c for c in cmp1["classification_changes"]}
    tc = {c["sale_id"]: c for c in cmp1["transaction_corrections"]}
    check("分类变化归因：POS-0003 由撤回事件#30 驱动，积分 +50",
          "POS-0003" in cc and cc["POS-0003"]["points_delta"] == 50 and 30 in cc["POS-0003"]["cause_event_ids"], cc)
    check("交易更正归因：POS-0004 由补传事件#35 驱动，积分 +450",
          "POS-0004" in tc and tc["POS-0004"]["points_delta"] == 450 and 35 in tc["POS-0004"]["cause_event_ids"], tc)
    check("分类变化与交易更正分别归因（50 / 450 不重复计数）",
          cmp1["points_impact"] == {"classification_change": 50.0, "transaction_correction": 450.0}, cmp1["points_impact"])
    comp_set = {(c["category"], c["reg_class"]): c for c in cmp1["comparable_set"]}
    check("可比较集合：两口径一致的品类（含维矿补充 48 / 抗感染 80）",
          comp_set.get(("维矿补充", "保健"), {}).get("net_amount") == 48.0 and comp_set.get(("抗感染", "RX"), {}).get("net_amount") == 80.0, cmp1["comparable_set"])
    check("待排除范围：一对多/缺单位/旧口径缺折扣",
          {e["sale_id"] for e in cmp1["exclusions"]} == {"POS-0004", "POS-0007", "POS-0013"}, cmp1["exclusions"])
    mig = cmp1["migration"]
    check("分类迁移量：感冒用药 RX → OTC 迁移 50 元",
          len(mig) == 1 and mig[0]["from_reg_class"] == "RX" and mig[0]["to_reg_class"] == "OTC" and mig[0]["net_amount"] == 50.0, mig)

    # 数据集：两路线输出相同可比范围
    dm = call("POST", "/api/datasets", {"name": "2026-02月报·物化切片", "period_from": "2026-02-01T00:00:00Z", "period_to": "2026-03-01T00:00:00Z", "route": "MATERIALIZED"}, token)
    di = call("POST", "/api/datasets", {"name": "2026-02月报·查询时解释", "period_from": "2026-02-01T00:00:00Z", "period_to": "2026-03-01T00:00:00Z", "route": "INTERPRETED"}, token)
    check("数据集显示所用路线与目录版本",
          dm["route"] == "MATERIALIZED" and di["route"] == "INTERPRETED" and dm["catalog_version"] == 30 and di["catalog_version"] == 30, (dm, di))
    rm = call("GET", f"/api/datasets/{dm['id']}", token=token)
    ri = call("GET", f"/api/datasets/{di['id']}", token=token)
    rowkey = lambda rows: sorted((x["period"], x["store"], x["category"], x["reg_class"], x["net_amount"], x["qty_base"], x["points"], x["line_count"]) for x in rows)
    check("两路线同一期间输出相同可比范围（行级一致）", rowkey(rm["rows"]) == rowkey(ri["rows"]) and len(rm["rows"]) == 5, (rm["rows"], ri["rows"]))
    rows = {(x["store"], x["category"]): x for x in rm["rows"]}
    check("月报行：S002 解热镇痛 净额450 积分450（补传折扣后）",
          rows.get(("S002", "解热镇痛"), {}).get("net_amount") == 450.0 and rows.get(("S002", "解热镇痛"), {}).get("points") == 450.0, rows)
    check("月报行：S001 感冒用药 净额113（POS-0003 的 50 + 套装分摊 63）",
          rows.get(("S001", "感冒用药"), {}).get("net_amount") == 113.0 and rows.get(("S001", "感冒用药"), {}).get("line_count") == 2, rows)
    check("待核行不参与聚合（POS-0007 / POS-0013 仅列待核）",
          {p["sale_id"] for p in rm["detail"]["pending_lines"]} == {"POS-0007", "POS-0013"}, rm["detail"]["pending_lines"])
    pts = {(p["member_key"], p["period"]): p["points"] for p in rm["detail"]["downstream"]["points"]}
    check("下游积分口径：会员 B 2026-02 得 450 分", pts.get((B, "2026-02")) == 450.0, pts)
    fin = {(f["store"], f["period"]): f["net_amount"] for f in rm["detail"]["downstream"]["finance_reconciliation"]}
    check("下游财务对账：S002 2026-02 净额 498", fin.get(("S002", "2026-02")) == 498.0, fin)

    dq = call("POST", "/api/datasets", {"name": "2026-Q1·AUTO", "period_from": "2026-01-01T00:00:00Z", "period_to": "2026-04-01T00:00:00Z", "route": "AUTO"}, token)
    check("AUTO 按目录修订频率选路线（高频修订 → INTERPRETED）", dq["route"] == "INTERPRETED" and "频率" in dq["route_reason"], dq)

    # 已发布数据集引用的目录被追溯修订 → 待复核期间 + 原月报依据保留
    before_rows = call("GET", f"/api/datasets/{dm['id']}", token=token)["rows"]
    ev45 = call("POST", "/api/events", {
        "source": "catalog_admin", "event_type": "catalog_upsert", "biz_time": "2026-04-15T10:00:00Z",
        "ingested_at": "2026-04-15T10:00:00Z", "idempotency_key": "verify-vit7-v5",
        "payload": {"sku": "VIT-7", "name": "维生素C咀嚼片", "base_unit": "片",
                    "unit_conversions": {"瓶": 90}, "reg_class": "保健", "category": "膳食营养",
                    "store_scope": "ALL", "effective_from": "2026-02-10T00:00:00+00:00",
                    "comment": "品类口径追溯调整：维矿补充 → 膳食营养"}}, token)
    check("追溯修订事件入账（VIT-7 v5，生效自 02-10）", ev45["created"] is True, ev45)
    rv_m = call("GET", f"/api/reviews?dataset_id={dm['id']}", token=token)
    rv_i = call("GET", f"/api/reviews?dataset_id={di['id']}", token=token)
    rv_q = call("GET", f"/api/reviews?dataset_id={dq['id']}", token=token)
    check("发布器登记待复核期间（三份数据集均命中 2026-02）",
          rv_m and rv_m[0]["periods"] == ["2026-02"] and rv_m[0]["status"] == "PENDING"
          and rv_i and rv_q and "2026-03" in rv_q[0]["periods"], (rv_m, rv_i, rv_q))
    after_rows = call("GET", f"/api/datasets/{dm['id']}", token=token)["rows"]
    check("原月报依据保留（已发布行不被修改）", before_rows == after_rows, (before_rows, after_rows))

    d2 = call("POST", "/api/datasets", {"name": "2026-02月报·复核后", "period_from": "2026-02-01T00:00:00Z", "period_to": "2026-03-01T00:00:00Z", "route": "INTERPRETED"}, token)
    r2 = call("GET", f"/api/datasets/{d2['id']}", token=token)
    cats2 = {(x["store"], x["category"]) for x in r2["rows"]}
    check("复核后新数据集反映新口径（S002 膳食营养 48）",
          ("S002", "膳食营养") in cats2 and ("S002", "维矿补充") not in cats2, r2["rows"])
    check("复核后数据集目录版本为事件#45", r2["catalog_version"] == 45, r2["catalog_version"])

    cmp2 = call("POST", "/api/comparability", {
        "period_from": "2026-02-01T00:00:00Z", "period_to": "2026-03-01T00:00:00Z",
        "old_sys_time": "2026-04-10T00:00:00Z", "new_sys_time": "2026-04-16T00:00:00Z"}, token)
    mig2 = cmp2["migration"]
    check("可比性#2 迁移量：维矿补充 → 膳食营养 48 元（追溯修订）",
          len(mig2) == 1 and mig2[0]["from_category"] == "维矿补充" and mig2[0]["to_category"] == "膳食营养" and mig2[0]["net_amount"] == 48.0, mig2)
    cc2 = {c["sale_id"]: c for c in cmp2["classification_changes"]}
    check("可比性#2 分类变化归因到追溯修订事件#45",
          "POS-0010" in cc2 and 45 in cc2["POS-0010"]["cause_event_ids"], cc2)

    # 区间限定重审留痕：VIT-7 v5 只重审 02-10 起的交易
    all_slices = call("GET", "/api/mapping/slices", token=token)
    by_sale = {}
    for s in all_slices:
        by_sale.setdefault(s["sale_id"], []).append(s)
    s10 = by_sale.get("POS-0010", [])
    check("区间重审留痕：POS-0010 旧切片闭合（维矿补充）新切片开立（膳食营养）",
          len(s10) == 2 and any(s["sys_to"] and s["category"] == "维矿补充" for s in s10)
          and any(s["sys_to"] is None and s["category"] == "膳食营养" for s in s10), s10)
    s09 = by_sale.get("POS-0009", [])
    check("区间外交易未被重审：POS-0009（01-15）仍只有原始切片",
          len(s09) == 1 and s09[0]["sys_to"] is None and s09[0]["category"] == "保健", s09)

    # ---------- 5. 账本完整性 / 追加-only / 幂等 / 投影重建 ----------
    v = call("GET", "/api/admin/ledger/verify", token=token)
    check("链式哈希完整（45 事件）", v["ok"] and v["checked"] == 45, v)

    dup = call("POST", "/api/events", {"source": "pos", "event_type": "transaction", "biz_time": "2026-01-05T10:20:00Z",
                                       "payload": {"member_key": A, "store": "S001", "category": "OTC", "amount": 500},
                                       "idempotency_key": "seed-t1"}, token)
    check("幂等键重放不重复入账", dup["created"] is False, dup)

    # 追加-only：直接 UPDATE/DELETE events 应被触发器拒绝（本机有 psql 时验证）
    psql = shutil.which("psql") or "/opt/postgresql-18/bin/psql"
    if os.path.exists(psql):
        sql = "UPDATE events SET actor='hacker' WHERE id=1;"
        r1 = subprocess.run([psql, "postgresql://ccdev:ccdev@127.0.0.1:5432/member_bitemp", "-c", sql],
                            capture_output=True, text=True,
                            env={"PGPASSWORD": "ccdev", "PATH": "/usr/bin:/bin"})
        check("追加-only：UPDATE 被拒绝", "追加式" in (r1.stderr + r1.stdout) or r1.returncode != 0, r1.stderr[:200])
    else:
        print("SKIP  追加-only 触发器直查（无 psql，容器内验证见 README）")

    # 重建前记录切片结论，重建后比对（投影 = 账本重放，结论一致）
    pre = call("GET", "/api/mapping/slices?current=true", token=token)
    pre_sig = sorted((s["sale_id"], s["category"], s["flag"], s["points"]) for s in pre)
    rb = call("POST", "/api/admin/rebuild", token=token)
    check("投影重建（45 事件重放，含目录/切片/校验/待复核）",
          rb["events_replayed"] == 45 and rb["identity_edges"] >= 4 and rb["rule_versions"] == 3
          and rb["catalog_items"] == 12 and rb["catalog_withdrawals"] == 1
          and rb["mapping_slices"] == 14 and rb["validation_flags_open"] == 4 and rb["dataset_reviews"] >= 3, rb)
    post = call("GET", "/api/mapping/slices?current=true", token=token)
    post_sig = sorted((s["sale_id"], s["category"], s["flag"], s["points"]) for s in post)
    check("重建后映射结论一致（14 条当前切片签名相同）", pre_sig == post_sig and len(post_sig) == 14, (pre_sig, post_sig))
    r = elig(token, A, "2026-02-28T23:59:00Z", "AS_KNOWN_NOW")
    check("重建后资格结论一致（SILVER）", r["tier"] == "SILVER" and r["total_spend"] == 1630.0, r)
    r = call("GET", "/api/mapping/resolve?sale_id=POS-0003", token=token)
    check("重建后解析结论一致（POS-0003 仍回落 v1 OTC）", r["catalog"]["version"] == 1 and r["catalog"]["reg_class"] == "OTC", r)
    rv_after = call("GET", f"/api/reviews?dataset_id={dm['id']}", token=token)
    check("重建后待复核期间仍在（投影可重建）", len(rv_after) >= 1 and rv_after[0]["periods"] == ["2026-02"], rv_after)

    # 并发修订解决：v2/v3 同链修订 v1，03-15 应适用 v3
    r = elig(token, F, "2026-03-15T23:59:00Z", "AS_KNOWN_NOW")
    check("并发修订确定性解决：03-15 适用 v3(1300)，F 1200 -> SILVER",
          r["tier"] == "SILVER" and all(s["rule_version"] == 3 for s in r["scopes"]), r["scopes"])

    print(f"\n== 验证完成: {passed} 通过, {failed} 失败 ==")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
