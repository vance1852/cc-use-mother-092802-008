"""销售费用承诺与分配系统的离线端到端验收。

覆盖一条完整链路：生命周期版本 → 年度/期间额度 → 活动受益范围 →
合同承诺与可解释分摊草案 → 分负责人确认（含超预算例外）→
跨期发票与退款 → 争议部分冻结与部分结账 → 全过程追踪与审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database

from .service import ExpenseService


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "expense_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        svc = ExpenseService(database, clock)

        base.register_organization(request_id="acc-org", actor_id="bootstrap",
                                   organization_id="org-100", name="示范酒业集团")
        base.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="u-admin",
                            display_name="财务管理员", role="admin", organization_id="org-100")
        base.register_actor(request_id="acc-boss", actor_id="u-admin", new_actor_id="u-boss",
                            display_name="分管副总", role="admin", organization_id="org-100")
        base.register_actor(request_id="acc-brand", actor_id="u-admin", new_actor_id="u-brand",
                            display_name="品牌负责人", role="operator", organization_id="org-100")
        base.register_actor(request_id="acc-region", actor_id="u-admin", new_actor_id="u-region",
                            display_name="区域负责人", role="operator", organization_id="org-100")
        base.register_actor(request_id="acc-channel", actor_id="u-admin", new_actor_id="u-channel",
                            display_name="渠道负责人", role="operator", organization_id="org-100")
        base.register_actor(request_id="acc-reviewer", actor_id="u-admin", new_actor_id="u-reviewer",
                            display_name="复核人", role="reviewer", organization_id="org-100")

        # 成熟品、新品、培育期产品各一
        products = [("sku-mature", "mature"), ("sku-new", "new"), ("sku-nurture", "nurturing")]
        for pid, stage in products:
            svc.register_product(request_id=f"acc-prod-{pid}", actor_id="u-admin",
                                 product_id=pid, organization_id="org-100", name=pid)
            svc.publish_lifecycle_version(request_id=f"acc-lc-{pid}", actor_id="u-admin",
                                          product_id=pid, stage=stage, effective_from="2026-01-01")
            annual = svc.set_annual_budget(request_id=f"acc-ab-{pid}", actor_id="u-admin",
                                           product_id=pid, year=2026, stage=stage, amount=60_000_00)
            may_amount = 1_000_00 if stage == "new" else 8_000_00
            svc.set_period_budget(request_id=f"acc-pb5-{pid}", actor_id="u-admin",
                                  budget_id=annual["resource_id"], period_key="2026M05", amount=may_amount)
            svc.set_period_budget(request_id=f"acc-pb6-{pid}", actor_id="u-admin",
                                  budget_id=annual["resource_id"], period_key="2026M06", amount=8_000_00)

        # 品牌团队发起跨产品活动
        svc.create_campaign(request_id="acc-campaign", actor_id="u-brand", campaign_id="camp-001",
                            organization_id="org-100", name="端午联合陈列", owner_team="brand",
                            starts_on="2026-05-01", ends_on="2026-06-15")
        svc.set_beneficiaries(request_id="acc-beneficiaries", actor_id="u-brand", campaign_id="camp-001",
                              beneficiaries=[
                                  {"product_id": "sku-mature", "factor": 60, "owner_actor_id": "u-brand"},
                                  {"product_id": "sku-new", "factor": 30, "owner_actor_id": "u-region"},
                                  {"product_id": "sku-nurture", "factor": 10, "owner_actor_id": "u-channel"},
                              ])
        commitment = svc.create_commitment(request_id="acc-commitment", actor_id="u-brand",
                                           campaign_id="camp-001", period_key="2026M05",
                                           total_amount=10_000_00, rule_name="factor_weighted",
                                           contract_ref="HT-2026-0501", vendor="某陈列服务商")
        commitment_id = commitment["resource_id"]
        draft_lines = {line["product_id"]: line for line in commitment["allocation"]["lines"]}
        assert draft_lines["sku-mature"]["amount"] == 6_000_00
        assert draft_lines["sku-new"]["amount"] == 3_000_00
        assert draft_lines["sku-nurture"]["amount"] == 1_000_00

        trace = svc.expense_trace(commitment_id)
        share_of = {s["product_id"]: s["share_id"] for s in trace["shares"]}

        # 新品份额超过新品阶段 M05 期间额度（3000 > 1000），需走例外授权链
        svc.confirm_share(request_id="acc-confirm-mature", actor_id="u-brand",
                          share_id=share_of["sku-mature"])
        svc.confirm_share(request_id="acc-confirm-nurture", actor_id="u-channel",
                          share_id=share_of["sku-nurture"])
        exception = svc.request_exception(request_id="acc-exception", actor_id="u-admin",
                                          commitment_id=commitment_id, over_amount=2_000_00,
                                          limit_amount=3_000_00, valid_until="2026-05-31",
                                          reason="新品上市首月加码", required_levels=2)
        svc.decide_exception(request_id="acc-exc-l1", actor_id="u-reviewer",
                             exception_id=exception["resource_id"], level=1, decision="approved")
        svc.decide_exception(request_id="acc-exc-l2", actor_id="u-boss",
                             exception_id=exception["resource_id"], level=2, decision="approved")
        svc.confirm_share(request_id="acc-confirm-new", actor_id="u-region",
                          share_id=share_of["sku-new"])

        # 生命周期切换：成熟品 6 月起转为新品阶段，已确认份额仍快照 mature
        svc.publish_lifecycle_version(request_id="acc-lc-switch", actor_id="u-admin",
                                      product_id="sku-mature", stage="new", effective_from="2026-06-01")
        trace = svc.expense_trace(commitment_id)
        mature_share = next(s for s in trace["shares"] if s["product_id"] == "sku-mature")
        assert mature_share["stage_snapshot"] == "mature"

        # 发票跨期：6 月到票，归属 2026M06；月末先在 5 月计提，发票到达红字冲回
        svc.book_accrual(request_id="acc-accrual", actor_id="u-reviewer",
                         commitment_id=commitment_id, period_key="2026M05",
                         amount=8_000_00, reason="月末已服务未到票计提")
        invoice = svc.register_invoice(request_id="acc-invoice", actor_id="u-reviewer",
                                       commitment_id=commitment_id, invoice_no="INV-2026-0601",
                                       invoice_date="2026-06-03", taxable_period="2026M06",
                                       amount=8_000_00, reverse_accrual_period="2026M05")
        # 退款回到原承诺
        svc.register_refund(request_id="acc-refund", actor_id="u-reviewer",
                            commitment_id=commitment_id, invoice_id=invoice["resource_id"],
                            amount=1_000_00, reason="部分门店未执行折让", period_key="2026M06")

        # 争议只冻结新品份额；6 月期间结账继续处理其余产品
        svc.raise_dispute(request_id="acc-dispute", actor_id="u-brand",
                          commitment_id=commitment_id, reason="新品陈列点位数量待核",
                          product_id="sku-new")
        settlement = svc.settle_period(request_id="acc-settle-6", actor_id="u-admin",
                                       period_key="2026M06")
        settled_products = {item["product_id"] for item in settlement["settled"]}
        assert "sku-new" not in settled_products, "争议份额不应被结账"
        assert {"sku-mature", "sku-nurture"} <= settled_products, "无争议份额应继续结账"
        assert {f["product_id"] for f in settlement["skipped_frozen"]} == {"sku-new"}

        # 全过程事件序列完整
        trace = svc.expense_trace(commitment_id)
        events = {e["event"] for s in trace["shares"] for e in s["ledger"]}
        assert {"reserved", "accrued", "occurred", "refunded", "frozen", "settled"} <= events

        audit_valid, audit_events = base.verify_audit()
        result = {
            "status": "ok",
            "commitment_id": commitment_id,
            "draft_total": commitment["allocation"]["allocated_amount"],
            "cross_period_invoice": invoice["cross_period"],
            "settled_without_dispute": len(settlement["settled"]),
            "frozen_skipped": len(settlement["skipped_frozen"]),
            "trace_share_count": len(trace["shares"]),
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
