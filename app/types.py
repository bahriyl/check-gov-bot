from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CheckStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    NOT_FOUND = "NOT_FOUND"
    UNPARSEABLE = "UNPARSEABLE"
    CHECK_ERROR = "CHECK_ERROR"


@dataclass
class ParsedReceipt:
    bank_label: str | None
    bank_key: str | None
    provider_code: str | None
    receipt_code: str | None
    confidence: float = 0.0
    raw_text: str = ""
    amount: str | None = None
    card_number: str | None = None
    fee_amount: str | None = None
    total_amount: str | None = None
    currency: str | None = None
    amount_extraction_method: str | None = None
    amount_confidence: float = 0.0
    amount_debug: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    status: CheckStatus
    source: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
