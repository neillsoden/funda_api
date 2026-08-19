"""Funda's search backend occasionally returns an embedded per-query error
(e.g. "status 401: no token provided") inside an outer HTTP 200 response -
a transient hiccup pyfunda's own retry logic never sees, since it only
inspects the outer HTTP status (see funda/constants.py RETRY_STATUS_CODES
and funda/funda.py's _raise_for_msearch_error). Small wrapper for call
sites that consume client.iter_search()."""

import time
from typing import Callable, TypeVar

from funda.exceptions import SearchError

T = TypeVar("T")


def with_retry(fn: Callable[[], T], *, retries: int = 2, backoff_seconds: float = 5.0) -> T:
    last_exc: SearchError | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except SearchError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff_seconds)
    raise last_exc
