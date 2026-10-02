import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database
from sales_expense.api import route
from sales_expense.service import ExpenseService


class ExpenseApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 5, 10, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = ExpenseService(self.database, clock)
        self.base.register_organization(request_id="org01", actor_id="bootstrap",
                                        organization_id="o1", name="集团")
        self.base.register_actor(request_id="aid-admin", actor_id="bootstrap",
                                 new_actor_id="admin", display_name="管理员",
                                 role="admin", organization_id="o1")
        self.base.register_actor(request_id="aid-brand", actor_id="admin",
                                 new_actor_id="brand", display_name="品牌",
                                 role="operator", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def _headers(self, actor="admin"):
        return {"X-Actor-Id": actor}

    def test_unknown_route_and_missing_actor(self):
        status, payload = route(self.service, "GET", "/expense/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])
        # 已提供 request_id 但无 actor -> 操作者不存在
        status, payload = route(self.service, "POST", "/expense/products",
                                {"request_id": "r0", "product_id": "p1",
                                 "organization_id": "o1", "name": "x"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_product_lifecycle_budget_flow_over_http(self):
        status, payload = route(self.service, "POST", "/expense/products",
                                {"request_id": "r1", "product_id": "p1",
                                 "organization_id": "o1", "name": "成熟品"}, self._headers())
        self.assertEqual(201, status)
        self.assertEqual("p1", payload["resource_id"])

        status, payload = route(self.service, "POST", "/expense/lifecycle-versions",
                                {"request_id": "r2", "product_id": "p1", "stage": "mature",
                                 "effective_from": "2026-01-01"}, self._headers())
        self.assertEqual(201, status)

        status, payload = route(self.service, "POST", "/expense/budgets/annual",
                                {"request_id": "r3", "product_id": "p1", "year": 2026,
                                 "stage": "mature", "amount": 12000}, self._headers())
        self.assertEqual(201, status)
        budget_id = payload["resource_id"]

        status, payload = route(self.service, "POST", "/expense/budgets/periods",
                                {"request_id": "r4", "budget_id": budget_id,
                                 "period_key": "2026M05", "amount": 1000}, self._headers())
        self.assertEqual(201, status)

        status, payload = route(self.service, "GET",
                                "/expense/budgets?product_id=p1&year=2026", None)
        self.assertEqual(200, status)
        self.assertEqual("mature", payload["budgets"][0]["stage"])
        self.assertEqual(1000, payload["budgets"][0]["periods"][0]["budget"])

    def test_commitment_and_trace_over_http(self):
        route(self.service, "POST", "/expense/products",
              {"request_id": "p1", "product_id": "p1", "organization_id": "o1", "name": "品1"},
              self._headers())
        route(self.service, "POST", "/expense/lifecycle-versions",
              {"request_id": "l1", "product_id": "p1", "stage": "mature",
               "effective_from": "2026-01-01"}, self._headers())
        ab = route(self.service, "POST", "/expense/budgets/annual",
                   {"request_id": "a1", "product_id": "p1", "year": 2026,
                    "stage": "mature", "amount": 100000}, self._headers())[1]
        route(self.service, "POST", "/expense/budgets/periods",
              {"request_id": "q1", "budget_id": ab["resource_id"],
               "period_key": "2026M05", "amount": 100000}, self._headers())
        route(self.service, "POST", "/expense/campaigns",
              {"request_id": "c1", "campaign_id": "c1", "organization_id": "o1",
               "name": "活动", "owner_team": "brand", "starts_on": "2026-05-01",
               "ends_on": "2026-05-31"}, self._headers("brand"))
        route(self.service, "POST", "/expense/beneficiaries",
              {"request_id": "b1", "campaign_id": "c1",
               "beneficiaries": [{"product_id": "p1", "factor": 1, "owner_actor_id": "brand"}]},
              self._headers("brand"))
        status, commitment = route(self.service, "POST", "/expense/commitments",
                                   {"request_id": "m1", "campaign_id": "c1",
                                    "period_key": "2026M05", "total_amount": 500,
                                    "rule_name": "factor_weighted"}, self._headers("brand"))
        self.assertEqual(201, status)
        cid = commitment["resource_id"]

        status, payload = route(self.service, "GET", f"/expense/trace?commitment_id={cid}", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["shares"]))
        self.assertEqual("draft", payload["shares"][0]["status"])

    def test_domain_error_maps_to_status(self):
        status, payload = route(self.service, "POST", "/expense/lifecycle-versions",
                                {"request_id": "x1", "product_id": "missing", "stage": "mature",
                                 "effective_from": "2026-01-01"}, self._headers())
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
