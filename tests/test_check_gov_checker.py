import unittest

from app.checkers.check_gov import CheckGovChecker
from app.types import CheckStatus


class CheckGovCheckerTests(unittest.TestCase):
    def test_provider_candidates_include_common_forms(self) -> None:
        variants = CheckGovChecker._provider_candidates("a-bank")
        self.assertIn("a-bank", variants)
        self.assertIn("abank", variants)
        self.assertIn("a_bank", variants)

    def test_retryable_internal_error_then_valid(self) -> None:
        checker = CheckGovChecker()
        calls = []

        def fake_check(provider_code: str, receipt_code: str):
            calls.append(provider_code)
            if len(calls) == 1:
                return 200, {"textUk": "Квитанція не знайдена", "eInfo": "internal error"}, ""
            return 200, {"payments": [{"id": 1}]}, ""

        checker._check_in_browser = fake_check  # type: ignore[method-assign]
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("a-bank", "2300-8317-6223-0167")
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertGreaterEqual(len(calls), 2)

    def test_unsupported_company_fallback_to_monobank(self) -> None:
        checker = CheckGovChecker()
        calls = []

        def fake_check(provider_code: str, receipt_code: str):
            calls.append(provider_code)
            if provider_code != "monobank":
                return 200, {"textUk": "Щось пішло не так...", "eInfo": "unsupported company"}, ""
            return 200, {"payments": [{"id": 1}]}, ""

        checker._check_in_browser = fake_check  # type: ignore[method-assign]
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("some-wrong-provider", "KPT2-0T15-39BM-HX28")
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertIn("monobank", calls)


if __name__ == "__main__":
    unittest.main()
