import unittest

from beverage_ops_foundation.fee_acceptance import run


class FeeAcceptanceTest(unittest.TestCase):
    def test_offline_fee_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual({"wine-mature": 84_000, "wine-new": 36_000}, result["draft"])
        self.assertEqual("settled", result["first_settle"]["wine-mature"])
        self.assertEqual("disputed", result["first_settle"]["wine-new"])
        self.assertEqual("settled", result["final_commitment_status"])
        self.assertTrue(result["cross_period_invoice"])
        self.assertEqual(12_000, result["refunded_cents"])
        self.assertTrue(result["overrun_blocked_before_exception"])
        self.assertEqual(252_000, result["mature_consumed_cents"])
        self.assertEqual(52_000, result["exception_used_cents"])
        self.assertEqual([
            "commitment.created", "share.allocation_drafted", "share.confirmed",
            "share.disputed", "accrual.recorded", "invoice.recorded", "share.settled",
            "share.dispute_resolved", "share.confirmed", "share.settled",
            "refund.recorded",
        ], result["timeline_actions"])


if __name__ == "__main__":
    unittest.main()
