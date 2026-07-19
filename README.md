# hotels_aggregator — Central Hospitality Platform (Service B)

A FastAPI application implementing the **central marketplace** side of a
two-service hotel booking ecosystem: cross-hotel discovery, search, booking
orchestration, and payments, sitting in front of many independent
[hotel_erp](https://github.com/tikayere/hotels_erp) deployments (one per
hotel). This service is never the source of truth for room inventory, guest
identity, or reservation status — it holds a cache of what each hotel
chooses to publish, plus its own marketplace-only data (traveler accounts,
the payment ledger, reviews).

The full design — requirements, architecture, database schema, and **the
wire contract** this app and every Hotel ERP instance both honor — lives in
the sibling `hotels` project's `phase_2_service_contracts.md` and
`IMPLEMENTATION_GUIDE.md`. This repo is the implementation of that
contract's Service B side; read those documents first if you're touching
`app/clients/hotel_erp_client.py`, `app/services/sync_engine.py`, or
anything under `app/webhooks/` — changes there are changes to a boundary
`hotel_erp` also depends on.

See [`ROADMAP.md`](ROADMAP.md) for what's implemented vs. what's still open.

## What's here

| Path | What it is |
|---|---|
| `app/main.py` | FastAPI app entrypoint — `/health`, the webhook receiver, and every router below. |
| `app/webhooks/verify.py`, `app/services/sync_engine.py` | HMAC verification (two-pass: resolve the hotel, then verify against *that* hotel's secret) and event dispatch into `room_type_index`/`rate_availability_index`/`sync_health`, deduped on `event_id`. |
| `app/clients/hotel_erp_client.py` | Typed async HTTP client for every endpoint a Hotel ERP exposes (contract §4.5), with idempotency-key generation and error-code mapping. |
| `app/services/orchestrator.py`, `app/api/routes/bookings.py` | The hold → pay → confirm/release state machine (contract §4.8), plus an independent hold-TTL watcher that doesn't rely solely on a callback from Service A. |
| `app/services/payment.py` | A mock payment gateway behind a small interface — the seam a real gateway (Stripe, etc.) plugs into later without the orchestrator needing to change. |
| `app/search/`, `app/api/routes/search.py` | OpenSearch-backed search — reads only from the local index, never calls a Hotel ERP synchronously on the read path. |
| `app/workers/reconciliation.py` | Per-hotel drift-correction poller with a circuit breaker, treating every Hotel ERP as untrusted/unreliable. |
| `app/security/crypto.py`, `auth.py` | Fernet encryption for stored hotel credentials; bcrypt + JWT for traveler accounts. |
| `Dockerfile` | Self-contained build, runs as a non-root user. Published to Docker Hub by `.github/workflows/docker-publish.yml` on every push to `main`. |

## Running it

The full two-service stack (this app + hotel_erp + all datastores) is
defined in the sibling `hotels` project's `docker-compose.dev.yml` (builds
this repo from source) and `docker-compose.prod.yml` (pulls the published
Docker Hub image) — start there.

To run just this app locally against its own dependencies:

```bash
cp .env.example .env   # fill in real values
docker build -t hotels_aggregator:local .
docker run --rm -p 8000:8000 --env-file .env hotels_aggregator:local
```

Or natively, for fast local iteration:

```bash
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8001
```

## Development notes

- **Never becomes authoritative for room inventory** (NFR-B3) — every write
  to `rate_availability_index` comes from a webhook or the reconciliation
  poller, never from a traveler-facing request. If you're adding a route
  that touches availability, it should be reading the index, not writing it
  outside those two paths.
- **Money** is an integer in minor currency units, never a float — same
  convention as `hotel_erp`, since both sides of the contract agree on it.
- **Guest identity documents never enter this service** (NFR-B6) — no
  passport/national-ID field should ever exist in this codebase. If you find
  yourself adding one, stop; it means the contract's been misread.
- **Payment data is isolated**: `app/services/payment.py`'s interface never
  accepts raw card fields, only an opaque payment method token — keep it
  that way when a real gateway replaces the mock.
