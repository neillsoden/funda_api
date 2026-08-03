"""Shared per-listing enrichment logic (distances, budget, market data) used
by both the email pipeline (run.py) and the browse/swipe page (webapp).
"""

from funda import Funda, Listing

from house_hunter.comparables import recently_sold_comparables
from house_hunter.email_report import EnrichedListing
from house_hunter.market import get_market_insights
from house_hunter.poi import distance_to, nearest_place
from house_hunter.pricing import previous_sale
from house_hunter.search import budget_for_label
from house_hunter.vrijescholen import nearest_vrijeschool


def enrich_listing(
    client: Funda,
    listing: Listing,
    config: dict,
    *,
    price_drop_from: int | None = None,
    already_viewed: bool = False,
    is_new: bool = True,
    favorited_by_people: set[str] | None = None,
    include_extras: bool = True,
) -> EnrichedListing:
    """include_extras=False skips the fixed-place/nearest-type distances and
    the previous-sale/market/comparables Funda lookups - used by the houses
    swipe deck, which only shows school distance + budget, to keep building
    it (up to ~40 listings) fast."""
    places = config["poi"]["places"]
    nearest_types = config["poi"].get("nearest_types", [])
    mortgage_budget = config["search"].get("mortgage_budget") or {}

    coords = listing.location.coordinates
    distances = {}
    if coords and include_extras:
        for place in places:
            place_city = place.get("city")
            if place_city and listing.city and place_city.lower() != listing.city.lower():
                continue  # place is scoped to a different city than this listing
            distances[place["name"]] = distance_to(
                coords[0], coords[1], place["lat"], place["lng"], mode="bicycling"
            )
        for nearest_type in nearest_types:
            found = nearest_place(coords[0], coords[1], nearest_type["google_place_type"])
            if found:
                name, place_lat, place_lng = found
                label = f"{nearest_type['label']} ({name})"
                distances[label] = distance_to(coords[0], coords[1], place_lat, place_lng, mode="bicycling")

    school_distance_km = None
    if coords:
        school = nearest_vrijeschool(coords[0], coords[1])
        if school:
            school_distance = distance_to(coords[0], coords[1], school["lat"], school["lng"], mode="bicycling")
            distances[f"{school['title']} (vrijeschool)"] = school_distance
            school_distance_km = school_distance.km

    return EnrichedListing(
        listing=listing,
        distances=distances,
        previous_sale=previous_sale(client, listing) if include_extras else None,
        mortgage_budget=budget_for_label(mortgage_budget, listing.energy_label),
        market=get_market_insights(client, listing.city, listing.address.neighbourhood) if include_extras else None,
        comparables=recently_sold_comparables(client, listing) if include_extras else [],
        price_drop_from=price_drop_from,
        school_distance_km=school_distance_km,
        max_school_distance_km=config["search"].get("max_school_distance_km"),
        already_viewed=already_viewed,
        is_new=is_new,
        favorited_by=favorited_by_people or set(),
    )


def sort_key(config: dict):
    max_school_distance_km = config["search"].get("max_school_distance_km")

    def _key(item: EnrichedListing) -> tuple[int, float]:
        within_range = (
            max_school_distance_km is None
            or item.school_distance_km is None
            or item.school_distance_km <= max_school_distance_km
        )
        fallback = min((d.km for d in item.distances.values()), default=999)
        primary_distance = item.school_distance_km if item.school_distance_km is not None else fallback
        return (0 if within_range else 1, primary_distance)

    return _key
