import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import (BudgetExceeded, ConflictError,
                                            NotFoundError, PermissionDenied,
                                            StateError, ValidationError)
from beverage_ops_foundation.fee_service import FeeService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database


class FeeFixture(unittest.TestCase):
    """提供组织、角色、产品、生命周期与期间额度的标准测试环境。"""

    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 1, 10, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.fee = FeeService(self.database, self.clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="经营主体")
        for rid, aid, name, role in [
            ("ra", "a1", "管理员", "admin"),
            ("rf", "fin", "财务", "finance"),
            ("rb1", "bo1", "品牌主一", "brand_owner"),
            ("rb2", "bo2", "品牌主二", "brand_owner"),
            ("rm", "mgr", "经理", "manager"),
            ("rd", "dir", "总监", "director"),
            ("rc", "cfo", "财务总监", "cfo"),
            ("rr", "ro1", "区域负责人", "region_owner"),
        ]:
            self.base.register_actor(request_id=rid, actor_id="bootstrap" if aid == "a1" else "a1",
                                     new_actor_id=aid, display_name=name, role=role,
                                     organization_id="o1")
        self.fee.register_product(request_id="p1", actor_id="fin", product_id="P1",
                                  name="成熟白酒", owner_actor_id="bo1")
        self.fee.register_product(request_id="p2", actor_id="fin", product_id="P2",
                                  name="新品果酒", owner_actor_id="bo2")
        self.fee.set_lifecycle(request_id="l1", actor_id="fin", product_id="P1",
                               stage="mature", effective_from="2026-01-01")
        self.fee.set_lifecycle(request_id="l2", actor_id="fin", product_id="P2",
                               stage="new", effective_from="2026-01-01")
        self.fee.set_annual_budget(request_id="ab1", actor_id="fin", product_id="P1",
                                   year=2026, amount_cents=1_000_000,
                                   effective_from="2026-01-01")
        self.fee.set_period_budget(request_id="pb1", actor_id="fin", product_id="P1",
                                   period="2026-01", amount_cents=100_000,
                                   effective_from="2026-01-01")
        self.fee.set_annual_budget(request_id="ab2", actor_id="fin", product_id="P2",
                                   year=2026, amount_cents=500_000,
                                   effective_from="2026-01-01")
        self.fee.set_period_budget(request_id="pb2", actor_id="fin", product_id="P2",
                                   period="2026-01", amount_cents=50_000,
                                   effective_from="2026-01-01")

    def tearDown(self):
        self.database.close()

    def make_activity_commitment(self, *, activity_id="A1", commitment_id="C1",
                                 amount=100_000, products=("P1", "P2"),
                                 rule="equal_share", inputs=None, period="2026-01",
                                 actor="bo1"):
        self.fee.create_activity(request_id=f"act-{activity_id}", actor_id=actor,
                                 activity_id=activity_id, team_type="brand",
                                 name="联合推广", starts_on="2026-01-15",
                                 ends_on="2026-02-15",
                                 beneficiary_product_ids=list(products))
        self.fee.create_commitment(request_id=f"com-{commitment_id}", actor_id=actor,
                                   activity_id=activity_id,
                                   commitment_id=commitment_id,
                                   contract_ref=f"HT-{commitment_id}",
                                   amount_cents=amount, period=period,
                                   allocation_rule=rule,
                                   allocation_inputs=inputs or {})
        return commitment_id


class LifecycleTest(FeeFixture):
    def test_versions_are_append_only(self):
        versions = self.fee.list_lifecycle_versions("P2")
        self.assertEqual(["new"], [v["stage"] for v in versions])
        self.fee.set_lifecycle(request_id="l3", actor_id="fin", product_id="P2",
                               stage="nurturing", effective_from="2026-06-01")
        versions = self.fee.list_lifecycle_versions("P2")
        self.assertEqual(["new", "nurturing"], [v["stage"] for v in versions])
        self.assertEqual(2, len({v["version_id"] for v in versions}))

    def test_duplicate_effective_day_rejected(self):
        with self.assertRaises(ConflictError):
            self.fee.set_lifecycle(request_id="l-dup", actor_id="fin", product_id="P1",
                                   stage="new", effective_from="2026-01-01")

    def test_lifecycle_switch_does_not_retroact_on_shares(self):
        cid = self.make_activity_commitment()
        self.fee.set_lifecycle(request_id="l4", actor_id="fin", product_id="P2",
                               stage="nurturing", effective_from="2026-01-05")
        snapshot = self.fee.get_commitment(cid)
        stages = {s["product_id"]: s["stage_snapshot"] for s in snapshot["shares"]}
        self.assertEqual("new", stages["P2"])  # 仍为承诺发生时的新品阶段
        # 1 月 10 日新设的额度版本则命中培育期阶段。
        self.fee.set_annual_budget(request_id="ab3", actor_id="fin", product_id="P2",
                                   year=2027, amount_cents=100,
                                   effective_from="2026-01-10")
        row = self.database.connection.execute(
            "SELECT stage_snapshot FROM annual_budgets WHERE product_id='P2' AND year=2027"
        ).fetchone()
        self.assertEqual("nurturing", row["stage_snapshot"])

    def test_budget_without_lifecycle_version_rejected(self):
        self.fee.register_product(request_id="p3", actor_id="fin", product_id="P3",
                                  name="无阶段产品", owner_actor_id="bo1")
        with self.assertRaises(ValidationError):
            self.fee.set_annual_budget(request_id="abx", actor_id="fin", product_id="P3",
                                       year=2026, amount_cents=100,
                                       effective_from="2026-01-01")


