"""SQLite-backed record of listings already notified on, so we can tell a
genuinely new listing apart from one we've already emailed, and detect when
an already-seen listing's asking price has since dropped.
"""

import sqlite3
from contextlib import contextmanager

from house_hunter.config import state_path


def _migrate_favorites_to_per_person(conn: sqlite3.Connection) -> None:
    """One-time migration: the favorites table used to be listing_id-only
    (anonymous). Preserve any existing rows under a generic 'household' label
    rather than losing them.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(favorites)")}
    if "person" in columns:
        return
    conn.execute("ALTER TABLE favorites RENAME TO favorites_old")
    conn.execute(
        """
        CREATE TABLE favorites (
            listing_id TEXT NOT NULL,
            person TEXT NOT NULL,
            favorited_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (listing_id, person)
        )
        """
    )
    old_rows = conn.execute("SELECT listing_id, favorited_at FROM favorites_old").fetchall()
    conn.executemany(
        "INSERT OR IGNORE INTO favorites (listing_id, person, favorited_at) VALUES (?, 'household', ?)",
        old_rows,
    )
    conn.execute("DROP TABLE favorites_old")


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
                listing_id TEXT NOT NULL,
                person TEXT NOT NULL,
                favorited_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (listing_id, person)
            )
            """
        )
        _migrate_favorites_to_per_person(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rejected (
                listing_id TEXT PRIMARY KEY,
                rejected_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS under_bid_listings (
                listing_id TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_checked_at TEXT NOT NULL DEFAULT (datetime('now'))
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


def add_favorite(listing_id: str, person: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (listing_id, person, favorited_at) VALUES (?, ?, datetime('now'))",
            (listing_id, person),
        )


def remove_favorite(listing_id: str, person: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM favorites WHERE listing_id = ? AND person = ?", (listing_id, person)
        )


def favorited_by(listing_ids: list[str]) -> dict[str, set[str]]:
    """{listing_id: {person, ...}} for each listing_id that has at least one
    favorite. Everyone can see everyone else's favorites - this is shared
    household state, not per-person private data.
    """
    if not listing_ids:
        return {}
    with _connect() as conn:
        placeholders = ",".join("?" for _ in listing_ids)
        rows = conn.execute(
            f"SELECT listing_id, person FROM favorites WHERE listing_id IN ({placeholders})",
            listing_ids,
        )
        result: dict[str, set[str]] = {}
        for listing_id, person in rows:
            result.setdefault(listing_id, set()).add(person)
        return result


def all_favorited_listing_ids() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT listing_id FROM favorites ORDER BY favorited_at DESC"
        )
        return [row[0] for row in rows]


def add_rejected(listing_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO rejected (listing_id, rejected_at) VALUES (?, datetime('now'))",
            (listing_id,),
        )


def remove_rejected(listing_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM rejected WHERE listing_id = ?", (listing_id,))


def rejected_listing_ids(listing_ids: list[str] | None = None) -> set[str]:
    """All rejected ('not interested') listing IDs, or just the ones present in
    listing_ids if given. Rejected listings should never be emailed again,
    regardless of price changes.
    """
    with _connect() as conn:
        if listing_ids is None:
            rows = conn.execute("SELECT listing_id FROM rejected")
        elif not listing_ids:
            return set()
        else:
            placeholders = ",".join("?" for _ in listing_ids)
            rows = conn.execute(
                f"SELECT listing_id FROM rejected WHERE listing_id IN ({placeholders})",
                listing_ids,
            )
        return {row[0] for row in rows}


def sync_under_bid_listings(listing_ids: list[str]) -> None:
    """Replace the under-bid set with exactly what's under bid right now -
    upserts current ones (keeping first_seen_at), drops anything no longer
    under bid (reopened or sold)."""
    with _connect() as conn:
        if listing_ids:
            conn.executemany(
                """
                INSERT INTO under_bid_listings (listing_id, first_seen_at, last_checked_at)
                VALUES (?, datetime('now'), datetime('now'))
                ON CONFLICT(listing_id) DO UPDATE SET last_checked_at = datetime('now')
                """,
                [(listing_id,) for listing_id in listing_ids],
            )
            placeholders = ",".join("?" for _ in listing_ids)
            conn.execute(
                f"DELETE FROM under_bid_listings WHERE listing_id NOT IN ({placeholders})",
                listing_ids,
            )
        else:
            conn.execute("DELETE FROM under_bid_listings")


def under_bid_listing_ids() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT listing_id FROM under_bid_listings ORDER BY first_seen_at DESC"
        )
        return [row[0] for row in rows]
