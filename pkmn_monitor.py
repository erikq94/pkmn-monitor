#!/usr/bin/env python3
import html
import json
import os
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, date

import random
import re
import time

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cf

warnings.filterwarnings("ignore")

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
STATE_FILE        = os.path.join(os.path.dirname(__file__), "seen_products.json")
HISTORY_FILE      = os.path.join(os.path.dirname(__file__), "restock_history.json")
DYNAMIC_TCINS_FILE = os.path.join(os.path.dirname(__file__), "dynamic_tcins.json")

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


def log_restock(history, retailer, name, store="Online"):
    if history is None:
        return
    now = datetime.now()
    history["restocks"].append({
        "retailer": retailer,
        "name": name[:80],
        "store": store,
        "timestamp": now.isoformat(),
        "day": now.strftime("%A"),
        "hour": now.hour,
    })


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
    for r in recent:
        label = f"{r['retailer']} — {r.get('store', 'Online')}"
        groups[label].append((r["day"], r["hour"]))

    lines = [f"📊 **Restock Patterns — last 30 days** ({len(recent)} total restocks)"]
    for location, times in sorted(groups.items()):
        counts = Counter(times)
        parts = []
        for (day, hour), n in sorted(counts.items(), key=lambda x: -x[1]):
            ampm = "am" if hour < 12 else "pm"
            h = hour % 12 or 12
            parts.append(f"{day[:3]} {h}{ampm}" + (f" ×{n}" if n > 1 else ""))
        lines.append(f"**{location}**: {', '.join(parts)}")

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
        f"@everyone\n"
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
            }

        new_alerts = 0

        # ── Online / ship-to-you (one check, works nationally) ──
        for tcin, prod in product_map.items():
            name, buy_url, ship_status = prod["name"], prod["buy_url"], prod["ship_status"]
            online_key = f"target_online_{tcin}"

            if not is_card_product(name):
                continue

            if not seed and ship_status == "IN_STOCK" and state.get(online_key) != "IN_STOCK":
                time.sleep(random.uniform(0.5, 2))
                if is_sold_by_target(buy_url):
                    notify(name, "Target", buy_url, is_local=False)
                    log_restock(history, "Target", name, "Online")
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
                    time.sleep(random.uniform(0.5, 2))
                    if is_sold_by_target(buy_url):
                        notify(name, f"Target {store_name}", buy_url, is_local=True)
                        log_restock(history, "Target", name, store_name)
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
                    f"@everyone\n"
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


def check_pokemoncenter_restock(state, seed=False, history=None):
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


# ── Best Buy ──────────────────────────────────────────────────────────────────

BESTBUY_WATCH = [
    # ── Mega Evolution ────────────────────────────────────────────────────────
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-chaos-rising-elite-trainer-box/JJG2TL34RT",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-chaos-rising-booster-bundle/JJG2TL34H9",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-perfect-order-elite-trainer-box/JJG2TL3W86",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-perfect-order-booster-bundle/JJG2TL3QK2",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-ascended-heroes-booster-bundle/JJG2TL3JP8",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-pitch-black-elite-trainer-box/JJG2TL8J45",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-elite-trainer-box-styles-may-vary/JJG2TL2LWZ",
    # ── Scarlet & Violet ──────────────────────────────────────────────────────
    "https://www.bestbuy.com/product/pokemon-trading-card-game-scarlet-violet-journey-together-booster-bundle-6-pk/JJG2TLCFST",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-scarlet-violet-prismatic-evolutions-elite-trainer-box/JJG2TLCW3L",
    "https://www.bestbuy.com/product/pokemon-trading-card-game-scarlet-violet-prismatic-evolutions-booster-bundle/JJG2TL23JK",
]


def _bestbuy_stock_status(url):
    """Returns 'IN_STOCK', 'COMING_SOON', 'OUT_OF_STOCK', 'THIRD_PARTY', or None."""
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if len(text) < 200:
            print(f"  [blocked] {url[-45:]}")
            return None
        # "Coming Soon" and "In Store Only" checks first — override JSON-LD InStock
        if re.search(r"\bComing Soon\b", text, re.IGNORECASE):
            return "COMING_SOON"
        if re.search(r"\bIn[- ]Store Only\b", text, re.IGNORECASE):
            return "IN_STORE_ONLY"
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
                    return "THIRD_PARTY"
                avail = offers.get("availability", "")
                if "InStock" in avail:
                    # Best Buy's own pages always render a "Sold by" section in
                    # server HTML (even just "Sold by Loading..."). Marketplace
                    # listings skip it entirely — JSON-LD seller field is
                    # unreliable for those, so don't trust InStock without it.
                    if re.search(r"\bsold by\b", text, re.IGNORECASE):
                        return "IN_STOCK"
                    return None
                if "OutOfStock" in avail or "SoldOut" in avail or "Discontinued" in avail:
                    return "OUT_OF_STOCK"
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        # Text fallbacks
        sold_by = re.search(r"Sold by\s+([^\n·|]+)", text, re.IGNORECASE)
        if sold_by and "best buy" not in sold_by.group(1).lower():
            return "THIRD_PARTY"
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK"
        if re.search(r"\b(Sold Out|Unavailable|Out of Stock)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK"
        return None
    except Exception:
        return None


def _bestbuy_name(url):
    slug = url.rstrip("/").split("/")[-2]
    slug = re.sub(r"^pokemon-trading-card-game-", "", slug)
    return slug.replace("-", " ").title()


def check_bestbuy(state, seed=False, history=None):
    print("Checking Best Buy watch list...")
    new_alerts = 0
    for url in BESTBUY_WATCH:
        key = f"bestbuy_{url}"
        status = _bestbuy_stock_status(url)
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
        if not seed and status == "COMING_SOON" and prev != "COMING_SOON":
            send_discord(
                f"**Coming Soon at Best Buy** 🔵\n"
                f"**{name}**\n"
                f"Not available yet — page is live, watch for it!\n{url}"
            )
            print(f"  [COMING SOON] {name[:55]}")
            new_alerts += 1
        elif not seed and status == "IN_STORE_ONLY" and prev != "IN_STORE_ONLY":
            send_discord(
                f"**In Store Only — Best Buy** 🔵\n"
                f"**{name}**\n"
                f"Available in stores but not online.\n"
                f"Check which store near 95122 has it:\n"
                f"bestbuy.com → search product → click 'Check stores'\n{url}"
            )
            print(f"  [IN STORE ONLY] {name[:55]}")
            new_alerts += 1
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
]


