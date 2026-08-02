"""Local-only web form for editing house_hunter/config.json, plus the public
/click redirect endpoint used for email click tracking.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from funda import Funda, FundaError  # noqa: E402

from house_hunter.config import load_config, save_config  # noqa: E402
from house_hunter.poi import geocode_address  # noqa: E402
from house_hunter.state import (  # noqa: E402
    add_favorite,
    favorited_listing_ids,
    record_click,
    remove_favorite,
)

_ALLOWED_REDIRECT_HOSTS = {"www.funda.nl", "funda.nl"}

app = Flask(__name__)

FREQUENCY_OPTIONS = ["hourly", "twice_daily", "daily", "weekly"]
CATEGORY_OPTIONS = ["buy", "rent"]
OBJECT_TYPE_OPTIONS = ["house", "apartment"]

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
        object_type_options=OBJECT_TYPE_OPTIONS,
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

    locations = [
        line.strip()
        for line in form_data.get("locations", "").splitlines()
        if line.strip()
    ]

    to_addresses = [
        line.strip()
        for line in form_data.get("to_addresses", "").splitlines()
        if line.strip()
    ]
    for address in to_addresses:
        if "@" not in address:
            errors.append(f"'{address}' does not look like a valid email address")

    places = []
    names = request.form.getlist("place_name")
    addresses = request.form.getlist("place_address")
    cities = request.form.getlist("place_city")
    lats = request.form.getlist("place_lat")
    lngs = request.form.getlist("place_lng")
    for name, address, place_city, lat, lng in zip(names, addresses, cities, lats, lngs):
        if not name.strip() or not address.strip():
            continue
        coords = None
        if lat.strip() and lng.strip():
            try:
                coords = (float(lat), float(lng))
            except ValueError:
                coords = None
        if coords is None:
            coords = geocode_address(address.strip())
        if coords is None:
            errors.append(f"Could not geocode address for '{name}': {address}")
            continue
        place = {
            "name": name.strip(),
            "address": address.strip(),
            "lat": coords[0],
            "lng": coords[1],
        }
        if place_city.strip():
            place["city"] = place_city.strip()
        places.append(place)

    max_school_distance = form_data.get("max_school_distance_km", "").strip()
    try:
        max_school_distance_km = float(max_school_distance) if max_school_distance else None
    except ValueError:
        errors.append("'max_school_distance_km' must be a number")
        max_school_distance_km = None

    # mortgage_budget has no form field yet (edit config.json directly for now) -
    # carry the existing value through so saving the form doesn't wipe it out.
    existing_mortgage_budget = load_config()["search"].get("mortgage_budget") or {}

    config = {
        "search": {
            "locations": locations,
            "category": form_data.get("category", "buy"),
            "object_type": form_data.get("object_type", "house"),
            "min_price": read_int("min_price"),
            "max_price": read_int("max_price"),
            "mortgage_budget": existing_mortgage_budget,
            "min_bedrooms": read_int("min_bedrooms"),
            "max_bedrooms": read_int("max_bedrooms"),
            "min_area": read_int("min_area"),
            "max_area": read_int("max_area"),
            "radius_km": read_int("radius_km"),
            "max_school_distance_km": max_school_distance_km,
        },
        "poi": {"places": places},
        "email": {
            "from_address": form_data.get("from_address", "").strip(),
            "to_addresses": to_addresses,
        },
        "schedule": {"frequency": form_data.get("frequency", "daily")},
        "server": {"public_base_url": form_data.get("public_base_url", "").strip().rstrip("/")},
    }

    if not locations:
        errors.append("At least one location is required")

    if errors:
        return render_template(
            "form.html",
            config=config,
            frequency_options=FREQUENCY_OPTIONS,
            category_options=CATEGORY_OPTIONS,
            object_type_options=OBJECT_TYPE_OPTIONS,
            last_saved=_last_saved,
            errors=errors,
        )

    save_config(config)
    _last_saved = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return redirect(url_for("form"))


@app.route("/click/<listing_id>", methods=["GET"])
def click(listing_id: str):
    """Logs a click on a listing link, then redirects to the real Funda page.
    Only redirects to funda.nl hosts, to avoid this becoming an open redirect.
    Also marks the listing as favorited if ?favorite=1 is present.
    """
    target = request.args.get("to", "")
    host = urlparse(target).hostname
    if not target or host not in _ALLOWED_REDIRECT_HOSTS:
        abort(400)

    record_click(listing_id)
    if request.args.get("favorite") == "1":
        add_favorite(listing_id)
    return redirect(target, code=302)


_CONFIRM_PAGE = """
<!doctype html>
<html><head><meta charset="utf-8"><title>House Hunter</title>
<style>body{{font-family:-apple-system,Arial,sans-serif;max-width:480px;margin:80px auto;text-align:center;color:#202124;}}
a{{color:#1967d2;}}</style></head>
<body><h2>{message}</h2><p><a href="{back_url}">&larr; Back to listing</a> &middot; <a href="/favorites">View all favorites</a></p></body></html>
"""


@app.route("/favorite/<listing_id>", methods=["GET"])
def favorite(listing_id: str):
    """Marks a listing favorited without redirecting away (idempotent GET, safe
    against email security scanners pre-fetching the link)."""
    add_favorite(listing_id)
    back_url = request.args.get("to", "/favorites")
    return _CONFIRM_PAGE.format(message="Added to favorites", back_url=back_url)


@app.route("/unfavorite/<listing_id>", methods=["GET"])
def unfavorite(listing_id: str):
    remove_favorite(listing_id)
    return redirect(url_for("favorites"))


@app.route("/favorites", methods=["GET"])
def favorites():
    ids = sorted(favorited_listing_ids())
    listings = []
    if ids:
        with Funda() as client:
            for listing_id in ids:
                try:
                    listings.append(client.listing(listing_id))
                except FundaError:
                    continue
    return render_template("favorites.html", listings=listings)


if __name__ == "__main__":
    # 0.0.0.0 so it's reachable from outside a Docker container. debug=False is
    # required here: Werkzeug's debugger has no auth and is a known RCE risk if
    # exposed beyond localhost.
    app.run(host="0.0.0.0", port=5000, debug=False)
