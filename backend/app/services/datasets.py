"""分析数据集发布器：冻结目录、映射与规则引用，向积分/品类分析/财务对账交付。

- 发布时冻结：目录版本（最大目录事件 id）、映射引用（路线 A=切片水位 /
  路线 B=销售行水位）、规则集合版本、证据水位；每份数据集显示所用路线与目录版本。
- 路线选择（route=AUTO 时按目录修订频率与销售行量启发式选择，理由随数据集保存）：
  MATERIALIZED 读物化切片（月度/门店分析读快，适合目录稳定期）；
  INTERPRETED 查询时逐行解释（避免高频修订下的切片重建写放大）。
  两路线共享同一解析核心，同一期间输出相同的可比范围（验证脚本断言）。
- 已发布数据集的目录被追溯修订时：登记待复核期间（dataset_reviews），
  原数据集行永不修改（原月报依据保留）；复核 = 按新目录版本发布新数据集。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .catalog import CATALOG_TYPES, SALE_TYPE, resolve_line, sale_lines_at
from .publications import RULE_TYPES

# AUTO 启发式阈值：目录修订频率（次/月）超过该值 → 查询时解释，避免切片写放大
REVISION_FREQ_THRESHOLD = 2.0
# 销售行量阈值：超过则物化切片存储预算不经济 → 查询时解释
LINE_VOLUME_THRESHOLD = 50000


def _months_between(start: datetime, end: datetime) -> list[str]:
    out = []
    cur = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur < end:
        out.append(f"{cur.year:04d}-{cur.month:02d}")
        cur = (cur.replace(day=28) + timedelta(days=10)).replace(day=1)
    return out


def _max_event_id(db: Session, types: tuple[str, ...] | None = None, sys_t: datetime | None = None) -> int:
    ids = []
    for e in db.execute(select(models.Event)).scalars().all():
        if types and e.event_type not in types:
            continue
        if sys_t and e.ingested_at > sys_t:
            continue
        ids.append(e.id)
    return max(ids, default=0)


def _mapping_watermark(db: Session, route: str, sys_t: datetime) -> int:
    """映射引用水位：路线 A 冻结最大切片 id，路线 B 冻结最大销售行事件 id。"""
    if route == "MATERIALIZED":
        return db.query(models.MappingSlice).count() and max(
            s[0] for s in db.query(models.MappingSlice.id).all()
        )
    return _max_event_id(db, (SALE_TYPE,), sys_t)


def choose_route(db: Session, period_from: datetime, period_to: datetime) -> tuple[str, str]:
    """依据目录修订频率、销售行量（历史分析并发与存储预算的代理指标）选择路线。"""
    months = max(len(_months_between(period_from, period_to)), 1)
    revisions = 0
    for e in db.execute(select(models.Event)).scalars().all():
        if e.event_type not in CATALOG_TYPES:
            continue
        p = e.payload
        ef = datetime.fromisoformat((p.get("effective_from") or p.get("withdraw_from")).replace("Z", "+00:00"))
        et = p.get("effective_to") or p.get("withdraw_to")
        et = datetime.fromisoformat(et.replace("Z", "+00:00")) if et else None
        if ef < period_to and (et is None or et > period_from):
            revisions += 1
    freq = revisions / months
    lines = sum(
        1 for e in db.execute(select(models.Event)).scalars().all()
        if e.event_type == SALE_TYPE and period_from <= e.biz_time < period_to
    )
    if freq > REVISION_FREQ_THRESHOLD:
        return "INTERPRETED", f"AUTO：目录修订频率 {freq:.1f} 次/月 > {REVISION_FREQ_THRESHOLD}，高频修订下物化切片重建写放大不经济，选查询时解释"
    if lines > LINE_VOLUME_THRESHOLD:
        return "INTERPRETED", f"AUTO：期间销售行 {lines} > {LINE_VOLUME_THRESHOLD}，物化存储预算不经济，选查询时解释"
    return "MATERIALIZED", f"AUTO：目录修订频率 {freq:.1f} 次/月、销售行 {lines}，目录稳定，物化切片使月度/门店分析读取快"


def _resolved_lines(db: Session, route: str, period_from: datetime, period_to: datetime, sys_t: datetime):
    """按路线取得期间内各销售行的解析结论（统一结构）。"""
    out = []
    if route == "MATERIALIZED":
        slices = db.execute(
            select(models.MappingSlice).where(models.MappingSlice.sys_to.is_(None))
        ).scalars().all()
        by_line: dict[str, list[models.MappingSlice]] = {}
        for s in slices:
            if not (period_from <= s.biz_time < period_to):
                continue
            by_line.setdefault(s.alloc_group, []).append(s)
        for group, rows in by_line.items():
            first = rows[0]
            out.append({
                "sale_event_id": first.sale_event_id, "sale_id": first.sale_id,
                "store": first.store, "sku": first.sku, "member_key": first.member_key,
                "biz_time": first.biz_time, "flag": first.flag,
                "allocations": [
                    {"category": r.category, "reg_class": r.reg_class, "net_amount": float(r.net_amount or 0),
                     "qty_base": float(r.qty_base or 0), "points": float(r.points), "points_eligible": r.points_eligible}
                    for r in rows if r.flag == "OK"
                ],
            })
    else:
        for ev in sale_lines_at(db, sys_t):
            if not (period_from <= ev.biz_time < period_to):
                continue
            res = resolve_line(db, ev.payload, ev.biz_time, sys_t)
            out.append({
                "sale_event_id": ev.id, "sale_id": ev.payload.get("sale_id"),
                "store": ev.payload.get("store", "UNKNOWN"), "sku": ev.payload.get("sku"),
                "member_key": ev.payload.get("member_key"), "biz_time": ev.biz_time,
                "flag": res["flag"] or "OK",
                "allocations": res["allocations"] if res["flag"] is None else [],
            })
    return out


def create_dataset(db: Session, name: str, period_from: datetime, period_to: datetime, route: str = "AUTO") -> models.AnalyticsDataset:
    sys_now = datetime.now(timezone.utc)
    if route == "AUTO":
        route, reason = choose_route(db, period_from, period_to)
    elif route in ("MATERIALIZED", "INTERPRETED"):
        reason = f"人工指定路线 {route}"
    else:
        raise ValueError(f"未知路线: {route}（可选 AUTO / MATERIALIZED / INTERPRETED）")

    lines = _resolved_lines(db, route, period_from, period_to, sys_now)

    # 聚合可比口径：仅未标记（OK）行进入聚合；待核行单列，不参与任何平均/分摊
    agg: dict[tuple, dict] = {}
    pending = []
    for ln in lines:
        period = f"{ln['biz_time'].year:04d}-{ln['biz_time'].month:02d}"
        if ln["flag"] != "OK":
            pending.append({
                "sale_id": ln["sale_id"], "sku": ln["sku"], "store": ln["store"],
                "biz_time": ln["biz_time"].isoformat(), "flag": ln["flag"],
            })
            continue
        for a in ln["allocations"]:
            key = (period, ln["store"], a["category"], a["reg_class"])
            slot = agg.setdefault(key, {"net_amount": 0.0, "qty_base": 0.0, "points": 0.0, "line_count": 0})
            slot["net_amount"] += a["net_amount"] or 0
            slot["qty_base"] += a["qty_base"] or 0
            slot["points"] += a["points"]
            slot["line_count"] += 1

    dataset = models.AnalyticsDataset(
        name=name, created_at=sys_now, period_from=period_from, period_to=period_to,
        route=route, route_reason=reason,
        catalog_version=_max_event_id(db, CATALOG_TYPES, sys_now),
        mapping_version=_mapping_watermark(db, route, sys_now),
        rule_set_version=_max_event_id(db, RULE_TYPES, sys_now),
        watermark_event_id=_max_event_id(db, None, sys_now),
        sys_as_of=sys_now,
        detail={},
    )
    db.add(dataset)
    db.flush()

    rows = []
    for (period, store, category, reg), slot in sorted(agg.items()):
        rows.append(models.AnalyticsDatasetRow(
            dataset_id=dataset.id, period=period, store=store, category=category, reg_class=reg,
            net_amount=round(slot["net_amount"], 2), qty_base=round(slot["qty_base"], 3),
            points=round(slot["points"], 2), line_count=slot["line_count"],
        ))
    for r in rows:
        db.add(r)

    # 下游口径汇总：积分（按会员/期间）、品类贡献、财务对账（按门店/期间）
    total_net = sum(float(r.net_amount) for r in rows) or 1.0
    by_category: dict[str, float] = {}
    by_member: dict[tuple, float] = {}
    by_store: dict[tuple, list] = {}
    for ln in lines:
        if ln["flag"] != "OK":
            continue
        period = f"{ln['biz_time'].year:04d}-{ln['biz_time'].month:02d}"
        for a in ln["allocations"]:
            by_category[a["category"]] = by_category.get(a["category"], 0.0) + (a["net_amount"] or 0)
            if ln["member_key"]:
                k = (ln["member_key"], period)
                by_member[k] = by_member.get(k, 0.0) + a["points"]
            k2 = (ln["store"], period)
            st = by_store.setdefault(k2, [0.0, 0])
            st[0] += a["net_amount"] or 0
            st[1] += 1
    dataset.detail = {
        "pending_lines": pending,
        "downstream": {
            "points": [
                {"member_key": k[0], "period": k[1], "points": round(v, 2)}
                for k, v in sorted(by_member.items())
            ],
            "category_analysis": [
                {"category": c, "net_amount": round(v, 2), "contribution_pct": round(v / total_net * 100, 2)}
                for c, v in sorted(by_category.items())
            ],
            "finance_reconciliation": [
                {"store": k[0], "period": k[1], "net_amount": round(v[0], 2), "line_count": v[1]}
                for k, v in sorted(by_store.items())
            ],
        },
    }
    db.commit()
    db.refresh(dataset)
    return dataset


def note_catalog_event(db: Session, ev: models.Event) -> int:
    """发布器守望：目录事件追溯修订命中已发布数据集时登记待复核期间。

    命中条件：数据集冻结的目录版本早于本事件，且事件生效区间与数据集期间相交。
    原数据集行保持不变（原月报依据）。
    """
    p = ev.payload
    if ev.event_type == "catalog_upsert":
        iv_from = datetime.fromisoformat(p["effective_from"].replace("Z", "+00:00"))
        iv_to = datetime.fromisoformat(p["effective_to"].replace("Z", "+00:00")) if p.get("effective_to") else None
        item = db.execute(
            select(models.CatalogItem).where(models.CatalogItem.catalog_event_id == ev.id)
        ).scalar_one_or_none()
        what = f"目录修订 {p.get('sku')} v{item.version if item else '?'}（{p.get('category')}/{p.get('reg_class')}）"
    else:
        iv_from = datetime.fromisoformat(p["withdraw_from"].replace("Z", "+00:00"))
        iv_to = datetime.fromisoformat(p["withdraw_to"].replace("Z", "+00:00")) if p.get("withdraw_to") else None
        what = f"目录撤回 {p.get('sku')}（{p.get('reason') or ''}）"

    created = 0
    datasets = db.execute(select(models.AnalyticsDataset)).scalars().all()
    for ds in datasets:
        if ds.catalog_version >= ev.id:
            continue  # 数据集已包含本目录版本
        lo, hi = max(ds.period_from, iv_from), min(ds.period_to, iv_to or ds.period_to)
        if lo >= hi:
            continue
        periods = _months_between(lo, hi)
        if not periods:
            continue
        dup = db.execute(
            select(models.DatasetReview).where(
                models.DatasetReview.dataset_id == ds.id,
                models.DatasetReview.catalog_event_id == ev.id,
            )
        ).scalar_one_or_none()
        if dup:
            continue
        db.add(models.DatasetReview(
            dataset_id=ds.id, catalog_event_id=ev.id, periods=periods,
            reason=f"{what} 追溯影响已发布数据集「{ds.name}」的期间 {periods}；原月报依据保留，请按新目录版本复核重发",
            status="PENDING", created_at=ev.ingested_at,
        ))
        created += 1
    return created
