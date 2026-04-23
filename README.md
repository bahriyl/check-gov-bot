# check-gov-bot

Telegram bot (Python + `pytelegrambotapi`) that validates bank receipts from photos/screenshots.

## What it does

- Accepts receipt image from Telegram chat
- Supports Binance order chat scan via `/active_orders` and `/test_active_orders`
- Runs OCR with configurable provider:
  - `PaddleOCR` (`OCR_PROVIDER=paddle`)
  - Google Document AI (`OCR_PROVIDER=docai`)
- Detects bank/service and extracts receipt/document code
- Checks:
  - `ПриватБанк` via direct request to `https://privatbank.ua/pb/ajax/find-document`
  - other providers via full browser automation on `https://check.gov.ua/` (select provider, enter receipt code, click `Перевірити`, parse rendered result)

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

For Binance active-order chat scan set:

- `BINANCE_API_KEY`
- `BINANCE_SECRET_KEY`
- Optional: `BINANCE_BASE_URL`, `BINANCE_TIMEOUT_SECONDS`
- Optional targeted test mode: `BINANCE_TEST_NON_ACTIVE_ORDER_NUMBERS=123,456` (comma-separated non-active order numbers for `/test_active_orders`)

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

Use `/active_orders` in Telegram to choose order side (`Купівля`, `Продаж`, `Усі`), then scan matching active Binance orders, image messages in each order chat, and validate detected receipts.
Use `/test_active_orders` to run scan only for non-active order numbers from `BINANCE_TEST_NON_ACTIVE_ORDER_NUMBERS`.
Menu buttons in chat:
- `Перевірити квитанцію`
- `Перевірити активні ордери` (opens the same side selection: `Купівля`, `Продаж`, `Усі`)
- `Ввести код квитанції` (select provider and enter receipt code manually)

## Amount And Card Source

Amount and recipient card are not extracted from OCR text.

Source of truth is provider verification APIs:
- `check.gov.ua /api/handler`: first payment object (`payments[0]`), including `recipient` and `amount`.
- `privatbank.ua /pb/ajax/find-document`: on success, bot uses `token` to download receipt PDF and extracts amount/card from PDF text.

Bot stores normalized provider payment data inside `CheckResult.details["payment"]`.

## Notes

- Provider list for `check.gov.ua` is refreshed automatically from the website (with fallback defaults).
- First OCR run may download PaddleOCR models and take longer.
- If OCR quality is poor, bot may return `UNPARSEABLE`.

## OCR Example Regression Test

To run OCR validation on images in `examples/`:

```bash
rtk proxy sh -lc 'RUN_OCR_EXAMPLES=1 .venv/bin/python -m unittest tests/test_ocr_examples.py -v'
```

## Tests

Run full suite:

```bash
rtk .venv/bin/python -m unittest discover -s tests -v
```
