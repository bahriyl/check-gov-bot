import os
import unittest
from pathlib import Path

from app.ocr import extract_text
from app.parsing import parse_receipt_text
from app.providers import ProviderRegistry


@unittest.skipUnless(os.getenv("RUN_OCR_EXAMPLES") == "1", "Set RUN_OCR_EXAMPLES=1 to run slow OCR example tests")
class OcrExamplesTests(unittest.TestCase):
    def test_examples_receipts(self) -> None:
        expected = {
            "photo_2026-04-20_12-55-03 (2).jpg": ("monobank", "84B0-3368-9584-T805"),
            "photo_2026-04-20_12-55-03.jpg": ("monobank", "96KP-4BX9-CA9A-B58B"),
            "photo_2026-04-20_12-55-04 (2).jpg": ("abank", "2300-8317-6223-0167"),
            "photo_2026-04-20_12-55-04.jpg": ("monobank", "KPT2-0T15-39BM-HX28"),
            "photo_2026-04-20_12-58-12.jpg": ("privatbank", "P24A5738337141D5456"),
            "photo_2026-04-20_12-58-52.jpg": ("privatbank", "P24A5652931971D5515"),
        }
        providers = ProviderRegistry()
        base = Path("examples")

        for file_name, (exp_provider, exp_code) in expected.items():
            image_path = base / file_name
            text = extract_text(image_path)
            parsed = parse_receipt_text(text, providers)
            self.assertEqual(parsed.provider_code, exp_provider, msg=file_name)
            self.assertEqual(parsed.receipt_code, exp_code, msg=file_name)


if __name__ == "__main__":
    unittest.main()
