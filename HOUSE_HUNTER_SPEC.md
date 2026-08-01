# House Hunter — Spec & Phases

## Goal

A scheduled job that:
1. Pulls listings from Funda (via `pyfunda`) matching a region + spec (price, bedrooms, size, etc.)
2. For each new listing, looks up nearby points of interest (schools, train stations, and others we add later) via Google Places
3. Emails the listing — photo, key details, and the nearby POIs — to you and your wife
4. Never emails the same listing twice

## Architecture

```
config.yaml          # region, spec filters, POI types/radii, email addresses, schedule
state.sqlite          # listing IDs already emailed (dedup)
house_hunter/
  search.py           # wraps pyfunda: fetch listings matching config
  poi.py               # Google Places lookups (schools, stations, distance)
  email.py             # builds + sends the HTML email via SMTP
  state.py             # sqlite read/write of "already sent" listing IDs
  run.py               # orchestrates: search -> filter new -> enrich -> email -> record
.github/workflows or cron / launchd  # the scheduler
```

**Flow per run (`run.py`):**
1. Load config (region, filters, POI settings, recipients).
2. `pyfunda` search → list of `Listing` objects matching spec.
3. Diff against `state.sqlite` → keep only unseen `global_id`s.
4. For each new listing: reverse-geocode/lookup nearby POIs from its `GeoLocation` (lat/lon) via Google Places (Nearby Search, ranked by distance) for each POI type (school, train_station, ...), keeping the closest N per type with walking/driving distance.
5. Render one email per listing (or a daily digest — see Phase 2 decision) with photo, price, address, key specs, and POI list with distances.
6. Send via SMTP to both addresses.
7. Record `global_id` in `state.sqlite` so it's never sent again.

## Config surface (`config.json`)

Stored as plain JSON on disk, read by `run.py` on every scheduled run:

```json
{
  "search": {
    "location": "amsterdam",
    "max_price": 500000,
    "min_bedrooms": 3
  },
  "poi": {
    "types": [
      { "key": "school", "google_place_type": "school", "max_results": 3, "max_radius_m": 2000 },
      { "key": "train_station", "google_place_type": "train_station", "max_results": 2, "max_radius_m": 3000 }
    ]
  },
  "email": {
    "to_addresses": ["you@example.com", "wife@example.com"]
  },
  "schedule": {
    "frequency": "daily"
  }
}
```

Secrets (SMTP password/app password, Google Places API key) go in environment variables / `.env`, never in `config.json`, and are **not** editable from the web form below.

## Config web form

A small local web app so both of you can edit `config.json` without touching a text editor.

- **Stack**: Flask (single `app.py`), one HTML template (Jinja2 + plain CSS, no JS framework needed), no database — it reads/writes `config.json` directly.
- **Fields**: location, min/max price, bedrooms, size; a repeatable POI section (type, max results, max radius); the two recipient email addresses; schedule frequency.
- **Behavior**: GET `/` renders the form pre-filled from the current `config.json`; POST validates and overwrites `config.json`; a "last saved" timestamp shown on the page confirms the write. Save triggers no immediate search run — the next scheduled run just picks up the new config.
- **Access**: runs on `localhost` (e.g. `python app.py` → `http://localhost:5000`), started manually when either of you wants to tweak preferences. No auth needed since it's local-only; not exposed to the internet.
- **Where it lives**: `house_hunter/webapp/` (`app.py`, `templates/form.html`), separate from the scheduled `run.py` so the scheduler doesn't depend on Flask running.

## Phases

### Phase 0 — Setup
- `uv sync` the pyfunda dev environment, confirm `client.search(...)` and `client.listing(...)` work end-to-end against a real region.
- Get a Google Places API key (Places API enabled) and a Gmail app password (or other SMTP creds).
- Build the config web form (see above) and use it to fill in the first real `config.json` — region, price range, bedrooms, POI settings, both recipient addresses.

### Phase 1 — Core search + dedup (no email yet)
- `house_hunter/search.py`: wraps `Funda.search()`/`iter_search()` with the config filters, returns `Listing` objects.
- `house_hunter/state.py`: SQLite table `sent_listings(global_id INTEGER PRIMARY KEY, sent_at TEXT)`.
- `run.py` v1: search → diff against state → print new matches to console (no email, no POIs yet). Verify it correctly finds new listings and doesn't re-show old ones on a second run.

### Phase 2 — POI enrichment
- `house_hunter/poi.py`: given a listing's lat/lon (`Listing.address.geo` or similar — confirm the field on `Listing`), call Google Places Nearby Search per configured POI type, compute distance (haversine or Google's returned distance), return top N per type.
- Wire into `run.py`: attach POI results to each new listing before the print step. Verify against a couple of known Amsterdam addresses that results look sane (right school, right station).

### Phase 3 — Email delivery
- `house_hunter/email.py`: HTML email template — listing photo (main image from `Listing.media`), title/address/price, a few key specs (size, rooms, year), POI list with distances, and a link back to the Funda listing.
- SMTP send via `smtplib` + `ssl`, using app-password auth.
- Wire into `run.py`: replace the console print with an actual send; on successful send, write to `state.sqlite`.
- Test with one deliberately-triggered listing to confirm formatting/deliverability (check spam folder too).

### Phase 4 — Scheduling
- Decide host: local machine (cron/launchd) vs. small always-on box/cloud (GitHub Actions scheduled workflow is a free, simple option since no server is needed).
- Wire up the chosen scheduler to run `run.py` daily (or whatever cadence you pick).
- Make failures visible — e.g. the job should not fail silently if Funda or Google Places changes/breaks (pyfunda's own README flags its endpoints as undocumented and liable to change).

### Phase 5 — Polish (optional, later)
- Digest mode: batch all new matches from a run into a single email instead of one-per-listing.
- More POI types (supermarket, park, gym) — just config additions given Phase 2's design.
- Simple "why this matched" / score in the email (e.g. under budget by X%, N min to nearest station).
- Unsubscribe/pause switch without deleting state.

## Open items to confirm before Phase 0 build starts
- Region/location string(s) and full filter spec (price, bedrooms, size, property type) — `pyfunda`'s `search()` kwargs need to be checked against `funda/search.py` for exact filter names.
- Confirm `Listing` actually exposes lat/lon coordinates (`GeoLocation` in `funda/listing.py`) — needed for Phase 2.
- Google Places API billing is pay-as-you-go past a free tier; fine at this run frequency/volume but worth knowing.
- Where this runs long-term (Phase 4) affects how secrets are stored (local `.env` vs. GitHub Actions secrets).
