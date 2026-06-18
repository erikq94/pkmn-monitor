#!/usr/bin/env python3
import html
import json
import os
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, date, timezone

import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cf

warnings.filterwarnings("ignore")

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
STATE_FILE          = os.path.join(os.path.dirname(__file__), "seen_products.json")
HISTORY_FILE        = os.path.join(os.path.dirname(__file__), "restock_history.json")
DYNAMIC_TCINS_FILE  = os.path.join(os.path.dirname(__file__), "dynamic_tcins.json")
DYNAMIC_PC_URLS_FILE = os.path.join(os.path.dirname(__file__), "dynamic_pc_urls.json")
WALMART_LOG_FILE    = os.path.join(os.path.dirname(__file__), "walmart_log.json")
DYNAMIC_MC_URLS_FILE = os.path.join(os.path.dirname(__file__), "dynamic_mc_urls.json")

TARGET_API_KEY = os.environ["TARGET_API_KEY"]
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
    "1004021935",                        # Destined Rivals Display 2-pack
    # Journey Together (new)
    "1002957621","1002957625","1003007312",
    # Black Bolt / White Flare (SV10.5 — Unova sets)
    "1004842250",                        # BB+WF Art Set (2 packs)
    "1004842207",                        # White Flare Booster Bundle (6 packs)
    "1008355524",                        # Unova Heavy Hitters at Target
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
    "1009318827",                        # Ascended Heroes Booster Pack
    "1009871732",                        # Ascended Heroes PC ETB
    "95230445",                          # Perfect Order ETB
    "1011318040",                        # Perfect Order ETB (solo listing)
    "1010669655",                        # Perfect Order Booster Bundle 2-pack
    "1010669398",                        # Perfect Order Art Set (4 packs)
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


# ── Restock history ───────────────────────────────────────────────────────────

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {"restocks": [], "last_summary": None}


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


_history_lock = threading.Lock()

