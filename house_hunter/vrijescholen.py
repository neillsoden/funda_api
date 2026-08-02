"""Nearest vrijeschool (Waldorf/Steiner school) lookup via vrijescholen.nl's
public directory API. The full directory is cached in state.sqlite (one row
per school, city/address included) and refreshed automatically once a month,
so normal runs don't hit the API at all.
"""

import json
import math
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from house_hunter.config import state_path

_API_URL = "https://cms.vrijescholen.nl/api/collections/schools/entries"
_REFRESH_INTERVAL = timedelta(days=30)


def _migrate_add_website_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(vrijescholen)")}
    if "website" not in columns:
        conn.execute("ALTER TABLE vrijescholen ADD COLUMN website TEXT")
    if "permalink" not in columns:
        conn.execute("ALTER TABLE vrijescholen ADD COLUMN permalink TEXT")


@contextmanager
def _connect():
    conn = sqlite3.connect(state_path())
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vrijescholen (
                id TEXT PRIMARY KEY,
                title TEXT,
                city TEXT,
                street TEXT,
                housenumber TEXT,
                postcode TEXT,
                education_type TEXT,
                website TEXT,
                permalink TEXT,
                lat REAL NOT NULL,
                lng REAL NOT NULL
            )
            """
        )
        _migrate_add_website_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vrijescholen_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fetch_all_from_api() -> list[dict]:
    entries: list[dict] = []
    page = 1
    while True:
        url = f"{_API_URL}?page={page}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, json.JSONDecodeError):
            break
        entries.extend(data.get("data", []))
        meta = data.get("meta", {})
        if page >= meta.get("last_page", page):
            break
        page += 1

    parsed = []
    for entry in entries:
        try:
            lat = float(entry["latitude"])
            lng = float(entry["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        parsed.append(
            {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "city": entry.get("city"),
                "street": entry.get("street"),
                "housenumber": entry.get("housenumber"),
                "postcode": entry.get("postcode"),
                "education_type": (entry.get("education_type") or {}).get("title"),
                "website": entry.get("website"),
                "permalink": entry.get("permalink"),
                "lat": lat,
                "lng": lng,
            }
        )
    return parsed


def _last_refreshed_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT value FROM vrijescholen_meta WHERE key = 'last_refreshed_at'"
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def refresh_schools(force: bool = False) -> int:
    """Pull the full vrijescholen.nl directory into state.sqlite. Skips the
    API call if already refreshed within the last 30 days, unless force=True.
    Returns the number of schools currently stored.
    """
    with _connect() as conn:
        last_refreshed = _last_refreshed_at(conn)
        stale = last_refreshed is None or datetime.now(timezone.utc) - last_refreshed > _REFRESH_INTERVAL
        if not force and not stale:
            return conn.execute("SELECT COUNT(*) FROM vrijescholen").fetchone()[0]

        schools = _fetch_all_from_api()
        if not schools:
            return conn.execute("SELECT COUNT(*) FROM vrijescholen").fetchone()[0]

        conn.execute("DELETE FROM vrijescholen")
        conn.executemany(
            """
            INSERT INTO vrijescholen
                (id, title, city, street, housenumber, postcode, education_type, website, permalink, lat, lng)
            VALUES (:id, :title, :city, :street, :housenumber, :postcode, :education_type, :website, :permalink, :lat, :lng)
            """,
            schools,
        )
        conn.execute(
            """
            INSERT INTO vrijescholen_meta (key, value) VALUES ('last_refreshed_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        return len(schools)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def nearest_vrijeschool(lat: float, lng: float) -> dict | None:
    refresh_schools()  # no-op unless the cached directory is >30 days old
    with _connect() as conn:
        rows = conn.execute("SELECT id, title, city, lat, lng FROM vrijescholen").fetchall()
    if not rows:
        return None
    school_id, title, city, school_lat, school_lng = min(
        rows, key=lambda row: _haversine_km(lat, lng, row[3], row[4])
    )
    return {"id": school_id, "title": title, "city": city, "lat": school_lat, "lng": school_lng}


def list_all_schools() -> list[dict]:
    """Every cached vrijeschool, sorted by city then name, for a browsable
    directory page - not tied to any particular listing.
    """
    refresh_schools()  # no-op unless the cached directory is >30 days old
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT title, city, street, housenumber, postcode, education_type, website, permalink
            FROM vrijescholen
            ORDER BY city, title
            """
        ).fetchall()
    return [
        {
            "title": title,
            "city": city,
            "street": street,
            "housenumber": housenumber,
            "postcode": postcode,
            "education_type": education_type,
            "website": website,
            "permalink": permalink,
        }
        for title, city, street, housenumber, postcode, education_type, website, permalink in rows
    ]