def _walmart_stock_status(url):
    """Returns 'IN_STOCK', 'OUT_OF_STOCK', 'COMING_SOON', 'THIRD_PARTY', or None."""
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        # Walmart real pages are 50k+ chars; anything under 5000 is a bot block
        if len(text) < 5000:
            print(f"  [blocked] {url.split('/')[-1][:45]}")
            return None
        # Coming Soon check first
        if re.search(r"\bComing Soon\b", text, re.IGNORECASE):
            return "COMING_SOON"
        # Seller check — Walmart direct shows "Sold by Walmart.com"
        sold_by = re.search(r"Sold by\s+([^\n·|,]+)", text, re.IGNORECASE)
        if sold_by and "walmart" not in sold_by.group(1).lower():
            return "THIRD_PARTY"
        # JSON-LD availability
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                seller = offers.get("seller", {}).get("name", "Walmart")
                if seller and "walmart" not in seller.lower():
                    return "THIRD_PARTY"
                avail = offers.get("availability", "")
                if "InStock" in avail:
                    return "IN_STOCK"
                if "OutOfStock" in avail or "SoldOut" in avail:
                    return "OUT_OF_STOCK"
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK"
        if re.search(r"\b(Out of Stock|Sold Out|Unavailable)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK"
        return None
    except Exception:
        return None


def _walmart_name(url):
    slug = url.rstrip("/").split("/")[-2]
    slug = re.sub(r"^(Pokemon|Pok-mon)-?(TCG|Trading-Card-Game)-?", "", slug, flags=re.IGNORECASE)
    return slug.replace("-", " ").title()


def check_walmart(state, seed=False, history=None):
    print("Checking Walmart watch list...")
    new_alerts = 0
    for url in WALMART_WATCH:
        key = f"walmart_{url}"
        status = _walmart_stock_status(url)
        if status is None:
            print(f"  [unknown] {_walmart_name(url)[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        prev = state.get(key)
        name = _walmart_name(url)
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
            send_discord(
                f"@everyone\n"
                f"**RESTOCK at Walmart!** 🟡\n"
                f"**{name}**\n"
                f"In stock — sold directly by Walmart at retail price!\n{url}"
            )
            log_restock(history, "Walmart", name)
            print(f"  [RESTOCK] {name[:60]}")
            new_alerts += 1
        else:
            print(f"  [{status}] {name[:55]}")
        state[key] = status
        time.sleep(random.uniform(1, 3))
    label = "seeded" if seed else f"{new_alerts} alerts sent"
    print(f"  {len(WALMART_WATCH)} products checked, {label}")
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--test" in sys.argv:
        print("Sending test Discord message...")
        ok = send_discord(
            "**Pokebot is online!**\n"
            f"Monitoring Pokemon cards near ZIP {TARGET_ZIP}\n"
            "Checking: Target + Pokemon Center + Costco + Best Buy"
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

    history = load_history()
    state = load_state()

    # Merge auto-discovered TCINs into the global watch list
    dynamic_tcins = load_dynamic_tcins()
    for tcin in dynamic_tcins:
        if tcin not in TARGET_TCINS:
            TARGET_TCINS.append(tcin)

    check_token_expiry(state)

    # Scan Target for new products not in our watch list
    state, dynamic_tcins = discover_target_tcins(state, dynamic_tcins)
    save_dynamic_tcins(dynamic_tcins)

    state = check_target(state, seed=seed, history=history)
    state = check_pokemoncenter(state, seed=seed)
    state = check_pokemoncenter_restock(state, seed=seed, history=history)
    state = check_costco(state, seed=seed, history=history)
    state = check_bestbuy(state, seed=seed, history=history)
    state = check_walmart(state, seed=seed, history=history)

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
