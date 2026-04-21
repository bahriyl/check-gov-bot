import unittest
from unittest.mock import patch

from app.checkers.privat import PrivatChecker
from app.types import CheckStatus


class DummyResp:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict:
        return self.payload


class PrivatCheckerTests(unittest.TestCase):
    @patch("app.checkers.privat.requests.post")
    @patch.object(PrivatChecker, "_extract_payment_from_receipt_pdf")
    def test_valid(self, mock_extract_payment, mock_post) -> None:
        mock_post.return_value = DummyResp(
            {"status": True, "reason": "Документ був знайдений", "token": "csrf-token"}
        )
        mock_extract_payment.return_value = (
            {
                "recipient": None,
                "recipient_card": "5355571306568825",
                "amount": "2500",
                "currency": "UAH",
                "source": "privatbank.ua/pdf",
            },
            None,
        )

        checker = PrivatChecker()
        result = checker.check("P24A5738337141D5456")
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertEqual(result.details.get("payment", {}).get("recipient_card"), "5355571306568825")
        self.assertEqual(result.details.get("payment", {}).get("amount"), "2500")

    @patch("app.checkers.privat.requests.post")
    def test_valid_without_token_sets_warning(self, mock_post) -> None:
        mock_post.return_value = DummyResp({"status": True, "reason": "Документ був знайдений"})
        checker = PrivatChecker()
        result = checker.check("P24A5738337141D5456")
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertEqual(result.details.get("payment_warning"), "find_document_token_missing")

    @patch("app.checkers.privat.requests.post")
    def test_not_found(self, mock_post) -> None:
        mock_post.return_value = DummyResp({"status": False, "reason": "Документ не знайдено"})
        checker = PrivatChecker()
        result = checker.check("BAD")
        self.assertEqual(result.status, CheckStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
