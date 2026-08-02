"""Look up a listing's most recent prior sale price, if any."""

from dataclasses import dataclass

from funda import Funda, FundaError, Listing


@dataclass
class PreviousSale:
    price: int
    date: str


def previous_sale(client: Funda, listing: Listing) -> PreviousSale | None:
    try:
        history = client.price_history(listing)
    except FundaError:
        return None

    sold_changes = [
        change
        for change in history.changes
        if change.status == "asking_price" and change.badge_text == "Verkocht"
    ]
    if not sold_changes:
        return None

    latest = sold_changes[0]
    if latest.price is None or latest.date is None:
        return None
    return PreviousSale(price=latest.price, date=latest.date)
