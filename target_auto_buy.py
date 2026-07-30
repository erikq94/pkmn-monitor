#!/usr/bin/env python3
"""
Target auto-buyer for Chaos Rising.
Run locally: python3 target_auto_buy.py
Polls Target's API every 30 seconds. When IN_STOCK + sold by Target,
opens your real Chrome session and gets you to the checkout screen.
"""
import html
import json
import os
import random
import re
import time
import subprocess
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cf
from playwright.sync_api import sync_playwright

WEBHOOK_URL   = os.environ["WEBHOOK_URL"]
TARGET_API_KEY = os.environ["TARGET_API_KEY"]
TARGET_ZIP    = "95122"
CHROME_PROFILE = os.path.expanduser("~/Library/Application Support/Google/Chrome")

# Products to auto-buy — add/remove TCINs here
AUTO_BUY_TCINS = {
    "95267143": "Chaos Rising Elite Trainer Box",
    "95298172": "Chaos Rising Booster Bundle",
}

PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

# Tracks which TCINs we've already triggered checkout for this session
_triggered = set()


def send_discord(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg, "username": "Pokebot"}, timeout=10)
    except Exception:
        pass


def fetch_fulfillment(tcins):
    url = (
        f"https://redsky.target.com/redsky_aggregations/v1/web/product_summary_with_fulfillment_v1"
        f"?key={TARGET_API_KEY}&tcins={'%2C'.join(tcins)}"
        f"&store_id=1984&zip={TARGET_ZIP}&state=CA"
        f"&latitude=37.290&longitude=-121.900"
        f"&scheduled_delivery_store_id=1984"
        f"&paid_membership=false&base_membership=false&card_membership=false"
        f"&required_store_id=1984&skip_price_promo=true&channel=WEB"
    )
    r = cf.get(url, headers={**PAGE_HEADERS, "Accept": "application/json"},
               impersonate="chrome120", timeout=20)
    if r.ok:
        return r.json().get("data", {}).get("product_summaries", [])
    return []


def is_sold_by_target(buy_url):
    try:
        r = cf.get(buy_url, headers=PAGE_HEADERS, impersonate="safari17_0", timeout=15)
        text = BeautifulSoup(r.text, "html.parser").get_text()
        if re.search(r"Sold\s*(?:&|and)\s*shipped\s*by\s+\w", text, re.IGNORECASE):
            return False
        return True
    except Exception:
        return True


def check_stock():
    """Returns list of (tcin, name, buy_url) that are IN_STOCK and sold by Target."""
    products = fetch_fulfillment(list(AUTO_BUY_TCINS.keys()))
    hits = []
    for p in products:
        tcin = p.get("tcin", "")
        if tcin not in AUTO_BUY_TCINS or tcin in _triggered:
            continue
        ship_status = p.get("fulfillment", {}).get("shipping_options", {}).get("availability_status", "")
        if ship_status != "IN_STOCK":
            continue
        name = html.unescape(p.get("item", {}).get("product_description", {}).get("title", AUTO_BUY_TCINS[tcin]))
        buy_url = p.get("item", {}).get("enrichment", {}).get("buy_url", f"https://www.target.com/p/-/A-{tcin}")
        print(f"  [{tcin}] IN_STOCK detected — verifying seller...")
        if is_sold_by_target(buy_url):
            print(f"  [{tcin}] ✅ Confirmed sold by Target — triggering checkout")
            hits.append((tcin, name, buy_url))
        else:
            print(f"  [{tcin}] ❌ Third-party seller — skipping")
    return hits


