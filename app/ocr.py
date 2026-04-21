from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

import cv2
import numpy as np

class OCRError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _get_ocr_engine(lang: str):
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:
        raise OCRError(
            "Paddle OCR dependencies are missing or broken. "
            "Run: rtk .venv/bin/python -m pip install -r requirements.txt. "
            f"Original error: {exc}"
        ) from exc

    # Keep one model instance for all messages to reduce startup overhead.
    return PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)


def _build_variants(image_path: Path) -> list[tuple[str, np.ndarray]]:
    image = cv2.imread(str(image_path))
    if image is None:
        raise OCRError(f"Cannot read image: {image_path}")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

    return [
        ("orig", image),
        ("up_gray", upscaled),
    ]


def _extract_lines(result: object) -> list[str]:
    lines: list[str] = []
    for block in result or []:
        if not block:
            continue
        for item in block:
            try:
                text = item[1][0]
            except Exception:
                continue
            if text and text.strip():
                lines.append(text.strip())
    return lines


def _quality_score(text: str) -> int:
    import re

    score = 0
    upper = text.upper()
    if re.search(r"P24A[A-Z0-9]{8,}", upper):
        score += 10
    if re.search(r"[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}", upper):
        score += 8
    if re.search(r"\d{4}(?:-\d{4}){3}", upper):
        score += 6
    if "MONOBANK" in upper:
        score += 2
    if "PRIVAT" in upper or "ПРИВАТ" in upper:
        score += 2
    if "A-BANK" in upper or "А-БАНК" in upper or "ABANK" in upper:
        score += 2
    score += min(4, len(text) // 250)
    return score


def _normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    return "\n".join(line.strip() for line in text.splitlines())


def _extract_text_paddle(image_path: Path) -> str:
    attempts: list[tuple[str, str, np.ndarray]] = []
    for variant_name, img in _build_variants(image_path):
        # English model recognizes receipt codes better; Ukrainian keeps Cyrillic bank names.
        attempts.append((variant_name, "en", img))
        attempts.append((variant_name, "uk", img))

    best_text: str | None = None
    best_score = -1
    errors: list[str] = []

    for variant_name, lang, img in attempts:
        try:
            result = _get_ocr_engine(lang).ocr(img, cls=True)
            lines = _extract_lines(result)
            if not lines:
                continue
            text = "\n".join(lines)
            score = _quality_score(text)
            if score > best_score:
                best_score = score
                best_text = text
            if score >= 12:
                break
        except Exception as exc:
            errors.append(f"{variant_name}/{lang}: {exc}")

    if not best_text:
        suffix = f" ({'; '.join(errors)})" if errors else ""
        raise OCRError(f"No OCR text extracted{suffix}")

    return _normalize_text(best_text)


def _extract_text_docai(image_path: Path) -> str:
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    project_id = os.getenv("DOCAI_PROJECT_ID", "").strip()
    location = os.getenv("DOCAI_LOCATION", "").strip()
    processor_id = os.getenv("DOCAI_PROCESSOR_ID", "").strip()
    timeout = int(os.getenv("DOCAI_TIMEOUT_SECONDS", "30"))

    missing = [
        name
        for name, value in (
            ("GOOGLE_APPLICATION_CREDENTIALS", credentials_path),
            ("DOCAI_PROJECT_ID", project_id),
            ("DOCAI_LOCATION", location),
            ("DOCAI_PROCESSOR_ID", processor_id),
        )
        if not value
    ]
    if missing:
        raise OCRError(
            "Document AI config missing env vars: " + ", ".join(sorted(missing))
        )

    try:
        from google.api_core.client_options import ClientOptions
        from google.oauth2 import service_account
        from google.cloud import documentai
    except Exception as exc:
        raise OCRError(
            "Google Document AI dependencies are missing. "
            "Run: rtk .venv/bin/python -m pip install -r requirements.txt. "
            f"Original error: {exc}"
        ) from exc

    try:
        client_options = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
        credentials = service_account.Credentials.from_service_account_file(credentials_path)
        client = documentai.DocumentProcessorServiceClient(
            client_options=client_options,
            credentials=credentials,
        )
    except Exception as exc:
        raise OCRError(f"Failed to initialize Document AI client: {exc}") from exc

    name = client.processor_path(project_id, location, processor_id)
    mime_type = "application/octet-stream"
    suffix = image_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        mime_type = "image/jpeg"
    elif suffix == ".png":
        mime_type = "image/png"
    elif suffix == ".pdf":
        mime_type = "application/pdf"

    try:
        raw_document = documentai.RawDocument(content=image_path.read_bytes(), mime_type=mime_type)
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)
        result = client.process_document(request=request, timeout=timeout)
    except Exception as exc:
        raise OCRError(f"Document AI OCR failed: {exc}") from exc

    text = (result.document.text or "").strip()
    if not text:
        raise OCRError("No OCR text extracted")
    return _normalize_text(text)


def extract_text(image_path: Path) -> str:
    provider = os.getenv("OCR_PROVIDER", "paddle").strip().lower() or "paddle"
    if provider == "docai":
        try:
            return _extract_text_docai(image_path)
        except OCRError:
            # Keep bot working if Document AI is temporarily unavailable/misconfigured.
            return _extract_text_paddle(image_path)
    if provider == "paddle":
        return _extract_text_paddle(image_path)
    raise OCRError("Unsupported OCR_PROVIDER. Use: paddle or docai")
