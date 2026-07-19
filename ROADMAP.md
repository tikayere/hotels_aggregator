# hotels_aggregator — Implementation status

Tracks what's actually built vs. what `phase_1.md`/`phase_2_service_contracts.md`
call for but this repo doesn't do yet. Written from the implementation and
verification work itself, not from a spec review.

## Fully implemented and verified

Everything in **contract §4** from this side: the webhook receiver (HMAC
verification, dedup on `event_id`, dispatch into the search index), the
reconciliation poller, the full booking orchestrator state machine
(hold → pay → confirm/release, including the independent hold-TTL watcher),
traveler accounts, and search. Verified end-to-end against a real running
`hotel_erp` instance — including a genuinely fresh boot from the published
Docker Hub images, not just against a mock.

- **FR-B1 admin auth** — `/api/v1/admin/hotels/*` now requires the
  `X-Admin-Api-Key` header (`app/api/deps.py::require_admin`), checked with a
  constant-time comparison against `ADMIN_API_KEY`. `docker-compose.prod.yml`
  requires a real value for it (and for `JWT_SECRET_KEY`, which had the same
  gap — silently defaulting to a value sitting in this repo's git history).
  `docker-compose.dev.yml` still runs on the code's built-in dev defaults,
  same as it already did for the JWT secret.
- **FR-B3 search — refundability** — `RateAvailabilityIndex.refundable` is
  now a real column, populated from the contract's `refundable` field
  (`NightlyAvailabilityData`/`AvailabilityQuoteLine`, both sides of the
  webhook and reconciliation paths) instead of inferred from rate-plan-code
  naming convention. The naming-convention guess still exists in
  `app/search/indexer.py` as a fallback for a hotel running an older
  `hotel_erp` that predates this field (`refundable: null` on the wire) —
  once every connected hotel is current, that fallback can be deleted.

## Implemented, but thinner than the spec describes

- **FR-B1 Hotel Portal** — onboarding CRUD and sync-health exist and are now
  authenticated, but there's still no promotions/photos management and no
  actual portal UI (this whole service is API-only, no frontend).
- **FR-B10 partner API** — the contract's own design has partners use the
  same API travelers do (§3.1), which this satisfies as-is, but there's no
  separate partner API-key issuance/rate-limiting tier distinct from
  traveler JWT auth. Fine today; worth revisiting if partner traffic
  patterns end up needing different limits than traveler traffic.

## Not implemented

- **FR-B5 real payment gateway** — `app/services/payment.py` is a mock that
  always succeeds; no Stripe/Adyen/etc. integration exists yet. The
  interface is deliberately shaped so a real gateway is a drop-in
  replacement (opaque payment-method token in, `payment_reference` +
  status out), but nothing beyond the mock has been built or tested.
- **FR-B9 analytics/benchmarking** — no anonymized cross-hotel
  occupancy/ADR/RevPAR benchmark endpoint exists at all.
- **Saved trips / notifications** (part of FR-B6) — traveler accounts,
  login, and booking history work; "saved trips" and any notification
  delivery (email/push) aren't built.
- **No automated test suite.** Same situation as `hotel_erp`: verification
  has been real and end-to-end (including a from-scratch Docker Hub image
  boot), but there's no `pytest` suite checked in and CI only builds and
  pushes, it doesn't test first.
- Everything in the contract's own **§6 "Open Items for Future Phases"**:
  channel-manager/OTA integration, Event Marketplace, loyalty platform,
  corporate booking, multi-currency/multi-country tax handling. Explicitly
  deferred at the design stage.
