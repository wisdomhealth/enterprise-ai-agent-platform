# PostgreSQL connection budget

FastAPI and Celery have intentionally different database connection lifetimes.
FastAPI uses the bounded SQLAlchemy pool configured by `DATABASE_POOL_SIZE` and
`DATABASE_MAX_OVERFLOW`. Celery uses `NullPool`: every synchronous task enters a
new `asyncio.run()` event loop, opens a connection for that invocation, and
closes it before the loop ends. This prevents asyncpg connections created in one
loop or prefork child from being reused in another.

Before deployment, set the following values to reflect the actual worker
topology. Startup rejects a configuration whose calculated maximum exceeds
`POSTGRES_MAX_CONNECTIONS`:

```text
FastAPI = API_PROCESS_COUNT * (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)
Celery  = CELERY_WORKER_INSTANCES * CELERY_WORKER_CONCURRENCY * CELERY_TASK_CONNECTION_PEAK
Total   = FastAPI + Celery + POSTGRES_ADMIN_MIGRATION_RESERVE
```

`CELERY_TASK_CONNECTION_PEAK` is at least two because email history processing
holds its work session while its lease-renewal coroutine opens a second session.
The reserve must cover migrations, administrators, recovery tooling, and a
small operational margin. Increasing PostgreSQL `max_connections` alone is not
a substitute for reducing an unsafe application budget.
