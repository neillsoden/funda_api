"""Web form for editing house_hunter/config.json, favorites/rejected views,
and the public /click, /favorite, /reject endpoints used from emails.

Reachable from the public internet (home.amglab.dev via nginx on the deploy
box), so every route except /login requires a real session login.
"""

import io
import os
import sys
import threading
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from funda import Funda, FundaError  # noqa: E402

from house_hunter.config import load_config, save_config  # noqa: E402
from house_hunter.email_report import (  # noqa: E402
    _contrast_text_color,
    _days_listed,
    _energy_color,
    _maps_directions_url,
    _price_budget_color,
    _school_proximity_color,
    _split_school_distance,
)
from house_hunter.poi import geocode_address  # noqa: E402
from house_hunter.run import browse_listings  # noqa: E402
from house_hunter.run import run as run_pipeline  # noqa: E402
from house_hunter.state import (  # noqa: E402
    add_favorite,
    add_rejected,
    all_favorited_listing_ids,
    favorited_by,
    record_click,
    recent_run_logs,
    rejected_listing_ids,
    remove_favorite,
    remove_rejected,
    under_bid_listing_ids,
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

_run_lock = threading.Lock()
_run_status: dict = {
    "running": False, "started_at": None, "finished_at": None, "output": None, "forced": False,
}


def _run_pipeline_in_background(force: bool = False) -> None:
    if not _run_lock.acquire(blocking=False):
        return  # a run is already in progress, ignore this trigger
    _run_status.update(running=True, started_at=datetime.now(timezone.utc), output=None, forced=force)
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            run_pipeline(force=force, reason="force" if force else "manual")
    except Exception as exc:  # noqa: BLE001 - surface any failure in the UI rather than losing it
        buffer.write(f"\nFAILED: {exc}")
    finally:
        _run_status.update(
            running=False, finished_at=datetime.now(timezone.utc), output=buffer.getvalue()
        )
        _run_lock.release()


def _to_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


def _split_list(raw: str) -> list[str]:
    """Accepts comma-separated (preferred, compact) or newline-separated input."""
    parts = raw.replace("\n", ",").split(",")
    return [part.strip() for part in parts if part.strip()]


# --- /houses swipe deck: in-memory cache of the last full enrichment pass,
# since building it (Funda search + Google distance calls per listing) is
# too slow to redo on every page load. Refreshed in the background when
# stale, or on demand via the Refresh button.
_BROWSE_STALE_SECONDS = 20 * 60
_browse_lock = threading.Lock()
_browse_cache: dict = {"items": [], "updated_at": None, "error": None}
_browse_status: dict = {"running": False}


def _refresh_browse_cache() -> None:
    if not _browse_lock.acquire(blocking=False):
        return
    _browse_status["running"] = True
    try:
        items = browse_listings()
        _browse_cache["items"] = items
        _browse_cache["updated_at"] = datetime.now(timezone.utc)
        _browse_cache["error"] = None
    except Exception as exc:  # noqa: BLE001 - keep the old cache, surface the error instead
        _browse_cache["error"] = str(exc)
    finally:
        _browse_status["running"] = False
        _browse_lock.release()


def _card_view(item) -> dict:
    """Flatten an EnrichedListing into plain display fields the houses.html
    template can render without needing Python helpers - same information
    the email cards show."""
    listing = item.listing
    price_color = _price_budget_color(listing.price.amount, item.mortgage_budget)
    energy_bg = _energy_color(listing.energy_label)

    other_distances, school_entry = _split_school_distance(item.distances)

    school = None
    if school_entry:
        _, school_dist = school_entry
        school = {
            "text": (
                f"{school_dist.km:.1f} km ({school_dist.duration_text}) to school"
                if school_dist.duration_text
                else f"{school_dist.km:.1f} km to school"
            ),
            "href": _maps_directions_url(listing.location.coordinates, school_dist),
        }

    within_range = None
    if item.school_distance_km is not None and item.max_school_distance_km is not None:
        ok = item.school_distance_km <= item.max_school_distance_km
        within_range = {
            "ok": ok,
            "text": f"{'Within' if ok else 'Outside'} {item.max_school_distance_km:g}km biking distance",
        }

    distance_chips = [
        {
            "text": (
                f"{dist.km:.1f} km ({dist.duration_text}) · {name}"
                if dist.duration_text
                else f"{dist.km:.1f} km · {name}"
            ),
            "href": _maps_directions_url(listing.location.coordinates, dist),
        }
        for name, dist in other_distances.items()
    ]

    listed_date, days_listed = _days_listed(listing)
    listed_text = f"Listed {listed_date} ({days_listed}d ago)" if listed_date else "Listed date unknown"
    sold_text = (
        f"Last sold €{item.previous_sale.price:,} ({item.previous_sale.date})"
        if item.previous_sale
        else "No previous sale on record"
    )

    budget_text = None
    budget_total_text = None
    if item.mortgage_budget is not None and listing.price.amount is not None:
        margin = item.mortgage_budget - listing.price.amount
        budget_text = f"€{margin:,} under budget (max €{item.mortgage_budget:,})"
        budget_total_text = f"€{item.mortgage_budget:,}"

    market_text = None
    if item.market and item.market.get("avg_asking_price_per_m2") and listing.living_area:
        avg_per_m2 = item.market["avg_asking_price_per_m2"]
        listing_per_m2 = round(listing.price.amount / listing.living_area) if listing.price.amount else None
        if listing_per_m2 is not None:
            diff_pct = round((listing_per_m2 - avg_per_m2) / avg_per_m2 * 100)
            neighbourhood = item.market.get("neighbourhood", "area")
            below = diff_pct <= 0
            market_text = {
                "ok": below,
                "text": (
                    f"€{listing_per_m2:,}/m² · {abs(diff_pct)}% "
                    f"{'below' if below else 'above'} {neighbourhood} avg (€{avg_per_m2:,}/m²)"
                ),
            }

    comparables = [
        {
            "title": comp.title,
            "url": comp.url,
            "price": comp.price,
            "price_per_m2": comp.price_per_m2,
        }
        for comp in item.comparables
    ]

    area_parts = []
    if listing.living_area:
        area_parts.append(f"{listing.living_area} m² living")
    if listing.plot_area:
        area_parts.append(f"{listing.plot_area} m² plot")

    return {
        "id": listing.id,
        "url": listing.url,
        "photo": listing.media.photo_urls[0] if listing.media.photo_urls else "",
        "title": listing.title,
        "city": listing.city,
        "neighbourhood": listing.address.neighbourhood,
        "price": f"€{listing.price.amount:,}" if listing.price.amount else "price unknown",
        "price_color": price_color,
        "budget_text": budget_text,
        "budget_total_text": budget_total_text,
        "area_text": " · ".join(area_parts),
        "bedrooms": listing.bedrooms,
        "energy_label": listing.energy_label or "?",
        "energy_bg": energy_bg,
        "energy_text": _contrast_text_color(energy_bg),
        "school": school,
        "within_range": within_range,
        "distance_chips": distance_chips,
        "listed_text": listed_text,
        "sold_text": sold_text,
        "market": market_text,
        "comparables": comparables,
        "accent_color": _school_proximity_color(item.school_distance_km, item.max_school_distance_km),
        "is_new": item.is_new,
        "price_drop_from": item.price_drop_from,
        "already_viewed": item.already_viewed,
        "favorited_by": sorted(item.favorited_by),
    }


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
        run_status=_run_status,
        recent_logs=recent_run_logs()[:5],
        errors=[],
    )


