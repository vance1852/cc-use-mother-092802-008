"""共享费用的可解释分摊规则。

每条规则都返回每一行的权重、比例、取整过程与尾差去向，
使月底草案能够逐分说明依据，而不是只给一个结果数字。
金额全程使用整数最小货币单位（分）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .errors import AllocationError
from .models import AllocationLine, AllocationResult

RULE_FACTOR = "factor_weighted"
RULE_EQUAL = "equal_split"
RULE_OVERRIDE = "amount_override"
RULES = frozenset({RULE_FACTOR, RULE_EQUAL, RULE_OVERRIDE})


@dataclass(frozen=True)
class BenefitInput:
    """分摊输入的一个受益产品。"""

    product_id: str
    owner_actor_id: str
    factor: int = 0
    override_amount: int | None = None


def _ratio_text(weight: int, total_weight: int) -> str:
    if total_weight <= 0:
        return "0/0"
    # 最大公约数化简，展示可核验的整数比例
    a, b = weight, total_weight
    while b:
        a, b = b, a % b
    return f"{weight // a}/{total_weight // a}"


def _largest_remainder(total_amount: int, weights: Sequence[tuple[str, str, int]]) -> list[tuple[str, str, int, int, int]]:
    """按权重做整数分摊，尾差按最大余数逐分补给。

    返回 (product_id, owner, weight, floor_amount, remainder_scaled)。
    """

    total_weight = sum(w for _, _, w in weights)
    if total_weight <= 0:
        raise AllocationError("分摊权重之和必须大于零")
    scaled = [(pid, owner, w, total_amount * w, w) for pid, owner, w in weights]
    floors = [(pid, owner, w, amount // total_weight, amount % total_weight)
              for pid, owner, w, amount, _ in scaled]
    allocated = sum(item[3] for item in floors)
    gap = total_amount - allocated
    if gap > 0:
        # 余数大者优先；余数相同按 product_id 稳定决胜，保证可重复
        ordered = sorted(floors, key=lambda item: (-item[4], item[0]))
        bumped = {item[0] for item in ordered[:gap]}
        floors = [(pid, owner, w, amount + (1 if pid in bumped else 0), rem)
                  for pid, owner, w, amount, rem in floors]
    return floors


def factor_weighted(total_amount: int, benefits: Sequence[BenefitInput]) -> AllocationResult:
    """按受益范围登记的因子（如预计销量、铺货门店数）加权分摊。"""

    if not benefits:
        raise AllocationError("受益范围不能为空")
    if any(b.factor < 0 for b in benefits):
        raise AllocationError("分摊因子不能为负")
    weights = [(b.product_id, b.owner_actor_id, b.factor) for b in benefits]
    rows = _largest_remainder(total_amount, weights)
    total_weight = sum(b.factor for b in benefits)
    lines: list[AllocationLine] = []
    for pid, owner, weight, amount, remainder in rows:
        lines.append(AllocationLine(
            product_id=pid, owner_actor_id=owner, amount=amount, weight=weight,
            ratio=_ratio_text(weight, total_weight), rule_name=RULE_FACTOR,
            reason=f"因子 {weight} / 总因子 {total_weight}，按最大余数法取整",
        ))
    gap = total_amount - sum(line.amount for line in lines)
    return AllocationResult(
        rule_name=RULE_FACTOR, total_amount=total_amount, lines=tuple(lines),
        rounding_gap=gap, rounding_to=None,
        explanation={"total_factor": total_weight, "method": "largest_remainder",
                     "basis": "受益范围因子加权"},
    )


def equal_split(total_amount: int, benefits: Sequence[BenefitInput]) -> AllocationResult:
    """在受益产品间均分，尾差按最大余数法补给编号最小的产品。"""

    if not benefits:
        raise AllocationError("受益范围不能为空")
    weights = [(b.product_id, b.owner_actor_id, 1) for b in benefits]
    rows = _largest_remainder(total_amount, weights)
    lines = [
        AllocationLine(product_id=pid, owner_actor_id=owner, amount=amount, weight=1,
                       ratio=_ratio_text(1, len(benefits)), rule_name=RULE_EQUAL,
                       reason=f"{len(benefits)} 个受益产品均分，按最大余数法取整")
        for pid, owner, _w, amount, _rem in rows
    ]
    gap = total_amount - sum(line.amount for line in lines)
    return AllocationResult(
        rule_name=RULE_EQUAL, total_amount=total_amount, lines=tuple(lines),
        rounding_gap=gap, rounding_to=None,
        explanation={"parts": len(benefits), "method": "largest_remainder", "basis": "受益产品均分"},
    )


def amount_override(total_amount: int, benefits: Sequence[BenefitInput]) -> AllocationResult:
    """按业务预先声明的金额分摊，用于有明确合同口径的场景；声明合计必须等于总额。"""

    if not benefits:
        raise AllocationError("受益范围不能为空")
    missing = [b.product_id for b in benefits if b.override_amount is None]
    if missing:
        raise AllocationError(f"以下受益产品缺少声明金额：{','.join(sorted(missing))}")
    if any(b.override_amount < 0 for b in benefits):
        raise AllocationError("声明金额不能为负")
    declared = sum(b.override_amount for b in benefits)
    if declared != total_amount:
        raise AllocationError(f"声明金额合计 {declared} 与费用总额 {total_amount} 不一致")
    lines = tuple(
        AllocationLine(product_id=b.product_id, owner_actor_id=b.owner_actor_id,
                       amount=b.override_amount, weight=b.override_amount,
                       ratio=_ratio_text(b.override_amount, total_amount),
                       rule_name=RULE_OVERRIDE, reason="按合同/申请声明金额直接归属")
        for b in benefits
    )
    return AllocationResult(
        rule_name=RULE_OVERRIDE, total_amount=total_amount, lines=lines, rounding_gap=0,
        rounding_to=None,
        explanation={"method": "declared_amount", "basis": "业务声明金额，合计已核对"},
    )


DISPATCH = {RULE_FACTOR: factor_weighted, RULE_EQUAL: equal_split, RULE_OVERRIDE: amount_override}


def allocate(rule_name: str, total_amount: int, benefits: Sequence[BenefitInput]) -> AllocationResult:
    """按规则名分派并校验总额为正。"""

    if total_amount <= 0:
        raise AllocationError("分摊总额必须大于零")
    rule = DISPATCH.get(rule_name)
    if rule is None:
        raise AllocationError(f"未知分摊规则：{rule_name}")
    products = [b.product_id for b in benefits]
    if len(set(products)) != len(products):
        raise AllocationError("受益产品重复")
    return rule(total_amount, benefits)


def explain(result: AllocationResult) -> dict[str, Any]:
    """生成可直接展示给业务负责人的分摊说明。"""

    return {
        "rule": result.rule_name,
        "basis": result.explanation.get("basis"),
        "total_amount": result.total_amount,
        "allocated_amount": sum(line.amount for line in result.lines),
        "rounding_gap": result.rounding_gap,
        "lines": [
            {"product_id": line.product_id, "amount": line.amount, "weight": line.weight,
             "ratio": line.ratio, "reason": line.reason}
            for line in result.lines
        ],
    }
