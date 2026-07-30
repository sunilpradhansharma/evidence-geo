"""Generic Snowflake-first / SQLite-fallback helper.

Every analytics endpoint calls ``with_snowflake_fallback`` which:
1. Checks if Snowflake is enabled.
2. Tries the Snowflake query function.
3. On *any* failure (disabled, network, SQL error) falls back to the SQLite
   implementation and logs a warning so the failure is visible.

The caller receives ``(result, source)`` where *source* is ``"snowflake"`` or
``"sqlite"`` — surfaced via the ``X-Data-Source`` response header.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.snowflake.client import SnowflakeDisabled, is_enabled
from app.utils.logging import get_logger

logger = get_logger("snowflake.fallback")


async def with_snowflake_fallback(
    sf_fn: Callable[..., Awaitable[Any]],
    sqlite_fn: Callable[..., Awaitable[Any]],
    *args: Any,
    **kwargs: Any,
) -> tuple[Any, str]:
    """Try *sf_fn* first; on any error fall back to *sqlite_fn*.

    The Snowflake callable (``sf_fn``) is **always called with no arguments** —
    it uses the Snowflake client internally. The SQLite fallback (``sqlite_fn``)
    receives the forwarded ``*args``/``**kwargs`` (typically a db session).

    Returns
    -------
    (result, source)
        *source* is ``"snowflake"`` or ``"sqlite"``.
    """
    if not is_enabled():
        return await sqlite_fn(*args, **kwargs), "sqlite"

    try:
        result = await sf_fn()
        return result, "snowflake"
    except SnowflakeDisabled:
        logger.info("Snowflake disabled — falling back to SQLite")
    except NotImplementedError as e:
        logger.debug("Snowflake view not yet defined (%s) — falling back to SQLite", e)
    except Exception:  # noqa: BLE001
        logger.warning("Snowflake query failed — falling back to SQLite", exc_info=True)

    return await sqlite_fn(*args, **kwargs), "sqlite"
