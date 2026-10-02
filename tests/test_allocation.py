import unittest

from beverage_ops_foundation.allocation import (BP_TOTAL, allocate,
                                                largest_remainder)
from beverage_ops_foundation.errors import ValidationError


class AllocationEngineTest(unittest.TestCase):
    def test_largest_remainder_splits_exactly(self):
        items = largest_remainder(1000, [("p1", 1, 0, ""), ("p2", 1, 1, ""),
                                         ("p3", 1, 2, "")])
        self.assertEqual([334, 333, 333], [i["cents"] for i in items])
        self.assertEqual(1000, sum(i["cents"] for i in items))
        self.assertTrue(items[0].pop("_residual", False))

    def test_largest_remainder_tie_goes_to_lower_ordinal(self):
        # 100 分三等分：余 1，序号最小者得到尾差。
        items = largest_remainder(100, [("b", 1, 1, ""), ("a", 1, 0, ""),
                                        ("c", 1, 2, "")])
        by_product = {i["product_id"]: i["cents"] for i in items}
        self.assertEqual(34, by_product["a"])
        self.assertEqual(33, by_product["b"])
        self.assertEqual(33, by_product["c"])

    def test_weights_ratio_in_basis_points(self):
        items = largest_remainder(100_000, [("p1", 30, 0, ""), ("p2", 70, 1, "")])
        self.assertEqual([30_000, 70_000], [i["cents"] for i in items])
        self.assertEqual([3000, 7000], [i["ratio_bp"] for i in items])

    def test_zero_scale_rejected(self):
        with self.assertRaises(ValidationError):
            largest_remainder(100, [("p", 0, 0, "")])

    def test_equal_share_rule(self):
        result = allocate("equal_share", 100,
                          [{"product_id": "p1", "ordinal": 0, "weight": 100},
                           {"product_id": "p2", "ordinal": 1, "weight": 100}])
        self.assertEqual(50, result["items"][0]["cents"])
        self.assertEqual(BP_TOTAL, sum(i["ratio_bp"] for i in result["items"]))

    def test_revenue_basis_rule(self):
        result = allocate("revenue_basis", 1000,
                          [{"product_id": "p1", "ordinal": 0, "weight": 1},
                           {"product_id": "p2", "ordinal": 1, "weight": 1}],
                          {"bases": {"p1": 900, "p2": 100}})
        self.assertEqual(900, result["items"][0]["cents"])
        self.assertEqual("收入基数 900", result["items"][0]["basis_value"])

    def test_revenue_basis_requires_bases(self):
        with self.assertRaises(ValidationError):
            allocate("revenue_basis", 1000,
                     [{"product_id": "p1", "ordinal": 0, "weight": 1}], {})

    def test_fixed_ratio_must_total_ten_thousand(self):
        with self.assertRaises(ValidationError):
            allocate("fixed_ratio", 1000,
                     [{"product_id": "p1", "ordinal": 0, "weight": 1},
                      {"product_id": "p2", "ordinal": 1, "weight": 1}],
                     {"ratios_bp": {"p1": 6000, "p2": 3000}})

    def test_fixed_ratio_rule(self):
        result = allocate("fixed_ratio", 1000,
                          [{"product_id": "p1", "ordinal": 0, "weight": 1},
                           {"product_id": "p2", "ordinal": 1, "weight": 1}],
                          {"ratios_bp": {"p1": 6000, "p2": 4000}})
        self.assertEqual([600, 400], [i["cents"] for i in result["items"]])

    def test_unknown_rule_rejected(self):
        with self.assertRaises(ValidationError):
            allocate("guessing", 100, [{"product_id": "p", "ordinal": 0, "weight": 1}])

    def test_explanation_is_complete(self):
        result = allocate("beneficiary_weight", 100,
                          [{"product_id": "p1", "ordinal": 0, "weight": 7},
                           {"product_id": "p2", "ordinal": 1, "weight": 3}])
        item = result["items"][0]
        self.assertEqual("受益权重 7", item["basis_value"])
        self.assertGreater(item["ratio_bp"], 0)
        self.assertEqual(100, sum(i["cents"] for i in result["items"]))


if __name__ == "__main__":
    unittest.main()
