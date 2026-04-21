from __future__ import annotations

from io import BytesIO
import requests
from pypdf import PdfReader

from app.payment_data import parse_privat_receipt_pdf_text
from app.types import CheckResult, CheckStatus


class PrivatChecker:
    URL = "https://privatbank.ua/pb/ajax/find-document"
    RECEIPT_URL_TEMPLATE = "https://privatbank.ua/pb/get-doc/download/receipt/{receipt_code}?csrf={token}"

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def _extract_payment_from_receipt_pdf(self, receipt_code: str, token: str) -> tuple[dict | None, str | None]:
        try:
            resp = requests.get(
                self.RECEIPT_URL_TEMPLATE.format(receipt_code=receipt_code, token=token),
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            reader = PdfReader(BytesIO(resp.content))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            payment = parse_privat_receipt_pdf_text(text)
            if payment.get("amount") is None and payment.get("recipient_card") is None:
                return payment, "receipt_pdf_parsed_but_payment_fields_missing"
            return payment, None
        except Exception as exc:
            return None, f"receipt_pdf_fetch_or_parse_failed: {exc}"

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
            details = dict(data)
            token = str(data.get("token") or "").strip()
            payment_warning: str | None = None
            payment: dict | None = None
            if token:
                payment, payment_warning = self._extract_payment_from_receipt_pdf(receipt_code, token)
            else:
                payment_warning = "find_document_token_missing"
            if payment is not None:
                details["payment"] = payment
            if payment_warning:
                details["payment_warning"] = payment_warning
            return CheckResult(
                status=CheckStatus.VALID,
                source="privatbank.ua",
                message=data.get("reason", "Документ знайдено"),
                details=details,
            )

        reason = data.get("reason") or "Документ не знайдено"
        return CheckResult(
            status=CheckStatus.NOT_FOUND,
            source="privatbank.ua",
            message=reason,
            details=data,
        )
