import unittest
from unittest.mock import patch

from app.binance import BinanceP2PClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class BinanceClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = BinanceP2PClient(api_key="key", secret_key="secret")

    @patch("app.binance.time.time", return_value=1000.0)
    def test_sign_query_includes_timestamp_and_signature(self, _mock_time) -> None:
        signed = self.client._sign_query({"orderNo": "123"})
        self.assertIn("orderNo=123", signed)
        self.assertIn("timestamp=1000000", signed)
        self.assertIn("signature=", signed)

    @patch("app.binance.requests.post")
    def test_get_active_orders_paginates(self, mock_post) -> None:
        mock_post.side_effect = [
            FakeResponse({"data": {"rows": [{"orderNumber": "1", "tradeType": "BUY", "amount": "2500"}]}}),
            FakeResponse({"data": {"rows": []}}),
        ]
        orders = self.client.get_active_orders(rows=1)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].order_number, "1")
        self.assertEqual(orders[0].trade_type, "BUY")

    @patch("app.binance.requests.get")
    def test_get_chat_messages_includes_text_and_image(self, mock_get) -> None:
        mock_get.side_effect = [
            FakeResponse(
                {
                    "data": {
                        "records": [
                            {"type": "text", "orderNo": "abc", "content": "x"},
                            {
                                "type": "image",
                                "orderNo": "abc",
                                "imageUrl": "https://example.com/1.jpg",
                                "createTime": 111,
                            },
                        ]
                    }
                }
            ),
            FakeResponse({"data": {"records": []}}),
        ]
        messages = self.client.get_chat_messages("abc", rows=2)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].message_type, "text")
        self.assertEqual(messages[1].message_type, "image")
        self.assertEqual(messages[1].image_url, "https://example.com/1.jpg")

    @patch("app.binance.requests.get")
    def test_get_orders_from_history_by_numbers(self, mock_get) -> None:
        mock_get.side_effect = [
            FakeResponse(
                {
                    "data": [
                        {"orderNumber": "1002", "tradeType": "BUY", "amount": "20", "createTime": 200},
                        {"orderNumber": "9999", "tradeType": "SELL", "amount": "10", "createTime": 100},
                    ]
                }
            ),
            FakeResponse(
                {
                    "data": [
                        {"orderNumber": "1001", "tradeType": "SELL", "amount": "30", "createTime": 300},
                    ]
                }
            ),
        ]
        orders = self.client.get_orders_from_history_by_numbers(["1001", "1002"], rows=2)
        self.assertEqual([order.order_number for order in orders], ["1001", "1002"])
        self.assertEqual([order.trade_type for order in orders], ["SELL", "BUY"])

    @patch("app.binance.requests.get")
    def test_get_orders_from_history_by_numbers_missing_returns_found_only(self, mock_get) -> None:
        mock_get.side_effect = [
            FakeResponse({"data": [{"orderNumber": "1002", "tradeType": "BUY", "amount": "20", "createTime": 200}]}),
            FakeResponse({"data": []}),
        ]
        orders = self.client.get_orders_from_history_by_numbers(["1001", "1002"], rows=1)
        self.assertEqual([order.order_number for order in orders], ["1002"])


if __name__ == "__main__":
    unittest.main()
