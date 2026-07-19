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

## Implemented, but thinner than the spec describes

- **FR-B1 Hotel Portal** — onboarding CRUD (`app/api/routes/hotels_admin.py`)
  and sync-health exist, but there's **no auth guard on the admin hotel
  routes at all** — anyone who can reach the API can register/edit/delete a
  hotel's credentials. This needs fixing before this is exposed anywhere
  outside a trusted network; it's a known gap, not an oversight that slipped
  through review. There's also no promotions/photos management and no
  actual portal UI (this whole service is API-only, no frontend).
- **FR-B3 search — refundability** — `rate_plan_code` is the only rate-plan
  signal in the webhook/reconciliation payloads (the contract doesn't carry
  a `refundable` boolean over the wire), so the search index infers
  refundability from naming convention (codes containing `NONREF`,
  `ADVANCE`, etc.). If a hotel's actual rate-plan codes don't follow that
  convention, this filter will be wrong for them. The real fix is a contract
  change (carry `refundable` in the payload), not something fixable
  unilaterally on this side.
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
