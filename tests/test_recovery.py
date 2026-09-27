import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402


class RecoveryLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def approved_claim(self, number="RC-001", payout=280000):
        claim = self.service.create_claim(
            "intake1", "intake", number, "TY-2026", "A区", "flood", "P-" + number,
            "R-" + number, 30.1, 121.1, 500000,
        )
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adj1", claim["version"], "sur1")
        claim = self.service.record_survey("adj1", "adjuster", claim["id"], 0.6, "受损", "赔付", claim["version"])
        claim = self.service.submit_review("adj1", "adjuster", claim["id"], claim["version"])
        return self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", payout, claim["version"])

    def ledger_row(self, claim_id):
        ledger = self.service.recovery_ledger("sup1", "supervisor")
        return next(c for c in ledger["claims"] if c["claim_id"] == claim_id)

    def test_expected_amount_reduces_net_loss_before_receipt(self):
        claim = self.approved_claim()
        self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 30000, "受损设备拍卖")
        row = self.ledger_row(claim["id"])
        self.assertEqual(280000, row["payout"])
        self.assertEqual(30000, row["pending_recovery"])
        self.assertEqual(0, row["recovered"])
        self.assertEqual(250000, row["net_outlay"])

    def test_receipt_confirms_recovered_and_same_voucher_cannot_repeat(self):
        claim = self.approved_claim()
        rec = self.service.register_recovery("sup1", "supervisor", claim["id"], "subrogation", 50000)
        confirmed = self.service.confirm_recovery("sup1", "supervisor", rec["id"], "VCH-001", 45000)
        self.assertEqual("received", confirmed["status"])
        self.assertEqual(45000, confirmed["received_amount"])
        row = self.ledger_row(claim["id"])
        self.assertEqual(0, row["pending_recovery"])
        self.assertEqual(45000, row["recovered"])
        self.assertEqual(235000, row["net_outlay"])
        # 同一条记录不能重复入账
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_recovery("sup1", "supervisor", rec["id"], "VCH-002")
        self.assertEqual(409, ctx.exception.status)
        # 同一凭证号不能用于另一笔回收
        other = self.service.register_recovery("sup1", "supervisor", claim["id"], "other", 1000)
        with self.assertRaises(DomainError) as ctx2:
            self.service.confirm_recovery("sup1", "supervisor", other["id"], "VCH-001")
        self.assertEqual(409, ctx2.exception.status)

    def test_recovery_exceeding_payout_stays_pending(self):
        claim = self.approved_claim(payout=100000)
        first = self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 60000)
        self.service.confirm_recovery("sup1", "supervisor", first["id"], "V-1")
        second = self.service.register_recovery("sup1", "supervisor", claim["id"], "subrogation", 60000)
        # 累计回收 120000 > 赔款 100000，留在待处理
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_recovery("sup1", "supervisor", second["id"], "V-2")
        self.assertEqual(409, ctx.exception.status)
        row = self.ledger_row(claim["id"])
        self.assertEqual(60000, row["pending_recovery"])
        self.assertEqual(60000, row["recovered"])
        # 未超赔的小额凭证仍然可以到账
        self.service.confirm_recovery("sup1", "supervisor", second["id"], "V-2", 40000)
        row = self.ledger_row(claim["id"])
        self.assertEqual(100000, row["recovered"])
        self.assertEqual(0, row["pending_recovery"])
        self.assertEqual(0, row["net_outlay"])

    def test_confirm_before_approval_stays_pending(self):
        # 案件尚未核定也能先登记预计回收，但不能确认到账
        claim = self.service.create_claim(
            "intake1", "intake", "RC-002", "TY-2026", "A区", "flood", "P-X", "R-X", 30.2, 121.2, 300000)
        rec = self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 20000)
        self.assertEqual("pending", rec["status"])
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_recovery("sup1", "supervisor", rec["id"], "V-9")
        self.assertEqual(409, ctx.exception.status)
        # 核定后即可到账
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adj1", claim["version"], "sur1")
        claim = self.service.record_survey("adj1", "adjuster", claim["id"], 0.5, "x", "y", claim["version"])
        claim = self.service.submit_review("adj1", "adjuster", claim["id"], claim["version"])
        claim = self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", 120000, claim["version"])
        done = self.service.confirm_recovery("sup1", "supervisor", rec["id"], "V-9")
        self.assertEqual("received", done["status"])

    def test_close_blocked_with_pending_recovery_and_reopen_continues(self):
        claim = self.approved_claim()
        rec = self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 30000)
        with self.assertRaises(DomainError) as ctx:
            self.service.close_claim("sup1", "supervisor", claim["id"], claim["version"])
        self.assertEqual(409, ctx.exception.status)
        # 待回收全部到账后可以结案
        self.service.confirm_recovery("sup1", "supervisor", rec["id"], "V-10")
        claim = self.service.close_claim("sup1", "supervisor", claim["id"], claim["version"])
        self.assertEqual("closed", claim["status"])
        # 结案后不能直接登记或确认，须先重开
        with self.assertRaises(DomainError):
            self.service.register_recovery("sup1", "supervisor", claim["id"], "subrogation", 5000)
        claim = self.service.reopen_claim("sup1", "supervisor", claim["id"], claim["version"], "第三方又追回一笔")
        self.assertEqual("approved", claim["status"])
        more = self.service.register_recovery("sup1", "supervisor", claim["id"], "subrogation", 5000)
        confirmed = self.service.confirm_recovery("sup1", "supervisor", more["id"], "V-11", 5000)
        self.assertEqual("received", confirmed["status"])

    def test_only_supervisor_manages_recoveries(self):
        claim = self.approved_claim()
        with self.assertRaises(DomainError) as ctx:
            self.service.register_recovery("adj1", "adjuster", claim["id"], "salvage", 1000)
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.recovery_ledger("x", "viewer")
        self.assertEqual(403, ctx2.exception.status)

    def test_invalid_method_and_amount_rejected(self):
        claim = self.approved_claim()
        with self.assertRaises(DomainError):
            self.service.register_recovery("sup1", "supervisor", claim["id"], "unknown", 1000)
        with self.assertRaises(DomainError):
            self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 0)


if __name__ == "__main__":
    unittest.main()