class BudgetAndCommitmentTest(FeeFixture):
    def test_period_budgets_cannot_exceed_annual(self):
        with self.assertRaises(ValidationError):
            self.fee.set_period_budget(request_id="pbx", actor_id="fin", product_id="P1",
                                       period="2026-02", amount_cents=950_000,
                                       effective_from="2026-01-01")

    def test_commitment_reserves_per_product_budget(self):
        self.make_activity_commitment(amount=60_000)
        status = self.fee.budget_status("P1", "2026-01")
        self.assertEqual(30_000, status["consumed_cents"])
        self.assertEqual(70_000, status["available_cents"])

    def test_over_budget_commitment_rejected(self):
        with self.assertRaises(BudgetExceeded):
            self.make_activity_commitment(products=("P1",), amount=120_000)
        # 失败后不留任何占用。
        status = self.fee.budget_status("P1", "2026-01")
        self.assertEqual(0, status["consumed_cents"])

    def test_idempotent_replay(self):
        cid = self.make_activity_commitment()
        replay = self.fee.create_commitment(
            request_id="com-C1", actor_id="bo1", activity_id="A1", commitment_id="C1",
            contract_ref="HT-C1", amount_cents=100_000, period="2026-01",
            allocation_rule="equal_share", allocation_inputs={})
        self.assertTrue(replay.replayed)
        self.assertEqual(cid, replay.resource_id)

    def test_wrong_team_cannot_create_activity(self):
        with self.assertRaises(PermissionDenied):
            self.fee.create_activity(request_id="bad-act", actor_id="ro1",
                                     activity_id="AX", team_type="brand", name="x",
                                     starts_on="2026-01-01", ends_on="2026-01-02",
                                     beneficiary_product_ids=["P1"])

    def test_cancelled_activity_rejects_commitment(self):
        self.fee.create_activity(request_id="a-x", actor_id="bo1", activity_id="AX",
                                 team_type="brand", name="x", starts_on="2026-01-01",
                                 ends_on="2026-01-02", beneficiary_product_ids=["P1"])
        self.fee.cancel_activity(request_id="cancel-x", actor_id="bo1",
                                 activity_id="AX", reason="取消")
        with self.assertRaises(StateError):
            self.fee.create_commitment(request_id="c-x", actor_id="bo1", activity_id="AX",
                                       commitment_id="CX", contract_ref="X",
                                       amount_cents=100, period="2026-01",
                                       allocation_rule="equal_share")


class AllocationAndConfirmTest(FeeFixture):
    def test_draft_uses_rule_and_is_explainable(self):
        cid = self.make_activity_commitment(rule="revenue_basis",
                                            inputs={"bases": {"P1": 700, "P2": 300}})
        self.fee.generate_allocation(request_id="g1", actor_id="fin",
                                     commitment_id=cid,
                                     allocation_inputs={"bases": {"P1": 700, "P2": 300}})
        snapshot = self.fee.get_commitment(cid)
        p1 = next(s for s in snapshot["shares"] if s["product_id"] == "P1")
        self.assertEqual(70_000, p1["draft_cents"])
        self.assertEqual(7000, p1["ratio_bp"])
        self.assertEqual("收入基数 700", p1["basis_value"])

    def test_only_product_owner_confirms_own_share(self):
        cid = self.make_activity_commitment()
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        with self.assertRaises(PermissionDenied):
            self.fee.confirm_share(request_id="cf-x", actor_id="bo2",
                                   commitment_id=cid, product_id="P1")
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        share = next(s for s in self.fee.get_commitment(cid)["shares"]
                     if s["product_id"] == "P1")
        self.assertEqual("confirmed", share["status"])

    def test_regenerate_after_confirmation_rejected(self):
        cid = self.make_activity_commitment()
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        with self.assertRaises(StateError):
            self.fee.generate_allocation(request_id="g2", actor_id="fin",
                                         commitment_id=cid)

    def test_confirm_more_than_reserved_rejected(self):
        cid = self.make_activity_commitment()
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        with self.assertRaises(BudgetExceeded):
            self.fee.confirm_share(request_id="cf-big", actor_id="bo1",
                                   commitment_id=cid, product_id="P1",
                                   confirmed_cents=999_999)


