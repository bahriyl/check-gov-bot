import unittest

from app.checkers.check_gov import CheckGovChecker
from app.types import CheckStatus


class CheckGovCheckerTests(unittest.TestCase):
    def test_retryable_internal_error_then_valid(self) -> None:
        checker = CheckGovChecker()
        calls = []

        def fake_once(provider_code: str, receipt_code: str):
            calls.append((provider_code, receipt_code))
            if len(calls) == 1:
                return 200, {"textUk": "Квитанція не знайдена", "eInfo": "internal error"}, ""
            return 200, {
                "payments": [
                    {
                        "sender": "Радченко Олександр Вікторович",
                        "recipient": "Вікторія В., 444111******1722",
                        "amount": 30000,
                        "date": "2026-04-20T09:02:35Z",
                        "description": "Переказ особистих коштів",
                        "currencyCode": 980,
                    }
                ]
            }, ""

        checker._check_once = fake_once  # type: ignore[method-assign]

        result = checker.check("a-bank", "2300-8317-6223-0167")
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertEqual(len(calls), 2)
        payment = result.details.get("payment")
        self.assertEqual(payment.get("amount"), "300")
        self.assertEqual(payment.get("recipient_card"), "444111******1722")

    def test_unsupported_company_returns_check_error(self) -> None:
        checker = CheckGovChecker()

        checker._check_once = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            200,
            {"textUk": "Щось пішло не так...", "eInfo": "unsupported company"},
            "",
        )

        result = checker.check("some-wrong-provider", "KPT2-0T15-39BM-HX28")
        self.assertEqual(result.status, CheckStatus.CHECK_ERROR)
        self.assertIn("unsupported company", result.message)

    def test_not_found_maps_to_not_found(self) -> None:
        checker = CheckGovChecker()
        checker._check_once = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            200,
            {"e": 404, "textUk": "Відсутня"},
            "",
        )

        result = checker.check("monobank", "KPT2-0T15-39BM-HX28")

        self.assertEqual(result.status, CheckStatus.NOT_FOUND)
        self.assertIn("Відсутня", result.message)

    def test_uncertain_response_returns_check_error(self) -> None:
        checker = CheckGovChecker()
        checker._check_once = lambda *_args, **_kwargs: (200, {"x": 1}, "")  # type: ignore[method-assign]

        result = checker.check("monobank", "KPT2-0T15-39BM-HX28")

        self.assertEqual(result.status, CheckStatus.CHECK_ERROR)
        self.assertIn("невизначена", result.message.lower())

    def test_request_errors_stop_after_two_attempts(self) -> None:
        checker = CheckGovChecker()
        calls = []

        def fake_once(*_args, **_kwargs):
            calls.append(1)
            raise RuntimeError("boom")

        checker._check_once = fake_once  # type: ignore[method-assign]

        result = checker.check("monobank", "KPT2-0T15-39BM-HX28")

        self.assertEqual(result.status, CheckStatus.CHECK_ERROR)
        self.assertEqual(len(calls), 2)
        self.assertIn("нестабільний", result.message.lower())


if __name__ == "__main__":
    unittest.main()
