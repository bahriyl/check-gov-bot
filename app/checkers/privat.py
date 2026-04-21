from __future__ import annotations

import requests

from app.types import CheckResult, CheckStatus


class PrivatChecker:
    URL = "https://privatbank.ua/pb/ajax/find-document"

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def check(self, receipt_code: str) -> CheckResult:
        payload = {
            "document[type]": "receipt",
            "document[id]": receipt_code,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            resp = requests.post(
                self.URL,
                data=payload,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return CheckResult(
                status=CheckStatus.CHECK_ERROR,
                source="privatbank.ua",
                message=f"Privat check failed: {exc}",
            )

        if data.get("status") is True:
            return CheckResult(
                status=CheckStatus.VALID,
                source="privatbank.ua",
                message=data.get("reason", "Документ знайдено"),
                details=data,
            )

        reason = data.get("reason") or "Документ не знайдено"
        return CheckResult(
            status=CheckStatus.NOT_FOUND,
            source="privatbank.ua",
            message=reason,
            details=data,
        )
