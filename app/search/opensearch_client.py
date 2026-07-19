"""OpenSearch client + index bootstrap for the traveler search read model.

The search index is a denormalized join of room_type_index and
rate_availability_index: one document per (room type, night, rate plan), so a
date-range + city + price + amenities query is a single OpenSearch call and
never touches a Hotel ERP synchronously (NFR-B1).
"""
from __future__ import annotations

import logging

from opensearchpy import AsyncOpenSearch

from app.config import settings

logger = logging.getLogger("aggregator.search")

INDEX_NAME = settings.opensearch_index

INDEX_MAPPING = {
    "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
    "mappings": {
        "properties": {
            "global_room_type_id": {"type": "keyword"},
            "hotel_id": {"type": "keyword"},
            "hotel_slug": {"type": "keyword"},
            "hotel_display_name": {"type": "text"},
            "city": {"type": "keyword"},
            "country": {"type": "keyword"},
            "star_rating": {"type": "integer"},
            "room_type_name": {"type": "text"},
            "description": {"type": "text"},
            "max_occupancy_adults": {"type": "integer"},
            "max_occupancy_children": {"type": "integer"},
            "amenities": {"type": "keyword"},
            "photos": {"type": "keyword"},
            "active": {"type": "boolean"},
            "date": {"type": "date", "format": "yyyy-MM-dd"},
            "rate_plan_code": {"type": "keyword"},
            "refundable": {"type": "boolean"},
            "price_minor": {"type": "long"},
            "currency": {"type": "keyword"},
            "rooms_available": {"type": "integer"},
            "updated_at": {"type": "date"},
        }
    },
}

_client: AsyncOpenSearch | None = None


def get_client() -> AsyncOpenSearch:
    global _client
    if _client is None:
        kwargs: dict = {"hosts": [settings.opensearch_url], "timeout": 5, "max_retries": 2, "retry_on_timeout": True}
        if settings.opensearch_username and settings.opensearch_password:
            kwargs["http_auth"] = (settings.opensearch_username, settings.opensearch_password)
            kwargs["verify_certs"] = False
            kwargs["ssl_show_warn"] = False
        _client = AsyncOpenSearch(**kwargs)
    return _client


async def ensure_index() -> None:
    """Create the index with its mapping if it does not yet exist (idempotent)."""
    client = get_client()
    try:
        if not await client.indices.exists(index=INDEX_NAME):
            await client.indices.create(index=INDEX_NAME, body=INDEX_MAPPING)
            logger.info("Created OpenSearch index %s", INDEX_NAME)
    except Exception as exc:  # noqa: BLE001 - search must never block the write path
        logger.warning("Could not ensure OpenSearch index %s: %s", INDEX_NAME, exc)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
