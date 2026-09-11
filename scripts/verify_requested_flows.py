"""Browser regression checks for the login, mailbox list, and personal queue."""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("UI_CHECK_URL", "http://localhost:3000")
OUT = ROOT / "scratch" / "requested-flows"
STAMP = datetime.now(timezone.utc).isoformat()
ROUTES = ["overview", "candidates", "assigned-candidates", "attendance", "payroll", "staff", "users", "settings"]


def user(user_id: str, role: str, name: str) -> dict:
    return {
        "id": user_id,
        "name": name,
        "email": f"{role}@example.com",
        "role": role,
        "pages": ROUTES,
        "page_grants": [],
    }


def candidate(candidate_id: str, name: str, owner_id: str | None, owner_name: str | None) -> dict:
    return {
        "id": candidate_id,
        "candidate_code": f"CAN-{candidate_id[-1]}",
        "status": "parsed",
        "created_at": STAMP,
        "updated_at": STAMP,
        "assigned_staff_id": owner_id,
        "assigned_staff_name": owner_name,
        "profile": {
            "full_name": name,
            "email": f"{candidate_id}@example.com",
            "phone": "+91 9000012345",
            "current_designation": "Engineer",
            "skills": ["QA"],
            "location": "Chennai",
            "total_experience_years": 4,
            "confidence": 0.9,
        },
    }


