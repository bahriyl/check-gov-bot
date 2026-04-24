from __future__ import annotations

import base64
import json
import re
from typing import Any

import requests

from app.payment_data import parse_check_gov_payment
from app.types import CheckResult, CheckStatus


class CheckGovChecker:
    CHECK_URL = "https://check.gov.ua/api/handler"
    RECAPTCHA_API_JS = "https://www.google.com/recaptcha/api.js"
    RECAPTCHA_ANCHOR = "https://www.google.com/recaptcha/api2/anchor"
    RECAPTCHA_RELOAD = "https://www.google.com/recaptcha/api2/reload"
    RECAPTCHA_SITE_KEY = "6Lft1MYUAAAAAJQ51w5cBYGmLmkcuJ_EjoDYG8Y4"
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, timeout_seconds: int = 20, max_total_attempts: int = 2) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_total_attempts = max(1, int(max_total_attempts))

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": self.USER_AGENT})
        return session

    def _co_param(self) -> str:
        return base64.b64encode(b"https://check.gov.ua:443").decode("ascii")

    def _recaptcha_version(self, session: requests.Session) -> str:
        resp = session.get(
            self.RECAPTCHA_API_JS,
            params={"render": self.RECAPTCHA_SITE_KEY},
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        match = re.search(r"/releases/([^/]+)/", resp.text)
        if not match:
            raise RuntimeError("Cannot parse reCAPTCHA version")
        return match.group(1)

    def _recaptcha_token(self, session: requests.Session, version: str) -> str:
        co_param = self._co_param()
        anchor = session.get(
            self.RECAPTCHA_ANCHOR,
            params={
                "ar": "1",
                "k": self.RECAPTCHA_SITE_KEY,
                "co": co_param,
                "hl": "uk",
                "v": version,
                "size": "invisible",
                "cb": "checkgov",
            },
            timeout=self.timeout_seconds,
        )
        anchor.raise_for_status()
        token_match = re.search(r'id="recaptcha-token" value="([^"]+)"', anchor.text)
        if not token_match:
            raise RuntimeError("Cannot parse reCAPTCHA anchor token")
        anchor_token = token_match.group(1)

        reload = session.post(
            self.RECAPTCHA_RELOAD,
            params={"k": self.RECAPTCHA_SITE_KEY},
            data={
                "v": version,
                "reason": "q",
                "k": self.RECAPTCHA_SITE_KEY,
                "c": anchor_token,
                "sa": "homepage",
                "co": co_param,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout_seconds,
        )
        reload.raise_for_status()
        resp_match = re.search(r'\["rresp","([^"]+)"', reload.text)
        if not resp_match:
            raise RuntimeError("Cannot parse reCAPTCHA response token")
        return resp_match.group(1)

    def _check_once(self, provider_code: str, receipt_code: str) -> tuple[int, dict[str, Any] | None, str]:
        session = self._new_session()
        version = self._recaptcha_version(session)
        recaptcha = self._recaptcha_token(session, version)
        payload = {
            "c": "check",
            "company": provider_code,
            "check": receipt_code,
            "browser": {
                "name": "chrome",
                "version": "124.0.0.0",
                "platform": "desktop",
                "os": "osx",
                "osVer": "10.15.7",
                "language": "uk",
                "adblockState": False,
            },
            "recaptcha": recaptcha,
        }

        resp = session.post(
            self.CHECK_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Origin": "https://check.gov.ua",
                "Referer": "https://check.gov.ua/",
                "Accept": "application/json, text/plain, */*",
            },
            timeout=self.timeout_seconds,
        )
        raw_text = resp.text or ""
        data: dict[str, Any] | None = None
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = None
        return int(resp.status_code), data, raw_text

    @staticmethod
    def _is_retryable_einfo(data: dict[str, Any] | None) -> bool:
        if not isinstance(data, dict):
            return False
        info = str(data.get("eInfo") or "").lower()
        text = str(data.get("textUk") or "").lower()
        return "internal" in info or "error" in info or "помил" in text

    def check(
        self,
        provider_code: str,
        receipt_code: str,
        reload_before_check: bool = False,
        user_scope: str | None = None,
    ) -> CheckResult:
        del reload_before_check
        del user_scope

        current_provider = (provider_code or "").strip().lower()
        if not current_provider:
            return CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="check.gov.ua",
                message="Не вказано банк/сервіс для перевірки",
            )

        last_result: tuple[int, dict[str, Any] | None, str] | None = None

        for attempt in range(1, self.max_total_attempts + 1):
            try:
                status_code, data, raw_text = self._check_once(current_provider, receipt_code)
            except Exception as exc:
                if attempt < self.max_total_attempts:
                    continue
                return CheckResult(
                    status=CheckStatus.CHECK_ERROR,
                    source="check.gov.ua",
                    message=f"Помилка перевірки check.gov.ua: нестабільний запит ({exc})",
                )

            last_result = (status_code, data, raw_text)

            if isinstance(data, dict) and data.get("payments"):
                payment = parse_check_gov_payment(data)
                return CheckResult(
                    status=CheckStatus.VALID,
                    source="check.gov.ua",
                    message="Платіж знайдено",
                    details={
                        **data,
                        "http_status": status_code,
                        "provider_code": current_provider,
                        "payment": payment,
                    },
                )

            if isinstance(data, dict) and data.get("e") == 404:
                return CheckResult(
                    status=CheckStatus.NOT_FOUND,
                    source="check.gov.ua",
                    message=str(data.get("textUk") or "Запис не знайдено"),
                    details={**data, "http_status": status_code, "provider_code": current_provider},
                )

            if self._is_retryable_einfo(data):
                if attempt < self.max_total_attempts:
                    continue
                return CheckResult(
                    status=CheckStatus.CHECK_ERROR,
                    source="check.gov.ua",
                    message="Помилка перевірки check.gov.ua: нестабільна або застаріла відповідь сервісу",
                    details={
                        "http_status": status_code,
                        "provider_code": current_provider,
                        "payload": data if isinstance(data, dict) else {"raw_text": raw_text[:1000]},
                    },
                )

            if isinstance(data, dict) and data.get("eInfo"):
                return CheckResult(
                    status=CheckStatus.CHECK_ERROR,
                    source="check.gov.ua",
                    message=f"{data.get('textUk', 'Помилка')}: {data.get('eInfo')}",
                    details={**data, "http_status": status_code, "provider_code": current_provider},
                )

            if status_code >= 400 and not isinstance(data, dict):
                return CheckResult(
                    status=CheckStatus.CHECK_ERROR,
                    source="check.gov.ua",
                    message=f"HTTP {status_code}: {raw_text[:200] or 'Bad request'}",
                    details={"http_status": status_code, "raw": raw_text[:1000]},
                )

            return CheckResult(
                status=CheckStatus.CHECK_ERROR,
                source="check.gov.ua",
                message="Помилка перевірки check.gov.ua: невизначена або застаріла відповідь",
                details=(data if isinstance(data, dict) else {"raw_text": raw_text[:1000]})
                | {"http_status": status_code, "provider_code": current_provider},
            )

        if not last_result:
            return CheckResult(
                status=CheckStatus.CHECK_ERROR,
                source="check.gov.ua",
                message="Помилка перевірки check.gov.ua: немає відповіді від сервісу",
            )

        status_code, data, raw_text = last_result
        return CheckResult(
            status=CheckStatus.CHECK_ERROR,
            source="check.gov.ua",
            message="Помилка перевірки check.gov.ua: невизначена або застаріла відповідь",
            details=(data if isinstance(data, dict) else {"raw_text": raw_text[:1000]})
            | {"http_status": status_code},
        )

    def close(self) -> None:
        return

    def shutdown(self) -> None:
        return
