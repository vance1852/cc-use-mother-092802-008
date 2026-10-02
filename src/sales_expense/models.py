"""销售费用域的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Product:
    product_id: str
    organization_id: str
    name: str


@dataclass(frozen=True)
class LifecycleVersion:
    """产品生命周期的一个生效版本。"""

    version_id: str
    product_id: str
    stage: str  # mature / new / nurturing
    effective_from: str
    effective_to: str | None
    status: str
    superseded_by: str | None
    note: str | None


@dataclass(frozen=True)
class Beneficiary:
    """活动受益范围中的一行。"""

    line_id: str
    campaign_id: str
    product_id: str
    factor: int
    owner_actor_id: str


@dataclass(frozen=True)
class AllocationLine:
    """一条分摊草案结果，可解释字段随结果返回。"""

    product_id: str
    owner_actor_id: str
    amount: int
    weight: int
    ratio: str
    rule_name: str
    reason: str


@dataclass(frozen=True)
class AllocationResult:
    """一次分摊草案：各行金额、合计与舍入尾差去向。"""

    rule_name: str
    total_amount: int
    lines: tuple[AllocationLine, ...]
    rounding_gap: int
    rounding_to: str | None
    explanation: dict[str, Any]


@dataclass(frozen=True)
class Share:
    share_id: str
    commitment_id: str
    product_id: str
    owner_actor_id: str
    stage_snapshot: str | None
    version_id: str | None
    period_budget_id: str | None
    period_key: str | None
    amount: int
    status: str
    rule_name: str | None
    explanation: dict[str, Any]
    confirmed_by: str | None
    settled_amount: int
    released_amount: int
    frozen_reason: str | None


@dataclass(frozen=True)
class LedgerEntry:
    """一笔费用在某一份额上的过程流水。"""

    entry_id: int
    share_id: str
    commitment_id: str
    product_id: str
    event: str
    amount: int
    reserved_after: int
    occurred_after: int
    settled_after: int
    detail: dict[str, Any]
    actor_id: str
    occurred_at: str
