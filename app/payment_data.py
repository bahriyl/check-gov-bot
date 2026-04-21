from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

AMOUNT_RE = re.compile(r"(\d{1,3}(?:[ \u00A0]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)")
FULL_CARD_RE = re.compile(r"\b(\d{16,19})\b")
MASKED_CARD_RE = re.compile(r"\b(\d{6}\*{4,8}\d{4})\b")


def normalize_amount(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip().replace("\u00a0", " ")
    raw = re.sub(r"(?i)(грн|uah|₴)", "", raw)
    raw = raw.replace(" ", "")
    if not raw:
        return None

    if "," in raw and "." in raw:
        if raw.rfind(".") > raw.rfind(","):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        parts = raw.rsplit(",", 1)
        if len(parts[1]) <= 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")

    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        return None

    normalized = format(parsed.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def normalize_check_gov_amount(value: Any, currency_code: Any) -> str | None:
    if value is None:
        return None
    if str(currency_code) == "980":
        try:
            if isinstance(value, str):
                raw = value.strip().replace(" ", "")
                if raw.isdigit():
                    return normalize_amount(Decimal(raw) / Decimal(100))
            if isinstance(value, int):
                return normalize_amount(Decimal(value) / Decimal(100))
        except Exception:
            pass
    return normalize_amount(value)


def extract_card_number(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.replace(" ", "")
    full_match = FULL_CARD_RE.search(normalized)
    if full_match:
        return full_match.group(1)
    masked_match = MASKED_CARD_RE.search(normalized)
    if masked_match:
        return masked_match.group(1)
    return None


def parse_check_gov_payment(data: dict[str, Any]) -> dict[str, Any] | None:
    payments = data.get("payments")
    if not isinstance(payments, list) or not payments:
        return None
    payment = payments[0] if isinstance(payments[0], dict) else None
    if not payment:
        return None

    recipient = str(payment.get("recipient") or "").strip() or None
    currency_code = payment.get("currencyCode")

    return {
        "recipient": recipient,
        "recipient_card": extract_card_number(recipient),
        "amount": normalize_check_gov_amount(payment.get("amount"), currency_code),
        "currency_code": currency_code,
        "date": payment.get("date"),
        "description": payment.get("description"),
        "source": "check.gov.ua",
    }


def parse_privat_receipt_pdf_text(text: str) -> dict[str, Any]:
    compact = "\n".join(line.strip() for line in text.splitlines() if line.strip())

    card = None
    labeled_card = re.search(
        r"Рахунок\s+отримувача[^\d]{0,40}(\d{16,19})",
        compact,
        re.IGNORECASE,
    )
    if labeled_card:
        card = labeled_card.group(1)
    if not card:
        card = extract_card_number(compact)

    amount = None
    currency = None
    amount_patterns = [
        r"Сума\s+переказу\s*[:\-]?\s*([\d\s\u00A0.,]+)",
        r"Сума\s+операц(?:ії|ии)\s*[:\-]?\s*([\d\s\u00A0.,]+)",
        r"Сума\s*[:\-]?\s*([\d\s\u00A0.,]+)",
        r"Загальна\s+сума\s*[:\-]?\s*([\d\s\u00A0.,]+)",
        r"До\s+сплати\s*[:\-]?\s*([\d\s\u00A0.,]+)",
    ]
    for pattern in amount_patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if not match:
            continue
        amount = normalize_amount(match.group(1))
        if amount is not None:
            window = compact[max(0, match.start() - 16) : match.end() + 16].lower()
            if "грн" in window or "₴" in window or "uah" in window:
                currency = "UAH"
            break

    if amount is None:
        for match in AMOUNT_RE.finditer(compact):
            candidate = normalize_amount(match.group(1))
            if candidate is None:
                continue
            amount = candidate
            window = compact[max(0, match.start() - 16) : match.end() + 16].lower()
            if "грн" in window or "₴" in window or "uah" in window:
                currency = "UAH"
            break

    return {
        "recipient": None,
        "recipient_card": card,
        "amount": amount,
        "currency": currency,
        "source": "privatbank.ua/pdf",
    }
