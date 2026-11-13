"""Browser-based verification of Manual Disposition Outcome Override.

Uses Playwright with real Chrome to test:
1. Navigating to Lead Management (#leads).
2. Clicking the 'Override' button on a lead with calls.
3. Verifying the modal opens with lead details, call selector, and schema-driven dispositions.
4. Entering an audit reason (including XSS test payload).
5. Triggering in-page confirmation dialog.
6. Submitting the override and verifying the live API call succeeds.
7. Confirming toast notification, modal dismissal, and table refresh.
8. Capturing screenshots for visual review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SCREENSHOT_DIR = Path(r"C:\Users\Guest user\.gemini\antigravity\brain\d5b428c1-e22e-48fa-8490-624987f9ec9c")


def verify_override_in_browser():
    alerts_triggered = []
    errors_detected = []

    # 1. First ensure there's at least one lead with calls by uploading test data via httpx
    print("Uploading test bundle via HTTP API...")
    import io
    import random
    import uuid
    import httpx

    uid = uuid.uuid4().hex[:6]
    test_phone = f"98765{random.randint(10000, 99999)}"
    test_pol = f"POL-BR-{uid}"
    test_call = f"CALL-BR-{uid}"

    test_csv = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        f"Ananya Deshmukh,{test_phone},{test_pol},2026-12-31,19500,Tata Nexon,en\n"
    )
    test_events = (
        f'{{"event_id":"evt-br-1-{uid}","workflow_id":"motor_renewal","event_type":"call.initiated","call_id":"{test_call}","occurred_at":"2026-03-02T10:00:00Z","phone":"{test_phone}"}}\n'
        f'{{"event_id":"evt-br-2-{uid}","workflow_id":"motor_renewal","event_type":"call.connected","call_id":"{test_call}","occurred_at":"2026-03-02T10:01:00Z","phone":"{test_phone}"}}\n'
        f'{{"event_id":"evt-br-3-{uid}","workflow_id":"motor_renewal","event_type":"disposition.set","call_id":"{test_call}","occurred_at":"2026-03-02T10:05:00Z","payload":{{"disposition_code":"callback_requested"}}}}\n'
    )

    with httpx.Client(base_url="http://127.0.0.1:8000") as client:
        r = client.post(
            "/api/upload/bundle",
            data={"workflow_id": "motor_renewal"},
            files={
                "leads_file": ("browser_leads.csv", io.BytesIO(test_csv.encode("utf-8")), "text/csv"),
                "events_file": ("browser_events.jsonl", io.BytesIO(test_events.encode("utf-8")), "application/x-jsonlines"),
            },
        )
        print(f"Bundle upload status: {r.status_code}")
        assert r.status_code == 200, f"Upload failed: {r.text}"

    print("Launching Chromium browser for Manual Disposition Outcome Override verification...")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        # Listen for any alert, confirm, or prompt dialogs (ensure zero XSS)
        page.on("dialog", lambda dialog: (
            alerts_triggered.append(dialog.message),
            print(f"SECURITY ALERT: Unexpected dialog triggered! Message: {dialog.message}"),
            dialog.dismiss()
        ))

        # Listen for unhandled page errors
        page.on("pageerror", lambda err: errors_detected.append(str(err)))

        # 2. Navigate to Lead Management
        print("Navigating to http://127.0.0.1:8000/#leads ...")
        page.goto("http://127.0.0.1:8000/#leads", wait_until="networkidle")

        # Wait for table to load
        page.wait_for_selector("#leads-table-body tr td:not([colspan])", timeout=10000)

        # 3. Find our lead row
        search_input = page.locator("#leads-search-input")
        search_input.fill(test_pol)
        page.wait_for_timeout(1000)
        page.wait_for_selector(f"#leads-table-body tr:has-text('{test_pol}')", timeout=10000)

        tbody = page.locator("#leads-table-body")
        row_text = tbody.text_content()
        assert test_pol in row_text, f"Uploaded lead not found in Lead Management table. row_text={row_text}"
        print(f"Lead {test_pol} located successfully in Lead table.")

        # 4. Click the 'Override' button
        override_btn = tbody.locator("button:has-text('Override')").first
        assert override_btn.is_visible(), "Override button should be visible"
        print("Clicking 'Override' button on lead...")
        override_btn.click()

        # Wait for modal to open
        modal = page.locator("#override-modal")
        page.wait_for_selector("#override-modal:not(.hidden)", timeout=5000)
        assert modal.is_visible(), "Override modal should be visible"
        print("Override modal opened successfully.")

        # Give 500ms for calls and schema to populate
        page.wait_for_timeout(800)

        # Verify lead info header in modal
        lead_info = page.locator("#modal-lead-info").text_content()
        print(f"Modal Header: {lead_info}")
        assert test_pol in lead_info

        # Verify call dropdown has our test call
        call_select = page.locator("#modal-call-select")
        call_val = call_select.input_value()
        print(f"Selected call in dropdown: {call_val}")
        assert test_call in call_val

        # Verify allowed disposition dropdown has choices from schema
        disp_select = page.locator("#modal-disp-select")
        disp_options = disp_select.locator("option").all_text_contents()
        print(f"Allowed dispositions count: {len(disp_options)}")
        assert any("renewed" in opt.lower() for opt in disp_options)
        disp_select.select_option(value="renewed")

        # Type audit reason with potential XSS payload to test escaping
        reason_textarea = page.locator("#modal-reason-input")
        xss_reason = 'Verified renewal via payment gateway <script>alert("REASON_XSS")</script>'
        reason_textarea.fill(xss_reason)

        counter_text = page.locator("#modal-reason-counter").text_content()
        assert f"{len(xss_reason)} / 500" in counter_text
        print(f"Audit reason character counter: {counter_text}")

        # Screenshot 1: Modal before submit
        shot1 = SCREENSHOT_DIR / "screen_modal_override_form.png"
        page.screenshot(path=str(shot1))
        print(f"Saved screenshot: {shot1}")

        # 5. Click 'Proceed to Override' to trigger confirmation dialog
        submit_btn = page.locator("#btn-modal-submit")
        submit_btn.click()

        # Verify confirmation box appears
        confirm_box = page.locator("#modal-confirm-box")
        assert confirm_box.is_visible(), "In-page confirmation box should be visible"
        confirm_text = page.locator("#modal-confirm-text").text_content()
        print(f"Confirmation dialog text: {confirm_text}")
        assert test_call in confirm_text
        assert "renewed" in confirm_text

        # Screenshot 2: Confirmation box
        shot2 = SCREENSHOT_DIR / "screen_modal_override_confirm.png"
        page.screenshot(path=str(shot2))
        print(f"Saved screenshot: {shot2}")

        # 6. Click 'Yes, Record Override'
        confirm_btn = page.locator("#btn-confirm-override")
        print("Confirming override submission...")
        confirm_btn.click()

        # Wait for modal dismissal
        page.wait_for_selector("#override-modal", state="hidden", timeout=5000)
        print("Modal dismissed successfully.")

        # Check toast message
        toast = page.locator("#toast")
        page.wait_for_selector("#toast:not(.translate-y-24)", timeout=5000)
        toast_msg = page.locator("#toast-message").text_content()
        print(f"Toast notification: {toast_msg}")
        assert "overridden" in toast_msg.lower() or "successfully" in toast_msg.lower()

        # Wait for leads table re-render
        page.wait_for_timeout(1000)

        # 7. Check lead row now reflects 'renewed' and 'completed'
        updated_row_text = tbody.text_content()
        print(f"Updated lead row: {updated_row_text}")
        assert "renewed" in updated_row_text
        assert "completed" in updated_row_text

        # Screenshot 3: Final updated dashboard
        shot3 = SCREENSHOT_DIR / "screen_lead_overridden_success.png"
        page.screenshot(path=str(shot3))
        print(f"Saved screenshot: {shot3}")

        # 8. Check security assertions
        print("\n--- Security and Stability Assertions ---")
        print(f"Total alert/confirm/prompt dialogs: {len(alerts_triggered)}")
        print(f"Total unhandled page errors: {len(errors_detected)}")
        assert len(alerts_triggered) == 0, f"XSS detected! Dialogs: {alerts_triggered}"
        assert len(errors_detected) == 0, f"JavaScript errors: {errors_detected}"
        print("Zero XSS alerts and zero JavaScript errors detected.")

        browser.close()
        print("\nSUCCESS: Manual Disposition Outcome Override verified completely in Chrome browser!")


if __name__ == "__main__":
    verify_override_in_browser()
