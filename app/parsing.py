from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.providers import ProviderRegistry
from app.types import ParsedReceipt

CONFUSABLES = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Н": "H",
        "І": "I",
        "К": "K",
        "М": "M",
        "О": "O",
        "Р": "P",
        "Т": "T",
        "Х": "X",
        "У": "Y",
        "а": "a",
        "в": "b",
        "с": "c",
        "е": "e",
        "і": "i",
        "к": "k",
        "м": "m",
        "о": "o",
        "р": "p",
        "т": "t",
        "х": "x",
        "у": "y",
        "з": "3",
        "З": "3",
        "б": "6",
    }
)


CODE_PATTERNS = [
    re.compile(r"N[:\s]*([A-Z0-9]{4}(?:-[A-Z0-9]{4}){3,4})", re.IGNORECASE),
    re.compile(r"квитанц(?:ія|ии|iя)\s*№\s*([A-Z0-9-]{8,})", re.IGNORECASE),
    re.compile(r"код\s+документ[ау]?\s*([A-Z0-9-]{8,})", re.IGNORECASE),
    re.compile(r"\b(P24A[A-Z0-9]{8,})\b", re.IGNORECASE),
    re.compile(r"\b([A-Z0-9]{4}(?:-[A-Z0-9]{4}){3,4})\b", re.IGNORECASE),
    re.compile(r"\b([A-Z0-9]{3,6}(?:-[A-Z0-9]{3,6}){2,})\b", re.IGNORECASE),
    re.compile(r"\b([A-Z]\d{3,}(?:-\d{3,}){2,})\b", re.IGNORECASE),
    re.compile(r"(\d{4}(?:-\d{4}){2,4})"),
]

CARD_RE = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")
AMOUNT_TOKEN_RE = re.compile(r"\b(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\b")
AMOUNT_LINE_HINT_RE = re.compile(r"(?:сума|amount|total|разом|всього|до\s+оплати)", re.IGNORECASE)


def _normalize_code(code: str) -> str:
    code = code.translate(CONFUSABLES)
    code = code.strip().replace(" ", "").upper()
    # OCR often prepends an extra N before real code.
    if code.startswith("N") and re.fullmatch(r"N[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3,4}", code):
        code = code[1:]
    if code.startswith("N") and code[1:].startswith("P24A"):
        code = code[1:]
    return code


def _normalized_for_code(text: str) -> str:
    return text.translate(CONFUSABLES).upper()


def _extract_code(text: str) -> str | None:
    candidates: list[str] = []
    normalized_text = _normalized_for_code(text)

    for pattern in CODE_PATTERNS:
        for source in (text, normalized_text):
            for match in pattern.finditer(source):
                candidates.append(_normalize_code(match.group(1)))

    if not candidates:
        return None

    def rank(code: str) -> tuple[int, int]:
        score = 0
        if code.startswith("P24A"):
            score += 10
        if re.fullmatch(r"[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3,4}", code):
            score += 8
        if re.fullmatch(r"\d{4}(?:-\d{4}){3,4}", code):
            score += 6
        score += min(len(code), 20)
        return (score, len(code))

    # Pick the strongest candidate (and deterministic tie-break by lexicographic order).
    return sorted(candidates, key=lambda c: (rank(c), c), reverse=True)[0]


def _normalize_amount(amount: str) -> str | None:
    cleaned = amount.replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def _extract_amount(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[str] = []

    def normalized_line(line: str) -> str:
        return line.translate(CONFUSABLES).lower()

    amount_markers = ("сума", "suma", "cyma", "cума")
    currency_markers = ("грн", "grn", "rph", "uah")

    # Highest-priority OCR-tolerant rule:
    # use first numeric value after line containing amount + currency markers.
    for idx, line in enumerate(lines):
        norm = normalized_line(line)
        has_amount = any(marker in norm for marker in amount_markers)
        has_currency = any(marker in norm for marker in currency_markers)
        if not (has_amount and has_currency):
            continue

        target_lines = [line]
        if idx + 1 < len(lines):
            target_lines.append(lines[idx + 1])
        if idx + 2 < len(lines):
            target_lines.append(lines[idx + 2])
        for target in target_lines:
            for match in AMOUNT_TOKEN_RE.finditer(target):
                normalized = _normalize_amount(match.group(1))
                if normalized:
                    return normalized

    # Secondary rule: first numeric value after amount marker in raw OCR stream.
    sum_pattern = re.compile(r"(?:сума|suma|cyma|cума)", re.IGNORECASE)
    for sum_match in sum_pattern.finditer(text):
        window = text[sum_match.end() : sum_match.end() + 120]
        for match in AMOUNT_TOKEN_RE.finditer(window):
            normalized = _normalize_amount(match.group(1))
            if normalized:
                return normalized

    # Prefer values from explicit amount lines (e.g. "Сума (грн) 540.00" or next-line value).
    for idx, line in enumerate(lines):
        if not AMOUNT_LINE_HINT_RE.search(line):
            continue
        target_lines = [line]
        if not AMOUNT_TOKEN_RE.search(line) and idx + 1 < len(lines):
            target_lines.append(lines[idx + 1])
        for candidate_line in target_lines:
            for match in AMOUNT_TOKEN_RE.finditer(candidate_line):
                normalized = _normalize_amount(match.group(1))
                if normalized:
                    candidates.append(normalized)

    if candidates:
        return sorted(candidates, key=lambda s: Decimal(s), reverse=True)[0]

    fallback_pattern = re.compile(r"\b(\d{1,7}(?:[.,]\d{1,2})?)\b")
    for match in fallback_pattern.finditer(text):
        token = match.group(1)
        if token.count(".") + token.count(",") > 1:
            continue
        digits = re.sub(r"\D", "", token)
        # Filter likely auth/reference identifiers when no amount context is available.
        if "." not in token and "," not in token and len(digits) >= 6:
            continue
        normalized = _normalize_amount(token)
        if normalized:
            candidates.append(normalized)

    if not candidates:
        return None
    return sorted(candidates, key=lambda s: Decimal(s), reverse=True)[0]


def _extract_card_number(text: str) -> str | None:
    matches = [re.sub(r"\D", "", m.group(0)) for m in CARD_RE.finditer(text)]
    if not matches:
        return None
    # Prefer last visible card-like number in receipt text.
    return matches[-1]


def parse_receipt_text(text: str, providers: ProviderRegistry) -> ParsedReceipt:
    cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    code = _extract_code(cleaned)
    provider = providers.find_provider_by_text(cleaned)
    amount = _extract_amount(cleaned)
    card_number = _extract_card_number(cleaned)

    if not provider and code:
        upper_code = code.upper()
        if upper_code.startswith("P24A"):
            provider = providers.providers.get("privatbank")
        elif re.fullmatch(r"\d{4}(?:-\d{4}){3,4}", upper_code):
            provider = providers.providers.get("abank")

    bank_key = provider.code if provider else None
    bank_label = provider.name if provider else None
    confidence = 0.9 if provider and code else 0.7 if (provider or code) else 0.0

    return ParsedReceipt(
        bank_label=bank_label,
        bank_key=bank_key,
        provider_code=bank_key,
        receipt_code=code,
        confidence=confidence,
        raw_text=cleaned,
        amount=amount,
        card_number=card_number,
    )