@app.route("/run-now", methods=["POST"])
@login_required
def run_now():
    if not _run_status["running"]:
        threading.Thread(target=_run_pipeline_in_background, daemon=True).start()
    return redirect(url_for("form"))


@app.route("/force-send", methods=["POST"])
@login_required
def force_send():
    """Sends the full current digest even if nothing's new - still excludes
    anything marked "not interested"."""
    if not _run_status["running"]:
        threading.Thread(target=_run_pipeline_in_background, kwargs={"force": True}, daemon=True).start()
    return redirect(url_for("form"))


@app.route("/run-status", methods=["GET"])
@login_required
def run_status():
    return _run_status


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

    locations = _split_list(form_data.get("locations", ""))

    to_addresses = _split_list(form_data.get("to_addresses", ""))
    for address in to_addresses:
        if "@" not in address:
            errors.append(f"'{address}' does not look like a valid email address")

    people = _split_list(form_data.get("people", ""))

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

    times = _split_list(form_data.get("times", ""))
    for time_str in times:
        parts = time_str.split(":")
        valid = len(parts) == 2 and all(part.isdigit() for part in parts)
        if valid:
            hour, minute = int(parts[0]), int(parts[1])
            valid = 0 <= hour <= 23 and 0 <= minute <= 59
        if not valid:
            errors.append(f"'{time_str}' is not a valid 24h time (expected HH:MM)")

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
        "schedule": {
            "frequency": form_data.get("frequency", "daily"),
            "times": times,
            "timezone": form_data.get("timezone", "Europe/Amsterdam").strip() or "Europe/Amsterdam",
        },
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
            run_status=_run_status,
            recent_logs=recent_run_logs()[:5],
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


