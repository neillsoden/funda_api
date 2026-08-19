"""Nationwide rental search for the /rentals page - same architecture and
criteria as house_hunter/apartments.py (garden, bedrooms, school/Utrecht
thresholds), just category="rent" with a flat monthly price cap instead of
the buy-side mortgage-budget-by-energy-label logic. Reuses the shared
Utrecht/school helpers from apartments.py rather than duplicating them.
"""

import math
import time

from funda import Funda

from house_hunter.apartments import UTRECHT_CENTRAAL, _nearby_schools, _utrecht_maps_url
from house_hunter.config import load_config
from house_hunter.email_report import _contrast_text_color, _days_listed, _energy_color, _price_budget_color
from house_hunter.funda_retry import with_retry
from house_hunter.poi import transit_ride_minutes
from house_hunter.search import _is_actually_available, is_under_bid
from house_hunter.state import (
    get_nl_rentals_scan_cursor,
    mark_nl_rentals_scanned,
    nl_rentals_scanned_ids,
    rejected_listing_ids,
    save_nl_rental_match,
    set_nl_rentals_scan_cursor,
)

PAGE_SIZE = 15
DETAIL_WORKERS = 3


def _card_dict(listing, schools: list[dict], utrecht_ride_minutes: float, max_price: int) -> dict:
    energy_bg = _energy_color(listing.energy_label)
    coords = listing.location.coordinates
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
        "price": f"€{listing.price.amount:,} /mo" if listing.price.amount else "price unknown",
        "price_color": _price_budget_color(listing.price.amount, max_price),
        "budget_total_text": f"€{max_price:,}/mo cap",
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
    """Same paced-batch pattern as apartments.scan_batch() - see that
    docstring. Criteria come from config.json's nl_rentals block."""
    config = load_config()
    cfg = config.get("nl_rentals", {})
    min_area = cfg.get("min_area") or 90
    max_area = cfg.get("max_area") or 110
    min_bedrooms = cfg.get("min_bedrooms") or 3
    max_price = cfg.get("max_price") or 2000
    max_school_minutes = cfg.get("max_school_minutes") or 15
    max_utrecht_minutes = cfg.get("max_utrecht_minutes") or 80

    start_page = get_nl_rentals_scan_cursor()

    with Funda() as client:
        def _collect() -> list[str]:
            ids = []
            for listing in client.iter_search(
                None,
                category="rent",
                object_type="apartment",
                min_area=min_area,
                max_area=max_area,
                min_bedrooms=min_bedrooms,
                max_price=max_price,
                start_page=start_page,
                max_pages=batch_pages,
            ):
                if listing.id:
                    ids.append(listing.id)
            return ids

        # Funda's backend occasionally rejects a query with a transient
        # embedded 401 ("no token provided") that pyfunda doesn't retry on
        # its own - see house_hunter/funda_retry.py.
        candidate_ids = with_retry(_collect)

        pages_fetched = math.ceil(len(candidate_ids) / PAGE_SIZE) if candidate_ids else batch_pages
        next_page = start_page + pages_fetched
        if len(candidate_ids) < batch_pages * PAGE_SIZE:
            next_page = 0
        set_nl_rentals_scan_cursor(next_page)

        if not candidate_ids:
            return 0

        already_scanned = nl_rentals_scanned_ids(candidate_ids)
        unseen_ids = [cid for cid in candidate_ids if cid not in already_scanned]
        if not unseen_ids:
            return 0

        rejected_ids = rejected_listing_ids(unseen_ids)
        listings = client.listings(unseen_ids, workers=DETAIL_WORKERS)
        mark_nl_rentals_scanned(unseen_ids)

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
            if listing.price.amount is not None and listing.price.amount > max_price:
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

            save_nl_rental_match(listing.id, _card_dict(listing, schools, ride_minutes, max_price))
            new_matches += 1

        return new_matches


def scan_until_target(*, max_batches: int = 3, pause_seconds: float = 3.0, **kwargs) -> int:
    total_new = 0
    for i in range(max_batches):
        total_new += scan_batch(**kwargs)
        if i < max_batches - 1:
            time.sleep(pause_seconds)
    return total_new
