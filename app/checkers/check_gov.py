from __future__ import annotations

import asyncio
import atexit
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.async_runner import AsyncLoopRunner
from app.payment_data import parse_check_gov_payment
from app.types import CheckResult, CheckStatus


class CheckGovChecker:
    CHECK_URL = "https://check.gov.ua/api/handler"
    CHECK_PAGE = "https://check.gov.ua/"

    def __init__(
        self,
        headless: bool = True,
        timeout_seconds: int = 20,
        global_parallel_limit: int = 8,
        per_user_parallel_limit: int = 2,
    ) -> None:
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self.global_parallel_limit = max(1, int(global_parallel_limit))
        self.per_user_parallel_limit = max(1, int(per_user_parallel_limit))

        self._runner = AsyncLoopRunner("checkgov-playwright-loop")
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._session_lock: asyncio.Lock | None = None
        self._global_limiter: asyncio.Semaphore | None = None
        self._user_limiters: dict[str, asyncio.Semaphore] = {}
        atexit.register(self.shutdown)

    def _timeout_ms(self) -> int:
        return self.timeout_seconds * 1000

    async def _ensure_async_state(self) -> None:
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        if self._global_limiter is None:
            self._global_limiter = asyncio.Semaphore(self.global_parallel_limit)

    async def _ensure_session(self) -> None:
        await self._ensure_async_state()
        assert self._session_lock is not None
        async with self._session_lock:
            if self._browser:
                return
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)

    async def _create_context(self) -> BrowserContext:
        await self._ensure_session()
        assert self._browser is not None
        context = await self._browser.new_context()

        async def _route_handler(route) -> None:
            req = route.request
            if req.resource_type in {"image", "font", "media"} or any(
                blocked in req.url
                for blocked in (
                    "googletagmanager",
                    "google-analytics",
                    "doubleclick.net",
                    "facebook.net",
                    "tiktok.com",
                )
            ):
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", _route_handler)
        return context

    async def _human_pause(self, page: Page, ms: int) -> None:
        await page.wait_for_timeout(ms)

    async def _human_click(self, page: Page, locator, *, force: bool = False) -> None:
        await locator.wait_for(state="visible", timeout=self._timeout_ms())
        try:
            await locator.scroll_into_view_if_needed(timeout=self._timeout_ms())
        except Exception:
            pass
        box = await locator.bounding_box()
        if not box:
            await locator.click(force=force, timeout=self._timeout_ms())
            return
        target_x = box["x"] + box["width"] * 0.5
        target_y = box["y"] + box["height"] * 0.5
        approach_x = max(1, target_x - min(40, box["width"] * 0.4))
        approach_y = max(1, target_y - min(14, box["height"] * 0.3))
        await page.mouse.move(approach_x, approach_y, steps=5)
        await self._human_pause(page, 25)
        await page.mouse.move(target_x, target_y, steps=8)
        await self._human_pause(page, 35)
        await page.mouse.down()
        await self._human_pause(page, 28)
        await page.mouse.up()
        await self._human_pause(page, 45)

    async def _resolve_provider_target(self, page: Page, provider_code: str) -> dict[str, Any]:
        return await page.evaluate(
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

    async def _prepare_form(self, page: Page, provider_code: str, receipt_code: str) -> None:
        target = await self._resolve_provider_target(page, provider_code)
        selected_value = str(target.get("selectedValue") or provider_code or "")
        raw_target_index = target.get("targetIndex")
        target_index = int(raw_target_index) if raw_target_index is not None else -1

        await page.evaluate(
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

        opener = page.locator("xpath=//*[@id='companyBlock']/div/div[1]")
        await self._human_click(page, opener)
        await self._human_pause(page, 65)

        if target_index >= 0:
            provider_item = page.locator("#companyBlock .selection-list > div").nth(target_index)
            await self._human_click(page, provider_item)
        await self._human_pause(page, 80)

        refs = page.locator("#references")
        await self._human_click(page, refs)
        await refs.press("ControlOrMeta+A")
        await self._human_pause(page, 20)
        await refs.press("Backspace")
        await self._human_pause(page, 28)
        await refs.type(receipt_code, delay=52)
        await self._human_pause(page, 42)

        await page.evaluate(
            """
            () => {
              const refs = document.getElementById('references');
              if (!refs) {
                throw new Error('check.gov form fields are not available');
              }
              refs.dispatchEvent(new Event('change', { bubbles: true }));
              refs.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: '0' }));
            }
            """
        )
        await self._human_pause(page, 75)

    async def _submit_form_and_capture(self, page: Page) -> tuple[int, dict | None, str]:
        submit = page.locator("xpath=//div[@id='submit']")
        await submit.wait_for(state="visible", timeout=self._timeout_ms())
        await page.wait_for_function(
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
            timeout=self._timeout_ms(),
        )
        async with page.expect_response(
            lambda r: "/api/handler" in r.url and r.request.method == "POST",
            timeout=self._timeout_ms(),
        ) as info:
            try:
                await self._human_click(page, submit)
            except Exception:
                await submit.click(force=True, timeout=self._timeout_ms())
        response = await info.value

        text = await response.text() or ""
        data: dict | None = None
        try:
            parsed = await response.json()
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = None

        return int(response.status), data, text

    async def _read_ui_result(self, page: Page) -> dict[str, Any]:
        return await page.evaluate(
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

    async def _check_in_browser_async(self, provider_code: str, receipt_code: str) -> tuple[int, dict | None, str]:
        context = await self._create_context()
        page = await context.new_page()
        try:
            await page.goto(self.CHECK_PAGE, wait_until="domcontentloaded", timeout=self._timeout_ms())
            await page.wait_for_function(
                "() => !!(window.grecaptcha && window.grecaptcha.execute && window.conf)",
                timeout=self._timeout_ms(),
            )
            await page.wait_for_function(
                """
                () => {
                  const select = document.getElementById('company');
                  const optionsReady = !!(select && select.options && select.options.length > 1);
                  const listReady = document.querySelectorAll('#companyBlock .selection-list div').length > 0;
                  return optionsReady || listReady;
                }
                """,
                timeout=self._timeout_ms(),
            )
            await self._prepare_form(page, provider_code, receipt_code)
            status_code, data, raw_text = await self._submit_form_and_capture(page)
            ui = await self._read_ui_result(page)
            out = dict(data) if isinstance(data, dict) else {}
            out["ui"] = ui
            return status_code, out, raw_text
        finally:
            await context.close()

    async def _reload_page_async(self) -> None:
        context = await self._create_context()
        page = await context.new_page()
        try:
            await page.goto(self.CHECK_PAGE, wait_until="domcontentloaded", timeout=self._timeout_ms())
            await page.wait_for_function(
                "() => !!(window.grecaptcha && window.grecaptcha.execute && window.conf)",
                timeout=self._timeout_ms(),
            )
            await page.wait_for_function(
                """
                () => {
                  const select = document.getElementById('company');
                  const optionsReady = !!(select && select.options && select.options.length > 1);
                  const listReady = document.querySelectorAll('#companyBlock .selection-list div').length > 0;
                  return optionsReady || listReady;
                }
                """,
                timeout=self._timeout_ms(),
            )
        finally:
            await context.close()

    async def _run_with_limits_async(
        self,
        provider_code: str,
        receipt_code: str,
        user_scope: str | None,
    ) -> tuple[int, dict | None, str]:
        await self._ensure_async_state()
        assert self._global_limiter is not None

        user_key = (user_scope or "anon").strip() or "anon"
        user_limiter = self._user_limiters.get(user_key)
        if user_limiter is None:
            user_limiter = asyncio.Semaphore(self.per_user_parallel_limit)
            self._user_limiters[user_key] = user_limiter

        await self._global_limiter.acquire()
        await user_limiter.acquire()
        try:
            return await self._check_in_browser_async(provider_code, receipt_code)
        finally:
            user_limiter.release()
            self._global_limiter.release()

    def _check_in_browser(self, provider_code: str, receipt_code: str, user_scope: str | None = None) -> tuple[int, dict | None, str]:
        return self._runner.run(self._run_with_limits_async(provider_code, receipt_code, user_scope))

    def _reload_page(self) -> None:
        self._runner.run(self._reload_page_async())

    @staticmethod
    def _is_retryable_einfo(data: dict | None) -> bool:
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
        last_result: tuple[int, dict | None, str] | None = None
        max_total_attempts = 2
        attempts = 0
        current_provider = (provider_code or "").strip().lower()
        if not current_provider:
            return CheckResult(
                status=CheckStatus.UNPARSEABLE,
                source="check.gov.ua",
                message="Не вказано банк/сервіс для перевірки",
            )

        for _ in range(max_total_attempts):
            if attempts >= max_total_attempts:
                break
            attempts += 1
            try:
                if reload_before_check:
                    self._reload_page()
                status_code, data, raw_text = self._check_in_browser(current_provider, receipt_code, user_scope=user_scope)
            except Exception as exc:
                self.close()
                if attempts < max_total_attempts:
                    continue
                return CheckResult(
                    status=CheckStatus.CHECK_ERROR,
                    source="check.gov.ua",
                    message=f"Помилка перевірки check.gov.ua: зациклений/нестабільний запит ({exc})",
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
                if attempts >= max_total_attempts:
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
                continue

        if not last_result:
            return CheckResult(
                status=CheckStatus.CHECK_ERROR,
                source="check.gov.ua",
                message="Помилка перевірки check.gov.ua: немає відповіді від сервісу",
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
            status=CheckStatus.CHECK_ERROR,
            source="check.gov.ua",
            message="Помилка перевірки check.gov.ua: невизначена або застаріла відповідь",
            details=(data if isinstance(data, dict) else {"raw": data, "raw_text": raw_text[:1000]})
            | {"http_status": status_code},
        )

    async def _close_session_async(self) -> None:
        await self._ensure_async_state()
        assert self._session_lock is not None
        async with self._session_lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    def close(self) -> None:
        self._runner.run(self._close_session_async())

    def shutdown(self) -> None:
        try:
            self.close()
        finally:
            self._runner.close()
