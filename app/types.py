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


@dataclass
class CheckResult:
    status: CheckStatus
    source: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
