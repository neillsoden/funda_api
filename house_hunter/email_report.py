"""Build and send the HTML listings digest email."""

import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import quote

from funda import Listing

from house_hunter.comparables import Comparable
from house_hunter.poi import Distance
from house_hunter.pricing import PreviousSale


@dataclass
class EnrichedListing:
    listing: Listing
    distances: dict[str, Distance]  # place name -> Distance
    previous_sale: PreviousSale | None
    mortgage_budget: int | None = None  # max capacity for this listing's energy label
    market: dict | None = None  # neighbourhood market_insights response
    comparables: list[Comparable] = field(default_factory=list)  # recently sold nearby
    price_drop_from: int | None = None  # previous notified price, if this is a drop alert
    school_distance_km: float | None = None  # nearest vrijeschool, straight-line/driving km
    max_school_distance_km: float | None = None  # configured biking-distance threshold
    already_viewed: bool = False  # link was clicked in a previous email
    is_new: bool = True  # first time this listing has ever been emailed
    favorited_by: set[str] = field(default_factory=set)  # people who already favorited this


def _days_listed(listing: Listing) -> tuple[str, int] | tuple[None, None]:
    if not listing.publication_date:
        return None, None
    listed_date = datetime.fromisoformat(
        listing.publication_date.replace("Z", "+00:00")
    ).date()
    return listed_date.isoformat(), (date.today() - listed_date).days


_ENERGY_COLORS = {
    "A++++": "#0d5c1f",
    "A+++": "#1b6e2b",
    "A++": "#237a34",
    "A+": "#2b7f3a",
    "A": "#2e7d32",
    "B": "#66bb6a",
    "C": "#c0d842",
    "D": "#fdd835",
    "E": "#fb8c00",
    "F": "#e64a19",
    "G": "#c62828",
}


def _energy_color(energy_label: str | None) -> str:
    """Dutch energy labels run G (worst) up to A++++ (best, new-builds).
    Positive plus-variants get progressively darker/richer green than plain A.
    """
    if not energy_label:
        return "#9aa0a6"
    return _ENERGY_COLORS.get(energy_label.strip().upper(), "#9aa0a6")


