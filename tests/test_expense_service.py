import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.storage import Database
from sales_expense.errors import (AuthorizationChainError, BudgetExceeded, ConflictError,
                                  PermissionDenied, ValidationError, WorkflowError)
from sales_expense.service import ExpenseService
from beverage_ops_foundation.service import DomainService


class ExpenseFixture(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 5, 10, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.service = ExpenseService(self.database, self.clock)
        self.base.register_organization(request_id="org01", actor_id="bootstrap",
                                        organization_id="o1", name="集团")
        self.base.register_actor(request_id="aid-admin", actor_id="bootstrap",
                                 new_actor_id="admin", display_name="管理员",
                                 role="admin", organization_id="o1")
        for rid, aid, role, name in [
                ("aid-boss", "boss", "admin", "分管副总"),
                ("aid-brand", "brand", "operator", "品牌负责人"),
                ("aid-region", "region", "operator", "区域负责人"),
                ("aid-channel", "channel", "operator", "渠道负责人"),
                ("aid-rev", "rev", "reviewer", "复核")]:
            self.base.register_actor(request_id=rid, actor_id="admin", new_actor_id=aid,
                                     display_name=name, role=role, organization_id="o1")
        for pid, stage in [("p-mature", "mature"), ("p-new", "new"), ("p-nurture", "nurturing")]:
            self.service.register_product(request_id="prod-" + pid, actor_id="admin",
                                          product_id=pid, organization_id="o1", name=pid)
            self.service.publish_lifecycle_version(request_id="lc-" + pid, actor_id="admin",
                                                   product_id=pid, stage=stage,
                                                   effective_from="2026-01-01")

    def tearDown(self):
        self.database.close()

    def budget(self, pid, stage, period, amount, year=2026):
        annual = self.service.set_annual_budget(
            request_id=f"ab-{pid}-{period}", actor_id="admin", product_id=pid,
            year=year, stage=stage, amount=max(amount * 12, amount))
        return self.service.set_period_budget(
            request_id=f"pb-{pid}-{period}", actor_id="admin",
            budget_id=annual["resource_id"], period_key=period, amount=amount)

    def campaign_with_commitment(self, total=1000, period="2026M05", factors=(50, 30, 20),
                                 rule="factor_weighted"):
        products = ["p-mature", "p-new", "p-nurture"]
        owners = ["brand", "region", "channel"]
        self.service.create_campaign(request_id="camp-1", actor_id="brand", campaign_id="c1",
                                     organization_id="o1", name="联合活动", owner_team="brand",
                                     starts_on="2026-05-01", ends_on="2026-05-31")
        self.service.set_beneficiaries(request_id="ben-1", actor_id="brand", campaign_id="c1",
                                       beneficiaries=[
                                           {"product_id": p, "factor": f, "owner_actor_id": o}
                                           for p, f, o in zip(products, factors, owners)])
        commitment = self.service.create_commitment(
            request_id="com-1", actor_id="brand", campaign_id="c1", period_key=period,
            total_amount=total, rule_name=rule)
        return commitment["resource_id"]

    def shares(self, commitment_id):
        return {s["product_id"]: s["share_id"]
                for s in self.service.expense_trace(commitment_id)["shares"]}


class LifecycleBudgetTest(ExpenseFixture):
    def test_versions_chain_and_effective_lookup(self):
        self.service.publish_lifecycle_version(
            request_id="lc-switch", actor_id="admin", product_id="p-mature",
            stage="new", effective_from="2026-07-01")
        versions = self.service.list_lifecycle_versions("p-mature")
        self.assertEqual("superseded", versions[0]["status"])
        self.assertEqual("active", versions[1]["status"])
        self.assertEqual("2026-06-30", versions[0]["effective_to"])
        self.assertEqual("mature", self.service.effective_version("p-mature", "2026-06-30")["stage"])
        self.assertEqual("new", self.service.effective_version("p-mature", "2026-07-01")["stage"])

    def test_cannot_publish_older_effective_date(self):
        with self.assertRaises(ValidationError):
            self.service.publish_lifecycle_version(
                request_id="lc-bad", actor_id="admin", product_id="p-mature",
                stage="new", effective_from="2025-12-31")

    def test_lifecycle_switch_does_not_retroact_confirmed_share(self):
        for pid, stage in [("p-mature", "mature"), ("p-new", "new"), ("p-nurture", "nurturing")]:
            self.budget(pid, stage, "2026M05", 100000)
        cid = self.campaign_with_commitment()
        shares = self.shares(cid)
        self.service.confirm_share(
            request_id="cf-1", actor_id="brand", share_id=shares["p-mature"])
        self.service.confirm_share(request_id="cf-2", actor_id="region", share_id=shares["p-new"])
        self.service.confirm_share(request_id="cf-3", actor_id="channel", share_id=shares["p-nurture"])
        self.service.publish_lifecycle_version(
            request_id="lc-switch2", actor_id="admin", product_id="p-mature",
            stage="new", effective_from="2026-06-01")
        trace = self.service.expense_trace(cid)
        share = next(s for s in trace["shares"] if s["product_id"] == "p-mature")
        self.assertEqual("mature", share["stage_snapshot"])

    def test_budget_cannot_shrink_below_usage(self):
        self.budget("p-mature", "mature", "2026M05", 500)
        cid = self.campaign_with_commitment()
        shares = self.shares(cid)
        self.service.confirm_share(request_id="cf-x", actor_id="brand",
                                   share_id=shares["p-mature"])
        with self.assertRaises(ConflictError):
            annual = self.service.budget_status("p-mature", 2026)["budgets"][0]["budget_id"]
            self.service.set_period_budget(request_id="pb-shrink", actor_id="admin",
                                           budget_id=annual, period_key="2026M05", amount=100)


class ConfirmationAndOwnerTest(ExpenseFixture):
    def test_only_owner_confirms_own_share(self):
        for pid, stage in [("p-mature", "mature"), ("p-new", "new"), ("p-nurture", "nurturing")]:
            self.budget(pid, stage, "2026M05", 100000)
        cid = self.campaign_with_commitment()
        shares = self.shares(cid)
        with self.assertRaises(PermissionDenied):
            self.service.confirm_share(request_id="cf-deny", actor_id="region",
                                       share_id=shares["p-mature"])
        self.service.confirm_share(request_id="cf-ok", actor_id="brand",
                                   share_id=shares["p-mature"])
        with self.assertRaises(WorkflowError):
            self.service.confirm_share(request_id="cf-again", actor_id="brand",
                                       share_id=shares["p-mature"])

    def test_draft_does_not_reserve_budget(self):
        self.budget("p-mature", "mature", "2026M05", 0)
        cid = self.campaign_with_commitment()
        status = self.service.budget_status("p-mature", 2026)
        self.assertEqual(0, status["budgets"][0]["periods"][0]["reserved"])
        shares = self.shares(cid)
        with self.assertRaises(BudgetExceeded):
            self.service.confirm_share(request_id="cf-block", actor_id="brand",
                                       share_id=shares["p-mature"])


class ExceptionChainTest(ExpenseFixture):
    def _over_budget_commitment(self):
        # p-mature 份额 500，期间额度只有 400
        self.budget("p-mature", "mature", "2026M05", 400)
        self.budget("p-new", "new", "2026M05", 100000)
        self.budget("p-nurture", "nurturing", "2026M05", 100000)
        cid = self.campaign_with_commitment()
        return cid, self.shares(cid)

    def test_requires_exception_chain_in_order_with_distinct_people(self):
        cid, shares = self._over_budget_commitment()
        with self.assertRaises(BudgetExceeded):
            self.service.confirm_share(request_id="cf1", actor_id="brand",
                                       share_id=shares["p-mature"])
        exc = self.service.request_exception(
            request_id="exc1", actor_id="admin", commitment_id=cid,
            over_amount=100, limit_amount=100, valid_until="2026-05-31",
            reason="超支", required_levels=2)
        with self.assertRaises(AuthorizationChainError):
            self.service.decide_exception(request_id="d-order", actor_id="boss",
                                          exception_id=exc["resource_id"], level=2,
                                          decision="approved")
        # 申请人不能自批
        with self.assertRaises(AuthorizationChainError):
            self.service.decide_exception(request_id="d-self", actor_id="admin",
                                          exception_id=exc["resource_id"], level=1,
                                          decision="approved")
        self.service.decide_exception(request_id="d-l1", actor_id="rev",
                                      exception_id=exc["resource_id"], level=1,
                                      decision="approved")
        # 同一人不能重复担任其他级别
        with self.assertRaises(AuthorizationChainError):
            self.service.decide_exception(request_id="d-same", actor_id="rev",
                                          exception_id=exc["resource_id"], level=2,
                                          decision="approved")
        self.service.decide_exception(request_id="d-l2", actor_id="boss",
                                      exception_id=exc["resource_id"], level=2,
                                      decision="approved")
        result = self.service.confirm_share(request_id="cf2", actor_id="brand",
                                            share_id=shares["p-mature"])
        self.assertEqual(100, result["over_amount"])
        self.assertIsNotNone(result["exception_id"])

    def test_exception_limit_and_consumption(self):
        cid, shares = self._over_budget_commitment()
        exc = self.service.request_exception(
            request_id="exc2", actor_id="admin", commitment_id=cid,
            over_amount=50, limit_amount=50, valid_until="2026-05-31",
            reason="小额", required_levels=1)
        self.service.decide_exception(request_id="d1", actor_id="rev",
                                      exception_id=exc["resource_id"], level=1,
                                      decision="approved")
        with self.assertRaises(BudgetExceeded):
            self.service.confirm_share(request_id="cf3", actor_id="brand",
                                       share_id=shares["p-mature"])  # 超100 > 上限50

    def test_exception_validity_window(self):
        cid, shares = self._over_budget_commitment()
        exc = self.service.request_exception(
            request_id="exc3", actor_id="admin", commitment_id=cid,
            over_amount=100, limit_amount=100, valid_until="2026-05-10",
            reason="当日有效", required_levels=1)
        self.service.decide_exception(request_id="d1", actor_id="rev",
                                      exception_id=exc["resource_id"], level=1,
                                      decision="approved")
        # 推进到次日，例外过期
        from beverage_ops_foundation.clock import FixedClock as _FC
        from datetime import datetime as _dt, timezone as _tz
        self.service.clock = _FC(_dt(2026, 5, 11, tzinfo=_tz.utc))
        with self.assertRaises(BudgetExceeded):
            self.service.confirm_share(request_id="cf4", actor_id="brand",
                                       share_id=shares["p-mature"])

    def test_rejection_closes_chain(self):
        cid, shares = self._over_budget_commitment()
        exc = self.service.request_exception(
            request_id="exc4", actor_id="admin", commitment_id=cid,
            over_amount=100, limit_amount=100, valid_until="2026-05-31",
            reason="被拒", required_levels=2)
        self.service.decide_exception(request_id="d1", actor_id="rev",
                                      exception_id=exc["resource_id"], level=1,
                                      decision="rejected")
        with self.assertRaises(WorkflowError):
            self.service.decide_exception(request_id="d2", actor_id="boss",
                                          exception_id=exc["resource_id"], level=2,
                                          decision="approved")


class InvoiceRefundCancelTest(ExpenseFixture):
    def _confirmed(self, total=1000, amount=100000):
        for pid, stage in [("p-mature", "mature"), ("p-new", "new"), ("p-nurture", "nurturing")]:
            self.budget(pid, stage, "2026M05", amount)
        cid = self.campaign_with_commitment(total=total)
        shares = self.shares(cid)
        self.service.confirm_share(request_id="c1", actor_id="brand", share_id=shares["p-mature"])
        self.service.confirm_share(request_id="c2", actor_id="region", share_id=shares["p-new"])
        self.service.confirm_share(request_id="c3", actor_id="channel", share_id=shares["p-nurture"])
        return cid, shares

    def test_invoice_cannot_exceed_commitment(self):
        cid, _ = self._confirmed()
        with self.assertRaises(ConflictError):
            self.service.register_invoice(request_id="iv1", actor_id="brand", commitment_id=cid,
                                          invoice_no="I1", invoice_date="2026-05-20",
                                          taxable_period="2026M05", amount=1001)

    def test_refund_returns_to_original_commitment_shares(self):
        cid, _ = self._confirmed()
        reserved_after_confirm = {s["product_id"]: s["reserved_amount"]
                                  for s in self.service.expense_trace(cid)["shares"]}
        invoice = self.service.register_invoice(
            request_id="iv2", actor_id="brand", commitment_id=cid, invoice_no="I2",
            invoice_date="2026-05-20", taxable_period="2026M05", amount=1000)
        after_invoice = {s["product_id"]: s["reserved_amount"]
                         for s in self.service.expense_trace(cid)["shares"]}
        self.assertTrue(all(v == 0 for v in after_invoice.values()))
        self.service.register_refund(request_id="rf1", actor_id="brand", commitment_id=cid,
                                     invoice_id=invoice["resource_id"], amount=1000,
                                     reason="全额退", period_key="2026M05")
        after_refund = {s["product_id"]: s["reserved_amount"]
                        for s in self.service.expense_trace(cid)["shares"]}
        # 发票把占用转为发生，退款再按原份额恢复承诺占用
        self.assertEqual({500, 300, 200}, set(reserved_after_confirm.values()))
        self.assertEqual(reserved_after_confirm, after_refund)
        with self.assertRaises(ConflictError):
            self.service.register_refund(request_id="rf2", actor_id="brand", commitment_id=cid,
                                         invoice_id=invoice["resource_id"], amount=1,
                                         reason="超额退", period_key="2026M05")

    def test_cancel_releases_only_unoccurred_and_keeps_facts(self):
        cid, _ = self._confirmed()
        self.service.register_invoice(request_id="iv3", actor_id="brand", commitment_id=cid,
                                      invoice_no="I3", invoice_date="2026-05-20",
                                      taxable_period="2026M05", amount=400)
        result = self.service.cancel_commitment(request_id="cx1", actor_id="brand",
                                                commitment_id=cid, reason="取消")
        self.assertEqual(600, result["released_total"])
        trace = self.service.expense_trace(cid)
        self.assertEqual(1, len(trace["invoices"]))
        self.assertTrue(all(s["status"] == "released" for s in trace["shares"]))
        with self.assertRaises(WorkflowError):
            self.service.register_invoice(request_id="iv4", actor_id="brand", commitment_id=cid,
                                          invoice_no="I4", invoice_date="2026-05-21",
                                          taxable_period="2026M05", amount=10)

    def test_cross_period_invoice_reverses_accrual(self):
        for pid, stage in [("p-mature", "mature"), ("p-new", "new"), ("p-nurture", "nurturing")]:
            annual = self.service.set_annual_budget(
                request_id=f"ab2-{pid}", actor_id="admin", product_id=pid, year=2026,
                stage=stage, amount=1000000)
            for period in ("2026M05", "2026M06"):
                self.service.set_period_budget(
                    request_id=f"pb2-{pid}-{period}", actor_id="admin",
                    budget_id=annual["resource_id"], period_key=period, amount=1000000)
        cid = self.campaign_with_commitment()
        shares = self.shares(cid)
        for actor, pid in [("brand", "p-mature"), ("region", "p-new"), ("channel", "p-nurture")]:
            self.service.confirm_share(request_id=f"c-{pid}", actor_id=actor,
                                       share_id=shares[pid])
        self.service.book_accrual(request_id="ac1", actor_id="rev", commitment_id=cid,
                                  period_key="2026M05", amount=1000, reason="计提")
        self.service.register_invoice(request_id="iv5", actor_id="rev", commitment_id=cid,
                                      invoice_no="I5", invoice_date="2026-06-05",
                                      taxable_period="2026M06", amount=1000,
                                      reverse_accrual_period="2026M05")
        status = self.service.budget_status("p-mature", 2026)
        periods = {p["period_key"]: p for p in status["budgets"][0]["periods"]}
        self.assertEqual(0, periods["2026M05"]["occurred"])
        self.assertEqual(500, periods["2026M06"]["occurred"])


class DisputeSettlementTest(ExpenseFixture):
    def test_dispute_freezes_only_target_share(self):
        for pid, stage in [("p-mature", "mature"), ("p-new", "new"), ("p-nurture", "nurturing")]:
            self.budget(pid, stage, "2026M05", 100000)
        cid = self.campaign_with_commitment()
        shares = self.shares(cid)
        for actor, pid in [("brand", "p-mature"), ("region", "p-new"), ("channel", "p-nurture")]:
            self.service.confirm_share(request_id=f"cf-{pid}", actor_id=actor,
                                       share_id=shares[pid])
        self.service.register_invoice(request_id="iv", actor_id="brand", commitment_id=cid,
                                      invoice_no="I", invoice_date="2026-05-20",
                                      taxable_period="2026M05", amount=1000)
        self.service.raise_dispute(request_id="dp1", actor_id="brand", commitment_id=cid,
                                   reason="新品有异议", product_id="p-new")
        trace = self.service.expense_trace(cid)
        statuses = {s["product_id"]: s["status"] for s in trace["shares"]}
        self.assertEqual("frozen", statuses["p-new"])
        self.assertNotEqual("frozen", statuses["p-mature"])

        result = self.service.settle_period(request_id="sp1", actor_id="admin",
                                            period_key="2026M05")
        settled = {item["product_id"] for item in result["settled"]}
        skipped = {item["product_id"] for item in result["skipped_frozen"]}
        self.assertEqual({"p-mature", "p-nurture"}, settled)
        self.assertEqual({"p-new"}, skipped)

        with self.assertRaises(WorkflowError):
            self.service.settle_share(request_id="ss1", actor_id="admin",
                                      share_id=shares["p-new"])

        dispute_id = trace["disputes"][0]["dispute_id"]
        self.service.resolve_dispute(request_id="rd1", actor_id="admin",
                                     dispute_id=dispute_id, resolution="按原方案")
        self.service.settle_share(request_id="ss2", actor_id="admin",
                                  share_id=shares["p-new"])
        final = self.service.expense_trace(cid)
        self.assertEqual("settled", next(s for s in final["shares"]
                                         if s["product_id"] == "p-new")["status"])


class TraceTest(ExpenseFixture):
    def test_trace_records_full_lifecycle_events(self):
        for pid, stage in [("p-mature", "mature"), ("p-new", "new"), ("p-nurture", "nurturing")]:
            self.budget(pid, stage, "2026M05", 100000)
        cid = self.campaign_with_commitment()
        shares = self.shares(cid)
        self.service.confirm_share(request_id="t1", actor_id="brand",
                                   share_id=shares["p-mature"])
        self.service.register_invoice(request_id="t2", actor_id="brand", commitment_id=cid,
                                      invoice_no="IT", invoice_date="2026-05-20",
                                      taxable_period="2026M05", amount=500)
        self.service.cancel_commitment(request_id="t3", actor_id="brand",
                                       commitment_id=cid, reason="终止")
        trace = self.service.expense_trace(cid)
        mature = next(s for s in trace["shares"] if s["product_id"] == "p-mature")
        events = [entry["event"] for entry in mature["ledger"]]
        self.assertEqual(["reserved", "occurred", "released"], events)
        # 分摊说明可解释
        self.assertIn("ratio", mature["explanation"])
        self.assertEqual("factor_weighted", mature["rule_name"])
        self.assertEqual(1, len(trace["allocation_runs"]))


class IdempotencyTest(ExpenseFixture):
    def test_same_request_replays_and_different_payload_conflicts(self):
        self.budget("p-mature", "mature", "2026M05", 100000)
        first = self.service.register_product(
            request_id="dup-prod", actor_id="admin", product_id="p-dup",
            organization_id="o1", name="重复产品")
        second = self.service.register_product(
            request_id="dup-prod", actor_id="admin", product_id="p-dup",
            organization_id="o1", name="重复产品")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])
        with self.assertRaises(ConflictError):
            self.service.register_product(
                request_id="dup-prod", actor_id="admin", product_id="p-other",
                organization_id="o1", name="换内容")


if __name__ == "__main__":
    unittest.main()
