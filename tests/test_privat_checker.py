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
    def test_valid(self, mock_post) -> None:
        mock_post.return_value = DummyResp({"status": True, "reason": "Документ був знайдений"})
        checker = PrivatChecker()
        result = checker.check("P24A5738337141D5456")
        self.assertEqual(result.status, CheckStatus.VALID)

    @patch("app.checkers.privat.requests.post")
    def test_not_found(self, mock_post) -> None:
        mock_post.return_value = DummyResp({"status": False, "reason": "Документ не знайдено"})
        checker = PrivatChecker()
        result = checker.check("BAD")
        self.assertEqual(result.status, CheckStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
