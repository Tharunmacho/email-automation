"""Delay/failure/retry and stale-data checks with isolated frontend API fixtures."""
import asyncio
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from verify_ui_audit import BASE, OUT, ROOT, fixtures, axe


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        for screen in ["overview", "candidates", "assigned-candidates", "staff"]:
            page = await browser.new_page(viewport=dict(width=320, height=740), reduced_motion="reduce")
            await page.clock.install()
            await page.add_init_script("localStorage.setItem('ats_token','ui-fixture-token')")
            release = asyncio.Event()
            failed = True
            async def route_request(route):
                if urlparse(route.request.url).path == "/candidates" and route.request.resource_type in ["fetch", "xhr"]:
                    await release.wait()
                    if failed:
                        await route.fulfill(status=503, json=dict(detail="Simulated unavailable service"))
                        return
                await fixtures(route)
            await page.route("**/*", route_request)
            await page.goto(f"{BASE}/{screen}")
            await page.get_by_text("Loading candidate records…", exact=True).wait_for()
            assert not await page.locator(".ds-stat-value, .ds-card-value").count()
            release.set()
            await page.get_by_text("Could not load candidate records", exact=True).wait_for()
            assert not await page.get_by_text("No candidates yet", exact=True).count()
            assert not await page.locator(".ds-stat-value, .ds-card-value").count()
            assert await page.locator("main h1").count() == 1
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not await axe(page)
            await page.screenshot(path=str(OUT / f"{screen}-load-error-320.png"))
            failed = False
            await page.get_by_role("button", name="Retry", exact=True).click()
            await page.locator(".candidate-data-state").wait_for(state="detached")
            await page.locator("main h1").wait_for()
            assert await page.locator("main h1").count() == 1
            print(f"PASS {screen}: delay, error, no false zeros, retry, mobile bounds, axe", flush=True)
            if screen == "candidates":
                failed = True
                await page.clock.fast_forward(45001)
                await page.get_by_text("Candidate data could not be refreshed", exact=True).wait_for()
                assert await page.get_by_text("Candidate 00", exact=True).count() > 0
                print("PASS failed background refresh preserves loaded candidates with stale-data feedback", flush=True)
            await page.close()
        await browser.close()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