class DisputeAndSettlementTest(FeeFixture):
    def _prepared(self, amount=100_000):
        cid = self.make_activity_commitment(amount=amount)
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        return cid

    def test_dispute_freezes_only_its_share(self):
        cid = self._prepared()
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        self.fee.raise_dispute(request_id="dp2", actor_id="bo2", commitment_id=cid,
                               product_id="P2", reason="基数存疑")
        self.fee.record_accrual(request_id="ac1", actor_id="fin", commitment_id=cid,
                                 amount_cents=100_000, period="2026-01",
                                 occurred_on="2026-01-31")
        result = self.fee.settle_commitment(request_id="st1", actor_id="fin",
                                             commitment_id=cid)
        self.assertFalse(result.replayed)
        snapshot = self.fee.get_commitment(cid)
        statuses = {s["product_id"]: s["status"] for s in snapshot["shares"]}
        self.assertEqual("settled", statuses["P1"])
        self.assertEqual("disputed", statuses["P2"])
        self.assertNotEqual("settled", snapshot["status"])

    def test_settle_after_resolution_completes_commitment(self):
        cid = self._prepared()
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        self.fee.raise_dispute(request_id="dp2", actor_id="bo2", commitment_id=cid,
                               product_id="P2", reason="基数存疑")
        self.fee.record_accrual(request_id="ac1", actor_id="fin", commitment_id=cid,
                                 amount_cents=100_000, period="2026-01",
                                 occurred_on="2026-01-31")
        self.fee.settle_commitment(request_id="st1", actor_id="fin", commitment_id=cid)
        self.fee.resolve_dispute(request_id="rd2", actor_id="fin", commitment_id=cid,
                                  product_id="P2", resolution="核对无误")
        self.fee.confirm_share(request_id="cf2", actor_id="bo2",
                               commitment_id=cid, product_id="P2")
        self.fee.settle_commitment(request_id="st2", actor_id="fin", commitment_id=cid)
        self.assertEqual("settled", self.fee.get_commitment(cid)["status"])

    def test_dispute_before_confirmation_returns_to_draft(self):
        cid = self._prepared()
        self.fee.raise_dispute(request_id="dp1", actor_id="bo1", commitment_id=cid,
                               product_id="P1", reason="草案有误")
        self.fee.resolve_dispute(request_id="rd1", actor_id="fin", commitment_id=cid,
                                  product_id="P1", resolution="维持草案")
        share = next(s for s in self.fee.get_commitment(cid)["shares"]
                     if s["product_id"] == "P1")
        self.assertEqual("draft", share["status"])

    def test_settle_releases_unused_reservation(self):
        cid = self._prepared()
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        self.fee.confirm_share(request_id="cf2", actor_id="bo2",
                               commitment_id=cid, product_id="P2")
        # 实际只发生 80000，各 40000，结账后每份额度各释放 10000。
        self.fee.record_accrual(request_id="ac1", actor_id="fin", commitment_id=cid,
                                 amount_cents=80_000, period="2026-01",
                                 occurred_on="2026-01-31")
        self.fee.settle_commitment(request_id="st1", actor_id="fin", commitment_id=cid)
        status = self.fee.budget_status("P1", "2026-01")
        self.assertEqual(40_000, status["consumed_cents"])


