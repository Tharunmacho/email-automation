"""Payroll loading/error/retry regressions using isolated API fixtures only."""
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("UI_CHECK_URL", "http://localhost:3000").rstrip("/")
OUT = ROOT / "scratch"
sys.path.insert(0, str(ROOT))
from scripts.verify_ui_audit import fixtures


async def payroll_axe(page):
    await page.add_script_tag(path=str(ROOT / "frontend/node_modules/axe-core/axe.min.js"))
    return await page.evaluate("""async () => (await axe.run('.payroll-page',{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})).violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))""")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(viewport=dict(width=1440, height=900))
        gate = asyncio.Event()
        phase = "delayed-failure"
        errors = []

        async def handler(route):
            path = urlparse(route.request.url).path
            if path.startswith("/payroll/") and route.request.resource_type in ["fetch", "xhr"]:
                if phase == "delayed-failure":
                    await gate.wait()
                if phase != "success":
                    await route.fulfill(status=500, json={"detail": "Payroll temporarily unavailable (fixture)"})
                    return
            await fixtures(route)

        await context.route("**/*", handler)
        await context.add_init_script("localStorage.setItem('ats_token','isolated-payroll-load-audit')")
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(BASE + "/payroll", wait_until="domcontentloaded")
        await page.get_by_text("Preparing payroll", exact=True).wait_for()
        assert await page.locator(".payroll-summary-grid").count() == 0
        assert await page.get_by_text("No active employees", exact=True).count() == 0
        assert await page.locator("[aria-busy=true]").count() > 0
        gate.set()
        await page.get_by_text("Payroll could not be loaded", exact=True).wait_for()
        assert await page.locator(".payroll-summary-grid").count() == 0
        assert await page.get_by_text("No active employees", exact=True).count() == 0
        await page.set_viewport_size(dict(width=320, height=740))
        await page.wait_for_timeout(400)
        await page.screenshot(path=str(OUT / "payroll-initial-error-320.png"))
        bounds = await page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth})")
        assert bounds["scroll"] <= bounds["width"], bounds
        initial_axe = await payroll_axe(page)
        assert not initial_axe, initial_axe
        phase = "success"
        await page.get_by_role("button", name="Retry", exact=True).click()
        await page.locator(".payroll-card").first.wait_for()
        assert await page.locator(".payroll-card").count() == 2
        assert await page.locator(".db-feedback.is-error").count() == 0
        before = await page.locator(".payroll-summary-grid").inner_text()
        phase = "failure"
        await page.get_by_role("button", name="Refresh", exact=True).click()
        await page.get_by_text("Payroll could not be refreshed", exact=True).wait_for()
        assert await page.locator(".payroll-card").count() == 2
        assert await page.locator(".payroll-summary-grid").inner_text() == before
        await page.set_viewport_size(dict(width=1440, height=900))
        await page.wait_for_timeout(400)
        await page.get_by_title("Dark theme", exact=True).click()
        await page.wait_for_timeout(200)
        await page.screenshot(path=str(OUT / "payroll-refresh-error-1440-dark.png"))
        dark_axe = await payroll_axe(page)
        assert not dark_axe, dark_axe
        await page.get_by_label("Pay period", exact=True).fill("2026-08")
        await page.get_by_text("Payroll could not be loaded", exact=True).wait_for()
        assert await page.locator(".payroll-card").count() == 0
        assert await page.locator(".payroll-summary-grid").count() == 0
        assert await page.get_by_text("No active employees", exact=True).count() == 0
        assert not errors, errors
        print(json.dumps(dict(initialLoading="passed", initialFailure="passed", retry="passed", refreshPreservesRows="passed", differentMonthHidesOldData="passed", mobileBounds=bounds, axeLightMobile=initial_axe, axeDarkDesktop=dark_axe, pageErrors=errors), indent=2))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
