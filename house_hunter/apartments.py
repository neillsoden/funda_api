"""Nationwide apartment search for the /apartments page - independent of the
city-scoped house_hunter search/config in search.py. Experimental/testing
feature, deliberately paced: scans a small batch of listings at a time
(cursor-tracked in state.sqlite) instead of one big nationwide burst, to
avoid hammering Funda's API. Matches persist in state.sqlite (no cap) so
they survive restarts and accumulate over time. Run on a fixed schedule by
house_hunter/scheduler.py, not triggered by webapp page views - only one
process should ever be scanning at a time.
"""

import math
import time

from funda import Funda

from house_hunter.config import load_config
from house_hunter.email_report import (
    _contrast_text_color,
    _days_listed,
    _energy_color,
    _maps_directions_url,
    _price_budget_color,
)
from house_hunter.poi import distance_to, transit_ride_minutes
from house_hunter.search import _is_actually_available, budget_for_label, is_under_bid
from house_hunter.state import (
    get_nl_apartments_scan_cursor,
    mark_nl_apartments_scanned,
    nl_apartments_scanned_ids,
    rejected_listing_ids,
    save_nl_apartment_match,
    set_nl_apartments_scan_cursor,
)
from house_hunter.vrijescholen import nearest_vrijeschool, schools_in_city

PAGE_SIZE = 15
DETAIL_WORKERS = 3  # deliberately gentler than find_matching_listings' 8

# Utrecht Centraal - used as the "commute to Utrecht by train" reference
# point (no specific work address was given, and everything into Utrecht
# funnels through Centraal anyway). Google's transit API has no notion of
# "intercity" specifically - just a generic train mode - so this is the
# fastest train route Google finds, which for NL is normally the intercity.
# max_utrecht_minutes filters on the ride itself (time actually on the
# train), not the door-to-door total - see poi.transit_ride_minutes().
UTRECHT_CENTRAAL = (52.0894, 5.1101)


def _nearby_schools(coords: tuple[float, float], city: str | None, max_school_minutes: float) -> list[dict]:
    """All vrijescholen worth showing for this listing: every school in the
    same city (there can be more than one), plus the true nationwide-nearest
    in case it's just outside the city boundary - deduped, and only ones
    actually within max_school_minutes biking.
    """
    candidates = {school["id"]: school for school in schools_in_city(city)}
    nearest = nearest_vrijeschool(coords[0], coords[1])
    if nearest:
        candidates.setdefault(nearest["id"], nearest)

    results = []
    for school in candidates.values():
        dist = distance_to(coords[0], coords[1], school["lat"], school["lng"], mode="bicycling")
        if dist.duration_minutes is not None and dist.duration_minutes <= max_school_minutes:
            results.append(
                {
                    "name": school["title"],
                    "km": round(dist.km, 1),
                    "minutes": round(dist.duration_minutes),
                    "maps_url": _maps_directions_url(coords, dist),
                }
            )
    results.sort(key=lambda s: s["minutes"])
    return results


def _utrecht_maps_url(coords: tuple[float, float]) -> str:
    return (
        f"https://www.google.com/maps/dir/?api=1&origin={coords[0]},{coords[1]}"
        f"&destination={UTRECHT_CENTRAAL[0]},{UTRECHT_CENTRAAL[1]}&travelmode=transit"
    )


def _card_dict(listing, schools: list[dict], utrecht_ride_minutes: float, mortgage_budget: dict) -> dict:
    energy_bg = _energy_color(listing.energy_label)
    coords = listing.location.coordinates
    budget = budget_for_label(mortgage_budget, listing.energy_label)
    listed_date, days_listed = _days_listed(listing)
    if days_listed is not None and days_listed <= 1:
        new_badge = "NEW TODAY"
    elif days_listed is not None and days_listed <= 7:
        new_badge = "NEW THIS WEEK"
    else:
        new_badge = None
    return {
        "id": listing.id,
        "url": listing.url,
        "photo": listing.media.photo_urls[0] if listing.media.photo_urls else "",
        "title": listing.title,
        "city": listing.city,
        "neighbourhood": listing.address.neighbourhood,
        "price": f"€{listing.price.amount:,}" if listing.price.amount else "price unknown",
        "price_color": _price_budget_color(listing.price.amount, budget),
        "budget_total_text": f"€{budget:,}" if budget is not None else None,
        "living_area": listing.living_area,
        "bedrooms": listing.bedrooms,
        "energy_label": listing.energy_label or "?",
        "energy_bg": energy_bg,
        "energy_text": _contrast_text_color(energy_bg),
        "schools": schools,
        "utrecht_minutes": round(utrecht_ride_minutes),
        "utrecht_maps_url": _utrecht_maps_url(coords),
        "listed_short": f"{days_listed}d ago" if days_listed is not None else None,
        "listed_title": f"Listed {listed_date}" if listed_date else "Listed date unknown",
        "new_badge": new_badge,
        "favorited_by": [],
    }


