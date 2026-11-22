"""可比性计算器：旧新目录口径之间的可比较集合、待排除范围与分类迁移量。

输入同一期间与两个系统水位（旧/新目录口径），对每笔销售行分别按两个口径
解析（路线 B 同一核心），比较结论：
- 可比较集合：两个口径下分类结论一致的品类（可直接对比的净额/数量/积分）；
- 待排除范围：任一口径下被校验器标记、或分类发生变化的销售行；
- 分类迁移量：旧分类 → 新分类 的净额/数量迁移汇总；
- 分别归因：分类变化（目录修订/撤回驱动）与交易更正（单位/折扣事实补传驱动）
  分两个桶列出原因事件并各自汇总积分影响；同一行同时受两类驱动时归入
  分类变化桶并备注更正事件，避免积分影响重复计数。
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .catalog import CATALOG_TYPES, SALE_TYPE, resolve_line


def _catalog_version_at(db: Session, sys_t: datetime) -> int:
    ids = [
        e.id
        for e in db.execute(select(models.Event)).scalars().all()
        if e.event_type in CATALOG_TYPES and e.ingested_at <= sys_t
    ]
    return max(ids, default=0)


def _line_chains(db: Session, period_from: datetime, period_to: datetime) -> dict[str, list[models.Event]]:
    """期间内全部销售行，按业务行键（sale_id#line_no）分链（含修订链全成员）。"""
    events = db.execute(
        select(models.Event).where(models.Event.event_type == SALE_TYPE)
    ).scalars().all()
    chains: dict[str, list[models.Event]] = {}
    for e in events:
        key = f"{e.payload.get('sale_id', f'EV{e.id}')}#{e.payload.get('line_no', 1)}"
        chains.setdefault(key, []).append(e)
    # 只保留链上有成员落在期间内的链
    out = {}
    for key, members in chains.items():
        if any(period_from <= m.biz_time < period_to for m in members):
            out[key] = sorted(members, key=lambda e: (e.ingested_at, e.id))
    return out


def _head_at(members: list[models.Event], sys_t: datetime) -> models.Event | None:
    visible = [m for m in members if m.ingested_at <= sys_t]
    return visible[-1] if visible else None


def _signature(res: dict) -> tuple:
    if res["flag"] is not None:
        return ("FLAG", res["flag"])
    return tuple(sorted((a["category"], a["reg_class"]) for a in res["allocations"]))


def _sums(res: dict) -> dict:
    return {
        "net_amount": round(sum(a["net_amount"] or 0 for a in res["allocations"]), 2),
        "qty_base": round(sum(a["qty_base"] or 0 for a in res["allocations"]), 3),
        "points": round(sum(a["points"] for a in res["allocations"]), 2),
    }


