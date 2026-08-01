"""Local-only web form for editing house_hunter/config.json."""

import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from house_hunter.config import load_config, save_config  # noqa: E402

app = Flask(__name__)

FREQUENCY_OPTIONS = ["hourly", "twice_daily", "daily", "weekly"]
CATEGORY_OPTIONS = ["buy", "rent"]

_last_saved: str | None = None


def _to_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


@app.route("/", methods=["GET"])
def form():
    config = load_config()
    return render_template(
        "form.html",
        config=config,
        frequency_options=FREQUENCY_OPTIONS,
        category_options=CATEGORY_OPTIONS,
        last_saved=_last_saved,
        errors=[],
    )


@app.route("/save", methods=["POST"])
def save():
    global _last_saved

    errors: list[str] = []
    form_data = request.form

    def read_int(name: str) -> int | None:
        try:
            return _to_int(form_data.get(name, ""))
        except ValueError:
            errors.append(f"'{name}' must be a whole number")
            return None

    to_addresses = [
        line.strip()
        for line in form_data.get("to_addresses", "").splitlines()
        if line.strip()
    ]
    for address in to_addresses:
        if "@" not in address:
            errors.append(f"'{address}' does not look like a valid email address")

    poi_types = []
    keys = request.form.getlist("poi_key")
    place_types = request.form.getlist("poi_google_place_type")
    max_results_list = request.form.getlist("poi_max_results")
    max_radius_list = request.form.getlist("poi_max_radius_m")
    for key, place_type, max_results, max_radius in zip(
        keys, place_types, max_results_list, max_radius_list
    ):
        if not key.strip():
            continue
        try:
            poi_types.append(
                {
                    "key": key.strip(),
                    "google_place_type": place_type.strip() or key.strip(),
                    "max_results": int(max_results) if max_results.strip() else 3,
                    "max_radius_m": int(max_radius) if max_radius.strip() else 2000,
                }
            )
        except ValueError:
            errors.append(f"POI '{key}' has a non-numeric max results / radius value")

    config = {
        "search": {
            "location": form_data.get("location", "").strip(),
            "category": form_data.get("category", "buy"),
            "min_price": read_int("min_price"),
            "max_price": read_int("max_price"),
            "min_bedrooms": read_int("min_bedrooms"),
            "max_bedrooms": read_int("max_bedrooms"),
            "min_area": read_int("min_area"),
            "max_area": read_int("max_area"),
            "radius_km": read_int("radius_km"),
        },
        "poi": {"types": poi_types},
        "email": {"to_addresses": to_addresses},
        "schedule": {"frequency": form_data.get("frequency", "daily")},
    }

    if not config["search"]["location"]:
        errors.append("Location is required")

    if errors:
        return render_template(
            "form.html",
            config=config,
            frequency_options=FREQUENCY_OPTIONS,
            category_options=CATEGORY_OPTIONS,
            last_saved=_last_saved,
            errors=errors,
        )

    save_config(config)
    _last_saved = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return redirect(url_for("form"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
