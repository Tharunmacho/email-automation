"""Keyboard regression checks against real React screens with isolated API fixtures.

All HTTP API calls are intercepted; this script does not touch CRM records.
Requires Python Playwright, installed Chrome, and frontend/node_modules/axe-core.
Set UI_CHECK_URL to test another locally served frontend build.
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("UI_CHECK_URL", "http://localhost:3000").rstrip("/")
SCREENSHOTS = ROOT / "scratch"

PAGES = ["overview", "candidates", "candidate-entry", "assigned-candidates", "attendance", "payroll", "staff", "job-orders", "sourcing", "b2b-enquiries", "data-management", "users", "settings"]
ADMIN = {"id": "audit-admin", "name": "Audit Administrator", "email": "admin@example.com", "role": "admin", "active": True, "pages": PAGES, "page_grants": []}
STAFF = {"id": "audit-staff", "name": "Audit Staff", "email": "staff@example.com", "role": "staff", "active": True, "phone": "", "pages": ["candidates"], "page_grants": [], "assigned": 1, "unviewed": 1, "pending": 1, "evaluated": 0, "progress": 0}
MANAGED_USERS = [ADMIN, STAFF]
CANDIDATE = {"id": "audit-candidate", "candidate_code": "CAN-AUDIT", "status": "parsed", "source": "upload", "created_at": "2026-09-12T09:00:00Z", "profile": {"full_name": "Audit Candidate", "email": "candidate@example.com", "skills": [], "confidence": 0.9}, "assigned_staff_id": "audit-staff", "assigned_staff_name": "Audit Staff"}
ENQUIRY = {"id": "audit-enquiry", "company_name": "Audit Company", "contact_name": "Audit Contact", "phone": "+1234567890", "job_title": "Welder", "headcount": 4, "requirement": "Four welders needed", "status": "new", "party_type": "client", "received_at": "2026-09-12T09:00:00Z"}
CLIENT = {"id": "audit-client", "name": "Audit Company", "type": "client", "contact": "Audit Contact", "phone": "+1234567890", "email": "client@example.com", "date": "2026-09-12", "status": "ACTIVE", "country": "India"}
A11Y = []

def fixture(route):
    parsed = urlparse(route.request.url)
    if route.request.resource_type not in ["fetch", "xhr"] or "_rsc" in parsed.query or parsed.path.startswith("/_next"):
        route.continue_()
        return
    path = urlparse(route.request.url).path.rstrip("/")
    body = {"items": [], "count": 0}
    if path == "/auth/me": body = {"user": ADMIN}
    elif path == "/users": body = {"items": MANAGED_USERS, "pages": PAGES}
    elif path == "/candidates": body = {"items": [CANDIDATE, {**CANDIDATE, "id": "audit-unassigned", "assigned_staff_id": None, "assigned_staff_name": None}, {**CANDIDATE, "id": "audit-orphan", "assigned_staff_id": "deleted-staff", "assigned_staff_name": "Former Staff"}], "count": 3, "total": 3}
    elif path == "/staff": body = {"items": [STAFF], "count": 1}
    elif path == "/staff/workload": body = {"items": [STAFF], "roster_ids": [STAFF["id"]], "totals": {"assigned": 1, "unassigned": 0, "evaluated": 0, "unviewed": 1, "pending": 1}}
    elif path == "/sla/breaches": body = {"items": [], "threshold_hours": 24}
    elif path == "/b2b-enquiries": body = {"items": [ENQUIRY], "count": 1}
    elif path == "/sourcing-clients": body = {"items": [CLIENT]}
    elif path == "/job-designations": body = {"items": [{"id": "audit-job", "title": "Welder", "active": True, "bot_visible": True, "bot_order": 1, "cv_required_default": True, "cv_overrides": {}}]}
    elif path == "/notifications": body = {"items": [], "unread_count": 0}
    elif path == "/health": body = {"status": "ok"}
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

def inspect(page, dialog):
    return dialog.evaluate("""el => ({role:el.getAttribute('role'), name:el.getAttribute('aria-label') || document.getElementById(el.getAttribute('aria-labelledby'))?.textContent, focused:el.contains(document.activeElement), active:document.activeElement?.getAttribute('aria-label') || document.activeElement?.tagName})""")

def visit(page, section):
    page.goto(BASE_URL + "/" + section)
    page.wait_for_timeout(650)

def baseline(page):
    results = []
    visit(page, "data-management")
    page.get_by_role("button", name="Add job", exact=True).first.click()
    dialog = page.locator(".dm-dialog")
    results.append({"screen": "Data Management", **inspect(page, dialog)})
    page.keyboard.press("Escape")
    visit(page, "sourcing")
    page.get_by_role("button", name="New client", exact=True).click()
    dialog = page.locator(".sh-modal")
    results.append({"screen": "Sourcing", **inspect(page, dialog)})
    page.keyboard.press("Escape")
    results[-1]["escapeClosed"] = dialog.count() == 0
    visit(page, "users")
    page.get_by_role("button", name="Edit", exact=True).last.click()
    dialog = page.locator(".modal-container")
    results.append({"screen": "User Management", **inspect(page, dialog)})
    page.keyboard.press("Escape")
    results[-1]["escapeClosed"] = dialog.count() == 0
    return results

def check_dialog(page, dialog, label):
    dialog.wait_for(state="visible")
    page.wait_for_function("dialog => dialog.contains(document.activeElement)", arg=dialog.element_handle())
    page.wait_for_timeout(100)
    details = inspect(page, dialog)
    assert details["focused"] and details["role"] in ("dialog", "alertdialog") and details["name"], (label, details)
    controls = dialog.locator("button:visible:not([disabled]), input:visible:not([disabled]), textarea:visible:not([disabled]), a[href]:visible, [tabindex='0']:visible")
    controls.first.focus()
    page.keyboard.press("Shift+Tab")
    assert controls.last.evaluate("el => el === document.activeElement"), label + ": backward Tab escaped"
    controls.last.focus()
    page.keyboard.press("Tab")
    assert controls.first.evaluate("el => el === document.activeElement"), label + ": forward Tab escaped"
    assert page.evaluate("document.body.style.overflow") == "hidden", label + ": missing scroll lock"
    rect = dialog.bounding_box()
    viewport = page.viewport_size
    assert rect["x"] >= 0 and rect["y"] >= 0 and rect["x"] + rect["width"] <= viewport["width"] + 1 and rect["y"] + rect["height"] <= viewport["height"] + 1, (label, "dialog out of viewport", rect, viewport)
    if not page.evaluate("Boolean(window.axe)"):
        page.add_script_tag(path=str(ROOT / "frontend" / "node_modules" / "axe-core" / "axe.min.js"))
    violations = dialog.evaluate("""async el => (await axe.run(el, {runOnly: {type:'tag', values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})).violations.map(v => ({id:v.id, impact:v.impact, targets:v.nodes.map(n=>n.target)}))""")
    A11Y.append({"dialog": label, "violations": violations})
    if label in ("Create sourcing client", "B2B delete confirmation", "Staff queue"):
        theme = page.evaluate("document.documentElement.dataset.theme")
        filename = label.lower().replace(" ", "-")
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOTS / f"modal-{filename}-{viewport['width']}-{theme}.png"))

def verify(page):
    results = []
    visit(page, "data-management")
    opener = page.get_by_role("button", name="Add job", exact=True).first
    opener.click()
    dialog = page.get_by_role("dialog", name="Add a job", exact=True)
    check_dialog(page, dialog, "Add job")
    field = dialog.get_by_role("combobox", name="Default CV requirement", exact=True)
    field.click()
    assert page.get_by_role("listbox").count() == 1
    page.keyboard.press("Escape")
    assert page.get_by_role("listbox").count() == 0 and dialog.count() == 1
    assert field.evaluate("el => el === document.activeElement")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert dialog.count() == 0 and opener.evaluate("el => el === document.activeElement")
    results.append("Data job: semantics, initial focus, both Tab boundaries, dropdown Escape, dismissal, restoration")

    page.get_by_role("button", name="Add country", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Add a country", exact=True)
    check_dialog(page, dialog, "Add country")
    page.keyboard.press("Escape")
    results.append("Data country: focus, both Tab boundaries, dismissal")
    page.get_by_role("button", name="Questions", exact=True).click()
    page.get_by_role("button", name="Add question", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Add a question", exact=True)
    check_dialog(page, dialog, "Add question")
    page.keyboard.press("Escape")
    results.append("Data question: focus, both Tab boundaries, dismissal")

    visit(page, "sourcing")
    opener = page.get_by_role("button", name="New client", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog", name="Create new client", exact=True)
    check_dialog(page, dialog, "Create sourcing client")
    field = dialog.get_by_role("combobox", name="Sourcing partner type", exact=True)
    field.click()
    page.keyboard.press("ArrowUp")
    page.keyboard.press("Tab")
    assert dialog.count() == 1 and page.get_by_role("listbox").count() == 0
    assert dialog.evaluate("el => el.contains(document.activeElement)")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert dialog.count() == 0 and opener.evaluate("el => el === document.activeElement"), page.evaluate("document.activeElement.outerHTML")
    results.append("Sourcing: focus, both Tab boundaries, Select keyboard commit, Escape, restoration")
    action_trigger = page.get_by_role("button", name="Actions for Audit Company", exact=True)
    action_trigger.click()
    page.get_by_role("button", name="Edit details", exact=True).click()
    dialog = page.get_by_role("dialog", name="Edit client", exact=True)
    check_dialog(page, dialog, "Edit sourcing client")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert action_trigger.evaluate("el => el === document.activeElement"), "Sourcing edit focus did not return to stable Actions trigger"

    visit(page, "users")
    opener = page.get_by_role("button", name="Edit", exact=True).last
    opener.click()
    dialog = page.get_by_role("dialog", name="Edit account", exact=True)
    check_dialog(page, dialog, "Edit user")
    dialog.get_by_role("combobox", name="Role", exact=True).click()
    page.keyboard.press("Escape")
    assert dialog.count() == 1 and page.get_by_role("listbox").count() == 0
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert dialog.count() == 0 and opener.evaluate("el => el === document.activeElement")
    results.append("Users edit: semantics, both Tab boundaries, dropdown Escape, dismissal, restoration")

    visit(page, "candidates")
    page.locator(".ds-table tbody tr").first.hover()
    opener = page.locator(".ds-review-action.is-assign").first
    opener.click()
    dialog = page.get_by_role("dialog", name="Assign candidate", exact=True)
    check_dialog(page, dialog, "Assign candidate")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert dialog.count() == 0 and opener.evaluate("el => el === document.activeElement")
    results.append("Candidates assignment: focus, both Tab boundaries, Escape, restoration")

    visit(page, "b2b-enquiries")
    opener = page.get_by_role("button", name="Open enquiry audit-enquiry from Audit Company", exact=True)
    opener.click()
    dialog = page.get_by_role("dialog", name="Enquiry audit-enquiry", exact=True)
    check_dialog(page, dialog, "B2B detail")
    delete_button = dialog.get_by_role("button", name="Delete", exact=True)
    delete_button.click()
    confirm = page.get_by_role("alertdialog", name="Delete enquiry", exact=True)
    check_dialog(page, confirm, "B2B delete confirmation")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert confirm.count() == 0 and dialog.count() == 1
    assert delete_button.evaluate("el => el === document.activeElement")
    assert page.evaluate("document.body.style.overflow") == "hidden"
    dialog.get_by_role("button", name="Convert to job order", exact=True).click()
    page.keyboard.press("Escape")
    assert dialog.count() == 1 and dialog.get_by_role("button", name="Convert to job order", exact=True).count() == 1
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert dialog.count() == 0 and opener.evaluate("el => el === document.activeElement")
    results.append("B2B: nested delete traps, only top Escape, nested scroll lock, restoration, conversion back")
    page.get_by_role("button", name="Log enquiry", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="Log a B2B enquiry", exact=True)
    check_dialog(page, dialog, "Log B2B enquiry")
    dialog.get_by_label("Contact person", exact=False).fill("Audit Contact")
    dialog.get_by_label("Job title", exact=True).fill("Welder")
    pending = []
    page.route("**/b2b-enquiries/manual", lambda route: pending.append(route))
    dialog.get_by_role("button", name="Log enquiry", exact=True).click()
    page.wait_for_timeout(100)
    assert pending, "Fixture submission did not start"
    page.keyboard.press("Escape")
    assert dialog.count() == 1
    assert dialog.get_by_role("button", name="Cancel", exact=True).is_disabled()
    assert dialog.get_by_role("button", name="Close enquiry form", exact=True).is_disabled()
    pending[0].fulfill(status=200, content_type="application/json", body=json.dumps({"enquiry": {**ENQUIRY, "id": "logged-audit-enquiry"}}))
    dialog.wait_for(state="hidden")
    results.append("B2B log: form submission fixture, in-flight dismiss safeguards, completion closes dialog")

    visit(page, "staff")
    row = page.locator(".staff-roster-table tbody tr").first
    row.click()
    dialog = page.get_by_role("dialog", name="Audit Staff", exact=True)
    check_dialog(page, dialog, "Staff queue")
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    assert not dialog.is_visible()
    row.get_by_role("button", name="Edit", exact=True).focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(100)
    assert page.get_by_role("dialog", name="Edit staff member", exact=True).is_visible(), "Staff row Enter swallowed nested Edit action"
    check_dialog(page, page.get_by_role("dialog", name="Edit staff member", exact=True), "Staff edit")
    results.append("Staff queue: focus, both Tab boundaries, Escape; row Edit keyboard action")
    page.keyboard.press("Escape")
    for label, name in [("Unallocated", "Unallocated profiles"), ("Orphaned", "Orphaned profiles")]:
        bucket = page.locator(".staff-roster-table tbody tr.is-bucket").filter(has_text=label).first
        bucket.focus()
        page.keyboard.press("Enter")
        assert page.get_by_role("dialog", name=name, exact=True).is_visible()
        page.keyboard.press("Escape")
    results.append("Staff bucket rows: unallocated and orphaned queues open with Enter")

    visit(page, "job-orders")
    page.get_by_role("button", name="Create New Order", exact=True).first.click()
    dialog = page.get_by_role("dialog", name="New job order", exact=True)
    check_dialog(page, dialog, "New job order")
    date_trigger = dialog.get_by_role("button", name="Target close date", exact=True)
    date_trigger.click()
    calendar = page.locator(".ui-date-panel")
    page.keyboard.press("Tab")
    assert calendar.evaluate("el => el.contains(document.activeElement)"), "Calendar portal not reachable by Tab"
    calendar.get_by_role("button", name="Next month", exact=True).focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Escape")
    assert calendar.count() == 0 and dialog.count() == 1
    assert date_trigger.evaluate("el => el === document.activeElement")
    page.keyboard.press("Escape")
    assert dialog.count() == 0
    results.append("Job Order date picker: Tab into portal, month navigation, inner Escape, outer Escape")
    return results

def run_checks(check):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        width = next((int(arg.split("=")[1]) for arg in sys.argv if arg.startswith("--width=")), 1440)
        context = browser.new_context(viewport={"width": width, "height": 900})
        context.route("**/*", fixture)
        context.add_init_script("localStorage.setItem('ats_token','isolated-ui-audit');")
        if "--dark" in sys.argv:
            context.add_init_script("localStorage.setItem('ats_theme','dark'); document.documentElement.dataset.theme='dark';")
        page = context.new_page()
        page.set_default_timeout(10000)
        try:
            print(json.dumps(check(page), indent=2))
            print(json.dumps(A11Y, indent=2))
            assert not any(result["violations"] for result in A11Y), "Opened-dialog accessibility violations found"
        except Exception:
            print(page.locator("body").inner_text()[:4500].encode("ascii", "replace").decode())
            print(json.dumps(A11Y, indent=2))
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    run_checks(baseline if "--baseline" in sys.argv else verify)
