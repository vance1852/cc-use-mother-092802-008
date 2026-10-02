"""销售费用承诺与分配系统的离线端到端验收。

在临时 SQLite 数据库中走通：生命周期生效版本 -> 年度/期间额度 ->
多产品共享活动承诺 -> 可解释分摊草案 -> 负责人逐份额确认 -> 争议仅冻结
相关份额且无争议部分继续结账 -> 跨期发票 -> 退款回原承诺 -> 超预算例外
授权链 -> 全过程时间线与审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .fee_service import FeeService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整的费用承诺链并返回可核对的结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "fee_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 1, 20, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        fee = FeeService(database, clock)

        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="org-1", name="示范酒业集团")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-1",
                            display_name="管理员", role="admin", organization_id="org-1")
        base.register_actor(request_id="finance", actor_id="admin-1", new_actor_id="fin-1",
                            display_name="费用会计", role="finance", organization_id="org-1")
        base.register_actor(request_id="owner-a", actor_id="admin-1", new_actor_id="brand-a",
                            display_name="品牌经理甲", role="brand_owner",
                            organization_id="org-1")
        base.register_actor(request_id="owner-b", actor_id="admin-1", new_actor_id="brand-b",
                            display_name="品牌经理乙", role="brand_owner",
                            organization_id="org-1")
        base.register_actor(request_id="manager", actor_id="admin-1", new_actor_id="mgr-1",
                            display_name="销售经理", role="manager", organization_id="org-1")

        # 两个产品：成熟产品与新品，生命周期分别建档。
        fee.register_product(request_id="prod-a", actor_id="fin-1", product_id="wine-mature",
                             name="陈酿大曲", owner_actor_id="brand-a")
        fee.register_product(request_id="prod-b", actor_id="fin-1", product_id="wine-new",
                             name="气泡清酒", owner_actor_id="brand-b")
        fee.set_lifecycle(request_id="life-a", actor_id="fin-1", product_id="wine-mature",
                          stage="mature", effective_from="2026-01-01")
        fee.set_lifecycle(request_id="life-b", actor_id="fin-1", product_id="wine-new",
                          stage="new", effective_from="2026-01-01")

        # 年度与期间额度（金额单位：分）。
        fee.set_annual_budget(request_id="annual-a", actor_id="fin-1",
                              product_id="wine-mature", year=2026,
                              amount_cents=2_000_000, effective_from="2026-01-01")
        fee.set_period_budget(request_id="period-a", actor_id="fin-1",
                              product_id="wine-mature", period="2026-01",
                              amount_cents=200_000, effective_from="2026-01-01")
        fee.set_annual_budget(request_id="annual-b", actor_id="fin-1",
                              product_id="wine-new", year=2026,
                              amount_cents=1_000_000, effective_from="2026-01-01")
        fee.set_period_budget(request_id="period-b", actor_id="fin-1",
                              product_id="wine-new", period="2026-01",
                              amount_cents=80_000, effective_from="2026-01-01")

        # 品牌团队提交同时受益两个产品的活动与合同承诺，按收入基数分摊。
        fee.create_activity(request_id="activity", actor_id="brand-a", activity_id="act-spring",
                            team_type="brand", name="春节双品联动",
                            starts_on="2026-01-25", ends_on="2026-02-20",
                            beneficiary_product_ids=["wine-mature", "wine-new"])
        fee.create_commitment(request_id="commitment", actor_id="brand-a",
                              activity_id="act-spring", commitment_id="com-001",
                              contract_ref="HT-2026-0108", description="双品陈列与品鉴",
                              amount_cents=120_000, period="2026-01",
                              allocation_rule="revenue_basis",
                              allocation_inputs={"bases": {"wine-mature": 700,
                                                           "wine-new": 300}})
        fee.generate_allocation(request_id="allocation", actor_id="fin-1",
                                commitment_id="com-001",
                                allocation_inputs={"bases": {"wine-mature": 700,
                                                             "wine-new": 300}})
        snapshot = fee.get_commitment("com-001")
        draft = {s["product_id"]: s["draft_cents"] for s in snapshot["shares"]}
        assert draft == {"wine-mature": 84_000, "wine-new": 36_000}, draft

        # 成熟产品负责人确认自己份额；新品份额进入争议，只冻结新品。
        fee.confirm_share(request_id="confirm-a", actor_id="brand-a",
                          commitment_id="com-001", product_id="wine-mature")
        fee.raise_dispute(request_id="dispute-b", actor_id="brand-b",
                          commitment_id="com-001", product_id="wine-new",
                          reason="门店收入基数需与经销商复核")

        # 费用发生并登记应计；2 月收到跨期发票，事实回到 1 月原承诺。
        fee.record_accrual(request_id="accrual", actor_id="fin-1",
                           commitment_id="com-001", amount_cents=120_000,
                           period="2026-01", occurred_on="2026-01-31",
                           description="一月陈列与品鉴服务费")
        fee.record_invoice(request_id="invoice", actor_id="fin-1",
                           commitment_id="com-001", invoice_no="FP-202602-009",
                           amount_cents=120_000, period="2026-02",
                           issued_on="2026-02-08")

        # 无争议的成熟产品份额先结账，新品份额保持冻结。
        fee.settle_commitment(request_id="settle-a", actor_id="fin-1",
                              commitment_id="com-001")
        after_first = {s["product_id"]: s["status"]
                       for s in fee.get_commitment("com-001")["shares"]}
        assert after_first["wine-mature"] == "settled"
        assert after_first["wine-new"] == "disputed"

        # 争议解决后新品负责人确认，第二份额结账，承诺整体结清。
        fee.resolve_dispute(request_id="resolve-b", actor_id="fin-1",
                            commitment_id="com-001", product_id="wine-new",
                            resolution="经销商确认收入基数，维持 30% 草案")
        fee.confirm_share(request_id="confirm-b", actor_id="brand-b",
                          commitment_id="com-001", product_id="wine-new")
        fee.settle_commitment(request_id="settle-b", actor_id="fin-1",
                              commitment_id="com-001")

        # 部分活动取消退款，回到原承诺并释放成熟产品预算。
        invoice_id = next(i["invoice_id"] for i in fee.get_commitment("com-001")["invoices"])
        fee.record_refund(request_id="refund", actor_id="fin-1", commitment_id="com-001",
                          amount_cents=12_000, reason="三家门店未执行陈列",
                          allocations={"wine-mature": 12_000}, invoice_id=invoice_id)

        # 后续活动超预算：先被拦截，经理级例外授权（限额与有效期）后才能占用。
        fee.create_activity(request_id="activity-2", actor_id="brand-a",
                            activity_id="act-tasting", team_type="brand",
                            name="新春品鉴会", starts_on="2026-01-28",
                            ends_on="2026-01-30",
                            beneficiary_product_ids=["wine-mature"])
        try:
            fee.create_commitment(request_id="commitment-2-blocked", actor_id="brand-a",
                                  activity_id="act-tasting", commitment_id="com-002",
                                  contract_ref="HT-2026-0119", amount_cents=180_000,
                                  period="2026-01", allocation_rule="equal_share")
            overrun_blocked = False
        except Exception:
            overrun_blocked = True
        fee.request_exception(request_id="exception", actor_id="brand-a",
                              product_id="wine-mature", period="2026-01",
                              cap_cents=100_000, valid_from="2026-01-20",
                              valid_until="2026-02-28", reason="春节重点品鉴会追加投入")
        exception_id = database.connection.execute(
            "SELECT exception_id FROM budget_exceptions WHERE reason='春节重点品鉴会追加投入'"
        ).fetchone()["exception_id"]
        fee.decide_exception(request_id="exception-approve", actor_id="mgr-1",
                             exception_id=exception_id, decision="approved",
                             comment="同意在限额与期限内执行")
        fee.create_commitment(request_id="commitment-2", actor_id="brand-a",
                              activity_id="act-tasting", commitment_id="com-002",
                              contract_ref="HT-2026-0119", amount_cents=180_000,
                              period="2026-01", allocation_rule="equal_share")

        trace = fee.commitment_trace("com-001")
        actions = [event["action"] for event in trace["timeline"]]
        valid, audit_count = base.verify_audit()
        status_mature = fee.budget_status("wine-mature", "2026-01")
        database.close()
        return {
            "status": "ok",
            "draft": draft,
            "first_settle": after_first,
            "final_commitment_status": trace["commitment"]["status"],
            "cross_period_invoice": trace["commitment"]["invoices"][0]["cross_period"],
            "refunded_cents": trace["commitment"]["shares"][0]["refunded_cents"],
            "overrun_blocked_before_exception": overrun_blocked,
            "mature_consumed_cents": status_mature["consumed_cents"],
            "exception_used_cents": sum(item["used_cents"]
                                        for item in status_mature["exceptions"]),
            "timeline_actions": actions,
            "audit_events": audit_count,
            "audit_valid": valid,
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
