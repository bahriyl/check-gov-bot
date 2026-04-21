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


if __name__ == "__main__":
    unittest.main()