@app.route("/onder-bod", methods=["GET"])
@login_required
def onder_bod():
    """Listings currently "under bid" - logged here instead of emailed, since
    making an offer on one is generally pointless. If one reopens (offer
    falls through), it flows back into the normal email pipeline on its own."""
    ids = under_bid_listing_ids()
    listings = []
    if ids:
        with Funda() as client:
            for listing_id in ids:
                try:
                    listings.append(client.listing(listing_id))
                except FundaError:
                    continue
    return render_template("onder_bod.html", listings=listings)


@app.route("/logs", methods=["GET"])
@login_required
def logs():
    """Run history for the last 7 days - when the pipeline fired, why
    (startup/scheduled/manual/force), and what happened."""
    return render_template("logs.html", logs=recent_run_logs())


@app.route("/houses", methods=["GET"])
@login_required
def houses():
    """Tinder-style swipe deck of every currently matching listing, tabbed
    by city. Swipe right favorites (as the logged-in person), left marks
    "not interested" - same actions as the email buttons, just faster."""
    stale = (
        _browse_cache["updated_at"] is None
        or (datetime.now(timezone.utc) - _browse_cache["updated_at"]).total_seconds() > _BROWSE_STALE_SECONDS
    )
    if stale and not _browse_status["running"]:
        threading.Thread(target=_refresh_browse_cache, daemon=True).start()

    grouped: dict[str, list[dict]] = {}
    for item in _browse_cache["items"]:
        city = item.listing.city or "Unknown"
        grouped.setdefault(city, []).append(_card_view(item))

    return render_template(
        "houses.html",
        cards=dict(sorted(grouped.items())),
        running=_browse_status["running"] or _browse_cache["updated_at"] is None,
        updated_at=_browse_cache["updated_at"],
        error=_browse_cache["error"],
    )


@app.route("/houses/refresh", methods=["POST"])
@login_required
def houses_refresh():
    if not _browse_status["running"]:
        threading.Thread(target=_refresh_browse_cache, daemon=True).start()
    return redirect(url_for("houses"))


@app.route("/houses/action/<listing_id>", methods=["POST"])
@login_required
def houses_action(listing_id: str):
    direction = (request.get_json(silent=True) or {}).get("direction")
    if direction not in ("left", "right"):
        return jsonify({"ok": False, "error": "invalid direction"}), 400

    if direction == "right":
        add_favorite(listing_id, session["user"].capitalize())
    else:
        add_rejected(listing_id)

    _browse_cache["items"] = [item for item in _browse_cache["items"] if item.listing.id != listing_id]
    return jsonify({"ok": True})


if __name__ == "__main__":
    # 0.0.0.0 so it's reachable from outside a Docker container. debug=False is
    # required here: Werkzeug's debugger has no auth and is a known RCE risk if
    # exposed beyond localhost.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
