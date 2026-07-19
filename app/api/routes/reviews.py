"""Review Service (FR-B7). Entirely Aggregator-owned, no Service A dependency."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_traveler
from app.db.models import BookingReference, Hotel, Review, TravelerAccount
from app.db.session import get_session
from app.schemas.api import ReviewCreate, ReviewOut

router = APIRouter(prefix="/api/v1", tags=["reviews"])


def _to_out(r: Review) -> ReviewOut:
    return ReviewOut(
        review_id=r.review_id,
        agg_booking_id=r.agg_booking_id,
        hotel_id=r.hotel_id,
        rating=r.rating,
        comment=r.comment,
        created_at=r.created_at,
    )


@router.post("/reviews", response_model=ReviewOut, status_code=201)
async def create_review(
    payload: ReviewCreate,
    traveler: TravelerAccount = Depends(get_current_traveler),
    session: AsyncSession = Depends(get_session),
) -> ReviewOut:
    booking = await session.get(BookingReference, payload.agg_booking_id)
    if booking is None or booking.traveler_id != traveler.traveler_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Booking not found")

    review = Review(
        agg_booking_id=payload.agg_booking_id,
        hotel_id=booking.hotel_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    session.add(review)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="A review already exists for this booking")
    return _to_out(review)


@router.get("/hotels/{slug}/reviews", response_model=list[ReviewOut])
async def hotel_reviews(slug: str, session: AsyncSession = Depends(get_session)) -> list[ReviewOut]:
    hotel = await session.scalar(select(Hotel).where(Hotel.slug == slug))
    if hotel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Hotel not found")
    rows = await session.scalars(
        select(Review).where(Review.hotel_id == hotel.hotel_id).order_by(Review.created_at.desc())
    )
    return [_to_out(r) for r in rows]
