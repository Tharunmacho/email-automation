"""Isolated command-search browser checks; every API request uses local fixtures."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("UI_CHECK_URL", "http://localhost:3000").rstrip("/")
OUT = ROOT / "scratch"
PAGES = ["overview", "candidates", "candidate-entry", "assigned-candidates", "attendance", "payroll", "staff", "job-orders", "sourcing", "b2b-enquiries", "data-management", "users", "settings"]
ADMIN = {"id": "command-audit-admin", "name": "Audit Administrator", "email": "admin@example.com", "role": "admin", "pages": PAGES}
STAFF = {"id": "command-audit-staff", "name": "Audit Staff", "email": "staff@example.com", "role": "staff", "pages": ["candidates", "candidate-entry", "settings"]}
CANDIDATE = {"id": "command-audit-candidate", "candidate_code": "CAN-AUDIT", "status": "parsed", "source": "upload", "created_at": "2026-09-12T09:00:00Z", "updated_at": "2026-09-12T09:00:00Z", "profile": {"full_name": "Audit Electrician", "email": "candidate@example.com", "current_designation": "Electrician", "skills": ["Wiring", "Safety"], "confidence": 0.9}}
AXE = ROOT / "frontend/node_modules/axe-core/axe.min.js"


def fixtures(user):
    def handle(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if request.resource_type not in ["fetch", "xhr"] or "_rsc" in parsed.query or path.startswith("/_next"):
            route.continue_()
            return
        body = {"items": [], "count": 0, "total": 0}
        if path == "/auth/me": body = {"user": user}
        elif path == "/candidates": body = {"items": [CANDIDATE], "count": 1, "total": 1}
        elif path == "/candidates/command-audit-candidate": body = CANDIDATE
        elif path == "/notifications": body = {"items": [], "unread_count": 0}
        elif path == "/health": body = {"status": "ok"}
        elif path == "/staff/workload": body = {"items": [], "roster_ids": [], "totals": {"assigned": 0, "unassigned": 0, "evaluated": 0, "unviewed": 0, "pending": 0}}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    return handle


def new_context(browser, user=ADMIN):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.route("**/*", fixtures(user))
    context.add_init_script("localStorage.setItem('ats_token','isolated-command-audit');")
    return context


def open_search(page):
    page.locator(".command-search-trigger").focus()
    page.keyboard.press("Control+k")
    page.locator(".command-search-dialog").wait_for()
    page.wait_for_timeout(220)
    assert page.locator(".command-search-input-row input").evaluate("el => el === document.activeElement"), "Input must receive initial focus"


with sync_playwright() as playwright:
    OUT.mkdir(parents=True, exist_ok=True)
    browser = playwright.chromium.launch(channel="chrome", headless=True)
    context = new_context(browser)
    page = context.new_page()
    console_errors = []
    page.on("pageerror", lambda error: console_errors.append(str(error)))
    report = []
    for width in [320, 1440]:
        for theme in ["light", "dark"]:
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{BASE}/preview?screen=candidates&theme={theme}")
            page.locator(".command-search-trigger").wait_for()
            open_search(page)
            first_active = page.locator(".command-search-input-row input").get_attribute("aria-activedescendant")
            page.keyboard.press("ArrowDown")
            assert page.locator(".command-search-input-row input").get_attribute("aria-activedescendant") != first_active
            page.keyboard.press("ArrowUp")
            assert page.locator(".command-search-input-row input").get_attribute("aria-activedescendant") == first_active
            box = page.locator(".command-search-dialog").bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width, box
            assert box["y"] >= 0 and box["y"] + box["height"] <= 900, box
            page.add_script_tag(path=str(AXE))
            violations = page.evaluate("""async () => (await axe.run('.command-search-dialog', {runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})).violations.map(v=>({id:v.id, impact:v.impact, nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))""")
            report.append({"viewport": width, "theme": theme, "contained": True, "axe": violations})
            assert not violations, violations
            page.screenshot(path=str(OUT / f"command-search-{width}-{theme}.png"))
            page.keyboard.press("Tab")
            assert page.get_by_role("button", name="Close search", exact=True).evaluate("el => el === document.activeElement")
            page.keyboard.press("Tab")
            assert page.locator(".command-search-body").evaluate("el => el === document.activeElement"), "Results must support keyboard scrolling"
            page.keyboard.press("Tab")
            assert page.locator(".command-search-input-row input").evaluate("el => el === document.activeElement"), "Tab must wrap to input"
            page.keyboard.press("Escape")
            page.wait_for_timeout(80)
            assert page.locator(".command-search-dialog").count() == 0
            assert page.locator(".command-search-trigger").evaluate("el => el === document.activeElement"), "Escape must restore focus"
    print(json.dumps({"viewportChecks": report}, indent=2), flush=True)
    open_search(page)
    page.get_by_role("combobox", name="Search pages and candidates").fill("Job Orders")
    assert page.get_by_role("option").count() == 1
    page.keyboard.press("Enter")
    page.wait_for_url(lambda url: "screen=job-orders" in url, wait_until="domcontentloaded")
    report.append({"navigationEnter": "passed", "url": page.url})
    page.goto(f"{BASE}/candidates")
    page.locator(".command-search-trigger").wait_for()
    open_search(page)
    page.get_by_role("combobox", name="Search pages and candidates").fill("Wiring")
    page.get_by_role("option", name="Audit Electrician").wait_for()
    assert page.get_by_role("option").count() == 1
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    assert page.locator(".command-search-dialog").count() == 0
    assert "Audit Electrician" in page.locator("main").inner_text()
    assert page.locator(".detail-screen").count() or page.get_by_role("button", name="Back", exact=False).count(), "Candidate detail should open"
    open_search(page)
    assert page.get_by_role("option", name="Wiring Search again").count() == 1
    page.get_by_role("button", name="Clear recent searches", exact=True).click()
    assert page.get_by_role("option", name="Wiring Search again").count() == 0
    page.get_by_role("combobox", name="Search pages and candidates").fill("zz-nothing-matches")
    assert page.get_by_text("No matching results", exact=True).count() == 1
    page.get_by_role("button", name="Clear search", exact=True).click()
    assert page.get_by_role("combobox", name="Search pages and candidates").input_value() == ""
    page.keyboard.press("Escape")
    open_search(page)
    page.get_by_role("combobox", name="Search pages and candidates").fill("Electrician")
    assert page.locator(".command-search-result mark").count() > 0, "Name matches must be highlighted"
    page.keyboard.press("Enter")
    page.wait_for_timeout(100)
    assert "Electrician" in page.evaluate("localStorage.getItem('adira-command-search:command-audit-admin')")
    report.append({"candidateSkillSearch": "passed", "candidateDetail": "passed", "recentSearchAndClear": "passed", "emptyState": "passed", "arrowNavigation": "passed", "queryHighlight": "passed"})
    context.unroute("**/*")
    context.route("**/*", fixtures(STAFF))
    staff_page = context.new_page()
    staff_page.goto(f"{BASE}/candidates")
    staff_page.locator(".command-search-trigger").wait_for()
    open_search(staff_page)
    labels = staff_page.get_by_role("option").all_text_contents()
    assert len(labels) == 4, labels
    assert not any("User Management" in label or "Job Orders" in label or "Payroll" in label for label in labels), labels
    assert not any("Electrician" in label for label in labels), "Recent searches must be scoped to the user sharing this browser"
    report.append({"staffPermissions": labels, "userScopedHistory": "passed", "pageErrors": console_errors})
    print(json.dumps(report, indent=2))
    browser.close()
