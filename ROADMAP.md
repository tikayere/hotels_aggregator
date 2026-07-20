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
- **Webhook receiver forward compatibility (contract §4.3)** — found live,
  not by inspection: adding `hotel_erp`'s new `waitlist.available` event
  type initially got a hard `400` here, because the discriminated-union
  validation in `app/main.py` treated *any* unrecognized `event_type` as a
  malformed request. That's a real violation of "consumers must ignore
  unknown event types" — and it would have broken on *any* future event
  type this build doesn't know about yet, not just this one. Fixed: a
  Pydantic `union_tag_invalid` error (discriminator mismatch specifically,
  as opposed to a recognized event type with a genuinely malformed body) is
  now a `200` no-op, not a `400`. `WaitlistAvailableEvent` itself is also
  now a real schema (`app/schemas/webhooks.py`), dispatched as a no-op in
  `sync_engine.py` (same as `ReservationStatusEvent` — it exists for a
  future traveler notification, not built yet, see below).

- **FR-B1 Hotel Portal, self-service side** — a `HotelUser` model/table,
  scoped to exactly one hotel, with its own JWT scope (`hotel_portal`,
  distinct from a traveler's `traveler` scope and from the operator's
  `X-Admin-Api-Key` — a token minted for one is rejected by the other two's
  routes, checked server-side, verified live in both directions). Provisioned
  by the operator (`POST /admin/hotels/{slug}/users`), not self-registered —
  onboarding stays operator-mediated. New `app/api/routes/hotel_portal.py`:
  login, own profile (editable: display_name/city/star_rating/photos — never
  legal_name/slug/country/status/ERP credentials, which stay operator-only),
  sync-health, and full promotion CRUD (`Promotion`, a new
  Aggregator-owned model with no ERP equivalent). Room types and bookings are
  exposed read-only (NFR-B3 — this service is never authoritative for a
  hotel's inventory, and that now applies to what the portal is *allowed to
  write*, not just what the Sync Engine writes). `Hotel.photos` (marketing
  photos — exterior/lobby/amenities) is new too, distinct from
  `RoomTypeIndex.photos` which stays a read-only ERP mirror. A real
  frontend now exists for this — see the sibling `hotels` project's
  `portal/` (React/TypeScript), verified end-to-end against this API with a
  real headless-browser pass (login, dashboard, profile edit incl. photos,
  full promotion CRUD, logout), not just curl.
- **Error response shape, standardized across every client-facing route** —
  found while writing `openapi/aggregator-client-api.yaml` (the sibling
  `hotels` project, the shared contract for the portal/future web/mobile
  clients): most routes raised `HTTPException(status, detail="a string")`,
  producing `{"detail": "..."}`, while `bookings.py` raised
  `detail={"code":..., "message":...}`, producing a *differently shaped*
  `{"detail": {...}}` — two incompatible envelopes depending which endpoint
  failed, a real problem for any client written against one shape. Fixed
  with two global exception handlers in `app/main.py` that normalize both
  into the one envelope the webhook receiver's own `_error()` helper already
  used (`{"error": {"code", "message", "trace_id"}}`) — verified live across
  a plain-string 404, a dict-shaped booking error, a pydantic 422, and a
  manual 400, all now identically shaped.
- **CORS** — was entirely unconfigured (fine when nothing but curl/tests hit
  this API; not fine once a real browser-based client, the portal, needed
  to). `CORSMiddleware` added, origins configurable via
  `CORS_ALLOWED_ORIGINS` (defaults wide open — every route here already
  guards itself by bearer/admin-key auth or is public read data, not by
  request origin).
- **Reconciliation poller failure-handling crash, found and fixed** — found
  live while seeding real search data to verify the sibling `hotels`
  project's traveler-facing `web` app: any real `HotelERPError` (a hotel's
  ERP being unreachable, timing out, or 404ing) crashed
  `app/workers/reconciliation.py`'s except block itself
  (`sqlalchemy.exc.MissingGreenlet`) instead of recording the failure and
  moving on. Root cause: `except HotelERPError: await session.rollback();
  await _record_failure(session, sh)` — `rollback()` expires every ORM
  object the session was tracking, including `sh` (and `hotel`, read again
  inside the same handler for logging), so touching their attributes
  afterward triggers an implicit lazy-load that asyncpg's async session
  can't service outside an explicit await. This meant a real hotel-ERP
  outage would crash the exact job that exists to handle that gracefully
  (NFR-B2 — every hotel ERP is untrusted/unreliable) rather than recording
  `consecutive_failures`/`status=down` and moving on to the next hotel.
  Fixed by capturing `hotel_id`/`hotel_slug` as plain values before the try
  block and having `_record_failure` re-fetch `SyncHealth` fresh by id
  instead of accepting a possibly-stale instance — removes the whole class
  of bug, not just these two call sites. Verified live: a real 404 from a
  misconfigured hotel now correctly records a failure and returns cleanly
  instead of raising.
- **`Hotel.api_base_url` onboarding footgun, found and documented** — must
  include the `/api/v1` prefix (matching the contract's own server URL
  convention, `openapi/hotel-erp-api.yaml`'s `servers:` entry) — nothing
  validates or documents this, so a hotel onboarded with just the origin
  (`http://host:8080` instead of `http://host:8080/api/v1`) silently 404s
  on every reconciliation call. Not yet fixed with validation (worth adding
  either a startup-time check in `HotelCreate`/`HotelUpdate` or a clearer
  onboarding doc example) — found and worked around during testing, tracked
  here rather than fixed blind since the right fix (reject vs. auto-append)
  is a real design choice, not obvious enough to guess at.

## Implemented, but thinner than the spec describes

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
