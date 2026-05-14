#!/usr/bin/env python3
import html
import json
import os
import sys
import warnings
from datetime import datetime

import re
import time

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cf

warnings.filterwarnings("ignore")

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://discord.com/api/webhooks/1497888304836640798/hbydxuJkfCTwqTWGqKfOu7ifqyN7pFPtTAT3xxGLzIcWgU7fg-Ie9yi5oo1tFB_01hzV")
STATE_FILE = os.path.join(os.path.dirname(__file__), "seen_products.json")

TARGET_API_KEY = os.environ.get("TARGET_API_KEY", "9f36aeafbe60771e321a7cc95a78140772ab3e96")
TARGET_ZIP = "95122"

# All 5 Target stores near 95122
TARGET_STORES = {
    "1984": "San Jose Story Road",
    "2238": "San Jose East",
    "1426": "San Jose Capitol",
    "2281": "San Jose Central",
    "2088": "San Jose College Park",
}

# Booster packs + ETBs + booster bundles near 95122
TARGET_TCINS = [
    # Booster packs / blisters / bundles
    "1001304528","1001148312","94300067","93859728","1001190585",
    "1001148311","92340902","1004842210","93859727","1006059992",
    "94300074","1001148307","1006188659","1001632615","1003557564",
    "1003375188","1004842211","1007155451","1003557552","1006893009",
    "94300069","1003298511","1007155449","1003298513",
    # Destined Rivals (new)
    "94300082","1006512287","1006512288","1004026208",
    # Journey Together (new)
    "1002957621","1002957625","1003007312",
    # Mega Evolution — blisters & bundles (new)
    "94681766","94681782","94681786",
    "94884511",                          # Phantasmal Flames blister Sneasel
    "95230446","95230447",               # Perfect Order blister & bundle
    "95298172",                          # Chaos Rising bundle
    "1006274802",                        # ME1 blister Golduck
    # Elite Trainer Boxes + sets
    "93504915","93565629","1010669487","1002893312",
    "1004842209","1004021933","1000174443","1005019724","93565630",
    "1001373732","1001193702","93803439","1008746912","1002908306",
    "94484578","1009003207","1010767187","1001632618","1010583462",
    "1006188618","1007819055","94300072","93565639","94794595",
    "1001095458","1004842404","1001373733","1003670472","93566842",
    # Mega Evolution ETBs (new)
    "94681776","94681784",               # ME1 ETBs Lucario & Gardevoir
    "95082118","1010148053",             # Ascended Heroes ETBs
    "1009871732",                        # Ascended Heroes PC ETB
    "95230445",                          # Perfect Order ETB
    "95267143",                          # Chaos Rising ETB
    "94860231",                          # Phantasmal Flames ETB
    "1006188619",                        # ME1 ETB 2-pack set
]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_discord(message):
    try:
        r = requests.post(WEBHOOK_URL, json={"content": message, "username": "Pokebot"}, timeout=10)
        return r.status_code == 204
    except Exception:
        return False


def notify(name, store, url, price="", is_local=False):
    location = "local store" if is_local else "online"
    price_str = f" — **{price}**" if price else ""
    now = datetime.now().strftime("%I:%M %p")
    send_discord(
        f"**RESTOCK** [{now}] {location}\n"
        f"**{html.unescape(name)}**{price_str}\n"
        f"Store: {store}\n{url}"
    )
    print(f"  [ALERT] {name[:55]} @ {store} ({location})")


PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

_CARD_KEYWORDS = {
    "booster", "pack", "trainer box", "tcg", "trading card", "expansion",
    "bundle", "blister", "collection box", "tin", "elite trainer", "promo",
    "card game", "booster box",
}
_NON_CARD_KEYWORDS = {
    "action figure", "display case", "plush", "stuffed", "surprise attack",
    "articulated", "figurine", "buildable", "vinyl", "doll", "toy",
}

def is_card_product(name):
    n = name.lower()
    if any(kw in n for kw in _NON_CARD_KEYWORDS):
        return False
    return any(kw in n for kw in _CARD_KEYWORDS)


