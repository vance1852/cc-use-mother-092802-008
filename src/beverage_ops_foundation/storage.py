"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_versions (
    version_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    stage TEXT NOT NULL CHECK(stage IN ('mature', 'new', 'nurturing')),
    effective_from TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, effective_from)
);
CREATE TABLE IF NOT EXISTS annual_budgets (
    annual_budget_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    year INTEGER NOT NULL CHECK(year BETWEEN 2000 AND 2100),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    stage_snapshot TEXT NOT NULL,
    lifecycle_version_id TEXT NOT NULL REFERENCES lifecycle_versions(version_id),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, year, effective_from)
);
CREATE TABLE IF NOT EXISTS period_budgets (
    period_budget_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    year INTEGER NOT NULL CHECK(year BETWEEN 2000 AND 2100),
    period INTEGER NOT NULL CHECK(period BETWEEN 1 AND 12),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    stage_snapshot TEXT NOT NULL,
    lifecycle_version_id TEXT NOT NULL REFERENCES lifecycle_versions(version_id),
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(product_id, year, period, effective_from)
);
CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    team_type TEXT NOT NULL CHECK(team_type IN ('brand', 'region', 'channel')),
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    name TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'cancelled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_beneficiaries (
    activity_id TEXT NOT NULL REFERENCES activities(activity_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    weight INTEGER NOT NULL CHECK(weight > 0),
    PRIMARY KEY(activity_id, product_id)
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL REFERENCES activities(activity_id),
    contract_ref TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    currency TEXT NOT NULL DEFAULT 'CNY',
    year INTEGER NOT NULL,
    period INTEGER NOT NULL,
    allocation_rule TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reserved', 'allocated', 'settled', 'cancelled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitment_shares (
    share_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    ordinal INTEGER NOT NULL,
    weight INTEGER NOT NULL CHECK(weight > 0),
    lifecycle_version_id TEXT NOT NULL REFERENCES lifecycle_versions(version_id),
    stage_snapshot TEXT NOT NULL,
    planned_cents INTEGER NOT NULL CHECK(planned_cents >= 0),
    draft_cents INTEGER NOT NULL DEFAULT 0 CHECK(draft_cents >= 0),
    confirmed_cents INTEGER NOT NULL DEFAULT 0 CHECK(confirmed_cents >= 0),
    reserved_cents INTEGER NOT NULL CHECK(reserved_cents >= 0),
    accrued_cents INTEGER NOT NULL DEFAULT 0 CHECK(accrued_cents >= 0),
    settled_cents INTEGER NOT NULL DEFAULT 0 CHECK(settled_cents >= 0),
    refunded_cents INTEGER NOT NULL DEFAULT 0 CHECK(refunded_cents >= 0),
    basis_value TEXT NOT NULL DEFAULT '',
    ratio_bp INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('planned', 'draft', 'confirmed', 'disputed', 'settled', 'released')),
    dispute_reason TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT,
    UNIQUE(commitment_id, product_id)
);
CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    rule TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    residual_product_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accrual_facts (
    accrual_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    year INTEGER NOT NULL,
    period INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    cross_period INTEGER NOT NULL CHECK(cross_period IN (0, 1)),
    description TEXT NOT NULL DEFAULT '',
    occurred_on TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accrual_allocations (
    accrual_id TEXT NOT NULL REFERENCES accrual_facts(accrual_id),
    share_id TEXT NOT NULL REFERENCES commitment_shares(share_id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    PRIMARY KEY(accrual_id, share_id)
);
CREATE TABLE IF NOT EXISTS invoice_facts (
    invoice_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    accrual_id TEXT REFERENCES accrual_facts(accrual_id),
    invoice_no TEXT NOT NULL,
    year INTEGER NOT NULL,
    period INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    cross_period INTEGER NOT NULL CHECK(cross_period IN (0, 1)),
    refunded_cents INTEGER NOT NULL DEFAULT 0,
    issued_on TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(commitment_id, invoice_no)
);
CREATE TABLE IF NOT EXISTS refund_facts (
    refund_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    invoice_id TEXT REFERENCES invoice_facts(invoice_id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refund_allocations (
    refund_id TEXT NOT NULL REFERENCES refund_facts(refund_id),
    share_id TEXT NOT NULL REFERENCES commitment_shares(share_id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    PRIMARY KEY(refund_id, share_id)
);
CREATE TABLE IF NOT EXISTS budget_ledger (
    entry_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    year INTEGER NOT NULL,
    period INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('reserve', 'release', 'refund')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    share_id TEXT REFERENCES commitment_shares(share_id),
    exception_id TEXT,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_exceptions (
    exception_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(product_id),
    year INTEGER NOT NULL,
    period INTEGER NOT NULL CHECK(period BETWEEN 1 AND 12),
    cap_cents INTEGER NOT NULL CHECK(cap_cents > 0),
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested', 'granted', 'rejected', 'expired', 'revoked')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exception_approvals (
    exception_id TEXT NOT NULL REFERENCES budget_exceptions(exception_id),
    sequence INTEGER NOT NULL,
    approver_actor_id TEXT NOT NULL,
    required_role TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected')),
    comment TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY(exception_id, sequence)
);
CREATE TABLE IF NOT EXISTS exception_usage (
    exception_id TEXT NOT NULL REFERENCES budget_exceptions(exception_id),
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    share_id TEXT NOT NULL REFERENCES commitment_shares(share_id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(exception_id, commitment_id, share_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