def _contrast_text_color(hex_color: str) -> str:
    """Black or white text, whichever reads better against a given background."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#1a1a1a" if luminance > 0.6 else "#ffffff"

_FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif"

# Google Material Icons, served as static SVGs (no webfont, no @font-face -
# just <img> tags, which is the one icon technique that survives email clients).
_ICON_BASE = "https://cdn.jsdelivr.net/npm/@material-icons/svg@1.0.33/svg"
_ICONS = {
    "bed": f"{_ICON_BASE}/bed/baseline.svg",
    "bolt": f"{_ICON_BASE}/bolt/baseline.svg",
    "place": f"{_ICON_BASE}/place/baseline.svg",
    "car": f"{_ICON_BASE}/directions_car/baseline.svg",
    "bike": f"{_ICON_BASE}/directions_bike/baseline.svg",
    "schedule": f"{_ICON_BASE}/schedule/baseline.svg",
    "sell": f"{_ICON_BASE}/sell/baseline.svg",
    "check": f"{_ICON_BASE}/check_circle/baseline.svg",
    "star": f"{_ICON_BASE}/star/baseline.svg",
    "star_border": f"{_ICON_BASE}/star_border/baseline.svg",
    "not_interested": f"{_ICON_BASE}/not_interested/baseline.svg",
    "trending_down": f"{_ICON_BASE}/trending_down/baseline.svg",
    "trending_up": f"{_ICON_BASE}/trending_up/baseline.svg",
    "insights": f"{_ICON_BASE}/insights/baseline.svg",
    "home": f"{_ICON_BASE}/home/baseline.svg",
}


def _icon(key: str, size: int = 14) -> str:
    return (
        f'<img src="{_ICONS[key]}" width="{size}" height="{size}" '
        f'style="vertical-align:-2px;margin-right:5px;opacity:0.7;">'
    )


def _chip(
    text: str, *, icon: str | None = None, bg: str = "#f1f3f4", color: str = "#3c4043", href: str | None = None
) -> str:
    icon_html = _icon(icon) if icon else ""
    inner = f"{icon_html}{text}"
    if href:
        inner = f'<a href="{href}" style="color:inherit;text-decoration:none;">{inner}</a>'
    return (
        f'<span style="display:inline-block;background:{bg};color:{color};'
        f"font-size:12px;font-weight:600;padding:5px 10px;border-radius:12px;"
        f'margin:0 6px 6px 0;white-space:nowrap;">{inner}</span>'
    )


_TRAVEL_ICONS = {"driving": "car", "bicycling": "bike"}
_MAPS_TRAVEL_MODES = {"driving": "driving", "bicycling": "bicycling", "transit": "transit"}


def _maps_directions_url(
    origin: tuple[float, float] | None, distance: Distance
) -> str | None:
    if origin is None or distance.dest_lat is None or distance.dest_lng is None:
        return None
    travelmode = _MAPS_TRAVEL_MODES.get(distance.travel_mode, "driving")
    return (
        f"https://www.google.com/maps/dir/?api=1&origin={origin[0]},{origin[1]}"
        f"&destination={distance.dest_lat},{distance.dest_lng}&travelmode={travelmode}"
    )


def _distance_chips(distances: dict[str, Distance], origin: tuple[float, float] | None) -> str:
    chips = []
    for place_name, distance in distances.items():
        icon = _TRAVEL_ICONS.get(distance.mode, "place")
        if distance.duration_text:
            label = f"{distance.km:.1f} km ({distance.duration_text}) · {place_name}"
        else:
            label = f"{distance.km:.1f} km · {place_name}"
        href = _maps_directions_url(origin, distance)
        chips.append(_chip(label, icon=icon, bg="#e8f0fe", color="#1967d2", href=href))
    return "".join(chips)


def _split_school_distance(
    distances: dict[str, Distance],
) -> tuple[dict[str, Distance], tuple[str, Distance] | None]:
    """Pulls the vrijeschool entry out of the generic distances dict so it can
    be shown up front with the other priority facts, instead of mixed in with
    secondary distances (station, fixed landmarks)."""
    school_entry = None
    rest: dict[str, Distance] = {}
    for name, dist in distances.items():
        if "(vrijeschool)" in name and school_entry is None:
            school_entry = (name, dist)
        else:
            rest[name] = dist
    return rest, school_entry


def _market_group(item: EnrichedListing) -> str:
    listing = item.listing
    parts = []

    if item.market and item.market.get("avg_asking_price_per_m2") and listing.living_area:
        avg_per_m2 = item.market["avg_asking_price_per_m2"]
        listing_per_m2 = round(listing.price.amount / listing.living_area) if listing.price.amount else None
        if listing_per_m2 is not None:
            diff_pct = round((listing_per_m2 - avg_per_m2) / avg_per_m2 * 100)
            neighbourhood = item.market.get("neighbourhood", "area")
            if diff_pct <= 0:
                text = f"€{listing_per_m2:,}/m² · {abs(diff_pct)}% below {neighbourhood} avg (€{avg_per_m2:,}/m²)"
                bg, color = "#e6f4ea", "#146c2e"
            else:
                text = f"€{listing_per_m2:,}/m² · {diff_pct}% above {neighbourhood} avg (€{avg_per_m2:,}/m²)"
                bg, color = "#fce8e6", "#c5221f"
            parts.append(_chip(text, icon="insights", bg=bg, color=color))

    comps_html = ""
    if item.comparables:
        rows = []
        for comp in item.comparables:
            per_m2 = f" · €{comp.price_per_m2:,}/m²" if comp.price_per_m2 else ""
            comp_link = (
                f'<a href="{comp.url}" style="color:#3c4043;text-decoration:none;">{comp.title}</a>'
                if comp.url
                else comp.title
            )
            rows.append(
                f'<div style="font-size:12px;color:#3c4043;padding:3px 0;">'
                f"{_icon('home', 12)}{comp_link} — €{comp.price:,}{per_m2}</div>"
            )
        comps_html = (
            f'<div style="margin-top:8px;padding:8px 10px;background:#f8f9fa;border-radius:8px;">'
            f'<div style="font-size:11px;font-weight:700;color:#5f6368;text-transform:uppercase;'
            f'letter-spacing:0.4px;margin-bottom:2px;">Recently sold nearby</div>'
            f'{"".join(rows)}</div>'
        )

    if not parts and not comps_html:
        return ""
    return "".join(parts) + comps_html


def _price_budget_color(price: int | None, budget: int | None) -> str:
    """Color the headline price by how it sits against the mortgage budget for
    this listing's energy label: comfortably under / cutting it close / over
    budget. Falls back to the neutral blue when there's no budget to compare
    against (e.g. mortgage_budget not configured for that energy label).
    """
    if price is None or budget is None:
        return "#1967d2"
    margin = budget - price
    if margin < 0:
        return "#d93025"  # over budget - red
    if margin <= budget * 0.05:
        return "#f9ab00"  # within 5% of budget - amber, cutting it close
    return "#137333"  # comfortably under budget - green


def _price_drop_banner(item: EnrichedListing) -> str:
    if item.price_drop_from is None or item.listing.price.amount is None:
        return ""
    old_price = item.price_drop_from
    new_price = item.listing.price.amount
    pct = round((old_price - new_price) / old_price * 100)
    return (
        f'<div style="background:#fce8e6;color:#c5221f;font-size:13px;font-weight:700;'
        f'padding:10px 20px;">{_icon("trending_down", 16)}'
        f"Price dropped: €{old_price:,} → €{new_price:,} (-{pct}%)</div>"
    )


def _school_proximity_color(distance_km: float | None, max_km: float | None) -> str | None:
    """Solid traffic-light color for a left accent border, based on distance to
    the nearest vrijeschool relative to the configured biking-distance budget.
    None means no school data for this listing - no accent at all.
    """
    if distance_km is None:
        return None
    budget = max_km if max_km is not None else 5.0
    if distance_km <= budget * 0.5:
        return "#1e8e3e"  # comfortably close - solid green
    if distance_km <= budget:
        return "#f9ab00"  # within budget but not close - solid amber
    return "#d93025"  # outside budget - solid red


def _tracked_url(listing: Listing, public_base_url: str) -> str:
    if not public_base_url or not listing.url or not listing.id:
        return listing.url or "#"
    return f"{public_base_url}/click/{listing.id}?to={quote(listing.url, safe='')}"


def _favorite_url(listing: Listing, public_base_url: str, person: str) -> str | None:
    if not public_base_url or not listing.url or not listing.id:
        return None
    return (
        f"{public_base_url}/favorite/{listing.id}?person={quote(person)}"
        f"&to={quote(listing.url, safe='')}"
    )


def _reject_url(listing: Listing, public_base_url: str) -> str | None:
    if not public_base_url or not listing.id:
        return None
    return f"{public_base_url}/reject/{listing.id}"


def _favorite_and_reject_row(
    item: EnrichedListing, public_base_url: str, people: list[str], reject_url: str | None
) -> str:
    """One row of small action chips: a favorite button per person who hasn't
    already favorited this listing, plus a "not interested" button."""
    chips = []
    for person in people:
        if person in item.favorited_by:
            continue
        url = _favorite_url(item.listing, public_base_url, person)
        if url:
            chips.append(_chip(f"Favorite ({person})", icon="star_border", bg="#fff8e1", color="#8a6d00", href=url))
    if reject_url:
        chips.append(_chip("Not interested", icon="not_interested", bg="#f1f3f4", color="#5f6368", href=reject_url))
    if not chips:
        return ""
    return f'<tr><td style="padding:8px 20px 0 20px;">{"".join(chips)}</td></tr>'


def _row_html(item: EnrichedListing, public_base_url: str = "", people: list[str] | None = None) -> str:
    listing = item.listing
    people = people or []
    link_url = _tracked_url(listing, public_base_url)
    reject_url = _reject_url(listing, public_base_url)
    photo = listing.media.photo_urls[0] if listing.media.photo_urls else ""
    listed_date, days_listed = _days_listed(listing)
    listed_text = (
        f"Listed {listed_date} ({days_listed}d ago)" if listed_date else "Listed date unknown"
    )
    sold_text = (
        f"Last sold €{item.previous_sale.price:,} ({item.previous_sale.date})"
        if item.previous_sale
        else "No previous sale on record"
    )
    price = f"€{listing.price.amount:,}" if listing.price.amount else "price unknown"

    area_parts = []
    if listing.living_area:
        area_parts.append(f"{listing.living_area} m² living")
    if listing.plot_area:
        area_parts.append(f"{listing.plot_area} m² plot")
    area_text = " · ".join(area_parts)

    energy_label = listing.energy_label or "?"
    energy_bg = _energy_color(listing.energy_label)
    energy_text = _contrast_text_color(energy_bg)

    # Priority facts (what matters most): bedrooms + bike time to school,
    # shown first and bigger than everything else. Secondary distances
    # (station, fixed landmarks) are split out and shown further down.
    other_distances, school_entry = _split_school_distance(item.distances)

    key_chips = _chip(f"{listing.bedrooms} bedrooms", icon="bed")
    if school_entry:
        school_name, school_dist = school_entry
        school_href = _maps_directions_url(listing.location.coordinates, school_dist)
        school_label = (
            f"{school_dist.km:.1f} km ({school_dist.duration_text}) to school"
            if school_dist.duration_text
            else f"{school_dist.km:.1f} km to school"
        )
        key_chips += _chip(school_label, icon="bike", bg="#e6f4ea", color="#146c2e", href=school_href)
    key_chips += _chip(energy_label, bg=energy_bg, color=energy_text)

    within_range_badge = ""
    if item.school_distance_km is not None and item.max_school_distance_km is not None:
        within_range = item.school_distance_km <= item.max_school_distance_km
        within_range_badge = _chip(
            f"{'Within' if within_range else 'Outside'} {item.max_school_distance_km:g}km biking distance",
            icon="check" if within_range else None,
            bg="#e6f4ea" if within_range else "#fce8e6",
            color="#146c2e" if within_range else "#c5221f",
        )

    distance_chips = _distance_chips(other_distances, listing.location.coordinates)
    meta_chips = _chip(listed_text, icon="schedule") + _chip(sold_text, icon="sell")

    budget_chip = ""
    if item.mortgage_budget is not None and listing.price.amount is not None:
        margin = item.mortgage_budget - listing.price.amount
        budget_chip = _chip(
            f"€{margin:,} under budget (max €{item.mortgage_budget:,})",
            icon="check",
            bg="#e6f4ea",
            color="#146c2e",
        )

    def _group(chips_html: str) -> str:
        if not chips_html:
            return ""
        return (
            f'<div style="padding-top:10px;margin-top:10px;'
            f'border-top:1px solid #f1f3f4;">{chips_html}</div>'
        )

    groups = (
        f'<div>{key_chips}</div>'
        + (f'<div style="margin-top:6px;">{within_range_badge}</div>' if within_range_badge else '')
        + _group(distance_chips)
        + _group(meta_chips)
        + _group(budget_chip)
        + _group(_market_group(item))
    )

    accent_color = _school_proximity_color(item.school_distance_km, item.max_school_distance_km)

    border_style = (
        f"border-top:1px solid #eceff1;border-right:1px solid #eceff1;"
        f"border-bottom:1px solid #eceff1;border-left:6px solid {accent_color};"
        if accent_color
        else "border:1px solid #eceff1;"
    )

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(60,64,67,0.16),0 4px 12px rgba(60,64,67,0.10);{border_style}">
      <tr>
        <td style="position:relative;">
          <a href="{link_url}" style="text-decoration:none;">
            <img src="{photo}" width="600" style="display:block;width:100%;height:auto;max-height:280px;object-fit:cover;background:#f1f3f4;">
          </a>
          {'<div style="position:absolute;top:12px;left:12px;background:#1967d2;color:#ffffff;font-size:11px;font-weight:700;letter-spacing:0.5px;padding:5px 11px;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,0.35);">NEW</div>' if item.is_new else ''}
          {'<div style="position:absolute;top:12px;right:12px;background:#fbbc04;color:#202124;font-size:11px;font-weight:700;padding:5px 8px;border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,0.35);">' + _icon('star', 12) + '&#9733; ' + ', '.join(sorted(item.favorited_by)) + '</div>' if item.favorited_by else ''}
        </td>
      </tr>
      {f'<tr><td>{_price_drop_banner(item)}</td></tr>' if item.price_drop_from is not None else ''}
      {f'<tr><td style="padding:8px 20px 0 20px;">{_chip("Previously viewed", icon="check", bg="#e8eaed", color="#3c4043")}</td></tr>' if item.already_viewed else ''}
      {_favorite_and_reject_row(item, public_base_url, people, reject_url)}
      <tr>
        <td style="padding:18px 20px 20px 20px;font-family:{_FONT_STACK};">
          <a href="{link_url}" style="color:#202124;text-decoration:none;font-size:17px;font-weight:700;">{listing.title}</a>
          <div style="color:#5f6368;font-size:13px;margin:2px 0 10px 0;">{_icon("place", 12)}{listing.address.neighbourhood + ', ' if listing.address.neighbourhood else ''}{listing.city}</div>
          <div style="margin-bottom:12px;">
            <span style="color:{_price_budget_color(listing.price.amount, item.mortgage_budget)};font-size:22px;font-weight:700;">{price}</span>
            {f'<span style="color:#5f6368;font-size:13px;margin-left:8px;">{area_text}</span>' if area_text else ''}
            {f'<div style="color:{_price_budget_color(listing.price.amount, item.mortgage_budget)};font-size:12px;font-weight:600;margin-top:2px;">of €{item.mortgage_budget:,} budget</div>' if item.mortgage_budget is not None and listing.price.amount is not None else ''}
          </div>
          {groups}
        </td>
      </tr>
    </table>
    """


_SPACER = (
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
    '<tr><td height="24" style="font-size:0;line-height:0;">&nbsp;</td></tr>'
    "</table>"
)


def build_email_html(
    items: list[EnrichedListing], title: str, public_base_url: str = "", people: list[str] | None = None
) -> str:
    cards_html = _SPACER.join(_row_html(item, public_base_url, people) for item in items)
    return f"""
    <html>
    <body style="margin:0;padding:24px 12px;background:#f4f5f7;font-family:{_FONT_STACK};">
      <div style="max-width:600px;margin:0 auto;">
        <h1 style="font-size:20px;color:#202124;margin:0 0 4px 0;">{title}</h1>
        <p style="font-size:13px;color:#5f6368;margin:0;">{len(items)} matching listings</p>
      </div>
      {_SPACER}
      {cards_html}
    </body>
    </html>
    """


def send_email(subject: str, html: str, to_addresses: list[str]) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    from_address = os.environ.get("SMTP_FROM_ADDRESS", username)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Home Hunter Agent", from_address))
    msg["To"] = ", ".join(to_addresses)
    msg.set_content("This email requires an HTML-capable client to view listings.")
    msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(username, password)
        server.send_message(msg)
