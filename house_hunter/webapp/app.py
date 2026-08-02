"""Web form for editing house_hunter/config.json, favorites/rejected views,
and the public /click, /favorite, /reject endpoints used from emails.

Reachable from the public internet (home.amglab.dev via nginx on the deploy
box), so every route except /login requires a real session login.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, session, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from funda import Funda, FundaError  # noqa: E402

from house_hunter.config import load_config, save_config  # noqa: E402
from house_hunter.poi import geocode_address  # noqa: E402
from house_hunter.state import (  # noqa: E402
    add_favorite,
    add_rejected,
    all_favorited_listing_ids,
    favorited_by,
    record_click,
    rejected_listing_ids,
    remove_favorite,
    remove_rejected,
)
from house_hunter.vrijescholen import list_all_schools  # noqa: E402
from house_hunter.webapp.auth import (  # noqa: E402
    is_rate_limited,
    login_required,
    record_failed_attempt,
    safe_next_url,
    verify_credentials,
)

_ALLOWED_REDIRECT_HOSTS = {"www.funda.nl", "funda.nl"}

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # nginx terminates HTTPS in front of this in production; set
    # FORCE_HTTPS_COOKIES=1 in that .env so the session cookie requires TLS.
    SESSION_COOKIE_SECURE=os.environ.get("FORCE_HTTPS_COOKIES") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,  # 30 days
)

FREQUENCY_OPTIONS = ["hourly", "twice_daily", "daily", "weekly"]
CATEGORY_OPTIONS = ["buy", "rent"]
OBJECT_TYPE_OPTIONS = ["house", "apartment"]

_last_saved: str | None = None


def _to_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "")

    if request.method == "GET":
        return render_template("login.html", error=None, next=next_url)

    if is_rate_limited():
        return render_template(
            "login.html", error="Too many attempts. Try again in a few minutes.", next=next_url
        )

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if verify_credentials(username, password):
        session.permanent = True
        session["user"] = username.strip().lower()
        return redirect(safe_next_url(next_url))

    record_failed_attempt()
    return render_template("login.html", error="Incorrect username or password.", next=next_url)


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
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
@login_required
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

    people = [
        line.strip()
        for line in form_data.get("people", "").splitlines()
        if line.strip()
    ]

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
        "people": people,
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
@login_required
def click(listing_id: str):
    """Logs a click on a listing link, then redirects to the real Funda page.
    Only redirects to funda.nl hosts, to avoid this becoming an open redirect.
    Also marks the listing as favorited if ?favorite=<person> is present.
    """
    target = request.args.get("to", "")
    host = urlparse(target).hostname
    if not target or host not in _ALLOWED_REDIRECT_HOSTS:
        abort(400)

    record_click(listing_id)
    person = request.args.get("favorite", "").strip()
    if person:
        add_favorite(listing_id, person)
    return redirect(target, code=302)


_CONFIRM_PAGE = """
<!doctype html>
<html><head><meta charset="utf-8"><title>House Hunter</title>
<style>body{{font-family:-apple-system,Arial,sans-serif;max-width:480px;margin:80px auto;text-align:center;color:#202124;}}
a{{color:#1967d2;}}</style></head>
<body><h2>{message}</h2><p><a href="{back_url}">&larr; Back to listing</a> &middot; <a href="/favorites">View all favorites</a></p></body></html>
"""


@app.route("/favorite/<listing_id>", methods=["GET"])
@login_required
def favorite(listing_id: str):
    """Marks a listing favorited by ?person=<name>, without redirecting away
    (idempotent GET, safe against email security scanners pre-fetching the
    link)."""
    person = request.args.get("person", "").strip()
    if not person:
        abort(400)
    add_favorite(listing_id, person)
    back_url = request.args.get("to", "/favorites")
    return _CONFIRM_PAGE.format(message=f"Added to {person}'s favorites", back_url=back_url)


@app.route("/unfavorite/<listing_id>", methods=["GET"])
@login_required
def unfavorite(listing_id: str):
    person = request.args.get("person", "").strip()
    if person:
        remove_favorite(listing_id, person)
    return redirect(url_for("favorites"))


@app.route("/favorites", methods=["GET"])
@login_required
def favorites():
    ids = all_favorited_listing_ids()
    by_person = favorited_by(ids)
    listings = []
    if ids:
        with Funda() as client:
            for listing_id in ids:
                try:
                    listing = client.listing(listing_id)
                except FundaError:
                    continue
                listings.append({"listing": listing, "favorited_by": sorted(by_person.get(listing_id, []))})
    return render_template("favorites.html", listings=listings)


@app.route("/reject/<listing_id>", methods=["GET"])
@login_required
def reject(listing_id: str):
    """Marks a listing 'not interested' - it will never be emailed again,
    regardless of price changes. Idempotent GET."""
    add_rejected(listing_id)
    return _CONFIRM_PAGE.format(message="Got it, won't show this one again", back_url="/rejected")


@app.route("/unreject/<listing_id>", methods=["GET"])
@login_required
def unreject(listing_id: str):
    remove_rejected(listing_id)
    return redirect(url_for("rejected"))


@app.route("/rejected", methods=["GET"])
@login_required
def rejected():
    ids = sorted(rejected_listing_ids())
    listings = []
    if ids:
        with Funda() as client:
            for listing_id in ids:
                try:
                    listings.append(client.listing(listing_id))
                except FundaError:
                    continue
    return render_template("rejected.html", listings=listings)


@app.route("/schools", methods=["GET"])
@login_required
def schools():
    all_schools = list_all_schools()
    grouped: dict[str, list[dict]] = {}
    for school in all_schools:
        grouped.setdefault(school["city"] or "Unknown", []).append(school)
    return render_template("schools.html", grouped=dict(sorted(grouped.items())))


if __name__ == "__main__":
    # 0.0.0.0 so it's reachable from outside a Docker container. debug=False is
    # required here: Werkzeug's debugger has no auth and is a known RCE risk if
    # exposed beyond localhost.
    app.run(host="0.0.0.0", port=5000, debug=False)
