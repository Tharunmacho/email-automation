"""Inspect dashboard sizing on the production route using shared UI fixtures."""
import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright
import verify_ui_audit as shared

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("UI_CHECK_URL", "http://localhost:3000").rstrip("/")

async def main():
    (ROOT / "scratch").mkdir(exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        await page.route("**/*", shared.fixtures)
        await page.add_init_script("localStorage.setItem('ats_token','ui-fixture-token')")
        await page.goto(f"{BASE}/overview")
        await page.locator(".ds-flow").wait_for()
        await page.wait_for_timeout(300)
        for width in [320, 375, 414, 768, 1024, 1280, 1440, 1920]:
            await page.set_viewport_size({"width": width, "height": 900})
            for delay in [60, 500]:
                await page.wait_for_timeout(delay)
                result = await page.evaluate("""() => ({width:innerWidth, scroll:document.documentElement.scrollWidth, elements:[...document.querySelectorAll('main *, .topbar, .workspace')].filter(el=>!el.closest('.ds-table-wrap')).map(el => {const r=el.getBoundingClientRect(); const s=getComputedStyle(el); return {tag:el.tagName,cls:el.className,width:r.width,left:r.left,right:r.right,minWidth:s.minWidth,overflow:s.overflow,display:s.display,transform:s.transform,transition:s.transition,grid:s.gridTemplateColumns}}).filter(el=>el.right>innerWidth+1 && el.width>0).slice(0,18)})""")
                print(json.dumps({"delay": delay, **result}), flush=True)
            if width == 1024:
                await page.screenshot(path=str(ROOT / "scratch/dashboard-actual-1024.png"))
        await page.set_viewport_size({"width": 320, "height": 740})
        await page.locator(".ds-flow button").first.focus()
        assert await page.locator(".ds-flow-tip").is_visible()
        await page.keyboard.press("Tab")
        assert await page.locator(".ds-flow button").nth(1).evaluate("el=>el===document.activeElement")
        await page.keyboard.press("Escape")
        assert await page.locator(".ds-flow-tip").count() == 0
        await page.locator(".ds-flow button").last.focus()
        await page.set_viewport_size({"width": 1024, "height": 900})
        await page.wait_for_timeout(60)
        assert await page.evaluate("document.documentElement.scrollWidth === innerWidth")
        await page.get_by_text("View chart data", exact=True).click()
        assert await page.locator(".ds-flow details tbody tr").count() == 8
        print("Actual-route chart keyboard, Escape, data disclosure and resize-with-tooltip checks passed", flush=True)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