def scan_batch(*, batch_pages: int = 1) -> int:
    """Scans one small batch (batch_pages search-result pages, ~15 listings
    each) starting from the persisted cursor, evaluates only the ones not
    already scanned before, and persists any real matches. Returns how many
    NEW matches this batch found. Intended to be called repeatedly (e.g. a
    few times per scheduled run, paced with a short sleep between calls)
    rather than scanning everything nationwide in one go. No cap on total
    accumulated matches - it just keeps finding more over time.

    Criteria (area/bedrooms/school/Utrecht thresholds) come from
    config.json's nl_apartments block, editable on the Preferences page.
    Budget is the same per-energy-label mortgage table from the bank used
    for the Houses search (config.json search.mortgage_budget), not a flat
    price cap - a good energy label buys more borrowing capacity.
    """
    config = load_config()
    apt_cfg = config.get("nl_apartments", {})
    # `or` (not .get(key, default)) deliberately - a blank form field saves
    # as a literal None in config.json, which .get() would happily return
    # instead of falling back to the default.
    min_area = apt_cfg.get("min_area") or 90
    max_area = apt_cfg.get("max_area") or 110
    min_bedrooms = apt_cfg.get("min_bedrooms") or 3
    max_school_minutes = apt_cfg.get("max_school_minutes") or 15
    max_utrecht_minutes = apt_cfg.get("max_utrecht_minutes") or 80

    mortgage_budget = config["search"].get("mortgage_budget") or {}
    price_ceiling = max(mortgage_budget.values()) if mortgage_budget else 400_000

    start_page = get_nl_apartments_scan_cursor()

    with Funda() as client:
        candidate_ids: list[str] = []
        pages_fetched = 0
        for listing in client.iter_search(
            None,
            category="buy",
            object_type="apartment",
            min_area=min_area,
            max_area=max_area,
            min_bedrooms=min_bedrooms,
            max_price=price_ceiling,
            start_page=start_page,
            max_pages=batch_pages,
        ):
            if listing.id:
                candidate_ids.append(listing.id)

        # Exhausted all pages for this filter - wrap back to the start so
        # newly listed apartments eventually get picked up too.
        pages_fetched = math.ceil(len(candidate_ids) / PAGE_SIZE) if candidate_ids else batch_pages
        next_page = start_page + pages_fetched
        if len(candidate_ids) < batch_pages * PAGE_SIZE:
            next_page = 0
        set_nl_apartments_scan_cursor(next_page)

        if not candidate_ids:
            return 0

        already_scanned = nl_apartments_scanned_ids(candidate_ids)
        unseen_ids = [cid for cid in candidate_ids if cid not in already_scanned]
        if not unseen_ids:
            return 0

        rejected_ids = rejected_listing_ids(unseen_ids)
        listings = client.listings(unseen_ids, workers=DETAIL_WORKERS)
        mark_nl_apartments_scanned(unseen_ids)

        new_matches = 0
        for listing in listings:
            if listing.id in rejected_ids:
                continue
            if not _is_actually_available(listing):
                continue
            if is_under_bid(listing):
                continue
            if not listing.property_details.features.get("has_garden"):
                continue
            budget = budget_for_label(mortgage_budget, listing.energy_label)
            if budget is not None and listing.price.amount is not None and listing.price.amount > budget:
                continue

            coords = listing.location.coordinates
            if not coords:
                continue
            schools = _nearby_schools(coords, listing.city, max_school_minutes)
            if not schools:
                continue

            ride_minutes = transit_ride_minutes(
                coords[0], coords[1], UTRECHT_CENTRAAL[0], UTRECHT_CENTRAAL[1]
            )
            if ride_minutes is None or ride_minutes > max_utrecht_minutes:
                continue

            save_nl_apartment_match(listing.id, _card_dict(listing, schools, ride_minutes, mortgage_budget))
            new_matches += 1

        return new_matches


def scan_until_target(
    *,
    max_batches: int = 3,
    pause_seconds: float = 3.0,
    **kwargs,
) -> int:
    """Runs a few paced scan_batch() passes in sequence (with a short pause
    between each) - bounds how much a single run can pull from Funda at
    once. Returns total new matches found this run.
    """
    total_new = 0
    for i in range(max_batches):
        total_new += scan_batch(**kwargs)
        if i < max_batches - 1:
            time.sleep(pause_seconds)
    return total_new
