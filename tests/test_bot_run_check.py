import unittest
from types import SimpleNamespace

from app.bot import ReceiptBot
from app.types import CheckResult, CheckStatus, ParsedReceipt


class _PrivatStub:
    def check(self, receipt_code: str) -> CheckResult:
        return CheckResult(
            status=CheckStatus.VALID,
            source="privatbank.ua",
            message=f"privat:{receipt_code}",
        )


class _CheckGovStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool, str | None]] = []

    def check(
        self,
        provider_code: str,
        receipt_code: str,
        reload_before_check: bool = False,
        user_scope: str | None = None,
    ) -> CheckResult:
        self.calls.append((provider_code, receipt_code, reload_before_check, user_scope))
        return CheckResult(
            status=CheckStatus.VALID,
            source="check.gov.ua",
            message="ok",
            details={"provider_code": provider_code, "receipt_code": receipt_code},
        )


class BotRunCheckTests(unittest.TestCase):
    def test_unknown_provider_returns_unparseable(self) -> None:
        check_gov = _CheckGovStub()
        bot_like = SimpleNamespace(check_gov_checker=check_gov, privat_checker=_PrivatStub())
        parsed = ParsedReceipt(
            bank_label=None,
            bank_key=None,
            provider_code=None,
            receipt_code="9B1K-AKB5-C1MP-26B6",
            confidence=0.0,
            raw_text="",
        )

        result = ReceiptBot._run_check(bot_like, parsed)

        self.assertEqual(result.status, CheckStatus.UNPARSEABLE)
        self.assertEqual(check_gov.calls, [])

    def test_active_orders_check_uses_reload_before_check(self) -> None:
        check_gov = _CheckGovStub()
        bot_like = SimpleNamespace(check_gov_checker=check_gov, privat_checker=_PrivatStub())
        parsed = ParsedReceipt(
            bank_label="Монобанк",
            bank_key="monobank",
            provider_code="monobank",
            receipt_code="9B1K-AKB5-C1MP-26B6",
            confidence=1.0,
            raw_text="",
        )

        result = ReceiptBot._run_check_for_active_orders(bot_like, parsed)

        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertEqual(check_gov.calls, [("monobank", "9B1K-AKB5-C1MP-26B6", True, None)])


if __name__ == "__main__":
    unittest.main()
