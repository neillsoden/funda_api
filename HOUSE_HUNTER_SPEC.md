# House Hunter — Status

## Goal

A job that:
1. Pulls listings from Funda (via `pyfunda`) matching a region + spec (price, bedrooms, size, energy label, mortgage budget)
2. Enriches each new/changed listing with real biking distances (vrijeschool, station, landmarks), neighbourhood market data, recently sold comparables, and mortgage-budget fit
3. Emails matches — photo, key details, all enrichment — to the household
4. Tracks what's already been sent, and re-alerts on asking-price drops
5. Runs as multiple independent instances, one per city/region

## Multi-instance setup

Each city/region is a fully independent instance: its own config file and its own dedup database, selected via the `HOUSE_HUNTER_INSTANCE` env var.

| Instance | Config | State DB | Recipients |
|---|---|---|---|
| Arnhem (default) | `config.json` | `state.sqlite` | ns@neillsoden.co.za, yvonnesoden@ytje.co.za |
| Den Bosch | `config.den_bosch.json` | `state.den_bosch.sqlite` | ns@neillsoden.co.za |

Run any instance: `HOUSE_HUNTER_INSTANCE=<name> uv run python -m house_hunter.run` (omit the env var for the default/Arnhem instance). Same pattern for the web form. Adding a new city is just a new `config.<name>.json` file, following the Den Bosch one as a template — no code changes needed.

Both current instances: buy, houses only, min 3 bedrooms, €300k–€410k fallback cap, same mortgage-budget table (A €410,000 / B €406,600 / C €399,200 / D €398,700 / E–F €392,250), 5km vrijeschool biking-distance threshold (primary sort, not a hard cutoff — farther listings still show, just lower and badged "outside").

Nijmegen was removed from the Arnhem instance's `locations` (was combined before) — not yet rebuilt as its own instance.

## Deployment target

**LAN-only** — will run on `192.168.178.6`, not exposed to the internet. This simplifies things: no HTTPS/domain/reverse-proxy needed, and the webapp's lack of authentication is a much smaller concern since only devices on the home network can reach it.

