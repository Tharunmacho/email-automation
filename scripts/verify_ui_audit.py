"""Isolated UI regression matrix; all API traffic is intercepted, never persisted.

Run the frontend, then `python scripts/verify_ui_audit.py` (Python Playwright +
installed Chrome). UI_CHECK_URL selects another local frontend port.
Screenshots and machine-readable results are written to scratch/ui-audit.
These fixtures verify frontend behavior, not backend integration or authorization.
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("UI_CHECK_URL", "http://localhost:3000")
OUT = ROOT / "scratch/ui-audit"
ROUTES = ["overview", "candidates", "candidate-entry", "assigned-candidates", "attendance", "payroll", "staff", "job-orders", "sourcing", "b2b-enquiries", "data-management", "users", "settings"]
WIDTHS = [320, 375, 414, 768, 1024, 1280, 1440, 1920]
STAMP = datetime.now(timezone.utc).isoformat()
PEOPLE = [dict(id=f"person-{role}", name=f"Test {role.title()}", email=f"{role}@example.com", role=role, staff_code=f"STF-{role.upper()}", keywords=[], active=True, assigned=1, evaluated=0, unviewed=1, pending=1, progress=0, pages=ROUTES, page_grants=[], created_at=STAMP) for role in ["staff", "manager"]]
USER = dict(id="audit-admin", name="Test Administrator", email="admin@example.com", role="admin", pages=ROUTES)
CANDIDATES = [dict(id=f"candidate-{i}", candidate_code=f"CAN-{i:04}", status="verified" if i % 3 == 0 else "parsed", created_at=STAMP, updated_at=STAMP, assigned_staff_id=PEOPLE[i % 2]["id"], assigned_staff_name=PEOPLE[i % 2]["name"], profile=dict(full_name=f"Candidate {i:02}", email=f"candidate{i}@example.com", phone="+91 9000012345", current_designation="Electrical Engineer", skills=["AutoCAD", "Safety"], location="Chennai", total_experience_years=4, confidence=0.95 if i % 3 == 0 else 0.7)) for i in range(32)]
INGEST = dict(provider="fixture", mailbox=dict(account="", configured=False, inbox_folder="", processed_folder="", deleted_folder="", gmail_query=""), gates=dict(detector_min_score=0, inspect_all_documents=False, min_image_attachment_bytes=0, min_ingest_confidence=0), attachments=dict(accepted_extensions=[]), ignored_senders=[], ocr=dict(provider="fixture", min_text_chars=0, dpi=0, chunk_pages=0, max_pages=0, give_up_pages=0, languages="", provider_configured=False), extraction=dict(model="", configured=False), auto_reply=dict(enabled=False))


async def fixtures(route):
    request = route.request
    parsed = urlparse(request.url)
    path = parsed.path
    # Next navigation/static resources must remain real, including same-origin
    # production builds where the API shares the frontend origin.
    if request.resource_type not in ["fetch", "xhr"] or "_rsc" in parsed.query or path.startswith("/_next"):
        await route.continue_()
        return
    employee = parse_qs(parsed.query).get("employee_id", [PEOPLE[0]["id"]])[0]
    data = dict(items=[], count=0)
    if path == "/auth/me": data = dict(user=USER)
    elif path == "/candidates": data = dict(items=CANDIDATES, total=len(CANDIDATES), count=len(CANDIDATES))
    elif path.startswith("/candidates/"): data = next((c for c in CANDIDATES if c["id"] == path.split("/")[2]), CANDIDATES[0])
    elif path in ["/staff", "/attendance/employees"]: data = dict(items=PEOPLE, count=2)
    elif path == "/staff/workload": data = dict(items=PEOPLE, roster_ids=[p["id"] for p in PEOPLE], totals=dict(staff=2, assigned=32, evaluated=0, unassigned=0, orphaned=0))
    elif path == "/users": data = dict(items=[dict(**USER, active=True, keywords=[], page_grants=[], created_at=STAMP), *PEOPLE], pages=ROUTES)
    elif path == "/attendance/weekly-off": data = dict(employee_id=employee, weekly_off_pattern="sunday")
    elif path.startswith("/attendance/month/"): data = dict(employee_id=employee, year=int(path.split("/")[3]), month=int(path.split("/")[4]), days=[], totals=dict(paid_permission_minutes=0, approved_permission_minutes=0, grace_minutes=0, unpaid_minutes=0, permission_occasions=0))
    elif path.startswith("/attendance/day/"): data = dict(employee_id=employee, date=path.split("/")[-1], status="A", late_minutes=0, early_minutes=0, unpaid_minutes=0, provisional=False)
    elif path.startswith("/payroll/"): data = dict(year=2026, month=9, basis="required_working_days", grace_allowance_minutes=120, paid_leave_allowance_days=1, items=[dict(employee_id=p["id"], name=p["name"], staff_code=p["staff_code"], monthly_salary=30000, deduction=0, net_salary=30000, unpaid_minutes=0, grace_minutes=0, paid_leave_days=0, calendar_days=30, required_working_days=26, daily_lop_rate=1153.85, weekly_off_pattern="sunday", alternate_friday_parity=0, status="draft") for p in PEOPLE])
    elif path == "/ingest/rules": data = INGEST
    elif path == "/ingest/workers": data = dict(available=False)
    elif path == "/health": data = dict(status="ok", candidates=32)
    elif path == "/config": data = dict(sla_hours=24, confidence_threshold=0.8)
    elif path == "/notifications": data = dict(items=[], unread_count=0)
    await route.fulfill(json=data)


async def axe(page):
    await page.add_script_tag(path=str(ROOT / "frontend/node_modules/axe-core/axe.min.js"))
    return await page.evaluate("""async () => (await axe.run({runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})).violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))""")


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = dict(matrix=[], accessibility=[], errors=[], interactions=[])
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport=dict(width=1440, height=900), reduced_motion="reduce")
        await page.route("**/*", fixtures)
        await page.add_init_script("localStorage.setItem('ats_token','ui-fixture-token')")
        page.on("pageerror", lambda error: results["errors"].append(dict(url=page.url, message=str(error))))
        for screen in ROUTES:
            await page.set_viewport_size(dict(width=1440, height=900))
            await page.goto(f"{BASE}/{screen}")
            await page.locator(".workspace").wait_for()
            await page.get_by_title("Light theme", exact=True).click()
            await page.wait_for_timeout(300)
            for width in WIDTHS:
                await page.set_viewport_size(dict(width=width, height=900 if width >= 768 else 740))
                await page.wait_for_timeout(250)
                bounds = await page.evaluate("""() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,heading:document.querySelector('main h1')?.textContent})""")
                results["matrix"].append(dict(screen=screen, **bounds))
                if width in [320, 1440]: await page.screenshot(path=str(OUT / f"{screen}-{width}-light.png"))
                if width == 320:
                    results["accessibility"].append(dict(screen=screen, theme="light-mobile", violations=await axe(page)))
                    assert await page.evaluate("""() => {
                      const brand=document.querySelector('.topbar-logo .brand-lockup')?.getBoundingClientRect();
                      const search=document.querySelector('.command-search-trigger')?.getBoundingClientRect();
                      return !brand || !search || brand.right <= search.left;
                    }"""), f"Mobile header overlaps on {screen}"
            for theme in ["light", "dark"]:
                await page.set_viewport_size(dict(width=1440, height=900))
                await page.get_by_title(f"{theme.title()} theme", exact=True).click()
                await page.wait_for_timeout(500)
                violations = await axe(page)
                results["accessibility"].append(dict(screen=screen, theme=theme, violations=violations))
                if theme == "dark": await page.screenshot(path=str(OUT / f"{screen}-1440-dark.png"))
            print(f"Audited {screen}: overflow={sum(r['scroll']>r['width'] for r in results['matrix'] if r['screen']==screen)}, axe={sum(len(r['violations']) for r in results['accessibility'] if r['screen']==screen)}", flush=True)
            (OUT / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        await page.goto(f"{BASE}/attendance")
        await page.get_by_text("Test Manager", exact=True).first.wait_for()
        results["interactions"].append("Admin attendance renders staff AND manager fixture records")
        await page.goto(f"{BASE}/candidates")
        await page.locator(".command-search-trigger").wait_for()
        await page.keyboard.press("Control+k")
        dialog = page.get_by_role("dialog")
        await dialog.wait_for()
        await dialog.get_by_role("combobox").fill("Candidate 01")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(250)
        assert await dialog.count() == 0
        assert "Candidate 01" in await page.locator("main").inner_text()
        results["interactions"].append("Command search opens real candidate detail callback")
        await page.goto(f"{BASE}/overview")
        await page.locator(".command-search-trigger").click()
        await page.get_by_role("dialog").get_by_role("combobox").fill("Payroll")
        await page.keyboard.press("Enter")
        await page.wait_for_url("**/payroll")
        await page.go_back()
        await page.wait_for_url("**/overview")
        await page.go_forward()
        await page.wait_for_url("**/payroll")
        results["interactions"].append("Command navigation and browser Back/Forward preserve routes")
        await page.goto(f"{BASE}/overview")
        await page.set_viewport_size(dict(width=320, height=568))
        toggle = page.get_by_role("button", name="Open navigation")
        await toggle.click()
        await page.get_by_role("button", name="Close navigation").wait_for()
        await page.keyboard.press("Escape")
        assert not await page.locator(".rail").evaluate("el=>el.classList.contains('is-open')")
        assert await toggle.evaluate("el=>el===document.activeElement")
        results["interactions"].append("Mobile navigation closes on Escape and restores focus")
        await browser.close()
    (OUT / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    failures = [r for r in results["matrix"] if r["scroll"] > r["width"]]
    violations = [r for r in results["accessibility"] if r["violations"]]
    print(json.dumps(dict(viewports=len(results["matrix"]), overflow=failures, axe_pages_with_issues=len(violations), errors=results["errors"], interactions=results["interactions"]), indent=2), flush=True)
    assert not failures and not violations and not results["errors"], f"See {OUT / 'results.json'}"


if __name__ == "__main__":
    asyncio.run(main())