def log_restock(history, retailer, name, store="Online", qty=None):
    if history is None:
        return
    now = datetime.now()
    try:
        qty = int(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty = None
    entry = {
        "retailer": retailer,
        "name": name[:80],
        "store": store,
        "timestamp": now.isoformat(),
        "day": now.strftime("%A"),
        "hour": now.hour,
        "qty": qty,
    }
    with _history_lock:
        history["restocks"].append(entry)


def send_pattern_summary(history):
    """Send a Discord message showing restock time patterns from the last 30 days."""
    restocks = history.get("restocks", [])
    cutoff = datetime.now().timestamp() - 30 * 86400
    recent = []
    for r in restocks:
        try:
            if datetime.fromisoformat(r["timestamp"]).timestamp() > cutoff:
                recent.append(r)
        except (KeyError, ValueError):
            continue

    if not recent:
        send_discord("📊 **Restock Patterns** — No restocks logged in the last 30 days.")
        history["last_summary"] = datetime.now().isoformat()
        return

    groups = defaultdict(list)
    qtys = defaultdict(list)
    for r in recent:
        label = f"{r['retailer']} — {r.get('store', 'Online')}"
        groups[label].append((r["day"], r["hour"]))
        if isinstance(r.get("qty"), int) and r["qty"] > 0:
            qtys[label].append(r["qty"])

    lines = [f"📊 **Restock Patterns — last 30 days** ({len(recent)} total restocks)"]
    for location, times in sorted(groups.items()):
        counts = Counter(times)
        parts = []
        for (day, hour), n in sorted(counts.items(), key=lambda x: -x[1]):
            ampm = "am" if hour < 12 else "pm"
            h = hour % 12 or 12
            parts.append(f"{day[:3]} {h}{ampm}" + (f" ×{n}" if n > 1 else ""))
        line = f"**{location}**: {', '.join(parts)}"
        location_qtys = qtys.get(location)
        if location_qtys:
            avg = round(sum(location_qtys) / len(location_qtys))
            line += f" — typically ~{avg} units (range {min(location_qtys)}–{max(location_qtys)})"
        lines.append(line)

    send_discord("\n".join(lines))
    history["last_summary"] = datetime.now().isoformat()


# ── Dynamic TCIN list (auto-discovered from Target search) ────────────────────

def load_dynamic_tcins():
    if os.path.exists(DYNAMIC_TCINS_FILE):
        with open(DYNAMIC_TCINS_FILE) as f:
            return json.load(f)
    return []


def save_dynamic_tcins(tcins):
    with open(DYNAMIC_TCINS_FILE, "w") as f:
        json.dump(tcins, f, indent=2)


def load_dynamic_pc_urls():
    if os.path.exists(DYNAMIC_PC_URLS_FILE):
        with open(DYNAMIC_PC_URLS_FILE) as f:
            return json.load(f)
    return []


def save_dynamic_pc_urls(urls):
    with open(DYNAMIC_PC_URLS_FILE, "w") as f:
        json.dump(urls, f, indent=2)


def send_discord(message):
    try:
        r = requests.post(WEBHOOK_URL, json={"content": message, "username": "Pokebot"}, timeout=10)
        return r.status_code == 204
    except Exception:
        return False


def qty_line(qty):
    """Formats a stock-quantity line for alerts. Returns '' when qty is unknown."""
    if qty is None:
        return ""
    try:
        n = int(qty)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n <= 5:
        return f"🔴 **Only {n} left** — grab it fast!\n"
    if n <= 20:
        return f"🟡 **{n} available**\n"
    return f"🟢 **{n}+ available**\n"


_QTY_LEFT_RE = re.compile(r"only\s+(\d+)\s+left", re.IGNORECASE)

def _qty_left(text):
    """Extracts a low-stock 'Only N left' count from page text, or None."""
    m = _QTY_LEFT_RE.search(text)
    return int(m.group(1)) if m else None


def notify(name, store, url, price="", is_local=False, qty=None):
    location = "local store" if is_local else "online"
    price_str = f" — **{price}**" if price else ""
    send_discord(
        f"@everyone\n"
        f"**RESTOCK** {location}\n"
        f"**{html.unescape(name)}**{price_str}\n"
        f"{qty_line(qty)}"
        f"Store: {store}\n{url}"
    )
    qty_log = f" [qty={qty}]" if qty is not None else ""
    print(f"  [ALERT] {name[:55]} @ {store} ({location}){qty_log}")


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
    "binder", "portfolio", "card sleeve", "card protector",
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


# ── Target discovery ─────────────────────────────────────────────────────────

def discover_target_tcins(state, dynamic_tcins):
    """Search Target's catalog for Pokemon TCG products not in our watch list."""
    print("Scanning Target for new Pokemon TCG products...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    all_known = set(TARGET_TCINS) | set(dynamic_tcins)
    primary_store = next(iter(TARGET_STORES))
    store_ids = "%2C".join(TARGET_STORES.keys())
    new_found = []

    offset = 0
    count = 24
    for _ in range(4):  # up to 4 pages = 96 products
        url = (
            f"https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
            f"?key={TARGET_API_KEY}&channel=WEB&count={count}&offset={offset}"
            f"&default_purchasability_filter=false&include_sponsored=false"
            f"&keyword=pokemon+trading+card+game"
            f"&pricing_store_id={primary_store}&store_ids={store_ids}"
            f"&zip={TARGET_ZIP}&state=CA&latitude=37.290&longitude=-121.900"
        )
        try:
            r = cf.get(url, headers=headers, impersonate="chrome120", timeout=20)
            if not r.ok:
                print(f"  Target search returned {r.status_code}")
                break
            data = r.json()
            products = data.get("data", {}).get("search", {}).get("products", [])
            if not products:
                break
            for p in products:
                tcin = p.get("tcin", "")
                name = html.unescape(
                    p.get("item", {}).get("product_description", {}).get("title", "")
                )
                buy_url = p.get("item", {}).get("enrichment", {}).get("buy_url", "")
                if not tcin or not is_card_product(name):
                    continue
                if tcin not in all_known and not state.get(f"discovered_{tcin}"):
                    new_found.append((tcin, name, buy_url))
                    state[f"discovered_{tcin}"] = True
                    dynamic_tcins.append(tcin)
                    all_known.add(tcin)
            total = data.get("data", {}).get("search", {}).get("total_results", 0)
            offset += count
            if offset >= total:
                break
            time.sleep(random.uniform(1, 2))
        except Exception as e:
            print(f"  Target discovery error: {e}")
            break

    for tcin, name, buy_url in new_found:
        send_discord(
            f"🆕 **New Product at Target!**\n"
            f"**{name}**\n"
            f"Just appeared in Target's catalog — now being monitored.\n{buy_url}"
        )
        print(f"  [NEW TCIN] {tcin} — {name[:55]}")

    label = f"{len(new_found)} new TCINs" if new_found else "no new TCINs"
    print(f"  Target discovery: {label}")
    return state, dynamic_tcins


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


def check_target(state, seed=False, history=None):
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
                "ship_qty": p.get("fulfillment", {}).get("shipping_options", {}).get("available_to_promise_quantity"),
                "sold_out": p.get("fulfillment", {}).get("sold_out"),
            }

        new_alerts = 0

        # ── Online / ship-to-you (one check, works nationally) ──
        for tcin, prod in product_map.items():
            name, buy_url, ship_status = prod["name"], prod["buy_url"], prod["ship_status"]
            sold_out = prod["sold_out"]
            online_key = f"target_online_{tcin}"
            sold_out_key = f"target_sold_out_{tcin}"

            if not is_card_product(name):
                continue

            if not seed and ship_status == "IN_STOCK" and state.get(online_key) != "IN_STOCK":
                time.sleep(random.uniform(0.5, 2))
                if is_sold_by_target(buy_url):
                    notify(name, "Target", buy_url, is_local=False, qty=prod.get("ship_qty"))
                    log_restock(history, "Target", name, "Online", qty=prod.get("ship_qty"))
                    new_alerts += 1
                else:
                    print(f"  [skipped 3rd party] {name[:50]}")

            # sold_out False → early restock signal (fires only on True→False transition)
            if not seed and state.get(sold_out_key) is True and sold_out is False:
                send_discord(
                    f"👀 **Target restock signal** — inventory unlocking\n"
                    f"**{name}**\n"
                    f"sold_out flipped False — drop likely within minutes\n{buy_url}"
                )
                print(f"  [sold_out signal] {name[:55]}")

            state[online_key] = ship_status
            if sold_out is not None:
                state[sold_out_key] = sold_out

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
                    time.sleep(random.uniform(0.5, 2))
                    if is_sold_by_target(buy_url):
                        notify(name, f"Target {store_name}", buy_url, is_local=True, qty=int(store_qty) if store_qty else None)
                        log_restock(history, "Target", name, store_name, qty=int(store_qty) if store_qty else None)
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


def check_pokemoncenter_site_queue(state, seed=False):
    """Probes the PC homepage for Incapsula/Imperva waiting room signals.
    Fires a site-wide @everyone alert the moment the queue opens — before any product can be checked."""
    key = "pc_site_queue"
    try:
        r = cf.get("https://www.pokemoncenter.com/", impersonate="chrome120", timeout=15, allow_redirects=True)
        text = r.text
        is_queue = (
            "_Incapsula_Resource" in text
            or "queue-it.net" in text
            or "waiting room" in text.lower()
            or "virtual queue" in text.lower()
        )
        prev = state.get(key)
        if not seed and is_queue and prev != "QUEUE":
            send_discord(
                f"@everyone\n"
                f"🚨 **POKEMON CENTER QUEUE IS OPEN!** 🚨\n"
                f"The entire site has a virtual waiting room — join NOW before your position gets worse!\n"
                f"https://www.pokemoncenter.com/"
            )
            print("  [PC SITE QUEUE] Waiting room detected on homepage — alert sent")
        elif not is_queue and prev == "QUEUE":
            print("  [PC SITE QUEUE] Queue cleared")
        state[key] = "QUEUE" if is_queue else "OPEN"
    except Exception as e:
        print(f"  PC site queue probe failed: {e}")
    return state


def check_pokemoncenter(state, seed=False, dynamic_pc_urls=None):
    if dynamic_pc_urls is None:
        dynamic_pc_urls = []
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
                    f"🆕 **New at Pokemon Center** 🎴\n"
                    f"**{name}**\n"
                    f"Just appeared in the catalog — now monitoring for restock!\n{url}"
                )
                print(f"  [NEW] {name[:60]}")
                new_alerts += 1
                # Auto-add to dynamic restock watch so stock is checked next run
                if url not in PC_RESTOCK_WATCH and url not in dynamic_pc_urls:
                    dynamic_pc_urls.append(url)
            state[key] = True

        save_dynamic_pc_urls(dynamic_pc_urls)
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
    "https://www.pokemoncenter.com/product/10-10022-102/pokemon-tcg-scarlet-and-violet-prismatic-evolutions-tech-sticker-collection-leafeon",
    "https://www.pokemoncenter.com/product/10-10022-103/pokemon-tcg-scarlet-and-violet-prismatic-evolutions-tech-sticker-collection-glaceon",
    "https://www.pokemoncenter.com/product/10-10022-104/pokemon-tcg-scarlet-and-violet-prismatic-evolutions-tech-sticker-collection-sylveon",
    # ── Black Bolt / White Flare ──────────────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10037-117/pokemon-tcg-scarlet-and-violet-white-flare-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/10-10037-118/pokemon-tcg-scarlet-and-violet-black-bolt-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/10-10035-115/pokemon-tcg-scarlet-and-violet-white-flare-booster-bundle-6-packs",
    "https://www.pokemoncenter.com/product/10-10115-113/pokemon-tcg-scarlet-and-violet-black-bolt-booster-bundle-6-packs",
    "https://www.pokemoncenter.com/product/10-10116-114/pokemon-tcg-scarlet-and-violet-white-flare-tech-sticker-collection",
    "https://www.pokemoncenter.com/product/10-10128-114/pokemon-tcg-scarlet-and-violet-black-bolt-tech-sticker-collection",
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
    "https://www.pokemoncenter.com/product/10-10314-121/pokemon-tcg-mega-evolution-ascended-heroes-tech-sticker-collection-charmander",
    "https://www.pokemoncenter.com/product/10-10314-122/pokemon-tcg-mega-evolution-ascended-heroes-tech-sticker-collection-gastly",
    # ── Mega Evolution — Phantasmal Flames ───────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10190-119/pokemon-tcg-mega-evolution-phantasmal-flames-booster-display-box-36-packs",
    "https://www.pokemoncenter.com/product/10-10186-109/pokemon-tcg-mega-evolution-phantasmal-flames-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/10-10191-109/pokemon-tcg-mega-evolution-phantasmal-flames-booster-bundle-6-packs",
    "https://www.pokemoncenter.com/product/10-10187-108/pokemon-tcg-mega-evolution-phantasmal-flames-3-booster-packs-and-sneasel-promo-card",
    "https://www.pokemoncenter.com/product/10-10187-114/pokemon-tcg-mega-evolution-phantasmal-flames-3-booster-packs-and-weavile-promo-card",
    # ── Mega Evolution — Base Set (ME1) ───────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10047-108/pokemon-tcg-mega-evolution-pokemon-center-elite-trainer-box-mega-lucario",
    "https://www.pokemoncenter.com/product/10-10047-120/pokemon-tcg-mega-evolution-pokemon-center-elite-trainer-box-mega-gardevoir",
    "https://www.pokemoncenter.com/product/10-10054-108/pokemon-tcg-mega-evolution-booster-bundle-6-packs",
    "https://www.pokemoncenter.com/product/10-10057-127/pokemon-tcg-mega-evolution-enhanced-booster-display-box-36-packs-and-1-promo-card",
    "https://www.pokemoncenter.com/product/10-10050-108/pokemon-tcg-mega-evolution-3-booster-packs-and-psyduck-promo-card",
    "https://www.pokemoncenter.com/product/10-10050-114/pokemon-tcg-mega-evolution-3-booster-packs-and-golduck-promo-card",
    # ── Chaos Rising — extras ─────────────────────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10401-101/pokemon-tcg-mega-evolution-chaos-rising-build-battle-box",
    # ── Special & Premium products ────────────────────────────────────────────
    "https://www.pokemoncenter.com/product/290-85466/pokemon-tcg-scarlet-and-violet-151-pokemon-center-elite-trainer-box",
    "https://www.pokemoncenter.com/product/100-10424/pokemon-tcg-cynthia-s-garchomp-ex-premium-collection",
    "https://www.pokemoncenter.com/product/100-10431/pokemon-tcg-iono-s-bellibolt-ex-premium-collection",
    "https://www.pokemoncenter.com/product/10-10408-101/pokemon-tcg-mega-zygarde-ex-premium-collection",
    "https://www.pokemoncenter.com/product/10-10360-101/pokemon-tcg-mega-lucario-ex-league-battle-deck",
    "https://www.pokemoncenter.com/product/10-10394-108/pokemon-tcg-pokemon-day-2026-collection",
    # ── First Partner Illustration Collections ────────────────────────────────
    "https://www.pokemoncenter.com/product/10-10058-101/pokemon-tcg-first-partner-illustration-collection-series-1",
    "https://www.pokemoncenter.com/product/10-10058-102/pokemon-tcg-first-partner-illustration-collection-series-2",
]