async def install_fixtures(page, current_user: dict, candidates: list[dict]):
    async def fixtures(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if request.resource_type not in ["fetch", "xhr"] or "_rsc" in parsed.query or path.startswith("/_next"):
            await route.continue_()
            return

        data: dict = {"items": [], "count": 0}
        if path == "/auth/me":
            data = {"user": current_user}
        elif path == "/candidates":
            data = {"items": candidates, "total": len(candidates), "count": len(candidates)}
        elif path == "/users":
            data = {
                "items": [
                    {**current_user, "active": True, "keywords": [], "created_at": STAMP},
                    {
                        "id": "staff-1",
                        "name": "Staff Account",
                        "email": "staff-person@adiragroups.com",
                        "phone": "+91 9000000001",
                        "role": "staff",
                        "active": True,
                        "keywords": [],
                        "page_grants": [],
                        "pages": ROUTES,
                        "created_at": STAMP,
                    },
                ],
                "pages": ROUTES,
            }
        elif path == "/ingest/rules":
            data = {
                "provider": "smtp_imap",
                "mailbox": {
                    "account": "cv@adiragroups.com",
                    "configured": True,
                    "accounts": [
                        {"address": "cv@adiragroups.com", "provider": "smtp_imap", "configured": True},
                        {"address": "jobs@adiragroups.com", "provider": "smtp_imap", "configured": True},
                    ],
                    "inbox_folder": "INBOX",
                    "processed_folder": "Processed",
                    "deleted_folder": "Deleted",
                    "gmail_query": "",
                },
                "gates": {
                    "detector_min_score": 0,
                    "inspect_all_documents": False,
                    "min_image_attachment_bytes": 0,
                    "min_ingest_confidence": 0,
                },
                "attachments": {"accepted_extensions": []},
                "ignored_senders": [],
                "ocr": {"provider": "Veris", "min_text_chars": 0, "dpi": 0, "chunk_pages": 0, "max_pages": 0, "give_up_pages": 0, "languages": "", "provider_configured": True},
                "extraction": {"model": "fixture", "configured": True},
                "auto_reply": {"enabled": False},
            }
        elif path == "/config":
            data = {"sla_threshold_hours": 24, "auto_assign_enabled": False}
        elif path == "/notifications":
            data = {"items": [], "unread_count": 0}
        elif path in ["/staff", "/attendance/employees"]:
            data = {"items": [], "count": 0}
        await route.fulfill(json=data)

    await page.route("**/*", fixtures)


async def axe(page) -> list[dict]:
    await page.add_script_tag(path=str(ROOT / "frontend/node_modules/axe-core/axe.min.js"))
    return await page.evaluate(
        """async () => (await axe.run({runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})).violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>n.target)}))"""
    )


async def authenticated_page(browser, current_user: dict, candidates: list[dict], path: str):
    context = await browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    await context.add_init_script("localStorage.setItem('ats_token','fixture-token'); localStorage.setItem('ats_theme','light')")
    page = await context.new_page()
    await install_fixtures(page, current_user, candidates)
    await page.goto(f"{BASE}/{path}")
    await page.locator(".workspace").wait_for()
    return context, page


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = {"viewports": [], "axe": [], "checks": [], "errors": []}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)

        context = await browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        await context.add_init_script("localStorage.removeItem('ats_token'); localStorage.setItem('ats_theme','light')")
        page = await context.new_page()
        page.on("pageerror", lambda error: results["errors"].append(str(error)))
        await page.goto(BASE)
        await page.get_by_role("heading", name="Sign in with email").wait_for()
        for width in [320, 375, 414, 768, 1440]:
            await page.set_viewport_size({"width": width, "height": 740 if width < 768 else 900})
            await page.wait_for_timeout(150)
            dimensions = await page.evaluate("() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth})")
            assert dimensions["scrollWidth"] <= dimensions["width"], f"Login overflows at {width}px"
            results["viewports"].append({"width": width, **dimensions})
            results["axe"].append({"screen": f"login-{width}", "violations": await axe(page)})
            if width in [320, 1440]:
                await page.screenshot(path=str(OUT / f"login-{width}-light.png"), full_page=True)

        await page.get_by_role("button", name="Get started").click()
        await page.locator(".signin-error").wait_for()
        assert "Enter both your email and password" in await page.locator(".signin-error").inner_text()
        await page.get_by_label("Password", exact=True).fill("visible-test")
        await page.get_by_role("button", name="Show password").click()
        assert await page.get_by_label("Password", exact=True).get_attribute("type") == "text"
        results["checks"].append("Login validation and password visibility work")

        await page.set_viewport_size({"width": 1440, "height": 900})
        await page.get_by_role("button", name="Switch to dark theme").click()
        assert await page.locator("html").get_attribute("data-theme") == "dark"
        assert await page.locator(".signin-page").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(0, 0, 0)"
        results["axe"].append({"screen": "login-dark", "violations": await axe(page)})
        await page.screenshot(path=str(OUT / "login-1440-dark.png"), full_page=True)
        results["checks"].append("Dark login surface is pure black")
        await context.close()

        admin = user("admin-1", "admin", "Administrator")
        other = candidate("candidate-1", "Assigned To Staff", "staff-1", "Staff Account")
        unassigned = candidate("candidate-2", "Unassigned Person", None, None)

        context, page = await authenticated_page(browser, admin, [other, unassigned], "settings")
        await page.get_by_text("cv@adiragroups.com", exact=True).wait_for()
        settings_text = await page.locator("main").inner_text()
        assert "cv@adiragroups.com" in settings_text
        assert "jobs@adiragroups.com" in settings_text
        assert "staff-person@adiragroups.com" not in settings_text
        assert "admin@example.com" not in await page.locator(".settings-config").inner_text()
        results["checks"].append("Settings lists automation inboxes without user/staff emails")
        await page.screenshot(path=str(OUT / "settings-mailboxes.png"), full_page=True)
        results["axe"].append({"screen": "settings-mailboxes", "violations": await axe(page)})
        await context.close()

        context, page = await authenticated_page(browser, admin, [other, unassigned], "assigned-candidates")
        await page.get_by_role("heading", name="No candidates assigned to you").wait_for()
        body = await page.locator("main").inner_text()
        assert "Assigned To Staff" not in body and "Unassigned Person" not in body
        results["checks"].append("Admin personal queue is empty when nothing is assigned to admin")
        await page.screenshot(path=str(OUT / "assigned-admin-empty.png"), full_page=True)
        results["axe"].append({"screen": "assigned-admin-empty", "violations": await axe(page)})
        await context.close()

        manager = user("manager-1", "manager", "Manager One")
        own = candidate("candidate-3", "Manager Candidate", "manager-1", "Manager One")
        context, page = await authenticated_page(browser, manager, [own, other, unassigned], "assigned-candidates")
        body = await page.locator("main").inner_text()
        assert "Manager Candidate" in body
        assert "Assigned To Staff" not in body and "Unassigned Person" not in body
        results["checks"].append("Manager personal queue contains only candidates assigned to that manager")
        await context.close()

        staff = user("staff-1", "staff", "Staff One")
        own_staff = candidate("candidate-4", "Staff Candidate", "staff-1", "Staff One")
        context, page = await authenticated_page(browser, staff, [own_staff, own, unassigned], "assigned-candidates")
        body = await page.locator("main").inner_text()
        assert "Staff Candidate" in body
        assert "Manager Candidate" not in body and "Unassigned Person" not in body
        results["checks"].append("Staff queue contains only candidates assigned to that staff account")
        await context.close()

        await browser.close()

    violations = [entry for entry in results["axe"] if entry["violations"]]
    (OUT / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"checks": results["checks"], "viewports": len(results["viewports"]), "axe_pages_with_issues": violations, "errors": results["errors"]}, indent=2))
    assert not violations and not results["errors"]


if __name__ == "__main__":
    asyncio.run(main())
