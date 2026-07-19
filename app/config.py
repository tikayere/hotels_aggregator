"""Application configuration (contract-driven, read from the environment).

Reads exactly the variables documented in .env.example via pydantic-settings.
A couple of settings not present in the original template are added here with
safe development defaults and flagged in comments (JWT signing secret and the
dev shared webhook secret used by docker-compose.dev.yml); production must set
them explicitly.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Core service ---
    environment: str = "development"
    api_version: str = "v1"
    log_level: str = "info"

    # --- PostgreSQL ---
    database_url: str = "postgresql+asyncpg://aggregator:aggregator@localhost:5433/hotel_aggregator"

    # --- Redis (arq queue / hold-TTL tracking) ---
    redis_url: str = "redis://localhost:6380/0"

    # --- OpenSearch ---
    opensearch_url: str = "http://localhost:9201"
    opensearch_username: str | None = None
    opensearch_password: str | None = None
    opensearch_index: str = "room_offers"

    # --- Application-layer secret encryption for hotel_credential columns ---
    # Empty in .env.example: crypto.py falls back to an ephemeral dev key with a
    # loud warning when this is unset, so the app still boots for local dev.
    credential_encryption_key: str | None = None

    # --- Webhook receiver ---
    webhook_signature_header: str = "X-Hotel-Signature"
    webhook_timestamp_header: str = "X-Hotel-Timestamp"
    webhook_replay_window_seconds: int = 300
    # Fallback secret used only until a Hotel row (with a real per-hotel secret)
    # exists; matches docker-compose.dev.yml's aggregator service env.
    dev_shared_webhook_secret: str = "dev-secret-do-not-use-in-production"

    # --- Payment gateway (mock in this build, see app/services/payment.py) ---
    payment_gateway_api_key: str | None = None
    payment_gateway_webhook_secret: str | None = None

    # --- Sync Engine cadence ---
    reconciliation_poll_interval_seconds: int = 900
    reconciliation_full_resync_cron: str = "0 2 * * *"
    hotel_health_poll_interval_seconds: int = 60

    # --- Object storage / CDN ---
    photo_cdn_base_url: str | None = None

    # --- Outbound courtesy rate limit toward hotel ERPs ---
    outbound_calls_per_hotel_per_minute: int = 250

    # --- Reconciliation reliability tuning (NFR-B2) ---
    hotel_call_timeout_seconds: float = 10.0
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_cooldown_seconds: int = 300

    # --- Traveler auth (JWT) -- not in .env.example, dev default provided ---
    jwt_secret_key: str = "dev-jwt-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60 * 24

    @property
    def sync_database_url(self) -> str:
        """psycopg (sync) URL for any synchronous consumer (e.g. arq startup)."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
