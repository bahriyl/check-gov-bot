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
        self.assertEqual(settings.binance_test_non_active_order_numbers, [])
        self.assertEqual(settings.bot_handler_workers, 8)
        self.assertEqual(settings.checkgov_global_parallel_limit, 8)
        self.assertEqual(settings.checkgov_per_user_parallel_limit, 2)

    @patch.dict(
        os.environ,
        {"BOT_TOKEN": "tkn", "BINANCE_TEST_NON_ACTIVE_ORDER_NUMBERS": "1001, 1002,1001,,1003"},
        clear=True,
    )
    def test_binance_test_non_active_order_numbers_load(self) -> None:
        settings = load_settings()
        self.assertEqual(settings.binance_test_non_active_order_numbers, ["1001", "1002", "1003"])

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

    @patch.dict(os.environ, {"BOT_TOKEN": "tkn", "BOT_HANDLER_WORKERS": "0"}, clear=True)
    def test_invalid_bot_handler_workers_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            load_settings()

    @patch.dict(os.environ, {"BOT_TOKEN": "tkn", "CHECKGOV_GLOBAL_PARALLEL_LIMIT": "0"}, clear=True)
    def test_invalid_global_parallel_limit_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            load_settings()


if __name__ == "__main__":
    unittest.main()