class CancelAndRefundTest(FeeFixture):
    def test_cancel_releases_only_unhappened_part(self):
        cid = self.make_activity_commitment(amount=100_000)
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        self.fee.confirm_share(request_id="cf2", actor_id="bo2",
                               commitment_id=cid, product_id="P2")
        self.fee.record_accrual(request_id="ac1", actor_id="fin", commitment_id=cid,
                                 amount_cents=40_000, period="2026-01",
                                 occurred_on="2026-01-20")
        self.fee.cancel_activity(request_id="cancel", actor_id="bo1",
                                 activity_id="A1", reason="活动终止")
        # P1 占用 50000、已发生 20000 -> 释放 30000，保留 20000 待结账。
        status = self.fee.budget_status("P1", "2026-01")
        self.assertEqual(20_000, status["consumed_cents"])
        snapshot = self.fee.get_commitment(cid)
        share = next(s for s in snapshot["shares"] if s["product_id"] == "P1")
        self.assertEqual(20_000, share["reserved_cents"])
        # 已发生份额仍可结账。
        self.fee.settle_commitment(request_id="st1", actor_id="fin", commitment_id=cid)

    def test_refund_returns_to_original_commitment_and_budget(self):
        cid = self.make_activity_commitment(products=("P1",), amount=50_000)
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        self.fee.record_accrual(request_id="ac1", actor_id="fin", commitment_id=cid,
                                 amount_cents=50_000, period="2026-01",
                                 occurred_on="2026-01-31")
        self.fee.record_invoice(request_id="iv1", actor_id="fin", commitment_id=cid,
                                 invoice_no="INV-1", amount_cents=50_000,
                                 period="2026-01", issued_on="2026-02-05")
        self.fee.settle_commitment(request_id="st1", actor_id="fin", commitment_id=cid)
        invoice_row = self.database.connection.execute(
            "SELECT invoice_id FROM invoice_facts WHERE invoice_no='INV-1'").fetchone()
        self.fee.record_refund(request_id="rf1", actor_id="fin", commitment_id=cid,
                                amount_cents=10_000, reason="部分撤销",
                                allocations={"P1": 10_000},
                                invoice_id=invoice_row["invoice_id"])
        snapshot = self.fee.get_commitment(cid)
        share = snapshot["shares"][0]
        self.assertEqual(10_000, share["refunded_cents"])
        status = self.fee.budget_status("P1", "2026-01")
        self.assertEqual(40_000, status["consumed_cents"])
        invoice = snapshot["invoices"][0]
        self.assertEqual(10_000, invoice["refunded_cents"])

    def test_refund_cannot_exceed_accrued_net(self):
        cid = self.make_activity_commitment(products=("P1",), amount=50_000)
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        with self.assertRaises(StateError):
            self.fee.record_refund(request_id="rf-bad", actor_id="fin",
                                    commitment_id=cid, amount_cents=1, reason="x",
                                    allocations={"P1": 1})


class CrossPeriodFactTest(FeeFixture):
    def test_cross_period_invoice_is_flagged_but_keeps_commitment(self):
        cid = self.make_activity_commitment(products=("P1",), amount=50_000)
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        self.fee.record_accrual(request_id="ac1", actor_id="fin", commitment_id=cid,
                                 amount_cents=50_000, period="2026-01",
                                 occurred_on="2026-01-31")
        self.fee.record_invoice(request_id="iv1", actor_id="fin", commitment_id=cid,
                                 invoice_no="INV-1", amount_cents=50_000,
                                 period="2026-02", issued_on="2026-02-10")
        invoice = self.fee.get_commitment(cid)["invoices"][0]
        self.assertTrue(invoice["cross_period"])
        self.assertEqual("2026-02", invoice["period"])

    def test_invoice_net_cannot_exceed_accruals(self):
        cid = self.make_activity_commitment(products=("P1",), amount=50_000)
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        self.fee.record_accrual(request_id="ac1", actor_id="fin", commitment_id=cid,
                                 amount_cents=10_000, period="2026-01",
                                 occurred_on="2026-01-31")
        with self.assertRaises(StateError):
            self.fee.record_invoice(request_id="iv-big", actor_id="fin",
                                    commitment_id=cid, invoice_no="INV-X",
                                    amount_cents=11_000, period="2026-01",
                                    issued_on="2026-01-31")


