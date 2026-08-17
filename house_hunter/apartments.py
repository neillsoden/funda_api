"""Nationwide apartment search for the /apartments browse page - independent
of the city-scoped house_hunter search/config in search.py. Experimental/
testing feature: capped to a small number of results, not wired into the
scheduled email pipeline.
"""

import math

from funda import Funda

from house_hunter.email_report import _contrast_text_color, _energy_color
from house_hunter.poi import distance_to
from house_hunter.search import _is_actually_available
from house_hunter.state import favorited_by, rejected_listing_ids
from house_hunter.vrijescholen import nearest_vrijeschool

PAGE_SIZE = 15


def search_nl_apartments(
    *,
    min_area: int = 90,
    max_area: int = 110,
    min_rooms: int = 3,
    max_school_minutes: float = 15,
    max_results: int = 25,
    max_scanned: int = 150,
) -> list[dict]:
    """Nationwide (no location filter) apartment search: ~min_area-max_area
    sqm, min_rooms+ total rooms, has a garden, within max_school_minutes
    biking of ANY vrijeschool in the cached directory (not just one city's
    nearest). Returns display-ready card dicts, most matches found first.

    Bounded two ways: max_scanned caps how many search-result candidates get
    a full detail fetch (garden data only exists on the detail response, not
    the search response), and max_results stops early once enough real
    matches are found. May return fewer than max_results if max_scanned is
    hit first - acceptable for this experimental/testing feature.
    """
    with Funda() as client:
        max_pages = math.ceil(max_scanned / PAGE_SIZE)
        candidate_ids: list[str] = []
        for listing in client.iter_search(
            None,
            category="buy",
            object_type="apartment",
            min_area=min_area,
            max_area=max_area,
            min_rooms=min_rooms,
            max_pages=max_pages,
        ):
            if listing.id:
                candidate_ids.append(listing.id)
            if len(candidate_ids) >= max_scanned:
                break

        if not candidate_ids:
            return []

        rejected_ids = rejected_listing_ids(candidate_ids)
        favorites_by_listing = favorited_by(candidate_ids)
        candidates = client.listings(candidate_ids, workers=8)

        matches: list[dict] = []
        for listing in candidates:
            if listing.id in rejected_ids:
                continue
            if not _is_actually_available(listing):
                continue
            if not listing.property_details.features.get("has_garden"):
                continue

            coords = listing.location.coordinates
            if not coords:
                continue
            school = nearest_vrijeschool(coords[0], coords[1])
            if not school:
                continue
            school_distance = distance_to(
                coords[0], coords[1], school["lat"], school["lng"], mode="bicycling"
            )
            if school_distance.duration_minutes is None or school_distance.duration_minutes > max_school_minutes:
                continue

            energy_bg = _energy_color(listing.energy_label)
            matches.append(
                {
                    "id": listing.id,
                    "url": listing.url,
                    "photo": listing.media.photo_urls[0] if listing.media.photo_urls else "",
                    "title": listing.title,
                    "city": listing.city,
                    "neighbourhood": listing.address.neighbourhood,
                    "price": f"€{listing.price.amount:,}" if listing.price.amount else "price unknown",
                    "living_area": listing.living_area,
                    "rooms_total": listing.rooms_count,
                    "energy_label": listing.energy_label or "?",
                    "energy_bg": energy_bg,
                    "energy_text": _contrast_text_color(energy_bg),
                    "school_name": school["title"],
                    "school_km": round(school_distance.km, 1),
                    "school_minutes": round(school_distance.duration_minutes),
                    "favorited_by": sorted(favorites_by_listing.get(listing.id, set())),
                }
            )
            if len(matches) >= max_results:
                break

        return matches
