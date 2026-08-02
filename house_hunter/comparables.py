"""Recently sold comparable listings near a given listing."""

from dataclasses import dataclass

from funda import Funda, FundaError, Listing


@dataclass
class Comparable:
    title: str
    price: int
    living_area: int | None
    url: str | None

    @property
    def price_per_m2(self) -> int | None:
        if self.living_area:
            return round(self.price / self.living_area)
        return None


def recently_sold_comparables(client: Funda, listing: Listing, limit: int = 3) -> list[Comparable]:
    try:
        data = client.similar_listings(listing)
    except (FundaError, LookupError, ValueError):
        return []

    sold_ids = (data or {}).get("recently_sold", [])[:limit]
    comparables = []
    for global_id in sold_ids:
        try:
            sold = client.listing(global_id)
        except FundaError:
            continue
        if sold.price.amount is None:
            continue
        comparables.append(
            Comparable(
                title=sold.title or "Unknown address",
                price=sold.price.amount,
                living_area=sold.living_area,
                url=sold.url,
            )
        )
    return comparables
