"""共享费用的可解释分摊规则。

所有金额均为整数分。每条规则输出每个受益产品的分摊金额、万分比口径和
基数说明，差额用最大余数法补给余数最大的产品，分配过程可以完整复盘。
"""

from __future__ import annotations

from typing import Any

from .errors import ValidationError

RULE_EQUAL_SHARE = "equal_share"
RULE_BENEFICIARY_WEIGHT = "beneficiary_weight"
RULE_REVENUE_BASIS = "revenue_basis"
RULE_FIXED_RATIO = "fixed_ratio"

ALLOCATION_RULES = frozenset({
    RULE_EQUAL_SHARE,
    RULE_BENEFICIARY_WEIGHT,
    RULE_REVENUE_BASIS,
    RULE_FIXED_RATIO,
})

BP_TOTAL = 10_000


def largest_remainder(total_cents: int, weights: list[tuple[str, int, int, str]],
                      ) -> list[dict[str, Any]]:
    """按权重分摊总额，使用最大余数法处理无法整除的尾差。

    weights 为 (product_id, weight, ordinal, basis_value) 列表，
    weight 必须为正整数。余数并列时取 ordinal 较小者，保证结果确定。
    """

    scale = sum(item[1] for item in weights)
    if scale <= 0:
        raise ValidationError("分摊基数合计必须大于零")
    raw: list[dict[str, Any]] = []
    for product_id, weight, ordinal, basis_value in weights:
        ratio_bp = weight * BP_TOTAL // scale
        raw.append({
            "product_id": product_id,
            "ordinal": ordinal,
            "weight": weight,
            "basis_value": basis_value,
            "ratio_bp": ratio_bp,
            "cents": total_cents * weight // scale,
            "_remainder": (total_cents * weight) % scale,
            "_residual": False,
        })
    leftover = total_cents - sum(item["cents"] for item in raw)
    bumped = sorted(raw, key=lambda r: (-r["_remainder"], r["ordinal"]))[:leftover]
    for item in bumped:
        item["cents"] += 1
        item["ratio_bp"] = min(BP_TOTAL, item["ratio_bp"] + 1)
    if bumped:
        bumped[0]["_residual"] = True
    raw.sort(key=lambda r: r["ordinal"])
    return raw


def allocate(rule: str, total_cents: int,
             beneficiaries: list[dict[str, Any]],
             inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成一份可解释的分摊草案。

    beneficiaries 为 [{"product_id", "ordinal", "weight"}]。
    revenue_basis 需要 inputs["bases"]：product_id -> 收入基数（非负整数）；
    fixed_ratio 需要 inputs["ratios_bp"]：product_id -> 万分比，合计 10000。
    返回 {"items", "residual_product_id", "inputs"}，尾差接收方一并标注。
    """

    inputs = inputs or {}
    if total_cents <= 0:
        raise ValidationError("分摊金额必须大于零")
    if not beneficiaries:
        raise ValidationError("活动至少需要一个受益产品")
    ordered = sorted(beneficiaries, key=lambda b: b["ordinal"])
    product_ids = [b["product_id"] for b in ordered]
    weights: list[tuple[str, int, int, str]]

    if rule == RULE_EQUAL_SHARE:
        weights = [(b["product_id"], 1, b["ordinal"], "均分") for b in ordered]
    elif rule == RULE_BENEFICIARY_WEIGHT:
        weights = [(b["product_id"], int(b["weight"]), b["ordinal"], f"受益权重 {b['weight']}")
                   for b in ordered]
    elif rule == RULE_REVENUE_BASIS:
        bases = inputs.get("bases") or {}
        weights = []
        for b in ordered:
            if b["product_id"] not in bases:
                raise ValidationError(f"产品 {b['product_id']} 缺少收入基数")
            try:
                value = int(bases[b["product_id"]])
            except (TypeError, ValueError) as exc:
                raise ValidationError("收入基数必须是非负整数") from exc
            if value < 0:
                raise ValidationError("收入基数必须是非负整数")
            weights.append((b["product_id"], value, b["ordinal"], f"收入基数 {value}"))
    elif rule == RULE_FIXED_RATIO:
        ratios = inputs.get("ratios_bp") or {}
        weights = []
        total_ratio = 0
        for b in ordered:
            if b["product_id"] not in ratios:
                raise ValidationError(f"产品 {b['product_id']} 缺少固定比例")
            try:
                value = int(ratios[b["product_id"]])
            except (TypeError, ValueError) as exc:
                raise ValidationError("固定比例必须是 0 到 10000 的整数（万分比）") from exc
            if not 0 <= value <= BP_TOTAL:
                raise ValidationError("固定比例必须是 0 到 10000 的整数（万分比）")
            total_ratio += value
            weights.append((b["product_id"], value, b["ordinal"], f"固定比例 {value}bp"))
        if total_ratio != BP_TOTAL:
            raise ValidationError(f"固定比例合计必须为 {BP_TOTAL}bp，当前为 {total_ratio}bp")
    else:
        raise ValidationError("分摊规则不受支持")

    items = largest_remainder(total_cents, weights)
    residual_product_id = next((item["product_id"] for item in items if item.pop("_residual", False)), None)
    for item in items:
        item.pop("_remainder", None)
    return {
        "rule": rule,
        "total_cents": total_cents,
        "products": product_ids,
        "residual_product_id": residual_product_id,
        "inputs": inputs,
        "items": items,
    }