def compute_comparability(
    db: Session, period_from: datetime, period_to: datetime, old_sys: datetime, new_sys: datetime
) -> dict:
    chains = _line_chains(db, period_from, period_to)
    lo, hi = sorted([old_sys, new_sys])
    catalog_events_between = [
        e for e in db.execute(select(models.Event)).scalars().all()
        if e.event_type in CATALOG_TYPES and lo < e.ingested_at <= hi
    ]

    comparable: dict[tuple, dict] = {}
    exclusions, migration_map = [], {}
    class_changes, corrections = [], []
    pts_class, pts_corr = 0.0, 0.0

    for key, members in sorted(chains.items()):
        old_head, new_head = _head_at(members, old_sys), _head_at(members, new_sys)
        if old_head is None and new_head is None:
            continue
        # 任一水位下不可见（尚未入账）的行：作为迟到事实归入交易更正
        old_res = resolve_line(db, old_head.payload, old_head.biz_time, old_sys) if old_head else None
        new_res = resolve_line(db, new_head.payload, new_head.biz_time, new_sys) if new_head else None
        head = new_head or old_head
        p = head.payload
        sale_id, sku = p.get("sale_id"), p.get("sku")

        if old_res is None or new_res is None:
            res = new_res or old_res
            delta = _sums(res)["points"] if new_res else 0
            corrections.append({
                "sale_id": sale_id, "sku": sku,
                "before": None if old_res is None else _sums(old_res),
                "after": None if new_res is None else _sums(new_res),
                "cause_event_ids": [m.id for m in members if lo < m.ingested_at <= hi],
                "points_delta": delta,
                "note": "迟到/补传销售行（旧水位不可见）",
            })
            pts_corr += delta
            continue

        old_sig, new_sig = _signature(old_res), _signature(new_res)
        old_item = old_res["item"].catalog_event_id if old_res["item"] else None
        new_item = new_res["item"].catalog_event_id if new_res["item"] else None
        head_changed = old_head.id != new_head.id
        catalog_changed = old_item != new_item
        old_sum, new_sum = _sums(old_res), _sums(new_res)
        points_delta = round(new_sum["points"] - old_sum["points"], 2)

        flagged = old_res["flag"] or new_res["flag"]
        if flagged:
            exclusions.append({
                "sale_id": sale_id, "sku": sku, "biz_time": head.biz_time.isoformat(),
                "flag": new_res["flag"] or old_res["flag"],
                "detail": new_res["flag_detail"] or old_res["flag_detail"],
            })

        if catalog_changed:
            # 分类变化：目录修订/撤回/换码驱动（签名或目录版本变化）
            causes = [
                e.id for e in catalog_events_between
                if e.payload.get("sku") == sku or e.payload.get("supersedes_code") == sku
            ]
            note = None
            if head_changed:
                note = f"同时存在交易更正事件 #{new_head.id}（积分影响计入本桶，不重复计数）"
            class_changes.append({
                "sale_id": sale_id, "sku": sku, "biz_time": head.biz_time.isoformat(),
                "before": {"signature": list(old_sig), **old_sum},
                "after": {"signature": list(new_sig), **new_sum},
                "cause_event_ids": causes + ([new_head.id] if head_changed else []),
                "points_delta": points_delta, "note": note,
            })
            pts_class += points_delta
            if old_sig != new_sig and old_sig[0] != "FLAG" and new_sig[0] != "FLAG":
                # 行级迁移：旧主分类（权重最大）→ 新各分摊分类，金额/数量按新口径
                old_primary = max(old_res["allocations"], key=lambda a: a["weight"])
                for a_new in new_res["allocations"]:
                    mk = (
                        old_primary["category"], old_primary["reg_class"],
                        a_new["category"], a_new["reg_class"],
                    )
                    slot = migration_map.setdefault(mk, {
                        "from_category": mk[0], "from_reg_class": mk[1],
                        "to_category": mk[2], "to_reg_class": mk[3],
                        "net_amount": 0.0, "qty_base": 0.0, "line_count": 0, "skus": set(),
                    })
                    slot["net_amount"] += a_new["net_amount"] or 0
                    slot["qty_base"] += a_new["qty_base"] or 0
                    slot["skus"].add(sku)
                    slot["line_count"] += 1
        elif head_changed:
            # 交易更正：单位/折扣等事实补传驱动（修订链链首变化，目录版本未变）
            corrections.append({
                "sale_id": sale_id, "sku": sku,
                "before": old_sum, "after": new_sum,
                "cause_event_ids": [m.id for m in members if lo < m.ingested_at <= hi],
                "points_delta": points_delta,
                "note": "单位/折扣等事实补传（修订链链首变化）",
            })
            pts_corr += points_delta
        elif not flagged:
            for a in new_res["allocations"]:
                slot = comparable.setdefault((a["category"], a["reg_class"]), {
                    "category": a["category"], "reg_class": a["reg_class"],
                    "net_amount": 0.0, "qty_base": 0.0, "points": 0.0, "line_count": 0,
                })
                slot["net_amount"] += a["net_amount"] or 0
                slot["qty_base"] += a["qty_base"] or 0
                slot["points"] += a["points"]
                slot["line_count"] += 1

    for slot in comparable.values():
        slot["net_amount"] = round(slot["net_amount"], 2)
        slot["qty_base"] = round(slot["qty_base"], 3)
        slot["points"] = round(slot["points"], 2)
    migration = []
    for slot in migration_map.values():
        slot["net_amount"] = round(slot["net_amount"], 2)
        slot["qty_base"] = round(slot["qty_base"], 3)
        slot["skus"] = sorted(slot["skus"])
        migration.append(slot)
    migration.sort(key=lambda m: (str(m["from_category"]), str(m["to_category"])))

    return {
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "old": {"sys_as_of": old_sys.isoformat(), "catalog_version": _catalog_version_at(db, old_sys)},
        "new": {"sys_as_of": new_sys.isoformat(), "catalog_version": _catalog_version_at(db, new_sys)},
        "comparable_set": sorted(comparable.values(), key=lambda c: c["category"]),
        "exclusions": exclusions,
        "migration": migration,
        "classification_changes": class_changes,
        "transaction_corrections": corrections,
        "points_impact": {
            "classification_change": round(pts_class, 2),
            "transaction_correction": round(pts_corr, 2),
        },
    }
