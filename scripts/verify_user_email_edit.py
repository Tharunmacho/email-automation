"""User Management email editing regression checks using intercepted API requests.

Uses the shared browser/axe fixture runner. UI_CHECK_URL defaults to localhost:3000;
pass --width=320 or --dark to check mobile or dark mode. No live records are edited.
"""
import json

from verify_modal_keyboard import ADMIN, MANAGED_USERS, ROOT, STAFF, check_dialog, run_checks, visit


def verify_email_edit(page):
    manager = {**STAFF, "id": "audit-manager", "name": "Audit Manager", "role": "manager", "email": "manager@example.com", "page_grants": ["sourcing"]}
    MANAGED_USERS.append(manager)
    pending = []
    page.route("**/users/audit-*", lambda route: pending.append(route))
    page.route("**/ingest/rules", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "mailbox": {"account": "recruitment@example.com", "configured": True},
            "ocr": {"provider": "Veris", "provider_configured": True},
        }),
    ))
    visit(page, "users")
    results = []

    for user in [STAFF, manager, ADMIN]:
        role = user["role"]
        row = page.locator(".staff-table tbody tr").filter(has_text=user["email"])
        row.get_by_role("button", name="Edit", exact=True).click()
        dialog = page.get_by_role("dialog", name="Edit account", exact=True)
        check_dialog(page, dialog, "Edit " + role + " email")
        email = dialog.get_by_label("Email address", exact=True)
        assert email.input_value() == user["email"] and email.is_enabled()
        expected_grants = list(user["page_grants"])
        original_name = user["name"]
        if role == "admin":
            assert dialog.get_by_role("combobox", name="Role", exact=True).is_disabled()
            assert dialog.get_by_role("checkbox", name="Account is active", exact=False).is_disabled()
        if role == "staff":
            for invalid, message in [("invalid-address", "Enter a valid email address."), ("", "Email address is required.")]:
                email.fill(invalid)
                dialog.get_by_role("button", name="Save", exact=True).click()
                assert not pending
                assert email.get_attribute("aria-invalid") == "true"
                assert dialog.get_by_role("alert").filter(has_text=message).is_visible()

        proposed = role + ".updated@example.com"
        email.fill(proposed)
        assert dialog.locator("#edit-user-email").inner_text() == proposed
        if role == "staff":
            (ROOT / "scratch").mkdir(parents=True, exist_ok=True)
            theme = page.evaluate("document.documentElement.dataset.theme")
            page.screenshot(path=str(ROOT / "scratch" / f"user-email-editor-{page.viewport_size['width']}-{theme}.png"))
        dialog.get_by_role("button", name="Save", exact=True).click()
        page.wait_for_timeout(100)
        assert len(pending) == 1
        assert dialog.get_by_role("button", name="Saving…", exact=True).is_disabled()
        assert email.is_disabled()
        page.keyboard.press("Escape")
        assert dialog.is_visible()
        assert dialog.get_by_role("button", name="Close account editor", exact=True).is_disabled()

        if role == "staff":
            pending.pop(0).fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "A user with this email already exists."}))
            dialog.get_by_role("alert").filter(has_text="A user with this email already exists.").wait_for()
            assert email.input_value() == proposed and email.is_enabled()
            assert dialog.get_by_role("combobox", name="Role", exact=True).inner_text() == "Staff"
            proposed = "staff.unique@example.com"
            email.fill(proposed)
            dialog.get_by_role("button", name="Save", exact=True).click()
            page.wait_for_timeout(100)

        request = pending.pop(0)
        payload = request.request.post_data_json
        assert request.request.method == "PATCH" and request.request.url.endswith("/users/" + user["id"])
        assert payload == {"email": proposed, "name": original_name, "phone": user.get("phone", ""), "role": role, "active": user["active"], "page_grants": expected_grants}, payload
        user["email"] = proposed
        request.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "ok", "user": user}))
        dialog.wait_for(state="hidden")
        page.locator(".staff-table tbody tr").filter(has_text=proposed).wait_for()
        results.append(role + ": correct email PATCH, existing role/name/grants preserved, saved email visible")

    # Read the current in-memory session through actual SPA navigation. A full
    # reload would conceal a missing onUserUpdated callback by fetching /auth/me.
    page.evaluate("window.__emailEditDocumentMarker = true")
    page.get_by_role("button", name="Open profile menu for Audit Administrator", exact=True).click()
    assert page.locator(".topbar-profile-card").get_by_text(ADMIN["email"], exact=True).is_visible()
    page.get_by_role("menuitem", name="Profile & settings", exact=True).click()
    page.wait_for_url("**/settings")
    assert page.evaluate("window.__emailEditDocumentMarker")
    assert page.locator(".settings-account-grid").get_by_text(ADMIN["email"], exact=True).is_visible()
    results.append("Self email immediately updated in profile menu and Settings without a page reload")
    results.append("Invalid and blank email prevented; duplicate rejected inline; retry and busy safeguards passed")
    return results


if __name__ == "__main__":
    run_checks(verify_email_edit)
