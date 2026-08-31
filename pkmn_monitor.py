#!/usr/bin/env python3
import html
import json
import os
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta, timezone

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
    # Pitch Black (new) — verified sold & shipped by Target directly, 2026-08-16
    "1011483406",                        # Pitch Black ETB ($59.99)
    "1011483414",                        # Pitch Black Booster Bundle ($31.99)
    "1011483413",                        # Pitch Black Booster Display ($179.99)
    # 30th Celebration (new, 2026-08-30) — verified sold & shipped by Target directly,
    # $69.99, releases 2026-09-16. Preorder window already opened/closed once Aug 18.
    "1010892076",                        # 30th Celebration ETB
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


# ── Checker health alerting ─────────────────────────────────────────────────
# Consecutive-failure tracking lives in `state` (persisted to seen_products.json)
# since each run is a fresh process. Alerts fire once on crossing the failure
# threshold and once on recovery — not on every failed run — so a retailer
# having a bad day doesn't spam the channel every 5 minutes.
_ERROR_ALERT_THRESHOLD = 3          # consecutive failures before alerting
_ERROR_ALERT_COOLDOWN_SECONDS = 3600  # re-alert at most once an hour while still down

def report_error(state, key, message):
    count_key = f"_err_count_{key}"
    alerted_key = f"_err_alerted_{key}"
    last_alert_key = f"_err_last_alert_{key}"
    count = state.get(count_key, 0) + 1
    state[count_key] = count
    if count < _ERROR_ALERT_THRESHOLD:
        return
    now = datetime.now()
    last_alert = state.get(last_alert_key)
    stale = not last_alert or (now - datetime.fromisoformat(last_alert)).total_seconds() >= _ERROR_ALERT_COOLDOWN_SECONDS
    if not state.get(alerted_key) or stale:
        send_discord(f"⚠️ **{key} has failed {count}x in a row**\nLatest error: {message}")
        state[alerted_key] = True
        state[last_alert_key] = now.isoformat()


def report_recovery(state, key):
    if state.get(f"_err_alerted_{key}"):
        send_discord(f"✅ **{key} recovered** — back to normal after {state.get(f'_err_count_{key}', '?')} failed run(s)")
    for suffix in ("_err_count_", "_err_alerted_", "_err_last_alert_"):
        state.pop(f"{suffix}{key}", None)


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


# For retailers where the only quantity signal we can scrape is a low-stock
# warning ("Only N left"), its absence isn't proof of a specific count — we
# don't know the retailer's own threshold for showing it. Say what we
# actually observed (no warning) rather than invent a number.
NO_LOW_STOCK_WARNING = "🟢 In stock — no low-stock warning shown, likely well-stocked\n"


_QTY_LEFT_RE = re.compile(r"only\s+(\d+)\s+left", re.IGNORECASE)

def _qty_left(text):
    """Extracts a low-stock 'Only N left' count from page text, or None."""
    m = _QTY_LEFT_RE.search(text)
    return int(m.group(1)) if m else None


# Best-effort scrape for an announced release/drop date on COMING_SOON pages.
# Frequently won't match — retailers don't always print one — that's fine,
# it's purely extra data when it's there, never required for alert logic.
_RELEASE_DATE_RE = re.compile(
    r"\b(?:available|releases?|coming|arrives?|launch(?:es|ing)?|drops?)\s+(?:on\s+|in\s+)?"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)",
    re.IGNORECASE,
)

def _release_date_text(text):
    m = _RELEASE_DATE_RE.search(text)
    return m.group(1) if m else None


# ── Per-retailer stock logging ─────────────────────────────────────────────
# Every checker can log price/qty/status on every check without bloating the
# file: an entry is only written when something actually changed, or once an
# hour as a heartbeat if nothing did. Dedup state lives under its own key
# (not the alert-logic "prev" key) so an unknown/blocked read doesn't get
# compared against a stale real status and force a log line every 5 minutes.
STOCK_LOG_HEARTBEAT_SECONDS = 3300  # ~55 min


