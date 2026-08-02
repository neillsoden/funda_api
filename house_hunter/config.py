"""Load/save the house_hunter config.json.

Supports multiple independent instances (one per city/region), each with its
own config + state file, selected via HOUSE_HUNTER_INSTANCE. e.g.
HOUSE_HUNTER_INSTANCE=den_bosch uses config.den_bosch.json / state.den_bosch.sqlite
instead of the default config.json / state.sqlite.
"""

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    # In Docker this points at a bind-mounted directory (set via
    # HOUSE_HUNTER_DATA_DIR). Deliberately a directory, not individual file
    # mounts: SQLite's atomic rename-based commits break single-file bind
    # mounts (the container ends up writing to a new inode the host mount
    # no longer tracks, so writes silently don't appear on the host).
    override = os.environ.get("HOUSE_HUNTER_DATA_DIR", "").strip()
    return Path(override) if override else _ROOT


def _instance_suffix() -> str:
    instance = os.environ.get("HOUSE_HUNTER_INSTANCE", "").strip()
    return f".{instance}" if instance else ""


def config_path() -> Path:
    return _data_dir() / f"config{_instance_suffix()}.json"


def state_path() -> Path:
    return _data_dir() / f"state{_instance_suffix()}.sqlite"

DEFAULT_CONFIG: dict[str, Any] = {
    "search": {
        "locations": ["amsterdam"],
        "category": "buy",
        "object_type": "house",
        "min_price": None,
        "max_price": 500000,
        # Max mortgage capacity by the property's own energy label (better label
        # -> lower interest -> higher capacity), from the mortgage advisor.
        # Overrides max_price when set.
        "mortgage_budget": {},
        "min_bedrooms": 3,
        "max_bedrooms": None,
        "min_area": None,
        "max_area": None,
        "radius_km": None,
        # Max biking distance (km) to the nearest vrijeschool (vrijescholen.nl).
        # Listings farther than this are excluded entirely. None = no filter.
        "max_school_distance_km": None,
    },
    "poi": {
        # Fixed named places to compute distance to (geocoded once, cached here).
        "places": [],
        # Nearest place of a given Google Places type, looked up fresh per listing
        # (e.g. the closest train station, whichever one that turns out to be).
        "nearest_types": [
            {"key": "train_station", "google_place_type": "train_station", "label": "Nearest train station"}
        ],
    },
    "email": {
        "from_address": "",
        "to_addresses": [],
    },
    # Named household members. Drives per-person favorite buttons in emails
    # (no login system exists, so "who" is just a labeled button per person -
    # good enough for a shared household inbox, not real authentication).
    "people": [],
    "schedule": {
        "frequency": "daily",
        # Fixed clock times to check each day, e.g. ["11:00", "21:00"] for
        # twice daily. Takes priority over "frequency" when set; leave empty
        # to fall back to the simple repeating-interval behavior.
        "times": [],
        "timezone": "Europe/Amsterdam",
    },
    "server": {
        # Public base URL of the deployed webapp, e.g. "https://house.example.com".
        # Required for click tracking (email links route through /click here) -
        # leave blank and emails link straight to Funda instead.
        "public_base_url": "",
    },
}


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return deepcopy(DEFAULT_CONFIG)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    merged = deepcopy(DEFAULT_CONFIG)
    _deep_update(merged, data)
    return merged


def save_config(config: dict[str, Any]) -> None:
    with config_path().open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def _deep_update(base: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
