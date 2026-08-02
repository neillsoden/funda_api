"""Search Funda for listings matching the house_hunter config."""

from funda import Funda, Listing


def budget_for_label(mortgage_budget: dict[str, int], energy_label: str | None) -> int | None:
    """Max mortgage capacity for a given energy label. Unknown/unlisted labels
    use the lowest (most conservative) budget in the table.
    """
    if not mortgage_budget:
        return None
    label = (energy_label or "").upper()
    return mortgage_budget.get(label, min(mortgage_budget.values()))


def _within_mortgage_budget(listing: Listing, mortgage_budget: dict[str, int]) -> bool:
    """Max borrowing capacity depends on the property's own energy label (better
    label -> lower interest -> higher capacity), so the price cap isn't a flat
    number - it's per label.
    """
    if not mortgage_budget or listing.price.amount is None:
        return True
    budget = budget_for_label(mortgage_budget, listing.energy_label)
    return listing.price.amount <= budget


_ACTIVE_STATUSES = {"available", "negotiations"}


def _is_actually_available(listing: Listing) -> bool:
    """Safety net against a listing that's flipped to sold/unavailable between
    the search call and the detail fetch, or lingers in a stale search index.
    """
    return (listing.status or "").lower() in _ACTIVE_STATUSES


def find_matching_listings(client: Funda, config: dict) -> list[Listing]:
    """Search every configured location and return full-detail Listing objects,
    deduplicated across locations and filtered to the energy-label-aware
    mortgage budget.
    """
    search_cfg = config["search"]
    mortgage_budget = search_cfg.get("mortgage_budget") or {}
    highest_budget = max(mortgage_budget.values()) if mortgage_budget else None

    kwargs = {
        "category": search_cfg.get("category", "buy"),
        "min_bedrooms": search_cfg.get("min_bedrooms"),
        "max_bedrooms": search_cfg.get("max_bedrooms"),
        "min_price": search_cfg.get("min_price"),
        "max_price": highest_budget or search_cfg.get("max_price"),
        "min_area": search_cfg.get("min_area"),
        "max_area": search_cfg.get("max_area"),
        "radius_km": search_cfg.get("radius_km"),
    }
    if search_cfg.get("object_type"):
        kwargs["object_type"] = search_cfg["object_type"]
    kwargs = {key: value for key, value in kwargs.items() if value is not None}

    ids: list[str] = []
    for location in search_cfg["locations"]:
        results = client.search(location, **kwargs)
        ids.extend(listing.id for listing in results if listing.id)

    unique_ids = list(dict.fromkeys(ids))
    if not unique_ids:
        return []

    listings = client.listings(unique_ids, workers=8)
    category = search_cfg.get("category", "buy")
    return [
        listing
        for listing in listings
        if _within_mortgage_budget(listing, mortgage_budget)
        and (category == "sold" or _is_actually_available(listing))
    ]
