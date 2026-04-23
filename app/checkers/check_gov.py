from __future__ import annotations

import atexit
import re
from threading import Lock
from typing import Any

from playwright.sync_api import sync_playwright

from app.payment_data import parse_check_gov_payment
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

    def _human_pause(self, ms: int) -> None:
        self._page.wait_for_timeout(ms)

    def _human_click(self, locator, *, force: bool = False) -> None:
        locator.wait_for(state="visible", timeout=self.timeout_seconds * 1000)
        try:
            locator.scroll_into_view_if_needed(timeout=self.timeout_seconds * 1000)
        except Exception:
            pass
        box = locator.bounding_box()
        if not box:
            locator.click(force=force, timeout=self.timeout_seconds * 1000)
            return
        target_x = box["x"] + box["width"] * 0.5
        target_y = box["y"] + box["height"] * 0.5
        approach_x = max(1, target_x - min(40, box["width"] * 0.4))
        approach_y = max(1, target_y - min(14, box["height"] * 0.3))
        self._page.mouse.move(approach_x, approach_y, steps=5)
        self._human_pause(25)
        self._page.mouse.move(target_x, target_y, steps=8)
        self._human_pause(35)
        self._page.mouse.down()
        self._human_pause(28)
        self._page.mouse.up()
        self._human_pause(45)

    def _resolve_provider_target(self, provider_code: str) -> dict[str, Any]:
        return self._page.evaluate(
            """
            ({ providerCode }) => {
              const normalize = (value) => String(value || '').trim().toLowerCase();
              const provider = normalize(providerCode);
              const company = document.getElementById('company');
              const companyBlock = document.getElementById('companyBlock');
              const parseAliases = (raw) => {
                try {
                  const parsed = JSON.parse(raw || '[]');
                  return Array.isArray(parsed) ? parsed.map((x) => normalize(x)) : [];
                } catch (_err) {
                  return [];
                }
              };

              let selectedValue = providerCode;
              let selectedText = '';
              let selectedAliases = [];
              if (company) {
                const options = Array.from(company.options || []);
                const direct = options.find((opt) => normalize(opt.value) === provider);
                const byAlias = options.find((opt) => parseAliases(opt.getAttribute('alt')).includes(provider));
                const byText = options.find((opt) => normalize(opt.textContent).includes(provider));
                const matched = direct || byAlias || byText;
                if (matched) {
                  selectedValue = matched.value;
                  selectedText = (matched.textContent || '').trim();
                  selectedAliases = parseAliases(matched.getAttribute('alt'));
                }
              }

              let targetIndex = -1;
              const list = companyBlock ? companyBlock.querySelector('.selection-list') : null;
              if (list) {
                const items = Array.from(list.querySelectorAll('div'));
                const byExactText = items.findIndex((item) => normalize(item.textContent) === normalize(selectedText));
                const byProviderAlias = items.findIndex((item) => parseAliases(item.getAttribute('alt')).includes(provider));
                const bySelectedAlias = items.findIndex((item) =>
                  parseAliases(item.getAttribute('alt')).some((alias) => selectedAliases.includes(alias))
                );
                const byProviderText = items.findIndex((item) => normalize(item.textContent).includes(provider));
                targetIndex = [byExactText, byProviderAlias, bySelectedAlias, byProviderText].find((idx) => idx >= 0) ?? -1;
              }

              return { selectedValue, targetIndex };
            }
            """,
            {"providerCode": provider_code},
        )

    def _prepare_form(self, provider_code: str, receipt_code: str) -> None:
        target = self._resolve_provider_target(provider_code)
        selected_value = str(target.get("selectedValue") or provider_code or "")
        raw_target_index = target.get("targetIndex")
        target_index = int(raw_target_index) if raw_target_index is not None else -1

        self._page.evaluate(
            """
            ({ selectedValue }) => {
              const company = document.getElementById('company');
              if (company) {
                company.value = selectedValue;
                company.dispatchEvent(new Event('input', { bubbles: true }));
                company.dispatchEvent(new Event('change', { bubbles: true }));
              }
            }
            """,
            {"selectedValue": selected_value},
        )

        opener = self._page.locator("xpath=//*[@id='companyBlock']/div/div[1]")
        self._human_click(opener)
        self._human_pause(65)

        if target_index >= 0:
            provider_item = self._page.locator("#companyBlock .selection-list > div").nth(target_index)
            self._human_click(provider_item)
        self._human_pause(80)

        refs = self._page.locator("#references")
        self._human_click(refs)
        refs.press("ControlOrMeta+A")
        self._human_pause(20)
        refs.press("Backspace")
        self._human_pause(28)
        refs.type(receipt_code, delay=52)
        self._human_pause(42)

        self._page.evaluate(
            """
            () => {
              const refs = document.getElementById('references');
              if (!refs) {
                throw new Error('check.gov form fields are not available');
              }
              refs.dispatchEvent(new Event('change', { bubbles: true }));
              refs.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: '0' }));
            }
            """,
        )
        self._human_pause(75)

    def _submit_form_and_capture(self) -> tuple[int, dict | None, str]:
        response = None
        submit_xpath = "//div[@id='submit']"
        submit = self._page.locator(f"xpath={submit_xpath}")
        submit.wait_for(state="visible", timeout=self.timeout_seconds * 1000)
        self._page.wait_for_function(
            """
            () => {
              const node = document.getElementById('submit');
              if (!node) return false;
              const style = window.getComputedStyle(node);
              const className = String(node.className || '').toLowerCase();
              const visible = style.display !== 'none'
                && style.visibility !== 'hidden'
                && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
              const clickableByStyle = style.pointerEvents !== 'none';
              const notDisabledClass = !className.includes('disabled');
              return visible && clickableByStyle && notDisabledClass;
            }
            """,
            timeout=self.timeout_seconds * 1000,
        )
        with self._page.expect_response(
            lambda r: "/api/handler" in r.url and r.request.method == "POST",
            timeout=self.timeout_seconds * 1000,
        ) as info:
            # Custom layout overlays pointer targets; fall back to force click if needed.
            try:
                self._human_click(submit)
            except Exception:
                submit.click(force=True, timeout=self.timeout_seconds * 1000)
        response = info.value

        text = response.text() or ""
        data: dict | None = None
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = None

        return int(response.status), data, text

    def _read_ui_result(self) -> dict[str, Any]:
        return self._page.evaluate(
            """
            () => {
              const read = (id) => {
                const node = document.getElementById(id);
                if (!node) return { text: '', visible: false };
                const style = window.getComputedStyle(node);
                const text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
                const visible = style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
                return { text, visible };
              };

              const checkResult = read('checkResult');
              const resultFlag = read('resultFlag');
              const hint = read('hint');
              const submitNode = document.getElementById('submit');
              const submitClass = submitNode ? (submitNode.className || '') : '';
              const fileNode = document.getElementById('resultFile');
              const fileHref = fileNode ? (fileNode.getAttribute('href') || '') : '';
              return {
                check_result_text: checkResult.text,
                check_result_visible: checkResult.visible,
                result_flag_text: resultFlag.text,
                result_flag_visible: resultFlag.visible,
                hint_text: hint.text,
                hint_visible: hint.visible,
                submit_class: submitClass,
                result_file_href: fileHref,
              };
            }
            """
        )

    def _check_in_browser(self, provider_code: str, receipt_code: str) -> tuple[int, dict | None, str]:
        self._ensure_session()
        self._page.wait_for_function(
            "() => !!(window.grecaptcha && window.grecaptcha.execute && window.conf)",
            timeout=self.timeout_seconds * 1000,
        )
        self._page.wait_for_function(
            """
            () => {
              const select = document.getElementById('company');
              const optionsReady = !!(select && select.options && select.options.length > 1);
              const listReady = document.querySelectorAll('#companyBlock .selection-list div').length > 0;
              return optionsReady || listReady;
            }
            """,
            timeout=self.timeout_seconds * 1000,
        )
        self._prepare_form(provider_code, receipt_code)
        status_code, data, raw_text = self._submit_form_and_capture()
        ui = self._read_ui_result()

        out = dict(data) if isinstance(data, dict) else {}
        out["ui"] = ui
        return status_code, out, raw_text

    def _reload_page(self) -> None:
        self._ensure_session()
        self._page.goto(self.CHECK_PAGE, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
        self._page.wait_for_function(
            "() => !!(window.grecaptcha && window.grecaptcha.execute && window.conf)",
            timeout=self.timeout_seconds * 1000,
        )
        self._page.wait_for_function(
            """
            () => {
              const select = document.getElementById('company');
              const optionsReady = !!(select && select.options && select.options.length > 1);
              const listReady = document.querySelectorAll('#companyBlock .selection-list div').length > 0;
              return optionsReady || listReady;
            }
            """,
            timeout=self.timeout_seconds * 1000,
        )

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

    def check(self, provider_code: str, receipt_code: str, reload_before_check: bool = False) -> CheckResult:
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
                        if reload_before_check:
                            self._reload_page()
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
                if isinstance(data, dict):
                    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
                    flag_text = str(ui.get("result_flag_text") or "").lower()
                    result_text = str(ui.get("check_result_text") or "").lower()
                    hint_text = str(ui.get("hint_text") or "").lower()
                    if any(token in flag_text for token in ("оплачен", "успіш")):
                        return CheckResult(
                            status=CheckStatus.VALID,
                            source="check.gov.ua",
                            message=str(ui.get("check_result_text") or "Платіж знайдено"),
                            details={**data, "http_status": status_code, "provider_code": current_provider},
                        )
                    if "не знайден" in flag_text or "не знайден" in result_text or "не знайден" in hint_text:
                        return CheckResult(
                            status=CheckStatus.NOT_FOUND,
                            source="check.gov.ua",
                            message=str(ui.get("check_result_text") or ui.get("hint_text") or "Запис не знайдено"),
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
