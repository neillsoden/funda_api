"""Distance from a listing to configured fixed points of interest (e.g. a school)."""

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class Distance:
    km: float
    mode: str  # "driving"/"bicycling" (real travel distance) or "straight_line" (haversine fallback)
    duration_text: str | None = None
    duration_minutes: float | None = None  # numeric, for hard cutoff filters (None on straight-line fallback)
    travel_mode: str = "driving"  # intended mode (survives straight-line fallback), for Maps links
    dest_lat: float | None = None
    dest_lng: float | None = None


def geocode_address(address: str) -> tuple[float, float] | None:
    """Resolve a street address to (lat, lng) using the Geocoding API."""
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return None

    params = urllib.parse.urlencode({"address": address, "key": api_key})
    url = f"https://maps.googleapis.com/maps/api/geocode/json?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
    except urllib.error.URLError:
        return None

    if data.get("status") != "OK" or not data.get("results"):
        return None
    location = data["results"][0]["geometry"]["location"]
    return location["lat"], location["lng"]


def nearest_place(
    lat: float, lng: float, google_place_type: str
) -> tuple[str, float, float] | None:
    """Find the nearest place of a given Google Places type (e.g. 'train_station')
    to a coordinate. Returns (name, lat, lng) of the closest match, or None.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return None

    params = urllib.parse.urlencode(
        {
            "location": f"{lat},{lng}",
            "rankby": "distance",
            "type": google_place_type,
            "key": api_key,
        }
    )
    url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
    except urllib.error.URLError:
        return None

    if data.get("status") != "OK" or not data.get("results"):
        return None
    place = data["results"][0]
    location = place["geometry"]["location"]
    return place.get("name", google_place_type), location["lat"], location["lng"]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


_TRAVEL_MODES = {"driving": "DRIVE", "bicycling": "BICYCLE", "transit": "TRANSIT"}


def _format_duration(seconds_str: str) -> str:
    seconds = int(seconds_str.rstrip("s"))
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    return f"{hours} hr {mins} min" if mins else f"{hours} hr"


def distance_to(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    mode: str = "driving",
) -> Distance:
    """Real travel distance/time via the Routes API (mode: "driving",
    "bicycling", or "transit"), falling back to straight-line distance if
    the API isn't enabled or the call fails. "transit" is biased toward
    train (Google has no "intercity vs sprinter" distinction - just a
    generic train mode) and needs a near-future departure time, since
    transit routing is schedule-based, not a fixed distance.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if api_key:
        url = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
        body = {
            "origins": [
                {"waypoint": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}}}
            ],
            "destinations": [
                {"waypoint": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}}}
            ],
            "travelMode": _TRAVEL_MODES.get(mode, "DRIVE"),
        }
        if mode == "transit":
            body["transitPreferences"] = {"allowedTravelModes": ["TRAIN"], "routingPreference": "LESS_WALKING"}
            departure = datetime.now(timezone.utc) + timedelta(minutes=5)
            body["departureTime"] = departure.strftime("%Y-%m-%dT%H:%M:%SZ")
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,condition",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            element = data[0]
            if element.get("condition") == "ROUTE_EXISTS":
                return Distance(
                    km=element["distanceMeters"] / 1000,
                    mode=mode,
                    duration_text=_format_duration(element["duration"]),
                    duration_minutes=int(element["duration"].rstrip("s")) / 60,
                    travel_mode=mode,
                    dest_lat=dest_lat,
                    dest_lng=dest_lng,
                )
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, ValueError):
            pass

    return Distance(
        km=_haversine_km(origin_lat, origin_lng, dest_lat, dest_lng),
        mode="straight_line",
        travel_mode=mode,
        dest_lat=dest_lat,
        dest_lng=dest_lng,
    )


def transit_ride_minutes(
    origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float
) -> float | None:
    """Time actually spent ON a train, not the door-to-door total. The
    distanceMatrix endpoint distance_to() uses only returns one combined
    duration (walk to station + wait + ride + walk from destination
    station), with no way to isolate the ride itself - so this uses the
    fuller computeRoutes endpoint instead, which breaks the trip into steps
    per travel mode, and sums only the TRANSIT ones. Returns None if the API
    isn't enabled, the call fails, or no transit route exists.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return None

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    departure = datetime.now(timezone.utc) + timedelta(minutes=5)
    body = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
        "travelMode": "TRANSIT",
        "transitPreferences": {"allowedTravelModes": ["TRAIN"], "routingPreference": "LESS_WALKING"},
        "departureTime": departure.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.legs.steps.travelMode,routes.legs.steps.staticDuration",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        route = data["routes"][0]
        transit_seconds = 0
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                if step.get("travelMode") != "TRANSIT":
                    continue
                raw = step.get("staticDuration")
                if raw:
                    transit_seconds += int(raw.rstrip("s"))
        return transit_seconds / 60 if transit_seconds else None
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, ValueError):
        return None
