import os
import unittest
from unittest.mock import patch

from app.config import load_settings


class ConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {"BOT_TOKEN": "tkn"}, clear=True)
    def test_defaults_use_paddle(self) -> None:
        settings = load_settings()
        self.assertEqual(settings.ocr_provider, "paddle")
        self.assertEqual(settings.docai_timeout_seconds, 30)
        self.assertFalse(settings.binance_test_include_latest_non_active)
        self.assertEqual(settings.binance_test_latest_non_active_count, 1)

    @patch.dict(
        os.environ,
        {"BOT_TOKEN": "tkn", "BINANCE_TEST_INCLUDE_LATEST_NON_ACTIVE": "true"},
        clear=True,
    )
    def test_binance_test_fallback_flag_loads(self) -> None:
        settings = load_settings()
        self.assertTrue(settings.binance_test_include_latest_non_active)

    @patch.dict(
        os.environ,
        {"BOT_TOKEN": "tkn", "BINANCE_TEST_LATEST_NON_ACTIVE_COUNT": "3"},
        clear=True,
    )
    def test_binance_test_fallback_count_loads(self) -> None:
        settings = load_settings()
        self.assertEqual(settings.binance_test_latest_non_active_count, 3)

    @patch.dict(os.environ, {"BOT_TOKEN": "tkn", "OCR_PROVIDER": "bad"}, clear=True)
    def test_invalid_provider_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            load_settings()

    @patch.dict(os.environ, {"BOT_TOKEN": "tkn", "OCR_PROVIDER": "docai"}, clear=True)
    def test_docai_missing_env_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            load_settings()

    @patch.dict(
        os.environ,
        {
            "BOT_TOKEN": "tkn",
            "OCR_PROVIDER": "docai",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/key.json",
            "DOCAI_PROJECT_ID": "proj",
            "DOCAI_LOCATION": "us",
            "DOCAI_PROCESSOR_ID": "proc",
            "DOCAI_TIMEOUT_SECONDS": "45",
        },
        clear=True,
    )
    def test_docai_config_loads(self) -> None:
        settings = load_settings()
        self.assertEqual(settings.ocr_provider, "docai")
        self.assertEqual(settings.docai_timeout_seconds, 45)
        self.assertEqual(settings.docai_location, "us")


if __name__ == "__main__":
    unittest.main()
