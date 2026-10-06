import logging
import time

import httpx
from supabase import create_client, Client
from functools import lru_cache
from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache()
def get_supabase() -> Client:
    """
    Returns a cached Supabase client using the service role key.
    Service role bypasses RLS — only use on the backend, never expose to clients.
    """
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_key)


# ── Transient-failure retry ───────────────────────────────────────────────────
# WHY THIS EXISTS. On 5 Oct 2026 Supabase recycled an HTTP/2 connection and sent
# a routine GOAWAY frame (error_code 0 — "no error", the server simply closing
# an old connection). httpx does not transparently reopen and retry when that
# lands on an in-flight request; it raises:
#
#     httpx.RemoteProtocolError: <ConnectionTerminated error_code:0, ...>
#
# That morning it killed an inbound CV webhook, the trial-lead digest, the
# worker loop, and — eight minutes later — the Storage download of a client's
# CV builder, which silently degraded that CV to the generic formatter and put
# the candidate's mobile number and personal email in front of the client.
#
# The proper fix is to stop using HTTP/2 for these calls, which needs
# supabase-py >= 2.22.0 (on 2.9.1 a custom httpx client gets its base_url
# clobbered by each service and Storage calls start 404ing). Until that upgrade
# is tested, this retries.
#
# READS ONLY. Read the docstring before you wrap anything in it.

RETRYABLE = (httpx.TransportError,)


def db_retry(fn, *, attempts: int = 3, base_delay: float = 0.25, label: str = "Supabase read"):
    """
    Run a Supabase READ, retrying the transient transport failures that come
    from a connection being recycled underneath us.

    ONLY WRAP READS. Never wrap an insert, update, upsert or delete.

    A transport error means the request may or may not have reached the server —
    we cannot tell. Repeating a SELECT costs nothing. Repeating an INSERT that
    actually succeeded creates a duplicate row, and in this codebase a duplicate
    async_jobs row means the same CV is formatted and emailed to the client
    twice. If you need a retryable write, make it idempotent first (a unique
    constraint on the message id, or an upsert on a natural key).

    Total added latency is bounded at roughly base_delay * (2^(attempts-1) - 1)
    — about 0.75s at the defaults. That ceiling matters: get_organisation_by_domain
    runs inside the Postmark inbound webhook, and if we hold that request open
    past Postmark's timeout it redelivers the whole webhook and we get the
    duplicate we were trying to avoid.

    Args:
        fn:          zero-argument callable performing the read.
        attempts:    total tries, including the first.
        base_delay:  seconds before the second try; doubles each time after.
        label:       what to call this in the logs.

    Returns:
        Whatever fn() returns.

    Raises:
        The final exception if every attempt fails.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RETRYABLE as e:
            if attempt == attempts:
                logger.error(
                    f"{label} failed after {attempts} attempts: "
                    f"{type(e).__name__}: {e}"
                )
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                f"{label} hit {type(e).__name__} on attempt {attempt}/{attempts} "
                f"({e}). Retrying in {delay:.2f}s."
            )
            time.sleep(delay)
