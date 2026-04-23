from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests


class BinanceAPIError(RuntimeError):
    pass


@dataclass
class BinanceOrder:
    order_number: str
    trade_type: str
    total_amount: str
    raw: dict


@dataclass
class BinanceChatMessage:
    order_number: str
    message_type: str
    content: str
    image_url: str
    message_time: int
    raw: dict


class BinanceP2PClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str = "https://api.binance.com",
        timeout_seconds: int = 20,
    ) -> None:
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

        if not self.api_key or not self.secret_key:
            raise BinanceAPIError("BINANCE_API_KEY/BINANCE_SECRET_KEY are required")

    def _sign_query(self, params: dict) -> str:
        payload = {**params, "timestamp": int(time.time() * 1000)}
        query = urlencode(payload)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _headers(self) -> dict[str, str]:
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json",
            "clientType": "web",
        }

    @staticmethod
    def _order_total_amount(order: dict) -> str:
        for key in ("amount", "totalPrice", "fiatAmount", "totalAmount", "price"):
            value = order.get(key)
            if value not in (None, ""):
                return str(value)
        return "0"

    @staticmethod
    def _extract_rows(payload: dict) -> list[dict]:
        data = payload.get("data")
        if isinstance(data, dict):
            return data.get("rows", []) or data.get("list", []) or data.get("records", []) or []
        if isinstance(data, list):
            return data
        return payload.get("rows", []) or []

    @staticmethod
    def _map_orders(all_orders: list[dict]) -> list[BinanceOrder]:
        mapped: list[BinanceOrder] = []
        for order in all_orders:
            order_number = str(order.get("orderNumber") or order.get("orderNo") or "").strip()
            if not order_number:
                continue
            trade_type = str(order.get("tradeType") or order.get("side") or "BUY").upper()
            mapped.append(
                BinanceOrder(
                    order_number=order_number,
                    trade_type=trade_type,
                    total_amount=BinanceP2PClient._order_total_amount(order),
                    raw=order,
                )
            )
        mapped.sort(key=lambda o: int(o.raw.get("createTime", 0)), reverse=True)
        return mapped

    def get_active_orders(self, rows: int = 100) -> list[BinanceOrder]:
        endpoint = f"{self.base_url}/sapi/v1/c2c/orderMatch/listOrders"
        page = 1
        max_pages = 1000
        all_orders: list[dict] = []

        while page <= max_pages:
            query = self._sign_query({})
            body = {
                "page": page,
                "rows": rows,
                "orderStatusList": [1, 2, 3],
            }
            resp = requests.post(
                endpoint,
                params=query,
                json=body,
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
            try:
                payload = resp.json()
            except Exception as exc:  # pragma: no cover
                raise BinanceAPIError(f"Failed to decode Binance orders response: {exc}") from exc

            if resp.status_code >= 400:
                raise BinanceAPIError(f"Binance orders error HTTP {resp.status_code}: {payload}")

            items = self._extract_rows(payload)
            if not items:
                break
            all_orders.extend(items)
            page += 1
        else:  # pragma: no cover
            raise BinanceAPIError("Exceeded max pages while loading Binance active orders")

        return self._map_orders(all_orders)

    def get_orders_from_history_by_numbers(self, order_numbers: list[str], rows: int = 100) -> list[BinanceOrder]:
        requested = list(dict.fromkeys([num.strip() for num in order_numbers if num and num.strip()]))
        if not requested:
            return []

        endpoint = f"{self.base_url}/sapi/v1/c2c/orderMatch/listUserOrderHistory"
        page = 1
        max_pages = 1000
        requested_set = set(requested)
        found: dict[str, BinanceOrder] = {}

        while page <= max_pages:
            query = self._sign_query({"page": page, "rows": rows})
            resp = requests.get(
                endpoint,
                params=query,
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
            try:
                payload = resp.json()
            except Exception as exc:  # pragma: no cover
                raise BinanceAPIError(f"Failed to decode Binance order history response: {exc}") from exc

            if resp.status_code >= 400:
                raise BinanceAPIError(f"Binance history error HTTP {resp.status_code}: {payload}")

            items = self._extract_rows(payload)
            if not items:
                break

            for order in self._map_orders(items):
                if order.order_number in requested_set and order.order_number not in found:
                    found[order.order_number] = order

            if len(found) == len(requested_set):
                break
            page += 1
        else:  # pragma: no cover
            raise BinanceAPIError("Exceeded max pages while loading Binance order history")

        return [found[num] for num in requested if num in found]

    def get_chat_messages(self, order_number: str, rows: int = 100, max_pages: int = 100) -> list[BinanceChatMessage]:
        endpoint = f"{self.base_url}/sapi/v1/c2c/chat/retrieveChatMessagesWithPagination"
        page = 1
        out: list[BinanceChatMessage] = []
        seen_signatures: set[tuple[str, int, str, str]] = set()
        consecutive_pages_without_new = 0

        while page <= max_pages:
            query = self._sign_query(
                {
                    "orderNo": order_number,
                    "page": page,
                    "rows": rows,
                }
            )
            resp = requests.get(
                endpoint,
                params=query,
                headers=self._headers(),
                timeout=self.timeout_seconds,
            )
            try:
                payload = resp.json()
            except Exception as exc:  # pragma: no cover
                raise BinanceAPIError(f"Failed to decode Binance chat response: {exc}") from exc

            if resp.status_code >= 400:
                raise BinanceAPIError(f"Binance chat error HTTP {resp.status_code}: {payload}")

            messages = self._extract_rows(payload)
            if not messages:
                break

            page_added = 0
            for msg in messages:
                msg_type = str(msg.get("type", "")).lower()
                if msg_type not in {"image", "text", "system", "auto_reply"}:
                    continue
                message_order_no = str(msg.get("orderNo") or order_number)
                image_url = str(msg.get("imageUrl") or "").strip()
                content = str(msg.get("content") or msg.get("autoReplyMsg") or "").strip()
                message_time = int(msg.get("createTime") or 0)
                signature = (msg_type, message_time, image_url, content)
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                out.append(
                    BinanceChatMessage(
                        order_number=message_order_no,
                        message_type=msg_type,
                        content=content,
                        image_url=image_url,
                        message_time=message_time,
                        raw=msg,
                    )
                )
                page_added += 1

            if page_added == 0:
                consecutive_pages_without_new += 1
                if consecutive_pages_without_new >= 3:
                    break
            else:
                consecutive_pages_without_new = 0

            page += 1
        else:  # pragma: no cover
            raise BinanceAPIError("Exceeded max pages while loading Binance chat messages")

        out.sort(key=lambda item: item.message_time)
        return out
