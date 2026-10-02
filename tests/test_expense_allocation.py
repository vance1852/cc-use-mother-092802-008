import unittest

from sales_expense.allocation import (RULE_EQUAL, RULE_FACTOR, RULE_OVERRIDE, BenefitInput,
                                      allocate, explain)
from sales_expense.errors import AllocationError


class AllocationTest(unittest.TestCase):
    def test_factor_weighted_splits_exactly_without_losing_cents(self):
        result = allocate(RULE_FACTOR, 100, [
            BenefitInput("p1", "u1", factor=1),
            BenefitInput("p2", "u2", factor=1),
            BenefitInput("p3", "u3", factor=1),
        ])
        self.assertEqual(100, sum(line.amount for line in result.lines))
        amounts = sorted(line.amount for line in result.lines)
        self.assertEqual([33, 33, 34], amounts)
        # 最大余数法稳定可重复：编号最小且余数相同者拿到尾差
        self.assertEqual(34, next(line.amount for line in result.lines if line.product_id == "p1"))

    def test_factor_weighted_uses_ratio(self):
        result = allocate(RULE_FACTOR, 1000, [
            BenefitInput("p1", "u1", factor=60),
            BenefitInput("p2", "u2", factor=40),
        ])
        amounts = {line.product_id: line.amount for line in result.lines}
        self.assertEqual({600, 400}, set(amounts.values()))
        doc = explain(result)
        self.assertEqual(1000, doc["allocated_amount"])
        self.assertIn("总因子 100", doc["lines"][0]["reason"])

    def test_equal_split_handles_remainder(self):
        result = allocate(RULE_EQUAL, 10, [BenefitInput("p1", "u1"),
                                           BenefitInput("p2", "u2"),
                                           BenefitInput("p3", "u3")])
        self.assertEqual(10, sum(line.amount for line in result.lines))
        self.assertEqual([3, 3, 4], sorted(line.amount for line in result.lines))

    def test_override_requires_matching_total(self):
        with self.assertRaises(AllocationError):
            allocate(RULE_OVERRIDE, 100, [BenefitInput("p1", "u1", override_amount=60),
                                          BenefitInput("p2", "u2", override_amount=30)])
        result = allocate(RULE_OVERRIDE, 100, [BenefitInput("p1", "u1", override_amount=60),
                                               BenefitInput("p2", "u2", override_amount=40)])
        self.assertEqual(100, sum(line.amount for line in result.lines))

    def test_zero_total_factor_rejected(self):
        with self.assertRaises(AllocationError):
            allocate(RULE_FACTOR, 100, [BenefitInput("p1", "u1", factor=0)])

    def test_unknown_rule_and_duplicate_products(self):
        with self.assertRaises(AllocationError):
            allocate("mystery", 100, [BenefitInput("p1", "u1", factor=1)])
        with self.assertRaises(AllocationError):
            allocate(RULE_FACTOR, 100, [BenefitInput("p1", "u1", factor=1),
                                        BenefitInput("p1", "u2", factor=1)])


if __name__ == "__main__":
    unittest.main()
