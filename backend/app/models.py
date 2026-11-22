"""数据模型。

架构选型：追加式双时间事件账 + 可重建投影。
- events 为唯一事实来源（append-only，DB 触发器禁止 UPDATE/DELETE）。
- identity_edges / rule_versions / rule_withdrawals / member_directory 均为可重建投影，
  由事件重放生成，可随时通过 /api/admin/rebuild 全量重建。
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Event(Base):
    """接入账：追加式双时间事件账。

    每行保存：来源哈希(source_hash)、链式哈希(chain_hash)、两类时间
    (biz_time 业务发生时间 / ingested_at 平台接收时间)、修订链(supersedes_id)、
    幂等键(idempotency_key)。
    """

    __tablename__ = "events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key = Column(String(128), unique=True, nullable=False)
    source = Column(String(32), nullable=False)  # pos | online | franchise_offline | identity_desk | rule_admin
    event_type = Column(String(32), nullable=False)
    source_hash = Column(String(64), nullable=False)  # sha256(canonical payload)
    chain_hash = Column(String(64), nullable=False)  # sha256(prev chain_hash + row) 防篡改链
    biz_time = Column(DateTime(timezone=True), nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)
    actor = Column(String(64))
    supersedes_id = Column(BigInteger, ForeignKey("events.id"), nullable=True)
    payload = Column(JSONB, nullable=False)


class IdentityEdge(Base):
    """身份关系图投影：合并/拆分/待核/撤销的有效区间（双时间区间）。"""

    __tablename__ = "identity_edges"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    relation_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    member_a = Column(String(64), nullable=False)
    member_b = Column(String(64), nullable=False)
    relation = Column(String(16), nullable=False)  # merge | split | pending | revoke
    status = Column(String(16), nullable=False)  # active | split | revoked | pending
    biz_from = Column(DateTime(timezone=True), nullable=False)
    biz_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    sys_to = Column(DateTime(timezone=True), nullable=True)
    target_edge_id = Column(BigInteger, nullable=True)  # split/revoke 指向的 merge 边
    reason = Column(Text)


class RuleVersion(Base):
    """等级规则版本投影：按门店、商品范围与生效区间修订。"""

    __tablename__ = "rule_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    rule_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    rule_id = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    supersedes_version = Column(Integer, nullable=True)
    store_scope = Column(JSONB, nullable=False)  # "ALL" 或门店列表
    product_scope = Column(JSONB, nullable=False)  # "ALL" 或品类列表
    window_days = Column(Integer, nullable=False)
    thresholds = Column(JSONB, nullable=False)  # {"SILVER": 300, "GOLD": 1000}
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)


class RuleWithdrawal(Base):
    """规则撤回投影：支持部分（按门店范围）追溯撤回。"""

    __tablename__ = "rule_withdrawals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    withdraw_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    rule_id = Column(String(64), nullable=False)
    target_version = Column(Integer, nullable=False)
    store_scope = Column(JSONB, nullable=False)
    withdraw_from = Column(DateTime(timezone=True), nullable=False)
    withdraw_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text)


class MemberDirectory(Base):
    """会员名录投影（仅索引已知会员键，不保存会覆盖历史的“当前主档”）。"""

    __tablename__ = "member_directory"

    member_key = Column(String(64), primary_key=True)
    display_name = Column(String(128))
    first_seen_event_id = Column(BigInteger)


class Semantics(Base):
    """时间语义目录：定义查询口径。"""

    __tablename__ = "semantics"

    code = Column(String(32), primary_key=True)
    name = Column(String(64), nullable=False)
    biz_policy = Column(String(16), nullable=False)  # query
    sys_policy = Column(String(16), nullable=False)  # query | now | both
    description = Column(Text)


class Publication(Base):
    """发布库：冻结证据水位、身份关系图版本与规则集合版本。"""

    __tablename__ = "publications"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    biz_as_of = Column(DateTime(timezone=True), nullable=False)
    sys_as_of = Column(DateTime(timezone=True), nullable=False)
    watermark_event_id = Column(BigInteger, nullable=False)  # 证据水位
    identity_graph_version = Column(BigInteger, nullable=False)  # 关系图冻结版本(最大身份事件id)
    rule_set_version = Column(BigInteger, nullable=False)  # 规则集合冻结版本(最大规则事件id)
    semantics_code = Column(String(32), nullable=False)


class PublicationRow(Base):
    __tablename__ = "publication_rows"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    publication_id = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    tier = Column(String(16), nullable=False)
    spend = Column(Numeric(14, 2), nullable=False)
    detail = Column(JSONB, nullable=False)


class Diff(Base):
    __tablename__ = "diffs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pub_a = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    pub_b = Column(BigInteger, ForeignKey("publications.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DiffItem(Base):
    """差异归因：把资格变化连到具体事件。"""

    __tablename__ = "diff_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    diff_id = Column(BigInteger, ForeignKey("diffs.id"), nullable=False)
    member_key = Column(String(64), nullable=False)
    before = Column(JSONB, nullable=False)
    after = Column(JSONB, nullable=False)
    causes = Column(JSONB, nullable=False)  # [{event_id, cause, detail}]


class Impact(Base):
    """影响索引：变化对营销名单、门店预算、训练样本的下游影响。"""

    __tablename__ = "impacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    diff_id = Column(BigInteger, ForeignKey("diffs.id"), nullable=False)
    dimension = Column(String(16), nullable=False)  # marketing | budget | training
    entity = Column(String(128), nullable=False)  # 下游实体键
    member_key = Column(String(64), nullable=False)
    change_type = Column(String(32), nullable=False)
    before = Column(JSONB)
    after = Column(JSONB)
    attributed_event_ids = Column(JSONB)


class User(Base):
    __tablename__ = "users"

    username = Column(String(64), primary_key=True)
    password = Column(String(128), nullable=False)
    role = Column(String(32), nullable=False)
    token = Column(String(128), nullable=False)


# ==================================================================
# 交易分类历史映射与可比数据服务（商品主数据）
# 与基础服务共用同一追加式事件账；下列各表均为可重建投影或冻结发布。
# ==================================================================


class CatalogItem(Base):
    """目录接收器投影：商品目录的来源版本。

    每行 = 某商品码的一个来源版本：商品码、包装层级、单位换算、监管分类、
    品类、换码链（supersedes_code）、门店可售范围与生效区间。
    生效终点多为派生：被同码后续版本或换码新码的生效起点截断（见 catalog 服务）。
    """

    __tablename__ = "catalog_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    catalog_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    sku = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)  # 同一 sku 内按事件序的版本号
    name = Column(String(128))
    base_unit = Column(String(16))  # 基本单位（片/粒/支...）
    pack_hierarchy = Column(JSONB)  # 包装层级 [{unit, contains, of?}]
    unit_conversions = Column(JSONB)  # 单位换算 {单位: 基本单位数}
    reg_class = Column(String(16))  # 监管分类 RX | OTC | 双跨 | 保健
    category = Column(String(64))  # 品类
    supersedes_code = Column(String(64), nullable=True)  # 换码链：本码取代的旧码
    store_scope = Column(JSONB, nullable=False)  # 门店可售状态 "ALL" 或门店列表
    bundle_components = Column(JSONB, nullable=True)  # 套装成分 [{sku, category, reg_class, qty}]
    allocation_basis = Column(JSONB, nullable=True)  # 套装分摊依据 {type, weights:{品类:权重}}
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_to = Column(DateTime(timezone=True), nullable=True)  # 显式终点（换码退役）
    sys_from = Column(DateTime(timezone=True), nullable=False)


class CatalogWithdrawal(Base):
    """目录撤回投影：追溯撤回某来源版本的一段生效期（可只覆盖部分区间）。"""

    __tablename__ = "catalog_withdrawals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    withdraw_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    sku = Column(String(64), nullable=False)
    target_event_id = Column(BigInteger, nullable=False)  # 被撤回的目录版本事件
    withdraw_from = Column(DateTime(timezone=True), nullable=False)
    withdraw_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text)


class MappingSlice(Base):
    """映射解析器物化切片（路线 A）：销售行 → 历史分类的映射，带有效区间。

    每行把一条销售行（sale_event_id）关联到交易日口径的目录版本与分类；
    套装分摊时一行销售行对应多条切片（alloc_group 相同）。目录修订仅重审
    与其生效区间相交的切片：旧切片闭合 sys_to，新切片以修订事件时间开立。
    """

    __tablename__ = "mapping_slices"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    sale_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    sale_id = Column(String(64), nullable=False)
    line_no = Column(Integer, nullable=False, default=1)
    store = Column(String(32), nullable=False)
    sku = Column(String(64), nullable=False)
    member_key = Column(String(64), nullable=True)
    alloc_group = Column(String(40), nullable=False)  # 同一条销售行的分摊组（sale_id#line_no）
    catalog_item_id = Column(BigInteger, nullable=True)
    catalog_event_id = Column(BigInteger, nullable=True)
    catalog_version = Column(Integer, nullable=True)
    category = Column(String(64), nullable=True)
    reg_class = Column(String(16), nullable=True)
    alloc_weight = Column(Numeric(6, 4), nullable=False, default=1)  # 套装分摊权重
    qty_base = Column(Numeric(14, 3), nullable=True)  # 基本单位数量（单位换算后）
    net_amount = Column(Numeric(14, 2), nullable=True)  # 净额（总额-折扣）
    points_eligible = Column(Boolean, nullable=False, default=False)
    points = Column(Numeric(14, 2), nullable=False, default=0)
    rule_version_id = Column(BigInteger, nullable=True)  # 解析所用规则版本
    rule_version = Column(Integer, nullable=True)
    flag = Column(String(24), nullable=False, default="OK")  # OK | ONE_TO_MANY | STALE_CODE | ...
    biz_time = Column(DateTime(timezone=True), nullable=False)  # 交易日
    mapping_valid_from = Column(DateTime(timezone=True), nullable=True)  # 所用目录版本生效区间
    mapping_valid_to = Column(DateTime(timezone=True), nullable=True)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    sys_to = Column(DateTime(timezone=True), nullable=True)  # 被重审闭合前为 NULL


class ValidationFlag(Base):
    """口径校验器：无法自动归类的销售行（待核队列）。

    一对多映射（缺分摊依据，不平均分配）、失效换码、缺少单位/折扣事实、
    门店不可售、未知商品码等。事实补传或目录修订使记录可归类时闭合（RESOLVED）。
    """

    __tablename__ = "validation_flags"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    sale_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    sale_id = Column(String(64), nullable=False)
    line_no = Column(Integer, nullable=False, default=1)
    store = Column(String(32), nullable=False)
    sku = Column(String(64), nullable=False)
    flag = Column(String(24), nullable=False)
    detail = Column(Text)
    status = Column(String(16), nullable=False, default="OPEN")  # OPEN | RESOLVED
    biz_time = Column(DateTime(timezone=True), nullable=False)
    sys_from = Column(DateTime(timezone=True), nullable=False)
    sys_to = Column(DateTime(timezone=True), nullable=True)


class AnalyticsDataset(Base):
    """分析数据集发布器：冻结目录、映射与规则引用，供积分/品类分析/财务对账消费。

    每份数据集显示所用路线（MATERIALIZED 物化切片 / INTERPRETED 查询时解释）
    与目录版本（冻结时最大目录事件 id）。发布后行内容永不修改（原月报依据）。
    """

    __tablename__ = "analytics_datasets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    period_from = Column(DateTime(timezone=True), nullable=False)
    period_to = Column(DateTime(timezone=True), nullable=False)
    route = Column(String(16), nullable=False)  # MATERIALIZED | INTERPRETED
    route_reason = Column(Text)  # 路线选择依据（AUTO 启发式或人工指定）
    catalog_version = Column(BigInteger, nullable=False)  # 冻结的目录版本（最大目录事件 id）
    mapping_version = Column(BigInteger, nullable=False)  # 映射引用（切片水位或销售行水位）
    rule_set_version = Column(BigInteger, nullable=False)
    watermark_event_id = Column(BigInteger, nullable=False)
    sys_as_of = Column(DateTime(timezone=True), nullable=False)
    detail = Column(JSONB, nullable=False)  # 下游口径汇总（积分/品类贡献/财务对账）与待核清单


class AnalyticsDatasetRow(Base):
    """数据集行：按 (期间, 门店, 品类, 监管分类) 聚合的可比口径。"""

    __tablename__ = "analytics_dataset_rows"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id = Column(BigInteger, ForeignKey("analytics_datasets.id"), nullable=False)
    period = Column(String(7), nullable=False)  # YYYY-MM
    store = Column(String(32), nullable=False)
    category = Column(String(64), nullable=False)
    reg_class = Column(String(16), nullable=False)
    net_amount = Column(Numeric(14, 2), nullable=False)
    qty_base = Column(Numeric(16, 3), nullable=False)
    points = Column(Numeric(14, 2), nullable=False)
    line_count = Column(Integer, nullable=False)


class DatasetReview(Base):
    """待复核期间：已发布数据集引用的目录被追溯修订时由发布器登记。

    原数据集行保持不变（原月报依据）；复核方式为依据新目录版本发布新数据集。
    """

    __tablename__ = "dataset_reviews"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id = Column(BigInteger, ForeignKey("analytics_datasets.id"), nullable=False)
    catalog_event_id = Column(BigInteger, ForeignKey("events.id"), nullable=False)
    periods = Column(JSONB, nullable=False)  # 受影响的期间 ["2026-02", ...]
    reason = Column(Text)
    status = Column(String(16), nullable=False, default="PENDING")  # PENDING
    created_at = Column(DateTime(timezone=True), nullable=False)
