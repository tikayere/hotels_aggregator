"""Payment Service (FR-B5) -- isolated, PCI-boundary-respecting subsystem.

The Orchestrator talks only to PaymentService and never learns which gateway is
behind it. Swapping in a real gateway (Stripe, etc.) later touches ONLY the
`PaymentGateway` implementation + `get_payment_gateway()` factory in this file.

Raw card data never reaches this service (NFR-B5): the interface accepts an
opaque `payment_method_token`, never PAN/CVV/expiry fields.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BookingReference, PaymentStatus, PaymentTransaction

logger = logging.getLogger("aggregator.payment")


@dataclass
class GatewayResult:
    gateway_ref: str
    success: bool
    message: str = ""


class PaymentGateway(ABC):
    """The seam a real gateway plugs into. All methods take an opaque token or a
    prior gateway_ref -- never card fields.
    """

    @abstractmethod
    async def create_intent(self, *, amount_minor: int, currency: str, payment_method_token: str) -> GatewayResult:
        ...

    @abstractmethod
    async def capture(self, *, gateway_ref: str) -> GatewayResult:
        ...

    @abstractmethod
    async def refund(self, *, gateway_ref: str, amount_minor: int) -> GatewayResult:
        ...


class MockPaymentGateway(PaymentGateway):
    """Deterministic sandbox gateway. Succeeds unless the opaque token equals the
    magic value 'tok_fail' (so tests can force a decline without card data).
    Emits real-looking 'mock-<uuid>' references. NOT for production use.
    """

    FAIL_TOKEN = "tok_fail"
    _LATENCY_SECONDS = 0.01

    async def create_intent(self, *, amount_minor: int, currency: str, payment_method_token: str) -> GatewayResult:
        await asyncio.sleep(self._LATENCY_SECONDS)
        ref = f"mock-{uuid.uuid4()}"
        if payment_method_token == self.FAIL_TOKEN:
            return GatewayResult(gateway_ref=ref, success=False, message="card_declined (mock)")
        return GatewayResult(gateway_ref=ref, success=True)

    async def capture(self, *, gateway_ref: str) -> GatewayResult:
        await asyncio.sleep(self._LATENCY_SECONDS)
        return GatewayResult(gateway_ref=gateway_ref, success=True)

    async def refund(self, *, gateway_ref: str, amount_minor: int) -> GatewayResult:
        await asyncio.sleep(self._LATENCY_SECONDS)
        return GatewayResult(gateway_ref=f"{gateway_ref}-refund", success=True)


def get_payment_gateway() -> PaymentGateway:
    """Factory -- the single place that decides which gateway implementation is
    live. Swap the return value to introduce a real provider.
    """
    return MockPaymentGateway()


class PaymentService:
    def __init__(self, gateway: PaymentGateway | None = None):
        self._gateway = gateway or get_payment_gateway()

    async def create_and_capture(
        self,
        session: AsyncSession,
        *,
        booking: BookingReference,
        amount_minor: int,
        currency: str,
        payment_method_token: str,
    ) -> PaymentTransaction:
        """Create an intent, capture it, persist a PaymentTransaction, and link
        it to the booking. Status reflects the deterministic gateway outcome.
        """
        intent = await self._gateway.create_intent(
            amount_minor=amount_minor, currency=currency, payment_method_token=payment_method_token
        )
        status = PaymentStatus.pending
        gateway_ref = intent.gateway_ref
        if intent.success:
            capture = await self._gateway.capture(gateway_ref=intent.gateway_ref)
            status = PaymentStatus.succeeded if capture.success else PaymentStatus.failed
        else:
            status = PaymentStatus.failed

        txn = PaymentTransaction(
            agg_booking_id=booking.agg_booking_id,
            amount_minor=amount_minor,
            currency=currency,
            gateway_ref=gateway_ref,
            status=status,
        )
        session.add(txn)
        await session.flush()
        if status == PaymentStatus.succeeded:
            booking.payment_id = txn.payment_id
        await session.commit()
        return txn

    async def refund(self, session: AsyncSession, *, payment: PaymentTransaction, amount_minor: int) -> PaymentTransaction:
        result = await self._gateway.refund(gateway_ref=payment.gateway_ref, amount_minor=amount_minor)
        if result.success:
            payment.status = PaymentStatus.refunded
        await session.commit()
        return payment
