from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.paths import HTML_DIR, SCREENSHOT_DIR

LOGGER = logging.getLogger(__name__)


@dataclass
class PageResult:
    page: Page
    url: str


class CaptchaError(RuntimeError):
    pass


class BrowserManager:
    def __init__(self, settings: dict):
        self.settings = settings["amazon"]
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.location_initialized = False

    async def __aenter__(self) -> "BrowserManager":
        self.playwright = await async_playwright().start()
        launch_options = {
            "headless": bool(self.settings.get("headless", False)),
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        executable = self.settings.get("chrome_executable")
        if executable and Path(executable).exists():
            launch_options["executable_path"] = executable
        self.browser = await self.playwright.chromium.launch(**launch_options)
        self.context = await self.browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale=self.settings.get("language", "en-US"),
            timezone_id="America/Los_Angeles",
        )
        self.context.set_default_timeout(int(self.settings.get("navigation_timeout_ms", 45000)))
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def new_page(self) -> Page:
        if not self.context:
            raise RuntimeError("BrowserManager 尚未启动")
        page = await self.context.new_page()
        if not self.location_initialized:
            try:
                await self._set_postal_code(page)
                self.location_initialized = True
            except Exception:
                await page.close()
                raise
        return page

    async def goto(self, page: Page, url: str) -> None:
        await asyncio.sleep(random.uniform(float(self.settings.get("min_delay_seconds", 2.5)), float(self.settings.get("max_delay_seconds", 5.5))))
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        if await self.is_captcha(page):
            raise CaptchaError(f"Amazon 返回验证码页面: {url}")

    async def recover_page(self, page: Page, attempt: int) -> Page:
        """Replace a possibly poisoned Chrome error tab before the next item retry."""
        delay = max(1.0, float(self.settings.get("retry_delay_seconds", 5))) * (attempt + 1)
        try:
            await page.close()
        except Exception:
            pass
        LOGGER.warning("采集页面异常，%.1f 秒后使用新标签页重试", delay)
        await asyncio.sleep(delay)
        return await self.new_page()

    async def is_captcha(self, page: Page) -> bool:
        title = (await page.title()).lower()
        body = (await page.locator("body").inner_text()).lower()
        return "robot check" in title or "enter the characters you see below" in body or "api-services-support@amazon.com" in body

    async def _set_postal_code(self, page: Page) -> None:
        postal_code = str(self.settings.get("postal_code", "90001"))
        attempts = max(1, int(self.settings.get("initialization_attempts", 3)))
        delay = max(0.5, float(self.settings.get("initialization_retry_delay_seconds", 3)))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self._set_postal_code_once(page)
                return
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                wait_seconds = delay * attempt
                LOGGER.warning(
                    "Amazon 邮编初始化第 %d/%d 次失败，%.1f 秒后重试: %s",
                    attempt, attempts, wait_seconds, exc,
                )
                await asyncio.sleep(wait_seconds)
        raise RuntimeError(f"无法设置 Amazon 邮编 {postal_code}，已尝试 {attempts} 次: {last_error}") from last_error

    async def _set_postal_code_once(self, page: Page) -> None:
        base_url = self.settings.get("base_url", "https://www.amazon.com")
        postal_code = str(self.settings.get("postal_code", "90001"))
        LOGGER.info("设置 Amazon 配送邮编为 %s", postal_code)
        await page.goto(base_url, wait_until="domcontentloaded")
        location_text = await self._location_text(page)
        if postal_code in re.sub(r"\s+", "", location_text):
            LOGGER.info("Amazon 配送位置已确认: %s", location_text)
            return
        if not self.context:
            raise RuntimeError("浏览器上下文不存在")
        # This is the same front-end request made by Amazon's delivery-location dialog.
        response = await self.context.request.post(
            f"{base_url}/gp/delivery/ajax/address-change.html",
            form={
                "locationType": "LOCATION_INPUT", "zipCode": postal_code,
                "storeContext": "generic", "deviceType": "web",
                "pageType": "Gateway", "actionSource": "glow",
            },
            headers={
                "accept": "application/json, text/javascript, */*; q=0.01",
                "x-requested-with": "XMLHttpRequest", "referer": f"{base_url}/",
            },
        )
        result = await response.json()
        if not response.ok or not result.get("successful"):
            raise RuntimeError(f"地址设置请求返回 HTTP {response.status}: {result}")
        # Amazon 偶尔会在地址请求后触发内部跳转；短暂等待后重新进入首页，
        # 避免立即 reload 与该跳转冲突并产生 net::ERR_ABORTED。
        await page.wait_for_timeout(1200)
        await page.goto(base_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        location_text = await self._location_text(page)
        if postal_code not in re.sub(r"\s+", "", location_text):
            raise RuntimeError(f"未能确认邮编 {postal_code} 已生效，当前位置文本: {location_text}")
        LOGGER.info("Amazon 配送位置已确认: %s", location_text)

    @staticmethod
    async def _location_text(page: Page) -> str:
        locator = page.locator("#glow-ingress-line2")
        if await locator.count():
            return (await locator.first.inner_text()).strip()
        return ""

    async def save_diagnostics(self, page: Page, prefix: str) -> tuple[str, str]:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix)[:80]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot = SCREENSHOT_DIR / f"{safe}_{stamp}.png"
        html = HTML_DIR / f"{safe}_{stamp}.html"
        await page.screenshot(path=str(screenshot), full_page=True)
        html.write_text(await page.content(), encoding="utf-8")
        return str(screenshot), str(html)
