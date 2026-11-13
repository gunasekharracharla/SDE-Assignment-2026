"""Browser-based verification of DOM XSS prevention.

Uses Playwright to open http://127.0.0.1:8000, simulates leads containing
XSS attack payloads, verifies that NO alert dialog or script executes,
and confirms that legitimate data renders correctly.
"""

import sys
from playwright.sync_api import sync_playwright

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

def run_browser_verification():
    alerts_triggered = []
    errors_detected = []

    print("Launching Chromium browser for DOM XSS security audit...")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Listen for any alert, confirm, or prompt dialogs
        page.on("dialog", lambda dialog: (
            alerts_triggered.append(dialog.message),
            print(f"SECURITY ALERT: Dialog triggered! Message: {dialog.message}"),
            dialog.dismiss()
        ))

        # Listen for unhandled page errors
        page.on("pageerror", lambda err: errors_detected.append(str(err)))

        # Intercept /api/dashboard/leads to return malicious payloads
        def handle_leads_route(route):
            mock_leads_response = {
                "status": "success",
                "total": 3,
                "page": 1,
                "limit": 20,
                "total_pages": 1,
                "leads": [
                    {
                        "id": 1,
                        "workflow_id": "motor_renewal",
                        "external_ref": "<img src=x onerror=alert('XSS_IMG')>",
                        "batch_id": "batch_001",
                        "phone_masked": "+91 ****** 1111",
                        "bucket": "completed",
                        "total_calls": 2,
                        "connected_calls": 1,
                        "current_disposition": "CONNECTED_INTERESTED",
                        "ever_success": True
                    },
                    {
                        "id": 2,
                        "workflow_id": "motor_renewal",
                        "external_ref": "<script>alert('XSS_SCRIPT')</script>",
                        "batch_id": "batch_001",
                        "phone_masked": "+91 ****** 2222",
                        "bucket": "in_progress_dialed",
                        "total_calls": 1,
                        "connected_calls": 0,
                        "current_disposition": "BUSY",
                        "ever_success": False
                    },
                    {
                        "id": 3,
                        "workflow_id": "motor_renewal",
                        "external_ref": '"><svg onload=alert(\'XSS_SVG\')>',
                        "batch_id": "batch_001",
                        "phone_masked": "+91 ****** 3333",
                        "bucket": "yet_to_reach_out",
                        "total_calls": 0,
                        "connected_calls": 0,
                        "current_disposition": "",
                        "ever_success": False
                    }
                ]
            }
            route.fulfill(
                status=200,
                content_type="application/json",
                body=__import__("json").dumps(mock_leads_response)
            )

        page.route("**/api/dashboard/leads*", handle_leads_route)

        # 1. Open Dashboard
        print("Navigating to http://127.0.0.1:8000/#leads ...")
        page.goto("http://127.0.0.1:8000/#leads", wait_until="networkidle")

        # Give 1 second for any malicious script to potentially execute
        page.wait_for_timeout(1000)

        # 2. Check table body text content and innerHTML
        tbody = page.locator("#leads-table-body")
        raw_html = tbody.inner_html()
        text_content = tbody.text_content()

        print("\n--- Verification Results ---")
        print(f"Alert dialogs triggered: {len(alerts_triggered)}")
        assert len(alerts_triggered) == 0, f"XSS EXECUTED! Alerts: {alerts_triggered}"

        # 3. Assert HTML has been escaped
        assert "<img src=x onerror" not in raw_html, "Raw <img> tag found in HTML!"
        assert "&lt;img src=x onerror" in raw_html, "Escaped &lt;img tag missing from HTML!"

        assert "<script>alert" not in raw_html, "Raw <script> tag found in HTML!"
        assert "&lt;script&gt;alert" in raw_html, "Escaped &lt;script&gt; tag missing from HTML!"

        assert "<svg onload" not in raw_html, "Raw <svg> tag found in HTML!"
        assert "&lt;svg onload" in raw_html, "Escaped &lt;svg tag missing from HTML!"

        # 4. Assert text content displays accurately to the user
        assert "<img src=x onerror=alert('XSS_IMG')>" in text_content
        assert "<script>alert('XSS_SCRIPT')</script>" in text_content
        assert '"><svg onload=alert(\'XSS_SVG\')>' in text_content

        # 5. Assert legitimate data displays correctly
        assert "motor_renewal" in text_content
        assert "+91 ****** 1111" in text_content
        assert "CONNECTED_INTERESTED" in text_content

        print("✓ All 3 XSS payloads safely neutralized as plain text.")
        print("✓ Zero alert dialogs executed.")
        print("✓ Legitimate workflow and phone data displayed accurately.")

        browser.close()
        return True

if __name__ == "__main__":
    success = run_browser_verification()
    sys.exit(0 if success else 1)
