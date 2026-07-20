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

import stripe
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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


# Stripe's amount is in the currency's smallest unit for every currency
# except a fixed list of "zero-decimal" currencies, where the smallest unit
# IS the base unit (no /100). This codebase's own convention treats
# amount_minor uniformly (always the ERP/search-index price divided by
# nothing further), which matches Stripe's default for every currency this
# build has actually exercised (USD) but would silently overcharge 100x on
# a zero-decimal currency -- listed here so that's a loud, deliberate
# decision to revisit rather than a silent bug if one of these ever comes up.
# https://docs.stripe.com/currencies#zero-decimal
_STRIPE_ZERO_DECIMAL_CURRENCIES = frozenset({
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg",
    "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
})


class StripePaymentGateway(PaymentGateway):
    """Real gateway. Auth-then-capture (capture_method='manual' on create,
    a real .capture() call on confirm) so a card is verified/held at hold
    time without money actually moving until the booking is confirmed --
    avoids charging then immediately refunding on any failure in between.

    payment_method_token is a Stripe PaymentMethod id (pm_...) the client
    obtained via Stripe.js/Elements -- raw card data never reaches this
    service (NFR-B5), same guarantee the mock gateway already documented.
    """

    def __init__(self) -> None:
        self._client = stripe.StripeClient(settings.stripe_secret_key)

    def _stripe_amount(self, amount_minor: int, currency: str) -> int:
        if currency.lower() in _STRIPE_ZERO_DECIMAL_CURRENCIES:
            logger.warning(
                "Zero-decimal currency %s passed to Stripe as amount_minor=%s unmodified -- "
                "see _STRIPE_ZERO_DECIMAL_CURRENCIES's comment, this is almost certainly wrong.",
                currency, amount_minor,
            )
        return amount_minor

    async def create_intent(self, *, amount_minor: int, currency: str, payment_method_token: str) -> GatewayResult:
        try:
            intent = await self._client.v1.payment_intents.create_async({
                "amount": self._stripe_amount(amount_minor, currency),
                "currency": currency.lower(),
                "payment_method": payment_method_token,
                "capture_method": "manual",
                "confirm": True,
                "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
            })
        except stripe.error.CardError as exc:
            # A declined/failed card still creates a PaymentIntent on Stripe's
            # side -- surface its id as gateway_ref so it's traceable in the
            # Stripe dashboard even though this booking's hold gets released,
            # not because anything downstream uses it for a failed payment.
            pi_id = (exc.json_body or {}).get("error", {}).get("payment_intent", {}).get("id", "")
            return GatewayResult(gateway_ref=pi_id, success=False, message=str(exc))
        except stripe.error.StripeError as exc:
            logger.error("Stripe create_intent failed: %s", exc)
            return GatewayResult(gateway_ref="", success=False, message=str(exc))

        # requires_capture is the expected status for capture_method=manual
        # once Stripe has authorized the card; anything else (e.g.
        # requires_action, for 3DS) isn't handled by this build yet -- see
        # ROADMAP.md.
        success = intent.status == "requires_capture"
        return GatewayResult(gateway_ref=intent.id, success=success, message="" if success else f"unexpected status: {intent.status}")

    async def capture(self, *, gateway_ref: str) -> GatewayResult:
        try:
            intent = await self._client.v1.payment_intents.capture_async(gateway_ref)
        except stripe.error.StripeError as exc:
            logger.error("Stripe capture failed for %s: %s", gateway_ref, exc)
            return GatewayResult(gateway_ref=gateway_ref, success=False, message=str(exc))
        return GatewayResult(gateway_ref=intent.id, success=intent.status == "succeeded")

    async def refund(self, *, gateway_ref: str, amount_minor: int) -> GatewayResult:
        try:
            refund = await self._client.v1.refunds.create_async({
                "payment_intent": gateway_ref,
                "amount": amount_minor,
            })
        except stripe.error.StripeError as exc:
            logger.error("Stripe refund failed for %s: %s", gateway_ref, exc)
            return GatewayResult(gateway_ref=gateway_ref, success=False, message=str(exc))
        return GatewayResult(gateway_ref=refund.id, success=refund.status in ("succeeded", "pending"))


def get_payment_gateway() -> PaymentGateway:
    """Factory -- the single place that decides which gateway implementation is
    live. Falls back to the mock (with a loud warning) if no Stripe key is
    configured, so local dev/CI never needs a real Stripe account -- the
    same dev-default-with-loud-name pattern as CREDENTIAL_ENCRYPTION_KEY.
    """
    if not settings.stripe_secret_key:
        logger.warning(
            "STRIPE_SECRET_KEY is unset -- using MockPaymentGateway. Set a real "
            "(test-mode is fine) Stripe secret key before any non-dev use."
        )
        return MockPaymentGateway()
    return StripePaymentGateway()


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
