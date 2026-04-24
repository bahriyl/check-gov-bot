from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import requests
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.binance import BinanceAPIError, BinanceP2PClient
from app.checkers import CheckGovChecker, PrivatChecker
from app.config import Settings
from app.ocr import OCRError, extract_ocr_payload
from app.parsing import parse_receipt_text
from app.providers import ProviderRegistry
from app.types import CheckResult, CheckStatus, ParsedReceipt


@dataclass
class ManualEntryState:
    stage: str
    parsed: ParsedReceipt | None = None


@dataclass
class ActiveOrderTask:
    order_key: str
    order_prefix: str
    image_idx: int
    image_total: int
    image_url: str


@dataclass
class ActiveOrdersState:
    source_message: telebot.types.Message
    test_mode: bool
    tasks: list[ActiveOrderTask]
    progress_message_id: int | None = None
    next_index: int = 0
    lines_by_order: dict[str, list[str]] | None = None
    order_labels: dict[str, str] | None = None
    waiting_task: ActiveOrderTask | None = None
    waiting_parsed: ParsedReceipt | None = None


class ReceiptBot:
    MANUAL_ENTRY_PROVIDERS: tuple[tuple[str, str], ...] = (
        ("abank", "А-Банк"),
        ("altabank", "АльтБанк"),
        ("vostok", "VST | Банк Власний Рахунок"),
        ("monobank", "Монобанк"),
        ("mtb", "МТБ БАНК"),
        ("privatbank", "Приватбанк"),
        ("pumb", "ПУМБ"),
        ("sensbank", "СЕНС БАНК"),
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bot = telebot.TeleBot(settings.bot_token, num_threads=settings.bot_handler_workers)
        self.providers = ProviderRegistry(
            refresh_hours=settings.provider_refresh_hours,
        )
        self.check_gov_checker = CheckGovChecker(
            timeout_seconds=settings.http_timeout_seconds,
        )
        self.privat_checker = PrivatChecker(timeout_seconds=settings.http_timeout_seconds)
        self._state_lock = Lock()
        self._manual_context: dict[tuple[int, int], ManualEntryState] = {}
        self._active_orders_context: dict[tuple[int, int], ActiveOrdersState] = {}

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
                reply_markup=self._build_commands_menu(),
            )

        @self.bot.message_handler(commands=["active_orders"])
        def _active_orders(message: telebot.types.Message) -> None:
            self._prompt_active_orders_selection(message)

        @self.bot.message_handler(commands=["test_active_orders"])
        def _test_active_orders(message: telebot.types.Message) -> None:
            self._handle_orders_scan(message, test_mode=True)

        @self.bot.message_handler(func=lambda message: (message.text or "").strip() == "Перевірити квитанцію")
        def _check_receipt_button(message: telebot.types.Message) -> None:
            self.bot.reply_to(message, "Надішліть, будь ласка, фото або скріншот квитанції.")

        @self.bot.message_handler(func=lambda message: (message.text or "").strip() == "Перевірити активні ордери")
        def _check_active_orders_button(message: telebot.types.Message) -> None:
            self._prompt_active_orders_selection(message)

        @self.bot.message_handler(func=lambda message: (message.text or "").strip() == "Ввести код квитанції")
        def _manual_receipt_code_button(message: telebot.types.Message) -> None:
            self._prompt_manual_provider_selection(message)

        @self.bot.callback_query_handler(func=lambda call: (call.data or "").startswith("active_orders:"))
        def _active_orders_filter_callback(call: telebot.types.CallbackQuery) -> None:
            self._handle_active_orders_filter_callback(call)

        @self.bot.message_handler(content_types=["photo", "document"])
        def _handle_receipt(message: telebot.types.Message) -> None:
            self._handle_receipt_message(message)

        @self.bot.callback_query_handler(func=lambda call: (call.data or "").startswith("manual_code:"))
        def _manual_code_callback(call: telebot.types.CallbackQuery) -> None:
            self._handle_manual_code_callback(call)

        @self.bot.callback_query_handler(func=lambda call: (call.data or "").startswith("manual_provider:"))
        def _manual_provider_callback(call: telebot.types.CallbackQuery) -> None:
            self._handle_manual_provider_callback(call)

        @self.bot.callback_query_handler(func=lambda call: (call.data or "") == "manual_cancel")
        def _manual_cancel_callback(call: telebot.types.CallbackQuery) -> None:
            self._handle_manual_cancel_callback(call)

        @self.bot.callback_query_handler(func=lambda call: (call.data or "").startswith("active_manual_provider:"))
        def _active_manual_provider_callback(call: telebot.types.CallbackQuery) -> None:
            self._handle_active_manual_provider_callback(call)

        @self.bot.callback_query_handler(func=lambda call: (call.data or "") == "active_manual_skip")
        def _active_manual_skip_callback(call: telebot.types.CallbackQuery) -> None:
            self._handle_active_manual_skip_callback(call)

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

    def _run_check(self, parsed: ParsedReceipt, user_scope: str | None = None) -> CheckResult:
        if not parsed.receipt_code:
            return CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="local",
                message="Не вдалося знайти номер/код квитанції",
            )
        if not parsed.provider_code:
            return CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="local",
                message="Не вдалося визначити банк/сервіс",
            )

        if parsed.provider_code == "privatbank":
            return self.privat_checker.check(parsed.receipt_code)
        return self.check_gov_checker.check(parsed.provider_code, parsed.receipt_code, user_scope=user_scope)

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
    def _build_commands_menu() -> ReplyKeyboardMarkup:
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.row(
            KeyboardButton("Перевірити квитанцію"),
            KeyboardButton("Перевірити активні ордери"),
        )
        markup.row(KeyboardButton("Ввести код квитанції"))
        return markup

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

    def _build_manual_provider_menu(self) -> InlineKeyboardMarkup:
        markup = InlineKeyboardMarkup()
        for provider_code, label in self.MANUAL_ENTRY_PROVIDERS:
            markup.row(InlineKeyboardButton(text=label, callback_data=f"manual_provider:{provider_code}"))
        markup.row(InlineKeyboardButton(text="Скасувати перевірку", callback_data="manual_cancel"))
        return markup

    def _build_active_manual_provider_menu(self) -> InlineKeyboardMarkup:
        markup = InlineKeyboardMarkup()
        for provider_code, label in self.MANUAL_ENTRY_PROVIDERS:
            markup.row(InlineKeyboardButton(text=label, callback_data=f"active_manual_provider:{provider_code}"))
        markup.row(InlineKeyboardButton(text="Пропустити цю квитанцію", callback_data="active_manual_skip"))
        return markup

    @staticmethod
    def _build_manual_cancel_menu() -> InlineKeyboardMarkup:
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton(text="Скасувати", callback_data="manual_cancel"))
        return markup

    @staticmethod
    def _build_user_scope(chat_id: int, user_id: int) -> str:
        return f"{chat_id}:{user_id}"

    def _manual_set(self, key: tuple[int, int], state: ManualEntryState) -> None:
        with self._state_lock:
            self._manual_context[key] = state

    def _manual_get(self, key: tuple[int, int]) -> ManualEntryState | None:
        with self._state_lock:
            return self._manual_context.get(key)

    def _manual_pop(self, key: tuple[int, int]) -> ManualEntryState | None:
        with self._state_lock:
            return self._manual_context.pop(key, None)

    def _active_get(self, key: tuple[int, int]) -> ActiveOrdersState | None:
        with self._state_lock:
            return self._active_orders_context.get(key)

    def _active_set(self, key: tuple[int, int], state: ActiveOrdersState) -> None:
        with self._state_lock:
            self._active_orders_context[key] = state

    def _active_pop(self, key: tuple[int, int]) -> ActiveOrdersState | None:
        with self._state_lock:
            return self._active_orders_context.pop(key, None)

    def _active_start_if_idle(self, key: tuple[int, int], state: ActiveOrdersState) -> bool:
        with self._state_lock:
            if key in self._active_orders_context:
                return False
            self._active_orders_context[key] = state
            return True

    @staticmethod
    def _debug_test_active_orders_log(enabled: bool, text: str) -> None:
        if enabled:
            print(f"[test_active_orders] {text}", flush=True)

    def _resolve_manual_provider_from_text(self, text: str) -> tuple[str, str] | None:
        provider_map = dict(self.MANUAL_ENTRY_PROVIDERS)
        if not text:
            return None
        norm = text.strip().lower()
        for code, label in self.MANUAL_ENTRY_PROVIDERS:
            if norm in {code.lower(), label.strip().lower()}:
                return code, label

        finder = getattr(self.providers, "find_provider_by_text", None)
        if callable(finder):
            provider = finder(text)
            if provider and provider.code in provider_map:
                return provider.code, provider_map[provider.code]
        return None

    @staticmethod
    def _trade_type_label(trade_type: str) -> str:
        return "Купівля" if trade_type.upper() == "BUY" else "Продаж"

    def _build_active_orders_filter_menu(self) -> InlineKeyboardMarkup:
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton(text="Купівля", callback_data="active_orders:buy"),
            InlineKeyboardButton(text="Продаж", callback_data="active_orders:sell"),
            InlineKeyboardButton(text="Усі", callback_data="active_orders:all"),
        )
        return markup

    def _prompt_active_orders_selection(self, message: telebot.types.Message) -> None:
        self.bot.send_message(
            message.chat.id,
            "Оберіть тип ордерів для перевірки:",
            reply_markup=self._build_active_orders_filter_menu(),
            reply_to_message_id=message.message_id,
        )

    def _prompt_manual_provider_selection(self, message: telebot.types.Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        key = (message.chat.id, user_id)
        self._manual_set(key, ManualEntryState(stage="await_provider"))
        self.bot.send_message(
            message.chat.id,
            "Оберіть банк/сервіс для ручної перевірки:",
            reply_markup=self._build_manual_provider_menu(),
            reply_to_message_id=message.message_id,
        )

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
        reason = (result.message or "").strip()
        if reason:
            lines.append(f"Деталі: {reason}")
        payment = result.details.get("payment") if isinstance(result.details, dict) else None
        if isinstance(payment, dict):
            if payment.get("amount") is not None:
                lines.append(f"Сума: {payment.get('amount')}")
            if payment.get("recipient_card"):
                lines.append(f"Картка отримувача: {payment.get('recipient_card')}")
            if payment.get("recipient"):
                lines.append(f"Отримувач: {payment.get('recipient')}")
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
        payload = extract_ocr_payload(image_path)
        parsed = parse_receipt_text(
            payload.text,
            self.providers,
            docai_document=payload.docai_document,
        )
        result = self._run_check(parsed)
        return parsed, result

    def _run_check_for_active_orders(self, parsed: ParsedReceipt, user_scope: str | None = None) -> CheckResult:
        if not parsed.receipt_code:
            return CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="local",
                message="Не вдалося знайти номер/код квитанції",
            )
        if not parsed.provider_code:
            return CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="local",
                message="Не вдалося визначити банк/сервіс",
            )
        if parsed.provider_code == "privatbank":
            return self.privat_checker.check(parsed.receipt_code)
        return self.check_gov_checker.check(
            parsed.provider_code,
            parsed.receipt_code,
            reload_before_check=True,
            user_scope=user_scope,
        )

    def _format_active_orders_line(self, parsed: ParsedReceipt | None, result: CheckResult | None, fallback: str) -> str:
        if not parsed or not result:
            return f"⚠️ {fallback}"
        code = parsed.receipt_code or "Не знайдено"
        provider = parsed.bank_label or parsed.provider_code or "Невідомо"
        payment = result.details.get("payment") if isinstance(result.details, dict) else None
        amount = "?"
        card = "?"
        if isinstance(payment, dict):
            amount = str(payment.get("amount") or "?")
            card = str(payment.get("recipient_card") or "?")
        line = f"{self._status_icon(result)} {amount} - {card} | {provider} | {code} | {result.status.value}"
        reason = (result.message or "").strip()
        if result.status != CheckStatus.VALID and reason:
            line = f"{line} | {reason}"
        return line

    def _active_key(self, message: telebot.types.Message) -> tuple[int, int]:
        from_user = getattr(message, "from_user", None)
        user_id = from_user.id if from_user else 0
        return (message.chat.id, user_id)

    def _send_active_unknown_provider_prompt(self, state: ActiveOrdersState, task: ActiveOrderTask, parsed: ParsedReceipt) -> None:
        chat_id = state.source_message.chat.id
        text = (
            f"Не вдалося визначити банк для квитанції {task.image_idx}/{task.image_total}.\n"
            f"Код квитанції: {parsed.receipt_code or 'Не знайдено'}\n"
            "Оберіть банк/сервіс або пропустіть цю квитанцію."
        )
        try:
            self.bot.send_photo(
                chat_id,
                task.image_url,
                caption=text,
                reply_markup=self._build_active_manual_provider_menu(),
                reply_to_message_id=state.source_message.message_id,
            )
        except Exception:
            self.bot.send_message(
                chat_id,
                f"{text}\nФото: {task.image_url}",
                reply_markup=self._build_active_manual_provider_menu(),
                reply_to_message_id=state.source_message.message_id,
            )

    def _set_active_progress(self, state: ActiveOrdersState, text: str) -> None:
        self._safe_edit_or_send(
            state.source_message.chat.id,
            state.progress_message_id,
            text,
            fallback_to_message_id=state.source_message.message_id,
        )

    def _finalize_active_orders_state(self, state: ActiveOrdersState) -> None:
        self._set_active_progress(state, "🧾 Формую підсумок перевірки")
        blocks: list[str] = []
        for order_key, label in (state.order_labels or {}).items():
            lines = (state.lines_by_order or {}).get(order_key) or []
            if not lines:
                continue
            blocks.append(f"{label}:\n" + "\n".join(lines))
        if not blocks:
            self.bot.send_message(
                state.source_message.chat.id,
                "У чатах активних ордерів не знайдено валідних фото-квитанцій",
                reply_to_message_id=state.source_message.message_id,
            )
            return
        self._send_long_text(
            state.source_message.chat.id,
            "\n\n".join(blocks),
            reply_to_message_id=state.source_message.message_id,
        )

    def _continue_active_orders_scan(self, key: tuple[int, int]) -> None:
        state = self._active_get(key)
        if not state:
            return
        user_scope = self._build_user_scope(key[0], key[1])
        while True:
            with self._state_lock:
                state = self._active_orders_context.get(key)
                if not state:
                    return
                if state.next_index >= len(state.tasks):
                    break
                task = state.tasks[state.next_index]
            parsed: ParsedReceipt | None = None
            result: CheckResult | None = None
            temp_path: Path | None = None
            try:
                self._set_active_progress(
                    state,
                    f"🔎 OCR зображення {task.image_idx}/{task.image_total} для ордера {task.order_key}",
                )
                temp_path = self._download_remote_image(task.image_url)
                payload = extract_ocr_payload(temp_path)
                parsed = parse_receipt_text(payload.text, self.providers, docai_document=payload.docai_document)
            except OCRError as exc:
                with self._state_lock:
                    if state.lines_by_order is not None:
                        state.lines_by_order.setdefault(task.order_key, []).append(
                            f"{task.image_idx}. {self._format_active_orders_line(None, None, f'Помилка OCR: {exc}')}"
                        )
                    state.next_index += 1
                continue
            except Exception as exc:
                with self._state_lock:
                    if state.lines_by_order is not None:
                        state.lines_by_order.setdefault(task.order_key, []).append(
                            f"{task.image_idx}. {self._format_active_orders_line(None, None, f'Помилка завантаження: {exc}')}"
                        )
                    state.next_index += 1
                continue
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink(missing_ok=True)

            if not parsed.receipt_code:
                with self._state_lock:
                    if state.lines_by_order is not None:
                        state.lines_by_order.setdefault(task.order_key, []).append(
                            f"{task.image_idx}. {self._format_active_orders_line(parsed, None, 'Не вдалося знайти код квитанції')}"
                        )
                    state.next_index += 1
                continue

            if not parsed.provider_code:
                self._set_active_progress(
                    state,
                    f"⏸️ Очікую вибір банку для квитанції {task.image_idx}/{task.image_total} (ордер {task.order_key})",
                )
                with self._state_lock:
                    state.waiting_task = task
                    state.waiting_parsed = parsed
                self._send_active_unknown_provider_prompt(state, task, parsed)
                return

            try:
                self._set_active_progress(
                    state,
                    f"🌐 Перевіряю квитанцію {task.image_idx}/{task.image_total} для ордера {task.order_key}",
                )
                result = self._run_check_for_active_orders(parsed, user_scope=user_scope)
                with self._state_lock:
                    if state.lines_by_order is not None:
                        state.lines_by_order.setdefault(task.order_key, []).append(
                            f"{task.image_idx}. {self._format_active_orders_line(parsed, result, 'Помилка перевірки')}"
                        )
            except Exception as exc:
                with self._state_lock:
                    if state.lines_by_order is not None:
                        state.lines_by_order.setdefault(task.order_key, []).append(
                            f"{task.image_idx}. {self._format_active_orders_line(parsed, None, f'Помилка перевірки: {exc}')}"
                        )
            with self._state_lock:
                state.next_index += 1

        self._finalize_active_orders_state(state)
        self._active_pop(key)

    def _handle_orders_scan(
        self,
        message: telebot.types.Message,
        test_mode: bool,
        trade_type_filter: str | None = None,
    ) -> None:
        if not self.binance_client:
            self.bot.reply_to(
                message,
                "Для команд /active_orders і /test_active_orders треба налаштувати BINANCE_API_KEY і BINANCE_SECRET_KEY в .env",
            )
            return

        key = self._active_key(message)
        if self._active_get(key):
            self.bot.reply_to(message, "Сканування вже виконується для цього користувача. Дочекайтесь завершення.")
            return

        trade_type_filter_norm = (trade_type_filter or "").strip().upper()
        if trade_type_filter_norm not in {"BUY", "SELL"}:
            trade_type_filter_norm = ""
        self._debug_test_active_orders_log(
            test_mode,
            f"start scan chat_id={message.chat.id} trade_type_filter={trade_type_filter_norm or 'ALL'}",
        )
        progress_label = "тестові неактивні" if test_mode else "активні"
        if trade_type_filter_norm:
            progress_label = f"{progress_label} ({self._trade_type_label(trade_type_filter_norm)})"
        progress = self.bot.send_message(
            message.chat.id,
            f"🔄 Завантажую {progress_label} ордери Binance",
            reply_to_message_id=message.message_id,
        )
        progress_message_id = progress.message_id

        try:
            if test_mode:
                if not self.settings.binance_test_non_active_order_numbers:
                    self._safe_edit_or_send(
                        message.chat.id,
                        progress_message_id,
                        "Не вказано BINANCE_TEST_NON_ACTIVE_ORDER_NUMBERS для /test_active_orders",
                        fallback_to_message_id=message.message_id,
                    )
                    self._debug_test_active_orders_log(test_mode, "missing BINANCE_TEST_NON_ACTIVE_ORDER_NUMBERS, abort")
                    return
                self._debug_test_active_orders_log(
                    test_mode,
                    f"loading history orders for numbers={self.settings.binance_test_non_active_order_numbers}",
                )
                orders = self.binance_client.get_orders_from_history_by_numbers(
                    self.settings.binance_test_non_active_order_numbers
                )
            else:
                orders = self.binance_client.get_active_orders()
            self._debug_test_active_orders_log(test_mode, f"loaded orders count={len(orders)} before filter")
            if trade_type_filter_norm:
                orders = [order for order in orders if order.trade_type.upper() == trade_type_filter_norm]
                self._debug_test_active_orders_log(
                    test_mode, f"orders count after trade_type filter={len(orders)}"
                )
            if not orders:
                if test_mode:
                    empty_message = "Тестові ордери не знайдено. Перевірте BINANCE_TEST_NON_ACTIVE_ORDER_NUMBERS"
                elif trade_type_filter_norm:
                    empty_message = f"Активних ордерів типу {self._trade_type_label(trade_type_filter_norm)} не знайдено"
                else:
                    empty_message = "Активних ордерів не знайдено"
                self._safe_edit_or_send(
                    message.chat.id,
                    progress_message_id,
                    empty_message,
                    fallback_to_message_id=message.message_id,
                )
                self._debug_test_active_orders_log(test_mode, "no orders after filtering, abort")
                return

            tasks: list[ActiveOrderTask] = []
            order_labels: dict[str, str] = {}
            lines_by_order: dict[str, list[str]] = {}
            for idx, order in enumerate(orders, start=1):
                self._safe_edit_or_send(
                    message.chat.id,
                    progress_message_id,
                    f"📥 Збираю дані для ордера {idx}/{len(orders)}: {order.order_number}",
                    fallback_to_message_id=message.message_id,
                )
                self._debug_test_active_orders_log(
                    test_mode,
                    f"order {idx}/{len(orders)} number={order.order_number} trade_type={order.trade_type} total={order.total_amount}",
                )
                order_amount = self._format_amount_for_order(order.total_amount)
                order_key = str(order.order_number)
                order_labels[order_key] = f"{self._order_prefix(order.trade_type)} {order_amount}"
                lines_by_order.setdefault(order_key, [])
                chat_messages = self.binance_client.get_chat_messages(order.order_number)
                self._debug_test_active_orders_log(
                    test_mode, f"order {order.order_number}: chat messages count={len(chat_messages)}"
                )
                if not chat_messages:
                    lines_by_order[order_key].append("⚠️ Немає повідомлень чату ордера")
                    self._debug_test_active_orders_log(test_mode, f"order {order.order_number}: no chat messages")
                    continue
                seen_urls: set[str] = set()
                image_urls = []
                for item in chat_messages:
                    if item.message_type != "image" or not item.image_url:
                        continue
                    if item.image_url in seen_urls:
                        continue
                    seen_urls.add(item.image_url)
                    image_urls.append(item.image_url)
                self._debug_test_active_orders_log(
                    test_mode, f"order {order.order_number}: unique image urls count={len(image_urls)}"
                )
                if not image_urls:
                    lines_by_order[order_key].append("⚠️ У чаті ордера немає зображень")
                    self._debug_test_active_orders_log(test_mode, f"order {order.order_number}: no images")
                    continue
                for image_idx, image_url in enumerate(image_urls, start=1):
                    tasks.append(
                        ActiveOrderTask(
                            order_key=order_key,
                            order_prefix=order_labels[order_key],
                            image_idx=image_idx,
                            image_total=len(image_urls),
                            image_url=image_url,
                        )
                    )
            state = ActiveOrdersState(
                source_message=message,
                test_mode=test_mode,
                tasks=tasks,
                progress_message_id=progress_message_id,
                lines_by_order=lines_by_order,
                order_labels=order_labels,
            )
            if not self._active_start_if_idle(key, state):
                self._safe_edit_or_send(
                    message.chat.id,
                    progress_message_id,
                    "Сканування вже виконується для цього користувача. Дочекайтесь завершення.",
                    fallback_to_message_id=message.message_id,
                )
                return
            self._continue_active_orders_scan(key)
        except BinanceAPIError as exc:
            self._safe_edit_or_send(
                message.chat.id,
                progress_message_id,
                f"Помилка Binance API: {exc}",
                fallback_to_message_id=message.message_id,
            )
            self._debug_test_active_orders_log(test_mode, f"Binance API error={exc}")
        except Exception as exc:
            self._safe_edit_or_send(
                message.chat.id,
                progress_message_id,
                f"Помилка обробки активних ордерів: {exc}",
                fallback_to_message_id=message.message_id,
            )
            self._debug_test_active_orders_log(test_mode, f"processing error={exc}")

    def _handle_active_orders_filter_callback(self, call: telebot.types.CallbackQuery) -> None:
        data = (call.data or "").strip().lower()
        selected = data.split(":", 1)[1] if ":" in data else ""
        selected_to_trade_type = {
            "buy": "BUY",
            "sell": "SELL",
            "all": None,
        }
        trade_type = selected_to_trade_type.get(selected)
        if selected not in selected_to_trade_type:
            self.bot.answer_callback_query(call.id, "Невідомий тип ордерів")
            return
        if not call.message:
            self.bot.answer_callback_query(call.id, "Помилка контексту")
            return

        self.bot.answer_callback_query(call.id, "Запускаю перевірку")
        self._handle_orders_scan(call.message, test_mode=False, trade_type_filter=trade_type)

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
        self._manual_set((chat_id, user_id), ManualEntryState(stage="await_code", parsed=parsed))
        self.bot.answer_callback_query(call.id, "Введіть код квитанції")
        self.bot.send_message(
            chat_id,
            "Введіть код квитанції вручну одним повідомленням",
            reply_markup=self._build_manual_cancel_menu(),
        )

    def _handle_manual_provider_callback(self, call: telebot.types.CallbackQuery) -> None:
        data = call.data or ""
        provider_code = data.split(":", 1)[1] if ":" in data else ""
        provider_map = dict(self.MANUAL_ENTRY_PROVIDERS)
        if provider_code not in provider_map:
            self.bot.answer_callback_query(call.id, "Невідомий банк/сервіс")
            return

        user_id = call.from_user.id if call.from_user else 0
        chat_id = call.message.chat.id if call.message and call.message.chat else 0
        if not chat_id or not user_id:
            self.bot.answer_callback_query(call.id, "Помилка контексту")
            return

        key = (chat_id, user_id)
        pending = self._manual_get(key)
        if pending and pending.stage == "await_provider_receipt" and pending.parsed and pending.parsed.receipt_code:
            parsed = ParsedReceipt(
                bank_label=provider_map[provider_code],
                bank_key=provider_code,
                provider_code=provider_code,
                receipt_code=pending.parsed.receipt_code,
                confidence=1.0,
                raw_text=pending.parsed.raw_text,
            )
            progress = self.bot.send_message(chat_id, "🌐 Перевіряю квитанцію")
            result = self._run_check(parsed, user_scope=self._build_user_scope(chat_id, user_id))
            self._manual_pop(key)
            self.bot.answer_callback_query(call.id, "Перевіряю квитанцію")
            self._safe_edit_or_send(
                chat_id,
                progress.message_id,
                self._format_reply(parsed, result),
                reply_markup=self._build_manual_button(parsed),
            )
            return

        parsed = ParsedReceipt(
            bank_label=provider_map[provider_code],
            bank_key=provider_code,
            provider_code=provider_code,
            receipt_code=None,
            confidence=0.0,
            raw_text="",
        )
        self._manual_set(key, ManualEntryState(stage="await_code", parsed=parsed))
        self.bot.answer_callback_query(call.id, "Введіть код квитанції")
        self.bot.send_message(
            chat_id,
            "Введіть код квитанції вручну одним повідомленням",
            reply_markup=self._build_manual_cancel_menu(),
        )

    def _handle_manual_cancel_callback(self, call: telebot.types.CallbackQuery) -> None:
        user_id = call.from_user.id if call.from_user else 0
        chat_id = call.message.chat.id if call.message and call.message.chat else 0
        if not chat_id or not user_id:
            self.bot.answer_callback_query(call.id, "Помилка контексту")
            return

        key = (chat_id, user_id)
        if self._manual_pop(key) is None:
            self.bot.answer_callback_query(call.id, "Немає активного ручного вводу")
            return
        self.bot.answer_callback_query(call.id, "Скасовано")
        self.bot.send_message(chat_id, "Ручне введення коду скасовано")

    def _handle_active_manual_provider_callback(self, call: telebot.types.CallbackQuery) -> None:
        data = call.data or ""
        provider_code = data.split(":", 1)[1] if ":" in data else ""
        provider_map = dict(self.MANUAL_ENTRY_PROVIDERS)
        if provider_code not in provider_map:
            self.bot.answer_callback_query(call.id, "Невідомий банк/сервіс")
            return
        if not call.message:
            self.bot.answer_callback_query(call.id, "Помилка контексту")
            return
        key = self._active_key(call.message)
        state = self._active_get(key)
        if not state:
            self.bot.answer_callback_query(call.id, "Немає квитанції для ручного вибору")
            return
        with self._state_lock:
            waiting_task = state.waiting_task
            waiting_parsed = state.waiting_parsed
        if not waiting_task or not waiting_parsed:
            self.bot.answer_callback_query(call.id, "Немає квитанції для ручного вибору")
            return
        parsed = ParsedReceipt(
            bank_label=provider_map[provider_code],
            bank_key=provider_code,
            provider_code=provider_code,
            receipt_code=waiting_parsed.receipt_code,
            confidence=1.0,
            raw_text=waiting_parsed.raw_text,
        )
        task = waiting_task
        try:
            result = self._run_check_for_active_orders(parsed, user_scope=self._build_user_scope(*key))
            with self._state_lock:
                if state.lines_by_order is not None:
                    state.lines_by_order.setdefault(task.order_key, []).append(
                        f"{task.image_idx}. {self._format_active_orders_line(parsed, result, 'Помилка перевірки')}"
                    )
        except Exception as exc:
            with self._state_lock:
                if state.lines_by_order is not None:
                    state.lines_by_order.setdefault(task.order_key, []).append(
                        f"{task.image_idx}. {self._format_active_orders_line(parsed, None, f'Помилка перевірки: {exc}')}"
                    )
        with self._state_lock:
            state.waiting_task = None
            state.waiting_parsed = None
            state.next_index += 1
        self.bot.answer_callback_query(call.id, "Квитанцію перевірено")
        self._continue_active_orders_scan(key)

    def _handle_active_manual_skip_callback(self, call: telebot.types.CallbackQuery) -> None:
        if not call.message:
            self.bot.answer_callback_query(call.id, "Помилка контексту")
            return
        key = self._active_key(call.message)
        state = self._active_get(key)
        if not state:
            self.bot.answer_callback_query(call.id, "Немає квитанції для пропуску")
            return
        with self._state_lock:
            task = state.waiting_task
            if not task:
                self.bot.answer_callback_query(call.id, "Немає квитанції для пропуску")
                return
            if state.lines_by_order is not None:
                state.lines_by_order.setdefault(task.order_key, []).append(f"{task.image_idx}. ⚠️ Квитанцію пропущено вручну")
            state.waiting_task = None
            state.waiting_parsed = None
            state.next_index += 1
        self.bot.answer_callback_query(call.id, "Квитанцію пропущено")
        self._continue_active_orders_scan(key)

    def _handle_manual_code_message_if_pending(self, message: telebot.types.Message) -> bool:
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else 0
        key = (chat_id, user_id)
        pending = self._manual_get(key)
        if not pending:
            return False

        if pending.stage == "await_provider":
            matched_provider = self._resolve_manual_provider_from_text(message.text or "")
            if not matched_provider:
                self.bot.reply_to(
                    message,
                    "Оберіть банк/сервіс кнопками вище або введіть назву/код банку зі списку. Для виходу натисніть «Скасувати».",
                )
                return True

            provider_code, provider_label = matched_provider
            parsed = ParsedReceipt(
                bank_label=provider_label,
                bank_key=provider_code,
                provider_code=provider_code,
                receipt_code=None,
                confidence=0.0,
                raw_text="",
            )
            self._manual_set(key, ManualEntryState(stage="await_code", parsed=parsed))
            self.bot.reply_to(
                message,
                "Введіть код квитанції вручну одним повідомленням",
                reply_markup=self._build_manual_cancel_menu(),
            )
            return True

        if pending.stage == "await_provider_receipt":
            matched_provider = self._resolve_manual_provider_from_text(message.text or "")
            if not matched_provider or not pending.parsed or not pending.parsed.receipt_code:
                self.bot.reply_to(
                    message,
                    "Оберіть банк/сервіс кнопками вище або введіть назву/код банку зі списку. Для виходу натисніть «Скасувати перевірку».",
                )
                return True
            provider_code, provider_label = matched_provider
            parsed = ParsedReceipt(
                bank_label=provider_label,
                bank_key=provider_code,
                provider_code=provider_code,
                receipt_code=pending.parsed.receipt_code,
                confidence=1.0,
                raw_text=pending.parsed.raw_text,
            )
            self.bot.send_message(
                chat_id,
                "🌐 Перевіряю квитанцію",
                reply_to_message_id=getattr(message, "message_id", None),
            )
            result = self._run_check(parsed, user_scope=self._build_user_scope(chat_id, user_id))
            self.bot.reply_to(message, self._format_reply(parsed, result), reply_markup=self._build_manual_button(parsed))
            self._manual_pop(key)
            return True

        if pending.stage != "await_code" or not pending.parsed:
            self._manual_pop(key)
            return False

        manual_code = self._sanitize_manual_receipt_code(message.text or "")
        if not manual_code:
            self.bot.reply_to(message, "Код порожній. Спробуйте ще раз.")
            return True

        parsed = ParsedReceipt(
            bank_label=pending.parsed.bank_label,
            bank_key=pending.parsed.bank_key,
            provider_code=pending.parsed.provider_code,
            receipt_code=manual_code,
            confidence=1.0,
            raw_text=pending.parsed.raw_text,
        )
        self.bot.send_message(
            chat_id,
            "🌐 Перевіряю квитанцію",
            reply_to_message_id=getattr(message, "message_id", None),
        )
        result = self._run_check(parsed, user_scope=self._build_user_scope(chat_id, user_id))
        self.bot.reply_to(message, self._format_reply(parsed, result), reply_markup=self._build_manual_button(parsed))
        self._manual_pop(key)
        return True

    def _handle_receipt_message(self, message: telebot.types.Message) -> None:
        temp_path: Path | None = None
        progress_message_id: int | None = None
        result_sent = False
        try:
            progress = self.bot.send_message(message.chat.id, "📥 Отримано квитанцію")
            progress_message_id = progress.message_id

            self.providers.maybe_refresh()
            self._safe_edit_or_send(message.chat.id, progress_message_id, "🔎 Розпізнаю текст (OCR)")
            temp_path = self._download_image(message)
            payload = extract_ocr_payload(temp_path)
            parsed = parse_receipt_text(
                payload.text,
                self.providers,
                docai_document=payload.docai_document,
            )
            if parsed.receipt_code and not parsed.provider_code:
                user_id = message.from_user.id if message.from_user else 0
                self._manual_set(
                    (message.chat.id, user_id),
                    ManualEntryState(
                        stage="await_provider_receipt",
                        parsed=parsed,
                    ),
                )
                self._safe_edit_or_send(
                    message.chat.id,
                    progress_message_id,
                    "Не вдалося визначити банк автоматично. Оберіть банк/сервіс для перевірки:",
                    fallback_to_message_id=message.message_id,
                    reply_markup=self._build_manual_provider_menu(),
                )
                result_sent = True
                return
            self._safe_edit_or_send(message.chat.id, progress_message_id, "🌐 Перевіряю квитанцію")
            user_id = message.from_user.id if message.from_user else 0
            result = self._run_check(parsed, user_scope=self._build_user_scope(message.chat.id, user_id))
            self._safe_edit_or_send(
                message.chat.id,
                progress_message_id,
                self._format_reply(parsed, result),
                fallback_to_message_id=message.message_id,
                reply_markup=self._build_manual_button(parsed),
            )
            result_sent = True
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
            result_sent = True
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
            result_sent = True
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def run(self) -> None:
        self.bot.infinity_polling(skip_pending=True)
