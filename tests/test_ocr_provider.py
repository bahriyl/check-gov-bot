import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app import ocr


class OcrProviderRoutingTests(unittest.TestCase):
    @patch("app.ocr._extract_text_paddle", return_value="paddle-text")
    def test_routes_to_paddle(self, mock_paddle) -> None:
        with patch.dict(os.environ, {"OCR_PROVIDER": "paddle"}, clear=False):
            text = ocr.extract_text(Path("dummy.jpg"))
        self.assertEqual(text, "paddle-text")
        mock_paddle.assert_called_once()

    @patch("app.ocr._extract_text_docai", return_value="docai-text")
    def test_routes_to_docai(self, mock_docai) -> None:
        with patch.dict(os.environ, {"OCR_PROVIDER": "docai"}, clear=False):
            text = ocr.extract_text(Path("dummy.jpg"))
        self.assertEqual(text, "docai-text")
        mock_docai.assert_called_once()


if __name__ == "__main__":
    unittest.main()
