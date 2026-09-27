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

    def claim(self, number="C-100", loss=500000, finalize=False, payout=280000):
        claim = self.service.create_claim(
            "intake1", "intake", number, "TY-2026", "A区", "flood", "P-" + number,
            "R-" + number, 30.1, 121.1, loss, False, True,
        )
        claim = self.service.triage_claim("sup1", "supervisor", claim["id"], claim["version"], 0.1, True)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adjuster1", claim["version"], "survey1")
        claim = self.service.record_survey("adjuster1", "adjuster", claim["id"], 0.6, "结构受损", "部分赔付", claim["version"])
        claim = self.service.submit_review("adjuster1", "adjuster", claim["id"], claim["version"])
        if finalize:
            claim = self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", payout, claim["version"])
        return claim

    def ledger_row(self, claim_id):
        ledger = self.service.recovery_ledger("sup1", "supervisor")["ledger"]
        rows = [r for r in ledger if r["claim_id"] == claim_id]
        return rows[0] if rows else None

    def test_register_then_confirm_offsets_net_loss(self):
        claim = self.claim(finalize=True)
        entry = self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 30000, "V-001", "变压器残值")
        self.assertEqual("pending", entry["status"])
        row = self.ledger_row(claim["id"])
        self.assertEqual(280000, row["payout"])
        self.assertEqual(30000, row["pending"])
        self.assertEqual(0, row["received"])
        self.assertEqual(250000, row["net"])
        done = self.service.confirm_recovery("sup1", "supervisor", entry["id"], 30000)
        self.assertEqual("received", done["status"])
        self.assertEqual(30000, done["received_amount"])
        row = self.ledger_row(claim["id"])
        self.assertEqual(0, row["pending"])
        self.assertEqual(30000, row["received"])
        self.assertEqual(250000, row["net"])
        actions = [t["action"] for t in self.service.state("sup1", "supervisor")["timeline"]]
        self.assertIn("recovery.registered", actions)
        self.assertIn("recovery.received", actions)

    def test_same_voucher_cannot_post_twice(self):
        first = self.claim("C-110", finalize=True)
        second = self.claim("C-111", finalize=True)
        self.service.register_recovery("sup1", "supervisor", first["id"], "subrogation", 10000, "V-DUP")
        with self.assertRaises(DomainError) as ctx:
            self.service.register_recovery("sup1", "supervisor", second["id"], "subrogation", 5000, "V-DUP")
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("同一凭证不能重复入账", str(ctx.exception))

    def test_unfinalized_claim_stays_pending(self):
        claim = self.claim()  # 停在 review，尚未核定
        entry = self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 20000, "V-200")
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_recovery("sup1", "supervisor", entry["id"], 20000)
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("留在待处理", str(ctx.exception))
        row = self.ledger_row(claim["id"])
        self.assertEqual(20000, row["pending"])
        self.assertEqual(0, row["received"])

    def test_recovery_exceeding_payout_stays_pending(self):
        claim = self.claim(finalize=True, payout=280000)
        over = self.service.register_recovery("sup1", "supervisor", claim["id"], "subrogation", 300000, "V-300")
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_recovery("sup1", "supervisor", over["id"], 300000)
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("超过核定赔款", str(ctx.exception))
        full = self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 280000, "V-301")
        self.service.confirm_recovery("sup1", "supervisor", full["id"], 280000)
        extra = self.service.register_recovery("sup1", "supervisor", claim["id"], "other", 1, "V-302")
        with self.assertRaises(DomainError):
            self.service.confirm_recovery("sup1", "supervisor", extra["id"], 1)
        row = self.ledger_row(claim["id"])
        self.assertEqual(280000, row["received"])
        self.assertEqual(300001, row["pending"])

    def test_reopen_continues_recovery(self):
        claim = self.claim(finalize=True, payout=280000)
        entry = self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 20000, "V-400")
        self.service.confirm_recovery("sup1", "supervisor", entry["id"], 20000)
        reopened = self.service.reopen_claim("sup1", "supervisor", claim["id"], claim["version"], "发现新的追偿线索")
        self.assertEqual("review", reopened["status"])
        follow = self.service.register_recovery("sup1", "supervisor", claim["id"], "subrogation", 5000, "V-401")
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_recovery("sup1", "supervisor", follow["id"], 5000)
        self.assertIn("尚未核定", str(ctx.exception))
        with self.assertRaises(DomainError) as ctx2:
            self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", 10000, reopened["version"])
        self.assertIn("已回收金额超过核定赔款", str(ctx2.exception))
        refinalized = self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", 280000, reopened["version"])
        self.assertEqual("approved", refinalized["status"])
        self.service.confirm_recovery("sup1", "supervisor", follow["id"], 5000)
        row = self.ledger_row(claim["id"])
        self.assertEqual(25000, row["received"])
        self.assertEqual(255000, row["net"])

    def test_reopen_validation(self):
        claim = self.claim(finalize=True)
        with self.assertRaises(DomainError) as ctx:
            self.service.reopen_claim("sup1", "supervisor", claim["id"], claim["version"], "")
        self.assertEqual(400, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.reopen_claim("sup1", "supervisor", claim["id"], claim["version"] + 9, "理由")
        self.assertEqual(409, ctx2.exception.status)
        open_claim = self.claim("C-120")
        with self.assertRaises(DomainError) as ctx3:
            self.service.reopen_claim("sup1", "supervisor", open_claim["id"], open_claim["version"], "理由")
        self.assertEqual(409, ctx3.exception.status)

    def test_recovery_permissions_and_validation(self):
        claim = self.claim(finalize=True)
        with self.assertRaises(DomainError) as ctx:
            self.service.register_recovery("adjuster1", "adjuster", claim["id"], "salvage", 1000, "V-500")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.recovery_ledger("adjuster1", "adjuster")
        self.assertEqual(403, ctx2.exception.status)
        with self.assertRaises(DomainError):
            self.service.register_recovery("sup1", "supervisor", claim["id"], "bad-method", 1000, "V-501")
        with self.assertRaises(DomainError):
            self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", -5, "V-502")
        entry = self.service.register_recovery("sup1", "supervisor", claim["id"], "salvage", 1000, "V-503")
        with self.assertRaises(DomainError) as ctx3:
            self.service.confirm_recovery("auditor1", "auditor", entry["id"], 1000)
        self.assertEqual(403, ctx3.exception.status)
        auditor_view = self.service.recovery_ledger("auditor1", "auditor")
        self.assertEqual(1, len(auditor_view["ledger"]))


if __name__ == "__main__":
    unittest.main()