def run_checkout(tcin, name, buy_url):
    """Opens Chrome with your existing profile, adds to cart, proceeds to checkout."""
    _triggered.add(tcin)
    send_discord(
        f"🤖 **Auto-buyer triggered** 🎯\n"
        f"**{name}**\n"
        f"Opening checkout now — get to your laptop!"
    )
    print(f"\n{'='*55}")
    print(f"  LAUNCHING CHECKOUT: {name}")
    print(f"{'='*55}\n")

    # Quit Chrome if running so Playwright can take over the profile
    if subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True).returncode == 0:
        print("  Quitting Chrome to free profile for Playwright...")
        subprocess.run(["osascript", "-e", 'quit app "Google Chrome"'])
        time.sleep(2)

    with sync_playwright() as p:
        # Use your real Chrome with your existing login session
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            channel="chrome",
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        try:
            print("  Navigating to product page...")
            page.goto(buy_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            # Triple-check: confirm no third-party seller text on the live page
            body = page.inner_text("body")
            if re.search(r"Sold\s*(?:&|and)\s*shipped\s*by\s+\w", body, re.IGNORECASE):
                send_discord(f"⚠️ Auto-buyer aborted — page shows 3rd party seller for **{name}**")
                print("  ABORTED — 3rd party seller detected on live page")
                ctx.close()
                return

            # Try "Ship it" / "Buy now" first — skips cart entirely, faster path
            fast_btn = None
            for selector in [
                "button[data-test='shipItButton']",
                "button[data-test='buy-now-button']",
                "button:has-text('Ship it')",
                "button:has-text('Buy now')",
            ]:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=2000):
                        fast_btn = btn
                        print(f"  Found fast-path button: {selector}")
                        break
                except Exception:
                    continue

            if fast_btn:
                page.wait_for_timeout(random.randint(500, 1000))
                print("  Clicking Ship it / Buy now (fast path)...")
                fast_btn.click()
                page.wait_for_timeout(random.randint(500, 1000))
            else:
                # Fall back to Add to Cart → cart → checkout
                add_btn = None
                for selector in [
                    "button[data-test='fulfillment-add-to-cart-button']",
                    "button:has-text('Add to cart')",
                    "button[id*='addToCart']",
                ]:
                    try:
                        btn = page.locator(selector).first
                        if btn.is_visible(timeout=3000):
                            add_btn = btn
                            print(f"  Found cart button: {selector}")
                            break
                    except Exception:
                        continue

                if not add_btn:
                    send_discord(f"⚠️ Auto-buyer: couldn't find any buy button for **{name}** — may be sold out")
                    print("  Could not find any buy button — product may have sold out")
                    ctx.close()
                    return

                page.wait_for_timeout(random.randint(500, 1000))
                print("  Clicking Add to Cart...")
                add_btn.click()
                page.wait_for_timeout(random.randint(500, 1000))

                # Go to cart
                for selector in [
                    "button:has-text('Go to cart')",
                    "a:has-text('Go to cart')",
                    "a:has-text('View cart')",
                    "a[data-test='cart-link']",
                ]:
                    try:
                        el = page.locator(selector).first
                        if el.is_visible(timeout=2000):
                            page.wait_for_timeout(random.randint(500, 1000))
                            el.click()
                            break
                    except Exception:
                        continue
                else:
                    page.goto("https://www.target.com/cart", wait_until="domcontentloaded", timeout=15000)

                page.wait_for_timeout(random.randint(500, 1000))

                # Proceed to checkout
                for selector in [
                    "button[data-test='checkout-button']",
                    "a[data-test='checkout-button']",
                    "button:has-text('Check out')",
                    "button:has-text('Checkout')",
                ]:
                    try:
                        btn = page.locator(selector).first
                        if btn.is_visible(timeout=3000):
                            page.wait_for_timeout(random.randint(500, 1000))
                            btn.click()
                            break
                    except Exception:
                        continue

                page.wait_for_timeout(random.randint(500, 1000))

            # Place the order
            for selector in [
                "button[data-test='placeOrderButton']",
                "button:has-text('Place your order')",
                "button:has-text('Place order')",
                "button[data-test='place-order-button']",
            ]:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=3000):
                        page.wait_for_timeout(random.randint(500, 1000))
                        print(f"  Placing order via: {selector}")
                        btn.click()
                        break
                except Exception:
                    continue

            page.wait_for_timeout(2000)
            send_discord(
                f"✅ **Auto-buyer: ORDER PLACED!**\n"
                f"**{name}**\n"
                f"Check your email for confirmation!"
            )
            print("\n  ✅ ORDER PLACED — check your email!\n")
            page.wait_for_timeout(5000)

        except Exception as e:
            send_discord(f"⚠️ Auto-buyer error for **{name}**: {e}")
            print(f"  Error during checkout: {e}")

        ctx.close()


def main():
    print("=" * 55)
    print("  Target Auto-Buyer — Chaos Rising")
    print(f"  Watching: {', '.join(AUTO_BUY_TCINS.values())}")
    print("  Polling every 30 seconds. Ctrl+C to stop.")
    print("=" * 55)
    send_discord("🤖 **Auto-buyer is running** — watching for Chaos Rising at Target")

    try:
        while True:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{now}] Checking Target...")
            hits = check_stock()
            if not hits:
                print(f"  Out of stock — next check in 30s")
            for tcin, name, buy_url in hits:
                run_checkout(tcin, name, buy_url)
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n  Stopped.")
        send_discord("🤖 Auto-buyer stopped.")


if __name__ == "__main__":
    main()
