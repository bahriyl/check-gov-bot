import unittest

from app.bot import ReceiptBot


class BotAmountResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = ReceiptBot.__new__(ReceiptBot)

    def test_normalize_amount_case(self) -> None:
        self.assertEqual(self.bot._normalize_amount_value("1 200.00"), "1200")
        self.assertEqual(self.bot._normalize_amount_value("1,060.00"), "1060")

    def test_prefers_card_matched_amount(self) -> None:
        amount = self.bot._resolve_amount_from_chat_messages(
            image_time=1000,
            text_events=[
                (998, "Сума 1 060.00"),
                (1002, "4441111042298079 1 200.00"),
            ],
            ocr_card_number="4441111042298079",
            ocr_amount="540",
        )
        self.assertEqual(amount, "1200")

    def test_uses_nearest_amount_when_no_card_match(self) -> None:
        amount = self.bot._resolve_amount_from_chat_messages(
            image_time=1000,
            text_events=[
                (900, "Сума 1200"),
                (995, "Сума 1060"),
            ],
            ocr_card_number="",
            ocr_amount="540",
        )
        self.assertEqual(amount, "1060")


if __name__ == "__main__":
    unittest.main()
