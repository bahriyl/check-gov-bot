from __future__ import annotations

import re

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



def parse_receipt_text(text: str, providers: ProviderRegistry) -> ParsedReceipt:
    cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    code = _extract_code(cleaned)
    provider = providers.find_provider_by_text(cleaned)

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
    )
