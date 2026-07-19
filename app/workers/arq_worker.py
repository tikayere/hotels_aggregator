"""arq worker settings -- run with:  arq app.workers.arq_worker.WorkerSettings

Aggregates the Sync Engine's background jobs (contract section 3.3):
  * hold-expiry watcher (~every 30s)
  * reconciliation poller (room types, ~every 15m by default)
  * nightly full availability resync (cron, default 02:00)

Needs a new docker-compose service (aggregator-worker) with the same build/env
as the `aggregator` app service -- see the delivery report.
"""
from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings

from app.config import settings
from app.search.opensearch_client import close as close_search
from app.search.opensearch_client import ensure_index
from app.workers.hold_watch import expire_stale_holds
from app.workers.reconciliation import full_availability_resync, reconcile_room_types


def _cron_fields(expr: str) -> dict:
    """Parse a 5-field 'm h dom mon dow' cron string into arq cron() kwargs."""
    minute, hour, dom, mon, dow = expr.split()

    def _f(value: str):
        if value == "*":
            return None
        return {int(p) for p in value.split(",")}

    return {"minute": _f(minute), "hour": _f(hour), "day": _f(dom), "month": _f(mon), "weekday": _f(dow)}


async def startup(ctx: dict) -> None:
    await ensure_index()


async def shutdown(ctx: dict) -> None:
    await close_search()


_poll_minutes = max(settings.reconciliation_poll_interval_seconds // 60, 1)


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    functions = [reconcile_room_types, full_availability_resync, expire_stale_holds]
    cron_jobs = [
        cron(expire_stale_holds, second={0, 30}, run_at_startup=False),
        cron(reconcile_room_types, minute=set(range(0, 60, _poll_minutes)), run_at_startup=True),
        cron(full_availability_resync, **_cron_fields(settings.reconciliation_full_resync_cron)),
    ]
