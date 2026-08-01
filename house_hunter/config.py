"""Load/save the house_hunter config.json."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "search": {
        "location": "amsterdam",
        "category": "buy",
        "min_price": None,
        "max_price": 500000,
        "min_bedrooms": None,
        "max_bedrooms": None,
        "min_area": None,
        "max_area": None,
        "radius_km": None,
    },
    "poi": {
        "types": [
            {
                "key": "school",
                "google_place_type": "school",
                "max_results": 3,
                "max_radius_m": 2000,
            },
            {
                "key": "train_station",
                "google_place_type": "train_station",
                "max_results": 2,
                "max_radius_m": 3000,
            },
        ]
    },
    "email": {
        "to_addresses": [],
    },
    "schedule": {
        "frequency": "daily",
    },
}


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return deepcopy(DEFAULT_CONFIG)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    merged = deepcopy(DEFAULT_CONFIG)
    _deep_update(merged, data)
    return merged


def save_config(config: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def _deep_update(base: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
