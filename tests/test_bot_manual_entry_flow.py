import unittest
from threading import Lock
from types import SimpleNamespace

from app.bot import ManualEntryState, ReceiptBot
from app.types import CheckResult, CheckStatus, ParsedReceipt


class _FakeTeleBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.replies: list[dict] = []
        self.answered_callbacks: list[tuple[str, str]] = []

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

    def reply_to(self, message, text: str, reply_markup=None) -> None:
        self.replies.append(
            {
                "message": message,
                "text": text,
                "reply_markup": reply_markup,
            }
        )


class BotManualEntryFlowTests(unittest.TestCase):
    def _build_bot(self) -> tuple[ReceiptBot, _FakeTeleBot]:
        bot = ReceiptBot.__new__(ReceiptBot)
        fake_telebot = _FakeTeleBot()
        bot.bot = fake_telebot
        bot._state_lock = Lock()
        bot._manual_context = {}
        bot._active_orders_context = {}
        provider_aliases = {
            "privatbank": {"privatbank", "приватбанк", "приват"},
            "monobank": {"monobank", "монобанк", "mono"},
        }

        def _find_provider_by_text(text: str):
            norm = (text or "").strip().lower()
            for code, aliases in provider_aliases.items():
                if norm in aliases:
                    return SimpleNamespace(code=code, name=code)
            return None

        bot.providers = SimpleNamespace(
            providers={
                "privatbank": SimpleNamespace(name="Приватбанк"),
                "monobank": SimpleNamespace(name="Монобанк"),
            },
            find_provider_by_text=_find_provider_by_text,
        )
        return bot, fake_telebot

    def test_build_commands_menu_has_manual_code_button_second_row(self) -> None:
        markup = ReceiptBot._build_commands_menu()
        rows = [[button.get("text") if isinstance(button, dict) else button.text for button in row] for row in markup.keyboard]
        self.assertEqual(rows[0], ["Перевірити квитанцію", "Перевірити активні ордери"])
        self.assertEqual(rows[1], ["Ввести код квитанції"])

    def test_prompt_manual_provider_selection_sends_fixed_provider_buttons(self) -> None:
        bot, fake_telebot = self._build_bot()
        message = SimpleNamespace(chat=SimpleNamespace(id=777), message_id=55, from_user=SimpleNamespace(id=9))

        bot._prompt_manual_provider_selection(message)

        self.assertEqual(len(fake_telebot.sent_messages), 1)
        sent = fake_telebot.sent_messages[0]
        self.assertEqual(sent["text"], "Оберіть банк/сервіс для ручної перевірки:")
        callback_data = [button.callback_data for row in sent["reply_markup"].keyboard for button in row]
        self.assertEqual(
            callback_data,
            [
                "manual_provider:abank",
                "manual_provider:altabank",
                "manual_provider:vostok",
                "manual_provider:monobank",
                "manual_provider:mtb",
                "manual_provider:privatbank",
                "manual_provider:pumb",
                "manual_provider:sensbank",
                "manual_cancel",
            ],
        )
        pending = bot._manual_context[(777, 9)]
        self.assertEqual(pending.stage, "await_provider")
        self.assertIsNone(pending.parsed)

    def test_manual_provider_callback_switches_to_code_entry(self) -> None:
        bot, fake_telebot = self._build_bot()
        call = SimpleNamespace(
            data="manual_provider:privatbank",
            id="cb-1",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(chat=SimpleNamespace(id=777)),
        )

        bot._handle_manual_provider_callback(call)

        self.assertEqual(fake_telebot.answered_callbacks, [("cb-1", "Введіть код квитанції")])
        self.assertEqual(fake_telebot.sent_messages[-1]["text"], "Введіть код квитанції вручну одним повідомленням")
        cancel_callback_data = [button.callback_data for row in fake_telebot.sent_messages[-1]["reply_markup"].keyboard for button in row]
        self.assertEqual(cancel_callback_data, ["manual_cancel"])
        pending = bot._manual_context[(777, 9)]
        self.assertEqual(pending.stage, "await_code")
        self.assertEqual(pending.parsed.provider_code, "privatbank")
        self.assertEqual(pending.parsed.bank_label, "Приватбанк")

    def test_pending_manual_code_message_runs_check(self) -> None:
        bot, fake_telebot = self._build_bot()
        captured: list[ParsedReceipt] = []
        bot._run_check = lambda parsed, **_kwargs: captured.append(parsed) or CheckResult(
            status=CheckStatus.VALID,
            source="check.gov.ua",
            message="ok",
        )
        bot._format_reply = lambda parsed, _result: f"checked:{parsed.provider_code}:{parsed.receipt_code}"
        key = (777, 9)
        bot._manual_context[key] = ManualEntryState(
            stage="await_code",
            parsed=ParsedReceipt(
                bank_label="Приватбанк",
                bank_key="privatbank",
                provider_code="privatbank",
                receipt_code=None,
                confidence=0.0,
                raw_text="",
            ),
        )
        message = SimpleNamespace(chat=SimpleNamespace(id=777), from_user=SimpleNamespace(id=9), text=" p24a 1111-2222-3333-4444 ")

        handled = bot._handle_manual_code_message_if_pending(message)

        self.assertTrue(handled)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].provider_code, "privatbank")
        self.assertEqual(captured[0].receipt_code, "P24A1111-2222-3333-4444")
        self.assertNotIn(key, bot._manual_context)
        self.assertEqual(fake_telebot.replies[-1]["text"], "checked:privatbank:P24A1111-2222-3333-4444")

    def test_empty_manual_code_keeps_pending_state(self) -> None:
        bot, fake_telebot = self._build_bot()
        key = (777, 9)
        bot._manual_context[key] = ManualEntryState(
            stage="await_code",
            parsed=ParsedReceipt(
                bank_label="Приватбанк",
                bank_key="privatbank",
                provider_code="privatbank",
                receipt_code=None,
                confidence=0.0,
                raw_text="",
            ),
        )
        message = SimpleNamespace(chat=SimpleNamespace(id=777), from_user=SimpleNamespace(id=9), text="   ")

        handled = bot._handle_manual_code_message_if_pending(message)

        self.assertTrue(handled)
        self.assertEqual(fake_telebot.replies[-1]["text"], "Код порожній. Спробуйте ще раз.")
        self.assertIn(key, bot._manual_context)

    def test_manual_cancel_clears_state(self) -> None:
        bot, fake_telebot = self._build_bot()
        key = (777, 9)
        bot._manual_context[key] = ManualEntryState(stage="await_provider", parsed=None)
        call = SimpleNamespace(
            id="cb-2",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(chat=SimpleNamespace(id=777)),
        )

        bot._handle_manual_cancel_callback(call)

        self.assertNotIn(key, bot._manual_context)
        self.assertEqual(fake_telebot.answered_callbacks, [("cb-2", "Скасовано")])
        self.assertEqual(fake_telebot.sent_messages[-1]["text"], "Ручне введення коду скасовано")
        handled_after_cancel = bot._handle_manual_code_message_if_pending(
            SimpleNamespace(chat=SimpleNamespace(id=777), from_user=SimpleNamespace(id=9), text="ANYCODE")
        )
        self.assertFalse(handled_after_cancel)

    def test_existing_inline_manual_code_callback_still_works(self) -> None:
        bot, fake_telebot = self._build_bot()
        captured: list[ParsedReceipt] = []
        bot._run_check = lambda parsed, **_kwargs: captured.append(parsed) or CheckResult(
            status=CheckStatus.VALID,
            source="check.gov.ua",
            message="ok",
        )
        bot._format_reply = lambda parsed, _result: f"ok:{parsed.provider_code}:{parsed.receipt_code}"
        call = SimpleNamespace(
            data="manual_code:privatbank",
            id="cb-3",
            from_user=SimpleNamespace(id=9),
            message=SimpleNamespace(chat=SimpleNamespace(id=777)),
        )
        message = SimpleNamespace(chat=SimpleNamespace(id=777), from_user=SimpleNamespace(id=9), text="1234-5678-9012-3456")

        bot._handle_manual_code_callback(call)
        handled = bot._handle_manual_code_message_if_pending(message)

        self.assertTrue(handled)
        self.assertEqual(fake_telebot.answered_callbacks, [("cb-3", "Введіть код квитанції")])
        self.assertEqual(fake_telebot.sent_messages[0]["text"], "Введіть код квитанції вручну одним повідомленням")
        self.assertEqual(fake_telebot.sent_messages[-1]["text"], "🌐 Перевіряю квитанцію")
        self.assertEqual(captured[0].provider_code, "privatbank")
        self.assertEqual(captured[0].receipt_code, "1234-5678-9012-3456")

    def test_manual_debug_flow_text_provider_monobank_then_receipt_code(self) -> None:
        bot, fake_telebot = self._build_bot()
        captured: list[ParsedReceipt] = []
        bot._run_check = lambda parsed, **_kwargs: captured.append(parsed) or CheckResult(
            status=CheckStatus.VALID,
            source="check.gov.ua",
            message="ok",
        )
        bot._format_reply = lambda parsed, _result: f"ok:{parsed.provider_code}:{parsed.receipt_code}"
        key = (777, 9)
        bot._manual_context[key] = ManualEntryState(stage="await_provider", parsed=None)

        provider_message = SimpleNamespace(chat=SimpleNamespace(id=777), from_user=SimpleNamespace(id=9), text="monobank")
        code_message = SimpleNamespace(chat=SimpleNamespace(id=777), from_user=SimpleNamespace(id=9), text="9B1K-AKB5-C1MP-26B6")

        handled_provider = bot._handle_manual_code_message_if_pending(provider_message)
        handled_code = bot._handle_manual_code_message_if_pending(code_message)

        self.assertTrue(handled_provider)
        self.assertTrue(handled_code)
        self.assertEqual(captured[0].provider_code, "monobank")
        self.assertEqual(captured[0].receipt_code, "9B1K-AKB5-C1MP-26B6")
        self.assertEqual(fake_telebot.replies[0]["text"], "Введіть код квитанції вручну одним повідомленням")
        self.assertEqual(fake_telebot.replies[1]["text"], "ok:monobank:9B1K-AKB5-C1MP-26B6")


if __name__ == "__main__":
    unittest.main()
