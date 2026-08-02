"""Neighbourhood market insight lookups, cached per (city, neighbourhood) for
the lifetime of one run since many listings share the same neighbourhood.
"""

from funda import Funda, FundaError

_cache: dict[tuple[str, str], dict | None] = {}


def get_market_insights(client: Funda, city: str, neighbourhood: str) -> dict | None:
    if not city or not neighbourhood:
        return None
    key = (city, neighbourhood)
    if key not in _cache:
        try:
            _cache[key] = client.market_insights(city, neighbourhood)
        except (FundaError, LookupError, ValueError):
            _cache[key] = None
    return _cache[key]
