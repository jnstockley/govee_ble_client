"""Private metadata-only logging helpers; configuration belongs to the host."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from itertools import count
from time import monotonic
from typing import Any

_CONNECTION_IDS = count(1)


def new_connection_id() -> str:
    """Return a process-local token unrelated to a peripheral identifier."""
    return f"connection-{next(_CONNECTION_IDS)}"


class _ConnectionLogger(logging.LoggerAdapter):
    """Include correlation in both ordinary formatters and structured records."""

    def process(
        self, msg: Any, kwargs: Mapping[str, Any]
    ) -> tuple[Any, Mapping[str, Any]]:
        msg, kwargs = super().process(msg, kwargs)
        return f"[{self.extra['connection_id']}] {msg}", kwargs


def connection_logger(
    logger: logging.Logger, connection: object
) -> logging.LoggerAdapter:
    """Share a GATT handle's token, or allocate one for an injected transport."""
    token = getattr(connection, "_log_id", None) or new_connection_id()
    return _ConnectionLogger(logger, {"connection_id": token})


@contextmanager
def log_operation(
    logger: logging.Logger | logging.LoggerAdapter,
    operation: str,
    timeout: float | None,
) -> Iterator[None]:
    """Time an operation without logging exception text, frames, or arguments."""
    started = monotonic()
    logger.debug("%s started timeout=%s", operation, timeout)
    try:
        yield
    except BaseException as error:
        # Backend exception messages and tracebacks may include device addresses
        # or traffic. Preserve the original exception solely for the caller.
        logger.debug(
            "%s failed error=%s elapsed=%.3fs timeout=%s",
            operation,
            type(error).__name__,
            monotonic() - started,
            timeout,
        )
        raise
    else:
        logger.debug("%s completed elapsed=%.3fs", operation, monotonic() - started)
