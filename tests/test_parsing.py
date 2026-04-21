import unittest

from app.parsing import parse_receipt_text
from app.providers import ProviderRegistry


class ParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.providers = ProviderRegistry()

    def test_monobank_receipt(self) -> None:
        text = """
        monobank
        Квитанція № KPT2-0T15-39BM-HX28 від 20.04.2026
        """
        parsed = parse_receipt_text(text, self.providers)
        self.assertEqual(parsed.provider_code, "monobank")
        self.assertEqual(parsed.receipt_code, "KPT2-0T15-39BM-HX28")

    def test_abank_receipt(self) -> None:
        text = """
        а-банк
        Квитанція № 2300-8317-6223-0167
        """
        parsed = parse_receipt_text(text, self.providers)
        self.assertEqual(parsed.provider_code, "abank")
        self.assertEqual(parsed.receipt_code, "2300-8317-6223-0167")

    def test_privat_receipt(self) -> None:
        text = """
        Платіжна інструкція
        Код документа P24A5738337141D5456
        АТ КБ ПРИВАТБАНК
        """
        parsed = parse_receipt_text(text, self.providers)
        self.assertEqual(parsed.provider_code, "privatbank")
        self.assertEqual(parsed.receipt_code, "P24A5738337141D5456")

    def test_extract_amount_and_card(self) -> None:
        text = """
        Квитанція
        Сума: 1 000.00 UAH
        Картка отримувача: 4441 1111 1522 0419
        Код документа P24A5738337141D5456
        """
        parsed = parse_receipt_text(text, self.providers)
        self.assertEqual(parsed.amount, "1000")
        self.assertEqual(parsed.card_number, "4441111115220419")

    def test_prefers_sum_over_authorization_code(self) -> None:
        text = """
        monobank
        Квитанція № P7B1-CCA8-A268-T58B
        Деталі транзакції
        Сума (грн)
        540.00
        Код авторизації
        840856
        Комісія (грн)
        0.00
        Дата і час операції
        21.04.2026 18:32
        """
        parsed = parse_receipt_text(text, self.providers)
        self.assertEqual(parsed.amount, "540")

    def test_amount_is_first_number_after_suma(self) -> None:
        text = """
        Деталі транзакції
        Сума (грн)
        540.00
        Комісія (грн)
        0.00
        Дата і час операції
        21.04.2026 18:32
        """
        parsed = parse_receipt_text(text, self.providers)
        self.assertEqual(parsed.amount, "540")

    def test_ocr_mixed_script_mono_receipt_amount_1200(self) -> None:
        text = """
        erani tpah3akuii
        Cyma (rph)
        1200.00
        Komicia (rph)
        0.00
        487407******4812
        """
        parsed = parse_receipt_text(text, self.providers)
        self.assertEqual(parsed.amount, "1200")

    def test_ocr_mixed_script_mono_receipt_amount_1060(self) -> None:
        text = """
        etani Tpah3akyil
        Cyma (rph)
        1060.00
        Komicia (rph)
        0.00
        21.04.2026 18:32
        """
        parsed = parse_receipt_text(text, self.providers)
        self.assertEqual(parsed.amount, "1060")


if __name__ == "__main__":
    unittest.main()