def is_sold_by_target(buy_url):
    """Returns True if Target is the direct seller, False if it's a Target+ marketplace seller."""
    try:
        r = cf.get(buy_url, headers=PAGE_HEADERS, impersonate="safari17_0", timeout=15)
        text = BeautifulSoup(r.text, "html.parser").get_text()
        if re.search(r"Sold\s*(?:&|and)\s*shipped\s*by\s+\w", text, re.IGNORECASE):
            return False
        return True
    except Exception:
        return True  # assume Target's if we can't check


# ── Target ────────────────────────────────────────────────────────────────────

def _fetch_fulfillment(tcins, store_id, headers):
    url = (
        f"https://redsky.target.com/redsky_aggregations/v1/web/product_summary_with_fulfillment_v1"
        f"?key={TARGET_API_KEY}&tcins={'%2C'.join(tcins)}"
        f"&store_id={store_id}&zip={TARGET_ZIP}&state=CA"
        f"&latitude=37.290&longitude=-121.900"
        f"&scheduled_delivery_store_id={store_id}"
        f"&paid_membership=false&base_membership=false&card_membership=false"
        f"&required_store_id={store_id}&skip_price_promo=true&channel=WEB"
    )
    r = cf.get(url, headers=headers, impersonate="chrome120", timeout=20)
    if r.ok:
        return r.json().get("data", {}).get("product_summaries", [])
    print(f"  Target API returned {r.status_code} for store {store_id}")
    return []


def check_target(state, seed=False):
    print("Checking Target...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    primary_store = next(iter(TARGET_STORES))
    try:
        # Online availability — use any store ID, it reflects national inventory
        online_products = []
        for i in range(0, len(TARGET_TCINS), 24):
            online_products.extend(_fetch_fulfillment(TARGET_TCINS[i:i+24], primary_store, headers))

        # Build a lookup: tcin -> product info
        product_map = {}
        for p in online_products:
            tcin = p.get("tcin", "")
            product_map[tcin] = {
                "name": html.unescape(p.get("item", {}).get("product_description", {}).get("title", "")),
                "buy_url": p.get("item", {}).get("enrichment", {}).get("buy_url", ""),
                "ship_status": p.get("fulfillment", {}).get("shipping_options", {}).get("availability_status", ""),
            }

        new_alerts = 0

        # ── Online / ship-to-you (one check, works nationally) ──
        for tcin, prod in product_map.items():
            name, buy_url, ship_status = prod["name"], prod["buy_url"], prod["ship_status"]
            online_key = f"target_online_{tcin}"

            if not is_card_product(name):
                continue

            if not seed and ship_status == "IN_STOCK" and state.get(online_key) != "IN_STOCK":
                time.sleep(0.3)
                if is_sold_by_target(buy_url):
                    notify(name, "Target", buy_url, is_local=False)
                    new_alerts += 1
                else:
                    print(f"  [skipped 3rd party] {name[:50]}")

            state[online_key] = ship_status

        # ── Local store availability — check all 5 nearby stores ──
        for store_id, store_name in TARGET_STORES.items():
            store_products = []
            for i in range(0, len(TARGET_TCINS), 24):
                store_products.extend(_fetch_fulfillment(TARGET_TCINS[i:i+24], store_id, headers))

            for p in store_products:
                tcin = p.get("tcin", "")
                name = product_map.get(tcin, {}).get("name", p.get("item", {}).get("product_description", {}).get("title", ""))
                buy_url = product_map.get(tcin, {}).get("buy_url", p.get("item", {}).get("enrichment", {}).get("buy_url", ""))
                store_opts = p.get("fulfillment", {}).get("store_options", [])
                store_qty = store_opts[0].get("location_available_to_promise_quantity", 0) if store_opts else 0
                pickup = store_opts[0].get("order_pickup", {}).get("availability_status", "") if store_opts else ""

                if not is_card_product(name):
                    continue

                local_key = f"target_local_{store_id}_{tcin}"
                in_stock_locally = store_qty > 0 or pickup == "AVAILABLE"

                if not seed and in_stock_locally and not state.get(local_key):
                    time.sleep(0.3)
                    if is_sold_by_target(buy_url):
                        notify(name, f"Target {store_name}", buy_url, is_local=True)
                        new_alerts += 1
                    else:
                        print(f"  [skipped 3rd party] {name[:50]}")

                state[local_key] = in_stock_locally

        label = "seeded" if seed else f"{new_alerts} new alerts"
        print(f"  {len(product_map)} products checked across {len(TARGET_STORES)} stores, {label}")
    except Exception as e:
        print(f"  Target failed: {e}")

    return state


# ── Pokemon Center Sitemap ────────────────────────────────────────────────────

PC_SITEMAP_URL = "https://www.pokemoncenter.com/sitemaps/products.xml"
_PC_CARD_SLUGS = [
    "booster", "trainer-box", "booster-bundle", "booster-pack",
    "collection-box", "collection-chest", "blister", "mini-tin", "etb",
    "display-box", "battle-deck", "build-battle", "league-battle",
    "premium-collection", "sleeved-booster",
]
_PC_NON_CARD_SLUGS = [
    "plush", "pillow", "mug", "shirt", "hat", "bag", "backpack", "figure",
    "sticker", "pin", "poster", "postcard", "planter", "throw", "puma", "socks",
    # apparel & accessories that slipped through via Pokemon name false-positives
    "playmat", "tin-sign", "shorts", "jacket", "windbreaker", "keychain",
    "umbrella", "wallet", "towel", "blanket", "apron",
]

# Tins are TCG products (poke-ball-tin, stacking-tin, etc.) but "-tin" is too
# broad — it matches "tinkaton" (a Pokemon name). Check separately with a
# word-boundary pattern so only slug segments that ARE the word "tin" match.
import re as _re
_PC_TIN_RE = _re.compile(r'(?<![a-z])tin(?![a-z])')


def _is_pc_card_url(url):
    slug = url.lower()
    if any(kw in slug for kw in _PC_NON_CARD_SLUGS):
        return False
    if any(kw in slug for kw in _PC_CARD_SLUGS):
        return True
    return bool(_PC_TIN_RE.search(slug))


def _slug_to_name(url):
    """Turn a product URL slug into a readable title."""
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"^pokemon-tcg-", "", slug)
    return slug.replace("-", " ").title()


