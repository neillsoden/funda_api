"""SQLite-backed record of listings already notified on, so we can tell a
genuinely new listing apart from one we've already emailed, and detect when
an already-seen listing's asking price has since dropped.
"""

import sqlite3
from contextlib import contextmanager

from house_hunter.config import state_path


@contextmanager
def _connect():
    conn = sqlite3.connect(state_path())
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_listings (
                listing_id TEXT PRIMARY KEY,
                first_seen_price INTEGER,
                last_notified_price INTEGER,
                first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_checked_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clicks (
                listing_id TEXT PRIMARY KEY,
                first_clicked_at TEXT NOT NULL DEFAULT (datetime('now')),
                click_count INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                listing_id TEXT PRIMARY KEY,
                favorited_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def classify_listings(listings: list[tuple[str, int | None]]) -> dict[str, str]:
    """listings: [(listing_id, current_price), ...].
    Returns {listing_id: "new" | "price_drop"} for the ones worth emailing.
    """
    if not listings:
        return {}
    with _connect() as conn:
        placeholders = ",".join("?" for _ in listings)
        ids = [listing_id for listing_id, _ in listings]
        known = dict(
            conn.execute(
                f"SELECT listing_id, last_notified_price FROM tracked_listings "
                f"WHERE listing_id IN ({placeholders})",
                ids,
            )
        )

    events: dict[str, str] = {}
    for listing_id, price in listings:
        if listing_id not in known:
            events[listing_id] = "new"
        elif (
            price is not None
            and known[listing_id] is not None
            and price < known[listing_id]
        ):
            events[listing_id] = "price_drop"
    return events


def last_notified_price(listing_id: str) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_notified_price FROM tracked_listings WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()
    return row[0] if row else None


def record_notified(listings: list[tuple[str, int | None]]) -> None:
    """Upsert last_notified_price after a listing has actually been emailed."""
    if not listings:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO tracked_listings
                (listing_id, first_seen_price, last_notified_price, first_seen_at, last_checked_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(listing_id) DO UPDATE SET
                last_notified_price = excluded.last_notified_price,
                last_checked_at = datetime('now')
            """,
            [(listing_id, price, price) for listing_id, price in listings],
        )


def record_click(listing_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO clicks (listing_id, first_clicked_at, click_count)
            VALUES (?, datetime('now'), 1)
            ON CONFLICT(listing_id) DO UPDATE SET
                click_count = click_count + 1
            """,
            (listing_id,),
        )


def clicked_listing_ids(listing_ids: list[str]) -> set[str]:
    if not listing_ids:
        return set()
    with _connect() as conn:
        placeholders = ",".join("?" for _ in listing_ids)
        rows = conn.execute(
            f"SELECT listing_id FROM clicks WHERE listing_id IN ({placeholders})",
            listing_ids,
        )
        return {row[0] for row in rows}


def add_favorite(listing_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (listing_id, favorited_at) VALUES (?, datetime('now'))",
            (listing_id,),
        )


def remove_favorite(listing_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM favorites WHERE listing_id = ?", (listing_id,))


def favorited_listing_ids(listing_ids: list[str] | None = None) -> set[str]:
    """All favorited listing IDs, or just the ones present in listing_ids if given."""
    with _connect() as conn:
        if listing_ids is None:
            rows = conn.execute("SELECT listing_id FROM favorites")
        elif not listing_ids:
            return set()
        else:
            placeholders = ",".join("?" for _ in listing_ids)
            rows = conn.execute(
                f"SELECT listing_id FROM favorites WHERE listing_id IN ({placeholders})",
                listing_ids,
            )
        return {row[0] for row in rows}
