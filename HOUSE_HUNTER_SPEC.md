# House Hunter — Status

## Goal

A tool with two independent search tracks, both backed by `pyfunda`:

1. **Houses** — city-scoped search (currently Arnhem), matching a region + spec (price, bedrooms, size, energy label, mortgage budget). Enriches each new/changed listing with real biking distances (vrijeschool, station, landmarks), neighbourhood market data, recently sold comparables, and mortgage-budget fit. Emails matches to the household, tracks what's already been sent, and re-alerts on asking-price drops.
2. **NL Apartments** — nationwide, garden apartments only, built up gradually by a paced background scanner (not an email digest). See its own section below.

Both are browsable/actionable via a shared Flask webapp (favorite, reject, tag), which also has a Tinder-style swipe deck for Houses.

## Multi-instance setup (Houses only)

Each city/region is a fully independent instance: its own config file and its own dedup database, selected via the `HOUSE_HUNTER_INSTANCE` env var.

| Instance | Config | State DB | Recipients |
|---|---|---|---|
| Arnhem (default, live) | `config.json` | `state.sqlite` | ns@neillsoden.co.za, yvonnesoden@ytje.co.za |
| Den Bosch (built, not deployed) | `config.den_bosch.json` | `state.den_bosch.sqlite` | ns@neillsoden.co.za |

Run any instance: `HOUSE_HUNTER_INSTANCE=<name> uv run python -m house_hunter.run` (omit the env var for the default/Arnhem instance). Adding a new city is a new `config.<name>.json` file — no code changes needed.

Arnhem: buy, houses only, min 3 bedrooms, €300k–€410k fallback cap, mortgage-budget table by energy label (A €410,000 / B €406,600 / C €399,200 / D €398,700 / E–F €392,250), 5km vrijeschool biking-distance threshold (primary sort, not a hard cutoff), hourly checks 08:00–22:00 Europe/Amsterdam.

NL Apartments is **not** part of the multi-instance system — it's nationwide and lives in the same instance's state.sqlite as extra tables, independent of `config["search"]`.

## Deployment

**Live and public**: `https://home.amglab.dev` (nginx reverse proxy on `192.168.178.6` → webapp container on port 5010). Real session-based login (hashed passwords via `AUTH_USERS_B64`, rate limiting, secure cookies) — two accounts, `neill` and `yvonne`.

| Service | Purpose | Port on 192.168.178.6 |
|---|---|---|
| `house_hunter_arnhem` | Scheduler container: Houses hourly checks (08:00–22:00) + NL Apartments paced scan (4x/day) | — |
| `house_hunter_arnhem_webapp` | Web UI | 5010 |

Den Bosch is built/tested but not in the live compose file; its services live in `docker-compose.den_bosch.yml`.

Deploy loop: `git push` locally → on the server, `git pull && docker compose up -d --build`. `config.json`/`.env`/`state.sqlite` are gitignored and live only on the server (`~/funda_api/data/`, directory bind-mount — a single-file bind mount silently loses SQLite writes, learned the hard way).

## What's built and working

