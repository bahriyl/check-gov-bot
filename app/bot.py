from __future__ import annotations

import re
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.binance import BinanceAPIError, BinanceP2PClient
from app.checkers import CheckGovChecker, PrivatChecker
from app.config import Settings
from app.ocr import OCRError, extract_text
from app.parsing import parse_receipt_text
from app.providers import ProviderRegistry
from app.types import CheckResult, CheckStatus, ParsedReceipt


class ReceiptBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot = telebot.TeleBot(settings.bot_token)
        self.providers = ProviderRegistry(
            headless=settings.playwright_headless,
            refresh_hours=settings.provider_refresh_hours,
        )
        self.check_gov_checker = CheckGovChecker(
            headless=settings.playwright_headless,
            timeout_seconds=settings.http_timeout_seconds,
        )
        self.privat_checker = PrivatChecker(timeout_seconds=settings.http_timeout_seconds)
        self._manual_context: dict[tuple[int, int], ParsedReceipt] = {}

        self.binance_client: BinanceP2PClient | None = None
        if settings.binance_api_key and settings.binance_secret_key:
            self.binance_client = BinanceP2PClient(
                api_key=settings.binance_api_key,
                secret_key=settings.binance_secret_key,
                base_url=settings.binance_base_url,
                timeout_seconds=settings.binance_timeout_seconds,
            )

        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.bot.message_handler(commands=["start", "help"])
        def _start(message: telebot.types.Message) -> None:
            self.bot.reply_to(
                message,
                "Надішліть фото або скріншот квитанції. Я визначу банк, знайду код та перевірю платіж.",
            )

        @self.bot.message_handler(commands=["active_orders_receipts"])
        def _active_orders_receipts(message: telebot.types.Message) -> None:
            self._handle_active_orders_receipts(message)

        @self.bot.message_handler(content_types=["photo", "document"])
        def _handle_receipt(message: telebot.types.Message) -> None:
            self._handle_receipt_message(message)

        @self.bot.callback_query_handler(func=lambda call: (call.data or "").startswith("manual_code:"))
        def _manual_code_callback(call: telebot.types.CallbackQuery) -> None:
            self._handle_manual_code_callback(call)

        @self.bot.message_handler(func=lambda _: True)
        def _fallback(message: telebot.types.Message) -> None:
            if self._handle_manual_code_message_if_pending(message):
                return
            self.bot.reply_to(message, "Надішліть, будь ласка, фото або скріншот квитанції.")

    def _download_image(self, message: telebot.types.Message) -> Path:
        file_id = None
        suffix = ".jpg"

        if message.photo:
            file_id = message.photo[-1].file_id
        elif message.document:
            mime = (message.document.mime_type or "").lower()
            if not mime.startswith("image/"):
                raise ValueError("Файл має бути зображенням")
            file_id = message.document.file_id
            if message.document.file_name and "." in message.document.file_name:
                suffix = "." + message.document.file_name.rsplit(".", 1)[1]
        if not file_id:
            raise ValueError("Не вдалося отримати файл")

        file_info = self.bot.get_file(file_id)
        file_bytes = self.bot.download_file(file_info.file_path)

        temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp.write(file_bytes)
        temp.flush()
        temp.close()
        return Path(temp.name)

    def _download_remote_image(self, image_url: str) -> Path:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        resp = requests.get(image_url, headers=headers, timeout=self.settings.http_timeout_seconds)
        resp.raise_for_status()

        content_type = (resp.headers.get("Content-Type") or "").lower()
        suffix = ".jpg"
        if "png" in content_type:
            suffix = ".png"
        elif "webp" in content_type:
            suffix = ".webp"

        temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp.write(resp.content)
        temp.flush()
        temp.close()
        return Path(temp.name)

    def _run_check(self, parsed: ParsedReceipt) -> CheckResult:
        if not parsed.receipt_code:
            return CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="local",
                message="Не вдалося знайти номер/код квитанції",
            )

        if parsed.provider_code == "privatbank":
            return self.privat_checker.check(parsed.receipt_code)

        provider_code = parsed.provider_code
        if not provider_code:
            return CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="local",
                message="Не вдалося визначити банк/сервіс квитанції",
            )

        return self.check_gov_checker.check(provider_code, parsed.receipt_code)

    @staticmethod
    def _sanitize_manual_receipt_code(code: str) -> str:
        return "".join(ch for ch in code.strip().upper().replace(" ", "") if ch.isalnum() or ch == "-")

    @staticmethod
    def _order_prefix(trade_type: str) -> str:
        return "Купівля" if trade_type.upper() == "BUY" else "Продаж"

    @staticmethod
    def _status_icon(result: CheckResult) -> str:
        return "✅" if result.status == CheckStatus.VALID else "❌"

    @staticmethod
    def _format_amount_for_order(value: str) -> str:
        try:
            num = float(str(value).replace(" ", "").replace(",", "."))
            if num.is_integer():
                return str(int(num))
            return (f"{num:.2f}").rstrip("0").rstrip(".")
        except Exception:
            return str(value)

    @staticmethod
    def _normalize_amount_value(raw: str) -> str | None:
        cleaned = raw.replace("\u00a0", " ").strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            if cleaned.count(",") == 1 and len(cleaned.rsplit(",", 1)[1]) <= 2:
                cleaned = cleaned.replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
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

    @staticmethod
    def _normalize_card_value(raw: str | None) -> str:
        return re.sub(r"\D", "", raw or "")

    @staticmethod
    def _text_contains_card(text: str, card_digits: str) -> bool:
        if not text or not card_digits:
            return False
        digits_in_text = re.sub(r"\D", "", text)
        if len(card_digits) >= 10 and card_digits in digits_in_text:
            return True
        last4 = card_digits[-4:] if len(card_digits) >= 4 else ""
        if not last4:
            return False
        compact = text.replace(" ", "").lower()
        return (
            f"****{last4}" in compact
            or f"******{last4}" in compact
            or f"xxxx{last4}" in compact
            or f"xxxxxx{last4}" in compact
        )

    @staticmethod
    def _extract_amount_candidates_from_text(text: str) -> list[str]:
        if not text:
            return []
        cleaned = text
        cleaned = re.sub(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", " ", cleaned)
        cleaned = re.sub(r"\b\d{1,2}:\d{2}\b", " ", cleaned)
        pattern = re.compile(r"\b\d{1,3}(?:[ \u00A0]\d{3})+(?:[.,]\d{1,2})?\b|\b\d+(?:[.,]\d{1,2})?\b")
        out: list[str] = []
        for match in pattern.finditer(cleaned):
            token = match.group(0)
            digits_only = re.sub(r"\D", "", token)
            if "." not in token and "," not in token and " " not in token and len(digits_only) >= 7:
                continue
            normalized = ReceiptBot._normalize_amount_value(token)
            if normalized:
                out.append(normalized)
        return out

    def _resolve_amount_from_chat_messages(
        self,
        image_time: int,
        text_events: list[tuple[int, str]],
        ocr_card_number: str | None,
        ocr_amount: str | None,
    ) -> str | None:
        if not text_events:
            return self._normalize_amount_value(ocr_amount or "")

        card_digits = self._normalize_card_value(ocr_card_number)
        ranked: list[tuple[tuple[int, int], bool, str]] = []
        for ts, text in text_events:
            amounts = self._extract_amount_candidates_from_text(text)
            if not amounts:
                continue
            delta = ts - image_time
            rank = (abs(delta), 0 if delta <= 0 else 1)
            has_card = self._text_contains_card(text, card_digits)
            ranked.append((rank, has_card, amounts[0]))

        if not ranked:
            return self._normalize_amount_value(ocr_amount or "")

        card_ranked = [item for item in ranked if item[1]]
        if card_ranked:
            card_ranked.sort(key=lambda item: item[0])
            return card_ranked[0][2]

        ranked.sort(key=lambda item: item[0])
        return ranked[0][2]

    def _build_manual_button(self, parsed: ParsedReceipt) -> InlineKeyboardMarkup | None:
        if not parsed.provider_code:
            return None
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                text="Ввести код вручну",
                callback_data=f"manual_code:{parsed.provider_code}",
            )
        )
        return markup

    def _format_reply(self, parsed: ParsedReceipt, result: CheckResult) -> str:
        status_emoji = {
            CheckStatus.VALID: "✅",
            CheckStatus.NOT_FOUND: "❌",
            CheckStatus.INVALID: "⚠️",
            CheckStatus.UNPARSEABLE: "🧩",
            CheckStatus.CHECK_ERROR: "🚨",
        }
        bank = parsed.bank_label or "Невідомо"
        code = parsed.receipt_code or "Не знайдено"
        lines = [
            "Результат перевірки:",
            f"Банк/сервіс: {bank}",
            f"Код квитанції: {code}",
            f"Джерело перевірки: {result.source}",
            f"Статус: {status_emoji.get(result.status, '🚨')}",
        ]
        return "\n".join(lines)

    def _safe_edit_or_send(
        self,
        chat_id: int,
        progress_message_id: int | None,
        text: str,
        fallback_to_message_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if progress_message_id:
            try:
                self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=progress_message_id, reply_markup=reply_markup
                )
                return
            except Exception:
                pass
        self.bot.send_message(
            chat_id,
            text,
            reply_to_message_id=fallback_to_message_id,
            reply_markup=reply_markup,
        )

    def _send_long_text(self, chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
        max_len = 3900
        if len(text) <= max_len:
            self.bot.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
            return
        chunk: list[str] = []
        chunk_len = 0
        for line in text.splitlines():
            if chunk_len + len(line) + 1 > max_len and chunk:
                self.bot.send_message(chat_id, "\n".join(chunk), reply_to_message_id=reply_to_message_id)
                chunk = []
                chunk_len = 0
                reply_to_message_id = None
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            self.bot.send_message(chat_id, "\n".join(chunk), reply_to_message_id=reply_to_message_id)

    def _process_local_image(self, image_path: Path) -> tuple[ParsedReceipt, CheckResult]:
        text = extract_text(image_path)
        parsed = parse_receipt_text(text, self.providers)
        result = self._run_check(parsed)
        return parsed, result

    def _handle_active_orders_receipts(self, message: telebot.types.Message) -> None:
        if not self.binance_client:
            self.bot.reply_to(
                message,
                "Для команди /active_orders_receipts треба налаштувати BINANCE_API_KEY і BINANCE_SECRET_KEY в .env",
            )
            return

        progress = self.bot.send_message(message.chat.id, "🔄 Завантажую активні ордери Binance")
        progress_message_id = progress.message_id

        try:
            self.providers.maybe_refresh()
            orders = self.binance_client.get_active_orders()
            if not orders and self.settings.binance_test_include_latest_non_active:
                orders = self.binance_client.get_latest_orders_from_history(
                    limit=self.settings.binance_test_latest_non_active_count
                )
            if not orders:
                self._safe_edit_or_send(message.chat.id, progress_message_id, "Активних ордерів не знайдено")
                return

            output_blocks: list[str] = []
            for idx, order in enumerate(orders, start=1):
                self._safe_edit_or_send(
                    message.chat.id,
                    progress_message_id,
                    f"🔍 Обробляю ордер {idx}/{len(orders)}: {order.order_number}",
                )
                chat_messages = self.binance_client.get_chat_messages(order.order_number)
                if not chat_messages:
                    continue
                images = [m for m in chat_messages if m.message_type == "image" and m.image_url]
                text_events = [
                    (m.message_time, m.content)
                    for m in chat_messages
                    if m.message_type in {"text", "system", "auto_reply"} and m.content
                ]
                if not images:
                    continue

                lines: list[str] = []
                seen_urls: set[str] = set()
                for image in images:
                    if image.image_url in seen_urls:
                        continue
                    seen_urls.add(image.image_url)

                    temp_path: Path | None = None
                    try:
                        temp_path = self._download_remote_image(image.image_url)
                        parsed, result = self._process_local_image(temp_path)
                    except OCRError:
                        continue
                    except Exception:
                        continue
                    finally:
                        if temp_path and temp_path.exists():
                            temp_path.unlink(missing_ok=True)

                    if not parsed.provider_code or not parsed.receipt_code:
                        continue

                    amount = self._resolve_amount_from_chat_messages(
                        image_time=image.message_time,
                        text_events=text_events,
                        ocr_card_number=parsed.card_number,
                        ocr_amount=parsed.amount,
                    ) or "?"
                    card = parsed.card_number or "?"
                    lines.append(f"{amount} - {card} - {self._status_icon(result)}")

                if not lines:
                    continue

                order_amount = self._format_amount_for_order(order.total_amount)
                output_blocks.append(f"{self._order_prefix(order.trade_type)} {order_amount}:\n" + "\n".join(lines))

            if not output_blocks:
                self._safe_edit_or_send(message.chat.id, progress_message_id, "У чатах активних ордерів не знайдено валідних фото-квитанцій")
                return

            summary = "\n\n".join(output_blocks)
            try:
                self.bot.delete_message(message.chat.id, progress_message_id)
            except Exception:
                pass
            self._send_long_text(message.chat.id, summary, reply_to_message_id=message.message_id)
        except BinanceAPIError as exc:
            self._safe_edit_or_send(
                message.chat.id,
                progress_message_id,
                f"Помилка Binance API: {exc}",
                fallback_to_message_id=message.message_id,
            )
        except Exception as exc:
            self._safe_edit_or_send(
                message.chat.id,
                progress_message_id,
                f"Помилка обробки активних ордерів: {exc}",
                fallback_to_message_id=message.message_id,
            )

    def _handle_manual_code_callback(self, call: telebot.types.CallbackQuery) -> None:
        data = call.data or ""
        provider_code = data.split(":", 1)[1] if ":" in data else ""
        if not provider_code:
            self.bot.answer_callback_query(call.id, "Не вдалося визначити банк")
            return

        user_id = call.from_user.id if call.from_user else 0
        chat_id = call.message.chat.id if call.message and call.message.chat else 0
        if not chat_id or not user_id:
            self.bot.answer_callback_query(call.id, "Помилка контексту")
            return

        provider = self.providers.providers.get(provider_code)
        parsed = ParsedReceipt(
            bank_label=provider.name if provider else provider_code,
            bank_key=provider_code,
            provider_code=provider_code,
            receipt_code=None,
            confidence=0.0,
            raw_text="",
        )
        self._manual_context[(chat_id, user_id)] = parsed
        self.bot.answer_callback_query(call.id, "Введіть код квитанції")
        self.bot.send_message(chat_id, "Введіть код квитанції вручну одним повідомленням")

    def _handle_manual_code_message_if_pending(self, message: telebot.types.Message) -> bool:
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else 0
        key = (chat_id, user_id)
        pending = self._manual_context.get(key)
        if not pending:
            return False

        manual_code = self._sanitize_manual_receipt_code(message.text or "")
        if not manual_code:
            self.bot.reply_to(message, "Код порожній. Спробуйте ще раз.")
            return True

        parsed = ParsedReceipt(
            bank_label=pending.bank_label,
            bank_key=pending.bank_key,
            provider_code=pending.provider_code,
            receipt_code=manual_code,
            confidence=1.0,
            raw_text=pending.raw_text,
        )
        result = self._run_check(parsed)
        self.bot.reply_to(message, self._format_reply(parsed, result), reply_markup=self._build_manual_button(parsed))
        self._manual_context.pop(key, None)
        return True

    def _handle_receipt_message(self, message: telebot.types.Message) -> None:
        temp_path: Path | None = None
        progress_message_id: int | None = None
        try:
            progress = self.bot.send_message(message.chat.id, "📥 Отримано квитанцію")
            progress_message_id = progress.message_id

            self.providers.maybe_refresh()
            self._safe_edit_or_send(message.chat.id, progress_message_id, "🔎 Розпізнаю текст (OCR)")
            temp_path = self._download_image(message)
            text = extract_text(temp_path)
            parsed = parse_receipt_text(text, self.providers)
            self._safe_edit_or_send(message.chat.id, progress_message_id, "🌐 Перевіряю квитанцію")
            result = self._run_check(parsed)
            self._safe_edit_or_send(
                message.chat.id,
                progress_message_id,
                self._format_reply(parsed, result),
                fallback_to_message_id=message.message_id,
                reply_markup=self._build_manual_button(parsed),
            )
        except OCRError as exc:
            parsed = ParsedReceipt(
                bank_label=None,
                bank_key=None,
                provider_code=None,
                receipt_code=None,
                confidence=0.0,
                raw_text="",
            )
            result = CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="local",
                message=f"Помилка OCR: {exc}",
            )
            self._safe_edit_or_send(
                message.chat.id,
                progress_message_id,
                self._format_reply(parsed, result),
                fallback_to_message_id=message.message_id,
            )
        except Exception as exc:
            parsed = ParsedReceipt(
                bank_label=None,
                bank_key=None,
                provider_code=None,
                receipt_code=None,
                confidence=0.0,
                raw_text="",
            )
            result = CheckResult(
                status=CheckStatus.CHECK_ERROR,
                source="local",
                message=f"Помилка обробки: {exc}",
            )
            self._safe_edit_or_send(
                message.chat.id,
                progress_message_id,
                self._format_reply(parsed, result),
                fallback_to_message_id=message.message_id,
            )
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def run(self) -> None:
        self.bot.infinity_polling(skip_pending=True)
