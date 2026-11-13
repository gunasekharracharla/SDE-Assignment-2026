"""Browser automation script to test all 5 views, verify all 5 downloads, and capture screenshots."""

import os
from pathlib import Path
from playwright.sync_api import sync_playwright

SCREENSHOT_DIR = Path(r"C:\Users\Guest user\.gemini\antigravity\brain\d5b428c1-e22e-48fa-8490-624987f9ec9c")
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE_URL = "http://127.0.0.1:8000"


def run_browser_verification():
    print("Starting Playwright with Chrome at:", CHROME_PATH)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_PATH,
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        page = context.new_page()

        # 1. Overview Dashboard
        print("Navigating to Overview Dashboard...")
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_selector("#metric-total-leads", state="visible")
        # Wait a moment for async stats fetch to populate
        page.wait_for_timeout(1000)
        overview_path = SCREENSHOT_DIR / "screen_dashboard.png"
        page.screenshot(path=str(overview_path))
        print("Captured:", overview_path)

        # 2. Upload Center
        print("Navigating to Upload Center...")
        page.click("#nav-upload")
        page.wait_for_selector("#view-upload", state="visible")
        page.wait_for_timeout(500)
        upload_path = SCREENSHOT_DIR / "screen_upload.png"
        page.screenshot(path=str(upload_path))
        print("Captured:", upload_path)

        # 3. Lead Management
        print("Navigating to Lead Management...")
        page.click("#nav-leads")
        page.wait_for_selector("#view-leads", state="visible")
        # Wait for table rows to render
        page.wait_for_selector("#leads-table-body tr td", state="visible", timeout=10000)
        page.wait_for_timeout(1000)
        leads_path = SCREENSHOT_DIR / "screen_leads.png"
        page.screenshot(path=str(leads_path))
        print("Captured:", leads_path)

        # 4. Interaction Ledger
        print("Navigating to Interaction Ledger...")
        page.click("#nav-ledger")
        page.wait_for_selector("#view-ledger", state="visible")
        page.wait_for_selector("#ledger-table-body tr td", state="visible", timeout=10000)
        page.wait_for_timeout(1000)
        ledger_path = SCREENSHOT_DIR / "screen_ledger.png"
        page.screenshot(path=str(ledger_path))
        print("Captured:", ledger_path)

        # 5. Official Reports & Test All 5 Browser Downloads
        print("Navigating to Official Reports...")
        page.click("#nav-reports")
        page.wait_for_selector("#view-reports", state="visible")
        page.wait_for_timeout(1000)
        reports_path = SCREENSHOT_DIR / "screen_reports.png"
        page.screenshot(path=str(reports_path))
        print("Captured:", reports_path)

        reports_to_download = [
            "batch_summary.csv",
            "daily_overview.csv",
            "lead_report.csv",
            "rejections.csv",
            "data_quality.json",
        ]
        downloads_dir = SCREENSHOT_DIR / "browser_downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)

        print("\n--- Testing All 5 Browser Report Downloads ---")
        for filename in reports_to_download:
            download_selector = f'a[href="/api/reports/download/{filename}"]'
            with page.expect_download() as download_info:
                page.click(download_selector)
            download = download_info.value
            saved_path = downloads_dir / filename
            download.save_as(str(saved_path))
            size = saved_path.stat().st_size
            print(f"Downloaded via Browser: {filename} -> {size} bytes (suggested filename: {download.suggested_filename})")
            assert download.suggested_filename == filename
            assert size > 0

        # 6. Data Quality Audit
        print("\nNavigating to Data Quality Audit...")
        page.click("#nav-quality")
        page.wait_for_selector("#view-quality", state="visible")
        page.wait_for_timeout(1000)
        quality_path = SCREENSHOT_DIR / "screen_quality.png"
        page.screenshot(path=str(quality_path))
        print("Captured:", quality_path)

        browser.close()
        print("\nBrowser verification completed successfully!")


if __name__ == "__main__":
    run_browser_verification()
