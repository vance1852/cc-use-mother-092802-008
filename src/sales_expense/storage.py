"""销售费用域的 SQLite 表结构。

所有金额以整数最小货币单位（分）保存，避免浮点分摊误差。
期间键采用 `YYYYMxx`（月度）或 `YYYYQx`（季度）形式。
"""

from __future__ import annotations

EXPENSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS exp_products (
    product_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 产品生命周期生效版本：同一产品的版本生效区间互不重叠，切换通过新版本取代旧版本完成
CREATE TABLE IF NOT EXISTS exp_lifecycle_versions (
    version_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES exp_products(product_id),
    stage TEXT NOT NULL CHECK(stage IN ('mature','new','nurturing')),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    superseded_by TEXT,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, effective_from)
);
-- 年度额度按产品 + 年份 + 生命周期阶段分别设定；阶段切换后新份额落到新阶段额度，历史份额不动
CREATE TABLE IF NOT EXISTS exp_budget_annual (
    budget_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES exp_products(product_id),
    year INTEGER NOT NULL CHECK(year BETWEEN 2000 AND 2100),
    stage TEXT NOT NULL CHECK(stage IN ('mature','new','nurturing')),
    amount INTEGER NOT NULL CHECK(amount >= 0),
    currency TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, year, stage)
);
CREATE TABLE IF NOT EXISTS exp_budget_periods (
    period_id TEXT PRIMARY KEY,
    budget_id TEXT NOT NULL REFERENCES exp_budget_annual(budget_id),
    period_key TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(budget_id, period_key)
);
CREATE TABLE IF NOT EXISTS exp_campaigns (
    campaign_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    name TEXT NOT NULL,
    owner_team TEXT NOT NULL CHECK(owner_team IN ('brand','region','channel')),
    owner_actor_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','requested','approved','active','completed','cancelled')),
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 活动受益范围：一行对应一个受益产品及其分摊因子与份额负责人
CREATE TABLE IF NOT EXISTS exp_beneficiaries (
    line_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES exp_campaigns(campaign_id),
    product_id TEXT NOT NULL REFERENCES exp_products(product_id),
    factor INTEGER NOT NULL CHECK(factor >= 0),
    override_amount INTEGER,
    owner_actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, product_id)
);
CREATE TABLE IF NOT EXISTS exp_commitments (
    commitment_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES exp_campaigns(campaign_id),
    period_key TEXT NOT NULL,
    total_amount INTEGER NOT NULL CHECK(total_amount >= 0),
    currency TEXT NOT NULL,
    contract_ref TEXT,
    vendor TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('draft','requested','approved','rejected','cancelled','completed')),
    exception_id TEXT,
    requested_at TEXT,
    approved_by TEXT,
    approved_at TEXT,
    cancelled_by TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 产品份额：申请时生成草案，负责人逐份确认；金额与阶段/版本/期间额度在确认时快照，生命周期后续切换不影响它
CREATE TABLE IF NOT EXISTS exp_shares (
    share_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES exp_commitments(commitment_id),
    product_id TEXT NOT NULL REFERENCES exp_products(product_id),
    line_id TEXT NOT NULL,
    owner_actor_id TEXT NOT NULL,
    stage_snapshot TEXT,
    version_id TEXT,
    period_budget_id TEXT,
    period_key TEXT,
    amount INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN
        ('draft','confirmed','rejected','frozen','released','settled')),
    rule_name TEXT,
    explanation_json TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    frozen_at TEXT,
    frozen_reason TEXT,
    dispute_id TEXT,
    settled_amount INTEGER NOT NULL DEFAULT 0,
    released_amount INTEGER NOT NULL DEFAULT 0,
    reserved_amount INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(commitment_id, product_id)
);
CREATE TABLE IF NOT EXISTS exp_allocation_runs (
    run_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES exp_commitments(commitment_id),
    kind TEXT NOT NULL CHECK(kind IN ('draft','redistribution')),
    rule_name TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    rounding_gap INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exp_invoices (
    invoice_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES exp_commitments(commitment_id),
    invoice_no TEXT NOT NULL,
    vendor TEXT,
    invoice_date TEXT NOT NULL,
    taxable_period TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    tax_amount INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('received','void')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(commitment_id, invoice_no)
);
CREATE TABLE IF NOT EXISTS exp_accruals (
    accrual_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES exp_commitments(commitment_id),
    invoice_id TEXT REFERENCES exp_invoices(invoice_id),
    share_id TEXT REFERENCES exp_shares(share_id),
    period_key TEXT NOT NULL,
    amount INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('accrual','reversal','invoice','refund')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exp_refunds (
    refund_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES exp_commitments(commitment_id),
    invoice_id TEXT REFERENCES exp_invoices(invoice_id),
    amount INTEGER NOT NULL CHECK(amount > 0),
    reason TEXT NOT NULL,
    period_key TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exp_settlements (
    settlement_id TEXT PRIMARY KEY,
    share_id TEXT NOT NULL REFERENCES exp_shares(share_id),
    period_key TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exp_disputes (
    dispute_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES exp_commitments(commitment_id),
    product_id TEXT REFERENCES exp_products(product_id),
    disputed_amount INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    resolution TEXT,
    raised_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT
);
-- 超预算例外：金额上限、有效期与逐级授权链
CREATE TABLE IF NOT EXISTS exp_exceptions (
    exception_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES exp_commitments(commitment_id),
    over_amount INTEGER NOT NULL CHECK(over_amount > 0),
    limit_amount INTEGER NOT NULL CHECK(limit_amount > 0),
    reason TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    required_levels INTEGER NOT NULL CHECK(required_levels BETWEEN 1 AND 3),
    status TEXT NOT NULL CHECK(status IN ('requested','approved','rejected','used','expired')),
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS exp_exception_approvals (
    approval_id TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES exp_exceptions(exception_id),
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
    approver_actor_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    comment TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(exception_id, level)
);
-- 份额金额变动流水：申请、占用、发生、分摊、确认、冻结、结账、释放全过程
CREATE TABLE IF NOT EXISTS exp_share_ledger (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    share_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    event TEXT NOT NULL,
    amount INTEGER NOT NULL,
    reserved_after INTEGER NOT NULL,
    occurred_after INTEGER NOT NULL,
    settled_after INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
-- 期间额度占用汇总（已确认/已批准份额按额度期间行聚合）
CREATE TABLE IF NOT EXISTS exp_budget_reservations (
    period_budget_id TEXT NOT NULL REFERENCES exp_budget_periods(period_id),
    reserved_amount INTEGER NOT NULL DEFAULT 0,
    occurred_amount INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(period_budget_id)
);
CREATE INDEX IF NOT EXISTS idx_exp_shares_commitment ON exp_shares(commitment_id);
CREATE INDEX IF NOT EXISTS idx_exp_ledger_commitment ON exp_share_ledger(commitment_id);
CREATE INDEX IF NOT EXISTS idx_exp_accruals_commitment ON exp_accruals(commitment_id);
"""


def ensure_expense_schema(connection) -> None:
    """在基础库同一连接上幂等创建费用域表。"""

    connection.executescript(EXPENSE_SCHEMA)
