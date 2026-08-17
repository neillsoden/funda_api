"""SQLite-backed record of listings already notified on, so we can tell a
genuinely new listing apart from one we've already emailed, and detect when
an already-seen listing's asking price has since dropped.
"""

import json
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                triggered_at TEXT NOT NULL DEFAULT (datetime('now')),
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                matched_count INTEGER,
                sent_count INTEGER,
                detail TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nl_apartment_matches (
                listing_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                found_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nl_apartments_scanned (
                listing_id TEXT PRIMARY KEY,
                scanned_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nl_apartments_scan_cursor (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                next_page INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS condition_tags (
                listing_id TEXT PRIMARY KEY,
                tag TEXT NOT NULL,
                tagged_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS apartment_filter_prefs (
                username TEXT PRIMARY KEY,
                tag_filter TEXT NOT NULL DEFAULT 'all',
                viewed_filter TEXT NOT NULL DEFAULT 'all',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS school_favorites (
                school_id TEXT NOT NULL,
                person TEXT NOT NULL,
                favorited_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (school_id, person)
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


def record_run(
    reason: str,
    status: str,
    matched_count: int | None = None,
    sent_count: int | None = None,
    detail: str | None = None,
) -> None:
    """Log one pipeline run (reason: startup/scheduled/manual/force, status:
    ok/error), and prune anything older than 7 days so the log doesn't grow
    forever."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO run_log (triggered_at, reason, status, matched_count, sent_count, detail)
            VALUES (datetime('now'), ?, ?, ?, ?, ?)
            """,
            (reason, status, matched_count, sent_count, detail),
        )
        conn.execute("DELETE FROM run_log WHERE triggered_at < datetime('now', '-7 days')")


def recent_run_logs() -> list[dict]:
    """Run log entries from the last 7 days, most recent first."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT triggered_at, reason, status, matched_count, sent_count, detail
            FROM run_log
            WHERE triggered_at >= datetime('now', '-7 days')
            ORDER BY triggered_at DESC
            """
        ).fetchall()
        return [
            {
                "triggered_at": row[0],
                "reason": row[1],
                "status": row[2],
                "matched_count": row[3],
                "sent_count": row[4],
                "detail": row[5],
            }
            for row in rows
        ]


def save_nl_apartment_match(listing_id: str, data: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO nl_apartment_matches (listing_id, data_json, found_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(listing_id) DO UPDATE SET data_json = excluded.data_json
            """,
            (listing_id, json.dumps(data)),
        )


def remove_nl_apartment_match(listing_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM nl_apartment_matches WHERE listing_id = ?", (listing_id,))


def nl_apartment_matches() -> list[dict]:
    """Persisted apartment matches, most recently found first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT data_json FROM nl_apartment_matches ORDER BY found_at DESC"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]


def nl_apartment_match_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM nl_apartment_matches").fetchone()[0]


def nl_apartments_scanned_ids(listing_ids: list[str]) -> set[str]:
    """Which of these candidate IDs have already been detail-fetched and
    evaluated before (match or not) - so a scan pass never re-fetches the
    same listing twice."""
    if not listing_ids:
        return set()
    with _connect() as conn:
        placeholders = ",".join("?" for _ in listing_ids)
        rows = conn.execute(
            f"SELECT listing_id FROM nl_apartments_scanned WHERE listing_id IN ({placeholders})",
            listing_ids,
        )
        return {row[0] for row in rows}


def mark_nl_apartments_scanned(listing_ids: list[str]) -> None:
    if not listing_ids:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO nl_apartments_scanned (listing_id, scanned_at) VALUES (?, datetime('now'))",
            [(listing_id,) for listing_id in listing_ids],
        )


def get_nl_apartments_scan_cursor() -> int:
    """Which search-results page to resume nationwide scanning from, so
    incremental batches make forward progress instead of re-walking pages
    already scanned on every pass."""
    with _connect() as conn:
        row = conn.execute("SELECT next_page FROM nl_apartments_scan_cursor WHERE id = 1").fetchone()
        return row[0] if row else 0


def set_nl_apartments_scan_cursor(next_page: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO nl_apartments_scan_cursor (id, next_page, updated_at)
            VALUES (1, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET next_page = excluded.next_page, updated_at = datetime('now')
            """,
            (next_page,),
        )


# Manual "how much work does this place need" tag - user-applied while
# browsing, not auto-detected (Funda's data isn't reliable enough for that -
# see project notes). Generic (listing_id-keyed, no source table), usable on
# any listing type.
CONDITION_TAGS = ("needs_work", "move_in_ready")


def set_condition_tag(listing_id: str, tag: str) -> None:
    if tag not in CONDITION_TAGS:
        raise ValueError(f"Unknown condition tag: {tag!r}")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO condition_tags (listing_id, tag, tagged_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(listing_id) DO UPDATE SET tag = excluded.tag, tagged_at = datetime('now')
            """,
            (listing_id, tag),
        )


def remove_condition_tag(listing_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM condition_tags WHERE listing_id = ?", (listing_id,))


def condition_tags(listing_ids: list[str]) -> dict[str, str]:
    """{listing_id: tag} for whichever of these listing_ids have been tagged."""
    if not listing_ids:
        return {}
    with _connect() as conn:
        placeholders = ",".join("?" for _ in listing_ids)
        rows = conn.execute(
            f"SELECT listing_id, tag FROM condition_tags WHERE listing_id IN ({placeholders})",
            listing_ids,
        )
        return dict(rows)


def save_apartment_filter_prefs(username: str, tag_filter: str, viewed_filter: str) -> None:
    """Remembers the last tag/viewed filter selection on /apartments per
    logged-in user, so it's restored on their next visit (any device)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO apartment_filter_prefs (username, tag_filter, viewed_filter, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(username) DO UPDATE SET
                tag_filter = excluded.tag_filter,
                viewed_filter = excluded.viewed_filter,
                updated_at = datetime('now')
            """,
            (username, tag_filter, viewed_filter),
        )


def get_apartment_filter_prefs(username: str) -> tuple[str, str]:
    """(tag_filter, viewed_filter), defaulting to ("all", "all") if unset."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT tag_filter, viewed_filter FROM apartment_filter_prefs WHERE username = ?",
            (username,),
        ).fetchone()
    return (row[0], row[1]) if row else ("all", "all")


def toggle_school_favorite(school_id: str, person: str) -> bool:
    """Favorites the school for this person if not already, otherwise
    unfavorites it. Returns the new state (True = now favorited)."""
    with _connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM school_favorites WHERE school_id = ? AND person = ?", (school_id, person)
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM school_favorites WHERE school_id = ? AND person = ?", (school_id, person)
            )
            return False
        conn.execute(
            "INSERT INTO school_favorites (school_id, person, favorited_at) VALUES (?, ?, datetime('now'))",
            (school_id, person),
        )
        return True


def school_favorited_by(school_ids: list[str]) -> dict[str, set[str]]:
    """{school_id: {person, ...}} for whichever of these schools have at
    least one favorite. Shared/visible to everyone, same as listing
    favorites."""
    if not school_ids:
        return {}
    with _connect() as conn:
        placeholders = ",".join("?" for _ in school_ids)
        rows = conn.execute(
            f"SELECT school_id, person FROM school_favorites WHERE school_id IN ({placeholders})",
            school_ids,
        )
        result: dict[str, set[str]] = {}
        for school_id, person in rows:
            result.setdefault(school_id, set()).add(person)
        return result
