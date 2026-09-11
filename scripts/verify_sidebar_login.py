"""Check collapsed navigation groups through real login UI with mocked auth."""
import asyncio
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from verify_ui_audit import BASE, USER, OUT, fixtures


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport=dict(width=1440, height=900))
        async def route_request(route):
            if urlparse(route.request.url).path == "/auth/login":
                await route.fulfill(json=dict(token="ui-fixture-token", user=USER))
            else:
                await fixtures(route)
        await page.route("**/*", route_request)
        await page.goto(BASE + "/")
        for login_number in range(2):
            await page.locator("#login-email").fill(USER["email"])
            await page.locator("#login-password").fill("Fixture-password-123")
            await page.get_by_role("button", name="Sign in", exact=True).click()
            await page.locator(".rail").wait_for()
            groups = page.locator(".rail-section-toggle")
            assert await groups.count() == 2
            assert await groups.evaluate_all("nodes=>nodes.every(node=>node.getAttribute('aria-expanded')==='false')")
            candidates = page.get_by_role("button", name="Candidates", exact=True)
            await candidates.click()
            await page.get_by_role("link", name="All Candidates", exact=True).click()
            assert await candidates.get_attribute("aria-expanded") == "true"
            await candidates.click()
            assert await candidates.get_attribute("aria-expanded") == "false"
            assert not await page.get_by_role("link", name="All Candidates", exact=True).is_visible()
            staff = page.get_by_role("button", name="Staff Management", exact=True)
            await staff.click()
            await page.get_by_role("link", name="Attendance", exact=True).click()
            await page.get_by_text("Test Manager", exact=True).first.wait_for()
            if login_number == 0:
                await page.locator(".topbar-profile-trigger").click()
                await page.get_by_role("menuitem", name="Sign out", exact=True).click()
        print("PASS: both groups collapsed after login and re-login; manual toggle, active-group collapse, attendance navigation", flush=True)
        await page.reload()
        await page.locator(".rail-section-toggle").first.wait_for()
        assert await page.locator(".rail-section-toggle").evaluate_all("nodes=>nodes.every(node=>node.getAttribute('aria-expanded')==='false')")
        OUT.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(OUT / "sidebar-collapsed-login.png"))
        print("PASS: groups stay collapsed on fresh deep-link mount", flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
