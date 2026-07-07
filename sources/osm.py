"""OpenStreetMap lead source: Nominatim (geocode) + Overpass (businesses).

Free, no API key, ToS-clean, global. Coverage is sparser than Google Maps
(many small businesses aren't tagged with a website), but it never gets
bot-blocked and is fully above-board.
"""
from __future__ import annotations

import asyncio

import httpx

from models import Business

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "leadgen-scraper/1.0 (personal outreach tool)"

# Common niche keyword -> OSM tag filter. Anything not listed falls back to a
# broad multi-key search (see _build_query).
NICHE_TAGS = {
    "dentist": '["amenity"="dentist"]',
    "doctor": '["amenity"="doctors"]',
    "pharmacy": '["amenity"="pharmacy"]',
    "restaurant": '["amenity"="restaurant"]',
    "cafe": '["amenity"="cafe"]',
    "coffee": '["amenity"="cafe"]',
    "bar": '["amenity"="bar"]',
    "hotel": '["tourism"="hotel"]',
    "gym": '["leisure"="fitness_centre"]',
    "fitness": '["leisure"="fitness_centre"]',
    "salon": '["shop"="hairdresser"]',
    "hairdresser": '["shop"="hairdresser"]',
    "barber": '["shop"="hairdresser"]',
    "beauty": '["shop"="beauty"]',
    "spa": '["leisure"="spa"]',
    "lawyer": '["office"="lawyer"]',
    "law firm": '["office"="lawyer"]',
    "accountant": '["office"="accountant"]',
    "real estate": '["office"="estate_agent"]',
    "estate agent": '["office"="estate_agent"]',
    "realtor": '["office"="estate_agent"]',
    "insurance": '["office"="insurance"]',
    "car repair": '["shop"="car_repair"]',
    "mechanic": '["shop"="car_repair"]',
    "car dealer": '["shop"="car"]',
    "florist": '["shop"="florist"]',
    "bakery": '["shop"="bakery"]',
    "butcher": '["shop"="butcher"]',
    "supermarket": '["shop"="supermarket"]',
    "clothing": '["shop"="clothes"]',
    "jewelry": '["shop"="jewelry"]',
    "optician": '["shop"="optician"]',
    "veterinary": '["amenity"="veterinary"]',
    "vet": '["amenity"="veterinary"]',
    "plumber": '["craft"="plumber"]',
    "electrician": '["craft"="electrician"]',
    "carpenter": '["craft"="carpenter"]',
    "photographer": '["craft"="photographer"]',
    "school": '["amenity"="school"]',
    "clinic": '["amenity"="clinic"]',
}


async def _geocode(location: str, client: httpx.AsyncClient) -> list[str] | None:
    """Return a bounding box [south, north, west, east] for a place name."""
    r = await client.get(
        NOMINATIM,
        params={"q": location, "format": "json", "limit": 1},
        headers={"User-Agent": UA},
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    return data[0]["boundingbox"]  # [south, north, west, east] as strings


def _build_query(niche: str, bbox: list[str], limit: int) -> str:
    south, north, west, east = bbox
    box = f"{south},{west},{north},{east}"
    key = niche.strip().lower()

    if key in NICHE_TAGS:
        selectors = [NICHE_TAGS[key]]
    else:
        # Broad fallback: match the term across the usual business tag keys,
        # plus a name match, so arbitrary niches still return something.
        v = key
        selectors = [
            f'["amenity"~"{v}",i]',
            f'["shop"~"{v}",i]',
            f'["office"~"{v}",i]',
            f'["craft"~"{v}",i]',
            f'["cuisine"~"{v}",i]',
            f'["name"~"{v}",i]',
        ]

    parts = []
    for sel in selectors:
        parts.append(f"nwr{sel}({box});")
    body = "\n  ".join(parts)
    return f"[out:json][timeout:60];\n(\n  {body}\n);\nout center tags {limit};"


def _tag(tags: dict, *keys: str) -> str:
    for k in keys:
        if tags.get(k):
            return tags[k]
    return ""


def _category(tags: dict) -> str:
    for k in ("amenity", "shop", "office", "craft", "leisure", "tourism"):
        if tags.get(k):
            return f"{k}={tags[k]}"
    return ""


def _address(tags: dict) -> str:
    parts = [
        " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x),
        tags.get("addr:city", ""),
        tags.get("addr:postcode", ""),
        tags.get("addr:country", ""),
    ]
    return ", ".join(p for p in parts if p)


async def fetch_businesses(query: str, location: str, limit: int) -> list[Business]:
    limits = httpx.Limits(max_keepalive_connections=0)
    async with httpx.AsyncClient(timeout=90, limits=limits) as client:
        bbox = await _geocode(location, client)
        if not bbox:
            print(f"[osm] Could not geocode location: {location!r}")
            return []
        overpass_q = _build_query(query, bbox, limit)
        # Nominatim asks for <=1 req/sec; small pause before Overpass is polite.
        await asyncio.sleep(1.0)
        r = await client.post(OVERPASS, data={"data": overpass_q}, headers={"User-Agent": UA})
        r.raise_for_status()
        elements = r.json().get("elements", [])

    businesses: list[Business] = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        if not name:
            continue
        socials = {
            "instagram": _tag(tags, "contact:instagram"),
            "facebook": _tag(tags, "contact:facebook"),
            "linkedin": _tag(tags, "contact:linkedin"),
        }
        b = Business(
            name=name,
            category=_category(tags),
            address=_address(tags),
            phone=_tag(tags, "phone", "contact:phone"),
            website=_tag(tags, "website", "contact:website", "url"),
            emails=[e for e in [_tag(tags, "email", "contact:email")] if e],
            instagram=socials["instagram"],
            facebook=socials["facebook"],
            linkedin=socials["linkedin"],
            source="osm",
        )
        businesses.append(b)
        if len(businesses) >= limit:
            break
    return businesses