def _should_log_stock(state, dedup_key, status, price=None, qty=None):
    now = datetime.now()
    last_status_key = f"{dedup_key}__log_status"
    last_logged_key = f"{dedup_key}__log_ts"
    last_price_key = f"{dedup_key}__log_price"
    last_qty_key = f"{dedup_key}__log_qty"

    last_logged = state.get(last_logged_key)
    stale = not last_logged or (now - datetime.fromisoformat(last_logged)).total_seconds() >= STOCK_LOG_HEARTBEAT_SECONDS
    changed = (
        status != state.get(last_status_key)
        or price != state.get(last_price_key)
        or qty != state.get(last_qty_key)
    )
    if changed or stale:
        state[last_status_key] = status
        state[last_logged_key] = now.isoformat()
        state[last_price_key] = price
        state[last_qty_key] = qty
        return True
    return False


STOCK_LOG_FILE = os.path.join(os.path.dirname(__file__), "stock_log.json")
STOCK_LOG_RETENTION_DAYS = 90


_stock_log_lock = threading.Lock()

def _append_stock_log(retailer, name, url, status, store="Online", price=None, qty=None, seller=None,
                       duration_minutes=None, release_date=None, state=None):
    try:
        with _stock_log_lock:
            log = []
            if os.path.exists(STOCK_LOG_FILE):
                with open(STOCK_LOG_FILE) as f:
                    log = json.load(f)
            log.append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "retailer": retailer,
                "name": name[:60],
                "store": store,
                "status": str(status),
                "price": price,
                "qty": qty,
                "seller": seller,
                "duration_minutes": duration_minutes,
                "release_date": release_date,
                "url": url,
            })
            cutoff = (datetime.now() - timedelta(days=STOCK_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
            log = [e for e in log if e.get("ts", "") >= cutoff]
            tmp_path = STOCK_LOG_FILE + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(log, f, indent=2)
            os.replace(tmp_path, STOCK_LOG_FILE)
        if state is not None:
            report_recovery(state, "stock_log_write")
    except Exception as e:
        print(f"  [stock log error] {e}")
        if state is not None:
            report_error(state, "stock_log_write", str(e))


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
        return False  # couldn't verify — don't risk alerting on a 3rd-party listing


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
                "price": p.get("price", {}).get("current_retail"),
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

            if _should_log_stock(state, f"target_online_{tcin}", ship_status, prod.get("price"), prod.get("ship_qty")):
                _append_stock_log("Target", name, buy_url, ship_status, price=prod.get("price"), qty=prod.get("ship_qty"), state=state)

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

                local_status = "IN_STOCK" if in_stock_locally else "OUT_OF_STOCK"
                local_qty = int(store_qty) if store_qty else None
                if _should_log_stock(state, f"target_local_{store_id}_{tcin}", local_status, None, local_qty):
                    _append_stock_log("Target", name, buy_url, local_status, store=store_name, qty=local_qty, state=state)

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
    """Returns (status, debug) where status is 'IN_STOCK', 'OUT_OF_STOCK', 'QUEUE', or None if unknown."""
    debug = {"price": None, "qty_left": None}
    try:
        r = cf.get(url, impersonate="chrome120", timeout=15, allow_redirects=True)
        if not r.ok:
            return None, debug
        final_url = str(r.url)
        # Incapsula/Imperva waiting room — gates the entire PC site during high-traffic drops
        if "_Incapsula_Resource" in r.text or "incapsula" in r.text.lower():
            return "QUEUE", debug
        if "queue-it.net" in final_url or "queue-it.net" in r.text:
            return "QUEUE", debug
        soup = BeautifulSoup(r.text, "html.parser")
        text_quick = soup.get_text(" ", strip=True)
        if "waiting room" in text_quick.lower() or "virtual queue" in text_quick.lower():
            return "QUEUE", debug
        debug["qty_left"] = _qty_left(text_quick)
        # JSON-LD structured data is in the raw HTML (not JS-rendered)
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                price = offers.get("price")
                if price is not None:
                    try:
                        debug["price"] = float(price)
                    except (TypeError, ValueError):
                        debug["price"] = price
                avail = offers.get("availability", "")
                if "InStock" in avail:
                    return "IN_STOCK", debug
                if "OutOfStock" in avail or "SoldOut" in avail or "Discontinued" in avail:
                    return "OUT_OF_STOCK", debug
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        # Fallback: plain text signals
        text = soup.get_text(" ", strip=True)
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK", debug
        if re.search(r"\b(Out of Stock|Sold Out|Notify Me)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK", debug
        return None, debug
    except Exception:
        return None, debug


def check_pokemoncenter_restock(state, seed=False, history=None, dynamic_pc_urls=None):
    all_urls = list(PC_RESTOCK_WATCH) + [u for u in (dynamic_pc_urls or []) if u not in PC_RESTOCK_WATCH]
    print("Checking Pokemon Center restock watch list...")
    new_alerts = 0
    for url in all_urls:
        key = f"pc_stock_{url}"
        status, debug = _pc_stock_status(url)
        name = _slug_to_name(url)

        if _should_log_stock(state, f"pc_{url}", status, debug.get("price"), debug.get("qty_left")):
            _append_stock_log("Pokemon Center", name, url, status, price=debug.get("price"), qty=debug.get("qty_left"), state=state)

        if status is None:
            time.sleep(0.5)
            continue
        prev = state.get(key)
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
                f"{qty_line(debug.get('qty_left')) or NO_LOW_STOCK_WARNING}"
                f"Back in stock — buy directly at retail price!\n{url}"
            )
            log_restock(history, "Pokemon Center", name, qty=debug.get("qty_left"))
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
    """Returns (status, debug) where status is 'IN_STOCK', 'OUT_OF_STOCK', 'QUEUE', or None if unknown."""
    debug = {"price": None, "qty_left": None}
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None, debug
        # Queue-it detection — Costco redirects high-demand drops to a virtual waiting room
        final_url = str(r.url)
        if "queue-it.net" in final_url or "queue-it.net" in r.text:
            return "QUEUE", debug
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if "waiting room" in text.lower() or "virtual queue" in text.lower():
            return "QUEUE", debug
        # Akamai block — returns a tiny privacy page instead of product content
        if len(text) < 200:
            print(f"  [blocked by Akamai] {url[-50:]}")
            return None, debug
        debug["qty_left"] = _qty_left(text)
        # JSON-LD structured data (most reliable)
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                price = offers.get("price")
                if price is not None:
                    try:
                        debug["price"] = float(price)
                    except (TypeError, ValueError):
                        debug["price"] = price
                avail = offers.get("availability", "")
                if "InStock" in avail:
                    return "IN_STOCK", debug
                if "OutOfStock" in avail or "SoldOut" in avail:
                    return "OUT_OF_STOCK", debug
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK", debug
        if re.search(r"\b(Out of Stock|Sold Out)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK", debug
        # oos-overlay: "hide" class means the overlay is hidden = item IS in stock
        oos = soup.find(class_="oos-overlay")
        if oos:
            return ("IN_STOCK" if "hide" in oos.get("class", []) else "OUT_OF_STOCK"), debug
        return None, debug
    except Exception:
        return None, debug


def _costco_name(url):
    slug = url.split("/")[-1].split(".product.")[0]
    slug = re.sub(r"%[0-9a-fA-F]{2}", "", slug)
    return slug.replace("-", " ").replace(":", "").title()


def check_costco(state, seed=False, history=None):
    print("Checking Costco watch list...")
    new_alerts = 0
    for url in COSTCO_WATCH:
        key = f"costco_{url}"
        status, debug = _costco_stock_status(url)
        name = _costco_name(url)

        if _should_log_stock(state, f"costco_{url}", status, debug.get("price"), debug.get("qty_left")):
            _append_stock_log("Costco", name, url, status, price=debug.get("price"), qty=debug.get("qty_left"), state=state)

        if status is None:
            print(f"  [unknown] {name[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        prev = state.get(key)
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
                f"{qty_line(debug.get('qty_left')) or NO_LOW_STOCK_WARNING}"
                f"Available online now — also check the Costco app for local warehouse stock!\n{url}"
            )
            log_restock(history, "Costco", name, qty=debug.get("qty_left"))
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
    """Returns (status, debug) where status is 'IN_STOCK', 'COMING_SOON', 'OUT_OF_STOCK', 'QUEUE', or None."""
    debug = {"price": None, "qty_left": None, "release_date": None}
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None, debug
        # Queue-it detection — same as Costco
        final_url = str(r.url)
        if "queue-it.net" in final_url or "queue-it.net" in r.text:
            return "QUEUE", debug
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if "waiting room" in text.lower() or "virtual queue" in text.lower():
            return "QUEUE", debug
        if len(text) < 500:
            print(f"  [blocked] {url.split('/')[-1][:40]}")
            return None, debug
        debug["qty_left"] = _qty_left(text)
        # Coming Soon check
        if re.search(r"\bcoming soon\b", text, re.IGNORECASE):
            debug["release_date"] = _release_date_text(text)
            return "COMING_SOON", debug
        # JSON-LD structured data
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                price = offers.get("price")
                if price is not None:
                    try:
                        debug["price"] = float(price)
                    except (TypeError, ValueError):
                        debug["price"] = price
                avail = offers.get("availability", "")
                if "InStock" in avail:
                    return "IN_STOCK", debug
                if "OutOfStock" in avail or "SoldOut" in avail:
                    return "OUT_OF_STOCK", debug
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK", debug
        if re.search(r"\b(Out of Stock|Sold Out|Not available)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK", debug
        return None, debug
    except Exception:
        return None, debug


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
        status, debug = _samsclub_stock_status(url)
        name = _samsclub_name(url)

        if _should_log_stock(state, f"samsclub_{url}", status, debug.get("price"), debug.get("qty_left")):
            _append_stock_log("Sam's Club", name, url, status, price=debug.get("price"), qty=debug.get("qty_left"),
                               release_date=debug.get("release_date"), state=state)

        if status is None:
            print(f"  [unknown] {name[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        prev = state.get(key)
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
                f"{qty_line(debug.get('qty_left')) or NO_LOW_STOCK_WARNING}"
                f"In stock online — Plus Members, limit 2!\n{url}"
            )
            log_restock(history, "Sam's Club", name, "Online", qty=debug.get("qty_left"))
            print(f"  [RESTOCK] {name[:60]}")
            new_alerts += 1
        elif not seed and status == "COMING_SOON" and prev != "COMING_SOON":
            date_line = f"Release date spotted: **{debug['release_date']}**\n" if debug.get("release_date") else ""
            send_discord(
                f"**Coming Soon at Sam's Club** 🟠\n"
                f"**{name}**\n"
                f"{date_line}"
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
    # ── 30th Celebration (new, 2026-08-30) — releases 2026-09-16 ────────────────
    "https://www.bestbuy.com/product/pokemon-trading-card-game-30th-celebration-elite-trainer-box/JJG2TL8XCJ",
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
    """Returns (status, sku, debug) where status is 'IN_STOCK', 'INVITE', 'IN_STORE_ONLY', 'COMING_SOON', 'OUT_OF_STOCK', 'THIRD_PARTY', or None."""
    debug = {"price": None, "qty_left": None, "seller": None, "release_date": None}
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None, None, debug
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if len(text) < 200:
            print(f"  [blocked] {url[-45:]}")
            return None, None, debug
        debug["qty_left"] = _qty_left(text)
        sku = _bestbuy_sku_from_text(text)
        # Invite/drop detection — check before everything else, it's the highest priority
        if re.search(r"\b(invitation required|get an invite|purchase invitation|invite only|access code required)\b", text, re.IGNORECASE):
            return "INVITE", sku, debug
        # "Coming Soon" and "In Store Only" checks first — override JSON-LD InStock
        if re.search(r"\bComing Soon\b", text, re.IGNORECASE):
            debug["release_date"] = _release_date_text(text)
            return "COMING_SOON", sku, debug
        if re.search(r"\bIn[- ]Store Only\b", text, re.IGNORECASE):
            return "IN_STORE_ONLY", sku, debug
        # Check seller + availability from JSON-LD in one pass
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, list):
                    data = data[0]
                offers = data.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0]
                price = offers.get("price")
                if price is not None:
                    try:
                        debug["price"] = float(price)
                    except (TypeError, ValueError):
                        debug["price"] = price
                # Seller check — Best Buy direct listings say "Best Buy" explicitly
                # in the offer's seller.name. No default here: defaulting a missing
                # key to "Best Buy" would silently trust an offer that never actually
                # said who's selling it.
                seller = offers.get("seller", {}).get("name")
                if seller:
                    debug["seller"] = seller
                if seller and "best buy" not in seller.lower():
                    return "THIRD_PARTY", sku, debug
                avail = offers.get("availability", "")
                if "InStock" in avail:
                    if seller and "best buy" in seller.lower():
                        # JSON-LD explicitly named Best Buy as the seller — trust it.
                        return "IN_STOCK", sku, debug
                    # Seller key was missing from this offer entirely (not just
                    # non-Best-Buy) — Best Buy's server HTML used to always render
                    # a "Sold by" section confirming this either way, but that's no
                    # longer reliably present since their pages went client-rendered.
                    # Fall back to a text search as a secondary check; if that also
                    # can't confirm it, stay unresolved rather than assume Best Buy.
                    if re.search(r"\bsold by\s+best buy\b", text, re.IGNORECASE):
                        return "IN_STOCK", sku, debug
                    return None, sku, debug
                if "OutOfStock" in avail or "SoldOut" in avail or "Discontinued" in avail:
                    return "OUT_OF_STOCK", sku, debug
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
        # Text fallbacks
        sold_by = re.search(r"Sold by\s+([^\n·|]+)", text, re.IGNORECASE)
        if sold_by:
            debug["seller"] = sold_by.group(1).strip()
        if sold_by and "best buy" not in sold_by.group(1).lower():
            return "THIRD_PARTY", sku, debug
        if re.search(r"\bAdd to Cart\b", text, re.IGNORECASE):
            return "IN_STOCK", sku, debug
        if re.search(r"\b(Sold Out|Unavailable|Out of Stock)\b", text, re.IGNORECASE):
            return "OUT_OF_STOCK", sku, debug
        return None, sku, debug
    except Exception:
        return None, None, debug


def _bestbuy_name(url):
    slug = url.rstrip("/").split("/")[-2]
    slug = re.sub(r"^pokemon-trading-card-game-", "", slug)
    return slug.replace("-", " ").title()


def check_bestbuy(state, seed=False, history=None):
    print("Checking Best Buy watch list...")
    new_alerts = 0
    for url in BESTBUY_WATCH:
        key = f"bestbuy_{url}"
        status, sku, debug = _bestbuy_stock_status(url)
        name = _bestbuy_name(url)

        if _should_log_stock(state, f"bestbuy_{url}", status, debug.get("price"), debug.get("qty_left")):
            _append_stock_log("Best Buy", name, url, status, price=debug.get("price"),
                               qty=debug.get("qty_left"), seller=debug.get("seller"),
                               release_date=debug.get("release_date"), state=state)

        if status is None:
            print(f"  [unknown] {name[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        prev = state.get(key)
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
            date_line = f"Release date spotted: **{debug['release_date']}**\n" if debug.get("release_date") else ""
            send_discord(
                f"**Coming Soon at Best Buy** 🔵\n"
                f"**{name}**\n"
                f"{date_line}"
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
                f"{qty_line(debug.get('qty_left')) or NO_LOW_STOCK_WARNING}"
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
    # Pitch Black (new, 2026-08-16) — seller unverified, debug mode
    "https://www.walmart.com/ip/Pok-mon-TCG-Mega-Evolution-Pitch-Black-Elite-Trainer-Box/20161351456",
    "https://www.walmart.com/ip/Pok-mon-TCG-Mega-Evolution-Pitch-Black-Booster-Box-ME05/20140716298",
    # 30th Celebration (new, 2026-08-30) — both current listings confirmed
    # marketplace-marked-up ($174-190 vs $49.99 MSRP), debug mode
    "https://www.walmart.com/ip/Pokemon-TCG-30th-Celebration-Elite-Trainer-Box-ETB/20754418655",
    "https://www.walmart.com/ip/Pokemon-TCG-30th-Celebration-Elite-Trainer-Box/20640569221",
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
    "https://www.walmart.com/ip/Pok-mon-TCG-Mega-Evolution-Pitch-Black-Elite-Trainer-Box/20161351456",
    "https://www.walmart.com/ip/Pok-mon-TCG-Mega-Evolution-Pitch-Black-Booster-Box-ME05/20140716298",
    "https://www.walmart.com/ip/Pokemon-TCG-30th-Celebration-Elite-Trainer-Box-ETB/20754418655",
    "https://www.walmart.com/ip/Pokemon-TCG-30th-Celebration-Elite-Trainer-Box/20640569221",
}


def _walmart_next_data_status(soup, debug):
    """Walmart's product pages are Next.js-rendered — real seller/availability/price
    data lives in the __NEXT_DATA__ JSON blob, not in static HTML text. BeautifulSoup's
    get_text() only sees ~1-2KB of nav/shell text on these pages regardless of whether
    the page loaded fine, which used to get misclassified as 'blocked' by a page-length
    check. Read the structured data directly instead — more reliable and immune to that.
    Returns (status, debug) with status set, or (None, debug) if NEXT_DATA is missing/
    unparseable so the caller can fall back to text-based heuristics.
    """
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None, debug
    try:
        product = (
            json.loads(tag.string)
            .get("props", {}).get("pageProps", {}).get("initialData", {})
            .get("data", {}).get("product", {})
        )
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None, debug
    if not product:
        return None, debug

    seller = product.get("sellerName") or ""
    avail = ((product.get("availabilityStatusV2") or {}).get("value")
             or product.get("availabilityStatus") or "").upper()
    price = (product.get("priceInfo", {}).get("currentPrice") or {}).get("price")

    debug["jsonld_seller"] = seller or None
    debug["jsonld_avail"] = avail or None
    if price is not None:
        debug["price"] = price
    walmart_seller = bool(seller) and "walmart" in seller.lower()
    debug["walmart_confirmed"] = walmart_seller

    if seller and not walmart_seller:
        return "THIRD_PARTY", debug
    if "OUT_OF_STOCK" in avail or "SOLDOUT" in avail or "SOLD_OUT" in avail:
        return "OUT_OF_STOCK", debug
    if "IN_STOCK" in avail:
        return ("IN_STOCK" if walmart_seller else None), debug
    return None, debug


def _walmart_stock_status(url):
    """Returns (status, debug) where status is 'IN_STOCK', 'OUT_OF_STOCK', 'COMING_SOON', 'THIRD_PARTY', or None."""
    debug = {"seller_text": None, "jsonld_seller": None, "jsonld_avail": None, "walmart_confirmed": False, "page_len": 0, "qty_left": None, "price": None, "release_date": None}
    try:
        r = cf.get(url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None, debug
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        debug["page_len"] = len(text)
        debug["qty_left"] = _qty_left(text)

        next_data_status, debug = _walmart_next_data_status(soup, debug)
        if next_data_status is not None:
            return next_data_status, debug

        if len(text) < 5000:
            print(f"  [blocked] {url.split('/')[-1][:45]}")
            return None, debug
        if re.search(r"\bComing Soon\b", text, re.IGNORECASE):
            debug["release_date"] = _release_date_text(text)
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
                price = offers.get("price")
                if price is not None:
                    try:
                        debug["price"] = float(price)
                    except (TypeError, ValueError):
                        debug["price"] = price
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


WALMART_LOG_RETENTION_DAYS = 90


def _append_walmart_log(name, url, status, debug, duration_minutes=None):
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
            "price": debug.get("price"),
            "qty_left": debug.get("qty_left"),
            "release_date": debug.get("release_date"),
            "duration_minutes": duration_minutes,
            "page_len": debug.get("page_len"),
            "url": url,
        })
        cutoff = (datetime.now() - timedelta(days=WALMART_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
        log = [e for e in log if e.get("ts", "") >= cutoff]
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
    now = datetime.now()
    for url in WALMART_WATCH:
        key = f"walmart_{url}"
        status, debug = _walmart_stock_status(url)
        name = _walmart_name(url)
        prev = state.get(key)

        # How long an item stayed IN_STOCK before it changed — the key stat for
        # judging whether polling cadence is fast enough to build a checkout bot on.
        duration_minutes = None
        instock_since_key = f"walmart_instock_since_{url}"
        if prev == "IN_STOCK" and status != "IN_STOCK":
            since = state.get(instock_since_key)
            if since:
                duration_minutes = round((now - datetime.fromisoformat(since)).total_seconds() / 60, 1)
            state.pop(instock_since_key, None)
        if status == "IN_STOCK" and prev != "IN_STOCK":
            state[instock_since_key] = now.isoformat()

        # Log every status/price/qty change immediately, plus an hourly heartbeat
        # even when nothing changed — keeps months of history without every
        # 5-minute check bloating the (git-committed) log file.
        if _should_log_stock(state, f"walmart_{url}", status, debug.get("price"), debug.get("qty_left")):
            _append_walmart_log(name, url, status, debug, duration_minutes=duration_minutes)

        if status is None:
            print(f"  [unknown] {name[:55]}")
            time.sleep(random.uniform(1, 3))
            continue
        if status == "THIRD_PARTY":
            # Buy-box flip: this listing was confirmed Walmart-direct and is now
            # showing a different seller — distinct from a normal sellout
            # (IN_STOCK -> OUT_OF_STOCK). Quiet, informational — not a buy signal.
            # Fires once on the transition only: state[key] must be updated here
            # since we `continue` past the general update below, otherwise prev
            # stays frozen at "IN_STOCK" and this re-fires every single check.
            if prev == "IN_STOCK":
                flip_seller = debug.get("seller_text") or debug.get("jsonld_seller") or "a 3rd-party seller"
                dur_text = f" after ~{duration_minutes:.0f} min" if duration_minutes is not None else ""
                send_discord(
                    f"🔁 **Walmart buy-box flipped**{dur_text}\n"
                    f"**{name}**\n"
                    f"Was sold directly by Walmart — now showing **{flip_seller}**. Don't buy at this price!\n{url}"
                )
                print(f"  [BUYBOX FLIP -> {flip_seller}] {name[:45]}")
            else:
                print(f"  [skipped 3rd party] {name[:50]}")
            state[key] = status
            time.sleep(random.uniform(1, 3))
            continue
        if not seed and status == "COMING_SOON" and prev != "COMING_SOON":
            date_line = f"Release date spotted: **{debug['release_date']}**\n" if debug.get("release_date") else ""
            send_discord(
                f"**Coming Soon at Walmart** 🟡\n"
                f"**{name}**\n"
                f"{date_line}"
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
                    f"{qty_line(debug.get('qty_left')) or NO_LOW_STOCK_WARNING}"
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
    """Returns (online_status, store_status, inv_count, price) — statuses are 'IN_STOCK', 'OUT_OF_STOCK', or None; inv_count/price are the real number or None."""
    store_url = f"{url}?storeid={MICROCENTER_STORE_ID}" if "?" not in url else f"{url}&storeid={MICROCENTER_STORE_ID}"
    price = None
    try:
        r = cf.get(store_url, impersonate="chrome124", timeout=20, allow_redirects=True)
        if not r.ok:
            return None, None, None, None
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        if len(text) < 500:
            print(f"  [blocked] {url[-45:]}")
            return None, None, None, None

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
                    if offers.get("price") is not None:
                        try:
                            price = float(offers["price"])
                        except (TypeError, ValueError):
                            price = offers["price"]
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

        return online_status, store_status, inv_count, price
    except Exception:
        return None, None, None, None


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
        online_status, store_status, inv_count, price = _microcenter_stock_status(url)
        online_key = f"mc_online_{url}"
        store_key  = f"mc_store_{url}"
        inv_key    = f"mc_inv_{url}"
        prev_online = state.get(online_key)
        prev_store  = state.get(store_key)
        prev_inv    = state.get(inv_key)  # last known count (int or None)

        log_status = store_status or online_status
        if _should_log_stock(state, f"mc_{url}", log_status, price, inv_count):
            _append_stock_log("Micro Center", name, url, log_status, store=MICROCENTER_STORE_NAME,
                               price=price, qty=inv_count, state=state)

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

GITHUB_TOKEN_EXPIRY = date(2026, 11, 10)

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
            price = f"${e['price']:.2f}" if e.get("price") is not None else "—"
            qty = e.get("qty_left")
            qty_str = f"{qty} left" if qty is not None else "—"
            dur = f" | in_stock_for={e['duration_minutes']}min" if e.get("duration_minutes") is not None else ""
            lines.append(
                f"`{e['ts']}` status=**{e['status']}** | "
                f"seller={seller} | avail={avail} | walmart={confirmed} | "
                f"price={price} | qty={qty_str}{dur}"
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
        _, sku, _ = _bestbuy_stock_status(url)
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

class _StateDelta(dict):
    """A read-through view of the shared state for one retailer's thread.

    Starts as a full copy so reads (state.get(...)) see everything, but only
    *records* what this thread actually sets or deletes. Threads run in
    parallel and finish in unpredictable order — merging each thread's full
    returned dict back with dict.update() would silently overwrite every
    other thread's changes with this thread's stale pre-run snapshot of
    them. Merging only the recorded deltas makes the merge order-independent.
    """
    def __init__(self, base):
        super().__init__(base)
        self.changed = {}
        self.deleted = set()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.changed[key] = value
        self.deleted.discard(key)

    def pop(self, key, *default):
        self.deleted.add(key)
        self.changed.pop(key, None)
        return super().pop(key, *default)


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
    # reads don't race; writes are recorded as a delta per thread and merged
    # back in afterward, so completion order can't clobber another thread's changes
    checks = [
        ("Target",           lambda: check_target(_StateDelta(state), seed=seed, history=history)),
        ("PC Site Queue",    lambda: check_pokemoncenter_site_queue(_StateDelta(state), seed=seed)),
        ("Pokemon Center",   lambda: check_pokemoncenter(_StateDelta(state), seed=seed, dynamic_pc_urls=dynamic_pc_urls)),
        ("PC Restock",       lambda: check_pokemoncenter_restock(_StateDelta(state), seed=seed, history=history, dynamic_pc_urls=list(dynamic_pc_urls))),
        ("Costco",           lambda: check_costco(_StateDelta(state), seed=seed, history=history)),
        ("Sam's Club",       lambda: check_samsclub(_StateDelta(state), seed=seed, history=history)),
        ("Best Buy",         lambda: check_bestbuy(_StateDelta(state), seed=seed, history=history)),
        ("Walmart",          lambda: check_walmart(_StateDelta(state), seed=seed, history=history)),
        ("Micro Center",     lambda: check_microcenter(_StateDelta(state), seed=seed, history=history)),
    ]

    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = {executor.submit(fn): name for name, fn in checks}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                state.update(getattr(result, "changed", result))
                for key in getattr(result, "deleted", ()):
                    state.pop(key, None)
                report_recovery(state, name)
            except Exception as e:
                print(f"  {name} check failed: {e}")
                report_error(state, name, str(e))

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