def _pc_stock_status(url):
    """Returns 'IN_STOCK', 'OUT_OF_STOCK', 'QUEUE', or None if unknown."""
    try:
        r = cf.get(url, impersonate="chrome120", timeout=15, allow_redirects=True)
        if not r.ok:
            return None
        final_url = str(r.url)
        # Incapsula/Imperva waiting room — gates the entire PC site during high-traffic drops
        if "_Incapsula_Resource" in r.text or "incapsula" in r.text.lower():
            return "QUEUE"
        if "queue-it.net" in final_url or "queue-it.net" in r.text:
            return "QUEUE"
        soup = BeautifulSoup(r.text, "html.parser")
        text_quick = soup.get_text(" ", strip=True)
        if "waiting room" in text_quick.lower() or "virtual queue" in text_quick.lower():
            return "QUEUE"
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


def check_pokemoncenter_restock(state, seed=False, history=None, dynamic_pc_urls=None):
    all_urls = list(PC_RESTOCK_WATCH) + [u for u in (dynamic_pc_urls or []) if u not in PC_RESTOCK_WATCH]
    print("Checking Pokemon Center restock watch list...")
    new_alerts = 0
    for url in all_urls:
        key = f"pc_stock_{url}"
        status = _pc_stock_status(url)
        if status is None:
            time.sleep(0.5)
            continue
        prev = state.get(key)
        name = _slug_to_name(url)
        if not seed and status == "QUEUE" and prev != "QUEUE":
            send_discord(
                f"@everyone\n"
                f"🚨 **POKEMON CENTER QUEUE IS OPEN!** 🚨\n"
                f"**{name}**\n"
                f"Virtual waiting room is live — join NOW!\n{url}"
            )
            print(f"  [QUEUE OPEN] {name[:60]}")
            new_alerts += 1
        elif not seed and status == "IN_STOCK" and prev != "IN_STOCK":
            send_discord(
                f"@everyone\n"
                f"**RESTOCK at Pokemon Center!**\n"
                f"**{name}**\n"
                f"Back in stock — buy directly at retail price!\n{url}"
            )
            log_restock(history, "Pokemon Center", name)
            print(f"  [RESTOCK] {name[:60]}")
            new_alerts += 1
        state[key] = status
        time.sleep(random.uniform(0.5, 2))
    label = "seeded" if seed else f"{new_alerts} restocks found"
    print(f"  {len(all_urls)} products checked, {label}")
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
            ship_qty = p.get("fulfillment", {}).get("shipping_options", {}).get("available_to_promise_quantity")
            online_available.append((name, buy_url, ship_qty))

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
    lines = ["**Target Status Report**"]

    lines.append("\n**Online (ship to you):**")
    if online_available:
        for name, url, ship_qty in online_available:
            qty_str = f" (qty: {ship_qty})" if ship_qty else ""
            lines.append(f"✅ [{name[:60]}]({url}){qty_str}")
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


# ── Costco ────────────────────────────────────────────────────────────────────

COSTCO_WATCH = [
    # Pokéball 6-Pack Tin Bundle — 18 booster packs (item 4000449856)
    "https://www.costco.com/pok%C3%A9mon-6-pack-poke-balls.product.4000449856.html",
    # Unova Heavy Hitters Premium Collection 2-pack — 12 packs (item 1943158)
    "https://www.costco.com/pok%C3%A9mon-unova-heavy-hitters-premium-collection.product.1943158.html",
    # Mega Charizard X ex Ultra Premium Collection 2-pack (item 1997714)
    "https://www.costco.com/pok%C3%A9mon-tcg-mega-charizard-x-ex-ultra-premium-collection-2-pack.product.1997714.html",
    # Charizard ex Super-Premium Collection (item 4000313298)
    "https://www.costco.com/pok%C3%A9mon-tcg:-charizard-ex-super-premium-collection.product.4000313298.html",
]


