"""Traveler search (FR-B3). Reads ONLY from OpenSearch -- never calls a Hotel
ERP synchronously on the read path (NFR-B1).

The index holds one document per (room type, night, rate plan). A stay search
fetches every matching night in [check_in, check_out), then keeps only
(room type, rate plan) combinations that have availability for *every* night,
summing the nightly prices into a stay total.

check_in/check_out are optional so a client can offer a browse-first
experience (list what's available, then let the traveler narrow by dates/
city/etc. incrementally) instead of forcing a full form before showing
anything -- every other filter was already optional. Omitting one or both
defaults to a representative 1-night window starting tomorrow, which is
enough to show a real per-night price without requiring the caller to guess
a stay length that means anything.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

from app.schemas.api import SearchResponse, SearchResultItem
from app.search.opensearch_client import INDEX_NAME, get_client

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search(
    check_in: date | None = Query(None),
    check_out: date | None = Query(None),
    city: str | None = None,
    adults: int = Query(1, ge=1),
    children: int = Query(0, ge=0),
    rooms: int = Query(1, ge=1),
    min_price_minor: int | None = Query(None, ge=0),
    max_price_minor: int | None = Query(None, ge=0),
    min_star_rating: int | None = Query(None, ge=1, le=5),
    amenities: list[str] | None = Query(None),
    refundable: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> SearchResponse:
    if check_in is None:
        check_in = date.today() + timedelta(days=1)
    if check_out is None:
        check_out = check_in + timedelta(days=1)

    nights = (check_out - check_in).days
    if nights <= 0:
        raise HTTPException(400, detail="check_out must be after check_in")

    filters: list[dict] = [
        {"term": {"active": True}},
        {"range": {"date": {"gte": check_in.isoformat(), "lt": check_out.isoformat()}}},
        {"range": {"rooms_available": {"gte": rooms}}},
        {"range": {"max_occupancy_adults": {"gte": adults}}},
        {"range": {"max_occupancy_children": {"gte": children}}},
    ]
    if city:
        filters.append({"term": {"city": city}})
    if min_star_rating is not None:
        filters.append({"range": {"star_rating": {"gte": min_star_rating}}})
    if refundable is not None:
        filters.append({"term": {"refundable": refundable}})
    if amenities:
        for amenity in amenities:
            filters.append({"term": {"amenities": amenity}})

    body = {"size": 2000, "query": {"bool": {"filter": filters}}}

    try:
        resp = await get_client().search(index=INDEX_NAME, body=body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, detail=f"Search backend unavailable: {exc}")

    # Group nightly docs by (room type, rate plan); keep only combinations that
    # cover every night of the stay, and sum the nightly prices.
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"dates": set(), "total": 0, "min_rooms": None, "src": None}
    )
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit["_source"]
        key = (src["global_room_type_id"], src["rate_plan_code"])
        g = groups[key]
        if src["date"] in g["dates"]:
            continue
        g["dates"].add(src["date"])
        g["total"] += src["price_minor"]
        g["min_rooms"] = src["rooms_available"] if g["min_rooms"] is None else min(g["min_rooms"], src["rooms_available"])
        g["src"] = src

    results: list[SearchResultItem] = []
    for (room_type_id, rate_plan_code), g in groups.items():
        if len(g["dates"]) != nights:
            continue  # not available for the whole stay
        total = g["total"]
        if min_price_minor is not None and total < min_price_minor:
            continue
        if max_price_minor is not None and total > max_price_minor:
            continue
        s = g["src"]
        results.append(
            SearchResultItem(
                global_room_type_id=room_type_id,
                hotel_slug=s["hotel_slug"],
                hotel_display_name=s["hotel_display_name"],
                city=s["city"],
                country=s["country"],
                star_rating=s.get("star_rating"),
                room_type_name=s["room_type_name"],
                amenities=s.get("amenities", []),
                photos=s.get("photos", []),
                total_price_minor=total,
                currency=s["currency"],
                nights=nights,
                rate_plan_code=rate_plan_code,
                refundable=s.get("refundable", True),
                min_rooms_available=g["min_rooms"],
            )
        )

    results.sort(key=lambda r: r.total_price_minor)
    results = results[:limit]
    return SearchResponse(results=results, count=len(results))
