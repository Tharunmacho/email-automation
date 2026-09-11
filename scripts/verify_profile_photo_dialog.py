"""Regression check for the profile photo editor, using an isolated preview session.

Start the frontend, then run: python scripts/verify_profile_photo_dialog.py --verify
Requires Python Playwright and installed Chrome. UI_CHECK_URL overrides localhost:3000.
Screenshots are written to scratch; the uploaded fixture and saved photo stay in
the temporary browser context. No backend or personal account is needed.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("UI_CHECK_URL", "http://localhost:3000")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/notifications?*", lambda route: route.fulfill(json={"items": [], "unread_count": 0}))
        await page.goto(f"{BASE}/preview?screen=overview")
        await page.locator(".topbar-profile-trigger").wait_for()
        await page.locator(".topbar-profile-trigger").click()
        await page.locator(".topbar-profile-upload input").set_input_files(ROOT / "frontend/public/adira-logo.png")
        await page.locator(".profile-photo-crop img").wait_for()
        await page.wait_for_timeout(350)
        results = []
        for width, height in [(1440, 900), (1563, 527), (1250, 420), (812, 375), (320, 568), (375, 667), (768, 600)]:
            await page.set_viewport_size({"width": width, "height": height})
            await page.wait_for_timeout(250)
            result = await page.evaluate("""() => {
              const rect = selector => { const r=document.querySelector(selector).getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right}; };
              return {viewport:[innerWidth,innerHeight], overlay:rect('.profile-photo-editor-overlay'), dialog:rect('.profile-photo-editor'), header:rect('.profile-photo-editor-head'), footer:rect('.profile-photo-editor-foot'), scrollWidth:document.documentElement.scrollWidth, bodyLocked:document.body.style.overflow==='hidden'};
            }""")
            results.append(result)
        print(json.dumps({"bounds":[{"viewport":r["viewport"],"dialogTop":r["dialog"]["y"],"dialogBottom":r["dialog"]["bottom"],"overlayHeight":r["overlay"]["height"]} for r in results],"errors":errors}, indent=2), flush=True)
        if "--verify" in sys.argv:
            for result in results:
                width, height = result["viewport"]
                assert result["overlay"]["y"] == 0 and abs(result["overlay"]["height"]-height) < 2, result
                assert result["dialog"]["y"] >= 0 and result["dialog"]["bottom"] <= height+1, result
                assert result["header"]["y"] >= 0 and result["footer"]["bottom"] <= height+1, result
                assert result["scrollWidth"] <= width and result["bodyLocked"], result
            await page.get_by_role("button", name="Save photo", exact=True).focus()
            await page.keyboard.press("Tab")
            assert await page.get_by_role("button", name="Close photo editor").evaluate("el => el === document.activeElement")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(100)
            assert await page.locator(".profile-photo-editor").count() == 0
            assert await page.locator(".topbar-profile-trigger").evaluate("el => el === document.activeElement")
            assert await page.evaluate("document.body.style.overflow !== 'hidden'")
            print("PASS: viewport containment, visible header/footer, scroll lock, Tab, Escape and focus restoration", flush=True)
            await page.locator(".topbar-profile-trigger").click()
            await page.locator(".topbar-profile-upload input").set_input_files(ROOT / "frontend/public/adira-logo.png")
            await page.locator(".profile-photo-crop img").wait_for()
            zoom = page.locator('.profile-photo-controls input[type=range]')
            await zoom.focus()
            await zoom.press("End")
            assert await zoom.input_value() == "3"
            await page.get_by_role("button", name="Reset position and zoom").click()
            assert await zoom.input_value() == "1"
            await page.get_by_role("button", name="256px", exact=True).click()
            await page.get_by_role("button", name="Save photo", exact=True).click()
            await page.locator(".profile-photo-editor").wait_for(state="detached")
            saved = await page.evaluate("""async () => {
              const key=Object.keys(localStorage).find(k=>k.startsWith('adira-profile-photo:'));
              const image=new Image(); image.src=localStorage.getItem(key); await image.decode();
              return [image.naturalWidth,image.naturalHeight];
            }""")
            assert saved == [256, 256], saved
            await page.locator(".topbar-profile-trigger").click()
            await page.locator(".topbar-profile-upload input").set_input_files(ROOT / "frontend/public/adira-logo.png")
            await page.locator(".profile-photo-crop img").wait_for()
            await page.wait_for_timeout(250)
            for width, height, theme in [(1563,527,"light"), (320,568,"light"), (1440,900,"dark")]:
                await page.set_viewport_size({"width":width,"height":height})
                await page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
                await page.wait_for_timeout(250)
                await page.screenshot(path=str(ROOT / f"scratch/profile-photo-dialog-{width}-{theme}.png"))
            axe_path = ROOT / "frontend/node_modules/axe-core/axe.min.js"
            if axe_path.exists():
                await page.add_script_tag(path=str(axe_path))
                violations = await page.evaluate("async () => (await axe.run(document.querySelector('.profile-photo-editor'))).violations.map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}))")
                print(json.dumps({"dialogAxe":violations}), flush=True)
                assert not violations, violations
            await page.get_by_role("button", name="1024px", exact=True).click()
            await page.get_by_role("button", name="Cancel", exact=True).click()
            await page.locator(".profile-photo-editor").wait_for(state="detached")
            retained = await page.evaluate("""async () => {
              const key=Object.keys(localStorage).find(k=>k.startsWith('adira-profile-photo:'));
              const image=new Image(); image.src=localStorage.getItem(key); await image.decode();
              return [image.naturalWidth,image.naturalHeight];
            }""")
            assert retained == [256,256]
            assert not errors, errors
            print("PASS: zoom, reset, 256px save, cancel preserving the saved photo, and no page errors", flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
