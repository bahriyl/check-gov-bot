from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from playwright.sync_api import sync_playwright


@dataclass
class Provider:
    code: str
    name: str
    aliases: set[str] = field(default_factory=set)


class ProviderRegistry:
    def __init__(self, headless: bool = True, refresh_hours: int = 6) -> None:
        self._headless = headless
        self._refresh_hours = refresh_hours
        self._providers: dict[str, Provider] = {}
        self._last_refresh: datetime | None = None
        self._seed_defaults()

    @property
    def providers(self) -> dict[str, Provider]:
        return self._providers

    def _seed_defaults(self) -> None:
        defaults = {
            "abank": "А-Банк",
            "altabank": "АльтБанк",
            "vostok": "VST | Банк Власний Рахунок",
            "gerz": "ГЕРЦ",
            "diia": "ДІЯ",
            "monobank": "Монобанк",
            "mtb": "МТБ БАНК",
            "privatbank": "Приватбанк",
            "pumb": "ПУМБ",
            "sensbank": "СЕНС БАНК",
            "opendatabot": "Опендатабот",
            "zss": "Інтерпейсервіс",
            "forwardbank": "Форвард банк",
            "shtrafua": "Штрафы UA",
            "easypay": "EasyPay",
            "govpay24": "Govpay24",
            "ibox": "IBox",
            "luckypay": "LuckyPay.Online",
            "portmone": "portmone.com",
            "uapay": "UaPay",
        }
        self._set_providers(defaults.items())

    def _set_providers(self, items: Iterable[tuple[str, str]]) -> None:
        providers: dict[str, Provider] = {}
        for code, name in items:
            norm_name = self._normalize_text(name)
            aliases = {norm_name, norm_name.replace("-", " "), code.lower()}
            if code == "privatbank":
                aliases.update({"приват", "приватбанк", "privat"})
            if code == "monobank":
                aliases.update({"mono", "monobank", "універсал банк"})
            if code == "abank":
                aliases.update({"а банк", "a bank", "a-bank", "а-банк"})
            providers[code] = Provider(code=code, name=name, aliases=aliases)
        self._providers = providers

    @staticmethod
    def _normalize_text(text: str) -> str:
        # Normalize mixed Cyrillic/Latin lookalikes common for OCR output.
        mapping = str.maketrans(
            {
                "А": "A",
                "В": "B",
                "С": "C",
                "Е": "E",
                "Н": "H",
                "І": "I",
                "К": "K",
                "М": "M",
                "О": "O",
                "Р": "P",
                "Т": "T",
                "Х": "X",
                "У": "Y",
                "а": "a",
                "в": "b",
                "с": "c",
                "е": "e",
                "і": "i",
                "к": "k",
                "м": "m",
                "о": "o",
                "р": "p",
                "т": "t",
                "х": "x",
                "у": "y",
                "ђ": "d",
                "ј": "i",
                "6": "b",
            }
        )
        return " ".join(text.translate(mapping).lower().replace("-", " ").split())

    def maybe_refresh(self) -> None:
        now = datetime.now(tz=timezone.utc)
        if self._last_refresh and now - self._last_refresh < timedelta(hours=self._refresh_hours):
            return
        self.refresh_from_check_gov()

    def refresh_from_check_gov(self) -> None:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self._headless)
                page = browser.new_page()
                page.goto("https://check.gov.ua/", wait_until="networkidle", timeout=30000)
                options = page.evaluate(
                    """
                    () => {
                      const select = document.querySelector('#company');
                      if (!select) return [];
                      return Array.from(select.options)
                        .filter(o => o.value && o.value !== '0')
                        .map(o => [o.value, (o.textContent || '').trim()]);
                    }
                    """
                )
                browser.close()
            if options:
                self._set_providers(options)
                self._last_refresh = datetime.now(tz=timezone.utc)
        except Exception:
            # Keep defaults; runtime checking continues working.
            self._last_refresh = datetime.now(tz=timezone.utc)

    def find_provider_by_text(self, text: str) -> Provider | None:
        norm = self._normalize_text(text)
        for provider in self._providers.values():
            if any(alias and alias in norm for alias in provider.aliases):
                return provider
        return None