def _costco_stock_status(url):
    """Returns 'IN_STOCK', 'OUT_OF_STOCK', 'QUEUE', or None if unknown."""
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None
        # Queue-it detection — Costco redirects high-demand drops to a virtual waiting room
        final_url = str(r.url)
        if "queue-it.net" in final_url or "queue-it.net" in r.text:
            return "QUEUE"
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if "waiting room" in text.lower() or "virtual queue" in text.lower():
            return "QUEUE"
        # Akamai block — returns a tiny privacy page instead of product content
        if len(text) < 200:
            print(f"  [blocked by Akamai] {url[-50:]}")
            return None
        # JSON-LD structured data (most reliable)
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
                if "OutOfStock" in avail or "SoldOut" in avail:
                    return "OUT_OF_STOCK"
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK"
        if re.search(r"\b(Out of Stock|Sold Out)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK"
        # oos-overlay: "hide" class means the overlay is hidden = item IS in stock
        oos = soup.find(class_="oos-overlay")
        if oos:
            return "IN_STOCK" if "hide" in oos.get("class", []) else "OUT_OF_STOCK"
        return None
    except Exception:
        return None


def _costco_name(url):
    slug = url.split("/")[-1].split(".product.")[0]
    slug = re.sub(r"%[0-9a-fA-F]{2}", "", slug)
    return slug.replace("-", " ").replace(":", "").title()


def check_costco(state, seed=False, history=None):
    print("Checking Costco watch list...")
    new_alerts = 0
    for url in COSTCO_WATCH:
        key = f"costco_{url}"
        status = _costco_stock_status(url)
        if status is None:
            print(f"  [unknown] {_costco_name(url)[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        prev = state.get(key)
        name = _costco_name(url)
        if not seed and status == "QUEUE" and prev != "QUEUE":
            send_discord(
                f"@everyone\n"
                f"🚨 **COSTCO QUEUE IS OPEN!** 🚨\n"
                f"**{name}**\n"
                f"Virtual waiting room is live — join NOW before it fills up!\n{url}"
            )
            print(f"  [QUEUE OPEN] {name[:60]}")
            new_alerts += 1
        elif not seed and status == "IN_STOCK" and prev != "IN_STOCK":
            send_discord(
                f"@everyone\n"
                f"**RESTOCK at Costco!** 🎴\n"
                f"**{name}**\n"
                f"Available online now — also check the Costco app for local warehouse stock!\n{url}"
            )
            log_restock(history, "Costco", name)
            print(f"  [RESTOCK] {name[:60]}")
            new_alerts += 1
        else:
            print(f"  [{status}] {name[:55]}")
        state[key] = status
        time.sleep(random.uniform(1, 3))
    label = "seeded" if seed else f"{new_alerts} alerts sent"
    print(f"  {len(COSTCO_WATCH)} products checked, {label}")
    return state


# ── Sam's Club ───────────────────────────────────────────────────────────────

SAMSCLUB_WATCH = [
    # Prismatic Evolutions Super Premium Collection — drops May 26 10pm CST (Plus Members only, limit 2)
    "https://www.samsclub.com/ip/19170800669",
    # Surprise Box Booster Bundle
    "https://www.samsclub.com/ip/pokemon-surprise-box-booster-bundle/18933156288",
    # Binder + Poster Collection Booster Packs
    "https://www.samsclub.com/ip/pokemon-binder-poster-collection-booster-packs/19167901990",
]


def _samsclub_stock_status(url):
    """Returns 'IN_STOCK', 'COMING_SOON', 'OUT_OF_STOCK', 'QUEUE', or None."""
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None
        # Queue-it detection — same as Costco
        final_url = str(r.url)
        if "queue-it.net" in final_url or "queue-it.net" in r.text:
            return "QUEUE"
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if "waiting room" in text.lower() or "virtual queue" in text.lower():
            return "QUEUE"
        if len(text) < 500:
            print(f"  [blocked] {url.split('/')[-1][:40]}")
            return None
        # Coming Soon check
        if re.search(r"\bcoming soon\b", text, re.IGNORECASE):
            return "COMING_SOON"
        # JSON-LD structured data
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
                if "OutOfStock" in avail or "SoldOut" in avail:
                    return "OUT_OF_STOCK"
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK"
        if re.search(r"\b(Out of Stock|Sold Out|Not available)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK"
        return None
    except Exception:
        return None


def _samsclub_name(url):
    parts = url.rstrip("/").split("/")
    # URL may be /ip/slug/id or /ip/id — use slug if present
    for part in reversed(parts):
        if part.isdigit():
            continue
        if part == "ip":
            break
        slug = re.sub(r"^pokemon-?(tcg-|trading-card-game-)?", "", part, flags=re.IGNORECASE)
        return slug.replace("-", " ").title()
    return f"Sam's Club item {parts[-1]}"


# Known drop times in UTC — GitHub Actions runner is UTC
# 8:00 PM PDT = 03:00 UTC next day
SAMSCLUB_DROPS = {
    "https://www.samsclub.com/ip/19170800669": {
        "drop_utc": datetime(2026, 5, 27, 3, 0, 0, tzinfo=timezone.utc),
        "label": "8:00 PM Pacific",
        "note": "Plus Members only, limit 2",
    },
}


def _check_samsclub_reminders(state):
    now = datetime.now(timezone.utc)
    for url, info in SAMSCLUB_DROPS.items():
        drop = info["drop_utc"]
        name = _samsclub_name(url)
        minutes_until = (drop - now).total_seconds() / 60

        # ~15-minute warning: fire when between 10–20 min out (catches one 5-min cron tick)
        if 10 <= minutes_until <= 20:
            key = f"samsclub_reminder_15_{url}"
            if not state.get(key):
                send_discord(
                    f"⏰ **Dropping in ~15 minutes — Sam's Club** 🟠\n"
                    f"**{name}**\n"
                    f"{info['label']} — {info['note']}\n{url}"
                )
                state[key] = True
                print(f"  [REMINDER 15min] {name[:55]}")

        # Drop alert: fire within 5 minutes of drop time (before or after)
        if -5 <= minutes_until <= 5:
            key = f"samsclub_reminder_now_{url}"
            if not state.get(key):
                send_discord(
                    f"@everyone\n"
                    f"🚨 **DROPPING NOW — Sam's Club!** 🟠\n"
                    f"**{name}**\n"
                    f"{info['label']} — {info['note']}\n{url}"
                )
                state[key] = True
                print(f"  [DROPPING NOW] {name[:55]}")


def check_samsclub(state, seed=False, history=None):
    if not seed:
        _check_samsclub_reminders(state)
    print("Checking Sam's Club watch list...")
    new_alerts = 0
    for url in SAMSCLUB_WATCH:
        key = f"samsclub_{url}"
        status = _samsclub_stock_status(url)
        if status is None:
            print(f"  [unknown] {_samsclub_name(url)[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        prev = state.get(key)
        name = _samsclub_name(url)
        if not seed and status == "QUEUE" and prev != "QUEUE":
            send_discord(
                f"@everyone\n"
                f"🚨 **SAM'S CLUB QUEUE IS OPEN!** 🚨\n"
                f"**{name}**\n"
                f"Virtual waiting room is live — join NOW!\n{url}"
            )
            log_restock(history, "Sam's Club", name, "Online")
            print(f"  [QUEUE OPEN] {name[:60]}")
            new_alerts += 1
        elif not seed and status == "IN_STOCK" and prev != "IN_STOCK":
            send_discord(
                f"@everyone\n"
                f"**RESTOCK at Sam's Club!** 🟠\n"
                f"**{name}**\n"
                f"In stock online — Plus Members, limit 2!\n{url}"
            )
            log_restock(history, "Sam's Club", name, "Online")
            print(f"  [RESTOCK] {name[:60]}")
            new_alerts += 1
        elif not seed and status == "COMING_SOON" and prev != "COMING_SOON":
            send_discord(
                f"**Coming Soon at Sam's Club** 🟠\n"
                f"**{name}**\n"
                f"Page is live — dropping soon, stay ready!\n{url}"
            )
            print(f"  [COMING SOON] {name[:55]}")
            new_alerts += 1
        else:
            print(f"  [{status}] {name[:55]}")
        state[key] = status
        time.sleep(random.uniform(1, 3))
    label = "seeded" if seed else f"{new_alerts} alerts sent"
    print(f"  {len(SAMSCLUB_WATCH)} products checked, {label}")
    return state


# ── Best Buy ──────────────────────────────────────────────────────────────────

# Best Buy stores near 95122 — store IDs from stores.bestbuy.com URL slugs
BESTBUY_STORES = {
    "1423": "San Jose Curtner",
    "190":  "San Jose Almaden",
    "851":  "San Jose Stevens Creek",
}

BESTBUY_WATCH = [
    # ── Mega Evolution ────────────────────────────────────────────────────────
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-chaos-rising-elite-trainer-box/JJG2TL34RT",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-chaos-rising-booster-bundle/JJG2TL34H9",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-perfect-order-elite-trainer-box/JJG2TL3W86",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-perfect-order-booster-bundle/JJG2TL3QK2",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-ascended-heroes-booster-bundle/JJG2TL3JP8",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-pitch-black-elite-trainer-box/JJG2TL8J45",
    # "styles may vary" removed — random variant, not specific enough to alert on
    # ── Scarlet & Violet ──────────────────────────────────────────────────────
    "https://www.bestbuy.com/product/pokemon-trading-card-game-scarlet-violet-journey-together-booster-bundle-6-pk/JJG2TLCFST",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-scarlet-violet-prismatic-evolutions-elite-trainer-box/JJG2TLCW3L",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-scarlet-violet-prismatic-evolutions-booster-bundle/JJG2TL23JK",
]


def _bestbuy_sku_from_text(text):
    m = re.search(r'\bSKU[:\s]+(\d{6,8})\b', text)
    return m.group(1) if m else None


def _bestbuy_store_status(sku, store_id):
    """Checks Best Buy's tcfb button-state API for a specific store. Returns 'IN_STOCK', 'INVITE', or 'OUT_OF_STOCK'."""
    try:
        path = [
            "shop", "buttonstate", "v5", "item", "skus", int(sku),
            "conditions", "NONE", "destinationZipCode", TARGET_ZIP,
            "storeId", store_id, "context", "cyp", "addAll", "false"
        ]
        r = cf.get(
            "https://www.bestbuy.com/api/tcfb/model.json",
            params={"paths": json.dumps([path]), "method": "get"},
            impersonate="chrome124", timeout=15
        )
        if not r.ok:
            return None
        m = re.search(r'"buttonState"\s*:\s*"([^"]+)"', r.text)
        if not m:
            return None
        btn = m.group(1)
        if btn == "ADD_TO_CART":
            return "IN_STOCK"
        if btn == "PURCHASE_INVITATION":
            return "INVITE"
        return "OUT_OF_STOCK"
    except Exception:
        return None


def _bestbuy_stock_status(url):
    """Returns (status, sku) where status is 'IN_STOCK', 'INVITE', 'IN_STORE_ONLY', 'COMING_SOON', 'OUT_OF_STOCK', 'THIRD_PARTY', or None."""
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None, None
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if len(text) < 200:
            print(f"  [blocked] {url[-45:]}")
            return None, None
        sku = _bestbuy_sku_from_text(text)
        # Invite/drop detection — check before everything else, it's the highest priority
        if re.search(r"\b(invitation required|get an invite|purchase invitation|invite only|access code required)\b", text, re.IGNORECASE):
            return "INVITE", sku
        # "Coming Soon" and "In Store Only" checks first — override JSON-LD InStock
        if re.search(r"\bComing Soon\b", text, re.IGNORECASE):
            return "COMING_SOON", sku
        if re.search(r"\bIn[- ]Store Only\b", text, re.IGNORECASE):
            return "IN_STORE_ONLY", sku
        # Check seller + availability from JSON-LD in one pass
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                # Seller check — Best Buy direct listings say "Best Buy"
                seller = offers.get("seller", {}).get("name", "Best Buy")
                if seller and "best buy" not in seller.lower():
                    return "THIRD_PARTY", sku
                avail = offers.get("availability", "")
                if "InStock" in avail:
                    # Best Buy's own pages always render a "Sold by" section in
                    # server HTML (even just "Sold by Loading..."). Marketplace
                    # listings skip it entirely — JSON-LD seller field is
                    # unreliable for those, so don't trust InStock without it.
                    if re.search(r"\bsold by\b", text, re.IGNORECASE):
                        return "IN_STOCK", sku
                    return None, sku
                if "OutOfStock" in avail or "SoldOut" in avail or "Discontinued" in avail:
                    return "OUT_OF_STOCK", sku
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        # Text fallbacks
        sold_by = re.search(r"Sold by\s+([^\n·|]+)", text, re.IGNORECASE)
        if sold_by and "best buy" not in sold_by.group(1).lower():
            return "THIRD_PARTY", sku
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK", sku
        if re.search(r"\b(Sold Out|Unavailable|Out of Stock)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK", sku
        return None, sku
    except Exception:
        return None, None


def _bestbuy_name(url):
    slug = url.rstrip("/").split("/")[-2]
    slug = re.sub(r"^pokemon-trading-card-game-", "", slug)
    return slug.replace("-", " ").title()


def check_bestbuy(state, seed=False, history=None):
    print("Checking Best Buy watch list...")
    new_alerts = 0
    for url in BESTBUY_WATCH:
        key = f"bestbuy_{url}"
        status, sku = _bestbuy_stock_status(url)
        if status is None:
            print(f"  [unknown] {_bestbuy_name(url)[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        prev = state.get(key)
        name = _bestbuy_name(url)
        if status == "THIRD_PARTY":
            print(f"  [skipped 3rd party] {name[:50]}")
            time.sleep(random.uniform(1, 3))
            continue
        if not seed and status == "INVITE" and prev != "INVITE":
            send_discord(
                f"🎟️ **Best Buy Invite Drop is LIVE** 🔵\n"
                f"**{name}**\n"
                f"Check your email or Best Buy app for an invite link.\n{url}"
            )
            print(f"  [INVITE OPEN] {name[:55]}")
            new_alerts += 1
        elif not seed and status == "COMING_SOON" and prev != "COMING_SOON":
            send_discord(
                f"**Coming Soon at Best Buy** 🔵\n"
                f"**{name}**\n"
                f"Not available yet — page is live, watch for it!\n{url}"
            )
            print(f"  [COMING SOON] {name[:55]}")
            new_alerts += 1
        elif not seed and status == "IN_STORE_ONLY" and prev != "IN_STORE_ONLY":
            # Check each nearby store — only alert if a nearby store actually confirms stock
            in_stock_stores = []
            if sku:
                for store_id, store_name in BESTBUY_STORES.items():
                    store_st = _bestbuy_store_status(sku, store_id)
                    if store_st == "IN_STOCK":
                        in_stock_stores.append(store_name)
                    time.sleep(random.uniform(0.5, 1.5))
            if in_stock_stores:
                store_list = "\n".join(f"• {s}" for s in in_stock_stores)
                send_discord(
                    f"**In Stock at Nearby Best Buy** 🔵\n"
                    f"**{name}**\n"
                    f"Available in store at:\n{store_list}\n{url}"
                )
                log_restock(history, "Best Buy", name, ", ".join(in_stock_stores))
                print(f"  [IN STORE] {name[:45]} @ {', '.join(in_stock_stores)}")
                new_alerts += 1
            else:
                print(f"  [IN STORE ONLY — not nearby] {name[:45]}")
        elif not seed and status == "IN_STOCK" and prev != "IN_STOCK":
            send_discord(
                f"@everyone\n"
                f"**RESTOCK at Best Buy!** 🔵\n"
                f"**{name}**\n"
                f"In stock — sold directly by Best Buy at retail price!\n{url}"
            )
            log_restock(history, "Best Buy", name)
            print(f"  [RESTOCK] {name[:60]}")
            new_alerts += 1
        else:
            print(f"  [{status}] {name[:55]}")
        state[key] = status
        time.sleep(random.uniform(1, 3))
    label = "seeded" if seed else f"{new_alerts} restocks found"
    print(f"  {len(BESTBUY_WATCH)} products checked, {label}")
    return state


# ── Walmart ───────────────────────────────────────────────────────────────────

WALMART_WATCH = [
    # ── Mega Evolution ────────────────────────────────────────────────────────
    "https://www.walmart.com/ip/Pokemon-Trading-Card-Game-Mega-Evolution-Ascended-Heroes-Elite-Trainer-Box/18710966734",
    "https://www.walmart.com/ip/Pokemon-TCG-Mega-Evolution-Perfect-Order-Booster-Bundle-6-Packs/19380764160",
    # ── Black Bolt / White Flare (Unova) ──────────────────────────────────────
    "https://www.walmart.com/ip/Pokemon-TCG-Scarlet-Violet-Black-Bolt-White-Flare-Booster-Bundles/17752173132",
    "https://www.walmart.com/ip/Pokemon-TCG-Scarlet-Violet-Black-Bolt-White-Flare-Elite-Trainer-Box-ETB/17337259478",
    # ── Destined Rivals ───────────────────────────────────────────────────────
    "https://www.walmart.com/ip/Pokemon-TCG-Scarlet-Violet-Destined-Rivals-Booster-Bundle-6-Packs/16019713971",
    "https://www.walmart.com/ip/TCG-Scarlet-Violet-Destined-Rivals-Booster-Bundle-6-Packs/15700422581",
    # ── New — debug mode until confirmed sold by Walmart directly ─────────────
    "https://www.walmart.com/ip/Pokemon-TCG-Mega-Evolution-Chaos-Rising-Elite-Trainer-Box/19988614228",
    "https://www.walmart.com/ip/Pokemon-TCG-Mega-Evolution-Chaos-Rising-Bundle/19986002628",
    "https://www.walmart.com/ip/Pokemon-TCG-Mega-Evolution-Perfect-Order-Elite-Trainer-Box/19402160990",
    "https://www.walmart.com/ip/Pok-mon-TCG-Mega-Evolution-Ascended-Heroes-Booster-Bundle-6-Packs/18728422476",
    "https://www.walmart.com/ip/POKEMON-ME2-PHANTASMAL-FLAMES-ELITE-TRAINER-BOX/17780209250",
    "https://www.walmart.com/ip/POKEMON-ME2-PHANTASMAL-FLAMES-BOOSTER-BUNDLE/17785924366",
]

# URLs in debug mode — logged and sent as quiet messages, no @everyone alert.
# Remove a URL from this set once you've confirmed it restocks from Walmart directly.
WALMART_DEBUG = {
    "https://www.walmart.com/ip/Pokemon-TCG-Mega-Evolution-Chaos-Rising-Elite-Trainer-Box/19988614228",
    "https://www.walmart.com/ip/Pokemon-TCG-Mega-Evolution-Chaos-Rising-Bundle/19986002628",
    "https://www.walmart.com/ip/Pokemon-TCG-Mega-Evolution-Perfect-Order-Elite-Trainer-Box/19402160990",
    "https://www.walmart.com/ip/Pok-mon-TCG-Mega-Evolution-Ascended-Heroes-Booster-Bundle-6-Packs/18728422476",
    "https://www.walmart.com/ip/POKEMON-ME2-PHANTASMAL-FLAMES-ELITE-TRAINER-BOX/17780209250",
    "https://www.walmart.com/ip/POKEMON-ME2-PHANTASMAL-FLAMES-BOOSTER-BUNDLE/17785924366",
}


def _walmart_stock_status(url):
    """Returns (status, debug) where status is 'IN_STOCK', 'OUT_OF_STOCK', 'COMING_SOON', 'THIRD_PARTY', or None."""
    debug = {"seller_text": None, "jsonld_seller": None, "jsonld_avail": None, "walmart_confirmed": False, "page_len": 0, "qty_left": None}
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None, debug
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        debug["page_len"] = len(text)
        debug["qty_left"] = _qty_left(text)
        if len(text) < 5000:
            print(f"  [blocked] {url.split('/')[-1][:45]}")
            return None, debug
        if re.search(r"\bComing Soon\b", text, re.IGNORECASE):
            return "COMING_SOON", debug
        walmart_seller = bool(re.search(r"sold by\s+walmart", text, re.IGNORECASE))
        sold_by_m = re.search(r"Sold by\s+([^\n·|,]{1,50})", text, re.IGNORECASE)
        if sold_by_m:
            debug["seller_text"] = sold_by_m.group(1).strip()
        debug["walmart_confirmed"] = walmart_seller
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                seller = offers.get("seller", {}).get("name", "")
                avail = offers.get("availability", "").split("/")[-1]  # shorten URL
                if seller:
                    debug["jsonld_seller"] = seller
                if avail:
                    debug["jsonld_avail"] = avail
                if seller and "walmart" not in seller.lower():
                    return "THIRD_PARTY", debug
                if seller and "walmart" in seller.lower():
                    walmart_seller = True
                    debug["walmart_confirmed"] = True
                if "InStock" in avail:
                    return ("IN_STOCK" if walmart_seller else None), debug
                if "OutOfStock" in avail or "SoldOut" in avail:
                    return "OUT_OF_STOCK", debug
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return ("IN_STOCK" if walmart_seller else None), debug
        if re.search(r"\b(Out of Stock|Sold Out|Unavailable)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK", debug
        return None, debug
    except Exception:
        return None, debug


def _append_walmart_log(name, url, status, debug):
    try:
        log = []
        if os.path.exists(WALMART_LOG_FILE):
            with open(WALMART_LOG_FILE) as f:
                log = json.load(f)
        log.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "name": name[:60],
            "status": str(status),
            "seller_text": debug.get("seller_text"),
            "jsonld_seller": debug.get("jsonld_seller"),
            "jsonld_avail": debug.get("jsonld_avail"),
            "walmart_confirmed": debug.get("walmart_confirmed"),
            "page_len": debug.get("page_len"),
            "url": url,
        })
        log = log[-200:]  # keep last 200 entries
        with open(WALMART_LOG_FILE, "w") as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        print(f"  [walmart log error] {e}")


def _walmart_name(url):
    slug = url.rstrip("/").split("/")[-2]
    slug = re.sub(r"^(Pokemon|Pok-mon)-?(TCG|Trading-Card-Game)-?", "", slug, flags=re.IGNORECASE)
    return slug.replace("-", " ").title()


def check_walmart(state, seed=False, history=None):
    print("Checking Walmart watch list...")
    new_alerts = 0
    for url in WALMART_WATCH:
        key = f"walmart_{url}"
        status, debug = _walmart_stock_status(url)
        name = _walmart_name(url)
        _append_walmart_log(name, url, status, debug)
        if status is None:
            print(f"  [unknown] {name[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        prev = state.get(key)
        if status == "THIRD_PARTY":
            print(f"  [skipped 3rd party] {name[:50]}")
            time.sleep(random.uniform(1, 3))
            continue
        if not seed and status == "COMING_SOON" and prev != "COMING_SOON":
            send_discord(
                f"**Coming Soon at Walmart** 🟡\n"
                f"**{name}**\n"
                f"Not available yet — page is live, watch for it!\n{url}"
            )
            print(f"  [COMING SOON] {name[:55]}")
            new_alerts += 1
        elif not seed and status == "IN_STOCK" and prev != "IN_STOCK":
            if url in WALMART_DEBUG:
                send_discord(
                    f"🔍 **[DEBUG] Walmart — sold by Walmart, in stock**\n"
                    f"**{name}**\n"
                    f"Check walmart-log to confirm, then remove from WALMART_DEBUG to enable full alerts.\n{url}"
                )
                print(f"  [DEBUG IN_STOCK] {name[:55]}")
            else:
                send_discord(
                    f"@everyone\n"
                    f"**RESTOCK at Walmart!** 🟡\n"
                    f"**{name}**\n"
                    f"{qty_line(debug.get('qty_left'))}"
                    f"In stock — sold directly by Walmart at retail price!\n{url}"
                )
            log_restock(history, "Walmart", name, qty=debug.get("qty_left"))
            print(f"  [RESTOCK] {name[:60]}")
            new_alerts += 1
        else:
            print(f"  [{status}] {name[:55]}")
        state[key] = status
        time.sleep(random.uniform(1, 3))
    label = "seeded" if seed else f"{new_alerts} alerts sent"
    print(f"  {len(WALMART_WATCH)} products checked, {label}")
    return state


# ── Micro Center ─────────────────────────────────────────────────────────────

MICROCENTER_STORE_ID   = "045"
MICROCENTER_STORE_NAME = "Santa Clara"

MICROCENTER_SEARCH_URL = (
    "https://www.microcenter.com/search/search_results.aspx"
    "?fq=category:Tabletop+Games%7C646,Subcategory:Trading+Card+Game,brand:Pok%C3%A9mon"
    f"&storeID={MICROCENTER_STORE_ID}"
)

MICROCENTER_WATCH = [
    "https://www.microcenter.com/product/706055/nintendo-pokemon-mega-evolution-ascended-heroes-elite-trainer-box",
    "https://www.microcenter.com/product/709059/nintendo-pokemon-mega-evolution-perfect-order-booster-display-box",
    "https://www.microcenter.com/product/706781/nintendo-pokemon-day-2026-collection",
]


def load_dynamic_mc_urls():
    if os.path.exists(DYNAMIC_MC_URLS_FILE):
        with open(DYNAMIC_MC_URLS_FILE) as f:
            return json.load(f)
    return []


def save_dynamic_mc_urls(urls):
    with open(DYNAMIC_MC_URLS_FILE, "w") as f:
        json.dump(urls, f, indent=2)


def _microcenter_name(url):
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"^nintendo-pokemon-?(tcg-)?", "", slug, flags=re.IGNORECASE)
    return slug.replace("-", " ").title()


def _microcenter_stock_status(url):
    """Returns (online_status, store_status, inv_count) — statuses are 'IN_STOCK', 'OUT_OF_STOCK', or None; inv_count is int or None."""
    store_url = f"{url}?storeid={MICROCENTER_STORE_ID}" if "?" not in url else f"{url}&storeid={MICROCENTER_STORE_ID}"
    try:
        r = cf.get(store_url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None, None, None
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if len(text) < 500:
            print(f"  [blocked] {url[-45:]}")
            return None, None, None

        # In-store stock from the inventory panel (reflects storeid= in URL)
        store_status = None
        inv_count = None
        inv_panel = soup.find(id="pnlInventory")
        if inv_panel:
            inv_text = inv_panel.get_text(" ", strip=True)
            count_m = re.search(r"(\d+)\s+in\s+stock", inv_text, re.IGNORECASE)
            if count_m:
                inv_count = int(count_m.group(1))
                store_status = "IN_STOCK"
            elif re.search(r"\bin\s+stock\b", inv_text, re.IGNORECASE):
                inv_count = 1
                store_status = "IN_STOCK"
            elif re.search(r"\bsold out\b", inv_text, re.IGNORECASE):
                inv_count = 0
                store_status = "OUT_OF_STOCK"

        # "In Store Only" means not shippable online
        in_store_only = bool(re.search(r"\bIn[- ]Store Only\b", text, re.IGNORECASE))

        online_status = None
        if not in_store_only:
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
                        online_status = "IN_STOCK"
                    elif "OutOfStock" in avail or "SoldOut" in avail:
                        online_status = "OUT_OF_STOCK"
                    if online_status:
                        break
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
            if online_status is None:
                if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
                    online_status = "IN_STOCK"
                elif re.search(r"\b(Out of Stock|Sold Out|Not Available)\b", text, re.IGNORECASE):
                    online_status = "OUT_OF_STOCK"

        return online_status, store_status, inv_count
    except Exception:
        return None, None, None


def discover_microcenter_products(state, dynamic_mc_urls):
    """Scrape Micro Center's Pokemon TCG search page for new products."""
    print("Scanning Micro Center for new Pokemon TCG products...")
    all_known = set(MICROCENTER_WATCH) | set(dynamic_mc_urls)
    try:
        r = cf.get(MICROCENTER_SEARCH_URL, impersonate="chrome124", timeout=20)
        if not r.ok:
            print(f"  Micro Center search returned {r.status_code}")
            return state, dynamic_mc_urls
        soup = BeautifulSoup(r.text, "html.parser")
        new_found = []
        seen_ids = set()
        for a in soup.find_all("a", href=re.compile(r"/product/\d+/")):
            href = a.get("href", "")
            m = re.search(r"/product/(\d+)/([^?#\"']+)", href)
            if not m:
                continue
            prod_id, slug = m.group(1), m.group(2).rstrip("/")
            if prod_id in seen_ids:
                continue
            seen_ids.add(prod_id)
            full_url = f"https://www.microcenter.com/product/{prod_id}/{slug}"
            disc_key = f"mc_discovered_{prod_id}"
            if full_url not in all_known and not state.get(disc_key):
                name = re.sub(r"^nintendo-pokemon-?(tcg-)?", "", slug, flags=re.IGNORECASE).replace("-", " ").title()
                if not is_card_product(name):
                    continue
                new_found.append((name, full_url))
                state[disc_key] = True
                dynamic_mc_urls.append(full_url)
                all_known.add(full_url)
        for name, url in new_found:
            send_discord(
                f"🆕 **New Product at Micro Center!**\n"
                f"**{name}**\n"
                f"Just appeared in their Pokemon TCG catalog — now monitoring!\n{url}"
            )
            print(f"  [NEW MC] {name[:55]}")
        label = f"{len(new_found)} new products" if new_found else "no new products"
        print(f"  Micro Center discovery: {label}")
    except Exception as e:
        print(f"  Micro Center discovery error: {e}")
    return state, dynamic_mc_urls


def check_microcenter(state, seed=False, history=None):
    print("Checking Micro Center watch list...")
    new_alerts = 0
    for url in list(MICROCENTER_WATCH):
        name = _microcenter_name(url)
        online_status, store_status, inv_count = _microcenter_stock_status(url)
        online_key = f"mc_online_{url}"
        store_key  = f"mc_store_{url}"
        inv_key    = f"mc_inv_{url}"
        prev_online = state.get(online_key)
        prev_store  = state.get(store_key)
        prev_inv    = state.get(inv_key)  # last known count (int or None)

        if online_status is None and store_status is None:
            print(f"  [unknown] {name[:55]}")
            time.sleep(random.uniform(1, 3))
            continue

        if not seed and online_status == "IN_STOCK" and prev_online != "IN_STOCK":
            notify(name, "Micro Center", url, is_local=False)
            log_restock(history, "Micro Center", name, "Online")
            new_alerts += 1
        elif not seed and store_status == "IN_STOCK" and prev_store != "IN_STOCK":
            notify(name, f"Micro Center {MICROCENTER_STORE_NAME}", url, is_local=True, qty=inv_count)
            log_restock(history, "Micro Center", name, MICROCENTER_STORE_NAME, qty=inv_count)
            new_alerts += 1
        elif not seed and inv_count and inv_count > 0 and (prev_inv is None or prev_inv == 0):
            # Inventory count moved from zero — quiet heads up, no @everyone
            send_discord(
                f"👀 **Micro Center inventory signal** — {MICROCENTER_STORE_NAME}\n"
                f"**{name}**\n"
                f"{inv_count} unit(s) appeared in store — drop may be incoming!\n{url}"
            )
            print(f"  [inv signal] {name[:50]} — count={inv_count}")
            new_alerts += 1
        else:
            print(f"  [online={online_status or '?'} store={store_status or '?'} inv={inv_count}] {name[:35]}")

        if online_status is not None:
            state[online_key] = online_status
        if store_status is not None:
            state[store_key] = store_status
        if inv_count is not None:
            state[inv_key] = inv_count
        time.sleep(random.uniform(1, 3))

    label = "seeded" if seed else f"{new_alerts} alerts sent"
    print(f"  {len(MICROCENTER_WATCH)} products checked, {label}")
    return state


# ── Token expiry reminder ─────────────────────────────────────────────────────

GITHUB_TOKEN_EXPIRY = date(2026, 8, 11)

def check_token_expiry(state):
    today = date.today()
    days_left = (GITHUB_TOKEN_EXPIRY - today).days
    if days_left > 7:
        return
    last_warned = state.get("token_expiry_last_warned")
    if last_warned == str(today):
        return
    send_discord(
        f"⚠️ **GitHub token expires in {days_left} day{'s' if days_left != 1 else ''}** ({GITHUB_TOKEN_EXPIRY})\n"
        "Renew it at: github.com → Settings → Developer settings → Personal access tokens\n"
        "Then update cron-job.org with the new token."
    )
    state["token_expiry_last_warned"] = str(today)


# ── Walmart log viewer ───────────────────────────────────────────────────────

def run_walmart_log():
    """Send the last 5 Walmart log entries per product to Discord."""
    if not os.path.exists(WALMART_LOG_FILE):
        send_discord("No Walmart log yet — run the monitor first.")
        return
    with open(WALMART_LOG_FILE) as f:
        log = json.load(f)

    # Group by URL, keep last 5 per product
    by_url = {}
    for entry in log:
        by_url.setdefault(entry["url"], []).append(entry)
    lines = ["**Walmart Debug Log** (last 5 checks per product)"]
    for url, entries in by_url.items():
        recent = entries[-5:]
        name = recent[-1]["name"]
        lines.append(f"\n**{name}**")
        for e in recent:
            seller = e.get("seller_text") or e.get("jsonld_seller") or "—"
            avail = e.get("jsonld_avail") or "—"
            confirmed = "✅" if e.get("walmart_confirmed") else "❌"
            lines.append(
                f"`{e['ts']}` status=**{e['status']}** | "
                f"seller={seller} | avail={avail} | walmart={confirmed}"
            )
    send_discord("\n".join(lines))
    print("Walmart log sent to Discord.")


# ── Micro Center announce ────────────────────────────────────────────────────

def run_mc_announce():
    """Post a one-time Discord message announcing Micro Center monitoring."""
    lines = [
        f"🟢 **Pokebot now monitoring Micro Center — {MICROCENTER_STORE_NAME}!**\n",
        "Watching for Pokemon TCG restocks both online and in-store at retail price.\n",
        "**Products currently tracked:**",
    ]
    for url in MICROCENTER_WATCH:
        name = _microcenter_name(url)
        lines.append(f"• [{name}]({url})")
    lines.append(
        "\nNew products are auto-discovered each run — "
        "anything new in their TCG catalog will show up here automatically."
    )
    send_discord("\n".join(lines))
    print("Micro Center announcement sent to Discord.")


# ── Best Buy store scan ───────────────────────────────────────────────────────

def run_bestbuy_store_check():
    """Check every watched Best Buy product at all 3 nearby stores and report to Discord."""
    print("Scanning Best Buy store inventory near 95122...")
    lines = ["**Best Buy Store Inventory — 95122**"]
    any_found = False

    for url in BESTBUY_WATCH:
        name = _bestbuy_name(url)
        print(f"  {name[:55]}...")
        _, sku = _bestbuy_stock_status(url)
        if not sku:
            lines.append(f"⚠️ **{name}** — couldn't read SKU")
            time.sleep(random.uniform(1, 2))
            continue
        store_results = []
        for store_id, store_name in BESTBUY_STORES.items():
            st = _bestbuy_store_status(sku, store_id)
            store_results.append((store_name, st))
            time.sleep(random.uniform(0.5, 1))
        in_stock = [s for s, st in store_results if st == "IN_STOCK"]
        out = [s for s, st in store_results if st == "OUT_OF_STOCK"]
        unknown = [s for s, st in store_results if st is None]
        if in_stock:
            store_list = ", ".join(in_stock)
            lines.append(f"✅ **{name}**\n   In stock at: {store_list}")
            any_found = True
        else:
            store_str = " | ".join(f"{s}: {'OOS' if st == 'OUT_OF_STOCK' else '?'}" for s, st in store_results)
            lines.append(f"❌ **{name}** — {store_str}")
        time.sleep(random.uniform(1, 2))

    if not any_found:
        lines.append("\nNo stock found at any nearby store right now.")
    send_discord("\n".join(lines))
    print("Done — results sent to Discord.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--test" in sys.argv:
        print("Sending test Discord message...")
        ok = send_discord(
            "**Pokebot is online!**\n"
            f"Monitoring Pokemon cards near ZIP {TARGET_ZIP}\n"
            "Checking: Target + Pokemon Center + Costco + Best Buy + Micro Center"
        )
        print("Discord webhook works! Check your server." if ok else "Discord webhook FAILED")
        return

    if "--status" in sys.argv:
        run_status()
        return

    if "--bb-stores" in sys.argv:
        run_bestbuy_store_check()
        return

    if "--walmart-log" in sys.argv:
        run_walmart_log()
        return

    if "--mc-announce" in sys.argv:
        run_mc_announce()
        return


    first_run = not os.path.exists(STATE_FILE)
    seed = first_run or "--seed" in sys.argv
    if seed:
        print("First run — seeding state without sending alerts...")

    history = load_history()
    state = load_state()

    # Merge auto-discovered TCINs into the global watch list
    dynamic_tcins = load_dynamic_tcins()
    for tcin in dynamic_tcins:
        if tcin not in TARGET_TCINS:
            TARGET_TCINS.append(tcin)

    # Load auto-discovered Pokemon Center URLs
    dynamic_pc_urls = load_dynamic_pc_urls()

    # Load auto-discovered Micro Center URLs and merge into watch list
    dynamic_mc_urls = load_dynamic_mc_urls()
    for url in dynamic_mc_urls:
        if url not in MICROCENTER_WATCH:
            MICROCENTER_WATCH.append(url)

    check_token_expiry(state)

    # Scan Target for new products — runs first so newly found TCINs are
    # included in the parallel check_target call below
    state, dynamic_tcins = discover_target_tcins(state, dynamic_tcins)
    save_dynamic_tcins(dynamic_tcins)

    # Scan Micro Center for new products
    state, dynamic_mc_urls = discover_microcenter_products(state, dynamic_mc_urls)
    save_dynamic_mc_urls(dynamic_mc_urls)

    # Run all retailer checks in parallel — each gets its own state copy so
    # reads don't race; writes go to separate key namespaces so merging is safe
    checks = [
        ("Target",           lambda: check_target(state.copy(), seed=seed, history=history)),
        ("PC Site Queue",    lambda: check_pokemoncenter_site_queue(state.copy(), seed=seed)),
        ("Pokemon Center",   lambda: check_pokemoncenter(state.copy(), seed=seed, dynamic_pc_urls=dynamic_pc_urls)),
        ("PC Restock",       lambda: check_pokemoncenter_restock(state.copy(), seed=seed, history=history, dynamic_pc_urls=list(dynamic_pc_urls))),
        ("Costco",           lambda: check_costco(state.copy(), seed=seed, history=history)),
        ("Sam's Club",       lambda: check_samsclub(state.copy(), seed=seed, history=history)),
        ("Best Buy",         lambda: check_bestbuy(state.copy(), seed=seed, history=history)),
        ("Walmart",          lambda: check_walmart(state.copy(), seed=seed, history=history)),
        ("Micro Center",     lambda: check_microcenter(state.copy(), seed=seed, history=history)),
    ]

    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = {executor.submit(fn): name for name, fn in checks}
        for future in as_completed(futures):
            name = futures[future]
            try:
                state.update(future.result())
            except Exception as e:
                print(f"  {name} check failed: {e}")

    # Weekly restock pattern summary — fires once a week automatically
    if not seed:
        last = history.get("last_summary")
        days_since = (datetime.now() - datetime.fromisoformat(last)).days if last else 999
        if days_since >= 7:
            send_pattern_summary(history)

    save_history(history)
    save_state(state)
    print("Done." if not seed else "Done. Run again to start receiving alerts.")


if __name__ == "__main__":
    main()
