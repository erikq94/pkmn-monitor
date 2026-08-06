# Pokebot — Pokemon TCG Restock Monitor

## What this is

A Python script (`pkmn_monitor.py`) that runs every 5 minutes via GitHub Actions and sends Discord alerts when Pokemon TCG products go in stock at retail price. Alerts go to a Discord server shared with friends via webhook.

## Critical rules — never break these

- **Retail price only** — only alert when sold DIRECTLY by the retailer, never 3rd party/marketplace sellers
- **No GameStop** — marks up prices
- **No credentials stored** for any retailer
- **No Co-Authored-By Claude** lines in git commits

## Retailers monitored

| Retailer       | Online | Local Stores             | Auto-discovery                    | 3rd party filter              |
| -------------- | ------ | ------------------------ | --------------------------------- | ----------------------------- |
| Target         | ✅     | ✅ 5 stores (RedSky API) | ✅ dynamic_tcins.json             | ✅ is_sold_by_target()        |
| Pokemon Center | ✅     | N/A                      | ✅ sitemap + dynamic_pc_urls.json | ✅ no marketplace             |
| Costco         | ✅     | ❌                       | ❌ manual                         | ✅ no marketplace             |
| Sam's Club     | ✅     | ❌                       | ❌ manual                         | ✅ no marketplace             |
| Best Buy       | ✅     | ✅ 3 stores (tcfb API)   | ❌ manual                         | ✅ requires "sold by" text    |
| Walmart        | ✅     | ❌                       | ❌ manual                         | ✅ requires "sold by walmart" |

## Target stores near 95122

- 1984: San Jose Story Road
- 2238: San Jose East
- 1426: San Jose Capitol
- 2281: San Jose Central
- 2088: San Jose College Park

## Best Buy stores near 95122

- 1423: San Jose Curtner (181 Curtner Ave)
- 190: San Jose Almaden (5065 Almaden Expy)
- 851: San Jose Stevens Creek (3090 Stevens Creek Blvd)

## Key files

| File                            | Purpose                                           |
| ------------------------------- | ------------------------------------------------- |
| `pkmn_monitor.py`               | Main script                                       |
| `seen_products.json`            | State — prevents duplicate alerts                 |
| `restock_history.json`          | Restock log — used for weekly pattern summary     |
| `dynamic_tcins.json`            | Auto-discovered Target TCINs                      |
| `dynamic_pc_urls.json`          | Auto-discovered Pokemon Center URLs               |
| `walmart_log.json`              | Walmart debug log (seller/availability per check) |
| `stock_log.json`                | Price/qty/status log for Target, Best Buy, Costco, Sam's Club, Micro Center, Pokemon Center |
| `watchlist.md`                  | Human-readable product list, shareable link       |
| `.github/workflows/monitor.yml` | GitHub Actions cron (every 5 min)                 |
| `requirements.txt`              | requests, beautifulsoup4, curl_cffi               |

## Status types used internally

`IN_STOCK`, `OUT_OF_STOCK`, `COMING_SOON`, `IN_STORE_ONLY`, `THIRD_PARTY`, `QUEUE`, `INVITE`, `None`

## Alert rules

- `@everyone` — IN_STOCK online, QUEUE open, Sam's Club/Costco restock, nearby Best Buy store confirmed
- Quiet message (no @everyone) — Best Buy IN_STORE_ONLY not nearby, INVITE drop, COMING_SOON, new product discovered
- Silent — THIRD_PARTY, OUT_OF_STOCK, blocked pages

## Key technical details

- Uses `curl_cffi` with `impersonate="chrome124"` to bypass bot protection (Akamai)
- `ThreadPoolExecutor(max_workers=7)` — all retailers check in parallel
- `threading.Lock()` on restock history writes
- Walmart: requires positive "sold by walmart" text match before alerting (prevents marketplace false positives)
- Best Buy: requires "Sold by" section in HTML before trusting JSON-LD InStock
- Costco/Sam's Club: detects Queue-it redirects (`queue-it.net` in URL or page text)
- Sam's Club: timed drop reminders in `SAMSCLUB_DROPS` dict (UTC times, fires 15min before and at drop time)

## Manual run modes (GitHub Actions → Run workflow → enter mode)

- `bb-stores` — check all 3 Best Buy stores for current inventory, send Discord summary
- `walmart-log` — send last 5 Walmart debug log entries per product to Discord
- `status` — Target inventory status report to Discord
- `seed` — reset state without sending alerts

## Upcoming products to watch (as of May 2026)

- **Pitch Black** (Mega Evolution set) — releases July 17, 2026. Best Buy ETB already in watch list. Target/PC/Walmart listings not live yet — auto-discovery will catch them.

## GitHub repo

`github.com/erikq94/pkmn-monitor`
Watchlist (shareable): `github.com/erikq94/pkmn-monitor/blob/main/watchlist.md`
GitHub token expires: 2026-08-11