**Scoped to Arnhem only for the initial deployment** (Neill's choice, 2026-08-02) — `docker-compose.yml` currently only defines the Arnhem services:

| Service | Purpose | Port on 192.168.178.6 |
|---|---|---|
| `house_hunter_arnhem` | Arnhem scheduler loop (checks daily, only emails on new/changed listings) | — |
| `house_hunter_arnhem_webapp` | Arnhem config form | 5000 |

Den Bosch is fully built and tested (manually run, emails sent successfully) but deliberately **not** in the main compose file yet. Its services live in `docker-compose.den_bosch.yml` and can be added alongside the Arnhem deployment whenever wanted:
```bash
docker compose -f docker-compose.yml -f docker-compose.den_bosch.yml up -d --build
```

`config.json`'s `server.public_base_url` is already set to `http://192.168.178.6:5000`, so click tracking activates automatically the moment this is deployed there — no further config changes needed at deploy time.

Deploy steps on that box (Arnhem only):
```bash
git clone https://github.com/neillsoden/funda_api.git && cd funda_api
cp .env.example .env   # fill in real Google/Mailgun keys
touch state.sqlite   # must exist before the bind-mount
docker compose up -d --build
```

**I (Claude) do not have SSH/remote access to 192.168.178.6** — these are prepared files for Neill to run there himself; I can't deploy this directly.

## What's built and working

**Package layout** (`house_hunter/`):
- `config.py` — loads/saves per-instance config with sane defaults; `config_path()`/`state_path()` resolve the right files based on `HOUSE_HUNTER_INSTANCE`
- `search.py` — multi-location Funda search, energy-label-aware mortgage budget filter, and a status safety-net that drops anything not `available`/`negotiations` (excludes sold, and "Verkocht onder voorbehoud" / under offer)
- `state.py` — SQLite `tracked_listings` table (new vs. price-drop detection) and `clicks` table (click tracking, see below)
- `poi.py` — geocoding, real travel distance/time via the **Routes API** (`computeRouteMatrix`, driving or bicycling) with straight-line haversine fallback, nearest-place-of-type search (Google Places, used for "nearest train station")
- `vrijescholen.py` — automatic nearest-vrijeschool lookup. Full directory (152 schools, vrijescholen.nl's public API) is cached in a `vrijescholen` table in state.sqlite and refreshed at most once every 30 days — normal runs hit the local table only, no API call
- `market.py` — neighbourhood market insights (avg asking €/m²) via pyfunda, cached per run
- `comparables.py` — recently sold nearby properties via pyfunda's `similar_listings`
- `pricing.py` — prior "sold" price/date from pyfunda's price history (**see caveat below** — this is not the real transaction price)
- `email_report.py` — HTML digest: card-per-listing with a "NEW" badge on the photo corner for genuinely new listings, solid color-coded energy pill (A++++ through G, contrast-aware text), Material Icon chips grouped into sections, every distance chip is a clickable Google Maps directions link in the correct travel mode, price-drop banner, recently-sold mini-panel, mortgage-budget-fit chip, green/amber/red left-accent border scaled to vrijeschool proximity, "Previously viewed" badge, mobile-safe spacing (explicit spacer rows, not CSS margin)
- `run.py` — orchestrates: search → classify new/price-drop → enrich (POI, school, market, comps, budget, click history) → sort (within school-distance budget first) → build + send email → record state
- `scheduler.py` — loop that calls `run()` then sleeps per `config.schedule.frequency`, for the eventual Docker deployment
- `webapp/` — local Flask form to edit a config (locations, price/bedroom/area filters, fixed places with optional city-scoping, max school distance, recipients, schedule, public base URL), plus the `/click/<id>` redirect-and-log endpoint for click tracking

**Infrastructure**:
- `Dockerfile.house_hunter` + `docker-compose.yml` — four services (scheduler + webapp × 2 instances), config/state/`.env` mounted from outside the image, see Deployment target above
- `.env` / `.env.example` — Google Places/Geocoding/Routes API key, Mailgun SMTP creds; gitignored along with all `config*.json` and `state*.sqlite` files
- Repo pushed to `github.com/neillsoden/funda_api` (pyfunda upstream kept as a separate `upstream` remote)
- Fixed a real security issue before this goes anywhere public: Flask's `debug=True` (RCE risk if network-exposed) is off
- Click tracking is **built and configured, but not yet live**: `server.public_base_url` is set in both configs pointing at the LAN deployment target, but until that box is actually running the webapp, links still go straight to Funda/Maps (there's nothing to route through yet)

**Verified live**: search + mortgage-budget + sold-status filtering, per-city nearest vrijeschool with persistent caching, real bike distance/time to school/station/landmarks via Routes API, clickable Maps directions links, NEW badge, energy label colors, multi-instance isolation (Arnhem and Den Bosch run and email independently without interfering with each other's dedup state).

## Known caveat — "last sold" price is not the real transaction price

Confirmed by inspecting Funda's own price-history data: the "Verkocht" (sold) entry and the "Vraagprijs" (asking) entry immediately before it always show the **same number**. Funda doesn't capture the actual negotiated/paid price — it just relabels the last asking price as "sold." The real number lives at the **Kadaster** (Dutch land registry), which isn't free to query per-address.
→ **Not yet fixed**: still deciding whether to (a) relabel the field honestly as "last asking price," or (b) wire up a paid Kadaster lookup for the real figure.

## What's left

1. **Relabel or fix the sold-price field** (see caveat above) — undecided.
2. **Actually run `docker compose up -d --build` on 192.168.178.6** — files are ready and Arnhem-scoped (see Deployment target above); Neill needs to run this himself, I don't have remote access to that box.
3. **Add Den Bosch to the live deployment** — whenever wanted, via `docker-compose.den_bosch.yml` on top of the main compose file (see above). Fully built/tested already, just deliberately excluded from today's initial deploy.
4. **NS (Nederlandse Spoorwegen) API** — discussed as a better source than Google for real train stations/journey times; needs an NS API subscription key, not built.
5. **Real favorite/star button in emails** — different from click tracking ("previously viewed" already works); an actual favorite action needs its own endpoint + table. Not started.
6. **Mortgage budget table isn't editable via the web form** — hand-edit the config file directly for now; the form no longer wipes it on save, but there's no UI for it.
7. **Webapp has no authentication** — low priority now (LAN-only), but still worth addressing before any public/internet exposure.
8. **Nijmegen instance** — removed from Arnhem's config, not yet rebuilt as its own `config.nijmegen.json` (same 2-minute process as Den Bosch, just say the word).
9. Suggested-but-not-built: price-per-m² ranking across a whole digest, broker info/reviews, contact-form availability flag, instant alerts via `new_listings()` polling instead of periodic runs, lightweight per-listing status (viewed/contacted/rejected) as a mini CRM.

## Verification

Run any instance manually:
```bash
uv run python -m house_hunter.run                              # default (Arnhem)
HOUSE_HUNTER_INSTANCE=den_bosch uv run python -m house_hunter.run
```
Edit preferences locally:
```bash
uv run python house_hunter/webapp/app.py   # http://localhost:5000
```
