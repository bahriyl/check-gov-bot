# check-gov-bot

Telegram bot (Python + `pytelegrambotapi`) that validates bank receipts from photos/screenshots.

## What it does

- Accepts receipt image from Telegram chat
- Runs OCR with configurable provider:
  - `PaddleOCR` (`OCR_PROVIDER=paddle`)
  - Google Document AI (`OCR_PROVIDER=docai`)
- Detects bank/service and extracts receipt/document code
- Checks:
  - `ПриватБанк` via direct request to `https://privatbank.ua/pb/ajax/find-document`
  - other providers via `https://check.gov.ua/api/handler`
- For `check.gov.ua` runs the request inside Playwright page context (captcha + fetch in the same browser session), because server-side raw HTTP calls can be rejected with `400 Bad request`

## Requirements

- Python 3.10+
- Playwright Chromium browser

## Setup

```bash
rtk python3 -m venv .venv
rtk .venv/bin/pip install -r requirements.txt
rtk .venv/bin/playwright install chromium
```

Create `.env` from `.env.example` and set `BOT_TOKEN`.

If using Google Document AI, configure these env vars:

- `OCR_PROVIDER=docai`
- `GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json`
- `DOCAI_PROJECT_ID`
- `DOCAI_LOCATION` (for example `us` or `eu`)
- `DOCAI_PROCESSOR_ID`

## Run

```bash
rtk .venv/bin/python main.py
```

## Notes

- Provider list for `check.gov.ua` is refreshed automatically from the website (with fallback defaults).
- First OCR run may download PaddleOCR models and take longer.
- If OCR quality is poor, bot may return `UNPARSEABLE`.

## OCR Example Regression Test

To run OCR validation on images in `examples/`:

```bash
rtk proxy sh -lc 'RUN_OCR_EXAMPLES=1 .venv/bin/python -m unittest tests/test_ocr_examples.py -v'
```