def check_pokemoncenter(state, seed=False):
    print("Checking Pokemon Center sitemap...")
    try:
        r = cf.get(PC_SITEMAP_URL, impersonate="chrome120", timeout=20)
        if not r.ok:
            print(f"  Pokemon Center sitemap returned {r.status_code}")
            return state

        all_urls = re.findall(
            r"<loc>(https://www\.pokemoncenter\.com/product/[^<]+)</loc>", r.text
        )
        card_urls = [u for u in all_urls if _is_pc_card_url(u)]

        new_alerts = 0
        for url in card_urls:
            key = f"pc_{url}"
            if not seed and key not in state:
                name = _slug_to_name(url)
                send_discord(
                    f"**NEW at Pokemon Center** 🎴\n"
                    f"**{name}**\n"
                    f"Just appeared in their catalog — may be going on sale soon!\n{url}"
                )
                print(f"  [NEW] {name[:60]}")
                new_alerts += 1
            state[key] = True

        label = "seeded" if seed else f"{new_alerts} new products"
        print(f"  {len(card_urls)} TCG products in sitemap, {label}")
    except Exception as e:
        print(f"  Pokemon Center sitemap failed: {e}")

    return state


# ── Pokemon Center Restock Watch ─────────────────────────────────────────────

# Products to actively watch for restock — add any URL from pokemoncenter.com/product/...
PC_RESTOCK_WATCH = [
    # ── Destined Rivals ───────────────────────────────────────────────────────
    "https://www.pokemoncenter.com/product/100-10653/pokemon-tcg-scarlet-and-violet-destined-rivals-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/100-10638/pokemon-tcg-scarlet-and-violet-destined-rivals-booster-bundle-6-packs",
    "https://www.pokemoncenter.com/product/10-10157-101/pokemon-tcg-scarlet-and-violet-destined-rivals-booster-display-box-36-packs",
    "https://www.pokemoncenter.com/product/100-10636/pokemon-tcg-scarlet-and-violet-destined-rivals-3-booster-packs-and-zebstrika-promo-card",
    "https://www.pokemoncenter.com/product/100-10637/pokemon-tcg-scarlet-and-violet-destined-rivals-3-booster-packs-and-kangaskhan-promo-card",
    "https://www.pokemoncenter.com/product/100-10623/pokemon-tcg-scarlet-and-violet-destined-rivals-sleeved-booster-pack-10-cards",
    # ── Journey Together ──────────────────────────────────────────────────────
    "https://www.pokemoncenter.com/product/100-10356/pokemon-tcg-scarlet-and-violet-journey-together-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/100-10341/pokemon-tcg-scarlet-and-violet-journey-together-booster-bundle-6-packs",
    "https://www.pokemoncenter.com/product/10-10125-102/pokemon-tcg-scarlet-and-violet-journey-together-enhanced-booster-display-box-36-packs-and-1-promo-card",
    "https://www.pokemoncenter.com/product/100-10326/pokemon-tcg-scarlet-and-violet-journey-together-sleeved-booster-pack-10-cards",
    # ── Prismatic Evolutions ─────────────────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10025-101/pokemon-tcg-scarlet-and-violet-prismatic-evolutions-booster-bundle-6-packs",
    # ── Mega Evolution — Chaos Rising ─────────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10407-119/pokemon-tcg-mega-evolution-chaos-rising-booster-display-box-36-packs",
    "https://www.pokemoncenter.com/product/10-10399-112/pokemon-tcg-mega-evolution-chaos-rising-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/10-10403-109/pokemon-tcg-mega-evolution-chaos-rising-booster-bundle",
    # ── Mega Evolution — Perfect Order ────────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10380-119/pokemon-tcg-mega-evolution-perfect-order-booster-display-box-36-packs",
    "https://www.pokemoncenter.com/product/10-10372-109/pokemon-tcg-mega-evolution-perfect-order-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/10-10377-109/pokemon-tcg-mega-evolution-perfect-order-booster-bundle-6-packs",
    # ── Mega Evolution — Ascended Heroes ─────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10315-108/pokemon-tcg-mega-evolution-ascended-heroes-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/10-10311-114/pokemon-tcg-mega-evolution-ascended-heroes-booster-bundle-6-packs",
    # ── Mega Evolution — Phantasmal Flames ───────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10190-119/pokemon-tcg-mega-evolution-phantasmal-flames-booster-display-box-36-packs",
    "https://www.pokemoncenter.com/product/10-10186-109/pokemon-tcg-mega-evolution-phantasmal-flames-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/10-10191-109/pokemon-tcg-mega-evolution-phantasmal-flames-booster-bundle-6-packs",
    "https://www.pokemoncenter.com/product/10-10187-108/pokemon-tcg-mega-evolution-phantasmal-flames-3-booster-packs-and-sneasel-promo-card",
    # ── Chaos Rising — extras ─────────────────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10401-101/pokemon-tcg-mega-evolution-chaos-rising-build-battle-box",
    # ── Special & Premium products ────────────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10408-101/pokemon-tcg-mega-zygarde-ex-premium-collection",
    "https://www.pokemoncenter.com/product/10-10360-101/pokemon-tcg-mega-lucario-ex-league-battle-deck",
    "https://www.pokemoncenter.com/product/10-10394-108/pokemon-tcg-pokemon-day-2026-collection",
    # ── First Partner Illustration Collections ────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10058-101/pokemon-tcg-first-partner-illustration-collection-series-1",
    "https://www.pokemoncenter.com/product/10-10058-102/pokemon-tcg-first-partner-illustration-collection-series-2",
]


