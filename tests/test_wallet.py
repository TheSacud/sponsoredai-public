import unittest
from pathlib import Path

from sai.wallet import InsufficientCredits, Wallet, WalletError


TEST_ROOT = Path.cwd() / ".sai-test-data"


def wallet_path(name):
    TEST_ROOT.mkdir(exist_ok=True)
    path = TEST_ROOT / f"{name}.json"
    path.unlink(missing_ok=True)
    return path


class WalletTests(unittest.TestCase):
    def test_earn_and_spend_update_balance(self):
        wallet = Wallet(wallet_path("earn-and-spend"))
        wallet.earn(0.05, "sponsor:test", daily_cap=1.0)
        wallet.spend(0.02, "gateway:test")
        self.assertEqual(wallet.balance(), 0.03)

    def test_earnings_are_not_capped_by_local_wallet(self):
        wallet = Wallet(wallet_path("daily-cap"))
        wallet.earn(0.2, "sponsor:test", daily_cap=0.25)
        wallet.earn(0.2, "sponsor:test", daily_cap=0.25)
        self.assertEqual(wallet.balance(), 0.4)

    def test_spend_requires_balance(self):
        wallet = Wallet(wallet_path("insufficient"))
        with self.assertRaises(InsufficientCredits):
            wallet.spend(0.01, "gateway:test")

    def test_spend_up_to_clamps_to_balance(self):
        wallet = Wallet(wallet_path("spend-up-to"))
        wallet.earn(0.05, "sponsor:test", daily_cap=1.0)
        entry = wallet.spend_up_to(0.2, "gateway:test")
        self.assertEqual(entry.amount, -0.05)
        self.assertEqual(wallet.balance(), 0.0)

    def test_spend_up_to_returns_none_when_empty(self):
        wallet = Wallet(wallet_path("spend-up-to-empty"))
        self.assertIsNone(wallet.spend_up_to(0.2, "gateway:test"))

    def test_corrupt_wallet_raises_wallet_error(self):
        path = wallet_path("corrupt")
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(WalletError):
            Wallet(path).balance()


if __name__ == "__main__":
    unittest.main()
