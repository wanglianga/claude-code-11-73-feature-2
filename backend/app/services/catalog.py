"""目录接收器 + 映射解析器 + 口径校验器。

目录接收器：把 catalog_upsert / catalog_withdraw 事件投影为来源版本
（商品码、包装层级、单位换算、监管分类、品类、换码链、门店可售、生效区间）。

目录版本解析语义（确定性，两种路线共用）：
- 同一 sku 的多个来源版本按 (effective_from, version) 全序排列；
- 版本 v 的派生生效区间 = [v.effective_from, min(v.effective_to,
  未在 T_b 被撤回的后序版本的最小 effective_from))；
  —— 撤回后序版本时，前序版本的派生区间自然延伸覆盖该缺口（回落语义）；
- 撤回 (sku, 目标版本, [withdraw_from, withdraw_to)) 只在该业务区间内剔除目标版本；
- 候选中取 (effective_from, version) 最大者；门店须在可售范围内。

映射解析器：按交易日、门店可售状态与规则版本把销售行关联到历史分类。
- 路线 A（MATERIALIZED）：目录/销售事件到达时物化 mapping_slices（带有效区间），
  目录修订仅重审生效区间相交的切片（闭合 sys_to + 新开），月度/门店分析直接读切片；
- 路线 B（INTERPRETED）：查询时按同一 resolve_line 核心逐行解释，不依赖切片。
两路线共享同一解析核心，结论必然一致（验证脚本断言）。

口径校验器：无法自动归类的记录列入待核（不平均分配、不猜测）：
ONE_TO_MANY（套装一对多且无可验证分摊依据）、STALE_CODE（失效换码）、
MISSING_UNIT（缺单位换算）、MISSING_DISCOUNT（缺折扣事实）、
NOT_SELLABLE（门店不可售）、UNKNOWN_SKU、NOT_YET_EFFECTIVE。
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import rules

# 积分资格：监管分类白名单（处方药 RX 不计积分）；另须规则版本商品范围覆盖、事实齐全
POINTS_ELIGIBLE_REG = {"OTC", "双跨", "保健"}

CATALOG_TYPES = ("catalog_upsert", "catalog_withdraw")
SALE_TYPE = "sale_line"


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- 目录接收器投影
def apply_catalog_event(db: Session, ev: models.Event, head_time: datetime | None = None) -> None:
    p = ev.payload
    now = head_time or _ledger_head_time(db)
    if ev.event_type == "catalog_upsert":
        version = (
            db.query(models.CatalogItem).filter(models.CatalogItem.sku == p["sku"]).count() + 1
        )
        item = models.CatalogItem(
            catalog_event_id=ev.id,
            sku=p["sku"],
            version=version,
            name=p.get("name"),
            base_unit=p.get("base_unit"),
            pack_hierarchy=p.get("pack_hierarchy"),
            unit_conversions=p.get("unit_conversions") or {},
            reg_class=p.get("reg_class"),
            category=p.get("category"),
            supersedes_code=p.get("supersedes_code"),
            store_scope=p.get("store_scope", "ALL"),
            bundle_components=p.get("bundle_components"),
            allocation_basis=p.get("allocation_basis"),
            effective_from=_parse(p["effective_from"]),
            effective_to=_parse(p["effective_to"]) if p.get("effective_to") else None,
            sys_from=ev.ingested_at,
        )
        db.add(item)
        db.flush()
        # 换码：旧码退役点不修改旧版本行，而在解析时按「可见后继版本的生效起点」派生
        # （避免把未来的换码事实泄漏到过去的系统时点口径）。
        affected_skus = {p["sku"]} | ({p["supersedes_code"]} if p.get("supersedes_code") else set())
        reexamine_slices(db, affected_skus, item.effective_from, None, now)
    elif ev.event_type == "catalog_withdraw":
        db.add(
            models.CatalogWithdrawal(
                withdraw_event_id=ev.id,
                sku=p["sku"],
                target_event_id=p["target_event_id"],
                withdraw_from=_parse(p["withdraw_from"]),
                withdraw_to=_parse(p["withdraw_to"]) if p.get("withdraw_to") else None,
                sys_from=ev.ingested_at,
                reason=p.get("reason"),
            )
        )
        db.flush()
        # 仅重审撤回区间内的交易（区间外切片不动）
        reexamine_slices(
            db, {p["sku"]}, _parse(p["withdraw_from"]),
            _parse(p["withdraw_to"]) if p.get("withdraw_to") else None,
            now,
        )
    # 发布器守望：目录追溯修订命中已发布数据集时登记待复核期间
    from . import datasets  # 延迟导入避免循环

    datasets.note_catalog_event(db, ev)


# ---------------------------------------------------------------- 目录版本解析
def _withdrawn_at(withdrawals, item: models.CatalogItem, biz_t: datetime, sys_t: datetime) -> bool:
    for w in withdrawals:
        if w.sys_from > sys_t:
            continue
        if w.sku != item.sku or w.target_event_id != item.catalog_event_id:
            continue
        if w.withdraw_from <= biz_t and (w.withdraw_to is None or w.withdraw_to > biz_t):
            return True
    return False


def catalog_item_at(
    db: Session, sku: str, store: str, biz_t: datetime, sys_t: datetime
) -> tuple[models.CatalogItem | None, datetime | None, str | None]:
    """解析 (sku, 门店, 交易日, 系统时点) 的目录版本。

    返回 (版本, 派生生效终点, 不可归类原因)。原因为 None 表示成功；
    否则为 STALE_CODE / NOT_SELLABLE / UNKNOWN_SKU / NOT_YET_EFFECTIVE / NO_VALID_VERSION。
    """
    versions = db.execute(
        select(models.CatalogItem).where(models.CatalogItem.sku == sku)
    ).scalars().all()
    withdrawals = db.execute(select(models.CatalogWithdrawal)).scalars().all()
    visible = [v for v in versions if v.sys_from <= sys_t]
    if not visible:
        return None, None, "UNKNOWN_SKU"

    # 换码退役点：sys_t 可见的后继新码的最早生效起点（派生，不改写旧版本行）
    successors = db.execute(
        select(models.CatalogItem).where(
            models.CatalogItem.supersedes_code == sku,
            models.CatalogItem.sys_from <= sys_t,
        )
    ).scalars().all()
    retired_at = min((s.effective_from for s in successors), default=None)

    def later_versions(v: models.CatalogItem):
        return [
            w for w in visible
            if (w.effective_from, w.version) > (v.effective_from, v.version)
            and not _withdrawn_at(withdrawals, w, biz_t, sys_t)
        ]

    def derived_to(v: models.CatalogItem):
        caps = [w.effective_from for w in later_versions(v)]
        if v.effective_to is not None:
            caps.append(v.effective_to)
        if retired_at is not None:
            caps.append(retired_at)
        return min(caps) if caps else None

    # 先在「门店可售」口径下找候选
    sellable, not_sellable = [], []
    for v in visible:
        if _withdrawn_at(withdrawals, v, biz_t, sys_t):
            continue
        v_to = derived_to(v)
        if v.effective_from <= biz_t and (v_to is None or v_to > biz_t):
            (sellable if rules.store_in_scope(v.store_scope, store) else not_sellable).append((v, v_to))
    if sellable:
        best = max(sellable, key=lambda x: (x[0].effective_from, x[0].version))
        return best[0], best[1], None
    if not_sellable:
        v, v_to = max(not_sellable, key=lambda x: (x[0].effective_from, x[0].version))
        return v, v_to, "NOT_SELLABLE"
    # 无生效候选：区分失效换码 / 尚未生效 / 版本空洞
    latest = max(visible, key=lambda v: (v.effective_from, v.version))
    if retired_at is not None and retired_at <= biz_t:
        return latest, retired_at, "STALE_CODE"
    if all(v.effective_from > biz_t for v in visible):
        return min(visible, key=lambda v: v.effective_from), None, "NOT_YET_EFFECTIVE"
    return latest, None, "NO_VALID_VERSION"


# ---------------------------------------------------------------- 映射解析核心（两路线共用）
def resolve_line(db: Session, payload: dict, biz_t: datetime, sys_t: datetime) -> dict:
    """把一条销售行解析到交易日口径的历史分类。纯函数式解析，不写库。

    返回 {flag, flag_detail, item, derived_to, allocations, rule, net_amount, qty_base}；
    allocations 元素 {category, reg_class, weight, net_amount, qty_base, points_eligible, points}。
    """
    sku = payload.get("sku", "")
    store = payload.get("store", "UNKNOWN")
    item, derived_to, err = catalog_item_at(db, sku, store, biz_t, sys_t)

    # 事实校验：单位换算 / 折扣事实（与目录解析相互独立，逐项记录）
    qty = payload.get("qty")
    unit = payload.get("unit")
    gross = payload.get("gross_amount")
    discount = payload.get("discount_amount", None)
    qty_base = None
    unit_err = None
    if qty is not None:
        if item is not None and err is None:
            conv = dict(item.unit_conversions or {})
            if item.base_unit:
                conv[item.base_unit] = 1
            if unit in conv:
                qty_base = round(float(qty) * float(conv[unit]), 3)
            else:
                unit_err = f"单位 {unit!r} 无换算定义（sku {sku}，基本单位 {item.base_unit}）"
        elif unit is None:
            unit_err = "缺少计量单位"
    net = None
    if gross is not None and discount is not None:
        net = round(float(gross) - float(discount), 2)

    result = {
        "flag": None, "flag_detail": None, "item": item, "derived_to": derived_to,
        "allocations": [], "rule": None, "net_amount": net, "qty_base": qty_base,
    }

    if err is not None:
        if err == "STALE_CODE":
            succ = _successor(db, sku)
            retired_at = derived_to.date() if derived_to else "?"
            detail = (
                f"商品码 {sku} 已于 {retired_at} 退役"
                + (f"（换码后继 {succ}）" if succ != "?" else "")
                + f"，销售日 {biz_t.date()} 无有效目录版本（失效换码）"
            )
        elif err == "NOT_SELLABLE":
            detail = f"门店 {store} 不在目录版本 v{item.version if item else '?'} 的可售范围 {item.store_scope if item else '?'}"
        elif err == "UNKNOWN_SKU":
            detail = f"商品码 {sku} 在目录中不存在（系统时点 {sys_t.date()} 可见版本）"
        elif err == "NOT_YET_EFFECTIVE":
            detail = f"商品码 {sku} 在销售日 {biz_t.date()} 尚未生效"
        else:
            detail = f"商品码 {sku} 在销售日 {biz_t.date()} 无可用目录版本（生效区间被撤回覆盖且无回落版本）"
        result["flag"], result["flag_detail"] = err, detail
        return result
    if unit_err:
        result["flag"], result["flag_detail"] = "MISSING_UNIT", unit_err
        return result
    if discount is None:
        result["flag"] = "MISSING_DISCOUNT"
        result["flag_detail"] = "缺少折扣事实，无法确定净额与积分资格（待补传）"
        return result

    # 规则版本（按交易日 + 门店解析），积分资格的商品范围口径
    rv = rules.rule_for_store(db, store, biz_t, sys_t)
    if rv is not None:
        result["rule"] = {"id": rv.id, "rule_id": rv.rule_id, "version": rv.version}

    def eligible(category: str, reg_class: str) -> bool:
        if reg_class not in POINTS_ELIGIBLE_REG:
            return False
        if rv is None:
            return False
        return rules.product_in_scope(rv.product_scope, category)

    components = item.bundle_components or []
    if components:
        cats = sorted({c.get("category") for c in components})
        basis = item.allocation_basis or {}
        weights = basis.get("weights") or {}
        if len(cats) > 1 and not all(weights.get(c) for c in cats):
            result["flag"] = "ONE_TO_MANY"
            result["flag_detail"] = (
                f"套装 {sku} 映射到多个品类 {cats}，缺少可验证的分摊依据，"
                "列为待核（不平均分配）"
            )
            result["net_amount"] = None
            result["qty_base"] = None
            return result
        for c in components:
            cat = c.get("category")
            w = float(weights.get(cat, 1.0)) if len(cats) > 1 else 1.0
            a_net = round(net * w, 2) if net is not None else None
            a_qty = round(qty_base * w, 3) if qty_base is not None else None
            ok = eligible(cat, c.get("reg_class"))
            result["allocations"].append({
                "category": cat, "reg_class": c.get("reg_class"), "weight": w,
                "net_amount": a_net, "qty_base": a_qty,
                "points_eligible": ok, "points": round(a_net) if ok and a_net is not None else 0,
            })
        return result

    ok = eligible(item.category, item.reg_class)
    result["allocations"].append({
        "category": item.category, "reg_class": item.reg_class, "weight": 1.0,
        "net_amount": net, "qty_base": qty_base,
        "points_eligible": ok, "points": round(net) if ok and net is not None else 0,
    })
    return result


def _successor(db: Session, sku: str) -> str:
    nxt = db.execute(
        select(models.CatalogItem).where(models.CatalogItem.supersedes_code == sku)
    ).scalars().first()
    return nxt.sku if nxt else "?"


# ---------------------------------------------------------------- 路线 A：切片物化与区间重审
def _sale_heads(db: Session, sys_t: datetime) -> dict[int, models.Event]:
    """销售行修订链在 sys_t 口径下的链首：{原始事件id: 链首事件}。"""
    events = db.execute(
        select(models.Event).where(models.Event.event_type == SALE_TYPE)
    ).scalars().all()
    children: dict[int, list[models.Event]] = {}
    for e in events:
        if e.supersedes_id:
            children.setdefault(e.supersedes_id, []).append(e)

    def head(e: models.Event) -> models.Event:
        cur = e
        while True:
            nxt = [c for c in children.get(cur.id, []) if c.ingested_at <= sys_t]
            if not nxt:
                return cur
            cur = max(nxt, key=lambda c: (c.ingested_at, c.id))

    return {e.id: head(e) for e in events if e.ingested_at <= sys_t}


def _ledger_head_time(db: Session) -> datetime:
    """账本头部时间（最大入账时间）。物化/重审均以账本全部已知事实为口径，
    保证回放与补录场景下切片结论确定且与「当前」一致。"""
    head = db.execute(
        select(models.Event).order_by(models.Event.ingested_at.desc(), models.Event.id.desc()).limit(1)
    ).scalar_one_or_none()
    return head.ingested_at if head else datetime.now(timezone.utc)


def apply_sale_event(db: Session, ev: models.Event, head_time: datetime | None = None) -> None:
    """销售行入账（含补传/更正链）：闭合被替代行的切片与标志，解析并物化新切片。"""
    if ev.supersedes_id:
        _close_line_state(db, ev.supersedes_id, ev.ingested_at)
    _materialize_line(db, ev, head_time)


def _close_line_state(db: Session, sale_event_id: int, sys_t: datetime) -> None:
    for s in db.execute(
        select(models.MappingSlice).where(
            models.MappingSlice.sale_event_id == sale_event_id, models.MappingSlice.sys_to.is_(None)
        )
    ).scalars().all():
        s.sys_to = sys_t
    for f in db.execute(
        select(models.ValidationFlag).where(
            models.ValidationFlag.sale_event_id == sale_event_id, models.ValidationFlag.sys_to.is_(None)
        )
    ).scalars().all():
        f.sys_to = sys_t
        f.status = "RESOLVED"


def _materialize_line(db: Session, ev: models.Event, head_time: datetime | None = None) -> None:
    """按账本当前全部已知事实解析销售行并写入切片/校验标志。"""
    sys_t = head_time or _ledger_head_time(db)
    res = resolve_line(db, ev.payload, ev.biz_time, sys_t)
    _write_resolution(db, ev, res, sys_t)


def _write_resolution(db: Session, ev: models.Event, res: dict, sys_t: datetime) -> None:
    p = ev.payload
    item = res["item"]
    rule = res["rule"] or {}
    base = dict(
        sale_event_id=ev.id,
        sale_id=p.get("sale_id", f"EV{ev.id}"),
        line_no=p.get("line_no", 1),
        store=p.get("store", "UNKNOWN"),
        sku=p.get("sku", ""),
        member_key=p.get("member_key"),
        alloc_group=f"{p.get('sale_id', f'EV{ev.id}')}#{p.get('line_no', 1)}",
        catalog_item_id=item.id if item else None,  # 最近版本引用（含未命中场景，供追查）
        catalog_event_id=item.catalog_event_id if item else None,
        catalog_version=item.version if item else None,
        rule_version_id=rule.get("id"),
        rule_version=rule.get("version"),
        biz_time=ev.biz_time,
        mapping_valid_from=item.effective_from if item else None,
        mapping_valid_to=res["derived_to"] if item else None,
        sys_from=sys_t,
    )
    if res["flag"] is not None:
        db.add(models.MappingSlice(
            **base, category=None, reg_class=None, alloc_weight=1,
            qty_base=res["qty_base"], net_amount=res["net_amount"],
            points_eligible=False, points=0, flag=res["flag"],
        ))
        db.add(models.ValidationFlag(
            sale_event_id=ev.id, sale_id=base["sale_id"], line_no=base["line_no"],
            store=base["store"], sku=base["sku"], flag=res["flag"],
            detail=res["flag_detail"], status="OPEN", biz_time=ev.biz_time, sys_from=sys_t,
        ))
        return
    for a in res["allocations"]:
        db.add(models.MappingSlice(
            **base, category=a["category"], reg_class=a["reg_class"],
            alloc_weight=a["weight"], qty_base=a["qty_base"], net_amount=a["net_amount"],
            points_eligible=a["points_eligible"], points=a["points"], flag="OK",
        ))


def reexamine_slices(
    db: Session, skus: set[str], biz_from: datetime, biz_to: datetime | None, sys_t: datetime
) -> int:
    """目录修订后的区间限定重审：只重审 sku 相关且交易日落在 [biz_from, biz_to) 的切片。

    旧切片闭合 sys_to（留痕），按修订后目录重新解析开立；校验标志同步闭合/重开。
    """
    current = db.execute(
        select(models.MappingSlice).where(models.MappingSlice.sys_to.is_(None))
    ).scalars().all()
    targets: dict[int, models.Event] = {}
    for s in current:
        if s.sku not in skus:
            continue
        if s.biz_time < biz_from or (biz_to is not None and s.biz_time >= biz_to):
            continue
        targets[s.sale_event_id] = None
    for eid in targets:
        ev = db.get(models.Event, eid)
        if ev is None:
            continue
        _close_line_state(db, eid, sys_t)
        res = resolve_line(db, ev.payload, ev.biz_time, sys_t)
        _write_resolution(db, ev, res, sys_t)
    return len(targets)


def sale_lines_at(db: Session, sys_t: datetime) -> list[models.Event]:
    """路线 B 数据源：sys_t 口径下全部销售行链首事件（按业务时间排序）。"""
    heads = _sale_heads(db, sys_t)
    out = sorted({id(h): h for h in heads.values()}.values(), key=lambda e: (e.biz_time, e.id))
    return out


def resolve_sale_event(db: Session, ev: models.Event, sys_t: datetime) -> dict:
    """路线 B：查询时按历史目录版本解释一笔销售行（不读切片）。"""
    res = resolve_line(db, ev.payload, ev.biz_time, sys_t)
    item = res["item"]
    return {
        "sale_event_id": ev.id,
        "sale_id": ev.payload.get("sale_id"),
        "line_no": ev.payload.get("line_no", 1),
        "store": ev.payload.get("store"),
        "sku": ev.payload.get("sku"),
        "biz_time": ev.biz_time.isoformat(),
        "sys_as_of": sys_t.isoformat(),
        "flag": res["flag"] or "OK",
        "flag_detail": res["flag_detail"],
        "catalog": {
            "item_id": item.id, "catalog_event_id": item.catalog_event_id,
            "version": item.version, "category": item.category, "reg_class": item.reg_class,
            "effective_from": item.effective_from.isoformat(),
            "effective_to": res["derived_to"].isoformat() if res["derived_to"] else None,
        } if item and res["flag"] is None else None,
        "rule_version": (res["rule"] or {}).get("version"),
        "allocations": [
            {k: v for k, v in a.items()} for a in res["allocations"]
        ],
        "net_amount": res["net_amount"],
        "qty_base": res["qty_base"],
    }