class ExceptionChainTest(FeeFixture):
    def _request(self, cap, period="2026-01", rid="e1"):
        self.fee.request_exception(request_id=rid, actor_id="bo1", product_id="P1",
                                    period=period, cap_cents=cap,
                                    valid_from="2026-01-10", valid_until="2026-03-31",
                                    reason="重点活动")
        return self.database.connection.execute(
            "SELECT exception_id FROM budget_exceptions WHERE cap_cents=? ORDER BY created_at",
            (cap,)).fetchone()["exception_id"]

    def test_small_exception_needs_one_level(self):
        eid = self._request(1_000_000)
        self.fee.decide_exception(request_id="m1", actor_id="mgr", exception_id=eid,
                                   decision="approved")
        self.assertEqual("granted", self.database.connection.execute(
            "SELECT status FROM budget_exceptions WHERE exception_id=?", (eid,)
        ).fetchone()["status"])

    def test_large_exception_needs_three_levels(self):
        eid = self._request(30_000_000)
        self.fee.decide_exception(request_id="m1", actor_id="mgr", exception_id=eid,
                                   decision="approved")
        self.assertEqual("requested", self.database.connection.execute(
            "SELECT status FROM budget_exceptions WHERE exception_id=?", (eid,)
        ).fetchone()["status"])
        with self.assertRaises(PermissionDenied):
            self.fee.decide_exception(request_id="c-early", actor_id="cfo",
                                       exception_id=eid, decision="approved")
        self.fee.decide_exception(request_id="d1", actor_id="dir", exception_id=eid,
                                   decision="approved")
        self.fee.decide_exception(request_id="c1", actor_id="cfo", exception_id=eid,
                                   decision="approved")
        self.assertEqual("granted", self.database.connection.execute(
            "SELECT status FROM budget_exceptions WHERE exception_id=?", (eid,)
        ).fetchone()["status"])

    def test_rejection_terminates_chain(self):
        eid = self._request(30_000_000)
        self.fee.decide_exception(request_id="m1", actor_id="mgr", exception_id=eid,
                                   decision="rejected", comment="依据不足")
        with self.assertRaises(StateError):
            self.fee.decide_exception(request_id="d1", actor_id="dir", exception_id=eid,
                                       decision="approved")

    def test_granted_exception_funds_overrun(self):
        with self.assertRaises(BudgetExceeded):
            self.make_activity_commitment(products=("P1",), amount=120_000)
        eid = self._request(30_000)
        self.fee.decide_exception(request_id="m1", actor_id="mgr", exception_id=eid,
                                   decision="approved")
        self.make_activity_commitment(activity_id="A2", commitment_id="C2",
                                      products=("P1",), amount=120_000)
        status = self.fee.budget_status("P1", "2026-01")
        self.assertEqual(20_000, sum(x["used_cents"] for x in status["exceptions"]))
        self.assertEqual(120_000, status["consumed_cents"])

    def test_expired_exception_not_usable(self):
        self.fee.request_exception(request_id="eold", actor_id="bo1", product_id="P1",
                                    period="2026-01", cap_cents=100_000,
                                    valid_from="2025-12-01", valid_until="2026-01-05",
                                    reason="过期窗口")
        eid = self.database.connection.execute(
            "SELECT exception_id FROM budget_exceptions WHERE valid_until='2026-01-05'"
        ).fetchone()["exception_id"]
        self.fee.decide_exception(request_id="m-old", actor_id="mgr", exception_id=eid,
                                   decision="approved")
        with self.assertRaises(BudgetExceeded):
            self.make_activity_commitment(activity_id="A9", commitment_id="C9",
                                          products=("P1",), amount=120_000)
        # 查询预算状态时过期例外被自动标记。
        self.fee.budget_status("P1", "2026-01")
        self.assertEqual("expired", self.database.connection.execute(
            "SELECT status FROM budget_exceptions WHERE exception_id=?", (eid,)
        ).fetchone()["status"])

    def test_exception_capped_by_amount(self):
        eid = self._request(10_000, rid="ecap")
        self.fee.decide_exception(request_id="mcap", actor_id="mgr", exception_id=eid,
                                   decision="approved")
        with self.assertRaises(BudgetExceeded):
            self.make_activity_commitment(activity_id="A3", commitment_id="C3",
                                          products=("P1",), amount=120_000)


class TraceTest(FeeFixture):
    def test_trace_shows_full_lifecycle(self):
        cid = self.make_activity_commitment()
        self.fee.generate_allocation(request_id="g1", actor_id="fin", commitment_id=cid)
        self.fee.confirm_share(request_id="cf1", actor_id="bo1",
                               commitment_id=cid, product_id="P1")
        self.fee.confirm_share(request_id="cf2", actor_id="bo2",
                               commitment_id=cid, product_id="P2")
        self.fee.record_accrual(request_id="ac1", actor_id="fin", commitment_id=cid,
                                 amount_cents=100_000, period="2026-01",
                                 occurred_on="2026-01-31")
        self.fee.settle_commitment(request_id="st1", actor_id="fin", commitment_id=cid)
        trace = self.fee.commitment_trace(cid)
        actions = [event["action"] for event in trace["timeline"]]
        self.assertEqual(["commitment.created", "share.allocation_drafted",
                          "share.confirmed", "share.confirmed",
                          "accrual.recorded", "share.settled"], actions)
        valid, count = self.base.verify_audit()
        self.assertTrue(valid)
        self.assertGreaterEqual(count, len(actions))


if __name__ == "__main__":
    unittest.main()
