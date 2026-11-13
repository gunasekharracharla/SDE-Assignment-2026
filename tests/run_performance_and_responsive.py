"""Automated Performance Benchmarking, Responsive Layout Testing, and UI Control Verification.

Runs actual empirical measurements on the live FastAPI server (http://127.0.0.1:8000)
and captures responsive screenshots across Desktop, Tablet, and Mobile viewports.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Any

import httpx
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE_URL = "http://127.0.0.1:8000"
ARTIFACT_DIR = Path(r"C:\Users\Guest user\.gemini\antigravity\brain\d5b428c1-e22e-48fa-8490-624987f9ec9c")


def measure_performance_metrics() -> Dict[str, Any]:
    """Empirically measure live endpoint response times and throughput."""
    results: Dict[str, Any] = {}
    client = httpx.Client(base_url=BASE_URL, timeout=30.0)

    # 1. Health Endpoint Latency
    t0 = time.perf_counter()
    r = client.get("/api/health")
    r.raise_for_status()
    results["health_check_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    # 2. Dashboard Stats Query Latency
    durations = []
    for _ in range(5):
        t0 = time.perf_counter()
        r = client.get("/api/dashboard/stats")
        r.raise_for_status()
        durations.append((time.perf_counter() - t0) * 1000)
    results["dashboard_stats_avg_ms"] = round(sum(durations) / len(durations), 2)
    results["dashboard_stats_min_ms"] = round(min(durations), 2)
    results["dashboard_stats_max_ms"] = round(max(durations), 2)

    # 3. Leads Search & Filter Latency
    search_queries = ["MOT/2026", "1819", "batch_001", "nonexistent"]
    search_durations = []
    for q in search_queries:
        t0 = time.perf_counter()
        r = client.get(f"/api/dashboard/leads?q={q}&limit=25")
        r.raise_for_status()
        search_durations.append((time.perf_counter() - t0) * 1000)
    results["leads_search_avg_ms"] = round(sum(search_durations) / len(search_durations), 2)

    # 4. Leads Pagination Latency
    pages = [1, 5, 20, 50]
    page_durations = []
    for p in pages:
        t0 = time.perf_counter()
        r = client.get(f"/api/dashboard/leads?page={p}&limit=20")
        r.raise_for_status()
        page_durations.append((time.perf_counter() - t0) * 1000)
    results["leads_pagination_avg_ms"] = round(sum(page_durations) / len(page_durations), 2)

    # 5. Ledger Pagination & Filter Latency
    ledger_durations = []
    for ev_type in ["call.initiated", "call.connected", "disposition.set"]:
        t0 = time.perf_counter()
        r = client.get(f"/api/dashboard/ledger?event_type={ev_type}&page=1&limit=25")
        r.raise_for_status()
        ledger_durations.append((time.perf_counter() - t0) * 1000)
    results["ledger_filter_avg_ms"] = round(sum(ledger_durations) / len(ledger_durations), 2)

    # 6. Report Downloads Latency & Payload Sizes
    reports = [
        "batch_summary.csv",
        "daily_overview.csv",
        "lead_report.csv",
        "rejections.csv",
        "data_quality.json",
    ]
    report_perf = {}
    for rep in reports:
        t0 = time.perf_counter()
        r = client.get(f"/api/reports/download/{rep}")
        r.raise_for_status()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        report_perf[rep] = {
            "latency_ms": elapsed_ms,
            "size_bytes": len(r.content),
            "status": r.status_code,
        }
    results["report_downloads"] = report_perf

    # 7. Upload Processing Latency
    test_csv = (
        "Customer Name,Phone Number,Policy Number,Policy Expiry,Premium Amount,Vehicle Model,Preferred Language\n"
        + "\n".join(
            f"Perf Customer {i},987654{i:04d},POL-PERF-{i:04d},2026-12-31,15000,Car,en"
            for i in range(100)
        )
    ).encode("utf-8")
    t0 = time.perf_counter()
    r = client.post(
        "/api/upload/leads",
        data={"workflow_id": "motor_renewal"},
        files={"file": ("perf_leads.csv", io.BytesIO(test_csv), "text/csv")},
    )
    r.raise_for_status()
    results["upload_100_leads_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    client.close()
    return results


def run_responsive_and_ui_tests():
    """Verify all 6 sections and capture responsive screenshots on Tablet and Mobile."""
    print("Launching Playwright for responsive verification...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_PATH,
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )

        # ----------------------------------------------------------------------
        # A. TABLET VIEWPORT (768 x 1024)
        # ----------------------------------------------------------------------
        print("Testing Tablet Viewport (768x1024)...")
        tab_ctx = browser.new_context(viewport={"width": 768, "height": 1024})
        tab_page = tab_ctx.new_page()

        tab_page.goto(BASE_URL, wait_until="networkidle")
        tab_page.wait_for_timeout(1000)
        tab_page.screenshot(path=str(ARTIFACT_DIR / "screen_tablet_overview.png"))

        # Navigate to reports on tablet
        tab_page.click("#nav-reports")
        tab_page.wait_for_timeout(1000)
        tab_page.screenshot(path=str(ARTIFACT_DIR / "screen_tablet_reports.png"))

        tab_ctx.close()

        # ----------------------------------------------------------------------
        # B. MOBILE VIEWPORT (375 x 812)
        # ----------------------------------------------------------------------
        print("Testing Mobile Viewport (375x812)...")
        mob_ctx = browser.new_context(viewport={"width": 375, "height": 812})
        mob_page = mob_ctx.new_page()

        mob_page.goto(BASE_URL, wait_until="networkidle")
        mob_page.wait_for_timeout(1000)
        mob_page.screenshot(path=str(ARTIFACT_DIR / "screen_mobile_overview.png"))

        # Test mobile drawer/sidebar toggle
        print("Toggling Mobile Navigation Sidebar...")
        mob_page.click("button[onclick='toggleMobileNav()']")
        mob_page.wait_for_timeout(500)
        mob_page.screenshot(path=str(ARTIFACT_DIR / "screen_mobile_sidebar.png"))

        # Navigate to Upload Center on mobile
        mob_page.click("#nav-upload")
        mob_page.wait_for_timeout(1000)
        mob_page.screenshot(path=str(ARTIFACT_DIR / "screen_mobile_upload.png"))

        # Navigate to Lead Management on mobile
        mob_page.click("button[onclick='toggleMobileNav()']")
        mob_page.wait_for_timeout(300)
        mob_page.click("#nav-leads")
        mob_page.wait_for_timeout(1000)
        mob_page.screenshot(path=str(ARTIFACT_DIR / "screen_mobile_leads.png"))

        mob_ctx.close()

        # ----------------------------------------------------------------------
        # C. DESKTOP INTERACTIVE CONTROLS VERIFICATION
        # ----------------------------------------------------------------------
        print("Testing Desktop Interactive Controls...")
        desk_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        desk_page = desk_ctx.new_page()
        desk_page.goto(BASE_URL, wait_until="networkidle")
        desk_page.wait_for_timeout(1000)

        # 1. Test Leads Search Input
        desk_page.click("#nav-leads")
        desk_page.wait_for_timeout(1000)
        desk_page.fill("#leads-search-input", "MOT/2026/100000")
        desk_page.wait_for_timeout(1500)  # Wait for debounced search and fetch
        desk_page.screenshot(path=str(ARTIFACT_DIR / "screen_leads_searched.png"))

        # 2. Test Bucket Filter Dropdown
        desk_page.select_option("#leads-bucket-filter", "yet_to_reach_out")
        desk_page.wait_for_timeout(800)
        desk_page.screenshot(path=str(ARTIFACT_DIR / "screen_leads_filtered.png"))

        # 3. Test Ledger Event Type Filter
        desk_page.click("#nav-ledger")
        desk_page.wait_for_timeout(1000)
        desk_page.select_option("#ledger-type-filter", "disposition.set")
        desk_page.wait_for_timeout(800)
        desk_page.screenshot(path=str(ARTIFACT_DIR / "screen_ledger_filtered.png"))

        desk_ctx.close()
        browser.close()
        print("Responsive testing completed successfully!")


if __name__ == "__main__":
    print("\n--- 1. EMPIRICAL PERFORMANCE BENCHMARKING ---")
    perf_data = measure_performance_metrics()
    print(json.dumps(perf_data, indent=2))

    # Save performance metrics to artifact scratch directory
    perf_path = ARTIFACT_DIR / "performance_benchmark.json"
    perf_path.write_text(json.dumps(perf_data, indent=2), encoding="utf-8")
    print(f"Saved benchmark results to {perf_path}")

    print("\n--- 2. RESPONSIVE BROWSER & UI AUTOMATION ---")
    run_responsive_and_ui_tests()
