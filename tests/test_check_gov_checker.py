import unittest

from app.checkers.check_gov import CheckGovChecker
from app.types import CheckStatus


class CheckGovCheckerTests(unittest.TestCase):
    def test_retryable_internal_error_then_valid(self) -> None:
        checker = CheckGovChecker()
        calls = []

        def fake_check(provider_code: str, receipt_code: str, *_args, **_kwargs):
            calls.append(provider_code)
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

        checker._check_in_browser = fake_check  # type: ignore[method-assign]
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("a-bank", "2300-8317-6223-0167")
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertGreaterEqual(len(calls), 2)
        payment = result.details.get("payment")
        self.assertEqual(payment.get("amount"), "300")
        self.assertEqual(payment.get("recipient_card"), "444111******1722")

    def test_unsupported_company_returns_check_error(self) -> None:
        checker = CheckGovChecker()
        calls = []

        def fake_check(provider_code: str, receipt_code: str, *_args, **_kwargs):
            calls.append(provider_code)
            return 200, {"textUk": "Щось пішло не так...", "eInfo": "unsupported company"}, ""

        checker._check_in_browser = fake_check  # type: ignore[method-assign]
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("some-wrong-provider", "KPT2-0T15-39BM-HX28")
        self.assertEqual(result.status, CheckStatus.CHECK_ERROR)
        self.assertEqual(calls, ["some-wrong-provider", "some-wrong-provider"])

    def test_ui_paid_result_without_payments_is_valid(self) -> None:
        checker = CheckGovChecker()

        checker._check_in_browser = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            200,
            {
                "ui": {
                    "check_result_text": "Квитанція Оплачена",
                    "result_flag_text": "Оплачена",
                    "hint_text": "",
                }
            },
            "",
        )
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("monobank", "KPT2-0T15-39BM-HX28")
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertIn("Оплачена", result.message)

    def test_reload_before_check_calls_reload_per_attempt(self) -> None:
        checker = CheckGovChecker()
        reload_calls: list[int] = []

        checker._reload_page = lambda: reload_calls.append(1)  # type: ignore[assignment]
        checker._check_in_browser = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            200,
            {"payments": [{"id": 1, "recipient": "User, 444111******1722", "amount": 200}]},
            "",
        )
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("monobank", "KPT2-0T15-39BM-HX28", reload_before_check=True)
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertEqual(len(reload_calls), 1)

    def test_reload_before_check_default_false(self) -> None:
        checker = CheckGovChecker()
        reload_calls: list[int] = []

        checker._reload_page = lambda: reload_calls.append(1)  # type: ignore[assignment]
        checker._check_in_browser = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            200,
            {"payments": [{"id": 1, "recipient": "User, 444111******1722", "amount": 200}]},
            "",
        )
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("monobank", "KPT2-0T15-39BM-HX28")
        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertEqual(reload_calls, [])

    def test_uncertain_response_returns_check_error(self) -> None:
        checker = CheckGovChecker()
        checker._check_in_browser = lambda *_args, **_kwargs: (200, {"ui": {"check_result_text": "..."}} , "")  # type: ignore[method-assign]
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("monobank", "KPT2-0T15-39BM-HX28")

        self.assertEqual(result.status, CheckStatus.CHECK_ERROR)
        self.assertIn("невизначена", result.message.lower())

    def test_browser_errors_stop_after_two_attempts(self) -> None:
        checker = CheckGovChecker()
        calls = []

        def fake_check(*_args, **_kwargs):
            calls.append(1)
            raise RuntimeError("boom")

        checker._check_in_browser = fake_check  # type: ignore[method-assign]
        checker.close = lambda: None  # type: ignore[assignment]

        result = checker.check("monobank", "KPT2-0T15-39BM-HX28")

        self.assertEqual(result.status, CheckStatus.CHECK_ERROR)
        self.assertEqual(len(calls), 2)
        self.assertIn("зациклен", result.message.lower())


if __name__ == "__main__":
    unittest.main()
