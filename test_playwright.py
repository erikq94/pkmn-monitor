#!/usr/bin/env python3
"""Quick Playwright smoke test — opens Chrome, checks Target page, no purchases."""
import re
import subprocess
import time
from playwright.sync_api import sync_playwright

CHROME_PROFILE = "/Users/erikquiroz/Library/Application Support/Google/Chrome"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEBUG_PORT = 9222
URL = "https://www.target.com/p/-/A-95267143"

print("equiroz - starting playwright test")

# Kill any existing Chrome so we get a clean launch with debug port
result = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True)
if result.returncode == 0:
    print("equiroz - Chrome is running, killing it...")
    subprocess.run(["pkill", "-9", "Google Chrome"])
    time.sleep(2)
    print("equiroz - Chrome killed")

# Launch Chrome ourselves with remote debugging — this keeps full profile access
print(f"equiroz - launching Chrome with remote debugging on port {DEBUG_PORT}...")
chrome_proc = subprocess.Popen([
    CHROME_BIN,
    f"--remote-debugging-port={DEBUG_PORT}",
    f"--user-data-dir={CHROME_PROFILE}",
    "--start-maximized",
    "--no-first-run",
    "--no-default-browser-check",
])
print(f"equiroz - Chrome pid={chrome_proc.pid}, waiting for debug port to be ready...")
time.sleep(3)

with sync_playwright() as p:
    print(f"equiroz - connecting to Chrome via CDP at localhost:{DEBUG_PORT}...")
    browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
    print(f"equiroz - connected. Contexts: {len(browser.contexts)}")

    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    print(f"equiroz - pages open: {len(ctx.pages)}")

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.bring_to_front()
    print("equiroz - navigating to product page...")

    page.goto(URL, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)
    print("equiroz - page loaded")

    # Check login state
    body = page.inner_text("body")
    if "sign in" in body.lower() or "log in" in body.lower():
        print("equiroz - WARNING: NOT logged in — sign into Target in this browser before running the auto-buyer")
    else:
        print("equiroz - logged in")

    # Check seller
    if re.search(r"Sold\s*(?:&|and)\s*shipped\s*by\s+\w", body, re.IGNORECASE):
        print("equiroz - WARNING: 3rd party seller detected on page")
    else:
        print("equiroz - no 3rd party seller text — would pass seller check")

    # Scan for every button we care about
    buttons = {
        "Ship it (fast path)":   "button[data-test='shipItButton']",
        "Buy now (fast path)":   "button[data-test='buy-now-button']",
        "Buy now text":          "button:has-text('Buy now')",
        "Ship it text":          "button:has-text('Ship it')",
        "Add to cart":           "button[data-test='fulfillment-add-to-cart-button']",
        "Add to cart text":      "button:has-text('Add to cart')",
        "Place order":           "button[data-test='placeOrderButton']",
        "Place order text":      "button:has-text('Place your order')",
    }

    print("\nequiroz - scanning buttons on page:")
    for label, selector in buttons.items():
        try:
            el = page.locator(selector).first
            visible = el.is_visible(timeout=1000)
            enabled = el.is_enabled() if visible else False
            text = el.inner_text() if visible else ""
            status = "VISIBLE" if visible else "not found"
            print(f"  equiroz - {status} | {label} | enabled={enabled} | text='{text.strip()[:40]}'")
        except Exception as e:
            print(f"  equiroz - error | {label} | {e}")

    print("\nequiroz - test complete — browser staying open for 10 seconds")
    page.wait_for_timeout(10000)
    browser.close()
    print("equiroz - done")
