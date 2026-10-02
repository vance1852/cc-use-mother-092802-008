import json
import unittest

from beverage_ops_foundation.api import route
from beverage_ops_foundation.fee_service import FeeService
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.storage import Database


class FeeApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.fee = FeeService(self.database)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="经营主体")
        self.service.register_actor(request_id="ra", actor_id="bootstrap",
                                    new_actor_id="a1", display_name="管理员",
                                    role="admin", organization_id="o1")
        self.service.register_actor(request_id="rf", actor_id="a1",
                                    new_actor_id="fin", display_name="财务",
                                    role="finance", organization_id="o1")
        self.service.register_actor(request_id="rb", actor_id="a1",
                                    new_actor_id="bo1", display_name="品牌主",
                                    role="brand_owner", organization_id="o1")
        self.service.register_actor(request_id="rm", actor_id="a1",
                                    new_actor_id="mgr", display_name="经理",
                                    role="manager", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="fin"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor}, fee=self.fee)

    def test_product_and_budget_routes(self):
        status, payload = self.call("POST", "/products", {
            "request_id": "p1", "product_id": "P1", "name": "成熟白酒",
            "owner_actor_id": "bo1"})
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/lifecycle-versions", {
            "request_id": "l1", "product_id": "P1", "stage": "mature",
            "effective_from": "2026-01-01"})
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/annual-budgets", {
            "request_id": "ab1", "product_id": "P1", "year": 2026,
            "amount_cents": 1_000_000, "effective_from": "2026-01-01"})
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/period-budgets", {
            "request_id": "pb1", "product_id": "P1", "period": "2026-01",
            "amount_cents": 100_000, "effective_from": "2026-01-01"})
        self.assertEqual(201, status)
        status, payload = self.call("GET", "/budget-status?product_id=P1&period=2026-01")
        self.assertEqual(200, status)
        self.assertEqual("mature", payload["stage"])
        self.assertEqual(100_000, payload["budget_cents"])

    def test_commitment_flow_through_routes(self):
        self.call("POST", "/products", {"request_id": "p1", "product_id": "P1",
                                        "name": "白酒", "owner_actor_id": "bo1"})
        self.call("POST", "/lifecycle-versions", {"request_id": "l1", "product_id": "P1",
                                                   "stage": "mature",
                                                   "effective_from": "2026-01-01"})
        self.call("POST", "/annual-budgets", {"request_id": "ab1", "product_id": "P1",
                                              "year": 2026, "amount_cents": 1_000_000,
                                              "effective_from": "2026-01-01"})
        self.call("POST", "/period-budgets", {"request_id": "pb1", "product_id": "P1",
                                              "period": "2026-01", "amount_cents": 100_000,
                                              "effective_from": "2026-01-01"})
        status, payload = self.call("POST", "/activities", {
            "request_id": "a1", "activity_id": "A1",
            "team_type": "brand", "name": "推广", "starts_on": "2026-01-15",
            "ends_on": "2026-02-15", "beneficiary_product_ids": ["P1"]}, actor="bo1")
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/commitments", {
            "request_id": "c1", "activity_id": "A1",
            "commitment_id": "C1", "contract_ref": "HT-1", "amount_cents": 50_000,
            "period": "2026-01", "allocation_rule": "equal_share"}, actor="bo1")
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/allocations", {
            "request_id": "g1", "commitment_id": "C1"})
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/shares/confirm", {
            "request_id": "cf1", "commitment_id": "C1",
            "product_id": "P1"}, actor="bo1")
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/accruals", {
            "request_id": "ac1", "commitment_id": "C1", "amount_cents": 50_000,
            "period": "2026-01", "occurred_on": "2026-01-31"})
        self.assertEqual(201, status)
        status, payload = self.call("POST", "/settlements", {
            "request_id": "st1", "commitment_id": "C1"})
        self.assertEqual(201, status)
        status, payload = self.call("GET", "/commitments/C1")
        self.assertEqual(200, status)
        self.assertEqual("settled", payload["status"])
        self.assertEqual(50_000, payload["shares"][0]["settled_cents"])

    def test_trace_route_returns_timeline(self):
        self.call("POST", "/products", {"request_id": "p1", "product_id": "P1",
                                        "name": "白酒", "owner_actor_id": "bo1"})
        self.call("POST", "/lifecycle-versions", {"request_id": "l1", "product_id": "P1",
                                                   "stage": "mature",
                                                   "effective_from": "2026-01-01"})
        self.call("POST", "/annual-budgets", {"request_id": "ab1", "product_id": "P1",
                                              "year": 2026, "amount_cents": 1_000_000,
                                              "effective_from": "2026-01-01"})
        self.call("POST", "/period-budgets", {"request_id": "pb1", "product_id": "P1",
                                              "period": "2026-01", "amount_cents": 100_000,
                                              "effective_from": "2026-01-01"})
        self.call("POST", "/activities", {"request_id": "a1",
                                          "activity_id": "A1", "team_type": "brand",
                                          "name": "推广", "starts_on": "2026-01-15",
                                          "ends_on": "2026-02-15",
                                          "beneficiary_product_ids": ["P1"]})
        self.call("POST", "/commitments", {"request_id": "c1",
                                           "activity_id": "A1", "commitment_id": "C1",
                                           "contract_ref": "HT-1", "amount_cents": 100,
                                           "period": "2026-01",
                                           "allocation_rule": "equal_share"})
        status, payload = self.call("GET", "/commitments/C1/trace")
        self.assertEqual(200, status)
        self.assertEqual("commitment.created", payload["timeline"][0]["action"])

    def test_budget_exceeded_returns_422(self):
        self.call("POST", "/products", {"request_id": "p1", "product_id": "P1",
                                        "name": "白酒", "owner_actor_id": "bo1"})
        self.call("POST", "/lifecycle-versions", {"request_id": "l1", "product_id": "P1",
                                                   "stage": "mature",
                                                   "effective_from": "2026-01-01"})
        self.call("POST", "/annual-budgets", {"request_id": "ab1", "product_id": "P1",
                                              "year": 2026, "amount_cents": 1_000_000,
                                              "effective_from": "2026-01-01"})
        self.call("POST", "/period-budgets", {"request_id": "pb1", "product_id": "P1",
                                              "period": "2026-01", "amount_cents": 100,
                                              "effective_from": "2026-01-01"})
        self.call("POST", "/activities", {"request_id": "a1",
                                          "activity_id": "A1", "team_type": "brand",
                                          "name": "推广", "starts_on": "2026-01-15",
                                          "ends_on": "2026-02-15",
                                          "beneficiary_product_ids": ["P1"]})
        status, payload = self.call("POST", "/commitments", {"request_id": "c-big",
                                                              "activity_id": "A1",
                                                              "commitment_id": "CB",
                                                              "contract_ref": "X",
                                                              "amount_cents": 500,
                                                              "period": "2026-01",
                                                              "allocation_rule": "equal_share"})
        self.assertEqual(422, status)
        self.assertEqual("budget_exceeded", payload["error"])

    def test_lifecycle_listing_requires_product(self):
        status, payload = self.call("GET", "/lifecycle-versions")
        self.assertEqual(400, status)

    def test_fee_route_missing_actor_is_forbidden(self):
        self.call("POST", "/products", {"request_id": "p1", "product_id": "P1",
                                        "name": "白酒", "owner_actor_id": "bo1"})
        status, payload = route(self.service, "POST", "/period-budgets",
                                {"request_id": "pb-x", "product_id": "P1",
                                 "period": "2026-01", "amount_cents": 1,
                                 "effective_from": "2026-01-01"},
                                {"X-Actor-Id": ""}, fee=self.fee)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
