import unittest
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch

from app.bot import ActiveOrdersState, ReceiptBot
from app.ocr import OCRError
from app.types import CheckResult, CheckStatus, ParsedReceipt


class _FakeTeleBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.answered_callbacks: list[tuple[str, str]] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self.replies: list[dict] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup=None,
        reply_to_message_id: int | None = None,
    ):
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return SimpleNamespace(message_id=100 + len(self.sent_messages))

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self.answered_callbacks.append((callback_query_id, text))

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted_messages.append((chat_id, message_id))

    def reply_to(self, message, text: str, reply_markup=None) -> None:
        self.replies.append({"message": message, "text": text, "reply_markup": reply_markup})


class _FakeProviders:
    def maybe_refresh(self) -> None:
        return None


class _FakeBinanceClient:
    def __init__(self, orders: list[SimpleNamespace]) -> None:
        self._orders = orders
        self.chat_calls: list[str] = []

    def get_active_orders(self) -> list[SimpleNamespace]:
        return self._orders

    def get_chat_messages(self, order_number: str) -> list[SimpleNamespace]:
        self.chat_calls.append(order_number)
        return []


class BotActiveOrdersFlowTests(unittest.TestCase):
    def _build_bot(self) -> tuple[ReceiptBot, _FakeTeleBot]:
        bot = ReceiptBot.__new__(ReceiptBot)
        fake_telebot = _FakeTeleBot()
        bot.bot = fake_telebot
        bot._state_lock = Lock()
        bot._manual_context = {}
        bot._active_orders_context = {}
        return bot, fake_telebot

    def test_prompt_active_orders_selection_sends_inline_buttons(self) -> None:
        bot, fake_telebot = self._build_bot()
        message = SimpleNamespace(chat=SimpleNamespace(id=777), message_id=88)

        bot._prompt_active_orders_selection(message)

        self.assertEqual(len(fake_telebot.sent_messages), 1)
        sent = fake_telebot.sent_messages[0]
        self.assertEqual(sent["text"], "Оберіть тип ордерів для перевірки:")
        self.assertEqual(sent["reply_to_message_id"], 88)
        markup = sent["reply_markup"]
        callback_data = [button.callback_data for row in markup.keyboard for button in row]
        self.assertEqual(callback_data, ["active_orders:buy", "active_orders:sell", "active_orders:all"])

    def test_active_orders_callback_buy_maps_to_buy_filter(self) -> None:
        bot, fake_telebot = self._build_bot()
        captured: list[str | None] = []

        def _capture_scan(message, test_mode: bool, trade_type_filter: str | None = None) -> None:
            self.assertFalse(test_mode)
            self.assertEqual(message.message_id, 42)
            captured.append(trade_type_filter)

        bot._handle_orders_scan = _capture_scan
        call = SimpleNamespace(
            data="active_orders:buy",
            id="cb-1",
            message=SimpleNamespace(chat=SimpleNamespace(id=777), message_id=42),
        )

        bot._handle_active_orders_filter_callback(call)

        self.assertEqual(captured, ["BUY"])
        self.assertEqual(fake_telebot.answered_callbacks, [("cb-1", "Запускаю перевірку")])

    def test_active_orders_callback_all_maps_to_no_filter(self) -> None:
        bot, fake_telebot = self._build_bot()
        captured: list[str | None] = []

        def _capture_scan(_message, test_mode: bool, trade_type_filter: str | None = None) -> None:
            self.assertFalse(test_mode)
            captured.append(trade_type_filter)

        bot._handle_orders_scan = _capture_scan
        call = SimpleNamespace(
            data="active_orders:all",
            id="cb-2",
            message=SimpleNamespace(chat=SimpleNamespace(id=777), message_id=43),
        )

        bot._handle_active_orders_filter_callback(call)

        self.assertEqual(captured, [None])
        self.assertEqual(fake_telebot.answered_callbacks, [("cb-2", "Запускаю перевірку")])

    def test_handle_orders_scan_filters_buy_orders(self) -> None:
        bot, fake_telebot = self._build_bot()
        bot.providers = _FakeProviders()
        bot.settings = SimpleNamespace(binance_test_non_active_order_numbers=[])
        bot.binance_client = _FakeBinanceClient(
            [
                SimpleNamespace(order_number="B1", trade_type="BUY", total_amount="10"),
                SimpleNamespace(order_number="S1", trade_type="SELL", total_amount="20"),
            ]
        )
        bot._close_check_gov_session = lambda: None
        message = SimpleNamespace(chat=SimpleNamespace(id=777), message_id=99, from_user=SimpleNamespace(id=9))

        bot._handle_orders_scan(message, test_mode=False, trade_type_filter="BUY")

        self.assertEqual(bot.binance_client.chat_calls, ["B1"])
        self.assertIn("⚠️ Немає повідомлень чату ордера", fake_telebot.sent_messages[-1]["text"])

    def test_handle_orders_scan_filters_sell_orders(self) -> None:
        bot, _fake_telebot = self._build_bot()
        bot.providers = _FakeProviders()
        bot.settings = SimpleNamespace(binance_test_non_active_order_numbers=[])
        bot.binance_client = _FakeBinanceClient(
            [
                SimpleNamespace(order_number="B1", trade_type="BUY", total_amount="10"),
                SimpleNamespace(order_number="S1", trade_type="SELL", total_amount="20"),
            ]
        )
        bot._safe_edit_or_send = lambda *_args, **_kwargs: None
        bot._close_check_gov_session = lambda: None
        message = SimpleNamespace(chat=SimpleNamespace(id=777), message_id=99, from_user=SimpleNamespace(id=9))

        bot._handle_orders_scan(message, test_mode=False, trade_type_filter="SELL")

        self.assertEqual(bot.binance_client.chat_calls, ["S1"])

    def test_handle_orders_scan_progress_steps_and_summary_with_ocr_failure(self) -> None:
        bot, _fake_telebot = self._build_bot()
        bot.providers = _FakeProviders()
        bot.settings = SimpleNamespace(binance_test_non_active_order_numbers=[])
        bot.binance_client = _FakeBinanceClient([SimpleNamespace(order_number="B1", trade_type="BUY", total_amount="10")])
        bot.binance_client.get_chat_messages = lambda _order_number: [
            SimpleNamespace(message_type="image", image_url="https://img/1"),
            SimpleNamespace(message_type="image", image_url="https://img/2"),
        ]
        bot._download_remote_image = lambda _url: SimpleNamespace(exists=lambda: False)
        bot._close_check_gov_session = lambda: None
        summary_holder: dict[str, str] = {}
        bot._send_long_text = lambda _chat_id, text, **_kwargs: summary_holder.update({"text": text})
        bot._run_check_for_active_orders = lambda _parsed, **_kwargs: CheckResult(
            status=CheckStatus.VALID,
            source="check.gov.ua",
            message="ok",
            details={"payment": {"amount": "100", "recipient_card": "4444********1111"}},
        )

        parsed_ok = ParsedReceipt(
            bank_label="Монобанк",
            bank_key="monobank",
            provider_code="monobank",
            receipt_code="9B1K-AKB5-C1MP-26B6",
            confidence=1.0,
            raw_text="",
        )
        with (
            patch("app.bot.extract_ocr_payload", side_effect=[SimpleNamespace(text="ok", docai_document=None), OCRError("bad ocr")]),
            patch("app.bot.parse_receipt_text", return_value=parsed_ok),
        ):
            message = SimpleNamespace(chat=SimpleNamespace(id=777), message_id=99, from_user=SimpleNamespace(id=9))
            bot._handle_orders_scan(message, test_mode=False, trade_type_filter=None)

        summary = summary_holder.get("text", "")
        self.assertIn("✅ 100 - 4444********1111 | Монобанк | 9B1K-AKB5-C1MP-26B6 | VALID", summary)
        self.assertIn("⚠️ Помилка OCR: bad ocr", summary)

    def test_format_reply_includes_result_message(self) -> None:
        bot, _fake_telebot = self._build_bot()
        parsed = ParsedReceipt(
            bank_label="Монобанк",
            bank_key="monobank",
            provider_code="monobank",
            receipt_code="9B1K-AKB5-C1MP-26B6",
            confidence=1.0,
            raw_text="",
        )
        result = CheckResult(
            status=CheckStatus.CHECK_ERROR,
            source="check.gov.ua",
            message="Помилка перевірки check.gov.ua: невизначена або застаріла відповідь",
        )

        text = bot._format_reply(parsed, result)

        self.assertIn("Деталі: Помилка перевірки check.gov.ua: невизначена або застаріла відповідь", text)

    def test_active_orders_line_appends_reason_for_non_valid_status(self) -> None:
        bot, _fake_telebot = self._build_bot()
        parsed = ParsedReceipt(
            bank_label="Монобанк",
            bank_key="monobank",
            provider_code="monobank",
            receipt_code="9B1K-AKB5-C1MP-26B6",
            confidence=1.0,
            raw_text="",
        )
        result = CheckResult(
            status=CheckStatus.CHECK_ERROR,
            source="check.gov.ua",
            message="Нестабільна відповідь сервісу",
        )

        line = bot._format_active_orders_line(parsed, result, "fallback")

        self.assertIn("| CHECK_ERROR | Нестабільна відповідь сервісу", line)

    def test_handle_orders_scan_rejects_parallel_run_for_same_user(self) -> None:
        bot, fake_telebot = self._build_bot()
        bot.providers = _FakeProviders()
        bot.settings = SimpleNamespace(binance_test_non_active_order_numbers=[])
        bot.binance_client = _FakeBinanceClient([])
        key = (777, 9)
        bot._active_orders_context[key] = ActiveOrdersState(
            source_message=SimpleNamespace(chat=SimpleNamespace(id=777), message_id=1),
            test_mode=False,
            tasks=[],
        )
        message = SimpleNamespace(chat=SimpleNamespace(id=777), message_id=99, from_user=SimpleNamespace(id=9))

        bot._handle_orders_scan(message, test_mode=False, trade_type_filter=None)

        self.assertEqual(len(fake_telebot.replies), 1)
        self.assertIn("вже виконується", fake_telebot.replies[0]["text"])


if __name__ == "__main__":
    unittest.main()
