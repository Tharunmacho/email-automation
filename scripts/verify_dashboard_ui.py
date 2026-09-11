"""Read-only dashboard checks in an isolated browser with preview data."""
import json
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("UI_CHECK_URL", "http://localhost:3000").rstrip("/")
(ROOT / "scratch").mkdir(exist_ok=True)

def fixture(route):
    route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [], "unread_count": 0, "count": 0}))

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="chrome", headless=True)
    context = browser.new_context()
    context.route("**/notifications?*", fixture)
    page = context.new_page()
    if "--screenshots" in sys.argv:
        visuals = []
        for theme in ["light", "dark"]:
            for width in [320, 1440]:
                page.set_viewport_size({"width": width, "height": 960})
                page.goto(f"{BASE}/preview?screen=overview&theme={theme}")
                page.locator(".ds-flow").wait_for()
                page.wait_for_timeout(500)
                page.evaluate("window.scrollTo(0, 0)")
                visuals.append(page.evaluate("""() => {
                  const mark = document.querySelector('[class*="barMark"]');
                  const panel = document.querySelector('.ds-flow-panel');
                  const rgb = value => value.match(/[\\d.]+/g).slice(0,3).map(Number);
                  const markStyle = getComputedStyle(mark);
                  const foreground = rgb(markStyle.fill), background = rgb(getComputedStyle(panel).backgroundColor);
                  const opacity = Number(markStyle.opacity);
                  const composite = foreground.map((v,i) => v*opacity + background[i]*(1-opacity));
                  const luminance = values => values.map(v => v/255).map(v => v <= 0.04045 ? v/12.92 : ((v+0.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[0.2126,0.7152,0.0722][i],0);
                  const a = luminance(composite), b = luminance(background);
                  return {theme:document.documentElement.dataset.theme, width:innerWidth, chartContrast:Math.round((Math.max(a,b)+0.05)/(Math.min(a,b)+0.05)*100)/100};
                }"""))
                page.screenshot(path=str(ROOT / f"scratch/dashboard-polish-{width}-{theme}.png"), full_page=True)
        page.emulate_media(reduced_motion="reduce")
        print(json.dumps({"screenshots": visuals, "reducedMotionAnimation": page.locator('[class*="metricValue"]').first.evaluate("el => getComputedStyle(el).animationName")}))
        browser.close()
        sys.exit(0)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    results = []
    for theme in ["light", "dark"]:
        for width in [320, 375, 768, 1440]:
            page.set_viewport_size({"width": width, "height": 960})
            page.goto(f"{BASE}/preview?screen=overview&theme={theme}")
            page.locator(".ds-flow").wait_for()
            page.wait_for_timeout(400)
            page.add_script_tag(path=str(ROOT / "frontend/node_modules/axe-core/axe.min.js"))
            scan = page.evaluate("""async () => {
              const result = await axe.run(document.querySelector('.ds-page'), {runOnly: {type:'tag', values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}});
              return result.violations.map(v => ({id:v.id, impact:v.impact, nodes:v.nodes.map(n=>({target:n.target, summary:n.failureSummary}))}));
            }""")
            layout = page.evaluate("""() => ({viewport:innerWidth, documentWidth:document.documentElement.scrollWidth, chartWidth:document.querySelector('.ds-flow').getBoundingClientRect().width, svgWidth:document.querySelector('.ds-flow svg')?.getBoundingClientRect().width, headings:document.querySelectorAll('h1').length})""")
            targets = page.locator(".ds-flow button")
            targets.first.focus()
            focus_tip = page.locator(".ds-flow-tip").is_visible()
            tip_rect = page.locator(".ds-flow-tip").bounding_box()
            page.keyboard.press("Tab")
            next_focus = targets.nth(1).evaluate("el => document.activeElement === el")
            page.keyboard.press("Escape")
            escape = page.locator(".ds-flow-tip").count() == 0
            page.get_by_text("View chart data", exact=True).click()
            data_rows = page.locator(".ds-flow details tbody tr").count()
            page.get_by_text("View chart data", exact=True).click()
            page.get_by_role("searchbox", name="Search recent candidates").fill("no_such_candidate_123")
            empty_search = page.get_by_role("heading", name="No matching candidates").is_visible()
            page.get_by_role("button", name="Clear search", exact=True).click()
            restored = page.locator(".ds-table tbody tr").count() == 6
            if width in [320, 1440]:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(200)
                page.screenshot(path=str(ROOT / f"scratch/dashboard-polish-{width}-{theme}.png"), full_page=True)
            results.append({"theme": theme, "width": width, "layout": layout, "axe": scan, "keyboardTooltip": focus_tip, "tabNextBar": next_focus, "escapeDismisses": escape, "tooltipWithinViewport": bool(tip_rect and tip_rect["x"] >= 0 and tip_rect["x"] + tip_rect["width"] <= width), "dataRows": data_rows, "searchEmpty": empty_search, "clearRestoresRows": restored})
    print(json.dumps({"checks": results, "pageErrors": errors}, indent=2))
    browser.close()
