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
        self.calls: list[tuple[str, str]] = []

    def check(self, provider_code: str, receipt_code: str) -> CheckResult:
        self.calls.append((provider_code, receipt_code))
        return CheckResult(
            status=CheckStatus.VALID,
            source="check.gov.ua",
            message="ok",
            details={"provider_code": provider_code, "receipt_code": receipt_code},
        )


class BotRunCheckTests(unittest.TestCase):
    def test_unknown_provider_still_uses_check_gov(self) -> None:
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

        self.assertEqual(result.status, CheckStatus.VALID)
        self.assertEqual(check_gov.calls, [("", "9B1K-AKB5-C1MP-26B6")])


if __name__ == "__main__":
    unittest.main()
