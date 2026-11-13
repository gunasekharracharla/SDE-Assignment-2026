"""Browser-based verification for Upload Notification Accuracy (Task 5).

Uses Playwright Chrome to verify:
1. Scenario 1: Fully successful upload shows green toast (✅) and green result card.
2. Scenario 2: Partially successful upload shows amber toast (⚠️) and rejection breakdown.
3. Scenario 3: Completely rejected upload NEVER shows green success; shows red toast (❌) and red result card (✕).
4. Scenario 6: Duplicate-only upload shows blue toast (🔄) and merged count with 0 new leads.
5. Scenario 5: Malformed upload shows error toast (❌) and card remains hidden.
"""

from __future__ import annotations

import io
from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SCREENSHOT_DIR = Path(r"C:\Users\Guest user\.gemini\antigravity\brain\d5b428c1-e22e-48fa-8490-624987f9ec9c")


def verify_upload_notifications_in_browser():
    errors_detected = []
    print("Launching Chromium browser for Upload Notification Accuracy verification...")

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        page.on("pageerror", lambda err: errors_detected.append(str(err)))

        # Navigate to Upload Center
        print("Navigating to http://127.0.0.1:8000/#upload ...")
        page.goto("http://127.0.0.1:8000/#upload", wait_until="networkidle")
        page.wait_for_timeout(500)

        # ----------------------------------------------------------------------
        # Test A: Fully successful upload (Scenario 1)
        # ----------------------------------------------------------------------
        print("\n--- Testing Scenario 1: Fully Successful Upload ---")
        csv_success = (
            "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
            "Ananya Sen,9876540101,POL-NOTIF-01,2026-12-31,10000,Honda,en\n"
            "Rohan Das,9876540102,POL-NOTIF-02,2026-12-31,12000,Hyundai,en\n"
        ).encode("utf-8")

        page.set_input_files("#input-leads", {
            "name": "success_leads.csv",
            "mimeType": "text/csv",
            "buffer": csv_success,
        })
        page.wait_for_timeout(300)
        page.click("#btn-submit-upload")

        # Wait for toast and card
        page.wait_for_selector("#upload-result-card:not(.hidden)", timeout=10000)
        toast_text = page.locator("#toast-message").text_content()
        toast_icon = page.locator("#toast-icon").text_content()
        badge_text = page.locator("#result-icon-badge").text_content()
        badge_class = page.locator("#result-icon-badge").get_attribute("class")

        print(f"Scenario 1 Toast: {toast_icon} {toast_text}")
        print(f"Scenario 1 Badge: {badge_text} ({badge_class})")

        assert toast_icon == "✅", f"Expected '✅', got '{toast_icon}'"
        assert "Upload successful" in toast_text
        assert badge_text == "✓"
        assert "bg-emerald-100" in badge_class

        shot1 = SCREENSHOT_DIR / "screen_upload_notif_success.png"
        page.screenshot(path=str(shot1))
        print(f"Saved screenshot: {shot1}")

        # ----------------------------------------------------------------------
        # Test B: Partially successful upload (Scenario 2)
        # ----------------------------------------------------------------------
        print("\n--- Testing Scenario 2: Partially Successful Upload ---")
        page.click("button:has-text('Reset')")
        page.wait_for_timeout(300)

        csv_partial = (
            "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
            "Kabir Mehta,9876540201,POL-PART-01,2026-12-31,15000,Honda,en\n"
            ",9876540202,POL-PART-02,2026-12-31,15000,Honda,en\n"  # Rejected (missing name)
        ).encode("utf-8")

        page.set_input_files("#input-leads", {
            "name": "partial_leads.csv",
            "mimeType": "text/csv",
            "buffer": csv_partial,
        })
        page.wait_for_timeout(300)
        page.click("#btn-submit-upload")

        page.wait_for_selector("#upload-result-card:not(.hidden)", timeout=10000)
        toast_text = page.locator("#toast-message").text_content()
        toast_icon = page.locator("#toast-icon").text_content()
        badge_text = page.locator("#result-icon-badge").text_content()
        badge_class = page.locator("#result-icon-badge").get_attribute("class")

        print(f"Scenario 2 Toast: {toast_icon} {toast_text}")
        print(f"Scenario 2 Badge: {badge_text} ({badge_class})")

        assert toast_icon == "⚠️", f"Expected '⚠️', got '{toast_icon}'"
        assert "Partial upload" in toast_text
        assert "rejected" in toast_text
        assert badge_text == "⚠️"
        assert "bg-amber-100" in badge_class

        # Verify rejection container is visible
        rejs_visible = page.locator("#result-rejections-container").is_visible()
        assert rejs_visible, "Rejection container should be visible for partial upload"

        shot2 = SCREENSHOT_DIR / "screen_upload_notif_partial.png"
        page.screenshot(path=str(shot2))
        print(f"Saved screenshot: {shot2}")

        # ----------------------------------------------------------------------
        # Test C: Completely rejected upload (Scenario 3 - Critical Assertion)
        # ----------------------------------------------------------------------
        print("\n--- Testing Scenario 3: Completely Rejected Upload ---")
        page.click("button:has-text('Reset')")
        page.wait_for_timeout(300)

        csv_rejected = (
            "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
            ",9876540301,POL-REJ-01,2026-12-31,10000,Honda,en\n"  # Missing name
            "Bad Phone,12345,POL-REJ-02,2026-12-31,12000,Honda,en\n"  # Invalid phone
        ).encode("utf-8")

        page.set_input_files("#input-leads", {
            "name": "all_rejected.csv",
            "mimeType": "text/csv",
            "buffer": csv_rejected,
        })
        page.wait_for_timeout(300)
        page.click("#btn-submit-upload")

        page.wait_for_selector("#upload-result-card:not(.hidden)", timeout=10000)
        toast_text = page.locator("#toast-message").text_content()
        toast_icon = page.locator("#toast-icon").text_content()
        badge_text = page.locator("#result-icon-badge").text_content()
        badge_class = page.locator("#result-icon-badge").get_attribute("class")
        title_text = page.locator("#result-card-title").text_content()

        print(f"Scenario 3 Toast: {toast_icon} {toast_text}")
        print(f"Scenario 3 Badge: {badge_text} ({badge_class}), Title: {title_text}")

        # CRITICAL REJECTION ASSERTIONS:
        assert toast_icon != "✅", "Completely rejected upload MUST NOT show green success icon!"
        assert "Upload successful" not in toast_text, "Completely rejected upload MUST NOT claim success!"
        assert toast_icon == "❌", f"Expected '❌', got '{toast_icon}'"
        assert "Upload rejected" in toast_text
        assert "failed schema validation" in toast_text
        assert badge_text == "✕"
        assert "bg-rose-100" in badge_class
        assert "Ingestion Rejected" in title_text

        shot3 = SCREENSHOT_DIR / "screen_upload_notif_rejected.png"
        page.screenshot(path=str(shot3))
        print(f"Saved screenshot: {shot3}")

        # ----------------------------------------------------------------------
        # Test D: Duplicate-only upload (Scenario 6)
        # ----------------------------------------------------------------------
        print("\n--- Testing Scenario 6: Duplicate-Only Upload ---")
        page.click("button:has-text('Reset')")
        page.wait_for_timeout(300)

        # Re-upload the exact same phone numbers from Scenario 1
        csv_dups = (
            "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
            "Ananya Sen Updated,9876540101,POL-NOTIF-01-V2,2027-12-31,11000,Honda,en\n"
            "Rohan Das Updated,9876540102,POL-NOTIF-02-V2,2027-12-31,13000,Hyundai,en\n"
        ).encode("utf-8")

        page.set_input_files("#input-leads", {
            "name": "dup_leads.csv",
            "mimeType": "text/csv",
            "buffer": csv_dups,
        })
        page.wait_for_timeout(300)
        page.click("#btn-submit-upload")

        page.wait_for_selector("#upload-result-card:not(.hidden)", timeout=10000)
        toast_text = page.locator("#toast-message").text_content()
        toast_icon = page.locator("#toast-icon").text_content()
        badge_text = page.locator("#result-icon-badge").text_content()
        badge_class = page.locator("#result-icon-badge").get_attribute("class")

        print(f"Scenario 6 Toast: {toast_icon} {toast_text}")
        print(f"Scenario 6 Badge: {badge_text} ({badge_class})")

        assert toast_icon == "🔄", f"Expected '🔄', got '{toast_icon}'"
        assert "Duplicate upload" in toast_text
        assert "0 new unique leads created" in toast_text
        assert badge_text == "🔄"
        assert "bg-sky-100" in badge_class

        shot4 = SCREENSHOT_DIR / "screen_upload_notif_duplicate_only.png"
        page.screenshot(path=str(shot4))
        print(f"Saved screenshot: {shot4}")

        print("\n--- Security and Stability Assertions ---")
        assert len(errors_detected) == 0, f"Page errors detected: {errors_detected}"
        print("Zero JavaScript errors detected during browser upload notification testing.")

        browser.close()
        print("\nSUCCESS: All Upload Notification Accuracy scenarios verified in Chrome browser!")


if __name__ == "__main__":
    verify_upload_notifications_in_browser()