**Package layout** (`house_hunter/`):
- `config.py` — loads/saves per-instance config; `config_path()`/`state_path()` resolve based on `HOUSE_HUNTER_INSTANCE` and `HOUSE_HUNTER_DATA_DIR`
- `search.py` — multi-location Funda search (paginated via `iter_search`, not `search`), energy-label-aware mortgage budget filter, status safety-net (`_is_actually_available`), `is_under_bid()` (checks `raw_status` directly since pyfunda normalizes "Onder bod" to the generic "negotiations")
- `enrich.py` — shared per-listing enrichment (distances, budget, market data) used by both the email pipeline and the Houses browse page; `include_extras=False` skips market/comparables/sale-history for the faster browse path
- `apartments.py` — the NL Apartments scanner, see its own section below
- `state.py` — SQLite tables: `tracked_listings` (dedup/price-drop), `clicks`, `favorites` (per-person), `rejected`, `under_bid_listings`, `run_log` (7-day pipeline history), `nl_apartment_matches`/`nl_apartments_scanned`/`nl_apartments_scan_cursor` (apartments), `condition_tags` (generic, listing-id-keyed manual "needs work"/"move-in ready" tag)
- `poi.py` — geocoding, real travel distance/time via the Routes API (`computeRouteMatrix`: driving/bicycling/transit) with straight-line fallback; `transit_ride_minutes()` uses the fuller `computeRoutes` endpoint to isolate just the in-vehicle transit time (the matrix endpoint only returns door-to-door, no way to exclude walk-to-station time)
- `vrijescholen.py` — vrijeschool directory (153 schools, vrijescholen.nl's public API), cached and refreshed at most every 30 days; `nearest_vrijeschool()` (closest of all, nationwide) and `schools_in_city()` (all schools in one city, for listings near more than one)
- `market.py` / `comparables.py` / `pricing.py` — market insights, recently-sold comparables, prior "sold" price (see caveat below)
- `email_report.py` — HTML digest for Houses: card-per-listing, NEW badge, price colored by mortgage-budget fit, energy pill, clickable Maps directions chips, price-drop banner, favorite/reject action chips
- `run.py` — `run()` orchestrates the Houses email pipeline; `browse_listings()` is the lighter-weight path for the Houses swipe deck (all active non-rejected matches, not just new ones)
- `scheduler.py` — container's long-running process: Houses checks at fixed times (`schedule.times`) in one loop, NL Apartments paced scan on an independent 4x/day thread
- `webapp/` — Flask app, see routes below

**Webapp pages**:
- `/` — Preferences form (search filters, places, schedule, recipients), "Check now"/"Force send" buttons, last-5-runs summary
- `/houses` — Tinder-style swipe deck (drag or tap heart/X), tabbed by city, cached 20 min
- `/apartments` — NL Apartments grid (3-4 per row), filters (condition tag, viewed/not-viewed), see below
- `/favorites`, `/rejected`, `/onder-bod`, `/schools`, `/logs` — list views
- `/login`, `/logout` — session auth

## NL Apartments — nationwide garden-apartment scanner

Independent of the Houses config/pipeline. Criteria: ~90–110 m², 3+ bedrooms (Funda's `bedrooms` field specifically, not the generic "kamers" total which includes the living room), has a garden, mortgage-budget-fit by energy label (same bank table as Houses), within 15 min biking of **any** vrijeschool in the same city (not just the nearest overall), train ride itself under 1h20 to Utrecht Centraal (in-vehicle time only, not door-to-door), not under bid, not already rejected.

**Why it's built the way it is**:
- Garden data (`property_details.features["has_garden"]`) only exists on the full listing *detail* response, not search results — so every candidate needs a detail fetch regardless, same two-step pattern as the Houses search.
- Deliberately paced to avoid hammering Funda: `scan_batch()` does one small batch (~15 candidates) from a persisted DB cursor, `scan_until_target()` chains 3 batches with a pause between; 3 parallel detail-fetch workers (vs. 8 for Houses). Every scanned listing ID is marked in `nl_apartments_scanned` so it's never re-fetched, even across restarts.
- No cap on total matches — the scheduler runs this 4x/day (6-hour interval) independently of Houses' schedule, and it's the *only* process that scans (not triggered by webapp page views), so there's no cross-container race on the shared cursor/scanned state.
- Matches persist directly in `state.sqlite` (`nl_apartment_matches`), so the grid reads instantly regardless of scan state.

**On the card**: photo, price (colored by budget fit), bedrooms, area, garden badge, one pill per nearby vrijeschool (not just the nearest — a city can have more than one), Utrecht train-ride pill, energy label, listing age, "NEW TODAY"/"NEW THIS WEEK" badge (from Funda's own listing date), "Viewed" badge (via the same `/click` tracker as emails). Condition tag buttons ("Needs work"/"Move-in ready") and viewed/not-viewed + tag filters, all client-side after the initial render.

**Known hit rate**: narrow — stacking that many independent filters means roughly ~2% of scanned candidates pass all of them in early testing. Expected, not a bug.

## Known caveat — "last sold" price is not the real transaction price

Confirmed by inspecting Funda's own price-history data: the "Verkocht" (sold) entry and the "Vraagprijs" (asking) entry immediately before it always show the same number. Funda doesn't expose the real negotiated price — the actual figure lives at the Kadaster (Dutch land registry), not free to query per-address. Not fixed; not planned (cost doesn't seem worth it for this use case).

## What's left

1. **Sold-price field still mislabeled** (see caveat above) — undecided whether to relabel honestly or pay for Kadaster lookups.
2. **NL Apartments has no city tabs yet** — flat grid; worth adding once the unrestricted 4x/day scanning accumulates enough matches across enough cities to need it (discussed, not built).
3. **NL Apartments isn't emailed** — deliberately deferred until the criteria are proven out; browse-only for now.
4. **Mortgage budget table isn't editable via the web form** — hand-edit `config.json` directly; reused as-is by NL Apartments too.
5. **Den Bosch instance** built/tested but not in the live deploy.
6. **Nijmegen instance** — not built (same effort as Den Bosch, on request).
7. Suggested-but-not-built: price-per-m² ranking across a whole digest, broker info/reviews, NS (Nederlandse Spoorwegen) API for more precise train data than Google's generic transit mode.

## Verification

Run Houses manually:
```bash
uv run python -m house_hunter.run                              # default (Arnhem)
HOUSE_HUNTER_INSTANCE=den_bosch uv run python -m house_hunter.run
```
Run one NL Apartments scan batch manually:
```bash
uv run python -c "from house_hunter.apartments import scan_until_target; print(scan_until_target())"
```
Edit preferences / browse locally:
```bash
uv run python house_hunter/webapp/app.py   # http://localhost:5000
```
