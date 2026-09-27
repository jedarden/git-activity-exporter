"""Small, deterministic retry policy for remote operations.

The caller decides which exceptions are transient. Keeping the attempt and
backoff policy here prevents Forgejo, Git, and S3 from silently acquiring
different retry behavior.
"""
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

# Three total attempts keeps a bad cycle bounded while allowing a short-lived
# service interruption to recover. The delays deliberately have no jitter:
# there is one exporter writer and deterministic delays make cycle timing and
# tests auditable.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 2)


def call(operation: Callable, *, is_retryable: Callable[[Exception], bool], label: str):
    """Run one remote operation with the shared bounded retry policy.

    ``is_retryable`` is intentionally supplied by the transport adapter. A
    validation error, authentication failure, or corrupt local mirror must
    not be retried just because it happened inside a remote-operation helper.
    The original exception is re-raised after the final attempt.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as error:
            if attempt >= MAX_ATTEMPTS or not is_retryable(error):
                raise
            delay = BACKOFF_SECONDS[attempt - 1]
            log.warning(
                "transient failure during %s (attempt %d/%d); retrying in %ss: %s",
                label, attempt, MAX_ATTEMPTS, delay, error,
            )
            time.sleep(delay)
