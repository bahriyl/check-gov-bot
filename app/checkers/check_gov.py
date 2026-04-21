from __future__ import annotations

import atexit
import re
from threading import Lock

from playwright.sync_api import sync_playwright

from app.types import CheckResult, CheckStatus


class CheckGovChecker:
    CHECK_URL = "https://check.gov.ua/api/handler"
    CHECK_PAGE = "https://check.gov.ua/"

    def __init__(self, headless: bool = True, timeout_seconds: int = 20) -> None:
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self._lock = Lock()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        atexit.register(self.close)

    def _ensure_session(self) -> None:
        if self._page:
            return

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context()

        # Block non-essential resources to speed up page warmup and runtime.
        self._context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "font", "media"}
            or any(
                blocked in route.request.url
                for blocked in (
                    "googletagmanager",
                    "google-analytics",
                    "doubleclick.net",
                    "facebook.net",
                    "tiktok.com",
                )
            )
            else route.continue_(),
        )

        self._page = self._context.new_page()
        # Warm up once; further checks reuse loaded page/session.
        self._page.goto(self.CHECK_PAGE, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
        self._page.wait_for_function(
            "() => !!(window.grecaptcha && window.grecaptcha.execute && window.conf)",
            timeout=self.timeout_seconds * 1000,
        )

    def _check_in_browser(self, provider_code: str, receipt_code: str) -> tuple[int, dict | None, str]:
        self._ensure_session()
        self._page.wait_for_function(
            "() => !!(window.grecaptcha && window.grecaptcha.execute && window.conf)",
            timeout=self.timeout_seconds * 1000,
        )
        result = self._page.evaluate(
            """
            async ({ providerCode, receiptCode }) => {
              const key = (window.conf && window.conf.recaptchaKey) || '6Lft1MYUAAAAAJQ51w5cBYGmLmkcuJ_EjoDYG8Y4';
              const token = await grecaptcha.execute(key, { action: 'homepage' });

              const payload = {
                c: 'check',
                company: providerCode,
                check: receiptCode,
                browser: (window.conf && window.conf.info)
                  ? window.conf.info
                  : { agent: navigator.userAgent, lang: (navigator.language || 'uk-UA') },
                recaptcha: token,
              };

              const response = await fetch('/api/handler', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
              });

              const text = await response.text();
              let json = null;
              try {
                json = JSON.parse(text);
              } catch (e) {}

              return {
                statusCode: response.status,
                json,
                text,
              };
            }
            """,
            {"providerCode": provider_code, "receiptCode": receipt_code},
        )
        return int(result["statusCode"]), result.get("json"), result.get("text", "")

    @staticmethod
    def _provider_candidates(provider_code: str) -> list[str]:
        code = (provider_code or "").strip().lower()
        if not code:
            return []
        variants = [code]
        if "-" in code:
            variants.append(code.replace("-", ""))
            variants.append(code.replace("-", "_"))
        if "_" in code:
            variants.append(code.replace("_", ""))
            variants.append(code.replace("_", "-"))
        if re.fullmatch(r"[a-z]+", code):
            variants.append(code[:1] + "-" + code[1:] if len(code) > 1 else code)
        # Preserve order, remove duplicates.
        out: list[str] = []
        for item in variants:
            if item and item not in out:
                out.append(item)
        return out

    @staticmethod
    def _is_retryable_einfo(data: dict | None) -> bool:
        if not isinstance(data, dict):
            return False
        info = str(data.get("eInfo") or "").lower()
        text = str(data.get("textUk") or "").lower()
        return "internal" in info or "error" in info or "помил" in text

    @staticmethod
    def _is_unsupported_company(data: dict | None) -> bool:
        if not isinstance(data, dict):
            return False
        info = str(data.get("eInfo") or "").lower()
        text = str(data.get("textUk") or "").lower()
        return "unsupported company" in info or "підприємств" in text

    def check(self, provider_code: str, receipt_code: str) -> CheckResult:
        last_result: tuple[int, dict | None, str] | None = None
        tries = 0
        provider_candidates = self._provider_candidates(provider_code) or [provider_code]
        # check.gov.ua can reject a parsed provider with "unsupported company".
        # In that case probe a small set of known provider codes before failing.
        provider_candidates.extend(
            [
                "monobank",
                "abank",
                "pumb",
                "easypay",
                "portmone",
                "uapay",
                "ibox",
                "govpay24",
                "opendatabot",
            ]
        )
        provider_candidates = [p for i, p in enumerate(provider_candidates) if p and p not in provider_candidates[:i]]

        for current_provider in provider_candidates:
            for _ in range(2):
                tries += 1
                try:
                    with self._lock:
                        status_code, data, raw_text = self._check_in_browser(current_provider, receipt_code)
                except Exception as exc:
                    # Force session reset on unexpected browser errors and retry once.
                    self.close()
                    if tries < max(2, len(provider_candidates) * 2):
                        continue
                    return CheckResult(
                        status=CheckStatus.CHECK_ERROR,
                        source="check.gov.ua",
                        message=f"Check.gov request failed: {exc}",
                    )

                last_result = (status_code, data, raw_text)
                if isinstance(data, dict) and data.get("payments"):
                    return CheckResult(
                        status=CheckStatus.VALID,
                        source="check.gov.ua",
                        message="Платіж знайдено",
                        details={**data, "http_status": status_code, "provider_code": current_provider},
                    )

                if self._is_retryable_einfo(data):
                    self.close()
                    continue
                if self._is_unsupported_company(data):
                    # Try next provider candidate.
                    break
                break

        if not last_result:
            return CheckResult(
                status=CheckStatus.CHECK_ERROR,
                source="check.gov.ua",
                message="Check.gov request failed: no response",
            )

        status_code, data, raw_text = last_result

        if status_code >= 400 and not isinstance(data, dict):
            return CheckResult(
                status=CheckStatus.CHECK_ERROR,
                source="check.gov.ua",
                message=f"HTTP {status_code}: {raw_text[:200] or 'Bad request'}",
                details={"http_status": status_code, "raw": raw_text[:1000]},
            )

        if isinstance(data, dict) and data.get("eInfo"):
            message = f"{data.get('textUk', 'Помилка')}: {data.get('eInfo')}"
            return CheckResult(
                status=CheckStatus.CHECK_ERROR,
                source="check.gov.ua",
                message=message,
                details={**data, "http_status": status_code},
            )

        return CheckResult(
            status=CheckStatus.NOT_FOUND,
            source="check.gov.ua",
            message="Запис не знайдено",
            details=(data if isinstance(data, dict) else {"raw": data, "raw_text": raw_text[:1000]})
            | {"http_status": status_code},
        )

    def close(self) -> None:
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
