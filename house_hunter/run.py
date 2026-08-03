"""Search Funda, enrich new/price-dropped listings, email the digest, and
record what was notified.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv
from funda import Funda

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from house_hunter.config import load_config  # noqa: E402
from house_hunter.email_report import EnrichedListing, build_email_html, send_email  # noqa: E402
from house_hunter.enrich import enrich_listing, sort_key  # noqa: E402
from house_hunter.search import find_matching_listings, is_under_bid  # noqa: E402
from house_hunter.state import (  # noqa: E402
    classify_listings,
    clicked_listing_ids,
    favorited_by,
    last_notified_price,
    record_notified,
    record_run,
    rejected_listing_ids,
    sync_under_bid_listings,
)


def run(force: bool = False, reason: str = "scheduled") -> None:
    """force=True sends the full current digest regardless of what's already
    been notified (still excludes anything marked "not interested" - that
    signal holds even on a forced send). reason is just for the run log
    (startup/scheduled/manual/force) shown in the webapp - it doesn't affect
    behavior.
    """
    config = load_config()
    matched_count = 0

    try:
        with Funda() as client:
            listings = find_matching_listings(client, config)
            matched_count = len(listings)
            print(f"{len(listings)} listings matched the search filters")

            under_bid_ids = [listing.id for listing in listings if listing.id and is_under_bid(listing)]
            sync_under_bid_listings(under_bid_ids)
            if under_bid_ids:
                print(f"{len(under_bid_ids)} under bid (logged in the Onder bod tab, not emailed unless they reopen)")

            available_listings = [listing for listing in listings if not is_under_bid(listing)]

            events = classify_listings(
                [(listing.id, listing.price.amount) for listing in available_listings if listing.id]
            )
            rejected_ids = rejected_listing_ids([listing.id for listing in available_listings if listing.id])

            if force:
                notify_listings = [listing for listing in available_listings if listing.id not in rejected_ids]
                print(f"Force send: {len(notify_listings)} listings (ignoring dedup)")
            else:
                notify_listings = [
                    listing for listing in available_listings if listing.id in events and listing.id not in rejected_ids
                ]
                new_count = sum(1 for e in events.values() if e == "new")
                drop_count = sum(1 for e in events.values() if e == "price_drop")
                skipped_rejected = sum(
                    1 for listing in available_listings if listing.id in events and listing.id in rejected_ids
                )
                print(f"{new_count} new, {drop_count} price drops, {skipped_rejected} skipped (marked not interested)")

            if not notify_listings:
                print("Nothing to send.")
                record_run(reason, "ok", matched_count, 0, "nothing new to send")
                return

            viewed_ids = clicked_listing_ids([listing.id for listing in notify_listings if listing.id])
            favorites_by_listing = favorited_by([listing.id for listing in notify_listings if listing.id])
            enriched: list[EnrichedListing] = []
            for listing in notify_listings:
                price_drop_from = (
                    last_notified_price(listing.id) if events.get(listing.id) == "price_drop" else None
                )
                enriched.append(
                    enrich_listing(
                        client,
                        listing,
                        config,
                        price_drop_from=price_drop_from,
                        already_viewed=listing.id in viewed_ids,
                        is_new=events.get(listing.id) == "new",
                        favorited_by_people=favorites_by_listing.get(listing.id, set()),
                    )
                )

        enriched.sort(key=sort_key(config))

        cities = sorted({item.listing.city for item in enriched if item.listing.city})
        locations = ", ".join(cities) if cities else ", ".join(
            loc.title() for loc in config["search"]["locations"]
        )
        suffix = "full digest" if force else "new matches"
        title = f"House Hunter — {locations} ({suffix})"
        public_base_url = config.get("server", {}).get("public_base_url", "")
        people = config.get("people", [])
        html = build_email_html(enriched, title, public_base_url, people)

        to_addresses = config["email"]["to_addresses"]
        if not to_addresses:
            print("No recipients configured in config.json (email.to_addresses) — skipping send.")
            record_run(reason, "ok", matched_count, 0, "no recipients configured")
            return

        send_email(subject=title, html=html, to_addresses=to_addresses)
        record_notified([(item.listing.id, item.listing.price.amount) for item in enriched])
        print(f"Sent {len(enriched)} listings to {', '.join(to_addresses)}")
        record_run(reason, "ok", matched_count, len(enriched), f"sent {len(enriched)} listings")
    except Exception as exc:
        record_run(reason, "error", matched_count, 0, str(exc))
        raise


def browse_listings(config: dict | None = None) -> list[EnrichedListing]:
    """Full enrichment for every currently active, non-rejected matching
    listing (not just new/price-dropped ones) - used by the /houses
    swipe page. Independent of the email dedup state, but respects
    "not interested" rejections and skips under-bid listings.
    """
    config = config or load_config()
    with Funda() as client:
        listings = find_matching_listings(client, config)
        available_listings = [listing for listing in listings if not is_under_bid(listing)]
        rejected_ids = rejected_listing_ids([listing.id for listing in available_listings if listing.id])
        browsable = [listing for listing in available_listings if listing.id not in rejected_ids]

        events = classify_listings([(listing.id, listing.price.amount) for listing in browsable if listing.id])
        viewed_ids = clicked_listing_ids([listing.id for listing in browsable if listing.id])
        favorites_by_listing = favorited_by([listing.id for listing in browsable if listing.id])

        enriched: list[EnrichedListing] = []
        for listing in browsable:
            price_drop_from = (
                last_notified_price(listing.id) if events.get(listing.id) == "price_drop" else None
            )
            enriched.append(
                enrich_listing(
                    client,
                    listing,
                    config,
                    price_drop_from=price_drop_from,
                    already_viewed=listing.id in viewed_ids,
                    is_new=events.get(listing.id) == "new",
                    favorited_by_people=favorites_by_listing.get(listing.id, set()),
                )
            )

    enriched.sort(key=sort_key(config))
    return enriched


if __name__ == "__main__":
    run()
