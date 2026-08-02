"""Search Funda, enrich new/price-dropped listings, email the digest, and
record what was notified.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv
from funda import Funda

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from house_hunter.comparables import recently_sold_comparables  # noqa: E402
from house_hunter.config import load_config  # noqa: E402
from house_hunter.email_report import EnrichedListing, build_email_html, send_email  # noqa: E402
from house_hunter.market import get_market_insights  # noqa: E402
from house_hunter.poi import distance_to, nearest_place  # noqa: E402
from house_hunter.pricing import previous_sale  # noqa: E402
from house_hunter.search import budget_for_label, find_matching_listings  # noqa: E402
from house_hunter.state import classify_listings, clicked_listing_ids, last_notified_price, record_notified  # noqa: E402
from house_hunter.vrijescholen import nearest_vrijeschool  # noqa: E402


def run() -> None:
    config = load_config()

    with Funda() as client:
        listings = find_matching_listings(client, config)
        print(f"{len(listings)} listings matched the search filters")

        events = classify_listings([(listing.id, listing.price.amount) for listing in listings if listing.id])
        notify_listings = [listing for listing in listings if listing.id in events]
        new_count = sum(1 for e in events.values() if e == "new")
        drop_count = sum(1 for e in events.values() if e == "price_drop")
        print(f"{new_count} new, {drop_count} price drops")

        if not notify_listings:
            print("Nothing new to send.")
            return

        places = config["poi"]["places"]
        nearest_types = config["poi"].get("nearest_types", [])
        mortgage_budget = config["search"].get("mortgage_budget") or {}
        viewed_ids = clicked_listing_ids([listing.id for listing in notify_listings if listing.id])
        enriched: list[EnrichedListing] = []
        for listing in notify_listings:
            coords = listing.location.coordinates
            distances = {}
            if coords:
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
                        distances[label] = distance_to(
                            coords[0], coords[1], place_lat, place_lng, mode="bicycling"
                        )

            price_drop_from = (
                last_notified_price(listing.id) if events.get(listing.id) == "price_drop" else None
            )

            school_distance_km = None
            if coords:
                school = nearest_vrijeschool(coords[0], coords[1])
                if school:
                    school_distance = distance_to(
                        coords[0], coords[1], school["lat"], school["lng"], mode="bicycling"
                    )
                    distances[f"{school['title']} (vrijeschool)"] = school_distance
                    school_distance_km = school_distance.km

            enriched.append(
                EnrichedListing(
                    listing=listing,
                    distances=distances,
                    previous_sale=previous_sale(client, listing),
                    mortgage_budget=budget_for_label(mortgage_budget, listing.energy_label),
                    market=get_market_insights(client, listing.city, listing.address.neighbourhood),
                    comparables=recently_sold_comparables(client, listing),
                    price_drop_from=price_drop_from,
                    school_distance_km=school_distance_km,
                    max_school_distance_km=config["search"].get("max_school_distance_km"),
                    already_viewed=listing.id in viewed_ids,
                    is_new=events.get(listing.id) == "new",
                )
            )

    max_school_distance_km = config["search"].get("max_school_distance_km")

    def _sort_key(item: EnrichedListing) -> tuple[int, float]:
        within_range = (
            max_school_distance_km is None
            or item.school_distance_km is None
            or item.school_distance_km <= max_school_distance_km
        )
        fallback = min((d.km for d in item.distances.values()), default=999)
        primary_distance = item.school_distance_km if item.school_distance_km is not None else fallback
        return (0 if within_range else 1, primary_distance)

    enriched.sort(key=_sort_key)

    cities = sorted({item.listing.city for item in enriched if item.listing.city})
    locations = ", ".join(cities) if cities else ", ".join(
        loc.title() for loc in config["search"]["locations"]
    )
    title = f"House Hunter — {locations} (new matches)"
    public_base_url = config.get("server", {}).get("public_base_url", "")
    html = build_email_html(enriched, title, public_base_url)

    to_addresses = config["email"]["to_addresses"]
    if not to_addresses:
        print("No recipients configured in config.json (email.to_addresses) — skipping send.")
        return

    send_email(subject=title, html=html, to_addresses=to_addresses)
    record_notified([(item.listing.id, item.listing.price.amount) for item in enriched])
    print(f"Sent {len(enriched)} listings to {', '.join(to_addresses)}")


if __name__ == "__main__":
    run()