def _pc_stock_status(url):
    """Returns 'IN_STOCK', 'OUT_OF_STOCK', or None if unknown."""
    try:
        r = cf.get(url, impersonate="chrome120", timeout=15)
        if not r.ok:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # JSON-LD structured data is in the raw HTML (not JS-rendered)
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                avail = offers.get("availability", "")
                if "InStock" in avail:
                    return "IN_STOCK"
                if "OutOfStock" in avail or "SoldOut" in avail or "Discontinued" in avail:
                    return "OUT_OF_STOCK"
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        # Fallback: plain text signals
        text = soup.get_text(" ", strip=True)
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK"
        if re.search(r"\b(Out of Stock|Sold Out|Notify Me)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK"
        return None
    except Exception:
        return None


def check_pokemoncenter_restock(state, seed=False):
    print("Checking Pokemon Center restock watch list...")
    new_alerts = 0
    for url in PC_RESTOCK_WATCH:
        key = f"pc_stock_{url}"
        status = _pc_stock_status(url)
        if status is None:
            time.sleep(0.5)
            continue
        prev = state.get(key)
        if not seed and status == "IN_STOCK" and prev != "IN_STOCK":
            name = _slug_to_name(url)
            send_discord(
                f"**RESTOCK at Pokemon Center!**\n"
                f"**{name}**\n"
                f"Back in stock — buy directly at retail price!\n{url}"
            )
            print(f"  [RESTOCK] {name[:60]}")
            new_alerts += 1
        state[key] = status
        time.sleep(0.5)
    label = "seeded" if seed else f"{new_alerts} restocks found"
    print(f"  {len(PC_RESTOCK_WATCH)} products checked, {label}")
    return state


# ── Status ────────────────────────────────────────────────────────────────────

def run_status():
    print("Fetching current Target inventory status...")
    api_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }

    primary_store = next(iter(TARGET_STORES))
    products = []
    for i in range(0, len(TARGET_TCINS), 24):
        products.extend(_fetch_fulfillment(TARGET_TCINS[i:i+24], primary_store, api_headers))

    # Online check (no seller verification — status is informational only)
    online_available = []
    for p in products:
        ship = p.get("fulfillment", {}).get("shipping_options", {}).get("availability_status", "")
        if ship == "IN_STOCK":
            name = html.unescape(p.get("item", {}).get("product_description", {}).get("title", ""))
            if not is_card_product(name):
                continue
            buy_url = p.get("item", {}).get("enrichment", {}).get("buy_url", "")
            online_available.append((name, buy_url))

    # Local store check
    store_stock = {}
    for store_id, store_name in TARGET_STORES.items():
        store_products = []
        for i in range(0, len(TARGET_TCINS), 24):
            store_products.extend(_fetch_fulfillment(TARGET_TCINS[i:i+24], store_id, api_headers))
        hits = []
        for p in store_products:
            opts = p.get("fulfillment", {}).get("store_options", [])
            qty = opts[0].get("location_available_to_promise_quantity", 0) if opts else 0
            pickup = opts[0].get("order_pickup", {}).get("availability_status", "") if opts else ""
            if qty > 0 or pickup == "AVAILABLE":
                name = html.unescape(p.get("item", {}).get("product_description", {}).get("title", ""))
                if not is_card_product(name):
                    continue
                hits.append((name, int(qty)))
        if hits:
            store_stock[store_name] = hits

    # Build Discord message
    now = datetime.now().strftime("%I:%M %p")
    lines = [f"**Target Status Report** [{now}]"]

    lines.append("\n**Online (ship to you):**")
    if online_available:
        for name, url in online_available:
            lines.append(f"✅ [{name[:60]}]({url})")
    else:
        lines.append("❌ No cards sold by Target directly right now")

    lines.append("\n**Local Stores:**")
    if store_stock:
        for store_name, items in store_stock.items():
            lines.append(f"📍 **{store_name}**")
            for name, qty in items:
                lines.append(f"  ✅ {name[:55]} (qty: {qty})")
    else:
        lines.append("❌ All 5 nearby stores empty")

    message = "\n".join(lines)
    send_discord(message)
    print("Status sent to Discord!")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--test" in sys.argv:
        print("Sending test Discord message...")
        ok = send_discord(
            "**Pokebot is online!**\n"
            f"Monitoring Pokemon cards near ZIP {TARGET_ZIP}\n"
            "Checking: Target (booster packs + ETBs)"
        )
        print("Discord webhook works! Check your server." if ok else "Discord webhook FAILED")
        return

    if "--status" in sys.argv:
        run_status()
        return

    first_run = not os.path.exists(STATE_FILE)
    seed = first_run or "--seed" in sys.argv
    if seed:
        print("First run — seeding state without sending alerts...")

    state = load_state()
    state = check_target(state, seed=seed)
    state = check_pokemoncenter(state, seed=seed)
    state = check_pokemoncenter_restock(state, seed=seed)
    save_state(state)
    print("Done." if not seed else "Done. Run again to start receiving alerts.")


if __name__ == "__main__":
    main()
